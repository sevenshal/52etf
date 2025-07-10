import requests
import json
import os
import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Optional, Tuple
from dateutil import parser

@dataclass
class TagData:
    id: str
    created_at: datetime
    name: str
    built_in: bool
    official_only: bool
    includes_option_put_call: bool
    option_put_call_fetch_tag_ordinal: Optional[int]
    sort_group: int

    @classmethod
    def from_dict(cls, data: dict) -> 'TagData':
        return cls(
            id=data['id'],
            created_at=datetime.fromisoformat(data['createdAt'].replace('Z', '+00:00')),
            name=data['name'],
            built_in=data['builtIn'],
            official_only=data['officialOnly'],
            includes_option_put_call=data['includesOptionPutCall'],
            option_put_call_fetch_tag_ordinal=data['optionPutCallFetchTagOrdinal'],
            sort_group=data['sortGroup']
        )

@dataclass
class EVCData:
    symbol: str
    company: Optional[str]
    last_price: Optional[float]
    last_change: Optional[float]
    last_change_percent: Optional[float]
    fair_value_lo: Optional[float]
    fair_value_hi: Optional[float]
    fair_value_date: Optional[date]
    forward_next_fy_lo: Optional[float]
    forward_next_fy_hi: Optional[float]
    forward_next_fy_max_value_lo: Optional[float]
    forward_next_fy_max_value_hi: Optional[float]
    beta: Optional[float]
    pe_ratio: Optional[float]
    forward_pe_ratio: Optional[float]
    is_under: Optional[bool]
    is_over: Optional[bool]
    tags: List[str]

    @classmethod
    def from_dict(cls, data: dict) -> 'EVCData':
        return cls(
            symbol=f"{data['symbol']}.US",
            company=data.get('company', ''),
            last_price=float(str(data['lastPrice'])) if data.get('lastPrice') else None,
            last_change=float(str(data['lastChange'])) if data.get('lastChange') else None,
            last_change_percent=float(str(data['lastChangePercent'])) if data.get('lastChangePercent') else None,
            fair_value_lo=float(str(data['fairValueLo'])) if data.get('fairValueLo') else None,
            fair_value_hi=float(str(data['fairValueHi'])) if data.get('fairValueHi') else None,
            fair_value_date=parser.parse(data['fairValueDate']).date() if data.get('fairValueDate') else None,
            forward_next_fy_lo=float(str(data['forwardNextFyFairValueLo'])) if data.get('forwardNextFyFairValueLo') else None,
            forward_next_fy_hi=float(str(data['forwardNextFyFairValueHi'])) if data.get('forwardNextFyFairValueHi') else None,
            forward_next_fy_max_value_lo=float(str(data['forwardNextFyMaxValueLo'])) if data.get('forwardNextFyMaxValueLo') else None,
            forward_next_fy_max_value_hi=float(str(data['forwardNextFyMaxValueHi'])) if data.get('forwardNextFyMaxValueHi') else None,
            beta=float(str(data['beta'])) if data.get('beta') else None,
            pe_ratio=float(str(data['peRatio'])) if data.get('peRatio') else None,
            forward_pe_ratio=float(str(data['forwardPeRatio'])) if data.get('forwardPeRatio') else None,
            is_under=data.get('isUnder', False),
            is_over=data.get('isOver', False),
            tags=data.get('tags', []) or []
        )

class EVCService:
    """EasyValueCheck API服务"""
    
    COOKIE_FILE = '/var/lib/quant_robot/data/evc_data.json'
    
    def __init__(self):
        self.search_url = 'https://easyvaluecheck.com/api/v1/stock/search'
        self.tag_url = 'https://easyvaluecheck.com/api/v1/stocktag'
        self.headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'content-type': 'application/json; charset=UTF-8',
            'origin': 'https://easyvaluecheck.com',
            'priority': 'u=1, i',
            'referer': 'https://easyvaluecheck.com/stock',
            'sec-ch-ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site':'same-origin',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        }
        self.headers['cookie'] = self._load_cookies()

    def _load_cookies(self):
        if not os.path.exists(self.COOKIE_FILE):
            return None
        with open(self.COOKIE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('cookies')

    def _save_cookies(self):
        data = {'cookies': self.headers['cookie']}
        with open(self.COOKIE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def set_cookies(self, cookies_str):
        self.headers['cookie'] = cookies_str
        self._save_cookies()

    def get_stock_tags(self) -> List[TagData]:
        """获取股票标签列表"""
        try:
            response = requests.get(self.tag_url, headers=self.headers)
            response.raise_for_status()
            return [TagData.from_dict(item) for item in response.json()]
        except Exception as e:
            logging.error(f"获取股票标签失败: {str(e)}")
            return []

    def search_stock(self, page: int = 1, size: int = 60, tags: List[str] = None, 
                    underValued: bool = None, inValued: bool = None, overValued: bool = None) -> Tuple[List[EVCData], int, int]:
        """搜索股票"""
        try:
            params = {
                'page': page,
                'size': size,
                'tags': tags or [],
                'underValued': underValued,
                'inValued': inValued,
                'overValued': overValued
            }
            response = requests.post(self.search_url, headers=self.headers, json=params)
            response.raise_for_status()
            data = response.json()
            return list(map(EVCData.from_dict, data['data'])), data['page'], data['count']
        except Exception as e:
            logging.error(f"搜索股票失败: {str(e)}")
            return [], 0, 0
    
    def stock_evc_info(self, symbol) -> EVCData:
        symbol = symbol[:-3]
        url = f'https://easyvaluecheck.com/api/v1/stock/s/{symbol}/evc_info'
        headers = self.headers.copy()
        headers['referer'] = f'https://easyvaluecheck.com/stock/{symbol}'
        headers['cookie'] = self.headers['cookie']
        response = requests.get(url, headers = headers)
        return EVCData.from_dict(response.json())
