from abc import ABC, abstractmethod
from typing import List, Tuple
from ...core.models.etf import ETFHoldingsData
import logging

class ETFDataFetcher(ABC):
    """ETF数据获取基类"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }

    @abstractmethod
    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取ETF持仓数据
        
        Args:
            etf_symbol: ETF代码
            
        Returns:
            ETFHoldingsData: 标准格式的持仓数据
            
        Raises:
            Exception: 获取数据失败时抛出异常
        """
        pass 
