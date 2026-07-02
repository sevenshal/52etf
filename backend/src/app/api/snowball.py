from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import date, datetime
from zoneinfo import ZoneInfo
from sqlalchemy.exc import OperationalError
import httpx
import logging
import fnmatch
import asyncio
import re
import hashlib
import json
import os
import time
from ...core.database import (
    get_db,
    get_db_ctx,
    Session,
    SnowballCopyConfig,
    SnowballCopyLog,
    SnowballAccountConfig,
    SnowballBacktestRun,
    SnowballBacktestCurvePoint,
)
from ...core.event_stream import publish_event
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
from ...core.services.external_trading_execution_policy import resolve_execution_policy
from ...core.services.external_trading_ledger import (
    STRATEGY_SNOWBALL,
    is_star_market_symbol,
    normalize_symbol as normalize_trading_symbol,
    safe_float,
    safe_int,
    sync_target_positions,
)
from ...core.services.external_trading_valuation import (
    ExternalTradingValuationError,
    calculate_sub_account_net_asset,
)
from ...core.services.symbol_names import load_symbol_name_map, normalize_symbol_for_name
from ...core.services.snowball_backtest import run_snowball_cube_backtest
from .account import valid_account

router = APIRouter(prefix="/api/snowball")
logger = logging.getLogger(__name__)

# --- Constants ---
SNOWBALL_A_SHARE_LOT_SIZE = 100
SNOWBALL_MIN_ROUND_UP_QUANTITY = 50
SNOWBALL_STAR_MARKET_MIN_ROUND_UP_QUANTITY = 150
SNOWBALL_STAR_MARKET_MIN_BUY_QUANTITY = 200
SNOWBALL_MAIN_DB_WRITE_RETRY_ATTEMPTS = 3
SNOWBALL_MAIN_DB_WRITE_RETRY_BASE_SECONDS = 1.0
SNOWBALL_MAIN_DB_WRITE_RETRY_MAX_SECONDS = 4.0
XUEQIU_API_BASE_URL = "https://api.xueqiu.com"
XUEQIU_STOCK_BASE_URL = "https://stock.xueqiu.com"
XUEQIU_WEB_BASE_URL = "https://xueqiu.com"
XUEQIU_HEADERS = {
    "Host": "api.xueqiu.com",
    "Cookie": "xq_a_token=91eabb39aba7af77c2b00d8f8ac5700ade3cf02b;",
    "accept": "application/json",
    "accept-language": "zh-Hans-CN;q=1, en-CN;q=0.9",
    "x-device-os": "iOS 26.4.2",
    "x-device-model-name": "iPhone 16 Pro Max_iPhone17,2",
    "x-device-id": "933A28E8-45D4-447A-AA4D-93FECC7B78C5",
    "user-agent": "Xueqiu iPhone 14.90.2",
    "priority": "u=3, i"
}

XUEQIU_STOCK_HEADERS = XUEQIU_HEADERS.copy()
XUEQIU_STOCK_HEADERS["Host"] = "stock.xueqiu.com"

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
            headers = XUEQIU_HEADERS.copy()
            headers["Host"] = "xueqiu.com"
            headers["Referer"] = XUEQIU_WEB_BASE_URL
            _apply_xueqiu_cookie(headers, cookie)
                    
            response = await client.get(f"{XUEQIU_WEB_BASE_URL}/about/contact-us", headers=headers, timeout=10.0)
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
                                now = datetime.now()
                                old_c = config.xueqiu_cookie
                                if "xq_a_token=" in old_c:
                                    config.xueqiu_cookie = re.sub(r'xq_a_token=[^;]+', f'xq_a_token={new_token}', old_c)
                                else:
                                    config.xueqiu_cookie = f"xq_a_token={new_token}; {old_c}"
                                config.updated_at = now
                    break
    except Exception as e:
        logger.error(f"Failed to refresh Xueqiu guest token: {e}")
    finally:
        _is_refreshing_token = False


def _normalize_xueqiu_cookie(cookie: str) -> str:
    match = re.search(r"(?:^|;\s*)xq_a_token=([^;\s]+)", (cookie or "").strip())
    if not match:
        raise HTTPException(status_code=400, detail="Missing xq_a_token")
    return f"xq_a_token={match.group(1)};"


