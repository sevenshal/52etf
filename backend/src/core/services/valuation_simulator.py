import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session as ORMSession

from ..database import (
    SessionLocal,
    StockEVC,
    StockTag,
    ValuationSimConfig,
    ValuationSimLog,
    stock_tags,
)
from ..external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
    ExternalTradingValuationSimPositionState,
    get_external_trading_db_ctx,
)
from ..static_info import get_static_info_snapshot_map
from .external_trading_ledger import (
    STRATEGY_VALUATION_SIM,
    normalize_symbol as normalize_external_symbol,
    safe_float as external_safe_float,
    safe_int as external_safe_int,
    sync_target_positions,
)
from .external_trading_market import (
    EXTERNAL_TRADING_MARKET_US_STOCK,
    normalize_external_trading_market_type,
)
from .factor_backtest_engine import get_max_trade_date, load_price_frame, load_universe_history

logger = logging.getLogger(__name__)

QQQ_ETF_SYMBOL = "QQQ.US"
DEFAULT_UNIVERSE_TAG_NAMES = ("Nasdaq 100+", "Nasdaq 100", "纳斯达克100", "纳指100")
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_TRIGGER_TIME = "18:00"
ONE_HUNDRED_MILLION = 100_000_000


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


def _market_cap_threshold_to_usd(value: Any) -> Optional[float]:
    number = _safe_float(value, 0.0)
    return number * ONE_HUNDRED_MILLION if number > 0 else None


def _calculate_market_cap(price: Any, static_info: Dict[str, Any]) -> Optional[float]:
    last_price = _positive_float(price)
    shares = _positive_float((static_info or {}).get("total_shares"))
    if last_price is None or shares is None:
        return None
    return last_price * shares


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


def _normalize_tag_ids(value: Optional[Iterable[Any]]) -> List[str]:
    result: List[str] = []
    for item in value or []:
        tag_id = str(item or "").strip()
        if tag_id and tag_id not in result:
            result.append(tag_id)
    return result


def _default_universe_tag_ids(db: ORMSession) -> List[str]:
    rows = (
        db.query(StockTag)
        .filter(StockTag.name.in_(list(DEFAULT_UNIVERSE_TAG_NAMES)))
        .all()
    )
    rows = sorted(rows, key=lambda row: (row.sort_group if row.sort_group is not None else 999999, row.name or ""))
    return [str(row.id) for row in rows if row.id]


def _load_tag_universe(db: ORMSession, tag_ids: Sequence[str]) -> List[str]:
    safe_tag_ids = _normalize_tag_ids(tag_ids)
    if not safe_tag_ids:
        return []
    latest_tag_date = db.query(func.max(stock_tags.c.date)).scalar()
    if not latest_tag_date:
        return []
    rows = (
        db.query(stock_tags.c.stock_symbol)
        .filter(stock_tags.c.date == latest_tag_date, stock_tags.c.tag_id.in_(safe_tag_ids))
        .distinct()
        .order_by(stock_tags.c.stock_symbol.asc())
        .all()
    )
    return [str(row[0]).strip().upper() for row in rows if row[0]]


def _load_latest_universe(
    db: ORMSession,
    as_of_date: date,
    tag_ids: Optional[Iterable[Any]] = None,
) -> List[str]:
    selected_tag_ids = _normalize_tag_ids(tag_ids)
    explicit_tags = bool(selected_tag_ids)
    if not selected_tag_ids:
        selected_tag_ids = _default_universe_tag_ids(db)
    if selected_tag_ids:
        tag_symbols = _load_tag_universe(db, selected_tag_ids)
        if tag_symbols or explicit_tags:
            return tag_symbols

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
    universe_tag_ids = _normalize_tag_ids(getattr(config, "universe_tag_ids", None))
    universe_symbols = _load_latest_universe(db, as_of_date, universe_tag_ids)
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
    min_market_cap = _market_cap_threshold_to_usd(getattr(config, "min_market_cap_100m", None))
    max_market_cap = _market_cap_threshold_to_usd(getattr(config, "max_market_cap_100m", None))
    has_market_cap_filter = min_market_cap is not None or max_market_cap is not None

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
        market_cap = _calculate_market_cap(_positive_float(getattr(valuation, "last_price", None)) or market_price, static_info)
        if has_market_cap_filter:
            if market_cap is None:
                continue
            if min_market_cap is not None and market_cap < min_market_cap:
                continue
            if max_market_cap is not None and market_cap > max_market_cap:
                continue
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
            "market_cap": market_cap,
            "market_cap_100m": market_cap / ONE_HUNDRED_MILLION if market_cap else None,
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
        "universe_tag_ids": universe_tag_ids,
        "universe_count": len(universe_symbols),
        "price_signal_count": len(signals),
        "valuation_count": len(valuation_rows),
        "candidates": candidates,
        "latest_prices": latest_prices,
    }


