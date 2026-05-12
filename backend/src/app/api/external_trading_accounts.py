from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session as OrmSession

from ...core.database import (
    SnowballCopyConfig,
    W20MomentumLiveConfig,
    get_db,
)
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingOrder,
    ExternalTradingOrderFill,
    ExternalTradingSubAccount,
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
    DEFAULT_EXECUTOR_CLIP_SELL_TO_AVAILABLE,
    DEFAULT_EXECUTOR_LOT_SIZE,
    DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    DEFAULT_EXECUTOR_PRICE_LEVEL,
    DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE,
    normalize_lot_size,
    normalize_clip_sell_to_available,
    normalize_max_replace_count,
    normalize_price_level,
    normalize_price_level_sequence,
    normalize_timeout_seconds,
    resolve_execution_policy,
)
from ...core.services.external_trading_ledger import (
    ACTIVE_ORDER_STATUSES,
    STRATEGY_SNOWBALL,
    STRATEGY_W20,
    build_netted_target_execution_plan,
    expire_insufficient_sellable_blocks,
    get_ledger_positions,
    get_open_order_quantities,
    normalize_symbol,
    safe_int,
    serialize_ledger_position,
    serialize_order,
    serialize_sub_account,
)
from ...core.services.external_trading_crypto import (
    ExternalTradingCryptoError,
    verify_handshake_signature,
)
from .account import is_valid_account, valid_account

router = APIRouter(prefix="/api/external-trading-accounts", tags=["external-trading-accounts"])
logger = logging.getLogger(__name__)


class ExternalTradingAccountBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    identifier: str = Field(..., min_length=1, max_length=128)
    enabled: bool = True
    executor_enabled: bool = True
    executor_price_level: int = Field(default=DEFAULT_EXECUTOR_PRICE_LEVEL)
    executor_lot_size: int = Field(default=DEFAULT_EXECUTOR_LOT_SIZE, ge=1)
    executor_order_timeout_seconds: int = Field(default=DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS, ge=10, le=3600)
    executor_max_replace_count: int = Field(default=DEFAULT_EXECUTOR_MAX_REPLACE_COUNT, ge=0, le=20)
    executor_clip_sell_to_available: bool = DEFAULT_EXECUTOR_CLIP_SELL_TO_AVAILABLE
    executor_price_level_sequence: List[int] = Field(default_factory=lambda: DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE.copy())

    @validator("name", "identifier", pre=True)
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

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


class ExternalTradingAccountCreate(ExternalTradingAccountBase):
    pass


class ExternalTradingAccountUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    identifier: Optional[str] = Field(default=None, min_length=1, max_length=128)
    enabled: Optional[bool] = None
    executor_enabled: Optional[bool] = None
    executor_price_level: Optional[int] = None
    executor_lot_size: Optional[int] = Field(default=None, ge=1)
    executor_order_timeout_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
    executor_max_replace_count: Optional[int] = Field(default=None, ge=0, le=20)
    executor_clip_sell_to_available: Optional[bool] = None
    executor_price_level_sequence: Optional[List[int]] = None

    @validator("name", "identifier", pre=True)
    def strip_text(cls, value):
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
        if value is None:
            return value
        return normalize_price_level_sequence(value)


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


class ExternalTradingSubAccountPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    cash_allocated: float = Field(default=0.0, ge=0)
    remark: Optional[str] = Field(default=None, max_length=1000)
    enabled: bool = True
    executor_price_level: Optional[int] = None
    executor_lot_size: Optional[int] = Field(default=None, ge=1)
    executor_order_timeout_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
    executor_max_replace_count: Optional[int] = Field(default=None, ge=0, le=20)
    executor_clip_sell_to_available: Optional[bool] = None
    executor_price_level_sequence: Optional[List[int]] = None

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


