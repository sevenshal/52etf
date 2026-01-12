from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
import httpx
import logging
import collections
from sqlalchemy.orm import Session
from ...core.database import get_db_session, SnowballCopyConfig, SnowballCopyLog, SnowballPortfolioSnapshot
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
    total_position_ratio: Optional[float] = 100.0
    total_amount: Optional[float] = None
    tracking_error_pct: float = 1.0
    blacklisted_symbols: List[str] = []

class SnowballConfigUpdate(BaseModel):
    cli_id: Optional[str] = None
    combination_id: Optional[str] = None
    combination_name: Optional[str] = None
    enabled: Optional[bool] = None
    total_position_ratio: Optional[float] = None
    total_amount: Optional[float] = None
    tracking_error_pct: Optional[float] = None
    blacklisted_symbols: Optional[List[str]] = None

class SnowballConfigResponse(SnowballConfigCreate):
    id: int
    updated_at: datetime
    snapshot_value: Optional[float] = 0.0
    
    class Config:
        from_attributes = True


class SnowballLogResponse(BaseModel):
    id: int
    cli_id: Optional[str]
    timestamp: datetime
    combination_id: Optional[str]
    action: Optional[str]
    symbol: Optional[str]
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
                raise Exception(f"Xueqiu API Error: {data.get('message', 'Unknown error')}")
        except Exception as e:
            logger.error(f"Failed to fetch Xueqiu holdings: {e}")
            raise e

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
        result = []
        for c in configs:
            resp = SnowballConfigResponse.from_orm(c)
            # Fetch snapshot value
            snapshot = db.query(SnowballPortfolioSnapshot).filter_by(config_id=c.id).first()
            if snapshot:
                resp.snapshot_value = snapshot.market_value
            else:
                resp.snapshot_value = c.total_amount # Fallback or 0
            result.append(resp)
        return result

@router.post("/configs", response_model=SnowballConfigResponse)
async def create_config(
    config: SnowballConfigCreate,
    account_id: str = Depends(valid_account)
):
    with get_db_session(account_id) as db:
        # Check if cli_id exists - REMOVED for Multi-Portfolio Support
        # existing = db.query(SnowballCopyConfig).filter_by(cli_id=config.cli_id).first()
        # if existing:
        #     raise HTTPException(status_code=400, detail="CLI ID already exists")
        
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

@router.get("/logs", response_model=List[SnowballLogResponse])
async def get_logs(
    account_id: str = Depends(valid_account),
    limit: int = 100,
    cli_id: Optional[str] = None
):
    with get_db_session(account_id) as db:
        query = db.query(SnowballCopyLog)
        if cli_id:
            query = query.filter(SnowballCopyLog.cli_id == cli_id)
        logs = query.order_by(SnowballCopyLog.timestamp.desc()).limit(limit).all()
        return [SnowballLogResponse.from_orm(log) for log in logs]

@router.post("/logs/status")
async def update_log_status(
    status_update: SnowballLogStatusUpdate,
    account_id: str = Depends(valid_account)
):
    with get_db_session(account_id) as db:
        log = db.query(SnowballCopyLog).filter_by(id=status_update.id).first()
        if not log:
            raise HTTPException(status_code=404, detail="Log entry not found")
        
        log.status = status_update.status
        if status_update.message:
            log.message = status_update.message
        if status_update.price:
            log.price = status_update.price
            
        db.commit()
        return {"message": "Status updated"}

# --- Core Logic ---

