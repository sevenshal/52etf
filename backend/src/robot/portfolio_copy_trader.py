# ... existing imports ...
import asyncio
import threading
import time
import logging
import requests
import math
import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from zoneinfo import ZoneInfo
from croniter import croniter
from ..core.database import Session, PortfolioCopyConfig, PortfolioCopyLog, IBKRAccountConfig
from ..core.utils import mask_account_id, send_alert_email
import traceback
from ..core.services.ib_service import IBKRService, IBOrderSubmissionPending
from ..core.services.market import MarketService
from ..core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    get_external_trading_db_ctx,
)
from ..core.services.external_trading_executor import trigger_external_trading_executor
from ..core.services.external_trading_ledger import (
    STRATEGY_PORTFOLIO_COPY,
    safe_float,
    safe_int,
    sync_target_positions,
)
from ..core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_US_STOCK,
    normalize_external_trading_market_type,
)
from ..core.services.external_trading_valuation import calculate_sub_account_net_asset
from ..core.utils import normalize_us_equity_symbol

logger = logging.getLogger(__name__)

SUPPORTED_PORTFOLIO_COPY_PLATFORMS = {"futu", "star_wealth", "yingli"}

class PortfolioCopyTrader:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(PortfolioCopyTrader, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.is_running = False
        self._thread_started = False
        self._thread_lock = threading.Lock()
        self.task_queue = None
        self._processing_keys = set()
        self.ib_services: Dict[str, IBKRService] = {} # Key: "port_clientid"
        self.worker_loop_obj = None # Capture the worker loop
        
        # Initialize sub-traders
        from .longport_copy_trader import LongportCopyTrader
        self.longport_trader = LongportCopyTrader(self)

    def _log(self, account_id: str, portfolio_id: str, action: str, status: str, message: str, symbol: str = None, quantity: float = None, price: float = None, config_id: int = None):
        db = Session()
        try:
            log = PortfolioCopyLog(
                account_id=account_id,
                portfolio_id=portfolio_id,
                config_id=config_id,
                action=action,
                status=status,
                message=message,
                symbol=symbol,
                quantity=quantity,
                price=price
            )
            db.add(log)
            db.commit()
        except Exception as e:
            logger.error(f"Failed to save copy trading log: {e}")
        finally:
            db.close()

    async def _run_in_executor(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    async def get_portfolio_info(self, portfolio_id: str, headers: dict, platform: str = 'futu') -> dict:
        """异步包装器：获取组合信息"""
        if platform == 'star_wealth':
             return await self._run_in_executor(self.get_starwealth_portfolio_info_sync, portfolio_id, headers)
        elif platform == 'yingli':
             return await self._run_in_executor(self.get_yingli_portfolio_info_sync, portfolio_id, headers)
        return await self._run_in_executor(self.get_portfolio_info_sync, portfolio_id, headers)

    def get_portfolio_info_sync(self, portfolio_id: str, headers: dict) -> dict:
        """从 Futu API 获取组合信息 (同步方法，供 executor 调用)"""
        url = f"https://portfolio.futunn.com/portfolio-api/get-portfolio-info?portfolio_id={portfolio_id}&_={int(time.time() * 1000)}"
        
        # Merge defaults with provided headers
        final_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "Referer": f"https://portfolio.futunn.com/portfolio/{portfolio_id}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "sec-ch-ua": "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\", \"Google Chrome\";v=\"144\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"macOS\""
        }
        if headers:
            final_headers.update(headers)
            
        try:
            response = requests.get(url, headers=final_headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                return data["data"]
            else:
                raise Exception(f"Futu API error: {data.get('message')}")
        except Exception as e:
            # Log full response text for debugging 439/403 errors
            error_details = response.text if 'response' in locals() and response else "No response content"
            logger.error(f"Failed to fetch Futu portfolio info: {e}. Response: {error_details}")
            raise

    def get_futu_positions_sync(self, portfolio_id: str, headers: dict) -> List[dict]:
        """从 Futu API 获取持仓 (同步方法，供 executor 调用)"""
        url = f"https://portfolio.futunn.com/portfolio-api/get-portfolio-position?portfolio_id={portfolio_id}&language=0&_={int(time.time() * 1000)}"
        
        # Merge defaults with provided headers
        final_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "Referer": f"https://portfolio.futunn.com/portfolio/{portfolio_id}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "sec-ch-ua": "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\", \"Google Chrome\";v=\"144\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"macOS\""
        }
        if headers:
            final_headers.update(headers)
            
        try:
            response = requests.get(url, headers=final_headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                return data["data"]["record_items"]
            else:
                raise Exception(f"Futu API error: {data.get('message')}")
        except Exception as e:
            # Log full response text for debugging 439/403 errors
            error_details = response.text if 'response' in locals() and response else "No response content"
            logger.error(f"Failed to fetch Futu positions: {e}. Response: {error_details}")
            raise

    def get_starwealth_portfolio_info_sync(self, portfolio_id: str, headers: dict) -> dict:
        """Fetch portfolio info from StarWealth (Fosun) API"""
        # uin can be hardcoded or passed if available, using 2617074 as example from curl if needed, but trying without first or with default
        url = f"https://tapi.fosunhanig.com/followInvest/v1/PortfolioBasicInfo?id={portfolio_id}&uin=2617074"
        
        default_headers = {
             "accept": "application/json, text/plain, */*",
             "content-type": "application/json",
             "origin": "https://h5.fotechwealth.com",
             "referer": "https://h5.fotechwealth.com/",
             "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
             "x-device-info": "platform=Mac OS;osver=10.15.7;model=Macintosh;browser=Chrome;brover=144;lang=zhCn;channel=H5;",
             "x-lang": "zhCn",
             "x-product": "product=app-wealth;version=0.0.1",
             "x-source": "102",
             "priority": "u=1, i"
        }
        
        if headers:
            default_headers.update(headers)
            
        try:
            response = requests.get(url, headers=default_headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            result = data.get("result", {})
            if not result:
                 raise Exception(f"StarWealth API error: No result in response: {data}")
            
            return {
                "name": result.get("portfolioName", f"SW-{portfolio_id}"),
                "id": result.get("portfolioId", portfolio_id),
                "founder_name": result.get("creatorNick", ""),
                "brief": result.get("portfolioBrief", ""),
                "raw_data": result
            }
                 
        except Exception as e:
             logger.error(f"Failed to fetch StarWealth portfolio info: {e}")
             raise

    def get_starwealth_positions_sync(self, portfolio_id: str, headers: dict) -> List[dict]:
        """Fetch positions from StarWealth (Fosun) API"""
        url = f"https://tapi.fosunhanig.com/followInvest/v1/PortfolioHoldingAllocation?id={portfolio_id}&queryType=1"
        
        default_headers = {
             "accept": "application/json, text/plain, */*",
             "content-type": "application/json",
             "origin": "https://h5.fotechwealth.com",
             "referer": "https://h5.fotechwealth.com/",
             "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
             "x-device-info": "platform=Mac OS;osver=10.15.7;model=Macintosh;browser=Chrome;brover=144;lang=zhCn;channel=H5;",
             "x-lang": "zhCn",
             "x-product": "product=app-wealth;version=0.0.1",
             "x-source": "102",
             "priority": "u=1, i"
        }
        
        if headers:
            default_headers.update(headers)
            
        try:
            response = requests.get(url, headers=default_headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "result" not in data:
                 raise Exception(f"StarWealth API error: No result in response: {data}")
                 
            holdings = []
            result_data = data.get("result", {})
            detail = result_data.get("detail", {})
            
            # 优先从 marketHolding 中获取 stockUs 的持仓
            # Calculate holdings from marketHearing (specifically stockUs as requested)
            market_holdings = detail.get("marketHolding", [])
            
            for mh in market_holdings:
                # 只取 stockUs，或者如果有 stockHk 也可以考虑，但用户明确指出了 stockUs
                if mh.get("marketType") == "stockUs":
                    for stock in mh.get("holding", []):
                        try:
                            ratio_val = float(stock.get("ratio", 0))
                        except:
                            ratio_val = 0
                            
                        stock_item = {
                            "symbol": stock.get("symbol"),
                            "market": stock.get("market"),
                            "ratio_pct": ratio_val,
                            "price": float(stock.get("latestPrice", 0) or 0)
                        }
                        holdings.append(stock_item)
                    break # Assuming only one stockUs entry
            return holdings
                 
        except Exception as e:
             logger.error(f"Failed to fetch StarWealth positions: {e}")
             raise

    def get_yingli_portfolio_info_sync(self, portfolio_id: str, headers: dict) -> dict:
        invest_id = headers.get("investId", "")
        url = f"https://jy.yxzq.com/ams-center/api/v1/follow-invest-detail?strategyId={portfolio_id}&investId={invest_id}"
        
        default_headers = {
            "Host": "jy.yxzq.com",
            "X-Lang": "1",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko)",
            "Accept": "application/json, text/plain, */*"
        }
        if headers:
            for k, v in headers.items():
                if k not in ["investId"]:
                    default_headers[k] = v
                    
        try:
            response = requests.get(url, headers=default_headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0 and "data" in data:
                return {
                    "name": data["data"].get("name", f"Yingli-{portfolio_id}"),
                    "id": portfolio_id,
                    "founder_name": data["data"].get("userName", ""),
                    "brief": data["data"].get("description", ""),
                    "raw_data": data["data"]
                }
            else:
                 raise Exception(f"Yingli API error: {data.get('msg')}")
        except Exception as e:
            logger.error(f"Failed to fetch Yingli portfolio info: {e}")
            raise

    def get_yingli_positions_sync(self, portfolio_id: str, headers: dict) -> List[dict]:
        invest_id = headers.get("investId", "")
        url = f"https://jy.yxzq.com/ams-center/api/v1/follow-invest-detail?strategyId={portfolio_id}&investId={invest_id}"
        
        default_headers = {
            "Host": "jy.yxzq.com",
            "X-Lang": "1",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko)",
            "Accept": "application/json, text/plain, */*"
        }
        if headers:
            for k, v in headers.items():
                if k not in ["investId"]:
                    default_headers[k] = v
                    
        try:
            response = requests.get(url, headers=default_headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0 and "data" in data:
                positions_data = data["data"].get("position", [])
                current_money = float(data["data"].get("currentMoney", 1))
                if current_money <= 0:
                    current_money = 1.0
                
                holdings = []
                for p in positions_data:
                    symbol = p.get("stockCode")
                    market_val = float(p.get("marketValue", 0))
                    ratio_pct = (market_val / current_money) * 100
                    holdings.append({
                        "symbol": symbol,
                        "market": p.get("market"),
                        "ratio_pct": ratio_pct,
                        "price": float(p.get("curPrice", 0))
                    })
                return holdings
            else:
                 raise Exception(f"Yingli API error: {data.get('msg')}")
        except Exception as e:
            logger.error(f"Failed to fetch Yingli positions: {e}")
            raise

    async def _ensure_ib_connected(self, port: int, client_id: int) -> IBKRService:
        """确保在当前线程(Worker)中连接到特定的 IB 账户，并返回该 service 实例"""
        key = f"{port}_{client_id}"
        if key not in self.ib_services:
            logger.info(f"Creating new IBKRService instance for port={port}, client_id={client_id}")
            self.ib_services[key] = IBKRService(port=port, client_id=client_id)
        
        service = self.ib_services[key]
        await service.connect()
        return service

    async def _fetch_portfolio_source_positions(self, config: PortfolioCopyConfig) -> tuple[Dict[str, float], Dict[str, float]]:
        positions_map: Dict[str, float] = {}
        price_map: Dict[str, float] = {}
        platform = getattr(config, 'platform', 'futu') or 'futu'
        if platform not in SUPPORTED_PORTFOLIO_COPY_PLATFORMS:
            raise ValueError(f"Unsupported portfolio copy platform: {platform}")

        if platform == 'star_wealth':
            records = await self._run_in_executor(self.get_starwealth_positions_sync, config.portfolio_id, config.api_headers or {})
            for row in records:
                symbol = str(row["symbol"]).upper()
                ratio = row["ratio_pct"] / 100.0
                positions_map[symbol] = positions_map.get(symbol, 0) + ratio
                if row["price"] > 0:
                    price_map[symbol] = row["price"]
        elif platform == 'yingli':
            records = await self._run_in_executor(self.get_yingli_positions_sync, config.portfolio_id, config.api_headers or {})
            for row in records:
                symbol = str(row["symbol"]).upper()
                ratio = row["ratio_pct"] / 100.0
                positions_map[symbol] = positions_map.get(symbol, 0) + ratio
                if row["price"] > 0:
                    price_map[symbol] = row["price"]
        else:
            futu_records = await self._run_in_executor(self.get_futu_positions_sync, config.portfolio_id, config.api_headers or {})
            for row in futu_records:
                symbol = row["stock_code"].replace('US.', '').upper()
                ratio = row["position_ratio"] / 1000000000.0
                if ratio > 1.0:
                    ratio /= 100.0
                positions_map[symbol] = positions_map.get(symbol, 0) + ratio
                if "current_price" in row and row["current_price"]:
                    price_map[symbol] = float(row["current_price"]) / 1000000000.0

        return positions_map, price_map

    async def calculate_rebalance_plan(self, config: PortfolioCopyConfig, client_id: Optional[int] = None) -> List[dict]:
        """计算调仓计划但不执行 (Should run in worker loop)"""
        masked_account_id = mask_account_id(config.account_id)
        
        # Dispatch to Longport Trader if configured
        if config.account_type == "longport":
             return await self.longport_trader.calculate_rebalance_plan(config)
        if config.account_type == "external":
             plan, _ = await self._build_external_target_plan(config)
             return plan
             
        logger.info(f"Calculating rebalance plan for account {masked_account_id} for portfolio {config.portfolio_id}")
        
        # 1. 获取针对该账户的 IB Service 实例 (多账户持久连接)
        port = config.ib_port
        if config.ib_account_id:
            db = Session()
            try:
                ib_account = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == config.ib_account_id).first()
                if ib_account:
                    port = ib_account.ib_port
                    logger.info(f"Resolved port {port} from account {ib_account.name} (ID: {config.ib_account_id})")
                else:
                    logger.warning(f"IB Account ID {config.ib_account_id} not found, falling back to legacy port {port}")
            finally:
                db.close()
        
        if not port:
             raise ValueError("No IB port configured (neither ib_account_id nor ib_port is valid)")

        ib = await self._ensure_ib_connected(port, client_id)

        plan = []
        try:
            # 1. Fetch and Parse Positions based on Platform
            futu_positions_map, futu_price_map = await self._fetch_portfolio_source_positions(config)

            # 2. IB 状态已经在 _ensure_ib_connected 中准备好
            net_liq = ib.get_net_liquidation()
            if net_liq <= 0:
                raise Exception("IB Account net liquidation is zero or negative")

            # 3. 抓取 IB 实时快照 + 价格 (一次性获取，避免在后续计算中多次触发 API 遍历)
            ib_positions_data = ib.get_positions_dict()  # {symbol: {qty, price}}
            ib_positions = {symbol: data['qty'] for symbol, data in ib_positions_data.items()}
            ib_price_map = {symbol: data['price'] for symbol, data in ib_positions_data.items() if data['price'] is not None}
            
            ib_pending = ib.get_all_pending_qtys()

            # 4. 计算调仓总额 (Target amount for the entire strategy)
            total_target_amount = config.total_amount or (net_liq * config.total_position_ratio / 100.0)
            
            # --- 目标符号集 ---
            # 收集所有我们需要关注的股票代码 (Futu 目标 + IB 当前持仓)
            all_clean_symbols = set(list(futu_positions_map.keys()) + list(ib_positions.keys()))
            
            # 5. 构建价格字典 (优先使用 IB 价格，回退到 Futu 价格)
            market_prices = {}
            for symbol in all_clean_symbols:
                if symbol in ib_price_map:
                    market_prices[symbol] = ib_price_map[symbol]
                elif symbol in futu_price_map:
                    market_prices[symbol] = futu_price_map[symbol]
            
            # 对于缺失价格的股票，批量查询 (这应该很少发生)
            missing_symbols = [s for s in all_clean_symbols if s not in market_prices]
            if missing_symbols:
                logger.warning(f"Fetching missing prices for {len(missing_symbols)} symbols: {missing_symbols}")
                missing_prices = await ib.get_market_prices(missing_symbols)
                market_prices.update(missing_prices)
            
            # 6. 计算调仓方案
            plan = []
            for symbol in all_clean_symbols:
                target_ratio = futu_positions_map.get(symbol, 0)
                
                # 获取该代码的 IB 实时数据 (当前持仓和待成交挂单)
                current_qty = ib_positions.get(symbol, 0)
                pending_qty = ib_pending.get(symbol, 0)
                
                price = market_prices.get(symbol)
                if not price or math.isnan(price) or price <= 0:
                    logger.warning(f"Skipping {symbol} due to missing or invalid price: {price}")
                    continue

                # 当前实际占比 (基于现有持仓和最新价)
                current_ratio = (current_qty * price) / total_target_amount
                
                # 目标股数 = (总目标调仓额 * 目标占比) / 价格
                target_qty = int((total_target_amount * target_ratio) / price)
                # 核心逻辑：需交易股数 = 目标股数 - (当前股数 + 正在路上的股数)
                trade_qty = target_qty - (current_qty + pending_qty)
                
                action = "HOLD"
                # 决策触发逻辑：
                # 1. 如果目标比例为 0，且当前有持仓，强制卖出 (不考虑阈值)
                if target_ratio == 0:
                    if (current_qty + pending_qty) > 0:
                        action = "SELL"
                else:
                    # 2. 如果目标比例 > 0，计算绝对仓位变化比例 (Absolute Position Difference)
                    # 绝对偏差 = abs(目标比例 - 当前比例) * 100 (转换为百分点)
                    absolute_diff_pct = abs(target_ratio - current_ratio) * 100
                    if absolute_diff_pct > (config.tracking_error_pct or 0):
                        if trade_qty != 0:
                            action = "BUY" if trade_qty > 0 else "SELL"
                
                plan.append({
                    "symbol": symbol,
                    "action": action,
                    "quantity": abs(trade_qty),
                    "current_qty": current_qty,
                    "pending_qty": pending_qty,
                    "target_qty": target_qty,
                    "price": price,
                    "current_ratio": round(current_ratio * 100, 2),
                    "target_ratio": round(target_ratio * 100, 2)
                })
            
            logger.info(f"Rebalance plan calculated: {len(plan)} trades identified")
            return plan
        except Exception as e:
            logger.error(f"Error calculating plan: {e}")
            raise
        # 注意：这里我们不再 disconnect，因为是保持长连接或者复用连接

    def _portfolio_copy_signal_version(self, config: PortfolioCopyConfig, targets: List[Dict[str, Any]]) -> str:
        payload = {
            "strategy": STRATEGY_PORTFOLIO_COPY,
            "config_id": config.id,
            "portfolio_id": config.portfolio_id,
            "targets": [
                {
                    "symbol": row.get("symbol"),
                    "target_quantity": safe_int(row.get("target_quantity")),
                    "reference_price": safe_float(row.get("reference_price"), None),
                }
                for row in sorted(targets, key=lambda item: str(item.get("symbol") or ""))
            ],
        }
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return f"portfolio_copy:{config.id}:{digest[:16]}"[:64]

    async def _build_external_target_plan(self, config: PortfolioCopyConfig) -> tuple[List[dict], List[dict]]:
        source_positions, source_prices = await self._fetch_portfolio_source_positions(config)
        target_ratios: Dict[str, float] = {}
        target_prices: Dict[str, float] = {}
        for raw_symbol, ratio in source_positions.items():
            symbol = normalize_us_equity_symbol(raw_symbol)
            if not symbol:
                continue
            target_ratios[symbol] = target_ratios.get(symbol, 0.0) + safe_float(ratio)
        for raw_symbol, price in source_prices.items():
            symbol = normalize_us_equity_symbol(raw_symbol)
            if symbol and safe_float(price) > 0:
                target_prices[symbol] = safe_float(price)

        with get_external_trading_db_ctx() as trading_db:
            external_account = trading_db.query(ExternalTradingAccount).filter(
                ExternalTradingAccount.id == config.external_trading_account_id,
                ExternalTradingAccount.account_id == config.account_id,
                ExternalTradingAccount.enabled == True,  # noqa: E712
            ).first()
            if not external_account:
                raise ValueError("美股跟单绑定的外部交易账户不存在或未启用")
            if normalize_external_trading_market_type(external_account.market_type) != EXTERNAL_TRADING_MARKET_US_STOCK:
                raise ValueError("美股跟单只能绑定美股外部交易账户")

            sub_account = trading_db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == config.live_sub_account_id,
                ExternalTradingSubAccount.account_id == config.account_id,
                ExternalTradingSubAccount.external_trading_account_id == config.external_trading_account_id,
                ExternalTradingSubAccount.enabled == True,  # noqa: E712
            ).first()
            if not sub_account:
                raise ValueError("美股跟单绑定的虚拟子账户不存在或未启用")
            if sub_account.strategy_type != STRATEGY_PORTFOLIO_COPY or sub_account.strategy_config_id != config.id:
                raise ValueError("美股跟单绑定的虚拟子账户归属不匹配")

            valuation = await calculate_sub_account_net_asset(trading_db, sub_account)
            net_asset = safe_float(valuation.get("net_asset"))
            if net_asset <= 0 and not config.total_amount:
                raise ValueError("外部交易虚拟子账户净资产为空，请检查现金和持仓市值")

            ledger_rows = trading_db.query(ExternalTradingLedgerPosition).filter(
                ExternalTradingLedgerPosition.sub_account_id == sub_account.id,
            ).all()
            ledger_positions: Dict[str, int] = {}
            ledger_values: Dict[str, float] = {}
            ledger_prices: Dict[str, float] = {}
            for row in ledger_rows:
                symbol = normalize_us_equity_symbol(row.symbol)
                if not symbol:
                    continue
                quantity = safe_int(row.quantity)
                ledger_positions[symbol] = quantity
                ledger_values[symbol] = safe_float(row.market_value)
                market_price = safe_float(row.market_price)
                if market_price <= 0 and quantity:
                    market_price = safe_float(row.market_value) / abs(quantity)
                if market_price > 0:
                    ledger_prices[symbol] = market_price

            current_target_rows = trading_db.query(ExternalTradingTargetPosition).filter(
                ExternalTradingTargetPosition.sub_account_id == sub_account.id,
                ExternalTradingTargetPosition.strategy_type == STRATEGY_PORTFOLIO_COPY,
                ExternalTradingTargetPosition.strategy_config_id == config.id,
                ExternalTradingTargetPosition.status == "ACTIVE",
            ).all()
            current_targets = {}
            for row in current_target_rows:
                symbol = normalize_us_equity_symbol(row.symbol)
                if symbol:
                    current_targets[symbol] = {
                        "target_quantity": safe_int(row.target_quantity),
                        "target_weight_pct": safe_float(row.target_weight_pct, 0.0),
                        "target_value": safe_float(row.target_value, 0.0),
                        "reference_price": safe_float(row.reference_price, None),
                    }

        total_target_amount = config.total_amount or (net_asset * safe_float(config.total_position_ratio, 100.0) / 100.0)
        if total_target_amount <= 0:
            raise ValueError("外部交易目标跟单金额为空")

        all_symbols = set(target_ratios.keys()) | set(ledger_positions.keys()) | set(current_targets.keys())
        plan: List[dict] = []
        targets: List[dict] = []
        for symbol in sorted(all_symbols):
            target_ratio = safe_float(target_ratios.get(symbol))
            current_qty = safe_int(ledger_positions.get(symbol))
            current_value = safe_float(ledger_values.get(symbol))
            price = target_prices.get(symbol) or ledger_prices.get(symbol)
            old_target = current_targets.get(symbol)

            if target_ratio > 0 and (not price or math.isnan(price) or price <= 0):
                if old_target:
                    target_qty = safe_int(old_target.get("target_quantity"))
                    accepted_weight_pct = safe_float(old_target.get("target_weight_pct"), 0.0)
                    target_value = safe_float(old_target.get("target_value"), 0.0)
                    reference_price = safe_float(old_target.get("reference_price"), None)
                else:
                    logger.warning("Skipping external target %s due to missing price", symbol)
                    continue
            else:
                target_qty = int((total_target_amount * target_ratio) / price) if target_ratio > 0 and price else 0
                target_value = target_qty * safe_float(price)
                accepted_weight_pct = (target_value / net_asset * 100.0) if net_asset > 0 else target_ratio * 100.0
                reference_price = safe_float(price, None) if target_qty > 0 else None

            trade_qty = target_qty - current_qty
            current_ratio = (current_value / total_target_amount) if total_target_amount > 0 else 0.0
            action = "HOLD"
            if target_ratio == 0:
                if current_qty > 0 or (old_target and safe_int(old_target.get("target_quantity")) != 0):
                    action = "SELL"
            else:
                absolute_diff_pct = abs(target_ratio - current_ratio) * 100
                if absolute_diff_pct > (config.tracking_error_pct or 0) and trade_qty != 0:
                    action = "BUY" if trade_qty > 0 else "SELL"

            targets.append({
                "symbol": symbol,
                "target_quantity": target_qty,
                "target_weight_pct": round(accepted_weight_pct, 6),
                "target_value": round(target_value, 2),
                "reference_price": reference_price,
                "reference_price_source": "portfolio_copy_source_price" if reference_price else None,
            })
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
            })

        return plan, targets

    async def sync_external_targets(
        self,
        config: PortfolioCopyConfig,
        *,
        trigger_source: str,
        trigger_executor: bool = True,
    ) -> Dict[str, Any]:
        if config.account_type != "external":
            raise ValueError("Portfolio copy config is not bound to an external trading account")

        plan, targets = await self._build_external_target_plan(config)
        signal_version = self._portfolio_copy_signal_version(config, targets)
        changed = False
        with get_external_trading_db_ctx() as trading_db:
            sub_account = trading_db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == config.live_sub_account_id,
                ExternalTradingSubAccount.account_id == config.account_id,
                ExternalTradingSubAccount.external_trading_account_id == config.external_trading_account_id,
                ExternalTradingSubAccount.strategy_type == STRATEGY_PORTFOLIO_COPY,
                ExternalTradingSubAccount.strategy_config_id == config.id,
            ).first()
            if not sub_account:
                raise ValueError("美股跟单绑定的虚拟子账户不存在")

            current_rows = trading_db.query(ExternalTradingTargetPosition).filter(
                ExternalTradingTargetPosition.sub_account_id == sub_account.id,
                ExternalTradingTargetPosition.status == "ACTIVE",
            ).all()
            current_qty_map = {
                normalize_us_equity_symbol(row.symbol): safe_int(row.target_quantity)
                for row in current_rows
                if normalize_us_equity_symbol(row.symbol)
            }
            next_qty_map = {
                normalize_us_equity_symbol(row.get("symbol")): safe_int(row.get("target_quantity"))
                for row in targets
                if normalize_us_equity_symbol(row.get("symbol"))
            }
            changed = current_qty_map != next_qty_map

            sync_target_positions(
                trading_db,
                sub_account=sub_account,
                targets=targets,
                signal_id=f"portfolio_copy:{config.portfolio_id}",
                signal_version=signal_version,
                source_execution_id=None,
            )

        with Session() as db:
            db_config = db.query(PortfolioCopyConfig).filter(PortfolioCopyConfig.id == config.id).first()
            if db_config:
                db_config.last_external_sync_at = datetime.now()
                db_config.last_external_sync_status = "SYNCED"
                db_config.last_external_sync_message = f"{trigger_source}: 同步目标仓位 {len(targets)} 个"[:500]
                db.add(PortfolioCopyLog(
                    account_id=db_config.account_id,
                    config_id=db_config.id,
                    portfolio_id=db_config.portfolio_id,
                    action="TARGET_SYNC",
                    status="TARGET_SYNCED",
                    quantity=len(targets),
                    message=db_config.last_external_sync_message,
                ))
                db.commit()

        executor_result = None
        if trigger_executor and (changed or trigger_source == "manual"):
            executor_result = await trigger_external_trading_executor(
                account_id=config.account_id,
                external_account_id=config.external_trading_account_id,
                trigger_source=f"portfolio_copy_{trigger_source}",
            )

        return {
            "config_id": config.id,
            "portfolio_id": config.portfolio_id,
            "external_trading_account_id": config.external_trading_account_id,
            "live_sub_account_id": config.live_sub_account_id,
            "target_count": len(targets),
            "changed": changed,
            "signal_version": signal_version,
            "executor_result": executor_result,
            "plan": plan,
        }

    async def rebalance(self, config: PortfolioCopyConfig, client_id: Optional[int] = None):
        """执行调仓逻辑 (由 worker_loop 调用)"""
        masked_account_id = mask_account_id(config.account_id)
        
        # Dispatch to Longport Trader
        if config.account_type == "longport":
            return await self.longport_trader.rebalance(config)
        if config.account_type == "external":
            return await self.sync_external_targets(config, trigger_source="rebalance", trigger_executor=True)

        try:
            # 1. Check Market Status
            if not MarketService.is_us_market_open(include_extended=False):
                logger.info(f"Market is closed. Skipping rebalance for {masked_account_id}")
                return

            # 2. Ensure IB Service is ready (获取对应账户的持久连接)
            port = config.ib_port
            if config.ib_account_id:
                db = Session()
                try:
                    ib_account = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == config.ib_account_id).first()
                    if ib_account:
                        port = ib_account.ib_port
                finally:
                    db.close()
            
            if not port:
                logger.error(f"[{masked_account_id}] No IB port configured. Skipping rebalance.")
                return

            ib = await self._ensure_ib_connected(port, client_id)

            # 3. Check Market Status via IB (Liquid Hours)
            is_open = await ib.is_market_open("SPY")
            if not is_open:
                logger.info(f"Market is CLOSED (Liquid Hours check) for {masked_account_id}. Skipping.")
                return

            # 4. Calculate plan
            plan_full = await self.calculate_rebalance_plan(config, client_id=client_id)
            plan = [p for p in plan_full if p["action"] != "HOLD" and p["quantity"] != 0]
            
            logger.info(f"[{masked_account_id}] Rebalance plan filtered: {len(plan)} active trades (from total {len(plan_full)} symbols)")
            
            if not plan:
                logger.info(f"No rebalance needed for {masked_account_id}")
                return

            # 5. Execute trades
            logger.info(f"[{masked_account_id}] Starting execution loop for {len(plan)} trades...")
            for item in plan:
                symbol = item["symbol"]
                action = item["action"]
                qty = item["quantity"]
                price = item["price"]
                current_ratio = item.get("current_ratio", 0.0)
                target_ratio = item["target_ratio"]

                logger.info(f"[{masked_account_id}] Attempting {action} {qty} for {symbol}...")
                try:
                    # 改用市价单确保立即成交 (幂等性由 get_effective_position 保证)
                    trade = await ib.place_market_order(symbol, action, qty)
                    ib_status = str(getattr(trade.orderStatus, "status", "") or "")
                    log_status = "SUCCESS" if ib_status == "Filled" else "SUBMITTED"
                    
                    # Log with specific Action and Ratio change
                    msg = (
                        f"{action} {qty * price} USD (Market Order). "
                        f"Ratio: {current_ratio:.2f}% -> {target_ratio:.2f}%. "
                        f"IB status: {ib_status or 'UNKNOWN'}"
                    )
                    
                    self._log(config.account_id, config.portfolio_id, action, log_status, 
                                msg, 
                                symbol=symbol, quantity=qty, price=price, config_id=config.id)

                    if log_status == "SUCCESS":
                        logger.info(f"[{masked_account_id}] Filled MARKET {action} order for {qty} {symbol}")
                    else:
                        logger.info(
                            f"[{masked_account_id}] Submitted MARKET {action} order for {qty} {symbol} "
                            f"(IB status: {ib_status or 'UNKNOWN'})"
                        )
                except IBOrderSubmissionPending as e:
                    logger.warning(f"[{masked_account_id}] Execution pending broker acknowledgement for {symbol}: {e}")
                    self._log(
                        config.account_id,
                        config.portfolio_id,
                        action,
                        "SUBMITTED",
                        str(e),
                        symbol=symbol,
                        quantity=qty,
                        price=price,
                        config_id=config.id,
                    )
                except Exception as e:
                    logger.error(f"[{masked_account_id}] Execution failed for {symbol}: {e}")
                    self._log(config.account_id, config.portfolio_id, action, "FAILED", str(e), symbol=symbol, config_id=config.id)

        except Exception as e:
            logger.error(f"Rebalance process failed for {masked_account_id}: {e}")
            self._log(config.account_id, config.portfolio_id, "SYSTEM_ERROR", "FAILED", str(e), config_id=config.id)
            send_alert_email(
                f"自动化跟单策略报错: Portfolio Copy Rebalance {masked_account_id}",
                f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}",
                scenario_key="portfolio_copy_trading_error",
            )

    def _should_run(self, cron_rule: str, timezone_str: str = "America/New_York") -> bool:
        """使用 croniter 检查当前时间是否符合 cron 规则"""
        try:
            # 1. 获取目标时区的当前时间
            tz = ZoneInfo(timezone_str)
            now = datetime.now(tz).replace(second=0, microsecond=0)
            
            # 2. 检查当前分钟是否在 cron 规则触发点
            # croniter 需要 awareness-consistent 的时间
            iter = croniter(cron_rule, now - timedelta(minutes=1))
            next_run = iter.get_next(datetime)
            
            return next_run == now
        except Exception as e:
            logger.error(f"Cron parse error: {cron_rule} ({timezone_str}) - {e}")
            return False

    async def submit_rebalance_task(self, config: PortfolioCopyConfig, client_id: int) -> List[dict]:
        """
        API 调用此方法提交任务。
        此方法在 API (Main) 线程调用，需要线程安全地将任务放入 Worker 线程的队列。
        """
        if not self.worker_loop_obj:
            raise RuntimeError("Worker loop not ready")
            
        loop = asyncio.get_running_loop() # API Loop
        future = loop.create_future()
        
        task = {
            "type": "calculate_plan",
            "config": config,
            "client_id": client_id,
            "future": future,
            "loop": loop 
        }
        
        # Thread-safe put
        self.worker_loop_obj.call_soon_threadsafe(self.task_queue.put_nowait, task)
        
        return await future

    async def _handle_task(self, task: dict):
        """统一处理不同类型的任务"""
        task_type = task.get("type")
        config = task.get("config")
        cid = task.get("client_id")
        
        if task_type == "calculate_plan":
            api_loop = task["loop"]
            fut = task["future"]
            try:
                result = await self.calculate_rebalance_plan(config, client_id=cid)
                if not fut.cancelled():
                    api_loop.call_soon_threadsafe(fut.set_result, result)
            except Exception as e:
                logger.error(f"Calculate plan task error: {e}")
                if not fut.cancelled():
                    api_loop.call_soon_threadsafe(fut.set_exception, e)
        
        elif task_type == "rebalance":
            key = task.get("key")
            try:
                if key: self._processing_keys.add(key)
                await self.rebalance(config, client_id=cid)
            except Exception as e:
                logger.error(f"Rebalance task error: {e}")
                send_alert_email(
                    f"自动化跟单策略报错: Rebalance task failed",
                    f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}",
                    scenario_key="portfolio_copy_trading_error",
                )
            finally:
                if key: self._processing_keys.discard(key)

    async def worker_loop(self):
        """后台 Worker 主循环 (Task Queue Mode)"""
        logger.info(f"Starting Portfolio Copy Trader Worker Loop in thread {threading.get_ident()}")
        self.task_queue = asyncio.Queue()
        self.worker_loop_obj = asyncio.get_running_loop()
        self._last_ran_map = {}

        while True:
            # 1. 尝试从队列获取任务 (等待最多 1 秒)
            try:
                task = await asyncio.wait_for(self.task_queue.get(), timeout=1.0)
                await self._handle_task(task)
                self.task_queue.task_done()
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"Error in worker loop task execution: {e}")
                send_alert_email(
                    "自动化跟单策略报错: Portfolio Trader Queue",
                    f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}",
                    scenario_key="portfolio_copy_trading_error",
                )

            # 2. 定时检查 Cron 规则并提交任务
            try:
                now_minute = datetime.now().strftime("%Y-%m-%d %H:%M")
                db = Session()
                try:
                    active_configs = db.query(PortfolioCopyConfig).filter(PortfolioCopyConfig.enabled == True).all()
                    for config in active_configs:
                        if (config.platform or 'futu') not in SUPPORTED_PORTFOLIO_COPY_PLATFORMS:
                            continue

                        # Ensure unique key per configuration entry (using config.id)
                        key = f"cfg_{config.id}"
                        
                        if self._should_run(config.cron_rule, config.timezone or "America/New_York") and self._last_ran_map.get(key) != now_minute:
                            if key in self._processing_keys:
                                masked_account_id = mask_account_id(config.account_id)
                                logger.warning(f"Cron Skip: Rebalance for {masked_account_id} is still in progress. Skipping this tick.")
                                self._last_ran_map[key] = now_minute # 标记本分钟已“处理”过
                                continue

                            # 2. 标记本轮已处理
                            self._last_ran_map[key] = now_minute
                            
                            # 3. 提交到任务队列
                            masked_account_id = mask_account_id(config.account_id)
                            logger.info(f"Cron Trigger: Queuing rebalance for {masked_account_id}")
                            self.task_queue.put_nowait({
                                "type": "rebalance",
                                "config": config,
                                "client_id": 100 + (config.id or 0),
                                "key": key # 传递 key 用于完成后清除状态
                            })
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in Cron check: {e}")
                send_alert_email(
                    "自动化跟单策略报错: Portfolio Trader Cron",
                    f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}",
                    scenario_key="portfolio_copy_trading_error",
                )


    async def trigger_rebalance_if_name_in_content(self, content: str, platform: str = None) -> List[str]:
        """
        Check if any active portfolio name exists in the content, and trigger rebalance if so.
        If platform is provided, only triggers configs with matching platform.
        """
        triggered_accounts = []
        if not content:
            return triggered_accounts

        if not self.worker_loop_obj:
            logger.warning("Worker loop not ready, cannot trigger rebalance")
            return triggered_accounts

        db = Session()
        try:
            # Get all active configs
            query = db.query(PortfolioCopyConfig).filter(PortfolioCopyConfig.enabled == True)
            if platform:
                 query = query.filter(PortfolioCopyConfig.platform == platform)
            
            configs = query.all()

            for config in configs:
                if (config.platform or 'futu') not in SUPPORTED_PORTFOLIO_COPY_PLATFORMS:
                    continue

                # Check if portfolio name is valid and exists in content
                if config.portfolio_name and config.portfolio_name.strip() and config.portfolio_name in content:
                    
                    key = f"cfg_{config.id}"
                    
                    # Check if already processing
                    if key in self._processing_keys:
                        logger.info(f"Skipping rebalance trigger for {config.portfolio_name} (ID: {config.id}) - already in progress")
                        continue

                    masked_account_id = mask_account_id(config.account_id)
                    logger.info(f"Notification Trigger: Content '{content}' matched portfolio '{config.portfolio_name}'. Queuing rebalance.")
                    
                    # Submit to queue
                    self.worker_loop_obj.call_soon_threadsafe(self.task_queue.put_nowait, {
                        "type": "rebalance",
                        "config": config,
                        "client_id": 100 + (config.id or 0),
                        "key": key
                    })
                    triggered_accounts.append(masked_account_id)
                
        except Exception as e:
            logger.error(f"Error checking rebalance trigger for content: {e}")
        finally:
            db.close()
            
        return triggered_accounts

def start_portfolio_copy_trader():
    """在单独的线程中启动 Worker"""
    trader = PortfolioCopyTrader()
    with trader._thread_lock:
        if trader._thread_started:
            logger.info("Portfolio Copy Trader Worker already started, skipping.")
            return
        trader._thread_started = True

    def run_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trader.worker_loop())

    thread = threading.Thread(target=run_worker, daemon=True, name="PortfolioCopyTraderWorker")
    thread.start()
    logger.info("Portfolio Copy Trader Worker Thread started")
