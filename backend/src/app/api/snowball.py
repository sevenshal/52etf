from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
import logging
import fnmatch
import asyncio
import re
import hashlib
import json
import os
from sqlalchemy.orm import Session
from ...core.database import (
    get_db,
    get_db_ctx,
    Session,
    SnowballCopyConfig,
    SnowballCopyLog,
    SnowballAccountConfig,
)
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    get_external_trading_db,
    get_external_trading_db_ctx,
)
from ...core.services.external_trading_executor import (
    is_a_share_trading_window,
    next_a_share_trading_time,
    trigger_external_trading_executor,
)
from ...core.services.external_trading_ledger import (
    STRATEGY_SNOWBALL,
    normalize_symbol as normalize_trading_symbol,
    safe_float,
    safe_int,
    sync_target_positions,
)
from ...core.services.external_trading_valuation import (
    ExternalTradingValuationError,
    calculate_sub_account_net_asset,
)
from .account import valid_account

router = APIRouter(prefix="/api/snowball")
logger = logging.getLogger(__name__)

# --- Constants ---
XUEQIU_HEADERS = {
    "Host": "api.xueqiu.com",
    "Cookie": "xq_a_token=91eabb39aba7af77c2b00d8f8ac5700ade3cf02b;",
    "accept": "application/json",
    "accept-language": "zh-Hans-CN;q=1, en-CN;q=0.9",
    "x-device-os": "iOS 26.1",
    "x-device-model-name": "iPhone 16 Pro Max_iPhone17,2",
    "user-agent": "Xueqiu iPhone 14.81.1",
    "priority": "u=3, i"
}

XUEQIU_STOCK_HEADERS = XUEQIU_HEADERS.copy()
XUEQIU_STOCK_HEADERS["Host"] = "stock.xueqiu.com"
XUEQIU_COOKIE_VALIDATE_URL = "https://xueqiu.com/user/setting/select.json"
XUEQIU_COOKIE_VALIDATE_PARAMS = {"types": "like_receive"}

# --- Globals for Token Refresh ---
_last_token_refresh_time = None
_is_refreshing_token = False

async def _refresh_xueqiu_guest_token_task(account_id: str = None, cookie: str = None):
    global _last_token_refresh_time, _is_refreshing_token, XUEQIU_HEADERS, XUEQIU_STOCK_HEADERS
    
    if _is_refreshing_token:
        return
        
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if _last_token_refresh_time and (now - _last_token_refresh_time).total_seconds() < 43200:
        return

    _is_refreshing_token = True
    try:
        logger.info("Starting background refresh of Xueqiu guest token...")
        async with httpx.AsyncClient() as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            if cookie:
                if "xq_a_token" in cookie:
                    headers["Cookie"] = cookie
                else:
                    headers["Cookie"] = f"xq_a_token={cookie};"
                    
            response = await client.get("https://xueqiu.com/about/contact-us", headers=headers, timeout=10.0)
            _last_token_refresh_time = datetime.now() # Record the attempt time to enforce the 1-hour limit
            
            for cookie in response.cookies.jar:
                if cookie.name == "xq_a_token":
                    new_token = cookie.value
                    new_cookie_str = f"xq_a_token={new_token};"
                    XUEQIU_HEADERS["Cookie"] = new_cookie_str
                    XUEQIU_STOCK_HEADERS["Cookie"] = new_cookie_str
                    logger.info(f"Successfully refreshed Xueqiu guest token: {new_token[:10]}...")
                    
                    if account_id:
                        with get_db_ctx() as db:
                            config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
                            if config and config.xueqiu_cookie:
                                old_c = config.xueqiu_cookie
                                if "xq_a_token=" in old_c:
                                    config.xueqiu_cookie = re.sub(r'xq_a_token=[^;]+', f'xq_a_token={new_token}', old_c)
                                else:
                                    config.xueqiu_cookie = f"xq_a_token={new_token}; {old_c}"
                    break
    except Exception as e:
        logger.error(f"Failed to refresh Xueqiu guest token: {e}")
    finally:
        _is_refreshing_token = False


async def _validate_xueqiu_cookie(cookie: str) -> Dict[str, Any]:
    match = re.search(r"xq_a_token=([^;\s]+)", cookie or "")
    if not match:
        raise HTTPException(status_code=400, detail="雪球 cookie 校验失败: 缺少 xq_a_token")
    token = match.group(1)

    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "priority": "u=1, i",
        "referer": "https://xueqiu.com/",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        response = await client.get(
            XUEQIU_COOKIE_VALIDATE_URL,
            params=XUEQIU_COOKIE_VALIDATE_PARAMS,
            headers=headers,
            cookies={"xq_a_token": token},
        )

    if response.status_code != 200:
        snippet = response.text[:200].replace("\n", " ").replace("\r", " ")
        raise HTTPException(
            status_code=400,
            detail=f"雪球 cookie 校验失败: HTTP {response.status_code} ({response.headers.get('content-type', 'unknown')}) {snippet}"
        )

    try:
        payload = response.json()
    except Exception:
        snippet = (response.text or "")[:300].replace("\n", " ").replace("\r", " ")
        raise HTTPException(
            status_code=400,
            detail=f"雪球 cookie 校验失败: 响应不是 JSON ({response.headers.get('content-type', 'unknown')}) {snippet}"
        )

    if isinstance(payload, dict):
        error_code = payload.get("error_code")
        if error_code:
            description = payload.get("error_description") or "雪球返回错误"
            raise HTTPException(status_code=400, detail=f"雪球 cookie 校验失败: {description} ({error_code})")
        if payload.get("uid"):
            return payload
        raise HTTPException(status_code=400, detail="雪球 cookie 校验失败: 未获取到有效登录信息")

    if isinstance(payload, list) and payload:
        first_item = payload[0]
        if isinstance(first_item, dict) and first_item.get("uid") is not None:
            return {"items": payload}

    raise HTTPException(status_code=400, detail="雪球 cookie 校验失败: 登录态无效")

# --- Models ---

class SnowballAccountConfigModel(BaseModel):
    xueqiu_cookie: Optional[str] = None
    updated_at: Optional[datetime] = None

