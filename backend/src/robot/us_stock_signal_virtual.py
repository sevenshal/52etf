import bisect
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Set

import numpy as np
from sqlalchemy import distinct, or_
from sqlalchemy.orm import Session as ORMSession

from ..core.database import ETFHolding
from ..core.services.quote import QuoteService
from ..core.utils import normalize_us_equity_symbol

logger = logging.getLogger(__name__)

DAILY_PRICE_SOURCE = "daily_close"
NEXT_OPEN_PRICE_SOURCE = "next_open"
DEFAULT_CANDIDATE_ETFS = ["SPY.US", "QQQ.US"]
SUPPORTED_MOMENTUM_WINDOWS = [20, 60, 120]
DEFAULT_MOMENTUM_WEIGHTS = {"20": 0.05, "60": 0.20, "120": 0.75}
DEFAULT_INDEX_WEIGHT_BLEND = 0.40
DEFAULT_SELL_RANK_MULTIPLIER = 2.0
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
        weighted_score += float(snapshot.get("risk_adjusted_score") or 0) * weight
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
        "components": components,
    }


def _is_weekly_rebalance_day(dates: List[date], index: int) -> bool:
    if index >= len(dates) - 1:
        return True
    return dates[index].isocalendar()[:2] != dates[index + 1].isocalendar()[:2]


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


def _apply_index_weight_blend(ranked: List[Dict], index_weight_blend: float) -> List[Dict]:
    blend = max(0.0, min(1.0, float(index_weight_blend or 0.0)))
    if not ranked:
        return ranked

    ranked_by_momentum = sorted(
        ranked,
        key=lambda item: (
            float(item.get("momentum_score") or -1e18),
            float(item.get("turnover") or 0),
            item["symbol"],
        ),
        reverse=True,
    )
    ranked_by_weight = sorted(
        ranked,
        key=lambda item: (
            float(item.get("index_weight") or 0),
            float(item.get("turnover") or 0),
            item["symbol"],
        ),
        reverse=True,
    )
    denominator = max(1, len(ranked) - 1)
    momentum_percentile = {
        item["symbol"]: 1 - index / denominator
        for index, item in enumerate(ranked_by_momentum)
    }
    weight_percentile = {
        item["symbol"]: 1 - index / denominator
        for index, item in enumerate(ranked_by_weight)
    }

    for item in ranked:
        symbol = item["symbol"]
        item["momentum_percentile"] = momentum_percentile.get(symbol, 0.0)
        item["index_weight_percentile"] = weight_percentile.get(symbol, 0.0)
        item["rank_score"] = (
            (1 - blend) * item["momentum_percentile"]
            + blend * item["index_weight_percentile"]
            if blend > 0
            else float(item.get("momentum_score") or -1e18)
        )

    return sorted(
        ranked,
        key=lambda item: (
            float(item.get("rank_score") or -1e18),
            float(item.get("momentum_score") or -1e18),
            float(item.get("index_weight") or 0),
            float(item.get("turnover") or 0),
            item["symbol"],
        ),
        reverse=True,
    )


