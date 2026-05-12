from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
import logging
import collections
import fnmatch
import asyncio
import re
import hashlib
import json
from sqlalchemy.orm import Session
from ...core.database import (
    get_db,
    get_db_ctx,
    Session,
    SnowballApiHeartbeat,
    SnowballCopyConfig,
    SnowballCopyLog,
    SnowballPortfolioSnapshot,
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
from .trade import TradeRequest

router = APIRouter(prefix="/api/snowball")
logger = logging.getLogger(__name__)
SNOWBALL_PTRADE_HEARTBEAT_ENDPOINT = "snowball_opportunities"

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

# --- Globals for Token Refresh ---
_last_token_refresh_time = None
_is_refreshing_token = False

def _record_snowball_ptrade_heartbeat(db: Session, account_id: str, cli_id: str):
    """记录 PTrade 对雪球交易机会接口的最近调用时间。"""
    try:
        now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        heartbeat = db.query(SnowballApiHeartbeat).filter(
            SnowballApiHeartbeat.endpoint == SNOWBALL_PTRADE_HEARTBEAT_ENDPOINT
        ).first()
        if not heartbeat:
            heartbeat = SnowballApiHeartbeat(
                endpoint=SNOWBALL_PTRADE_HEARTBEAT_ENDPOINT,
                call_count=0,
            )
            db.add(heartbeat)

        heartbeat.last_called_at = now
        heartbeat.last_account_id = account_id
        heartbeat.last_cli_id = cli_id
        heartbeat.call_count = (heartbeat.call_count or 0) + 1
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Failed to record Snowball PTrade heartbeat: %s", exc)

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

# --- Models ---

class SnowballAccountConfigModel(BaseModel):
    xueqiu_cookie: Optional[str] = None

class SnowballHeartbeatResponse(BaseModel):
    endpoint: str
    last_called_at: Optional[datetime] = None
    last_called_at_text: Optional[str] = None
    last_cli_id: Optional[str] = None
    call_count: int = 0
    seconds_since_last_call: Optional[float] = None
    is_recent: bool = False

class SnowballConfigCreate(BaseModel):
    cli_id: str
    combination_id: str
    combination_name: Optional[str] = None
    enabled: bool = True
    total_position_ratio: Optional[float] = 100.0
    total_amount: Optional[float] = None
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
    total_position_ratio: Optional[float] = None
    total_amount: Optional[float] = None
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
    symbol: Optional[str]
    stock_name: Optional[str] = None # Added field
    quantity: Optional[float]
    price: Optional[float]
    status: Optional[str]
    message: Optional[str]
    
    class Config:
        from_attributes = True

class TradeResponse(BaseModel):
    opportunities: List[Any]
    msg: Optional[str] = None

class SnowballLogStatusUpdate(BaseModel):
    id: int
    status: str
    message: Optional[str] = None
    price: Optional[float] = None

class PaginatedSnowballLogs(BaseModel):
    total: int
    items: List[SnowballLogResponse]


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
    snapshot = db.query(SnowballPortfolioSnapshot).filter_by(config_id=config.id).first()
    resp.snapshot_value = snapshot.market_value if snapshot else config.total_amount

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
                "total_position_ratio": safe_float(config.total_position_ratio, 100.0),
                "total_amount": safe_float(config.total_amount),
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
    position_ratio = max(safe_float(item.get("total_position_ratio"), 100.0), 0.0) / 100.0
    base_value = net_asset * position_ratio
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
    return SnowballAccountConfigModel(xueqiu_cookie=config.xueqiu_cookie if config else None)

@router.post("/account-config")
async def update_account_config(
    data: SnowballAccountConfigModel,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    if not config:
        config = SnowballAccountConfig(account_id=account_id, xueqiu_cookie=data.xueqiu_cookie)
        db.add(config)
    else:
        config.xueqiu_cookie = data.xueqiu_cookie
    db.commit()
    return {"message": "Success"}

@router.get("/heartbeat", response_model=SnowballHeartbeatResponse)
async def get_snowball_heartbeat(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    heartbeat = db.query(SnowballApiHeartbeat).filter(
        SnowballApiHeartbeat.endpoint == SNOWBALL_PTRADE_HEARTBEAT_ENDPOINT
    ).first()
    if not heartbeat:
        return SnowballHeartbeatResponse(endpoint=SNOWBALL_PTRADE_HEARTBEAT_ENDPOINT)

    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    seconds_since_last_call = (
        (now - heartbeat.last_called_at).total_seconds()
        if heartbeat.last_called_at else None
    )
    return SnowballHeartbeatResponse(
        endpoint=heartbeat.endpoint,
        last_called_at=heartbeat.last_called_at,
        last_called_at_text=(
            heartbeat.last_called_at.strftime("%Y-%m-%d %H:%M:%S")
            if heartbeat.last_called_at else None
        ),
        last_cli_id=heartbeat.last_cli_id,
        call_count=heartbeat.call_count or 0,
        seconds_since_last_call=seconds_since_last_call,
        is_recent=seconds_since_last_call is not None and seconds_since_last_call <= 300,
    )

@router.get("/configs", response_model=List[SnowballConfigResponse])
async def list_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    configs = db.query(SnowballCopyConfig).filter(SnowballCopyConfig.account_id == account_id).all()
    return [_snowball_config_response(db, c, trading_db) for c in configs]

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
    return _snowball_config_response(db, db_config, trading_db)

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
    return _snowball_config_response(db, db_config, trading_db)

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
    symbol: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(SnowballCopyLog).filter(SnowballCopyLog.account_id == account_id)
    
    if cli_id:
        query = query.filter(SnowballCopyLog.cli_id == cli_id)
    if combination_id:
        query = query.filter(SnowballCopyLog.combination_id.contains(combination_id))
    if symbol:
        query = query.filter(SnowballCopyLog.symbol.contains(symbol))
        
    total = query.count()
    
    logs = query.order_by(SnowballCopyLog.timestamp.desc())\
        .offset((page - 1) * page_size)\
        .limit(page_size)\
        .all()
        
    # --- Fetch Stock Names ---
    unique_symbols = {log.symbol for log in logs if log.symbol}
    
    acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    cookie = acc_config.xueqiu_cookie if acc_config else None
    
    quotes = await fetch_xueqiu_batch_quotes(list(unique_symbols), cookie)
    
    items = []
    for log in logs:
        item = SnowballLogResponse.from_orm(log)
        if log.symbol and log.symbol in quotes:
            item.stock_name = quotes[log.symbol].get("name", "")
        items.append(item)
        
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
    if status_update.price:
        log.price = status_update.price
        
    db.commit()
    return {"message": "Status updated"}

class SnapshotHolding(BaseModel):
    symbol: str
    name: str # Added name
    quantity: float
    price: float
    market_value: float
    ratio: float # Percentage 0-100

class SnowballSnapshotResponse(BaseModel):
    config_id: int
    market_value: float # Total MV (Stocks + Cash)
    cash: float
    stock_ratio: float
    cash_ratio: float
    last_synced_amount: float
    holdings: List[SnapshotHolding] = []
    updated_at: datetime
    
    class Config:
        from_attributes = True

@router.get("/snapshot/{config_id}", response_model=SnowballSnapshotResponse)
async def get_snapshot(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    snapshot = db.query(SnowballPortfolioSnapshot).filter(
        SnowballPortfolioSnapshot.config_id == config_id,
        SnowballPortfolioSnapshot.account_id == account_id
    ).first()
    
    if not snapshot:
            return SnowballSnapshotResponse(
                config_id=config_id,
                market_value=0.0,
                cash=0.0,
                stock_ratio=0.0,
                cash_ratio=0.0,
                last_synced_amount=0.0,
                holdings=[],
                updated_at=datetime.now()
            )
    
    # Calculate Real-time Values
    holdings_dict = snapshot.holdings or {}
    symbols = list(holdings_dict.keys())
    
    # 1. Fetch Real-time Prices
    acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    cookie = acc_config.xueqiu_cookie if acc_config else None
    quotes = await fetch_xueqiu_batch_quotes(symbols, cookie)
    
    # 2. Build Holding Details & Calc Total
    detailed_holdings = []
    total_stock_value = 0.0
    
    for sym, qty in holdings_dict.items():
        info = quotes.get(sym, {})
        price = info.get("price", 0.0)
        name = info.get("name", "")
        
        mv = qty * price
        total_stock_value += mv
        
        detailed_holdings.append(SnapshotHolding(
            symbol=sym,
            name=name,
            quantity=qty,
            price=price,
            market_value=mv,
            ratio=0.0 # Calc later
        ))
        
    current_cash = snapshot.cash or 0.0
    total_market_value = total_stock_value + current_cash
    
    # 3. Calculate Ratios
    if total_market_value > 0:
        for h in detailed_holdings:
            h.ratio = (h.market_value / total_market_value) * 100
        
        stock_ratio = (total_stock_value / total_market_value) * 100
        cash_ratio = (current_cash / total_market_value) * 100
    else:
        stock_ratio = 0.0
        cash_ratio = 100.0 if current_cash > 0 else 0.0

    # Sort by Market Value desc
    detailed_holdings.sort(key=lambda x: x.market_value, reverse=True)

    return SnowballSnapshotResponse(
        config_id=config_id,
        market_value=total_market_value,
        cash=current_cash,
        stock_ratio=stock_ratio,
        cash_ratio=cash_ratio,
        last_synced_amount=snapshot.last_synced_amount,
        holdings=detailed_holdings,
        updated_at=snapshot.updated_at or datetime.now()
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


@router.post("/configs/{config_id}/sync-snapshot-to-ledger")
async def sync_snapshot_to_external_ledger(
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
    if sub_account.strategy_type != STRATEGY_SNOWBALL or sub_account.strategy_config_id != config.id:
        raise HTTPException(status_code=400, detail="绑定的虚拟子账户尚未绑定当前雪球配置，请先保存配置")

    snapshot = db.query(SnowballPortfolioSnapshot).filter(
        SnowballPortfolioSnapshot.config_id == config_id,
        SnowballPortfolioSnapshot.account_id == account_id,
    ).first()
    holdings = snapshot.holdings if snapshot else None
    if not holdings:
        raise HTTPException(status_code=400, detail="该雪球配置没有可同步的旧快照持仓")

    acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    cookie = acc_config.xueqiu_cookie if acc_config else None
    xq_symbols = sorted({_to_xueqiu_symbol(symbol) for symbol in holdings.keys() if _to_xueqiu_symbol(symbol)})
    quotes = await fetch_xueqiu_quotes(xq_symbols, cookie)

    now = datetime.now()
    existing_rows = {
        normalize_trading_symbol(row.symbol): row
        for row in trading_db.query(ExternalTradingLedgerPosition)
        .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
        .all()
        if row.symbol
    }
    touched_symbols = set()
    target_rows = []
    total_market_value = safe_float(snapshot.market_value)
    for raw_symbol, raw_quantity in holdings.items():
        xq_symbol = _to_xueqiu_symbol(raw_symbol)
        trade_symbol = _to_trade_symbol(raw_symbol)
        quantity = safe_int(raw_quantity)
        if not xq_symbol or not trade_symbol or quantity <= 0:
            continue
        touched_symbols.add(trade_symbol)
        row = existing_rows.get(trade_symbol)
        price = safe_float(quotes.get(xq_symbol))
        if not row:
            row = ExternalTradingLedgerPosition(
                account_id=account_id,
                external_trading_account_id=config.external_trading_account_id,
                sub_account_id=sub_account.id,
                symbol=trade_symbol,
                quantity=0,
                available_quantity=0,
                avg_cost=0.0,
                realized_pnl=0.0,
                updated_at=now,
            )
            trading_db.add(row)
            trading_db.flush()
        if price <= 0:
            price = safe_float(row.market_price, safe_float(row.avg_cost))
        row.quantity = quantity
        row.available_quantity = quantity
        row.avg_cost = price if price > 0 else safe_float(row.avg_cost)
        row.market_price = price if price > 0 else None
        row.market_value = round(quantity * price, 2) if price > 0 else None
        row.updated_at = now
        market_value = safe_float(row.market_value)
        target_rows.append({
            "symbol": trade_symbol,
            "target_quantity": quantity,
            "target_weight_pct": round((market_value / total_market_value) * 100, 4) if total_market_value > 0 else None,
            "target_value": market_value,
        })

    for symbol, row in existing_rows.items():
        if symbol in touched_symbols:
            continue
        row.quantity = 0
        row.available_quantity = 0
        row.market_value = 0.0
        row.updated_at = now

    sub_account.cash_available = safe_float(snapshot.cash)
    sub_account.updated_at = now

    signal_version = _build_snowball_target_signal_version({
        "id": config.id,
        "combination_id": config.combination_id,
    }, target_rows)
    sync_target_positions(
        trading_db,
        sub_account=sub_account,
        targets=target_rows,
        signal_id=f"snowball:init:{config.combination_id}",
        signal_version=signal_version,
        source_execution_id=None,
    )

    config.last_external_sync_at = now
    config.last_external_sync_status = "LEDGER_INIT"
    config.last_external_sync_message = f"已从旧雪球快照初始化子账户账本和目标仓位，共 {len(target_rows)} 个标的"
    db.add(SnowballCopyLog(
        cli_id=config.cli_id,
        combination_id=config.combination_id,
        action="LEDGER_INIT",
        quantity=len(target_rows),
        status="SUCCESS",
        message=config.last_external_sync_message,
        account_id=account_id,
    ))
    trading_db.commit()
    db.commit()

    return {
        "message": config.last_external_sync_message,
        "sub_account_id": sub_account.id,
        "position_count": len(target_rows),
        "cash_available": sub_account.cash_available,
        "signal_version": signal_version,
    }

# --- Core Logic ---

@router.post("/opportunities", response_model=TradeResponse)
async def get_snowball_opportunities(
    request: TradeRequest,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """
    Calculate trading opportunities based on Snowball combination holdings.
    External caller provides current positions and cli_id.
    Supports multiple combinations per cli_id using Snapshot tracking.
    """
    cli_id = request.cli_id
    _record_snowball_ptrade_heartbeat(db, account_id, cli_id)

    # 1. Fetch Configs
    _configs_orm = db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.cli_id == cli_id,
        SnowballCopyConfig.enabled == True,
        SnowballCopyConfig.account_id == account_id
    ).all()
    if not _configs_orm:
        return TradeResponse(opportunities=[], msg="Configuration not found or disabled")
        
    # Convert to dicts to avoid DetachedInstanceError across awaits
    configs = []
    for c in _configs_orm:
        configs.append({
            "id": c.id,
            "combination_id": c.combination_id,
            "combination_name": c.combination_name,
            "total_amount": c.total_amount,
            "tracking_error_pct": c.tracking_error_pct,
            "cli_id": c.cli_id,
            "blacklisted_symbols": c.blacklisted_symbols or []
        })

    # Fetch global account cookie
    acc_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    acc_cookie = acc_config.xueqiu_cookie if acc_config else None

    # 0. Background Token Refresh
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if not _last_token_refresh_time or (now - _last_token_refresh_time).total_seconds() >= 3600:
        asyncio.create_task(_refresh_xueqiu_guest_token_task(account_id, acc_cookie))

    # 2. Pre-fetch Data
    # 2.1 Gather all symbols (Held + Targets from all configs)
    all_symbols = set()
    
    # Local cache for target holdings: {config_id: [{symbol, weight}]}
    config_target_weights = {} 
    
    # A. From Request Positions
    current_quantities = {} # symbol -> quantity
    for pos in request.positions:
            current_quantities[pos.symbol] = pos.quantity
            all_symbols.add(pos.symbol)

    # B. From Config Targets (Fetch XQ Holdings)
    valid_configs = []
    for config in configs:
        try:
            weights = await fetch_xueqiu_holdings(config['combination_id'], acc_cookie)
            config_target_weights[config['id']] = weights
            for w in weights:
                all_symbols.add(w['symbol'])
            valid_configs.append(config)
        except Exception as e:
            if "组合不存在" in str(e):
                logger.warning(f"Skipping config {config['id']} ({config['combination_id']}) because it does not exist: {e}")
                continue
            raise e # Create a crash for other errors as requested
            
    # Upgrade configs list to only include valid ones to prevent liquidation on error
    configs = valid_configs

    # 2.2 Fetch Prices
    prices = await fetch_xueqiu_quotes(list(all_symbols), acc_cookie)
    
    # Helper to get price
    def get_price(sym):
        p = prices.get(sym)
        if not p:
                pos = next((pos for pos in request.positions if pos.symbol == sym), None)
                if pos: p = pos.cost_price
        return p or 0.0

    # 3. Process Snapshots & Aggregate Targets
    aggregated_target_quantities = {} # symbol -> quantity
    symbol_contributors = collections.defaultdict(set) # symbol -> set(combination_id)
    symbol_needs = collections.defaultdict(list) # symbol -> list of {id, reason, diff}
    
    current_time = request.current_time or datetime.now()
    # Reset Window: 14:55 - 15:00 (A-share closing)
    is_closing_window = (current_time.hour == 14 and current_time.minute >= 50) or (current_time.hour == 15 and current_time.minute == 0)

    logger.info(f"Current time: {current_time}, Is closing window: {is_closing_window}")

    for config in configs:
        snapshot = db.query(SnowballPortfolioSnapshot).filter_by(config_id=config['id']).first()
        
        # --- Initialize or Calculate Current Snapshot Value ---
        if not snapshot:
            snapshot = SnowballPortfolioSnapshot(
                config_id=config['id'],
                holdings={},
                cash=0.0,
                market_value=0.0,
                last_synced_amount=0.0,
                account_id=account_id
            )
            db.add(snapshot)
            db.flush()
        
        # --- Determine Base Value for Rebalancing ---
        # base_value 是本次 rebalance 的"目标总金额基准"
        target_amt = config['total_amount'] or 0.0
        synced_amt = snapshot.last_synced_amount or 0.0
        amt_changed = abs(target_amt - synced_amt) > 1e-6

        snap_holdings = snapshot.holdings or {}
        snap_mv = sum(qty * get_price(sym) for sym, qty in snap_holdings.items())
        snap_cash = max(snapshot.cash or 0.0, 0.0)  # 防止旧数据中存在负现金

        if amt_changed and is_closing_window:
            # ✅ 收盘窗口 + 金额有变化：以新配置金额作为基准（无论增资还是减资）
            # 这样 rebalance 会自动计算 target_val = target_amt × weight，生成相应的买/卖信号
            base_value = target_amt
            snapshot.last_synced_amount = target_amt
            logger.info(f"Config {config['id']} amount synced: {synced_amt} -> {target_amt}")
        elif amt_changed and not is_closing_window:
            # ⏳ 非收盘窗口 + 金额有变化：暂不生效，沿用当前快照市值
            # 等到收盘窗口再统一处理，避免白天改配置立即触发大量信号
            base_value = snap_mv + snap_cash
            logger.info(f"Config {config['id']} amount change {synced_amt}->{target_amt} pending until closing window")
        else:
            # 金额未变化：正常用当前快照市值做 rebalance（处理权重调整、价格漂移等）
            base_value = snap_mv + snap_cash

        if base_value <= 0:
            if target_amt > 0 and is_closing_window:
                # 首次使用且在收盘窗口：直接用配置金额初始化
                base_value = target_amt
                snapshot.last_synced_amount = target_amt
            else:
                logger.warning(f"Config {config['id']} base_value={base_value}, skipping rebalance")
                continue


        # --- Calculate New Target State ---
        new_snap_holdings = {}
        used_cash = 0.0
        
        weights = config_target_weights.get(config['id'], [])
        threshold_pct = config['tracking_error_pct'] or 1.0
        
        # Combine all symbols (Current Snapshot Holdings + Target symbols from XQ)
        all_snap_symbols = set(snap_holdings.keys())
        target_weights_map = {}
        for item in weights:
            # Blacklist Check: Treat target weight as 0 if blacklisted
            is_blacklisted = any(
                fnmatch.fnmatch(item['symbol'], pattern) 
                for pattern in config.get('blacklisted_symbols', [])
            )
            if is_blacklisted:
                continue
                
            all_snap_symbols.add(item['symbol'])
            target_weights_map[item['symbol']] = item['weight']
        
        for sym in all_snap_symbols:
            price = get_price(sym)
            if price <= 0: continue
            
            # 1. Target Value = 目标金额 × 权重%
            w = target_weights_map.get(sym, 0.0)
            target_val = base_value * (w / 100.0)
            
            # 2. Current Snapshot Value (按上次快照的股数 × 当前价)
            cur_q = snap_holdings.get(sym, 0)
            cur_val = cur_q * price
            
            # 3. Deviation Check vs target_amt
            diff_val = target_val - cur_val
            diff_pct_of_total = (abs(diff_val) / base_value) * 100 if base_value > 0 else 100
            
            final_q = cur_q  # Default: Keep current quantity
            
            # If deviation > threshold OR need to clear (target=0), rebalance to target
            if diff_pct_of_total >= threshold_pct or (target_val == 0 and cur_q > 0):
                raw_q = target_val / price
                final_q = int(raw_q / 100) * 100
            
            if final_q > 0:
                new_snap_holdings[sym] = final_q
                used_cash += final_q * price
            
            # Record Need & Reason for opportunity message
            snapshot_diff = final_q - cur_q
            if snapshot_diff != 0:
                s_tgt_pct = (target_val / base_value) * 100 if base_value > 0 else 0
                s_cur_pct = (cur_val / base_value) * 100 if base_value > 0 else 0
                
                combo_name = config['combination_name'] or config['combination_id'] or str(config['id'])
                short_name = combo_name[:10]
                need_reason = f"[{short_name}: {s_cur_pct:.1f}%->{s_tgt_pct:.1f}%]"
                
                symbol_needs[sym].append(need_reason)
                symbol_contributors[sym].add(config['combination_id'])

        # Update Snapshot: holdings 记录目标股数，cash 记录账面剩余
        snapshot.holdings = new_snap_holdings
        snapshot.cash = max(base_value - used_cash, 0.0)
        snapshot.market_value = base_value

        # Aggregate Targets across all configs for this cli_id
        for sym, qty in new_snap_holdings.items():
            aggregated_target_quantities[sym] = aggregated_target_quantities.get(sym, 0) + qty

    # 4. Generate Opportunities (Diff vs Actual)
    opportunities = []
    
    projected_cash = request.portfolio.available_cash
    
    # Identify all symbols needing action
    all_op_symbols = set(aggregated_target_quantities.keys()) | set(current_quantities.keys())
    
    def get_diff_info(sym):
        tgt_q = aggregated_target_quantities.get(sym, 0)
        cur_q = current_quantities.get(sym, 0)
        diff = tgt_q - cur_q
        return sym, diff, tgt_q, cur_q

    # Sort: Sells first (diff < 0)
    sorted_ops = sorted([get_diff_info(s) for s in all_op_symbols], key=lambda x: x[1]) # diff ascending (negative first)
    
    for sym, diff_qty, tgt_q, cur_q in sorted_ops:
        if diff_qty == 0:
            continue
            
        price = get_price(sym)
        if price <= 0: continue
        
        action = "BUY" if diff_qty > 0 else "SELL"
        abs_qty = abs(diff_qty)
        
        # Min Qty Check (100 or 200 for STAR)
        is_star = sym.startswith("SH.688")
        min_qty = 200 if is_star else 100
        
        if abs_qty < min_qty:
            continue
        
        final_qty = 0
        reason_global = ""
        
        # Global Stats (Optional, maybe append at end?)
        # total_actual_asset = request.portfolio.portfolio_value or 1.0
        # cur_val = cur_q * price
        # cur_pct = (cur_val / total_actual_asset) * 100
        
        if action == "SELL":
            # T+1 Check
            pos = next((p for p in request.positions if p.symbol == sym), None)
            available = pos.available_quantity if pos and pos.available_quantity is not None else cur_q
            
            final_qty = min(abs_qty, available)
            if final_qty < min_qty:
                logger.info(f"Skipping SELL {sym}: Need {abs_qty}, Avail {available}")
                continue
                
            proceeds = final_qty * price
            projected_cash += proceeds
            
        elif action == "BUY":
            # Cash Check
            est_cost = abs_qty * price
            if est_cost <= projected_cash:
                final_qty = abs_qty
                projected_cash -= est_cost
            else:
                # Partial buy
                max_can_buy = int((projected_cash / price) / 100) * 100
                if max_can_buy >= min_qty:
                    final_qty = max_can_buy
                    projected_cash -= final_qty * price
                    reason_global = " [Cash Ltd]"
                else:
                    logger.info(f"Skipping BUY {sym}: Need {est_cost}, Have {projected_cash}")
                    continue
        
        # Create Log & Opp
        contributors = symbol_contributors.get(sym, set())
        combo_id_str = ",".join(sorted(contributors)) if contributors else "AGGREGATED"
        
        # Concatenate Reasons
        specific_reasons = " ".join(symbol_needs.get(sym, []))
        final_message = f"{specific_reasons}{reason_global}"
        if not final_message:
            final_message = f"Adjusting to Target"

        log_entry = SnowballCopyLog(
            cli_id=cli_id,
            combination_id=combo_id_str, # Mixed
            action=action,
            symbol=sym,
            quantity=final_qty,
            price=price,
            status='SIGNAL',
            message=final_message,
            account_id=account_id
        )
            
        db.add(log_entry)
        db.flush()
        
        opportunities.append({
            "symbol": sym,
            "name": "",
            "action": action,
            "quantity": final_qty,
            "price": price,
            "reason": final_message,
            "op_id": log_entry.id # Single ID as Int
        })

    db.commit()
    
    # Sort output: SELLs first
    opportunities.sort(key=lambda x: 0 if x["action"] == "SELL" else 1)
    
    return TradeResponse(opportunities=opportunities, msg="Success")
