import bisect
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set

import numpy as np
from sqlalchemy import distinct, or_
from sqlalchemy.orm import Session as ORMSession

from ..core.database import ETFHolding
from ..core.services.factor_backtest_engine import (
    make_virtual_signal_backtest_config,
    run_factor_backtest as run_shared_factor_backtest,
)
from ..core.utils import normalize_us_equity_symbol

logger = logging.getLogger(__name__)

DAILY_PRICE_SOURCE = "daily_close"
NEXT_OPEN_PRICE_SOURCE = "next_open"
DEFAULT_CANDIDATE_ETFS = ["SPY.US", "QQQ.US"]
SUPPORTED_MOMENTUM_WINDOWS = [20, 60, 120]
DEFAULT_MOMENTUM_WEIGHTS = {"20": 0.05, "60": 0.20, "120": 0.75}
DEFAULT_SELL_RANK_MULTIPLIER = 2.0
DEFAULT_REBALANCE_FREQUENCY = "weekly"
SUPPORTED_REBALANCE_FREQUENCIES = ["daily", "weekly", "monthly"]
CANDIDATE_ETF_OPTIONS = [
    {"label": "标普500", "value": "SPY.US", "description": "SPDR S&P 500 ETF Trust 成分股"},
    {"label": "纳指100", "value": "QQQ.US", "description": "Invesco QQQ Trust 成分股"},
]
TRADING_DAYS_PER_YEAR = 252


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


