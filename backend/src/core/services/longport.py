import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta
import time
from ratelimit import limits, sleep_and_retry
from longport.openapi import (
    Config, Language, QuoteContext, TradeContext, TopicType, 
    OrderSide as LPOrderSide, OrderType as LPOrderType, TimeInForceType as LPTimeInForceType, OutsideRTH as LPOutsideRTH,
    Period, AdjustType, PushOrderChanged, OrderStatus, SubType as LPSubType, PushQuote
)
from ..utils import load_config_file, save_config_file, mask_account_id
from ..models.account import AccountCfg
from .trade import TradeService, OrderSide, OrderType, TimeInForceType, OutsideRTH
from .quote import QuoteProvider, SubType, QuoteObserver, QuoteEvent

order_side_map = {
    OrderSide.Buy: LPOrderSide.Buy,
    OrderSide.Sell: LPOrderSide.Sell
}

order_type_map = {
    OrderType.MO: LPOrderType.MO,
    OrderType.LO: LPOrderType.LO
}

time_in_force_map = {
    TimeInForceType.Day: LPTimeInForceType.Day,
    TimeInForceType.GTC: LPTimeInForceType.GoodTilCanceled
}

outside_rth_map = {
    OutsideRTH.AnyTime: LPOutsideRTH.AnyTime,
    OutsideRTH.RTHOnly: LPOutsideRTH.RTHOnly
}

# 创建一个映射字典
sub_type_map = {
    SubType.Quote: LPSubType.Quote, #基础报价
    SubType.OrderBook: LPSubType.Depth, #摆盘
    SubType.Ticker: LPSubType.Trade #逐笔
}