def _apply_xueqiu_cookie(headers: Dict[str, str], cookie: Optional[str]) -> Dict[str, str]:
    if not cookie:
        return headers
    if "xq_a_token" in cookie:
        headers["Cookie"] = cookie
    else:
        headers["Cookie"] = f"xq_a_token={cookie};"
    return headers


# --- Models ---

class SnowballAccountConfigModel(BaseModel):
    xueqiu_cookie: Optional[str] = None
    updated_at: Optional[datetime] = None

class SnowballCookieSyncRequest(BaseModel):
    xueqiu_cookie: Optional[str] = None
    login_detected: Optional[bool] = None
    token_present: Optional[bool] = None
    source: Optional[str] = None
    status_message: Optional[str] = None

class SnowballConfigCreate(BaseModel):
    combination_id: str
    combination_name: Optional[str] = None
    enabled: bool = True
    tracking_error_pct: float = 1.0
    blacklisted_symbols: List[str] = []
    live_trade_enabled: bool = False
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None

class SnowballConfigUpdate(BaseModel):
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


class SnowballBacktestStartRequest(BaseModel):
    slippage_pct: float = 0.1


class SnowballBacktestRunResponse(BaseModel):
    id: int
    config_id: int
    combination_id: str
    combination_name: Optional[str] = None
    status: str
    slippage_pct: float
    requested_start_date: Optional[date] = None
    requested_end_date: Optional[date] = None
    effective_start_date: Optional[date] = None
    actual_nav_start: Optional[date] = None
    actual_nav_end: Optional[date] = None
    actual_rebalance_start: Optional[datetime] = None
    benchmark_symbol: Optional[str] = None
    benchmark_name: Optional[str] = None
    performance_raw: Optional[Dict[str, Any]] = None
    performance_after_slippage: Optional[Dict[str, Any]] = None
    benchmark_metrics: Optional[Dict[str, Any]] = None
    slippage: Optional[Dict[str, Any]] = None
    comparison: Optional[Dict[str, Any]] = None
    rebalancing: Optional[Dict[str, Any]] = None
    rebalance_fetch: Optional[Dict[str, Any]] = None
    yearly_returns: List[Dict[str, Any]] = []
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SnowballBacktestCurvePointResponse(BaseModel):
    date: date
    raw_nav: Optional[float] = None
    slippage_nav: Optional[float] = None
    benchmark_nav: Optional[float] = None
    raw_return_pct: Optional[float] = None
    slippage_return_pct: Optional[float] = None
    benchmark_return_pct: Optional[float] = None
    raw_drawdown_pct: Optional[float] = None
    slippage_drawdown_pct: Optional[float] = None
    benchmark_drawdown_pct: Optional[float] = None
    slippage_cost_pct: Optional[float] = None

    class Config:
        from_attributes = True


class SnowballBacktestDetailResponse(SnowballBacktestRunResponse):
    curve_points: List[SnowballBacktestCurvePointResponse] = []


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
    reference_price: Optional[float] = None
    reference_price_source: Optional[str] = None
    initial_protection_price: Optional[float] = None
    execution_protection_price: Optional[float] = None
    executor_max_slippage_pct: Optional[float] = None
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


def _snowball_target_quantity(target_value: float, price: float, symbol: Optional[str] = None) -> int:
    target_value = safe_float(target_value)
    price = safe_float(price)
    if target_value <= 0 or price <= 0:
        return 0

    raw_quantity = target_value / price
    quantity = (
        int(raw_quantity / SNOWBALL_A_SHARE_LOT_SIZE)
        * SNOWBALL_A_SHARE_LOT_SIZE
    )
    if (
        is_star_market_symbol(symbol)
        and quantity <= SNOWBALL_A_SHARE_LOT_SIZE
        and raw_quantity > SNOWBALL_STAR_MARKET_MIN_ROUND_UP_QUANTITY
    ):
        return SNOWBALL_STAR_MARKET_MIN_BUY_QUANTITY
    if quantity == 0 and raw_quantity > SNOWBALL_MIN_ROUND_UP_QUANTITY:
        return SNOWBALL_A_SHARE_LOT_SIZE
    return quantity