def _to_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _is_positive_number(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _floor_lot(quantity: float, lot_size: int = 1) -> int:
    lot = max(1, int(lot_size or 1))
    if quantity <= 0:
        return 0
    return int(quantity // lot) * lot


def _portfolio_value(cash: float, positions: Dict[str, Dict], last_prices: Dict[str, float]) -> float:
    value = float(cash or 0)
    for symbol, position in positions.items():
        price = last_prices.get(symbol) or position.get("last_price") or position.get("avg_cost") or 0
        value += int(position.get("shares") or 0) * float(price or 0)
    return value


def _normalize_momentum_weights(raw_weights) -> Dict[int, float]:
    raw_weights = raw_weights if isinstance(raw_weights, dict) else DEFAULT_MOMENTUM_WEIGHTS
    weights: Dict[int, float] = {}
    for window in SUPPORTED_MOMENTUM_WINDOWS:
        raw_value = raw_weights.get(str(window), raw_weights.get(window, 0.0))
        try:
            value = float(raw_value or 0)
        except (TypeError, ValueError):
            value = 0.0
        weights[window] = max(0.0, value)

    total = sum(weights.values())
    if total <= 0:
        weights = {20: 1.0, 60: 0.0, 120: 0.0}
        total = 1.0
    return {
        window: weight / total
        for window, weight in weights.items()
        if weight > 0
    }


def _format_momentum_weights(weights: Dict[int, float]) -> Dict[str, float]:
    return {
        str(window): _round_or_none(weights.get(window, 0.0), 6) or 0.0
        for window in SUPPORTED_MOMENTUM_WINDOWS
    }


def _prepare_klines(raw_klines: List[Dict]) -> List[Dict]:
    normalized = []
    for item in raw_klines or []:
        item_date = _to_date(item.get("timestamp"))
        if not item_date:
            continue
        if not all(_is_positive_number(item.get(key)) for key in ("open", "high", "low", "close")):
            continue
        normalized.append({
            "date": item_date,
            "open": float(item.get("open")),
            "high": float(item.get("high")),
            "low": float(item.get("low")),
            "close": float(item.get("close")),
            "volume": float(item.get("volume") or 0),
            "turnover": float(item.get("turnover") or 0),
        })

    normalized.sort(key=lambda item: item["date"])
    return normalized


def _build_benchmark_curve(
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
            (row for row in rows if row["date"] >= start_date and _is_positive_number(row.get("close"))),
            None,
        )
        if first_row:
            initial_close_by_symbol[symbol] = float(first_row["close"])

    benchmark_curve = []
    for current_date in dates:
        values = {}
        for symbol, initial_close in initial_close_by_symbol.items():
            row = row_by_symbol_date.get(symbol, {}).get(current_date)
            if row and _is_positive_number(row.get("close")):
                last_close_by_symbol[symbol] = float(row["close"])
            latest_close = last_close_by_symbol.get(symbol)
            values[symbol] = (
                _round_or_none(float(initial_capital or 0) * latest_close / initial_close, 2)
                if latest_close and initial_close > 0
                else None
            )
        benchmark_curve.append({
            "date": current_date.isoformat(),
            "values": values,
        })
    return benchmark_curve


def _compute_period_return(start_value, end_value) -> Optional[float]:
    if not _is_positive_number(start_value) or not _is_positive_number(end_value):
        return None
    return (float(end_value) / float(start_value) - 1) * 100


def _build_yearly_stats(
    equity_curve: List[Dict],
    benchmark_curve: List[Dict],
    benchmark_symbols: List[str],
) -> List[Dict]:
    benchmark_by_date = {
        item.get("date"): item.get("values") or {}
        for item in benchmark_curve or []
        if item.get("date")
    }
    by_year: Dict[int, Dict] = {}

    for item in equity_curve or []:
        item_date = item.get("date")
        if not item_date:
            continue
        year = date.fromisoformat(item_date).year
        bucket = by_year.setdefault(year, {
            "year": year,
            "start_date": item_date,
            "end_date": item_date,
            "start_value": item.get("value"),
            "end_value": item.get("value"),
            "benchmark_start_values": {},
            "benchmark_end_values": {},
        })
        bucket["end_date"] = item_date
        bucket["end_value"] = item.get("value")
        benchmark_values = benchmark_by_date.get(item_date) or {}
        for symbol in benchmark_symbols or []:
            value = benchmark_values.get(symbol)
            if _is_positive_number(value) and symbol not in bucket["benchmark_start_values"]:
                bucket["benchmark_start_values"][symbol] = value
            if _is_positive_number(value):
                bucket["benchmark_end_values"][symbol] = value

    yearly_stats = []
    for year in sorted(by_year):
        bucket = by_year[year]
        strategy_return = _compute_period_return(bucket.get("start_value"), bucket.get("end_value"))
        benchmark_returns = {}
        excess_returns = {}
        outperformed_by_symbol = {}
        for symbol in benchmark_symbols or []:
            benchmark_return = _compute_period_return(
                bucket["benchmark_start_values"].get(symbol),
                bucket["benchmark_end_values"].get(symbol),
            )
            benchmark_returns[symbol] = _round_or_none(benchmark_return, 2)
            excess_return = (
                strategy_return - benchmark_return
                if strategy_return is not None and benchmark_return is not None
                else None
            )
            excess_returns[symbol] = _round_or_none(excess_return, 2)
            outperformed_by_symbol[symbol] = (
                bool(strategy_return > benchmark_return)
                if strategy_return is not None and benchmark_return is not None
                else None
            )

        valid_outperformance = [
            value for value in outperformed_by_symbol.values()
            if value is not None
        ]
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


def _compute_risk_adjusted_momentum_snapshot(rows: List[Dict], index: int, window: int = 20) -> Optional[Dict]:
    if index < 0 or index >= len(rows):
        return None
    lookback = max(2, int(window or 20))
    history = [row for row in rows[:index + 1] if _is_positive_number(row.get("close"))]
    if len(history) < lookback:
        return None

    window_rows = history[-lookback:]
    closes = np.array([float(row["close"]) for row in window_rows], dtype=float)
    if np.any(closes <= 0):
        return None

    x = np.arange(lookback, dtype=float)
    y = np.log(closes)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 0.0 if ss_tot <= 0 else max(0.0, 1.0 - (ss_res / ss_tot))

    daily_returns = np.diff(closes) / closes[:-1]
    annualized_vol_pct = float(np.std(daily_returns, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100) if len(daily_returns) > 1 else 0.0
    annualized_slope_pct = float(slope * TRADING_DAYS_PER_YEAR * 100)
    raw_score = float(annualized_slope_pct * r_squared)
    risk_adjusted_score = float(raw_score / annualized_vol_pct * 100) if annualized_vol_pct > 0 else None
    window_return_pct = float((closes[-1] / closes[0] - 1) * 100)

    return {
        "as_of": rows[index]["date"].isoformat(),
        "window": lookback,
        "window_return_pct": _round_or_none(window_return_pct, 4),
        "annualized_slope_pct": _round_or_none(annualized_slope_pct, 4),
        "r_squared": _round_or_none(r_squared, 6),
        "raw_score": _round_or_none(raw_score, 4),
        "annualized_volatility_pct": _round_or_none(annualized_vol_pct, 4),
        "risk_adjusted_score": _round_or_none(risk_adjusted_score, 4),
        "risk_adjusted_score_value": risk_adjusted_score,
    }


def _compute_mixed_risk_adjusted_momentum_snapshot(
    rows: List[Dict],
    index: int,
    weights: Dict[int, float],
) -> Optional[Dict]:
    components: Dict[str, Dict] = {}
    weighted_score = 0.0
    weighted_return = 0.0
    weighted_slope = 0.0
    weighted_r_squared = 0.0
    weighted_raw_score = 0.0
    weighted_volatility = 0.0

    for window, weight in sorted(weights.items()):
        snapshot = _compute_risk_adjusted_momentum_snapshot(rows, index, window)
        if not snapshot or snapshot.get("risk_adjusted_score") is None:
            return None
        components[str(window)] = snapshot
        weighted_score += float(snapshot.get("risk_adjusted_score_value") or snapshot.get("risk_adjusted_score") or 0) * weight
        weighted_return += float(snapshot.get("window_return_pct") or 0) * weight
        weighted_slope += float(snapshot.get("annualized_slope_pct") or 0) * weight
        weighted_r_squared += float(snapshot.get("r_squared") or 0) * weight
        weighted_raw_score += float(snapshot.get("raw_score") or 0) * weight
        weighted_volatility += float(snapshot.get("annualized_volatility_pct") or 0) * weight

    if not components:
        return None

    return {
        "as_of": rows[index]["date"].isoformat(),
        "window": "+".join(str(window) for window in sorted(weights)),
        "active_windows": sorted(weights),
        "momentum_weights": _format_momentum_weights(weights),
        "window_return_pct": _round_or_none(weighted_return, 4),
        "annualized_slope_pct": _round_or_none(weighted_slope, 4),
        "r_squared": _round_or_none(weighted_r_squared, 6),
        "raw_score": _round_or_none(weighted_raw_score, 4),
        "annualized_volatility_pct": _round_or_none(weighted_volatility, 4),
        "risk_adjusted_score": _round_or_none(weighted_score, 4),
        "risk_adjusted_score_value": weighted_score,
        "components": components,
    }


def _normalize_rebalance_frequency(value) -> str:
    text = str(value or DEFAULT_REBALANCE_FREQUENCY).strip().lower()
    return text if text in SUPPORTED_REBALANCE_FREQUENCIES else DEFAULT_REBALANCE_FREQUENCY


def _is_rebalance_day(dates: List[date], index: int, frequency: str = DEFAULT_REBALANCE_FREQUENCY) -> bool:
    if index >= len(dates) - 1:
        return True
    current_date = dates[index]
    next_date = dates[index + 1]
    frequency = _normalize_rebalance_frequency(frequency)
    if frequency == "daily":
        return True
    if frequency == "weekly":
        return current_date.isocalendar()[:2] != next_date.isocalendar()[:2]
    if frequency == "monthly":
        return (current_date.year, current_date.month) != (next_date.year, next_date.month)
    return False


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


def _weights_for_date(
    universe_history: UniverseHistory,
    weight_history: Dict[str, Dict[date, Dict[str, float]]],
    current_date: date,
) -> Dict[str, float]:
    weights: Dict[str, float] = {}
    for etf_symbol, snapshot_dates in universe_history.snapshot_dates_by_etf.items():
        if not snapshot_dates:
            continue
        index = bisect.bisect_right(snapshot_dates, current_date) - 1
        if index < 0:
            continue
        snapshot_date = snapshot_dates[index]
        for symbol, weight in weight_history.get(etf_symbol, {}).get(snapshot_date, {}).items():
            weights[symbol] = weights.get(symbol, 0.0) + float(weight or 0)
    return weights


def _finite_float(value) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _rank_percentiles_from_values(values_by_symbol: Dict[str, float]) -> Dict[str, float]:
    valid_values = [
        (symbol, number)
        for symbol, value in values_by_symbol.items()
        for number in [_finite_float(value)]
        if number is not None
    ]
    if not valid_values:
        return {}
    if len(valid_values) == 1:
        return {valid_values[0][0]: 1.0}

    valid_values.sort(key=lambda item: item[1])
    denominator = len(valid_values) - 1
    percentiles: Dict[str, float] = {}
    start = 0
    while start < len(valid_values):
        end = start + 1
        value = valid_values[start][1]
        while end < len(valid_values) and valid_values[end][1] == value:
            end += 1
        average_rank = ((start + 1) + end) / 2
        percentile = (average_rank - 1) / denominator
        for index in range(start, end):
            percentiles[valid_values[index][0]] = percentile
        start = end
    return percentiles


def _build_momentum_window_percentiles(
    momentum_candidates: List[Dict],
    weights: Dict[int, float],
) -> Dict[int, Dict[str, float]]:
    percentiles_by_window: Dict[int, Dict[str, float]] = {}
    for window in sorted(weights):
        values_by_symbol: Dict[str, float] = {}
        for item in momentum_candidates:
            component = (item.get("snapshot") or {}).get("components", {}).get(str(window)) or {}
            score = _finite_float(component.get("risk_adjusted_score_value", component.get("risk_adjusted_score")))
            if score is not None:
                values_by_symbol[item["symbol"]] = score
        percentiles_by_window[window] = _rank_percentiles_from_values(values_by_symbol)
    return percentiles_by_window


def _apply_index_weight_blend(
    ranked: List[Dict],
    index_weight_blend: float,
    momentum_weights: Dict[int, float],
    momentum_percentiles_by_window: Dict[int, Dict[str, float]],
    index_weight_percentile: Dict[str, float],
) -> List[Dict]:
    blend = max(0.0, min(1.0, float(index_weight_blend or 0.0)))
    if not ranked:
        return ranked

    scored: List[Dict] = []
    for item in ranked:
        symbol = item["symbol"]
        window_percentiles: Dict[str, float] = {}
        momentum_score = 0.0
        missing_window = False
        for window, weight in sorted(momentum_weights.items()):
            percentile = momentum_percentiles_by_window.get(window, {}).get(symbol)
            if percentile is None:
                missing_window = True
                break
            window_percentiles[str(window)] = percentile
            momentum_score += percentile * float(weight)
        if missing_window:
            continue

        weight_percentile = index_weight_percentile.get(symbol)
        if blend > 0 and weight_percentile is None:
            continue
        weight_percentile = float(weight_percentile or 0.0)

        item["raw_mixed_risk_adjusted_score"] = item.get("momentum_score")
        item["momentum_score"] = momentum_score
        item["momentum_percentile"] = momentum_score
        item["momentum_window_percentiles"] = window_percentiles
        item["index_weight_percentile"] = weight_percentile
        item["rank_score"] = (1 - blend) * momentum_score + blend * weight_percentile
        item["factor_score"] = item["rank_score"]
        scored.append(item)

    ranked_result = sorted(
        scored,
        key=lambda item: (
            float(item.get("rank_score") or -1e18),
            float(item.get("turnover") or 0),
            item["symbol"],
        ),
        reverse=True,
    )
    denominator = max(1, len(ranked_result) - 1)
    for index, item in enumerate(ranked_result):
        item["factor_percentile"] = 1 - index / denominator
    return ranked_result


class USStockSignalVirtualEngine:
    def __init__(
        self,
        db: ORMSession,
        config,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ):
        self.db = db
        self.config = config
        self.progress_callback = progress_callback

    def report(self, progress: int, message: str):
        if self.progress_callback:
            self.progress_callback(int(max(0, min(100, progress))), message)

    def run(self) -> Dict:
        shared_config = make_virtual_signal_backtest_config(self.config)
        candidate_etfs = self.config.candidate_etfs or DEFAULT_CANDIDATE_ETFS

        self.report(1, "使用共享因子回测引擎从DuckDB读取历史行情")
        result = run_shared_factor_backtest(shared_config, self.db)
        metadata = result.get("metadata") or result.get("meta") or {}
        metadata.update(
            {
                "candidate_etfs": candidate_etfs,
                "min_listing_days": shared_config.min_listing_days,
                "max_positions": shared_config.max_positions,
                "sell_rank_threshold": max(shared_config.max_positions, int(round(shared_config.max_positions * shared_config.sell_rank_multiplier))),
                "sell_rank_multiplier": shared_config.sell_rank_multiplier,
                "rebalance_frequency": shared_config.rebalance_frequency,
                "execution_rule": "signal_close_next_open",
                "rotation_rule": "hold_until_out_of_sell_rank",
                "strategy": shared_config.strategy,
                "signal_price_source": DAILY_PRICE_SOURCE,
                "execution_price_source": NEXT_OPEN_PRICE_SOURCE,
                "price_source": DAILY_PRICE_SOURCE,
                "data_source": "duckdb.us_stock_daily",
            }
        )
        result["metadata"] = metadata
        result["meta"] = metadata
        result.setdefault("errors", [])
        self.report(100, "共享因子回测引擎运行完成")
        return result
