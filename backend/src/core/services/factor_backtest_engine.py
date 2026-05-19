import bisect
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Union

import numpy as np
import polars as pl
from sqlalchemy import distinct, or_
from sqlalchemy.orm import Session as ORMSession

from ..database import (
    AStockInnovation100Constituent,
    AStockInnovation100Level,
    AStockInnovation100Rebalance,
    ETFHolding,
    USStockIndustrySnapshot,
)
from .factor_engine import (
    DEFAULT_MOMENTUM_WEIGHTS,
    FACTOR_DIRECTION_OPTIONS,
    FACTOR_REGISTRY,
    MIXED_WINDOW_KEY,
    MOMENTUM_FACTOR_SCORE_PREFIX,
    NEUTRALIZATION_OPTIONS,
    STANDARDIZATION_OPTIONS,
    SUPPORTED_MOMENTUM_WINDOWS,
    SUPPORTED_WINDOWS,
    FactorContext,
    FactorDefinition,
    _momentum_score_source_frame,
    _prepare_factor_frame,
    _prepare_momentum_factor_frame_from_source,
    load_valuation_frame,
    normalize_momentum_weights,
    normalize_momentum_weights_payload,
)
from .symbol_names import attach_symbol_names, load_symbol_name_map, normalize_symbol_for_name
from ..utils import normalize_us_equity_symbol
from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_SYMBOLS, A_STOCK_FACTOR_INDEX_POOLS

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")

DAILY_PRICE_SOURCE = "daily_close"
NEXT_OPEN_PRICE_SOURCE = "next_open"
DEFAULT_CANDIDATE_ETFS = ["SPY.US", "QQQ.US"]
A_STOCK_INNO100_POOL = "INNO100"
A_STOCK_INNO100_SYMBOL = "INNO100.CN"
A_STOCK_INNO100_INDEX_CODE = A_STOCK_INNO100_SYMBOL
A_STOCK_INDEX_POOL_CODES = tuple(item["index_code"] for item in A_STOCK_FACTOR_INDEX_POOLS)
A_STOCK_INDEX_POOL_CODE_SET = set(A_STOCK_INDEX_POOL_CODES)
A_STOCK_ETF_DAILY_SYMBOL_SET = {str(symbol).upper() for symbol in A_STOCK_ETF_DAILY_SYMBOLS}
DEFAULT_SELL_RANK_MULTIPLIER = 2.0
DEFAULT_REBALANCE_FREQUENCY = "weekly"
SUPPORTED_REBALANCE_FREQUENCIES = ["daily", "weekly", "monthly", "quarterly", "semiannual"]
REBALANCE_FREQUENCY_LABELS = {
    "daily": "每日",
    "weekly": "每周",
    "monthly": "每月",
    "quarterly": "季度",
    "semiannual": "半年",
}
ROTATION_MODE_RANK_EXIT_REBALANCE = "rank_exit_rebalance"
ROTATION_MODE_SCHEDULED_REBALANCE = "scheduled_rebalance"
ROTATION_MODE_CASH_FILL_REBALANCE = "cash_fill_rebalance"
DEFAULT_ROTATION_MODE = ROTATION_MODE_RANK_EXIT_REBALANCE
SUPPORTED_ROTATION_MODES = [
    ROTATION_MODE_RANK_EXIT_REBALANCE,
    ROTATION_MODE_CASH_FILL_REBALANCE,
    ROTATION_MODE_SCHEDULED_REBALANCE,
]
ROTATION_MODE_LABELS = {
    ROTATION_MODE_RANK_EXIT_REBALANCE: "跌出排名再补位调仓",
    ROTATION_MODE_CASH_FILL_REBALANCE: "现金补位不减仓",
    ROTATION_MODE_SCHEDULED_REBALANCE: "定期调仓到目标仓位",
}
DEFAULT_MIN_LISTING_DAYS = 365
DEFAULT_VIRTUAL_FACTOR_LEGS = [
    {
        "factor": "risk_adjusted_momentum",
        "window": MIXED_WINDOW_KEY,
        "weight": 0.6,
        "neutralization": "none",
        "standardization": "rank_percentile",
        "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS.copy(),
    },
    {
        "factor": "index_weight",
        "window": 20,
        "weight": 0.4,
        "neutralization": "none",
        "standardization": "rank_percentile",
        "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS.copy(),
    },
]
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")

POOL_OPTIONS = [
    {"key": "QQQ", "label": "纳斯达克100ETF QQQ", "description": "纳指100成分股", "etfs": ["QQQ.US"]},
    {"key": "SPY", "label": "标普500ETF SPY", "description": "标普500成分股", "etfs": ["SPY.US"]},
    {"key": "SPY_QQQ", "label": "标普500ETF SPY + 纳斯达克100ETF QQQ", "description": "标普500与纳指100成分股并集", "etfs": ["SPY.US", "QQQ.US"]},
    {"key": A_STOCK_INNO100_POOL, "label": "A股创新100 INNO100.CN", "description": "自编A股创新100历史成分股", "etfs": [A_STOCK_INNO100_SYMBOL]},
    *[
        {
            "key": item["index_code"],
            "label": f"{item['index_code']} {item['name']}",
            "description": f"{item['name']}历史成分股",
            "etfs": [item["index_code"]],
        }
        for item in A_STOCK_FACTOR_INDEX_POOLS
    ],
]
POOL_ETFS = {item["key"]: item["etfs"] for item in POOL_OPTIONS}
CUSTOM_A_STOCK_POOL = "CUSTOM_A_STOCK"
CUSTOM_US_STOCK_POOL = "CUSTOM_US_STOCK"
CUSTOM_POOL_KEYS = {CUSTOM_A_STOCK_POOL, CUSTOM_US_STOCK_POOL}
CUSTOM_POOL_LABELS = {
    CUSTOM_A_STOCK_POOL: "自定义A股股票池",
    CUSTOM_US_STOCK_POOL: "自定义美股股票池",
}
CUSTOM_POOL_UNSUPPORTED_FACTOR_KEYS = {"index_weight"}

BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT = 8
BACKTEST_SEARCH_FACTOR_VALUES_CACHE_LIMIT = 8


@dataclass
class UniverseHistory:
    snapshot_dates_by_etf: Dict[str, List[date]]
    symbols_by_etf_date: Dict[str, Dict[date, List[str]]]
    all_symbols: List[str]
    holdings_date_count: Dict[str, int]

    def symbols_for_date(self, current_date: date) -> List[str]:
        symbols: List[str] = []
        for etf_symbol, snapshot_dates in self.snapshot_dates_by_etf.items():
            if not snapshot_dates:
                continue
            index = bisect.bisect_right(snapshot_dates, current_date) - 1
            if index < 0:
                continue
            snapshot_date = snapshot_dates[index]
            symbols.extend(self.symbols_by_etf_date.get(etf_symbol, {}).get(snapshot_date, []))
        return list(dict.fromkeys(symbols))


@dataclass(frozen=True)
class FactorBacktestLeg:
    factor: str
    window: Union[int, str] = 20
    weight: float = 1.0
    neutralization: str = "none"
    standardization: str = "rank_percentile"
    momentum_weights: Dict[str, float] = field(default_factory=lambda: DEFAULT_MOMENTUM_WEIGHTS.copy())


@dataclass(frozen=True)
class FactorBacktestConfig:
    pool: str = "SPY_QQQ"
    pool_label: Optional[str] = None
    candidate_etfs: Optional[List[str]] = None
    custom_symbols: List[str] = field(default_factory=list)
    start_date: date = date(2020, 1, 2)
    end_date: Optional[date] = None
    initial_capital: float = 100_000.0
    max_positions: int = 7
    position_weights: List[float] = field(default_factory=list)
    sell_rank_multiplier: float = DEFAULT_SELL_RANK_MULTIPLIER
    rebalance_frequency: str = DEFAULT_REBALANCE_FREQUENCY
    rotation_mode: str = DEFAULT_ROTATION_MODE
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    lot_size: int = 1
    min_listing_days: int = DEFAULT_MIN_LISTING_DAYS
    legs: List[FactorBacktestLeg] = field(default_factory=list)
    mode: str = "factor_backtest"
    strategy: str = "factor_lab_top_n_rotation"
    execution_price_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)
    execution_price_source_overrides: Dict[str, Dict[str, str]] = field(default_factory=dict)
    execution_quote_timestamp_overrides: Dict[str, Dict[str, str]] = field(default_factory=dict)
    execution_depth_overrides: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)


def _get_attr(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def default_virtual_factor_leg_payloads() -> List[Dict[str, Any]]:
    return [
        {
            **leg,
            "momentum_weights": dict(leg.get("momentum_weights") or DEFAULT_MOMENTUM_WEIGHTS),
        }
        for leg in DEFAULT_VIRTUAL_FACTOR_LEGS
    ]


def _round_or_none(value, digits: int = 2):
    if value is None:
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        return round(number, digits)
    except (TypeError, ValueError):
        return None


def _safe_float(value, digits: Optional[int] = None):
    if value is None:
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        return round(number, digits) if digits is not None else number
    except (TypeError, ValueError):
        return None


def _serialize_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float):
        return _safe_float(value)
    return value


def _quote_sql_string(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def _coerce_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _is_a_stock_symbol(symbol: str) -> bool:
    text = str(symbol or "").strip().upper()
    return text.endswith((".SH", ".SZ", ".BJ"))


def _is_a_stock_index_pool_symbol(symbol: str) -> bool:
    return str(symbol or "").strip().upper() in A_STOCK_INDEX_POOL_CODE_SET


def is_custom_pool(pool: Optional[str]) -> bool:
    return str(pool or "").strip().upper() in CUSTOM_POOL_KEYS


def is_custom_a_stock_pool(pool: Optional[str]) -> bool:
    return str(pool or "").strip().upper() == CUSTOM_A_STOCK_POOL


def is_custom_us_stock_pool(pool: Optional[str]) -> bool:
    return str(pool or "").strip().upper() == CUSTOM_US_STOCK_POOL


def normalize_a_stock_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper().replace(" ", "")
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text):
        return text
    if not re.fullmatch(r"\d{6}", text):
        return text
    if text.startswith(("43", "83", "87", "88", "92")):
        return f"{text}.BJ"
    if text.startswith(("5", "6", "9")):
        return f"{text}.SH"
    return f"{text}.SZ"


def normalize_custom_pool_symbols(pool: Optional[str], raw_symbols: Any) -> List[str]:
    pool_key = str(pool or "").strip().upper()
    if pool_key not in CUSTOM_POOL_KEYS:
        return []
    if isinstance(raw_symbols, (list, tuple, set)):
        items = list(raw_symbols)
    elif raw_symbols is None:
        items = []
    else:
        items = [raw_symbols]

    normalized: List[str] = []
    for item in items:
        text = str(item or "").strip().upper().replace(" ", "")
        if not text:
            continue
        if pool_key == CUSTOM_US_STOCK_POOL:
            if re.search(r"\.(SH|SZ|BJ)$", text):
                raise ValueError("自定义美股股票池只能选择美股代码")
            symbol = text if text.endswith(".US") else f"{text}.US"
            if not SYMBOL_PATTERN.match(symbol):
                raise ValueError(f"美股代码格式不正确: {item}")
        else:
            symbol = normalize_a_stock_symbol(text)
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise ValueError(f"A股代码格式不正确: {item}")
            if _is_a_stock_index_pool_symbol(symbol) or symbol == A_STOCK_INNO100_SYMBOL:
                raise ValueError("自定义A股股票池仅支持个股和ETF，不支持指数代码")
        if symbol not in normalized:
            normalized.append(symbol)

    if not normalized:
        raise ValueError("自定义股票池至少选择一个标的")
    return normalized


def _is_a_stock_pool(pool: Optional[str], candidate_etfs: Optional[List[str]] = None) -> bool:
    pool_key = str(pool or "").strip().upper()
    if pool_key == CUSTOM_A_STOCK_POOL:
        return True
    if pool_key == A_STOCK_INNO100_POOL or pool_key in A_STOCK_INDEX_POOL_CODE_SET:
        return True
    candidates = set(str(item).strip().upper() for item in (candidate_etfs or []))
    return bool(candidates.intersection({A_STOCK_INNO100_SYMBOL, *A_STOCK_INDEX_POOL_CODE_SET}))