def _should_recalculate_snowball_target(
    *,
    has_old_target: bool,
    old_quantity: int,
    old_weight: Optional[float],
    new_weight: float,
    candidate_quantity: int,
    price: float,
    base_value: float,
    threshold_pct: float,
) -> bool:
    if not has_old_target or old_weight is None:
        return True
    if abs(new_weight - old_weight) >= threshold_pct:
        return True
    if safe_int(old_quantity) == safe_int(candidate_quantity):
        return False
    if threshold_pct <= 0:
        return True

    quantity_delta_value = abs(safe_int(candidate_quantity) - safe_int(old_quantity)) * max(safe_float(price), 0.0)
    allowed_drift_value = max(safe_float(base_value), 0.0) * threshold_pct / 100.0
    return quantity_delta_value > allowed_drift_value

async def fetch_xueqiu_holdings(symbol: str, cookie: str = None) -> List[Dict]:
    """Fetch holdings from Xueqiu API"""
    url = f"{XUEQIU_API_BASE_URL}/cube/center/cube/holdSymbols.json?symbol={symbol}"
    
    headers = XUEQIU_HEADERS.copy()
    _apply_xueqiu_cookie(headers, cookie)
    
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


async def fetch_xueqiu_latest_rebalance_prices(symbol: str, cookie: str = None) -> Dict[str, Dict[str, Any]]:
    """Fetch latest Snowball rebalance fill prices by Xueqiu symbol."""
    headers = XUEQIU_HEADERS.copy()
    headers["Referer"] = f"{XUEQIU_WEB_BASE_URL}/P/{symbol}"
    _apply_xueqiu_cookie(headers, cookie)

    params = {"cube_symbol": symbol, "count": 20, "page": 1}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/history.json",
                params=params,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            events = data.get("list") if isinstance(data, dict) else None
            if not events:
                return {}
            result = {}
            for event in events:
                for row in event.get("rebalancing_histories") or []:
                    xq_symbol = _to_xueqiu_symbol(row.get("stock_symbol"))
                    price = safe_float(row.get("price"))
                    if not xq_symbol or price <= 0 or xq_symbol in result:
                        continue
                    result[xq_symbol] = {
                        "price": price,
                        "event_id": event.get("id"),
                        "created_at": event.get("created_at"),
                        "target_weight": row.get("target_weight"),
                        "previous_weight": (
                            row.get("prev_weight_adjusted")
                            if row.get("prev_weight_adjusted") is not None
                            else row.get("prev_target_weight", row.get("prev_weight"))
                        ),
                    }
            return result
        except Exception as e:
            logger.warning("Failed to fetch Xueqiu latest rebalance prices for %s: %s", symbol, e)
            return {}


async def fetch_xueqiu_cube_info(symbol: str, cookie: str = None) -> Optional[Dict]:
    """Fetch cube info including name from Xueqiu"""
    ts = int(datetime.now().timestamp() * 1000)
    url = f"{XUEQIU_API_BASE_URL}/cubes/nav_daily/all.json?cube_symbol={symbol}&since={ts}&until={ts}"
    
    headers = XUEQIU_HEADERS.copy()
    headers["Referer"] = f"{XUEQIU_WEB_BASE_URL}/P/{symbol}"
    _apply_xueqiu_cookie(headers, cookie)

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
    
    url = f"{XUEQIU_STOCK_BASE_URL}/v5/stock/realtime/quotec.json?symbol={raw_symbol_str}"
    
    headers = XUEQIU_STOCK_HEADERS.copy()
    _apply_xueqiu_cookie(headers, cookie)

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
    
    url = f"{XUEQIU_STOCK_BASE_URL}/v5/stock/batch/quote.json?symbol={raw_symbol_str}"
    
    headers = XUEQIU_STOCK_HEADERS.copy()
    _apply_xueqiu_cookie(headers, cookie)
             
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


