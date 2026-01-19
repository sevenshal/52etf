import logging
import math
from typing import List, Optional, Dict
from ..core.database import Session, PortfolioCopyConfig, LongPortAccount
from ..core.services.longport import LongPortService
from ..core.services.trade import OrderSide, OrderType, TimeInForceType
from ..core.utils import mask_account_id
import requests
import time

logger = logging.getLogger(__name__)

class LongportCopyTrader:
    """
    长桥跟单交易逻辑
    """
    
    def __init__(self, parent_trader):
        self.parent_trader = parent_trader # Reference to PortfolioCopyTrader for logging/saving

    async def _get_lp_service(self, lp_account_id: str) -> LongPortService:
        # LongPortService handles connection and caching
        return LongPortService.get_instance(lp_account_id)

    async def calculate_rebalance_plan(self, config: PortfolioCopyConfig) -> List[dict]:
        """计算长桥账户的调仓计划"""
        masked_account_id = mask_account_id(config.account_id)
        lp_account_id = config.longport_account_id
        
        if not lp_account_id:
             raise ValueError(f"Longport Account ID not configured for {masked_account_id}")

        logger.info(f"Adding Longport rebalance calculation for {masked_account_id} (LP: {lp_account_id})")
        
        service = await self._get_lp_service(lp_account_id)
        
        # 1. 获取 Futu 目标组合 (Source) - Use parent's method
        futu_records = await self.parent_trader._run_in_executor(
            self.parent_trader.get_futu_positions_sync, 
            config.portfolio_id, 
            config.api_headers or {}
        )
        
        # --- 符号归一化 + 价格提取 (Source) ---
        futu_positions_map = {} # symbol -> target_ratio (0.0 - 1.0)
        futu_price_map = {}
        for r in futu_records:
            symbol = r["stock_code"].replace('US.', '') # Remove US. prefix
            ratio = r["position_ratio"] / 1000000000.0 # Futu returns 14.5% as 145000000
            if ratio > 1.0: 
                ratio /= 100.0
            futu_positions_map[symbol] = futu_positions_map.get(symbol, 0) + ratio
            
            if "current_price" in r and r["current_price"]:
                futu_price_map[symbol] = float(r["current_price"]) / 1000000000.0

        # 2. 获取 Longport 账户信息 (Destination)
        # 资金
        balance = service.account_balance()
        if not balance:
             raise Exception("Failed to fetch Longport account balance")
             
        # Net Liquidation Value roughly = Available + Market Value? Longport API returns Cash Info.
        # Uses simplified assumption: Total Assets ≈ Cash + Market Value of positions.
        # But `account_balance()` only returns cash info. 
        # We need total assets. LongPort SDK `account_balance` returns `AccountBalance`.
        # Looking at service.py, `account_balance` returns dict with `available_balance`.
        # We need a way to get Total Net Assets.
        # Let's check `service.stock_positions()` it might return cost/market value.
        
        positions = service.stock_positions()
        market_value = 0.0
        lp_positions = {} # symbol -> qty
        
        # Extract Longport positions
        for p in positions:
            # Longport symbols usually "AAPL.US", need to strip ".US" or check format.
            # SDK usually returns "AAPL.US". Futu uses "US.AAPL".
            # Clean symbol to "AAPL".
            raw_symbol = p['symbol']
            symbol = raw_symbol.split('.')[0] 
            qty = int(p['quantity'])
            lp_positions[symbol] = qty
            
            # Estimate market value for Net Liq calculation
            # Ideally we have real-time price or market value from position
            # If position info has 'market_value', use it? 
            # The `stock_positions` in service.py returns limited fields.
            # But `p` is originally a `Position` object which has `market_value`?
            # Service only extracts specific fields. 
            # Let's assume we can rely on `available_balance` + sum(qty * price).
            pass

        # We need prices for all involved symbols (Source + Dest)
        all_clean_symbols = set(list(futu_positions_map.keys()) + list(lp_positions.keys()))
        
        # Fetch quotes from Longport for accurate pricing
        # Map clean symbol back to Longport format ("AAPL" -> "AAPL.US")
        lp_symbols_map = {s: f"{s}.US" for s in all_clean_symbols}
        lp_symbols_list = list(lp_symbols_map.values())
        
        quotes = service.get_quote_batch(lp_symbols_list)
        market_prices = {}
        for q in quotes:
            clean_sym = q['symbol'].split('.')[0]
            market_prices[clean_sym] = q['price']
            
        # Fallback to Futu prices if Longport fails
        for s in all_clean_symbols:
            if s not in market_prices and s in futu_price_map:
                market_prices[s] = futu_price_map[s]

        # Calculate Net Liq
        available_cash = float(balance.get('available_balance', 0))
        total_assets = available_cash
        for s, qty in lp_positions.items():
            if s in market_prices:
                total_assets += qty * market_prices[s]
                
        # 3. Calculate Target Amount
        total_target_amount = config.total_amount or (total_assets * config.total_position_ratio / 100.0)
        
        # 4. Generate Plan
        plan = []
        for symbol in all_clean_symbols:
            target_ratio = futu_positions_map.get(symbol, 0)
            current_qty = lp_positions.get(symbol, 0)
            
            # Pending qty? Longport API can fetch open orders.
            # `today_orders` or `history_orders` with status open.
            # For simplicity, we might ignore pending for now or implement `get_pending`.
            # Let's try to get pending.
            pending_qty = 0
            # TODO: Add get_pending to service if needed.
            
            price = market_prices.get(symbol)
            if not price or math.isnan(price) or price <= 0:
                logger.warning(f"Skipping {symbol} due to missing price")
                continue
                
            current_ratio = (current_qty * price) / total_target_amount
            target_qty = int((total_target_amount * target_ratio) / price)
            trade_qty = target_qty - current_qty # Ignoring pending for now
            
            action = "HOLD"
            if target_ratio == 0:
                if current_qty > 0:
                    action = "SELL"
            else:
                 absolute_diff_pct = abs(target_ratio - current_ratio) * 100
                 if absolute_diff_pct > (config.tracking_error_pct or 0):
                     if trade_qty != 0:
                         action = "BUY" if trade_qty > 0 else "SELL"
            
            plan.append({
                "symbol": symbol,
                "action": action,
                "quantity": abs(trade_qty),
                "current_qty": current_qty,
                "pending_qty": 0,
                "target_qty": target_qty,
                "price": price,
                "current_ratio": round(current_ratio * 100, 2),
                "target_ratio": round(target_ratio * 100, 2),
                "lp_symbol": lp_symbols_map.get(symbol) # Helper for execution
            })
            
        return plan

    async def rebalance(self, config: PortfolioCopyConfig):
        """执行调仓"""
        masked_account_id = mask_account_id(config.account_id)
        lp_account_id = config.longport_account_id
        
        try:
            # 1. Calc Plan
            plan_full = await self.calculate_rebalance_plan(config)
            plan = [p for p in plan_full if p["action"] != "HOLD" and p["quantity"] != 0]
            
            if not plan:
                logger.info(f"[{masked_account_id}] No Longport rebalance needed.")
                return

            service = await self._get_lp_service(lp_account_id)
            
            # 2. Execute
            logger.info(f"[{masked_account_id}] Executing Longport plan: {len(plan)} trades")
            for item in plan:
                symbol = item["lp_symbol"] # Use .US symbol
                action = item["action"] # BUY/SELL
                qty = item["quantity"]
                price = item["price"]
                
                side = OrderSide.Buy if action == "BUY" else OrderSide.Sell
                
                try:
                    # Place ID
                    # Use Market Order for simplicity as per IB implementation
                    order_id = service.submit_order(
                        side=side, 
                        symbol=symbol, 
                        order_type=OrderType.MO, # Market 
                        submitted_price=0, # Market
                        submitted_quantity=qty
                    )
                    
                    msg = f"{action} {qty} {symbol} (Market). ID: {order_id}"
                    self.parent_trader._log(
                        config.account_id, config.portfolio_id, action, "SUCCESS", msg, 
                        symbol=symbol, quantity=qty, price=price, config_id=config.id
                    )
                except Exception as e:
                    logger.error(f"Longport Execution failed for {symbol}: {e}")
                    self.parent_trader._log(
                        config.account_id, config.portfolio_id, action, "FAILED", str(e), 
                        symbol=symbol, config_id=config.id
                    )
                    
        except Exception as e:
            logger.error(f"Longport Rebalance failed: {e}")
            self.parent_trader._log(config.account_id, config.portfolio_id, "SYSTEM_ERROR", "FAILED", str(e), config_id=config.id)
