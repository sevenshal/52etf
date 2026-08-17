from fastapi import APIRouter, HTTPException, Header, Depends, Query
from pydantic import BaseModel
from typing import List, Optional
import os
import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from ...core.utils import read_json_file
from sqlalchemy import func, inspect, text
from ...core.database import SessionLocal, WebAccount, WebAccountDailyUsage, engine

router = APIRouter(prefix="/api/profile")
ADMIN_ACCOUNT_ID = "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl"
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

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
    today_request_count: int = 0
    last_30_days_request_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AccountUsageItem(BaseModel):
    account_id: str
    usage_date: str
    request_count: int


def _shanghai_today() -> date:
    return datetime.now(SHANGHAI_TIMEZONE).date()


def record_account_request(account_id: Optional[str]) -> None:
    """Persist one valid account's API request in its daily aggregate.

    This deliberately uses one short SQLite transaction and is safe to call from
    middleware.  Aggregating in place keeps historical usage without retaining
    an unbounded row for every individual request.
    """
    if not account_id or len(account_id) > 128:
        return

    usage_date = _shanghai_today()
    recorded_at = datetime.now(SHANGHAI_TIMEZONE).replace(tzinfo=None)
    db = SessionLocal()
    try:
        # Count only accounts that exist and remain enabled.  The UPSERT keeps
        # the write atomic even when the same account makes concurrent requests.
        db.execute(
            text(
                """
                INSERT INTO web_account_daily_usage (
                    account_id, usage_date, request_count, created_at, updated_at
                )
                SELECT :account_id, :usage_date, 1, :recorded_at, :recorded_at
                WHERE EXISTS (
                    SELECT 1
                    FROM web_accounts
                    WHERE account_id = :account_id AND enabled = 1
                )
                ON CONFLICT(account_id, usage_date) DO UPDATE SET
                    request_count = web_account_daily_usage.request_count + 1,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "account_id": account_id,
                "usage_date": usage_date,
                "recorded_at": recorded_at,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        # Usage statistics must never make a normal API request fail.
        logger.exception("Failed to record daily API usage for an account")
    finally:
        db.close()

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


# ---------------------------------------------------------------------------
# web_accounts 有效账户集内存缓存（TTL 24h）
# ---------------------------------------------------------------------------
# valid_account / is_valid_account 命中每个带 X-Account-ID 的 API 请求，原实现
# 每次请求都查 SQLite（PRAGMA 建表检查 + 2 次 SELECT）。这里把「已启用账户 id
# 集合」在内存缓存 24 小时；账户增删改（create/update/delete）成功后调用
# invalidate_account_cache() 立即失效，保证页面上的删除/停用即时生效。
_ACCOUNT_CACHE_TTL_SECONDS = 24 * 60 * 60

_account_cache = {
    # 用 -inf 而非 0：monotonic 时钟刚开机时远小于 TTL，0 会被误判为“新鲜”而跳过首次加载
    "loaded_at": float("-inf"),
    "ids": frozenset(),
}
_account_cache_lock = threading.Lock()


def invalidate_account_cache() -> None:
    """web_accounts 发生写入后调用：让下一次 is_valid_account 重新加载。"""
    with _account_cache_lock:
        _account_cache["loaded_at"] = float("-inf")
        _account_cache["ids"] = frozenset()


def _load_enabled_account_ids() -> frozenset:
    """从数据库加载全部已启用账户 id（含首次 seed）。"""
    _seed_accounts_if_needed()
    db = SessionLocal()
    try:
        rows = (
            db.query(WebAccount.account_id)
            .filter(WebAccount.enabled.is_(True))
            .all()
        )
        return frozenset(row[0] for row in rows)
    finally:
        db.close()


def is_valid_account(account_id: str) -> bool:
    """检查账户是否存在且已启用（带 24h 内存缓存）。"""
    now = time.monotonic()
    with _account_cache_lock:
        if now - _account_cache["loaded_at"] >= _ACCOUNT_CACHE_TTL_SECONDS:
            try:
                ids = _load_enabled_account_ids()
                _account_cache["loaded_at"] = now
                _account_cache["ids"] = ids
            except Exception:
                # 加载失败（如 DB 暂时不可用）时保留旧缓存并重试，避免误伤存量请求
                logger.exception("Failed to load enabled account ids")
        return account_id in _account_cache["ids"]


async def valid_account(x_account_id: Optional[str] = Header(None)) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    if not is_valid_account(x_account_id):
        raise HTTPException(status_code=401, detail="Invalid account ID")
    return x_account_id


async def valid_admin_account(account_id: str = Depends(valid_account)) -> str:
    if account_id != ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="仅管理员可操作")
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


def _account_item(
    account: WebAccount,
    *,
    today_request_count: int = 0,
    last_30_days_request_count: int = 0,
) -> AccountItem:
    return AccountItem(
        account_id=account.account_id,
        note=account.note or "",
        enabled=account.enabled,
        is_admin=account.account_id == ADMIN_ACCOUNT_ID,
        today_request_count=today_request_count,
        last_30_days_request_count=last_30_days_request_count,
        created_at=account.created_at.isoformat() if account.created_at else None,
        updated_at=account.updated_at.isoformat() if account.updated_at else None,
    )


@router.get("/accounts", response_model=List[AccountItem])
def list_accounts(_: str = Depends(valid_admin_account)):
    _seed_accounts_if_needed()
    db = SessionLocal()
    try:
        today = _shanghai_today()
        period_start = today - timedelta(days=29)
        today_counts = dict(
            db.query(
                WebAccountDailyUsage.account_id,
                WebAccountDailyUsage.request_count,
            )
            .filter(WebAccountDailyUsage.usage_date == today)
            .all()
        )
        last_30_days_counts = dict(
            db.query(
                WebAccountDailyUsage.account_id,
                func.sum(WebAccountDailyUsage.request_count),
            )
            .filter(WebAccountDailyUsage.usage_date >= period_start)
            .filter(WebAccountDailyUsage.usage_date <= today)
            .group_by(WebAccountDailyUsage.account_id)
            .all()
        )
        return [
            _account_item(
                item,
                today_request_count=int(today_counts.get(item.account_id, 0)),
                last_30_days_request_count=int(last_30_days_counts.get(item.account_id, 0)),
            )
            for item in db.query(WebAccount).order_by(WebAccount.created_at).all()
        ]
    finally:
        db.close()


@router.get("/account-usage", response_model=List[AccountUsageItem])
def list_account_usage(
    account_id: Optional[str] = Query(None, max_length=128),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    _: str = Depends(valid_admin_account),
):
    """Return persisted per-day request counts for the selected date range."""
    today = _shanghai_today()
    start_date = start_date or today - timedelta(days=29)
    end_date = end_date or today
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    db = SessionLocal()
    try:
        query = (
            db.query(WebAccountDailyUsage)
            .filter(WebAccountDailyUsage.usage_date >= start_date)
            .filter(WebAccountDailyUsage.usage_date <= end_date)
        )
        if account_id:
            query = query.filter(WebAccountDailyUsage.account_id == account_id)
        return [
            AccountUsageItem(
                account_id=item.account_id,
                usage_date=item.usage_date.isoformat(),
                request_count=item.request_count,
            )
            for item in query.order_by(
                WebAccountDailyUsage.usage_date.desc(),
                WebAccountDailyUsage.account_id,
            ).all()
        ]
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
        invalidate_account_cache()
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
        invalidate_account_cache()
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
        invalidate_account_cache()
    finally:
        db.close()
