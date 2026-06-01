import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session as ORMSession

from ..database import (
    SessionLocal,
    StockEVC,
    ValuationSimConfig,
    ValuationSimEquity,
    ValuationSimLog,
    ValuationSimPendingOrder,
    ValuationSimPosition,
    ValuationSimTrade,
)
from ..static_info import get_static_info_snapshot_map
from .factor_backtest_engine import get_max_trade_date, load_price_frame, load_universe_history
from .longport import LongPortService
from .market import MarketService

logger = logging.getLogger(__name__)

QQQ_ETF_SYMBOL = "QQQ.US"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_TRIGGER_TIME = "18:00"
LONGPORT_MARKET_DATA_ACCOUNT_ID = os.getenv(
    "VALUATION_SIM_LONGPORT_ACCOUNT_ID",
    os.getenv("EXTERNAL_TRADING_VALUATION_LONGPORT_ACCOUNT_ID", "LBPT10001248"),
)
US_MARKET_OPEN_TIME = time(hour=9, minute=30)


@dataclass
class PriceSignal:
    symbol: str
    trade_date: date
    close: float
    ema: float
    atr: Optional[float]
    atrp_pct: Optional[float]
    price_vs_ema_pct: float
    volume_ratio: float
    volume_base_avg: float
    recent_volumes: List[float]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _positive_float(value: Any) -> Optional[float]:
    number = _safe_float(value, 0.0)
    return number if number > 0 else None


def _parse_clock_time(value: Optional[str]) -> time:
    text = str(value or DEFAULT_TRIGGER_TIME).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text[:2]))
    except Exception:
        return time(hour=18, minute=0)


def _get_timezone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or DEFAULT_TIMEZONE))
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _aware_now(now: Optional[datetime] = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _is_due_for_schedule(config: ValuationSimConfig, now: Optional[datetime] = None) -> bool:
    tz = _get_timezone(config.trigger_timezone)
    local_now = _aware_now(now).astimezone(tz)
    return local_now.time() >= _parse_clock_time(config.trigger_time)


def _current_us_trade_date(now: Optional[datetime] = None) -> date:
    return _aware_now(now).astimezone(ZoneInfo("US/Eastern")).date()


def _is_us_trading_day(trade_date: date) -> bool:
    return trade_date.weekday() < 5 and not MarketService.is_us_market_holiday(trade_date)


def _is_us_execution_window(execution_date: date, now: Optional[datetime] = None) -> bool:
    local_now = _aware_now(now).astimezone(ZoneInfo("US/Eastern"))
    if local_now.date() != execution_date:
        return False
    return _is_us_trading_day(execution_date) and local_now.time() >= US_MARKET_OPEN_TIME


def _compute_ema(values: Sequence[float], span: int) -> Optional[float]:
    clean_values = [_safe_float(value) for value in values if _positive_float(value) is not None]
    if not clean_values:
        return None
    alpha = 2.0 / (max(1, int(span)) + 1.0)
    ema = clean_values[0]
    for value in clean_values[1:]:
        ema = alpha * value + (1.0 - alpha) * ema
    return ema


def _compute_atr(rows: Sequence[Dict[str, Any]], window: int) -> Optional[float]:
    period = max(1, int(window or 20))
    true_ranges: List[float] = []
    previous_close: Optional[float] = None
    for row in rows:
        high = _positive_float(row.get("high"))
        low = _positive_float(row.get("low"))
        close = _positive_float(row.get("close"))
        if high is None or low is None:
            previous_close = close or previous_close
            continue
        components = [high - low]
        if previous_close is not None:
            components.extend([abs(high - previous_close), abs(low - previous_close)])
        true_range = max(components)
        if true_range > 0:
            true_ranges.append(true_range)
        previous_close = close or previous_close
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = ((atr * (period - 1)) + true_range) / period
    return atr if atr > 0 else None


def _load_latest_universe(db: ORMSession, as_of_date: date) -> List[str]:
    start_date = as_of_date - timedelta(days=14)
    universe_history = load_universe_history(db, [QQQ_ETF_SYMBOL], start_date, as_of_date)
    return universe_history.symbols_for_date(as_of_date)


def _load_price_context(
    symbols: Sequence[str],
    as_of_date: date,
    ema_window: int,
    volume_lookback_days: int,
    volume_consecutive_days: int,
    atr_window: int = 20,
) -> Tuple[Dict[str, PriceSignal], Dict[str, Dict[str, Any]]]:
    safe_symbols = [str(symbol or "").strip().upper() for symbol in symbols if symbol]
    if not safe_symbols:
        return {}, {}

    required_rows = max(
        int(ema_window or 120),
        int(volume_lookback_days or 20) + int(volume_consecutive_days or 3),
        int(atr_window or 20) + 1,
    )
    fetch_days = max(260, required_rows * 4)
    price_df = load_price_frame(list(dict.fromkeys(safe_symbols)), as_of_date - timedelta(days=fetch_days), as_of_date)
    if price_df.is_empty():
        return {}, {}

    rows_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for row in price_df.sort(["symbol", "trade_date"]).to_dicts():
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            rows_by_symbol.setdefault(symbol, []).append(row)

    signals: Dict[str, PriceSignal] = {}
    latest_prices: Dict[str, Dict[str, Any]] = {}
    for symbol, rows in rows_by_symbol.items():
        usable_rows = [row for row in rows if row.get("trade_date") and row.get("trade_date") <= as_of_date]
        if not usable_rows:
            continue

        latest = usable_rows[-1]
        latest_close = _positive_float(latest.get("close"))
        latest_date = latest.get("trade_date")
        atr = _compute_atr(usable_rows, int(atr_window or 20))
        atrp_pct = (atr / latest_close * 100.0) if atr is not None and latest_close else None
        if latest_close is not None:
            latest_prices[symbol] = {
                "trade_date": latest_date,
                "close": latest_close,
                "high": _positive_float(latest.get("high")) or latest_close,
                "atr": atr,
                "atrp_pct": atrp_pct,
                "volume": _safe_float(latest.get("volume")),
            }

        if latest_date != as_of_date or latest_close is None or len(usable_rows) < required_rows:
            continue

        closes = [_safe_float(row.get("close")) for row in usable_rows]
        ema = _compute_ema(closes, int(ema_window or 120))
        if ema is None or ema <= 0:
            continue

        consecutive = max(1, int(volume_consecutive_days or 3))
        lookback = max(1, int(volume_lookback_days or 20))
        if len(usable_rows) < lookback + consecutive:
            continue
        recent_rows = usable_rows[-consecutive:]
        base_rows = usable_rows[-(lookback + consecutive):-consecutive]
        base_volumes = [_safe_float(row.get("volume")) for row in base_rows if _safe_float(row.get("volume")) > 0]
        recent_volumes = [_safe_float(row.get("volume")) for row in recent_rows]
        if len(base_volumes) < lookback or any(volume <= 0 for volume in recent_volumes):
            continue
        base_avg = sum(base_volumes) / len(base_volumes)
        if base_avg <= 0:
            continue
        volume_ratio = min(volume / base_avg for volume in recent_volumes)
        signals[symbol] = PriceSignal(
            symbol=symbol,
            trade_date=latest_date,
            close=latest_close,
            ema=ema,
            atr=atr,
            atrp_pct=atrp_pct,
            price_vs_ema_pct=(latest_close / ema - 1.0) * 100.0,
            volume_ratio=volume_ratio,
            volume_base_avg=base_avg,
            recent_volumes=recent_volumes,
        )

    return signals, latest_prices


def _parse_quote_market_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(ZoneInfo("US/Eastern")).date()
        return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("US/Eastern")).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(ZoneInfo("US/Eastern")).date()
        return parsed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("US/Eastern")).date()
    except Exception:
        try:
            return date.fromisoformat(text[:10])
        except Exception:
            return None


