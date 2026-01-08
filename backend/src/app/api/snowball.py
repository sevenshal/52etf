from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
import httpx
import logging
from sqlalchemy.orm import Session
from ...core.database import get_db_session, SnowballCopyConfig, SnowballCopyLog
from .account import valid_account
from .trade import TradeRequest

router = APIRouter(prefix="/api/snowball")
logger = logging.getLogger(__name__)

# --- Constants ---
XUEQIU_HEADERS = {
    "Host": "api.xueqiu.com",
    "Cookie": "xq_a_token=814ff1069fad352c1e283f4306b012b1b44d3d42;",
    "accept": "application/json",
    "accept-language": "zh-Hans-CN;q=1, en-CN;q=0.9",
    "x-device-os": "iOS 26.1",
    "x-device-model-name": "iPhone 16 Pro Max_iPhone17,2",
    "user-agent": "Xueqiu iPhone 14.81.1",
    "priority": "u=3, i"
}

XUEQIU_STOCK_HEADERS = XUEQIU_HEADERS.copy()
XUEQIU_STOCK_HEADERS["Host"] = "stock.xueqiu.com"

# --- Models ---

class SnowballConfigCreate(BaseModel):
    cli_id: str
    combination_id: str
    combination_name: Optional[str] = None
    enabled: bool = True
    total_position_ratio: float = 100.0
    total_amount: Optional[float] = None
    tracking_error_pct: float = 1.0

class SnowballConfigUpdate(BaseModel):
    cli_id: Optional[str] = None
    combination_id: Optional[str] = None
    combination_name: Optional[str] = None
    enabled: Optional[bool] = None
    total_position_ratio: Optional[float] = None
    total_amount: Optional[float] = None
    tracking_error_pct: Optional[float] = None

class SnowballConfigResponse(SnowballConfigCreate):
    id: int
    updated_at: datetime
    
    class Config:
        from_attributes = True

class TradeResponse(BaseModel):
    opportunities: List[Any]
    msg: Optional[str] = None

# --- Helpers ---

def normalize_symbol(symbol: str) -> str:
    """Normalize symbol to SH.xxxxxx format from SHxxxxxx"""
    if not symbol: 
        return symbol
    if "." in symbol:
        return symbol
    if len(symbol) > 2 and (symbol.startswith("SH") or symbol.startswith("SZ")):
        return f"{symbol[:2]}.{symbol[2:]}"
    return symbol

async def fetch_xueqiu_holdings(symbol: str) -> List[Dict]:
    """Fetch holdings from Xueqiu API"""
    url = f"https://api.xueqiu.com/cube/center/cube/holdSymbols.json?symbol={symbol}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=XUEQIU_HEADERS)
            response.raise_for_status()
            data = response.json()
            if data.get("result_code") == 0 and data.get("success"):
                holdings = data.get("data", [])
                for h in holdings:
                    h['symbol'] = normalize_symbol(h.get('symbol'))
                return holdings
            else:
                logger.error(f"Xueqiu API Error (Holdings): {data}")
                return []
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu holdings: {e}")
            return []

async def fetch_xueqiu_cube_info(symbol: str) -> Optional[Dict]:
    """Fetch cube info including name from Xueqiu"""
    url = f"https://api.xueqiu.com/cubes/show.json?symbol={symbol}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=XUEQIU_HEADERS)
            response.raise_for_status()
            data = response.json()
            # Response format: {"id":..., "name": "...", "symbol": "...", ...}
            # Or sometimes {"id":..., "name": "..."} at root level
            if "name" in data:
                return data
            else:
                logger.error(f"Xueqiu API Error (Info): {data}")
                return None
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu cube info: {e}")
            return None

async def fetch_xueqiu_quotes(symbols: List[str]) -> Dict[str, float]:
    """Fetch real-time quotes for a list of symbols"""
    if not symbols:
        return {}
    
    # URL encode comma is %2C, but usually httpx handles params or we can just join
    # Map dotted to raw for API call (SH.600 -> SH600)
    raw_to_dotted = {s.replace(".", ""): s for s in symbols}
    raw_symbol_str = ",".join(raw_to_dotted.keys())
    
    url = f"https://stock.xueqiu.com/v5/stock/realtime/quotec.json?symbol={raw_symbol_str}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=XUEQIU_STOCK_HEADERS)
            response.raise_for_status()
            data = response.json()
            # Format: {"data":[{"symbol":"SZ000858","current":107.46 ...}]}
            result = {}
            if "data" in data:
                for item in data["data"]:
                    raw_sym = item["symbol"]
                    # Map back to dotted symbol if possible
                    dotted_sym = raw_to_dotted.get(raw_sym, raw_sym)
                    result[dotted_sym] = item["current"]
            return result
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu quotes: {e}")
            return {}

@router.get("/info/{symbol}")
async def get_combination_info(
    symbol: str, 
    account_id: str = Depends(valid_account)
):
    """Get combination info (name) from Xueqiu"""
    info = await fetch_xueqiu_cube_info(symbol)
    if not info:
         raise HTTPException(status_code=404, detail="Combination not found or Xueqiu API error")
    return info


# --- Endpoints ---

@router.get("/configs", response_model=List[SnowballConfigResponse])
async def list_configs(account_id: str = Depends(valid_account)):
    with get_db_session(account_id) as db:
        configs = db.query(SnowballCopyConfig).all()
        return [SnowballConfigResponse.from_orm(c) for c in configs]