class SnowballCookieSyncRequest(BaseModel):
    xueqiu_cookie: str

class SnowballConfigCreate(BaseModel):
    cli_id: str
    combination_id: str
    combination_name: Optional[str] = None
    enabled: bool = True
    tracking_error_pct: float = 1.0
    blacklisted_symbols: List[str] = []
    live_trade_enabled: bool = False
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None

class SnowballConfigUpdate(BaseModel):
    cli_id: Optional[str] = None
    combination_id: Optional[str] = None
    combination_name: Optional[str] = None
    enabled: Optional[bool] = None
    tracking_error_pct: Optional[float] = None
    blacklisted_symbols: Optional[List[str]] = None
    live_trade_enabled: Optional[bool] = None
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None

class SnowballConfigResponse(SnowballConfigCreate):
    id: int
    updated_at: datetime
    snapshot_value: Optional[float] = 0.0
    external_trading_account_name: Optional[str] = None
    live_sub_account_name: Optional[str] = None
    live_sub_account_enabled: Optional[bool] = None
    last_external_sync_at: Optional[datetime] = None
    last_external_sync_status: Optional[str] = None
    last_external_sync_message: Optional[str] = None
    
    class Config:
        from_attributes = True


class SnowballLogResponse(BaseModel):
    id: int
    cli_id: Optional[str]
    timestamp: datetime
    combination_id: Optional[str]
    action: Optional[str]
    status: Optional[str]
    message: Optional[str]
    
    class Config:
        from_attributes = True

class SnowballLogStatusUpdate(BaseModel):
    id: int
    status: str
    message: Optional[str] = None

class PaginatedSnowballLogs(BaseModel):
    total: int
    items: List[SnowballLogResponse]


class SnowballSnapshotHolding(BaseModel):
    symbol: str
    xueqiu_symbol: Optional[str] = None
    name: str = ""
    price: float = 0.0
    xueqiu_weight_pct: float = 0.0
    target_weight_pct: float = 0.0
    target_quantity: int = 0
    target_value: float = 0.0
    ledger_quantity: int = 0
    ledger_available_quantity: int = 0
    ledger_market_value: float = 0.0
    ledger_weight_pct: float = 0.0
    quantity_diff: int = 0
    value_diff: float = 0.0
    weight_diff_pct: float = 0.0
    blacklisted: bool = False
    diff_type: str = "MATCHED"


class SnowballSnapshotResponse(BaseModel):
    config_id: int
    updated_at: datetime
    source: str = "xueqiu_live_with_ledger_diff"
    sub_account_id: Optional[int] = None
    sub_account_name: Optional[str] = None
    target_market_value: float = 0.0
    target_cash: float = 0.0
    ledger_market_value: float = 0.0
    ledger_cash: float = 0.0
    ledger_net_asset: float = 0.0
    ledger_stock_ratio: float = 0.0
    ledger_cash_ratio: float = 0.0
    cash_diff: float = 0.0
    diff_count: int = 0
    holdings: List[SnowballSnapshotHolding] = []


# --- Helpers ---

def normalize_symbol(symbol: str) -> str:
    """Normalize symbol to SH.xxxxxx format from SHxxxxxx"""
    if not symbol: 
        return symbol
    if "." in symbol:
        return symbol
    if len(symbol) > 2 and (symbol.startswith("SH") or symbol.startswith("SZ") or symbol.startswith("BJ")):
        return f"{symbol[:2]}.{symbol[2:]}"
    return symbol

async def fetch_xueqiu_holdings(symbol: str, cookie: str = None) -> List[Dict]:
    """Fetch holdings from Xueqiu API"""
    url = f"https://api.xueqiu.com/cube/center/cube/holdSymbols.json?symbol={symbol}"
    
    headers = XUEQIU_HEADERS.copy()
    if cookie:
        # If user provided a raw cookie string (key=value), use it. 
        # If they provided just the token, we might need to format it, but let's assume raw cookie for simplicity or just replace xq_a_token.
        # But safest is to treat it as the full Cookie header value if it contains '='
        if "xq_a_token" in cookie:
             headers["Cookie"] = cookie
        else:
             # Assume it's just the token value
             headers["Cookie"] = f"xq_a_token={cookie};"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            if data.get("result_code") == 0 and data.get("success"):
                holdings = data.get("data", [])
                for h in holdings:
                    h['symbol'] = normalize_symbol(h.get('symbol'))
                return holdings
            else:
                logger.error(f"Xueqiu API Error (Holdings): {data}")
                raise Exception(f"Xueqiu API Error: {data.get('message', 'Unknown error')}")
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu holdings: {e}")
            raise e

async def fetch_xueqiu_cube_info(symbol: str, cookie: str = None) -> Optional[Dict]:
    """Fetch cube info including name from Xueqiu"""
    ts = int(datetime.now().timestamp() * 1000)
    url = f"https://xueqiu.com/cubes/nav_daily/all.json?cube_symbol={symbol}&since={ts}&until={ts}"
    
    headers = XUEQIU_HEADERS.copy()
    headers["Referer"] = f"https://xueqiu.com/P/{symbol}"
    
    if cookie:
        if "xq_a_token" in cookie:
             headers["Cookie"] = cookie
        else:
             headers["Cookie"] = f"xq_a_token={cookie};"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            if isinstance(data, list) and len(data) > 0:
                for item in data:
                    if item.get("symbol") == symbol:
                        return item
                return data[0]
            elif isinstance(data, dict) and "name" in data:
                return data
            else:
                logger.error(f"Xueqiu API Error (Info): {data}")
                return None
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu cube info: {e}")
            return None