class USStockSignalVirtualEngine:
    def __init__(
        self,
        db: ORMSession,
        quote_service: QuoteService,
        config,
        end_date: Optional[date] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ):
        self.db = db
        self.quote_service = quote_service
        self.config = config
        self.end_date = end_date or date.today()
        self.progress_callback = progress_callback

    def report(self, progress: int, message: str):
        if self.progress_callback:
            self.progress_callback(int(max(0, min(100, progress))), message)

    def _fetch_klines(self, symbols: List[str], fetch_start: date, end_date: date) -> Dict[str, List[Dict]]:
        klines_by_symbol: Dict[str, List[Dict]] = {}
        errors = []
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            self.report(5 + int(35 * (index - 1) / max(1, total)), f"获取K线 {index}/{total}: {symbol}")
            try:
                raw_klines = self.quote_service.get_klines(symbol, start_date=fetch_start, end_date=end_date, period="d")
                rows = _prepare_klines(raw_klines)
                if rows:
                    klines_by_symbol[symbol] = rows
                else:
                    errors.append({"symbol": symbol, "message": "没有可用K线"})
            except Exception as exc:
                logger.warning("Fetch klines failed for %s: %s", symbol, exc)
                errors.append({"symbol": symbol, "message": str(exc)})
        return klines_by_symbol, errors

    def run(self) -> Dict:
        start_date = self.config.start_date
        end_date = self.end_date
        momentum_weights = _normalize_momentum_weights(getattr(self.config, "momentum_weights", None))
        momentum_weights_payload = _format_momentum_weights(momentum_weights)
        momentum_windows = sorted(momentum_weights)
        max_momentum_window = max(momentum_windows)
        momentum_label = "+".join(str(window) for window in momentum_windows)
        index_weight_blend = max(0.0, min(1.0, float(getattr(self.config, "index_weight_blend", DEFAULT_INDEX_WEIGHT_BLEND) or 0.0)))
        min_listing_days = max(0, int(getattr(self.config, "min_listing_days", 365) or 0))
        max_positions = max(1, int(self.config.max_positions or 1))
        sell_rank_multiplier = max(1.0, float(getattr(self.config, "sell_rank_multiplier", DEFAULT_SELL_RANK_MULTIPLIER) or 1.0))
        sell_rank_threshold = max(max_positions, int(round(max_positions * sell_rank_multiplier)))
        lot_size = max(1, int(self.config.lot_size or 1))
        commission_rate = max(0.0, float(self.config.commission_pct or 0)) / 100
        slippage_rate = max(0.0, float(self.config.slippage_pct or 0)) / 100
        candidate_etfs = self.config.candidate_etfs or DEFAULT_CANDIDATE_ETFS

        self.report(1, "读取历史成分股")
        universe_history = load_universe_history(self.db, candidate_etfs, start_date, end_date)
        weight_history = load_universe_weight_history(self.db, candidate_etfs, start_date, end_date)
        if not universe_history.all_symbols:
            raise ValueError("没有找到候选ETF的历史成分股，请先同步 ETF 持仓历史")

        fetch_padding_days = max(370, min_listing_days + 30, int(max_momentum_window * 3))
        fetch_start = start_date - timedelta(days=fetch_padding_days)
        self.report(4, f"准备获取 {len(universe_history.all_symbols)} 个候选股票K线")
        klines_by_symbol, errors = self._fetch_klines(universe_history.all_symbols, fetch_start, end_date)
        if not klines_by_symbol:
            raise ValueError("没有可用的候选股票K线")

        benchmark_rows_by_symbol, benchmark_errors = self._fetch_klines(
            candidate_etfs,
            start_date - timedelta(days=10),
            end_date,
        )
        errors.extend([{**item, "scope": "benchmark"} for item in benchmark_errors])

        index_by_symbol_date = {
            symbol: {row["date"]: index for index, row in enumerate(rows)}
            for symbol, rows in klines_by_symbol.items()
        }
        row_by_symbol_date = {
            symbol: {row["date"]: row for row in rows}
            for symbol, rows in klines_by_symbol.items()
        }
        dates = sorted({
            row["date"]
            for rows in klines_by_symbol.values()
            for row in rows
            if start_date <= row["date"] <= end_date
        })
        if not dates:
            raise ValueError("回跑区间内没有可用交易日")
        benchmark_curve = _build_benchmark_curve(
            benchmark_rows_by_symbol,
            dates,
            float(self.config.initial_capital or 0),
            start_date,
        )

        cash = float(self.config.initial_capital or 0)
        positions: Dict[str, Dict] = {}
        last_prices: Dict[str, float] = {}
        equity_curve = []
        events = []
        trades = []
        closed_profits = []
        peak_value = cash
        universe_size_by_date = {}
        rebalance_count = 0
        pending_rebalance = None

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
        ):
            amount = price * quantity
            portfolio_after = _portfolio_value(cash, positions, last_prices)
            symbol_market_value = 0.0
            if symbol in positions:
                symbol_market_value = int(positions[symbol].get("shares") or 0) * float(last_prices.get(symbol) or price)
            trades.append({
                "date": trade_date.isoformat(),
                "signal_date": signal_date.isoformat(),
                "action": action,
                "symbol": symbol,
                "price": _round_or_none(price, 4),
                "quantity": int(quantity),
                "amount": _round_or_none(amount, 2),
                "commission": _round_or_none(commission, 2),
                "profit": _round_or_none(profit, 2),
                "profit_pct": _round_or_none(profit_pct, 2),
                "reason": reason,
                "reason_detail": reason_detail,
                "cash_after": _round_or_none(cash, 2),
                "portfolio_value_after": _round_or_none(portfolio_after, 2),
                "symbol_market_value_after": _round_or_none(symbol_market_value, 2),
                "symbol_weight_pct_after": _round_or_none(symbol_market_value / portfolio_after * 100 if portfolio_after > 0 else 0, 2),
                "price_source": NEXT_OPEN_PRICE_SOURCE,
            })

        def sell_position(trade_date: date, signal_date: date, symbol: str, quantity: int, price: float, reason_detail: str):
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
                "weekly_rebalance",
                reason_detail,
                profit=profit,
                profit_pct=profit_pct,
            )

        def buy_position(trade_date: date, signal_date: date, symbol: str, budget: float, price: float, reason_detail: str):
            nonlocal cash
            buy_price = price * (1 + slippage_rate)
            quantity = _floor_lot(budget / (buy_price * (1 + commission_rate)), lot_size)
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
                "weekly_rebalance",
                reason_detail,
            )

        for date_index, current_date in enumerate(dates):
            if date_index % max(1, len(dates) // 100) == 0:
                self.report(42 + int(53 * date_index / max(1, len(dates))), f"模拟交易日 {date_index + 1}/{len(dates)}")

            open_map = {}
            price_map = {}
            for symbol, rows in row_by_symbol_date.items():
                row = rows.get(current_date)
                if not row:
                    continue
                open_price = float(row.get("open") or 0)
                close_price = float(row.get("close") or 0)
                if open_price > 0:
                    open_map[symbol] = open_price
                if close_price > 0:
                    price_map[symbol] = close_price

            if pending_rebalance:
                signal_date = pending_rebalance["signal_date"]
                for symbol in list(pending_rebalance["sell_symbols"]):
                    if symbol not in positions:
                        continue
                    price = open_map.get(symbol)
                    if price is None or price <= 0:
                        continue
                    shares = int(positions[symbol].get("shares") or 0)
                    sell_position(
                        current_date,
                        signal_date,
                        symbol,
                        shares,
                        price,
                        (
                            f"下一交易日开盘执行: 跌出风险调整{momentum_label}日混合动量"
                            f"Top{sell_rank_threshold}: {', '.join(pending_rebalance['sell_rank_symbols'])}"
                        ),
                    )

                slots_to_fill = max(0, max_positions - len(positions))
                buy_candidates = [
                    item
                    for item in pending_rebalance["selected"]
                    if item["symbol"] not in positions
                ][:slots_to_fill]
                budget_per_symbol = cash / len(buy_candidates) if buy_candidates else 0.0
                for item in buy_candidates:
                    symbol = item["symbol"]
                    price = open_map.get(symbol)
                    if price is None or price <= 0:
                        continue
                    buy_budget = min(cash, budget_per_symbol)
                    if buy_budget <= 0:
                        continue
                    buy_position(
                        current_date,
                        signal_date,
                        symbol,
                        buy_budget,
                        price,
                        f"下一交易日开盘补位买入风险调整{momentum_label}日混合动量Top{max_positions}",
                    )
                pending_rebalance = None

            for symbol, price in price_map.items():
                last_prices[symbol] = price
                if symbol in positions:
                    positions[symbol]["last_price"] = price

            if not price_map:
                continue

            current_universe = set(universe_history.symbols_for_date(current_date))
            current_index_weights = _weights_for_date(universe_history, weight_history, current_date)
            universe_size_by_date[current_date.isoformat()] = len(current_universe)

            if _is_weekly_rebalance_day(dates, date_index):
                rebalance_count += 1
                ranked = []
                for symbol in sorted(current_universe):
                    symbol_index = index_by_symbol_date.get(symbol, {}).get(current_date)
                    if symbol_index is None or symbol not in price_map:
                        continue
                    rows = klines_by_symbol[symbol]
                    first_kline_date = rows[0]["date"] if rows else None
                    if first_kline_date and min_listing_days > 0 and (current_date - first_kline_date).days < min_listing_days:
                        continue
                    snapshot = _compute_mixed_risk_adjusted_momentum_snapshot(rows, symbol_index, momentum_weights)
                    if not snapshot or snapshot.get("risk_adjusted_score") is None:
                        continue
                    row = row_by_symbol_date[symbol][current_date]
                    momentum_score = snapshot.get("risk_adjusted_score")
                    ranked.append({
                        "symbol": symbol,
                        "price": price_map[symbol],
                        "turnover": float(row.get("turnover") or 0),
                        "index_weight": float(current_index_weights.get(symbol) or 0),
                        "momentum_score": momentum_score,
                        "snapshot": snapshot,
                    })

                ranked = _apply_index_weight_blend(ranked, index_weight_blend)
                selected = ranked[:max_positions]
                selected_symbols = [item["symbol"] for item in selected]
                sell_rank_symbols = [item["symbol"] for item in ranked[:sell_rank_threshold]]
                rank_by_symbol = {item["symbol"]: rank for rank, item in enumerate(ranked, start=1)}

                for rank, item in enumerate(selected, start=1):
                    snapshot = item["snapshot"]
                    events.append({
                        "config_id": self.config.id,
                        "account_id": self.config.account_id,
                        "symbol": item["symbol"],
                        "date": current_date.isoformat(),
                        "direction": "RANK",
                        "signal_price": _round_or_none(item["price"], 4),
                        "turnover": _round_or_none(item.get("turnover"), 2),
                        "annualized_volatility_pct": snapshot.get("annualized_volatility_pct"),
                        "threshold_pct": snapshot.get("risk_adjusted_score"),
                        "payload": {
                            **snapshot,
                            "rank": rank,
                            "rank_score": _round_or_none(item.get("rank_score"), 6),
                            "momentum_score": item.get("momentum_score"),
                            "momentum_percentile": _round_or_none(item.get("momentum_percentile"), 6),
                            "index_weight": _round_or_none(item.get("index_weight"), 8),
                            "index_weight_pct": _round_or_none((item.get("index_weight") or 0) * 100, 4),
                            "index_weight_percentile": _round_or_none(item.get("index_weight_percentile"), 6),
                            "index_weight_blend": index_weight_blend,
                            "selected_symbols": selected_symbols,
                            "sell_rank_symbols": sell_rank_symbols,
                            "max_positions": max_positions,
                            "sell_rank_threshold": sell_rank_threshold,
                            "sell_rank_multiplier": sell_rank_multiplier,
                            "min_listing_days": min_listing_days,
                            "momentum_windows": momentum_windows,
                            "momentum_weights": momentum_weights_payload,
                            "rebalance_frequency": "weekly",
                            "execution_rule": "signal_close_next_open",
                            "rotation_rule": "hold_until_out_of_sell_rank",
                            "strategy": "risk_adjusted_mixed_momentum_top_n_rotation",
                        },
                        "price_source": DAILY_PRICE_SOURCE,
                    })

                sell_symbols = [
                    symbol
                    for symbol in list(positions.keys())
                    if rank_by_symbol.get(symbol, 10**9) > sell_rank_threshold
                ]
                pending_rebalance = {
                    "signal_date": current_date,
                    "selected": selected,
                    "selected_symbols": selected_symbols,
                    "sell_rank_symbols": sell_rank_symbols,
                    "sell_symbols": sell_symbols,
                }

            value = _portfolio_value(cash, positions, last_prices)
            peak_value = max(peak_value, value)
            drawdown = (value / peak_value - 1) * 100 if peak_value > 0 else 0.0
            equity_curve.append({
                "date": current_date.isoformat(),
                "value": _round_or_none(value, 2),
                "cash": _round_or_none(cash, 2),
                "position_value": _round_or_none(value - cash, 2),
                "drawdown": _round_or_none(drawdown, 2),
            })

        current_value = equity_curve[-1]["value"] if equity_curve else float(self.config.initial_capital or 0)
        initial_value = float(self.config.initial_capital or 0)
        total_return = (current_value / initial_value - 1) * 100 if initial_value > 0 else 0.0
        elapsed_days = (
            (date.fromisoformat(equity_curve[-1]["date"]) - date.fromisoformat(equity_curve[0]["date"])).days
            if len(equity_curve) > 1
            else 0
        )
        annualized_return = (
            ((1 + total_return / 100) ** (365 / elapsed_days) - 1) * 100
            if elapsed_days > 0 and total_return > -100
            else 0.0
        )
        win_count = sum(1 for item in closed_profits if item > 0)

        holdings = []
        for symbol, position in positions.items():
            price = float(last_prices.get(symbol) or position.get("last_price") or position.get("avg_cost") or 0)
            market_value = int(position["shares"]) * price
            holdings.append({
                "symbol": symbol,
                "shares": int(position["shares"]),
                "price": _round_or_none(price, 4),
                "avg_cost": _round_or_none(position.get("avg_cost"), 4),
                "entry_date": position["entry_date"].isoformat() if position.get("entry_date") else None,
                "market_value": _round_or_none(market_value, 2),
                "actual_weight_pct": _round_or_none(market_value / current_value * 100 if current_value > 0 else 0, 2),
            })
        holdings.sort(key=lambda item: item.get("market_value") or 0, reverse=True)

        metrics = {
            "total_return": _round_or_none(total_return, 2),
            "annualized_return": _round_or_none(annualized_return, 2),
            "max_drawdown": _round_or_none(min([item["drawdown"] for item in equity_curve] or [0]), 2),
            "signal_count": len(events),
            "rank_signal_count": len(events),
            "rebalance_count": rebalance_count,
            "buy_signal_count": sum(1 for item in trades if item["action"] == "BUY"),
            "sell_signal_count": sum(1 for item in trades if item["action"] == "SELL"),
            "trade_count": len(trades),
            "closed_trade_count": len(closed_profits),
            "win_count": win_count,
            "win_rate": _round_or_none(win_count / len(closed_profits) * 100 if closed_profits else 0.0, 2),
            "ending_value": _round_or_none(current_value, 2),
            "cash": equity_curve[-1]["cash"] if equity_curve else _round_or_none(cash, 2),
            "holding_count": len(holdings),
            "pending_signal_date": pending_rebalance["signal_date"].isoformat() if pending_rebalance else None,
        }

        return {
            "metrics": metrics,
            "equity_curve": equity_curve,
            "benchmark_curve": benchmark_curve,
            "events": events,
            "trades": trades,
            "current_holdings": holdings,
            "errors": errors,
            "meta": {
                "candidate_etfs": candidate_etfs,
                "symbols_used": list(klines_by_symbol.keys()),
                "symbol_count": len(klines_by_symbol),
                "holdings_date_count": universe_history.holdings_date_count,
                "universe_size_by_date": universe_size_by_date,
                "min_listing_days": min_listing_days,
                "max_positions": max_positions,
                "momentum_window": max_momentum_window,
                "momentum_windows": momentum_windows,
                "momentum_weights": momentum_weights_payload,
                "index_weight_blend": index_weight_blend,
                "sell_rank_threshold": sell_rank_threshold,
                "sell_rank_multiplier": sell_rank_multiplier,
                "rebalance_frequency": "weekly",
                "execution_rule": "signal_close_next_open",
                "rotation_rule": "hold_until_out_of_sell_rank",
                "strategy": "risk_adjusted_mixed_momentum_top_n_rotation",
                "signal_price_source": DAILY_PRICE_SOURCE,
                "execution_price_source": NEXT_OPEN_PRICE_SOURCE,
                "price_source": DAILY_PRICE_SOURCE,
            },
        }
