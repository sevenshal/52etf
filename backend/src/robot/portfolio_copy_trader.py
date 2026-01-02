# ... existing imports ...
import asyncio
import threading
import time
import logging
import requests
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from croniter import croniter
from ..core.database import Session, PortfolioCopyConfig, PortfolioCopyLog
from ..core.services.ib_service import IBKRService
from ..core.services.market import MarketService

logger = logging.getLogger(__name__)

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
        self.task_queue = asyncio.Queue()
        self.ib_service = None # Use a shared service instance for the worker
        self.worker_loop_obj = None # Capture the worker loop

    def _log(self, account_id: str, portfolio_id: str, action: str, status: str, message: str, symbol: str = None, quantity: float = None, price: float = None):
        db = Session()
        try:
            log = PortfolioCopyLog(
                account_id=account_id,
                portfolio_id=portfolio_id,
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

    async def get_portfolio_info(self, portfolio_id: str, headers: dict) -> dict:
        """异步包装器：获取组合信息"""
        return await self._run_in_executor(self.get_portfolio_info_sync, portfolio_id, headers)

    def get_portfolio_info_sync(self, portfolio_id: str, headers: dict) -> dict:
        """从 Futu API 获取组合信息 (同步方法，供 executor 调用)"""
        url = f"https://portfolio.futunn.com/portfolio-api/get-portfolio-info?portfolio_id={portfolio_id}&_={int(time.time() * 1000)}"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                return data["data"]
            else:
                raise Exception(f"Futu API error: {data.get('message')}")
        except Exception as e:
            logger.error(f"Failed to fetch Futu portfolio info: {e}")
            raise

    def get_futu_positions_sync(self, portfolio_id: str, headers: dict) -> List[dict]:
        """从 Futu API 获取持仓 (同步方法，供 executor 调用)"""
        url = f"https://portfolio.futunn.com/portfolio-api/get-portfolio-position?portfolio_id={portfolio_id}&language=0&_={int(time.time() * 1000)}"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                return data["data"]["record_items"]
            else:
                raise Exception(f"Futu API error: {data.get('message')}")
        except Exception as e:
            logger.error(f"Failed to fetch Futu positions: {e}")
            raise

    async def _ensure_ib_connected(self, port: int, client_id: int):
        """确保在当前线程(Worker)中连接 IB"""
        if not self.ib_service:
            self.ib_service = IBKRService(port=port, client_id=client_id)
        
        # 如果端口变了，需要重新初始化
        if self.ib_service.port != port or self.ib_service.client_id != client_id:
             if self.ib_service.ib and self.ib_service.isConnected:
                 self.ib_service.disconnect()
             self.ib_service = IBKRService(port=port, client_id=client_id)

        await self.ib_service.connect()

    async def calculate_rebalance_plan(self, config: PortfolioCopyConfig, client_id: Optional[int] = None) -> List[dict]:
        """计算调仓计划但不执行 (Should run in worker loop)"""
        masked_account_id = f"***{config.account_id[-4:]}" if len(config.account_id) > 4 else config.account_id
        logger.info(f"Calculating rebalance plan for account {masked_account_id} for portfolio {config.portfolio_id}")
        
        # Use local service or managed service within this thread
        # 既然我们在 Worker 线程，我们可以安全地使用 managed service
        await self._ensure_ib_connected(config.ib_port, client_id)
        ib = self.ib_service 

        plan = []
        try:
            # 1. 获取 Futu 持仓占比 (Run in executor to avoid blocking loop)
            futu_records = await self._run_in_executor(self.get_futu_positions_sync, config.portfolio_id, config.api_headers or {})
            
            # --- 符号归一化 (Normalizing symbols) ---
            # Normalized Futu map: symbol (clean) -> target_ratio (0.0 - 1.0)
            futu_positions_map = {}
            for r in futu_records:
                symbol = r["stock_code"].replace('US.', '')
                ratio = r["position_ratio"] / 1000000000.0
                if ratio > 1.0: # 14.0 代表 14%
                    ratio /= 100.0
                futu_positions_map[symbol] = futu_positions_map.get(symbol, 0) + ratio

            # 2. IB 状态已经在 _ensure_ib_connected 中准备好
            net_liq = ib.net_liquidation
            if net_liq <= 0:
                raise Exception("IB Account net liquidation is zero or negative")

            # 3. 计算调仓总额 (Target amount for the entire strategy)
            total_target_amount = config.total_amount or (net_liq * config.total_position_ratio / 100.0)
            
            # --- 目标符号集 ---
            # 收集所有我们需要关注的股票代码 (Futu 目标 + IB 当前持仓)
            all_clean_symbols = set(list(futu_positions_map.keys()) + [s.replace('US.', '') for s in ib._positions.keys()])
            
            # 4. 批量获取市场价格
            logger.info(f"Fetching market prices for {len(all_clean_symbols)} symbols...")
            market_prices = await ib.get_market_prices(list(all_clean_symbols))
            
            # 5. 计算调仓方案
            plan = []
            for symbol in all_clean_symbols:
                target_ratio = futu_positions_map.get(symbol, 0)
                
                # 获取 IB 实时数据 (当前持仓和待成交挂单)
                current_qty = ib.get_position(symbol)
                pending_qty = ib.get_pending_qty(symbol)
                
                price = market_prices.get(symbol)
                if not price or math.isnan(price) or price <= 0:
                    logger.warning(f"Skipping {symbol} due to missing or invalid price: {price}")
                    continue

                # 当前实际占比 (基于现有持仓和最新价)
                current_ratio = (current_qty * price) / net_liq
                diff_ratio = target_ratio - current_ratio
                
                # 目标股数 = (总目标调仓额 * 目标占比) / 价格
                target_qty = int((total_target_amount * target_ratio) / price)
                # 核心逻辑：需交易股数 = 目标股数 - (当前股数 + 正在路上的股数)
                trade_qty = target_qty - (current_qty + pending_qty)
                
                action = "HOLD"
                # 只有当占比误差超过设定阈值 (Tracking Error %) 时才行动
                if abs(diff_ratio) * 100 > (config.tracking_error_pct or 0):
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

    async def rebalance(self, config: PortfolioCopyConfig, client_id: Optional[int] = None):
        """执行调仓逻辑 (Should run in worker loop)"""
        try:
            
            # 直接调用 MarketService 判断开盘
            if not MarketService.is_us_market_open(include_extended=False):
                logger.info("Market (including extended) is not open")
                return

            # 0. Ensure IB Service is ready
            await self._ensure_ib_connected(config.ib_port, client_id)

            # 1. Check Market Status
            # 既然我们需要在盘前盘后也交易，我们依赖 is_market_open (基于 liquidHours)
            # 如果不开放，直接跳过
            is_open = await self.ib_service.is_market_open("SPY")
            if not is_open:
                logger.info(f"Market is CLOSED (Liquid Hours check). Skipping rebalance for ***{config.account_id[-4:]}")
                return

            # Re-use the calculation logic
            plan = await self.calculate_rebalance_plan(config, client_id=client_id)
            plan = [p for p in plan if p["action"] != "HOLD" and p["quantity"] != 0]
            if not plan:
                masked_account_id = f"***{config.account_id[-4:]}" if len(config.account_id) > 4 else config.account_id
                logger.info(f"No rebalance needed for {masked_account_id}")
                return

            ib = self.ib_service
            # No need to connect again (is_market_open ensured connection)
            
            for item in plan:
                symbol = item["symbol"]
                action = item["action"]
                qty = item["quantity"]
                price = item["price"]
                target_ratio = item["target_ratio"]

                try:
                    # 改用市价单确保立即成交 (幂等性由 get_effective_position 保证)
                    trade = await ib.place_market_order(symbol, action, qty)
                    self._log(config.account_id, config.portfolio_id, "REBALANCE", "SUCCESS", 
                                f"{action} {qty} (Market Order) for Target Ratio: {target_ratio:.2f}%", 
                                symbol=symbol, quantity=qty, price=item["price"])
                    logger.info(f"Placed MARKET {action} order for {qty} {symbol}")
                except Exception as e:
                    self._log(config.account_id, config.portfolio_id, "REBALANCE", "FAILED", str(e), symbol=symbol)

        except Exception as e:
            masked_account_id = f"***{config.account_id[-4:]}" if len(config.account_id) > 4 else config.account_id
            logger.error(f"Rebalance failed for {masked_account_id}: {e}")
            self._log(config.account_id, config.portfolio_id, "SYSTEM_ERROR", "FAILED", str(e))

    def _should_run(self, cron_rule: str) -> bool:
        """使用 croniter 检查当前时间是否符合 cron 规则"""
        try:
            now = datetime.now().replace(second=0, microsecond=0)
            # 检查当前分钟是否在 cron 规则触发点
            iter = croniter(cron_rule, now - timedelta(minutes=1))
            next_run = iter.get_next(datetime)
            return next_run == now
        except Exception as e:
            logger.error(f"Cron parse error: {cron_rule} - {e}")
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

    async def worker_loop(self):
        """
        后台 Worker 主循环
        """
        logger.info(f"Starting Portfolio Copy Trader Worker Loop in thread {threading.get_ident()}")
        self.worker_loop_obj = asyncio.get_running_loop()
        
        last_ran_minute = ""
        
        while True:
            # 1. 优先处理队列任务
            while not self.task_queue.empty():
                try:
                    task = self.task_queue.get_nowait()
                    # ... processing logic ...
                    task_type = task.get("type")
                    
                    if task_type == "calculate_plan":
                        config = task["config"]
                        cid = task["client_id"]
                        api_loop = task["loop"]
                        fut = task["future"]
                        
                        try:
                            # 在当前 Worker 线程执行计算
                            masked_account_id = f"***{config.account_id[-4:]}" if len(config.account_id) > 4 else config.account_id
                            logger.info(f"Worker Processing task for {masked_account_id}")
                            result = await self.calculate_rebalance_plan(config, client_id=cid)
                            
                            # 在 API 线程设置结果
                            if not fut.cancelled():
                                api_loop.call_soon_threadsafe(fut.set_result, result)
                        except Exception as e:
                            logger.error(f"Task processing error: {e}")
                            if not fut.cancelled():
                                api_loop.call_soon_threadsafe(fut.set_exception, e)
                        finally:
                            # 必须在主 try-finally 中，确保无论成功失败都会标志 done
                            pass
                    
                    self.task_queue.task_done()

                except Exception as e:
                    logger.error(f"Error processing task queue: {e}")

            # 2. 处理定时任务 (Cron)
            try:
                # 记录每一个账户最后运行的分钟，避免由 while True 循环速度引起的重复触发
                if not hasattr(self, '_last_ran_map'):
                    self._last_ran_map = {}

                now_minute = datetime.now().strftime("%Y-%m-%d %H:%M")
                db = Session()
                try:
                    configs = db.query(PortfolioCopyConfig).filter(PortfolioCopyConfig.enabled == True).all()
                    for config in configs:
                        key = f"{config.account_id}_{config.portfolio_id}"
                        if self._should_run(config.cron_rule):
                            if self._last_ran_map.get(key) != now_minute:
                                masked_account_id = f"***{config.account_id[-4:]}" if len(config.account_id) > 4 else config.account_id
                                logger.info(f"Cron Trigger: Triggering rebalance for {masked_account_id}")
                                await self.rebalance(config, client_id=100+config.id if config.id else 999)
                                self._last_ran_map[key] = now_minute
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in Cron check: {e}")

            await asyncio.sleep(1) # 1秒轮询一次

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