async def fetch_xueqiu_quotes(symbols: List[str], cookie: str = None) -> Dict[str, float]:
    """Fetch real-time quotes using the lightweight API (Price Only)"""
    if not symbols:
        return {}
    
    # URL encode comma is %2C, but usually httpx handles params or we can just join
    # Map dotted to raw for API call (SH.600 -> SH600)
    raw_to_dotted = {s.replace(".", ""): s for s in symbols}
    raw_symbol_str = ",".join(raw_to_dotted.keys())
    
    url = f"https://stock.xueqiu.com/v5/stock/realtime/quotec.json?symbol={raw_symbol_str}"
    
    headers = XUEQIU_STOCK_HEADERS.copy()
    if cookie:
        if "xq_a_token" in cookie:
             headers["Cookie"] = cookie
        else:
             headers["Cookie"] = f"xq_a_token={cookie};"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            # Format: {"data":[{"symbol":"SZ000858","current":107.46 ...}]}
            result = {}
            if "data" in data:
                for item in data["data"]:
                    raw_sym = item["symbol"]
                    # Map back to dotted symbol if possible
                    dotted_sym = raw_to_dotted.get(raw_sym, raw_to_dotted.get(raw_sym.upper(), raw_sym))
                    result[dotted_sym] = item["current"]
            return result
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu quotes: {e}")
            return {}

async def fetch_xueqiu_batch_quotes(symbols: List[str], cookie: str = None) -> Dict[str, Dict]:
    """Fetch detailed quotes (Price + Name) using the batch API"""
    if not symbols:
        return {}
    
    raw_to_dotted = {s.replace(".", ""): s for s in symbols}
    raw_symbol_str = ",".join(raw_to_dotted.keys())
    
    url = f"https://stock.xueqiu.com/v5/stock/batch/quote.json?symbol={raw_symbol_str}"
    
    headers = XUEQIU_STOCK_HEADERS.copy()
    if cookie:
        if "xq_a_token" in cookie:
             headers["Cookie"] = cookie
        else:
             headers["Cookie"] = f"xq_a_token={cookie};"
             
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            # Format: {"data": {"items": [{"quote": {"symbol": "SH520830", "name": "沙特ETF", "current": 0.937 ...}}]}}
            result = {}
            if "data" in data and "items" in data["data"]:
                for item in data["data"]["items"]:
                    quote = item.get("quote", {})
                    raw_sym = quote.get("symbol")
                    if raw_sym:
                        dotted_sym = raw_to_dotted.get(raw_sym, raw_to_dotted.get(raw_sym.upper(), raw_sym))
                        result[dotted_sym] = {
                            "price": quote.get("current", 0.0),
                            "name": quote.get("name", "")
                        }
            return result
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu batch quotes: {e}")
            return {}


def _to_xueqiu_symbol(symbol: Optional[str]) -> Optional[str]:
    """Convert common A-share formats to Xueqiu dotted format, e.g. 510500.SH -> SH.510500."""
    if not symbol:
        return None
    value = str(symbol).strip().upper()
    if not value:
        return None
    value = value.replace("_", ".")
    if "." in value:
        left, right = value.split(".", 1)
        if left in {"SH", "SS", "SZ", "BJ"}:
            market = "SH" if left in {"SH", "SS"} else left
            return f"{market}.{right}"
        if right in {"SH", "SS", "SZ", "BJ"}:
            market = "SH" if right in {"SH", "SS"} else right
            return f"{market}.{left}"
        return value
    if len(value) > 2 and value[:2] in {"SH", "SZ", "BJ", "SS"}:
        market = "SH" if value[:2] in {"SH", "SS"} else value[:2]
        return f"{market}.{value[2:]}"
    if len(value) == 6:
        market = "SH" if value[0] in {"5", "6", "9"} else "SZ"
        return f"{market}.{value}"
    return value


def _to_trade_symbol(symbol: Optional[str]) -> Optional[str]:
    return normalize_trading_symbol(_to_xueqiu_symbol(symbol))


def _snowball_config_response(
    db: Session,
    config: SnowballCopyConfig,
    trading_db: Session,
) -> SnowballConfigResponse:
    resp = SnowballConfigResponse.from_orm(config)
    resp.snapshot_value = 0.0

    if getattr(config, "external_trading_account_id", None):
        account = trading_db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.id == config.external_trading_account_id,
            ExternalTradingAccount.account_id == config.account_id,
        ).first()
        if account:
            resp.external_trading_account_name = f"{account.name} ({account.identifier})"
    if getattr(config, "live_sub_account_id", None):
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
        ).first()
        if sub_account:
            resp.live_sub_account_name = sub_account.name
            resp.live_sub_account_enabled = sub_account.enabled
            position_rows = trading_db.query(ExternalTradingLedgerPosition).filter(
                ExternalTradingLedgerPosition.sub_account_id == sub_account.id
            ).all()
            ledger_market_value = sum(
                safe_float(row.market_value)
                for row in position_rows
                if safe_int(row.quantity) > 0
            )
            resp.snapshot_value = round(safe_float(sub_account.cash_available) + ledger_market_value, 2)
    return resp


async def _snowball_config_response_with_net_asset(
    db: Session,
    config: SnowballCopyConfig,
    trading_db: Session,
) -> SnowballConfigResponse:
    resp = _snowball_config_response(db, config, trading_db)
    if not getattr(config, "live_sub_account_id", None):
        return resp
    sub_account = trading_db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == config.live_sub_account_id,
        ExternalTradingSubAccount.account_id == config.account_id,
    ).first()
    if not sub_account:
        return resp
    try:
        valuation = await calculate_sub_account_net_asset(trading_db, sub_account)
        resp.snapshot_value = safe_float(valuation.get("net_asset"))
    except ExternalTradingValuationError as exc:
        logger.warning("Failed to calculate Snowball sub-account net asset: config=%s error=%s", config.id, exc)
    return resp


def _validate_snowball_external_account_selection(db: Session, account_id: str, external_account_id: Optional[int]):
    if not external_account_id:
        return None
    account = db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == external_account_id,
        ExternalTradingAccount.account_id == account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="所选外部交易账户不存在")
    if not account.enabled:
        raise HTTPException(status_code=400, detail="所选外部交易账户未启用")
    return account