def _snowball_reference_price(
    *,
    xq_symbol: str,
    old_quantity: int,
    final_quantity: int,
    rebalance_prices: Dict[str, Dict[str, Any]],
    old_reference_price: Optional[float] = None,
) -> Optional[float]:
    delta_quantity = safe_int(final_quantity) - safe_int(old_quantity)
    previous_reference_price = safe_float(old_reference_price, None)
    if delta_quantity == 0 and previous_reference_price and previous_reference_price > 0:
        return previous_reference_price

    fill = rebalance_prices.get(xq_symbol) or {}
    fill_price = safe_float(fill.get("price"))
    if fill_price <= 0:
        return previous_reference_price
    return round(fill_price, 4)


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
                        "target_weight_pct": safe_float(row.target_weight_pct, None),
                        "target_value": safe_float(row.target_value),
                        "signal_version": row.signal_version,
                        "reference_price": safe_float(row.reference_price, None),
                        "reference_price_source": row.reference_price_source,
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
                "reference_price": safe_float(row.get("reference_price"), None),
            }
            for row in sorted(target_rows, key=lambda data: str(data.get("symbol") or ""))
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"snowball:{item.get('id')}:{digest[:16]}"[:64]


def _is_sqlite_lock_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _write_snowball_main_db_with_retry(operation_name: str, writer):
    for attempt in range(1, SNOWBALL_MAIN_DB_WRITE_RETRY_ATTEMPTS + 1):
        try:
            return writer()
        except Exception as exc:
            if not _is_sqlite_lock_error(exc) or attempt >= SNOWBALL_MAIN_DB_WRITE_RETRY_ATTEMPTS:
                raise
            sleep_seconds = min(
                SNOWBALL_MAIN_DB_WRITE_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                SNOWBALL_MAIN_DB_WRITE_RETRY_MAX_SECONDS,
            )
            logger.warning(
                "%s hit SQLite lock, retrying %s/%s in %.1fs: %s",
                operation_name,
                attempt + 1,
                SNOWBALL_MAIN_DB_WRITE_RETRY_ATTEMPTS,
                sleep_seconds,
                exc,
            )
            time.sleep(sleep_seconds)
    return None


def _mark_snowball_external_sync_failure(config_id: int, message: str) -> None:
    def write_failure():
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

    _write_snowball_main_db_with_retry("mark Snowball external sync failure", write_failure)


async def _sync_one_snowball_external_target(item: Dict[str, Any], *, trigger_source: str) -> Dict[str, Any]:
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if not _last_token_refresh_time or (now - _last_token_refresh_time).total_seconds() >= 3600:
        asyncio.create_task(_refresh_xueqiu_guest_token_task(item.get("account_id"), item.get("cookie")))

    holdings = await fetch_xueqiu_holdings(item["combination_id"], item.get("cookie"))
    rebalance_prices = await fetch_xueqiu_latest_rebalance_prices(item["combination_id"], item.get("cookie"))
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
        has_old_target = xq_symbol in current_targets
        old_quantity = safe_int(old_target.get("target_quantity"))
        old_weight = safe_float(old_target.get("target_weight_pct"), None)
        weight = safe_float(target_weights.get(xq_symbol))
        price = safe_float(quotes.get(xq_symbol))
        latest_target_value = base_value * (weight / 100.0)
        final_quantity = old_quantity
        accepted_weight = old_weight if old_weight is not None else weight

        if weight <= 0:
            final_quantity = 0
            accepted_weight = 0.0
            if old_quantity != 0 or safe_float(old_weight) != 0:
                changed = True
        elif price <= 0:
            skipped.append({"symbol": trade_symbol, "message": "缺少雪球行情价格，保留原目标仓位"})
            if not has_old_target:
                continue
            accepted_weight = old_weight if old_weight is not None else 0.0
        else:
            candidate_quantity = _snowball_target_quantity(latest_target_value, price, xq_symbol)
            should_recalculate = _should_recalculate_snowball_target(
                has_old_target=has_old_target,
                old_quantity=old_quantity,
                old_weight=old_weight,
                new_weight=weight,
                candidate_quantity=candidate_quantity,
                price=price,
                base_value=base_value,
                threshold_pct=threshold_pct,
            )
            if should_recalculate:
                final_quantity = candidate_quantity
                accepted_weight = weight
                if old_quantity != final_quantity or old_weight is None or abs(weight - old_weight) > 1e-9:
                    changed = True

        target_value = base_value * (accepted_weight / 100.0)

        reference_price = _snowball_reference_price(
            xq_symbol=xq_symbol,
            old_quantity=old_quantity,
            final_quantity=final_quantity,
            rebalance_prices=rebalance_prices,
            old_reference_price=old_target.get("reference_price"),
        )
        reference_price_source = "snowball_latest_rebalance_fill_price" if reference_price else None

        row_target_value = target_value if weight > 0 else 0.0
        if price > 0 and final_quantity > 0:
            row_target_value = final_quantity * price
        target_rows.append({
            "symbol": trade_symbol,
            "target_quantity": final_quantity,
            "target_weight_pct": accepted_weight,
            "target_value": round(row_target_value, 2),
            "reference_price": reference_price,
            "reference_price_source": reference_price_source,
        })

    signal_version = _build_snowball_target_signal_version(item, target_rows)

    def write_sync_result():
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
            if changed:
                db.add(SnowballCopyLog(
                    cli_id=config.cli_id,
                    combination_id=config.combination_id,
                    action="TARGET_SYNC",
                    quantity=len(target_rows),
                    status="TARGET_SYNCED",
                    message=config.last_external_sync_message,
                    account_id=config.account_id,
                ))

    _write_snowball_main_db_with_retry("persist Snowball external sync result", write_sync_result)

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
            try:
                _mark_snowball_external_sync_failure(item.get("id"), error_message)
            except Exception as mark_exc:
                if _is_sqlite_lock_error(mark_exc):
                    logger.warning(
                        "Snowball external sync failure marker skipped after SQLite lock: config=%s error=%s",
                        item.get("id"),
                        mark_exc,
                    )
                else:
                    logger.exception(
                        "Snowball external sync failure marker failed: config=%s",
                        item.get("id"),
                    )

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


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo:
        parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return parsed


