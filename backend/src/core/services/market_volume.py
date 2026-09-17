"""沪深两市分时成交额对比（缩放量）。

数据源：tushare 指数分钟线。历史分钟用 idx_mins，当天盘中尚未入库的分钟用 rt_idx_min 补齐。
沪+深成交额 = 上证指数 000001.SH 成交额 + 深证成指 399001.SZ 成交额。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .tushare import TushareService


logger = logging.getLogger(__name__)

SH_TS_CODE = "000001.SH"
SZ_TS_CODE = "399001.SZ"
MAX_TRADE_DAYS = 6  # 5 个可选目标日 + 最早一天作为前收基准
HISTORY_LOOKBACK_CALENDAR_DAYS = 12
MARKET_CLOSE_MINUTE = "15:00"
CACHE_TTL_SECONDS = 20
YI = 1e8

MinuteRows = Dict[str, List[Tuple[str, float, float]]]
_cache: Dict[str, Tuple[float, MinuteRows]] = {}


class MarketVolumeDataError(RuntimeError):
    pass


def frame_to_minute_rows(frame: Optional[pd.DataFrame]) -> MinuteRows:
    """按交易日分组：{date: [(HH:MM, close, amount), ...]}，同一分钟保留最后一条，保持时间顺序。"""
    if frame is None or frame.empty:
        return {}
    by_date: Dict[str, Dict[str, Tuple[float, float]]] = {}
    for row in frame.sort_values("trade_time").itertuples(index=False):
        stamp = pd.Timestamp(row.trade_time)
        by_date.setdefault(stamp.strftime("%Y-%m-%d"), {})[stamp.strftime("%H:%M")] = (
            float(row.close),
            float(row.amount),
        )
    return {
        date: [(minute, close, amount) for minute, (close, amount) in sorted(minutes.items())]
        for date, minutes in by_date.items()
    }


def merge_minute_rows(history: MinuteRows, realtime: MinuteRows) -> MinuteRows:
    """实时分钟只补历史里没有的分钟，不覆盖已入库数据。"""
    merged = {date: list(rows) for date, rows in history.items()}
    for date, rows in realtime.items():
        existing = {minute for minute, _, _ in merged.get(date, [])}
        extra = [row for row in rows if row[0] not in existing]
        if extra:
            merged[date] = sorted(merged.get(date, []) + extra)
    return merged


def _fetch_index_minutes(ts_code: str, now: Optional[datetime] = None) -> MinuteRows:
    cached = _cache.get(ts_code)
    now_ts = time.time()
    if cached and now_ts - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    now = now or datetime.now()
    service = TushareService.get_instance()
    start = (now - timedelta(days=HISTORY_LOOKBACK_CALENDAR_DAYS)).replace(hour=9, minute=0, second=0, microsecond=0)
    try:
        history = frame_to_minute_rows(service.get_index_historical_minute_frame(ts_code, start, now))
    except Exception as exc:
        raise MarketVolumeDataError(f"tushare idx_mins 获取 {ts_code} 分钟线失败: {exc}") from exc

    today = now.strftime("%Y-%m-%d")
    today_rows = history.get(today) or []
    realtime: MinuteRows = {}
    if now.weekday() < 5 and (not today_rows or today_rows[-1][0] < MARKET_CLOSE_MINUTE):
        try:
            realtime = frame_to_minute_rows(service.get_index_realtime_minute_frame(ts_code))
        except Exception as exc:  # 盘中实时补齐失败时仍返回历史数据
            logger.warning("tushare rt_idx_min 获取 %s 失败: %s", ts_code, exc)

    rows = merge_minute_rows(history, realtime)
    if not rows:
        raise MarketVolumeDataError(f"tushare 未返回 {ts_code} 分钟线")
    _cache[ts_code] = (now_ts, rows)
    return rows


def _yi(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value / YI, 2)


def build_volume_compare(
    sh: MinuteRows,
    sz: MinuteRows,
    target_date: Optional[str] = None,
    compare_date: Optional[str] = None,
) -> Dict[str, Any]:
    dates = sorted(set(sh) & set(sz))[-MAX_TRADE_DAYS:]
    if len(dates) < 2:
        raise MarketVolumeDataError("分时数据不足两个交易日，无法对比")

    # 目标日需要有前一交易日（用于对比量与涨跌幅基准），所以最早一天不能作为目标日
    selectable = dates[1:]
    target = target_date or selectable[-1]
    if target not in selectable:
        raise ValueError(f"目标日期不可选：{target}，可选 {', '.join(selectable)}")
    target_index = dates.index(target)
    compare = compare_date or dates[target_index - 1]
    if compare not in dates or compare == target:
        raise ValueError(f"对比日期不可选：{compare}")

    prev_date = dates[target_index - 1]
    sh_prev_close = sh[prev_date][-1][1]
    sz_prev_close = sz[prev_date][-1][1]

    def _amount_map(date: str) -> Dict[str, float]:
        sz_amount = {minute: amount for minute, _, amount in sz[date]}
        return {minute: amount + sz_amount[minute] for minute, _, amount in sh[date] if minute in sz_amount}

    target_amounts = _amount_map(target)
    compare_amounts = _amount_map(compare)
    sh_close = {minute: close for minute, close, _ in sh[target]}
    sz_close = {minute: close for minute, close, _ in sz[target]}

    grid = sorted(set(compare_amounts) | set(target_amounts))
    points: List[Dict[str, Any]] = []
    target_cum = 0.0
    compare_cum = 0.0
    last_time = None
    compare_same_time_cum = 0.0
    for minute in grid:
        target_amount = target_amounts.get(minute)
        compare_amount = compare_amounts.get(minute)
        if compare_amount is not None:
            compare_cum += compare_amount
        has_target = target_amount is not None
        if has_target:
            target_cum += target_amount
            last_time = minute
            compare_same_time_cum = compare_cum
        deviation = None
        if has_target and compare_amount:
            deviation = round((target_amount / compare_amount - 1) * 100, 2)
        sh_pct = (sh_close[minute] / sh_prev_close - 1) * 100 if minute in sh_close and sh_prev_close else None
        sz_pct = (sz_close[minute] / sz_prev_close - 1) * 100 if minute in sz_close and sz_prev_close else None
        points.append({
            "time": minute,
            "target_amount": _yi(target_amount),
            "compare_amount": _yi(compare_amount),
            "target_cum": _yi(target_cum) if has_target else None,
            "compare_cum": _yi(compare_cum) if compare_amount is not None else None,
            "diff_cum": _yi(target_cum - compare_cum) if has_target else None,
            "deviation_pct": deviation,
            "sh_pct": None if sh_pct is None else round(sh_pct, 3),
            "sz_pct": None if sz_pct is None else round(sz_pct, 3),
        })

    diff = target_cum - compare_same_time_cum
    return {
        "source": "tushare idx_mins / rt_idx_min（上证指数 + 深证成指成交额）",
        "dates": dates,
        "selectable_dates": list(reversed(selectable)),
        "target_date": target,
        "compare_date": compare,
        "is_intraday": bool(last_time) and last_time < MARKET_CLOSE_MINUTE,
        "last_time": last_time,
        "target_total": _yi(target_cum),
        "compare_same_time_total": _yi(compare_same_time_cum),
        "compare_full_total": _yi(compare_cum),
        "diff": _yi(diff),
        "diff_pct": round(diff / compare_same_time_cum * 100, 2) if compare_same_time_cum else None,
        "points": points,
    }


def fetch_intraday_volume_compare(
    target_date: Optional[str] = None,
    compare_date: Optional[str] = None,
) -> Dict[str, Any]:
    sh = _fetch_index_minutes(SH_TS_CODE)
    sz = _fetch_index_minutes(SZ_TS_CODE)
    result = build_volume_compare(sh, sz, target_date=target_date, compare_date=compare_date)
    result["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result