def _get_valid_snowball_live_sub_account_selection(
    db: Session,
    account_id: str,
    external_account_id: Optional[int],
    sub_account_id: Optional[int],
    *,
    config_id: Optional[int] = None,
    require_enabled: bool = False,
) -> Optional[ExternalTradingSubAccount]:
    if not sub_account_id:
        return None
    if not external_account_id:
        raise HTTPException(status_code=400, detail="选择实盘虚拟子账户前请先选择外部交易账户")
    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户不存在")
    if require_enabled and not sub_account.enabled:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户未启用")
    is_bound = bool(sub_account.strategy_type or sub_account.strategy_config_id)
    is_current_binding = (
        sub_account.strategy_type == STRATEGY_SNOWBALL
        and config_id
        and sub_account.strategy_config_id == config_id
    )
    if is_bound and not is_current_binding:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户已被其他策略绑定")
    return sub_account


def _deactivate_snowball_target_positions(
    db: Session,
    *,
    sub_account_id: Optional[int],
    config_id: Optional[int],
) -> None:
    if not sub_account_id:
        return
    query = db.query(ExternalTradingTargetPosition).filter(
        ExternalTradingTargetPosition.sub_account_id == sub_account_id,
        ExternalTradingTargetPosition.status == "ACTIVE",
    )
    if config_id:
        query = query.filter(
            ExternalTradingTargetPosition.strategy_type == STRATEGY_SNOWBALL,
            ExternalTradingTargetPosition.strategy_config_id == config_id,
        )
    now = datetime.now()
    for row in query.all():
        row.status = "PREVIEW"
        row.updated_at = now


def _sync_snowball_live_sub_account_binding(
    db: Session,
    config: SnowballCopyConfig,
    *,
    previous_sub_account_id: Optional[int],
) -> None:
    if getattr(config, "live_trade_enabled", False):
        if not config.external_trading_account_id:
            raise HTTPException(status_code=400, detail="开启通用执行器实盘跟单时必须选择外部交易账户")
        if not config.live_sub_account_id:
            raise HTTPException(status_code=400, detail="开启通用执行器实盘跟单时必须选择虚拟子账户")

    _validate_snowball_external_account_selection(db, config.account_id, config.external_trading_account_id)
    selected_sub_account = _get_valid_snowball_live_sub_account_selection(
        db,
        config.account_id,
        config.external_trading_account_id,
        config.live_sub_account_id,
        config_id=config.id,
        require_enabled=bool(config.live_sub_account_id),
    )

    if previous_sub_account_id and previous_sub_account_id != config.live_sub_account_id:
        _deactivate_snowball_target_positions(
            db,
            sub_account_id=previous_sub_account_id,
            config_id=config.id,
        )
        previous = db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == previous_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_SNOWBALL,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if previous:
            previous.strategy_type = None
            previous.strategy_config_id = None
            previous.updated_at = datetime.now()

    if not getattr(config, "enabled", True) or not getattr(config, "live_trade_enabled", False):
        _deactivate_snowball_target_positions(
            db,
            sub_account_id=config.live_sub_account_id or previous_sub_account_id,
            config_id=config.id,
        )

    if selected_sub_account:
        selected_sub_account.strategy_type = STRATEGY_SNOWBALL
        selected_sub_account.strategy_config_id = config.id
        selected_sub_account.updated_at = datetime.now()


def _load_snowball_external_sync_items(
    *,
    account_id: Optional[str] = None,
    config_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    with get_db_ctx() as db, get_external_trading_db_ctx() as trading_db:
        query = db.query(SnowballCopyConfig).filter(
            SnowballCopyConfig.enabled == True,  # noqa: E712
            SnowballCopyConfig.live_trade_enabled == True,  # noqa: E712
            SnowballCopyConfig.external_trading_account_id.isnot(None),
            SnowballCopyConfig.live_sub_account_id.isnot(None),
        )
        if account_id:
            query = query.filter(SnowballCopyConfig.account_id == account_id)
        if config_ids:
            query = query.filter(SnowballCopyConfig.id.in_(config_ids))

        configs = query.order_by(SnowballCopyConfig.account_id.asc(), SnowballCopyConfig.id.asc()).all()
        items = []
        for config in configs:
            external_account = trading_db.query(ExternalTradingAccount).filter(
                ExternalTradingAccount.id == config.external_trading_account_id,
                ExternalTradingAccount.account_id == config.account_id,
                ExternalTradingAccount.enabled == True,  # noqa: E712
            ).first()
            sub_account = trading_db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == config.live_sub_account_id,
                ExternalTradingSubAccount.account_id == config.account_id,
                ExternalTradingSubAccount.external_trading_account_id == config.external_trading_account_id,
                ExternalTradingSubAccount.enabled == True,  # noqa: E712
            ).first()
            if not external_account or not sub_account:
                continue
            if sub_account.strategy_type != STRATEGY_SNOWBALL or sub_account.strategy_config_id != config.id:
                continue

            target_rows = trading_db.query(ExternalTradingTargetPosition).filter(
                ExternalTradingTargetPosition.sub_account_id == sub_account.id,
                ExternalTradingTargetPosition.status == "ACTIVE",
            ).all()
            current_targets = {}
            for row in target_rows:
                xq_symbol = _to_xueqiu_symbol(row.symbol)
                if xq_symbol:
                    current_targets[xq_symbol] = {
                        "target_quantity": safe_int(row.target_quantity),
                        "target_value": safe_float(row.target_value),
                        "signal_version": row.signal_version,
                    }

            acc_config = db.query(SnowballAccountConfig).filter_by(account_id=config.account_id).first()
            items.append({
                "id": config.id,
                "account_id": config.account_id,
                "cli_id": config.cli_id,
                "combination_id": config.combination_id,
                "combination_name": config.combination_name,
                "tracking_error_pct": safe_float(config.tracking_error_pct, 1.0),
                "blacklisted_symbols": config.blacklisted_symbols or [],
                "external_trading_account_id": external_account.id,
                "live_sub_account_id": sub_account.id,
                "current_targets": current_targets,
                "cookie": acc_config.xueqiu_cookie if acc_config else None,
            })
        return items


