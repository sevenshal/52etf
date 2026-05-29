import asyncio
from datetime import date, datetime, timedelta
import logging
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, root_validator, validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session as OrmSession

from ...core.database import (
    FactorLiveTradingConfig,
    PortfolioCopyConfig,
    SnowballCopyConfig,
    get_db,
)
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBrokerPositionSnapshot,
    ExternalTradingDeliverRecord,
    ExternalTradingEventLog,
    ExternalTradingLedgerPosition,
    ExternalTradingOrder,
    ExternalTradingOrderFill,
    ExternalTradingSubAccount,
    ExternalTradingSubAccountNetAssetHistory,
    ExternalTradingTargetPosition,
    ExternalTradingSessionLocal as ExternalTradingDBSession,
    get_external_trading_db,
)
from ...core.services.external_trading import (
    ExternalTradingConnectionError,
    external_trading_hub,
)
from ...core.services.external_trading_executor import trigger_external_trading_executor
from ...core.services.external_trading_execution_policy import (
    ALLOWED_EXECUTOR_PRICE_LEVELS,
    DEFAULT_EXECUTOR_LOT_SIZE,
    DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
    DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT,
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE,
    DEFAULT_EXECUTOR_PRICE_LEVEL,
    DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE,
    MAX_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    normalize_lot_size,
    normalize_max_replace_count,
    normalize_max_slippage_pct,
    normalize_price_level,
    normalize_price_level_sequence,
    normalize_timeout_seconds,
    normalize_timeout_seconds_sequence,
    resolve_execution_policy,
)
from ...core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_A_STOCK,
    EXTERNAL_TRADING_MARKET_US_STOCK,
    external_trading_market_label,
    is_external_trading_market_open,
    normalize_external_trading_market_type,
)

from ...core.services.external_trading_ledger import (
    ACTIVE_ORDER_STATUSES,
    STATUS_BLOCKED_INSUFFICIENT_POSITION,
    STRATEGY_SNOWBALL,
    STRATEGY_PORTFOLIO_COPY,
    STRATEGY_FACTOR_LIVE,
    STRATEGY_W20,
    build_netted_target_execution_plan,
    build_broker_position_diff,
    collect_internal_cross_reference_symbols,
    compute_position_sellability,
    empty_sub_account_fee_summary,
    get_account_ledger_positions,
    get_external_account_fee_summary,
    get_latest_broker_position_snapshot,
    get_ledger_positions,
    get_today_buy_quantities,
    get_open_order_quantities,
    get_sub_account_fee_summaries,
    persist_broker_position_snapshot,
    mark_block_order_manual_success,
    normalize_symbol,
    repair_parent_order_manual_fill,
    resolve_manual_block_fill_price,
    safe_int,
    serialize_broker_position_snapshot,
    serialize_ledger_position,
    serialize_order,
    serialize_sub_account,
)
from ...core.services.external_trading_valuation import (
    ExternalTradingValuationError,
    calculate_sub_account_net_asset,
    get_realtime_price_details,
    get_realtime_reference_prices,
)
from ...core.services.external_trading_crypto import (
    ExternalTradingCryptoError,
    verify_handshake_signature,
)
from ...core.services.symbol_names import load_symbol_name_map, normalize_symbol_for_name
from .account import is_valid_account, valid_account

router = APIRouter(prefix="/api/external-trading-accounts", tags=["external-trading-accounts"])
logger = logging.getLogger(__name__)


def _ensure_execution_sequence_lengths(
    *,
    price_level_sequence: List[int],
    order_timeout_seconds_sequence: List[int],
    max_replace_count: int,
) -> None:
    required_length = normalize_max_replace_count(max_replace_count) + 1
    if len(price_level_sequence or []) < required_length:
        raise ValueError(f"executor_price_level_sequence length must be greater than max_replace_count ({max_replace_count})")
    if len(order_timeout_seconds_sequence or []) < required_length:
        raise ValueError(
            f"executor_order_timeout_seconds_sequence length must be greater than max_replace_count ({max_replace_count})"
        )


def _validation_http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


class ExternalTradingAccountBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    identifier: str = Field(..., min_length=1, max_length=128)
    market_type: str = Field(default=EXTERNAL_TRADING_MARKET_A_STOCK)
    enabled: bool = True
    executor_price_level: int = Field(default=DEFAULT_EXECUTOR_PRICE_LEVEL)
    executor_lot_size: int = Field(default=DEFAULT_EXECUTOR_LOT_SIZE, ge=1)
    executor_order_timeout_seconds: int = Field(
        default=DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
        ge=10,
        le=MAX_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    )
    executor_max_replace_count: int = Field(default=DEFAULT_EXECUTOR_MAX_REPLACE_COUNT, ge=0, le=20)
    executor_max_slippage_pct: float = Field(default=DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT, ge=0)
    executor_price_level_sequence: List[int] = Field(default_factory=lambda: DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE.copy())
    executor_order_timeout_seconds_sequence: List[int] = Field(
        default_factory=lambda: DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE.copy()
    )
    commission_rate_pct: float = Field(default=0.025, ge=0)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax_rate_pct: float = Field(default=0.05, ge=0)

    @root_validator(pre=True)
    def apply_market_defaults(cls, values):
        if not isinstance(values, dict):
            return values
        market_type = normalize_external_trading_market_type(values.get("market_type"))
        if market_type == EXTERNAL_TRADING_MARKET_US_STOCK:
            values.setdefault("executor_lot_size", 1)
            values.setdefault("stamp_tax_rate_pct", 0)
        return values

    @validator("name", "identifier", pre=True)
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @validator("market_type", pre=True, always=True)
    def validate_market_type(cls, value):
        return normalize_external_trading_market_type(value)

    @validator("executor_price_level")
    def validate_executor_price_level(cls, value):
        if value not in ALLOWED_EXECUTOR_PRICE_LEVELS:
            raise ValueError("executor_price_level must be one of -1, 0, 1, 2, 3, 4, 5")
        return value

    @validator("executor_price_level_sequence", pre=True)
    def validate_executor_price_level_sequence(cls, value):
        sequence = normalize_price_level_sequence(value)
        if not sequence:
            raise ValueError("executor_price_level_sequence is invalid")
        return sequence

    @validator("executor_order_timeout_seconds_sequence", pre=True)
    def validate_executor_order_timeout_seconds_sequence(cls, value):
        sequence = normalize_timeout_seconds_sequence(value)
        if not sequence:
            raise ValueError("executor_order_timeout_seconds_sequence is invalid")
        return sequence

    @root_validator(skip_on_failure=True)
    def validate_executor_sequence_lengths(cls, values):
        _ensure_execution_sequence_lengths(
            price_level_sequence=values.get("executor_price_level_sequence") or DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE,
            order_timeout_seconds_sequence=(
                values.get("executor_order_timeout_seconds_sequence")
                or DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE
            ),
            max_replace_count=values.get("executor_max_replace_count", DEFAULT_EXECUTOR_MAX_REPLACE_COUNT),
        )
        return values


class ExternalTradingAccountCreate(ExternalTradingAccountBase):
    pass


class ExternalTradingAccountUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    identifier: Optional[str] = Field(default=None, min_length=1, max_length=128)
    market_type: Optional[str] = None
    enabled: Optional[bool] = None
    executor_price_level: Optional[int] = None
    executor_lot_size: Optional[int] = Field(default=None, ge=1)
    executor_order_timeout_seconds: Optional[int] = Field(
        default=None,
        ge=10,
        le=MAX_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    )
    executor_max_replace_count: Optional[int] = Field(default=None, ge=0, le=20)
    executor_max_slippage_pct: Optional[float] = Field(default=None, ge=0)
    executor_price_level_sequence: Optional[List[int]] = None
    executor_order_timeout_seconds_sequence: Optional[List[int]] = None
    commission_rate_pct: Optional[float] = Field(default=None, ge=0)
    min_commission: Optional[float] = Field(default=None, ge=0)
    stamp_tax_rate_pct: Optional[float] = Field(default=None, ge=0)

    @root_validator(pre=True)
    def apply_market_defaults(cls, values):
        if not isinstance(values, dict) or "market_type" not in values or values.get("market_type") is None:
            return values
        market_type = normalize_external_trading_market_type(values.get("market_type"))
        if market_type == EXTERNAL_TRADING_MARKET_US_STOCK:
            values.setdefault("executor_lot_size", 1)
            values.setdefault("stamp_tax_rate_pct", 0)
        elif market_type == EXTERNAL_TRADING_MARKET_A_STOCK:
            values.setdefault("executor_lot_size", DEFAULT_EXECUTOR_LOT_SIZE)
            values.setdefault("stamp_tax_rate_pct", 0.05)
        return values

    @validator("name", "identifier", pre=True)
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @validator("market_type", pre=True)
    def validate_market_type(cls, value):
        if value is None:
            return value
        return normalize_external_trading_market_type(value)

    @validator("executor_price_level")
    def validate_executor_price_level(cls, value):
        if value is not None and value not in ALLOWED_EXECUTOR_PRICE_LEVELS:
            raise ValueError("executor_price_level must be one of -1, 0, 1, 2, 3, 4, 5")
        return value

    @validator("executor_price_level_sequence", pre=True)
    def validate_executor_price_level_sequence(cls, value):
        if value is None:
            return value
        return normalize_price_level_sequence(value)

    @validator("executor_order_timeout_seconds_sequence", pre=True)
    def validate_executor_order_timeout_seconds_sequence(cls, value):
        if value is None:
            return value
        return normalize_timeout_seconds_sequence(value)

    @root_validator(skip_on_failure=True)
    def validate_executor_sequence_lengths(cls, values):
        max_replace_count = values.get("executor_max_replace_count")
        price_sequence = values.get("executor_price_level_sequence")
        timeout_sequence = values.get("executor_order_timeout_seconds_sequence")
        if max_replace_count is not None and price_sequence is not None and timeout_sequence is not None:
            _ensure_execution_sequence_lengths(
                price_level_sequence=price_sequence,
                order_timeout_seconds_sequence=timeout_sequence,
                max_replace_count=max_replace_count,
            )
        return values


class ExternalTradingAccountResponse(ExternalTradingAccountBase):
    id: int
    account_id: str
    connected: bool = False
    pending_count: int = 0
    connected_at: Optional[datetime] = None
    runtime_last_seen_at: Optional[datetime] = None
    last_connected_at: Optional[datetime] = None
    last_disconnected_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    last_disconnect_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class QuoteRequest(BaseModel):
    symbols: List[str] = Field(..., min_items=1)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)


class OrderInstruction(BaseModel):
    symbol: str
    side: str
    quantity: int = Field(..., gt=0)
    order_type: Optional[str] = "LIMIT"
    market_type: Optional[int] = Field(default=0, ge=0, le=5)
    price: Optional[float] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    protection_limit_price: Optional[float] = Field(default=None, gt=0)
    market_limit_price: Optional[float] = Field(default=None, gt=0)
    clip_sell_to_available: Optional[bool] = None
    remark: Optional[str] = None

    @validator("side")
    def normalize_side(cls, value):
        upper = (value or "").upper()
        if upper not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        return upper

    @validator("order_type")
    def normalize_order_type(cls, value):
        upper = (value or "LIMIT").upper()
        if upper in ("MKT", "MARKET"):
            return "MARKET"
        if upper != "LIMIT":
            raise ValueError("order_type must be LIMIT or MARKET")
        return upper


class OrderBatchRequest(BaseModel):
    orders: List[OrderInstruction] = Field(..., min_items=1)
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)


class OrderCancelInstruction(BaseModel):
    order_id: str
    client_order_id: Optional[str] = None


class OrderCancelBatchRequest(BaseModel):
    orders: List[OrderCancelInstruction] = Field(..., min_items=1)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)