def _existing_a_stock_fund_symbols(symbols: List[str], connection: Any = None) -> Set[str]:
    safe_symbols = [
        str(symbol or "").strip().upper()
        for symbol in list(dict.fromkeys(symbols or []))
        if symbol and SYMBOL_PATTERN.match(str(symbol).strip().upper()) and _is_a_stock_symbol(str(symbol).strip().upper())
    ]
    if not safe_symbols:
        return set()
    symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in safe_symbols)
    close_connection = connection is None
    conn = connection or _connect_duckdb()
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT ts_code
            FROM a_stock_fund_daily
            WHERE ts_code IN ({symbol_sql})
            """
        ).fetchall()
    finally:
        if close_connection:
            conn.close()
    return {str(row[0] or "").strip().upper() for row in rows if row and row[0]}


def _price_adjustment_metadata(candidate_etfs: List[str], universe_symbols: List[str]) -> Dict[str, Any]:
    symbols = [str(item or "").strip().upper() for item in universe_symbols if item]
    candidates = [str(item or "").strip().upper() for item in candidate_etfs if item]
    all_symbols = list(dict.fromkeys([*symbols, *candidates]))
    a_fund_symbol_set = set(A_STOCK_ETF_DAILY_SYMBOL_SET)
    try:
        a_fund_symbol_set.update(_existing_a_stock_fund_symbols(all_symbols))
    except Exception:
        pass
    has_us = any(symbol.endswith(".US") for symbol in all_symbols)
    has_a_index = any(_is_a_stock_index_pool_symbol(symbol) or symbol == A_STOCK_INNO100_SYMBOL for symbol in all_symbols)
    has_a_fund = any(symbol in a_fund_symbol_set for symbol in all_symbols)
    has_a_stock = any(
        _is_a_stock_symbol(symbol)
        and symbol not in a_fund_symbol_set
        and not _is_a_stock_index_pool_symbol(symbol)
        for symbol in all_symbols
    )

    sources: Dict[str, str] = {}
    if has_us:
        sources["us_stock"] = "duckdb.us_stock_daily"
    if has_a_stock:
        sources["a_stock"] = "duckdb.a_stock_market_daily_qfq"
    if has_a_fund:
        sources["a_stock_fund"] = "duckdb.a_stock_fund_daily_qfq"
    if has_a_index:
        sources["a_stock_index"] = "duckdb.a_stock_index_daily"

    if has_a_stock or has_a_fund:
        adjustment = "qfq"
    elif has_a_index and not has_us:
        adjustment = "raw_index"
    elif has_us and not (has_a_stock or has_a_fund or has_a_index):
        adjustment = "forward"
    else:
        adjustment = "mixed"
    return {"price_adjustment": adjustment, "price_sources": sources}


def _import_duckdb():
    try:
        import duckdb
    except Exception as exc:
        raise RuntimeError("DuckDB依赖不可用") from exc
    return duckdb


def _connect_duckdb():
    duckdb = _import_duckdb()
    try:
        return duckdb.connect(database=ANALYTICS_DB_PATH, read_only=True)
    except Exception as exc:
        raise RuntimeError(f"DuckDB分析库当前不可读，可能正在同步写入或被其他进程占用: {exc}") from exc


def _selected_inno100_rebalance_ids_by_date(
    db: ORMSession,
    start_date: date,
    end_date: date,
) -> Dict[date, int]:
    rows = (
        db.query(AStockInnovation100Rebalance)
        .filter(AStockInnovation100Rebalance.index_code == A_STOCK_INNO100_INDEX_CODE)
        .order_by(
            AStockInnovation100Rebalance.effective_date.asc(),
            AStockInnovation100Rebalance.rebalance_date.asc(),
            AStockInnovation100Rebalance.id.asc(),
        )
        .all()
    )
    dated_rows: List[tuple[date, int]] = []
    for row in rows:
        snapshot_date = row.effective_date or row.rebalance_date
        if snapshot_date and snapshot_date <= end_date:
            dated_rows.append((snapshot_date, int(row.id)))
    if not dated_rows:
        return {}

    id_by_date: Dict[date, int] = {}
    for snapshot_date, rebalance_id in dated_rows:
        if start_date <= snapshot_date <= end_date:
            id_by_date[snapshot_date] = rebalance_id
    pre_start = [(snapshot_date, rebalance_id) for snapshot_date, rebalance_id in dated_rows if snapshot_date < start_date]
    if pre_start:
        snapshot_date, rebalance_id = pre_start[-1]
        id_by_date[snapshot_date] = rebalance_id
    return dict(sorted(id_by_date.items(), key=lambda item: item[0]))


def get_max_trade_date(symbols: Optional[List[str]] = None) -> date:
    safe_symbols = [
        str(symbol or "").strip().upper()
        for symbol in (symbols or [])
        if symbol and SYMBOL_PATTERN.match(str(symbol).strip().upper())
    ]
    where_clause = ""
    if safe_symbols:
        symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in list(dict.fromkeys(safe_symbols)))
        where_clause = f" WHERE symbol IN ({symbol_sql})"
    connection = _connect_duckdb()
    try:
        row = connection.execute(f"SELECT MAX(trade_date) FROM us_stock_daily{where_clause}").fetchone()
    finally:
        connection.close()
    value = row[0] if row else None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return datetime.fromisoformat(str(value)).date()
    return date.today()


def _get_max_a_stock_market_date(symbols: Optional[List[str]] = None) -> Optional[date]:
    safe_symbols = [
        str(symbol or "").strip().upper()
        for symbol in (symbols or [])
        if symbol and SYMBOL_PATTERN.match(str(symbol).strip().upper())
    ]
    where_clause = ""
    if safe_symbols:
        symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in list(dict.fromkeys(safe_symbols)))
        where_clause = f" WHERE ts_code IN ({symbol_sql})"
    connection = _connect_duckdb()
    try:
        try:
            row = connection.execute(f"SELECT MAX(trade_date) FROM a_stock_market_daily_qfq{where_clause}").fetchone()
        except Exception:
            row = connection.execute(f"SELECT MAX(trade_date) FROM a_stock_market_daily{where_clause}").fetchone()
    finally:
        connection.close()
    return _coerce_date(row[0] if row else None)


def _get_max_a_stock_fund_date(symbols: Optional[List[str]] = None) -> Optional[date]:
    safe_symbols = [
        str(symbol or "").strip().upper()
        for symbol in (symbols or list(A_STOCK_ETF_DAILY_SYMBOL_SET))
        if symbol and SYMBOL_PATTERN.match(str(symbol).strip().upper())
    ]
    if not safe_symbols:
        return None
    symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in list(dict.fromkeys(safe_symbols)))
    connection = _connect_duckdb()
    try:
        try:
            row = connection.execute(
                f"""
                SELECT MAX(trade_date)
                FROM a_stock_fund_daily_qfq
                WHERE ts_code IN ({symbol_sql})
                """
            ).fetchone()
        except Exception:
            row = connection.execute(
                f"""
                SELECT MAX(trade_date)
                FROM a_stock_fund_daily
                WHERE ts_code IN ({symbol_sql})
                """
            ).fetchone()
    finally:
        connection.close()
    return _coerce_date(row[0] if row else None)


def get_max_a_stock_index_daily_date(index_codes: Optional[List[str]] = None) -> Optional[date]:
    safe_codes = [
        str(item or "").strip().upper()
        for item in (index_codes or list(A_STOCK_INDEX_POOL_CODES))
        if item and SYMBOL_PATTERN.match(str(item).strip().upper())
    ]
    if not safe_codes:
        return None
    code_sql = ", ".join(_quote_sql_string(code) for code in list(dict.fromkeys(safe_codes)))
    connection = _connect_duckdb()
    try:
        row = connection.execute(
            f"""
            SELECT MAX(trade_date)
            FROM a_stock_index_daily
            WHERE ts_code IN ({code_sql})
            """
        ).fetchone()
    finally:
        connection.close()
    return _coerce_date(row[0] if row else None)


def _get_latest_inno100_level_date(db: ORMSession) -> Optional[date]:
    row = (
        db.query(AStockInnovation100Level.date)
        .filter(AStockInnovation100Level.index_code == A_STOCK_INNO100_INDEX_CODE)
        .order_by(AStockInnovation100Level.date.desc())
        .first()
    )
    value = row[0] if row else None
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _resolve_backtest_end_date(request: FactorBacktestConfig, db: ORMSession) -> date:
    if request.end_date:
        return request.end_date
    pool_key = str(request.pool or "").strip().upper()
    if is_custom_us_stock_pool(pool_key):
        return get_max_trade_date(normalize_custom_pool_symbols(pool_key, request.custom_symbols))
    if is_custom_a_stock_pool(pool_key):
        symbols = normalize_custom_pool_symbols(pool_key, request.custom_symbols)
        fund_symbols = _existing_a_stock_fund_symbols(symbols)
        stock_symbols = [
            symbol
            for symbol in symbols
            if _is_a_stock_symbol(symbol) and symbol not in fund_symbols
        ]
        candidates = []
        if stock_symbols:
            candidates.append(_get_max_a_stock_market_date(stock_symbols))
        if fund_symbols:
            candidates.append(_get_max_a_stock_fund_date(list(fund_symbols)))
        candidates = [item for item in candidates if item is not None]
        return min(candidates) if candidates else date.today()
    if not _is_a_stock_pool(request.pool, request.candidate_etfs):
        return get_max_trade_date()
    candidate_etfs = list(
        dict.fromkeys(
            str(item or "").strip().upper()
            for item in (request.candidate_etfs or POOL_ETFS.get(pool_key, []))
            if item
        )
    )
    candidates = [_get_max_a_stock_market_date()]
    if pool_key == A_STOCK_INNO100_POOL or A_STOCK_INNO100_SYMBOL in candidate_etfs:
        candidates.append(_get_latest_inno100_level_date(db))
    index_codes = [item for item in candidate_etfs if _is_a_stock_index_pool_symbol(item)]
    if index_codes:
        candidates.append(get_max_a_stock_index_daily_date(index_codes))
    candidates = [item for item in candidates if item is not None]
    return min(candidates) if candidates else date.today()


def load_price_frame(symbols: List[str], start_date: date, end_date: date) -> pl.DataFrame:
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
    connection = _connect_duckdb()
    try:
        us_symbols = [symbol for symbol in safe_symbols if str(symbol).upper().endswith(".US")]
        if us_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in us_symbols)
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
            frames.append(
                pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]})
            )

        a_index_symbols = [symbol for symbol in safe_symbols if _is_a_stock_index_pool_symbol(symbol)]
        if a_index_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_index_symbols)
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
            frames.append(
                pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]})
            )

        a_price_symbols = [
            symbol
            for symbol in safe_symbols
            if _is_a_stock_symbol(symbol) and not _is_a_stock_index_pool_symbol(symbol)
        ]
        a_fund_symbol_set = {symbol for symbol in a_price_symbols if symbol in A_STOCK_ETF_DAILY_SYMBOL_SET}
        try:
            a_fund_symbol_set.update(_existing_a_stock_fund_symbols(a_price_symbols, connection))
        except Exception:
            pass
        a_fund_symbols = [symbol for symbol in a_price_symbols if symbol in a_fund_symbol_set]
        if a_fund_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_fund_symbols)
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
            frames.append(
                pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]})
            )

        a_symbols = [symbol for symbol in a_price_symbols if symbol not in a_fund_symbol_set]
        if a_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_symbols)
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
            frames.append(
                pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]})
            )
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


def normalize_rebalance_frequency(value) -> str:
    text = str(value or DEFAULT_REBALANCE_FREQUENCY).strip().lower()
    return text if text in SUPPORTED_REBALANCE_FREQUENCIES else DEFAULT_REBALANCE_FREQUENCY


def normalize_rotation_mode(value) -> str:
    text = str(value or DEFAULT_ROTATION_MODE).strip().lower()
    return text if text in SUPPORTED_ROTATION_MODES else DEFAULT_ROTATION_MODE


def normalize_position_weights(raw_weights: Any, max_positions: Any = 1) -> List[float]:
    if isinstance(raw_weights, str):
        items = [item for item in re.split(r"[:：,，\s]+", raw_weights.strip()) if item]
    elif isinstance(raw_weights, (list, tuple)):
        items = list(raw_weights)
    elif raw_weights is None:
        items = []
    else:
        items = [raw_weights]

    weights: List[float] = []
    for item in items:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            weights.append(number)

    if not weights:
        try:
            position_count = int(max_positions or 1)
        except (TypeError, ValueError):
            position_count = 1
        position_count = max(1, min(100, position_count))
        return [round(1.0 / position_count, 10) for _ in range(position_count)]

    total = sum(weights)
    if total <= 0 or not math.isfinite(total):
        raise ValueError("仓位权重之和必须大于0")
    if total > 1.000001:
        weights = [weight / total for weight in weights]
    if len(weights) > 100:
        raise ValueError("仓位权重最多支持100个标的")
    return [round(float(weight), 10) for weight in weights]


def format_position_weights(weights: List[float]) -> str:
    return ":".join(f"{float(weight):.4f}".rstrip("0").rstrip(".") for weight in weights)


def is_rebalance_day(dates: List[date], index: int, frequency: str = DEFAULT_REBALANCE_FREQUENCY) -> bool:
    if index >= len(dates) - 1:
        return True
    current_date = dates[index]
    next_date = dates[index + 1]
    frequency = normalize_rebalance_frequency(frequency)
    if frequency == "daily":
        return True
    if frequency == "weekly":
        return current_date.isocalendar()[:2] != next_date.isocalendar()[:2]
    if frequency == "monthly":
        return (current_date.year, current_date.month) != (next_date.year, next_date.month)
    if frequency == "quarterly":
        current_quarter = (current_date.month - 1) // 3
        next_quarter = (next_date.month - 1) // 3
        return (current_date.year, current_quarter) != (next_date.year, next_quarter)
    if frequency == "semiannual":
        current_half = (current_date.month - 1) // 6
        next_half = (next_date.month - 1) // 6
        return (current_date.year, current_half) != (next_date.year, next_half)
    return False


def floor_lot(quantity: float, lot_size: int = 1) -> int:
    lot = max(1, int(lot_size or 1))
    if quantity <= 0:
        return 0
    return int(quantity // lot) * lot


def _depth_levels(depth_payload: Optional[Dict[str, Any]], side: str) -> List[Dict[str, float]]:
    if not depth_payload:
        return []
    levels = depth_payload.get(side) or []
    normalized = []
    for level in levels:
        price = _safe_float(level.get("price") if isinstance(level, dict) else None)
        volume = _safe_float(level.get("volume") if isinstance(level, dict) else None)
        if price is None or volume is None or price <= 0 or volume <= 0:
            continue
        normalized.append({"price": price, "volume": volume})
    return normalized


def estimate_depth_execution_price(
    depth_payload: Optional[Dict[str, Any]],
    action: str,
    quantity: Optional[int] = None,
    budget: Optional[float] = None,
) -> Optional[float]:
    action = str(action or "").upper()
    levels = _depth_levels(depth_payload, "bid" if action == "SELL" else "ask")
    if not levels:
        return None

    target_quantity = int(quantity or 0)
    if action == "BUY" and target_quantity <= 0 and budget and budget > 0:
        remaining_budget = float(budget)
        total_quantity = 0.0
        total_amount = 0.0
        for level in levels:
            price = float(level["price"])
            volume = float(level["volume"])
            affordable_quantity = math.floor(remaining_budget / price) if price > 0 else 0
            fill_quantity = min(volume, affordable_quantity)
            if fill_quantity <= 0:
                break
            total_quantity += fill_quantity
            total_amount += fill_quantity * price
            remaining_budget -= fill_quantity * price
            if remaining_budget < price:
                break
        return total_amount / total_quantity if total_quantity > 0 else None

    if target_quantity <= 0:
        return float(levels[0]["price"])

    remaining_quantity = float(target_quantity)
    total_quantity = 0.0
    total_amount = 0.0
    for level in levels:
        fill_quantity = min(float(level["volume"]), remaining_quantity)
        if fill_quantity <= 0:
            continue
        total_quantity += fill_quantity
        total_amount += fill_quantity * float(level["price"])
        remaining_quantity -= fill_quantity
        if remaining_quantity <= 0:
            break
    if total_quantity <= 0:
        return None
    return total_amount / total_quantity


def portfolio_value(cash: float, positions: Dict[str, Dict], last_prices: Dict[str, float]) -> float:
    value = float(cash or 0)
    for symbol, position in positions.items():
        price = last_prices.get(symbol) or position.get("last_price") or position.get("avg_cost") or 0
        value += int(position.get("shares") or 0) * float(price or 0)
    return value


def _compute_period_return(start_value, end_value) -> Optional[float]:
    if start_value is None or end_value is None:
        return None
    try:
        start = float(start_value)
        end = float(end_value)
    except (TypeError, ValueError):
        return None
    if start <= 0:
        return None
    return (end / start - 1) * 100


def build_benchmark_curve(
    benchmark_rows_by_symbol: Dict[str, List[Dict]],
    dates: List[date],
    initial_capital: float,
    start_date: date,
) -> List[Dict]:
    row_by_symbol_date = {
        symbol: {row["date"]: row for row in rows}
        for symbol, rows in benchmark_rows_by_symbol.items()
    }
    initial_close_by_symbol: Dict[str, float] = {}
    last_close_by_symbol: Dict[str, float] = {}
    for symbol, rows in benchmark_rows_by_symbol.items():
        first_row = next(
            (row for row in rows if row["date"] >= start_date and _safe_float(row.get("close")) and float(row["close"]) > 0),
            None,
        )
        if first_row:
            initial_close_by_symbol[symbol] = float(first_row["close"])

    benchmark_curve = []
    for current_date in dates:
        values = {}
        for symbol, initial_close in initial_close_by_symbol.items():
            row = row_by_symbol_date.get(symbol, {}).get(current_date)
            if row and _safe_float(row.get("close")) and float(row["close"]) > 0:
                last_close_by_symbol[symbol] = float(row["close"])
            latest_close = last_close_by_symbol.get(symbol)
            values[symbol] = (
                _round_or_none(float(initial_capital or 0) * latest_close / initial_close, 2)
                if latest_close and initial_close > 0
                else None
            )
        primary_symbol = next(iter(values), None)
        benchmark_curve.append({
            "date": current_date.isoformat(),
            "value": values.get(primary_symbol) if primary_symbol else None,
            "values": values,
        })
    return benchmark_curve


def build_yearly_stats(
    equity_curve: List[Dict],
    benchmark_curve: List[Dict],
    benchmark_symbols: List[str],
) -> List[Dict]:
    if not equity_curve:
        return []
    benchmark_by_date = {item["date"]: item for item in benchmark_curve or []}
    buckets: Dict[int, Dict[str, Any]] = {}
    for item in equity_curve:
        item_date = date.fromisoformat(item["date"])
        bucket = buckets.setdefault(
            item_date.year,
            {
                "start_date": item["date"],
                "end_date": item["date"],
                "start_value": item.get("value"),
                "end_value": item.get("value"),
                "benchmark_start": {},
                "benchmark_end": {},
            },
        )
        bucket["end_date"] = item["date"]
        bucket["end_value"] = item.get("value")
        benchmark_item = benchmark_by_date.get(item["date"]) or {}
        values = benchmark_item.get("values") or {}
        for symbol in benchmark_symbols:
            if values.get(symbol) is None:
                continue
            bucket["benchmark_start"].setdefault(symbol, values.get(symbol))
            bucket["benchmark_end"][symbol] = values.get(symbol)

    yearly_stats = []
    for year in sorted(buckets):
        bucket = buckets[year]
        strategy_return = _compute_period_return(bucket.get("start_value"), bucket.get("end_value"))
        benchmark_returns = {}
        excess_returns = {}
        outperformed_by_symbol = {}
        valid_outperformance = []
        for symbol in benchmark_symbols:
            benchmark_return = _compute_period_return(
                bucket.get("benchmark_start", {}).get(symbol),
                bucket.get("benchmark_end", {}).get(symbol),
            )
            benchmark_returns[symbol] = _round_or_none(benchmark_return, 2)
            if strategy_return is not None and benchmark_return is not None:
                excess = strategy_return - benchmark_return
                excess_returns[symbol] = _round_or_none(excess, 2)
                outperformed = excess > 0
                outperformed_by_symbol[symbol] = outperformed
                valid_outperformance.append(outperformed)
            else:
                excess_returns[symbol] = None
                outperformed_by_symbol[symbol] = None

        primary_symbol = (benchmark_symbols or [None])[0]
        yearly_stats.append({
            "year": year,
            "start_date": bucket.get("start_date"),
            "end_date": bucket.get("end_date"),
            "strategy_return_pct": _round_or_none(strategy_return, 2),
            "benchmark_returns_pct": benchmark_returns,
            "excess_returns_pct": excess_returns,
            "outperformed_by_symbol": outperformed_by_symbol,
            "outperformed_all": all(valid_outperformance) if valid_outperformance else None,
            "primary_benchmark_symbol": primary_symbol,
            "primary_benchmark_return_pct": benchmark_returns.get(primary_symbol) if primary_symbol else None,
            "primary_excess_return_pct": excess_returns.get(primary_symbol) if primary_symbol else None,
            "primary_outperformed": outperformed_by_symbol.get(primary_symbol) if primary_symbol else None,
        })
    return yearly_stats


def _selected_a_stock_index_weight_dates(index_code: str, start_date: date, end_date: date) -> List[date]:
    connection = _connect_duckdb()
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT CAST(trade_date AS DATE) AS trade_date
            FROM a_stock_index_weight
            WHERE index_code = ?
              AND trade_date <= ?
            ORDER BY trade_date ASC
            """,
            [index_code, end_date],
        ).fetchall()
    finally:
        connection.close()

    all_dates = [_coerce_date(row[0]) for row in rows]
    all_dates = [item for item in all_dates if item is not None]
    pre_start_dates = [item for item in all_dates if item < start_date]
    selected_dates = {item for item in all_dates if start_date <= item <= end_date}
    if pre_start_dates:
        selected_dates.add(pre_start_dates[-1])
    return sorted(selected_dates)