def _build_snowball_target_signal_version(item: Dict[str, Any], target_rows: List[Dict[str, Any]]) -> str:
    payload = {
        "strategy": STRATEGY_SNOWBALL,
        "config_id": item.get("id"),
        "combination_id": item.get("combination_id"),
        "targets": [
            {
                "symbol": row.get("symbol"),
                "target_quantity": safe_int(row.get("target_quantity")),
            }
            for row in sorted(target_rows, key=lambda data: str(data.get("symbol") or ""))
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"snowball:{item.get('id')}:{digest[:16]}"[:64]


def _mark_snowball_external_sync_failure(config_id: int, message: str) -> None:
    with get_db_ctx() as db:
        config = db.query(SnowballCopyConfig).filter(SnowballCopyConfig.id == config_id).first()
        if config:
            config.last_external_sync_at = datetime.now()
            config.last_external_sync_status = "FAILED"
            config.last_external_sync_message = message[:500]
            db.add(SnowballCopyLog(
                cli_id=config.cli_id,
                combination_id=config.combination_id,
                action="TARGET_SYNC",
                status="FAILED",
                message=message[:1000],
                account_id=config.account_id,
            ))


async def _sync_one_snowball_external_target(item: Dict[str, Any], *, trigger_source: str) -> Dict[str, Any]:
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if not _last_token_refresh_time or (now - _last_token_refresh_time).total_seconds() >= 3600:
        asyncio.create_task(_refresh_xueqiu_guest_token_task(item.get("account_id"), item.get("cookie")))

    holdings = await fetch_xueqiu_holdings(item["combination_id"], item.get("cookie"))
    current_targets = item.get("current_targets") or {}
    target_weights = {}
    all_symbols = set(current_targets.keys())
    blacklist = item.get("blacklisted_symbols") or []
    for holding in holdings:
        symbol = _to_xueqiu_symbol(holding.get("symbol"))
        if not symbol:
            continue
        all_symbols.add(symbol)
        if any(fnmatch.fnmatch(symbol, pattern) or fnmatch.fnmatch(_to_trade_symbol(symbol) or "", pattern) for pattern in blacklist):
            continue
        target_weights[symbol] = safe_float(holding.get("weight"))

    with get_external_trading_db_ctx() as trading_db:
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == item["live_sub_account_id"],
            ExternalTradingSubAccount.account_id == item["account_id"],
            ExternalTradingSubAccount.strategy_type == STRATEGY_SNOWBALL,
            ExternalTradingSubAccount.strategy_config_id == item["id"],
        ).first()
        if not sub_account:
            raise ValueError("雪球配置绑定的虚拟子账户不存在")
        try:
            valuation = await calculate_sub_account_net_asset(trading_db, sub_account)
        except ExternalTradingValuationError as exc:
            raise ValueError(str(exc)) from exc
    all_symbols.update(_to_xueqiu_symbol(symbol) for symbol in (valuation.get("position_symbols") or []) if _to_xueqiu_symbol(symbol))
    quotes = await fetch_xueqiu_quotes(sorted(all_symbols), item.get("cookie"))
    net_asset = safe_float(valuation.get("net_asset"))
    base_value = net_asset
    if base_value <= 0:
        raise ValueError("雪球通用执行器目标净资产为空，请检查虚拟子账户现金和持仓市值")

    threshold_pct = max(safe_float(item.get("tracking_error_pct"), 1.0), 0.0)
    target_rows = []
    skipped = []
    changed = False
    for xq_symbol in sorted(all_symbols):
        trade_symbol = _to_trade_symbol(xq_symbol)
        if not trade_symbol:
            continue
        old_target = current_targets.get(xq_symbol) or {}
        old_quantity = safe_int(old_target.get("target_quantity"))
        weight = safe_float(target_weights.get(xq_symbol))
        price = safe_float(quotes.get(xq_symbol))
        target_value = base_value * (weight / 100.0)
        final_quantity = old_quantity

        if weight <= 0:
            final_quantity = 0
        elif price <= 0:
            skipped.append({"symbol": trade_symbol, "message": "缺少雪球行情价格，保留原目标仓位"})
        else:
            current_value = old_quantity * price
            diff_pct = (abs(target_value - current_value) / base_value) * 100 if base_value > 0 else 100
            if diff_pct >= threshold_pct:
                final_quantity = int((target_value / price) / 100) * 100

        if old_quantity != final_quantity:
            changed = True

        row_target_value = target_value if weight > 0 else 0.0
        if price > 0 and final_quantity > 0:
            row_target_value = final_quantity * price
        target_rows.append({
            "symbol": trade_symbol,
            "target_quantity": final_quantity,
            "target_weight_pct": weight,
            "target_value": round(row_target_value, 2),
        })

    signal_version = _build_snowball_target_signal_version(item, target_rows)
    old_versions = {
        data.get("signal_version")
        for data in current_targets.values()
        if data.get("signal_version")
    }
    changed = changed or (bool(target_rows) and signal_version not in old_versions)

    with get_db_ctx() as db, get_external_trading_db_ctx() as trading_db:
        config = db.query(SnowballCopyConfig).filter(SnowballCopyConfig.id == item["id"]).first()
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == item["live_sub_account_id"],
            ExternalTradingSubAccount.account_id == item["account_id"],
            ExternalTradingSubAccount.strategy_type == STRATEGY_SNOWBALL,
            ExternalTradingSubAccount.strategy_config_id == item["id"],
        ).first()
        if not config or not sub_account:
            raise ValueError("雪球配置或绑定虚拟子账户不存在")

        sync_target_positions(
            trading_db,
            sub_account=sub_account,
            targets=target_rows,
            signal_id=f"snowball:{item['combination_id']}",
            signal_version=signal_version,
            source_execution_id=None,
        )
        config.last_external_sync_at = datetime.now()
        config.last_external_sync_status = "SYNCED"
        config.last_external_sync_message = (
            f"{trigger_source}: 同步目标仓位 {len(target_rows)} 个"
            + (f"，跳过 {len(skipped)} 个缺价标的" if skipped else "")
        )[:500]
        if changed or trigger_source in {"manual", "notification"}:
            db.add(SnowballCopyLog(
                cli_id=config.cli_id,
                combination_id=config.combination_id,
                action="TARGET_SYNC",
                quantity=len(target_rows),
                status="TARGET_SYNCED",
                message=config.last_external_sync_message,
                account_id=config.account_id,
            ))

    return {
        "config_id": item["id"],
        "combination_id": item["combination_id"],
        "external_trading_account_id": item["external_trading_account_id"],
        "target_count": len(target_rows),
        "changed": changed,
        "signal_version": signal_version,
        "skipped": skipped,
    }


