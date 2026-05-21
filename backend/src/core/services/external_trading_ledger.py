import hashlib
import json
import logging
import uuid
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBrokerPositionSnapshot,
    ExternalTradingDeliverRecord,
    ExternalTradingLedgerPosition,
    ExternalTradingOrder,
    ExternalTradingOrderFill,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
)
from .external_trading_execution_policy import (
    DEFAULT_EXECUTOR_LOT_SIZE,
    DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    DEFAULT_EXECUTOR_PRICE_LEVEL,
    DEFAULT_EXECUTOR_CLIP_SELL_TO_AVAILABLE,
    aggregate_execution_policy,
    resolve_execution_policy,
)

logger = logging.getLogger(__name__)

CHINA_TZ = ZoneInfo("Asia/Shanghai")
STRATEGY_W20 = "w20_momentum_live"
STRATEGY_SNOWBALL = "snowball_copy_live"
STRATEGY_FACTOR_LIVE = "factor_live_trading"
STRATEGY_NETTED_EXECUTOR = "netted_executor"
STATUS_BLOCKED_INSUFFICIENT_SELLABLE = "BLOCKED_INSUFFICIENT_SELLABLE"
STATUS_BLOCKED_INSUFFICIENT_POSITION = "BLOCKED_INSUFFICIENT_POSITION"
STATUS_BLOCKED_NON_RETRYABLE_REJECTION = "BLOCKED_NON_RETRYABLE_REJECTION"
BLOCKED_ORDER_STATUSES = {
    STATUS_BLOCKED_INSUFFICIENT_SELLABLE,
    STATUS_BLOCKED_INSUFFICIENT_POSITION,
    STATUS_BLOCKED_NON_RETRYABLE_REJECTION,
}
ACTIVE_ORDER_STATUSES = {"CREATED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING"}
TERMINAL_ORDER_STATUSES = {
    "FILLED",
    "CANCELED",
    "PARTIALLY_CANCELED",
    "REJECTED",
    "NOT_SUPPORTED",
    "FAILED",
    "EXPIRED",
    STATUS_BLOCKED_INSUFFICIENT_SELLABLE,
    STATUS_BLOCKED_INSUFFICIENT_POSITION,
    STATUS_BLOCKED_NON_RETRYABLE_REJECTION,
}
PASSIVE_CHILD_ORDER_STATUSES = {"CREATED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING"}

PTRADE_STATUS_MAP = {
    "0": "SUBMITTED",
    "1": "SUBMITTED",
    "2": "ACKNOWLEDGED",
    "3": "CANCEL_PENDING",
    "4": "CANCEL_PENDING",
    "5": "PARTIALLY_CANCELED",
    "6": "CANCELED",
    "7": "PARTIALLY_FILLED",
    "8": "FILLED",
    "9": "FAILED",
    "+": "ACKNOWLEDGED",
    "-": "FAILED",
    "V": "ACKNOWLEDGED",
}
PTRADE_ORDER_FILL_STATUSES = {"4", "5", "7", "8"}
A_SHARE_ETF_PREFIXES = ("15", "50", "51", "52", "56", "58")
A_SHARE_T0_ETF_PREFIXES = ("511", "513", "518", "520")
A_SHARE_T0_ETF_NAME_KEYWORDS = (
    "货币",
    "现金",
    "债",
    "国债",
    "信用",
    "黄金",
    "商品",
    "纳指",
    "标普",
    "恒生",
    "港股",
    "港",
    "日经",
    "海外",
    "跨境",
    "QDII",
)


def normalize_symbol(symbol: Optional[str]) -> Optional[str]:
    if not symbol:
        return None
    text = str(symbol).strip().upper()
    if not text:
        return None
    if "." not in text and len(text) == 6 and text.isdigit():
        if text.startswith(("60", "68", "51", "52", "56", "58", "50", "11")):
            return f"{text}.SH"
        if text.startswith(("00", "30", "20", "15", "12", "13")):
            return f"{text}.SZ"
        if text.startswith(("43", "83", "87", "88", "92")):
            return f"{text}.BJ"
    parts = text.split(".")
    if len(parts) != 2:
        return text
    first, second = parts
    if first in {"SH", "SS", "XSHG", "SZ", "XSHE", "BJ", "XBSE"}:
        market = {"SS": "SH", "XSHG": "SH", "XSHE": "SZ", "XBSE": "BJ"}.get(first, first)
        return f"{parts[1]}.{market}"
    market = {"SS": "SH", "XSHG": "SH", "XSHE": "SZ", "XBSE": "BJ"}.get(second, second)
    return f"{parts[0]}.{market}"


def _normalized_symbol_parts(symbol: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    normalized = normalize_symbol(symbol)
    if not normalized or "." not in normalized:
        return normalized, None
    code, market = normalized.split(".", 1)
    return code, market


def _configured_t0_symbols() -> set:
    return {
        normalize_symbol(item)
        for item in os.getenv("EXTERNAL_TRADING_T0_SYMBOLS", "").split(",")
        if normalize_symbol(item)
    }


def is_convertible_bond_symbol(symbol: Optional[str]) -> bool:
    code, market = _normalized_symbol_parts(symbol)
    if not code or market not in {"SH", "SZ"} or not code.isdigit() or len(code) != 6:
        return False
    return code.startswith(("11", "12"))


def is_a_share_etf_symbol(symbol: Optional[str]) -> bool:
    code, market = _normalized_symbol_parts(symbol)
    if not code or market not in {"SH", "SZ"} or not code.isdigit() or len(code) != 6:
        return False
    return code.startswith(A_SHARE_ETF_PREFIXES)


def _a_share_etf_name(symbol: Optional[str]) -> Optional[str]:
    try:
        from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_NAMES

        return A_STOCK_ETF_DAILY_NAMES.get(normalize_symbol(symbol))
    except Exception:
        return None


def is_t0_etf_symbol(symbol: Optional[str]) -> bool:
    normalized = normalize_symbol(symbol)
    code, market = _normalized_symbol_parts(normalized)
    if not normalized or not code or market not in {"SH", "SZ"} or not is_a_share_etf_symbol(normalized):
        return False
    if normalized in _configured_t0_symbols():
        return True
    if market == "SH" and code.startswith(A_SHARE_T0_ETF_PREFIXES):
        return True
    name = _a_share_etf_name(normalized)
    if name and any(keyword.upper() in str(name).upper() for keyword in A_SHARE_T0_ETF_NAME_KEYWORDS):
        return True
    return False


def sellable_rule_for_symbol(symbol: Optional[str]) -> Dict[str, Any]:
    if is_convertible_bond_symbol(symbol):
        return {"settlement_rule": "T+0", "security_type": "CONVERTIBLE_BOND"}
    if is_t0_etf_symbol(symbol):
        return {"settlement_rule": "T+0", "security_type": "ETF"}
    if is_a_share_etf_symbol(symbol):
        return {"settlement_rule": "T+1", "security_type": "ETF"}
    code, market = _normalized_symbol_parts(symbol)
    if code and market in {"SH", "SZ", "BJ"} and code.isdigit() and len(code) == 6:
        return {"settlement_rule": "T+1", "security_type": "A_SHARE_STOCK"}
    return {"settlement_rule": "T+0", "security_type": "OTHER"}


def compute_sellability(
    symbol: Optional[str],
    *,
    quantity: Any,
    available_quantity: Any,
    today_buy_quantity: Any = 0,
) -> Dict[str, Any]:
    total_quantity = max(safe_int(quantity), 0)
    base_available_quantity = min(max(safe_int(available_quantity, total_quantity), 0), total_quantity)
    today_buy = max(safe_int(today_buy_quantity), 0)
    rule = sellable_rule_for_symbol(symbol)
    t1_locked_quantity = 0
    if rule["settlement_rule"] == "T+1":
        t1_locked_quantity = min(today_buy, base_available_quantity)
    computed_sellable_quantity = max(base_available_quantity - t1_locked_quantity, 0)
    return {
        "computed_sellable_quantity": computed_sellable_quantity,
        "sellable_quantity": computed_sellable_quantity,
        "t1_locked_quantity": t1_locked_quantity,
        "today_buy_quantity": today_buy,
        "sellable_rule": rule["settlement_rule"],
        "sellable_security_type": rule["security_type"],
    }


def compute_position_sellability(
    row: ExternalTradingLedgerPosition,
    today_buy_quantity: Any = 0,
) -> Dict[str, Any]:
    return compute_sellability(
        row.symbol,
        quantity=row.quantity,
        available_quantity=getattr(row, "available_quantity", row.quantity),
        today_buy_quantity=today_buy_quantity,
    )


def _china_today() -> date:
    return datetime.now(CHINA_TZ).date()


def get_today_buy_quantities(
    db: Session,
    sub_account_ids: List[int],
    *,
    as_of_date: Optional[date] = None,
) -> Dict[Tuple[int, str], int]:
    ids = [safe_int(item) for item in dict.fromkeys(sub_account_ids or []) if safe_int(item) > 0]
    if not ids:
        return {}
    trade_date = as_of_date or _china_today()
    start_dt = datetime.combine(trade_date, datetime.min.time())
    end_dt = start_dt + timedelta(days=1)
    rows = (
        db.query(ExternalTradingOrderFill)
        .filter(
            ExternalTradingOrderFill.sub_account_id.in_(ids),
            ExternalTradingOrderFill.side == "BUY",
            or_(
                and_(
                    ExternalTradingOrderFill.traded_at >= start_dt,
                    ExternalTradingOrderFill.traded_at < end_dt,
                ),
                and_(
                    ExternalTradingOrderFill.traded_at.is_(None),
                    ExternalTradingOrderFill.created_at >= start_dt,
                    ExternalTradingOrderFill.created_at < end_dt,
                ),
            ),
        )
        .all()
    )
    quantities: Dict[Tuple[int, str], int] = {}
    for row in rows:
        symbol = normalize_symbol(row.symbol)
        sub_account_id = safe_int(row.sub_account_id)
        if not symbol or sub_account_id <= 0:
            continue
        key = (sub_account_id, symbol)
        quantities[key] = quantities.get(key, 0) + max(safe_int(row.quantity), 0)
    return quantities


def is_star_market_symbol(symbol: Optional[str]) -> bool:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return False
    parts = normalized.split(".")
    return len(parts) == 2 and parts[1] == "SH" and parts[0].startswith(("688", "689"))

def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def round_money(value: Any) -> float:
    return round(safe_float(value), 2)


def _estimate_fee_totals(account: Optional[ExternalTradingAccount], side: str, cumulative_amount: float) -> Dict[str, float]:
    amount = max(safe_float(cumulative_amount), 0.0)
    if amount <= 0:
        return {"commission": 0.0, "stamp_tax": 0.0, "fee_total": 0.0}

    commission_rate_pct = safe_float(getattr(account, "commission_rate_pct", 0.025), 0.025)
    min_commission = safe_float(getattr(account, "min_commission", 5.0), 5.0)
    stamp_tax_rate_pct = safe_float(getattr(account, "stamp_tax_rate_pct", 0.05), 0.05)

    commission = amount * commission_rate_pct / 100.0 if commission_rate_pct > 0 else 0.0
    if min_commission > 0 and (commission > 0 or commission_rate_pct > 0):
        commission = max(commission, min_commission)
    stamp_tax = amount * stamp_tax_rate_pct / 100.0 if str(side or "").upper() == "SELL" and stamp_tax_rate_pct > 0 else 0.0
    commission = round_money(commission)
    stamp_tax = round_money(stamp_tax)
    return {
        "commission": commission,
        "stamp_tax": stamp_tax,
        "fee_total": round_money(commission + stamp_tax),
    }


def _estimated_fee_increment_for_order(db: Session, order: ExternalTradingOrder, fill_amount: float) -> Dict[str, float]:
    account = db.query(ExternalTradingAccount).filter(ExternalTradingAccount.id == order.external_trading_account_id).first()
    previous_amount, previous_commission, previous_stamp_tax, previous_fee_total = _order_fill_money_totals(db, order.id)
    target_totals = _estimate_fee_totals(account, order.side, previous_amount + safe_float(fill_amount))
    commission = max(target_totals["commission"] - previous_commission, 0.0)
    stamp_tax = max(target_totals["stamp_tax"] - previous_stamp_tax, 0.0)
    fee_total = max(target_totals["fee_total"] - previous_fee_total, 0.0)
    return {
        "commission": round_money(commission),
        "stamp_tax": round_money(stamp_tax),
        "fee_total": round_money(fee_total),
    }


def _allocate_money(total: float, weights: List[float]) -> List[float]:
    total = round_money(total)
    if not weights:
        return []
    positive_total = sum(max(safe_float(weight), 0.0) for weight in weights)
    if positive_total <= 0:
        return [0.0 for _ in weights]
    allocations = []
    allocated = 0.0
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            value = round_money(total - allocated)
        else:
            value = round_money(total * max(safe_float(weight), 0.0) / positive_total)
            allocated = round_money(allocated + value)
        allocations.append(value)
    return allocations


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text_value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            if fmt == "%Y-%m-%d":
                candidate = text_value[:10]
            elif fmt == "%Y%m%d":
                candidate = text_value[:8]
            else:
                candidate = text_value[:10]
            return datetime.strptime(candidate, fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text_value).date()
    except Exception:
        return None


def _first_present_value(obj: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in obj and obj.get(key) not in (None, ""):
            return obj.get(key)
    return default


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text_value, fmt)
            if fmt == "%H:%M:%S":
                now = datetime.now()
                return parsed.replace(year=now.year, month=now.month, day=now.day)
            return parsed
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text_value)
    except Exception:
        return None


def submission_result_retryable(value: Any) -> Optional[bool]:
    if not isinstance(value, dict) or "retryable" not in value:
        return None
    retryable = value.get("retryable")
    if retryable is None:
        return None
    if isinstance(retryable, str):
        lowered = retryable.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return bool(retryable)


def ptrade_status_to_lifecycle(raw_status: Any, filled_quantity: int = 0, quantity: int = 0) -> str:
    raw = "" if raw_status is None else str(raw_status)
    mapped = PTRADE_STATUS_MAP.get(raw)
    if mapped in {"SUBMITTED", "ACKNOWLEDGED"}:
        if quantity > 0 and filled_quantity >= quantity:
            return "FILLED"
        if filled_quantity > 0:
            return "PARTIALLY_FILLED"
        return mapped
    if mapped:
        return mapped
    if quantity > 0 and filled_quantity >= quantity:
        return "FILLED"
    if filled_quantity > 0:
        return "PARTIALLY_FILLED"
    return "ACKNOWLEDGED"


def order_event_filled_quantity(payload: Dict[str, Any], raw_status: Any, current: int = 0) -> int:
    raw = "" if raw_status is None else str(raw_status)
    current_quantity = safe_int(current)
    reported_value = payload.get("filled", None)
    if reported_value is not None:
        reported = abs(safe_int(reported_value, current_quantity))
        return max(current_quantity, reported)
    if raw not in PTRADE_ORDER_FILL_STATUSES:
        return current_quantity
    reported_value = payload.get(
        "filled_quantity",
        payload.get("business_amount", payload.get("filled_amount")),
    )
    reported = abs(safe_int(reported_value, current_quantity))
    return max(current_quantity, reported)


def merge_lifecycle_status(current_status: Optional[str], incoming_status: str) -> str:
    current = str(current_status or "").upper()
    incoming = str(incoming_status or "").upper()
    if current == "FILLED":
        return current
    if current == "PARTIALLY_FILLED" and incoming in {"CREATED", "SUBMITTED", "ACKNOWLEDGED"}:
        return current
    if current in TERMINAL_ORDER_STATUSES and incoming in ACTIVE_ORDER_STATUSES:
        return current
    return incoming


