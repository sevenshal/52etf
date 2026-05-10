import bisect
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Set, Union

import numpy as np
import polars as pl
from sqlalchemy import distinct, or_
from sqlalchemy.orm import Session as ORMSession

from ..database import ETFHolding, StockEVC, USStockIndustrySnapshot
from ..utils import normalize_us_equity_symbol

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")

DAILY_PRICE_SOURCE = "daily_close"
NEXT_OPEN_PRICE_SOURCE = "next_open"
DEFAULT_CANDIDATE_ETFS = ["SPY.US", "QQQ.US"]
SUPPORTED_MOMENTUM_WINDOWS = [20, 60, 120]
SUPPORTED_WINDOWS = [20, 60, 120]
MIXED_WINDOW_KEY = "mixed"
DEFAULT_MOMENTUM_WEIGHTS = {"20": 0.05, "60": 0.20, "120": 0.75}
DEFAULT_SELL_RANK_MULTIPLIER = 2.0
DEFAULT_REBALANCE_FREQUENCY = "weekly"
SUPPORTED_REBALANCE_FREQUENCIES = ["daily", "weekly", "monthly"]
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
    {"key": "QQQ", "label": "QQQ", "description": "纳指100成分股", "etfs": ["QQQ.US"]},
    {"key": "SPY", "label": "SPY", "description": "标普500成分股", "etfs": ["SPY.US"]},
    {"key": "SPY_QQQ", "label": "SPY+QQQ", "description": "标普500与纳指100成分股并集", "etfs": ["SPY.US", "QQQ.US"]},
]
POOL_ETFS = {item["key"]: item["etfs"] for item in POOL_OPTIONS}

FACTOR_DIRECTION_OPTIONS = {
    "higher_is_better": {"sign": 1.0, "label": "高值更好"},
    "lower_is_better": {"sign": -1.0, "label": "低值更好"},
    "exploratory": {"sign": 1.0, "label": "探索方向"},
}
NEUTRALIZATION_OPTIONS = {
    "none": {"label": "不做中性化"},
    "sector": {"label": "行业大类中性化（Sector）"},
    "sector_market_cap": {"label": "行业大类+市值中性化"},
    "fine_industry": {"label": "细行业中性化（Industry，小样本回退Sector）"},
    "fine_industry_market_cap": {"label": "细行业+市值中性化（小样本回退Sector）"},
}
STANDARDIZATION_OPTIONS = {
    "none": {"label": "不标准化"},
    "zscore": {"label": "截面 Z-Score"},
    "rank_percentile": {"label": "截面排名分位"},
}
MIN_FINE_INDUSTRY_NEUTRALIZATION_SIZE = 10
BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT = 8
BACKTEST_SEARCH_FACTOR_VALUES_CACHE_LIMIT = 8
MOMENTUM_FACTOR_SCORE_PREFIX = {
    "risk_adjusted_momentum": "_ram",
    "raw_momentum": "_raw_mom",
}


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
    start_date: date = date(2020, 1, 2)
    end_date: Optional[date] = None
    initial_capital: float = 100_000.0
    max_positions: int = 7
    sell_rank_multiplier: float = DEFAULT_SELL_RANK_MULTIPLIER
    rebalance_frequency: str = DEFAULT_REBALANCE_FREQUENCY
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


@dataclass(frozen=True)
class FactorContext:
    windows: List[int]
    momentum_weights: Dict[int, float]
    db: ORMSession
    symbols: List[str]
    start_date: date
    end_date: date
    analysis_dates: List[date] = field(default_factory=list)
    industry_df: Optional[pl.DataFrame] = None
    candidate_etfs: List[str] = field(default_factory=list)
    valuation_df: Optional[pl.DataFrame] = None
    weight_history: Optional[Dict[str, Dict[date, Dict[str, float]]]] = None


@dataclass(frozen=True)
class FactorDefinition:
    key: str
    label: str
    group: str
    description: str
    default_windows: List[int]
    supports_windows: bool
    supports_mixed_windows: bool
    direction: str
    compute: Any

    def to_option(self) -> Dict[str, Any]:
        direction = FACTOR_DIRECTION_OPTIONS.get(self.direction, FACTOR_DIRECTION_OPTIONS["exploratory"])
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "description": self.description,
            "default_windows": self.default_windows,
            "supports_windows": self.supports_windows,
            "supports_mixed_windows": self.supports_mixed_windows,
            "direction": self.direction,
            "direction_label": direction["label"],
            "direction_sign": direction["sign"],
        }


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


def get_max_trade_date() -> date:
    connection = _connect_duckdb()
    try:
        row = connection.execute("SELECT MAX(trade_date) FROM us_stock_daily").fetchone()
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


