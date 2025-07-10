from typing import Dict, Optional
import httpx
import logging
from datetime import datetime

class MarketService:
    """市场数据服务"""
    
    def __init__(self):
        self.base_url = "https://markets.newyorkfed.org"
        self.logger = logging.getLogger("MarketService")

    async def get_fed_rate(self) -> Optional[Dict]:
        """获取美联储利率数据
        
        Returns:
            Dict: 利率数据，格式如下:
            {
                "effectiveDate": "2025-02-26",
                "type": "EFFR",
                "percentRate": 4.33,
                "percentPercentile1": 4.31,
                "percentPercentile25": 4.33,
                "percentPercentile75": 4.34,
                "percentPercentile99": 4.4,
                "targetRateFrom": 4.25,
                "targetRateTo": 4.5,
                "volumeInBillions": 108
            }
        """
        try:
            url = f"{self.base_url}/read"
            params = {
                "productCode": "50",
                "eventCodes": "500",
                "limit": "1",
                "startPosition": "0",
                "sort": "postDt:-1",
                "format": "json"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                
                if data and "refRates" in data and len(data["refRates"]) > 0:
                    return data["refRates"][0]
                    
                return None
                
        except Exception as e:
            self.logger.error(f"获取美联储利率数据失败: {str(e)}")
            return None