def ensure_strategy_sub_account(
    db: Session,
    *,
    account_id: str,
    external_trading_account_id: int,
    strategy_type: str,
    strategy_config_id: int,
    name: str,
    cash_allocated: float,
) -> ExternalTradingSubAccount:
    sub_account = (
        db.query(ExternalTradingSubAccount)
        .filter(
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.external_trading_account_id == external_trading_account_id,
            ExternalTradingSubAccount.strategy_type == strategy_type,
            ExternalTradingSubAccount.strategy_config_id == strategy_config_id,
        )
        .first()
    )
    now = datetime.now()
    if sub_account:
        sub_account.name = name
        sub_account.cash_allocated = float(cash_allocated or 0)
        if not sub_account.cash_available and float(cash_allocated or 0) > 0:
            sub_account.cash_available = float(cash_allocated or 0)
        sub_account.enabled = True
        sub_account.updated_at = now
        return sub_account

    sub_account = ExternalTradingSubAccount(
        account_id=account_id,
        external_trading_account_id=external_trading_account_id,
        name=name,
        strategy_type=strategy_type,
        strategy_config_id=strategy_config_id,
        cash_allocated=float(cash_allocated or 0),
        cash_available=float(cash_allocated or 0),
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(sub_account)
    db.flush()
    return sub_account


def serialize_sub_account(sub_account: Optional[ExternalTradingSubAccount]) -> Optional[Dict[str, Any]]:
    if not sub_account:
        return None
    return {
        "id": sub_account.id,
        "account_id": sub_account.account_id,
        "external_trading_account_id": sub_account.external_trading_account_id,
        "name": sub_account.name,
        "strategy_type": sub_account.strategy_type,
        "strategy_config_id": sub_account.strategy_config_id,
        "cash_allocated": sub_account.cash_allocated,
        "cash_available": sub_account.cash_available,
        "enabled": sub_account.enabled,
        "executor_price_level": sub_account.executor_price_level,
        "executor_lot_size": sub_account.executor_lot_size,
        "executor_order_timeout_seconds": sub_account.executor_order_timeout_seconds,
        "executor_max_replace_count": sub_account.executor_max_replace_count,
        "executor_max_slippage_pct": sub_account.executor_max_slippage_pct,
        "executor_clip_sell_to_available": sub_account.executor_clip_sell_to_available,
        "executor_price_level_sequence": sub_account.executor_price_level_sequence,
        "remark": sub_account.remark,
        "created_at": sub_account.created_at.isoformat() if sub_account.created_at else None,
        "updated_at": sub_account.updated_at.isoformat() if sub_account.updated_at else None,
    }


def get_ledger_positions(db: Session, sub_account_id: Optional[int]) -> Dict[str, ExternalTradingLedgerPosition]:
    if not sub_account_id:
        return {}
    rows = (
        db.query(ExternalTradingLedgerPosition)
        .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account_id)
        .all()
    )
    return {normalize_symbol(row.symbol): row for row in rows if row.symbol}


def get_account_ledger_positions(db: Session, external_trading_account_id: int, account_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    query = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.external_trading_account_id == external_trading_account_id,
    )
    if account_id:
        query = query.filter(ExternalTradingSubAccount.account_id == account_id)
    sub_account_ids = [safe_int(row.id) for row in query.all() if safe_int(row.id) > 0]
    if not sub_account_ids:
        return {}

    today_buy_by_key = get_today_buy_quantities(db, sub_account_ids)
    rows = (
        db.query(ExternalTradingLedgerPosition)
        .filter(ExternalTradingLedgerPosition.sub_account_id.in_(sub_account_ids))
        .all()
    )
    aggregated: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        symbol = normalize_symbol(row.symbol)
        if not symbol:
            continue
        bucket = aggregated.setdefault(symbol, {
            "symbol": symbol,
            "quantity": 0,
            "available_quantity": 0,
            "raw_available_quantity": 0,
            "computed_sellable_quantity": 0,
            "sellable_quantity": 0,
            "t1_locked_quantity": 0,
            "today_buy_quantity": 0,
            "sellable_rule": None,
            "sellable_security_type": None,
            "_avg_cost_notional": 0.0,
            "_market_price_notional": 0.0,
            "_market_price_weight": 0,
            "market_value": 0.0,
            "realized_pnl": 0.0,
        })
        quantity = safe_int(row.quantity)
        available_quantity = safe_int(getattr(row, "available_quantity", row.quantity), quantity)
        sellability = compute_position_sellability(
            row,
            today_buy_by_key.get((safe_int(row.sub_account_id), symbol), 0),
        )
        bucket["quantity"] += quantity
        bucket["raw_available_quantity"] += available_quantity
        bucket["computed_sellable_quantity"] += safe_int(sellability.get("computed_sellable_quantity"))
        bucket["sellable_quantity"] += safe_int(sellability.get("sellable_quantity"))
        bucket["available_quantity"] += safe_int(sellability.get("computed_sellable_quantity"))
        bucket["t1_locked_quantity"] += safe_int(sellability.get("t1_locked_quantity"))
        bucket["today_buy_quantity"] += safe_int(sellability.get("today_buy_quantity"))
        bucket["sellable_rule"] = bucket["sellable_rule"] or sellability.get("sellable_rule")
        bucket["sellable_security_type"] = bucket["sellable_security_type"] or sellability.get("sellable_security_type")
        bucket["realized_pnl"] = round_money(bucket["realized_pnl"] + safe_float(row.realized_pnl))
        bucket["market_value"] = round_money(bucket["market_value"] + safe_float(row.market_value))
        bucket["_avg_cost_notional"] += safe_float(row.avg_cost) * quantity
        market_price = safe_float(row.market_price)
        if market_price > 0 and quantity > 0:
            bucket["_market_price_notional"] += market_price * quantity
            bucket["_market_price_weight"] += quantity

    for bucket in aggregated.values():
        quantity = safe_int(bucket.get("quantity"))
        bucket["avg_cost"] = round_money(bucket.pop("_avg_cost_notional", 0.0) / quantity) if quantity > 0 else 0.0
        price_weight = safe_int(bucket.pop("_market_price_weight", 0))
        bucket["market_price"] = round_money(bucket.pop("_market_price_notional", 0.0) / price_weight) if price_weight > 0 else None
    return aggregated


