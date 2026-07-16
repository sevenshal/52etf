from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import List, Optional
import os
from ...core.utils import read_json_file
from sqlalchemy import inspect, text
from ...core.database import SessionLocal, WebAccount, engine

router = APIRouter(prefix="/api/profile")
ADMIN_ACCOUNT_ID = "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl"

class AccountValidation(BaseModel):
    valid: bool
    message: str = ""
    is_admin: bool = False


class AccountCreate(BaseModel):
    account_id: str
    note: str = ""
    enabled: bool = True


class AccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    note: Optional[str] = None


class AccountItem(BaseModel):
    account_id: str
    note: str
    enabled: bool
    is_admin: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

def get_accounts_file_path():
    """获取账户配置文件路径"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, '..', 'data', 'accounts.json')

def _seed_accounts_if_needed() -> None:
    """首次升级时将旧 JSON 账户一次性导入数据库。"""
    _ensure_web_account_schema()
    db = SessionLocal()
    try:
        if db.query(WebAccount).first() is not None:
            return
        accounts = read_json_file(get_accounts_file_path()).get("valid_accounts", [])
        for account_id in dict.fromkeys([*accounts, ADMIN_ACCOUNT_ID]):
            db.add(WebAccount(account_id=account_id, enabled=True))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_web_account_schema() -> None:
    """为已存在的账户表补充后续新增字段。"""
    columns = {column["name"] for column in inspect(engine).get_columns(WebAccount.__tablename__)}
    if "note" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE web_accounts ADD COLUMN note VARCHAR(500) NOT NULL DEFAULT ''"))


def is_valid_account(account_id: str) -> bool:
    """检查账户是否存在且已启用。"""
    _seed_accounts_if_needed()
    db = SessionLocal()
    try:
        return db.query(WebAccount).filter(
            WebAccount.account_id == account_id,
            WebAccount.enabled.is_(True),
        ).first() is not None
    finally:
        db.close()

async def valid_account(x_account_id: Optional[str] = Header(None)) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    if not is_valid_account(x_account_id):
        raise HTTPException(status_code=401, detail="Invalid account ID")
    return x_account_id


async def valid_admin_account(account_id: str = Depends(valid_account)) -> str:
    if account_id != ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="仅管理员可操作账户")
    return account_id

@router.get("/validate-account", response_model=AccountValidation)
async def validate_account(account_id: str):
    """验证账户ID是否有效
    
    验证查询参数中的账户是否在数据库中存在且已启用。
    
    Args:
        account_id: 查询参数中的账户ID
    
    Returns:
        AccountValidation: 包含验证结果和消息的对象
    """
    if not account_id:
        return AccountValidation(valid=False, message="缺少账户ID")
    
    if is_valid_account(account_id):
        return AccountValidation(valid=True, is_admin=account_id == ADMIN_ACCOUNT_ID)
    else:
        return AccountValidation(valid=False, message="无效或已停用的账户ID")


def _account_item(account: WebAccount) -> AccountItem:
    return AccountItem(
        account_id=account.account_id,
        note=account.note or "",
        enabled=account.enabled,
        is_admin=account.account_id == ADMIN_ACCOUNT_ID,
        created_at=account.created_at.isoformat() if account.created_at else None,
        updated_at=account.updated_at.isoformat() if account.updated_at else None,
    )


@router.get("/accounts", response_model=List[AccountItem])
def list_accounts(_: str = Depends(valid_admin_account)):
    _seed_accounts_if_needed()
    db = SessionLocal()
    try:
        return [_account_item(item) for item in db.query(WebAccount).order_by(WebAccount.created_at).all()]
    finally:
        db.close()


@router.post("/accounts", response_model=AccountItem, status_code=201)
def create_account(payload: AccountCreate, _: str = Depends(valid_admin_account)):
    account_id = payload.account_id.strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="账户ID不能为空")
    if len(account_id) > 128:
        raise HTTPException(status_code=400, detail="账户ID不能超过128个字符")
    note = payload.note.strip()
    if len(note) > 500:
        raise HTTPException(status_code=400, detail="备注不能超过500个字符")
    db = SessionLocal()
    try:
        if db.get(WebAccount, account_id):
            raise HTTPException(status_code=409, detail="账户ID已存在")
        account = WebAccount(account_id=account_id, note=note, enabled=payload.enabled)
        db.add(account)
        db.commit()
        db.refresh(account)
        return _account_item(account)
    finally:
        db.close()


@router.patch("/accounts/{account_id}", response_model=AccountItem)
def update_account(account_id: str, payload: AccountUpdate, _: str = Depends(valid_admin_account)):
    if account_id == ADMIN_ACCOUNT_ID and payload.enabled is False:
        raise HTTPException(status_code=400, detail="不能停用管理员账户")
    if payload.note is not None and len(payload.note.strip()) > 500:
        raise HTTPException(status_code=400, detail="备注不能超过500个字符")
    db = SessionLocal()
    try:
        account = db.get(WebAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账户不存在")
        if payload.enabled is not None:
            account.enabled = payload.enabled
        if payload.note is not None:
            account.note = payload.note.strip()
        db.commit()
        db.refresh(account)
        return _account_item(account)
    finally:
        db.close()


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: str, _: str = Depends(valid_admin_account)):
    if account_id == ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=400, detail="不能删除管理员账户")
    db = SessionLocal()
    try:
        account = db.get(WebAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="账户不存在")
        db.delete(account)
        db.commit()
    finally:
        db.close()
