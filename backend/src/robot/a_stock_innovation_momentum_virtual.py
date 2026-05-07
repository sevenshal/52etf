import bisect
import logging
import math
from functools import lru_cache
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session as ORMSession

from ..core.database import (
    AStockInnovation100Constituent,
    AStockInnovation100Level,
    AStockInnovation100Rebalance,
)
from ..core.analytics_database import analytics_engine
from .a_stock_innovation100 import INDEX_CODE as BENCHMARK_INDEX_CODE
from .us_stock_signal_virtual import (
    DAILY_PRICE_SOURCE,
    NEXT_OPEN_PRICE_SOURCE,
    SUPPORTED_MOMENTUM_WINDOWS,
    SUPPORTED_REBALANCE_FREQUENCIES,
    _apply_index_weight_blend,
    _build_yearly_stats,
    _compute_mixed_risk_adjusted_momentum_snapshot,
    _floor_lot,
    _format_momentum_weights,
    _is_positive_number,
    _is_rebalance_day,
    _normalize_momentum_weights,
    _normalize_rebalance_frequency,
    _portfolio_value,
    _round_or_none,
)


logger = logging.getLogger(__name__)

DEFAULT_NAME = "A股创新100风险调整混合动量虚拟盘"
DEFAULT_INITIAL_CAPITAL = 1_000_000.0
DEFAULT_START_DATE = date(2020, 1, 2)
DEFAULT_MOMENTUM_WEIGHTS = {"20": 0.0, "60": 0.20, "120": 0.80}
DEFAULT_MAX_POSITIONS = 5
DEFAULT_SELL_RANK_MULTIPLIER = 2.0
DEFAULT_INDEX_WEIGHT_BLEND = 0.8
DEFAULT_REBALANCE_FREQUENCY = "weekly"
DEFAULT_COMMISSION_PCT = 0.03
DEFAULT_SLIPPAGE_PCT = 0.02
DEFAULT_LOT_SIZE = 100
DEFAULT_MIN_LISTING_DAYS = 365
DEFAULT_FUNDAMENTAL_WEIGHTS = {
    "circ_mv": 0.34,
    "revenue_growth_3y": 0.33,
    "rd_exp_ratio": 0.33,
}
DEFAULT_FUNDAMENTAL_BLEND = 0.0
BENCHMARK_SYMBOL = BENCHMARK_INDEX_CODE
FUNDAMENTAL_WEIGHT_KEYS = ["circ_mv", "revenue_growth_3y", "rd_exp_ratio"]
FUNDAMENTAL_HISTORY_LOOKBACK_DAYS = 365 * 6


@dataclass
class AStockInnovationUniverseHistory:
    snapshot_dates: List[date]
    symbols_by_date: Dict[date, List[str]]
    weights_by_date: Dict[date, Dict[str, float]]
    all_symbols: List[str]

    def symbols_for_date(self, current_date: date) -> List[str]:
        if not self.snapshot_dates:
            return []
        index = bisect.bisect_right(self.snapshot_dates, current_date) - 1
        if index < 0:
            return []
        return self.symbols_by_date.get(self.snapshot_dates[index], [])

    def weights_for_date(self, current_date: date) -> Dict[str, float]:
        if not self.snapshot_dates:
            return {}
        index = bisect.bisect_right(self.snapshot_dates, current_date) - 1
        if index < 0:
            return {}
        return self.weights_by_date.get(self.snapshot_dates[index], {})