def serialize_ledger_position(
    row: ExternalTradingLedgerPosition,
    *,
    today_buy_quantity: Any = 0,
) -> Dict[str, Any]:
    item = {
        "id": row.id,
        "sub_account_id": row.sub_account_id,
        "symbol": row.symbol,
        "quantity": row.quantity,
        "available_quantity": row.available_quantity,
        "raw_available_quantity": row.available_quantity,
        "avg_cost": row.avg_cost,
        "market_price": row.market_price,
        "market_value": row.market_value,
        "realized_pnl": row.realized_pnl,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    sellability = compute_position_sellability(row, today_buy_quantity)
    item.update(sellability)
    item["available_quantity"] = safe_int(sellability.get("computed_sellable_quantity"))
    return item


def _normalize_broker_position(position: Dict[str, Any]) -> Dict[str, Any]:
    symbol = normalize_symbol(position.get("symbol") or position.get("client_symbol"))
    quantity = safe_int(position.get("quantity", position.get("amount")))
    available_quantity = safe_int(position.get("available_quantity", position.get("enable_amount", quantity)), quantity)
    cost_price = safe_float(position.get("cost_price", position.get("cost_basis")))
    last_price = safe_float(position.get("last_price"))
    market_value = safe_float(position.get("market_value"))
    if market_value <= 0 and quantity > 0 and last_price > 0:
        market_value = round_money(quantity * last_price)
    if market_value <= 0 and quantity > 0 and cost_price > 0:
        market_value = round_money(quantity * cost_price)
    return {
        "symbol": symbol,
        "client_symbol": position.get("client_symbol"),
        "quantity": quantity,
        "available_quantity": available_quantity,
        "cost_price": cost_price,
        "last_price": last_price if last_price > 0 else None,
        "market_value": round_money(market_value),
        "profit": round_money(position.get("profit")),
        "profit_ratio": safe_float(position.get("profit_ratio")),
    }


def persist_broker_position_snapshot(
    db: Session,
    *,
    account: ExternalTradingAccount,
    payload: Dict[str, Any],
    snapshot_source: str,
    snapshot_kind: Optional[str] = None,
    market_window_open: bool = False,
    snapshot_at: Optional[datetime] = None,
    status: str = "SUCCESS",
    message: Optional[str] = None,
) -> ExternalTradingBrokerPositionSnapshot:
    raw_positions = payload.get("positions") if isinstance(payload, dict) else []
    if isinstance(raw_positions, dict):
        raw_positions = list(raw_positions.values())
    normalized_positions = [
        _normalize_broker_position(position)
        for position in (raw_positions or [])
        if normalize_symbol((position or {}).get("symbol") or (position or {}).get("client_symbol"))
    ]
    if snapshot_at is None:
        candidate = payload.get("current_time") if isinstance(payload, dict) else None
        snapshot_at = parse_dt(candidate) or datetime.now()
    snapshot_at = snapshot_at.replace(microsecond=0)
    snapshot_date = snapshot_at.date()
    position_count = len(normalized_positions)
    total_market_value = round_money(sum(safe_float(row.get("market_value")) for row in normalized_positions))
    total_available_market_value = round_money(
        sum(safe_float(row.get("market_value")) * safe_int(row.get("available_quantity")) / safe_int(row.get("quantity"))
            if safe_int(row.get("quantity")) > 0 else 0.0
            for row in normalized_positions)
    )
    row = ExternalTradingBrokerPositionSnapshot(
        account_id=account.account_id,
        external_trading_account_id=account.id,
        snapshot_date=snapshot_date,
        snapshot_at=snapshot_at,
        snapshot_source=snapshot_source,
        snapshot_kind=snapshot_kind,
        market_window_open=bool(market_window_open),
        position_count=position_count,
        total_market_value=total_market_value,
        total_available_market_value=total_available_market_value,
        positions=normalized_positions,
        raw_payload=payload,
        status=status,
        message=message,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def get_latest_broker_position_snapshot(
    db: Session,
    *,
    external_trading_account_id: int,
    account_id: Optional[str] = None,
) -> Optional[ExternalTradingBrokerPositionSnapshot]:
    query = db.query(ExternalTradingBrokerPositionSnapshot).filter(
        ExternalTradingBrokerPositionSnapshot.external_trading_account_id == external_trading_account_id,
    )
    if account_id:
        query = query.filter(ExternalTradingBrokerPositionSnapshot.account_id == account_id)
    return query.order_by(ExternalTradingBrokerPositionSnapshot.snapshot_at.desc(), ExternalTradingBrokerPositionSnapshot.id.desc()).first()


def serialize_broker_position_snapshot(row: ExternalTradingBrokerPositionSnapshot) -> Dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "external_trading_account_id": row.external_trading_account_id,
        "snapshot_date": row.snapshot_date.isoformat() if row.snapshot_date else None,
        "snapshot_at": row.snapshot_at.isoformat() if row.snapshot_at else None,
        "snapshot_source": row.snapshot_source,
        "snapshot_kind": row.snapshot_kind,
        "market_window_open": row.market_window_open,
        "position_count": row.position_count,
        "total_market_value": row.total_market_value,
        "total_available_market_value": row.total_available_market_value,
        "positions": row.positions or [],
        "status": row.status,
        "message": row.message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def build_broker_position_diff(
    snapshot: Optional[ExternalTradingBrokerPositionSnapshot],
    ledger_positions: Dict[str, Dict[str, Any]],
    target_positions: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    target_positions = target_positions or {}
    broker_positions = {}
    for row in (snapshot.positions if snapshot else []) or []:
        symbol = normalize_symbol(row.get("symbol") or row.get("client_symbol"))
        if not symbol:
            continue
        broker_positions[symbol] = {
            "symbol": symbol,
            "client_symbol": row.get("client_symbol"),
            "quantity": safe_int(row.get("quantity")),
            "available_quantity": safe_int(row.get("available_quantity")),
            "cost_price": safe_float(row.get("cost_price")),
            "last_price": safe_float(row.get("last_price")),
            "market_value": round_money(row.get("market_value")),
        }

    symbols = sorted(set(broker_positions.keys()) | set(ledger_positions.keys()) | set(target_positions.keys()))
    rows = []
    broker_total = 0.0
    ledger_total = 0.0
    quantity_diff_total = 0
    available_quantity_diff_total = 0
    sellable_quantity_diff_total = 0
    matched_count = 0
    mismatch_count = 0
    broker_only_count = 0
    ledger_only_count = 0
    for symbol in symbols:
        broker_row = broker_positions.get(symbol, {})
        ledger_row = ledger_positions.get(symbol, {})
        broker_quantity = safe_int(broker_row.get("quantity"))
        ledger_quantity = safe_int(ledger_row.get("quantity"))
        ledger_target_quantity = safe_int(target_positions.get(symbol))
        broker_available_quantity = safe_int(broker_row.get("available_quantity"))
        ledger_available_quantity = safe_int(ledger_row.get("available_quantity"))
        ledger_computed_sellable_quantity = safe_int(
            ledger_row.get("computed_sellable_quantity", ledger_available_quantity)
        )
        if (
            broker_quantity == 0
            and ledger_quantity == 0
            and ledger_target_quantity == 0
            and broker_available_quantity == 0
            and ledger_computed_sellable_quantity == 0
            and round_money(broker_row.get("market_value")) == 0
            and round_money(ledger_row.get("market_value")) == 0
        ):
            continue
        quantity_diff = broker_quantity - ledger_quantity
        available_quantity_diff = broker_available_quantity - ledger_computed_sellable_quantity
        sellable_quantity_diff = available_quantity_diff
        broker_market_value = round_money(broker_row.get("market_value"))
        ledger_market_value = round_money(ledger_row.get("market_value"))
        market_value_diff = round_money(broker_market_value - ledger_market_value)
        if (
            broker_quantity == ledger_quantity
            and ledger_quantity == ledger_target_quantity
            and broker_available_quantity == ledger_computed_sellable_quantity
        ):
            diff_status = "MATCH"
            matched_count += 1
        elif broker_quantity > 0 and ledger_quantity <= 0:
            diff_status = "BROKER_ONLY"
            broker_only_count += 1
        elif ledger_quantity > 0 and broker_quantity <= 0:
            diff_status = "LEDGER_ONLY"
            ledger_only_count += 1
        else:
            diff_status = "MISMATCH"
            mismatch_count += 1
        broker_total += broker_market_value
        ledger_total += ledger_market_value
        quantity_diff_total += quantity_diff
        available_quantity_diff_total += available_quantity_diff
        sellable_quantity_diff_total += sellable_quantity_diff
        rows.append({
            "symbol": symbol,
            "client_symbol": broker_row.get("client_symbol"),
            "broker_quantity": broker_quantity,
            "broker_available_quantity": broker_available_quantity,
            "broker_sellable_quantity": broker_available_quantity,
            "broker_market_value": broker_market_value,
            "ledger_quantity": ledger_quantity,
            "ledger_target_quantity": ledger_target_quantity,
            "ledger_available_quantity": ledger_available_quantity,
            "ledger_raw_available_quantity": safe_int(ledger_row.get("raw_available_quantity", ledger_available_quantity)),
            "ledger_computed_sellable_quantity": ledger_computed_sellable_quantity,
            "ledger_sellable_quantity": ledger_computed_sellable_quantity,
            "ledger_t1_locked_quantity": safe_int(ledger_row.get("t1_locked_quantity")),
            "ledger_today_buy_quantity": safe_int(ledger_row.get("today_buy_quantity")),
            "ledger_sellable_rule": ledger_row.get("sellable_rule"),
            "ledger_sellable_security_type": ledger_row.get("sellable_security_type"),
            "ledger_market_value": ledger_market_value,
            "quantity_diff": quantity_diff,
            "available_quantity_diff": available_quantity_diff,
            "sellable_quantity_diff": sellable_quantity_diff,
            "market_value_diff": market_value_diff,
            "diff_status": diff_status,
            "broker": broker_row,
            "ledger": ledger_row,
        })

    return {
        "rows": rows,
        "summary": {
            "symbol_count": len(symbols),
            "matched_count": matched_count,
            "mismatch_count": mismatch_count,
            "broker_only_count": broker_only_count,
            "ledger_only_count": ledger_only_count,
            "quantity_diff_total": quantity_diff_total,
            "available_quantity_diff_total": available_quantity_diff_total,
            "sellable_quantity_diff_total": sellable_quantity_diff_total,
            "broker_market_value_total": round_money(broker_total),
            "ledger_market_value_total": round_money(ledger_total),
            "market_value_diff_total": round_money(broker_total - ledger_total),
        },
    }


def sync_target_positions(
    db: Session,
    *,
    sub_account: ExternalTradingSubAccount,
    targets: List[Dict[str, Any]],
    signal_id: Optional[str] = None,
    signal_version: Optional[str] = None,
    source_execution_id: Optional[int] = None,
) -> None:
    now = datetime.now()
    target_symbols = set()
    for target in targets:
        symbol = normalize_symbol(target.get("symbol"))
        if not symbol:
            continue
        target_symbols.add(symbol)
        row = (
            db.query(ExternalTradingTargetPosition)
            .filter(
                ExternalTradingTargetPosition.sub_account_id == sub_account.id,
                ExternalTradingTargetPosition.symbol == symbol,
            )
            .first()
        )
        if not row:
            row = ExternalTradingTargetPosition(
                account_id=sub_account.account_id,
                external_trading_account_id=sub_account.external_trading_account_id,
                sub_account_id=sub_account.id,
                strategy_type=sub_account.strategy_type,
                strategy_config_id=sub_account.strategy_config_id,
                symbol=symbol,
                created_at=now,
            )
            db.add(row)
        row.target_quantity = safe_int(target.get("target_quantity"))
        row.target_weight_pct = safe_float(target.get("target_weight_pct"), None)
        row.target_value = safe_float(target.get("target_value"), None)
        row.reference_price = safe_float(target.get("reference_price"), None)
        row.reference_price_source = target.get("reference_price_source")
        row.signal_id = signal_id
        row.signal_version = signal_version
        row.source_execution_id = source_execution_id
        row.status = "ACTIVE"
        row.updated_at = now

    stale_rows = (
        db.query(ExternalTradingTargetPosition)
        .filter(ExternalTradingTargetPosition.sub_account_id == sub_account.id)
        .all()
    )
    for row in stale_rows:
        if row.symbol not in target_symbols:
            row.target_quantity = 0
            row.target_weight_pct = 0
            row.target_value = 0
            row.reference_price = None
            row.reference_price_source = None
            row.signal_id = signal_id
            row.signal_version = signal_version
            row.source_execution_id = source_execution_id
            row.status = "ACTIVE"
            row.updated_at = now


def get_open_order_quantities(db: Session, sub_account_id: Optional[int]) -> Dict[str, Dict[str, int]]:
    if not sub_account_id:
        return {}
    rows = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.sub_account_id == sub_account_id,
            ExternalTradingOrder.status.in_(list(ACTIVE_ORDER_STATUSES)),
        )
        .all()
    )
    result: Dict[str, Dict[str, int]] = {}
    for row in rows:
        symbol = normalize_symbol(row.symbol)
        if not symbol:
            continue
        bucket = result.setdefault(symbol, {"BUY": 0, "SELL": 0})
        remaining = max(safe_int(row.remaining_quantity, row.quantity - row.filled_quantity), 0)
        if row.side in ("BUY", "SELL"):
            bucket[row.side] += remaining
    return result


def create_execution_orders(
    db: Session,
    *,
    account_id: str,
    external_trading_account_id: int,
    sub_account_id: Optional[int],
    strategy_type: Optional[str],
    strategy_config_id: Optional[int],
    execution_id: Optional[int],
    orders: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    now = datetime.now()
    for order in orders:
        client_order_id = uuid.uuid4().hex
        symbol = normalize_symbol(order.get("symbol"))
        quantity = safe_int(order.get("quantity"))
        side = str(order.get("side") or "").upper()
        order_type = str(order.get("order_type") or "LIMIT").upper()
        row = ExternalTradingOrder(
            account_id=account_id,
            external_trading_account_id=external_trading_account_id,
            sub_account_id=sub_account_id,
            strategy_type=strategy_type,
            strategy_config_id=strategy_config_id,
            execution_id=execution_id,
            allocation_role="DIRECT",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price_level=order.get("price_level"),
            quantity=quantity,
            filled_quantity=0,
            remaining_quantity=quantity,
            status="CREATED",
            raw_request=order,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        enriched_order = dict(order)
        enriched_order["client_order_id"] = client_order_id
        enriched_order["sub_account_id"] = sub_account_id
        enriched.append(enriched_order)
    db.flush()
    return enriched


def _role(row: ExternalTradingOrder) -> str:
    return str(getattr(row, "allocation_role", None) or "DIRECT").upper()


def _child_orders(db: Session, parent_order_id: Optional[int]) -> List[ExternalTradingOrder]:
    if not parent_order_id:
        return []
    return (
        db.query(ExternalTradingOrder)
        .filter(ExternalTradingOrder.parent_order_id == parent_order_id)
        .order_by(ExternalTradingOrder.id.asc())
        .all()
    )


def _pick_event_order(candidates: List[ExternalTradingOrder]) -> Optional[ExternalTradingOrder]:
    if not candidates:
        return None
    role_rank = {"PARENT": 0, "DIRECT": 1, "INTERNAL": 1, "CHILD": 2}
    return sorted(candidates, key=lambda row: (role_rank.get(_role(row), 3), row.id or 0))[0]


def _propagate_parent_order_state(db: Session, parent: ExternalTradingOrder) -> None:
    if _role(parent) != "PARENT":
        return
    children = _child_orders(db, parent.id)
    if not children:
        return
    now = datetime.now()
    parent_status = parent.status
    parent_terminal = parent_status in TERMINAL_ORDER_STATUSES
    for child in children:
        child.broker_order_id = parent.broker_order_id or child.broker_order_id
        child.entrust_no = parent.entrust_no or child.entrust_no
        child.ptrade_status = parent.ptrade_status or child.ptrade_status
        child.submitted_price = parent.submitted_price or child.submitted_price
        child.submitted_at = parent.submitted_at or child.submitted_at
        child.last_event_at = parent.last_event_at or child.last_event_at
        child.raw_submit_result = parent.raw_submit_result or child.raw_submit_result
        child.raw_order_event = parent.raw_order_event or child.raw_order_event
        if safe_int(child.quantity) <= 0 and child.status == "CANCELED":
            child.updated_at = now
            continue
        if parent_terminal:
            if safe_int(child.remaining_quantity) <= 0:
                child.status = "FILLED"
            elif parent_status == "FILLED":
                child.status = child.status if child.status == "PARTIALLY_FILLED" else "SUBMITTED"
            elif safe_int(child.filled_quantity) > 0 and parent_status in {"CANCELED", "PARTIALLY_CANCELED"}:
                child.status = "PARTIALLY_CANCELED"
            else:
                child.status = parent_status
        elif child.status in PASSIVE_CHILD_ORDER_STATUSES:
            child.status = parent_status
        child.updated_at = now


def _resize_child_orders_for_parent_clip(
    db: Session,
    parent: ExternalTradingOrder,
    submitted_quantity: int,
    requested_quantity: int,
) -> List[Dict[str, Any]]:
    children = _child_orders(db, parent.id)
    if not children:
        return []
    total_original = sum(max(safe_int(child.quantity), 0) for child in children)
    if submitted_quantity >= total_original or total_original <= 0:
        return []

    allocations = []
    assigned = 0
    for child in children:
        original_quantity = max(safe_int(child.quantity), 0)
        exact_quantity = submitted_quantity * original_quantity / total_original
        new_quantity = min(int(exact_quantity), original_quantity)
        allocations.append({
            "child": child,
            "original_quantity": original_quantity,
            "new_quantity": new_quantity,
            "remainder": exact_quantity - new_quantity,
        })
        assigned += new_quantity

    leftover = max(submitted_quantity - assigned, 0)
    for item in sorted(allocations, key=lambda row: row["remainder"], reverse=True):
        if leftover <= 0:
            break
        if item["new_quantity"] < item["original_quantity"]:
            item["new_quantity"] += 1
            leftover -= 1

    now = datetime.now()
    residual_allocations = []
    for item in allocations:
        child = item["child"]
        original_quantity = item["original_quantity"]
        new_quantity = item["new_quantity"]
        residual_quantity = max(original_quantity - new_quantity, 0)
        if new_quantity == original_quantity:
            continue
        child.quantity = new_quantity
        child.remaining_quantity = max(new_quantity - safe_int(child.filled_quantity), 0)
        child.raw_request = {
            **(child.raw_request or {}),
            "requested_quantity": original_quantity,
            "submitted_quantity": new_quantity,
            "quantity_clipped": True,
            "parent_requested_quantity": requested_quantity,
            "parent_submitted_quantity": submitted_quantity,
        }
        if new_quantity <= 0 and safe_int(child.filled_quantity) <= 0:
            child.status = "CANCELED"
            child.message = "父单按券商可卖数量裁剪，未分配提交数量"
        child.updated_at = now
        if residual_quantity > 0:
            residual_allocations.append({
                "child": child,
                "quantity": residual_quantity,
                "original_quantity": original_quantity,
                "submitted_quantity": new_quantity,
            })
    return residual_allocations


def _upsert_block_order(
    db: Session,
    *,
    parent: ExternalTradingOrder,
    child: ExternalTradingOrder,
    side: str,
    quantity: int,
    blocked_until: Optional[datetime],
    block_status: str,
    block_reason: str,
    block_message: str,
    raw_block: Dict[str, Any],
) -> bool:
    if quantity <= 0 or not child.sub_account_id or block_status not in BLOCKED_ORDER_STATUSES:
        return False
    now = datetime.now()
    query = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.external_trading_account_id == parent.external_trading_account_id,
            ExternalTradingOrder.sub_account_id == child.sub_account_id,
            ExternalTradingOrder.symbol == parent.symbol,
            ExternalTradingOrder.side == side,
            ExternalTradingOrder.status == block_status,
        )
    )
    if block_status == STATUS_BLOCKED_INSUFFICIENT_SELLABLE:
        query = query.filter(ExternalTradingOrder.deadline_at > now)
    existing = query.first()
    if existing:
        existing.quantity = max(safe_int(existing.quantity), quantity)
        existing.remaining_quantity = existing.quantity
        existing.signal_version = child.signal_version or parent.signal_version
        existing.cancel_reason = block_reason
        if blocked_until:
            existing.deadline_at = max(existing.deadline_at or blocked_until, blocked_until)
        existing.message = block_message
        existing.raw_request = {
            **(existing.raw_request or {}),
            **raw_block,
            "latest_block": raw_block,
        }
        existing.updated_at = now
        return False

    block_order = ExternalTradingOrder(
        account_id=child.account_id or parent.account_id,
        external_trading_account_id=parent.external_trading_account_id,
        sub_account_id=child.sub_account_id,
        strategy_type=child.strategy_type,
        strategy_config_id=child.strategy_config_id,
        execution_id=child.execution_id,
        allocation_role="BLOCK",
        client_order_id=uuid.uuid4().hex,
        symbol=parent.symbol,
        side=side,
        order_type="BLOCK",
        price_level=parent.price_level,
        signal_version=child.signal_version or parent.signal_version,
        replace_count=parent.replace_count,
        deadline_at=blocked_until,
        cancel_reason=block_reason,
        quantity=quantity,
        filled_quantity=0,
        remaining_quantity=quantity,
        status=block_status,
        message=block_message,
        raw_request=raw_block,
        created_at=now,
        updated_at=now,
    )
    db.add(block_order)
    return True


def _create_quantity_clip_block_orders(
    db: Session,
    *,
    parent: ExternalTradingOrder,
    residual_allocations: List[Dict[str, Any]],
    blocked_until: Optional[datetime],
    submit_result: Dict[str, Any],
    block_status: str,
    block_reason: str,
    block_message: str,
) -> int:
    if parent.side != "SELL" or block_status not in BLOCKED_ORDER_STATUSES:
        return 0
    created = 0
    for allocation in residual_allocations:
        child = allocation.get("child")
        if not child:
            continue
        quantity = safe_int(allocation.get("quantity"))
        if quantity <= 0 or not child.sub_account_id:
            continue

        raw_block = {
            "reason": block_reason,
            "source_parent_order_id": parent.id,
            "source_parent_client_order_id": parent.client_order_id,
            "source_child_order_id": child.id,
            "requested_quantity": allocation.get("original_quantity"),
            "submitted_quantity": allocation.get("submitted_quantity"),
            "blocked_quantity": quantity,
            "sellable_quantity": submit_result.get("sellable_quantity"),
            "position_quantity": submit_result.get("position_quantity"),
            "blocked_until": blocked_until.isoformat() if blocked_until else None,
            "submit_result": submit_result,
        }
        created += int(_upsert_block_order(
            db,
            parent=parent,
            child=child,
            side="SELL",
            quantity=quantity,
            blocked_until=blocked_until,
            block_status=block_status,
            block_reason=block_reason,
            block_message=block_message,
            raw_block=raw_block,
        ))
    return created


def _create_non_retryable_rejection_blocks(
    db: Session,
    *,
    parent: ExternalTradingOrder,
    submit_result: Dict[str, Any],
) -> int:
    if _role(parent) != "PARENT" or parent.strategy_type != STRATEGY_NETTED_EXECUTOR:
        return 0
    children = _child_orders(db, parent.id)
    if not children:
        return 0

    block_status = STATUS_BLOCKED_NON_RETRYABLE_REJECTION
    block_reason = str(submit_result.get("error_code") or "non_retryable_rejection").strip() or "non_retryable_rejection"
    block_message = (
        submit_result.get("message")
        or submit_result.get("error")
        or "订单被拒且不可重试，执行器已阻断重复报单"
    )
    created = 0
    for child in children:
        quantity = max(
            safe_int(child.remaining_quantity),
            safe_int(child.quantity) - safe_int(child.filled_quantity),
            0,
        )
        raw_block = {
            "reason": block_reason,
            "source_parent_order_id": parent.id,
            "source_parent_client_order_id": parent.client_order_id,
            "source_child_order_id": child.id,
            "requested_quantity": safe_int(child.quantity),
            "submitted_quantity": safe_int(submit_result.get("submitted_quantity")),
            "blocked_quantity": quantity,
            "response_status": submit_result.get("status"),
            "error_code": submit_result.get("error_code"),
            "retryable": False,
            "message": block_message,
            "submit_result": submit_result,
        }
        created += int(_upsert_block_order(
            db,
            parent=parent,
            child=child,
            side=str(child.side or parent.side or "").upper(),
            quantity=quantity,
            blocked_until=None,
            block_status=block_status,
            block_reason=block_reason,
            block_message=block_message,
            raw_block=raw_block,
        ))
    return created


def _block_type_for_quantity_clip(item: Dict[str, Any], requested_quantity: int) -> Tuple[str, str, str]:
    explicit_reason = str(item.get("block_reason") or "").strip()
    explicit_message = str(item.get("block_message") or item.get("message") or "").strip()
    position_quantity = safe_int(item.get("position_quantity"), -1)
    if position_quantity < 0:
        position_quantity = safe_int(item.get("sellable_quantity"), 0)
    if position_quantity >= requested_quantity:
        sellable_rule = str(item.get("sellable_rule") or "").upper()
        block_message = "可卖数量不足，阻断到下一交易日开盘后重试"
        if sellable_rule == "T+1":
            block_message = "A股 T+1 可卖数量不足，阻断到下一交易日开盘后重试"
        return (
            STATUS_BLOCKED_INSUFFICIENT_SELLABLE,
            "insufficient_sellable_quantity",
            block_message,
        )
    return (
        STATUS_BLOCKED_INSUFFICIENT_POSITION,
        "insufficient_broker_position",
        "券商真实持仓不足，疑似策略账本与券商持仓不一致，需要同步或人工处理",
    )


def expire_insufficient_sellable_blocks(
    db: Session,
    *,
    external_trading_account_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    current = now or datetime.now()
    query = db.query(ExternalTradingOrder).filter(
        ExternalTradingOrder.status == STATUS_BLOCKED_INSUFFICIENT_SELLABLE,
        ExternalTradingOrder.deadline_at <= current,
    )
    if external_trading_account_id:
        query = query.filter(ExternalTradingOrder.external_trading_account_id == external_trading_account_id)
    rows = query.all()
    for row in rows:
        row.status = "EXPIRED"
        row.message = "可卖数量阻断已到期，允许执行器重新尝试"
        row.updated_at = current
    return len(rows)


def _block_is_active_for_plan(row: ExternalTradingOrder, now: datetime) -> bool:
    status = str(getattr(row, "status", "") or "").upper()
    if status == STATUS_BLOCKED_INSUFFICIENT_SELLABLE:
        return bool(row.deadline_at and row.deadline_at > now)
    return status in BLOCKED_ORDER_STATUSES


def _non_retryable_block_rule(row: ExternalTradingOrder) -> Tuple[str, str]:
    raw = row.raw_request if isinstance(row.raw_request, dict) else {}
    submit_result = raw.get("submit_result") if isinstance(raw.get("submit_result"), dict) else {}
    error_code = str(raw.get("error_code") or submit_result.get("error_code") or "").upper()
    response_status = str(raw.get("response_status") or submit_result.get("status") or "").upper()
    return response_status, error_code


def _non_retryable_block_prevents_demand(row: ExternalTradingOrder) -> bool:
    # 这类规则跟单个子账户无关，必须等净额父单成型后再判断，否则会误杀可合并成合法手数的订单。
    response_status, error_code = _non_retryable_block_rule(row)
    if response_status == "NOT_SUPPORTED" or error_code == "UNSUPPORTED_MARKET":
        return True
    return False


def _non_retryable_block_matches_parent_order(
    row: ExternalTradingOrder,
    *,
    symbol: str,
    side: str,
    quantity: int,
    account_sellable_quantity: int,
) -> bool:
    response_status, error_code = _non_retryable_block_rule(row)
    if response_status == "NOT_SUPPORTED" or error_code == "UNSUPPORTED_MARKET":
        return True
    if error_code == "INVALID_LOT_SIZE" and is_star_market_symbol(symbol):
        if side == "BUY":
            return 0 < quantity < 200
        if side == "SELL":
            return 0 < quantity < 200 and quantity != account_sellable_quantity
    return True


def _block_matches_demand(
    row: ExternalTradingOrder,
    *,
    now: datetime,
    symbol: str,
    side: str,
    quantity: int,
    available_quantity: int,
) -> bool:
    status = str(getattr(row, "status", "") or "").upper()
    if not _block_is_active_for_plan(row, now):
        return False
    if status == STATUS_BLOCKED_NON_RETRYABLE_REJECTION:
        return _non_retryable_block_prevents_demand(row)
    return True


def _block_priority(row: ExternalTradingOrder) -> int:
    status = str(getattr(row, "status", "") or "").upper()
    if status == STATUS_BLOCKED_INSUFFICIENT_POSITION:
        return 0
    if status == STATUS_BLOCKED_NON_RETRYABLE_REJECTION:
        return 1
    if status == STATUS_BLOCKED_INSUFFICIENT_SELLABLE:
        return 2
    return 3


def _get_account_level_sellable_quantities(
    db: Session,
    *,
    account_id: Optional[str],
    external_trading_account_id: int,
) -> Dict[str, int]:
    sub_accounts_query = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.external_trading_account_id == external_trading_account_id,
    )
    if account_id:
        sub_accounts_query = sub_accounts_query.filter(ExternalTradingSubAccount.account_id == account_id)
    sub_accounts = sub_accounts_query.all()
    sub_account_ids = [safe_int(row.id) for row in sub_accounts if safe_int(row.id) > 0]
    if not sub_account_ids:
        return {}

    today_buy_by_key = get_today_buy_quantities(db, sub_account_ids)
    positions = (
        db.query(ExternalTradingLedgerPosition)
        .filter(ExternalTradingLedgerPosition.sub_account_id.in_(sub_account_ids))
        .all()
    )
    pending_sells = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.sub_account_id.in_(sub_account_ids),
            ExternalTradingOrder.side == "SELL",
            ExternalTradingOrder.status.in_(list(ACTIVE_ORDER_STATUSES)),
        )
        .all()
    )
    pending_sell_by_key: Dict[Tuple[int, str], int] = {}
    for row in pending_sells:
        symbol = normalize_symbol(row.symbol)
        if not symbol or not row.sub_account_id:
            continue
        key = (safe_int(row.sub_account_id), symbol)
        remaining = max(safe_int(row.remaining_quantity, row.quantity - row.filled_quantity), 0)
        pending_sell_by_key[key] = pending_sell_by_key.get(key, 0) + remaining

    sellable_by_symbol: Dict[str, int] = {}
    for row in positions:
        symbol = normalize_symbol(row.symbol)
        if not symbol:
            continue
        sellability = compute_position_sellability(
            row,
            today_buy_by_key.get((safe_int(row.sub_account_id), symbol), 0),
        )
        available_quantity = safe_int(sellability.get("computed_sellable_quantity"))
        pending_sell = pending_sell_by_key.get((safe_int(row.sub_account_id), symbol), 0)
        remaining_sellable = max(available_quantity - pending_sell, 0)
        if remaining_sellable <= 0:
            continue
        sellable_by_symbol[symbol] = sellable_by_symbol.get(symbol, 0) + remaining_sellable
    return sellable_by_symbol