def _load_a_stock_index_weight_rows(index_code: str, selected_dates: List[date]):
    if not selected_dates:
        return []
    date_sql = ", ".join(_quote_sql_string(item.isoformat()) for item in selected_dates)
    connection = _connect_duckdb()
    try:
        return connection.execute(
            f"""
            SELECT
                CAST(trade_date AS DATE) AS trade_date,
                con_code,
                CAST(weight AS DOUBLE) AS weight
            FROM a_stock_index_weight
            WHERE index_code = ?
              AND trade_date IN ({date_sql})
            ORDER BY trade_date ASC, weight DESC
            """,
            [index_code],
        ).fetchall()
    finally:
        connection.close()


def load_universe_history(
    db: ORMSession,
    candidate_etfs: List[str],
    start_date: date,
    end_date: date,
) -> UniverseHistory:
    candidate_etfs = list(
        dict.fromkeys(str(item or "").strip().upper() for item in (candidate_etfs or DEFAULT_CANDIDATE_ETFS) if item)
    )
    snapshot_dates_by_etf: Dict[str, List[date]] = {}
    symbols_by_etf_date: Dict[str, Dict[date, List[str]]] = {}
    all_symbols: Set[str] = set()
    holdings_date_count: Dict[str, int] = {}

    for etf_symbol in candidate_etfs:
        if etf_symbol == A_STOCK_INNO100_SYMBOL:
            selected_ids_by_date = _selected_inno100_rebalance_ids_by_date(db, start_date, end_date)
            snapshot_dates_by_etf[etf_symbol] = sorted(selected_ids_by_date)
            holdings_date_count[etf_symbol] = len(selected_ids_by_date)
            symbols_by_etf_date[etf_symbol] = {}
            if not selected_ids_by_date:
                continue
            date_by_id = {rebalance_id: snapshot_date for snapshot_date, rebalance_id in selected_ids_by_date.items()}
            rows = (
                db.query(AStockInnovation100Constituent)
                .filter(
                    AStockInnovation100Constituent.index_code == A_STOCK_INNO100_INDEX_CODE,
                    AStockInnovation100Constituent.rebalance_id.in_(list(date_by_id)),
                )
                .order_by(AStockInnovation100Constituent.rebalance_id.asc(), AStockInnovation100Constituent.weight_pct.desc())
                .all()
            )
            for row in rows:
                symbol = str(row.ts_code or "").strip().upper()
                snapshot_date = date_by_id.get(int(row.rebalance_id))
                if not symbol or not snapshot_date:
                    continue
                symbols_by_etf_date[etf_symbol].setdefault(snapshot_date, [])
                if symbol not in symbols_by_etf_date[etf_symbol][snapshot_date]:
                    symbols_by_etf_date[etf_symbol][snapshot_date].append(symbol)
                all_symbols.add(symbol)
            continue

        if _is_a_stock_index_pool_symbol(etf_symbol):
            index_code = str(etf_symbol).strip().upper()
            selected_dates = _selected_a_stock_index_weight_dates(index_code, start_date, end_date)
            snapshot_dates_by_etf[index_code] = selected_dates
            holdings_date_count[index_code] = len(selected_dates)
            symbols_by_etf_date[index_code] = {}
            for row in _load_a_stock_index_weight_rows(index_code, selected_dates):
                snapshot_date = _coerce_date(row[0])
                symbol = str(row[1] or "").strip().upper()
                if not snapshot_date or not symbol:
                    continue
                symbols_by_etf_date[index_code].setdefault(snapshot_date, [])
                if symbol not in symbols_by_etf_date[index_code][snapshot_date]:
                    symbols_by_etf_date[index_code][snapshot_date].append(symbol)
                all_symbols.add(symbol)
            continue

        all_dates = [
            row[0]
            for row in (
                db.query(distinct(ETFHolding.date))
                .filter(ETFHolding.etf_symbol == etf_symbol, ETFHolding.date <= end_date)
                .order_by(ETFHolding.date.asc())
                .all()
            )
        ]
        pre_start_dates = [item for item in all_dates if item < start_date]
        selected_dates = {item for item in all_dates if start_date <= item <= end_date}
        if pre_start_dates:
            selected_dates.add(pre_start_dates[-1])

        selected_dates = set(selected_dates)
        snapshot_dates_by_etf[etf_symbol] = sorted(selected_dates)
        holdings_date_count[etf_symbol] = len(selected_dates)
        symbols_by_etf_date[etf_symbol] = {}
        if not selected_dates:
            continue

        rows = (
            db.query(ETFHolding)
            .filter(
                ETFHolding.etf_symbol == etf_symbol,
                ETFHolding.date.in_(selected_dates),
                or_(ETFHolding.asset_class == "Equity", ETFHolding.asset_class == "EQUITY"),
            )
            .order_by(ETFHolding.date.asc(), ETFHolding.weight.desc())
            .all()
        )
        for row in rows:
            symbol = normalize_us_equity_symbol(row.symbol)
            if not symbol or not symbol.endswith(".US"):
                continue
            symbols_by_etf_date[etf_symbol].setdefault(row.date, [])
            if symbol not in symbols_by_etf_date[etf_symbol][row.date]:
                symbols_by_etf_date[etf_symbol][row.date].append(symbol)
            all_symbols.add(symbol)

    return UniverseHistory(
        snapshot_dates_by_etf=snapshot_dates_by_etf,
        symbols_by_etf_date=symbols_by_etf_date,
        all_symbols=sorted(all_symbols),
        holdings_date_count=holdings_date_count,
    )


def load_universe_weight_history(
    db: ORMSession,
    candidate_etfs: List[str],
    start_date: date,
    end_date: date,
) -> Dict[str, Dict[date, Dict[str, float]]]:
    candidate_etfs = list(
        dict.fromkeys(str(item or "").strip().upper() for item in (candidate_etfs or DEFAULT_CANDIDATE_ETFS) if item)
    )
    weight_history: Dict[str, Dict[date, Dict[str, float]]] = {}
    for etf_symbol in candidate_etfs:
        if etf_symbol == A_STOCK_INNO100_SYMBOL:
            selected_ids_by_date = _selected_inno100_rebalance_ids_by_date(db, start_date, end_date)
            if not selected_ids_by_date:
                continue
            date_by_id = {rebalance_id: snapshot_date for snapshot_date, rebalance_id in selected_ids_by_date.items()}
            weight_history[etf_symbol] = {}
            rows = (
                db.query(AStockInnovation100Constituent)
                .filter(
                    AStockInnovation100Constituent.index_code == A_STOCK_INNO100_INDEX_CODE,
                    AStockInnovation100Constituent.rebalance_id.in_(list(date_by_id)),
                )
                .order_by(AStockInnovation100Constituent.rebalance_id.asc(), AStockInnovation100Constituent.weight_pct.desc())
                .all()
            )
            for row in rows:
                symbol = str(row.ts_code or "").strip().upper()
                snapshot_date = date_by_id.get(int(row.rebalance_id))
                if not symbol or not snapshot_date:
                    continue
                weight_history[etf_symbol].setdefault(snapshot_date, {})
                weight_history[etf_symbol][snapshot_date][symbol] = float(row.weight_pct or 0) / 100.0
            continue

        if _is_a_stock_index_pool_symbol(etf_symbol):
            index_code = str(etf_symbol).strip().upper()
            selected_dates = _selected_a_stock_index_weight_dates(index_code, start_date, end_date)
            if not selected_dates:
                continue
            weight_history[index_code] = {}
            for row in _load_a_stock_index_weight_rows(index_code, selected_dates):
                snapshot_date = _coerce_date(row[0])
                symbol = str(row[1] or "").strip().upper()
                if not snapshot_date or not symbol:
                    continue
                weight = _safe_float(row[2])
                if weight is None or weight <= 0:
                    continue
                weight_history[index_code].setdefault(snapshot_date, {})
                weight_history[index_code][snapshot_date][symbol] = weight / 100.0
            continue

        all_dates = [
            row[0]
            for row in (
                db.query(distinct(ETFHolding.date))
                .filter(ETFHolding.etf_symbol == etf_symbol, ETFHolding.date <= end_date)
                .order_by(ETFHolding.date.asc())
                .all()
            )
        ]
        pre_start_dates = [item for item in all_dates if item < start_date]
        selected_dates = {item for item in all_dates if start_date <= item <= end_date}
        if pre_start_dates:
            selected_dates.add(pre_start_dates[-1])
        if not selected_dates:
            continue

        weight_history[etf_symbol] = {}
        rows = (
            db.query(ETFHolding)
            .filter(
                ETFHolding.etf_symbol == etf_symbol,
                ETFHolding.date.in_(selected_dates),
                or_(ETFHolding.asset_class == "Equity", ETFHolding.asset_class == "EQUITY"),
            )
            .order_by(ETFHolding.date.asc(), ETFHolding.weight.desc())
            .all()
        )
        for row in rows:
            symbol = normalize_us_equity_symbol(row.symbol)
            if not symbol or not symbol.endswith(".US"):
                continue
            weight_history[etf_symbol].setdefault(row.date, {})
            weight_history[etf_symbol][row.date][symbol] = float(row.weight or 0)
    return weight_history


def build_static_universe_history(pool_key: str, symbols: List[str], start_date: date) -> UniverseHistory:
    normalized_symbols = list(dict.fromkeys(str(symbol or "").strip().upper() for symbol in symbols if symbol))
    return UniverseHistory(
        snapshot_dates_by_etf={pool_key: [start_date]},
        symbols_by_etf_date={pool_key: {start_date: normalized_symbols}},
        all_symbols=normalized_symbols,
        holdings_date_count={pool_key: 1},
    )


def build_static_equal_weight_history(pool_key: str, symbols: List[str], start_date: date) -> Dict[str, Dict[date, Dict[str, float]]]:
    normalized_symbols = list(dict.fromkeys(str(symbol or "").strip().upper() for symbol in symbols if symbol))
    if not normalized_symbols:
        return {}
    weight = 1.0 / len(normalized_symbols)
    return {pool_key: {start_date: {symbol: weight for symbol in normalized_symbols}}}


def _load_industry_frame(
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    if not symbols:
        return pl.DataFrame()
    frames: List[pl.DataFrame] = []
    us_symbols = [symbol for symbol in symbols if str(symbol).upper().endswith(".US")]
    if us_symbols:
        rows = (
            db.query(
                USStockIndustrySnapshot.symbol,
                USStockIndustrySnapshot.date,
                USStockIndustrySnapshot.sector,
                USStockIndustrySnapshot.industry_group,
                USStockIndustrySnapshot.industry,
                USStockIndustrySnapshot.sub_industry,
                USStockIndustrySnapshot.market_cap,
            )
            .filter(
                USStockIndustrySnapshot.symbol.in_(us_symbols),
                USStockIndustrySnapshot.provider == "fmp",
            )
            .all()
        )
        if rows:
            frames.append(
                pl.DataFrame(
                    {
                        "symbol": [row.symbol for row in rows],
                        "industry_date": [row.date for row in rows],
                        "sector": [row.sector for row in rows],
                        "industry_group": [row.industry_group for row in rows],
                        "industry": [row.industry for row in rows],
                        "sub_industry": [row.sub_industry for row in rows],
                        "market_cap": [row.market_cap for row in rows],
                    }
                )
                .with_columns(
                    pl.col("industry_date").cast(pl.Date),
                    pl.col("market_cap").cast(pl.Float64),
                )
                .sort(["symbol", "industry_date"])
                .unique(subset=["symbol"], keep="last")
            )

    a_symbols = [symbol for symbol in symbols if _is_a_stock_symbol(symbol)]
    if a_symbols:
        symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_symbols)
        query = f"""
            SELECT
                ts_code AS symbol,
                industry AS sector,
                industry AS industry_group,
                industry AS industry,
                industry AS sub_industry,
                CAST(NULL AS DOUBLE) AS market_cap
            FROM a_stock_basic
            WHERE ts_code IN ({symbol_sql})
        """
        connection = _connect_duckdb()
        try:
            a_frame = pl.read_database(query, connection)
        finally:
            connection.close()
        if not a_frame.is_empty():
            frames.append(
                a_frame.with_columns(
                    pl.lit(end_date).cast(pl.Date).alias("industry_date"),
                    pl.col("market_cap").cast(pl.Float64),
                )
            )

    non_empty_frames = [frame for frame in frames if frame is not None and not frame.is_empty()]
    if not non_empty_frames:
        return pl.DataFrame()
    return (
        pl.concat(non_empty_frames, how="vertical_relaxed")
        .select(["symbol", "industry_date", "sector", "industry_group", "industry", "sub_industry", "market_cap"])
        .sort("symbol")
    )


