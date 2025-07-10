import httpx
from datetime import datetime, timedelta
from typing import Dict, Any

class CNNService:
    def __init__(self):
        self.base_url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.cnn.com/",
                "Origin": "https://www.cnn.com"
            }
        )
    
    async def get_fear_greed_index(self, days: int = 1) -> Dict[str, Any]:
        """获取最新的CNN恐贪指数数据
        
        Args:
            days: 获取多少天的数据，默认1天
        """
        # 计算开始日期
        today = datetime.now()
        start_date = (today - timedelta(days=days-1)).strftime("%Y-%m-%d")
        
        url = f"{self.base_url}/{start_date}"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 418:
                raise Exception("请求被服务器拒绝，可能需要更新请求头或等待一段时间后重试")
            raise Exception(f"获取CNN恐贪指数失败: HTTP {e.response.status_code}")
        except httpx.RequestError as e:
            raise Exception(f"请求CNN恐贪指数失败: {str(e)}")
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()