class NettedExecutorRequest(BaseModel):
    sub_account_ids: Optional[List[int]] = None
    price_level: Optional[int] = None
    lot_size: Optional[int] = Field(default=None, ge=1)
    timeout_seconds: Optional[float] = Field(default=None, ge=10.0, le=3600.0)
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
    executor_clip_sell_to_available = normalize_clip_sell_to_available(
        getattr(account, "executor_clip_sell_to_available", None)
    )
    executor_price_level_sequence = normalize_price_level_sequence(
        getattr(account, "executor_price_level_sequence", None)
    )
    return {
        "id": account.id,
        "account_id": account.account_id,
        "name": account.name,
        "identifier": account.identifier,
        "enabled": account.enabled,
        "executor_enabled": getattr(account, "executor_enabled", True),
        "executor_price_level": executor_price_level,
        "executor_lot_size": executor_lot_size,
        "executor_order_timeout_seconds": executor_order_timeout_seconds,
        "executor_max_replace_count": executor_max_replace_count,
        "executor_clip_sell_to_available": executor_clip_sell_to_available,
        "executor_price_level_sequence": executor_price_level_sequence,
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


def _strategy_binding_name(main_db: OrmSession, sub_account: ExternalTradingSubAccount) -> Optional[str]:
    if not sub_account.strategy_type or not sub_account.strategy_config_id:
        return None
    if sub_account.strategy_type == STRATEGY_W20:
        config = main_db.query(W20MomentumLiveConfig).filter(
            W20MomentumLiveConfig.id == sub_account.strategy_config_id,
            W20MomentumLiveConfig.account_id == sub_account.account_id,
        ).first()
        return config.name if config else "W20 风险调整动量虚拟盘（配置已删除）"
    if sub_account.strategy_type == STRATEGY_SNOWBALL:
        config = main_db.query(SnowballCopyConfig).filter(
            SnowballCopyConfig.id == sub_account.strategy_config_id,
            SnowballCopyConfig.account_id == sub_account.account_id,
        ).first()
        if config:
            return config.combination_name or config.combination_id or "A股雪球跟单"
        return "A股雪球跟单（配置已删除）"
    return sub_account.strategy_type


def _serialize_sub_account_with_binding(
    db: OrmSession,
    sub_account: ExternalTradingSubAccount,
    positions: Optional[List[ExternalTradingLedgerPosition]] = None,
    *,
    main_db: OrmSession,
) -> Dict[str, Any]:
    item = serialize_sub_account(sub_account)
    if positions is None:
        positions = (
            db.query(ExternalTradingLedgerPosition)
            .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
            .all()
        )
    position_market_value = 0.0
    for position in positions:
        market_value = _safe_float(position.market_value)
        if market_value <= 0 and _safe_float(position.quantity) > 0:
            reference_price = _safe_float(position.market_price, _safe_float(position.avg_cost))
            market_value = _safe_float(position.quantity) * reference_price
        position_market_value += market_value
    item["position_market_value"] = round(position_market_value, 2)
    item["net_asset"] = round(_safe_float(sub_account.cash_available) + position_market_value, 2)
    strategy_name = _strategy_binding_name(main_db, sub_account)
    item["strategy_name"] = strategy_name
    item["binding_status"] = "BOUND" if strategy_name else "FREE"
    item["binding_label"] = strategy_name or "空闲"
    account = db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == sub_account.external_trading_account_id,
        ExternalTradingAccount.account_id == sub_account.account_id,
    ).first()
    item["effective_executor_policy"] = resolve_execution_policy(account, sub_account) if account else None
    if positions is not None:
        item["positions"] = [serialize_ledger_position(pos) for pos in positions]
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


def _quote_reference_price(quote: Dict[str, Any]) -> float:
    price = _safe_float(quote.get("price") or quote.get("last_price"))
    if price > 0:
        return price
    bid = _safe_float(quote.get("bid"))
    ask = _safe_float(quote.get("ask"))
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 6)
    return bid or ask or 0.0


