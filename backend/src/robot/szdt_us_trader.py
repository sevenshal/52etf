import logging
import threading
import time
from datetime import datetime, timedelta
import asyncio
from math import pow
from typing import Dict, List, Optional

from ..core.database import get_db_ctx, SzdtTradeStock, TradingLog, TradingState, StockCooldown, SZDTTradingConfig, IBKRAccountConfig
from ..core.services.szdt import SZDTService
from ..core.services.market import MarketService
from ..core.services.ib_service import IBKRService
from ..core.utils import mask_account_id

logger = logging.getLogger(__name__)

class SZDTUSTrader:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SZDTUSTrader, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.szdt = SZDTService()
        self.ib_services: Dict[str, IBKRService] = {} # Key: "port_clientid"
        self._thread_started = False
        self._thread_lock = threading.Lock()

    def _log(self, account_id: str, level: str, message: str):
        with get_db_ctx() as db:
            db.add(TradingLog(
                account_id=account_id,
                timestamp=datetime.now(),
                level=level,
                message=message
            ))

    async def _ensure_ib_connected(self, port: int, client_id: int) -> IBKRService:
        """确保在当前线程(Worker)中连接到特定的 IB 账户，并返回该 service 实例"""
        key = f"{port}_{client_id}"
        if key not in self.ib_services:
            logger.info(f"Creating new IBKRService instance for port={port}, client_id={client_id}")
            self.ib_services[key] = IBKRService(port=port, client_id=client_id)
        
        service = self.ib_services[key]
        await service.connect()
        return service

    def _get_next_stock(self, account_id: str, cli_id: str) -> Dict:
        """按状态索引 + 冷却筛选，每次只取一只。"""
        with get_db_ctx() as db:
            state = db.query(TradingState).filter_by(account_id=account_id, cli_id=cli_id).first()
            if not state:
                state = TradingState(account_id=account_id, cli_id=cli_id, current_index=0)
                db.add(state)

            stocks = db.query(SzdtTradeStock).filter(
                SzdtTradeStock.account_id == account_id,
                SzdtTradeStock.type.in_([1, 2, 7])
            ).all()
            if not stocks:
                db.commit()
                return None

            now = datetime.now()
            db.query(StockCooldown).filter(
                StockCooldown.account_id == account_id,
                StockCooldown.cli_id == cli_id, 
                StockCooldown.until < now
            ).delete()

            if state.current_index >= len(stocks):
                state.current_index = 0

            selected = None
            # Do not loop infinitely if all on cooldown
            start_index = state.current_index
            loop_count = 0
            
            while loop_count < len(stocks):
                if state.current_index >= len(stocks):
                     state.current_index = 0
                
                s = stocks[state.current_index]
                state.current_index += 1
                loop_count += 1
                
                cooldown = db.query(StockCooldown).filter_by(
                    account_id=account_id,
                    cli_id=cli_id, 
                    stock_code=s.code
                ).first()
                if cooldown:
                    continue
                selected = {
                    'code': s.code,
                    'name': s.name,
                    'type': s.type,
                    'when_buy': s.when_buy,
                    'when_sell': s.when_sell,
                    'max_position': s.max_position,
                    'buy_amount': s.buy_amount,
                    'sell_amount': s.sell_amount,
                    'buy_factor': s.buy_factor,
                    'sell_factor': s.sell_factor,
                    'lever': s.lever,
                    'emo_area': s.emo_area,
                }
                break
            db.commit()
            return selected

    async def run_once(self, config: SZDTTradingConfig, ib_port: int):
        account_id = config.account_id
        cli_id = f'szdt-us-auto-{account_id}'
        # Use a consistent client ID for this account to allow connection reuse. 
        # config.id is unique per config entry. Using base 2000 to avoid conflict with portfolio copy trader (100+) and others.
        ib_client_id = 2000 + (config.id or 0) 
        
        logger.info(f"SZDTUSTrader tick for {mask_account_id(account_id)}")
            
        try:
            # 1. Reuse connection
            ib_service = await self._ensure_ib_connected(ib_port, ib_client_id)
            
            # 2. Check IB connectivity/NetLiquidation
            net_liq = ib_service.get_net_liquidation()
            available_cash = ib_service.get_available_cash()
            
            # If values are invalid, it might be disconnected or not ready, try re-connect/wait?
            # IBService usually handles re-connect on 'connect' call if needed.
            
            logger.info(f"Account {mask_account_id(account_id)} net_liq={net_liq:.2f} cash={available_cash:.2f}")
            
            stock = self._get_next_stock(account_id, cli_id)
            if not stock:
                logger.debug(f"Account {mask_account_id(account_id)}: No candidate stock")
                return
                
            name = f"{stock['name']}({stock['code']})"
            # 获取情绪
            emotion = await self.szdt.get_fresh_emotion_from_list(stock['type'], stock['code'])
            if not emotion or emotion.get('status') != 1:
                emotion = await self.szdt.get_stock_emotion(stock['code'], stock['lever'], stock['emo_area'])
                if not emotion or emotion.get('status') != 1:
                    self._log(account_id, 'DEBUG', f"{name} 获取情绪失败，跳过。 {emotion}")
                    return
            
            score = emotion['data']['score']
            emotion_price = emotion['data']['price']
            logger.info(f"Emotion fetched {name} score={score} emotion_price={emotion_price}")

            # 获取持仓数据 (包含数量和 IB 市场价格)
            pos_data = ib_service.get_position(stock['code'])
            position_qty = pos_data['qty']
            # 优先使用 IB 的市场价格，回退到情绪 API 价格
            price = pos_data['price'] if pos_data['price'] else emotion_price
            
            position_value = position_qty * price
            portfolio_value = max(net_liq, 1.0)
            position_ratio = 100 * position_value / portfolio_value

            # 过热/过冷保护并设置冷却
            if score > (stock['when_buy'] + 10) and position_qty == 0:
                self._log(account_id, 'DEBUG', f"{name} 无持仓且情绪分数过高(当前:{score},买:{stock['when_buy']})，冷却2小时")
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(hours=2), 
                        reason='无持仓且情绪分数过高'
                    ))
                return
            if score < (stock['when_sell'] - 10) and position_ratio > stock['max_position']:
                self._log(account_id, 'DEBUG', f"{name} 持仓已满(持仓:{position_ratio:.2f}% 上限:{stock['max_position']}%)且情绪分数过低(当前:{score},卖:{stock['when_sell']})，冷却2小时")
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(hours=2), 
                        reason='持仓已满且情绪分数过低'
                    ))
                return

            # 买入条件
            if score <= stock['when_buy']:
                if position_ratio < stock['max_position']:
                    score_factor = min(1, max(0, (stock['when_buy'] - score) / (stock['when_buy'] + 100)))
                    score_factor = 3 ** (score_factor ** stock['buy_factor'])
                    buy_amount = min(available_cash, stock['buy_amount'] * score_factor)
                    buy_quantity = int(buy_amount / price)
                    if buy_quantity >= 1:
                        trade = await ib_service.place_market_order(stock['code'], 'BUY', buy_quantity)
                        order_id = trade.order.orderId
                        self._log(account_id, 'INFO', f"{name} BUY x{buy_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                        logger.info(f"{name} BUY x{buy_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                    else:
                        self._log(account_id, 'INFO', f"{name} 可用资金 {available_cash:.2f} 不足，跳过买入")
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(hours=12), 
                        reason='决策后冷却12h'
                    ))
                return

            # 卖出条件
            if score >= stock['when_sell'] and position_qty > 0:
                score_factor = min(1, max(0, (score - stock['when_sell']) / (100 - stock['when_sell'])))
                score_factor = 3 ** (score_factor ** stock['sell_factor'])
                sell_amount = stock['sell_amount'] * score_factor
                sell_quantity = int(sell_amount / price)
                sell_quantity = max(min(sell_quantity, int(position_qty)), 1)
                if sell_quantity > 0:
                    trade = await ib_service.place_market_order(stock['code'], 'SELL', sell_quantity)
                    order_id = trade.order.orderId
                    self._log(account_id, 'INFO', f"{name} SELL x{sell_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                    logger.info(f"{name} SELL x{sell_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(hours=12), 
                        reason='决策后冷却1h'
                    ))
                return

            # 中性区间：设置距离相关冷却
            score_delta = min(score - stock['when_buy'], stock['when_sell'] - score)
            cooldown_minutes = round(min(720, pow(1.6, score_delta)))
            if cooldown_minutes > 1:
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(minutes=cooldown_minutes), 
                        reason='情绪分数距离买卖阈值较远'
                    ))
            self._log(account_id, 'DEBUG', f"{name} 情绪分数介于买卖阈值之间(当前:{score},买:{stock['when_buy']},卖:{stock['when_sell']}), 冷却{cooldown_minutes}分钟")
            logger.info(f"{name} neutral score={score} buy={stock['when_buy']} sell={stock['when_sell']} cooldown={cooldown_minutes}m")
                
        except Exception as e:
            logger.error(f"SZDTUSTrader的处理 {account_id} 失败: {e}")
            self._log(account_id, 'ERROR', f"处理异常: {e}")

    async def worker_loop(self):
        logger.info("SZDT US Trader Worker Loop Started")
        
        while True:
            try:
                # 1. Check market open
                if not MarketService.is_us_market_open():
                    logger.info("US market not open, skipping SZDT US check.")
                    await asyncio.sleep(60)
                    continue

                # 2. Get active configurations
                with get_db_ctx() as db:
                    configs = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.enabled == True).all()
                    for config in configs:
                        db.expunge(config)
                
                # 3. Iterate and process
                for config in configs:
                    try:
                        # Find mapped IB port
                        ib_port = 4001
                        with get_db_ctx() as db:
                             if config.ib_account_id:
                                ib_config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == config.ib_account_id).first()
                                if ib_config:
                                    ib_port = ib_config.ib_port

                        await self.run_once(config, ib_port)
                    except Exception as e:
                        logger.error(f"Failed to run SZDT US trader for account {config.account_id}: {e}")
                
                # 4. Wait for next minute
                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"SZDT US Trader check loop error: {e}")
                await asyncio.sleep(60)

def start_szdt_us_trader():
    trader = SZDTUSTrader()
    with trader._thread_lock:
        if trader._thread_started:
            logger.info("SZDT US Trader Worker already started, skipping.")
            return
        trader._thread_started = True

    def run_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trader.worker_loop())

    thread = threading.Thread(target=run_worker, daemon=True, name="SZDTUSTraderWorker")
    thread.start()
    logger.info("SZDT US Trader Worker Thread started")
