from typing import Dict, List, Optional
import httpx
import logging
import os
import re
import threading
from datetime import datetime

class FMPService:
    """Financial Modeling Prep API 服务"""
    _api_key_lock = threading.Lock()
    _api_key_index = 0
    
    def __init__(self):
        fallback_api_key = "OL3LA1wnJ5pVzhEKSZCIJ3uKHtvIHWB8"
        self.base_url = "https://financialmodelingprep.com/api/v3"
        self.stable_base_url = "https://financialmodelingprep.com/stable"
        self.logger = logging.getLogger("FMPService")
        self.api_keys = self._load_api_keys(fallback_api_key)
        self.api_key = self.api_keys[0]

    def _load_api_keys(self, fallback_api_key: str) -> List[str]:
        raw_values = [
            os.getenv("FMP_API_KEYS"),
            os.getenv("FMP_API_KEY"),
            os.getenv("FMP_API_TOKEN"),
            'n5HxCqbrt96Kw3J2rAZ7kQlBxFrISKsd',
            'G1EB4dsFkzSNGv2X1TuPoDhBZiLBucrt',
            'PEliVS2Zk3NxfSVj2LXS7zLR0TEn0fw0',
            'pq4BZ0KzPb2dU0c61HCn6yktvSbZ7CM8'
        ]
        keys: List[str] = []
        for raw_value in raw_values:
            for item in re.split(r"[\s,;]+", str(raw_value or "").strip()):
                key = item.strip()
                if key and key not in keys:
                    keys.append(key)
        return keys or [fallback_api_key]

    def _next_api_key(self) -> str:
        with self._api_key_lock:
            key = self.api_keys[self.__class__._api_key_index % len(self.api_keys)]
            self.__class__._api_key_index += 1
            return key

    def _params_with_api_key(self, **params) -> Dict:
        return {**params, "apikey": self._next_api_key()}

    async def get_quote(self, symbol: str) -> Optional[Dict]:
        """获取股票当前报价数据，包含价格、PE等信息
        
        Args:
            symbol: 股票代码，例如 'SPY' 或 'AAPL'
            
        Returns:
            Dict: 报价数据，格式如下:
            {
                "symbol": "SPY",
                "name": "SPDR S&P 500 ETF Trust",
                "price": 592.05,
                "changesPercentage": 0.26928,
                "change": 1.59,
                "dayLow": 589.28,
                "dayHigh": 592.425,
                "yearHigh": 613.23,
                "yearLow": 481.8,
                "marketCap": 543372852638,
                "priceAvg50": 553.7418,
                "priceAvg200": 574.2445,
                "exchange": "AMEX",
                "volume": 31981250,
                "avgVolume": 78599109,
                "open": 591.25,
                "previousClose": 590.46,
                "eps": 22.95078,
                "pe": 25.8,
                "earningsAnnouncement": null,
                "sharesOutstanding": 917782033,
                "timestamp": 1747413921
            }
        """
        try:
            url = f"{self.base_url}/quote/{symbol}"
            
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=self._params_with_api_key())
                response.raise_for_status()
                data = response.json()
                
                if data and len(data) > 0:
                    return data[0]
                    
            return None
                    
        except Exception as e:
            self.logger.error(f"获取{symbol}报价数据失败: {str(e)}")
            return None

    def get_company_profile(self, symbol: str) -> Optional[Dict]:
        """同步获取单只股票 Company Profile。"""
        if not symbol:
            return None
        try:
            url = f"{self.stable_base_url}/profile"
            params = self._params_with_api_key(symbol=symbol)
            with httpx.Client(timeout=30) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data:
                return data
            return None
        except Exception as e:
            self.logger.error(f"获取{symbol}公司Profile失败: {str(e)}")
            return None

    async def get_historical_data(self, symbol: str, days: int = 365) -> Optional[Dict]:
        """获取股票原始历史数据（未除权除息）
        
        Args:
            symbol: 股票代码，例如 'SPY' 或 'AAPL'
            days: 获取最近多少天的数据，默认365天
            
        Returns:
            List[Dict]: 历史数据列表，格式如下:
            {
            "symbol": "SPY",
            "historical": [
                {
                "date": "2025-05-16",
                "close": 592.1
                }
                ]
            }
        """
        try:
            url = f"{self.base_url}/historical-price-full/{symbol}"
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    params=self._params_with_api_key(serietype="line", limit=days),
                )
                response.raise_for_status()
                return response.json()
                    
        except Exception as e:
            self.logger.error(f"获取{symbol}历史数据失败: {str(e)}")
            return None

    async def get_adjusted_historical_data(self, symbol: str, days: int = 365) -> Optional[List[Dict]]:
        """获取股票历史数据（除权除息调整后）
        
        Args:
            symbol: 股票代码，例如 'SPY' 或 'AAPL'
            days: 获取最近多少天的数据，默认365天
            
        Returns:
            List[Dict]: 历史数据列表，格式如下:
            [
                {
                    "symbol": "SPY",
                    "date": "2025-05-16",
                    "adjOpen": 591.25,
                    "adjHigh": 592.27,
                    "adjLow": 589.28,
                    "adjClose": 591.97,
                    "volume": 29069507
                },
                ...
            ]
        """
        try:
            url = f"{self.stable_base_url}/historical-price-eod/dividend-adjusted"
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    params=self._params_with_api_key(symbol=symbol, limit=days),
                )
                response.raise_for_status()
                data = response.json()
                return data
                    
        except Exception as e:
            self.logger.error(f"获取{symbol}除权除息后历史数据失败: {str(e)}")
            return None

    async def get_us10y_yield(self) -> Optional[float]:
        """获取美国10年期国债收益率（实时，单位%）
        Returns:
            float: 10年美债收益率（如4.25），获取失败返回None
        """
        try:
            url = f"{self.base_url}/quote/^TNX"
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=self._params_with_api_key())
                response.raise_for_status()
                data = response.json()
                if data and len(data) > 0 and 'price' in data[0]:
                    # ^TNX单位为0.1%，需除以10
                    return float(data[0]['price']) / 10
            return None
        except Exception as e:
            self.logger.error(f"获取10年美债收益率失败: {str(e)}")
            return None

    async def get_analyst_estimates(self, symbol: str, period: str = 'annual', page: int = 0, limit: int = 10) -> Optional[Dict]:
        """获取某个股票的财报预测数据
        
        Args:
            symbol: 股票代码，例如 'MSFT' 或 'AAPL'
            period: 财报周期，'annual' 或 'quarter'，默认为 'annual'
            page: 页码，默认为 0
            limit: 每页数据数量，默认为 10
            
        Returns:
            Dict: 财报预测数据，获取失败返回 None
        """
        try:
            url = f"{self.stable_base_url}/analyst-estimates/{symbol}"
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    params=self._params_with_api_key(period=period, page=page, limit=limit),
                )
                response.raise_for_status()
                return response.json()
                    
        except Exception as e:
            self.logger.error(f"获取{symbol}财报预测数据失败: {str(e)}")
            return None
