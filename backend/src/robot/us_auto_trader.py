import logging
import threading
import time
from datetime import datetime, timedelta
import asyncio
from math import pow
from typing import Dict, List

from ..core.database import get_db_session, SzdtTradeStock, TradingLog, TradingState, StockCooldown
from ..core.services.szdt import SZDTService
from ..core.services.market import MarketService
from ..core.services.ib_service import IBKRService

logger = logging.getLogger(__name__)

class USAutoTrader:
    def __init__(self, account_id: str):
        self.account_id = account_id
        # 复用核心 IB 服务
        self.ib_service = IBKRService()
        self.szdt = SZDTService()
        self.cli_id = f'us-auto-{account_id}'

    def _log(self, level: str, message: str):
        with get_db_session(self.account_id) as db:
            db.add(TradingLog(
                account_id=self.account_id,
                timestamp=datetime.now(),
                level=level,
                message=message
            ))

    def _fetch_candidates(self) -> List[Dict]:
        """获取交易标的候选列表"""
        with get_db_session(self.account_id) as db:
            rows = db.query(SzdtTradeStock).filter(SzdtTradeStock.type.in_([1, 2, 7])).all()
            result: List[Dict] = []
            for r in rows:
                result.append({
                    'code': r.code,
                    'name': r.name,
                    'type': r.type,
                    'when_buy': r.when_buy,
                    'when_sell': r.when_sell,
                    'max_position': r.max_position,
                    'buy_amount': r.buy_amount,
                    'sell_amount': r.sell_amount,
                    'buy_factor': r.buy_factor,
                    'sell_factor': r.sell_factor,
                    'lever': r.lever,
                    'emo_area': r.emo_area,
                })
            return result

    def _get_next_stock(self) -> Dict:
        """按状态索引 + 冷却筛选，每次只取一只。"""
        with get_db_session(self.account_id) as db:
            state = db.query(TradingState).filter_by(cli_id=self.cli_id).first()
            if not state:
                state = TradingState(cli_id=self.cli_id, current_index=0)
                db.add(state)

            stocks = db.query(SzdtTradeStock).filter(SzdtTradeStock.type.in_([1, 2, 7])).all()
            if not stocks:
                db.commit()
                return None

            now = datetime.now()
            db.query(StockCooldown).filter(StockCooldown.cli_id == self.cli_id, StockCooldown.until < now).delete()

            if state.current_index >= len(stocks):
                state.current_index = 0

            selected = None
            while state.current_index < len(stocks):
                s = stocks[state.current_index]
                state.current_index += 1
                cooldown = db.query(StockCooldown).filter_by(cli_id=self.cli_id, stock_code=s.code).first()
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

    async def run_once(self):
        logging.info("USAutoTrader tick")
        # 直接调用 MarketService 判断开盘
        if not MarketService.is_us_market_open():
            logging.info("US market not open")
            return
            
        await self.ib_service.connect()
        logging.info(f"Account net_liq={self.ib_service.net_liquidation:.2f} cash={self.ib_service.available_cash:.2f}")
        
        stock = self._get_next_stock()
        if not stock:
            logging.info("No candidate stock")
            return
            
        try:
            name = f"{stock['name']}({stock['code']})"
            # 获取情绪
            emotion = await self.szdt.get_fresh_emotion_from_list(stock['type'], stock['code'])
            if not emotion or emotion.get('status') != 1:
                emotion = await self.szdt.get_stock_emotion(stock['code'], stock['lever'], stock['emo_area'])
                if not emotion or emotion.get('status') != 1:
                    self._log('DEBUG', f"{name} 获取情绪失败，跳过。 {emotion}")
                    return
            
            score = emotion['data']['score']
            price = emotion['data']['price']
            logging.info(f"Emotion fetched {name} score={score} price={price}")

            position_qty = self.ib_service.get_position(stock['code'])
            position_value = position_qty * price
            portfolio_value = max(self.ib_service.net_liquidation, 1.0)
            position_ratio = 100 * position_value / portfolio_value

            # 过热/过冷保护并设置冷却
            if score > (stock['when_buy'] + 10) and position_qty == 0:
                self._log('DEBUG', f"{name} 无持仓且情绪分数过高(当前:{score},买:{stock['when_buy']})，冷却2小时")
                with get_db_session(self.account_id) as db:
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=2), reason='无持仓且情绪分数过高'))
                return
            if score < (stock['when_sell'] - 10) and position_ratio > stock['max_position']:
                self._log('DEBUG', f"{name} 持仓已满(持仓:{position_ratio:.2f}% 上限:{stock['max_position']}%)且情绪分数过低(当前:{score},卖:{stock['when_sell']})，冷却2小时")
                with get_db_session(self.account_id) as db:
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=2), reason='持仓已满且情绪分数过低'))
                return

            # 买入条件
            if score <= stock['when_buy']:
                if position_ratio < stock['max_position']:
                    score_factor = min(1, max(0, (stock['when_buy'] - score) / (stock['when_buy'] + 100)))
                    score_factor = 3 ** (score_factor ** stock['buy_factor'])
                    buy_amount = min(self.ib_service.available_cash, stock['buy_amount'] * score_factor)
                    buy_quantity = int(buy_amount / price)
                    if buy_quantity >= 1:
                        trade = await self.ib_service.place_market_order(stock['code'], 'BUY', buy_quantity)
                        order_id = trade.order.orderId
                        self._log('INFO', f"{name} BUY x{buy_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                        logging.info(f"{name} BUY x{buy_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                    else:
                        self._log('INFO', f"{name} 可用资金 {self.ib_service.available_cash:.2f} 不足，跳过买入")
                with get_db_session(self.account_id) as db:
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=12), reason='决策后冷却12h'))
                return

            # 卖出条件
            if score >= stock['when_sell'] and position_qty > 0:
                score_factor = min(1, max(0, (score - stock['when_sell']) / (100 - stock['when_sell'])))
                score_factor = 3 ** (score_factor ** stock['sell_factor'])
                sell_amount = stock['sell_amount'] * score_factor
                sell_quantity = int(sell_amount / price)
                sell_quantity = max(min(sell_quantity, int(position_qty)), 1)
                if sell_quantity > 0:
                    trade = await self.ib_service.place_market_order(stock['code'], 'SELL', sell_quantity)
                    order_id = trade.order.orderId
                    self._log('INFO', f"{name} SELL x{sell_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                    logging.info(f"{name} SELL x{sell_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                with get_db_session(self.account_id) as db:
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=12), reason='决策后冷却1h'))
                return

            # 中性区间：设置距离相关冷却
            score_delta = min(score - stock['when_buy'], stock['when_sell'] - score)
            cooldown_minutes = round(min(720, pow(1.6, score_delta)))
            if cooldown_minutes > 1:
                with get_db_session(self.account_id) as db:
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(minutes=cooldown_minutes), reason='情绪分数距离买卖阈值较远'))
            self._log('DEBUG', f"{name} 情绪分数介于买卖阈值之间(当前:{score},买:{stock['when_buy']},卖:{stock['when_sell']}), 冷却{cooldown_minutes}分钟")
            logging.info(f"{name} neutral score={score} buy={stock['when_buy']} sell={stock['when_sell']} cooldown={cooldown_minutes}m")
        except Exception as e:
            logging.error(f"USAutoTrader 处理 {stock['code']} 失败: {e}")
            self._log('ERROR', f"{stock['code']} 处理失败: {e}")

def start_us_auto_trader(account_id):
    trader = USAutoTrader(account_id)

    async def tick():
        await trader.run_once()

    def loop():
        logging.info("USAutoTrader started")
        evloop = asyncio.new_event_loop()
        asyncio.set_event_loop(evloop)
        while True:
            try:
                evloop.run_until_complete(tick())
            except Exception as e:
                logging.error(f"USAutoTrader tick error: {e}")
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()
