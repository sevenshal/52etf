from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .account import valid_admin_account
from ...core.database import SystemServiceCredential, get_db
from ...core.services.evc import EVCService

router = APIRouter(prefix="/api/evc-accounts", tags=["evc-accounts"])


def serialize_utc_datetime(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


class EVCAccountUpsertRequest(BaseModel):
    username: str
    password: Optional[str] = None


class EVCAccountResponse(BaseModel):
    account_id: str
    username: Optional[str] = None
    password_configured: bool = False
    cookie_configured: bool = False
    cookie_expires_at: Optional[str] = None
    updated_at: Optional[str] = None


@router.get("", response_model=EVCAccountResponse)
async def get_evc_account(
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_admin_account),
):
    config = db.get(SystemServiceCredential, "evc")
    return EVCAccountResponse(
        account_id=account_id,
        username=config.username if config else None,
        password_configured=bool(config and config.password),
        cookie_configured=bool(config and config.cookie),
        cookie_expires_at=serialize_utc_datetime(config.cookie_expires_at if config else None),
        updated_at=config.updated_at.isoformat() if config and config.updated_at else None,
    )


@router.post("", response_model=EVCAccountResponse)
async def upsert_evc_account(
    payload: EVCAccountUpsertRequest,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_admin_account),
):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")

    config = db.get(SystemServiceCredential, "evc")
    if not config:
        config = SystemServiceCredential(service="evc")
        db.add(config)

    config.username = username
    if payload.password:
        config.password = payload.password
    if not config.password:
        raise HTTPException(status_code=400, detail="请提供密码")
    config.updated_at = datetime.now()

    db.commit()
    db.refresh(config)

    return EVCAccountResponse(
        account_id=account_id,
        username=config.username,
        password_configured=bool(config.password),
        cookie_configured=bool(config.cookie),
        cookie_expires_at=serialize_utc_datetime(config.cookie_expires_at),
        updated_at=config.updated_at.isoformat() if config.updated_at else None,
    )


@router.post("/login")
async def login_evc_account(
    account_id: str = Depends(valid_admin_account),
):
    try:
        result = EVCService().login()
        return {
            "message": "登录成功",
            "cookie_expires_at": result.get("cookie_expires_at"),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