@router.post("/configs", response_model=SnowballConfigResponse)
async def create_config(
    config: SnowballConfigCreate,
    account_id: str = Depends(valid_account)
):
    with get_db_session(account_id) as db:
        # Check if cli_id exists
        existing = db.query(SnowballCopyConfig).filter_by(cli_id=config.cli_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="CLI ID already exists")
        
        # Auto-fetch name if not provided
        if not config.combination_name:
            cube_info = await fetch_xueqiu_cube_info(config.combination_id)
            if cube_info:
                config.combination_name = cube_info.get("name")
            
        db_config = SnowballCopyConfig(**config.dict())
        db.add(db_config)
        db.commit()
        db.refresh(db_config)
        return SnowballConfigResponse.from_orm(db_config)

@router.put("/configs/{config_id}", response_model=SnowballConfigResponse)
async def update_config(
    config_id: int,
    config_update: SnowballConfigUpdate,
    account_id: str = Depends(valid_account)
):
    with get_db_session(account_id) as db:
        db_config = db.query(SnowballCopyConfig).filter_by(id=config_id).first()
        if not db_config:
            raise HTTPException(status_code=404, detail="Config not found")
            
        update_data = config_update.dict(exclude_unset=True)
        
        # If updating combination_id but not name, try to fetch name
        if "combination_id" in update_data and "combination_name" not in update_data:
             cube_info = await fetch_xueqiu_cube_info(update_data["combination_id"])
             if cube_info:
                 update_data["combination_name"] = cube_info.get("name")
                 
        for key, value in update_data.items():
            setattr(db_config, key, value)
            
        db.commit()
        db.refresh(db_config)
        return SnowballConfigResponse.from_orm(db_config)

@router.delete("/configs/{config_id}")
async def delete_config(
    config_id: int,
    account_id: str = Depends(valid_account)
):
    with get_db_session(account_id) as db:
        db.query(SnowballCopyConfig).filter_by(id=config_id).delete()
        return {"message": "Deleted successfully"}

# --- Core Logic ---

@router.post("/opportunities", response_model=TradeResponse)
async def get_snowball_opportunities(
    request: TradeRequest,
    account_id: str = Depends(valid_account)
):
    """
    Calculate trading opportunities based on Snowball combination holdings.
    External caller provides current positions and cli_id.
    """
    cli_id = request.cli_id

    with get_db_session(account_id) as db:
        config = db.query(SnowballCopyConfig).filter_by(cli_id=cli_id).first()
        if not config or not config.enabled:
            return TradeResponse(opportunities=[], msg="Configuration not found or disabled")

        # 1. Fetch Target Holdings from Xueqiu
        target_holdings_raw = await fetch_xueqiu_holdings(config.combination_id)
        if not target_holdings_raw:
             return TradeResponse(opportunities=[], msg="Failed to fetch target holdings from Xueqiu")

        # 2. Determine Total Value for Calculation
        # Priority: Configured Total Amount > Portfolio Value form Request
        total_value = config.total_amount if config.total_amount else request.portfolio.portfolio_value
        
        # Apply Total Position Ratio
        effective_total_value = total_value * (config.total_position_ratio / 100.0)

        # 3. Calculate Target Positions (Value)
        target_positions = {} # symbol -> target_value
        all_symbols = set()
        
        for holding in target_holdings_raw:
            symbol = holding['symbol'] # e.g. SH603722
            weight = holding['weight'] # percentage, e.g. 7.11
            target_value = effective_total_value * (weight / 100.0)
            target_positions[symbol] = target_value
            all_symbols.add(symbol)

        # 4. Calculate Current Positions (Quantity) and Collect Symbols
        current_quantities = {} # symbol -> quantity
        for pos in request.positions:
             current_quantities[pos.symbol] = pos.quantity
             all_symbols.add(pos.symbol) # Add held symbols to fetch price too

        # 5. Fetch Real-time Prices
        prices = await fetch_xueqiu_quotes(list(all_symbols))

        # 6. Generate Opportunities
        opportunities = []
        
        for symbol in all_symbols:
            price = prices.get(symbol)
            
            # Fallback to cost_price if market price unavailable (and we hold it)
            if not price:
                 pos = next((p for p in request.positions if p.symbol == symbol), None)
                 if pos:
                     price = pos.cost_price
            
            if not price:
                continue
                
            qty = current_quantities.get(symbol, 0)
            current_val = qty * price
            target_val = target_positions.get(symbol, 0.0)
            diff_val = target_val - current_val
            
            total_config_val = effective_total_value if effective_total_value > 0 else 1.0
            diff_pct = (diff_val / total_config_val) * 100.0
            
            if abs(diff_pct) < config.tracking_error_pct:
                continue 
                
            action = "BUY" if diff_val > 0 else "SELL"
            
            # Round to 100 lots (Standard A-share logic)
            # STAR Market (SH.688) has special rule: min 200 shares.
            is_star = symbol.startswith("SH.688")
            min_qty = 200 if is_star else 100
            
            raw_qty = abs(diff_val) / price
            # We still keep 100 increments for simplicity/safety across boards
            qty = int(raw_qty / 100) * 100
            
            if qty < min_qty:
                continue
                
            ops_info = {
                "symbol": symbol,
                "name": "", # Could be populated if we fetched stock names
                "action": action,
                "quantity": qty,
                "reason": f"Target: {target_val:.2f}, Current: {current_val:.2f}, Diff%: {diff_pct:.2f}%, Price: {price}"
            }
            opportunities.append(ops_info)
            
        opportunities.sort(key=lambda x: 0 if x["action"] == "SELL" else 1)

        logger.info(f"/opportunities request: ${str(request)} \ntarget_holdings_raw: ${target_holdings_raw}\nopportunities: ${str(opportunities)}")

        return TradeResponse(opportunities=opportunities, msg="Success")