def _iso(value: Any) -> Optional[str]:
    if not value:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _serialize_target_position_status(
    row: ExternalTradingTargetPosition,
    sub_account: Optional[ExternalTradingSubAccount],
    ledger_position: Optional[ExternalTradingLedgerPosition],
    open_quantities: Optional[Dict[str, int]],
    strategy_name: Optional[str],
) -> Dict[str, Any]:
    symbol = normalize_symbol(row.symbol)
    current_quantity = safe_int(getattr(ledger_position, "quantity", 0))
    available_quantity = safe_int(getattr(ledger_position, "available_quantity", current_quantity))
    pending_buy = safe_int((open_quantities or {}).get("BUY"))
    pending_sell = safe_int((open_quantities or {}).get("SELL"))
    effective_quantity = current_quantity + pending_buy - pending_sell
    target_quantity = safe_int(row.target_quantity)
    delta_quantity = target_quantity - effective_quantity
    side = "BUY" if delta_quantity > 0 else "SELL" if delta_quantity < 0 else None
    demand_quantity = abs(delta_quantity)
    if side == "SELL":
        demand_quantity = min(demand_quantity, max(available_quantity - pending_sell, 0))

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
        "available_quantity": available_quantity,
        "pending_buy_quantity": pending_buy,
        "pending_sell_quantity": pending_sell,
        "effective_quantity": effective_quantity,
        "delta_quantity": delta_quantity,
        "side": side,
        "demand_quantity": demand_quantity,
        "target_weight_pct": row.target_weight_pct,
        "target_value": row.target_value,
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
) -> Dict[str, Any]:
    item = serialize_ledger_position(row)
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
        "traded_at": _iso(row.traded_at),
        "created_at": _iso(row.created_at),
    }


async def _build_netted_executor_plan(
    db: OrmSession,
    account: ExternalTradingAccount,
    owner_account_id: str,
    payload: NettedExecutorRequest,
    *,
    require_connection: bool,
) -> Dict[str, Any]:
    price_level = normalize_price_level(payload.price_level, normalize_price_level(account.executor_price_level))
    lot_size = normalize_lot_size(payload.lot_size, normalize_lot_size(account.executor_lot_size))
    timeout_seconds = normalize_timeout_seconds(
        payload.timeout_seconds,
        normalize_timeout_seconds(account.executor_order_timeout_seconds),
    )
    plan = build_netted_target_execution_plan(
        db,
        account_id=owner_account_id,
        external_trading_account_id=account.id,
        sub_account_ids=payload.sub_account_ids,
        price_level=price_level,
        lot_size=lot_size,
        order_timeout_seconds=timeout_seconds,
        max_replace_count=normalize_max_replace_count(account.executor_max_replace_count),
        clip_sell_to_available=normalize_clip_sell_to_available(account.executor_clip_sell_to_available),
        price_level_sequence=normalize_price_level_sequence(account.executor_price_level_sequence),
    )
    symbols = plan.get("symbols") or []
    connected = external_trading_hub.get_status(account.id).get("connected")
    if require_connection and not connected:
        raise ExternalTradingConnectionError("外部交易账号未连接")
    if symbols and connected:
        quotes_resp = await external_trading_hub.get_quotes(account.id, symbols, timeout=min(timeout_seconds, 15.0))
        reference_prices = {}
        for quote in quotes_resp.get("quotes") or []:
            symbol = normalize_symbol(quote.get("symbol") or quote.get("client_symbol"))
            if not symbol:
                continue
            reference_prices[symbol] = _quote_reference_price(quote)
        plan = build_netted_target_execution_plan(
            db,
            account_id=owner_account_id,
            external_trading_account_id=account.id,
            sub_account_ids=payload.sub_account_ids,
            price_level=price_level,
            lot_size=lot_size,
            order_timeout_seconds=timeout_seconds,
            max_replace_count=normalize_max_replace_count(account.executor_max_replace_count),
            clip_sell_to_available=normalize_clip_sell_to_available(account.executor_clip_sell_to_available),
            price_level_sequence=normalize_price_level_sequence(account.executor_price_level_sequence),
            reference_prices=reference_prices,
        )
        plan["reference_prices"] = reference_prices
    else:
        plan["reference_prices"] = {}
    plan["connected"] = bool(connected)
    plan["account_executor_policy"] = resolve_execution_policy(account)
    return plan


