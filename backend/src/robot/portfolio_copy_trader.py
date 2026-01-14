# ... existing imports ...
import asyncio
import threading
import time
import logging
import requests
import math
import pytz
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from croniter import croniter
from ..core.database import Session, PortfolioCopyConfig, PortfolioCopyLog, IBKRAccountConfig
from ..core.utils import mask_account_id
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
        self.task_queue = None
        self._processing_keys = set()
        self.ib_services: Dict[str, IBKRService] = {} # Key: "port_clientid"
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

    async def _ensure_ib_connected(self, port: int, client_id: int) -> IBKRService:
        """确保在当前线程(Worker)中连接到特定的 IB 账户，并返回该 service 实例"""
        key = f"{port}_{client_id}"
        if key not in self.ib_services:
            logger.info(f"Creating new IBKRService instance for port={port}, client_id={client_id}")
            self.ib_services[key] = IBKRService(port=port, client_id=client_id)
        
        service = self.ib_services[key]
        await service.connect()
        return service

    async def calculate_rebalance_plan(self, config: PortfolioCopyConfig, client_id: Optional[int] = None) -> List[dict]:
        """计算调仓计划但不执行 (Should run in worker loop)"""
        masked_account_id = mask_account_id(config.account_id)
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
            # 1. 获取 Futu 持仓占比 (Run in executor to avoid blocking loop)
            futu_records = await self._run_in_executor(self.get_futu_positions_sync, config.portfolio_id, config.api_headers or {})
            
            # --- 符号归一化 + 价格提取 (Normalizing symbols + extracting prices) ---
            # Normalized Futu map: symbol (clean) -> {target_ratio, price}
            futu_positions_map = {}
            futu_price_map = {}
            for r in futu_records:
                symbol = r["stock_code"].replace('US.', '')
                ratio = r["position_ratio"] / 1000000000.0
                if ratio > 1.0: # 14.0 代表 14%
                    ratio /= 100.0
                futu_positions_map[symbol] = futu_positions_map.get(symbol, 0) + ratio
                # 从 Futu 提取价格 (价格被放大了 10^9 倍，需要除以 1000000000)
                if "current_price" in r and r["current_price"]:
                    futu_price_map[symbol] = float(r["current_price"]) / 1000000000.0

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

    async def rebalance(self, config: PortfolioCopyConfig, client_id: Optional[int] = None):
        """执行调仓逻辑 (由 worker_loop 调用)"""
        masked_account_id = mask_account_id(config.account_id)
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
                current_ratio = item.get("current_ratio", 0.0)
                target_ratio = item["target_ratio"]

                logger.info(f"[{masked_account_id}] Attempting {action} {qty} for {symbol}...")
                try:
                    # 改用市价单确保立即成交 (幂等性由 get_effective_position 保证)
                    await ib.place_market_order(symbol, action, qty)
                    
                    # Log with specific Action and Ratio change
                    msg = f"{action} {qty} (Market Order). Ratio: {current_ratio:.2f}% -> {target_ratio:.2f}%"
                    
                    self._log(config.account_id, config.portfolio_id, action, "SUCCESS", 
                                msg, 
                                symbol=symbol, quantity=qty, price=item["price"])
                                
                    logger.info(f"[{masked_account_id}] Successfully placed MARKET {action} order for {qty} {symbol}")
                except Exception as e:
                    logger.error(f"[{masked_account_id}] Execution failed for {symbol}: {e}")
                    self._log(config.account_id, config.portfolio_id, action, "FAILED", str(e), symbol=symbol)

        except Exception as e:
            logger.error(f"Rebalance process failed for {masked_account_id}: {e}")
            self._log(config.account_id, config.portfolio_id, "SYSTEM_ERROR", "FAILED", str(e))

    def _should_run(self, cron_rule: str, timezone_str: str = "America/New_York") -> bool:
        """使用 croniter 检查当前时间是否符合 cron 规则"""
        try:
            # 1. 获取目标时区的当前时间
            tz = pytz.timezone(timezone_str)
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

            # 2. 定时检查 Cron 规则并提交任务
            try:
                now_minute = datetime.now().strftime("%Y-%m-%d %H:%M")
                db = Session()
                try:
                    active_configs = db.query(PortfolioCopyConfig).filter(PortfolioCopyConfig.enabled == True).all()
                    for config in active_configs:
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