def _snowball_backtest_run_response(run: SnowballBacktestRun) -> SnowballBacktestRunResponse:
    return SnowballBacktestRunResponse(
        id=run.id,
        config_id=run.config_id,
        combination_id=run.combination_id,
        combination_name=run.combination_name,
        status=run.status,
        slippage_pct=safe_float(run.slippage_pct),
        requested_start_date=run.requested_start_date,
        requested_end_date=run.requested_end_date,
        effective_start_date=run.effective_start_date,
        actual_nav_start=run.actual_nav_start,
        actual_nav_end=run.actual_nav_end,
        actual_rebalance_start=run.actual_rebalance_start,
        benchmark_symbol=run.benchmark_symbol,
        benchmark_name=run.benchmark_name,
        performance_raw=run.performance_raw,
        performance_after_slippage=run.performance_after_slippage,
        benchmark_metrics=run.benchmark_metrics,
        slippage=run.slippage,
        comparison=run.comparison,
        rebalancing=run.rebalancing,
        rebalance_fetch=run.rebalance_fetch,
        yearly_returns=run.yearly_returns or [],
        error_message=run.error_message,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        updated_at=run.updated_at,
    )


def _publish_snowball_backtest_run(run: SnowballBacktestRun) -> None:
    publish_event(
        run.account_id,
        "snowball_backtest",
        _snowball_backtest_run_response(run).dict(),
    )


def _mark_snowball_backtest_failed(run_id: int, message: str) -> None:
    with get_db_ctx() as db:
        run = db.query(SnowballBacktestRun).filter(SnowballBacktestRun.id == run_id).first()
        if not run:
            return
        now = datetime.now()
        run.status = "FAILED"
        run.error_message = (message or "回测失败")[:4000]
        run.completed_at = now
        run.updated_at = now
        db.add(SnowballCopyLog(
            cli_id="",
            combination_id=run.combination_id,
            action="BACKTEST",
            status="FAILED",
            message=run.error_message[:1000],
            account_id=run.account_id,
        ))
        _publish_snowball_backtest_run(run)