def _to_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _is_finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _normalize_fundamental_weights(raw_weights) -> Dict[str, float]:
    raw_weights = raw_weights if isinstance(raw_weights, dict) else DEFAULT_FUNDAMENTAL_WEIGHTS
    weights: Dict[str, float] = {}
    for key in FUNDAMENTAL_WEIGHT_KEYS:
        raw_value = raw_weights.get(key, raw_weights.get(str(key), 0.0))
        try:
            weight = float(raw_value or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        weights[key] = max(0.0, weight)

    total = sum(weights.values())
    if total <= 0:
        return {key: 1.0 / len(FUNDAMENTAL_WEIGHT_KEYS) for key in FUNDAMENTAL_WEIGHT_KEYS}
    return {
        key: value / total
        for key, value in weights.items()
        if value > 0
    }


def _format_fundamental_weights(weights: Dict[str, float]) -> Dict[str, float]:
    return {
        key: _round_or_none(weights.get(key, 0.0), 6) or 0.0
        for key in FUNDAMENTAL_WEIGHT_KEYS
    }


def _assign_percentiles(items: List[Dict], value_key: str, percentile_key: str, default: float = 0.5):
    valid_items = [
        item for item in items
        if _is_finite_number(item.get(value_key))
    ]
    if not valid_items:
        for item in items:
            item[percentile_key] = default
        return

    ranked = sorted(
        valid_items,
        key=lambda item: (
            float(item.get(value_key) or 0.0),
            float(item.get("turnover") or 0.0),
            item["symbol"],
        ),
        reverse=True,
    )
    denominator = max(1, len(ranked) - 1)
    scores = {
        item["symbol"]: 1.0 - (index / denominator)
        for index, item in enumerate(ranked)
    }
    for item in items:
        item[percentile_key] = scores.get(item["symbol"], default)


@lru_cache(maxsize=1024)
def _load_symbol_income_history(ts_code: str, fetch_start_iso: str, end_date_iso: str) -> Tuple[Tuple]:
    fetch_start = date.fromisoformat(fetch_start_iso)
    end_date = date.fromisoformat(end_date_iso)
    with analytics_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT end_date, ann_date, operate_income, rd_exp
                FROM a_stock_income
                WHERE ts_code = :ts_code
                  AND ann_date IS NOT NULL
                  AND ann_date >= :fetch_start
                  AND ann_date <= :end_date
                  AND EXTRACT(MONTH FROM end_date) = 12
                  AND EXTRACT(DAY FROM end_date) = 31
                ORDER BY end_date ASC, ann_date ASC
            """),
            {
                "ts_code": ts_code,
                "fetch_start": fetch_start.isoformat(),
                "end_date": end_date.isoformat(),
            },
        ).fetchall()
    if not rows:
        return tuple()

    records_by_end_date = {}
    for row in rows:
        end_row_date = _to_date(row[0])
        ann_row_date = _to_date(row[1])
        operate_income = row[2]
        rd_exp = row[3]
        if not _is_finite_number(operate_income) or float(operate_income) <= 0:
            continue
        if not end_row_date or not ann_row_date:
            continue
        records_by_end_date[end_row_date] = {
            "end_date": end_row_date.isoformat() if isinstance(end_row_date, date) else None,
            "ann_date": ann_row_date.isoformat() if isinstance(ann_row_date, date) else None,
            "operate_income": float(operate_income),
            "rd_exp": float(rd_exp) if _is_finite_number(rd_exp) and float(rd_exp) >= 0 else None,
        }
    records = [records_by_end_date[key] for key in sorted(records_by_end_date)]
    return tuple(tuple(sorted(record.items())) for record in records)


def _parse_income_history(history: Tuple[Tuple]) -> List[Dict]:
    return [dict(record) for record in history or []]


def _build_fundamental_snapshot(history: List[Dict], current_date: date) -> Optional[Dict]:
    if not history:
        return None

    current_rows = [
        row for row in history
        if row.get("ann_date") and date.fromisoformat(row["ann_date"]) <= current_date
    ]
    if not current_rows:
        return None

    latest = max(
        current_rows,
        key=lambda item: (
            item.get("end_date") or "",
            item.get("ann_date") or "",
        ),
    )
    latest_end_date = date.fromisoformat(latest["end_date"])
    latest_income = float(latest.get("operate_income") or 0.0)
    latest_rd_exp = latest.get("rd_exp")
    rd_exp_ratio_pct = (
        float(latest_rd_exp) / latest_income * 100.0
        if _is_finite_number(latest_rd_exp) and latest_income > 0
        else None
    )

    base_candidates = [
        row for row in current_rows
        if row.get("end_date") and date.fromisoformat(row["end_date"]).year <= latest_end_date.year - 3
    ]
    revenue_growth_3y_pct = None
    if base_candidates:
        base = max(
            base_candidates,
            key=lambda item: (
                item.get("end_date") or "",
                item.get("ann_date") or "",
            ),
        )
        base_end_date = date.fromisoformat(base["end_date"])
        base_income = float(base.get("operate_income") or 0.0)
        years = latest_end_date.year - base_end_date.year
        if base_income > 0 and years >= 3 and latest_income > 0:
            revenue_growth_3y_pct = (latest_income / base_income) ** (1.0 / years) - 1.0
            revenue_growth_3y_pct *= 100.0

    return {
        "report_end_date": latest.get("end_date"),
        "report_ann_date": latest.get("ann_date"),
        "revenue_growth_3y_pct": _round_or_none(revenue_growth_3y_pct, 4),
        "rd_exp_ratio_pct": _round_or_none(rd_exp_ratio_pct, 4),
        "operate_income": _round_or_none(latest_income, 4),
        "rd_exp": _round_or_none(latest_rd_exp, 4),
    }


def _apply_fundamental_blend(
    ranked: List[Dict],
    fundamental_weights: Dict[str, float],
    fundamental_blend: float,
) -> List[Dict]:
    blend = max(0.0, min(1.0, float(fundamental_blend or 0.0)))
    if not ranked:
        return ranked

    for key in FUNDAMENTAL_WEIGHT_KEYS:
        _assign_percentiles(ranked, key, f"{key}_percentile")

    normalized_weights = _normalize_fundamental_weights(fundamental_weights)
    total_weight = sum(normalized_weights.values()) or 1.0

    for item in ranked:
        item["base_rank_score"] = item.get("rank_score")
        fundamental_score = 0.0
        for key, weight in normalized_weights.items():
            percentile = float(item.get(f"{key}_percentile") or 0.5)
            fundamental_score += percentile * weight
        fundamental_score /= total_weight
        item["fundamental_score"] = fundamental_score
        if blend <= 0:
            continue
        item["rank_score"] = (
            (1 - blend) * float(item.get("base_rank_score") or -1e18)
            + blend * fundamental_score
        )

    return sorted(
        ranked,
        key=lambda item: (
            float(item.get("rank_score") or -1e18),
            float(item.get("base_rank_score") or -1e18),
            float(item.get("fundamental_score") or -1e18),
            float(item.get("momentum_score") or -1e18),
            float(item.get("index_weight") or 0.0),
            float(item.get("turnover") or 0.0),
            item["symbol"],
        ),
        reverse=True,
    )


def load_a_stock_innovation_universe_history(
    db: ORMSession,
    start_date: date,
    end_date: date,
) -> AStockInnovationUniverseHistory:
    all_rebalance_dates = [
        row[0]
        for row in (
            db.query(AStockInnovation100Rebalance.effective_date)
            .filter(
                AStockInnovation100Rebalance.index_code == BENCHMARK_INDEX_CODE,
                AStockInnovation100Rebalance.effective_date <= end_date,
            )
            .order_by(AStockInnovation100Rebalance.effective_date.asc())
            .all()
        )
        if row[0]
    ]
    pre_start_dates = [item for item in all_rebalance_dates if item < start_date]
    selected_dates = {item for item in all_rebalance_dates if start_date <= item <= end_date}
    if pre_start_dates:
        selected_dates.add(pre_start_dates[-1])
    selected_dates = sorted(selected_dates)

    if not selected_dates:
        return AStockInnovationUniverseHistory([], {}, {}, [])

    rebalances = (
        db.query(AStockInnovation100Rebalance)
        .filter(
            AStockInnovation100Rebalance.index_code == BENCHMARK_INDEX_CODE,
            AStockInnovation100Rebalance.effective_date.in_(selected_dates),
        )
        .order_by(AStockInnovation100Rebalance.effective_date.asc())
        .all()
    )
    rebalance_id_to_effective = {
        item.id: item.effective_date
        for item in rebalances
        if item.effective_date
    }
    rows = (
        db.query(AStockInnovation100Constituent)
        .filter(
            AStockInnovation100Constituent.index_code == BENCHMARK_INDEX_CODE,
            AStockInnovation100Constituent.rebalance_id.in_(rebalance_id_to_effective.keys()),
        )
        .order_by(AStockInnovation100Constituent.rebalance_id.asc(), AStockInnovation100Constituent.weight_pct.desc())
        .all()
    )

    symbols_by_date: Dict[date, List[str]] = {}
    weights_by_date: Dict[date, Dict[str, float]] = {}
    all_symbols: Set[str] = set()
    for row in rows:
        effective_date = rebalance_id_to_effective.get(row.rebalance_id)
        if not effective_date:
            continue
        symbols_by_date.setdefault(effective_date, [])
        weights_by_date.setdefault(effective_date, {})
        if row.ts_code not in symbols_by_date[effective_date]:
            symbols_by_date[effective_date].append(row.ts_code)
        weights_by_date[effective_date][row.ts_code] = float(row.weight_pct or 0.0) / 100.0
        all_symbols.add(row.ts_code)

    return AStockInnovationUniverseHistory(
        snapshot_dates=selected_dates,
        symbols_by_date=symbols_by_date,
        weights_by_date=weights_by_date,
        all_symbols=sorted(all_symbols),
    )


def _load_price_rows(symbols: List[str], start_date: date, end_date: date) -> Dict[str, List[Dict]]:
    rows_by_symbol: Dict[str, List[Dict]] = {}
    if not symbols:
        return rows_by_symbol

    with analytics_engine.connect() as conn:
        for offset in range(0, len(symbols), 500):
            chunk = symbols[offset:offset + 500]
            placeholders = ",".join([f":symbol_{index}" for index in range(len(chunk))])
            params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
            params.update({"start_date": start_date.isoformat(), "end_date": end_date.isoformat()})
            price_rows = conn.execute(
                text(f"""
                    SELECT ts_code, trade_date, open, high, low, close, amount, circ_mv
                    FROM a_stock_market_daily
                    WHERE trade_date >= :start_date
                      AND trade_date <= :end_date
                      AND ts_code IN ({placeholders})
                    ORDER BY ts_code, trade_date
                """),
                params,
            ).fetchall()
            for row in price_rows:
                symbol = row[0]
                row_date = _to_date(row[1])
                if not symbol or not row_date:
                    continue
                if not all(_is_positive_number(value) for value in (row[2], row[3], row[4], row[5])):
                    continue
                rows_by_symbol.setdefault(symbol, []).append(
                    {
                        "date": row_date,
                        "open": float(row[2]),
                        "high": float(row[3]),
                        "low": float(row[4]),
                        "close": float(row[5]),
                        "volume": 0.0,
                        "turnover": float(row[6] or 0.0),
                        "circ_mv": float(row[7]) if row[7] is not None else None,
                    }
                )
    return rows_by_symbol


def _load_basic_map(symbols: List[str]) -> Dict[str, Dict]:
    basic_map: Dict[str, Dict] = {}
    if not symbols:
        return basic_map

    with analytics_engine.connect() as conn:
        for offset in range(0, len(symbols), 500):
            chunk = symbols[offset:offset + 500]
            placeholders = ",".join([f":symbol_{index}" for index in range(len(chunk))])
            params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
            rows = conn.execute(
                text(f"""
                    SELECT ts_code, name, industry, list_date
                    FROM a_stock_basic
                    WHERE ts_code IN ({placeholders})
                """),
                params,
            ).fetchall()
            for row in rows:
                basic_map[row[0]] = {
                    "name": row[1],
                    "industry": row[2],
                    "list_date": _to_date(row[3]),
                }

    return basic_map


def _build_benchmark_curve(level_rows: List[AStockInnovation100Level], initial_capital: float) -> List[Dict]:
    first_level = next((float(row.level) for row in level_rows if _is_positive_number(row.level)), None)
    curve = []
    for row in level_rows:
        value = (
            float(initial_capital or 0.0) * float(row.level) / first_level
            if first_level and _is_positive_number(row.level)
            else None
        )
        curve.append({
            "date": row.date.isoformat(),
            "values": {BENCHMARK_SYMBOL: _round_or_none(value, 2)},
        })
    return curve


class AStockInnovationMomentumVirtualEngine:
    def __init__(
        self,
        db: ORMSession,
        config,
        end_date: Optional[date] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ):
        self.db = db
        self.config = config
        self.end_date = end_date or date.today()
        self.progress_callback = progress_callback

    def report(self, progress: int, message: str):
        if self.progress_callback:
            self.progress_callback(int(max(0, min(100, progress))), message)

    def run(self) -> Dict:
        start_date = self.config.start_date
        end_date = self.end_date
        momentum_weights = _normalize_momentum_weights(getattr(self.config, "momentum_weights", None))
        momentum_weights_payload = _format_momentum_weights(momentum_weights)
        momentum_windows = sorted(momentum_weights)
        max_momentum_window = max(momentum_windows)
        momentum_label = "+".join(str(window) for window in momentum_windows)
        index_weight_blend = max(0.0, min(1.0, float(getattr(self.config, "index_weight_blend", DEFAULT_INDEX_WEIGHT_BLEND) or 0.0)))
        fundamental_weights = _normalize_fundamental_weights(getattr(self.config, "fundamental_weights", None))
        fundamental_weights_payload = _format_fundamental_weights(fundamental_weights)
        fundamental_blend = max(0.0, min(1.0, float(getattr(self.config, "fundamental_blend", DEFAULT_FUNDAMENTAL_BLEND) or 0.0)))
        min_listing_days = max(0, int(getattr(self.config, "min_listing_days", DEFAULT_MIN_LISTING_DAYS) or 0))
        max_positions = max(1, int(self.config.max_positions or 1))
        sell_rank_multiplier = max(1.0, float(getattr(self.config, "sell_rank_multiplier", DEFAULT_SELL_RANK_MULTIPLIER) or 1.0))
        sell_rank_threshold = max(max_positions, int(round(max_positions * sell_rank_multiplier)))
        rebalance_frequency = _normalize_rebalance_frequency(getattr(self.config, "rebalance_frequency", DEFAULT_REBALANCE_FREQUENCY))
        lot_size = max(1, int(getattr(self.config, "lot_size", DEFAULT_LOT_SIZE) or DEFAULT_LOT_SIZE))
        commission_rate = max(0.0, float(self.config.commission_pct or 0.0)) / 100
        slippage_rate = max(0.0, float(self.config.slippage_pct or 0.0)) / 100

        self.report(1, "读取A股创新100历史成分")
        universe_history = load_a_stock_innovation_universe_history(self.db, start_date, end_date)
        if not universe_history.all_symbols:
            raise ValueError("没有找到A股创新100历史成分，请先回跑A股创新100指数")

        level_rows = (
            self.db.query(AStockInnovation100Level)
            .filter(
                AStockInnovation100Level.index_code == BENCHMARK_INDEX_CODE,
                AStockInnovation100Level.date >= start_date,
                AStockInnovation100Level.date <= end_date,
            )
            .order_by(AStockInnovation100Level.date.asc())
            .all()
        )
        if not level_rows:
            raise ValueError("没有找到A股创新100指数点位，请先回跑A股创新100指数")
        dates = [row.date for row in level_rows]
        benchmark_curve = _build_benchmark_curve(level_rows, float(self.config.initial_capital or 0.0))

        fetch_padding_days = max(370, min_listing_days + 30, int(max_momentum_window * 3))
        fetch_start = start_date - timedelta(days=fetch_padding_days)
        self.report(5, f"载入 {len(universe_history.all_symbols)} 个创新100历史成分行情")
        klines_by_symbol = _load_price_rows(universe_history.all_symbols, fetch_start, end_date)
        if not klines_by_symbol:
            raise ValueError("没有可用的A股创新100成分行情")

        uses_income_factors = (
            fundamental_blend > 0
            and (
                fundamental_weights.get("revenue_growth_3y", 0.0) > 0
                or fundamental_weights.get("rd_exp_ratio", 0.0) > 0
            )
        )
        fundamental_history = {}
        if uses_income_factors:
            self.report(7, "载入创新100历史成分财务因子")
            fundamental_fetch_start = start_date - timedelta(days=FUNDAMENTAL_HISTORY_LOOKBACK_DAYS)
            fundamental_history = {
                symbol: _parse_income_history(
                    _load_symbol_income_history(symbol, fundamental_fetch_start.isoformat(), end_date.isoformat())
                )
                for symbol in universe_history.all_symbols
            }

        basic_map = _load_basic_map(universe_history.all_symbols)
        index_by_symbol_date = {
            symbol: {row["date"]: index for index, row in enumerate(rows)}
            for symbol, rows in klines_by_symbol.items()
        }
        row_by_symbol_date = {
            symbol: {row["date"]: row for row in rows}
            for symbol, rows in klines_by_symbol.items()
        }

        cash = float(self.config.initial_capital or 0.0)
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
            basic = basic_map.get(symbol) or {}
            trades.append({
                "date": trade_date.isoformat(),
                "signal_date": signal_date.isoformat(),
                "action": action,
                "symbol": symbol,
                "name": basic.get("name"),
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
            old_cost_basis = float(position.get("cost_basis") or 0.0)
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
                position["cost_basis"] = float(position.get("cost_basis") or 0.0) + amount + commission
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
            )

        for date_index, current_date in enumerate(dates):
            if date_index % max(1, len(dates) // 100) == 0:
                self.report(8 + int(87 * date_index / max(1, len(dates))), f"模拟交易日 {date_index + 1}/{len(dates)}")

            current_universe = universe_history.symbols_for_date(current_date)
            current_index_weights = universe_history.weights_for_date(current_date)
            universe_size_by_date[current_date.isoformat()] = len(current_universe)
            open_map = {}
            price_map = {}
            pricing_symbols = list(dict.fromkeys([*current_universe, *positions.keys()]))
            for symbol in pricing_symbols:
                row = row_by_symbol_date.get(symbol, {}).get(current_date)
                if not row:
                    continue
                if _is_positive_number(row.get("open")):
                    open_map[symbol] = float(row["open"])
                if _is_positive_number(row.get("close")):
                    price_map[symbol] = float(row["close"])

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
                        f"下一交易日开盘补位买入A股创新100风险调整{momentum_label}日混合动量Top{max_positions}",
                    )
                pending_rebalance = None

            for symbol, price in price_map.items():
                last_prices[symbol] = price
                if symbol in positions:
                    positions[symbol]["last_price"] = price

            if _is_rebalance_day(dates, date_index, rebalance_frequency):
                rebalance_count += 1
                ranked = []
                for symbol in sorted(current_universe):
                    symbol_index = index_by_symbol_date.get(symbol, {}).get(current_date)
                    if symbol_index is None or symbol not in price_map:
                        continue
                    rows = klines_by_symbol[symbol]
                    basic = basic_map.get(symbol) or {}
                    list_date = basic.get("list_date") or (rows[0]["date"] if rows else None)
                    if list_date and min_listing_days > 0 and (current_date - list_date).days < min_listing_days:
                        continue
                    snapshot = _compute_mixed_risk_adjusted_momentum_snapshot(rows, symbol_index, momentum_weights)
                    if not snapshot or snapshot.get("risk_adjusted_score") is None:
                        continue
                    row = row_by_symbol_date[symbol][current_date]
                    momentum_score = snapshot.get("risk_adjusted_score")
                    fundamental_snapshot = _build_fundamental_snapshot(fundamental_history.get(symbol) or [], current_date)
                    ranked.append({
                        "symbol": symbol,
                        "name": basic.get("name"),
                        "industry": basic.get("industry"),
                        "price": price_map[symbol],
                        "turnover": float(row.get("turnover") or 0.0),
                        "circ_mv": float(row.get("circ_mv") or 0.0) if _is_finite_number(row.get("circ_mv")) else None,
                        "index_weight": float(current_index_weights.get(symbol) or 0.0),
                        "momentum_score": momentum_score,
                        "snapshot": snapshot,
                        "fundamental_snapshot": fundamental_snapshot,
                        "revenue_growth_3y_pct": fundamental_snapshot.get("revenue_growth_3y_pct") if fundamental_snapshot else None,
                        "rd_exp_ratio_pct": fundamental_snapshot.get("rd_exp_ratio_pct") if fundamental_snapshot else None,
                        "report_end_date": fundamental_snapshot.get("report_end_date") if fundamental_snapshot else None,
                        "report_ann_date": fundamental_snapshot.get("report_ann_date") if fundamental_snapshot else None,
                    })

                ranked = _apply_index_weight_blend(ranked, index_weight_blend)
                ranked = _apply_fundamental_blend(ranked, fundamental_weights, fundamental_blend)
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
                            "name": item.get("name"),
                            "industry": item.get("industry"),
                            "rank_score": _round_or_none(item.get("rank_score"), 6),
                            "base_rank_score": _round_or_none(item.get("base_rank_score"), 6),
                            "momentum_score": item.get("momentum_score"),
                            "momentum_percentile": _round_or_none(item.get("momentum_percentile"), 6),
                            "index_weight": _round_or_none(item.get("index_weight"), 8),
                            "index_weight_pct": _round_or_none((item.get("index_weight") or 0.0) * 100, 4),
                            "index_weight_percentile": _round_or_none(item.get("index_weight_percentile"), 6),
                            "index_weight_blend": index_weight_blend,
                            "fundamental_blend": fundamental_blend,
                            "fundamental_score": _round_or_none(item.get("fundamental_score"), 6),
                            "fundamental_weights": fundamental_weights_payload,
                            "circ_mv": _round_or_none(item.get("circ_mv"), 4),
                            "circ_mv_percentile": _round_or_none(item.get("circ_mv_percentile"), 6),
                            "revenue_growth_3y_pct": _round_or_none(item.get("revenue_growth_3y_pct"), 4),
                            "revenue_growth_3y_percentile": _round_or_none(item.get("revenue_growth_3y_percentile"), 6),
                            "rd_exp_ratio_pct": _round_or_none(item.get("rd_exp_ratio_pct"), 4),
                            "rd_exp_ratio_percentile": _round_or_none(item.get("rd_exp_ratio_percentile"), 6),
                            "report_end_date": item.get("report_end_date"),
                            "report_ann_date": item.get("report_ann_date"),
                            "selected_symbols": selected_symbols,
                            "sell_rank_symbols": sell_rank_symbols,
                            "max_positions": max_positions,
                            "sell_rank_threshold": sell_rank_threshold,
                            "sell_rank_multiplier": sell_rank_multiplier,
                            "min_listing_days": min_listing_days,
                            "momentum_windows": momentum_windows,
                            "momentum_weights": momentum_weights_payload,
                            "rebalance_frequency": rebalance_frequency,
                            "execution_rule": "signal_close_next_open",
                            "rotation_rule": "hold_until_out_of_sell_rank",
                            "strategy": "a_stock_innovation100_risk_adjusted_mixed_momentum",
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
            drawdown = (value / peak_value - 1.0) * 100 if peak_value > 0 else 0.0
            equity_curve.append({
                "date": current_date.isoformat(),
                "value": _round_or_none(value, 2),
                "cash": _round_or_none(cash, 2),
                "position_value": _round_or_none(value - cash, 2),
                "drawdown": _round_or_none(drawdown, 2),
            })

        current_value = equity_curve[-1]["value"] if equity_curve else float(self.config.initial_capital or 0.0)
        initial_value = float(self.config.initial_capital or 0.0)
        total_return = (current_value / initial_value - 1.0) * 100 if initial_value > 0 else 0.0
        yearly_stats = _build_yearly_stats(equity_curve, benchmark_curve, [BENCHMARK_SYMBOL])
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
            price = float(last_prices.get(symbol) or position.get("last_price") or position.get("avg_cost") or 0.0)
            market_value = int(position["shares"]) * price
            basic = basic_map.get(symbol) or {}
            holdings.append({
                "symbol": symbol,
                "name": basic.get("name"),
                "shares": int(position["shares"]),
                "price": _round_or_none(price, 4),
                "avg_cost": _round_or_none(position.get("avg_cost"), 4),
                "entry_date": position["entry_date"].isoformat() if position.get("entry_date") else None,
                "market_value": _round_or_none(market_value, 2),
                "actual_weight_pct": _round_or_none(market_value / current_value * 100 if current_value > 0 else 0, 2),
            })
        holdings.sort(key=lambda item: item.get("market_value") or 0, reverse=True)

        benchmark_values = [
            item.get("values", {}).get(BENCHMARK_SYMBOL)
            for item in benchmark_curve
            if item.get("values", {}).get(BENCHMARK_SYMBOL) is not None
        ]
        benchmark_return = (
            (benchmark_values[-1] / benchmark_values[0] - 1.0) * 100
            if len(benchmark_values) > 1 and benchmark_values[0] > 0
            else None
        )
        metrics = {
            "total_return": _round_or_none(total_return, 2),
            "benchmark_total_return": _round_or_none(benchmark_return, 2),
            "excess_return": _round_or_none(total_return - benchmark_return if benchmark_return is not None else None, 2),
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
            "yearly_stats": yearly_stats,
            "events": events,
            "trades": trades,
            "current_holdings": holdings,
            "errors": [],
            "meta": {
                "benchmark_symbol": BENCHMARK_SYMBOL,
                "symbols_used": list(klines_by_symbol.keys()),
                "symbol_count": len(klines_by_symbol),
                "universe_snapshot_count": len(universe_history.snapshot_dates),
                "universe_size_by_date": universe_size_by_date,
                "min_listing_days": min_listing_days,
                "max_positions": max_positions,
                "momentum_window": max_momentum_window,
                "momentum_windows": momentum_windows,
                "momentum_weights": momentum_weights_payload,
                "index_weight_blend": index_weight_blend,
                "fundamental_weights": fundamental_weights_payload,
                "fundamental_blend": fundamental_blend,
                "fundamental_income_loaded": uses_income_factors,
                "sell_rank_threshold": sell_rank_threshold,
                "sell_rank_multiplier": sell_rank_multiplier,
                "rebalance_frequency": rebalance_frequency,
                "execution_rule": "signal_close_next_open",
                "rotation_rule": "hold_until_out_of_sell_rank",
                "strategy": "a_stock_innovation100_risk_adjusted_mixed_momentum",
                "signal_price_source": DAILY_PRICE_SOURCE,
                "execution_price_source": NEXT_OPEN_PRICE_SOURCE,
                "price_source": DAILY_PRICE_SOURCE,
            },
        }
