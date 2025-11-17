import logging
import threading
import time
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo
import os
from typing import Dict, List
from ib_insync import IB, Stock, MarketOrder, util


from ..core.database import get_db_session, SzdtTradeStock, TradingLog, TradingState, StockCooldown
from ..core.services.szdt import SZDTService

import asyncio

class IBTrader:
    """IB 网关薄封装。若依赖不可用，则仅记录日志，不实际下单。"""

    def __init__(self):
        self.enabled = False
        self._positions: Dict[str, int] = {}
        self._available_cash: float = 0.0
        self.ib = IB()
        try:
            # 确保当前线程有事件循环（ib_insync 需要）
            util.startLoop()
            self._connect()
        except Exception as e:
            logging.warning(f"IB 初始化连接失败: {e}")

    def _connect(self):
        if self.ib.isConnected():
            return
        
        host = os.getenv('IB_HOST', '127.0.0.1')
        port = int(os.getenv('IB_PORT', '4001'))
        client_id = int(os.getenv('IB_CLIENT_ID', '1001'))
        
        try:
            logging.info(f"正在连接 IB: {host}:{port} clientId:{client_id}")
            self.ib.connect(host, port, clientId=client_id, readonly=False, timeout=5)
            self.enabled = self.ib.isConnected()
            if not self.enabled:
                logging.warning("IB 连接失败")
        except Exception as e:
            self.enabled = False
            logging.warning(f"IB 连接异常: {e}")

    def refresh_account(self):
        try:
            if not self.ib.isConnected():
                logging.info("IB 连接已断开，正在尝试重新连接...")
                self._connect()
            
            if not self.ib.isConnected():
                logging.warning("IB 重新连接失败，无法刷新账户。")
                self.enabled = False
                return

            self.enabled = True
            # 刷新账户资金
            account_values = {v.tag: v.value for v in self.ib.accountValues()}
            logging.info(f"IB 账户值: {account_values}")
            self._net_liquidation = float(account_values.get('NetLiquidation', '0') or 0)
            self._available_cash = float(account_values.get('AvailableFunds', '0') or 0)
            # 刷新持仓
            self._positions = {}
            for p in self.ib.positions():
                symbol = p.contract.symbol
                self._positions[symbol] = self._positions.get(symbol, 0) + int(p.position)
        except Exception as e:
            logging.warning(f"刷新 IB 账户失败: {e}")
            self.enabled = False

    @property
    def net_liquidation(self) -> float:
        return self._net_liquidation

    @property
    def available_cash(self) -> float:
        return self._available_cash

    def get_position(self, symbol: str) -> int:
        return self._positions.get(symbol, 0)

    def place_market_order(self, symbol: str, quantity: int, action: str) -> str:
        if quantity <= 0:
            return ''
        if not self.enabled:
            logging.info(f"[DRY-RUN] {action} {quantity} {symbol}")
            return 'dry-run'
        try:
            contract = Stock(symbol.replace('US.', ''), 'SMART', 'USD')
            self.ib.qualifyContracts(contract)
            order = MarketOrder(action, quantity)
            trade = self.ib.placeOrder(contract, order)
            logging.info(f"placeOrder: {action} {quantity} {contract}")
            self.ib.sleep(0.5)
            try:
                status = getattr(trade.orderStatus, 'status', '')
                logging.info(f"orderStatus: {status} oid={trade.order.orderId}")
            except Exception:
                pass
            return str(trade.order.orderId)
        except Exception as e:
            logging.error(f"IB 下单失败 {action} {symbol} x{quantity}: {e}")
            return ''

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    def get_contract_details(self, symbol: str):
        try:
            return self.ib.reqContractDetails(Stock(symbol, 'SMART', 'USD'))
        except Exception:
            return []


