"""A-share rolling minute-bar storage and CZSC-compatible aggregation."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from czsc import BarGenerator

from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from .a_stock_consensus import normalize_a_stock_symbol
from .chan_analysis import rows_to_raw_bars
from .tushare import TushareService


ROLLING_TRADING_DAYS = 60
DISPLAY_TRADING_DAYS = 20


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


def refresh_realtime_minutes(symbols: list[str]) -> dict[str, int]:
    """Refresh the latest 1m bar in provider-supported batches of 300."""
    normalized = list(dict.fromkeys(filter(None, (normalize_a_stock_symbol(item) for item in symbols))))
    service = TushareService.get_instance()
    fetched = saved = batches = 0
    for offset in range(0, len(normalized), 300):
        frame = service.get_a_stock_realtime_minute_batch_frame(normalized[offset : offset + 300], "1MIN")
        batches += 1
        fetched += len(frame)
        saved += upsert_minute_frame(frame)
    return {"batches": batches, "fetched_rows": fetched, "saved_rows": saved}
