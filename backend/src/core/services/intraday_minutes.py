"""个股分时小图数据：库里分钟历史 + 当天实时补齐。

口径与缠论分钟图完全一致，直接复用 chan_minute_data 的四个函数：
`load_minute_rows` 读 DuckDB 的前复权分钟线，`is_complete_a_share_minute_day`
判断当天是否已经入库完整，不完整时用 `fetch_realtime_minute_rows`（rt_min_daily，
一次返回当日全部分钟）补齐，再用 `merge_minute_rows` 合并（实时覆盖同一分钟）。

只在用户点开某只股票时按需调用，不进盘中轮询，所以对 tushare 的压力是"看几只拉几只"。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from .chan_minute_data import (
    fetch_realtime_minute_rows,
    is_complete_a_share_minute_day,
    load_minute_rows,
    merge_minute_rows,
)


logger = logging.getLogger(__name__)

MARKET_OPEN = dtime(9, 30)
MAX_DAYS = 10
CALENDAR_DAYS_PER_TRADING_DAY = 2   # 取交易日要多留日历天数（周末与节假日）


def _safe_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def group_by_day(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按交易日分组，每组内按时间升序。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        timestamp = pd.to_datetime(row.get("timestamp"), errors="coerce")
        if pd.isna(timestamp):
            continue
        close = _safe_number(row.get("close"))
        if close is None:
            continue
        grouped.setdefault(timestamp.strftime("%Y-%m-%d"), []).append({
            "time": timestamp.strftime("%H:%M"),
            "close": round(close, 3),
            "volume": _safe_number(row.get("volume")) or 0.0,
            "amount": _safe_number(row.get("turnover")) or 0.0,
        })
    return {day: sorted(items, key=lambda item: item["time"]) for day, items in grouped.items()}


def build_minute_series(rows: List[Dict[str, Any]], days: int) -> Dict[str, Any]:
    """把分钟行整理成最近 days 个交易日的分时序列，并带上每日前收。

    前收用上一个交易日的最后一根收盘价；因此实际多取一天，仅用来给第一天做基准。
    """
    grouped = group_by_day(rows)
    all_days = sorted(grouped)
    if not all_days:
        return {"days": [], "points": [], "today": None}

    kept = all_days[-days:]
    day_infos: List[Dict[str, Any]] = []
    points: List[Dict[str, Any]] = []
    for day in kept:
        index = all_days.index(day)
        prev_day = all_days[index - 1] if index > 0 else None
        pre_close = grouped[prev_day][-1]["close"] if prev_day else grouped[day][0]["close"]
        bars = grouped[day]
        cum_volume = 0.0
        cum_amount = 0.0
        start = len(points)
        for bar in bars:
            cum_volume += bar["volume"]
            cum_amount += bar["amount"]
            points.append({
                "date": day,
                "time": bar["time"],
                "close": bar["close"],
                "pct": round((bar["close"] / pre_close - 1) * 100, 3) if pre_close else None,
                "volume": bar["volume"],
                "amount": bar["amount"],
            })
        last = bars[-1]
        day_infos.append({
            "date": day,
            "pre_close": pre_close,
            "start_index": start,
            "count": len(bars),
            "close": last["close"],
            "pct": round((last["close"] / pre_close - 1) * 100, 2) if pre_close else None,
            "volume": round(cum_volume, 2),
            "amount_yi": round(cum_amount / 1e8, 3),
        })
    return {"days": day_infos, "points": points, "today": day_infos[-1]["date"] if day_infos else None}


def fetch_intraday_minutes(ts_code: str, days: int = 5, now: Optional[datetime] = None) -> Dict[str, Any]:
    """某只股票最近 days 个交易日的分时（含当日实时补齐）。"""
    symbol = str(ts_code or "").strip().upper()
    if not symbol:
        raise ValueError("股票代码不能为空")
    days = max(1, min(int(days or 5), MAX_DAYS))
    now = now or datetime.now()
    today = now.date()

    # 多取一天用来给第一天算前收
    start_date = today - timedelta(days=(days + 1) * CALENDAR_DAYS_PER_TRADING_DAY + 5)
    rows = load_minute_rows(
        symbol,
        datetime.combine(start_date, datetime.min.time()),
        datetime.combine(today, datetime.max.time()),
    )

    realtime_merged = False
    today_complete = is_complete_a_share_minute_day(rows, today)
    if today.weekday() < 5 and now.time() >= MARKET_OPEN and not today_complete:
        try:
            realtime_rows = fetch_realtime_minute_rows([symbol], "1MIN").get(symbol, [])
            realtime_rows = [
                row for row in realtime_rows
                if not pd.isna(timestamp := pd.to_datetime(row.get("timestamp"), errors="coerce"))
                and timestamp.date() == today
            ]
            if realtime_rows:
                rows = merge_minute_rows(rows, realtime_rows)
                realtime_merged = True
        except Exception as exc:  # noqa: BLE001  实时补齐失败时退回纯历史
            logger.warning("rt_min_daily 补齐 %s 当日分时失败: %s", symbol, exc)

    series = build_minute_series(rows, days)
    series.update({
        "ts_code": symbol,
        "realtime_merged": realtime_merged,
        "today_complete": bool(today_complete),
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return series