def _factor_required_windows(window: Union[int, str], factor_definition: FactorDefinition) -> List[int]:
    if not factor_definition.supports_windows:
        return factor_definition.default_windows
    if window == MIXED_WINDOW_KEY:
        return factor_definition.default_windows
    return [int(window)]


def _weights_for_leg(leg: Dict[str, Any]) -> Dict[int, float]:
    windows = leg["windows"]
    if leg["window"] == MIXED_WINDOW_KEY:
        return normalize_momentum_weights(leg["momentum_weights"], windows)
    return normalize_momentum_weights({str(windows[0]): 1.0}, windows)


def unsupported_factor_keys_for_pool(pool: Optional[str]) -> Set[str]:
    if not is_custom_pool(pool):
        return set()
    return {
        key
        for key, definition in FACTOR_REGISTRY.items()
        if "custom" in (definition.unsupported_pool_types or []) or key in CUSTOM_POOL_UNSUPPORTED_FACTOR_KEYS
    }


def validate_factor_legs_for_pool(pool: Optional[str], legs: List[Any]) -> None:
    unsupported_keys = unsupported_factor_keys_for_pool(pool)
    if not unsupported_keys:
        return
    used_keys: List[str] = []
    for leg in legs or []:
        factor_key = _get_attr(leg, "factor")
        if isinstance(factor_key, dict):
            factor_key = factor_key.get("key")
        elif factor_key is None and isinstance(leg, dict):
            factor_payload = leg.get("factor")
            factor_key = factor_payload.get("key") if isinstance(factor_payload, dict) else factor_payload
        if factor_key in unsupported_keys and factor_key not in used_keys:
            used_keys.append(factor_key)
    if not used_keys:
        return
    labels = [
        FACTOR_REGISTRY.get(key).label if FACTOR_REGISTRY.get(key) else key
        for key in used_keys
    ]
    raise ValueError(f"自定义股票池不支持因子: {', '.join(labels)}")


def resolve_factor_legs(legs: List[Any]) -> List[Dict[str, Any]]:
    resolved: List[Dict[str, Any]] = []
    for index, leg in enumerate(legs):
        factor_key = _get_attr(leg, "factor")
        factor_definition = FACTOR_REGISTRY.get(factor_key)
        if not factor_definition:
            raise ValueError(f"未注册的组合子因子: {factor_key}")
        window = _get_attr(leg, "window", 20)
        if isinstance(window, str) and window.strip().lower() == MIXED_WINDOW_KEY:
            window = MIXED_WINDOW_KEY
        elif factor_definition.supports_windows:
            window = int(window)
        if window == MIXED_WINDOW_KEY and not factor_definition.supports_mixed_windows:
            raise ValueError(f"{factor_definition.label} 不支持多窗口合成")
        active_windows = _factor_required_windows(window, factor_definition)
        neutralization = _get_attr(leg, "neutralization", "none")
        standardization = _get_attr(leg, "standardization", "rank_percentile")
        resolved.append(
            {
                "index": index,
                "key": f"component_{index + 1}",
                "column": f"_component_{index + 1}",
                "factor_definition": factor_definition,
                "factor": factor_definition.to_option(),
                "window": window,
                "window_label": "多窗口合成" if window == MIXED_WINDOW_KEY else (f"{window}日" if factor_definition.supports_windows else "固定"),
                "windows": active_windows,
                "raw_weight": float(_get_attr(leg, "weight", 1.0)),
                "neutralization": neutralization,
                "neutralization_label": NEUTRALIZATION_OPTIONS.get(neutralization, NEUTRALIZATION_OPTIONS["none"])["label"],
                "standardization": standardization,
                "standardization_label": STANDARDIZATION_OPTIONS.get(standardization, STANDARDIZATION_OPTIONS["rank_percentile"])["label"],
                "momentum_weights": normalize_momentum_weights_payload(_get_attr(leg, "momentum_weights", DEFAULT_MOMENTUM_WEIGHTS)),
            }
        )
    total_abs_weight = sum(abs(item["raw_weight"]) for item in resolved)
    if total_abs_weight <= 0:
        raise ValueError("至少设置一个非0因子权重")
    return [{**item, "weight": item["raw_weight"] / total_abs_weight} for item in resolved]


def required_windows_for_legs(legs: List[Dict[str, Any]]) -> List[int]:
    required: List[int] = []
    for leg in legs:
        required.extend(int(item) for item in leg["windows"])
    return list(dict.fromkeys(required)) or SUPPORTED_WINDOWS.copy()


def _backtest_leg_factor_cache_key(leg: Dict[str, Any]) -> Any:
    momentum_weights = _weights_for_leg(leg)
    return (
        leg["factor"]["key"],
        str(leg["window"]),
        tuple(int(item) for item in leg["windows"]),
        leg["neutralization"],
        leg["standardization"],
        tuple((int(window), round(float(weight), 10)) for window, weight in sorted(momentum_weights.items())),
    )


def _put_limited_cache(cache: Dict[Any, Any], key: Any, value: Any, limit: int):
    if key in cache:
        cache[key] = value
        return
    while len(cache) >= max(1, int(limit)):
        cache.pop(next(iter(cache)))
    cache[key] = value


def _prepare_cached_leg_factor_frame(
    price_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    leg_context: FactorContext,
    leg_request: Any,
    raw_factor_cache: Optional[Dict[Any, pl.DataFrame]],
) -> pl.DataFrame:
    if factor_definition.key in MOMENTUM_FACTOR_SCORE_PREFIX:
        source_df = _momentum_score_source_frame(price_df, factor_definition.key, leg_context.windows, raw_factor_cache)
        return _prepare_momentum_factor_frame_from_source(source_df, factor_definition, leg_context, leg_request)
    return _prepare_factor_frame(price_df, factor_definition, leg_context, leg_request)


def _prepare_composite_factor_frame(
    price_df: pl.DataFrame,
    request: FactorBacktestConfig,
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
    analysis_dates: List[date],
    industry_df: Optional[pl.DataFrame],
    resolved_legs: List[Dict[str, Any]],
    candidate_etfs: List[str],
    valuation_df: Optional[pl.DataFrame] = None,
    weight_history: Optional[Dict[str, Dict[date, Dict[str, float]]]] = None,
    raw_factor_cache: Optional[Dict[Any, pl.DataFrame]] = None,
    component_factor_cache: Optional[Dict[Any, pl.DataFrame]] = None,
) -> pl.DataFrame:
    active_legs = [leg for leg in resolved_legs if abs(float(leg.get("weight") or 0)) > 1e-12]
    base_columns = [column for column in ["symbol", "trade_date", "open", "high", "low", "close", "volume", "turnover", "_first_trade_date"] if column in price_df.columns]
    composite_df = price_df.select(base_columns).unique(subset=["symbol", "trade_date"])
    for leg in active_legs:
        factor_definition = leg["factor_definition"]
        momentum_weights = _weights_for_leg(leg)
        leg_context = FactorContext(
            windows=leg["windows"],
            momentum_weights=momentum_weights,
            db=db,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            analysis_dates=analysis_dates,
            industry_df=industry_df,
            candidate_etfs=candidate_etfs,
            valuation_df=valuation_df,
            weight_history=weight_history,
            weight_history_loader=load_universe_weight_history,
        )
        leg_request = SimpleNamespace(neutralization=leg["neutralization"], standardization=leg["standardization"])
        leg_cache_key = _backtest_leg_factor_cache_key(leg)
        cache_component = leg["window"] != MIXED_WINDOW_KEY
        factor_df = component_factor_cache.get(leg_cache_key) if cache_component and component_factor_cache is not None else None
        if factor_df is None:
            factor_df = _prepare_cached_leg_factor_frame(
                price_df,
                factor_definition,
                leg_context,
                leg_request,
                raw_factor_cache,
            ).select("symbol", "trade_date", "factor_value")
            if cache_component and component_factor_cache is not None:
                _put_limited_cache(component_factor_cache, leg_cache_key, factor_df, BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT)
        composite_df = composite_df.join(
            factor_df.select("symbol", "trade_date", pl.col("factor_value").alias(leg["column"])),
            on=["symbol", "trade_date"],
            how="left",
        )
    composite_expr = None
    for leg in active_legs:
        if leg["column"] not in composite_df.columns:
            continue
        expr = pl.col(leg["column"]) * float(leg["weight"])
        composite_expr = expr if composite_expr is None else composite_expr + expr
    if composite_expr is None:
        return composite_df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return composite_df.with_columns(composite_expr.alias("factor_value")).with_columns(pl.col("factor_value").alias("factor_value_raw"))


def _filter_min_listing_days(df: pl.DataFrame, min_listing_days: int) -> pl.DataFrame:
    if df.is_empty() or min_listing_days <= 0:
        return df
    source = df
    if "_first_trade_date" not in source.columns:
        source = source.with_columns(pl.min("trade_date").over("symbol").alias("_first_trade_date"))
    return source.with_columns((pl.col("trade_date") - pl.col("_first_trade_date")).dt.total_days().alias("_listing_days")).filter(
        pl.col("_listing_days") >= int(min_listing_days)
    )


def _price_frame_to_rows_by_symbol(price_df: pl.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    rows_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    if price_df.is_empty():
        return rows_by_symbol
    columns = [column for column in ["symbol", "trade_date", "open", "close", "volume", "turnover"] if column in price_df.columns]
    for row in price_df.select(columns).sort(["symbol", "trade_date"]).to_dicts():
        symbol = row.get("symbol")
        row_date = row.get("trade_date")
        open_price = _safe_float(row.get("open"))
        close_price = _safe_float(row.get("close"))
        if not symbol or not row_date or open_price is None or close_price is None or open_price <= 0 or close_price <= 0:
            continue
        rows_by_symbol.setdefault(symbol, []).append(
            {
                "date": row_date,
                "open": open_price,
                "close": close_price,
                "volume": _safe_float(row.get("volume")) or 0.0,
                "turnover": _safe_float(row.get("turnover")) or 0.0,
            }
        )
    return rows_by_symbol


def _load_inno100_benchmark_rows(db: ORMSession, start_date: date, end_date: date) -> Dict[str, List[Dict[str, Any]]]:
    rows = (
        db.query(AStockInnovation100Level.date, AStockInnovation100Level.level)
        .filter(
            AStockInnovation100Level.index_code == A_STOCK_INNO100_INDEX_CODE,
            AStockInnovation100Level.date >= start_date,
            AStockInnovation100Level.date <= end_date,
            AStockInnovation100Level.level.isnot(None),
        )
        .order_by(AStockInnovation100Level.date.asc())
        .all()
    )
    result_rows: List[Dict[str, Any]] = []
    for row in rows:
        level = _safe_float(row.level)
        if level is None or level <= 0:
            continue
        result_rows.append(
            {
                "date": row.date,
                "open": level,
                "close": level,
                "volume": 0.0,
                "turnover": 0.0,
            }
        )
    return {A_STOCK_INNO100_SYMBOL: result_rows} if result_rows else {}


def _load_a_stock_index_benchmark_rows(index_codes: List[str], start_date: date, end_date: date) -> Dict[str, List[Dict[str, Any]]]:
    safe_codes = [
        str(item or "").strip().upper()
        for item in list(dict.fromkeys(index_codes or []))
        if item and _is_a_stock_index_pool_symbol(str(item).strip().upper())
    ]
    if not safe_codes:
        return {}
    code_sql = ", ".join(_quote_sql_string(code) for code in safe_codes)
    query = f"""
        SELECT
            ts_code AS symbol,
            CAST(trade_date AS DATE) AS trade_date,
            CAST(open AS DOUBLE) AS open,
            CAST(close AS DOUBLE) AS close,
            CAST(vol AS DOUBLE) AS volume,
            CAST(amount AS DOUBLE) AS turnover
        FROM a_stock_index_daily
        WHERE ts_code IN ({code_sql})
          AND trade_date BETWEEN ? AND ?
          AND close IS NOT NULL
          AND close > 0
        ORDER BY ts_code, trade_date
    """
    connection = _connect_duckdb()
    try:
        frame = pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]})
    finally:
        connection.close()
    return _price_frame_to_rows_by_symbol(frame) if not frame.is_empty() else {}


def load_benchmark_rows_by_symbol(
    db: ORMSession,
    benchmark_symbols: List[str],
    start_date: date,
    end_date: date,
) -> Dict[str, List[Dict[str, Any]]]:
    symbols = [str(item or "").strip().upper() for item in list(dict.fromkeys(benchmark_symbols or [])) if item]
    rows_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    if A_STOCK_INNO100_SYMBOL in symbols:
        rows_by_symbol.update(_load_inno100_benchmark_rows(db, start_date, end_date))

    index_symbols = [symbol for symbol in symbols if _is_a_stock_index_pool_symbol(symbol)]
    if index_symbols:
        rows_by_symbol.update(_load_a_stock_index_benchmark_rows(index_symbols, start_date, end_date))

    regular_symbols = [
        symbol
        for symbol in symbols
        if symbol != A_STOCK_INNO100_SYMBOL and not _is_a_stock_index_pool_symbol(symbol)
    ]
    if regular_symbols:
        rows_by_symbol.update(_price_frame_to_rows_by_symbol(load_price_frame(regular_symbols, start_date, end_date)))
    return rows_by_symbol


def append_execution_price_rows(
    prepared_data: Dict[str, Any],
    execution_date: date,
    prices_by_symbol: Dict[str, float],
):
    if not prepared_data or not execution_date or not prices_by_symbol:
        return
    row_by_symbol_date = prepared_data.setdefault("row_by_symbol_date", {})
    dates = prepared_data.setdefault("dates", [])
    inserted = False
    for symbol, raw_price in prices_by_symbol.items():
        price = _safe_float(raw_price)
        if not symbol or price is None or price <= 0:
            continue
        rows_for_symbol = row_by_symbol_date.setdefault(symbol, {})
        rows_for_symbol.setdefault(
            execution_date,
            {
                "date": execution_date,
                "open": price,
                "close": price,
                "volume": 0.0,
                "turnover": 0.0,
            },
        )
        inserted = True
    if inserted and execution_date not in dates:
        dates.append(execution_date)
        dates.sort()
    if inserted and prepared_data.get("end_date") and execution_date > prepared_data["end_date"]:
        prepared_data["end_date"] = execution_date


