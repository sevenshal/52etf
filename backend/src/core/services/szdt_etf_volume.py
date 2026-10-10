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


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def _latest_daily_metrics(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """休市或行情快照过期时，返回最近日线的成交量、成交额和日线量比。

    ``rt_etf_k`` 在周末、节假日会继续返回上一交易日的快照。盘中量比不能把这份
    旧快照拿去和今天的分钟基准相比；此时改为最近日线成交量除以前 20 个交易日均量。
    日线 ``vol`` 单位为手、``amount`` 单位为千元，统一换算成实时行情的股、元口径。
    """
    if not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    connection = connect_analytics_db()
    try:
        rows = connection.execute(
            f"""
            SELECT ts_code, trade_date, vol, amount
            FROM a_stock_fund_daily
            WHERE ts_code IN ({placeholders})
              AND vol IS NOT NULL AND vol >= 0
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) <= 21
            ORDER BY ts_code, trade_date DESC
            """,
            symbols,
        ).fetchall()
    finally:
        connection.close()

    grouped: Dict[str, List[tuple[Any, Any, Any]]] = {}
    for ts_code, trade_date, volume, amount in rows:
        grouped.setdefault(str(ts_code).upper(), []).append((trade_date, volume, amount))

    result: Dict[str, Dict[str, Any]] = {}
    for symbol, values in grouped.items():
        latest_date, latest_volume, latest_amount = values[0]
        try:
            volume = float(latest_volume) * 100.0
            turnover = float(latest_amount) * 1000.0 if latest_amount is not None else None
        except (TypeError, ValueError):
            continue
        prior_volumes = []
        for _, prior_volume, _ in values[1:]:
            try:
                number = float(prior_volume) * 100.0
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                prior_volumes.append(number)
        baseline = sum(prior_volumes) / len(prior_volumes) if prior_volumes else None
        ratio = volume / baseline if baseline and baseline > 0 else None
        result[symbol] = {
            "volume": volume,
            "turnover": turnover if turnover is not None and math.isfinite(turnover) else None,
            "volume_ratio": round(ratio, 3) if ratio is not None and math.isfinite(ratio) else None,
            "volume_baseline_20d": baseline,
            "volume_ratio_mode": "daily_20d",
            "trade_date": latest_date.isoformat() if hasattr(latest_date, "isoformat") else str(latest_date),
        }
    return result


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
    stale_symbols: List[str] = []
    today = datetime.now().date()
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
        if stamp.date() != today:
            stale_symbols.append(symbol)
            continue
        key = (stamp.date(), stamp.strftime("%H:%M"))
        if key not in baselines:
            baselines[key] = get_persisted_etf_intraday_volume_baseline(key[1], baseline_date=key[0])
        baseline = baselines[key].get(symbol)
        ratio = volume / baseline if baseline and baseline > 0 else None
        result[symbol] = {
            "volume": volume,
            # rt_etf_k: vol=股、amount=元，与页面展示和成交量统一口径。
            "turnover": _finite_number(row.get("amount")),
            "volume_ratio": round(ratio, 3) if ratio is not None and math.isfinite(ratio) else None,
            "volume_baseline_20d": baseline,
            "volume_ratio_mode": "same_time_cumulative_20d",
        }
    # 休市日和开盘前 rt_etf_k 返回的是上一交易日快照；用日线口径展示而非 0/错误量比。
    missing_symbols = [symbol for symbol in symbols if symbol not in result]
    if stale_symbols or missing_symbols:
        result.update(_latest_daily_metrics(list(dict.fromkeys([*stale_symbols, *missing_symbols]))))
    with _cache_lock:
        _cache["at"] = now_monotonic
        _cache["metrics"].update(result)
    return {symbol: result.get(symbol, _cache["metrics"].get(symbol, {})) for symbol in symbols}


def get_a_share_etf_ema5(symbol: Any, current_price: Any) -> float | None:
    """以日线收盘价和当前价格计算 ETF EMA5，供贪婪卖出确认复用。"""
    code = normalize_etf_symbol(symbol)
    try:
        price = float(current_price)
    except (TypeError, ValueError):
        return None
    if not code or not math.isfinite(price) or price <= 0:
        return None
    connection = connect_analytics_db()
    try:
        rows = connection.execute(
            """
            SELECT close FROM a_stock_fund_daily
            WHERE ts_code = ? AND trade_date < ? AND close IS NOT NULL AND close > 0
            ORDER BY trade_date DESC LIMIT 60
            """,
            [code, datetime.now().date()],
        ).fetchall()
    finally:
        connection.close()
    closes = [float(row[0]) for row in reversed(rows) if row[0] is not None]
    if len(closes) < 5:
        return None
    # adjust=False EMA 与回测 pandas ewm(span=5, adjust=False) 保持一致。
    ema = closes[0]
    alpha = 2 / 6
    for close in closes[1:]:
        ema = alpha * close + (1 - alpha) * ema
    return alpha * price + (1 - alpha) * ema
