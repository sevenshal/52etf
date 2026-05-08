from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from datetime import datetime, date
from enum import Enum


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
    def get_candlesticks(self, symbol: str, count: int, period: str = 'd') -> List[Dict]:
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

    QUOTE_BATCH_SIZE = 500
    
    def __init__(self, provider: QuoteProvider):
        self.provider = provider
        
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
        result = []
        for i in range(0, len(symbols), self.QUOTE_BATCH_SIZE):
            batch_symbols = symbols[i:i + self.QUOTE_BATCH_SIZE]
            result.extend(self.provider.get_quote_batch(batch_symbols) or [])
        return result

    def get_option_quote_batch(self, symbols: List[str]) -> List[Dict]:
        """获取期权实时行情数据"""
        result = []
        for i in range(0, len(symbols), self.QUOTE_BATCH_SIZE):
            batch_symbols = symbols[i:i + self.QUOTE_BATCH_SIZE]
            result.extend(self.provider.get_option_quote_batch(batch_symbols) or [])
        return result
    
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
        
    @staticmethod
    def _normalize_kline(item: Dict) -> Dict:
        timestamp = item.get("timestamp")
        if isinstance(timestamp, datetime):
            normalized_timestamp = timestamp
        elif isinstance(timestamp, date):
            normalized_timestamp = datetime.combine(timestamp, datetime.min.time())
        else:
            normalized_timestamp = timestamp

        return {
            "timestamp": normalized_timestamp,
            "open": item.get("open"),
            "high": item.get("high"),
            "low": item.get("low"),
            "close": item.get("close"),
            "volume": item.get("volume"),
            "turnover": item.get("turnover"),
        }

    @staticmethod
    def _to_date(value) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return None

    def _get_klines_by_date(self, symbol: str, start_date: date, end_date: date, period: Optional[str]) -> List[Dict]:
        if not hasattr(self.provider, "get_candlesticks_by_date"):
            raise NotImplementedError("Provider does not support date range fetching")
        data = self.provider.get_candlesticks_by_date(symbol, start_date, end_date, period)
        return [self._normalize_kline(k) for k in data or []]

    def get_klines(self, symbol: str, start_date: date, end_date: date, period: Optional[str] = 'd') -> List[Dict]:
        """获取K线数据
        
        Args:
            symbol: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            List[Dict]: K线数据列表
        """
        parsed_start_date = self._to_date(start_date)
        parsed_end_date = self._to_date(end_date)
        if not parsed_start_date or not parsed_end_date:
            raise ValueError("get_klines only supports date range fetching: start_date and end_date are required")
        if parsed_start_date > parsed_end_date:
            return []
        return self._get_klines_by_date(symbol, parsed_start_date, parsed_end_date, period)
    
    def subscribe(self, symbols: List[str], sub_types: List[str], is_first_push: bool = False) -> None:
        self.provider.subscribe(symbols, sub_types, is_first_push)

    def set_on_quote(self, observer: QuoteObserver):
        self.provider.set_on_quote(observer)

    def unsubscribe(self, symbols: List[str], sub_types: List[str]):
        self.provider.unsubscribe(symbols, sub_types)
