import os
import time
import logging
from datetime import datetime, timedelta, date
import pandas as pd
from typing import List, Optional, Dict
from ..core.utils import sendmail
from ..core.models.strategy import StrategyCfg
from ..core.models.trade import TradeOperation
from ..core.models.account import SzdtActiveCode
from ..core.utils import load_config_file, get_data_file
from ..core.services.longport import LongPortService, QuoteProvider
from ..core.services.futu import FutuTradeService
from ..core.services.evc import EVCService
from ..core.database import StockEVC, Session, StockTag, stock_tags, EVCTradeLog, get_db_session
from ..core.services.trade import TradeService, OrderSide, OrderType, TimeInForceType, OutsideRTH
from ..core.services.quote import QuoteService, SubType, QuoteEvent, QuoteObserver
from ..core.services.szdt import SZDTService
from .utils import is_stock_overvalued, is_stock_undervalued
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import traceback  # 添加到文件顶部的导入部分

current_directory = os.path.dirname(os.path.realpath(__file__))

class ConfigFileHandler(FileSystemEventHandler):
    def __init__(self, strategy):
        self.strategy = strategy
        self.last_reload_time = 0
        self.cooldown = 1  # 冷却时间（秒）

    def on_modified(self, event):
        # 检查是否是目标配置文件
        if event.src_path.endswith('evc_strategy.json'):
            current_time = time.time()
            # 检查是否在冷却期内
            if current_time - self.last_reload_time > self.cooldown:
                self.strategy.reload_config()
                self.last_reload_time = current_time