@router.get("", response_model=List[ExternalTradingAccountResponse])
async def list_external_trading_accounts(
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    accounts = (
        db.query(ExternalTradingAccount)
        .filter(ExternalTradingAccount.account_id == account_id)
        .order_by(ExternalTradingAccount.updated_at.desc())
        .all()
    )
    return [_serialize_account(account) for account in accounts]


@router.post("", response_model=ExternalTradingAccountResponse)
async def create_external_trading_account(
    payload: ExternalTradingAccountCreate,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    _ensure_unique(db, account_id, payload.name, payload.identifier)
    account = ExternalTradingAccount(
        account_id=account_id,
        name=payload.name,
        identifier=payload.identifier,
        enabled=payload.enabled,
        executor_enabled=payload.executor_enabled,
        executor_price_level=payload.executor_price_level,
        executor_lot_size=payload.executor_lot_size,
        executor_order_timeout_seconds=payload.executor_order_timeout_seconds,
        executor_max_replace_count=payload.executor_max_replace_count,
        executor_clip_sell_to_available=payload.executor_clip_sell_to_available,
        executor_price_level_sequence=payload.executor_price_level_sequence,
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

    _ensure_unique(
        db,
        account_id,
        update_data.get("name"),
        update_data.get("identifier"),
        exclude_id=account.id,
    )

    for key, value in update_data.items():
        setattr(account, key, value)
    account.updated_at = datetime.now()
    db.commit()
    db.refresh(account)

    if account.enabled is False:
        await external_trading_hub.disconnect_account(account.id, reason="account disabled")
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
    for config in main_db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.account_id == account_id,
        W20MomentumLiveConfig.external_trading_account_id == account_pk,
    ).all():
        config.external_trading_account_id = None
        config.live_sub_account_id = None
        config.live_trade_enabled = False
        config.updated_at = now
    for config in main_db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.account_id == account_id,
        SnowballCopyConfig.external_trading_account_id == account_pk,
    ).all():
        config.external_trading_account_id = None
        config.live_sub_account_id = None
        config.live_trade_enabled = False
        config.updated_at = now
    db.query(ExternalTradingOrderFill).filter(
        ExternalTradingOrderFill.external_trading_account_id == account_pk
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
    _get_account_or_404(db, account_id, external_account_id)
    sub_accounts = (
        db.query(ExternalTradingSubAccount)
        .filter(
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.external_trading_account_id == external_account_id,
        )
        .order_by(ExternalTradingSubAccount.updated_at.desc(), ExternalTradingSubAccount.id.desc())
        .all()
    )
    result = []
    for sub_account in sub_accounts:
        positions = (
            db.query(ExternalTradingLedgerPosition)
            .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
            .order_by(ExternalTradingLedgerPosition.symbol.asc())
            .all()
        )
        result.append(_serialize_sub_account_with_binding(db, sub_account, positions, main_db=main_db))
    return result


@router.post("/{external_account_id}/sub-accounts")
async def create_external_trading_sub_account(
    external_account_id: int,
    payload: ExternalTradingSubAccountPayload,
    db: OrmSession = Depends(get_external_trading_db),
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    _get_account_or_404(db, account_id, external_account_id)
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
        executor_order_timeout_seconds=payload.executor_order_timeout_seconds,
        executor_max_replace_count=payload.executor_max_replace_count,
        executor_clip_sell_to_available=payload.executor_clip_sell_to_available,
        executor_price_level_sequence=payload.executor_price_level_sequence,
        created_at=now,
        updated_at=now,
    )
    db.add(sub_account)
    db.commit()
    db.refresh(sub_account)
    return _serialize_sub_account_with_binding(db, sub_account, main_db=main_db)


@router.put("/{external_account_id}/sub-accounts/{sub_account_id}")
async def update_external_trading_sub_account(
    external_account_id: int,
    sub_account_id: int,
    payload: ExternalTradingSubAccountPayload,
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
    sub_account.name = payload.name
    sub_account.cash_allocated = payload.cash_allocated
    sub_account.remark = payload.remark
    sub_account.enabled = payload.enabled
    sub_account.executor_price_level = payload.executor_price_level
    sub_account.executor_lot_size = payload.executor_lot_size
    sub_account.executor_order_timeout_seconds = payload.executor_order_timeout_seconds
    sub_account.executor_max_replace_count = payload.executor_max_replace_count
    sub_account.executor_clip_sell_to_available = payload.executor_clip_sell_to_available
    sub_account.executor_price_level_sequence = payload.executor_price_level_sequence
    sub_account.updated_at = datetime.now()
    db.commit()
    db.refresh(sub_account)
    return _serialize_sub_account_with_binding(db, sub_account, main_db=main_db)


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
    bound_configs = main_db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.account_id == account_id,
        W20MomentumLiveConfig.live_sub_account_id == sub_account.id,
    ).all()
    for config in bound_configs:
        config.live_sub_account_id = None
        config.updated_at = now

    bound_snowball_configs = main_db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.account_id == account_id,
        SnowballCopyConfig.live_sub_account_id == sub_account.id,
    ).all()
    for config in bound_snowball_configs:
        config.live_sub_account_id = None
        config.updated_at = now

    db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingOrder).filter(ExternalTradingOrder.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingTargetPosition).filter(ExternalTradingTargetPosition.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingLedgerPosition).filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id).delete(synchronize_session=False)
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
    main_db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    expire_insufficient_sellable_blocks(db, external_trading_account_id=account.id)

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
    ledger_by_sub_account = {
        sub_account_id: get_ledger_positions(db, sub_account_id)
        for sub_account_id in sub_account_by_id.keys()
    }
    open_by_sub_account = {
        sub_account_id: get_open_order_quantities(db, sub_account_id)
        for sub_account_id in sub_account_by_id.keys()
    }

    target_rows = (
        db.query(ExternalTradingTargetPosition)
        .filter(
            ExternalTradingTargetPosition.account_id == account_id,
            ExternalTradingTargetPosition.external_trading_account_id == account.id,
            ExternalTradingTargetPosition.status == "ACTIVE",
        )
        .order_by(
            ExternalTradingTargetPosition.sub_account_id.asc(),
            ExternalTradingTargetPosition.symbol.asc(),
        )
        .all()
    )
    target_positions = []
    for row in target_rows:
        sub_account = sub_account_by_id.get(row.sub_account_id)
        symbol = normalize_symbol(row.symbol)
        target_positions.append(_serialize_target_position_status(
            row,
            sub_account,
            ledger_by_sub_account.get(row.sub_account_id, {}).get(symbol),
            open_by_sub_account.get(row.sub_account_id, {}).get(symbol),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
        ))

    ledger_rows = (
        db.query(ExternalTradingLedgerPosition)
        .filter(
            ExternalTradingLedgerPosition.account_id == account_id,
            ExternalTradingLedgerPosition.external_trading_account_id == account.id,
        )
        .order_by(
            ExternalTradingLedgerPosition.sub_account_id.asc(),
            ExternalTradingLedgerPosition.market_value.desc(),
            ExternalTradingLedgerPosition.symbol.asc(),
        )
        .all()
    )
    ledger_positions = [
        _serialize_ledger_position_status(
            row,
            sub_account_by_id.get(row.sub_account_id),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
        )
        for row in ledger_rows
    ]

    order_rows = (
        db.query(ExternalTradingOrder)
        .filter(
            ExternalTradingOrder.account_id == account_id,
            ExternalTradingOrder.external_trading_account_id == account.id,
        )
        .order_by(ExternalTradingOrder.created_at.desc(), ExternalTradingOrder.id.desc())
        .limit(300)
        .all()
    )
    orders = [
        _serialize_order_status(
            row,
            sub_account_by_id.get(row.sub_account_id),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
        )
        for row in order_rows
    ]

    fill_rows = (
        db.query(ExternalTradingOrderFill)
        .filter(
            ExternalTradingOrderFill.account_id == account_id,
            ExternalTradingOrderFill.external_trading_account_id == account.id,
        )
        .order_by(ExternalTradingOrderFill.created_at.desc(), ExternalTradingOrderFill.id.desc())
        .limit(300)
        .all()
    )
    fills = [
        _serialize_fill_status(
            row,
            sub_account_by_id.get(row.sub_account_id),
            strategy_name_by_sub_account_id.get(row.sub_account_id),
        )
        for row in fill_rows
    ]

    plan_error = None
    try:
        plan = await _build_netted_executor_plan(
            db,
            account,
            account_id,
            NettedExecutorRequest(),
            require_connection=False,
        )
    except Exception as exc:
        plan_error = str(exc)
        plan = build_netted_target_execution_plan(
            db,
            account_id=account_id,
            external_trading_account_id=account.id,
            price_level=normalize_price_level(account.executor_price_level),
            lot_size=normalize_lot_size(account.executor_lot_size),
            order_timeout_seconds=normalize_timeout_seconds(account.executor_order_timeout_seconds),
            max_replace_count=normalize_max_replace_count(account.executor_max_replace_count),
            clip_sell_to_available=normalize_clip_sell_to_available(account.executor_clip_sell_to_available),
            price_level_sequence=normalize_price_level_sequence(account.executor_price_level_sequence),
        )
        plan["connected"] = external_trading_hub.get_status(account.id).get("connected")
        plan["reference_prices"] = {}
        plan["account_executor_policy"] = resolve_execution_policy(account)

    order_status_counts: Dict[str, int] = {}
    for row in order_rows:
        key = row.status or "UNKNOWN"
        order_status_counts[key] = order_status_counts.get(key, 0) + 1

    return {
        "account": _serialize_account(account),
        "sub_accounts": [
            _serialize_sub_account_with_binding(db, row, main_db=main_db)
            for row in sub_accounts
        ],
        "target_positions": target_positions,
        "ledger_positions": ledger_positions,
        "orders": orders,
        "fills": fills,
        "plan": plan,
        "plan_error": plan_error,
        "summary": {
            "sub_account_count": len(sub_accounts),
            "active_sub_account_count": len([row for row in sub_accounts if row.enabled]),
            "target_position_count": len(target_positions),
            "nonzero_target_count": len([row for row in target_positions if safe_int(row.get("target_quantity")) != 0]),
            "pending_delta_count": len([row for row in target_positions if safe_int(row.get("demand_quantity")) > 0]),
            "ledger_position_count": len(ledger_positions),
            "active_order_count": len([row for row in order_rows if row.status in ACTIVE_ORDER_STATUSES]),
            "order_count": len(order_rows),
            "fill_count": len(fill_rows),
            "order_status_counts": order_status_counts,
            "external_order_count": len(plan.get("external_orders") or []),
            "internal_cross_count": len(plan.get("internal_crosses") or []),
            "demand_count": len(plan.get("demands") or []),
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


@router.post("/{external_account_id}/quotes")
async def get_external_quotes(
    external_account_id: int,
    payload: QuoteRequest,
    db: OrmSession = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    if not account.enabled:
        raise HTTPException(status_code=400, detail="External trading account is disabled")
    try:
        return await external_trading_hub.get_quotes(
            account.id,
            payload.symbols,
            timeout=payload.timeout_seconds,
        )
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


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
