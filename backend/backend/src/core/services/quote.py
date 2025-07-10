from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from datetime import datetime, date
from enum import Enum
import pandas as pd
from sqlalchemy import and_, desc
from ..database import Session, StockKline


class SubType(Enum):
    Quote = "QUOTE"
    OrderBook = "ORDER_BOOK"
    Ticker = "TICKER"

class QuoteEvent:
    def __init__(self, symbol: str, price: float, timestamp: datetime):
        self.symbol = symbol
        self.price = price
        self.timestamp = timestamp

class QuoteObserver:
    def on_quote(self, event: QuoteEvent):
        pass

class QuoteProvider(ABC):

    @abstractmethod
    def get_static_info(self, symbols: List[str]) -> List[Dict]:
        """获取静态信息，包括市值、发行股数等信息"""
        pass

    @abstractmethod
    def get_quote_batch(self, symbols: List[str]) -> List[Dict]:
        """批量获取实时行情数据"""
        pass

    @abstractmethod
    def get_quote(self, symbol: str) -> Dict:
        """获取实时交易价格
        参数:
          symbol: 股票代码
        返回:
          实时行情数据
            字段:
              price: 当前价格
              ... (其他字段)
        """
        pass

    """行情数据提供者接口"""
    
    @abstractmethod
    def get_candlesticks(self, symbol: str, count: int) -> List[Dict]:
        """获取K线数据
        
        Args:
            symbol: 股票代码
            count: K线数量，-1表示获取所有历史数据
            
        Returns:
            List[Dict]: K线数据列表,每个K线包含:
                - timestamp: datetime
                - open: 开盘价
                - high: 最高价
                - low: 最低价
                - close: 收盘价
                - volume: 成交量
                - turnover: 成交额
        """
        pass

    @abstractmethod
    def subscribe(self, symbols: List[str], sub_types: List[str], is_first_push: bool = False) -> None:
        """订阅标的的行情数据"""
        pass

    @abstractmethod
    def set_on_quote(self, observer: QuoteObserver):
        """设置行情回调"""
        pass

    @abstractmethod
    def unsubscribe(self, symbols: List[str], sub_types: List[str]):
        """取消订阅"""
        pass

    @abstractmethod
    def get_option_quote_batch(self, symbols: List[str]) -> List[Dict]:
        """获取期权实时行情数据
        
        Args:
            symbols: 期权代码列表
            
        Returns:
            List[Dict]: 期权行情数据列表
        """
        pass

class QuoteService:
    """行情数据服务"""
    
    def __init__(self, provider: QuoteProvider):
        self.provider = provider
        self.db = Session()
        
    def get_static_info(self, symbols: List[str]) -> List[Dict]:
        """获取静态信息，包括市值、发行股数等信息
        
        Args:
            symbols: 股票代码列表
            
        Returns:
            List[Dict]: 基础信息列表
        """
        batch_size = 500
        result = []
        
        # 如果symbols长度超过500,分批处理
        for i in range(0, len(symbols), batch_size):
            batch_symbols = symbols[i:i + batch_size]
            batch_result = self.provider.get_static_info(batch_symbols)
            result.extend(batch_result)
            
        return result

    def get_quote_batch(self, symbols: List[str]) -> List[Dict]:
        """批量获取实时行情数据"""
        return self.provider.get_quote_batch(symbols)

    def get_option_quote_batch(self, symbols: List[str]) -> List[Dict]:
        """获取期权实时行情数据"""
        return self.provider.get_option_quote_batch(symbols)
    
    def get_quote(self, symbol: str) -> Dict:
        """获取实时行情数据
        参数:
          symbol: 股票代码
        返回:
          实时行情数据
            字段:
              price: 当前价格
              ... (其他字段)
        """
        return self.provider.get_quote(symbol)
        
    def get_klines(self, symbol: str, count: int, end_date: Optional[date] = None, cache_only: Optional[bool] = False) -> List[Dict]:
        """获取K线数据(带缓存)
        
        Args:
            symbol: 股票代码
            count: 需要的K线数量
            end_date: 结束日期,默认为当前日期
            
        Returns:
            List[Dict]: K线数据列表
        """
        # 确定结束日期
        end_date = end_date or date.today()
        
        # 从数据库获取指定数量的历史K线
        db_klines = self.db.query(StockKline).filter(
            and_(
                StockKline.symbol == symbol,
                StockKline.date <= end_date
            )
        ).order_by(desc(StockKline.date)).limit(count).all()
        
        # 反转列表使其按日期升序
        db_klines = db_klines[::-1]
        
        # 如果没有数据或最新数据不是end_date
        if not cache_only and (not db_klines or db_klines[-1].date < end_date):
            # 从接口获取数据
            if not db_klines:
                # 如果没有历史数据，直接获取请求的数量
                new_klines = self.provider.get_candlesticks(symbol, -1)
            else:
                # 计算需要补充最新的k线
                loss_count = (datetime.now().date() - db_klines[-1].date).days
                new_klines = self.provider.get_candlesticks(symbol, loss_count + 1)
            
            # 保存到数据库
            for kline in new_klines:
                db_kline = StockKline(
                    symbol=symbol,
                    date=kline['timestamp'],
                    open=kline['open'],
                    high=kline['high'],
                    low=kline['low'],
                    close=kline['close'],
                    volume=kline['volume'],
                    turnover=kline['turnover']
                )
                self.db.merge(db_kline)
            
            self.db.commit()
            
            # 重新查询
            db_klines = self.db.query(StockKline).filter(
                and_(
                    StockKline.symbol == symbol,
                    StockKline.date <= end_date
                )
            ).order_by(desc(StockKline.date)).limit(count).all()
            
            # 反转列表使其按日期升序
            db_klines = db_klines[::-1]
        
        # 转换为字典列表
        return [{
            'timestamp': datetime.combine(k.date, datetime.min.time()),
            'open': float(k.open),
            'high': float(k.high),
            'low': float(k.low),
            'close': float(k.close),
            'volume': k.volume,
            'turnover': float(k.turnover)
        } for k in db_klines]
    
    def subscribe(self, symbols: List[str], sub_types: List[str], is_first_push: bool = False) -> None:
        self.provider.subscribe(symbols, sub_types, is_first_push)

    def set_on_quote(self, observer: QuoteObserver):
        self.provider.set_on_quote(observer)

    def unsubscribe(self, symbols: List[str], sub_types: List[str]):
        self.provider.unsubscribe(symbols, sub_types)