def _factor_values_cache_key(request: FactorBacktestConfig, resolved_legs: List[Dict[str, Any]]) -> Any:
    return (
        int(request.min_listing_days),
        tuple(
            (_backtest_leg_factor_cache_key(leg), round(float(leg.get("weight") or 0), 10))
            for leg in resolved_legs
            if abs(float(leg.get("weight") or 0)) > 1e-12
        ),
    )


def _factor_values_by_date(factor_df: pl.DataFrame, request: FactorBacktestConfig, end_date: date) -> Dict[date, Dict[str, float]]:
    if factor_df.is_empty():
        return {}
    source = (
        _filter_min_listing_days(factor_df, request.min_listing_days)
        .filter(
            (pl.col("trade_date") >= request.start_date)
            & (pl.col("trade_date") <= end_date)
            & pl.col("factor_value").is_not_null()
            & pl.col("factor_value").is_finite()
        )
        .select("trade_date", "symbol", "factor_value")
    )
    values_by_date: Dict[date, Dict[str, float]] = {}
    for row in source.to_dicts():
        score = _safe_float(row.get("factor_value"))
        if score is None:
            continue
        values_by_date.setdefault(row["trade_date"], {})[row["symbol"]] = score
    return values_by_date


def _factor_values_and_details_by_date(
    factor_df: pl.DataFrame,
    request: FactorBacktestConfig,
    end_date: date,
    resolved_legs: List[Dict[str, Any]],
) -> Dict[str, Dict[date, Dict[str, Any]]]:
    if factor_df.is_empty():
        return {"values": {}, "details": {}}
    detail_columns = [
        column
        for column in [
            "trade_date",
            "symbol",
            "factor_value",
            "factor_value_raw",
            *[leg["column"] for leg in resolved_legs],
        ]
        if column in factor_df.columns
    ]
    source = (
        _filter_min_listing_days(factor_df, request.min_listing_days)
        .filter(
            (pl.col("trade_date") >= request.start_date)
            & (pl.col("trade_date") <= end_date)
            & pl.col("factor_value").is_not_null()
            & pl.col("factor_value").is_finite()
        )
        .select(detail_columns)
    )
    values_by_date: Dict[date, Dict[str, float]] = {}
    details_by_date: Dict[date, Dict[str, Dict[str, Any]]] = {}
    for row in source.to_dicts():
        score = _safe_float(row.get("factor_value"))
        if score is None:
            continue
        component_scores: Dict[str, Any] = {}
        component_score_by_factor: Dict[str, Any] = {}
        for leg in resolved_legs:
            value = _safe_float(row.get(leg["column"]), 6)
            component_payload = {
                "component_key": leg["key"],
                "factor": leg["factor"]["key"],
                "factor_label": leg["factor"]["label"],
                "window": leg["window"],
                "window_label": leg["window_label"],
                "score": value,
                "weight": _safe_float(leg.get("weight"), 4),
            }
            component_scores[leg["key"]] = component_payload
            component_score_by_factor[leg["factor"]["key"]] = component_payload
        detail = {
            "factor_score": score,
            "factor_value_raw": _safe_float(row.get("factor_value_raw"), 6),
            "component_scores": component_scores,
            "component_score_by_factor": component_score_by_factor,
        }
        values_by_date.setdefault(row["trade_date"], {})[row["symbol"]] = score
        details_by_date.setdefault(row["trade_date"], {})[row["symbol"]] = detail
    return {"values": values_by_date, "details": details_by_date}


def _is_virtual_replication_shape(resolved_legs: List[Dict[str, Any]]) -> bool:
    if len(resolved_legs) != 2:
        return False
    by_key = {leg["factor"]["key"]: leg for leg in resolved_legs}
    momentum_leg = by_key.get("risk_adjusted_momentum")
    index_leg = by_key.get("index_weight")
    if not momentum_leg or not index_leg:
        return False
    return (
        momentum_leg["window"] == MIXED_WINDOW_KEY
        and momentum_leg["neutralization"] == "none"
        and momentum_leg["standardization"] == "rank_percentile"
        and index_leg["neutralization"] == "none"
        and index_leg["standardization"] == "rank_percentile"
        and abs(float(momentum_leg.get("weight") or 0) - 0.6) < 1e-6
        and abs(float(index_leg.get("weight") or 0) - 0.4) < 1e-6
    )