def load_price_frame(symbols: List[str], start_date: date, end_date: date) -> pl.DataFrame:
    safe_symbols = [
        symbol for symbol in list(dict.fromkeys(symbols))
        if symbol and SYMBOL_PATTERN.match(symbol)
    ]
    schema = {
        "symbol": pl.Utf8,
        "trade_date": pl.Date,
        "open": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "turnover": pl.Float64,
    }
    if not safe_symbols:
        return pl.DataFrame(schema=schema)

    symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in safe_symbols)
    query = f"""
        SELECT
            symbol,
            CAST(trade_date AS DATE) AS trade_date,
            CAST(open AS DOUBLE) AS open,
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
    connection = _connect_duckdb()
    try:
        df = pl.read_database(
            query,
            connection,
            execute_options={"parameters": [start_date, end_date]},
        )
    finally:
        connection.close()

    if df.is_empty():
        return pl.DataFrame(schema=schema)
    return df.with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("open").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("turnover").cast(pl.Float64),
    ).sort(["symbol", "trade_date"]).with_columns(
        pl.min("trade_date").over("symbol").alias("_first_trade_date")
    )


def normalize_momentum_weights(raw_weights: Dict[str, float], active_windows: List[int]) -> Dict[int, float]:
    active = list(dict.fromkeys(int(item) for item in active_windows))
    weights: Dict[int, float] = {}
    for window in active:
        raw_value = raw_weights.get(str(window), raw_weights.get(window, 0.0)) if isinstance(raw_weights, dict) else 0.0
        try:
            weights[window] = max(0.0, float(raw_value or 0))
        except (TypeError, ValueError):
            weights[window] = 0.0
    total = sum(weights.values())
    if total <= 0:
        return {window: 1.0 / len(active) for window in active}
    return {window: weight / total for window, weight in weights.items() if weight > 0}


def normalize_momentum_weights_payload(raw_weights: Dict[str, float]) -> Dict[str, float]:
    raw = raw_weights if isinstance(raw_weights, dict) else DEFAULT_MOMENTUM_WEIGHTS
    normalized: Dict[str, float] = {}
    for window in SUPPORTED_MOMENTUM_WINDOWS:
        try:
            weight = float(raw.get(str(window), raw.get(window, 0)) or 0)
        except (TypeError, ValueError):
            weight = 0.0
        normalized[str(window)] = max(0.0, weight)
    if sum(normalized.values()) <= 0:
        return DEFAULT_MOMENTUM_WEIGHTS.copy()
    return normalized


def normalize_rebalance_frequency(value) -> str:
    text = str(value or DEFAULT_REBALANCE_FREQUENCY).strip().lower()
    return text if text in SUPPORTED_REBALANCE_FREQUENCIES else DEFAULT_REBALANCE_FREQUENCY


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


def load_universe_history(
    db: ORMSession,
    candidate_etfs: List[str],
    start_date: date,
    end_date: date,
) -> UniverseHistory:
    candidate_etfs = list(dict.fromkeys(candidate_etfs or DEFAULT_CANDIDATE_ETFS))
    snapshot_dates_by_etf: Dict[str, List[date]] = {}
    symbols_by_etf_date: Dict[str, Dict[date, List[str]]] = {}
    all_symbols: Set[str] = set()
    holdings_date_count: Dict[str, int] = {}

    for etf_symbol in candidate_etfs:
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
    candidate_etfs = list(dict.fromkeys(candidate_etfs or DEFAULT_CANDIDATE_ETFS))
    weight_history: Dict[str, Dict[date, Dict[str, float]]] = {}
    for etf_symbol in candidate_etfs:
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


def _load_valuation_frame(
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    if not symbols:
        return pl.DataFrame()
    rows = (
        db.query(
            StockEVC.symbol,
            StockEVC.date,
            StockEVC.fair_value_lo,
            StockEVC.fair_value_hi,
            StockEVC.forward_pe_ratio,
            StockEVC.pe_ratio,
        )
        .filter(
            StockEVC.symbol.in_(symbols),
            StockEVC.date >= start_date,
            StockEVC.date <= end_date,
        )
        .all()
    )
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(
            {
                "symbol": [row.symbol for row in rows],
                "valuation_date": [row.date for row in rows],
                "fair_value_lo": [row.fair_value_lo for row in rows],
                "fair_value_hi": [row.fair_value_hi for row in rows],
                "forward_pe_ratio": [row.forward_pe_ratio for row in rows],
                "pe_ratio": [row.pe_ratio for row in rows],
            }
        )
        .with_columns(
            pl.col("valuation_date").cast(pl.Date),
            pl.col("fair_value_lo").cast(pl.Float64),
            pl.col("fair_value_hi").cast(pl.Float64),
            pl.col("forward_pe_ratio").cast(pl.Float64),
            pl.col("pe_ratio").cast(pl.Float64),
        )
        .with_columns(
            pl.coalesce(
                [
                    (pl.col("fair_value_lo") + pl.col("fair_value_hi")) / 2,
                    pl.col("fair_value_hi"),
                    pl.col("fair_value_lo"),
                ]
            ).alias("_fair_value_mid")
        )
        .filter(pl.col("_fair_value_mid").is_not_null() & (pl.col("_fair_value_mid") > 0))
        .sort(["symbol", "valuation_date"])
    )


def _load_industry_frame(
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    if not symbols:
        return pl.DataFrame()
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
            USStockIndustrySnapshot.symbol.in_(symbols),
            USStockIndustrySnapshot.provider == "fmp",
        )
        .all()
    )
    if not rows:
        return pl.DataFrame()
    return (
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
        .sort("symbol")
    )


def _ensure_base_columns(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.int_range(0, pl.len()).over("symbol").cast(pl.Float64).alias("_row_nr"),
            pl.col("close").log().alias("_log_close"),
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_daily_return"),
            pl.when(pl.col("volume").is_not_null() & (pl.col("volume") > 0))
            .then(pl.col("volume").log10())
            .otherwise(None)
            .alias("_log_volume"),
        )
    )


def _add_momentum_window_features(df: pl.DataFrame, window: int, prefix: str) -> pl.DataFrame:
    w = int(window)
    sum_x = w * (w - 1) / 2
    sum_x2 = w * (w - 1) * (2 * w - 1) / 6
    denominator = w * sum_x2 - sum_x * sum_x

    df = df.with_columns(
        pl.col("_log_close").rolling_sum(w, min_samples=w).over("symbol").alias(f"{prefix}_sum_y"),
        (pl.col("_log_close") ** 2).rolling_sum(w, min_samples=w).over("symbol").alias(f"{prefix}_sum_y2"),
        (pl.col("_row_nr") * pl.col("_log_close")).rolling_sum(w, min_samples=w).over("symbol").alias(f"{prefix}_sum_iy"),
        pl.col("_daily_return").rolling_std(w - 1, min_samples=w - 1).over("symbol").alias(f"{prefix}_daily_vol"),
        (pl.col("close") / pl.col("close").shift(w - 1).over("symbol") - 1).alias(f"{prefix}_window_return"),
    )
    df = df.with_columns(
        (
            pl.col(f"{prefix}_sum_iy")
            - (pl.col("_row_nr") - (w - 1)) * pl.col(f"{prefix}_sum_y")
        ).alias(f"{prefix}_sum_xy")
    )
    df = df.with_columns(
        ((w * pl.col(f"{prefix}_sum_xy") - sum_x * pl.col(f"{prefix}_sum_y")) / denominator).alias(f"{prefix}_slope")
    )
    df = df.with_columns(
        ((pl.col(f"{prefix}_sum_y") - pl.col(f"{prefix}_slope") * sum_x) / w).alias(f"{prefix}_intercept"),
        (pl.col(f"{prefix}_sum_y2") - (pl.col(f"{prefix}_sum_y") ** 2 / w)).alias(f"{prefix}_ss_tot"),
    )
    df = df.with_columns(
        (
            pl.col(f"{prefix}_sum_y2")
            - 2 * pl.col(f"{prefix}_intercept") * pl.col(f"{prefix}_sum_y")
            - 2 * pl.col(f"{prefix}_slope") * pl.col(f"{prefix}_sum_xy")
            + (pl.col(f"{prefix}_intercept") ** 2) * w
            + 2 * pl.col(f"{prefix}_intercept") * pl.col(f"{prefix}_slope") * sum_x
            + (pl.col(f"{prefix}_slope") ** 2) * sum_x2
        ).alias(f"{prefix}_ss_res")
    )
    return df.with_columns(
        pl.when(pl.col(f"{prefix}_ss_tot") > 0)
        .then((1 - pl.col(f"{prefix}_ss_res") / pl.col(f"{prefix}_ss_tot")).clip(0.0, 1.0))
        .otherwise(None)
        .alias(f"{prefix}_r_squared"),
        (pl.col(f"{prefix}_daily_vol") * math.sqrt(TRADING_DAYS_PER_YEAR) * 100).alias(f"{prefix}_annualized_vol_pct"),
        (pl.col(f"{prefix}_slope") * TRADING_DAYS_PER_YEAR * 100).alias(f"{prefix}_annualized_slope_pct"),
    )


def _add_risk_adjusted_momentum_score(df: pl.DataFrame, window: int) -> pl.DataFrame:
    w = int(window)
    prefix = f"_ram_{w}"
    df = _add_momentum_window_features(df, w, prefix)
    return df.with_columns(
        pl.when(pl.col(f"{prefix}_annualized_vol_pct") > 0)
        .then(pl.col(f"{prefix}_annualized_slope_pct") * pl.col(f"{prefix}_r_squared") / pl.col(f"{prefix}_annualized_vol_pct") * 100)
        .otherwise(None)
        .alias(f"{prefix}_score")
    )


def _add_raw_momentum_score(df: pl.DataFrame, window: int) -> pl.DataFrame:
    w = int(window)
    prefix = f"_raw_mom_{w}"
    df = _add_momentum_window_features(df, w, prefix)
    return df.with_columns(
        pl.when(pl.col(f"{prefix}_r_squared").is_not_null())
        .then(pl.col(f"{prefix}_annualized_slope_pct") * pl.col(f"{prefix}_r_squared"))
        .otherwise(None)
        .alias(f"{prefix}_score")
    )


def _compute_risk_adjusted_momentum(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    result = _ensure_base_columns(df)
    for window in context.momentum_weights:
        result = _add_risk_adjusted_momentum_score(result, window)
    factor_expr = None
    for window, weight in context.momentum_weights.items():
        expr = pl.col(f"_ram_{window}_score") * float(weight)
        factor_expr = expr if factor_expr is None else factor_expr + expr
    return result.with_columns((factor_expr if factor_expr is not None else pl.lit(None, dtype=pl.Float64)).alias("factor_value"))


def _compute_raw_momentum(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    result = _ensure_base_columns(df)
    for window in context.momentum_weights:
        result = _add_raw_momentum_score(result, window)
    factor_expr = None
    for window, weight in context.momentum_weights.items():
        expr = pl.col(f"_raw_mom_{window}_score") * float(weight)
        factor_expr = expr if factor_expr is None else factor_expr + expr
    return result.with_columns((factor_expr if factor_expr is not None else pl.lit(None, dtype=pl.Float64)).alias("factor_value"))


def _build_momentum_score_source_frame(price_df: pl.DataFrame, factor_key: str, windows: List[int]) -> pl.DataFrame:
    if price_df.is_empty():
        return price_df
    result = _ensure_base_columns(price_df)
    score_columns: List[str] = []
    for window in list(dict.fromkeys(int(item) for item in windows)):
        if factor_key == "risk_adjusted_momentum":
            result = _add_risk_adjusted_momentum_score(result, window)
            score_columns.append(f"_ram_{window}_score")
        elif factor_key == "raw_momentum":
            result = _add_raw_momentum_score(result, window)
            score_columns.append(f"_raw_mom_{window}_score")
    base_columns = [column for column in ["symbol", "trade_date", "close", "volume", "turnover", "_first_trade_date"] if column in result.columns]
    return result.select([*base_columns, *score_columns])


def _momentum_score_source_frame(
    price_df: pl.DataFrame,
    factor_key: str,
    windows: List[int],
    raw_factor_cache: Optional[Dict[Any, pl.DataFrame]],
) -> pl.DataFrame:
    cache_key = (factor_key, tuple(sorted(list(dict.fromkeys(int(item) for item in windows)))))
    if raw_factor_cache is not None and cache_key in raw_factor_cache:
        return raw_factor_cache[cache_key]
    source = _build_momentum_score_source_frame(price_df, factor_key, list(cache_key[1]))
    if raw_factor_cache is not None:
        raw_factor_cache[cache_key] = source
    return source


def _compute_volume_z(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    window = int(context.windows[0])
    result = _ensure_base_columns(df)
    return (
        result.with_columns(
            pl.col("_log_volume").shift(1).rolling_mean(window, min_samples=window).over("symbol").alias("_volume_avg"),
            pl.col("_log_volume").shift(1).rolling_std(window, min_samples=window).over("symbol").alias("_volume_std"),
        )
        .with_columns(
            pl.when(pl.col("_volume_std") > 0)
            .then((pl.col("_log_volume") - pl.col("_volume_avg")) / pl.col("_volume_std"))
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_volatility(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    window = int(context.windows[0])
    result = _ensure_base_columns(df)
    return result.with_columns(
        (pl.col("_daily_return").rolling_std(window, min_samples=window).over("symbol") * math.sqrt(TRADING_DAYS_PER_YEAR) * 100).alias("factor_value")
    )


def _compute_valuation_gap(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    valuation_df = context.valuation_df
    if valuation_df is None:
        valuation_df = _load_valuation_frame(context.db, context.symbols, context.start_date - timedelta(days=540), context.end_date)
    if valuation_df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .join_asof(valuation_df, left_on="trade_date", right_on="valuation_date", by="symbol", strategy="backward")
        .with_columns(((pl.col("_fair_value_mid") / pl.col("close")) - 1).alias("factor_value"))
    )


def _compute_index_weight(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    candidate_etfs = list(dict.fromkeys(context.candidate_etfs or DEFAULT_CANDIDATE_ETFS))
    analysis_dates = context.analysis_dates or (
        df.filter((pl.col("trade_date") >= context.start_date) & (pl.col("trade_date") <= context.end_date))
        .select("trade_date")
        .unique()
        .sort("trade_date")
        .to_series()
        .to_list()
    )
    weight_history = context.weight_history
    if weight_history is None:
        weight_history = load_universe_weight_history(context.db, candidate_etfs, context.start_date, context.end_date)
    if not analysis_dates or not weight_history:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    etf_weight = 1.0 / len(candidate_etfs)
    sorted_weight_dates = {etf_symbol: sorted(history.keys()) for etf_symbol, history in weight_history.items()}
    records: List[Dict[str, Any]] = []
    for current_date in analysis_dates:
        combined_weights: Dict[str, float] = {}
        for etf_symbol in candidate_etfs:
            snapshot_dates = sorted_weight_dates.get(etf_symbol) or []
            date_index = bisect.bisect_right(snapshot_dates, current_date) - 1
            if date_index < 0:
                continue
            snapshot_date = snapshot_dates[date_index]
            for symbol, weight in weight_history.get(etf_symbol, {}).get(snapshot_date, {}).items():
                combined_weights[symbol] = combined_weights.get(symbol, 0.0) + float(weight or 0.0) * etf_weight
        for symbol, weight in combined_weights.items():
            records.append({"trade_date": current_date, "symbol": symbol, "factor_value": weight})

    if not records:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    weight_df = pl.DataFrame(records).with_columns(pl.col("trade_date").cast(pl.Date), pl.col("factor_value").cast(pl.Float64))
    return df.join(weight_df, on=["symbol", "trade_date"], how="left")


def _compute_custom_momentum_volume(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    momentum = _compute_risk_adjusted_momentum(df, context).select(["symbol", "trade_date", "factor_value"])
    volume_context = FactorContext(
        windows=[20],
        momentum_weights=context.momentum_weights,
        db=context.db,
        symbols=context.symbols,
        start_date=context.start_date,
        end_date=context.end_date,
        candidate_etfs=context.candidate_etfs,
    )
    volume = _compute_volume_z(df, volume_context).select(["symbol", "trade_date", pl.col("factor_value").alias("_volume_z")])
    return (
        df.join(momentum.rename({"factor_value": "_momentum_score"}), on=["symbol", "trade_date"], how="left")
        .join(volume, on=["symbol", "trade_date"], how="left")
        .with_columns(
            (
                pl.col("_momentum_score").rank("average").over("trade_date")
                + pl.col("_volume_z").rank("average").over("trade_date") * 0.25
            ).alias("factor_value")
        )
    )


FACTOR_REGISTRY: Dict[str, FactorDefinition] = {
    "raw_momentum": FactorDefinition(
        key="raw_momentum",
        label="动量：原始动量",
        group="动量",
        description="与风险调整动量同源：ln(close) 回归斜率 * R2，不除以波动率，用来和风险调整版直接对照。",
        default_windows=[20, 60, 120],
        supports_windows=True,
        supports_mixed_windows=True,
        direction="higher_is_better",
        compute=_compute_raw_momentum,
    ),
    "risk_adjusted_momentum": FactorDefinition(
        key="risk_adjusted_momentum",
        label="动量：风险调整动量",
        group="动量",
        description="ln(close) 回归斜率 * R2 / 年化波动。",
        default_windows=[20, 60, 120],
        supports_windows=True,
        supports_mixed_windows=True,
        direction="higher_is_better",
        compute=_compute_risk_adjusted_momentum,
    ),
    "volume_z": FactorDefinition(
        key="volume_z",
        label="成交量：对数成交量Z分数",
        group="成交量",
        description="log10(volume) 相对过去窗口均值和标准差的异常程度，窗口不含当天。",
        default_windows=[20],
        supports_windows=True,
        supports_mixed_windows=False,
        direction="exploratory",
        compute=_compute_volume_z,
    ),
    "volatility": FactorDefinition(
        key="volatility",
        label="波动：年化波动率",
        group="波动",
        description="过去窗口日收益标准差年化。",
        default_windows=[20],
        supports_windows=True,
        supports_mixed_windows=False,
        direction="exploratory",
        compute=_compute_volatility,
    ),
    "valuation_gap": FactorDefinition(
        key="valuation_gap",
        label="估值：安全边际",
        group="估值",
        description="最近一次EVC估值中值 / 当日收盘价 - 1。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_valuation_gap,
    ),
    "index_weight": FactorDefinition(
        key="index_weight",
        label="指数：成分权重",
        group="指数",
        description="股票在所选股票池ETF中的成分权重。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_index_weight,
    ),
    "custom_momentum_volume": FactorDefinition(
        key="custom_momentum_volume",
        label="自定义：动量+成交量示例",
        group="自定义",
        description="风险调整混合动量截面排名 + 0.25 * 成交量Z分数截面排名。",
        default_windows=[20, 60, 120],
        supports_windows=True,
        supports_mixed_windows=True,
        direction="higher_is_better",
        compute=_compute_custom_momentum_volume,
    ),
}


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


def _apply_factor_direction(df: pl.DataFrame, factor_definition: FactorDefinition) -> pl.DataFrame:
    if df.is_empty() or "factor_value" not in df.columns:
        return df
    direction = FACTOR_DIRECTION_OPTIONS.get(factor_definition.direction, FACTOR_DIRECTION_OPTIONS["exploratory"])
    sign = float(direction["sign"])
    return df.with_columns(
        pl.col("factor_value").alias("factor_value_raw"),
        (pl.col("factor_value") * sign).alias("factor_value_directional"),
        (pl.col("factor_value") * sign).alias("factor_value"),
    )


def _with_neutralization_columns(df: pl.DataFrame, industry_df: Optional[pl.DataFrame], neutralization: str) -> pl.DataFrame:
    source = df
    if industry_df is not None and not industry_df.is_empty():
        source = source.sort(["symbol", "trade_date"]).join(industry_df, on="symbol", how="left")
    for column in ["industry_group", "industry", "sector", "sub_industry", "market_cap"]:
        if column not in source.columns:
            source = source.with_columns(pl.lit(None).alias(column))
    source = source.with_columns(
        pl.when(pl.col("industry").is_not_null() & (pl.col("industry").cast(pl.Utf8).str.len_chars() > 0))
        .then(pl.col("industry"))
        .otherwise(pl.col("sector"))
        .alias("_fine_industry_group")
    )
    if "fine_industry" in neutralization:
        source = source.with_columns(pl.len().over(["trade_date", "_fine_industry_group"]).alias("_fine_group_size")).with_columns(
            pl.when(pl.col("_fine_group_size") >= MIN_FINE_INDUSTRY_NEUTRALIZATION_SIZE)
            .then(pl.col("_fine_industry_group"))
            .otherwise(pl.col("sector"))
            .alias("_neutral_group")
        )
    else:
        source = source.with_columns(pl.col("sector").alias("_neutral_group"))
    return source.with_columns(
        pl.when(pl.col("_neutral_group").is_not_null() & (pl.col("_neutral_group").cast(pl.Utf8).str.len_chars() > 0))
        .then(pl.col("_neutral_group"))
        .otherwise(pl.lit("__UNKNOWN__"))
        .alias("_neutral_group")
    )


def _neutralize_by_group(df: pl.DataFrame, include_market_cap: bool) -> pl.DataFrame:
    source = df.filter(pl.col("factor_value").is_not_null() & pl.col("factor_value").is_finite())
    if source.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value_neutralized")).with_columns(
            pl.col("factor_value_neutralized").alias("factor_value")
        )
    result = source.with_columns(
        (pl.col("factor_value") - pl.mean("factor_value").over(["trade_date", "_neutral_group"])).alias("_group_residual")
    )
    if include_market_cap:
        result = result.with_columns(
            pl.when(pl.col("market_cap").is_not_null() & (pl.col("market_cap") > 0))
            .then(pl.col("market_cap").log())
            .otherwise(None)
            .alias("_log_market_cap")
        ).with_columns(
            pl.mean("_group_residual").over("trade_date").alias("_resid_mean"),
            pl.mean("_log_market_cap").over("trade_date").alias("_mcap_mean"),
        ).with_columns(
            ((pl.col("_group_residual") - pl.col("_resid_mean")) * (pl.col("_log_market_cap") - pl.col("_mcap_mean"))).alias("_cov_part"),
            ((pl.col("_log_market_cap") - pl.col("_mcap_mean")) ** 2).alias("_var_part"),
        ).with_columns(
            pl.sum("_cov_part").over("trade_date").alias("_cov"),
            pl.sum("_var_part").over("trade_date").alias("_var"),
        ).with_columns(
            pl.when(pl.col("_var") > 0).then(pl.col("_cov") / pl.col("_var")).otherwise(0.0).alias("_beta")
        ).with_columns(
            (pl.col("_group_residual") - pl.col("_beta") * (pl.col("_log_market_cap") - pl.col("_mcap_mean"))).alias("_group_residual")
        )
    return df.join(
        result.select("symbol", "trade_date", pl.col("_group_residual").alias("factor_value_neutralized")),
        on=["symbol", "trade_date"],
        how="left",
    ).with_columns(pl.col("factor_value_neutralized").alias("factor_value"))


def _apply_factor_neutralization(df: pl.DataFrame, neutralization: str, industry_df: Optional[pl.DataFrame]) -> pl.DataFrame:
    if neutralization == "none" or df.is_empty() or "factor_value" not in df.columns:
        return df
    source = _with_neutralization_columns(df, industry_df, neutralization)
    include_market_cap = neutralization.endswith("_market_cap")
    return _neutralize_by_group(source, include_market_cap)


def _apply_factor_standardization(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "factor_value" not in df.columns:
        return df
    return (
        df.with_columns(
            pl.when(pl.col("factor_value").is_not_null() & pl.col("factor_value").is_finite())
            .then(pl.col("factor_value"))
            .otherwise(None)
            .alias("_factor_for_standardization")
        )
        .with_columns(
            pl.mean("_factor_for_standardization").over("trade_date").alias("_factor_mean"),
            pl.std("_factor_for_standardization").over("trade_date").alias("_factor_std"),
        )
        .with_columns(
            pl.when((pl.col("_factor_std") > 0) & pl.col("factor_value").is_finite())
            .then((pl.col("factor_value") - pl.col("_factor_mean")) / pl.col("_factor_std"))
            .otherwise(pl.col("factor_value"))
            .alias("factor_value_standardized")
        )
        .with_columns(pl.col("factor_value_standardized").alias("factor_value"))
        .drop(["_factor_for_standardization", "_factor_mean", "_factor_std"])
    )


def _with_cross_section_rank_percentile(df: pl.DataFrame, source_column: str, output_column: str) -> pl.DataFrame:
    if df.is_empty() or source_column not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias(output_column))
    valid_column = f"_{output_column}_valid"
    rank_column = f"_{output_column}_rank"
    count_column = f"_{output_column}_count"
    return (
        df.with_columns(
            pl.when(pl.col(source_column).is_not_null() & pl.col(source_column).is_finite())
            .then(pl.col(source_column).cast(pl.Float64))
            .otherwise(None)
            .alias(valid_column)
        )
        .with_columns(
            pl.col(valid_column).rank("average").over("trade_date").alias(rank_column),
            pl.col(valid_column).count().over("trade_date").alias(count_column),
        )
        .with_columns(
            pl.when(pl.col(valid_column).is_null())
            .then(None)
            .when(pl.col(count_column) <= 1)
            .then(1.0)
            .otherwise((pl.col(rank_column) - 1) / (pl.col(count_column) - 1))
            .alias(output_column)
        )
        .drop([valid_column, rank_column, count_column])
    )


def _apply_factor_rank_percentile(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "factor_value" not in df.columns:
        return df
    return _with_cross_section_rank_percentile(df, "factor_value", "factor_value_rank_percentile").with_columns(
        pl.col("factor_value_rank_percentile").alias("factor_value")
    )


def _apply_factor_transformations(df: pl.DataFrame, request: Any, context: FactorContext) -> pl.DataFrame:
    result = df
    neutralization = _get_attr(request, "neutralization", "none")
    standardization = _get_attr(request, "standardization", "rank_percentile")
    if neutralization != "none":
        result = _apply_factor_neutralization(result, neutralization, context.industry_df)
    if standardization == "zscore":
        result = _apply_factor_standardization(result)
    elif standardization == "rank_percentile":
        result = _apply_factor_rank_percentile(result)
    return result


def _prepare_factor_frame(price_df: pl.DataFrame, factor_definition: FactorDefinition, context: FactorContext, request: Any) -> pl.DataFrame:
    return _apply_factor_transformations(_apply_factor_direction(factor_definition.compute(price_df, context), factor_definition), request, context)


def _prepare_momentum_factor_frame_from_source(
    source_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    context: FactorContext,
    request: Any,
) -> pl.DataFrame:
    prefix = MOMENTUM_FACTOR_SCORE_PREFIX.get(factor_definition.key)
    if source_df.is_empty() or not prefix:
        return source_df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    base_columns = [column for column in ["symbol", "trade_date", "close", "volume", "turnover", "_first_trade_date"] if column in source_df.columns]
    result = source_df.select(base_columns).unique(subset=["symbol", "trade_date"])
    factor_expr = None
    for window, weight in context.momentum_weights.items():
        column = f"{prefix}_{int(window)}_score"
        if column not in source_df.columns:
            continue
        window_factor_column = f"_window_factor_{int(window)}"
        window_df = source_df.select([*base_columns, column]).with_columns(pl.col(column).alias("factor_value"))
        window_df = _apply_factor_transformations(
            _apply_factor_direction(window_df, factor_definition),
            request,
            context,
        ).select("symbol", "trade_date", pl.col("factor_value").alias(window_factor_column))
        result = result.join(window_df, on=["symbol", "trade_date"], how="left")
        expr = pl.col(window_factor_column) * float(weight)
        factor_expr = expr if factor_expr is None else factor_expr + expr
    result = result.with_columns((factor_expr if factor_expr is not None else pl.lit(None, dtype=pl.Float64)).alias("factor_value"))
    return result.select([*base_columns, "factor_value"])


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
    base_columns = [column for column in ["symbol", "trade_date", "close", "volume", "turnover", "_first_trade_date"] if column in price_df.columns]
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
    end_date = request.end_date or get_max_trade_date()
    candidate_etfs = list(dict.fromkeys(request.candidate_etfs or POOL_ETFS.get(request.pool, DEFAULT_CANDIDATE_ETFS)))
    required_windows = required_windows_for_legs(resolved_legs)
    max_factor_window = max(required_windows)
    fetch_padding_days = max(370, int(request.min_listing_days) + 30, int(max_factor_window * 3))
    fetch_start = request.start_date - timedelta(days=fetch_padding_days)

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

    factor_keys = {leg["factor"]["key"] for leg in resolved_legs}
    needs_industry = any(leg["neutralization"] != "none" for leg in resolved_legs)
    industry_df = _load_industry_frame(db, universe_history.all_symbols, request.start_date - timedelta(days=3650), end_date) if needs_industry else None
    valuation_df = _load_valuation_frame(db, universe_history.all_symbols, request.start_date - timedelta(days=540), end_date) if "valuation_gap" in factor_keys else None
    weight_history = load_universe_weight_history(db, candidate_etfs, request.start_date, end_date) if "index_weight" in factor_keys else None
    benchmark_rows = _price_frame_to_rows_by_symbol(load_price_frame(candidate_etfs, request.start_date - timedelta(days=10), end_date))

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
        )
        leg_request = SimpleNamespace(neutralization=leg["neutralization"], standardization=leg["standardization"])
        factor_df = _prepare_cached_leg_factor_frame(price_df, factor_definition, leg_context, leg_request, raw_factor_cache).select(
            "symbol", "trade_date", "factor_value"
        )
        _put_limited_cache(component_factor_cache, _backtest_leg_factor_cache_key(leg), factor_df, BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT)
    factor_columns = [column for column in ["symbol", "trade_date", "close", "volume", "turnover", "_first_trade_date"] if column in price_df.columns]
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
    pool_label = request.pool_label or next((item["label"] for item in POOL_OPTIONS if item["key"] == request.pool), request.pool)
    return {
        "mode": request.mode,
        "pool": request.pool,
        "pool_label": pool_label,
        "candidate_etfs": candidate_etfs,
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
        "max_positions": request.max_positions,
        "sell_rank_multiplier": request.sell_rank_multiplier,
        "sell_rank_threshold": max(request.max_positions, int(math.ceil(request.max_positions * request.sell_rank_multiplier))),
        "factor_combination_method": "weighted_standardized_factor_values",
        "factor_combination_method_label": "子因子标准化后加权",
        "rebalance_frequency": request.rebalance_frequency,
        "commission_pct": request.commission_pct,
        "slippage_pct": request.slippage_pct,
        "lot_size": request.lot_size,
        "min_listing_days": request.min_listing_days,
        "windows": required_windows,
        "universe_symbols": len(universe_history.all_symbols),
        "holdings_date_count": universe_history.holdings_date_count,
        "price_rows": int(price_df.height),
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

    max_positions = int(request.max_positions)
    sell_rank_multiplier = float(request.sell_rank_multiplier)
    sell_rank_threshold = max(max_positions, int(math.ceil(max_positions * sell_rank_multiplier)))
    rebalance_frequency = normalize_rebalance_frequency(request.rebalance_frequency)
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
            for symbol in list(pending_rebalance["sell_symbols"]):
                if symbol not in positions:
                    continue
                shares = int(positions[symbol].get("shares") or 0)
                price, price_source, quote_timestamp = get_execution_price(
                    symbol,
                    current_date,
                    "SELL",
                    open_map,
                    quantity=shares,
                )
                if price is None or price <= 0:
                    continue
                sell_position(
                    current_date,
                    signal_date,
                    symbol,
                    shares,
                    price,
                    f"下一交易日开盘执行: 跌出因子排名Top{sell_rank_threshold}: {', '.join(pending_rebalance['sell_rank_symbols'])}",
                    price_source,
                    quote_timestamp,
                )
            slots_to_fill = max(0, max_positions - len(positions))
            buy_candidates = [item for item in pending_rebalance["selected"] if item["symbol"] not in positions][:slots_to_fill]
            budget_per_symbol = cash / len(buy_candidates) if buy_candidates else 0.0
            for item in buy_candidates:
                symbol = item["symbol"]
                buy_budget = min(cash, budget_per_symbol)
                if buy_budget <= 0:
                    continue
                price, price_source, quote_timestamp = get_execution_price(
                    symbol,
                    current_date,
                    "BUY",
                    open_map,
                    budget=buy_budget,
                )
                if price is None or price <= 0:
                    continue
                buy_position(
                    current_date,
                    signal_date,
                    symbol,
                    buy_budget,
                    price,
                    f"下一交易日开盘补位买入因子Top{max_positions}",
                    price_source,
                    quote_timestamp,
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
            rebalance_count += 1
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
                            "event_symbols": event_symbols,
                            "max_positions": max_positions,
                            "sell_rank_threshold": sell_rank_threshold,
                            "sell_rank_multiplier": sell_rank_multiplier,
                            "min_listing_days": request.min_listing_days,
                            "rebalance_frequency": rebalance_frequency,
                            "execution_rule": "signal_close_next_open",
                            "rotation_rule": "hold_until_out_of_sell_rank",
                            "strategy": request.strategy,
                        },
                        "price_source": DAILY_PRICE_SOURCE,
                    }
                )
            sell_symbols = [symbol for symbol in list(positions.keys()) if rank_by_symbol.get(symbol, 10**9) > sell_rank_threshold]
            pending_rebalance = {
                "signal_date": current_date,
                "selected": selected,
                "selected_symbols": selected_symbols,
                "sell_rank_symbols": sell_rank_symbols,
                "sell_symbols": sell_symbols,
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
            "rotation_rule": "hold_until_out_of_sell_rank",
            "strategy": request.strategy,
        }
    )
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
        pool_label=_get_attr(config, "name", "美股多因子策略虚拟盘"),
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