def _normalized_external_symbol(symbol: Any) -> Optional[str]:
    return normalize_external_symbol(str(symbol or "").strip().upper())


def _position_market_value(position: ExternalTradingLedgerPosition) -> float:
    quantity = external_safe_int(position.quantity)
    if quantity <= 0:
        return 0.0
    market_value = external_safe_float(position.market_value)
    if market_value > 0:
        return market_value
    market_price = external_safe_float(position.market_price)
    if market_price <= 0:
        market_price = external_safe_float(position.avg_cost)
    return quantity * market_price if market_price > 0 else 0.0


def _position_reference_price(position: ExternalTradingLedgerPosition) -> Optional[float]:
    for value in (position.market_price, position.avg_cost):
        price = _positive_float(value)
        if price is not None:
            return price
    quantity = external_safe_int(position.quantity)
    market_value = external_safe_float(position.market_value)
    return market_value / quantity if quantity > 0 and market_value > 0 else None


def _stored_sub_account_valuation(
    sub_account: ExternalTradingSubAccount,
    positions: Sequence[ExternalTradingLedgerPosition],
) -> Dict[str, Any]:
    position_market_value = round(sum(_position_market_value(position) for position in positions), 2)
    cash_available = round(external_safe_float(sub_account.cash_available), 2)
    return {
        "cash_available": cash_available,
        "position_market_value": position_market_value,
        "net_asset": round(cash_available + position_market_value, 2),
        "position_count": len([position for position in positions if external_safe_int(position.quantity) > 0]),
    }