class USAutoTrader:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.ib = IBTrader()
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

    def _nth_weekday_of_month(self, year: int, month: int, weekday: int, n: int) -> date:
        d = date(year, month, 1)
        count = 0
        while True:
            if d.weekday() == weekday:
                count += 1
                if count == n:
                    return d
            d += timedelta(days=1)

    def _last_weekday_of_month(self, year: int, month: int, weekday: int) -> date:
        d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d

    def _observed(self, d: date) -> date:
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    def _easter_date(self, year: int) -> date:
        a = year % 19
        b = year // 100
        c = year % 100
        d0 = (19 * a + b - b // 4 - ((b + 8) // 25) + 15) % 30
        e = (32 + 2 * (b % 4) + 2 * (c // 4) - d0 - (c % 4) - (a // 29)) % 7
        f = d0 + e + 114
        m = f // 31
        day = (f % 31) + 1
        return date(year, m, day)

    def _is_us_market_holiday(self, d: date) -> bool:
        y = d.year
        holidays = {
            self._observed(date(y, 1, 1)),
            self._nth_weekday_of_month(y, 1, 0, 3),
            self._nth_weekday_of_month(y, 2, 0, 3),
            self._easter_date(y) - timedelta(days=2),
            self._last_weekday_of_month(y, 5, 0),
            self._observed(date(y, 6, 19)),
            self._observed(date(y, 7, 4)),
            self._nth_weekday_of_month(y, 9, 0, 1),
            self._nth_weekday_of_month(y, 11, 3, 4),
            self._observed(date(y, 12, 25)),
        }
        return d in holidays

    def _get_us_market_close_time(self, d: date) -> dtime:
        y = d.year
        thanksgiving = self._nth_weekday_of_month(y, 11, 3, 4)
        if d == thanksgiving + timedelta(days=1):
            return dtime(13, 0)
        if d.month == 12 and d.day == 24 and d.weekday() < 5:
            return dtime(13, 0)
        return dtime(16, 0)

    def _us_market_open(self) -> bool:
        now = datetime.now(ZoneInfo('US/Eastern'))
        if now.weekday() >= 5:
            logging.info(f'美股 {now.strftime("%Y-%m-%d")} 为周末，不交易')
            return False
        if self._is_us_market_holiday(now.date()):
            logging.info('美股 当日节假日休市')
            return False
        start = dtime(9, 45)
        end = self._get_us_market_close_time(now.date())
        if not (start <= now.time() <= end):
            logging.info("US market outside local hours")
            return False
        
        return True

    def _fetch_candidates(self) -> List[Dict]:
        """在会话内将 ORM 对象转换为纯字典，避免会话关闭后的懒加载/刷新。"""
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
        """仿 trade.py：按状态索引 + 冷却筛选，每次只取一只。"""
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
        if not self._us_market_open():
            logging.info("US market not open")
            return
        self.ib.refresh_account()
        logging.info(f"Account net_liq={getattr(self.ib, 'net_liquidation', 0):.2f} cash={self.ib.available_cash:.2f}")
        stock = self._get_next_stock()
        if not stock:
            logging.info("No candidate stock")
            return
        try:
            name = f"{stock['name']}({stock['code']})"
            # 先用列表接口低成本获取情绪
            emotion = await self.szdt.get_fresh_emotion_from_list(stock['type'], stock['code'])
            if not emotion or emotion.get('status') != 1:
                # 回退到逐标接口（可能有额度消耗）
                emotion = await self.szdt.get_stock_emotion(stock['code'], stock['lever'], stock['emo_area'])
                if not emotion or emotion.get('status') != 1:
                    self._log('DEBUG', f"{name} 获取情绪失败，跳过。 {emotion}")
                    return
            score = emotion['data']['score']
            price = emotion['data']['price']
            logging.info(f"Emotion fetched {name} score={score} price={price}")

            position_qty = self.ib.get_position(stock['code'])
            position_value = position_qty * price
            portfolio_value = max(self.ib.net_liquidation, 1.0)
            position_ratio = 100 * position_value / portfolio_value

            # 过热/过冷保护并设置冷却（先基于列表情绪判断触发，再二次确认）
            if score > (stock['when_buy'] + 10) and position_qty == 0:
                self._log('DEBUG', f"{name} 无持仓且情绪分数过高(当前:{score},买:{stock['when_buy']})，冷却2小时")
                with get_db_session(self.account_id) as db:
                    from datetime import timedelta
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=2), reason='无持仓且情绪分数过高'))
                return
            if score < (stock['when_sell'] - 10) and position_ratio > stock['max_position']:
                self._log('DEBUG', f"{name} 持仓已满(持仓:{position_ratio:.2f}% 上限:{stock['max_position']}%)且情绪分数过低(当前:{score},卖:{stock['when_sell']})，冷却2小时")
                with get_db_session(self.account_id) as db:
                    from datetime import timedelta
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=2), reason='持仓已满且情绪分数过低'))
                return

            # 买入条件
            if score <= stock['when_buy']:
                if position_ratio < stock['max_position']:
                    score_factor = min(1, max(0, (stock['when_buy'] - score) / (stock['when_buy'] + 100)))
                    score_factor = 3 ** (score_factor ** stock['buy_factor'])
                    buy_amount = min(self.ib.available_cash, stock['buy_amount'] * score_factor)
                    buy_quantity = int(buy_amount / price)
                    if buy_quantity >= 1:
                        order_id = self.ib.place_market_order(stock['code'], buy_quantity, 'BUY')
                        self._log('INFO', f"{name} BUY x{buy_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                        logging.info(f"{name} BUY x{buy_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                    else:
                        self._log('INFO', f"{name} 可用资金 {self.ib.available_cash:.2f} 不足，跳过买入")
                with get_db_session(self.account_id) as db:
                    from datetime import timedelta
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=12), reason='决策后冷却12h'))
                return

            # 卖出条件
            if score >= stock['when_sell'] and position_qty > 0:
                score_factor = min(1, max(0, (score - stock['when_sell']) / (100 - stock['when_sell'])))
                score_factor = 3 ** (score_factor ** stock['sell_factor'])
                sell_amount = stock['sell_amount'] * score_factor
                sell_quantity = int(sell_amount / price)
                sell_quantity = max(min(sell_quantity, position_qty), 1)
                if sell_quantity > 0:
                    order_id = self.ib.place_market_order(stock['code'], sell_quantity, 'SELL')
                    self._log('INFO', f"{name} SELL x{sell_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                    logging.info(f"{name} SELL x{sell_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                with get_db_session(self.account_id) as db:
                    from datetime import timedelta
                    db.add(StockCooldown(cli_id=self.cli_id, stock_code=stock['code'], until=datetime.now() + timedelta(hours=12), reason='决策后冷却1h'))
                return

            # 中性区间：设置距离相关冷却
            score_delta = min(score - stock['when_buy'], stock['when_sell'] - score)
            from math import pow
            cooldown_minutes = round(min(720, pow(1.6, score_delta)))
            if cooldown_minutes > 1:
                with get_db_session(self.account_id) as db:
                    from datetime import timedelta
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
