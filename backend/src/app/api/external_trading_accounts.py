from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session as OrmSession

from ...core.database import ExternalTradingAccount, Session as DBSession, get_db
from ...core.services.external_trading import (
    ExternalTradingConnectionError,
    external_trading_hub,
)
from ...core.services.external_trading_crypto import (
    ExternalTradingCryptoError,
    verify_handshake_signature,
)
from .account import is_valid_account, valid_account

router = APIRouter(prefix="/api/external-trading-accounts", tags=["external-trading-accounts"])


class ExternalTradingAccountBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    identifier: str = Field(..., min_length=1, max_length=128)
    remark: Optional[str] = Field(default=None, max_length=1000)
    enabled: bool = True

    @validator("name", "identifier", "remark", pre=True)
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value


class ExternalTradingAccountCreate(ExternalTradingAccountBase):
    pass


class ExternalTradingAccountUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    identifier: Optional[str] = Field(default=None, min_length=1, max_length=128)
    remark: Optional[str] = Field(default=None, max_length=1000)
    enabled: Optional[bool] = None

    @validator("name", "identifier", "remark", pre=True)
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value


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
    price: Optional[float] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    remark: Optional[str] = None

    @validator("side")
    def normalize_side(cls, value):
        upper = (value or "").upper()
        if upper not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        return upper


class OrderBatchRequest(BaseModel):
    orders: List[OrderInstruction] = Field(..., min_items=1)
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)


def _serialize_account(account: ExternalTradingAccount) -> Dict[str, Any]:
    runtime_status = external_trading_hub.get_status(account.id)
    return {
        "id": account.id,
        "account_id": account.account_id,
        "name": account.name,
        "identifier": account.identifier,
        "remark": account.remark,
        "enabled": account.enabled,
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
    name = websocket.query_params.get("name") or websocket.query_params.get("account_name")
    identifier = (
        websocket.query_params.get("identifier")
        or websocket.query_params.get("uid")
        or websocket.query_params.get("client_id")
    )
    ts = websocket.query_params.get("ts")
    nonce = websocket.query_params.get("nonce")
    signature = websocket.query_params.get("signature")

    if not account_id or not name or not identifier:
        await websocket.close(code=1008, reason="account_id, name and identifier are required")
        return
    if not is_valid_account(account_id):
        await websocket.close(code=1008, reason="invalid account_id")
        return
    try:
        verify_handshake_signature(account_id, name, identifier, ts, nonce, signature)
    except ExternalTradingCryptoError as exc:
        await websocket.close(code=1008, reason=str(exc))
        return

    db = DBSession()
    try:
        account = db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.account_id == account_id,
            ExternalTradingAccount.name == name,
            ExternalTradingAccount.identifier == identifier,
        ).first()
        if not account:
            await websocket.close(code=1008, reason="external trading account not found")
            return
        if not account.enabled:
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
