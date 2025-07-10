from typing import Dict, Optional
import httpx
import logging
from datetime import datetime
import hmac
import hashlib
from ratelimit import limits, sleep_and_retry
import json
from diskcache import Cache
from urllib.parse import urlencode
import os

class SZDTService:
    """守猪逮兔量化服务
    
    提供守猪逮兔(SZDT)量化平台的API服务封装,包括:
    - 股票情绪指标查询
    - 其他SZDT相关功能
    """
    
    def __init__(self):
        self.base_url = "https://szdt.tech/api"
        self.logger = logging.getLogger("SZDTService")
        self.secret_key = "X9XuQ89fjX4nq4FbdDM4LjVMYvDTsVVh"  # 从JS代码中提取的密钥
        self.auth_code = 'meHTJgAi8hEausoh4ACj5FzMeOelDSIm:924bcbb718c7104b75ae545ba7ae5633cd253194'
        self.partner_auth = 'meHTJgAi8hEausoh4ACj5FzMeOelDSIm'  # 合作伙伴认证码
        self.user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        
        # 创建缓存目录
        cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "quant")
        os.makedirs(cache_dir, exist_ok=True)
        
        # 初始化文件缓存
        self.cache = Cache(directory=cache_dir)
        
        # 设置不同API的缓存时间（秒）
        self.cache_ttls = {
            'etf_emotion': 300,  # 5分钟
            'etf_emotion_history': 3600 * 24,  # 24小时
            'stock_emotion': 300  # 5分钟
        }

    def _generate_signature(self, method: str, path: str, data: str, timestamp: str) -> str:
        """生成请求签名
        
        Args:
            method: HTTP方法 (GET, POST等)
            path: API路径
            data: 请求数据（JSON字符串）
            timestamp: ISO格式的时间戳
        """
        key = self.secret_key + timestamp
        message = f"{method}_{path}_{data}_{key}"
        signature = hmac.new(
            key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def _make_request(
        self,
        method: str,
        path: str,
        cache_key: Optional[str] = None,
        data: Optional[Dict] = None,
        is_partner: bool = False
    ) -> Optional[Dict]:
        """基础请求方法
        
        Args:
            method: HTTP方法 ("GET" 或 "POST")
            path: API路径（包含查询参数）
            cache_key: 缓存键，如果提供则使用缓存
            data: 请求体数据（POST请求）
            is_partner: 是否为合作伙伴API（合作伙伴API不需要签名）
            
        Returns:
            Optional[Dict]: API响应数据
        """
        # 检查缓存
        if cache_key:
            # 从缓存键中提取API类型
            api_type = cache_key.split(':')[0]
            ttl = self.cache_ttls.get(api_type, 300)  # 默认5分钟
            
            # 尝试从缓存获取数据
            cached_data = self.cache.get(cache_key)
            if cached_data is not None:
                self.logger.debug(f"从缓存获取数据: {cache_key}")
                return cached_data

        try:
            # 准备请求头
            headers = {
                "Accept": "application/json",
                "User-Agent": self.user_agent,
                "X-Auth": self.partner_auth if is_partner else self.auth_code
            }
            
            # 非合作伙伴API需要签名
            if not is_partner:
                timestamp = datetime.utcnow().isoformat() + "Z"
                headers["X-Timestamp"] = timestamp
                data_str = json.dumps(data) if data else ""
                headers["X-Signature"] = self._generate_signature(method, path, data_str, timestamp)

            # 构建完整URL
            url = f"{self.base_url}{path}"
            
            # 发送请求
            async with httpx.AsyncClient() as client:
                if method == "GET":
                    response = await client.get(url, headers=headers)
                else:
                    response = await client.post(url, headers=headers, json=data)
                
                response.raise_for_status()
                resp = response.json()
                
                # 如果提供了缓存键，则缓存响应
                if cache_key:
                    self.cache.set(cache_key, resp, expire=ttl)
                    self.logger.debug(f"缓存数据: {cache_key}, TTL: {ttl}秒")
                
                return resp
                
        except Exception as e:
            self.logger.error(f"请求失败 {method} {path}: {str(e)}")
            return None

    @sleep_and_retry
    @limits(calls=10, period=1)
    async def get_stock_emotion(
        self, 
        code: str, 
        lever: int = 1, 
        emo_area: str = "a"
    ) -> Optional[Dict]:
        """获取股票情绪指标"""
        # 构建查询参数
        query_params = {
            "code": code,
            "lever": str(lever),
            "emo_area": emo_area
        }
        path = f"/partner/invest/stock/scan?{urlencode(query_params)}"
        cache_key = f"stock_emotion:{code}_{lever}_{emo_area}"
        return await self._make_request("POST", path, cache_key, is_partner=True)

    async def get_etf_emotion(self, etf_type: int = 1) -> Optional[Dict]:
        """获取ETF情绪指标"""
        path = f"/invest/stock_emotion?etf_type={etf_type}"
        cache_key = f"etf_emotion:{etf_type}"
        return await self._make_request("GET", path, cache_key)

    async def get_etf_emotion_history(self, code: str) -> Optional[Dict]:
        """获取ETF历史贪恐指数"""
        path = f"/invest/stock_emotion/history?code={code}"
        cache_key = f"etf_emotion_history:{code}"
        return await self._make_request("GET", path, cache_key)