def _store_snowball_backtest_result(run_id: int, result: Dict[str, Any]) -> None:
    with get_db_ctx() as db:
        run = db.query(SnowballBacktestRun).filter(SnowballBacktestRun.id == run_id).first()
        if not run:
            return

        run.status = "SUCCESS"
        run.combination_name = result.get("cube_name") or run.combination_name
        run.requested_start_date = _parse_iso_date(result.get("requested_start_date"))
        run.requested_end_date = _parse_iso_date(result.get("requested_end_date"))
        run.effective_start_date = _parse_iso_date(result.get("effective_start_date"))
        run.actual_nav_start = _parse_iso_date(result.get("actual_nav_start"))
        run.actual_nav_end = _parse_iso_date(result.get("actual_nav_end"))
        run.actual_rebalance_start = _parse_iso_datetime(result.get("actual_rebalance_start"))
        run.benchmark_symbol = result.get("benchmark_symbol")
        run.benchmark_name = result.get("benchmark_name")
        run.performance_raw = result.get("performance_raw")
        run.performance_after_slippage = result.get("performance_after_slippage")
        run.benchmark_metrics = result.get("benchmark_metrics")
        run.slippage = result.get("slippage")
        run.comparison = result.get("comparison")
        run.rebalancing = result.get("rebalancing")
        run.rebalance_fetch = result.get("rebalance_fetch")
        run.yearly_returns = result.get("yearly_returns") or []
        run.error_message = None
        now = datetime.now()
        run.completed_at = now
        run.updated_at = now

        db.query(SnowballBacktestCurvePoint).filter(
            SnowballBacktestCurvePoint.run_id == run.id
        ).delete(synchronize_session=False)
        for point in result.get("curve_points") or []:
            point_date = _parse_iso_date(point.get("date"))
            if not point_date:
                continue
            db.add(SnowballBacktestCurvePoint(
                run_id=run.id,
                date=point_date,
                raw_nav=safe_float(point.get("raw_nav"), None),
                slippage_nav=safe_float(point.get("slippage_nav"), None),
                benchmark_nav=safe_float(point.get("benchmark_nav"), None),
                raw_return_pct=safe_float(point.get("raw_return_pct"), None),
                slippage_return_pct=safe_float(point.get("slippage_return_pct"), None),
                benchmark_return_pct=safe_float(point.get("benchmark_return_pct"), None),
                raw_drawdown_pct=safe_float(point.get("raw_drawdown_pct"), None),
                slippage_drawdown_pct=safe_float(point.get("slippage_drawdown_pct"), None),
                benchmark_drawdown_pct=safe_float(point.get("benchmark_drawdown_pct"), None),
                slippage_cost_pct=safe_float(point.get("slippage_cost_pct"), None),
            ))

        total_return = (run.performance_after_slippage or {}).get("total_return_pct")
        message = (
            f"雪球组合回测完成：单边滑点 {run.slippage_pct:.2f}%，"
            f"滑点后总收益 {total_return:.2f}%"
            if total_return is not None
            else f"雪球组合回测完成：单边滑点 {run.slippage_pct:.2f}%"
        )
        db.add(SnowballCopyLog(
            cli_id="",
            combination_id=run.combination_id,
            action="BACKTEST",
            status="SUCCESS",
            message=message,
            account_id=run.account_id,
        ))
        _publish_snowball_backtest_run(run)