def _get_active_non_retryable_blocks(
    db: Session,
    *,
    external_trading_account_id: int,
    now: datetime,
) -> Dict[Tuple[str, str], List[ExternalTradingOrder]]:
    rows = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
            ExternalTradingOrder.status == STATUS_BLOCKED_NON_RETRYABLE_REJECTION,
        )
        .all()
    )
    block_by_key: Dict[Tuple[str, str], List[ExternalTradingOrder]] = {}
    for row in rows:
        symbol = normalize_symbol(row.symbol)
        side = str(row.side or "").upper()
        if not symbol or side not in {"BUY", "SELL"} or not _block_is_active_for_plan(row, now):
            continue
        block_by_key.setdefault((symbol, side), []).append(row)
    for same_key_rows in block_by_key.values():
        same_key_rows.sort(key=_block_priority)
    return block_by_key


def _filter_parent_orders_by_non_retryable_blocks(
    db: Session,
    *,
    account_id: Optional[str],
    external_trading_account_id: int,
    now: datetime,
    external_orders: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not external_orders:
        return external_orders
    active_blocks = _get_active_non_retryable_blocks(
        db,
        external_trading_account_id=external_trading_account_id,
        now=now,
    )
    if not active_blocks:
        return external_orders
    account_sellable_quantities = _get_account_level_sellable_quantities(
        db,
        account_id=account_id,
        external_trading_account_id=external_trading_account_id,
    )

    filtered_orders: List[Dict[str, Any]] = []
    for order in external_orders:
        symbol = normalize_symbol(order.get("symbol"))
        side = str(order.get("side") or "").upper()
        quantity = safe_int(order.get("quantity"))
        if not symbol or side not in {"BUY", "SELL"} or quantity <= 0:
            filtered_orders.append(order)
            continue
        account_sellable_quantity = safe_int(account_sellable_quantities.get(symbol))
        blocked_order = None
        for candidate in active_blocks.get((symbol, side), []):
            if _non_retryable_block_matches_parent_order(
                candidate,
                symbol=symbol,
                side=side,
                quantity=quantity,
                account_sellable_quantity=account_sellable_quantity,
            ):
                blocked_order = candidate
                break
        if not blocked_order:
            filtered_orders.append(order)
            continue
        skipped.append({
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "reason": blocked_order.cancel_reason,
            "blocked_status": blocked_order.status,
            "blocked_order_id": blocked_order.id,
            "message": blocked_order.message or "订单被明确规则永久阻断，执行器不再重复提交",
        })
    return filtered_orders


def resolve_manual_block_fill_price(
    db: Session,
    order: ExternalTradingOrder,
    explicit_price: Optional[float] = None,
) -> Tuple[float, str]:
    price = round_money(safe_float(explicit_price))
    if price > 0:
        return price, "manual_input"

    raw_request = order.raw_request if isinstance(order.raw_request, dict) else {}
    submit_result = raw_request.get("submit_result") if isinstance(raw_request.get("submit_result"), dict) else {}
    price = round_money(safe_float(order.submitted_price))
    if price > 0:
        return price, "block_submitted_price"

    price = round_money(safe_float(raw_request.get("submitted_price")))
    if price > 0:
        return price, "block_raw_submitted_price"

    price = round_money(safe_float(submit_result.get("submitted_price")))
    if price > 0:
        return price, "source_submit_result_price"

    source_parent_order_id = safe_int(raw_request.get("source_parent_order_id"), 0)
    if source_parent_order_id > 0:
        parent = db.query(ExternalTradingOrder).filter(ExternalTradingOrder.id == source_parent_order_id).first()
        if parent:
            price = round_money(safe_float(parent.submitted_price))
            if price > 0:
                return price, "source_parent_submitted_price"
            parent_submit_result = parent.raw_submit_result if isinstance(parent.raw_submit_result, dict) else {}
            price = round_money(safe_float(parent_submit_result.get("submitted_price")))
            if price > 0:
                return price, "source_parent_submit_result_price"

    if order.sub_account_id and order.symbol:
        position = (
            db.query(ExternalTradingLedgerPosition)
            .filter(
                ExternalTradingLedgerPosition.sub_account_id == order.sub_account_id,
                ExternalTradingLedgerPosition.symbol == order.symbol,
            )
            .first()
        )
        if position:
            price = round_money(safe_float(position.market_price))
            if price > 0:
                return price, "ledger_market_price"
            price = round_money(safe_float(position.avg_cost))
            if price > 0:
                return price, "ledger_avg_cost"

    raise ValueError("无法自动确定成交价，请后续补一个手动输入成交价的能力")


def mark_block_order_manual_success(
    db: Session,
    *,
    order: ExternalTradingOrder,
    fill_price: float,
    price_source: str,
    traded_at: Optional[datetime] = None,
    note: Optional[str] = None,
) -> ExternalTradingOrderFill:
    if _role(order) != "BLOCK":
        raise ValueError("只有阻断单支持手工标记成功")
    if str(order.status or "").upper() != STATUS_BLOCKED_INSUFFICIENT_POSITION:
        raise ValueError("当前仅支持“持仓不足”阻断单手工标记成功")
    if str(order.side or "").upper() != "SELL":
        raise ValueError("当前仅支持卖出阻断单手工标记成功")

    quantity = max(
        safe_int(order.remaining_quantity),
        safe_int(order.quantity) - safe_int(order.filled_quantity),
        0,
    )
    if quantity <= 0:
        raise ValueError("阻断单没有可处理的剩余数量")

    fill_price = round_money(safe_float(fill_price))
    if fill_price <= 0:
        raise ValueError("成交价必须大于 0")

    fill_key = f"manual:block-success:{order.id}"
    existing_fill = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.fill_key == fill_key).first()
    if existing_fill:
        raise ValueError("这条阻断单已经标记成功过了")

    position = None
    if order.sub_account_id and order.symbol:
        position = (
            db.query(ExternalTradingLedgerPosition)
            .filter(
                ExternalTradingLedgerPosition.sub_account_id == order.sub_account_id,
                ExternalTradingLedgerPosition.symbol == order.symbol,
            )
            .first()
        )
    if not position or safe_int(position.quantity) < quantity:
        raise ValueError("账本持仓数量不足，无法按成功成交回写")

    traded_time = traded_at or datetime.now()
    estimated_fee_increment = _estimated_fee_increment_for_order(db, order, quantity * fill_price)
    event = {
        "type": "manual_block_success",
        "price_source": price_source,
        "note": note,
        "quantity": quantity,
        "price": fill_price,
        "traded_at": traded_time.isoformat(),
        "source_status": order.status,
        "source_message": order.message,
    }
    fill = _insert_fill_row(
        db,
        order=order,
        fill_key=fill_key,
        quantity=quantity,
        price=fill_price,
        traded_at=traded_time,
        event=event,
        estimated_commission=estimated_fee_increment.get("commission", 0.0),
        estimated_stamp_tax=estimated_fee_increment.get("stamp_tax", 0.0),
        estimated_fee_total=estimated_fee_increment.get("fee_total", 0.0),
    )
    _apply_fill_to_ledger(db, order, quantity, fill_price, fee_total=estimated_fee_increment.get("fee_total", 0.0))
    _refresh_order_from_fill_totals(db, order)
    order.submitted_price = safe_float(order.submitted_price, fill_price) or fill_price
    order.avg_fill_price = fill_price
    order.submitted_at = order.submitted_at or traded_time
    order.last_event_at = traded_time
    order.raw_order_event = event
    order.message = (
        f"人工标记成功，按 {fill_price:.2f} 元回写账本"
        + (f"（{note}）" if note else "")
    )[:1000]
    order.updated_at = datetime.now()
    return fill


def _apply_submission_quantity_clip(
    db: Session,
    row: ExternalTradingOrder,
    item: Dict[str, Any],
    insufficient_sellable_block_until: Optional[datetime] = None,
) -> None:
    if not item.get("quantity_clipped"):
        return
    submitted_quantity = safe_int(item.get("submitted_quantity", item.get("quantity")))
    current_quantity = safe_int(row.quantity)
    if submitted_quantity >= current_quantity:
        return
    requested_quantity = safe_int(item.get("requested_quantity"), current_quantity)
    row.quantity = submitted_quantity
    row.remaining_quantity = max(submitted_quantity - safe_int(row.filled_quantity), 0)
    row.raw_request = {
        **(row.raw_request or {}),
        "requested_quantity": requested_quantity,
        "submitted_quantity": submitted_quantity,
        "quantity_clipped": True,
        "sellable_quantity": item.get("sellable_quantity"),
        "position_quantity": item.get("position_quantity"),
        "clip_sell_to_available": item.get("clip_sell_to_available"),
        "block_reason": item.get("block_reason"),
        "block_message": item.get("block_message"),
    }
    residual_allocations = []
    if _role(row) == "PARENT":
        residual_allocations = _resize_child_orders_for_parent_clip(db, row, submitted_quantity, requested_quantity)
        block_status, block_reason, block_message = _block_type_for_quantity_clip(item, requested_quantity)
        block_until = insufficient_sellable_block_until if block_status == STATUS_BLOCKED_INSUFFICIENT_SELLABLE else None
        _create_quantity_clip_block_orders(
            db,
            parent=row,
            residual_allocations=residual_allocations,
            blocked_until=block_until,
            submit_result=item,
            block_status=block_status,
            block_reason=block_reason,
            block_message=block_message,
        )


def record_submission_result(
    db: Session,
    *,
    external_trading_account_id: int,
    response_orders: List[Dict[str, Any]],
    insufficient_sellable_block_until: Optional[datetime] = None,
) -> None:
    now = datetime.now()
    for item in response_orders or []:
        client_order_id = item.get("client_order_id")
        row = None
        if client_order_id:
            row = (
                db.query(ExternalTradingOrder)
                .filter(ExternalTradingOrder.client_order_id == client_order_id)
                .first()
            )
        if not row and item.get("order_id"):
            row = (
                db.query(ExternalTradingOrder)
                .filter(
                    ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
                    ExternalTradingOrder.broker_order_id == str(item.get("order_id")),
                )
                .first()
            )
        if not row:
            continue

        _apply_submission_quantity_clip(
            db,
            row,
            item,
            insufficient_sellable_block_until=insufficient_sellable_block_until,
        )
        raw_status = item.get("raw_status")
        retryable = submission_result_retryable(item)
        filled_quantity = order_event_filled_quantity(
            item,
            raw_status,
            current=row.filled_quantity,
        )
        response_status = str(item.get("status") or "").upper()
        if item.get("ok") is False and retryable is False:
            lifecycle = response_status or ptrade_status_to_lifecycle(raw_status, filled_quantity, row.quantity)
        else:
            lifecycle = ptrade_status_to_lifecycle(raw_status, filled_quantity, row.quantity)
        if item.get("ok") is False and lifecycle not in TERMINAL_ORDER_STATUSES:
            lifecycle = "FAILED"
        lifecycle = merge_lifecycle_status(row.status, lifecycle)

        row.status = lifecycle
        row.ptrade_status = None if raw_status is None else str(raw_status)
        row.broker_order_id = str(item.get("order_id")) if item.get("order_id") else row.broker_order_id
        row.entrust_no = str(item.get("entrust_no")) if item.get("entrust_no") else row.entrust_no
        row.submitted_price = safe_float(item.get("submitted_price"), row.submitted_price)
        row.message = item.get("message")
        row.submitted_at = now if row.status not in {"FAILED", "REJECTED", "NOT_SUPPORTED"} else row.submitted_at
        row.last_event_at = now
        row.raw_submit_result = item
        row.updated_at = now
        _propagate_parent_order_state(db, row)
        if item.get("ok") is False and retryable is False:
            _create_non_retryable_rejection_blocks(
                db,
                parent=row,
                submit_result=item,
            )