@router.post("/opportunities", response_model=TradeResponse)
async def get_snowball_opportunities(
    request: TradeRequest,
    account_id: str = Depends(valid_account)
):
    """
    Calculate trading opportunities based on Snowball combination holdings.
    External caller provides current positions and cli_id.
    Supports multiple combinations per cli_id using Snapshot tracking.
    """
    cli_id = request.cli_id

    with get_db_session(account_id) as db:
        # 1. Fetch Configs
        _configs_orm = db.query(SnowballCopyConfig).filter_by(cli_id=cli_id, enabled=True).all()
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
        for config in configs:
            weights = await fetch_xueqiu_holdings(config['combination_id'])
            config_target_weights[config['id']] = weights
            for w in weights:
                all_symbols.add(w['symbol'])

        # 2.2 Fetch Prices
        prices = await fetch_xueqiu_quotes(list(all_symbols))
        
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
        is_closing_window = (current_time.hour == 14 and current_time.minute >= 55) or (current_time.hour == 15 and current_time.minute == 0)

        logger.info(f"Current time: {current_time}, Is closing window: {is_closing_window}")

        for config in configs:
            snapshot = db.query(SnowballPortfolioSnapshot).filter_by(config_id=config['id']).first()
            
            # --- Initialize or Calculate Current Snapshot Value ---
            if not snapshot:
                # Init with 0. Wait for adjustment logic to inject capital
                snapshot = SnowballPortfolioSnapshot(
                    config_id=config['id'],
                    holdings={},
                    cash=0.0,
                    market_value=0.0,
                    last_synced_amount=0.0
                )
                db.add(snapshot)
                db.flush() 
            
            # --- Capital Adjustment Logic ---
            target_amt = config['total_amount'] or 0.0
            synced_amt = snapshot.last_synced_amount or 0.0
            diff = target_amt - synced_amt
            
            if abs(diff) > 1e-6: # Float epsilon
                if is_closing_window:
                    snapshot.cash += diff
                    snapshot.last_synced_amount = target_amt
                    logger.info(f"Adjusted capital for Config {config['id']}: {synced_amt} -> {target_amt} (Diff: {diff})")
                    # Force recalc of current value after injection
                    snapshot.market_value += diff 
                else:
                    # Pending adjustment
                    pass

            # Calc Current Market Value of Snapshot
            # Value = Sum(HoldingQty * Price) + Cash
            snap_holdings = snapshot.holdings or {}
            snap_mv = sum(qty * get_price(sym) for sym, qty in snap_holdings.items())
            
            # Base Value for rebalancing is the current portfolio value (including any just-injected cash)
            base_value = snap_mv + snapshot.cash
            
            # --- Calculate New Target State ---
            new_snap_holdings = {}
            used_cash = 0.0
            
            weights = config_target_weights.get(config['id'], [])
            threshold_pct = config['tracking_error_pct'] or 1.0
            
            # Combine all symbols (Current + Target)
            all_snap_symbols = set(snap_holdings.keys())
            target_weights_map = {}
            for item in weights:
                # Blacklist Check: Treat target weight as 0 if blacklisted
                if item['symbol'] in config.get('blacklisted_symbols', []):
                    continue
                    
                all_snap_symbols.add(item['symbol'])
                target_weights_map[item['symbol']] = item['weight']
            
            for sym in all_snap_symbols:
                price = get_price(sym)
                if price <= 0: continue
                
                # 1. Target Value
                w = target_weights_map.get(sym, 0.0)
                target_val = base_value * (w / 100.0)
                
                # 2. Current Value
                cur_q = snap_holdings.get(sym, 0)
                cur_val = cur_q * price
                
                # 3. Deviation Check
                diff_val = target_val - cur_val
                diff_pct_of_total = (abs(diff_val) / base_value) * 100 if base_value > 0 else 100
                
                final_q = cur_q # Default: Keep
                
                # If deviation > threshold, rebalance to Target
                # Special case: If base_value is basically just cash (fresh start), we likely want to buy.
                if diff_pct_of_total >= threshold_pct:
                    raw_q = target_val / price
                    final_q = int(raw_q / 100) * 100
                
                if final_q > 0:
                    new_snap_holdings[sym] = final_q
                    used_cash += final_q * price
                
                # Record Need & Reason per Combo
                snapshot_diff = final_q - cur_q
                if snapshot_diff != 0:
                    s_tgt_pct = (target_val / base_value) * 100 if base_value > 0 else 0
                    s_cur_pct = (cur_val / base_value) * 100 if base_value > 0 else 0
                    # s_diff_pct = ((target_val - cur_val) / base_value) * 100 if base_value > 0 else 0
                    
                    # Concise Reason: "ZH123: 10%->12%"
                    combo_name = config['combination_name'] or config['combination_id'] or str(config['id'])
                    short_name = combo_name[:10] # Truncate if too long
                    need_reason = f"[{short_name}: {s_cur_pct:.1f}%->{s_tgt_pct:.1f}%]"
                    
                    symbol_needs[sym].append(need_reason)

            # Update Snapshot
            snapshot.holdings = new_snap_holdings
            snapshot.cash = base_value - used_cash
            snapshot.market_value = base_value

            # Aggregate Targets
            for sym, qty in new_snap_holdings.items():
                aggregated_target_quantities[sym] = aggregated_target_quantities.get(sym, 0) + qty
                symbol_contributors[sym].add(config['combination_id'])

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
                message=final_message
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
