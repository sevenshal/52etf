"""大盘观测：指数概览条、全A涨跌分布、每日成交额。

口径：
- 指数概览：tushare rt_idx_k 取现价/昨收，rt_idx_min_daily 取当日分钟收盘算分时涨跌曲线（迷你图）。
- 涨跌分布：DuckDB 分析库 a_stock_market_daily 最新交易日，按 pct_chg 分档；
  涨停/跌停用同步好的 limit_status（2/3 涨停、5/6 跌停）单列，不重复计入相邻档。
- 每日成交额：tushare index_daily 的 000001.SH + 399106.SZ 成交额（千元）相加；
  深市必须用深证综指 399106，深证成指 399001 只统计 500 只成分股。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from .duckdb_analytics import connect_analytics_db, safe_float
from .market_volume import SH_TS_CODE, SZ_TS_CODE, YI
from .tushare import TushareService


logger = logging.getLogger(__name__)

INDEX_TILES: Tuple[Tuple[str, str, str], ...] = (
    ("sh", "000001.SH", "上证指数"),
    ("sz", "399001.SZ", "深证成指"),
    ("cyb", "399006.SZ", "创业板指"),
    ("kc50", "000688.SH", "科创50"),
)
FULL_DAY_SLOTS = 241  # 09:30 集合竞价 + 上午 120 + 下午 120

DIST_BUCKETS: Tuple[Tuple[str, Optional[float], Optional[float]], ...] = (
    (">7%", 7.0, None),
    ("3~7%", 3.0, 7.0),
    ("0~3%", 0.0, 3.0),
    ("0~-3%", -3.0, 0.0),
    ("-3~-7%", -7.0, -3.0),
    ("<-7%", None, -7.0),
)
LIMIT_UP_STATUS = (2, 3)
LIMIT_DOWN_STATUS = (5, 6)

DAILY_AMOUNT_MAX_DAYS = 250
INDEX_CACHE_TTL_SECONDS = 30
DIST_CACHE_TTL_SECONDS = 300
DAILY_CACHE_TTL_SECONDS = 300
# index_daily 的 amount 单位是千元
INDEX_DAILY_AMOUNT_TO_YI = 1e3 / YI

_cache: Dict[str, Tuple[float, Any]] = {}


class MarketOverviewDataError(RuntimeError):
    pass


def _cached(key: str, ttl: float, producer: Callable[[], Any]) -> Any:
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = producer()
    _cache[key] = (now, value)
    return value


def build_index_overview(
    quotes: Optional[pd.DataFrame],
    minute_frames: Dict[str, Optional[pd.DataFrame]],
) -> Dict[str, Any]:
    """把 rt_idx_k 行情与分钟线组装成概览条数据。"""
    by_code: Dict[str, Any] = {}
    if quotes is not None and not quotes.empty:
        for row in quotes.itertuples(index=False):
            by_code[str(row.ts_code).strip().upper()] = row

    items: List[Dict[str, Any]] = []
    for key, ts_code, name in INDEX_TILES:
        quote = by_code.get(ts_code)
        price = safe_float(getattr(quote, "close", None), 2) if quote is not None else None
        pre_close = safe_float(getattr(quote, "pre_close", None), 4) if quote is not None else None
        pct = round((price / pre_close - 1) * 100, 2) if price and pre_close else None

        spark: List[float] = []
        frame = minute_frames.get(ts_code)
        if frame is not None and not frame.empty and pre_close:
            spark = [
                round((float(close) / pre_close - 1) * 100, 3)
                for close in frame.sort_values("trade_time")["close"].tolist()
            ]
            if spark and price is None:
                price = safe_float(frame.sort_values("trade_time")["close"].iloc[-1], 2)
        items.append({
            "key": key,
            "ts_code": ts_code,
            "name": name,
            "price": price,
            "pre_close": pre_close,
            "pct": pct,
            "spark": spark,
            "spark_slots": FULL_DAY_SLOTS,
        })
    return {"items": items, "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def keep_latest_session(frame: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """只保留最近一个交易日的分钟线，避免多日曲线拼在一起。"""
    if frame is None or frame.empty:
        return frame
    days = pd.to_datetime(frame["trade_time"]).dt.date
    return frame[days == days.max()]


def _load_latest_session_minutes(service: Any, ts_code: str) -> Optional[pd.DataFrame]:
    """当日分钟线用 rt_idx_min_daily；周末/节假日它返回空，回退到 idx_mins 的最近一个交易日。"""
    try:
        frame = service.get_index_realtime_minute_frame(ts_code)
        if frame is not None and not frame.empty:
            return keep_latest_session(frame)
    except Exception as exc:  # noqa: BLE001  分时曲线缺失不影响价格与涨跌幅
        logger.warning("tushare rt_idx_min_daily 获取 %s 失败: %s", ts_code, exc)
    now = datetime.now()
    try:
        history = service.get_index_historical_minute_frame(ts_code, now - timedelta(days=10), now)
        return keep_latest_session(history)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tushare idx_mins 回退获取 %s 失败: %s", ts_code, exc)
        return None


def _load_index_overview() -> Dict[str, Any]:
    service = TushareService.get_instance()
    codes = [ts_code for _, ts_code, _ in INDEX_TILES]
    try:
        quotes = service.get_a_stock_realtime_index_frame(codes)
    except Exception as exc:  # noqa: BLE001
        raise MarketOverviewDataError(f"tushare rt_idx_k 获取指数行情失败: {exc}") from exc

    minute_frames: Dict[str, Optional[pd.DataFrame]] = {}
    for ts_code in codes:
        minute_frames[ts_code] = _load_latest_session_minutes(service, ts_code)

    overview = build_index_overview(quotes, minute_frames)
    if all(item["price"] is None for item in overview["items"]):
        raise MarketOverviewDataError("tushare 未返回任何指数行情")
    return overview


def fetch_index_overview() -> Dict[str, Any]:
    return _cached("index_overview", INDEX_CACHE_TTL_SECONDS, _load_index_overview)


def build_distribution(rows: List[Tuple[Any, ...]], trade_date: Any) -> Dict[str, Any]:
    """rows: (pct_chg, limit_status) 序列 → 分档计数。"""
    names = ["涨停"] + [name for name, _, _ in DIST_BUCKETS] + ["跌停"]
    counts = [0] * len(names)
    for pct_chg, limit_status in rows:
        status = int(limit_status) if limit_status is not None else None
        if status in LIMIT_UP_STATUS:
            counts[0] += 1
            continue
        if status in LIMIT_DOWN_STATUS:
            counts[-1] += 1
            continue
        value = safe_float(pct_chg)
        if value is None:
            continue
        for index, (_, low, high) in enumerate(DIST_BUCKETS, start=1):
            if (low is None or value > low) and (high is None or value <= high):
                counts[index] += 1
                break
    total = sum(counts)
    up = counts[0] + sum(counts[1:4])
    down = counts[-1] + sum(counts[4:-1])
    return {
        "date": trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date or ""),
        "names": names,
        "counts": counts,
        "total": total,
        "up_count": up,
        "down_count": down,
        "limit_up": counts[0],
        "limit_down": counts[-1],
    }


def _load_distribution() -> Dict[str, Any]:
    connection = connect_analytics_db()
    try:
        latest = connection.execute(
            "SELECT max(trade_date) FROM a_stock_market_daily"
        ).fetchone()
        trade_date = latest[0] if latest else None
        if trade_date is None:
            raise MarketOverviewDataError("分析库没有全市场日线数据")
        rows = connection.execute(
            "SELECT pct_chg, limit_status FROM a_stock_market_daily WHERE trade_date = ?",
            [trade_date],
        ).fetchall()
    except MarketOverviewDataError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MarketOverviewDataError(f"读取全市场日线失败: {exc}") from exc
    finally:
        connection.close()

    result = build_distribution(rows, trade_date)
    result["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


def fetch_breadth_distribution() -> Dict[str, Any]:
    return _cached("distribution", DIST_CACHE_TTL_SECONDS, _load_distribution)


def build_daily_amount(
    sh_frame: Optional[pd.DataFrame],
    sz_frame: Optional[pd.DataFrame],
    days: int,
) -> Dict[str, Any]:
    """两个指数的 index_daily 按交易日对齐求和，返回最近 days 天的成交额（亿元）。"""
    if sh_frame is None or sh_frame.empty or sz_frame is None or sz_frame.empty:
        raise MarketOverviewDataError("tushare index_daily 未返回成交额数据")

    def _amount_map(frame: pd.DataFrame) -> Dict[str, float]:
        return {
            str(row.trade_date): float(row.amount)
            for row in frame.itertuples(index=False)
            if row.amount is not None and not pd.isna(row.amount)
        }

    sh_amount = _amount_map(sh_frame)
    sz_amount = _amount_map(sz_frame)
    sh_pct = {
        str(row.trade_date): safe_float(getattr(row, "pct_chg", None))
        for row in sh_frame.itertuples(index=False)
    }

    dates = sorted(set(sh_amount) & set(sz_amount))[-max(1, min(days, DAILY_AMOUNT_MAX_DAYS)):]
    if not dates:
        raise MarketOverviewDataError("两市成交额没有可对齐的交易日")

    amounts = [round((sh_amount[d] + sz_amount[d]) * INDEX_DAILY_AMOUNT_TO_YI, 1) for d in dates]
    return {
        "dates": [f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d for d in dates],
        "amounts": amounts,
        "up": [1 if (sh_pct.get(d) or 0) >= 0 else 0 for d in dates],
        "avg": round(sum(amounts) / len(amounts), 1),
        "max": max(amounts),
        "min": min(amounts),
        "latest": amounts[-1],
    }


def _load_daily_amount(days: int, today: Optional[date] = None) -> Dict[str, Any]:
    service = TushareService.get_instance()
    today = today or date.today()
    # 取日历天数的 1.6 倍再多 10 天，保证交易日够 days 根
    start = today - timedelta(days=int(days * 1.6) + 10)
    frames: Dict[str, Optional[pd.DataFrame]] = {}
    for ts_code in (SH_TS_CODE, SZ_TS_CODE):
        try:
            frames[ts_code] = service.pro.index_daily(
                ts_code=ts_code,
                start_date=start.strftime("%Y%m%d"),
                end_date=today.strftime("%Y%m%d"),
                fields="ts_code,trade_date,close,pct_chg,vol,amount",
            )
        except Exception as exc:  # noqa: BLE001
            raise MarketOverviewDataError(f"tushare index_daily 获取 {ts_code} 失败: {exc}") from exc

    result = build_daily_amount(frames.get(SH_TS_CODE), frames.get(SZ_TS_CODE), days)
    result["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


def fetch_daily_amount(days: int = 120) -> Dict[str, Any]:
    normalized = max(20, min(int(days or 120), DAILY_AMOUNT_MAX_DAYS))
    return _cached(f"daily_amount:{normalized}", DAILY_CACHE_TTL_SECONDS, lambda: _load_daily_amount(normalized))
