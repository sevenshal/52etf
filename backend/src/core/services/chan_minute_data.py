"""A-share rolling minute-bar storage and CZSC-compatible aggregation."""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from czsc import BarGenerator

from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from .a_stock_consensus import normalize_a_stock_symbol
from .chan_analysis import rows_to_raw_bars
from .tushare import TushareService


ROLLING_TRADING_DAYS = 32
DISPLAY_TRADING_DAYS = 20
HISTORICAL_MINUTE_SYMBOL_DAYS_PER_BATCH = 32
HISTORICAL_MINUTE_COMPLETE_DAY_MIN_BARS = 200
REALTIME_MINUTE_FREQS = {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}
REALTIME_MINUTE_FETCH_WORKERS = max(
    1,
    min(32, int(os.getenv("CHAN_REALTIME_MINUTE_FETCH_WORKERS", "16"))),
)

logger = logging.getLogger(__name__)
_MINUTE_WRITE_LOCK = threading.Lock()


def historical_minute_batch_size(trading_days: int) -> int:
    """Keep each request within 32 symbol-days: 32/16/10/.../1 symbols."""
    return max(1, HISTORICAL_MINUTE_SYMBOL_DAYS_PER_BATCH // max(1, int(trading_days)))


def plan_incremental_minute_groups(
    trading_dates: list[date],
    missing_rows: list[tuple[str, date]],
) -> list[dict[str, Any]]:
    """Group contiguous per-symbol gaps and prepend one already-synced trading day."""
    dates = sorted(dict.fromkeys(trading_dates))
    if not dates:
        return []
    date_indexes = {trade_date: index for index, trade_date in enumerate(dates)}
    missing_by_symbol: dict[str, set[int]] = defaultdict(set)
    for symbol, trade_date in missing_rows:
        normalized = normalize_a_stock_symbol(symbol)
        if normalized and trade_date in date_indexes:
            missing_by_symbol[normalized].add(date_indexes[trade_date])

    grouped: dict[tuple[date, date, int, int], list[str]] = defaultdict(list)
    for symbol, missing_indexes in missing_by_symbol.items():
        ordered = sorted(missing_indexes)
        sequence_start = sequence_end = ordered[0]
        sequences = []
        for index in ordered[1:]:
            if index == sequence_end + 1:
                sequence_end = index
                continue
            sequences.append((sequence_start, sequence_end))
            sequence_start = sequence_end = index
        sequences.append((sequence_start, sequence_end))

        for missing_start, missing_end in sequences:
            fetch_start = max(0, missing_start - 1)
            run_days = missing_end - fetch_start + 1
            missing_days = missing_end - missing_start + 1
            key = (dates[fetch_start], dates[missing_end], run_days, missing_days)
            grouped[key].append(symbol)

    return [
        {
            "start_date": start_date,
            "end_date": end_date,
            "run_days": run_days,
            "missing_days": missing_days,
            "symbols": sorted(symbols),
        }
        for (start_date, end_date, run_days, missing_days), symbols in sorted(grouped.items())
    ]


def incremental_minute_sync_groups(
    trading_days: int = ROLLING_TRADING_DAYS,
) -> tuple[list[dict[str, Any]], date, date]:
    """Find missing minute dates expected from daily bars within the rolling window."""
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        dates = connection.execute(
            "SELECT DISTINCT trade_date FROM a_stock_market_daily ORDER BY trade_date DESC LIMIT ?",
            [max(1, min(ROLLING_TRADING_DAYS, int(trading_days)))],
        ).fetchall()
        if not dates:
            raise ValueError("A股日行情为空，无法确定分钟行情同步区间")
        trading_dates = sorted(row[0] for row in dates)
        start_date, end_date = trading_dates[0], trading_dates[-1]
        missing_rows = connection.execute(
            """
            WITH minute_dates AS (
                SELECT ts_code, CAST(trade_time AS DATE) AS trade_date
                FROM a_stock_minute_bar
                WHERE trade_time >= ? AND trade_time <= ?
                GROUP BY ts_code, CAST(trade_time AS DATE)
                HAVING COUNT(*) >= ?
            )
            SELECT DISTINCT daily.ts_code, daily.trade_date
            FROM a_stock_market_daily daily
            JOIN a_stock_basic basic
              ON basic.ts_code = daily.ts_code AND basic.list_status = 'L'
            LEFT JOIN minute_dates minute
              ON minute.ts_code = daily.ts_code AND minute.trade_date = daily.trade_date
            WHERE daily.trade_date >= ? AND daily.trade_date <= ?
              AND minute.ts_code IS NULL
            ORDER BY daily.ts_code, daily.trade_date
            """,
            [
                datetime.combine(start_date, time.min),
                datetime.combine(end_date, time.max),
                HISTORICAL_MINUTE_COMPLETE_DAY_MIN_BARS,
                start_date,
                end_date,
            ],
        ).fetchall()
    finally:
        connection.close()
    return plan_incremental_minute_groups(trading_dates, missing_rows), start_date, end_date


def fetch_historical_minute_batch(
    service: TushareService,
    symbols: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Fetch one network batch, splitting missing or failed batches into single-symbol retries."""
    normalized = list(dict.fromkeys(filter(None, (normalize_a_stock_symbol(item) for item in symbols))))
    if not normalized:
        return {"symbols": [], "frame": pd.DataFrame(), "errors": []}

    start_time = datetime.combine(start_date, time.min)
    end_time = datetime.combine(end_date, time.max)
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    batch_failed = False
    try:
        frame = service.get_a_stock_historical_minute_batch_frame(
            normalized,
            start_time,
            end_time,
            freq="1min",
            raise_on_error=True,
        )
    except Exception as exc:
        batch_failed = True
        frame = pd.DataFrame()
        logger.warning("Tushare minute batch failed for %s symbols; retrying separately: %s", len(normalized), exc)
    if isinstance(frame, pd.DataFrame) and not frame.empty:
        frames.append(frame)
        returned = set(frame["ts_code"].astype(str).str.strip().str.upper())
    else:
        returned = set()

    missing = [symbol for symbol in normalized if symbol not in returned]
    retry_symbols = missing if len(normalized) > 1 or batch_failed else []
    for symbol in retry_symbols:
        try:
            retry = service.get_a_stock_historical_minute_batch_frame(
                [symbol],
                start_time,
                end_time,
                freq="1min",
                raise_on_error=True,
            )
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
            continue
        if isinstance(retry, pd.DataFrame) and not retry.empty:
            frames.append(retry)

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not combined.empty:
        combined = combined.drop_duplicates(["ts_code", "trade_time"], keep="last")
    return {"symbols": normalized, "frame": combined, "errors": errors}


def normalize_minute_frame(frame: pd.DataFrame, source: str = "tushare_stk_mins") -> pd.DataFrame:
    columns = ["ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount", "source", "updated_at"]
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=columns)
    result = frame.copy()
    result["ts_code"] = result["ts_code"].astype(str).str.strip().str.upper()
    result["trade_time"] = pd.to_datetime(result["trade_time"], errors="coerce").dt.tz_localize(None)
    for name in ("open", "high", "low", "close", "vol", "amount"):
        result[name] = pd.to_numeric(result.get(name), errors="coerce")
    result["vol"] = result["vol"].fillna(0).clip(lower=0)
    result["amount"] = result["amount"].fillna(0).clip(lower=0)
    result["source"] = source
    result["updated_at"] = datetime.now()
    result = result.dropna(subset=["ts_code", "trade_time", "open", "high", "low", "close"])
    valid = (
        (result["low"] <= result[["open", "close"]].min(axis=1))
        & (result["high"] >= result[["open", "close"]].max(axis=1))
        & (result["low"] <= result["high"])
    )
    return result.loc[valid, columns].drop_duplicates(["ts_code", "trade_time"], keep="last").sort_values("trade_time")


def upsert_minute_frame(frame: pd.DataFrame) -> int:
    normalized = normalize_minute_frame(frame)
    if normalized.empty:
        return 0
    with _MINUTE_WRITE_LOCK:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
        try:
            connection.register("chan_minute_upsert", normalized)
            connection.execute(
                """
                INSERT OR REPLACE INTO a_stock_minute_bar
                SELECT ts_code, trade_time, open, high, low, close, vol, amount, source, updated_at
                FROM chan_minute_upsert
                """
            )
        finally:
            connection.close()
    return len(normalized)


def load_minute_rows(symbol: str, start_time: datetime, end_time: datetime) -> list[dict[str, Any]]:
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        return []
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        frame = connection.execute(
            """
            SELECT trade_time AS timestamp, open, high, low, close, vol AS volume, amount AS turnover
            FROM a_stock_minute_bar_qfq
            WHERE ts_code = ? AND trade_time >= ? AND trade_time <= ?
            ORDER BY trade_time
            """,
            [normalized_symbol, start_time, end_time],
        ).fetchdf()
    finally:
        connection.close()
    return frame.to_dict("records")


def aggregate_minute_rows(symbol: str, rows: list[dict[str, Any]], freq: str) -> list[dict[str, Any]]:
    if freq == "1m":
        return rows
    target = {"5m": "5分钟", "30m": "30分钟"}.get(freq)
    if target is None:
        raise ValueError(f"Unsupported minute frequency: {freq}")
    raw_bars = rows_to_raw_bars(symbol, rows, "1m")
    generator = BarGenerator(base_freq="1分钟", freqs=[target], max_count=max(2000, len(raw_bars)), market="A股")
    for bar in raw_bars:
        generator.update(bar)
    return [
        {
            "timestamp": bar.dt,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.vol),
            "turnover": float(bar.amount),
        }
        for bar in generator.bars[target]
    ]


def is_complete_a_share_minute_day(rows: list[dict[str, Any]], trade_date: date) -> bool:
    """Return whether raw 1m rows contain every regular A-share minute for a closed day."""
    actual = {
        timestamp.to_pydatetime().replace(second=0, microsecond=0)
        for row in rows
        if not pd.isna(timestamp := pd.to_datetime(row.get("timestamp"), errors="coerce"))
        and timestamp.date() == trade_date
    }
    morning_start = datetime.combine(trade_date, time(9, 30))
    morning_end = datetime.combine(trade_date, time(11, 30))
    afternoon_start = datetime.combine(trade_date, time(13, 1))
    afternoon_end = datetime.combine(trade_date, time(15, 0))
    expected = set(pd.date_range(morning_start, morning_end, freq="1min").to_pydatetime())
    expected.update(pd.date_range(afternoon_start, afternoon_end, freq="1min").to_pydatetime())
    return expected.issubset(actual)


def merge_minute_rows(
    historical_rows: list[dict[str, Any]],
    realtime_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge normalized bars in memory, with realtime rows winning timestamp collisions."""
    rows_by_time: dict[datetime, dict[str, Any]] = {}
    for row in [*historical_rows, *realtime_rows]:
        timestamp = pd.to_datetime(row.get("timestamp"), errors="coerce")
        if pd.isna(timestamp):
            continue
        normalized = dict(row)
        normalized["timestamp"] = timestamp.to_pydatetime()
        rows_by_time[normalized["timestamp"]] = normalized
    return [rows_by_time[timestamp] for timestamp in sorted(rows_by_time)]


def backfill_symbol_minutes(symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    """Fetch in <=30-calendar-day chunks so every response stays below 8000 rows."""
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("无效的A股代码")
    service = TushareService.get_instance()
    cursor = start_date
    fetched = saved = chunks = 0
    while cursor <= end_date:
        chunk_end = min(end_date, cursor + timedelta(days=29))
        frame = service.get_a_stock_historical_minute_frame(
            normalized_symbol,
            datetime.combine(cursor, time.min),
            datetime.combine(chunk_end, time.max),
            freq="1min",
        )
        chunks += 1
        fetched += len(frame)
        saved += upsert_minute_frame(frame)
        cursor = chunk_end + timedelta(days=1)
    return {"symbol": normalized_symbol, "chunks": chunks, "fetched_rows": fetched, "saved_rows": saved}


def minute_data_status(symbol: str | None = None) -> dict[str, Any]:
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        if symbol:
            normalized = normalize_a_stock_symbol(symbol)
            row = connection.execute(
                "SELECT COUNT(*), MIN(trade_time), MAX(trade_time), COUNT(DISTINCT CAST(trade_time AS DATE)) FROM a_stock_minute_bar WHERE ts_code = ?",
                [normalized],
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*), MIN(trade_time), MAX(trade_time), COUNT(DISTINCT ts_code) FROM a_stock_minute_bar"
            ).fetchone()
    finally:
        connection.close()
    return {"row_count": int(row[0] or 0), "start_time": row[1], "end_time": row[2], "coverage": int(row[3] or 0)}


def recent_market_universe(trading_days: int = ROLLING_TRADING_DAYS) -> tuple[list[str], date, date]:
    """Return active symbols and the exact rolling trading-day boundary."""
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        dates = connection.execute(
            "SELECT DISTINCT trade_date FROM a_stock_market_daily ORDER BY trade_date DESC LIMIT ?",
            [max(1, int(trading_days))],
        ).fetchall()
        symbols = connection.execute(
            "SELECT ts_code FROM a_stock_basic WHERE list_status = 'L' ORDER BY ts_code"
        ).fetchall()
    finally:
        connection.close()
    if not dates:
        raise ValueError("A股日行情为空，无法确定分钟行情同步区间")
    ordered_dates = sorted(row[0] for row in dates)
    return [row[0] for row in symbols], ordered_dates[0], ordered_dates[-1]


def prune_minute_history(keep_from: date) -> int:
    with _MINUTE_WRITE_LOCK:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM a_stock_minute_bar WHERE CAST(trade_time AS DATE) < ?",
                [keep_from],
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM a_stock_minute_bar WHERE CAST(trade_time AS DATE) < ?",
                [keep_from],
            )
        finally:
            connection.close()
    return int(count or 0)


def fetch_realtime_minute_rows(
    symbols: list[str],
    freq: str = "1MIN",
) -> dict[str, list[dict[str, Any]]]:
    """Fetch each symbol's complete current-day target-frequency bars without persisting them."""
    normalized = list(dict.fromkeys(filter(None, (normalize_a_stock_symbol(item) for item in symbols))))
    if not normalized:
        return {}
    normalized_freq = str(freq or "1MIN").upper()
    if normalized_freq not in REALTIME_MINUTE_FREQS:
        raise ValueError("分钟频率必须为 1MIN、5MIN、15MIN、30MIN 或 60MIN")
    service = TushareService.get_instance()
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}

    def fetch_symbol(symbol: str) -> tuple[str, list[dict[str, Any]]]:
        frame = service.get_a_stock_realtime_minute_frame(symbol, normalized_freq)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return symbol, []
        rows: list[dict[str, Any]] = []
        for row in frame.to_dict("records"):
            timestamp = pd.to_datetime(row.get("time", row.get("trade_time")), errors="coerce")
            if pd.isna(timestamp):
                continue
            rows.append(
                {
                    "timestamp": timestamp.to_pydatetime(),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("vol"),
                    "turnover": row.get("amount"),
                }
            )
        return symbol, sorted(rows, key=lambda item: item["timestamp"])

    workers = min(REALTIME_MINUTE_FETCH_WORKERS, len(normalized))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chan-realtime-minute") as executor:
        for symbol, rows in executor.map(fetch_symbol, normalized):
            if rows:
                rows_by_symbol[symbol] = rows
    return rows_by_symbol