def record_cancel_result(
    db: Session,
    *,
    external_trading_account_id: int,
    response_orders: List[Dict[str, Any]],
) -> None:
    now = datetime.now()
    for item in response_orders or []:
        client_order_id = item.get("client_order_id")
        row = None
        if client_order_id:
            row = (
                db.query(ExternalTradingOrder)
                .filter(ExternalTradingOrder.client_order_id == client_order_id)
                .first()
            )
        for key in ("order_id", "entrust_no", "broker_order_id"):
            if row:
                break
            value = item.get(key)
            if not value:
                continue
            row = (
                db.query(ExternalTradingOrder)
                .filter(
                    ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
                    ExternalTradingOrder.broker_order_id == str(value),
                )
                .first()
            )
            if not row:
                row = (
                    db.query(ExternalTradingOrder)
                    .filter(
                        ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
                        ExternalTradingOrder.entrust_no == str(value),
                    )
                    .first()
                )
        if not row:
            continue

        if item.get("ok") is False:
            row.message = item.get("message") or item.get("error") or "撤单失败"
        else:
            row.status = "CANCEL_PENDING"
            row.message = item.get("message") or "撤单指令已提交"
            row.last_event_at = now
        row.raw_order_event = item
        row.updated_at = now
        _propagate_parent_order_state(db, row)


def _find_order_for_event(db: Session, external_trading_account_id: int, event: Dict[str, Any]) -> Optional[ExternalTradingOrder]:
    client_order_id = event.get("client_order_id")
    if client_order_id:
        row = db.query(ExternalTradingOrder).filter(ExternalTradingOrder.client_order_id == client_order_id).first()
        if row:
            return row
    for key in ("order_id", "entrust_no", "broker_order_id"):
        value = event.get(key)
        if not value:
            continue
        rows = (
            db.query(ExternalTradingOrder)
            .filter(
                ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
                ExternalTradingOrder.broker_order_id == str(value),
            )
            .all()
        )
        row = _pick_event_order(rows)
        if row:
            return row
        rows = (
            db.query(ExternalTradingOrder)
            .filter(
                ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
                ExternalTradingOrder.entrust_no == str(value),
            )
            .all()
        )
        row = _pick_event_order(rows)
        if row:
            return row
    return None


def process_order_events(db: Session, *, external_trading_account_id: int, orders: List[Dict[str, Any]]) -> int:
    updated = 0
    now = datetime.now()
    for event in orders or []:
        row = _find_order_for_event(db, external_trading_account_id, event)
        if not row:
            logger.warning("Unmatched external order event: %s", event)
            continue
        raw_status = event.get("status")
        filled_quantity = order_event_filled_quantity(
            event,
            raw_status,
            current=row.filled_quantity,
        )
        row.ptrade_status = None if raw_status is None else str(raw_status)
        row.status = merge_lifecycle_status(
            row.status,
            ptrade_status_to_lifecycle(raw_status, filled_quantity, row.quantity),
        )
        row.broker_order_id = str(event.get("order_id")) if event.get("order_id") else row.broker_order_id
        row.entrust_no = str(event.get("entrust_no")) if event.get("entrust_no") else row.entrust_no
        row.filled_quantity = max(row.filled_quantity or 0, filled_quantity)
        row.remaining_quantity = max(row.quantity - row.filled_quantity, 0)
        row.avg_fill_price = safe_float(event.get("avg_fill_price", event.get("business_price")), row.avg_fill_price)
        row.last_event_at = parse_dt(event.get("event_time") or event.get("submitted_at")) or now
        row.raw_order_event = event
        row.updated_at = now
        _propagate_parent_order_state(db, row)
        updated += 1
    return updated


def _trade_fill_key(event: Dict[str, Any]) -> str:
    for key in ("fill_key", "business_id", "business_no", "deal_no", "match_no", "serial_no"):
        if event.get(key):
            return str(event.get(key))
    stable = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _get_or_create_ledger_position(db: Session, order: ExternalTradingOrder) -> ExternalTradingLedgerPosition:
    row = (
        db.query(ExternalTradingLedgerPosition)
        .filter(
            ExternalTradingLedgerPosition.sub_account_id == order.sub_account_id,
            ExternalTradingLedgerPosition.symbol == order.symbol,
        )
        .first()
    )
    if row:
        return row
    row = ExternalTradingLedgerPosition(
        account_id=order.account_id,
        external_trading_account_id=order.external_trading_account_id,
        sub_account_id=order.sub_account_id,
        symbol=order.symbol,
        quantity=0,
        available_quantity=0,
        avg_cost=0.0,
        realized_pnl=0.0,
        updated_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def _apply_fill_to_ledger(
    db: Session,
    order: ExternalTradingOrder,
    fill_quantity: int,
    fill_price: float,
    fee_total: float = 0.0,
) -> None:
    if not order.sub_account_id or fill_quantity <= 0:
        return
    pos = _get_or_create_ledger_position(db, order)
    sub_account = db.query(ExternalTradingSubAccount).filter(ExternalTradingSubAccount.id == order.sub_account_id).first()
    amount = round(fill_quantity * fill_price, 2)
    fee_total = round_money(fee_total)
    now = datetime.now()

    if order.side == "BUY":
        old_qty = max(pos.quantity or 0, 0)
        new_qty = old_qty + fill_quantity
        old_cost_value = old_qty * safe_float(pos.avg_cost)
        pos.avg_cost = round((old_cost_value + amount + fee_total) / new_qty, 6) if new_qty else 0.0
        pos.quantity = new_qty
        pos.available_quantity = max(pos.available_quantity or 0, 0) + fill_quantity
        if sub_account:
            sub_account.cash_available = round_money(safe_float(sub_account.cash_available) - amount - fee_total)
    else:
        old_qty = max(pos.quantity or 0, 0)
        sell_qty = min(fill_quantity, old_qty)
        pos.quantity = max(old_qty - fill_quantity, 0)
        pos.available_quantity = max(safe_int(pos.available_quantity) - fill_quantity, 0)
        pos.realized_pnl = round_money(safe_float(pos.realized_pnl) + (fill_price - safe_float(pos.avg_cost)) * sell_qty - fee_total)
        if pos.quantity <= 0:
            pos.avg_cost = 0.0
        if sub_account:
            sub_account.cash_available = round_money(safe_float(sub_account.cash_available) + amount - fee_total)

    pos.market_price = fill_price
    pos.market_value = round(pos.quantity * fill_price, 2)
    pos.updated_at = now
    if sub_account:
        sub_account.updated_at = now


def _order_fill_money_totals(db: Session, order_id: Optional[int]) -> Tuple[float, float, float, float]:
    if not order_id:
        return 0.0, 0.0, 0.0, 0.0
    rows = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.order_id == order_id).all()
    amount = sum(safe_float(row.amount) for row in rows)
    commission = sum(safe_float(row.estimated_commission) for row in rows)
    stamp_tax = sum(safe_float(row.estimated_stamp_tax) for row in rows)
    fee_total = sum(safe_float(row.estimated_fee_total) for row in rows)
    return round_money(amount), round_money(commission), round_money(stamp_tax), round_money(fee_total)


def _order_fill_totals(db: Session, order_id: Optional[int]) -> Tuple[int, float]:
    if not order_id:
        return 0, 0.0
    rows = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.order_id == order_id).all()
    quantity = sum(safe_int(row.quantity) for row in rows)
    amount = sum(safe_float(row.amount) for row in rows)
    return quantity, amount


def _refresh_order_from_fill_totals(db: Session, order: ExternalTradingOrder) -> None:
    filled_quantity, filled_amount = _order_fill_totals(db, order.id)
    _, estimated_commission, estimated_stamp_tax, estimated_fee_total = _order_fill_money_totals(db, order.id)
    if filled_quantity > 0:
        order.avg_fill_price = round(filled_amount / filled_quantity, 6)
    order.filled_quantity = max(safe_int(order.filled_quantity), filled_quantity)
    order.remaining_quantity = max(safe_int(order.quantity) - safe_int(order.filled_quantity), 0)
    order.estimated_commission = estimated_commission
    order.estimated_stamp_tax = estimated_stamp_tax
    order.estimated_fee_total = estimated_fee_total
    if estimated_fee_total > 0 and not order.fee_source:
        order.fee_source = "ESTIMATED"
    if order.remaining_quantity <= 0:
        order.status = "FILLED"
    elif safe_int(order.filled_quantity) > 0:
        order.status = "PARTIALLY_FILLED"


def _insert_fill_row(
    db: Session,
    *,
    order: ExternalTradingOrder,
    fill_key: str,
    quantity: int,
    price: float,
    traded_at: datetime,
    event: Dict[str, Any],
    estimated_commission: float = 0.0,
    estimated_stamp_tax: float = 0.0,
    estimated_fee_total: float = 0.0,
    fee_source: str = "ESTIMATED",
) -> ExternalTradingOrderFill:
    fill = ExternalTradingOrderFill(
        account_id=order.account_id,
        external_trading_account_id=order.external_trading_account_id,
        sub_account_id=order.sub_account_id,
        order_id=order.id,
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        fill_key=fill_key,
        symbol=order.symbol,
        side=order.side,
        quantity=quantity,
        price=price,
        amount=round(quantity * price, 2),
        estimated_commission=round_money(estimated_commission),
        estimated_stamp_tax=round_money(estimated_stamp_tax),
        estimated_fee_total=round_money(estimated_fee_total),
        fee_source=fee_source,
        traded_at=traded_at,
        raw_event=event,
        created_at=datetime.now(),
    )
    db.add(fill)
    db.flush()
    return fill


def _allocate_quantity_to_child_orders(
    db: Session,
    parent: ExternalTradingOrder,
    fill_key: str,
    quantity: int,
    price: float,
    traded_at: datetime,
    event: Dict[str, Any],
    estimated_fee_increment: Optional[Dict[str, float]] = None,
) -> List[ExternalTradingOrder]:
    children = [
        child
        for child in _child_orders(db, parent.id)
        if safe_int(child.remaining_quantity, child.quantity - child.filled_quantity) > 0
    ]
    if not children or quantity <= 0:
        return []

    total_remaining = sum(max(safe_int(child.remaining_quantity, child.quantity - child.filled_quantity), 0) for child in children)
    if total_remaining <= 0:
        return []

    raw_allocations = []
    allocated_quantity = 0
    for child in children:
        remaining = max(safe_int(child.remaining_quantity, child.quantity - child.filled_quantity), 0)
        exact = quantity * remaining / total_remaining
        base = min(int(exact), remaining)
        raw_allocations.append({
            "child": child,
            "remaining": remaining,
            "quantity": base,
            "remainder": exact - base,
        })
        allocated_quantity += base

    leftover = min(quantity - allocated_quantity, total_remaining - allocated_quantity)
    for item in sorted(raw_allocations, key=lambda data: data["remainder"], reverse=True):
        if leftover <= 0:
            break
        if item["quantity"] < item["remaining"]:
            item["quantity"] += 1
            leftover -= 1

    payable_allocations = [
        item
        for item in raw_allocations
        if safe_int(item.get("quantity")) > 0
    ]
    allocation_amounts = [round_money(safe_int(item.get("quantity")) * price) for item in payable_allocations]
    fee_increment = estimated_fee_increment or {}
    commission_allocations = _allocate_money(safe_float(fee_increment.get("commission")), allocation_amounts)
    stamp_tax_allocations = _allocate_money(safe_float(fee_increment.get("stamp_tax")), allocation_amounts)
    fee_total_allocations = _allocate_money(safe_float(fee_increment.get("fee_total")), allocation_amounts)
    fee_by_child_id = {}
    for index, item in enumerate(payable_allocations):
        child = item["child"]
        fee_by_child_id[child.id] = {
            "commission": commission_allocations[index] if index < len(commission_allocations) else 0.0,
            "stamp_tax": stamp_tax_allocations[index] if index < len(stamp_tax_allocations) else 0.0,
            "fee_total": fee_total_allocations[index] if index < len(fee_total_allocations) else 0.0,
        }

    updated_children: List[ExternalTradingOrder] = []
    now = datetime.now()
    for item in raw_allocations:
        child = item["child"]
        child_quantity = item["quantity"]
        if child_quantity <= 0:
            continue
        child_fill_key = f"{fill_key}:child:{child.id}"
        if db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.fill_key == child_fill_key).first():
            continue
        child_fee = fee_by_child_id.get(child.id, {})
        _insert_fill_row(
            db,
            order=child,
            fill_key=child_fill_key,
            quantity=child_quantity,
            price=price,
            traded_at=traded_at,
            event={**event, "parent_fill_key": fill_key, "allocated_from_parent_order_id": parent.id},
            estimated_commission=child_fee.get("commission", 0.0),
            estimated_stamp_tax=child_fee.get("stamp_tax", 0.0),
            estimated_fee_total=child_fee.get("fee_total", 0.0),
        )
        _apply_fill_to_ledger(db, child, child_quantity, price, fee_total=child_fee.get("fee_total", 0.0))
        _refresh_order_from_fill_totals(db, child)
        child.last_event_at = traded_at
        child.updated_at = now
        updated_children.append(child)
    return updated_children


def process_trade_events(db: Session, *, external_trading_account_id: int, trades: List[Dict[str, Any]]) -> int:
    inserted = 0
    now = datetime.now()
    for event in trades or []:
        order = _find_order_for_event(db, external_trading_account_id, event)
        if not order:
            logger.warning("Unmatched external trade event: %s", event)
            continue
        fill_key = _trade_fill_key(event)
        existing = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.fill_key == fill_key).first()
        if existing:
            continue
        quantity = safe_int(event.get("quantity", event.get("business_amount", event.get("filled_quantity"))))
        price = safe_float(event.get("price", event.get("business_price", event.get("avg_fill_price"))))
        if quantity <= 0 or price <= 0:
            logger.warning("Ignored invalid external trade event: %s", event)
            continue

        traded_at = parse_dt(event.get("traded_at") or event.get("trade_time") or event.get("business_time")) or now
        estimated_fee_increment = _estimated_fee_increment_for_order(db, order, quantity * price)
        fill = _insert_fill_row(
            db,
            order=order,
            fill_key=fill_key,
            quantity=quantity,
            price=price,
            traded_at=traded_at,
            event=event,
            estimated_commission=estimated_fee_increment.get("commission", 0.0),
            estimated_stamp_tax=estimated_fee_increment.get("stamp_tax", 0.0),
            estimated_fee_total=estimated_fee_increment.get("fee_total", 0.0),
        )
        if _role(order) == "PARENT":
            updated_children = _allocate_quantity_to_child_orders(
                db,
                order,
                fill_key,
                quantity,
                price,
                traded_at,
                event,
                estimated_fee_increment=estimated_fee_increment,
            )
        else:
            _apply_fill_to_ledger(db, order, quantity, price, fee_total=estimated_fee_increment.get("fee_total", 0.0))

        _refresh_order_from_fill_totals(db, order)
        _propagate_parent_order_state(db, order)
        order.last_event_at = fill.traded_at
        order.updated_at = now
        inserted += 1
    return inserted


PTRADE_TRADE_BUSINESS_FLAGS = {"4001", "4002"}
PTRADE_NON_TRADE_BUSINESS_FLAGS = {"2434", "4018", "4081", "4082", "4420"}
NON_TRADE_DELIVER_KEYWORDS = (
    "组合费",
    "红利税",
    "股息",
    "利息",
    "资金下账",
    "资金上账",
)


def _is_blank_deliver_value(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, str) and value.strip() == "")


