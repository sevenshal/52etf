"""守猪逮兔 A 股 ETF 的实时成交量与提示看板同口径量比。"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime, time as dtime
from typing import Any, Dict, Iterable, List

import pandas as pd

from .chan_minute_data import upsert_minute_frame
from .duckdb_analytics import connect_analytics_db
from .market_alerts import get_persisted_etf_intraday_volume_baseline
from .tushare import TushareService


logger = logging.getLogger(__name__)
CACHE_TTL_SECONDS = 30
MINUTE_HISTORY_DAYS = 20
MINUTE_DAY_MIN_BARS = 200
_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "metrics": {}}
_sync_lock = threading.Lock()
_sync_pending: set[str] = set()
_sync_running = False


def normalize_etf_symbol(value: Any) -> str:
    return TushareService.normalize_symbol(str(value or ""))


def _missing_history_symbols(symbols: List[str], today: date) -> List[str]:
    if not symbols:
        return []
    placeholders = ", ".join("?" for _ in symbols)
    connection = connect_analytics_db()
    try:
        rows = connection.execute(
            f"""
            WITH expected AS (
              SELECT DISTINCT ts_code, trade_date
              FROM (
                SELECT ts_code, trade_date,
                       ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                FROM a_stock_fund_daily
                WHERE ts_code IN ({placeholders}) AND trade_date < ?
              ) WHERE rn <= ?
            ), minute_days AS (
              SELECT ts_code, CAST(trade_time AS DATE) AS trade_date
              FROM a_stock_minute_bar
              WHERE ts_code IN ({placeholders}) AND trade_time < ?
              GROUP BY ts_code, CAST(trade_time AS DATE)
              HAVING COUNT(*) >= ?
            )
            SELECT expected.ts_code
            FROM expected
            LEFT JOIN minute_days
              ON minute_days.ts_code = expected.ts_code AND minute_days.trade_date = expected.trade_date
            GROUP BY expected.ts_code
            HAVING COUNT(minute_days.trade_date) < COUNT(*)
            """,
            [*symbols, today, MINUTE_HISTORY_DAYS, *symbols, datetime.combine(today, dtime.min),
             MINUTE_DAY_MIN_BARS],
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]).upper() for row in rows]


def _history_window(symbol: str, today: date) -> tuple[date, date] | None:
    connection = connect_analytics_db()
    try:
        rows = connection.execute(
            """
            SELECT trade_date FROM a_stock_fund_daily
            WHERE ts_code = ? AND trade_date < ?
            ORDER BY trade_date DESC LIMIT ?
            """,
            [symbol, today, MINUTE_HISTORY_DAYS + 1],
        ).fetchall()
    finally:
        connection.close()
    dates = sorted(row[0] for row in rows)
    return (dates[0], dates[-1]) if len(dates) >= MINUTE_HISTORY_DAYS else None


def _sync_symbols(symbols: Iterable[str]) -> Dict[str, Any]:
    """复用个股分钟任务的缓存表和限流器，ETF 历史源改用 etf_mins。"""
    symbols = list(symbols)
    service = TushareService.get_instance()
    today = datetime.now().date()
    saved = requests = 0
    errors: List[str] = []
    for symbol in symbols:
        window = _history_window(symbol, today)
        if not window:
            continue
        try:
            frame = service.get_a_share_etf_historical_minute_frame(symbol, *window)
            if frame is not None and not frame.empty:
                saved += upsert_minute_frame(frame, source="tushare_etf_mins")
            requests += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")
    with _cache_lock:
        _cache["at"] = 0.0
    return {"symbols": len(symbols), "saved_rows": saved, "requests": requests, "errors": errors}


def _drain_sync_queue() -> None:
    global _sync_running
    while True:
        with _sync_lock:
            symbols = sorted(_sync_pending)
            _sync_pending.clear()
            if not symbols:
                _sync_running = False
                return
        result = _sync_symbols(symbols)
        if result["errors"]:
            logger.warning("守猪逮兔 ETF 分钟历史补齐存在错误: %s", result["errors"][:5])


def ensure_etf_minute_history_async(codes: Iterable[Any]) -> None:
    """缺分钟历史时异步补齐，首屏不被 Tushare 历史分钟请求阻塞。"""
    global _sync_running
    symbols = list(dict.fromkeys(
        symbol for symbol in (normalize_etf_symbol(code) for code in codes)
        if symbol.endswith((".SH", ".SZ"))
    ))
    missing = _missing_history_symbols(symbols, datetime.now().date())
    if not missing:
        return
    with _sync_lock:
        _sync_pending.update(missing)
        if _sync_running:
            return
        _sync_running = True
    threading.Thread(target=_drain_sync_queue, daemon=True, name="szdt-etf-minute-sync").start()


def sync_registered_etf_minutes() -> Dict[str, Any]:
    """盘后继续更新已缓存 ETF，保持与个股分钟缓存相同的滚动历史。"""
    connection = connect_analytics_db()
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT minute.ts_code
            FROM a_stock_minute_bar minute
            WHERE EXISTS (SELECT 1 FROM a_stock_fund_daily fund WHERE fund.ts_code = minute.ts_code)
            """
        ).fetchall()
    finally:
        connection.close()
    symbols = [str(row[0]).upper() for row in rows]
    return _sync_symbols(_missing_history_symbols(symbols, datetime.now().date()))


def get_a_share_etf_volume_metrics(codes: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """提示看板同口径：当日累计量 ÷ 前20交易日同一时刻累计量均值。"""
    symbols = list(dict.fromkeys(
        symbol for symbol in (normalize_etf_symbol(code) for code in codes)
        if symbol.endswith((".SH", ".SZ"))
    ))
    if not symbols:
        return {}
    ensure_etf_minute_history_async(symbols)
    now_monotonic = time.monotonic()
    with _cache_lock:
        cached = _cache["metrics"]
        if now_monotonic - _cache["at"] < CACHE_TTL_SECONDS and all(symbol in cached for symbol in symbols):
            return {symbol: cached[symbol] for symbol in symbols}

    realtime = TushareService.get_instance().get_a_stock_realtime_etf_rt_k_frame(symbols)
    if not isinstance(realtime, pd.DataFrame):
        realtime = pd.DataFrame()
    baselines: Dict[tuple[date, str], Dict[str, float]] = {}
    result: Dict[str, Dict[str, Any]] = {}
    for _, row in realtime.iterrows():
        symbol = normalize_etf_symbol(row.get("ts_code"))
        if symbol not in symbols:
            continue
        try:
            volume = float(row.get("vol"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(volume) or volume < 0:
            continue
        stamp = pd.to_datetime(row.get("trade_time"), errors="coerce")
        if pd.isna(stamp):
            continue
        key = (stamp.date(), stamp.strftime("%H:%M"))
        if key not in baselines:
            baselines[key] = get_persisted_etf_intraday_volume_baseline(key[1], baseline_date=key[0])
        baseline = baselines[key].get(symbol)
        ratio = volume / baseline if baseline and baseline > 0 else None
        result[symbol] = {
            "volume": volume,
            "volume_ratio": round(ratio, 3) if ratio is not None and math.isfinite(ratio) else None,
            "volume_baseline_20d": baseline,
            "volume_ratio_mode": "same_time_cumulative_20d",
        }
    with _cache_lock:
        _cache["at"] = now_monotonic
        _cache["metrics"].update(result)
    return {symbol: result.get(symbol, _cache["metrics"].get(symbol, {})) for symbol in symbols}