def _run_snowball_backtest_task(run_id: int) -> None:
    try:
        with get_db_ctx() as db:
            run = db.query(SnowballBacktestRun).filter(SnowballBacktestRun.id == run_id).first()
            if not run:
                return
            config = db.query(SnowballCopyConfig).filter(
                SnowballCopyConfig.id == run.config_id,
                SnowballCopyConfig.account_id == run.account_id,
            ).first()
            if not config:
                raise ValueError("雪球跟单配置不存在")
            account_config = db.query(SnowballAccountConfig).filter_by(account_id=run.account_id).first()
            cookie = account_config.xueqiu_cookie if account_config else None
            if not cookie:
                raise ValueError("未配置雪球全局 Cookie")
            now = datetime.now()
            run.status = "RUNNING"
            run.started_at = run.started_at or now
            run.updated_at = now
            cube_symbol = config.combination_id
            slippage_pct = safe_float(run.slippage_pct)
            _publish_snowball_backtest_run(run)

        result = run_snowball_cube_backtest(
            cube_symbol=cube_symbol,
            cookie=cookie,
            slippage_pct=slippage_pct,
        )
        _store_snowball_backtest_result(run_id, result)
    except Exception as exc:
        logger.exception("Snowball backtest failed: run_id=%s", run_id)
        _mark_snowball_backtest_failed(run_id, str(exc))


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
        data.xueqiu_cookie = _normalize_xueqiu_cookie(cookie)

    config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    now = datetime.now()
    if not config:
        config = SnowballAccountConfig(account_id=account_id, xueqiu_cookie=data.xueqiu_cookie, updated_at=now)
        db.add(config)
    else:
        config.xueqiu_cookie = data.xueqiu_cookie
        config.updated_at = now
    db.commit()
    return {"message": "Success", "updated_at": now.isoformat()}

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

    raw_cookie = (data.xueqiu_cookie or "").strip()
    if not raw_cookie or data.login_detected is False or data.token_present is False:
        from ...core.services.xueqiu_token_monitor import send_xueqiu_token_login_missing_alert

        alert_sent = send_xueqiu_token_login_missing_alert(
            account_id=account_id,
            source=data.source,
            status_message=data.status_message,
        )
        return {
            "message": "Missing xq_a_token status accepted",
            "status": "LOGIN_MISSING",
            "alert_sent": alert_sent,
            "updated_at": datetime.now().isoformat(),
        }

    try:
        normalized_cookie = _normalize_xueqiu_cookie(raw_cookie)
    except HTTPException:
        from ...core.services.xueqiu_token_monitor import send_xueqiu_token_login_missing_alert

        alert_sent = send_xueqiu_token_login_missing_alert(
            account_id=account_id,
            source=data.source,
            status_message=data.status_message or "Cookie payload missing xq_a_token",
        )
        return {
            "message": "Missing xq_a_token status accepted",
            "status": "LOGIN_MISSING",
            "alert_sent": alert_sent,
            "updated_at": datetime.now().isoformat(),
        }

    config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    now = datetime.now()
    if not config:
        config = SnowballAccountConfig(account_id=account_id, xueqiu_cookie=normalized_cookie, updated_at=now)
        db.add(config)
    else:
        config.xueqiu_cookie = normalized_cookie
        config.updated_at = now
    db.commit()
    return {"message": "Success", "updated_at": now.isoformat()}

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
    db_config.cli_id = ""

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