def _deliver_value(record: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    if not isinstance(record, dict):
        return default
    lowered = {str(key).lower(): value for key, value in record.items()}
    for key in keys:
        if key in record and not _is_blank_deliver_value(record.get(key)):
            return record.get(key)
        lower_key = str(key).lower()
        if lower_key in lowered and not _is_blank_deliver_value(lowered[lower_key]):
            return lowered[lower_key]
    return default


def _deliver_float(record: Dict[str, Any], keys: Tuple[str, ...], default: float = 0.0) -> float:
    return round_money(safe_float(_deliver_value(record, keys), default))


def _deliver_optional_float(record: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    value = _deliver_value(record, keys)
    if value is None:
        return None
    return round_money(safe_float(value))


def _deliver_business_flag(record: Dict[str, Any]) -> str:
    value = _deliver_value(record, ("business_flag", "业务代码"))
    if value is None:
        return ""
    try:
        return str(int(float(value)))
    except Exception:
        return str(value).strip()


def _deliver_exchange_type(record: Dict[str, Any]) -> str:
    value = _deliver_value(record, ("exchange_type", "market", "交易市场"))
    return str(value or "").strip().upper()


def _deliver_business_type(record: Dict[str, Any]) -> str:
    value = _deliver_value(record, ("business_type", "业务类型"))
    return str(value or "").strip().lower()


def _deliver_business_text(record: Dict[str, Any]) -> str:
    parts = (
        _deliver_value(record, ("business_name", "业务名称")),
        _deliver_value(record, ("remark", "备注")),
    )
    return " ".join(str(part).strip() for part in parts if not _is_blank_deliver_value(part))


def _deliver_is_hk_connect_record(record: Dict[str, Any]) -> bool:
    if _deliver_exchange_type(record) == "G":
        return True
    if _deliver_business_type(record) == "g":
        return True
    return "港股通" in _deliver_business_text(record)


def _deliver_identifier(record: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = _deliver_value(record, (key,), None)
        if _is_blank_deliver_value(value):
            continue
        text = str(value).strip()
        try:
            if float(text) == 0:
                continue
        except Exception:
            pass
        return text
    return None


def _deliver_side(record: Dict[str, Any]) -> Optional[str]:
    raw_side = _deliver_value(record, (
        "side",
        "entrust_bs",
        "business_flag",
        "bs_flag",
        "buy_sell",
        "买卖方向",
        "买卖标志",
        "业务名称",
    ))
    text = str(raw_side or "").strip().upper()
    if text in {"1", "B", "BUY", "买", "买入", "4002"} or "买入" in text:
        return "BUY"
    if text in {"2", "S", "SELL", "卖", "卖出", "4001"} or "卖出" in text:
        return "SELL"
    return None


def _deliver_is_trade_record(
    record: Dict[str, Any],
    *,
    symbol: Optional[str],
    side: Optional[str],
    quantity: int,
    price: float,
    amount: float,
) -> Tuple[bool, Optional[str]]:
    business_flag = _deliver_business_flag(record)
    business_text = _deliver_business_text(record)
    if _deliver_is_hk_connect_record(record):
        return False, "港股通流水，跳过订单对账和费用统计"
    if business_flag in PTRADE_NON_TRADE_BUSINESS_FLAGS:
        return False, f"非证券买卖业务({business_flag})，跳过订单对账"
    if any(keyword in business_text for keyword in NON_TRADE_DELIVER_KEYWORDS):
        return False, "非证券买卖资金流水，跳过订单对账"

    has_trade_shape = bool(symbol and side and abs(safe_int(quantity)) > 0 and price > 0 and amount > 0)
    if not has_trade_shape:
        return False, "非证券成交流水，跳过订单对账"

    if business_flag in PTRADE_TRADE_BUSINESS_FLAGS:
        return True, None
    if "证券买入" in business_text or "证券卖出" in business_text:
        return True, None
    if not business_flag:
        return True, None
    return False, f"非证券买卖业务({business_flag})，跳过订单对账"


def _deliver_cash_fee_total(record: Dict[str, Any], amount: float) -> Optional[float]:
    business_flag = _deliver_business_flag(record)
    business_text = _deliver_business_text(record)
    if business_flag not in PTRADE_TRADE_BUSINESS_FLAGS and "证券买入" not in business_text and "证券卖出" not in business_text:
        return None
    clear_balance = _deliver_optional_float(record, ("clear_balance", "清算金额", "结算金额"))
    gross_amount = _deliver_optional_float(record, ("business_balance", "match_amount", "turnover", "成交金额"))
    if clear_balance is None or gross_amount is None:
        return None
    gross_amount = abs(safe_float(gross_amount))
    if gross_amount <= 0:
        return None
    return round_money(abs(abs(clear_balance) - gross_amount))


def _deliver_non_trade_fee_total(record: Dict[str, Any], amount: float) -> float:
    if _deliver_is_hk_connect_record(record):
        return 0.0

    cash_delta = _deliver_optional_float(record, ("occur_balance", "clear_balance", "发生金额", "清算金额", "结算金额"))
    if cash_delta is not None:
        return round_money(abs(cash_delta)) if cash_delta < -0.004 else 0.0

    business_text = _deliver_business_text(record)
    if any(keyword in business_text for keyword in ("费用", "费收取", "税", "资金下账", "扣")):
        return round_money(abs(amount))
    return 0.0


def _deliver_non_trade_income_total(record: Dict[str, Any], amount: float) -> float:
    if _deliver_is_hk_connect_record(record):
        return 0.0

    cash_delta = _deliver_optional_float(record, ("occur_balance", "clear_balance", "发生金额", "清算金额", "结算金额"))
    if cash_delta is not None:
        return round_money(cash_delta) if cash_delta > 0.004 else 0.0

    business_text = _deliver_business_text(record)
    if any(keyword in business_text for keyword in ("入账", "资金上账", "派息", "红利")):
        return round_money(abs(amount))
    return 0.0


def _deliver_record_identity(normalized: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    raw_record = normalized.get("raw_record")
    record = raw_record if isinstance(raw_record, dict) else {}
    for key in ("position_str", "serial_no", "business_id", "business_no", "deal_no", "match_no"):
        value = _deliver_identifier(record, (key,))
        if value:
            return key, value
    return None


def normalize_deliver_record(raw_record: Dict[str, Any], default_trade_date: Optional[date] = None) -> Dict[str, Any]:
    record = raw_record if isinstance(raw_record, dict) else {"value": raw_record}
    trade_date = (
        _parse_date(_deliver_value(record, ("trade_date", "business_date", "init_date", "date", "成交日期", "交割日期")))
        or default_trade_date
        or datetime.now().date()
    )
    raw_symbol = _deliver_value(record, ("symbol", "stock_code", "security_code", "sid", "证券代码", "证券编号"))
    symbol = normalize_symbol(raw_symbol) if raw_symbol else None
    side = _deliver_side(record)
    quantity = safe_int(_deliver_value(record, (
        "quantity",
        "business_amount",
        "occur_amount",
        "match_quantity",
        "成交数量",
        "发生数量",
    )))
    price = safe_float(_deliver_value(record, (
        "price",
        "business_price",
        "match_price",
        "成交价格",
    )))
    amount = _deliver_float(record, (
        "amount",
        "business_balance",
        "occur_balance",
        "match_amount",
        "turnover",
        "成交金额",
        "发生金额",
    ))
    if amount <= 0 and quantity > 0 and price > 0:
        amount = round_money(quantity * price)

    commission = _deliver_float(record, (
        "fare0",
        "commission",
        "business_fare",
        "brokerage_fee",
        "佣金",
        "brokerage",
    ))
    stamp_tax = _deliver_float(record, (
        "fare1",
        "stamp_tax",
        "stamp_duty",
        "stamp_fee",
        "印花税",
    ))
    transfer_fee = _deliver_float(record, (
        "fare2",
        "transfer_fee",
        "transfer_fare",
        "过户费",
    ))
    other_fee_keys = (
        "other_fee",
        "fare3",
        "farex",
        "settlement_fee",
        "经手费",
        "证管费",
        "其他费",
    )
    if _deliver_value(record, ("fare0",), None) is None:
        other_fee_keys = (*other_fee_keys, "exchange_fare")
    other_fee = _deliver_float(record, other_fee_keys)
    total_fee = _deliver_float(record, (
        "total_fee",
        "fee_total",
        "fare_total",
        "total_fare",
        "费用合计",
    ))

    broker_order_id = _deliver_identifier(record, ("order_id", "broker_order_id", "委托编号"))
    entrust_no = _deliver_identifier(record, ("entrust_no", "order_id", "委托编号"))
    business_no = _deliver_identifier(record, (
        "business_no",
        "deal_no",
        "match_no",
        "serial_no",
        "business_id",
        "position_str",
        "成交编号",
        "流水号",
    ))
    stable_key_parts = [
        str(trade_date),
        str(business_no or ""),
        str(entrust_no or broker_order_id or ""),
        str(symbol or raw_symbol or ""),
        str(side or ""),
        str(quantity or ""),
        str(amount or ""),
    ]
    if not business_no and not entrust_no and not broker_order_id:
        stable_key_parts.append(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
    deliver_key = hashlib.sha256("|".join(stable_key_parts).encode("utf-8")).hexdigest()
    is_trade, ignore_reason = _deliver_is_trade_record(
        record,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        amount=amount,
    )
    is_hk_connect = _deliver_is_hk_connect_record(record)
    cash_total_fee = None if is_hk_connect else _deliver_cash_fee_total(record, amount)
    non_trade_income = 0.0
    if is_hk_connect:
        total_fee = 0.0
    elif cash_total_fee is not None:
        total_fee = cash_total_fee
    elif is_trade and total_fee <= 0:
        total_fee = round_money(commission + stamp_tax + transfer_fee + other_fee)
    elif not is_trade:
        total_fee = _deliver_non_trade_fee_total(record, amount)
        non_trade_income = _deliver_non_trade_income_total(record, amount)
    return {
        "trade_date": trade_date,
        "deliver_key": deliver_key,
        "broker_order_id": str(broker_order_id) if broker_order_id else None,
        "entrust_no": str(entrust_no) if entrust_no else None,
        "symbol": symbol,
        "side": side,
        "quantity": abs(quantity),
        "price": price,
        "amount": amount,
        "commission": commission,
        "stamp_tax": stamp_tax,
        "transfer_fee": transfer_fee,
        "other_fee": other_fee,
        "total_fee": total_fee,
        "non_trade_income": non_trade_income,
        "is_trade": is_trade,
        "ignore_reason": ignore_reason,
        "raw_record": stringify_jsonable(record),
    }


def stringify_jsonable(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        if isinstance(obj, dict):
            return {str(key): stringify_jsonable(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [stringify_jsonable(value) for value in obj]
        return str(obj)


def empty_sub_account_fee_summary(sub_account_id: Optional[int] = None) -> Dict[str, Any]:
    return {
        "sub_account_id": sub_account_id,
        "estimated_fee_total": 0.0,
        "actual_fee_total": 0.0,
        "effective_fee_total": 0.0,
        "fill_count": 0,
        "reconciled_fill_count": 0,
        "unreconciled_fill_count": 0,
    }


def get_sub_account_fee_summaries(
    db: Session,
    *,
    external_trading_account_id: int,
    account_id: Optional[str] = None,
) -> Dict[int, Dict[str, Any]]:
    query = db.query(
        ExternalTradingOrderFill.sub_account_id,
        ExternalTradingOrderFill.estimated_fee_total,
        ExternalTradingOrderFill.actual_fee_total,
    ).filter(
        ExternalTradingOrderFill.external_trading_account_id == external_trading_account_id,
        ExternalTradingOrderFill.sub_account_id != None,  # noqa: E711
    )
    if account_id:
        query = query.filter(ExternalTradingOrderFill.account_id == account_id)

    summaries: Dict[int, Dict[str, Any]] = {}
    for sub_account_id, estimated_fee_total, actual_fee_total in query.all():
        sub_id = safe_int(sub_account_id)
        if not sub_id:
            continue
        item = summaries.setdefault(sub_id, empty_sub_account_fee_summary(sub_id))
        estimated_fee = safe_float(estimated_fee_total)
        has_actual_fee = actual_fee_total is not None
        actual_fee = safe_float(actual_fee_total)
        effective_fee = actual_fee if has_actual_fee else estimated_fee
        item["estimated_fee_total"] = round_money(item["estimated_fee_total"] + estimated_fee)
        item["actual_fee_total"] = round_money(item["actual_fee_total"] + actual_fee)
        item["effective_fee_total"] = round_money(item["effective_fee_total"] + effective_fee)
        item["fill_count"] += 1
        if has_actual_fee:
            item["reconciled_fill_count"] += 1
        else:
            item["unreconciled_fill_count"] += 1
    return summaries


def get_external_account_fee_summary(
    db: Session,
    *,
    external_trading_account_id: int,
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    query = db.query(
        ExternalTradingDeliverRecord.status,
        ExternalTradingDeliverRecord.total_fee,
        ExternalTradingDeliverRecord.raw_record,
        ExternalTradingDeliverRecord.trade_date,
    ).filter(
        ExternalTradingDeliverRecord.external_trading_account_id == external_trading_account_id,
    )
    if account_id:
        query = query.filter(ExternalTradingDeliverRecord.account_id == account_id)

    trade_fee_total = 0.0
    non_trade_fee_total = 0.0
    non_trade_income_total = 0.0
    trade_record_count = 0
    non_trade_record_count = 0
    for status, total_fee, raw_record, trade_date in query.all():
        fee = safe_float(total_fee)
        income = 0.0
        is_trade = str(status or "").upper() != "IGNORED"
        if isinstance(raw_record, dict):
            normalized = normalize_deliver_record(raw_record, default_trade_date=trade_date)
            fee = safe_float(normalized.get("total_fee"))
            income = safe_float(normalized.get("non_trade_income"))
            is_trade = bool(normalized.get("is_trade", is_trade))
        if not is_trade:
            non_trade_fee_total = round_money(non_trade_fee_total + fee)
            non_trade_income_total = round_money(non_trade_income_total + income)
            non_trade_record_count += 1
        else:
            trade_fee_total = round_money(trade_fee_total + fee)
            trade_record_count += 1
    return {
        "trade_fee_total": round_money(trade_fee_total),
        "non_trade_fee_total": round_money(non_trade_fee_total),
        "non_trade_income_total": round_money(non_trade_income_total),
        "non_trade_net_total": round_money(non_trade_income_total - non_trade_fee_total),
        "total_fee": round_money(trade_fee_total + non_trade_fee_total),
        "trade_record_count": trade_record_count,
        "non_trade_record_count": non_trade_record_count,
    }


def _deliver_order_candidate_matches(
    row: ExternalTradingOrder,
    normalized: Dict[str, Any],
    *,
    strict_quantity: bool,
) -> bool:
    symbol = normalized.get("symbol")
    side = normalized.get("side")
    trade_date = normalized.get("trade_date")
    if symbol and normalize_symbol(row.symbol) != symbol:
        return False
    if side and row.side != side:
        return False
    if trade_date:
        order_dt = row.submitted_at or row.created_at
        if not order_dt or order_dt.date() != trade_date:
            return False
    quantity = safe_int(normalized.get("quantity"))
    if quantity > 0:
        order_quantity = safe_int(row.quantity)
        filled_quantity = safe_int(row.filled_quantity)
        if strict_quantity:
            if filled_quantity > 0:
                return filled_quantity == quantity
            return order_quantity == quantity
        if order_quantity > 0 and quantity > order_quantity:
            return False
    return True


def _find_order_for_deliver(
    db: Session,
    external_trading_account_id: int,
    normalized: Dict[str, Any],
) -> Optional[ExternalTradingOrder]:
    if not normalized.get("is_trade", True):
        return None

    candidates: List[ExternalTradingOrder] = []
    broker_order_id = normalized.get("broker_order_id")
    entrust_no = normalized.get("entrust_no")
    for field_name, value in (("broker_order_id", broker_order_id), ("entrust_no", entrust_no)):
        if not value:
            continue
        field = getattr(ExternalTradingOrder, field_name)
        rows = (
            db.query(ExternalTradingOrder)
            .filter(
                ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
                field == str(value),
            )
            .all()
        )
        candidates.extend(
            row for row in rows
            if _deliver_order_candidate_matches(row, normalized, strict_quantity=False)
        )
    if candidates:
        return _pick_event_order(candidates)

    symbol = normalized.get("symbol")
    side = normalized.get("side")
    trade_date = normalized.get("trade_date")
    if not symbol or not side or not trade_date:
        return None
    start_dt = datetime.combine(trade_date, datetime.min.time())
    end_dt = datetime.combine(trade_date, datetime.max.time())
    rows = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
            ExternalTradingOrder.symbol == symbol,
            ExternalTradingOrder.side == side,
            or_(
                and_(ExternalTradingOrder.submitted_at >= start_dt, ExternalTradingOrder.submitted_at <= end_dt),
                and_(
                    ExternalTradingOrder.submitted_at == None,  # noqa: E711
                    ExternalTradingOrder.created_at >= start_dt,
                    ExternalTradingOrder.created_at <= end_dt,
                ),
            ),
        )
        .all()
    )
    if not rows:
        return None
    quantity = safe_int(normalized.get("quantity"))
    if quantity > 0:
        exact_rows = [
            row for row in rows
            if _deliver_order_candidate_matches(row, normalized, strict_quantity=True)
        ]
        if exact_rows:
            return _pick_event_order(exact_rows)
    return _pick_event_order(rows)


def _upsert_deliver_record(
    db: Session,
    *,
    account: ExternalTradingAccount,
    normalized: Dict[str, Any],
    matched_order: Optional[ExternalTradingOrder],
    status: str,
    message: Optional[str] = None,
) -> ExternalTradingDeliverRecord:
    row = (
        db.query(ExternalTradingDeliverRecord)
        .filter(
            ExternalTradingDeliverRecord.external_trading_account_id == account.id,
            ExternalTradingDeliverRecord.trade_date == normalized["trade_date"],
            ExternalTradingDeliverRecord.deliver_key == normalized["deliver_key"],
        )
        .first()
    )
    now = datetime.now()
    if not row:
        normalized_identity = _deliver_record_identity(normalized)
        if normalized_identity:
            same_day_rows = (
                db.query(ExternalTradingDeliverRecord)
                .filter(
                    ExternalTradingDeliverRecord.external_trading_account_id == account.id,
                    ExternalTradingDeliverRecord.trade_date == normalized["trade_date"],
                )
                .all()
            )
            for candidate in same_day_rows:
                if not isinstance(candidate.raw_record, dict):
                    continue
                candidate_normalized = normalize_deliver_record(
                    candidate.raw_record,
                    default_trade_date=candidate.trade_date,
                )
                if _deliver_record_identity(candidate_normalized) == normalized_identity:
                    row = candidate
                    row.deliver_key = normalized["deliver_key"]
                    break

    if not row:
        row = ExternalTradingDeliverRecord(
            account_id=account.account_id,
            external_trading_account_id=account.id,
            trade_date=normalized["trade_date"],
            deliver_key=normalized["deliver_key"],
            created_at=now,
        )
        db.add(row)
    row.matched_order_id = matched_order.id if matched_order else None
    row.broker_order_id = normalized.get("broker_order_id")
    row.entrust_no = normalized.get("entrust_no")
    row.symbol = normalized.get("symbol")
    row.side = normalized.get("side")
    row.quantity = safe_int(normalized.get("quantity"))
    row.price = safe_float(normalized.get("price"))
    row.amount = round_money(normalized.get("amount"))
    row.commission = round_money(normalized.get("commission"))
    row.stamp_tax = round_money(normalized.get("stamp_tax"))
    row.transfer_fee = round_money(normalized.get("transfer_fee"))
    row.other_fee = round_money(normalized.get("other_fee"))
    row.total_fee = round_money(normalized.get("total_fee"))
    row.status = status
    row.message = message
    row.raw_record = normalized.get("raw_record")
    row.reconciled_at = now if status == "MATCHED" else None
    db.flush()
    return row


def _apply_fee_delta_to_ledger(db: Session, fill: ExternalTradingOrderFill, delta_fee: float) -> None:
    delta_fee = round_money(delta_fee)
    if not fill.sub_account_id or abs(delta_fee) < 0.005:
        return
    sub_account = db.query(ExternalTradingSubAccount).filter(ExternalTradingSubAccount.id == fill.sub_account_id).first()
    position = (
        db.query(ExternalTradingLedgerPosition)
        .filter(
            ExternalTradingLedgerPosition.sub_account_id == fill.sub_account_id,
            ExternalTradingLedgerPosition.symbol == fill.symbol,
        )
        .first()
    )
    now = datetime.now()
    if sub_account:
        sub_account.cash_available = round_money(safe_float(sub_account.cash_available) - delta_fee)
        sub_account.updated_at = now
    if not position:
        return
    if fill.side == "BUY":
        current_quantity = max(safe_int(position.quantity), 0)
        if current_quantity > 0:
            position.avg_cost = round(max((safe_float(position.avg_cost) * current_quantity + delta_fee) / current_quantity, 0.0), 6)
            if position.market_price:
                position.market_value = round_money(current_quantity * safe_float(position.market_price))
    elif fill.side == "SELL":
        position.realized_pnl = round_money(safe_float(position.realized_pnl) - delta_fee)
    position.updated_at = now


def _assign_actual_fees_to_fills(
    db: Session,
    fills: List[ExternalTradingOrderFill],
    *,
    actual_commission: float,
    actual_stamp_tax: float,
    actual_fee_total: float,
    source: str,
    adjust_ledger: bool,
) -> None:
    if not fills:
        return
    weights = [safe_float(fill.amount) for fill in fills]
    commission_allocations = _allocate_money(actual_commission, weights)
    stamp_tax_allocations = _allocate_money(actual_stamp_tax, weights)
    fee_total_allocations = _allocate_money(actual_fee_total, weights)
    now = datetime.now()
    for index, fill in enumerate(fills):
        new_commission = commission_allocations[index] if index < len(commission_allocations) else 0.0
        new_stamp_tax = stamp_tax_allocations[index] if index < len(stamp_tax_allocations) else 0.0
        new_fee_total = fee_total_allocations[index] if index < len(fee_total_allocations) else 0.0
        previous_effective_fee = (
            safe_float(fill.actual_fee_total)
            if fill.actual_fee_total is not None
            else safe_float(fill.estimated_fee_total)
        )
        if adjust_ledger:
            _apply_fee_delta_to_ledger(db, fill, round_money(new_fee_total - previous_effective_fee))
        fill.actual_commission = new_commission
        fill.actual_stamp_tax = new_stamp_tax
        fill.actual_fee_total = new_fee_total
        fill.fee_reconciled_at = now
        fill.fee_source = source


def _refresh_actual_order_fees_from_fills(
    db: Session,
    order: ExternalTradingOrder,
    *,
    source: str,
    fallback_commission: float = 0.0,
    fallback_stamp_tax: float = 0.0,
    fallback_fee_total: float = 0.0,
) -> None:
    fills = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.order_id == order.id).all()
    if fills:
        order.actual_commission = round_money(sum(safe_float(fill.actual_commission) for fill in fills))
        order.actual_stamp_tax = round_money(sum(safe_float(fill.actual_stamp_tax) for fill in fills))
        order.actual_fee_total = round_money(sum(safe_float(fill.actual_fee_total) for fill in fills))
    else:
        order.actual_commission = round_money(fallback_commission)
        order.actual_stamp_tax = round_money(fallback_stamp_tax)
        order.actual_fee_total = round_money(fallback_fee_total)
    order.fee_reconciled_at = datetime.now()
    order.fee_source = source
    order.updated_at = datetime.now()


def apply_fee_reconciliation(
    db: Session,
    order: ExternalTradingOrder,
    *,
    actual_commission: float,
    actual_stamp_tax: float,
    actual_fee_total: float,
    source: str = "DELIVER",
) -> Dict[str, Any]:
    actual_commission = round_money(actual_commission)
    actual_stamp_tax = round_money(actual_stamp_tax)
    actual_fee_total = round_money(actual_fee_total)
    role = _role(order)
    updated_child_order_ids: List[int] = []

    if role == "PARENT":
        parent_fills = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.order_id == order.id).all()
        _assign_actual_fees_to_fills(
            db,
            parent_fills,
            actual_commission=actual_commission,
            actual_stamp_tax=actual_stamp_tax,
            actual_fee_total=actual_fee_total,
            source=source,
            adjust_ledger=False,
        )
        order.actual_commission = actual_commission
        order.actual_stamp_tax = actual_stamp_tax
        order.actual_fee_total = actual_fee_total
        order.fee_reconciled_at = datetime.now()
        order.fee_source = source
        order.updated_at = datetime.now()

        children = _child_orders(db, order.id)
        child_order_ids = [child.id for child in children]
        child_fills = (
            db.query(ExternalTradingOrderFill)
            .filter(ExternalTradingOrderFill.order_id.in_(child_order_ids))
            .order_by(ExternalTradingOrderFill.id.asc())
            .all()
            if child_order_ids
            else []
        )
        _assign_actual_fees_to_fills(
            db,
            child_fills,
            actual_commission=actual_commission,
            actual_stamp_tax=actual_stamp_tax,
            actual_fee_total=actual_fee_total,
            source=source,
            adjust_ledger=True,
        )
        for child in children:
            _refresh_actual_order_fees_from_fills(db, child, source=source)
            updated_child_order_ids.append(child.id)
    else:
        fills = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.order_id == order.id).all()
        _assign_actual_fees_to_fills(
            db,
            fills,
            actual_commission=actual_commission,
            actual_stamp_tax=actual_stamp_tax,
            actual_fee_total=actual_fee_total,
            source=source,
            adjust_ledger=True,
        )
        _refresh_actual_order_fees_from_fills(
            db,
            order,
            source=source,
            fallback_commission=actual_commission,
            fallback_stamp_tax=actual_stamp_tax,
            fallback_fee_total=actual_fee_total,
        )

    return {
        "order_id": order.id,
        "role": role,
        "actual_commission": actual_commission,
        "actual_stamp_tax": actual_stamp_tax,
        "actual_fee_total": actual_fee_total,
        "updated_child_order_ids": updated_child_order_ids,
    }


def reconcile_deliver_records(
    db: Session,
    *,
    account: ExternalTradingAccount,
    records: List[Dict[str, Any]],
    default_trade_date: Optional[date] = None,
) -> Dict[str, Any]:
    normalized_records = []
    fee_groups: Dict[int, Dict[str, Any]] = {}
    matched = 0
    unmatched = 0
    ignored = 0
    for raw_record in records or []:
        normalized = normalize_deliver_record(raw_record, default_trade_date=default_trade_date)
        if not normalized.get("is_trade", True):
            ignored += 1
            _upsert_deliver_record(
                db,
                account=account,
                normalized=normalized,
                matched_order=None,
                status="IGNORED",
                message=normalized.get("ignore_reason") or "非证券买卖流水，跳过订单对账",
            )
            normalized_records.append({"status": "IGNORED", "order_id": None, **normalized})
            continue
        order = _find_order_for_deliver(db, account.id, normalized)
        if order:
            matched += 1
            group = fee_groups.setdefault(order.id, {
                "order": order,
                "commission": 0.0,
                "stamp_tax": 0.0,
                "total_fee": 0.0,
                "deliver_record_ids": [],
            })
            group["commission"] = round_money(group["commission"] + safe_float(normalized.get("commission")))
            group["stamp_tax"] = round_money(group["stamp_tax"] + safe_float(normalized.get("stamp_tax")))
            group["total_fee"] = round_money(group["total_fee"] + safe_float(normalized.get("total_fee")))
            deliver_row = _upsert_deliver_record(
                db,
                account=account,
                normalized=normalized,
                matched_order=order,
                status="MATCHED",
                message=None,
            )
            group["deliver_record_ids"].append(deliver_row.id)
            normalized_records.append({"status": "MATCHED", "order_id": order.id, **normalized})
        else:
            unmatched += 1
            _upsert_deliver_record(
                db,
                account=account,
                normalized=normalized,
                matched_order=None,
                status="UNMATCHED",
                message="未找到对应本地订单",
            )
            normalized_records.append({"status": "UNMATCHED", "order_id": None, **normalized})

    applied = []
    for group in fee_groups.values():
        order = group["order"]
        applied_item = apply_fee_reconciliation(
            db,
            order,
            actual_commission=group["commission"],
            actual_stamp_tax=group["stamp_tax"],
            actual_fee_total=group["total_fee"],
            source="DELIVER",
        )
        applied_item["deliver_record_ids"] = group["deliver_record_ids"]
        applied.append(applied_item)

    return {
        "received": len(records or []),
        "matched": matched,
        "unmatched": unmatched,
        "ignored": ignored,
        "applied_order_count": len(applied),
        "applied": applied,
        "records": normalized_records,
    }


def _reference_price_for_symbol(reference_prices: Dict[str, float], symbol: str) -> float:
    normalized = normalize_symbol(symbol)
    return safe_float(
        reference_prices.get(normalized),
        safe_float(reference_prices.get(str(symbol or "").upper())),
    )


def collect_internal_cross_reference_symbols(plan: Optional[Dict[str, Any]]) -> List[str]:
    symbols: List[str] = []
    for cross in (plan or {}).get("internal_crosses") or []:
        symbol = normalize_symbol(cross.get("symbol"))
        if not symbol or safe_int(cross.get("quantity")) <= 0:
            continue
        if safe_float(cross.get("price")) > 0 and str(cross.get("status") or "").upper() == "READY":
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _subtract_from_demands(demands: List[Dict[str, Any]], quantity: int) -> List[Dict[str, Any]]:
    allocations = []
    remaining = max(quantity, 0)
    for demand in demands:
        if remaining <= 0:
            break
        available = safe_int(demand.get("remaining_quantity"), demand.get("quantity"))
        matched = min(available, remaining)
        if matched <= 0:
            continue
        demand["remaining_quantity"] = available - matched
        allocation = dict(demand)
        allocation["quantity"] = matched
        allocation["remaining_quantity"] = matched
        allocations.append(allocation)
        remaining -= matched
    return allocations


def _build_demand_rows(
    db: Session,
    *,
    account_id: Optional[str],
    external_trading_account_id: int,
    sub_account_ids: Optional[List[int]] = None,
    execution_policy_fallback: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    external_account = (
        db.query(ExternalTradingAccount)
        .filter(ExternalTradingAccount.id == external_trading_account_id)
        .first()
    )
    account_scope_query = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.external_trading_account_id == external_trading_account_id,
    )
    if account_id:
        account_scope_query = account_scope_query.filter(ExternalTradingSubAccount.account_id == account_id)
    account_scope_sub_accounts = {row.id: row for row in account_scope_query.all()}
    sub_accounts = {
        row_id: row
        for row_id, row in account_scope_sub_accounts.items()
        if row.enabled
    }
    if sub_account_ids:
        requested_ids = {safe_int(item) for item in sub_account_ids}
        sub_accounts = {row_id: row for row_id, row in sub_accounts.items() if row_id in requested_ids}
    if not sub_accounts:
        return []

    target_rows = (
        db.query(ExternalTradingTargetPosition)
        .filter(
            ExternalTradingTargetPosition.external_trading_account_id == external_trading_account_id,
            ExternalTradingTargetPosition.sub_account_id.in_(list(sub_accounts.keys())),
            ExternalTradingTargetPosition.status == "ACTIVE",
        )
        .order_by(ExternalTradingTargetPosition.sub_account_id.asc(), ExternalTradingTargetPosition.symbol.asc())
        .all()
    )
    ledger_by_sub_account = {
        sub_account_id: get_ledger_positions(db, sub_account_id)
        for sub_account_id in sub_accounts.keys()
    }
    today_buy_by_key = get_today_buy_quantities(db, list(sub_accounts.keys()))
    open_by_sub_account = {
        sub_account_id: get_open_order_quantities(db, sub_account_id)
        for sub_account_id in sub_accounts.keys()
    }
    now = datetime.now()
    active_blocks = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.external_trading_account_id == external_trading_account_id,
            ExternalTradingOrder.sub_account_id.in_(list(sub_accounts.keys())),
            ExternalTradingOrder.status.in_(list(BLOCKED_ORDER_STATUSES)),
        )
        .all()
    )
    block_by_key: Dict[Tuple[int, Optional[str], str], List[ExternalTradingOrder]] = {}
    for row in active_blocks:
        if not row.sub_account_id or not row.symbol:
            continue
        side = str(row.side or "").upper()
        if side not in {"BUY", "SELL"} or not _block_is_active_for_plan(row, now):
            continue
        key = (safe_int(row.sub_account_id), normalize_symbol(row.symbol), side)
        block_by_key.setdefault(key, []).append(row)
    for rows in block_by_key.values():
        rows.sort(key=_block_priority)
    demands: List[Dict[str, Any]] = []
    for target in target_rows:
        sub_account = sub_accounts.get(target.sub_account_id)
        symbol = normalize_symbol(target.symbol)
        if not sub_account or not symbol:
            continue
        ledger_position = ledger_by_sub_account.get(sub_account.id, {}).get(symbol)
        current_quantity = safe_int(getattr(ledger_position, "quantity", 0))
        available_quantity_base = safe_int(getattr(ledger_position, "available_quantity", current_quantity))
        today_buy_quantity = today_buy_by_key.get((sub_account.id, symbol), 0)
        sellability = compute_sellability(
            symbol,
            quantity=current_quantity,
            available_quantity=available_quantity_base,
            today_buy_quantity=today_buy_quantity,
        )
        open_quantities = open_by_sub_account.get(sub_account.id, {}).get(symbol, {})
        pending_buy = safe_int(open_quantities.get("BUY"))
        pending_sell = safe_int(open_quantities.get("SELL"))
        effective_quantity = current_quantity + pending_buy - pending_sell
        target_quantity = safe_int(target.target_quantity)
        delta = target_quantity - effective_quantity
        if delta == 0:
            continue
        side = "BUY" if delta > 0 else "SELL"
        quantity = abs(delta)
        available_quantity = 0
        if side == "SELL":
            available_quantity = max(safe_int(sellability.get("computed_sellable_quantity")) - pending_sell, 0)
            quantity = min(quantity, available_quantity)
        if quantity <= 0:
            continue
        execution_policy = resolve_execution_policy(
            external_account,
            sub_account,
            fallback=execution_policy_fallback,
        )
        blocked_order = None
        for candidate in block_by_key.get((sub_account.id, symbol, side), []):
            if _block_matches_demand(
                candidate,
                now=now,
                symbol=symbol,
                side=side,
                quantity=quantity,
                available_quantity=available_quantity,
            ):
                blocked_order = candidate
                break
        demands.append({
            "account_id": sub_account.account_id,
            "external_trading_account_id": external_trading_account_id,
            "sub_account_id": sub_account.id,
            "sub_account_name": sub_account.name,
            "strategy_type": sub_account.strategy_type,
            "strategy_config_id": sub_account.strategy_config_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "remaining_quantity": quantity,
            "current_quantity": current_quantity,
            "available_quantity": available_quantity if side == "SELL" else 0,
            "raw_available_quantity": available_quantity_base,
            "computed_sellable_quantity": safe_int(sellability.get("computed_sellable_quantity")),
            "sellable_quantity": safe_int(sellability.get("computed_sellable_quantity")),
            "t1_locked_quantity": safe_int(sellability.get("t1_locked_quantity")),
            "today_buy_quantity": today_buy_quantity,
            "sellable_rule": sellability.get("sellable_rule"),
            "sellable_security_type": sellability.get("sellable_security_type"),
            "target_quantity": target_quantity,
            "effective_quantity": effective_quantity,
            "pending_buy_quantity": pending_buy,
            "pending_sell_quantity": pending_sell,
            "signal_id": target.signal_id,
            "signal_version": target.signal_version,
            "source_execution_id": target.source_execution_id,
            "target_updated_at": target.updated_at.isoformat() if target.updated_at else None,
            "reference_price": safe_float(target.reference_price, None),
            "reference_price_source": target.reference_price_source,
            "execution_policy": execution_policy,
            "price_level": execution_policy.get("price_level"),
            "lot_size": execution_policy.get("lot_size"),
            "order_timeout_seconds": execution_policy.get("order_timeout_seconds"),
            "max_replace_count": execution_policy.get("max_replace_count"),
            "price_level_sequence": execution_policy.get("price_level_sequence"),
            "blocked": blocked_order is not None,
            "blocked_reason": blocked_order.cancel_reason if blocked_order else None,
            "blocked_status": blocked_order.status if blocked_order else None,
            "blocked_until": blocked_order.deadline_at.isoformat() if blocked_order and blocked_order.deadline_at else None,
            "blocked_order_id": blocked_order.id if blocked_order else None,
            "blocked_quantity": safe_int(blocked_order.quantity) if blocked_order else 0,
            "blocked_message": blocked_order.message if blocked_order else None,
        })
    return demands


def build_netted_target_execution_plan(
    db: Session,
    *,
    account_id: Optional[str],
    external_trading_account_id: int,
    sub_account_ids: Optional[List[int]] = None,
    price_level: int = 1,
    lot_size: int = 100,
    order_timeout_seconds: int = DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    max_replace_count: int = DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
    clip_sell_to_available: bool = DEFAULT_EXECUTOR_CLIP_SELL_TO_AVAILABLE,
    price_level_sequence: Optional[List[int]] = None,
    reference_prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    reference_prices = reference_prices or {}
    lot_size = max(safe_int(lot_size, 100), 1)
    demands = _build_demand_rows(
        db,
        account_id=account_id,
        external_trading_account_id=external_trading_account_id,
        sub_account_ids=sub_account_ids,
        execution_policy_fallback={
            "price_level": price_level,
            "lot_size": lot_size,
            "order_timeout_seconds": order_timeout_seconds,
            "max_replace_count": max_replace_count,
            "clip_sell_to_available": clip_sell_to_available,
            "price_level_sequence": price_level_sequence,
        },
    )
    demands_by_symbol: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    blocked_demands = []
    for demand in demands:
        if demand.get("blocked"):
            blocked_demands.append(demand)
            continue
        symbol_bucket = demands_by_symbol.setdefault(demand["symbol"], {"BUY": [], "SELL": []})
        symbol_bucket[demand["side"]].append(dict(demand))

    internal_crosses = []
    external_orders = []
    skipped = []
    for symbol in sorted(demands_by_symbol.keys()):
        bucket = demands_by_symbol[symbol]
        buy_demands = bucket["BUY"]
        sell_demands = bucket["SELL"]
        total_buy = sum(safe_int(item.get("remaining_quantity")) for item in buy_demands)
        total_sell = sum(safe_int(item.get("remaining_quantity")) for item in sell_demands)
        cross_quantity = min(total_buy, total_sell)
        if cross_quantity > 0:
            price = _reference_price_for_symbol(reference_prices, symbol)
            buy_allocations = _subtract_from_demands(buy_demands, cross_quantity)
            sell_allocations = _subtract_from_demands(sell_demands, cross_quantity)
            internal_crosses.append({
                "symbol": symbol,
                "quantity": cross_quantity,
                "price": price or None,
                "buy_allocations": buy_allocations,
                "sell_allocations": sell_allocations,
                "status": "READY" if price > 0 else "MISSING_PRICE",
                "message": "" if price > 0 else "内部撮合缺少参考价，执行时会跳过",
            })

        for side, side_demands in (("BUY", buy_demands), ("SELL", sell_demands)):
            allocations = [
                {**item, "quantity": safe_int(item.get("remaining_quantity"))}
                for item in side_demands
                if safe_int(item.get("remaining_quantity")) > 0
            ]
            order_policy = aggregate_execution_policy(
                [item.get("execution_policy") or {} for item in allocations],
                fallback={
                    "price_level": price_level,
                    "lot_size": lot_size,
                    "order_timeout_seconds": order_timeout_seconds,
                    "max_replace_count": max_replace_count,
                    "clip_sell_to_available": clip_sell_to_available,
                    "price_level_sequence": price_level_sequence,
                },
            )
            order_lot_size = max(safe_int(order_policy.get("lot_size"), lot_size), 1)
            quantity = sum(safe_int(item.get("quantity")) for item in allocations)
            if quantity <= 0:
                continue
            if side == "BUY":
                raw_buy_quantity = quantity
                quantity = (quantity // order_lot_size) * order_lot_size
                if quantity <= 0 and allocations:
                    skipped.append({
                        "symbol": symbol,
                        "side": side,
                        "quantity": raw_buy_quantity,
                        "reason": "SKIPPED_INVALID_LOT",
                        "message": "净买入数量不足最小交易单位",
                    })
                    continue
                remaining_to_keep = quantity
                trimmed_allocations = []
                for allocation in allocations:
                    if remaining_to_keep <= 0:
                        break
                    kept = min(safe_int(allocation.get("quantity")), remaining_to_keep)
                    if kept > 0:
                        trimmed = dict(allocation)
                        trimmed["quantity"] = kept
                        trimmed_allocations.append(trimmed)
                        remaining_to_keep -= kept
                allocations = trimmed_allocations
            if quantity <= 0:
                continue
            external_orders.append({
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "price_level": order_policy.get("price_level"),
                "clip_sell_to_available": order_policy.get("clip_sell_to_available"),
                "execution_pricing": "PTRATE_SNAPSHOT_AT_ORDER_TIME",
                "remark": "netted target position execution",
                "execution_policy": order_policy,
                "allocations": allocations,
            })
    external_orders = _filter_parent_orders_by_non_retryable_blocks(
        db,
        account_id=account_id,
        external_trading_account_id=external_trading_account_id,
        now=datetime.now(),
        external_orders=external_orders,
        skipped=skipped,
    )
    for demand in blocked_demands:
        skipped.append({
            "symbol": demand.get("symbol"),
            "side": demand.get("side"),
            "quantity": demand.get("quantity"),
            "sub_account_id": demand.get("sub_account_id"),
            "sub_account_name": demand.get("sub_account_name"),
            "reason": demand.get("blocked_reason"),
            "blocked_until": demand.get("blocked_until"),
            "blocked_status": demand.get("blocked_status"),
            "blocked_order_id": demand.get("blocked_order_id"),
            "message": demand.get("blocked_message") or "可卖数量不足，下一交易日再重试",
        })

    external_orders.sort(key=lambda row: (0 if str(row.get("side") or "").upper() == "SELL" else 1, str(row.get("symbol") or "")))

    return {
        "external_trading_account_id": external_trading_account_id,
        "price_level": price_level,
        "lot_size": lot_size,
        "order_timeout_seconds": order_timeout_seconds,
        "max_replace_count": max_replace_count,
        "price_level_sequence": price_level_sequence,
        "symbols": sorted(demands_by_symbol.keys()),
        "demands": demands,
        "blocked_demands": blocked_demands,
        "internal_crosses": internal_crosses,
        "external_orders": external_orders,
        "skipped": skipped,
    }


def apply_internal_crosses(db: Session, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    applied = []
    now = datetime.now()
    for cross in plan.get("internal_crosses") or []:
        price = safe_float(cross.get("price"))
        if price <= 0:
            continue
        for side_key, side in (("sell_allocations", "SELL"), ("buy_allocations", "BUY")):
            for allocation in cross.get(side_key) or []:
                quantity = safe_int(allocation.get("quantity"))
                if quantity <= 0:
                    continue
                client_order_id = uuid.uuid4().hex
                order = ExternalTradingOrder(
                    account_id=allocation.get("account_id"),
                    external_trading_account_id=safe_int(allocation.get("external_trading_account_id")),
                    sub_account_id=safe_int(allocation.get("sub_account_id")),
                    strategy_type=allocation.get("strategy_type"),
                    strategy_config_id=allocation.get("strategy_config_id"),
                    execution_id=safe_int(allocation.get("source_execution_id") or allocation.get("execution_id"), None),
                    allocation_role="INTERNAL",
                    client_order_id=client_order_id,
                    symbol=normalize_symbol(allocation.get("symbol")),
                    side=side,
                    order_type="INTERNAL",
                    signal_version=allocation.get("signal_version"),
                    submitted_price=price,
                    quantity=quantity,
                    filled_quantity=0,
                    remaining_quantity=quantity,
                    avg_fill_price=price,
                    status="FILLED",
                    submitted_at=now,
                    last_event_at=now,
                    raw_request={
                        "type": "internal_cross",
                        "cross": {
                            "symbol": cross.get("symbol"),
                            "quantity": cross.get("quantity"),
                            "price": price,
                        },
                        "allocation": allocation,
                    },
                    created_at=now,
                    updated_at=now,
                )
                db.add(order)
                db.flush()
                _insert_fill_row(
                    db,
                    order=order,
                    fill_key=f"internal:{client_order_id}",
                    quantity=quantity,
                    price=price,
                    traded_at=now,
                    event={"type": "internal_cross", "allocation": allocation, "cross_symbol": cross.get("symbol")},
                )
                _apply_fill_to_ledger(db, order, quantity, price)
                _refresh_order_from_fill_totals(db, order)
                applied.append(serialize_order(order))
    return applied


def create_netted_execution_orders(
    db: Session,
    *,
    account_id: str,
    external_trading_account_id: int,
    orders: List[Dict[str, Any]],
    deadline_at: Optional[datetime] = None,
    executor_trigger: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[ExternalTradingOrder]]:
    enriched_orders: List[Dict[str, Any]] = []
    parent_rows: List[ExternalTradingOrder] = []
    now = datetime.now()
    for order in orders:
        parent_client_order_id = uuid.uuid4().hex
        symbol = normalize_symbol(order.get("symbol"))
        side = str(order.get("side") or "").upper()
        quantity = safe_int(order.get("quantity"))
        order_deadline_at = parse_dt(order.get("deadline_at")) or deadline_at
        signal_versions = sorted({
            str(allocation.get("signal_version"))
            for allocation in (order.get("allocations") or [])
            if allocation.get("signal_version")
        })
        parent_signal_version = ",".join(signal_versions)[:64] if signal_versions else order.get("signal_version")
        replace_count = safe_int(order.get("replace_count"))
        parent = ExternalTradingOrder(
            account_id=account_id,
            external_trading_account_id=external_trading_account_id,
            strategy_type=STRATEGY_NETTED_EXECUTOR,
            allocation_role="PARENT",
            client_order_id=parent_client_order_id,
            symbol=symbol,
            side=side,
            order_type="LIMIT",
            price_level=order.get("price_level"),
            signal_version=parent_signal_version,
            replace_count=replace_count,
            deadline_at=order_deadline_at,
            executor_trigger=executor_trigger,
            quantity=quantity,
            filled_quantity=0,
            remaining_quantity=quantity,
            status="CREATED",
            raw_request=order,
            created_at=now,
            updated_at=now,
        )
        db.add(parent)
        db.flush()
        child_summaries = []
        for allocation in order.get("allocations") or []:
            child_quantity = safe_int(allocation.get("quantity"))
            if child_quantity <= 0:
                continue
            child_client_order_id = uuid.uuid4().hex
            child = ExternalTradingOrder(
                account_id=allocation.get("account_id") or account_id,
                external_trading_account_id=external_trading_account_id,
                sub_account_id=safe_int(allocation.get("sub_account_id")),
                strategy_type=allocation.get("strategy_type"),
                strategy_config_id=allocation.get("strategy_config_id"),
                execution_id=safe_int(allocation.get("source_execution_id") or allocation.get("execution_id"), None),
                parent_order_id=parent.id,
                allocation_role="CHILD",
                client_order_id=child_client_order_id,
                symbol=symbol,
                side=side,
                order_type="LIMIT",
                price_level=order.get("price_level"),
                signal_version=allocation.get("signal_version"),
                replace_count=replace_count,
                deadline_at=order_deadline_at,
                executor_trigger=executor_trigger,
                quantity=child_quantity,
                filled_quantity=0,
                remaining_quantity=child_quantity,
                status="CREATED",
                raw_request=allocation,
                created_at=now,
                updated_at=now,
            )
            db.add(child)
            child_summaries.append({
                "client_order_id": child_client_order_id,
                "sub_account_id": child.sub_account_id,
                "strategy_type": child.strategy_type,
                "strategy_config_id": child.strategy_config_id,
                "quantity": child_quantity,
            })
        enriched = {
            key: value
            for key, value in dict(order).items()
            if key != "allocations"
        }
        enriched["client_order_id"] = parent_client_order_id
        enriched["order_type"] = "LIMIT"
        enriched["replace_count"] = replace_count
        enriched["allocations"] = child_summaries
        enriched_orders.append(enriched)
        parent_rows.append(parent)
    db.flush()
    return enriched_orders, parent_rows


def serialize_order(row: ExternalTradingOrder) -> Dict[str, Any]:
    return {
        "id": row.id,
        "sub_account_id": row.sub_account_id,
        "strategy_type": row.strategy_type,
        "strategy_config_id": row.strategy_config_id,
        "execution_id": row.execution_id,
        "parent_order_id": row.parent_order_id,
        "allocation_role": row.allocation_role,
        "client_order_id": row.client_order_id,
        "broker_order_id": row.broker_order_id,
        "entrust_no": row.entrust_no,
        "symbol": row.symbol,
        "side": row.side,
        "order_type": row.order_type,
        "price_level": row.price_level,
        "signal_version": row.signal_version,
        "replace_count": row.replace_count,
        "replaced_by_order_id": row.replaced_by_order_id,
        "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
        "cancel_reason": row.cancel_reason,
        "executor_trigger": row.executor_trigger,
        "submitted_price": row.submitted_price,
        "quantity": row.quantity,
        "filled_quantity": row.filled_quantity,
        "remaining_quantity": row.remaining_quantity,
        "avg_fill_price": row.avg_fill_price,
        "status": row.status,
        "ptrade_status": row.ptrade_status,
        "message": row.message,
        "estimated_commission": row.estimated_commission,
        "estimated_stamp_tax": row.estimated_stamp_tax,
        "estimated_fee_total": row.estimated_fee_total,
        "actual_commission": row.actual_commission,
        "actual_stamp_tax": row.actual_stamp_tax,
        "actual_fee_total": row.actual_fee_total,
        "fee_reconciled_at": row.fee_reconciled_at.isoformat() if row.fee_reconciled_at else None,
        "fee_source": row.fee_source,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "last_event_at": row.last_event_at.isoformat() if row.last_event_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
