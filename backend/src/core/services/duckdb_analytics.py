"""DuckDB 分析库与日线行情通用辅助（因子实验室 / 雪球组合共用）。

从 src/app/api/factor_lab.py 抽取的共享层，避免 API 模块之间互相依赖。
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import polars as pl

from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_SYMBOLS
from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from .factor_backtest_engine import A_STOCK_INDEX_POOL_CODES

SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
A_STOCK_INDEX_POOL_CODE_SET = set(A_STOCK_INDEX_POOL_CODES)
A_STOCK_ETF_DAILY_SYMBOL_SET = {str(symbol).upper() for symbol in A_STOCK_ETF_DAILY_SYMBOLS}


def safe_float(value: Any, digits: Optional[int] = None) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits) if digits is not None else number


def load_tushare_realtime_daily_k(ts_code: str, trade_date: date) -> Optional[Dict[str, Any]]:
    """Load one current-day A-share candle from Tushare rt_k.

    rt_k returns the previous trading day's row before the market opens or when
    a quote is stale, so callers only receive a row when trade_time matches the
    requested date and the OHLC values form a valid candle.
    """
    from .tushare import TushareService

    normalized = str(ts_code or "").strip().upper()
    if not normalized or trade_date is None:
        return None
    frame = TushareService.get_instance().get_a_stock_realtime_rt_k_frame([normalized])
    if frame is None or not hasattr(frame, "empty") or frame.empty:
        return None
    for _, row in frame.iterrows():
        if str(row.get("ts_code") or "").strip().upper() != normalized:
            continue
        trade_time = row.get("trade_time")
        try:
            quote_date = trade_time.date() if hasattr(trade_time, "date") else datetime.fromisoformat(str(trade_time)).date()
        except (TypeError, ValueError):
            continue
        if quote_date != trade_date:
            continue
        open_price = safe_float(row.get("open"))
        high_price = safe_float(row.get("high"))
        low_price = safe_float(row.get("low"))
        close_price = safe_float(row.get("close"))
        if (
            not all(value is not None and value > 0 for value in (open_price, high_price, low_price, close_price))
            or high_price < max(open_price, close_price, low_price)
            or low_price > min(open_price, close_price, high_price)
        ):
            continue
        return {
            "trade_date": trade_date,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "source": "tushare_rt_k",
        }
    return None


def serialize_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return safe_float(value)
    return value


def quote_sql_string(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def is_a_stock_symbol(symbol: str) -> bool:
    text = str(symbol or "").strip().upper()
    return text.endswith((".SH", ".SZ", ".BJ"))


def connect_analytics_db():
    """打开 DuckDB 分析库（只读优先），失败抛 RuntimeError（供 API 层转 HTTP 错误）。"""
    try:
        import duckdb  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("DuckDB依赖不可用") from exc
    try:
        return connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"DuckDB分析库当前不可读，可能正在同步写入或被其他进程占用: {exc}"
        ) from exc


def duckdb_table_exists(connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def duckdb_query_dicts(connection, query: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    cursor = connection.execute(query, params or [])
    columns = [column[0] for column in cursor.description or []]
    return [
        {key: serialize_value(value) for key, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def load_price_frame(symbols: List[str], start_date: date, end_date: date) -> pl.DataFrame:
    """加载美股/A股指数/ETF/A股个股的日线行情（前复权）到 polars DataFrame。"""
    safe_symbols = [
        symbol for symbol in list(dict.fromkeys(symbols))
        if symbol and SYMBOL_PATTERN.match(symbol)
    ]
    schema = {
        "symbol": pl.Utf8,
        "trade_date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "turnover": pl.Float64,
    }
    if not safe_symbols:
        return pl.DataFrame(schema=schema)

    frames: List[pl.DataFrame] = []
    connection = connect_analytics_db()
    try:
        us_symbols = [symbol for symbol in safe_symbols if str(symbol).upper().endswith(".US")]
        if us_symbols:
            symbol_sql = ", ".join(quote_sql_string(symbol) for symbol in us_symbols)
            query = f"""
                SELECT
                    symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(volume AS DOUBLE) AS volume,
                    CAST(turnover AS DOUBLE) AS turnover
                FROM us_stock_daily
                WHERE symbol IN ({symbol_sql})
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY symbol, trade_date
            """
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))

        a_index_symbols = [symbol for symbol in safe_symbols if symbol in A_STOCK_INDEX_POOL_CODE_SET]
        if a_index_symbols:
            symbol_sql = ", ".join(quote_sql_string(symbol) for symbol in a_index_symbols)
            query = f"""
                SELECT
                    ts_code AS symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(vol AS DOUBLE) AS volume,
                    CAST(amount AS DOUBLE) AS turnover
                FROM a_stock_index_daily
                WHERE ts_code IN ({symbol_sql})
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY ts_code, trade_date
            """
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))

        a_fund_symbols = [
            symbol
            for symbol in safe_symbols
            if symbol in A_STOCK_ETF_DAILY_SYMBOL_SET
        ]
        if a_fund_symbols:
            symbol_sql = ", ".join(quote_sql_string(symbol) for symbol in a_fund_symbols)
            query = f"""
                SELECT
                    ts_code AS symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(vol AS DOUBLE) AS volume,
                    CAST(amount AS DOUBLE) AS turnover
                FROM a_stock_fund_daily_qfq
                WHERE ts_code IN ({symbol_sql})
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY ts_code, trade_date
            """
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))

        a_symbols = [
            symbol
            for symbol in safe_symbols
            if is_a_stock_symbol(symbol) and symbol not in A_STOCK_INDEX_POOL_CODE_SET
            and symbol not in A_STOCK_ETF_DAILY_SYMBOL_SET
        ]
        if a_symbols:
            symbol_sql = ", ".join(quote_sql_string(symbol) for symbol in a_symbols)
            query = f"""
                SELECT
                    ts_code AS symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(vol AS DOUBLE) AS volume,
                    CAST(amount AS DOUBLE) AS turnover
                FROM a_stock_market_daily_qfq
                WHERE ts_code IN ({symbol_sql})
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY ts_code, trade_date
            """
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))
    finally:
        connection.close()

    non_empty_frames = [frame for frame in frames if frame is not None and not frame.is_empty()]
    if not non_empty_frames:
        return pl.DataFrame(schema=schema)
    df = pl.concat(non_empty_frames, how="vertical_relaxed")
    if df.is_empty():
        return pl.DataFrame(schema=schema)
    return df.with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("turnover").cast(pl.Float64),
    ).sort(["symbol", "trade_date"]).with_columns(
        pl.min("trade_date").over("symbol").alias("_first_trade_date")
    )