def _load_realtime_price_map(symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    safe_symbols = [str(symbol or "").strip().upper() for symbol in symbols if symbol]
    if not safe_symbols:
        return {}
    unique_symbols = list(dict.fromkeys(safe_symbols))
    service = LongPortService.get_instance(LONGPORT_MARKET_DATA_ACCOUNT_ID)
    quotes = service.get_quote_batch(unique_symbols) or []
    result: Dict[str, Dict[str, Any]] = {}
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        last_price = _positive_float(quote.get("price"))
        open_price = _positive_float(quote.get("open"))
        if last_price is None and open_price is None:
            continue
        timestamp = quote.get("timestamp")
        result[symbol] = {
            "trade_date": _parse_quote_market_date(timestamp),
            "market_date": _parse_quote_market_date(timestamp),
            "open": open_price,
            "price": last_price,
            "close": last_price or open_price,
            "volume": _safe_float(quote.get("volume")),
            "source": "longport_realtime",
            "timestamp": str(timestamp) if timestamp is not None else None,
        }
    return result


def _quote_is_fresh_for_execution(
    price_info: Optional[Dict[str, Any]],
    signal_date: date,
    execution_date: date,
) -> bool:
    if not price_info:
        return False
    market_date = price_info.get("market_date") or price_info.get("trade_date")
    if market_date is None:
        return True
    if isinstance(market_date, datetime):
        market_date = market_date.date()
    return market_date == execution_date and market_date > signal_date


def _latest_valuation_rows(db: ORMSession, symbols: Sequence[str]) -> Tuple[Optional[date], Dict[str, StockEVC]]:
    safe_symbols = [str(symbol or "").strip().upper() for symbol in symbols if symbol]
    if not safe_symbols:
        return None, {}
    latest_date = db.query(func.max(StockEVC.date)).scalar()
    if not latest_date:
        return None, {}
    rows = (
        db.query(StockEVC)
        .filter(StockEVC.date == latest_date, StockEVC.symbol.in_(list(dict.fromkeys(safe_symbols))))
        .all()
    )
    return latest_date, {str(row.symbol).upper(): row for row in rows}


def _build_candidate_payload(
    db: ORMSession,
    config: ValuationSimConfig,
    as_of_date: date,
    exclude_symbols: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    universe_symbols = _load_latest_universe(db, as_of_date)
    signals, latest_prices = _load_price_context(
        universe_symbols,
        as_of_date,
        int(config.ema_window or 120),
        int(config.volume_lookback_days or 20),
        int(config.volume_consecutive_days or 3),
        int(getattr(config, "trailing_stop_atr_window", 20) or 20),
    )
    valuation_date, valuation_rows = _latest_valuation_rows(db, universe_symbols)
    static_info_map = get_static_info_snapshot_map(db, list(valuation_rows))
    excluded = {str(symbol or "").strip().upper() for symbol in (exclude_symbols or []) if symbol}

    candidates: List[Dict[str, Any]] = []
    for symbol in universe_symbols:
        symbol = str(symbol or "").strip().upper()
        if not symbol or symbol in excluded:
            continue
        signal = signals.get(symbol)
        valuation = valuation_rows.get(symbol)
        if not signal or not valuation:
            continue

        market_price = signal.close
        fair_value_lo = _positive_float(valuation.fair_value_lo)
        fair_value_hi = _positive_float(valuation.fair_value_hi)
        next_fy_lo = _positive_float(valuation.forward_next_fy_lo)
        next_fy_hi = _positive_float(valuation.forward_next_fy_hi)
        if not all([fair_value_lo, fair_value_hi, next_fy_lo, next_fy_hi]):
            continue

        undervalued = market_price < _safe_float(config.undervalue_threshold, 0.9) * fair_value_lo
        growing = (
            next_fy_lo > _safe_float(config.next_fy_growth_threshold, 1.1) * fair_value_lo
            and next_fy_hi > _safe_float(config.next_fy_growth_threshold, 1.1) * fair_value_hi
        )
        below_ema = signal.price_vs_ema_pct <= -abs(_safe_float(config.price_below_ema_pct, 10.0))
        volume_ok = signal.volume_ratio >= _safe_float(config.volume_ratio_threshold, 1.4)
        if not (undervalued and growing and below_ema and volume_ok):
            continue

        static_info = static_info_map.get(symbol, {})
        candidates.append({
            "symbol": symbol,
            "company": static_info.get("name_cn") or static_info.get("name_en") or valuation.company,
            "trade_date": as_of_date,
            "valuation_date": valuation_date,
            "price": market_price,
            "ema": signal.ema,
            "atr": signal.atr,
            "atrp_pct": signal.atrp_pct,
            "price_vs_ema_pct": signal.price_vs_ema_pct,
            "volume_ratio": signal.volume_ratio,
            "volume_base_avg": signal.volume_base_avg,
            "recent_volumes": signal.recent_volumes,
            "fair_value_lo": fair_value_lo,
            "fair_value_hi": fair_value_hi,
            "forward_next_fy_lo": next_fy_lo,
            "forward_next_fy_hi": next_fy_hi,
            "undervalue_pct": (fair_value_lo / market_price - 1.0) * 100.0,
            "next_fy_growth_lo_pct": (next_fy_lo / fair_value_lo - 1.0) * 100.0,
            "next_fy_growth_hi_pct": (next_fy_hi / fair_value_hi - 1.0) * 100.0,
        })

    candidates.sort(key=lambda item: item.get("volume_ratio") or 0.0, reverse=True)
    return {
        "trade_date": as_of_date,
        "valuation_date": valuation_date,
        "universe_count": len(universe_symbols),
        "price_signal_count": len(signals),
        "valuation_count": len(valuation_rows),
        "candidates": candidates,
        "latest_prices": latest_prices,
    }


class ValuationSimulationService:
    def __init__(self, db: ORMSession):
        self.db = db

    def preview_candidates(self, config: ValuationSimConfig, limit: int = 50) -> Dict[str, Any]:
        as_of_date = get_max_trade_date()
        payload = _build_candidate_payload(self.db, config, as_of_date)
        payload["candidates"] = payload["candidates"][:max(1, min(int(limit or 50), 200))]
        payload.pop("latest_prices", None)
        return payload

    def run_config(
        self,
        config: ValuationSimConfig,
        trigger_source: str = "manual",
        as_of_date: Optional[date] = None,
        allow_signal_generation: bool = True,
        execution_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        now_aware = datetime.now(timezone.utc)
        now = datetime.now()
        target_date = as_of_date or get_max_trade_date()
        current_execution_date = execution_date or _current_us_trade_date(now_aware)
        results: List[Dict[str, Any]] = []
        execution_result = self._execute_pending_orders(config, current_execution_date, trigger_source, now=now_aware)
        if execution_result:
            results.append(execution_result)

        pending_order = self._oldest_pending_order(config)
        if pending_order:
            message = f"{pending_order.signal_date} 信号等待下个交易日 LongPort 实时开盘价"
            if results:
                return self._combine_run_results(results)
            config.last_run_at = now
            config.last_run_status = "WAITING"
            config.last_run_message = message
            config.updated_at = now
            if trigger_source == "manual":
                self._record_run_log(config, "SKIPPED", trigger_source, target_date, message, action="WAIT")
            return {
                "status": "WAITING",
                "message": message,
                "trade_date": target_date,
                "candidate_count": 0,
                "buy_count": 0,
                "sell_count": 0,
            }

        if not allow_signal_generation:
            if results:
                return self._combine_run_results(results)
            return {
                "status": "SKIPPED",
                "message": "未到信号生成时间",
                "trade_date": target_date,
                "candidate_count": 0,
                "buy_count": 0,
                "sell_count": 0,
            }

        if config.last_run_date and config.last_run_date >= target_date:
            if results:
                return self._combine_run_results(results)
            message = f"{target_date} 已生成过信号，未重复处理"
            self._record_run_log(config, "SKIPPED", trigger_source, target_date, message)
            config.last_run_at = now
            config.last_run_status = "SKIPPED"
            config.last_run_message = message
            config.updated_at = now
            return {
                "status": "SKIPPED",
                "message": message,
                "trade_date": target_date,
                "candidate_count": 0,
                "buy_count": 0,
                "sell_count": 0,
            }

        results.append(self._generate_pending_signal(config, target_date, trigger_source))
        return self._combine_run_results(results)

    def _generate_pending_signal(
        self,
        config: ValuationSimConfig,
        signal_date: date,
        trigger_source: str,
    ) -> Dict[str, Any]:
        now = datetime.now()
        active_positions = self._active_positions(config)
        symbols_for_prices = [position.symbol for position in active_positions]
        candidate_payload = _build_candidate_payload(self.db, config, signal_date, exclude_symbols=symbols_for_prices)
        latest_prices = dict(candidate_payload.get("latest_prices") or {})
        missing_price_symbols = [
            str(position.symbol).upper()
            for position in active_positions
            if str(position.symbol).upper() not in latest_prices
        ]
        if missing_price_symbols:
            _signals, held_latest_prices = _load_price_context(
                missing_price_symbols,
                signal_date,
                int(config.ema_window or 120),
                int(config.volume_lookback_days or 20),
                int(config.volume_consecutive_days or 3),
                int(getattr(config, "trailing_stop_atr_window", 20) or 20),
            )
            latest_prices.update(held_latest_prices)

        pending_sell_orders: List[ValuationSimPendingOrder] = []
        stop_sell_symbols: set[str] = set()

        for position in list(active_positions):
            price_info = latest_prices.get(str(position.symbol).upper())
            price = _positive_float((price_info or {}).get("close"))
            if price is None:
                continue
            high_price = _positive_float((price_info or {}).get("high")) or price
            self._update_position_mark(position, signal_date, price, high_price=high_price)
            highest_price = _positive_float(position.highest_price) or price
            atr = _positive_float((price_info or {}).get("atr"))
            atrp_pct = _safe_float((price_info or {}).get("atrp_pct"))
            atr_multiple = _safe_float(getattr(config, "trailing_stop_atr_multiple", 2.5), 2.5)
            atr_stop_amount = atr * atr_multiple if atr is not None else None
            drawdown_amount = highest_price - price
            if atr_stop_amount is not None and drawdown_amount >= atr_stop_amount:
                reason = "trailing_stop_profit" if price >= _safe_float(position.avg_cost) else "trailing_stop_loss"
                order = self._create_pending_order(config, signal_date, "SELL", position.symbol, reason, price, {
                    "highest_price": highest_price,
                    "drawdown_pct": (price / highest_price - 1.0) * 100.0,
                    "drawdown_amount": drawdown_amount,
                    "atr": atr,
                    "atrp_pct": atrp_pct,
                    "atr_window": getattr(config, "trailing_stop_atr_window", 20),
                    "atr_multiple": atr_multiple,
                    "atr_stop_amount": atr_stop_amount,
                    "avg_cost": position.avg_cost,
                })
                pending_sell_orders.append(order)
                stop_sell_symbols.add(str(position.symbol).upper())

        active_positions = self._active_positions(config)
        effective_positions = [
            position for position in active_positions
            if str(position.symbol).upper() not in stop_sell_symbols
        ]
        held_symbols = {str(position.symbol).upper() for position in effective_positions}
        candidate_symbols = {str(item["symbol"]).upper() for item in candidate_payload["candidates"]}
        stale_positions = [
            position for position in effective_positions
            if int(position.days_without_high or 0) >= int(config.stale_high_days or 5)
            and str(position.symbol).upper() not in candidate_symbols
        ]
        available_slots = max(0, int(config.max_positions or 5) - len(effective_positions))
        new_candidate_count = len([
            item for item in candidate_payload["candidates"]
            if str(item["symbol"]).upper() not in held_symbols
        ])
        replacement_slots = max(0, min(len(stale_positions), new_candidate_count - available_slots))

        if replacement_slots > 0:
            stale_positions.sort(key=lambda item: (int(item.days_without_high or 0), -_safe_float(item.last_market_value)), reverse=True)
            for priority, position in enumerate(stale_positions[:replacement_slots], start=1):
                price_info = latest_prices.get(str(position.symbol).upper())
                price = _positive_float((price_info or {}).get("close"))
                if price is None:
                    continue
                order = self._create_pending_order(config, signal_date, "SELL", position.symbol, "stale_replaced", price, {
                    "days_without_high": position.days_without_high,
                    "stale_high_days": config.stale_high_days,
                    "last_market_value": position.last_market_value,
                }, priority=priority)
                pending_sell_orders.append(order)
                held_symbols.discard(str(position.symbol).upper())

        sell_symbols = {str(order.symbol).upper() for order in pending_sell_orders}
        remaining_positions = [
            position for position in active_positions
            if str(position.symbol).upper() not in sell_symbols
        ]
        blocked_symbols = {str(position.symbol).upper() for position in remaining_positions}
        buy_slots = max(0, int(config.max_positions or 5) - len(remaining_positions))
        buy_candidates = [
            item for item in candidate_payload["candidates"]
            if str(item["symbol"]).upper() not in blocked_symbols
        ][:buy_slots]

        pending_buy_orders: List[ValuationSimPendingOrder] = []
        for priority, candidate in enumerate(buy_candidates, start=1):
            order = self._create_pending_order(
                config,
                signal_date,
                "BUY",
                candidate["symbol"],
                "signal",
                _safe_float(candidate.get("price")),
                self._candidate_order_metrics(candidate),
                priority=priority,
            )
            pending_buy_orders.append(order)

        equity = self._record_equity(config, signal_date, latest_prices, price_field="close", update_high_days=True)
        sell_count = len(pending_sell_orders)
        buy_count = len(pending_buy_orders)
        message = f"生成信号：待卖 {sell_count} 只，待买 {buy_count} 只，下一交易日按 LongPort 实时开盘价执行" if (sell_count or buy_count) else "无新信号"

        config.last_run_at = now
        config.last_run_date = signal_date
        config.last_run_status = "OK"
        config.last_run_message = message[:500]
        config.updated_at = now
        self._record_run_log(
            config,
            "OK",
            trigger_source,
            signal_date,
            message,
            candidate_count=len(candidate_payload["candidates"]),
            buy_count=buy_count,
            sell_count=sell_count,
            total_equity=equity.total_equity if equity else None,
            action="SIGNAL",
        )
        return {
            "status": "OK",
            "message": message,
            "trade_date": signal_date,
            "candidate_count": len(candidate_payload["candidates"]),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "total_equity": equity.total_equity if equity else None,
        }

    def _execute_pending_orders(
        self,
        config: ValuationSimConfig,
        execution_date: date,
        trigger_source: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        executable_orders = self._pending_orders(config, before_signal_date=execution_date)
        if not executable_orders:
            return None
        if not _is_us_execution_window(execution_date, now):
            return None
        signal_date = min(order.signal_date for order in executable_orders if order.signal_date)
        orders = [order for order in executable_orders if order.signal_date == signal_date]
        order_symbols = [order.symbol for order in orders]
        active_positions = self._active_positions(config)
        active_symbols = [position.symbol for position in active_positions]
        price_map = _load_realtime_price_map(list(dict.fromkeys(order_symbols + active_symbols)))
        if not price_map:
            return None

        position_by_symbol = {str(position.symbol).upper(): position for position in active_positions}
        sell_count = 0
        buy_count = 0
        skipped_count = 0
        deferred_count = 0
        messages: List[str] = []

        direct_sell_orders = [
            order for order in orders
            if order.action == "SELL" and str(order.reason or "").startswith("trailing_stop")
        ]
        stale_sell_orders = [
            order for order in orders
            if order.action == "SELL" and order.reason == "stale_replaced"
        ]
        buy_orders = [order for order in orders if order.action == "BUY"]

        for order in direct_sell_orders:
            price_info = price_map.get(str(order.symbol).upper()) or {}
            price = _positive_float(price_info.get("open"))
            position = position_by_symbol.get(str(order.symbol).upper())
            if price is None or not _quote_is_fresh_for_execution(price_info, signal_date, execution_date):
                self._defer_order(order, "等待LongPort实时开盘价")
                deferred_count += 1
                continue
            if not position:
                self._skip_order(order, execution_date, "缺少持仓")
                skipped_count += 1
                continue
            trade = self._sell_position(config, position, execution_date, price, order.reason or "trailing_stop", order.metrics)
            self._fill_order_from_trade(order, trade, execution_date)
            sell_count += 1
            messages.append(f"{order.symbol} 止盈/止损按LongPort开盘卖出")
            position_by_symbol.pop(str(order.symbol).upper(), None)

        active_positions = self._active_positions(config)
        free_slots = max(0, int(config.max_positions or 5) - len(active_positions))
        valid_buy_orders: List[ValuationSimPendingOrder] = []
        for order in sorted(buy_orders, key=lambda item: int(item.priority or 0)):
            price_info = price_map.get(str(order.symbol).upper()) or {}
            price = _positive_float(price_info.get("open"))
            if price is None or not _quote_is_fresh_for_execution(price_info, signal_date, execution_date):
                self._defer_order(order, "等待LongPort实时开盘价")
                deferred_count += 1
                continue
            if not self._pending_buy_still_valid(config, order, price):
                self._skip_order(order, execution_date, "LongPort开盘价重检不符合")
                skipped_count += 1
                continue
            valid_buy_orders.append(order)

        replacement_needed = max(0, len(valid_buy_orders) - free_slots)
        stale_sell_orders = sorted(stale_sell_orders, key=lambda item: int(item.priority or 0))
        deferred_replacement_count = 0
        for order in stale_sell_orders[:replacement_needed]:
            price_info = price_map.get(str(order.symbol).upper()) or {}
            price = _positive_float(price_info.get("open"))
            position = position_by_symbol.get(str(order.symbol).upper())
            if price is None or not _quote_is_fresh_for_execution(price_info, signal_date, execution_date):
                self._defer_order(order, "等待LongPort实时开盘价")
                deferred_count += 1
                deferred_replacement_count += 1
                continue
            if not position:
                self._skip_order(order, execution_date, "缺少持仓")
                skipped_count += 1
                continue
            highest_price = _positive_float(position.highest_price)
            if highest_price is not None and price > highest_price:
                self._update_position_mark(position, execution_date, price, high_price=price)
                self._skip_order(order, execution_date, "开盘价创新高，取消替换")
                skipped_count += 1
                continue
            trade = self._sell_position(config, position, execution_date, price, "stale_replaced", order.metrics)
            self._fill_order_from_trade(order, trade, execution_date)
            sell_count += 1
            messages.append(f"{order.symbol} 未创新高替换按LongPort开盘卖出")
            position_by_symbol.pop(str(order.symbol).upper(), None)

        for order in stale_sell_orders[replacement_needed:]:
            self._skip_order(order, execution_date, "开盘重检后无需替换")
            skipped_count += 1

        active_positions = self._active_positions(config)
        buy_slots = max(0, int(config.max_positions or 5) - len(active_positions))
        executable_buy_orders = valid_buy_orders[:buy_slots]
        overflow_buy_orders = valid_buy_orders[buy_slots:]
        for index, order in enumerate(overflow_buy_orders):
            if index < deferred_replacement_count:
                self._defer_order(order, "等待替换卖出执行")
                deferred_count += 1
            else:
                self._skip_order(order, execution_date, "仓位不足")
                skipped_count += 1

        if executable_buy_orders and _safe_float(config.current_cash) > 0:
            cash_per_symbol = _safe_float(config.current_cash) / max(1, buy_slots)
            for order in executable_buy_orders:
                price_info = price_map.get(str(order.symbol).upper()) or {}
                price = _positive_float(price_info.get("open"))
                if price is None or not _quote_is_fresh_for_execution(price_info, signal_date, execution_date):
                    self._defer_order(order, "等待LongPort实时开盘价")
                    deferred_count += 1
                    continue
                candidate = {"symbol": order.symbol, **(order.metrics or {})}
                if self._buy_candidate(config, candidate, execution_date, price, cash_per_symbol):
                    latest_trade = (
                        self.db.query(ValuationSimTrade)
                        .filter(ValuationSimTrade.config_id == config.id, ValuationSimTrade.symbol == order.symbol, ValuationSimTrade.action == "BUY")
                        .order_by(ValuationSimTrade.id.desc())
                        .first()
                    )
                    if latest_trade:
                        self._fill_order_from_trade(order, latest_trade, execution_date)
                    buy_count += 1
                    messages.append(f"{order.symbol} 按LongPort开盘买入")
                else:
                    self._skip_order(order, execution_date, "现金不足")
                    skipped_count += 1
        else:
            for order in executable_buy_orders:
                self._skip_order(order, execution_date, "现金不足")
                skipped_count += 1

        equity = self._record_equity(config, execution_date, price_map, price_field="price", update_high_days=False)
        if messages:
            message = "；".join(messages)
        elif deferred_count:
            message = f"等待LongPort实时行情，未成交 {deferred_count} 条"
        else:
            message = f"执行完成，无成交，跳过 {skipped_count} 条"
        config.last_run_at = datetime.now()
        config.last_run_status = "OK"
        config.last_run_message = message[:500]
        config.updated_at = datetime.now()
        self._record_run_log(
            config,
            "OK",
            trigger_source,
            execution_date,
            message,
            candidate_count=len(orders),
            buy_count=buy_count,
            sell_count=sell_count,
            total_equity=equity.total_equity if equity else None,
            action="EXECUTE",
        )
        return {
            "status": "OK",
            "message": message,
            "trade_date": execution_date,
            "candidate_count": len(orders),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "skipped_count": skipped_count,
            "deferred_count": deferred_count,
            "total_equity": equity.total_equity if equity else None,
        }

    @staticmethod
    def _combine_run_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not results:
            return {"status": "SKIPPED", "message": "无操作", "candidate_count": 0, "buy_count": 0, "sell_count": 0}
        status = "OK" if any(item.get("status") == "OK" for item in results) else results[-1].get("status", "SKIPPED")
        return {
            "status": status,
            "message": "；".join([str(item.get("message") or "") for item in results if item.get("message")]),
            "trade_date": results[-1].get("trade_date"),
            "candidate_count": sum(int(item.get("candidate_count") or 0) for item in results),
            "buy_count": sum(int(item.get("buy_count") or 0) for item in results),
            "sell_count": sum(int(item.get("sell_count") or 0) for item in results),
            "skipped_count": sum(int(item.get("skipped_count") or 0) for item in results),
            "deferred_count": sum(int(item.get("deferred_count") or 0) for item in results),
            "total_equity": next((item.get("total_equity") for item in reversed(results) if item.get("total_equity") is not None), None),
        }

    def _pending_orders(
        self,
        config: ValuationSimConfig,
        before_signal_date: Optional[date] = None,
    ) -> List[ValuationSimPendingOrder]:
        query = (
            self.db.query(ValuationSimPendingOrder)
            .filter(ValuationSimPendingOrder.config_id == config.id, ValuationSimPendingOrder.status == "PENDING")
        )
        if before_signal_date:
            query = query.filter(ValuationSimPendingOrder.signal_date < before_signal_date)
        return (
            query.order_by(
                ValuationSimPendingOrder.signal_date.asc(),
                ValuationSimPendingOrder.action.desc(),
                ValuationSimPendingOrder.priority.asc(),
                ValuationSimPendingOrder.id.asc(),
            )
            .all()
        )

    def _oldest_pending_order(self, config: ValuationSimConfig) -> Optional[ValuationSimPendingOrder]:
        return (
            self.db.query(ValuationSimPendingOrder)
            .filter(ValuationSimPendingOrder.config_id == config.id, ValuationSimPendingOrder.status == "PENDING")
            .order_by(ValuationSimPendingOrder.signal_date.asc(), ValuationSimPendingOrder.id.asc())
            .first()
        )

    @staticmethod
    def _candidate_order_metrics(candidate: Dict[str, Any]) -> Dict[str, Any]:
        metric_keys = [
            "ema",
            "atr",
            "atrp_pct",
            "price_vs_ema_pct",
            "volume_ratio",
            "volume_base_avg",
            "fair_value_lo",
            "fair_value_hi",
            "forward_next_fy_lo",
            "forward_next_fy_hi",
            "undervalue_pct",
            "next_fy_growth_lo_pct",
            "next_fy_growth_hi_pct",
        ]
        metrics = {key: candidate.get(key) for key in metric_keys}
        metrics["signal_price"] = candidate.get("price")
        metrics["valuation_date"] = str(candidate.get("valuation_date") or "")
        metrics["recent_volumes"] = candidate.get("recent_volumes") or []
        return metrics

    def _create_pending_order(
        self,
        config: ValuationSimConfig,
        signal_date: date,
        action: str,
        symbol: str,
        reason: str,
        signal_price: float,
        metrics: Optional[Dict[str, Any]] = None,
        priority: int = 0,
    ) -> ValuationSimPendingOrder:
        order = ValuationSimPendingOrder(
            config_id=config.id,
            account_id=config.account_id,
            signal_date=signal_date,
            symbol=str(symbol or "").strip().upper(),
            action=action,
            status="PENDING",
            reason=reason,
            signal_price=signal_price,
            priority=priority,
            metrics=metrics or {},
            message="待下一交易日按LongPort实时开盘价执行",
        )
        self.db.add(order)
        return order

    def _pending_buy_still_valid(self, config: ValuationSimConfig, order: ValuationSimPendingOrder, open_price: float) -> bool:
        metrics = order.metrics or {}
        fair_value_lo = _positive_float(metrics.get("fair_value_lo"))
        fair_value_hi = _positive_float(metrics.get("fair_value_hi"))
        next_fy_lo = _positive_float(metrics.get("forward_next_fy_lo"))
        next_fy_hi = _positive_float(metrics.get("forward_next_fy_hi"))
        ema = _positive_float(metrics.get("ema"))
        volume_ratio = _safe_float(metrics.get("volume_ratio"))
        if not all([fair_value_lo, fair_value_hi, next_fy_lo, next_fy_hi, ema]):
            return False
        undervalued = open_price < _safe_float(config.undervalue_threshold, 0.9) * fair_value_lo
        growing = (
            next_fy_lo > _safe_float(config.next_fy_growth_threshold, 1.1) * fair_value_lo
            and next_fy_hi > _safe_float(config.next_fy_growth_threshold, 1.1) * fair_value_hi
        )
        below_ema = open_price <= ema * (1.0 - abs(_safe_float(config.price_below_ema_pct, 10.0)) / 100.0)
        volume_ok = volume_ratio >= _safe_float(config.volume_ratio_threshold, 1.4)
        return bool(undervalued and growing and below_ema and volume_ok)

    @staticmethod
    def _skip_order(order: ValuationSimPendingOrder, execution_date: date, message: str):
        order.status = "SKIPPED"
        order.execution_date = execution_date
        order.message = message[:1000]
        order.updated_at = datetime.now()

    @staticmethod
    def _defer_order(order: ValuationSimPendingOrder, message: str):
        order.message = message[:1000]
        order.updated_at = datetime.now()

    @staticmethod
    def _fill_order_from_trade(order: ValuationSimPendingOrder, trade: ValuationSimTrade, execution_date: date):
        order.status = "EXECUTED"
        order.execution_date = execution_date
        order.execution_price = trade.price
        order.quantity = trade.quantity
        order.amount = trade.amount
        order.realized_pnl = trade.realized_pnl
        order.message = "已按LongPort实时行情执行"
        order.updated_at = datetime.now()

    def _active_positions(self, config: ValuationSimConfig) -> List[ValuationSimPosition]:
        return (
            self.db.query(ValuationSimPosition)
            .filter(ValuationSimPosition.config_id == config.id)
            .order_by(ValuationSimPosition.id.asc())
            .all()
        )

    def _update_position_mark(
        self,
        position: ValuationSimPosition,
        trade_date: date,
        price: float,
        high_price: Optional[float] = None,
    ):
        previous_trade_date = position.last_trade_date
        previous_high = _positive_float(position.highest_price)
        high_mark = _positive_float(high_price) or price
        if previous_high is None or high_mark > previous_high:
            position.highest_price = high_mark
            position.highest_price_date = trade_date
            position.days_without_high = 0
        elif previous_trade_date != trade_date:
            position.days_without_high = int(position.days_without_high or 0) + 1

        position.last_price = price
        position.last_market_value = _safe_float(position.quantity) * price
        position.last_trade_date = trade_date
        position.updated_at = datetime.now()

    def _sell_position(
        self,
        config: ValuationSimConfig,
        position: ValuationSimPosition,
        trade_date: date,
        price: float,
        reason: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> ValuationSimTrade:
        quantity = _safe_float(position.quantity)
        amount = quantity * price
        realized_pnl = amount - _safe_float(position.cost_basis)
        config.current_cash = _safe_float(config.current_cash) + amount
        trade = ValuationSimTrade(
            config_id=config.id,
            account_id=config.account_id,
            trade_date=trade_date,
            symbol=position.symbol,
            action="SELL",
            price=price,
            quantity=quantity,
            amount=amount,
            cash_after=config.current_cash,
            realized_pnl=realized_pnl,
            reason=reason,
            metrics=metrics or {},
            message=reason,
        )
        self.db.add(trade)
        self.db.delete(position)
        return trade

    def _buy_candidate(
        self,
        config: ValuationSimConfig,
        candidate: Dict[str, Any],
        trade_date: date,
        price: float,
        allocation: float,
    ) -> bool:
        amount = min(_safe_float(allocation), _safe_float(config.current_cash))
        if amount <= 0:
            return False
        quantity = amount / price
        if quantity <= 0:
            return False
        config.current_cash = max(0.0, _safe_float(config.current_cash) - amount)
        position = ValuationSimPosition(
            config_id=config.id,
            account_id=config.account_id,
            symbol=candidate["symbol"],
            quantity=quantity,
            avg_cost=price,
            cost_basis=amount,
            highest_price=price,
            highest_price_date=trade_date,
            days_without_high=0,
            opened_at=datetime.now(),
            opened_trade_date=trade_date,
            last_price=price,
            last_market_value=amount,
            last_trade_date=trade_date,
        )
        trade = ValuationSimTrade(
            config_id=config.id,
            account_id=config.account_id,
            trade_date=trade_date,
            symbol=candidate["symbol"],
            action="BUY",
            price=price,
            quantity=quantity,
            amount=amount,
            cash_after=config.current_cash,
            realized_pnl=0.0,
            reason="signal",
            metrics={
                "volume_ratio": candidate.get("volume_ratio"),
                "price_vs_ema_pct": candidate.get("price_vs_ema_pct"),
                "undervalue_pct": candidate.get("undervalue_pct"),
                "next_fy_growth_lo_pct": candidate.get("next_fy_growth_lo_pct"),
            },
            message="signal",
        )
        self.db.add(position)
        self.db.add(trade)
        return True

    def _record_equity(
        self,
        config: ValuationSimConfig,
        trade_date: date,
        latest_prices: Dict[str, Dict[str, Any]],
        price_field: str = "close",
        update_high_days: bool = True,
    ) -> Optional[ValuationSimEquity]:
        positions = self._active_positions(config)
        position_value = 0.0
        unrealized_pnl = 0.0
        for position in positions:
            price_info = latest_prices.get(str(position.symbol).upper()) or {}
            price = _positive_float(price_info.get(price_field))
            if price is None:
                price = _positive_float(position.last_price) or _safe_float(position.avg_cost)
            if update_high_days:
                high_price = _positive_float(price_info.get("high")) or price
                self._update_position_mark(position, trade_date, price, high_price=high_price)
            else:
                position.last_price = price
                position.last_market_value = _safe_float(position.quantity) * price
                position.updated_at = datetime.now()
            market_value = _safe_float(position.quantity) * price
            position_value += market_value
            unrealized_pnl += market_value - _safe_float(position.cost_basis)

        realized_pnl = _safe_float(
            self.db.query(func.sum(ValuationSimTrade.realized_pnl))
            .filter(ValuationSimTrade.config_id == config.id, ValuationSimTrade.action == "SELL")
            .scalar()
        )
        total_equity = _safe_float(config.current_cash) + position_value
        equity = (
            self.db.query(ValuationSimEquity)
            .filter(ValuationSimEquity.config_id == config.id, ValuationSimEquity.trade_date == trade_date)
            .first()
        )
        if not equity:
            equity = ValuationSimEquity(config_id=config.id, account_id=config.account_id, trade_date=trade_date)
            self.db.add(equity)
        equity.cash = _safe_float(config.current_cash)
        equity.position_value = position_value
        equity.total_equity = total_equity
        equity.realized_pnl = realized_pnl
        equity.unrealized_pnl = unrealized_pnl
        equity.position_count = len(positions)
        return equity

    def _record_run_log(
        self,
        config: ValuationSimConfig,
        status: str,
        trigger_source: str,
        trade_date: Optional[date],
        message: str,
        candidate_count: int = 0,
        buy_count: int = 0,
        sell_count: int = 0,
        total_equity: Optional[float] = None,
        action: str = "RUN",
    ):
        self.db.add(ValuationSimLog(
            config_id=config.id,
            account_id=config.account_id,
            trigger_source=trigger_source,
            status=status,
            action=action,
            trade_date=trade_date,
            candidate_count=candidate_count,
            buy_count=buy_count,
            sell_count=sell_count,
            total_equity=total_equity,
            message=message[:1000],
        ))


def process_enabled_valuation_simulations(now: Optional[datetime] = None) -> Dict[str, Any]:
    db = SessionLocal()
    processed: List[Dict[str, Any]] = []
    skipped = 0
    errors: List[str] = []
    try:
        try:
            latest_trade_date = get_max_trade_date()
        except Exception as exc:
            logger.warning("Valuation simulation failed to resolve latest trade date: %s", exc)
            return {"processed": processed, "skipped": skipped, "errors": [str(exc)]}

        configs = (
            db.query(ValuationSimConfig)
            .filter(ValuationSimConfig.enabled == True)  # noqa: E712
            .order_by(ValuationSimConfig.id.asc())
            .all()
        )
        service = ValuationSimulationService(db)
        execution_date = _current_us_trade_date(now)
        for config in configs:
            has_executable_pending = bool(service._pending_orders(config, before_signal_date=execution_date))
            due_for_signal = _is_due_for_schedule(config, now)
            if not due_for_signal and not has_executable_pending:
                skipped += 1
                continue
            if due_for_signal and not has_executable_pending and config.last_run_date and config.last_run_date >= latest_trade_date:
                skipped += 1
                continue
            try:
                result = service.run_config(
                    config,
                    trigger_source="auto",
                    as_of_date=latest_trade_date,
                    allow_signal_generation=due_for_signal,
                    execution_date=execution_date,
                )
                db.commit()
                processed.append({"config_id": config.id, **result})
            except Exception as exc:
                db.rollback()
                errors.append(f"config={config.id}: {exc}")
                logger.exception("Valuation simulation auto run failed, config_id=%s", config.id)
                failed_config = db.query(ValuationSimConfig).filter(ValuationSimConfig.id == config.id).first()
                if failed_config:
                    failed_config.last_run_at = datetime.now()
                    failed_config.last_run_status = "ERROR"
                    failed_config.last_run_message = str(exc)[:500]
                    db.add(ValuationSimLog(
                        config_id=failed_config.id,
                        account_id=failed_config.account_id,
                        trigger_source="auto",
                        status="ERROR",
                        action="RUN",
                        trade_date=latest_trade_date,
                        message=str(exc)[:1000],
                    ))
                    db.commit()
        return {"processed": processed, "skipped": skipped, "errors": errors}
    finally:
        db.close()