def _floor_to_lot(quantity: float, lot_size: int) -> int:
    lot = max(1, int(lot_size or 1))
    return max(0, int(quantity // lot) * lot)


def _resolve_lot_size(account: ExternalTradingAccount, sub_account: ExternalTradingSubAccount) -> int:
    sub_lot_size = external_safe_int(getattr(sub_account, "executor_lot_size", None))
    if sub_lot_size > 0:
        return sub_lot_size
    market_type = normalize_external_trading_market_type(getattr(account, "market_type", None))
    if market_type == EXTERNAL_TRADING_MARKET_US_STOCK:
        return 1
    account_lot_size = external_safe_int(getattr(account, "executor_lot_size", None))
    return account_lot_size if account_lot_size > 0 else 1


def _parse_iso_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _update_position_strategy_mark(
    state: ExternalTradingValuationSimPositionState,
    quantity: int,
    trade_date: date,
    price: float,
    high_price: Optional[float] = None,
) -> ExternalTradingValuationSimPositionState:
    high_mark = _positive_float(high_price) or price
    previous_high = _positive_float(state.highest_price)
    last_trade_date = _parse_iso_date(state.last_trade_date)

    if previous_high is None or high_mark > previous_high:
        state.highest_price = high_mark
        state.highest_price_date = trade_date
        state.days_without_high = 0
    elif last_trade_date != trade_date:
        state.days_without_high = int(state.days_without_high or 0) + 1

    if not state.opened_trade_date:
        state.opened_trade_date = trade_date
    state.last_trade_date = trade_date
    state.last_price = price
    state.last_market_value = max(0, int(quantity or 0)) * price
    state.updated_at = datetime.now()
    return state


class ValuationSimulationService:
    def __init__(self, db: ORMSession, external_db: Optional[ORMSession] = None):
        self.db = db
        self.external_db = external_db

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
        del execution_date
        now = datetime.now()
        target_date = as_of_date or get_max_trade_date()

        if not allow_signal_generation:
            return {
                "status": "SKIPPED",
                "message": "未到信号生成时间",
                "trade_date": target_date,
                "candidate_count": 0,
                "buy_count": 0,
                "sell_count": 0,
            }

        if config.last_run_date and config.last_run_date >= target_date:
            message = f"{target_date} 已同步过目标仓位，未重复处理"
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

        result = self._with_external_db(lambda external_db: self._sync_target_positions(
            external_db,
            config,
            signal_date=target_date,
        ))
        config.last_run_at = now
        config.last_run_date = target_date
        config.last_run_status = result.get("status") or "OK"
        config.last_run_message = str(result.get("message") or "")[:500]
        config.updated_at = now
        self._record_run_log(
            config,
            config.last_run_status,
            trigger_source,
            target_date,
            config.last_run_message,
            candidate_count=int(result.get("candidate_count") or 0),
            buy_count=int(result.get("buy_count") or 0),
            sell_count=int(result.get("sell_count") or 0),
            total_equity=result.get("total_equity"),
            action="SYNC_TARGETS",
        )
        return result

    def _with_external_db(self, callback: Callable[[ORMSession], Dict[str, Any]]) -> Dict[str, Any]:
        if self.external_db is not None:
            return callback(self.external_db)
        with get_external_trading_db_ctx() as external_db:
            return callback(external_db)

    def _get_external_account_and_sub_account(
        self,
        external_db: ORMSession,
        config: ValuationSimConfig,
    ) -> Tuple[ExternalTradingAccount, ExternalTradingSubAccount]:
        if not config.external_trading_account_id or not config.live_sub_account_id:
            raise ValueError("估值模拟盘必须绑定外部交易账户和子账户")
        account = external_db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.id == config.external_trading_account_id,
            ExternalTradingAccount.account_id == config.account_id,
        ).first()
        if not account:
            raise ValueError("外部交易账户不存在")
        market_type = normalize_external_trading_market_type(getattr(account, "market_type", None))
        if market_type != EXTERNAL_TRADING_MARKET_US_STOCK:
            raise ValueError("估值模拟盘只能绑定美股外部交易账户")
        sub_account = external_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
            ExternalTradingSubAccount.external_trading_account_id == account.id,
        ).first()
        if not sub_account:
            raise ValueError("外部交易子账户不存在")
        if not account.enabled or not sub_account.enabled:
            raise ValueError("外部交易账户或子账户未启用")
        if sub_account.strategy_type != STRATEGY_VALUATION_SIM or sub_account.strategy_config_id != config.id:
            raise ValueError("外部交易子账户未绑定到当前估值模拟盘配置")
        return account, sub_account

    def _sync_target_positions(
        self,
        external_db: ORMSession,
        config: ValuationSimConfig,
        signal_date: date,
    ) -> Dict[str, Any]:
        account, sub_account = self._get_external_account_and_sub_account(external_db, config)
        positions = (
            external_db.query(ExternalTradingLedgerPosition)
            .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
            .order_by(ExternalTradingLedgerPosition.symbol.asc())
            .all()
        )
        active_positions = [position for position in positions if external_safe_int(position.quantity) > 0]
        current_quantities = {
            _normalized_external_symbol(position.symbol): external_safe_int(position.quantity)
            for position in active_positions
            if _normalized_external_symbol(position.symbol)
        }

        active_symbols = [
            symbol for symbol in current_quantities.keys()
            if symbol
        ]
        zero_symbols = [
            _normalized_external_symbol(position.symbol)
            for position in positions
            if external_safe_int(position.quantity) <= 0 and _normalized_external_symbol(position.symbol)
        ]
        if zero_symbols:
            external_db.query(ExternalTradingValuationSimPositionState).filter(
                ExternalTradingValuationSimPositionState.config_id == config.id,
                ExternalTradingValuationSimPositionState.sub_account_id == sub_account.id,
                ExternalTradingValuationSimPositionState.symbol.in_(list(dict.fromkeys(zero_symbols))),
            ).delete(synchronize_session=False)

        state_by_symbol: Dict[str, ExternalTradingValuationSimPositionState] = {}
        if active_symbols:
            state_rows = (
                external_db.query(ExternalTradingValuationSimPositionState)
                .filter(
                    ExternalTradingValuationSimPositionState.config_id == config.id,
                    ExternalTradingValuationSimPositionState.sub_account_id == sub_account.id,
                    ExternalTradingValuationSimPositionState.symbol.in_(active_symbols),
                )
                .all()
            )
            state_by_symbol = {
                _normalized_external_symbol(row.symbol): row
                for row in state_rows
                if _normalized_external_symbol(row.symbol)
            }
            for symbol in active_symbols:
                if symbol in state_by_symbol:
                    continue
                row = ExternalTradingValuationSimPositionState(
                    account_id=config.account_id,
                    external_trading_account_id=account.id,
                    sub_account_id=sub_account.id,
                    config_id=config.id,
                    symbol=symbol,
                    days_without_high=0,
                )
                external_db.add(row)
                state_by_symbol[symbol] = row

        candidate_payload = _build_candidate_payload(self.db, config, signal_date, exclude_symbols=active_symbols)
        latest_prices = dict(candidate_payload.get("latest_prices") or {})
        missing_price_symbols = [
            str(position.symbol or "").strip().upper()
            for position in active_positions
            if str(position.symbol or "").strip().upper() not in latest_prices
        ]
        if missing_price_symbols:
            _signals, held_latest_prices = _load_price_context(
                list(dict.fromkeys(missing_price_symbols)),
                signal_date,
                int(config.ema_window or 120),
                int(config.volume_lookback_days or 20),
                int(config.volume_consecutive_days or 3),
                int(getattr(config, "trailing_stop_atr_window", 20) or 20),
            )
            latest_prices.update(held_latest_prices)

        max_positions = max(1, int(config.max_positions or 5))
        valuation = _stored_sub_account_valuation(sub_account, active_positions)
        lot_size = _resolve_lot_size(account, sub_account)
        position_by_symbol = {
            _normalized_external_symbol(position.symbol): position
            for position in active_positions
            if _normalized_external_symbol(position.symbol)
        }
        position_prices: Dict[str, float] = {}
        stop_sell_symbols = set()
        sell_reasons: Dict[str, str] = {}

        for symbol, position in position_by_symbol.items():
            price_info = latest_prices.get(symbol) or {}
            price = _positive_float(price_info.get("close")) or _position_reference_price(position)
            if price is None:
                continue
            position_prices[symbol] = price
            high_price = _positive_float(price_info.get("high")) or price
            state = _update_position_strategy_mark(
                state_by_symbol[symbol],
                external_safe_int(position.quantity),
                signal_date,
                price,
                high_price=high_price,
            )
            highest_price = _positive_float(state.highest_price) or price
            atr = _positive_float(price_info.get("atr"))
            atr_multiple = _safe_float(getattr(config, "trailing_stop_atr_multiple", 2.5), 2.5)
            atr_stop_amount = atr * atr_multiple if atr is not None and atr_multiple > 0 else None
            if atr_stop_amount is not None and highest_price > 0:
                drawdown_amount = highest_price - price
                if drawdown_amount >= atr_stop_amount:
                    reason = "trailing_stop_profit" if price >= external_safe_float(position.avg_cost) else "trailing_stop_loss"
                    stop_sell_symbols.add(symbol)
                    sell_reasons[symbol] = reason

        effective_positions = [
            position for position in active_positions
            if _normalized_external_symbol(position.symbol) not in stop_sell_symbols
        ]
        held_symbols = {
            _normalized_external_symbol(position.symbol)
            for position in effective_positions
            if _normalized_external_symbol(position.symbol)
        }
        raw_candidates = [
            candidate for candidate in (candidate_payload.get("candidates") or [])
            if _normalized_external_symbol(candidate.get("symbol")) not in held_symbols
        ]
        available_slots = max(0, max_positions - len(effective_positions))
        new_candidate_count = len(raw_candidates)
        stale_positions = []
        stale_high_days = int(getattr(config, "stale_high_days", 5) or 5)
        for position in effective_positions:
            symbol = _normalized_external_symbol(position.symbol)
            if not symbol or symbol not in position_prices:
                continue
            state = state_by_symbol.get(symbol)
            if state and int(state.days_without_high or 0) >= stale_high_days:
                stale_positions.append(position)

        replacement_slots = max(0, min(len(stale_positions), new_candidate_count - available_slots))
        if replacement_slots > 0:
            def stale_sort_key(item: ExternalTradingLedgerPosition):
                state = state_by_symbol.get(_normalized_external_symbol(item.symbol))
                return (
                    int(getattr(state, "days_without_high", 0) or 0),
                    -_safe_float(getattr(state, "last_market_value", None)),
                )

            stale_positions.sort(key=stale_sort_key, reverse=True)
            for priority, position in enumerate(stale_positions[:replacement_slots], start=1):
                symbol = _normalized_external_symbol(position.symbol)
                if not symbol:
                    continue
                sell_reasons[symbol] = "stale_replaced"

        sell_symbols = set(sell_reasons.keys())
        remaining_positions = [
            position for position in active_positions
            if _normalized_external_symbol(position.symbol) not in sell_symbols
        ]
        blocked_symbols = {
            _normalized_external_symbol(position.symbol)
            for position in remaining_positions
            if _normalized_external_symbol(position.symbol)
        }
        buy_slots = max(0, max_positions - len(remaining_positions))
        buy_candidates = [
            candidate for candidate in raw_candidates
            if _normalized_external_symbol(candidate.get("symbol")) not in blocked_symbols
        ][:buy_slots]

        estimated_sale_cash = 0.0
        for symbol in sell_symbols:
            position = position_by_symbol.get(symbol)
            if not position:
                continue
            price = position_prices.get(symbol) or _position_reference_price(position)
            if price is not None:
                estimated_sale_cash += external_safe_int(position.quantity) * price
        cash_for_buys = max(0.0, _safe_float(valuation.get("cash_available")) + estimated_sale_cash)
        cash_per_buy = cash_for_buys / len(buy_candidates) if buy_candidates else 0.0

        targets: List[Dict[str, Any]] = []
        target_quantities: Dict[str, int] = {}
        for position in remaining_positions:
            symbol = _normalized_external_symbol(position.symbol)
            quantity = external_safe_int(position.quantity)
            if not symbol or quantity <= 0:
                continue
            price = position_prices.get(symbol) or _position_reference_price(position)
            target_value = round(quantity * price, 2) if price is not None else _position_market_value(position)
            target_quantities[symbol] = quantity
            targets.append({
                "symbol": symbol,
                "target_quantity": quantity,
                "target_weight_pct": None,
                "target_value": target_value,
                "reference_price": round(price, 4) if price is not None else None,
                "reference_price_source": "valuation_hold_close" if symbol in position_prices else "external_ledger",
            })

        for symbol in sorted(sell_symbols):
            position = position_by_symbol.get(symbol)
            if not position:
                continue
            price = position_prices.get(symbol) or _position_reference_price(position)
            target_quantities[symbol] = 0
            targets.append({
                "symbol": symbol,
                "target_quantity": 0,
                "target_weight_pct": 0.0,
                "target_value": 0.0,
                "reference_price": round(price, 4) if price is not None else None,
                "reference_price_source": f"valuation_{sell_reasons.get(symbol) or 'sell'}",
            })

        for candidate in buy_candidates:
            symbol = _normalized_external_symbol(candidate.get("symbol"))
            price = _positive_float(candidate.get("price"))
            if not symbol or price is None:
                continue
            target_quantity = _floor_to_lot(cash_per_buy / price, lot_size)
            if target_quantity <= 0:
                continue
            target_quantities[symbol] = target_quantity
            targets.append({
                "symbol": symbol,
                "target_quantity": target_quantity,
                "target_weight_pct": None,
                "target_value": round(target_quantity * price, 2),
                "reference_price": round(price, 4),
                "reference_price_source": "valuation_candidate_close",
            })

        total_target_value = sum(_safe_float(target.get("target_value")) for target in targets if _safe_float(target.get("target_value")) > 0)
        for target in targets:
            if target.get("target_weight_pct") is not None:
                continue
            target["target_weight_pct"] = (
                round(_safe_float(target.get("target_value")) / total_target_value * 100.0, 4)
                if total_target_value > 0 and external_safe_int(target.get("target_quantity")) > 0
                else 0.0
            )

        signal_id = f"valuation_sim:{config.id}:{signal_date.isoformat()}"
        signal_version = datetime.now().strftime("%Y%m%d%H%M%S")
        sync_target_positions(
            external_db,
            sub_account=sub_account,
            targets=targets,
            signal_id=signal_id,
            signal_version=signal_version,
        )

        symbols = set(current_quantities.keys()) | set(target_quantities.keys())
        buy_count = sum(1 for symbol in symbols if target_quantities.get(symbol, 0) > current_quantities.get(symbol, 0))
        sell_count = sum(1 for symbol in symbols if target_quantities.get(symbol, 0) < current_quantities.get(symbol, 0))
        positive_target_symbols = {
            str(target.get("symbol"))
            for target in targets
            if external_safe_int(target.get("target_quantity")) > 0
        }
        message = (
            f"已同步估值模拟盘策略输出：持有目标 {len(positive_target_symbols)} 只，"
            f"止盈止损 {len(stop_sell_symbols)} 只，替换卖出 {len(sell_symbols - stop_sell_symbols)} 只，"
            f"需买 {buy_count} 只，需卖 {sell_count} 只"
            if targets
            else "当前无策略输出，已清空估值模拟盘输出"
        )
        return {
            "status": "OK",
            "message": message,
            "trade_date": signal_date,
            "candidate_count": len(candidate_payload.get("candidates") or []),
            "target_count": len(positive_target_symbols),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "total_equity": valuation.get("net_asset"),
            "cash_available": valuation.get("cash_available"),
            "position_market_value": valuation.get("position_market_value"),
            "lot_size": lot_size,
            "signal_id": signal_id,
            "signal_version": signal_version,
        }

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
        for config in configs:
            if not config.external_trading_account_id or not config.live_sub_account_id:
                skipped += 1
                continue
            due_for_signal = _is_due_for_schedule(config, now)
            if not due_for_signal:
                skipped += 1
                continue
            if config.last_run_date and config.last_run_date >= latest_trade_date:
                skipped += 1
                continue
            try:
                result = service.run_config(
                    config,
                    trigger_source="auto",
                    as_of_date=latest_trade_date,
                    allow_signal_generation=True,
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