async def sync_snowball_external_trading_config_ids(
    config_ids: Optional[List[int]] = None,
    *,
    account_id: Optional[str] = None,
    trigger_source: str = "manual",
    trigger_executor: bool = True,
) -> Dict[str, Any]:
    if trigger_source in {"robot_timer", "notification"} and not is_a_share_trading_window():
        return {
            "status": "SKIPPED",
            "reason": "market_closed",
            "trigger_source": trigger_source,
            "next_run_at": next_a_share_trading_time().isoformat(),
            "checked": 0,
            "synced": 0,
            "changed": 0,
            "failed": 0,
            "items": [],
            "executor_results": [],
        }

    items = _load_snowball_external_sync_items(account_id=account_id, config_ids=config_ids)
    result = {
        "status": "OK",
        "trigger_source": trigger_source,
        "checked": len(items),
        "synced": 0,
        "changed": 0,
        "failed": 0,
        "items": [],
        "executor_results": [],
    }
    affected_accounts = {}
    for item in items:
        try:
            sync_result = await _sync_one_snowball_external_target(item, trigger_source=trigger_source)
            result["items"].append(sync_result)
            result["synced"] += 1
            if sync_result.get("changed"):
                result["changed"] += 1
            if sync_result.get("changed") or trigger_source == "manual":
                affected_accounts[item["external_trading_account_id"]] = item["account_id"]
        except Exception as exc:
            logger.exception("Snowball external target sync failed: config=%s", item.get("id"))
            result["failed"] += 1
            error_message = str(exc)
            result["items"].append({
                "config_id": item.get("id"),
                "combination_id": item.get("combination_id"),
                "status": "FAILED",
                "error": error_message,
            })
            _mark_snowball_external_sync_failure(item.get("id"), error_message)

    if trigger_executor and affected_accounts:
        for external_account_id, owner_account_id in affected_accounts.items():
            executor_result = await trigger_external_trading_executor(
                account_id=owner_account_id,
                external_account_id=external_account_id,
                trigger_source=f"snowball_{trigger_source}",
            )
            result["executor_results"].append(executor_result)

    if result["failed"]:
        result["status"] = "PARTIAL_FAILED" if result["synced"] else "FAILED"
    return result


def process_snowball_external_trading_sync_for_robot() -> Dict[str, Any]:
    return asyncio.run(sync_snowball_external_trading_config_ids(trigger_source="robot_timer"))