def prepare_factor_backtest_base_data(
    request: FactorBacktestConfig,
    db: ORMSession,
    resolved_legs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    resolved_legs = resolved_legs or resolve_factor_legs(request.legs)
    validate_factor_legs_for_pool(request.pool, resolved_legs)
    end_date = _resolve_backtest_end_date(request, db)
    pool_key = str(request.pool or "").strip().upper()
    factor_keys = {leg["factor"]["key"] for leg in resolved_legs}
    is_custom = is_custom_pool(pool_key)
    custom_symbols = normalize_custom_pool_symbols(pool_key, request.custom_symbols) if is_custom else []
    candidate_etfs = list(
        dict.fromkeys(
            str(item or "").strip().upper()
            for item in (request.candidate_etfs or POOL_ETFS.get(pool_key, DEFAULT_CANDIDATE_ETFS))
            if item
        )
    )
    required_windows = required_windows_for_legs(resolved_legs)
    max_factor_window = max(required_windows)
    fetch_padding_days = max(370, int(request.min_listing_days) + 30, int(max_factor_window * 3))
    fetch_start = request.start_date - timedelta(days=fetch_padding_days)

    if is_custom:
        universe_history = build_static_universe_history(pool_key, custom_symbols, request.start_date)
        candidate_etfs = []
    else:
        universe_history = load_universe_history(db, candidate_etfs, request.start_date, end_date)
    if not universe_history.all_symbols:
        raise ValueError("股票池没有可用成分股数据")

    price_df = load_price_frame(universe_history.all_symbols, fetch_start, end_date)
    if price_df.is_empty():
        raise ValueError("股票池没有可用日行情数据")

    rows_by_symbol = _price_frame_to_rows_by_symbol(price_df)
    row_by_symbol_date = {symbol: {row["date"]: row for row in rows} for symbol, rows in rows_by_symbol.items()}
    dates = sorted(
        {
            row["date"]
            for rows in rows_by_symbol.values()
            for row in rows
            if request.start_date <= row["date"] <= end_date
        }
    )
    if not dates:
        raise ValueError("回测区间内没有可交易日行情")

    needs_industry = any(leg["neutralization"] != "none" for leg in resolved_legs)
    industry_df = _load_industry_frame(db, universe_history.all_symbols, request.start_date - timedelta(days=3650), end_date) if needs_industry else None
    valuation_df = load_valuation_frame(db, universe_history.all_symbols, request.start_date - timedelta(days=540), end_date) if "valuation_gap" in factor_keys else None
    if is_custom:
        weight_history = build_static_equal_weight_history(pool_key, universe_history.all_symbols, request.start_date) if "index_weight" in factor_keys else None
        benchmark_rows = {}
    else:
        weight_history = load_universe_weight_history(db, candidate_etfs, request.start_date, end_date) if "index_weight" in factor_keys else None
        benchmark_rows = load_benchmark_rows_by_symbol(
            db,
            candidate_etfs,
            request.start_date - timedelta(days=10),
            end_date,
        )

    return {
        "end_date": end_date,
        "candidate_etfs": candidate_etfs,
        "required_windows": required_windows,
        "universe_history": universe_history,
        "price_df": price_df,
        "symbol_count": len(rows_by_symbol),
        "row_by_symbol_date": row_by_symbol_date,
        "dates": dates,
        "industry_df": industry_df,
        "valuation_df": valuation_df,
        "weight_history": weight_history,
        "benchmark_rows": benchmark_rows,
        "raw_factor_cache": {},
        "component_factor_cache": {},
        "factor_values_cache": {},
    }


def _factor_values_payload_for_backtest_request(
    request: FactorBacktestConfig,
    prepared_data: Dict[str, Any],
    db: ORMSession,
    resolved_legs: List[Dict[str, Any]],
) -> Dict[str, Dict[date, Dict[str, Any]]]:
    factor_values_cache = prepared_data.setdefault("factor_values_cache", {})
    factor_values_key = _factor_values_cache_key(request, resolved_legs)
    cached_factor_payload = factor_values_cache.get(factor_values_key)
    if isinstance(cached_factor_payload, dict) and "values" in cached_factor_payload:
        return cached_factor_payload

    factor_df = _prepare_composite_factor_frame(
        price_df=prepared_data["price_df"],
        request=request,
        db=db,
        symbols=prepared_data["universe_history"].all_symbols,
        start_date=request.start_date,
        end_date=prepared_data["end_date"],
        analysis_dates=prepared_data["dates"],
        industry_df=prepared_data.get("industry_df"),
        valuation_df=prepared_data.get("valuation_df"),
        weight_history=prepared_data.get("weight_history"),
        raw_factor_cache=prepared_data.setdefault("raw_factor_cache", {}),
        component_factor_cache=prepared_data.setdefault("component_factor_cache", {}),
        resolved_legs=resolved_legs,
        candidate_etfs=prepared_data["candidate_etfs"],
    )
    factor_payload = _factor_values_and_details_by_date(factor_df, request, prepared_data["end_date"], resolved_legs)
    _put_limited_cache(
        factor_values_cache,
        factor_values_key,
        factor_payload,
        BACKTEST_SEARCH_FACTOR_VALUES_CACHE_LIMIT,
    )
    return factor_payload


def build_factor_signal_plan(
    request: FactorBacktestConfig,
    db: ORMSession,
    *,
    holding_symbols: Optional[List[str]] = None,
    signal_date: Optional[date] = None,
    prepared_data: Optional[Dict[str, Any]] = None,
    rank_limit: int = 100,
    next_trading_day_resolver: Optional[Callable[[date], Optional[date]]] = None,
) -> Dict[str, Any]:
    """Build the close-signal / next-open execution plan without running portfolio history."""
    resolved_legs = resolve_factor_legs(request.legs)
    validate_factor_legs_for_pool(request.pool, resolved_legs)
    if signal_date:
        request = replace(request, end_date=signal_date)
    if prepared_data is None:
        prepared_data = prepare_factor_backtest_base_data(request, db, resolved_legs)

    dates: List[date] = prepared_data["dates"]
    if not dates:
        raise ValueError("没有可用交易日行情")
    target_date = signal_date or prepared_data["end_date"]
    factor_payload = _factor_values_payload_for_backtest_request(request, prepared_data, db, resolved_legs)
    factor_values = factor_payload.get("values") or {}
    factor_details = factor_payload.get("details") or {}
    candidate_dates = [item for item in dates if item <= target_date and factor_values.get(item)]
    if not candidate_dates:
        raise ValueError("没有可用因子信号，请调整日期、窗口或股票池")
    actual_signal_date = candidate_dates[-1]
    if signal_date and actual_signal_date != signal_date:
        raise ValueError(
            f"{signal_date.isoformat()} 没有可用因子信号，"
            f"最新可用信号日为 {actual_signal_date.isoformat()}，请确认行情和复权数据已同步"
        )
    date_index = dates.index(actual_signal_date)

    universe_history = prepared_data["universe_history"]
    row_by_symbol_date = prepared_data["row_by_symbol_date"]
    current_universe = set(universe_history.symbols_for_date(actual_signal_date))
    price_map: Dict[str, float] = {}
    turnover_map: Dict[str, float] = {}
    for symbol, row_by_date in row_by_symbol_date.items():
        row = row_by_date.get(actual_signal_date)
        if not row:
            continue
        close_price = _safe_float(row.get("close"))
        if close_price is not None and close_price > 0:
            price_map[symbol] = close_price
        turnover_map[symbol] = _safe_float(row.get("turnover")) or 0.0

    score_map = factor_values.get(actual_signal_date, {})
    ranked: List[Dict[str, Any]] = []
    for symbol in sorted(current_universe):
        if symbol not in price_map:
            continue
        factor_score = _safe_float(score_map.get(symbol))
        if factor_score is None:
            continue
        detail = factor_details.get(actual_signal_date, {}).get(symbol, {})
        ranked.append(
            {
                "symbol": symbol,
                "price": _safe_float(price_map[symbol], 4),
                "turnover": _safe_float(turnover_map.get(symbol, 0.0), 2),
                "factor_score": _safe_float(factor_score, 6),
                "factor_value_raw": detail.get("factor_value_raw"),
                "component_scores": detail.get("component_scores"),
                "component_score_by_factor": detail.get("component_score_by_factor"),
            }
        )
    ranked.sort(key=lambda item: (float(item.get("factor_score") or -1e18), float(item.get("turnover") or 0), item["symbol"]), reverse=True)
    denominator = max(1, len(ranked) - 1)
    for rank_index, item in enumerate(ranked):
        item["rank"] = rank_index + 1
        item["factor_percentile"] = _safe_float(1 - rank_index / denominator, 6)
        item["rank_score"] = item["factor_score"]

    position_weights = normalize_position_weights(request.position_weights, request.max_positions)
    max_positions = len(position_weights)
    sell_rank_multiplier = float(request.sell_rank_multiplier)
    sell_rank_threshold = max(max_positions, int(math.ceil(max_positions * sell_rank_multiplier)))
    rebalance_frequency = normalize_rebalance_frequency(request.rebalance_frequency)
    rotation_mode = normalize_rotation_mode(request.rotation_mode)
    rotation_mode_label = ROTATION_MODE_LABELS.get(rotation_mode, rotation_mode)
    held_symbols = list(dict.fromkeys(str(item or "").strip().upper() for item in (holding_symbols or []) if item))
    selected = ranked[:max_positions]
    selected_symbols = [item["symbol"] for item in selected]
    sell_rank_symbols = [item["symbol"] for item in ranked[:sell_rank_threshold]]
    rank_by_symbol = {item["symbol"]: int(item["rank"]) for item in ranked}
    if date_index < len(dates) - 1:
        is_signal_day = is_rebalance_day(dates, date_index, rebalance_frequency)
        next_trading_day = dates[date_index + 1]
    else:
        next_trading_day = next_trading_day_resolver(actual_signal_date) if next_trading_day_resolver else None
        if next_trading_day and next_trading_day > actual_signal_date:
            is_signal_day = is_rebalance_day([actual_signal_date, next_trading_day], 0, rebalance_frequency)
        else:
            is_signal_day = is_rebalance_day(dates, date_index, rebalance_frequency)

    def _portfolio_target_weights(symbols: List[str]) -> Dict[str, float]:
        if not symbols:
            return {}
        weights = list(position_weights[: len(symbols)])
        if len(weights) < len(symbols):
            weights.extend([weights[-1] if weights else 1.0 / len(symbols)] * (len(symbols) - len(weights)))
        total = sum(weights)
        if total <= 0:
            return {symbol: 1.0 / len(symbols) for symbol in symbols}
        if total > 1.000001:
            weights = [weight / total for weight in weights]
        return {symbol: weight for symbol, weight in zip(symbols, weights) if weight > 0}

    def _cash_fill_buy_weights(symbols: List[str]) -> Dict[str, float]:
        if not symbols:
            return {}
        weights = list(position_weights[: len(symbols)])
        if len(weights) < len(symbols):
            weights.extend([weights[-1] if weights else 1.0] * (len(symbols) - len(weights)))
        total = sum(weights)
        if total <= 0:
            return {symbol: 1.0 / len(symbols) for symbol in symbols}
        return {symbol: weight / total for symbol, weight in zip(symbols, weights) if weight > 0}

    if not is_signal_day:
        planned = {
            "sell_symbols": [],
            "target_symbols": held_symbols,
            "target_weights": {},
            "buy_symbols": [],
            "buy_weights": {},
            "should_rebalance": False,
        }
    elif rotation_mode == ROTATION_MODE_SCHEDULED_REBALANCE:
        target_symbols = list(dict.fromkeys(selected_symbols))
        planned = {
            "sell_symbols": [symbol for symbol in held_symbols if symbol not in target_symbols],
            "target_symbols": target_symbols,
            "target_weights": _portfolio_target_weights(target_symbols),
            "buy_symbols": [symbol for symbol in target_symbols if symbol not in held_symbols],
            "buy_weights": {},
            "should_rebalance": True,
        }
    else:
        sell_symbols = [symbol for symbol in held_symbols if rank_by_symbol.get(symbol, 10**9) > sell_rank_threshold]
        survivors = [symbol for symbol in held_symbols if symbol not in sell_symbols and rank_by_symbol.get(symbol) is not None]
        target_symbols = list(dict.fromkeys(survivors))
        buy_symbols = []
        for item in ranked:
            symbol = item["symbol"]
            if symbol in target_symbols or symbol in sell_symbols:
                continue
            target_symbols.append(symbol)
            if symbol not in held_symbols:
                buy_symbols.append(symbol)
            if len(target_symbols) >= max_positions:
                break
        target_symbols = [symbol for symbol in target_symbols[:max_positions] if symbol in rank_by_symbol]
        target_symbols.sort(key=lambda symbol: rank_by_symbol.get(symbol, 10**9))
        buy_symbols = [symbol for symbol in buy_symbols if symbol in target_symbols]
        buy_symbols.sort(key=lambda symbol: rank_by_symbol.get(symbol, 10**9))
        planned = {
            "sell_symbols": sell_symbols,
            "target_symbols": target_symbols,
            "target_weights": _portfolio_target_weights(target_symbols),
            "buy_symbols": buy_symbols,
            "buy_weights": _cash_fill_buy_weights(buy_symbols),
            "should_rebalance": bool(
                sell_symbols
                or len(held_symbols) < max_positions
                or any(symbol not in held_symbols for symbol in target_symbols)
            ),
        }

    if not is_signal_day:
        action_status = "SKIPPED"
        action_message = f"{actual_signal_date.isoformat()} 不是{REBALANCE_FREQUENCY_LABELS.get(rebalance_frequency, rebalance_frequency)}信号日"
    elif planned["should_rebalance"]:
        action_status = "READY"
        action_message = "已生成下一交易日开盘执行计划"
    else:
        action_status = "NO_CHANGE"
        action_message = "当前持仓未跌出排名阈值，无需调仓"

    signal_symbols = {
        *held_symbols,
        *selected_symbols,
        *sell_rank_symbols,
        *planned["sell_symbols"],
        *planned["target_symbols"],
        *planned["buy_symbols"],
        *(item.get("symbol") for item in ranked[: max(1, int(rank_limit or 100))]),
    }
    symbol_names = load_symbol_name_map(signal_symbols, db)
    ranked_rows = ranked[: max(1, int(rank_limit or 100))]
    attach_symbol_names(ranked_rows, symbol_names)

    return {
        "signal_date": actual_signal_date.isoformat(),
        "requested_signal_date": target_date.isoformat() if target_date else None,
        "is_signal_day": is_signal_day,
        "status": action_status,
        "message": action_message,
        "pool": str(request.pool or "").strip().upper(),
        "pool_label": request.pool_label,
        "custom_symbols": list(request.custom_symbols or []),
        "current_holdings": held_symbols,
        "selected_symbols": selected_symbols,
        "sell_rank_symbols": sell_rank_symbols,
        "sell_symbols": planned["sell_symbols"],
        "target_symbols": planned["target_symbols"],
        "target_weights": planned["target_weights"],
        "buy_symbols": planned["buy_symbols"],
        "buy_weights": planned["buy_weights"],
        "should_rebalance": planned["should_rebalance"],
        "max_positions": max_positions,
        "position_weights": position_weights,
        "position_weights_label": format_position_weights(position_weights),
        "sell_rank_multiplier": sell_rank_multiplier,
        "sell_rank_threshold": sell_rank_threshold,
        "rebalance_frequency": rebalance_frequency,
        "rotation_mode": rotation_mode,
        "rotation_mode_label": rotation_mode_label,
        "execution_rule": "signal_close_next_open",
        "next_trading_date": next_trading_day.isoformat() if next_trading_day else None,
        "symbol_names": symbol_names,
        "ranked": ranked_rows,
        "ranked_count": len(ranked),
        "universe_size": len(current_universe),
        "price_source": DAILY_PRICE_SOURCE,
        "engine": "polars_duckdb_shared_factor_signal_plan",
    }


def warm_backtest_search_factor_caches(
    request: FactorBacktestConfig,
    prepared_data: Dict[str, Any],
    db: ORMSession,
):
    price_df = prepared_data.get("price_df")
    if price_df is None or price_df.is_empty():
        return
    raw_factor_cache = prepared_data.setdefault("raw_factor_cache", {})
    component_factor_cache = prepared_data.setdefault("component_factor_cache", {})
    base_resolved_legs = resolve_factor_legs(request.legs)
    validate_factor_legs_for_pool(request.pool, base_resolved_legs)
    for leg in base_resolved_legs:
        factor_definition = leg["factor_definition"]
        if factor_definition.key in MOMENTUM_FACTOR_SCORE_PREFIX:
            _momentum_score_source_frame(price_df, factor_definition.key, leg["windows"], raw_factor_cache)
        if leg["window"] == MIXED_WINDOW_KEY:
            continue
        leg_context = FactorContext(
            windows=leg["windows"],
            momentum_weights=_weights_for_leg(leg),
            db=db,
            symbols=prepared_data["universe_history"].all_symbols,
            start_date=request.start_date,
            end_date=prepared_data["end_date"],
            analysis_dates=prepared_data["dates"],
            industry_df=prepared_data.get("industry_df"),
            candidate_etfs=prepared_data["candidate_etfs"],
            valuation_df=prepared_data.get("valuation_df"),
            weight_history=prepared_data.get("weight_history"),
            weight_history_loader=load_universe_weight_history,
        )
        leg_request = SimpleNamespace(neutralization=leg["neutralization"], standardization=leg["standardization"])
        factor_df = _prepare_cached_leg_factor_frame(price_df, factor_definition, leg_context, leg_request, raw_factor_cache).select(
            "symbol", "trade_date", "factor_value"
        )
        _put_limited_cache(component_factor_cache, _backtest_leg_factor_cache_key(leg), factor_df, BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT)
    factor_columns = [column for column in ["symbol", "trade_date", "open", "high", "low", "close", "volume", "turnover", "_first_trade_date"] if column in price_df.columns]
    prepared_data["price_df"] = price_df.select(factor_columns).rechunk()


def _build_factor_backtest_metadata(
    request: FactorBacktestConfig,
    candidate_etfs: List[str],
    universe_history: UniverseHistory,
    price_df: pl.DataFrame,
    resolved_legs: List[Dict[str, Any]],
    end_date: date,
    elapsed_ms: float,
) -> Dict[str, Any]:
    required_windows = required_windows_for_legs(resolved_legs)
    pool_key = str(request.pool or "").strip().upper()
    position_weights = normalize_position_weights(request.position_weights, request.max_positions)
    rotation_mode = normalize_rotation_mode(request.rotation_mode)
    pool_label = request.pool_label or CUSTOM_POOL_LABELS.get(
        pool_key,
        next((item["label"] for item in POOL_OPTIONS if item["key"] == pool_key), request.pool),
    )
    custom_symbols = list(dict.fromkeys(str(item or "").strip().upper() for item in request.custom_symbols if item))
    return {
        "mode": request.mode,
        "pool": pool_key,
        "pool_label": pool_label,
        "candidate_etfs": candidate_etfs,
        "custom_symbols": custom_symbols,
        "custom_symbol_count": len(custom_symbols),
        "components": [
            {
                "component_key": leg["key"],
                "factor": leg["factor"],
                "factor_key": leg["factor"]["key"],
                "factor_label": leg["factor"]["label"],
                "window": leg["window"],
                "window_label": leg["window_label"],
                "windows": leg["windows"],
                "raw_weight": _safe_float(leg["raw_weight"], 4),
                "weight": _safe_float(leg["weight"], 4),
                "neutralization": leg["neutralization"],
                "neutralization_label": leg["neutralization_label"],
                "standardization": leg["standardization"],
                "standardization_label": leg["standardization_label"],
                "momentum_weights": leg["momentum_weights"],
            }
            for leg in resolved_legs
        ],
        "start_date": request.start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "initial_capital": request.initial_capital,
        "max_positions": len(position_weights),
        "position_weights": position_weights,
        "position_weights_label": format_position_weights(position_weights),
        "sell_rank_multiplier": request.sell_rank_multiplier,
        "sell_rank_threshold": max(len(position_weights), int(math.ceil(len(position_weights) * request.sell_rank_multiplier))),
        "factor_combination_method": "weighted_standardized_factor_values",
        "factor_combination_method_label": "子因子标准化后加权",
        "rebalance_frequency": request.rebalance_frequency,
        "rotation_mode": rotation_mode,
        "rotation_mode_label": ROTATION_MODE_LABELS.get(rotation_mode, rotation_mode),
        "commission_pct": request.commission_pct,
        "slippage_pct": request.slippage_pct,
        "lot_size": request.lot_size,
        "min_listing_days": request.min_listing_days,
        "windows": required_windows,
        "universe_symbols": len(universe_history.all_symbols),
        "holdings_date_count": universe_history.holdings_date_count,
        "price_rows": int(price_df.height),
        **_price_adjustment_metadata(candidate_etfs, universe_history.all_symbols),
        "engine": "polars_duckdb_shared_factor_backtest_engine",
        "elapsed_ms": round(elapsed_ms, 1),
        "replicates_virtual_strategy": _is_virtual_replication_shape(resolved_legs),
    }


def _compute_equity_risk_metrics(equity_curve: List[Dict[str, Any]], annualized_return_pct: float) -> Dict[str, Optional[float]]:
    values = [
        float(item.get("value"))
        for item in equity_curve or []
        if item.get("value") is not None and math.isfinite(float(item.get("value")))
    ]
    if len(values) < 2:
        return {"annualized_volatility": None, "sharpe": None, "calmar": None}
    returns = []
    for index in range(1, len(values)):
        previous = values[index - 1]
        current = values[index]
        if previous > 0:
            returns.append(current / previous - 1)
    if not returns:
        return {"annualized_volatility": None, "sharpe": None, "calmar": None}
    mean_return = sum(returns) / len(returns)
    std_return = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    annualized_volatility = std_return * math.sqrt(TRADING_DAYS_PER_YEAR) * 100 if std_return > 0 else None
    sharpe = mean_return / std_return * math.sqrt(TRADING_DAYS_PER_YEAR) if std_return > 0 else None
    max_drawdown = min([float(item.get("drawdown") or 0) for item in equity_curve] or [0])
    calmar = annualized_return_pct / abs(max_drawdown) if max_drawdown < 0 else None
    return {
        "annualized_volatility": _safe_float(annualized_volatility, 2),
        "sharpe": _safe_float(sharpe, 4),
        "calmar": _safe_float(calmar, 4),
    }


def run_factor_backtest(
    request: FactorBacktestConfig,
    db: ORMSession,
    prepared_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    resolved_legs = resolve_factor_legs(request.legs)
    validate_factor_legs_for_pool(request.pool, resolved_legs)
    if prepared_data is None:
        prepared_data = prepare_factor_backtest_base_data(request, db, resolved_legs)

    end_date = prepared_data["end_date"]
    candidate_etfs = prepared_data["candidate_etfs"]
    universe_history = prepared_data["universe_history"]
    price_df = prepared_data["price_df"]
    row_by_symbol_date = prepared_data["row_by_symbol_date"]
    dates = prepared_data["dates"]

    factor_values_cache = prepared_data.setdefault("factor_values_cache", {})
    factor_values_key = _factor_values_cache_key(request, resolved_legs)
    cached_factor_payload = factor_values_cache.get(factor_values_key)
    factor_details: Dict[date, Dict[str, Any]] = {}
    if isinstance(cached_factor_payload, dict) and "values" in cached_factor_payload:
        factor_values = cached_factor_payload.get("values") or {}
        factor_details = cached_factor_payload.get("details") or {}
    else:
        factor_values = cached_factor_payload
    if factor_values is None:
        factor_df = _prepare_composite_factor_frame(
            price_df=price_df,
            request=request,
            db=db,
            symbols=universe_history.all_symbols,
            start_date=request.start_date,
            end_date=end_date,
            analysis_dates=dates,
            industry_df=prepared_data.get("industry_df"),
            valuation_df=prepared_data.get("valuation_df"),
            weight_history=prepared_data.get("weight_history"),
            raw_factor_cache=prepared_data.setdefault("raw_factor_cache", {}),
            component_factor_cache=prepared_data.setdefault("component_factor_cache", {}),
            resolved_legs=resolved_legs,
            candidate_etfs=candidate_etfs,
        )
        factor_payload = _factor_values_and_details_by_date(factor_df, request, end_date, resolved_legs)
        factor_values = factor_payload["values"]
        factor_details = factor_payload["details"]
        _put_limited_cache(factor_values_cache, factor_values_key, factor_payload, BACKTEST_SEARCH_FACTOR_VALUES_CACHE_LIMIT)
    if not factor_values:
        raise ValueError("没有可用因子信号，请调整日期、窗口或股票池")

    benchmark_curve = build_benchmark_curve(prepared_data["benchmark_rows"], dates, float(request.initial_capital), request.start_date)

    position_weights = normalize_position_weights(request.position_weights, request.max_positions)
    max_positions = len(position_weights)
    sell_rank_multiplier = float(request.sell_rank_multiplier)
    sell_rank_threshold = max(max_positions, int(math.ceil(max_positions * sell_rank_multiplier)))
    rebalance_frequency = normalize_rebalance_frequency(request.rebalance_frequency)
    rotation_mode = normalize_rotation_mode(request.rotation_mode)
    rotation_mode_label = ROTATION_MODE_LABELS.get(rotation_mode, rotation_mode)
    lot_size = max(1, int(request.lot_size or 1))
    commission_rate = max(0.0, float(request.commission_pct or 0)) / 100
    slippage_rate = max(0.0, float(request.slippage_pct or 0)) / 100
    execution_price_overrides = request.execution_price_overrides or {}
    execution_price_source_overrides = request.execution_price_source_overrides or {}
    execution_quote_timestamp_overrides = request.execution_quote_timestamp_overrides or {}
    execution_depth_overrides = request.execution_depth_overrides or {}

    cash = float(request.initial_capital)
    positions: Dict[str, Dict[str, Any]] = {}
    last_prices: Dict[str, float] = {}
    equity_curve: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    closed_profits: List[float] = []
    peak_value = cash
    universe_size_by_date: Dict[str, int] = {}
    rebalance_count = 0
    pending_rebalance: Optional[Dict[str, Any]] = None

    def _current_portfolio_value_with_prices(price_overrides: Dict[str, float]) -> float:
        merged_prices = dict(last_prices)
        merged_prices.update(price_overrides)
        return portfolio_value(cash, positions, merged_prices)

    def _portfolio_target_weights(symbols: List[str]) -> Dict[str, float]:
        if not symbols:
            return {}
        weights = list(position_weights[: len(symbols)])
        if len(weights) < len(symbols):
            if weights:
                last_weight = weights[-1]
            else:
                last_weight = 1.0 / len(symbols)
            weights.extend([last_weight] * (len(symbols) - len(weights)))
        total = sum(weights)
        if total <= 0:
            return {symbol: 1.0 / len(symbols) for symbol in symbols}
        if total > 1.000001:
            weights = [weight / total for weight in weights]
        return {symbol: weight for symbol, weight in zip(symbols, weights) if weight > 0}

    def _cash_fill_buy_weights(symbols: List[str]) -> Dict[str, float]:
        if not symbols:
            return {}
        weights = list(position_weights[: len(symbols)])
        if len(weights) < len(symbols):
            weights.extend([weights[-1] if weights else 1.0] * (len(symbols) - len(weights)))
        total = sum(weights)
        if total <= 0:
            return {symbol: 1.0 / len(symbols) for symbol in symbols}
        return {symbol: weight / total for symbol, weight in zip(symbols, weights) if weight > 0}

    def _plan_target_symbols(ranked: List[Dict[str, Any]], selected_symbols: List[str], rank_by_symbol: Dict[str, int]) -> Dict[str, Any]:
        held_symbols = list(positions.keys())
        if rotation_mode == ROTATION_MODE_SCHEDULED_REBALANCE:
            target_symbols = list(dict.fromkeys(selected_symbols))
            sell_symbols = [symbol for symbol in held_symbols if symbol not in target_symbols]
            should_rebalance = True
            buy_symbols = [symbol for symbol in target_symbols if symbol not in positions]
        else:
            sell_symbols = [symbol for symbol in held_symbols if rank_by_symbol.get(symbol, 10**9) > sell_rank_threshold]
            survivors = [symbol for symbol in held_symbols if symbol not in sell_symbols and rank_by_symbol.get(symbol) is not None]
            target_symbols = list(dict.fromkeys(survivors))
            buy_symbols = []
            for item in ranked:
                symbol = item["symbol"]
                if symbol in target_symbols or symbol in sell_symbols:
                    continue
                target_symbols.append(symbol)
                if symbol not in positions:
                    buy_symbols.append(symbol)
                if len(target_symbols) >= max_positions:
                    break
            target_symbols = target_symbols[:max_positions]
            buy_symbols = [symbol for symbol in buy_symbols if symbol in target_symbols]
            should_rebalance = bool(sell_symbols or len(positions) < max_positions or any(symbol not in positions for symbol in target_symbols))
        target_symbols = [symbol for symbol in target_symbols if symbol in rank_by_symbol]
        target_symbols.sort(key=lambda symbol: rank_by_symbol.get(symbol, 10**9))
        buy_symbols = [symbol for symbol in buy_symbols if symbol in target_symbols]
        buy_symbols.sort(key=lambda symbol: rank_by_symbol.get(symbol, 10**9))
        return {
            "sell_symbols": sell_symbols,
            "target_symbols": target_symbols,
            "target_weights": _portfolio_target_weights(target_symbols),
            "buy_symbols": buy_symbols,
            "buy_weights": _cash_fill_buy_weights(buy_symbols),
            "should_rebalance": should_rebalance,
        }

    def append_trade(
        trade_date: date,
        signal_date: date,
        action: str,
        symbol: str,
        price: float,
        quantity: int,
        commission: float,
        reason: str,
        reason_detail: str,
        profit: Optional[float] = None,
        profit_pct: Optional[float] = None,
        price_source: str = NEXT_OPEN_PRICE_SOURCE,
        quote_timestamp: Optional[str] = None,
        execution_price: Optional[float] = None,
        decision_score: Optional[float] = None,
        decision_rank: Optional[int] = None,
        decision_percentile: Optional[float] = None,
    ):
        amount = price * quantity
        portfolio_after = portfolio_value(cash, positions, last_prices)
        symbol_market_value = 0.0
        if symbol in positions:
            symbol_market_value = int(positions[symbol].get("shares") or 0) * float(last_prices.get(symbol) or price)
        trades.append(
            {
                "date": trade_date.isoformat(),
                "signal_date": signal_date.isoformat(),
                "action": action,
                "symbol": symbol,
                "price": _safe_float(price, 4),
                "execution_price": _safe_float(execution_price if execution_price is not None else price, 4),
                "quantity": int(quantity),
                "amount": _safe_float(amount, 2),
                "commission": _safe_float(commission, 2),
                "profit": _safe_float(profit, 2),
                "profit_pct": _safe_float(profit_pct, 2),
                "reason": reason,
                "reason_detail": reason_detail,
                "decision_score": _safe_float(decision_score, 6),
                "decision_rank": int(decision_rank) if decision_rank is not None else None,
                "decision_percentile": _safe_float(decision_percentile, 6),
                "cash_after": _safe_float(cash, 2),
                "portfolio_value_after": _safe_float(portfolio_after, 2),
                "symbol_market_value_after": _safe_float(symbol_market_value, 2),
                "symbol_weight_pct_after": _safe_float(symbol_market_value / portfolio_after * 100 if portfolio_after > 0 else 0, 2),
                "price_source": price_source or NEXT_OPEN_PRICE_SOURCE,
                "quote_timestamp": quote_timestamp,
            }
        )

    def sell_position(
        trade_date: date,
        signal_date: date,
        symbol: str,
        quantity: int,
        price: float,
        reason_detail: str,
        price_source: str = NEXT_OPEN_PRICE_SOURCE,
        quote_timestamp: Optional[str] = None,
        decision_score: Optional[float] = None,
        decision_rank: Optional[int] = None,
        decision_percentile: Optional[float] = None,
    ):
        nonlocal cash
        if symbol not in positions:
            return
        position = positions[symbol]
        old_shares = int(position.get("shares") or 0)
        quantity = min(old_shares, int(quantity or 0))
        if quantity <= 0:
            return
        sell_price = price * (1 - slippage_rate)
        amount = sell_price * quantity
        commission = amount * commission_rate
        old_cost_basis = float(position.get("cost_basis") or 0)
        cost_basis_sold = old_cost_basis * quantity / old_shares if old_shares > 0 else 0.0
        cash += amount - commission
        profit = amount - commission - cost_basis_sold
        profit_pct = profit / cost_basis_sold * 100 if cost_basis_sold > 0 else None
        closed_profits.append(profit)
        remaining_shares = old_shares - quantity
        if remaining_shares <= 0:
            del positions[symbol]
        else:
            position["shares"] = remaining_shares
            position["cost_basis"] = max(0.0, old_cost_basis - cost_basis_sold)
            position["avg_cost"] = position["cost_basis"] / remaining_shares if remaining_shares > 0 else 0.0
            position["last_price"] = price
        last_prices[symbol] = price
        append_trade(
            trade_date,
            signal_date,
            "SELL",
            symbol,
            sell_price,
            quantity,
            commission,
            f"{rebalance_frequency}_rebalance",
            reason_detail,
            profit,
            profit_pct,
            price_source,
            quote_timestamp,
            price,
            decision_score,
            decision_rank,
            decision_percentile,
        )

    def buy_position(
        trade_date: date,
        signal_date: date,
        symbol: str,
        budget: float,
        price: float,
        reason_detail: str,
        price_source: str = NEXT_OPEN_PRICE_SOURCE,
        quote_timestamp: Optional[str] = None,
        decision_score: Optional[float] = None,
        decision_rank: Optional[int] = None,
        decision_percentile: Optional[float] = None,
    ):
        nonlocal cash
        buy_price = price * (1 + slippage_rate)
        quantity = floor_lot(budget / (buy_price * (1 + commission_rate)), lot_size)
        if quantity <= 0:
            return
        amount = buy_price * quantity
        commission = amount * commission_rate
        if amount + commission > cash + 1e-9:
            return
        cash -= amount + commission
        if symbol not in positions:
            positions[symbol] = {
                "shares": quantity,
                "avg_cost": (amount + commission) / quantity,
                "cost_basis": amount + commission,
                "entry_date": trade_date,
                "last_price": price,
            }
        else:
            position = positions[symbol]
            position["shares"] = int(position.get("shares") or 0) + quantity
            position["cost_basis"] = float(position.get("cost_basis") or 0) + amount + commission
            position["avg_cost"] = position["cost_basis"] / position["shares"] if position["shares"] > 0 else 0.0
            position["last_price"] = price
        last_prices[symbol] = price
        append_trade(
            trade_date,
            signal_date,
            "BUY",
            symbol,
            buy_price,
            quantity,
            commission,
            f"{rebalance_frequency}_rebalance",
            reason_detail,
            None,
            None,
            price_source,
            quote_timestamp,
            price,
            decision_score,
            decision_rank,
            decision_percentile,
        )

    def get_execution_price(
        symbol: str,
        current_date: date,
        action: str,
        open_map: Dict[str, float],
        quantity: Optional[int] = None,
        budget: Optional[float] = None,
    ):
        date_key = current_date.isoformat()
        depth_payload = (execution_depth_overrides.get(date_key) or {}).get(symbol)
        depth_price = estimate_depth_execution_price(depth_payload, action, quantity=quantity, budget=budget)
        if depth_price is not None and depth_price > 0:
            return (
                depth_price,
                depth_payload.get("price_source") or "depth_orderbook",
                depth_payload.get("timestamp"),
            )
        override_price = _safe_float((execution_price_overrides.get(date_key) or {}).get(symbol))
        if override_price is not None and override_price > 0:
            source = (execution_price_source_overrides.get(date_key) or {}).get(symbol) or NEXT_OPEN_PRICE_SOURCE
            quote_timestamp = (execution_quote_timestamp_overrides.get(date_key) or {}).get(symbol)
            return override_price, source, quote_timestamp
        fallback_price = _safe_float(open_map.get(symbol))
        if fallback_price is not None and fallback_price > 0:
            return fallback_price, NEXT_OPEN_PRICE_SOURCE, None
        return None, None, None

    for date_index, current_date in enumerate(dates):
        open_map: Dict[str, float] = {}
        price_map: Dict[str, float] = {}
        turnover_map: Dict[str, float] = {}
        for symbol, row_by_date in row_by_symbol_date.items():
            row = row_by_date.get(current_date)
            if not row:
                continue
            open_price = _safe_float(row.get("open"))
            close_price = _safe_float(row.get("close"))
            if open_price is not None and open_price > 0:
                open_map[symbol] = open_price
            if close_price is not None and close_price > 0:
                price_map[symbol] = close_price
            turnover_map[symbol] = _safe_float(row.get("turnover")) or 0.0

        if pending_rebalance:
            signal_date = pending_rebalance["signal_date"]
            target_prices: Dict[str, float] = {}
            target_price_sources: Dict[str, str] = {}
            target_quote_timestamps: Dict[str, Optional[str]] = {}

            def _decision_score(symbol: str) -> Optional[float]:
                return _safe_float((pending_rebalance.get("score_by_symbol") or {}).get(symbol))

            def _decision_rank(symbol: str) -> Optional[int]:
                value = (pending_rebalance.get("rank_by_symbol") or {}).get(symbol)
                return int(value) if value is not None else None

            def _decision_percentile(symbol: str) -> Optional[float]:
                return _safe_float((pending_rebalance.get("percentile_by_symbol") or {}).get(symbol))

            for symbol in pending_rebalance["target_symbols"]:
                price, price_source, quote_timestamp = get_execution_price(
                    symbol,
                    current_date,
                    "BUY",
                    open_map,
                )
                if price is None or price <= 0:
                    continue
                target_prices[symbol] = price
                target_price_sources[symbol] = price_source or NEXT_OPEN_PRICE_SOURCE
                target_quote_timestamps[symbol] = quote_timestamp

            for symbol in list(pending_rebalance["sell_symbols"]):
                if symbol not in positions:
                    continue
                shares = int(positions[symbol].get("shares") or 0)
                price = target_prices.get(symbol)
                if price is None or price <= 0:
                    price, price_source, quote_timestamp = get_execution_price(
                        symbol,
                        current_date,
                        "SELL",
                        open_map,
                        quantity=shares,
                    )
                else:
                    price_source = target_price_sources.get(symbol, NEXT_OPEN_PRICE_SOURCE)
                    quote_timestamp = target_quote_timestamps.get(symbol)
                if price is None or price <= 0:
                    continue
                sell_position(
                    current_date,
                    signal_date,
                    symbol,
                    shares,
                    price,
                    (
                        f"下一交易日开盘执行: 不在目标Top{max_positions}持仓: {', '.join(pending_rebalance['sell_symbols'])}"
                        if rotation_mode == ROTATION_MODE_SCHEDULED_REBALANCE
                        else f"下一交易日开盘执行: 跌出因子排名Top{sell_rank_threshold}: {', '.join(pending_rebalance['sell_symbols'])}"
                    ),
                    price_source,
                    quote_timestamp,
                    _decision_score(symbol),
                    _decision_rank(symbol),
                    _decision_percentile(symbol),
                )

            if pending_rebalance["should_rebalance"] and pending_rebalance["target_symbols"]:
                if pending_rebalance.get("rotation_mode") == ROTATION_MODE_CASH_FILL_REBALANCE:
                    max_new_positions = max(0, max_positions - len(positions))
                    buy_symbols = [
                        symbol
                        for symbol in pending_rebalance.get("buy_symbols") or []
                        if symbol not in positions and (target_prices.get(symbol) or 0) > 0
                    ][:max_new_positions]
                    buy_weights = _cash_fill_buy_weights(buy_symbols)
                    available_cash = cash
                    for symbol in buy_symbols:
                        buy_weight = float(buy_weights.get(symbol) or 0)
                        if buy_weight <= 0:
                            continue
                        buy_budget = min(cash, max(0.0, available_cash * buy_weight))
                        if buy_budget <= 0:
                            continue
                        buy_position(
                            current_date,
                            signal_date,
                            symbol,
                            buy_budget,
                            target_prices[symbol],
                            f"下一交易日开盘现金补位买入: 可用现金占比{format_position_weights([buy_weight])}",
                            target_price_sources.get(symbol, NEXT_OPEN_PRICE_SOURCE),
                            target_quote_timestamps.get(symbol),
                            _decision_score(symbol),
                            _decision_rank(symbol),
                            _decision_percentile(symbol),
                        )
                else:
                    target_weights = pending_rebalance["target_weights"]
                    portfolio_after_sells = _current_portfolio_value_with_prices(target_prices)
                    for symbol in pending_rebalance["target_symbols"]:
                        price = target_prices.get(symbol)
                        if price is None or price <= 0:
                            continue
                        target_weight = float(target_weights.get(symbol) or 0)
                        if target_weight <= 0:
                            continue
                        current_shares = int(positions.get(symbol, {}).get("shares") or 0)
                        current_value = current_shares * price
                        target_value = portfolio_after_sells * target_weight
                        if current_value <= target_value + 1e-9:
                            continue
                        excess_budget = current_value - target_value
                        sell_quantity = floor_lot(excess_budget / (price * (1 + slippage_rate)), lot_size)
                        if sell_quantity <= 0:
                            continue
                        sell_position(
                            current_date,
                            signal_date,
                            symbol,
                            sell_quantity,
                            price,
                            f"下一交易日开盘按目标仓位调仓: 目标{format_position_weights([target_weight])}",
                            target_price_sources.get(symbol, NEXT_OPEN_PRICE_SOURCE),
                            target_quote_timestamps.get(symbol),
                            _decision_score(symbol),
                            _decision_rank(symbol),
                            _decision_percentile(symbol),
                        )

                    portfolio_after_sells = _current_portfolio_value_with_prices(target_prices)
                    for symbol in pending_rebalance["target_symbols"]:
                        price = target_prices.get(symbol)
                        if price is None or price <= 0:
                            continue
                        target_weight = float(target_weights.get(symbol) or 0)
                        if target_weight <= 0:
                            continue
                        current_shares = int(positions.get(symbol, {}).get("shares") or 0)
                        current_value = current_shares * price
                        target_value = portfolio_after_sells * target_weight
                        if current_value >= target_value - 1e-9:
                            continue
                        buy_budget = min(cash, max(0.0, target_value - current_value))
                        if buy_budget <= 0:
                            continue
                        buy_position(
                            current_date,
                            signal_date,
                            symbol,
                            buy_budget,
                            price,
                            f"下一交易日开盘按目标仓位调仓: 目标{format_position_weights([target_weight])}",
                            target_price_sources.get(symbol, NEXT_OPEN_PRICE_SOURCE),
                            target_quote_timestamps.get(symbol),
                            _decision_score(symbol),
                            _decision_rank(symbol),
                            _decision_percentile(symbol),
                        )
            pending_rebalance = None

        for symbol, price in price_map.items():
            last_prices[symbol] = price
            if symbol in positions:
                positions[symbol]["last_price"] = price
        if not price_map:
            continue

        current_universe = set(universe_history.symbols_for_date(current_date))
        universe_size_by_date[current_date.isoformat()] = len(current_universe)

        score_map = factor_values.get(current_date, {})
        if score_map and is_rebalance_day(dates, date_index, rebalance_frequency):
            ranked: List[Dict[str, Any]] = []
            for symbol in sorted(current_universe):
                if symbol not in price_map:
                    continue
                factor_score = _safe_float(score_map.get(symbol))
                if factor_score is None:
                    continue
                ranked.append(
                    {
                        "symbol": symbol,
                        "price": price_map[symbol],
                        "turnover": turnover_map.get(symbol, 0.0),
                        "factor_score": factor_score,
                    }
                )
            ranked.sort(key=lambda item: (float(item.get("factor_score") or -1e18), float(item.get("turnover") or 0), item["symbol"]), reverse=True)
            denominator = max(1, len(ranked) - 1)
            for rank_index, item in enumerate(ranked):
                item["factor_percentile"] = 1 - rank_index / denominator
                item["rank_score"] = item["factor_score"]
            selected = ranked[:max_positions]
            selected_symbols = [item["symbol"] for item in selected]
            sell_rank_symbols = [item["symbol"] for item in ranked[:sell_rank_threshold]]
            rank_by_symbol = {item["symbol"]: rank for rank, item in enumerate(ranked, start=1)}
            ranked_by_symbol = {item["symbol"]: item for item in ranked}
            planned = _plan_target_symbols(ranked, selected_symbols, rank_by_symbol)
            if planned["should_rebalance"]:
                rebalance_count += 1
            event_symbols = list(dict.fromkeys([
                *sell_rank_symbols,
                *list(positions.keys()),
            ]))
            event_items: List[Dict[str, Any]] = []
            for symbol in event_symbols:
                item = ranked_by_symbol.get(symbol)
                position = positions.get(symbol) or {}
                rank = rank_by_symbol.get(symbol)
                price = (
                    _safe_float((item or {}).get("price"))
                    or _safe_float(price_map.get(symbol))
                    or _safe_float(last_prices.get(symbol))
                    or _safe_float(position.get("last_price"))
                    or _safe_float(position.get("avg_cost"))
                )
                shares = int(position.get("shares") or 0)
                market_value = shares * float(price or 0)
                event_items.append({
                    "symbol": symbol,
                    "rank": rank,
                    "item": item,
                    "price": price,
                    "turnover": (item or {}).get("turnover", turnover_map.get(symbol, 0.0)),
                    "position": position,
                    "held_shares": shares,
                    "held_market_value": market_value,
                    "is_holding": symbol in positions,
                    "is_selected": symbol in selected_symbols,
                    "in_sell_rank": symbol in sell_rank_symbols,
                })
            event_items.sort(key=lambda item: (item["rank"] is None, item["rank"] or 10**9, item["symbol"]))
            for event_item in event_items:
                item = event_item["item"] or {}
                rank = event_item["rank"]
                detail = factor_details.get(current_date, {}).get(event_item["symbol"], {})
                component_by_factor = detail.get("component_score_by_factor") or {}
                momentum_detail = component_by_factor.get("risk_adjusted_momentum") or {}
                index_weight_detail = component_by_factor.get("index_weight") or {}
                events.append(
                    {
                        "symbol": event_item["symbol"],
                        "date": current_date.isoformat(),
                        "direction": "RANK",
                        "signal_price": _safe_float(event_item.get("price"), 4),
                        "turnover": _safe_float(event_item.get("turnover"), 2),
                        "threshold_pct": _safe_float(item.get("factor_score"), 4),
                        "annualized_volatility_pct": None,
                        "payload": {
                            "rank": rank,
                            "rank_score": _safe_float(item.get("rank_score"), 6),
                            "factor_score": _safe_float(item.get("factor_score"), 6),
                            "factor_percentile": _safe_float(item.get("factor_percentile"), 6),
                            "factor_value_raw": detail.get("factor_value_raw"),
                            "component_scores": detail.get("component_scores"),
                            "component_score_by_factor": component_by_factor,
                            "momentum_score": momentum_detail.get("score"),
                            "momentum_percentile": momentum_detail.get("score"),
                            "index_weight_percentile": index_weight_detail.get("score"),
                            "is_holding": event_item["is_holding"],
                            "is_selected": event_item["is_selected"],
                            "in_sell_rank_threshold": event_item["in_sell_rank"],
                            "held_shares": event_item["held_shares"],
                            "held_market_value": _safe_float(event_item["held_market_value"], 2),
                            "selected_symbols": selected_symbols,
                            "sell_rank_symbols": sell_rank_symbols,
                            "target_symbols": planned["target_symbols"],
                            "buy_symbols": planned["buy_symbols"],
                            "buy_weights": planned["buy_weights"],
                            "event_symbols": event_symbols,
                            "max_positions": max_positions,
                            "sell_rank_threshold": sell_rank_threshold,
                            "sell_rank_multiplier": sell_rank_multiplier,
                            "min_listing_days": request.min_listing_days,
                            "rebalance_frequency": rebalance_frequency,
                            "rotation_mode": rotation_mode,
                            "rotation_mode_label": rotation_mode_label,
                            "execution_rule": "signal_close_next_open",
                            "rotation_rule": rotation_mode,
                            "strategy": request.strategy,
                        },
                        "price_source": DAILY_PRICE_SOURCE,
                    }
                )
            sell_symbols = planned["sell_symbols"]
            pending_rebalance = {
                "signal_date": current_date,
                "selected": selected,
                "selected_symbols": selected_symbols,
                "sell_rank_symbols": sell_rank_symbols,
                "sell_symbols": sell_symbols,
                "target_symbols": planned["target_symbols"],
                "target_weights": planned["target_weights"],
                "buy_symbols": planned["buy_symbols"],
                "buy_weights": planned["buy_weights"],
                "score_by_symbol": {item["symbol"]: item.get("factor_score") for item in ranked},
                "rank_by_symbol": rank_by_symbol,
                "percentile_by_symbol": {item["symbol"]: item.get("factor_percentile") for item in ranked},
                "rotation_mode": rotation_mode,
                "rotation_mode_label": rotation_mode_label,
                "should_rebalance": planned["should_rebalance"],
            }

        value = portfolio_value(cash, positions, last_prices)
        peak_value = max(peak_value, value)
        drawdown = (value / peak_value - 1) * 100 if peak_value > 0 else 0.0
        equity_curve.append(
            {
                "date": current_date.isoformat(),
                "value": _safe_float(value, 2),
                "cash": _safe_float(cash, 2),
                "position_value": _safe_float(value - cash, 2),
                "drawdown": _safe_float(drawdown, 2),
            }
        )

    current_value = equity_curve[-1]["value"] if equity_curve else float(request.initial_capital)
    initial_value = float(request.initial_capital)
    total_return = (current_value / initial_value - 1) * 100 if initial_value > 0 else 0.0
    yearly_stats = build_yearly_stats(equity_curve, benchmark_curve, candidate_etfs)
    elapsed_days = (date.fromisoformat(equity_curve[-1]["date"]) - date.fromisoformat(equity_curve[0]["date"])).days if len(equity_curve) > 1 else 0
    annualized_return = ((1 + total_return / 100) ** (365 / elapsed_days) - 1) * 100 if elapsed_days > 0 and total_return > -100 else 0.0
    win_count = sum(1 for item in closed_profits if item > 0)
    risk_metrics = _compute_equity_risk_metrics(equity_curve, annualized_return)

    holdings: List[Dict[str, Any]] = []
    for symbol, position in positions.items():
        price = float(last_prices.get(symbol) or position.get("last_price") or position.get("avg_cost") or 0)
        market_value = int(position["shares"]) * price
        holdings.append(
            {
                "symbol": symbol,
                "shares": int(position["shares"]),
                "price": _safe_float(price, 4),
                "avg_cost": _safe_float(position.get("avg_cost"), 4),
                "entry_date": position["entry_date"].isoformat() if position.get("entry_date") else None,
                "market_value": _safe_float(market_value, 2),
                "actual_weight_pct": _safe_float(market_value / current_value * 100 if current_value > 0 else 0, 2),
            }
        )
    holdings.sort(key=lambda item: item.get("market_value") or 0, reverse=True)

    metrics = {
        "total_return": _safe_float(total_return, 2),
        "annualized_return": _safe_float(annualized_return, 2),
        "max_drawdown": _safe_float(min([item["drawdown"] for item in equity_curve] or [0]), 2),
        "signal_count": len(events),
        "rank_signal_count": len(events),
        "rebalance_count": rebalance_count,
        "buy_signal_count": sum(1 for item in trades if item["action"] == "BUY"),
        "sell_signal_count": sum(1 for item in trades if item["action"] == "SELL"),
        "trade_count": len(trades),
        "closed_trade_count": len(closed_profits),
        "win_count": win_count,
        "win_rate": _safe_float(win_count / len(closed_profits) * 100 if closed_profits else 0.0, 2),
        "ending_value": _safe_float(current_value, 2),
        "cash": equity_curve[-1]["cash"] if equity_curve else _safe_float(cash, 2),
        "holding_count": len(holdings),
        "pending_signal_date": pending_rebalance["signal_date"].isoformat() if pending_rebalance else None,
        **risk_metrics,
    }
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    metadata = _build_factor_backtest_metadata(request, candidate_etfs, universe_history, price_df, resolved_legs, end_date, elapsed_ms)
    metadata.update(
        {
            "symbol_count": prepared_data.get("symbol_count"),
            "universe_size_latest": next(reversed(universe_size_by_date.values()), None) if universe_size_by_date else None,
            "execution_rule": "signal_close_next_open",
            "rotation_rule": rotation_mode,
            "strategy": request.strategy,
        }
    )
    symbols_for_names = set(candidate_etfs or [])
    symbols_for_names.update(request.custom_symbols or [])
    symbols_for_names.update(row.get("symbol") for row in trades)
    symbols_for_names.update(row.get("symbol") for row in holdings)
    symbols_for_names.update(row.get("primary_benchmark_symbol") for row in yearly_stats)
    symbol_names = load_symbol_name_map(symbols_for_names, db)
    attach_symbol_names(trades, symbol_names)
    attach_symbol_names(holdings, symbol_names)
    for row in yearly_stats:
        benchmark_symbol = normalize_symbol_for_name(row.get("primary_benchmark_symbol"))
        row["primary_benchmark_symbol_name"] = symbol_names.get(benchmark_symbol)
    metadata["symbol_names"] = symbol_names

    return {
        "metadata": metadata,
        "meta": metadata,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "yearly_stats": yearly_stats,
        "events": events,
        "trades": trades,
        "current_holdings": holdings,
        "component_correlation": [],
        "errors": [],
    }


def make_virtual_signal_backtest_config(config: Any) -> FactorBacktestConfig:
    candidate_etfs = list(dict.fromkeys(_get_attr(config, "candidate_etfs", None) or DEFAULT_CANDIDATE_ETFS))
    configured_legs = _get_attr(config, "legs", None) or default_virtual_factor_leg_payloads()
    legs = [
        FactorBacktestLeg(
            factor=_get_attr(leg, "factor"),
            window=_get_attr(leg, "window", 20),
            weight=float(_get_attr(leg, "weight", 0) or 0),
            neutralization=_get_attr(leg, "neutralization", "none"),
            standardization=_get_attr(leg, "standardization", "rank_percentile"),
            momentum_weights=normalize_momentum_weights_payload(_get_attr(leg, "momentum_weights", DEFAULT_MOMENTUM_WEIGHTS)),
        )
        for leg in configured_legs
        if _get_attr(leg, "factor")
    ]
    return FactorBacktestConfig(
        pool="CUSTOM",
        pool_label=_get_attr(config, "name", "美股多因子策略"),
        candidate_etfs=candidate_etfs,
        start_date=_get_attr(config, "start_date"),
        end_date=None,
        initial_capital=float(_get_attr(config, "initial_capital", 100_000.0) or 100_000.0),
        max_positions=max(1, int(_get_attr(config, "max_positions", 7) or 7)),
        sell_rank_multiplier=max(1.0, float(_get_attr(config, "sell_rank_multiplier", DEFAULT_SELL_RANK_MULTIPLIER) or DEFAULT_SELL_RANK_MULTIPLIER)),
        rebalance_frequency=normalize_rebalance_frequency(_get_attr(config, "rebalance_frequency", DEFAULT_REBALANCE_FREQUENCY)),
        commission_pct=max(0.0, float(_get_attr(config, "commission_pct", 0.03) or 0.0)),
        slippage_pct=max(0.0, float(_get_attr(config, "slippage_pct", 0.02) or 0.0)),
        lot_size=max(1, int(_get_attr(config, "lot_size", 1) or 1)),
        min_listing_days=max(0, int(_get_attr(config, "min_listing_days", DEFAULT_MIN_LISTING_DAYS) or 0)),
        legs=legs,
        mode="us_stock_signal_virtual",
        strategy="multi_factor_top_n_rotation",
    )
