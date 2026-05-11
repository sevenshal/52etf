from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session as OrmSession

from ...core.database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingOrder,
    ExternalTradingOrderFill,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    Session as DBSession,
    W20MomentumLiveConfig,
    get_db,
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
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    DEFAULT_EXECUTOR_PRICE_LEVEL,
    DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE,
    normalize_lot_size,
    normalize_max_replace_count,
    normalize_price_level,
    normalize_price_level_sequence,
    normalize_timeout_seconds,
    resolve_execution_policy,
)
from ...core.services.external_trading_ledger import (
    ACTIVE_ORDER_STATUSES,
    STRATEGY_W20,
    build_netted_target_execution_plan,
    normalize_symbol,
    serialize_ledger_position,
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
    remark: Optional[str] = Field(default=None, max_length=1000)
    enabled: bool = True
    executor_enabled: bool = True
    executor_price_level: int = Field(default=DEFAULT_EXECUTOR_PRICE_LEVEL)
    executor_lot_size: int = Field(default=DEFAULT_EXECUTOR_LOT_SIZE, ge=1)
    executor_order_timeout_seconds: int = Field(default=DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS, ge=10, le=3600)
    executor_max_replace_count: int = Field(default=DEFAULT_EXECUTOR_MAX_REPLACE_COUNT, ge=0, le=20)
    executor_price_level_sequence: List[int] = Field(default_factory=lambda: DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE.copy())

    @validator("name", "identifier", "remark", pre=True)
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
    remark: Optional[str] = Field(default=None, max_length=1000)
    enabled: Optional[bool] = None
    executor_enabled: Optional[bool] = None
    executor_price_level: Optional[int] = None
    executor_lot_size: Optional[int] = Field(default=None, ge=1)
    executor_order_timeout_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
    executor_max_replace_count: Optional[int] = Field(default=None, ge=0, le=20)
    executor_price_level_sequence: Optional[List[int]] = None

    @validator("name", "identifier", "remark", pre=True)
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
    executor_price_level_sequence = normalize_price_level_sequence(
        getattr(account, "executor_price_level_sequence", None)
    )
    return {
        "id": account.id,
        "account_id": account.account_id,
        "name": account.name,
        "identifier": account.identifier,
        "remark": account.remark,
        "enabled": account.enabled,
        "executor_enabled": getattr(account, "executor_enabled", True),
        "executor_price_level": executor_price_level,
        "executor_lot_size": executor_lot_size,
        "executor_order_timeout_seconds": executor_order_timeout_seconds,
        "executor_max_replace_count": executor_max_replace_count,
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


def _strategy_binding_name(db: OrmSession, sub_account: ExternalTradingSubAccount) -> Optional[str]:
    if not sub_account.strategy_type or not sub_account.strategy_config_id:
        return None
    if sub_account.strategy_type == STRATEGY_W20:
        config = db.query(W20MomentumLiveConfig).filter(
            W20MomentumLiveConfig.id == sub_account.strategy_config_id,
            W20MomentumLiveConfig.account_id == sub_account.account_id,
        ).first()
        return config.name if config else "W20 风险调整动量虚拟盘（配置已删除）"
    return sub_account.strategy_type


def _serialize_sub_account_with_binding(
    db: OrmSession,
    sub_account: ExternalTradingSubAccount,
    positions: Optional[List[ExternalTradingLedgerPosition]] = None,
) -> Dict[str, Any]:
    item = serialize_sub_account(sub_account)
    strategy_name = _strategy_binding_name(db, sub_account)
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    _ensure_unique(db, account_id, payload.name, payload.identifier)
    account = ExternalTradingAccount(
        account_id=account_id,
        name=payload.name,
        identifier=payload.identifier,
        remark=payload.remark,
        enabled=payload.enabled,
        executor_enabled=payload.executor_enabled,
        executor_price_level=payload.executor_price_level,
        executor_lot_size=payload.executor_lot_size,
        executor_order_timeout_seconds=payload.executor_order_timeout_seconds,
        executor_max_replace_count=payload.executor_max_replace_count,
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    account_pk = account.id
    db.delete(account)
    db.commit()
    await external_trading_hub.disconnect_account(account_pk, reason="account deleted")
    return {"message": "Deleted successfully"}


@router.get("/{external_account_id}/sub-accounts")
async def list_external_trading_sub_accounts(
    external_account_id: int,
    db: OrmSession = Depends(get_db),
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
        result.append(_serialize_sub_account_with_binding(db, sub_account, positions))
    return result


@router.post("/{external_account_id}/sub-accounts")
async def create_external_trading_sub_account(
    external_account_id: int,
    payload: ExternalTradingSubAccountPayload,
    db: OrmSession = Depends(get_db),
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
        executor_price_level_sequence=payload.executor_price_level_sequence,
        created_at=now,
        updated_at=now,
    )
    db.add(sub_account)
    db.commit()
    db.refresh(sub_account)
    return _serialize_sub_account_with_binding(db, sub_account)


@router.put("/{external_account_id}/sub-accounts/{sub_account_id}")
async def update_external_trading_sub_account(
    external_account_id: int,
    sub_account_id: int,
    payload: ExternalTradingSubAccountPayload,
    db: OrmSession = Depends(get_db),
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
    sub_account.executor_price_level_sequence = payload.executor_price_level_sequence
    sub_account.updated_at = datetime.now()
    db.commit()
    db.refresh(sub_account)
    return _serialize_sub_account_with_binding(db, sub_account)


@router.delete("/{external_account_id}/sub-accounts/{sub_account_id}")
async def delete_external_trading_sub_account(
    external_account_id: int,
    sub_account_id: int,
    db: OrmSession = Depends(get_db),
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
    bound_configs = db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.account_id == account_id,
        W20MomentumLiveConfig.live_sub_account_id == sub_account.id,
    ).all()
    for config in bound_configs:
        config.live_sub_account_id = None
        config.updated_at = now

    db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingOrder).filter(ExternalTradingOrder.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingTargetPosition).filter(ExternalTradingTargetPosition.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.query(ExternalTradingLedgerPosition).filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id).delete(synchronize_session=False)
    db.delete(sub_account)
    db.commit()
    return {"message": "Deleted successfully"}


@router.post("/{external_account_id}/executor/preview")
async def preview_external_trading_netted_executor(
    external_account_id: int,
    payload: NettedExecutorRequest,
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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


@router.get("/{external_account_id}/status")
async def get_external_trading_account_status(
    external_account_id: int,
    db: OrmSession = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    account = _get_account_or_404(db, account_id, external_account_id)
    return _serialize_account(account)


@router.post("/{external_account_id}/quotes")
async def get_external_quotes(
    external_account_id: int,
    payload: QuoteRequest,
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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
    db: OrmSession = Depends(get_db),
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

    db = DBSession()
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