@router.get("/info/{symbol}")
async def get_combination_info(
    symbol: str, 
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """Get combination info (name) from Xueqiu"""
    acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    cookie = acc_config.xueqiu_cookie if acc_config else None
    
    info = await fetch_xueqiu_cube_info(symbol, cookie)
    if not info:
         raise HTTPException(status_code=404, detail="Combination not found or Xueqiu API error")
    return info

# --- Endpoints ---

@router.get("/account-config", response_model=SnowballAccountConfigModel)
async def get_account_config(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    return SnowballAccountConfigModel(
        xueqiu_cookie=config.xueqiu_cookie if config else None,
        updated_at=config.updated_at if config else None,
    )

@router.post("/account-config")
async def update_account_config(
    data: SnowballAccountConfigModel,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    cookie = (data.xueqiu_cookie or "").strip()
    if cookie:
        await _validate_xueqiu_cookie(cookie)

    config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    if not config:
        config = SnowballAccountConfig(account_id=account_id, xueqiu_cookie=data.xueqiu_cookie)
        db.add(config)
    else:
        config.xueqiu_cookie = data.xueqiu_cookie
    db.commit()
    return {"message": "Success"}

@router.post("/account-config/xueqiu-cookie-sync")
async def sync_xueqiu_cookie_from_browser_extension(
    data: SnowballCookieSyncRequest,
    account_id: str = Depends(valid_account),
    x_snowball_cookie_sync_token: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    expected_token = os.getenv("SNOWBALL_COOKIE_SYNC_TOKEN")
    if expected_token and x_snowball_cookie_sync_token != expected_token:
        raise HTTPException(status_code=401, detail="Invalid sync token")

    cookie = (data.xueqiu_cookie or "").strip()
    match = re.search(r"(?:^|;\s*)xq_a_token=([^;\s]+)", cookie)
    if not match:
        raise HTTPException(status_code=400, detail="Missing xq_a_token")

    token = match.group(1)
    normalized_cookie = f"xq_a_token={token};"
    await _validate_xueqiu_cookie(normalized_cookie)
    config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    if not config:
        config = SnowballAccountConfig(account_id=account_id, xueqiu_cookie=normalized_cookie)
        db.add(config)
    else:
        config.xueqiu_cookie = normalized_cookie
    db.commit()
    return {"message": "Success", "updated_at": datetime.now().isoformat()}

@router.get("/configs", response_model=List[SnowballConfigResponse])
async def list_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    configs = db.query(SnowballCopyConfig).filter(SnowballCopyConfig.account_id == account_id).all()
    return [await _snowball_config_response_with_net_asset(db, c, trading_db) for c in configs]

@router.post("/configs", response_model=SnowballConfigResponse)
async def create_config(
    config: SnowballConfigCreate,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    # Auto-fetch name if not provided
    if not config.combination_name:
        acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
        cookie = acc_config.xueqiu_cookie if acc_config else None
        
        cube_info = await fetch_xueqiu_cube_info(config.combination_id, cookie)
        if cube_info:
            config.combination_name = cube_info.get("name")
        
    db_config = SnowballCopyConfig(**config.dict())
    db_config.account_id = account_id

    db.add(db_config)
    db.flush()
    _sync_snowball_live_sub_account_binding(trading_db, db_config, previous_sub_account_id=None)
    trading_db.commit()
    db.commit()
    db.refresh(db_config)
    return await _snowball_config_response_with_net_asset(db, db_config, trading_db)

@router.put("/configs/{config_id}", response_model=SnowballConfigResponse)
async def update_config(
    config_id: int,
    config_update: SnowballConfigUpdate,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    db_config = db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.id == config_id,
        SnowballCopyConfig.account_id == account_id
    ).first()
    if not db_config:
        raise HTTPException(status_code=404, detail="Config not found")
        
    update_data = config_update.dict(exclude_unset=True)
    
    # If updating combination_id but not name, try to fetch name
    if "combination_id" in update_data and "combination_name" not in update_data:
            acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
            cookie = acc_config.xueqiu_cookie if acc_config else None
            
            cube_info = await fetch_xueqiu_cube_info(update_data["combination_id"], cookie)
            if cube_info:
                update_data["combination_name"] = cube_info.get("name")
                
    previous_sub_account_id = getattr(db_config, "live_sub_account_id", None)

    for key, value in update_data.items():
        setattr(db_config, key, value)

    _sync_snowball_live_sub_account_binding(
        trading_db,
        db_config,
        previous_sub_account_id=previous_sub_account_id,
    )
    trading_db.commit()
    db.commit()
    db.refresh(db_config)
    return await _snowball_config_response_with_net_asset(db, db_config, trading_db)

@router.delete("/configs/{config_id}")
async def delete_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    db_config = db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.id == config_id,
        SnowballCopyConfig.account_id == account_id
    ).first()
    
    if not db_config:
        raise HTTPException(status_code=404, detail="Config not found")

    if getattr(db_config, "live_sub_account_id", None):
        _deactivate_snowball_target_positions(
            trading_db,
            sub_account_id=db_config.live_sub_account_id,
            config_id=db_config.id,
        )
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == db_config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_SNOWBALL,
            ExternalTradingSubAccount.strategy_config_id == db_config.id,
        ).first()
        if sub_account:
            sub_account.strategy_type = None
            sub_account.strategy_config_id = None
            sub_account.updated_at = datetime.now()

    db.delete(db_config)
    trading_db.commit()
    db.commit()
    return {"message": "Deleted successfully"}

@router.get("/logs", response_model=PaginatedSnowballLogs)
async def get_logs(
    account_id: str = Depends(valid_account),
    page: int = 1,
    page_size: int = 20,
    cli_id: Optional[str] = None,
    combination_id: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(SnowballCopyLog).filter(SnowballCopyLog.account_id == account_id)
    
    if cli_id:
        query = query.filter(SnowballCopyLog.cli_id == cli_id)
    if combination_id:
        query = query.filter(SnowballCopyLog.combination_id.contains(combination_id))
        
    total = query.count()
    
    logs = query.order_by(SnowballCopyLog.timestamp.desc())\
        .offset((page - 1) * page_size)\
        .limit(page_size)\
        .all()

    items = []
    for log in logs:
        items.append(SnowballLogResponse.from_orm(log))
        
    return PaginatedSnowballLogs(
        total=total,
        items=items
    )

@router.post("/logs/status")
async def update_log_status(
    status_update: SnowballLogStatusUpdate,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    log = db.query(SnowballCopyLog).filter(
        SnowballCopyLog.id == status_update.id,
        SnowballCopyLog.account_id == account_id
    ).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log entry not found")
    
    log.status = status_update.status
    if status_update.message and status_update.message not in log.message:
        log.message = f"{log.message} | {status_update.message}"
        
    db.commit()
    return {"message": "Status updated"}


@router.get("/snapshot/{config_id}", response_model=SnowballSnapshotResponse)
async def get_snapshot(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.id == config_id,
        SnowballCopyConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if not config.external_trading_account_id or not config.live_sub_account_id:
        raise HTTPException(status_code=400, detail="请先为雪球配置选择外部交易账户和虚拟子账户")

    sub_account = trading_db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == config.live_sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == config.external_trading_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=400, detail="绑定的虚拟子账户不存在")

    acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    cookie = acc_config.xueqiu_cookie if acc_config else None
    xueqiu_holdings = await fetch_xueqiu_holdings(config.combination_id, cookie)
    blacklist = config.blacklisted_symbols or []

    target_weights: Dict[str, Dict[str, Any]] = {}
    all_xq_symbols = set()
    for holding in xueqiu_holdings:
        xq_symbol = _to_xueqiu_symbol(holding.get("symbol"))
        trade_symbol = _to_trade_symbol(xq_symbol)
        if not xq_symbol or not trade_symbol:
            continue
        all_xq_symbols.add(xq_symbol)
        blacklisted = any(
            fnmatch.fnmatch(xq_symbol, pattern) or fnmatch.fnmatch(trade_symbol, pattern)
            for pattern in blacklist
        )
        weight = safe_float(holding.get("weight"))
        target_weights[trade_symbol] = {
            "xueqiu_symbol": xq_symbol,
            "name": (
                holding.get("stockName")
                or holding.get("stock_name")
                or holding.get("name")
                or holding.get("stockNameCN")
                or ""
            ),
            "xueqiu_weight_pct": weight,
            "target_weight_pct": 0.0 if blacklisted else weight,
            "blacklisted": blacklisted,
        }

    ledger_rows = trading_db.query(ExternalTradingLedgerPosition).filter(
        ExternalTradingLedgerPosition.sub_account_id == sub_account.id
    ).all()
    ledger_positions: Dict[str, ExternalTradingLedgerPosition] = {}
    for row in ledger_rows:
        trade_symbol = normalize_trading_symbol(row.symbol)
        if not trade_symbol:
            continue
        if (
            safe_int(row.quantity) <= 0
            and safe_int(row.available_quantity) <= 0
            and safe_float(row.market_value) <= 0
        ):
            continue
        ledger_positions[trade_symbol] = row
        xq_symbol = _to_xueqiu_symbol(trade_symbol)
        if xq_symbol:
            all_xq_symbols.add(xq_symbol)

    try:
        valuation = await calculate_sub_account_net_asset(trading_db, sub_account, positions=ledger_rows)
    except ExternalTradingValuationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    quotes = await fetch_xueqiu_batch_quotes(sorted(all_xq_symbols), cookie)
    ledger_position_values = {
        normalize_trading_symbol(row.get("symbol")): row
        for row in valuation.get("positions", [])
        if normalize_trading_symbol(row.get("symbol"))
    }
    ledger_cash = safe_float(valuation.get("cash_available"))
    ledger_market_value = safe_float(valuation.get("position_market_value"))
    ledger_net_asset = safe_float(valuation.get("net_asset"))

    def get_quote_info(trade_symbol: str) -> Dict[str, Any]:
        xq_symbol = _to_xueqiu_symbol(trade_symbol)
        quote = quotes.get(xq_symbol or "", {}) if xq_symbol else {}
        ledger_value = ledger_position_values.get(trade_symbol) or {}
        row = ledger_positions.get(trade_symbol)
        price = safe_float(quote.get("price"), safe_float(ledger_value.get("price")))
        if price <= 0 and row:
            price = safe_float(row.market_price, safe_float(row.avg_cost))
        return {
            "xueqiu_symbol": xq_symbol,
            "name": quote.get("name") or target_weights.get(trade_symbol, {}).get("name") or "",
            "price": price,
        }

    detailed_holdings = []
    target_market_value = 0.0
    all_trade_symbols = set(target_weights.keys()) | set(ledger_positions.keys())
    for trade_symbol in sorted(all_trade_symbols):
        quote_info = get_quote_info(trade_symbol)
        price = quote_info["price"]
        target = target_weights.get(trade_symbol) or {
            "xueqiu_symbol": quote_info["xueqiu_symbol"],
            "name": quote_info["name"],
            "xueqiu_weight_pct": 0.0,
            "target_weight_pct": 0.0,
            "blacklisted": False,
        }
        target_weight_pct = safe_float(target.get("target_weight_pct"))
        ideal_target_value = ledger_net_asset * target_weight_pct / 100.0
        target_quantity = 0
        if price > 0 and ideal_target_value > 0:
            target_quantity = int((ideal_target_value / price) / 100) * 100
        target_value = round(target_quantity * price, 2) if price > 0 else round(ideal_target_value, 2)
        target_market_value += target_value

        ledger_row = ledger_positions.get(trade_symbol)
        valued_ledger = ledger_position_values.get(trade_symbol) or {}
        ledger_quantity = safe_int(getattr(ledger_row, "quantity", 0))
        ledger_available_quantity = safe_int(getattr(ledger_row, "available_quantity", ledger_quantity))
        ledger_value = safe_float(valued_ledger.get("market_value"), safe_float(getattr(ledger_row, "market_value", 0.0)))
        if ledger_value <= 0 and price > 0 and ledger_quantity > 0:
            ledger_value = round(ledger_quantity * price, 2)

        quantity_diff = target_quantity - ledger_quantity
        value_diff = round(target_value - ledger_value, 2)
        ledger_weight_pct = (ledger_value / ledger_net_asset) * 100 if ledger_net_asset > 0 else 0.0
        weight_diff_pct = target_weight_pct - ledger_weight_pct
        if target_quantity > 0 and ledger_quantity <= 0:
            diff_type = "TARGET_ONLY"
        elif target_quantity <= 0 and ledger_quantity > 0:
            diff_type = "LEDGER_ONLY"
        elif quantity_diff > 0:
            diff_type = "BUY"
        elif quantity_diff < 0:
            diff_type = "SELL"
        else:
            diff_type = "MATCHED"

        detailed_holdings.append(SnowballSnapshotHolding(
            symbol=trade_symbol,
            xueqiu_symbol=target.get("xueqiu_symbol") or quote_info["xueqiu_symbol"],
            name=quote_info["name"] or target.get("name") or "",
            price=price,
            xueqiu_weight_pct=safe_float(target.get("xueqiu_weight_pct")),
            target_weight_pct=target_weight_pct,
            target_quantity=target_quantity,
            target_value=target_value,
            ledger_quantity=ledger_quantity,
            ledger_available_quantity=ledger_available_quantity,
            ledger_market_value=round(ledger_value, 2),
            ledger_weight_pct=ledger_weight_pct,
            quantity_diff=quantity_diff,
            value_diff=value_diff,
            weight_diff_pct=weight_diff_pct,
            blacklisted=bool(target.get("blacklisted")),
            diff_type=diff_type,
        ))

    target_cash = round(max(ledger_net_asset - target_market_value, 0.0), 2)
    cash_diff = round(target_cash - ledger_cash, 2)
    detailed_holdings.sort(
        key=lambda row: (abs(row.value_diff), row.target_value, row.ledger_market_value),
        reverse=True,
    )

    return SnowballSnapshotResponse(
        config_id=config_id,
        updated_at=datetime.now(),
        sub_account_id=sub_account.id,
        sub_account_name=sub_account.name,
        target_market_value=round(target_market_value, 2),
        target_cash=target_cash,
        ledger_market_value=round(ledger_market_value, 2),
        ledger_cash=round(ledger_cash, 2),
        ledger_net_asset=round(ledger_net_asset, 2),
        ledger_stock_ratio=(ledger_market_value / ledger_net_asset) * 100 if ledger_net_asset > 0 else 0.0,
        ledger_cash_ratio=(ledger_cash / ledger_net_asset) * 100 if ledger_net_asset > 0 else 0.0,
        cash_diff=cash_diff,
        diff_count=len([row for row in detailed_holdings if row.quantity_diff != 0]),
        holdings=detailed_holdings,
    )


@router.post("/configs/{config_id}/sync-external-targets")
async def sync_config_external_targets(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.id == config_id,
        SnowballCopyConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if not getattr(config, "live_trade_enabled", False):
        raise HTTPException(status_code=400, detail="该雪球配置未开启通用执行器实盘跟单")

    return await sync_snowball_external_trading_config_ids(
        [config_id],
        account_id=account_id,
        trigger_source="manual",
        trigger_executor=True,
    )