class ManualBlockSuccessRequest(BaseModel):
    price: Optional[float] = Field(default=None, gt=0)
    traded_at: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=500)

    @validator("note", pre=True)
    def strip_note(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value


class ManualParentFillRepairRequest(BaseModel):
    price: float = Field(..., gt=0)
    traded_at: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=500)

    @validator("note", pre=True)
    def strip_note(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value


class ExternalTradingSubAccountPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    cash_allocated: float = Field(default=0.0, ge=0)
    remark: Optional[str] = Field(default=None, max_length=1000)
    enabled: bool = True
    executor_price_level: Optional[int] = None
    executor_lot_size: Optional[int] = Field(default=None, ge=1)
    executor_order_timeout_seconds: Optional[int] = Field(
        default=None,
        ge=10,
        le=MAX_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    )
    executor_max_replace_count: Optional[int] = Field(default=None, ge=0, le=20)
    executor_max_slippage_pct: Optional[float] = Field(default=None, ge=0)
    executor_price_level_sequence: Optional[List[int]] = None
    executor_order_timeout_seconds_sequence: Optional[List[int]] = None

    @validator("name", "remark", pre=True)
    def strip_sub_account_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @validator("executor_price_level")
    def validate_executor_price_level(cls, value):
        if value is not None and value not in ALLOWED_EXECUTOR_PRICE_LEVELS:
            raise ValueError("executor_price_level must be one of -1, 0, 1, 2, 3, 4, 5")
        return value

    @validator("executor_price_level_sequence", pre=True)
    def validate_executor_price_level_sequence(cls, value):
        if value is None or value == "":
            return None
        return normalize_price_level_sequence(value)

    @validator("executor_order_timeout_seconds_sequence", pre=True)
    def validate_executor_order_timeout_seconds_sequence(cls, value):
        if value is None or value == "":
            return None
        return normalize_timeout_seconds_sequence(value)

    @root_validator(skip_on_failure=True)
    def validate_executor_sequence_lengths(cls, values):
        max_replace_count = values.get("executor_max_replace_count")
        price_sequence = values.get("executor_price_level_sequence")
        timeout_sequence = values.get("executor_order_timeout_seconds_sequence")
        if max_replace_count is not None and price_sequence is not None and timeout_sequence is not None:
            _ensure_execution_sequence_lengths(
                price_level_sequence=price_sequence,
                order_timeout_seconds_sequence=timeout_sequence,
                max_replace_count=max_replace_count,
            )
        return values


class NettedExecutorRequest(BaseModel):
    sub_account_ids: Optional[List[int]] = None
    price_level: Optional[int] = None
    lot_size: Optional[int] = Field(default=None, ge=1)
    timeout_seconds: Optional[float] = Field(default=None, ge=10.0, le=float(MAX_EXECUTOR_ORDER_TIMEOUT_SECONDS))
    force: bool = False

    @validator("price_level")
    def validate_price_level(cls, value):
        if value is not None and value not in ALLOWED_EXECUTOR_PRICE_LEVELS:
            raise ValueError("price_level must be one of -1, 0, 1, 2, 3, 4, 5")
        return value





def _serialize_account(account: ExternalTradingAccount) -> Dict[str, Any]:
    runtime_status = external_trading_hub.get_status(account.id)
    executor_price_level = normalize_price_level(getattr(account, "executor_price_level", None))
    executor_lot_size = normalize_lot_size(getattr(account, "executor_lot_size", None))
    executor_order_timeout_seconds = normalize_timeout_seconds(getattr(account, "executor_order_timeout_seconds", None))
    executor_max_replace_count = normalize_max_replace_count(getattr(account, "executor_max_replace_count", None))
    executor_max_slippage_pct = normalize_max_slippage_pct(getattr(account, "executor_max_slippage_pct", None))
    executor_clip_sell_to_available = True
    executor_price_level_sequence = normalize_price_level_sequence(
        getattr(account, "executor_price_level_sequence", None)
    )
    executor_order_timeout_seconds_sequence = normalize_timeout_seconds_sequence(
        getattr(account, "executor_order_timeout_seconds_sequence", None),
        default=[executor_order_timeout_seconds] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE),
    )
    executor_order_timeout_seconds = executor_order_timeout_seconds_sequence[0]
    return {
        "id": account.id,
        "account_id": account.account_id,
        "name": account.name,
        "identifier": account.identifier,
        "market_type": normalize_external_trading_market_type(getattr(account, "market_type", None)),
        "enabled": account.enabled,
        "executor_enabled": True,
        "executor_price_level": executor_price_level,
        "executor_lot_size": executor_lot_size,
        "executor_order_timeout_seconds": executor_order_timeout_seconds,
        "executor_max_replace_count": executor_max_replace_count,
        "executor_max_slippage_pct": executor_max_slippage_pct,
        "executor_clip_sell_to_available": executor_clip_sell_to_available,
        "executor_price_level_sequence": executor_price_level_sequence,
        "executor_order_timeout_seconds_sequence": executor_order_timeout_seconds_sequence,
        "commission_rate_pct": _safe_float(getattr(account, "commission_rate_pct", 0.025), 0.025),
        "min_commission": _safe_float(getattr(account, "min_commission", 5.0), 5.0),
        "stamp_tax_rate_pct": _safe_float(getattr(account, "stamp_tax_rate_pct", 0.05), 0.05),
        "connected": runtime_status.get("connected", False),
        "pending_count": runtime_status.get("pending_count", 0),
        "connected_at": runtime_status.get("connected_at"),
        "runtime_last_seen_at": runtime_status.get("last_seen_at"),
        "last_connected_at": account.last_connected_at,
        "last_disconnected_at": account.last_disconnected_at,
        "last_seen_at": account.last_seen_at,
        "last_disconnect_reason": account.last_disconnect_reason,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _list_serialized_accounts(db: OrmSession, account_id: str) -> List[Dict[str, Any]]:
    accounts = (
        db.query(ExternalTradingAccount)
        .filter(ExternalTradingAccount.account_id == account_id)
        .order_by(ExternalTradingAccount.id.desc())
        .all()
    )
    return [_serialize_account(account) for account in accounts]


def _strategy_binding_name(main_db: OrmSession, sub_account: ExternalTradingSubAccount) -> Optional[str]:
    if not sub_account.strategy_type or not sub_account.strategy_config_id:
        return None
    if sub_account.strategy_type == STRATEGY_W20:
        return "历史 W20 虚拟盘（已下线）"
    if sub_account.strategy_type == STRATEGY_SNOWBALL:
        config = main_db.query(SnowballCopyConfig).filter(
            SnowballCopyConfig.id == sub_account.strategy_config_id,
            SnowballCopyConfig.account_id == sub_account.account_id,
        ).first()
        if config:
            return config.combination_name or config.combination_id or "A股雪球跟单"
        return "A股雪球跟单（配置已删除）"
    if sub_account.strategy_type == STRATEGY_PORTFOLIO_COPY:
        config = main_db.query(PortfolioCopyConfig).filter(
            PortfolioCopyConfig.id == sub_account.strategy_config_id,
            PortfolioCopyConfig.account_id == sub_account.account_id,
        ).first()
        if config:
            return config.portfolio_name or config.portfolio_id or "美股账户跟单"
        return "美股账户跟单（配置已删除）"
    if sub_account.strategy_type == STRATEGY_FACTOR_LIVE:
        config = main_db.query(FactorLiveTradingConfig).filter(
            FactorLiveTradingConfig.id == sub_account.strategy_config_id,
            FactorLiveTradingConfig.account_id == sub_account.account_id,
        ).first()
        return config.name if config else "因子线上交易（配置已删除）"
    return sub_account.strategy_type


def _stored_sub_account_valuation(
    sub_account: ExternalTradingSubAccount,
    positions: List[ExternalTradingLedgerPosition],
) -> Dict[str, Any]:
    position_market_value = 0.0
    position_rows = []
    for position in positions or []:
        quantity = safe_int(position.quantity)
        if quantity <= 0:
            continue
        symbol = normalize_symbol(position.symbol)
        market_value = _safe_float(getattr(position, "market_value", 0))
        market_price = _safe_float(getattr(position, "market_price", 0))
        if market_value <= 0 and market_price > 0:
            market_value = round(quantity * market_price, 2)
        if market_value <= 0:
            avg_cost = _safe_float(getattr(position, "avg_cost", 0))
            market_price = market_price or avg_cost
            market_value = round(quantity * market_price, 2) if market_price > 0 else 0.0
        if market_price <= 0 and market_value > 0:
            market_price = round(market_value / quantity, 6)
        position_market_value += market_value
        position_rows.append({
            "symbol": symbol,
            "quantity": quantity,
            "price": market_price,
            "market_value": round(market_value, 2),
            "price_source": "stored_ledger",
        })
    cash_available = round(_safe_float(sub_account.cash_available), 2)
    position_market_value = round(position_market_value, 2)
    return {
        "sub_account_id": sub_account.id,
        "cash_available": cash_available,
        "position_market_value": position_market_value,
        "net_asset": round(cash_available + position_market_value, 2),
        "positions": position_rows,
        "position_symbols": [row["symbol"] for row in position_rows],
        "price_details": {},
        "valued_at": None,
        "source": "stored_ledger",
    }


async def _serialize_sub_account_with_binding(
    db: OrmSession,
    sub_account: ExternalTradingSubAccount,
    positions: Optional[List[ExternalTradingLedgerPosition]] = None,
    *,
    main_db: OrmSession,
    account: Optional[ExternalTradingAccount] = None,
    update_position_valuation: bool = False,
    prefetched_prices: Optional[Any] = None,
    use_stored_valuation: bool = False,
    fee_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item = serialize_sub_account(sub_account)
    if positions is None:
        positions = (
            db.query(ExternalTradingLedgerPosition)
            .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
            .all()
        )
    today_buy_by_key = get_today_buy_quantities(db, [sub_account.id])
    if use_stored_valuation:
        valuation = _stored_sub_account_valuation(sub_account, positions)
    else:
        try:
            valuation = await calculate_sub_account_net_asset(
                db,
                sub_account,
                positions=positions,
                prefetched_prices=prefetched_prices,
                update_positions=update_position_valuation,
            )
        except ExternalTradingValuationError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    item["position_market_value"] = valuation["position_market_value"]
    item["net_asset"] = valuation["net_asset"]
    item["position_count"] = len(valuation.get("positions") or [])
    item["valuation"] = valuation
    strategy_name = _strategy_binding_name(main_db, sub_account)
    item["strategy_name"] = strategy_name
    item["binding_status"] = "BOUND" if strategy_name else "FREE"
    item["binding_label"] = strategy_name or "空闲"
    item["trade_fee_summary"] = fee_summary or empty_sub_account_fee_summary(sub_account.id)
    item["cumulative_trade_fee_total"] = item["trade_fee_summary"]["effective_fee_total"]
    effective_account = account
    if effective_account is None:
        with db.no_autoflush:
            effective_account = db.query(ExternalTradingAccount).filter(
                ExternalTradingAccount.id == sub_account.external_trading_account_id,
                ExternalTradingAccount.account_id == sub_account.account_id,
            ).first()
    item["effective_executor_policy"] = resolve_execution_policy(effective_account, sub_account) if effective_account else None
    if positions is not None:
        item["positions"] = [
            serialize_ledger_position(
                pos,
                today_buy_quantity=today_buy_by_key.get((sub_account.id, normalize_symbol(pos.symbol)), 0),
            )
            for pos in positions
        ]
    return item


def _get_account_or_404(db: OrmSession, account_id: str, external_account_id: int) -> ExternalTradingAccount:
    account = db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == external_account_id,
        ExternalTradingAccount.account_id == account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="External trading account not found")
    return account


def _ensure_unique(
    db: OrmSession,
    account_id: str,
    name: Optional[str],
    identifier: Optional[str],
    exclude_id: Optional[int] = None,
):
    query = db.query(ExternalTradingAccount).filter(ExternalTradingAccount.account_id == account_id)
    if exclude_id is not None:
        query = query.filter(ExternalTradingAccount.id != exclude_id)

    if name:
        existing_name = query.filter(ExternalTradingAccount.name == name).first()
        if existing_name:
            raise HTTPException(status_code=400, detail=f"账户名 '{name}' 已存在")

    if identifier:
        existing_identifier = query.filter(ExternalTradingAccount.identifier == identifier).first()
        if existing_identifier:
            raise HTTPException(status_code=400, detail=f"唯一标识 '{identifier}' 已存在")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _iso(value: Any) -> Optional[str]:
    if not value:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _serialize_sub_account_net_asset_history(row: ExternalTradingSubAccountNetAssetHistory) -> Dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "external_trading_account_id": row.external_trading_account_id,
        "sub_account_id": row.sub_account_id,
        "strategy_type": row.strategy_type,
        "strategy_config_id": row.strategy_config_id,
        "trading_date": row.trading_date.isoformat() if row.trading_date else None,
        "cash_allocated": row.cash_allocated,
        "cash_available": row.cash_available,
        "position_market_value": row.position_market_value,
        "net_asset": row.net_asset,
        "position_count": row.position_count,
        "positions": row.positions or [],
        "price_details": row.price_details or {},
        "source": row.source,
        "status": row.status,
        "message": row.message,
        "valued_at": _iso(row.valued_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _serialize_broker_position_view(
    snapshot: Optional[ExternalTradingBrokerPositionSnapshot],
    ledger_positions: Dict[str, Dict[str, Any]],
    target_positions: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    diff = build_broker_position_diff(snapshot, ledger_positions, target_positions)
    snapshot_item = serialize_broker_position_snapshot(snapshot) if snapshot else None
    return {
        "snapshot": snapshot_item,
        "positions": diff["rows"],
        "summary": diff["summary"],
    }


def _get_account_target_quantities(db: OrmSession, external_account_id: int, account_id: str) -> Dict[str, int]:
    rows = (
        db.query(ExternalTradingTargetPosition.symbol, ExternalTradingTargetPosition.target_quantity)
        .filter(
            ExternalTradingTargetPosition.account_id == account_id,
            ExternalTradingTargetPosition.external_trading_account_id == external_account_id,
            ExternalTradingTargetPosition.status == "ACTIVE",
        )
        .all()
    )
    targets: Dict[str, int] = {}
    for symbol, quantity in rows:
        normalized = normalize_symbol(symbol)
        if normalized:
            targets[normalized] = targets.get(normalized, 0) + safe_int(quantity)
    return targets


def _collect_symbol_fields(value: Any, symbols: set) -> None:
    if isinstance(value, dict):
        for key in ("symbol", "client_symbol"):
            symbol = normalize_symbol_for_name(value.get(key))
            if symbol:
                symbols.add(symbol)
        for item in value.values():
            if isinstance(item, (dict, list, tuple)):
                _collect_symbol_fields(item, symbols)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_symbol_fields(item, symbols)


def _load_a_stock_name_map(symbols: set) -> Dict[str, str]:
    normalized_symbols = sorted({
        normalized
        for normalized in (normalize_symbol_for_name(symbol) for symbol in symbols)
        if normalized
    })
    return load_symbol_name_map(normalized_symbols)


def _attach_symbol_names(value: Any, stock_name_by_symbol: Dict[str, str]) -> None:
    if isinstance(value, dict):
        symbol = normalize_symbol_for_name(value.get("symbol") or value.get("client_symbol"))
        if symbol:
            symbol_name = stock_name_by_symbol.get(symbol)
            if symbol_name:
                value["symbol_name"] = symbol_name
            elif "symbol_name" not in value:
                value["symbol_name"] = None
        for item in list(value.values()):
            if isinstance(item, (dict, list, tuple)):
                _attach_symbol_names(item, stock_name_by_symbol)
    elif isinstance(value, list):
        for item in value:
            _attach_symbol_names(item, stock_name_by_symbol)
    elif isinstance(value, tuple):
        for item in value:
            _attach_symbol_names(item, stock_name_by_symbol)


def _serialize_target_position_status(
    row: ExternalTradingTargetPosition,
    sub_account: Optional[ExternalTradingSubAccount],
    ledger_position: Optional[ExternalTradingLedgerPosition],
    open_quantities: Optional[Dict[str, int]],
    strategy_name: Optional[str],
    today_buy_quantity: int = 0,
) -> Dict[str, Any]:
    symbol = normalize_symbol(row.symbol)
    current_quantity = safe_int(getattr(ledger_position, "quantity", 0))
    available_quantity = safe_int(getattr(ledger_position, "available_quantity", current_quantity))
    sellability = compute_position_sellability(ledger_position, today_buy_quantity) if ledger_position else {
        "computed_sellable_quantity": 0,
        "sellable_quantity": 0,
        "t1_locked_quantity": 0,
        "today_buy_quantity": 0,
        "sellable_rule": None,
        "sellable_security_type": None,
    }
    pending_buy = safe_int((open_quantities or {}).get("BUY"))
    pending_sell = safe_int((open_quantities or {}).get("SELL"))
    effective_quantity = current_quantity + pending_buy - pending_sell
    target_quantity = safe_int(row.target_quantity)
    delta_quantity = target_quantity - effective_quantity
    side = "BUY" if delta_quantity > 0 else "SELL" if delta_quantity < 0 else None
    demand_quantity = abs(delta_quantity)
    if side == "SELL":
        demand_quantity = min(demand_quantity, max(safe_int(sellability.get("computed_sellable_quantity")) - pending_sell, 0))

    return {
        "id": row.id,
        "sub_account_id": row.sub_account_id,
        "sub_account_name": sub_account.name if sub_account else None,
        "sub_account_enabled": sub_account.enabled if sub_account else None,
        "strategy_type": row.strategy_type,
        "strategy_config_id": row.strategy_config_id,
        "strategy_name": strategy_name,
        "symbol": symbol,
        "target_quantity": target_quantity,
        "current_quantity": current_quantity,
        "raw_available_quantity": available_quantity,
        "available_quantity": safe_int(sellability.get("computed_sellable_quantity")),
        "computed_sellable_quantity": safe_int(sellability.get("computed_sellable_quantity")),
        "sellable_quantity": safe_int(sellability.get("computed_sellable_quantity")),
        "t1_locked_quantity": safe_int(sellability.get("t1_locked_quantity")),
        "today_buy_quantity": safe_int(sellability.get("today_buy_quantity")),
        "sellable_rule": sellability.get("sellable_rule"),
        "sellable_security_type": sellability.get("sellable_security_type"),
        "pending_buy_quantity": pending_buy,
        "pending_sell_quantity": pending_sell,
        "effective_quantity": effective_quantity,
        "delta_quantity": delta_quantity,
        "side": side,
        "demand_quantity": demand_quantity,
        "target_weight_pct": row.target_weight_pct,
        "target_value": row.target_value,
        "reference_price": row.reference_price,
        "reference_price_source": row.reference_price_source,
        "signal_id": row.signal_id,
        "signal_version": row.signal_version,
        "source_execution_id": row.source_execution_id,
        "status": row.status,
        "valid_until": _iso(row.valid_until),
        "updated_at": _iso(row.updated_at),
        "created_at": _iso(row.created_at),
    }


def _serialize_ledger_position_status(
    row: ExternalTradingLedgerPosition,
    sub_account: Optional[ExternalTradingSubAccount],
    strategy_name: Optional[str],
    today_buy_quantity: int = 0,
) -> Dict[str, Any]:
    item = serialize_ledger_position(row, today_buy_quantity=today_buy_quantity)
    item["sub_account_name"] = sub_account.name if sub_account else None
    item["sub_account_enabled"] = sub_account.enabled if sub_account else None
    item["strategy_type"] = sub_account.strategy_type if sub_account else None
    item["strategy_config_id"] = sub_account.strategy_config_id if sub_account else None
    item["strategy_name"] = strategy_name
    return item


def _serialize_order_status(
    row: ExternalTradingOrder,
    sub_account: Optional[ExternalTradingSubAccount],
    strategy_name: Optional[str],
) -> Dict[str, Any]:
    item = serialize_order(row)
    item["sub_account_name"] = sub_account.name if sub_account else None
    item["strategy_name"] = strategy_name
    item["created_at"] = _iso(row.created_at)
    return item


def _load_parent_order_child_repair_summaries(
    db: OrmSession,
    parent_order_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    if not parent_order_ids:
        return {}
    summaries: Dict[int, Dict[str, Any]] = {
        parent_id: {
            "child_count": 0,
            "child_remaining_quantity": 0,
            "child_unfilled_count": 0,
        }
        for parent_id in parent_order_ids
    }
    children = (
        db.query(ExternalTradingOrder)
        .filter(ExternalTradingOrder.parent_order_id.in_(parent_order_ids))
        .all()
    )
    for child in children:
        parent_id = child.parent_order_id
        if parent_id not in summaries:
            continue
        remaining = max(safe_int(child.remaining_quantity, child.quantity - child.filled_quantity), 0)
        summaries[parent_id]["child_count"] += 1
        summaries[parent_id]["child_remaining_quantity"] += remaining
        if remaining > 0:
            summaries[parent_id]["child_unfilled_count"] += 1
    return summaries


def _attach_parent_order_repair_summary(
    item: Dict[str, Any],
    row: ExternalTradingOrder,
    repair_summary: Optional[Dict[str, Any]],
) -> None:
    if (row.allocation_role or "").upper() != "PARENT":
        return
    summary = repair_summary or {
        "child_count": 0,
        "child_remaining_quantity": 0,
        "child_unfilled_count": 0,
    }
    item.update(summary)
    status = (row.status or "").upper()
    ptrade_status = str(row.ptrade_status or "")
    item["needs_fill_repair"] = (
        summary.get("child_count", 0) > 0
        and safe_int(summary.get("child_remaining_quantity")) > 0
        and (status == "FILLED" or ptrade_status == "8" or safe_int(row.filled_quantity) > 0)
    )


def _serialize_fill_status(
    row: ExternalTradingOrderFill,
    sub_account: Optional[ExternalTradingSubAccount],
    strategy_name: Optional[str],
) -> Dict[str, Any]:
    allocation_role = "CHILD" if row.sub_account_id else "PARENT"
    return {
        "id": row.id,
        "allocation_role": allocation_role,
        "allocation_role_label": "子账户分配成交" if allocation_role == "CHILD" else "净额父单成交",
        "sub_account_id": row.sub_account_id,
        "sub_account_name": sub_account.name if sub_account else "净额父单",
        "strategy_name": strategy_name if sub_account else "券商原始成交",
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "broker_order_id": row.broker_order_id,
        "fill_key": row.fill_key,
        "symbol": row.symbol,
        "side": row.side,
        "quantity": row.quantity,
        "price": row.price,
        "amount": row.amount,
        "estimated_commission": row.estimated_commission,
        "estimated_stamp_tax": row.estimated_stamp_tax,
        "estimated_fee_total": row.estimated_fee_total,
        "actual_commission": row.actual_commission,
        "actual_stamp_tax": row.actual_stamp_tax,
        "actual_fee_total": row.actual_fee_total,
        "fee_reconciled_at": _iso(row.fee_reconciled_at),
        "fee_source": row.fee_source,
        "traded_at": _iso(row.traded_at),
        "created_at": _iso(row.created_at),
    }


def _serialize_event_log_status(
    row: ExternalTradingEventLog,
    matched_order: Optional[ExternalTradingOrder],
    sub_account_by_id: Dict[int, ExternalTradingSubAccount],
    strategy_name_by_sub_account_id: Dict[int, Optional[str]],
    children_by_parent_id: Dict[int, List[ExternalTradingOrder]],
) -> Dict[str, Any]:
    matched_role = (matched_order.allocation_role or "DIRECT") if matched_order else None
    matched_sub_account_id = row.matched_sub_account_id or (
        matched_order.sub_account_id if matched_order and matched_order.sub_account_id else None
    )
    sub_account = sub_account_by_id.get(matched_sub_account_id) if matched_sub_account_id else None
    related_sub_accounts = []
    if matched_order and (matched_role or "").upper() == "PARENT":
        seen_sub_account_ids = set()
        for child in children_by_parent_id.get(matched_order.id, []):
            child_sub_account = sub_account_by_id.get(child.sub_account_id)
            if not child_sub_account or child_sub_account.id in seen_sub_account_ids:
                continue
            seen_sub_account_ids.add(child_sub_account.id)
            related_sub_accounts.append({
                "id": child_sub_account.id,
                "name": child_sub_account.name,
                "strategy_name": strategy_name_by_sub_account_id.get(child_sub_account.id),
            })
    return {
        "id": row.id,
        "account_id": row.account_id,
        "account_name": row.account_name,
        "external_trading_account_id": row.external_trading_account_id,
        "event_type": row.event_type,
        "source": row.source,
        "client_order_id": row.client_order_id,
        "broker_order_id": row.broker_order_id,
        "entrust_no": row.entrust_no,
        "symbol": normalize_symbol(row.symbol),
        "side": row.side,
        "ptrade_status": row.ptrade_status,
        "event_time": _iso(row.event_time),
        "matched_order_id": row.matched_order_id,
        "matched_sub_account_id": matched_sub_account_id,
        "matched_order_role": matched_role,
        "matched_order_status": matched_order.status if matched_order else None,
        "sub_account_id": sub_account.id if sub_account else None,
        "sub_account_name": sub_account.name if sub_account else ("净额父单" if matched_order else "未匹配"),
        "strategy_name": strategy_name_by_sub_account_id.get(sub_account.id) if sub_account else None,
        "related_sub_accounts": related_sub_accounts,
        "process_status": row.process_status,
        "process_message": row.process_message,
        "processed_at": _iso(row.processed_at),
        "replay_count": row.replay_count,
        "raw_payload": jsonable_encoder(row.raw_payload or {}),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _dedupe_normalized_symbols(symbols: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for symbol in symbols or []:
        normalized = normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _collect_held_ledger_symbols(ledger_by_sub_account: Dict[int, Any]) -> List[str]:
    symbols: List[str] = []
    for positions in (ledger_by_sub_account or {}).values():
        rows = positions.values() if isinstance(positions, dict) else (positions or [])
        for row in rows:
            if safe_int(getattr(row, "quantity", 0)) <= 0:
                continue
            symbols.append(getattr(row, "symbol", None))
    return _dedupe_normalized_symbols(symbols)


def _collect_payload_symbols(*payloads: Any) -> List[str]:
    symbols = set()
    for payload in payloads:
        _collect_symbol_fields(payload, symbols)
    return _dedupe_normalized_symbols(symbols)


def _query_filter_values(value: Optional[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in str(value or "").split(","):
        text = item.strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _query_filter_symbols(value: Optional[str]) -> List[str]:
    return _dedupe_normalized_symbols(_query_filter_values(value))


def _normalize_page(value: int) -> int:
    return max(safe_int(value, 1), 1)


def _normalize_page_size(value: int) -> int:
    return min(max(safe_int(value, 10), 1), 200)


def _pagination_meta(page: int, page_size: int, total: int) -> Dict[str, int]:
    return {
        "page": page,
        "page_size": page_size,
        "total": safe_int(total),
    }


def _paginate_query(query: Any, *, page: int, page_size: int) -> tuple:
    page = _normalize_page(page)
    page_size = _normalize_page_size(page_size)
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return rows, _pagination_meta(page, page_size, total)


def _filter_options(values: Iterable[Any]) -> List[Dict[str, str]]:
    normalized = sorted({
        str(value).strip()
        for value in values or []
        if value is not None and str(value).strip()
    }, key=lambda value: value.lower())
    return [{"text": value, "value": value} for value in normalized]


def _symbol_filter_options(symbols: Iterable[Any]) -> List[Dict[str, str]]:
    normalized_symbols = _dedupe_normalized_symbols(symbols)
    name_by_symbol = _load_a_stock_name_map(set(normalized_symbols))
    return [
        {
            "text": f"{name_by_symbol.get(symbol)} {symbol}" if name_by_symbol.get(symbol) else symbol,
            "value": symbol,
        }
        for symbol in normalized_symbols
    ]


def _sub_account_ids_by_name(
    sub_accounts: List[ExternalTradingSubAccount],
    values: List[str],
) -> Optional[List[int]]:
    if not values:
        return None
    value_set = set(values)
    return [row.id for row in sub_accounts if row.name in value_set]


def _sub_account_ids_by_strategy_name(
    strategy_name_by_sub_account_id: Dict[int, Optional[str]],
    values: List[str],
) -> Optional[List[int]]:
    if not values:
        return None
    value_set = set(values)
    return [
        sub_account_id
        for sub_account_id, strategy_name in strategy_name_by_sub_account_id.items()
        if strategy_name in value_set
    ]


def _apply_sub_account_filter(query: Any, column: Any, sub_account_ids: Optional[List[int]]) -> Any:
    if sub_account_ids is None:
        return query
    if not sub_account_ids:
        return query.filter(False)
    return query.filter(column.in_(sub_account_ids))


def _apply_symbol_filter(query: Any, column: Any, symbols: List[str]) -> Any:
    if not symbols:
        return query
    return query.filter(column.in_(symbols))


def _distinct_symbols(db: OrmSession, model: Any, account_id: str, external_account_id: int) -> List[str]:
    rows = (
        db.query(model.symbol)
        .filter(
            model.account_id == account_id,
            model.external_trading_account_id == external_account_id,
        )
        .distinct()
        .order_by(model.symbol.asc())
        .all()
    )
    return _dedupe_normalized_symbols(row[0] for row in rows)


def _distinct_event_values(
    db: OrmSession,
    column: Any,
    account_id: str,
    external_account_id: int,
) -> List[str]:
    rows = (
        db.query(column)
        .filter(
            ExternalTradingEventLog.account_id == account_id,
            ExternalTradingEventLog.external_trading_account_id == external_account_id,
        )
        .distinct()
        .order_by(column.asc())
        .all()
    )
    return [str(row[0]) for row in rows if row[0] not in (None, "")]


def _distinct_active_target_symbols(db: OrmSession, account_id: str, external_account_id: int) -> List[str]:
    rows = (
        db.query(ExternalTradingTargetPosition.symbol)
        .filter(
            ExternalTradingTargetPosition.account_id == account_id,
            ExternalTradingTargetPosition.external_trading_account_id == external_account_id,
            ExternalTradingTargetPosition.status == "ACTIVE",
        )
        .distinct()
        .order_by(ExternalTradingTargetPosition.symbol.asc())
        .all()
    )
    return _dedupe_normalized_symbols(row[0] for row in rows)


def _role_filter_options(roles: Iterable[str]) -> List[Dict[str, str]]:
    label_by_role = {
        "PARENT": "父单",
        "CHILD": "子单",
        "BLOCK": "阻断",
    }
    result = []
    for role in roles:
        if not role:
            continue
        result.append({"text": label_by_role.get(role, role), "value": role})
    return result


def _load_executor_sub_account_context(
    db: OrmSession,
    main_db: OrmSession,
    account: ExternalTradingAccount,
    account_id: str,
) -> Dict[str, Any]:
    sub_accounts = (
        db.query(ExternalTradingSubAccount)
        .filter(
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.external_trading_account_id == account.id,
        )
        .order_by(ExternalTradingSubAccount.id.asc())
        .all()
    )
    sub_account_by_id = {row.id: row for row in sub_accounts}
    strategy_name_by_sub_account_id = {
        row.id: _strategy_binding_name(main_db, row)
        for row in sub_accounts
    }
    return {
        "sub_accounts": sub_accounts,
        "sub_account_by_id": sub_account_by_id,
        "strategy_name_by_sub_account_id": strategy_name_by_sub_account_id,
    }


def _attach_symbol_names_to_payloads(*payloads: Any) -> None:
    symbols = set()
    for payload in payloads:
        _collect_symbol_fields(payload, symbols)
    stock_name_by_symbol = _load_a_stock_name_map(symbols)
    for payload in payloads:
        _attach_symbol_names(payload, stock_name_by_symbol)


def _attach_top_level_symbol_names(rows: List[Dict[str, Any]]) -> None:
    symbols = {
        normalize_symbol_for_name(row.get("symbol"))
        for row in rows or []
        if normalize_symbol_for_name(row.get("symbol"))
    }
    stock_name_by_symbol = _load_a_stock_name_map(symbols)
    for row in rows or []:
        symbol = normalize_symbol_for_name(row.get("symbol"))
        if symbol:
            row["symbol_name"] = stock_name_by_symbol.get(symbol)


def _attach_strategy_names(value: Any, strategy_name_by_sub_account_id: Dict[int, Optional[str]]) -> None:
    if isinstance(value, dict):
        sub_account_id = safe_int(value.get("sub_account_id"))
        strategy_name = strategy_name_by_sub_account_id.get(sub_account_id)
        if strategy_name and not value.get("strategy_name"):
            value["strategy_name"] = strategy_name
        for item in value.values():
            if isinstance(item, (dict, list, tuple)):
                _attach_strategy_names(item, strategy_name_by_sub_account_id)
    elif isinstance(value, list):
        for item in value:
            _attach_strategy_names(item, strategy_name_by_sub_account_id)
    elif isinstance(value, tuple):
        for item in value:
            _attach_strategy_names(item, strategy_name_by_sub_account_id)


def _apply_nullable_sub_account_name_filter(
    query: Any,
    column: Any,
    sub_accounts: List[ExternalTradingSubAccount],
    values: List[str],
    *,
    parent_label: str,
) -> Any:
    if not values:
        return query
    include_parent = parent_label in values or "-" in values
    sub_account_ids = _sub_account_ids_by_name(
        sub_accounts,
        [value for value in values if value not in {parent_label, "-"}],
    )
    conditions = []
    if sub_account_ids:
        conditions.append(column.in_(sub_account_ids))
    if include_parent:
        conditions.append(column.is_(None))
    if not conditions:
        return query.filter(False)
    return query.filter(or_(*conditions))


def _apply_nullable_strategy_filter(
    query: Any,
    column: Any,
    strategy_name_by_sub_account_id: Dict[int, Optional[str]],
    values: List[str],
    *,
    parent_label: str,
) -> Any:
    if not values:
        return query
    include_parent = parent_label in values or "-" in values
    sub_account_ids = _sub_account_ids_by_strategy_name(
        strategy_name_by_sub_account_id,
        [value for value in values if value not in {parent_label, "-"}],
    )
    conditions = []
    if sub_account_ids:
        conditions.append(column.in_(sub_account_ids))
    if include_parent:
        conditions.append(column.is_(None))
    if not conditions:
        return query.filter(False)
    return query.filter(or_(*conditions))


def _apply_fill_role_filter(query: Any, roles: List[str]) -> Any:
    if not roles:
        return query
    role_set = set(roles)
    conditions = []
    if "PARENT" in role_set:
        conditions.append(ExternalTradingOrderFill.sub_account_id.is_(None))
    if "CHILD" in role_set:
        conditions.append(ExternalTradingOrderFill.sub_account_id.isnot(None))
    if not conditions:
        return query.filter(False)
    return query.filter(or_(*conditions))


async def _prefetch_table_price_details(
    account: ExternalTradingAccount,
    rows: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    symbols = _collect_payload_symbols(rows)
    price_details = await _prefetch_realtime_price_details(
        account,
        [],
        symbols,
        timeout=min(normalize_timeout_seconds(account.executor_order_timeout_seconds), 15.0),
    )
    return _serialize_price_details(price_details, symbols)


def _resolve_netted_executor_options(
    account: ExternalTradingAccount,
    payload: NettedExecutorRequest,
) -> Dict[str, Any]:
    price_level = normalize_price_level(payload.price_level, normalize_price_level(account.executor_price_level))
    lot_size = normalize_lot_size(payload.lot_size, normalize_lot_size(account.executor_lot_size))
    timeout_seconds = normalize_timeout_seconds(
        payload.timeout_seconds,
        normalize_timeout_seconds(account.executor_order_timeout_seconds),
    )
    timeout_sequence = (
        [timeout_seconds] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE)
        if payload.timeout_seconds is not None
        else normalize_timeout_seconds_sequence(
            getattr(account, "executor_order_timeout_seconds_sequence", None),
            default=[timeout_seconds] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE),
        )
    )
    timeout_seconds = timeout_sequence[0]
    return {
        "price_level": price_level,
        "lot_size": lot_size,
        "order_timeout_seconds": timeout_seconds,
        "order_timeout_seconds_sequence": timeout_sequence,
        "max_replace_count": normalize_max_replace_count(account.executor_max_replace_count),
        "clip_sell_to_available": True,
        "price_level_sequence": normalize_price_level_sequence(account.executor_price_level_sequence),
    }


def _build_netted_executor_base_plan(
    db: OrmSession,
    account: ExternalTradingAccount,
    owner_account_id: str,
    payload: NettedExecutorRequest,
    *,
    reference_prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    options = _resolve_netted_executor_options(account, payload)
    return build_netted_target_execution_plan(
        db,
        account_id=owner_account_id,
        external_trading_account_id=account.id,
        sub_account_ids=payload.sub_account_ids,
        reference_prices=reference_prices,
        **options,
    )


def _reference_prices_from_prefetched(
    symbols: List[str],
    prefetched_prices: Optional[Any],
) -> Dict[str, float]:
    if not prefetched_prices:
        return {}
    result: Dict[str, float] = {}
    for symbol in _dedupe_normalized_symbols(symbols):
        detail = prefetched_prices.get(symbol) if isinstance(prefetched_prices, dict) else None
        price = _safe_float(detail.get("price") if isinstance(detail, dict) else detail)
        if price > 0:
            result[symbol] = price
    return result


def _serialize_price_details(
    price_details: Optional[Dict[str, Dict[str, Any]]],
    symbols: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    allowed_symbols = set(_dedupe_normalized_symbols(symbols)) if symbols is not None else None
    result: Dict[str, Dict[str, Any]] = {}
    for key, detail in (price_details or {}).items():
        if not isinstance(detail, dict):
            continue
        symbol = normalize_symbol(detail.get("symbol") or key)
        price = _safe_float(detail.get("price"))
        if not symbol or price <= 0:
            continue
        if allowed_symbols is not None and symbol not in allowed_symbols:
            continue
        result[symbol] = {
            "symbol": symbol,
            "price": price,
            "source": detail.get("source") or None,
        }
    return result


async def _prefetch_realtime_price_details(
    account: ExternalTradingAccount,
    required_symbols: List[str],
    optional_symbols: Optional[List[str]] = None,
    *,
    timeout: float,
) -> Dict[str, Dict[str, Any]]:
    required_symbols = _dedupe_normalized_symbols(required_symbols)
    optional_symbols = _dedupe_normalized_symbols(optional_symbols or [])
    all_symbols = _dedupe_normalized_symbols([*required_symbols, *optional_symbols])
    price_details: Dict[str, Dict[str, Any]] = {}
    if all_symbols:
        try:
            price_details = await get_realtime_price_details(
                account.id,
                all_symbols,
                timeout=timeout,
                raise_on_missing=False,
            )
        except Exception as exc:
            logger.warning(
                "External trading price prefetch failed for account %s: %s",
                account.id,
                exc,
            )

    missing_required_symbols = [symbol for symbol in required_symbols if symbol not in price_details]
    if missing_required_symbols:
        raise HTTPException(status_code=409, detail=f"无法获取以下标的最新价: {', '.join(missing_required_symbols)}")
    return price_details


async def _build_netted_executor_plan(
    db: OrmSession,
    account: ExternalTradingAccount,
    owner_account_id: str,
    payload: NettedExecutorRequest,
    *,
    require_connection: bool,
    base_plan: Optional[Dict[str, Any]] = None,
    prefetched_prices: Optional[Any] = None,
) -> Dict[str, Any]:
    options = _resolve_netted_executor_options(account, payload)
    timeout_seconds = options["order_timeout_seconds"]
    plan = base_plan or _build_netted_executor_base_plan(db, account, owner_account_id, payload)
    symbols = collect_internal_cross_reference_symbols(plan)
    connected = external_trading_hub.get_status(account.id).get("connected")
    if require_connection and not connected:
        raise ExternalTradingConnectionError("外部交易账号未连接")
    reference_prices: Dict[str, float] = {}
    reference_price_error = None
    if symbols:
        try:
            if prefetched_prices is not None:
                reference_prices = _reference_prices_from_prefetched(symbols, prefetched_prices)
                missing_symbols = [
                    symbol
                    for symbol in _dedupe_normalized_symbols(symbols)
                    if symbol not in reference_prices
                ]
                if missing_symbols:
                    raise ExternalTradingValuationError(
                        f"无法获取以下标的最新价: {', '.join(missing_symbols)}"
                    )
            else:
                reference_prices = await get_realtime_reference_prices(
                    account.id,
                    symbols,
                    timeout=min(timeout_seconds, 15.0),
                )
        except ExternalTradingValuationError as exc:
            reference_price_error = str(exc)
            logger.warning(
                "External trading reference price lookup failed for account %s: %s",
                account.id,
                exc,
            )
        except Exception as exc:
            reference_price_error = str(exc)
            logger.warning(
                "External trading reference price lookup failed for account %s: %s",
                account.id,
                exc,
            )
    if reference_prices:
        plan = _build_netted_executor_base_plan(
            db,
            account,
            owner_account_id,
            payload,
            reference_prices=reference_prices,
        )
    plan["reference_prices"] = reference_prices
    if reference_price_error:
        plan["reference_price_error"] = reference_price_error
    plan["connected"] = bool(connected)
    plan["account_executor_policy"] = resolve_execution_policy(account)
    return plan


@router.get("", response_model=List[ExternalTradingAccountResponse])
async def list_external_trading_accounts(
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    return _list_serialized_accounts(db, account_id)


@router.websocket("/status/ws")
async def external_trading_account_status_websocket(websocket: WebSocket):
    account_id = websocket.query_params.get("account_id")
    if not account_id or not is_valid_account(account_id):
        await websocket.accept()
        await websocket.close(code=1008, reason="invalid account_id")
        return

    await websocket.accept()
    try:
        while True:
            db = ExternalTradingDBSession()
            try:
                payload = {
                    "type": "external_trading_accounts",
                    "accounts": _list_serialized_accounts(db, account_id),
                    "pushed_at": datetime.now(),
                }
            finally:
                db.close()
            await websocket.send_json(jsonable_encoder(payload))
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.exception("External trading account status WebSocket failed: %s", exc)
        try:
            await websocket.close(code=1011, reason="status websocket error")
        except Exception:
            pass


@router.post("", response_model=ExternalTradingAccountResponse)
async def create_external_trading_account(
    payload: ExternalTradingAccountCreate,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    _ensure_unique(db, account_id, payload.name, payload.identifier)
    try:
        _ensure_execution_sequence_lengths(
            price_level_sequence=payload.executor_price_level_sequence,
            order_timeout_seconds_sequence=payload.executor_order_timeout_seconds_sequence,
            max_replace_count=payload.executor_max_replace_count,
        )
    except ValueError as exc:
        raise _validation_http_error(exc)
    account = ExternalTradingAccount(
        account_id=account_id,
        name=payload.name,
        identifier=payload.identifier,
        market_type=payload.market_type,
        enabled=payload.enabled,
        executor_enabled=True,
        executor_price_level=payload.executor_price_level,
        executor_lot_size=payload.executor_lot_size,
        executor_order_timeout_seconds=payload.executor_order_timeout_seconds_sequence[0],
        executor_max_replace_count=payload.executor_max_replace_count,
        executor_max_slippage_pct=payload.executor_max_slippage_pct,
        executor_clip_sell_to_available=True,
        executor_price_level_sequence=payload.executor_price_level_sequence,
        executor_order_timeout_seconds_sequence=payload.executor_order_timeout_seconds_sequence,
        commission_rate_pct=payload.commission_rate_pct,
        min_commission=payload.min_commission,
        stamp_tax_rate_pct=payload.stamp_tax_rate_pct,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return _serialize_account(account)


@router.put("/{external_account_id}", response_model=ExternalTradingAccountResponse)
async def update_external_trading_account(
    external_account_id: int,
    payload: ExternalTradingAccountUpdate,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    update_data = payload.dict(exclude_unset=True)
    update_data.pop("executor_enabled", None)
    update_data.pop("executor_clip_sell_to_available", None)
    if update_data.get("market_type") is None:
        update_data.pop("market_type", None)
    market_type_updated = "market_type" in update_data and update_data["market_type"] != normalize_external_trading_market_type(
        getattr(account, "market_type", None)
    )

    current_timeout_seconds = normalize_timeout_seconds(getattr(account, "executor_order_timeout_seconds", None))
    if "executor_order_timeout_seconds_sequence" in update_data:
        update_data["executor_order_timeout_seconds"] = update_data["executor_order_timeout_seconds_sequence"][0]
    elif "executor_order_timeout_seconds" in update_data:
        current_timeout_seconds = normalize_timeout_seconds(update_data["executor_order_timeout_seconds"])
        update_data["executor_order_timeout_seconds_sequence"] = [
            current_timeout_seconds
        ] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE)

    effective_price_sequence = update_data.get(
        "executor_price_level_sequence",
        normalize_price_level_sequence(getattr(account, "executor_price_level_sequence", None)),
    )
    effective_timeout_sequence = update_data.get(
        "executor_order_timeout_seconds_sequence",
        normalize_timeout_seconds_sequence(
            getattr(account, "executor_order_timeout_seconds_sequence", None),
            default=[current_timeout_seconds] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE),
        ),
    )
    effective_max_replace_count = update_data.get(
        "executor_max_replace_count",
        normalize_max_replace_count(getattr(account, "executor_max_replace_count", None)),
    )
    try:
        _ensure_execution_sequence_lengths(
            price_level_sequence=effective_price_sequence,
            order_timeout_seconds_sequence=effective_timeout_sequence,
            max_replace_count=effective_max_replace_count,
        )
    except ValueError as exc:
        raise _validation_http_error(exc)

    _ensure_unique(
        db,
        account_id,
        update_data.get("name"),
        update_data.get("identifier"),
        exclude_id=account.id,
    )

    for key, value in update_data.items():
        setattr(account, key, value)
    account.executor_enabled = True
    account.executor_clip_sell_to_available = True
    account.updated_at = datetime.now()
    db.commit()
    db.refresh(account)

    if account.enabled is False:
        await external_trading_hub.disconnect_account(account.id, reason="account disabled")
    elif market_type_updated:
        await external_trading_hub.disconnect_account(account.id, reason="account market type updated")
    elif "name" in update_data or "identifier" in update_data:
        await external_trading_hub.disconnect_account(account.id, reason="account credentials updated")

    return _serialize_account(account)


@router.delete("/{external_account_id}")
async def delete_external_trading_account(
    external_account_id: int,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    account_pk = account.id
    now = datetime.now()
    for config in main_db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.account_id == account_id,
        SnowballCopyConfig.external_trading_account_id == account_pk,
    ).all():
        config.external_trading_account_id = None
        config.live_sub_account_id = None
        config.live_trade_enabled = False
        config.updated_at = now
    for config in main_db.query(PortfolioCopyConfig).filter(
        PortfolioCopyConfig.account_id == account_id,
        PortfolioCopyConfig.external_trading_account_id == account_pk,
    ).all():
        config.external_trading_account_id = None
        config.live_sub_account_id = None
        config.account_type = "ib"
        config.updated_at = now
    for config in main_db.query(FactorLiveTradingConfig).filter(
        FactorLiveTradingConfig.account_id == account_id,
        FactorLiveTradingConfig.external_trading_account_id == account_pk,
    ).all():
        config.external_trading_account_id = None
        config.live_sub_account_id = None
        config.enabled = False
        config.updated_at = now
    db.query(ExternalTradingOrderFill).filter(
        ExternalTradingOrderFill.external_trading_account_id == account_pk
    ).delete(synchronize_session=False)
    db.query(ExternalTradingDeliverRecord).filter(
        ExternalTradingDeliverRecord.external_trading_account_id == account_pk
    ).delete(synchronize_session=False)
    db.query(ExternalTradingOrder).filter(
        ExternalTradingOrder.external_trading_account_id == account_pk
    ).delete(synchronize_session=False)
    db.query(ExternalTradingTargetPosition).filter(
        ExternalTradingTargetPosition.external_trading_account_id == account_pk
    ).delete(synchronize_session=False)
    db.query(ExternalTradingLedgerPosition).filter(
        ExternalTradingLedgerPosition.external_trading_account_id == account_pk
    ).delete(synchronize_session=False)
    db.query(ExternalTradingSubAccountNetAssetHistory).filter(
        ExternalTradingSubAccountNetAssetHistory.external_trading_account_id == account_pk
    ).delete(synchronize_session=False)
    db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.external_trading_account_id == account_pk
    ).delete(synchronize_session=False)
    db.delete(account)
    main_db.commit()
    db.commit()
    await external_trading_hub.disconnect_account(account_pk, reason="account deleted")
    return {"message": "Deleted successfully"}


@router.get("/{external_account_id}/sub-accounts")
async def list_external_trading_sub_accounts(
    external_account_id: int,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    sub_accounts = (
        db.query(ExternalTradingSubAccount)
        .filter(
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.external_trading_account_id == external_account_id,
        )
        .order_by(ExternalTradingSubAccount.id.desc())
        .all()
    )
    result = []
    fee_summaries = get_sub_account_fee_summaries(
        db,
        account_id=account_id,
        external_trading_account_id=external_account_id,
    )
    position_rows = (
        db.query(ExternalTradingLedgerPosition)
        .filter(
            ExternalTradingLedgerPosition.account_id == account_id,
            ExternalTradingLedgerPosition.external_trading_account_id == external_account_id,
        )
        .order_by(
            ExternalTradingLedgerPosition.sub_account_id.asc(),
            ExternalTradingLedgerPosition.symbol.asc(),
        )
        .all()
    )
    positions_by_sub_account: Dict[int, List[ExternalTradingLedgerPosition]] = {}
    for row in position_rows:
        positions_by_sub_account.setdefault(row.sub_account_id, []).append(row)
    price_details = await _prefetch_realtime_price_details(
        account,
        _collect_held_ledger_symbols(positions_by_sub_account),
        timeout=10.0,
    )
    for sub_account in sub_accounts:
        result.append(await _serialize_sub_account_with_binding(
            db,
            sub_account,
            positions_by_sub_account.get(sub_account.id, []),
            main_db=main_db,
            account=account,
            prefetched_prices=price_details,
            fee_summary=fee_summaries.get(sub_account.id),
        ))
    return result


@router.get("/{external_account_id}/sub-accounts/{sub_account_id}/net-asset-history")
async def get_external_trading_sub_account_net_asset_history(
    external_account_id: int,
    sub_account_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=404, detail="External trading sub account not found")

    if end_date is None:
        end_date = datetime.now().date()
    if start_date is None:
        start_date = end_date - timedelta(days=180)
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date cannot be later than end_date")

    rows = (
        db.query(ExternalTradingSubAccountNetAssetHistory)
        .filter(
            ExternalTradingSubAccountNetAssetHistory.account_id == account_id,
            ExternalTradingSubAccountNetAssetHistory.external_trading_account_id == external_account_id,
            ExternalTradingSubAccountNetAssetHistory.sub_account_id == sub_account_id,
            ExternalTradingSubAccountNetAssetHistory.trading_date >= start_date,
            ExternalTradingSubAccountNetAssetHistory.trading_date <= end_date,
        )
        .order_by(ExternalTradingSubAccountNetAssetHistory.trading_date.asc())
        .all()
    )
    history = [_serialize_sub_account_net_asset_history(row) for row in rows]
    return {
        "account": _serialize_account(account),
        "sub_account": {
            **serialize_sub_account(sub_account),
            "strategy_name": _strategy_binding_name(main_db, sub_account),
        },
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "history": history,
        "summary": {
            "count": len(history),
            "success_count": len([row for row in history if row.get("status") == "SUCCESS"]),
            "failed_count": len([row for row in history if row.get("status") != "SUCCESS"]),
        },
    }


@router.post("/{external_account_id}/sub-accounts")
async def create_external_trading_sub_account(
    external_account_id: int,
    payload: ExternalTradingSubAccountPayload,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    account_policy = resolve_execution_policy(account)
    stored_timeout_sequence = payload.executor_order_timeout_seconds_sequence
    if stored_timeout_sequence is None and payload.executor_order_timeout_seconds is not None:
        timeout_seconds = normalize_timeout_seconds(payload.executor_order_timeout_seconds)
        stored_timeout_sequence = [timeout_seconds] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE)
    effective_price_sequence = payload.executor_price_level_sequence or account_policy.get("price_level_sequence")
    effective_timeout_sequence = stored_timeout_sequence or account_policy.get("order_timeout_seconds_sequence")
    effective_max_replace_count = (
        payload.executor_max_replace_count
        if payload.executor_max_replace_count is not None
        else account_policy.get("max_replace_count")
    )
    try:
        _ensure_execution_sequence_lengths(
            price_level_sequence=effective_price_sequence,
            order_timeout_seconds_sequence=effective_timeout_sequence,
            max_replace_count=effective_max_replace_count,
        )
    except ValueError as exc:
        raise _validation_http_error(exc)
    now = datetime.now()
    sub_account = ExternalTradingSubAccount(
        account_id=account_id,
        external_trading_account_id=external_account_id,
        name=payload.name,
        cash_allocated=payload.cash_allocated,
        cash_available=payload.cash_allocated,
        remark=payload.remark,
        enabled=payload.enabled,
        executor_price_level=payload.executor_price_level,
        executor_lot_size=payload.executor_lot_size,
        executor_order_timeout_seconds=stored_timeout_sequence[0] if stored_timeout_sequence else payload.executor_order_timeout_seconds,
        executor_max_replace_count=payload.executor_max_replace_count,
        executor_max_slippage_pct=payload.executor_max_slippage_pct,
        executor_clip_sell_to_available=None,
        executor_price_level_sequence=payload.executor_price_level_sequence,
        executor_order_timeout_seconds_sequence=stored_timeout_sequence,
        created_at=now,
        updated_at=now,
    )
    db.add(sub_account)
    db.commit()
    db.refresh(sub_account)
    return await _serialize_sub_account_with_binding(db, sub_account, main_db=main_db, account=account)


@router.put("/{external_account_id}/sub-accounts/{sub_account_id}")
async def update_external_trading_sub_account(
    external_account_id: int,
    sub_account_id: int,
    payload: ExternalTradingSubAccountPayload,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=404, detail="External trading sub account not found")
    account_policy = resolve_execution_policy(account)
    stored_timeout_sequence = payload.executor_order_timeout_seconds_sequence
    if stored_timeout_sequence is None and payload.executor_order_timeout_seconds is not None:
        timeout_seconds = normalize_timeout_seconds(payload.executor_order_timeout_seconds)
        stored_timeout_sequence = [timeout_seconds] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE)
    effective_price_sequence = payload.executor_price_level_sequence or account_policy.get("price_level_sequence")
    effective_timeout_sequence = stored_timeout_sequence or account_policy.get("order_timeout_seconds_sequence")
    effective_max_replace_count = (
        payload.executor_max_replace_count
        if payload.executor_max_replace_count is not None
        else account_policy.get("max_replace_count")
    )
    try:
        _ensure_execution_sequence_lengths(
            price_level_sequence=effective_price_sequence,
            order_timeout_seconds_sequence=effective_timeout_sequence,
            max_replace_count=effective_max_replace_count,
        )
    except ValueError as exc:
        raise _validation_http_error(exc)
    previous_cash_allocated = _safe_float(sub_account.cash_allocated)
    sub_account.name = payload.name
    sub_account.cash_allocated = payload.cash_allocated
    if abs(_safe_float(payload.cash_allocated) - previous_cash_allocated) >= 0.005:
        try:
            valuation = await calculate_sub_account_net_asset(db, sub_account)
        except ExternalTradingValuationError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        position_market_value = valuation["position_market_value"]
        sub_account.cash_available = round(_safe_float(payload.cash_allocated) - position_market_value, 2)
    sub_account.remark = payload.remark
    sub_account.enabled = payload.enabled
    sub_account.executor_price_level = payload.executor_price_level
    sub_account.executor_lot_size = payload.executor_lot_size
    sub_account.executor_order_timeout_seconds = (
        stored_timeout_sequence[0] if stored_timeout_sequence else payload.executor_order_timeout_seconds
    )
    sub_account.executor_max_replace_count = payload.executor_max_replace_count
    sub_account.executor_max_slippage_pct = payload.executor_max_slippage_pct
    sub_account.executor_clip_sell_to_available = None
    sub_account.executor_price_level_sequence = payload.executor_price_level_sequence
    sub_account.executor_order_timeout_seconds_sequence = stored_timeout_sequence
    sub_account.updated_at = datetime.now()
    db.commit()
    db.refresh(sub_account)
    return await _serialize_sub_account_with_binding(db, sub_account, main_db=main_db, account=account)


@router.delete("/{external_account_id}/sub-accounts/{sub_account_id}")
async def delete_external_trading_sub_account(
    external_account_id: int,
    sub_account_id: int,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    _get_account_or_404(db, account_id, external_account_id)
    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=404, detail="External trading sub account not found")

    active_order = db.query(ExternalTradingOrder).filter(
        ExternalTradingOrder.sub_account_id == sub_account.id,
        ExternalTradingOrder.status.in_(list(ACTIVE_ORDER_STATUSES)),
    ).first()
    if active_order:
        raise HTTPException(status_code=400, detail="该虚拟子账户有未完成订单，不能删除")

    now = datetime.now()
    bound_snowball_configs = main_db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.account_id == account_id,
        SnowballCopyConfig.live_sub_account_id == sub_account.id,
    ).all()
    for config in bound_snowball_configs:
        config.live_sub_account_id = None
        config.updated_at = now

    bound_portfolio_copy_configs = main_db.query(PortfolioCopyConfig).filter(
        PortfolioCopyConfig.account_id == account_id,
        PortfolioCopyConfig.live_sub_account_id == sub_account.id,
    ).all()
    for config in bound_portfolio_copy_configs:
        config.external_trading_account_id = None
        config.live_sub_account_id = None
        if config.account_type == "external":
            config.account_type = "ib"
        config.updated_at = now

    bound_factor_live_configs = main_db.query(FactorLiveTradingConfig).filter(
        FactorLiveTradingConfig.account_id == account_id,
        FactorLiveTradingConfig.live_sub_account_id == sub_account.id,
    ).all()
    for config in bound_factor_live_configs:
        config.live_sub_account_id = None
        config.updated_at = now

    db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingOrder).filter(ExternalTradingOrder.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingTargetPosition).filter(ExternalTradingTargetPosition.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingLedgerPosition).filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingSubAccountNetAssetHistory).filter(
        ExternalTradingSubAccountNetAssetHistory.sub_account_id == sub_account.id
    ).delete(synchronize_session=False)
    db.delete(sub_account)
    main_db.commit()
    db.commit()
    return {"message": "Deleted successfully"}


@router.post("/{external_account_id}/executor/preview")
async def preview_external_trading_netted_executor(
    external_account_id: int,
    payload: NettedExecutorRequest,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        plan = await _build_netted_executor_plan(
            db,
            account,
            account_id,
            payload,
            require_connection=False,
        )
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"plan": plan}


@router.post("/{external_account_id}/executor/execute")
async def execute_external_trading_netted_executor(
    external_account_id: int,
    payload: NettedExecutorRequest,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await trigger_external_trading_executor(
            account_id=account_id,
            external_account_id=account.id,
            trigger_source="manual_api",
            force=payload.force,
            price_level=normalize_price_level(payload.price_level, normalize_price_level(account.executor_price_level)),
            lot_size=normalize_lot_size(payload.lot_size, normalize_lot_size(account.executor_lot_size)),
            order_timeout_seconds=normalize_timeout_seconds(
                payload.timeout_seconds,
                normalize_timeout_seconds(account.executor_order_timeout_seconds),
            ),
        )
    except ExternalTradingConnectionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("External trading netted executor failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{external_account_id}/executor/status")
async def get_external_trading_executor_status(
    external_account_id: int,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)

    sub_account_fee_summaries = get_sub_account_fee_summaries(
        db,
        account_id=account_id,
        external_trading_account_id=account.id,
    )
    account_fee_summary = get_external_account_fee_summary(
        db,
        account_id=account_id,
        external_trading_account_id=account.id,
    )

    executor_status_payload = NettedExecutorRequest()
    plan_error = None
    base_plan = None
    try:
        base_plan = _build_netted_executor_base_plan(db, account, account_id, executor_status_payload)
    except Exception as exc:
        plan_error = str(exc)

    plan_for_summary = base_plan or {}

    attributed_trade_fee_total = round(
        sum(
            _safe_float((summary or {}).get("effective_fee_total"))
            for summary in sub_account_fee_summaries.values()
        ),
        2,
    )

    order_status_counts = {
        (status or "UNKNOWN"): safe_int(count)
        for status, count in (
            db.query(ExternalTradingOrder.status, func.count(ExternalTradingOrder.id))
            .filter(
                ExternalTradingOrder.account_id == account_id,
                ExternalTradingOrder.external_trading_account_id == account.id,
            )
            .group_by(ExternalTradingOrder.status)
            .all()
        )
    }
    sub_account_count = (
        db.query(ExternalTradingSubAccount)
        .filter(
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.external_trading_account_id == account.id,
        )
        .count()
    )
    active_sub_account_count = (
        db.query(ExternalTradingSubAccount)
        .filter(
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.external_trading_account_id == account.id,
            ExternalTradingSubAccount.enabled.is_(True),
        )
        .count()
    )
    target_position_count = (
        db.query(ExternalTradingTargetPosition)
        .filter(
            ExternalTradingTargetPosition.account_id == account_id,
            ExternalTradingTargetPosition.external_trading_account_id == account.id,
            ExternalTradingTargetPosition.status == "ACTIVE",
        )
        .count()
    )
    nonzero_target_count = (
        db.query(ExternalTradingTargetPosition)
        .filter(
            ExternalTradingTargetPosition.account_id == account_id,
            ExternalTradingTargetPosition.external_trading_account_id == account.id,
            ExternalTradingTargetPosition.status == "ACTIVE",
            ExternalTradingTargetPosition.target_quantity != 0,
        )
        .count()
    )
    ledger_position_count = (
        db.query(ExternalTradingLedgerPosition)
        .filter(
            ExternalTradingLedgerPosition.account_id == account_id,
            ExternalTradingLedgerPosition.external_trading_account_id == account.id,
        )
        .count()
    )
    order_count = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.account_id == account_id,
            ExternalTradingOrder.external_trading_account_id == account.id,
        )
        .count()
    )
    active_order_count = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.account_id == account_id,
            ExternalTradingOrder.external_trading_account_id == account.id,
            ExternalTradingOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .count()
    )
    fill_count = (
        db.query(ExternalTradingOrderFill)
        .filter(
            ExternalTradingOrderFill.account_id == account_id,
            ExternalTradingOrderFill.external_trading_account_id == account.id,
        )
        .count()
    )
    event_log_count = (
        db.query(ExternalTradingEventLog)
        .filter(
            ExternalTradingEventLog.account_id == account_id,
            ExternalTradingEventLog.external_trading_account_id == account.id,
        )
        .count()
    )

    return {
        "account": _serialize_account(account),
        "sub_accounts": [],
        "target_positions": [],
        "ledger_positions": [],
        "orders": [],
        "fills": [],
        "events": [],
        "plan": {},
        "plan_error": plan_error,
        "price_details": {},
        "fee_summary": account_fee_summary,
        "summary": {
            "sub_account_count": sub_account_count,
            "active_sub_account_count": active_sub_account_count,
            "target_position_count": target_position_count,
            "nonzero_target_count": nonzero_target_count,
            "pending_delta_count": len(plan_for_summary.get("demands") or []),
            "ledger_position_count": ledger_position_count,
            "active_order_count": active_order_count,
            "order_count": order_count,
            "fill_count": fill_count,
            "event_log_count": event_log_count,
            "order_status_counts": order_status_counts,
            "external_order_count": len(plan_for_summary.get("external_orders") or []),
            "internal_cross_count": len(plan_for_summary.get("internal_crosses") or []),
            "demand_count": len(plan_for_summary.get("demands") or []),
            "trade_fee_total": account_fee_summary["trade_fee_total"],
            "attributed_trade_fee_total": attributed_trade_fee_total,
            "non_trade_fee_total": account_fee_summary["non_trade_fee_total"],
            "non_trade_income_total": account_fee_summary["non_trade_income_total"],
            "non_trade_net_total": account_fee_summary["non_trade_net_total"],
            "total_fee": account_fee_summary["total_fee"],
        },
    }


@router.get("/{external_account_id}/executor/status/sub-accounts")
async def get_external_trading_executor_status_sub_accounts(
    external_account_id: int,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    context = _load_executor_sub_account_context(db, main_db, account, account_id)
    sub_accounts = context["sub_accounts"]
    sub_account_by_id = context["sub_account_by_id"]
    sub_account_fee_summaries = get_sub_account_fee_summaries(
        db,
        account_id=account_id,
        external_trading_account_id=account.id,
    )
    ledger_by_sub_account = {
        sub_account_id: get_ledger_positions(db, sub_account_id)
        for sub_account_id in sub_account_by_id.keys()
    }
    rows = [
        await _serialize_sub_account_with_binding(
            db,
            row,
            list(ledger_by_sub_account.get(row.id, {}).values()),
            main_db=main_db,
            account=account,
            use_stored_valuation=True,
            fee_summary=sub_account_fee_summaries.get(row.id),
        )
        for row in sub_accounts
    ]
    _attach_symbol_names_to_payloads(rows)
    return {
        "rows": rows,
        "price_details": {},
    }


@router.get("/{external_account_id}/executor/status/plan")
async def get_external_trading_executor_status_plan(
    external_account_id: int,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    context = _load_executor_sub_account_context(db, main_db, account, account_id)
    strategy_name_by_sub_account_id = context["strategy_name_by_sub_account_id"]
    payload = NettedExecutorRequest()
    base_plan = None
    plan_error = None
    price_details: Dict[str, Dict[str, Any]] = {}
    display_symbols: List[str] = []

    try:
        base_plan = _build_netted_executor_base_plan(db, account, account_id, payload)
        reference_symbols = collect_internal_cross_reference_symbols(base_plan)
        display_symbols = _collect_payload_symbols(base_plan)
        price_details = await _prefetch_realtime_price_details(
            account,
            [],
            [*reference_symbols, *display_symbols],
            timeout=min(normalize_timeout_seconds(account.executor_order_timeout_seconds), 15.0),
        )
        plan = await _build_netted_executor_plan(
            db,
            account,
            account_id,
            payload,
            require_connection=False,
            base_plan=base_plan,
            prefetched_prices=price_details,
        )
    except Exception as exc:
        plan_error = str(exc)
        if base_plan is None:
            try:
                base_plan = _build_netted_executor_base_plan(db, account, account_id, payload)
                display_symbols = _collect_payload_symbols(base_plan)
            except Exception:
                base_plan = {}
        plan = base_plan or {}
        plan["connected"] = external_trading_hub.get_status(account.id).get("connected")
        plan["reference_prices"] = {}
        plan["account_executor_policy"] = resolve_execution_policy(account)

    _attach_strategy_names(plan, strategy_name_by_sub_account_id)
    _attach_symbol_names_to_payloads(plan)
    return {
        "plan": plan,
        "plan_error": plan_error,
        "price_details": _serialize_price_details(price_details, display_symbols),
    }


@router.get("/{external_account_id}/executor/status/target-positions")
async def get_external_trading_executor_status_target_positions(
    external_account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    symbol: Optional[str] = Query(None),
    sub_account: Optional[str] = Query(None),
    strategy: Optional[str] = Query(None),
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    context = _load_executor_sub_account_context(db, main_db, account, account_id)
    sub_accounts = context["sub_accounts"]
    sub_account_by_id = context["sub_account_by_id"]
    strategy_name_by_sub_account_id = context["strategy_name_by_sub_account_id"]

    query = (
        db.query(ExternalTradingTargetPosition)
        .filter(
            ExternalTradingTargetPosition.account_id == account_id,
            ExternalTradingTargetPosition.external_trading_account_id == account.id,
            ExternalTradingTargetPosition.status == "ACTIVE",
        )
    )
    query = _apply_symbol_filter(query, ExternalTradingTargetPosition.symbol, _query_filter_symbols(symbol))
    query = _apply_sub_account_filter(
        query,
        ExternalTradingTargetPosition.sub_account_id,
        _sub_account_ids_by_name(sub_accounts, _query_filter_values(sub_account)),
    )
    query = _apply_sub_account_filter(
        query,
        ExternalTradingTargetPosition.sub_account_id,
        _sub_account_ids_by_strategy_name(strategy_name_by_sub_account_id, _query_filter_values(strategy)),
    )
    query = query.order_by(
        ExternalTradingTargetPosition.sub_account_id.asc(),
        ExternalTradingTargetPosition.symbol.asc(),
    )
    rows, pagination = _paginate_query(query, page=page, page_size=page_size)
    page_sub_account_ids = sorted({row.sub_account_id for row in rows if row.sub_account_id})
    ledger_by_sub_account = {
        sub_account_id: get_ledger_positions(db, sub_account_id)
        for sub_account_id in page_sub_account_ids
    }
    today_buy_by_key = get_today_buy_quantities(db, page_sub_account_ids)
    open_by_sub_account = {
        sub_account_id: get_open_order_quantities(db, sub_account_id)
        for sub_account_id in page_sub_account_ids
    }
    serialized_rows = []
    for row in rows:
        symbol_key = normalize_symbol(row.symbol)
        serialized_rows.append(_serialize_target_position_status(
            row,
            sub_account_by_id.get(row.sub_account_id),
            ledger_by_sub_account.get(row.sub_account_id, {}).get(symbol_key),
            open_by_sub_account.get(row.sub_account_id, {}).get(symbol_key),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
            today_buy_by_key.get((row.sub_account_id, symbol_key), 0),
        ))
    _attach_symbol_names_to_payloads(serialized_rows)
    return {
        "rows": serialized_rows,
        "pagination": pagination,
        "price_details": await _prefetch_table_price_details(account, serialized_rows),
        "filter_options": {
            "symbol": _symbol_filter_options(_distinct_active_target_symbols(db, account_id, account.id)),
            "sub_account": _filter_options(row.name for row in sub_accounts),
            "strategy": _filter_options(strategy_name_by_sub_account_id.values()),
        },
    }


@router.get("/{external_account_id}/executor/status/ledger-positions")
async def get_external_trading_executor_status_ledger_positions(
    external_account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    symbol: Optional[str] = Query(None),
    sub_account: Optional[str] = Query(None),
    strategy: Optional[str] = Query(None),
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    context = _load_executor_sub_account_context(db, main_db, account, account_id)
    sub_accounts = context["sub_accounts"]
    sub_account_by_id = context["sub_account_by_id"]
    strategy_name_by_sub_account_id = context["strategy_name_by_sub_account_id"]

    query = (
        db.query(ExternalTradingLedgerPosition)
        .filter(
            ExternalTradingLedgerPosition.account_id == account_id,
            ExternalTradingLedgerPosition.external_trading_account_id == account.id,
        )
    )
    query = _apply_symbol_filter(query, ExternalTradingLedgerPosition.symbol, _query_filter_symbols(symbol))
    query = _apply_sub_account_filter(
        query,
        ExternalTradingLedgerPosition.sub_account_id,
        _sub_account_ids_by_name(sub_accounts, _query_filter_values(sub_account)),
    )
    query = _apply_sub_account_filter(
        query,
        ExternalTradingLedgerPosition.sub_account_id,
        _sub_account_ids_by_strategy_name(strategy_name_by_sub_account_id, _query_filter_values(strategy)),
    )
    query = query.order_by(
        ExternalTradingLedgerPosition.sub_account_id.asc(),
        ExternalTradingLedgerPosition.market_value.desc(),
        ExternalTradingLedgerPosition.symbol.asc(),
    )
    rows, pagination = _paginate_query(query, page=page, page_size=page_size)
    today_buy_by_key = get_today_buy_quantities(db, sorted({row.sub_account_id for row in rows if row.sub_account_id}))
    serialized_rows = [
        _serialize_ledger_position_status(
            row,
            sub_account_by_id.get(row.sub_account_id),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
            today_buy_by_key.get((row.sub_account_id, normalize_symbol(row.symbol)), 0),
        )
        for row in rows
    ]
    _attach_symbol_names_to_payloads(serialized_rows)
    return {
        "rows": serialized_rows,
        "pagination": pagination,
        "price_details": await _prefetch_table_price_details(account, serialized_rows),
        "filter_options": {
            "symbol": _symbol_filter_options(_distinct_symbols(db, ExternalTradingLedgerPosition, account_id, account.id)),
            "sub_account": _filter_options(row.name for row in sub_accounts),
            "strategy": _filter_options(strategy_name_by_sub_account_id.values()),
        },
    }


@router.get("/{external_account_id}/executor/status/orders")
async def get_external_trading_executor_status_orders(
    external_account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    symbol: Optional[str] = Query(None),
    sub_account: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    context = _load_executor_sub_account_context(db, main_db, account, account_id)
    sub_accounts = context["sub_accounts"]
    sub_account_by_id = context["sub_account_by_id"]
    strategy_name_by_sub_account_id = context["strategy_name_by_sub_account_id"]

    query = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.account_id == account_id,
            ExternalTradingOrder.external_trading_account_id == account.id,
        )
    )
    query = _apply_symbol_filter(query, ExternalTradingOrder.symbol, _query_filter_symbols(symbol))
    query = _apply_sub_account_filter(
        query,
        ExternalTradingOrder.sub_account_id,
        _sub_account_ids_by_name(sub_accounts, _query_filter_values(sub_account)),
    )
    roles = _query_filter_values(role)
    if roles:
        query = query.filter(ExternalTradingOrder.allocation_role.in_(roles))
    query = query.order_by(ExternalTradingOrder.created_at.desc(), ExternalTradingOrder.id.desc())
    rows, pagination = _paginate_query(query, page=page, page_size=page_size)
    repair_summaries = _load_parent_order_child_repair_summaries(
        db,
        [
            row.id
            for row in rows
            if (row.allocation_role or "").upper() == "PARENT"
        ],
    )
    serialized_rows = []
    for row in rows:
        item = _serialize_order_status(
            row,
            sub_account_by_id.get(row.sub_account_id),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
        )
        _attach_parent_order_repair_summary(item, row, repair_summaries.get(row.id))
        serialized_rows.append(item)
    _attach_symbol_names_to_payloads(serialized_rows)
    return {
        "rows": serialized_rows,
        "pagination": pagination,
        "price_details": await _prefetch_table_price_details(account, serialized_rows),
        "filter_options": {
            "symbol": _symbol_filter_options(_distinct_symbols(db, ExternalTradingOrder, account_id, account.id)),
            "sub_account": _filter_options(row.name for row in sub_accounts),
            "role": _role_filter_options(["PARENT", "CHILD", "BLOCK"]),
        },
    }


@router.get("/{external_account_id}/executor/status/fills")
async def get_external_trading_executor_status_fills(
    external_account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    symbol: Optional[str] = Query(None),
    sub_account: Optional[str] = Query(None),
    strategy: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    context = _load_executor_sub_account_context(db, main_db, account, account_id)
    sub_accounts = context["sub_accounts"]
    sub_account_by_id = context["sub_account_by_id"]
    strategy_name_by_sub_account_id = context["strategy_name_by_sub_account_id"]

    query = (
        db.query(ExternalTradingOrderFill)
        .filter(
            ExternalTradingOrderFill.account_id == account_id,
            ExternalTradingOrderFill.external_trading_account_id == account.id,
        )
    )
    query = _apply_symbol_filter(query, ExternalTradingOrderFill.symbol, _query_filter_symbols(symbol))
    query = _apply_nullable_sub_account_name_filter(
        query,
        ExternalTradingOrderFill.sub_account_id,
        sub_accounts,
        _query_filter_values(sub_account),
        parent_label="净额父单",
    )
    query = _apply_nullable_strategy_filter(
        query,
        ExternalTradingOrderFill.sub_account_id,
        strategy_name_by_sub_account_id,
        _query_filter_values(strategy),
        parent_label="券商原始成交",
    )
    query = _apply_fill_role_filter(query, _query_filter_values(role))
    query = query.order_by(ExternalTradingOrderFill.created_at.desc(), ExternalTradingOrderFill.id.desc())
    rows, pagination = _paginate_query(query, page=page, page_size=page_size)
    serialized_rows = [
        _serialize_fill_status(
            row,
            sub_account_by_id.get(row.sub_account_id),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
        )
        for row in rows
    ]
    _attach_symbol_names_to_payloads(serialized_rows)
    has_parent_fills = (
        db.query(ExternalTradingOrderFill.id)
        .filter(
            ExternalTradingOrderFill.account_id == account_id,
            ExternalTradingOrderFill.external_trading_account_id == account.id,
            ExternalTradingOrderFill.sub_account_id.is_(None),
        )
        .first()
        is not None
    )
    sub_account_options = [row.name for row in sub_accounts]
    strategy_options = list(strategy_name_by_sub_account_id.values())
    if has_parent_fills:
        sub_account_options.append("净额父单")
        strategy_options.append("券商原始成交")
    return {
        "rows": serialized_rows,
        "pagination": pagination,
        "price_details": await _prefetch_table_price_details(account, serialized_rows),
        "filter_options": {
            "symbol": _filter_options(_distinct_symbols(db, ExternalTradingOrderFill, account_id, account.id)),
            "sub_account": _filter_options(sub_account_options),
            "strategy": _filter_options(strategy_options),
            "role": _role_filter_options(["PARENT", "CHILD"]),
        },
    }


@router.get("/{external_account_id}/executor/status/events")
async def get_external_trading_executor_status_events(
    external_account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    symbol: Optional[str] = Query(None),
    sub_account: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    process_status: Optional[str] = Query(None),
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    context = _load_executor_sub_account_context(db, main_db, account, account_id)
    sub_accounts = context["sub_accounts"]
    sub_account_by_id = context["sub_account_by_id"]
    strategy_name_by_sub_account_id = context["strategy_name_by_sub_account_id"]

    query = (
        db.query(ExternalTradingEventLog)
        .filter(
            ExternalTradingEventLog.account_id == account_id,
            ExternalTradingEventLog.external_trading_account_id == account.id,
        )
    )
    query = _apply_symbol_filter(query, ExternalTradingEventLog.symbol, _query_filter_symbols(symbol))
    query = _apply_sub_account_filter(
        query,
        ExternalTradingEventLog.matched_sub_account_id,
        _sub_account_ids_by_name(sub_accounts, _query_filter_values(sub_account)),
    )
    event_types = _query_filter_values(event_type)
    if event_types:
        query = query.filter(ExternalTradingEventLog.event_type.in_(event_types))
    process_statuses = _query_filter_values(process_status)
    if process_statuses:
        query = query.filter(ExternalTradingEventLog.process_status.in_(process_statuses))
    query = query.order_by(ExternalTradingEventLog.created_at.desc(), ExternalTradingEventLog.id.desc())
    rows, pagination = _paginate_query(query, page=page, page_size=page_size)

    matched_order_ids = sorted({row.matched_order_id for row in rows if row.matched_order_id})
    matched_orders = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.account_id == account_id,
            ExternalTradingOrder.external_trading_account_id == account.id,
            ExternalTradingOrder.id.in_(matched_order_ids),
        )
        .all()
        if matched_order_ids
        else []
    )
    matched_order_by_id = {row.id: row for row in matched_orders}
    parent_order_ids = [
        row.id
        for row in matched_orders
        if (row.allocation_role or "").upper() == "PARENT"
    ]
    children_by_parent_id: Dict[int, List[ExternalTradingOrder]] = {}
    if parent_order_ids:
        child_orders = (
            db.query(ExternalTradingOrder)
            .filter(
                ExternalTradingOrder.account_id == account_id,
                ExternalTradingOrder.external_trading_account_id == account.id,
                ExternalTradingOrder.parent_order_id.in_(parent_order_ids),
            )
            .order_by(ExternalTradingOrder.parent_order_id.asc(), ExternalTradingOrder.id.asc())
            .all()
        )
        for child in child_orders:
            children_by_parent_id.setdefault(child.parent_order_id, []).append(child)

    serialized_rows = [
        _serialize_event_log_status(
            row,
            matched_order_by_id.get(row.matched_order_id),
            sub_account_by_id,
            strategy_name_by_sub_account_id,
            children_by_parent_id,
        )
        for row in rows
    ]
    _attach_top_level_symbol_names(serialized_rows)
    return {
        "rows": serialized_rows,
        "pagination": pagination,
        "price_details": {},
        "filter_options": {
            "symbol": _symbol_filter_options(
                _distinct_event_values(db, ExternalTradingEventLog.symbol, account_id, account.id)
            ),
            "sub_account": _filter_options(row.name for row in sub_accounts),
            "event_type": _filter_options(
                _distinct_event_values(db, ExternalTradingEventLog.event_type, account_id, account.id)
            ),
            "process_status": _filter_options(
                _distinct_event_values(db, ExternalTradingEventLog.process_status, account_id, account.id)
            ),
        },
    }


@router.get("/{external_account_id}/status")
async def get_external_trading_account_status(
    external_account_id: int,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    return _serialize_account(account)


@router.post("/{external_account_id}/snapshots")
async def get_external_snapshots(
    external_account_id: int,
    payload: QuoteRequest,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.get_snapshots(
            account.id,
            payload.symbols,
            timeout=payload.timeout_seconds,
        )
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{external_account_id}/orders")
async def place_external_orders(
    external_account_id: int,
    payload: OrderBatchRequest,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.place_orders(
            account.id,
            [order.dict() for order in payload.orders],
            timeout=payload.timeout_seconds,
        )
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{external_account_id}/orders/cancel")
async def cancel_external_orders(
    external_account_id: int,
    payload: OrderCancelBatchRequest,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.cancel_orders(
            account.id,
            [order.dict() for order in payload.orders],
            timeout=payload.timeout_seconds,
        )
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{external_account_id}/orders/{order_id}/mark-success")
async def mark_external_block_order_success(
    external_account_id: int,
    order_id: int,
    payload: ManualBlockSuccessRequest = ManualBlockSuccessRequest(),
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    order = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.id == order_id,
            ExternalTradingOrder.account_id == account_id,
            ExternalTradingOrder.external_trading_account_id == account.id,
        )
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="External trading order not found")
    if (order.allocation_role or "").upper() != "BLOCK":
        raise HTTPException(status_code=400, detail="只有阻断单支持手工标记成功")
    if (order.status or "").upper() != STATUS_BLOCKED_INSUFFICIENT_POSITION:
        raise HTTPException(status_code=400, detail="当前仅支持“持仓不足”阻断单手工标记成功")

    try:
        fill_price, price_source = resolve_manual_block_fill_price(
            db,
            order,
            explicit_price=payload.price,
        )
        fill = mark_block_order_manual_success(
            db,
            order=order,
            fill_price=fill_price,
            price_source=price_source,
            traded_at=payload.traded_at,
            note=payload.note,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        db.rollback()
        raise

    return {
        "message": f"已按 {fill_price:.2f} 元将阻断单标记成功并回写账本",
        "order": serialize_order(order),
        "fill": {
            "id": fill.id,
            "fill_key": fill.fill_key,
            "quantity": fill.quantity,
            "price": fill.price,
            "amount": fill.amount,
            "traded_at": fill.traded_at.isoformat() if fill.traded_at else None,
        },
        "price_source": price_source,
    }


@router.post("/{external_account_id}/orders/{order_id}/repair-parent-fill")
async def repair_external_parent_order_fill(
    external_account_id: int,
    order_id: int,
    payload: ManualParentFillRepairRequest,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    order = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.id == order_id,
            ExternalTradingOrder.account_id == account_id,
            ExternalTradingOrder.external_trading_account_id == account.id,
        )
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="External trading order not found")

    try:
        result = repair_parent_order_manual_fill(
            db,
            order=order,
            fill_price=payload.price,
            traded_at=payload.traded_at,
            note=payload.note,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        db.rollback()
        raise

    parent_fill = result["parent_fill"]
    updated_children = result["updated_children"]
    return {
        "message": f"已按 {result['fill_price']:.2f} 元补成交 {result['repair_quantity']} 股并分配到子单",
        "order": serialize_order(order),
        "parent_fill": {
            "id": parent_fill.id,
            "fill_key": parent_fill.fill_key,
            "quantity": parent_fill.quantity,
            "price": parent_fill.price,
            "amount": parent_fill.amount,
            "traded_at": parent_fill.traded_at.isoformat() if parent_fill.traded_at else None,
        },
        "updated_child_order_ids": [child.id for child in updated_children],
    }


@router.get("/{external_account_id}/snapshot")
async def get_external_account_snapshot(
    external_account_id: int,
    timeout_seconds: float = 10.0,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.get_account_snapshot(account.id, timeout=timeout_seconds)
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/{external_account_id}/positions")
async def get_external_positions(
    external_account_id: int,
    timeout_seconds: float = 10.0,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.get_positions(account.id, timeout=timeout_seconds)
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/{external_account_id}/broker-positions")
async def get_external_broker_positions(
    external_account_id: int,
    refresh_if_open: bool = True,
    timeout_seconds: float = 10.0,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")

    runtime_status = external_trading_hub.get_status(account.id)
    market_type = normalize_external_trading_market_type(getattr(account, "market_type", None))
    market_window_open = bool(is_external_trading_market_open(market_type))
    refresh_attempted = bool(refresh_if_open and market_window_open)
    refresh_error = None
    refreshed = False
    snapshot = None

    if refresh_if_open and market_window_open:
        if runtime_status.get("connected"):
            try:
                payload = await external_trading_hub.get_positions(account.id, timeout=timeout_seconds)
                snapshot = persist_broker_position_snapshot(
                    db,
                    account=account,
                    payload=payload,
                    snapshot_source="refresh",
                    snapshot_kind="intraday",
                    market_window_open=True,
                )
                db.commit()
                db.refresh(snapshot)
                refreshed = True
            except ExternalTradingConnectionError as exc:
                db.rollback()
                refresh_error = str(exc)
            except Exception as exc:
                db.rollback()
                refresh_error = str(exc)
        else:
            refresh_error = "外部交易账号未连接，已回退到快照"

    if snapshot is None:
        snapshot = get_latest_broker_position_snapshot(
            db,
            external_trading_account_id=account.id,
            account_id=account_id,
        )

    ledger_positions = get_account_ledger_positions(db, account.id, account_id)
    target_positions = _get_account_target_quantities(db, account.id, account_id)
    response = _serialize_broker_position_view(snapshot, ledger_positions, target_positions)
    response["account"] = {
        "id": account.id,
        "account_id": account.account_id,
        "name": account.name,
        "identifier": account.identifier,
        "market_type": market_type,
        "connected": bool(runtime_status.get("connected")),
        "pending_count": runtime_status.get("pending_count", 0),
        "connected_at": runtime_status.get("connected_at"),
        "last_seen_at": runtime_status.get("last_seen_at"),
    }
    response["refresh"] = {
        "attempted": refresh_attempted,
        "refreshed": refreshed,
        "market_window_open": market_window_open,
        "market_type": market_type,
        "market_label": external_trading_market_label(market_type),
        "connected": bool(runtime_status.get("connected")),
        "refresh_if_open": refresh_if_open,
        "error": refresh_error,
        "timeout_seconds": timeout_seconds,
    }
    response["snapshot_exists"] = snapshot is not None

    symbols = set()
    _collect_symbol_fields(response, symbols)
    stock_name_by_symbol = _load_a_stock_name_map(symbols)
    _attach_symbol_names(response, stock_name_by_symbol)
    return response


@router.get("/{external_account_id}/assets")
async def get_external_assets(
    external_account_id: int,
    timeout_seconds: float = 10.0,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.get_assets(account.id, timeout=timeout_seconds)
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/{external_account_id}/orders/today")
async def get_external_today_orders(
    external_account_id: int,
    timeout_seconds: float = 10.0,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.get_today_orders(account.id, timeout=timeout_seconds)
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.websocket("/ws")
async def external_trading_websocket(websocket: WebSocket):
    account_id = websocket.query_params.get("account_id")
    identifier = websocket.query_params.get("identifier")
    ts = websocket.query_params.get("ts")
    nonce = websocket.query_params.get("nonce")
    signature = websocket.query_params.get("signature")

    if not account_id or not identifier:
        logger.warning(
            "Rejected external trading WebSocket: missing account_id/identifier account_id=%r identifier=%r",
            account_id,
            identifier,
        )
        await websocket.close(code=1008, reason="account_id and identifier are required")
        return
    if not is_valid_account(account_id):
        logger.warning("Rejected external trading WebSocket: invalid account_id=%r identifier=%r", account_id, identifier)
        await websocket.close(code=1008, reason="invalid account_id")
        return
    try:
        verify_handshake_signature(account_id, identifier, ts, nonce, signature)
    except ExternalTradingCryptoError as exc:
        logger.warning(
            "Rejected external trading WebSocket: signature failed account_id=%r identifier=%r reason=%s",
            account_id,
            identifier,
            exc,
        )
        await websocket.close(code=1008, reason=str(exc))
        return

    db = ExternalTradingDBSession()
    try:
        account = db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.account_id == account_id,
            ExternalTradingAccount.identifier == identifier,
        ).first()
        if not account:
            logger.warning(
                "Rejected external trading WebSocket: account not found account_id=%r identifier=%r",
                account_id,
                identifier,
            )
            await websocket.close(code=1008, reason="external trading account not found")
            return
        if not account.enabled:
            logger.warning(
                "Rejected external trading WebSocket: account disabled id=%s account_id=%r identifier=%r",
                account.id,
                account_id,
                identifier,
            )
            await websocket.close(code=1008, reason="external trading account disabled")
            return

        conn = await external_trading_hub.connect(websocket, account)
    finally:
        db.close()

    try:
        while True:
            data = await websocket.receive_text()
            await external_trading_hub.handle_client_message(conn.account_pk, conn.connection_id, data)
    except WebSocketDisconnect:
        await external_trading_hub.disconnect(conn.account_pk, conn.connection_id, reason="client disconnected")
    except Exception as exc:
        await external_trading_hub.disconnect(conn.account_pk, conn.connection_id, reason=f"websocket error: {exc}")
