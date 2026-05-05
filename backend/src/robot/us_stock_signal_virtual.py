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
DEFAULT_CANDIDATE_ETFS = ["SPY.US", "QQQ.US"]
CANDIDATE_ETF_OPTIONS = [
    {"label": "标普500", "value": "SPY.US", "description": "SPDR S&P 500 ETF Trust 成分股"},
    {"label": "纳指100", "value": "QQQ.US", "description": "Invesco QQQ Trust 成分股"},
]
VOLUME_WINDOW = 60
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


def _annualized_volatility_pct(rows: List[Dict]) -> float:
    closes = [float(item["close"]) for item in rows if _is_positive_number(item.get("close"))]
    if len(closes) < 2:
        return 0.0
    returns = []
    for index in range(1, len(closes)):
        prev_close = closes[index - 1]
        close = closes[index]
        if prev_close <= 0 or close <= 0:
            continue
        returns.append(math.log(close / prev_close))
    if len(returns) < 2:
        return 0.0
    return float(np.std(returns, ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100)


def _dynamic_threshold_pct(rows: List[Dict], floor_pct: float, cap_pct: float) -> Dict:
    lower = max(0.0, float(floor_pct or 0))
    upper = max(lower, float(cap_pct or lower))
    annualized_volatility_pct = _annualized_volatility_pct(rows)
    threshold_pct = min(upper, max(lower, annualized_volatility_pct))
    return {
        "annualized_volatility_pct": annualized_volatility_pct,
        "threshold_pct": threshold_pct,
    }


def _prepare_klines(raw_klines: List[Dict], volume_std_multiplier: float) -> List[Dict]:
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
            "is_volume_spike": False,
            "volume_ma": None,
            "volume_std": None,
        })

    normalized.sort(key=lambda item: item["date"])
    multiplier = max(0.0, float(volume_std_multiplier or 0))
    for index, item in enumerate(normalized):
        if index < VOLUME_WINDOW - 1:
            continue
        volumes = [row["volume"] for row in normalized[index - VOLUME_WINDOW + 1:index + 1]]
        mean = sum(volumes) / VOLUME_WINDOW
        variance = sum((volume - mean) ** 2 for volume in volumes) / VOLUME_WINDOW
        std = math.sqrt(variance)
        item["volume_ma"] = mean
        item["volume_std"] = std
        item["is_volume_spike"] = item["volume"] > mean + std * multiplier
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


def _find_highest(rows: List[Dict], start_index: int, end_index: int) -> Dict:
    best = {"price": 0.0, "index": -1}
    for index in range(start_index, end_index + 1):
        price = float(rows[index]["high"])
        if price > best["price"]:
            best = {"price": price, "index": index}
    return best


def _find_lowest(rows: List[Dict], start_index: int, end_index: int) -> Dict:
    best = {"price": math.inf, "index": -1}
    for index in range(start_index, end_index + 1):
        price = float(rows[index]["low"])
        if price < best["price"]:
            best = {"price": price, "index": index}
    return best