class LongPortService(QuoteProvider, TradeService):
    """长桥接口服务"""
    
    _instances = {}

    def __new__(cls, account_id: str):
        if account_id not in cls._instances:
            instance = super(LongPortService, cls).__new__(cls)
            cls._instances[account_id] = instance
        return cls._instances[account_id]

    def __init__(self, account_id: str):
        if hasattr(self, 'initialized') and self.initialized:
            return

        self.account_id = account_id
        self.account_cfg = load_config_file(account_id, "evc_config.json", AccountCfg)
        self.__init_lp_config()

        self.ctx = QuoteContext(self.lp_config)
        self.trade_ctx = TradeContext(self.lp_config)
        self.trade_ctx.subscribe([TopicType.Private])

        self.initialized = True

    def __init_lp_config(self):
        self.lp_config = Config(
            app_key=self.account_cfg.app_key,
            app_secret=self.account_cfg.app_secret,
            access_token=self.account_cfg.access_token,
            language = Language.ZH_CN
        )
        
        if (self.account_cfg.access_token_expired_at is not None and 
            time.time() < self.account_cfg.access_token_expired_at - timedelta(days=7).total_seconds()):
            return
            
        access_token = self.lp_config.refresh_access_token()
        self.account_cfg.access_token = access_token
        self.account_cfg.access_token_expired_at = (datetime.now() + timedelta(days=365)).timestamp()
        
        # 保存更新后的配置
        save_config_file(self.account_id, "evc_config.json", self.account_cfg)

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_static_info(self, symbols: List[str]) -> List[dict]:
        """获取标的基础信息
        
        Args:
            symbols: 股票代码列表
            
        Returns:
            List[dict]: 包含基础信息的字典列表
        """
        try:
            resp = self.ctx.static_info(symbols)
            if resp:
                return [{
                    'symbol': info.symbol,
                    'name_cn': info.name_cn,
                    'total_shares': info.total_shares,
                    'circulating_shares': info.circulating_shares,
                    'lot_size': info.lot_size,
                    'currency': info.currency,
                    'eps': float(info.eps) if info.eps else None,
                    'eps_ttm': float(info.eps_ttm) if info.eps_ttm else None, 
                    'bps': float(info.bps) if info.bps else None
                } for info in resp]
            return []
        except Exception as e:
            logging.error(f"获取{symbols}基础信息失败: {str(e)}")
            return []
    
    def get_quote(self, symbol: str) -> Dict:
        """获取标的实时行情"""
        try:
            return self.get_quote_batch([symbol])[0]
        except Exception as e:
            logging.error(f"获取{symbol}实时行情失败: {str(e)}")
            return {}

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_quote_batch(self, symbols: List[str]) -> List[Dict]:
        """批量获取实时行情数据"""
        try:
            resp = self.ctx.quote(symbols)
            if resp:
                return [{
                    'symbol': quote.symbol,
                    'price': float(quote.last_done),
                    'change': float(quote.last_done) - float(quote.prev_close),
                    'percent_change': (float(quote.last_done) - float(quote.prev_close)) / float(quote.prev_close) * 100,
                    'high': float(quote.high),
                    'low': float(quote.low),
                    'open': float(quote.open),
                    'prev_close': float(quote.prev_close),
                    'volume': quote.volume,
                    'turnover': float(quote.turnover),
                    'timestamp': quote.timestamp
                } for quote in resp]
            return []
        except Exception as e:
            logging.error(f"获取{symbols}实时行情失败: {str(e)}")
            return []

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_option_quote_batch(self, symbols: List[str]) -> List[Dict]:
        """批量获取期权实时行情数据"""
        try:
            resp = self.ctx.option_quote(symbols)
            if resp:
                return [{
                    'symbol': quote.symbol,
                    'price': float(quote.last_done),
                    'change': float(quote.last_done) - float(quote.prev_close),
                    'percent_change': (float(quote.last_done) - float(quote.prev_close)) / float(quote.prev_close) * 100,
                    'high': float(quote.high),
                    'low': float(quote.low),
                    'open': float(quote.open),
                    'prev_close': float(quote.prev_close),
                    'volume': quote.volume,
                    'turnover': float(quote.turnover),
                    'timestamp': quote.timestamp,
                    # 期权特有字段
                    'implied_volatility': float(quote.implied_volatility),
                    'open_interest': quote.open_interest,
                    'expiry_date': quote.expiry_date,
                    'strike_price': float(quote.strike_price),
                    'contract_multiplier': float(quote.contract_multiplier),
                    'contract_type': quote.contract_type.__class__.__name__,
                    'contract_size': float(quote.contract_size),
                    'direction': quote.direction.__class__.__name__,
                    'historical_volatility': float(quote.historical_volatility),
                    'underlying_symbol': quote.underlying_symbol
                } for quote in resp]
            return []
        except Exception as e:
            logging.error(f"获取期权{symbols}实时行情失败: {str(e)}")
            return []

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_candlesticks(self, symbol: str, count: int, period = 'd') -> List[Dict]:
        """实现QuoteProvider接口"""
        try:
            resp = self.ctx.candlesticks(
                symbol=symbol,
                period=Period.Day if period == 'd' else Period.Week if period == 'w' else Period.Month if period == 'm' else Period.Day,
                count=count if count > 0 else 1000,
                adjust_type=AdjustType.ForwardAdjust
            )
            if resp:
                return [{
                    'timestamp': candle.timestamp.date(),
                    'open': float(candle.open),
                    'high': float(candle.high),
                    'low': float(candle.low),
                    'close': float(candle.close),
                    'volume': candle.volume,
                    'turnover': float(candle.turnover)
                } for candle in resp]
            return []
        except Exception as e:
            logging.error(f"获取{symbol} K线数据失败: {str(e)}")
            return []

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_candlesticks_by_date(self, symbol: str, start: datetime.date, end: datetime.date, period = 'd') -> List[Dict]:
        """根据日期范围获取K线数据(自动分页, 从后向前获取)"""
        all_candlesticks = []
        current_end = end
        
        try:
            while start <= current_end:
                resp = self.ctx.history_candlesticks_by_date(
                    symbol=symbol,
                    period=Period.Day if period == 'd' else Period.Week if period == 'w' else Period.Month if period == 'm' else Period.Day,
                    start=start,
                    end=current_end,
                    adjust_type=AdjustType.ForwardAdjust
                )
                
                if not resp:
                    break
                    
                processed_resp = [{
                    'timestamp': candle.timestamp.date(),
                    'open': float(candle.open),
                    'high': float(candle.high),
                    'low': float(candle.low),
                    'close': float(candle.close),
                    'volume': candle.volume,
                    'turnover': float(candle.turnover)
                } for candle in resp]
                
                # Prepend this batch to the results (since we fetching latest first, and want chronological order)
                # But wait, resp is usually returned in chronological order for the requested range.
                # So if we requested [T_early, T_late] and got the latest 1000 items (T_x to T_late),
                # we should PREPEND these to our 'all_candlesticks' list which will eventually contain T_start...T_late.
                all_candlesticks = processed_resp + all_candlesticks
                
                if len(resp) < 1000:
                    # Fetched all remaining data in this range
                    break
                
                # Update End Date: The record immediately before the first one we just got
                first_date = resp[0].timestamp.date()
                next_end = first_date - timedelta(days=1)
                
                if next_end >= current_end:
                    # Prevent infinite loop if date doesn't retreat (shouldn't happen if API behaves)
                    logging.warning(f"Fetching loop stuck for {symbol} at {first_date}")
                    break
                    
                current_end = next_end
                
            return all_candlesticks
            
        except Exception as e:
            logging.error(f"获取{symbol}历史K线数据失败: {str(e)}")
            return []

    @sleep_and_retry
    @limits(calls=30, period=30)
    def submit_order(self, side: OrderSide, symbol: str, order_type: OrderType,
                     submitted_price: float, submitted_quantity: int,
                     time_in_force: TimeInForceType = TimeInForceType.Day,
                     outside_rth: OutsideRTH = OutsideRTH.AnyTime,
                     remark: str = "") -> str:
        """提交订单"""
        try:
            resp = self.trade_ctx.submit_order(
                side=order_side_map[side],
                symbol=symbol,
                order_type=order_type_map[order_type],
                submitted_price=submitted_price,
                submitted_quantity=submitted_quantity,
                time_in_force=time_in_force_map[time_in_force],
                outside_rth=outside_rth_map[outside_rth],
                remark=remark
            )
            return resp.order_id
        except Exception as e:
            logging.error(f"提交订单失败: {str(e)}")
            raise

    @sleep_and_retry
    @limits(calls=30, period=30)
    def today_orders(self, symbol: str = None, side: OrderSide = None) -> List[Dict]:
        """获取当日订单"""
        try:
            orders = self.trade_ctx.today_orders(symbol=symbol, side=order_side_map[side] if side else None)
            return [{
                'order_id': order.order_id,
                'symbol': order.symbol,
                'side': order.side,
                'quantity': order.quantity,
                'price': order.price,
                'status': order.status.__name__
            } for order in orders]
        except Exception as e:
            logging.error(f"获取当日订单失败: {str(e)}")
            return []
    
    @sleep_and_retry
    @limits(calls=10, period=1)
    def history_orders(self, symbol: str = None, side: OrderSide = None, time_interval: timedelta = None) -> List[Dict]:
        """获取历史订单"""
        try:
            # 转换参数
            lp_side = order_side_map.get(side) if side else None
            
            # 获取历史订单
            orders = self.trade_ctx.history_orders(
                symbol=symbol,
                side=lp_side,
                start_at = datetime.now() - time_interval if time_interval else None,
                end_at= datetime.now(),
                status = [OrderStatus.Filled]
            )
            
            if orders:
                return [{
                    'order_id': order.order_id,
                    'symbol': order.symbol,
                    'side': str(order.side),
                    'submitted_at': order.submitted_at,
                    'trigger_at': order.trigger_at,
                    'updated_at': order.updated_at,
                    'status': str(order.status)
                } for order in orders]
            return []
        except Exception as e:
            logging.error(f"获取历史订单失败: {str(e)}")
            return []
    
    @sleep_and_retry
    @limits(calls=30, period=30)
    def stock_positions(self) -> List[Dict]:
        """获取持仓信息"""
        try:
            response = self.trade_ctx.stock_positions()
            positions = response.channels[0].positions
            # 假设每个 position 是一个对象，需要转换为字典
            return [{
                'symbol': position.symbol,
                'symbol_name': position.symbol_name,
                'quantity': int(position.quantity),
                'available_quantity': position.available_quantity,
                'cost_price': position.cost_price,
                'market': str(position.market),
                'currency': position.currency,
                'init_quantity': position.init_quantity
            } for position in positions]
        except Exception as e:
            logging.error(f"获取持仓信息失败: {str(e)}")
            return []

    @sleep_and_retry
    @limits(calls=30, period=30)
    def account_balance(self) -> Dict:
        """获取账户资金"""
        try:
            resp = self.trade_ctx.account_balance()[0]
            return {
                'available_balance': resp.cash_infos[0].available_cash,
                'frozen_balance': resp.cash_infos[0].frozen_cash,
                'withdraw_cash': resp.cash_infos[0].withdraw_cash,
                'currency': resp.cash_infos[0].currency,
            }
        except Exception as e:
            logging.error(f"获取账户资金失败: {str(e)}")
            return {}

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_position_info(self, symbol: str) -> Dict:
        """获取特定股票的持仓信息"""
        try:
            resp = self.trade_ctx.stock_positions(symbols=[symbol])
            if not resp.channels:
                logging.warning(f"未找到任何持仓信息: {symbol}")
                return {}
            positions = resp.channels[0].positions
            if not positions:
                logging.warning(f"未找到任何持仓信息: {symbol}")
                return {}
            for position in positions:
                if position.symbol == symbol:
                    return {
                        'symbol': position.symbol,
                        'symbol_name': position.symbol_name,
                        'quantity': int(position.quantity),
                        'available_quantity': position.available_quantity,
                        'cost_price': position.cost_price,
                        'market': position.market,
                        'currency': position.currency,
                        'init_quantity': position.init_quantity
                    }
            return {}
        except Exception as e:
            logging.error(f"获取{symbol}持仓信息失败: {str(e)}")
            return {}

    def set_on_order_changed(self, callback):
        """设置订单变更回调"""
        self.trade_ctx.set_on_order_changed(lambda event: self._process_order_change(event, callback))

    def _process_order_change(self, event: PushOrderChanged, callback):
        """处理订单变更的内部逻辑"""
        # 调试日志不包含敏感信息
        logger.debug(f"Order state changed for symbol: {event.symbol}, status: {event.status}")
        
        masked_no = mask_account_id(event.account_no)
        if not event.account_no == self.account_cfg.account_no:
            logger.warning(f"Order account {masked_no} is not the listening account, ignore")
            return
        callback(event)

    def __on_quote(self, symbol: str, event:PushQuote):
        self.sub_observer.on_quote(QuoteEvent(symbol, event.last_done, event.timestamp))

    def set_on_quote(self, observer: QuoteObserver):
        self.sub_observer = observer
        self.ctx.set_on_quote(self.__on_quote)

    def subscribe(self, symbols: List[str], sub_types: List[str], is_first_push: bool = False) -> None:
        """订阅标的的行情数据"""
        try:
            # 使用映射字典
            sub_type_objects = [sub_type_map[sub_type] for sub_type in sub_types]
            self.ctx.subscribe(symbols, sub_type_objects, is_first_push)
            logging.info(f"Subscribed to {symbols} with types {sub_types}")
        except Exception as e:
            logging.error(f"订阅失败: {str(e)}")

    def unsubscribe(self, symbols: List[str], sub_types: List[str]):
        """取消所有订阅"""
        self.ctx.unsubscribe(symbols, [sub_type_map[sub_type] for sub_type in sub_types])