@router.post("/configs/{config_id}/backtests", response_model=SnowballBacktestRunResponse)
async def start_config_backtest(
    config_id: int,
    request: SnowballBacktestStartRequest,
    background_tasks: BackgroundTasks,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = db.query(SnowballCopyConfig).filter(
        SnowballCopyConfig.id == config_id,
        SnowballCopyConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")

    slippage_pct = safe_float(request.slippage_pct, 0.1)
    if slippage_pct < 0 or slippage_pct > 10:
        raise HTTPException(status_code=400, detail="滑点必须在 0% 到 10% 之间")

    account_config = db.query(SnowballAccountConfig).filter_by(account_id=account_id).first()
    if not account_config or not account_config.xueqiu_cookie:
        raise HTTPException(status_code=400, detail="请先配置雪球全局 Cookie")

    now = datetime.now()
    run = SnowballBacktestRun(
        account_id=account_id,
        config_id=config.id,
        combination_id=config.combination_id,
        combination_name=config.combination_name,
        status="RUNNING",
        slippage_pct=slippage_pct,
        benchmark_symbol="000905.SH",
        benchmark_name="中证500",
        created_at=now,
        started_at=now,
        updated_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    _publish_snowball_backtest_run(run)
    background_tasks.add_task(_run_snowball_backtest_task, run.id)
    return _snowball_backtest_run_response(run)


@router.get("/configs/{config_id}/backtests", response_model=List[SnowballBacktestRunResponse])
async def list_config_backtests(
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

    runs = db.query(SnowballBacktestRun).filter(
        SnowballBacktestRun.account_id == account_id,
        SnowballBacktestRun.config_id == config_id,
    ).order_by(SnowballBacktestRun.created_at.desc(), SnowballBacktestRun.id.desc()).limit(50).all()
    return [_snowball_backtest_run_response(run) for run in runs]


@router.get("/backtests/{run_id}", response_model=SnowballBacktestDetailResponse)
async def get_backtest_detail(
    run_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    run = db.query(SnowballBacktestRun).filter(
        SnowballBacktestRun.id == run_id,
        SnowballBacktestRun.account_id == account_id,
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest not found")

    points = db.query(SnowballBacktestCurvePoint).filter(
        SnowballBacktestCurvePoint.run_id == run.id,
    ).order_by(SnowballBacktestCurvePoint.date.asc()).all()
    base = _snowball_backtest_run_response(run).dict()
    base["curve_points"] = [
        SnowballBacktestCurvePointResponse(
            date=point.date,
            raw_nav=point.raw_nav,
            slippage_nav=point.slippage_nav,
            benchmark_nav=point.benchmark_nav,
            raw_return_pct=point.raw_return_pct,
            slippage_return_pct=point.slippage_return_pct,
            benchmark_return_pct=point.benchmark_return_pct,
            raw_drawdown_pct=point.raw_drawdown_pct,
            slippage_drawdown_pct=point.slippage_drawdown_pct,
            benchmark_drawdown_pct=point.benchmark_drawdown_pct,
            slippage_cost_pct=point.slippage_cost_pct,
        )
        for point in points
    ]
    return SnowballBacktestDetailResponse(**base)


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
    target_rows = trading_db.query(ExternalTradingTargetPosition).filter(
        ExternalTradingTargetPosition.sub_account_id == sub_account.id,
        ExternalTradingTargetPosition.status == "ACTIVE",
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

    target_position_map: Dict[str, Dict[str, Any]] = {}
    for row in target_rows:
        trade_symbol = normalize_trading_symbol(row.symbol)
        if not trade_symbol:
            continue
        target_position_map[trade_symbol] = {
            "reference_price": safe_float(row.reference_price, None),
            "reference_price_source": row.reference_price_source,
        }

    try:
        valuation = await calculate_sub_account_net_asset(trading_db, sub_account, positions=ledger_rows)
    except ExternalTradingValuationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    quotes = await fetch_xueqiu_batch_quotes(sorted(all_xq_symbols), cookie)
    policy_account = trading_db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == sub_account.external_trading_account_id,
        ExternalTradingAccount.account_id == account_id,
    ).first()
    effective_policy = resolve_execution_policy(policy_account, sub_account) if policy_account else {}
    max_slippage_pct = safe_float(effective_policy.get("max_slippage_pct"), 0.5)
    ledger_position_values = {
        normalize_trading_symbol(row.get("symbol")): row
        for row in valuation.get("positions", [])
        if normalize_trading_symbol(row.get("symbol"))
    }
    ledger_cash = safe_float(valuation.get("cash_available"))
    ledger_market_value = safe_float(valuation.get("position_market_value"))
    ledger_net_asset = safe_float(valuation.get("net_asset"))
    all_trade_symbols = set(target_weights.keys()) | set(ledger_positions.keys()) | set(target_position_map.keys())
    symbol_name_map = load_symbol_name_map(all_trade_symbols, db)

    def get_quote_info(trade_symbol: str) -> Dict[str, Any]:
        xq_symbol = _to_xueqiu_symbol(trade_symbol)
        quote = quotes.get(xq_symbol or "", {}) if xq_symbol else {}
        ledger_value = ledger_position_values.get(trade_symbol) or {}
        row = ledger_positions.get(trade_symbol)
        price = safe_float(quote.get("price"), safe_float(ledger_value.get("price")))
        if price <= 0 and row:
            price = safe_float(row.market_price, safe_float(row.avg_cost))
        normalized_symbol = normalize_symbol_for_name(trade_symbol)
        symbol_name = symbol_name_map.get(normalized_symbol or "")
        return {
            "xueqiu_symbol": xq_symbol,
            "name": quote.get("name")
            or target_weights.get(trade_symbol, {}).get("name")
            or symbol_name
            or "",
            "price": price,
        }

    detailed_holdings = []
    target_market_value = 0.0
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
            target_quantity = _snowball_target_quantity(ideal_target_value, price, trade_symbol)
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

        target_meta = target_position_map.get(trade_symbol) or {}
        reference_price = safe_float(target_meta.get("reference_price"), None)
        reference_price_source = target_meta.get("reference_price_source")
        initial_protection_price = None
        execution_protection_price = None
        if reference_price and reference_price > 0:
            initial_protection_price = reference_price
            if quantity_diff > 0:
                execution_protection_price = round(reference_price * (1.0 + max_slippage_pct / 100.0), 4)
            elif quantity_diff < 0:
                execution_protection_price = round(reference_price * (1.0 - max_slippage_pct / 100.0), 4)

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
            reference_price=reference_price,
            reference_price_source=reference_price_source,
            initial_protection_price=initial_protection_price,
            execution_protection_price=execution_protection_price,
            executor_max_slippage_pct=max_slippage_pct,
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