def detect_signal_for_day(
    rows: List[Dict],
    index: int,
    direction: str,
    window: int,
    stabilization_period: int,
    volatility_floor_pct: float,
    volatility_cap_pct: float,
) -> Optional[Dict]:
    if index < 0 or index >= len(rows):
        return None
    current = rows[index]
    if not current.get("is_volume_spike"):
        return None

    lookback = max(2, int(window or 2))
    stable_bars = max(1, int(stabilization_period or 1))
    window_start = max(0, index - lookback + 1)
    if index - window_start + 1 < stable_bars + 2:
        return None

    window_rows = rows[window_start:index + 1]
    threshold_info = _dynamic_threshold_pct(window_rows, volatility_floor_pct, volatility_cap_pct)
    threshold_pct = threshold_info["threshold_pct"]

    if direction == "BUY":
        highest = _find_highest(rows, window_start, index)
        if highest["index"] < 0 or highest["index"] >= index or highest["price"] <= 0:
            return None
        lowest_after_high = _find_lowest(rows, highest["index"] + 1, index)
        if lowest_after_high["index"] < 0 or index - lowest_after_high["index"] < stable_bars:
            return None
        move_pct = (highest["price"] - lowest_after_high["price"]) / highest["price"] * 100
        if move_pct < threshold_pct:
            return None
        pivot = highest
        extreme = lowest_after_high
        extreme_label = "low"
        signal_price = current["close"]
    elif direction == "SELL":
        lowest = _find_lowest(rows, window_start, index)
        if lowest["index"] < 0 or lowest["index"] >= index or lowest["price"] <= 0:
            return None
        highest_after_low = _find_highest(rows, lowest["index"] + 1, index)
        if highest_after_low["index"] < 0 or index - highest_after_low["index"] < stable_bars:
            return None
        move_pct = (highest_after_low["price"] - lowest["price"]) / lowest["price"] * 100
        if move_pct < threshold_pct:
            return None
        pivot = lowest
        extreme = highest_after_low
        extreme_label = "high"
        signal_price = current["close"]
    else:
        return None

    return {
        "date": current["date"].isoformat(),
        "direction": direction,
        "signal_price": signal_price,
        "turnover": current.get("turnover") or 0,
        "volume": current.get("volume") or 0,
        "volume_ma": _round_or_none(current.get("volume_ma"), 2),
        "volume_std": _round_or_none(current.get("volume_std"), 2),
        "is_volume_spike": True,
        "move_pct": move_pct,
        "annualized_volatility_pct": threshold_info["annualized_volatility_pct"],
        "threshold_pct": threshold_pct,
        "window_start_date": rows[window_start]["date"].isoformat(),
        "pivot_date": rows[pivot["index"]]["date"].isoformat(),
        "pivot_price": pivot["price"],
        f"{extreme_label}_date": rows[extreme["index"]]["date"].isoformat(),
        f"{extreme_label}_price": extreme["price"],
        "bars_since_extreme": index - extreme["index"],
    }


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
        multiplier = float(self.config.volume_std_multiplier or 0)
        for index, symbol in enumerate(symbols, start=1):
            self.report(5 + int(35 * (index - 1) / max(1, total)), f"获取K线 {index}/{total}: {symbol}")
            try:
                raw_klines = self.quote_service.get_klines(symbol, start_date=fetch_start, end_date=end_date, period="d")
                rows = _prepare_klines(raw_klines, multiplier)
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
        window = max(2, int(self.config.window or 2))
        stabilization_period = max(1, int(self.config.stabilization_period or 1))
        min_listing_days = max(0, int(getattr(self.config, "min_listing_days", 365) or 0))
        max_positions = max(1, int(self.config.max_positions or 1))
        lot_size = max(1, int(self.config.lot_size or 1))
        commission_rate = max(0.0, float(self.config.commission_pct or 0)) / 100
        slippage_rate = max(0.0, float(self.config.slippage_pct or 0)) / 100
        candidate_etfs = self.config.candidate_etfs or DEFAULT_CANDIDATE_ETFS

        self.report(1, "读取历史成分股")
        universe_history = load_universe_history(self.db, candidate_etfs, start_date, end_date)
        if not universe_history.all_symbols:
            raise ValueError("没有找到候选ETF的历史成分股，请先同步 ETF 持仓历史")

        fetch_padding_days = max(
            370,
            min_listing_days + 30,
            int((window + stabilization_period + VOLUME_WINDOW) * 2.2),
        )
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

        def build_event(symbol: str, current_date: date, signal: Dict) -> Dict:
            row = row_by_symbol_date[symbol][current_date]
            payload = {
                **signal,
                "window": window,
                "stabilization_period": stabilization_period,
                "volatility_floor_pct": float(self.config.volatility_floor_pct or 0),
                "volatility_cap_pct": float(self.config.volatility_cap_pct or 0),
                "min_listing_days": min_listing_days,
                "volume_std_multiplier": float(self.config.volume_std_multiplier or 0),
            }
            return {
                "config_id": self.config.id,
                "account_id": self.config.account_id,
                "symbol": symbol,
                "date": current_date.isoformat(),
                "direction": signal["direction"],
                "signal_price": _round_or_none(signal.get("signal_price"), 4),
                "turnover": _round_or_none(row.get("turnover"), 2),
                "annualized_volatility_pct": _round_or_none(signal.get("annualized_volatility_pct"), 4),
                "threshold_pct": _round_or_none(signal.get("threshold_pct"), 4),
                "payload": payload,
                "price_source": DAILY_PRICE_SOURCE,
            }

        def append_trade(
            current_date: date,
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
                "date": current_date.isoformat(),
                "signal_date": current_date.isoformat(),
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
                "price_source": DAILY_PRICE_SOURCE,
            })

        for date_index, current_date in enumerate(dates):
            if date_index % max(1, len(dates) // 100) == 0:
                self.report(42 + int(53 * date_index / max(1, len(dates))), f"模拟交易日 {date_index + 1}/{len(dates)}")

            price_map = {}
            for symbol, rows in row_by_symbol_date.items():
                row = rows.get(current_date)
                if not row:
                    continue
                price = float(row.get("close") or 0)
                if price <= 0:
                    continue
                price_map[symbol] = price
                last_prices[symbol] = price
                if symbol in positions:
                    positions[symbol]["last_price"] = price

            if not price_map:
                continue

            current_universe = set(universe_history.symbols_for_date(current_date))
            universe_size_by_date[current_date.isoformat()] = len(current_universe)
            symbols_to_scan = sorted(current_universe | set(positions.keys()))
            signal_events = []
            for symbol in symbols_to_scan:
                symbol_index = index_by_symbol_date.get(symbol, {}).get(current_date)
                if symbol_index is None:
                    continue

                rows = klines_by_symbol[symbol]
                first_kline_date = rows[0]["date"] if rows else None
                if first_kline_date and min_listing_days > 0 and (current_date - first_kline_date).days < min_listing_days:
                    continue
                if symbol in positions:
                    sell_signal = detect_signal_for_day(
                        rows,
                        symbol_index,
                        "SELL",
                        window,
                        stabilization_period,
                        self.config.volatility_floor_pct,
                        self.config.volatility_cap_pct,
                    )
                    if sell_signal:
                        event = build_event(symbol, current_date, sell_signal)
                        signal_events.append(event)
                        events.append(event)

                if symbol in current_universe:
                    buy_signal = detect_signal_for_day(
                        rows,
                        symbol_index,
                        "BUY",
                        window,
                        stabilization_period,
                        self.config.volatility_floor_pct,
                        self.config.volatility_cap_pct,
                    )
                    if buy_signal:
                        event = build_event(symbol, current_date, buy_signal)
                        signal_events.append(event)
                        events.append(event)

            sold_today = set()
            for event in [item for item in signal_events if item["direction"] == "SELL"]:
                symbol = event["symbol"]
                if symbol not in positions or symbol not in price_map:
                    continue
                position = positions[symbol]
                quantity = int(position.get("shares") or 0)
                if quantity <= 0:
                    continue
                sell_price = price_map[symbol] * (1 - slippage_rate)
                amount = sell_price * quantity
                commission = amount * commission_rate
                cash += amount - commission
                cost_basis = float(position.get("cost_basis") or 0)
                profit = amount - commission - cost_basis
                profit_pct = profit / cost_basis * 100 if cost_basis > 0 else None
                closed_profits.append(profit)
                del positions[symbol]
                sold_today.add(symbol)
                append_trade(
                    current_date,
                    "SELL",
                    symbol,
                    sell_price,
                    quantity,
                    commission,
                    "sell_signal",
                    "持仓股票当天产生卖点",
                    profit=profit,
                    profit_pct=profit_pct,
                )

            buy_events = [
                item for item in signal_events
                if item["direction"] == "BUY"
                and item["symbol"] not in positions
                and item["symbol"] not in sold_today
                and item["symbol"] in price_map
            ]
            buy_events.sort(key=lambda item: (float(item.get("turnover") or 0), item["symbol"]), reverse=True)
            for event in buy_events:
                if len(positions) >= max_positions:
                    break
                symbol = event["symbol"]
                portfolio_before = _portfolio_value(cash, positions, last_prices)
                target_amount = portfolio_before / max_positions
                buy_budget = min(cash, target_amount)
                buy_price = price_map[symbol] * (1 + slippage_rate)
                quantity = _floor_lot(buy_budget / (buy_price * (1 + commission_rate)), lot_size)
                if quantity <= 0:
                    continue
                amount = buy_price * quantity
                commission = amount * commission_rate
                if amount + commission > cash + 1e-9:
                    continue
                cash -= amount + commission
                positions[symbol] = {
                    "shares": quantity,
                    "avg_cost": (amount + commission) / quantity,
                    "cost_basis": amount + commission,
                    "entry_date": current_date,
                    "last_price": price_map[symbol],
                }
                append_trade(
                    current_date,
                    "BUY",
                    symbol,
                    buy_price,
                    quantity,
                    commission,
                    "buy_signal",
                    f"当日买点按成交额排序补位，成交额 {float(event.get('turnover') or 0):.0f}",
                )

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
            "buy_signal_count": sum(1 for item in events if item["direction"] == "BUY"),
            "sell_signal_count": sum(1 for item in events if item["direction"] == "SELL"),
            "trade_count": len(trades),
            "closed_trade_count": len(closed_profits),
            "win_count": win_count,
            "win_rate": _round_or_none(win_count / len(closed_profits) * 100 if closed_profits else 0.0, 2),
            "ending_value": _round_or_none(current_value, 2),
            "cash": equity_curve[-1]["cash"] if equity_curve else _round_or_none(cash, 2),
            "holding_count": len(holdings),
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
                "volume_window": VOLUME_WINDOW,
                "min_listing_days": min_listing_days,
                "price_source": DAILY_PRICE_SOURCE,
            },
        }