class EvcStrategy(QuoteObserver):
    def __init__(self, account_id: str, account_type: str):
        self.account_id = account_id
        self.account_type = account_type
        self.symbols_to_subscribe = []
        self.log = logging.getLogger(self.__class__.__name__)
        self.evc_service = EVCService()

        self.szdt_code:SzdtActiveCode  = load_config_file(account_id, "szdt_activated.json", SzdtActiveCode)

        self.emotion_service = SZDTService()
        
        # 初始化长桥接口，提供行情服务
        long_port_service:QuoteProvider = LongPortService(account_id)
        self.quote_service = QuoteService(provider=long_port_service)
        self.quote_service.set_on_quote(self)
        # 初始化交易服务
        self.trade_service =long_port_service if account_type == 'longport' else FutuTradeService()
        self.db_session = Session()

        # 设置文件监听
        self.config_path = get_data_file(account_id, "evc_strategy.json")
        self.observer = Observer()
        self.observer.schedule(ConfigFileHandler(self), self.config_path, recursive=False)
        self.observer.start()

        self.process()

    def _log_trade_operation(self, trade_log: EVCTradeLog):
        """记录交易操作到数据库"""
        self.log.info(f'{trade_log.symbol} {trade_log.operation} {trade_log.quantity} {trade_log.price} {trade_log.reason}')
        with get_db_session(self.account_id) as session:
            session.add(trade_log)
    
    def get_undervalued_stocks(self) -> List[StockEVC]:
        """根据策略配置从数据库中获取低估股票"""
        try:
            # 查询低估股票并且包含特定标签中的任意一个
            tag_ids = ["97638d21-2feb-4e7c-b47f-1984ff71dda6", "fbef4442-9f95-45e6-9859-b95f34889a5e"]
            today = date.today()
            p45d = today - timedelta(days=45)
            undervalued_stocks = self.db_session.query(StockEVC).join(stock_tags).join(StockTag).filter(
                StockEVC.date == today,
                StockEVC.fair_value_date >= p45d,  # 添加45天内的条件
                StockEVC.is_under == True,
                StockEVC.pe_ratio > 0,
                StockEVC.pe_ratio / StockEVC.forward_pe_ratio > self.strategy_cfg.next_fy_growth_threshold,
                StockEVC.last_price < StockEVC.fair_value_lo * self.strategy_cfg.undervalue_threshold * 1.1,
                (StockEVC.forward_next_fy_lo / StockEVC.fair_value_lo) > self.strategy_cfg.next_fy_growth_threshold,
                (StockEVC.forward_next_fy_hi / StockEVC.fair_value_hi) > self.strategy_cfg.next_fy_growth_threshold,
                StockTag.id.in_(tag_ids)
            ).all()
            return undervalued_stocks
        except Exception as e:
            self.log.error(f"Error fetching undervalued stocks: {str(e)}")
            self.log.error(traceback.format_exc())  # 打印完整堆栈
            return []
        
    def __refresh_evc_info_from_db(self, symbols: List[str]):
        self.subscribed_symbols_evc_info = {}
        today = date.today()
        evc_info_list = self.db_session.query(StockEVC).filter(StockEVC.symbol.in_(symbols), StockEVC.date == today).all()
        for evc_info in evc_info_list:
            self.subscribed_symbols_evc_info[evc_info.symbol] = evc_info

    def __is_option(self, symbol: str) -> bool:
        # symbol 包含数字是期权
        return any(char.isdigit() for char in symbol)

    def process(self):
        # 重新加载策略配置
        self.strategy_cfg = load_config_file(self.account_id, "evc_strategy.json", StrategyCfg)
        
        # 检查自动交易开关
        if not self.strategy_cfg.auto_trading_enabled:
            self.log.info("自动交易已关闭，跳过处理")
            return

        undervalued = self.get_undervalued_stocks()
        self.log.info(f"低估股票代码 {[ x.symbol for x in undervalued]}")
        self.hold_list = self.trade_service.stock_positions()
        self.hold_list = [hold for hold in self.hold_list if not self.__is_option(hold['symbol'])]
        self.log.info(f"持仓股票代码: {[ x['symbol'] for x in self.hold_list]}")
        self.today_buy_stocks = []
        self.today_sell_stocks = []

        # 订阅低估股票和持仓股票的行情
        self.symbols_to_subscribe = list(set([stock.symbol for stock in undervalued] + [hold['symbol'] for hold in self.hold_list]))
        self.__refresh_evc_info_from_db(self.symbols_to_subscribe)
        self.quote_service.subscribe(self.symbols_to_subscribe, [SubType.Quote])

    def on_quote(self, event: QuoteEvent):
        """处理接收到的行情事件"""
        try:
            symbol = event.symbol
            price = float(event.price)
            # 检查是否存在买入机会
            if self.is_buy_chance(symbol, price):
                self.execute_buy(symbol, price)
                self.today_buy_stocks.append(symbol)

            # 检查是否存在卖出机会
            if self.is_sell_chance(symbol, price):
                self.execute_sell(symbol, price)
                self.today_sell_stocks.append(symbol)
        except Exception as e:
            self.log.error(f"Error processing quote event: {event.symbol} {event.price}")
            self.log.error(traceback.format_exc())  # 打印完整堆栈

    def __get_or_refresh_evc_info(self, symbol: str):
        evc_info = self.subscribed_symbols_evc_info.get(symbol)
        if evc_info is None:
            evc_info = self.evc_service.stock_evc_info(symbol=symbol)
            self.subscribed_symbols_evc_info[symbol] = evc_info
        return evc_info

    def is_buy_chance(self, symbol: str, price: float) -> bool:
        # 实现买入机会检查逻辑
        if symbol in self.today_buy_stocks:
            return False
        evc_info = self.__get_or_refresh_evc_info(symbol)
        evc_info.last_price = price
        return is_stock_undervalued(evc_info, self.strategy_cfg.undervalue_threshold, self.strategy_cfg.next_fy_growth_threshold)

    def is_sell_chance(self, symbol: str, price: float) -> bool:
        if symbol in self.today_sell_stocks:
            return False
        # 实现卖出机会检查逻辑
        evc_info = self.__get_or_refresh_evc_info(symbol)
        evc_info.last_price = price
        return is_stock_overvalued(evc_info, self.strategy_cfg.current_fy_hi_threshold, self.strategy_cfg.next_fy_median_threshold)

    def execute_buy(self, symbol: str, price: float):
        # 实现买入操作

        hold_symbol_list = [hold['symbol'] for hold in self.hold_list]
        if symbol in hold_symbol_list:
            self.log.info(f"已有持仓 {symbol}")
            return

        # 计算 hold_list 和 today_buy_stocks 中去重后的股票代码数量
        unique_symbols = set(hold_symbol_list).union(self.today_buy_stocks)
        unique_count = len(unique_symbols)
        if unique_count >= self.strategy_cfg.max_hold_stock_count:
            self._log_trade_operation(EVCTradeLog(symbol=symbol, quantity=0, price=price, reason='持仓股票数量超过最大限制', operation='buy'))
            return

        evc_info = self.__get_or_refresh_evc_info(symbol)
        todayOrders = self.trade_service.today_orders(symbol=symbol, side=OrderSide.Buy)
        if len(todayOrders)>0:
            self._log_trade_operation(EVCTradeLog(symbol=symbol, quantity=0, price=price, reason='当日已存在其他买单', operation='buy'))
            return
        if self.szdt_code.activated:
            emo_resp = self.emotion_service.get_stock_emotion("US." + symbol.replace(".US", ""), lever = 1, emo_area= "us")
            if emo_resp["status"] != 1:
                self._log_trade_operation(EVCTradeLog(symbol=symbol, quantity=0, price=price, reason='获取情绪指标失败', operation='buy'))
                return
            emo_score =emo_resp["data"]["score"]
            if emo_score > 0:
                self._log_trade_operation(EVCTradeLog(symbol=symbol, quantity=0, price=price, reason=f'当前情绪 {emo_score}>=0', operation='buy'))
                return
        submitted_quantity = int(self.strategy_cfg.max_hold_amount_per_stock / price)
        self.trade_service.submit_order(
            side=OrderSide.Buy,
            symbol=symbol,
            order_type=OrderType.MO,
            submitted_price=price,
            submitted_quantity=submitted_quantity,
            time_in_force=TimeInForceType.Day,
            outside_rth=OutsideRTH.AnyTime,
            remark=f"低估,价格{price} 当前财年EVC估值范围{round(evc_info.fair_value_lo,2)}-{round(evc_info.fair_value_hi,2)}"
        )
        self._log_trade_operation(EVCTradeLog(symbol=evc_info.symbol, quantity=submitted_quantity, price=price, reason=f"低估,价格{price} 当前财年EVC估值范围{round(evc_info.fair_value_lo,2)}-{round(evc_info.fair_value_hi,2)}", operation='buy'))

    def execute_sell(self, symbol: str, price: float):
        # 实现卖出操作
        evc_info = self.__get_or_refresh_evc_info(symbol)
        evc_info.last_price = price
        todayOrders = self.trade_service.today_orders(symbol=symbol, side=OrderSide.Sell)
        if len(todayOrders)>0:
            self._log_trade_operation(EVCTradeLog(symbol=symbol, quantity=0, price=price, reason='当日已存在卖单', operation='sell'))
            return

        # 获取持仓信息
        position_info = self.trade_service.get_position_info(symbol)
        hold_quantity = position_info.get('quantity', 0)
        if hold_quantity == 0:
            return EVCTradeLog(symbol=symbol, quantity=0, price=price, reason='持仓数量为0', operation='sell')

        self.trade_service.submit_order(
            side=OrderSide.Sell,
            symbol=symbol,
            order_type=OrderType.MO,
            submitted_price=price,
            submitted_quantity=hold_quantity,
            time_in_force=TimeInForceType.Day,
            remark=f"高估卖出，当前价格{price}，估值范围{round(evc_info.fair_value_lo,2)}-{round(evc_info.fair_value_hi,2)}"
        )
        self._log_trade_operation(EVCTradeLog(symbol=evc_info.symbol, quantity=hold_quantity, price=price, reason=f"高估卖出，当前价格{price}，估值范围{round(evc_info.fair_value_lo,2)}-{round(evc_info.fair_value_hi,2)}", operation='sell'))

    def end_process(self):
        self.quote_service.unsubscribe(self.symbols_to_subscribe, [SubType.Quote])
        self.today_buy_stocks = []
        self.today_sell_stocks = []
        self.subscribed_symbols_evc_info = {}
        self.symbols_to_subscribe = []

    def reload_config(self):
        self.end_process()
        self.process()

    def __del__(self):
        """清理资源"""
        if hasattr(self, 'observer'):
            self.observer.stop()
            self.observer.join()