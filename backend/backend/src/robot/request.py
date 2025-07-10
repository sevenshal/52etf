import requests
import json
import os
import logging
import uuid
from ratelimit import limits, sleep_and_retry
from dataclasses import dataclass
from typing import Optional, List
from decimal import Decimal
from datetime import datetime, timedelta
from longport.openapi import Config, QuoteContext, TradeContext, TopicType, OrderSide, OrderType, TimeInForceType, OutsideRTH, Period, AdjustType

current_directory = os.path.dirname(os.path.realpath(__file__))


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

class EeayValueCheckApi:
    COOKIE_FILE = f'/var/lib/quant_robot/data/evc_data.json'

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
        with open(self.COOKIE_FILE, 'r', encoding='utf - 8') as f:
            data = json.load(f)
            return data.get('cookies')

    def _save_cookies(self):
        data = {'cookies': self.headers['cookie']}
        with open(self.COOKIE_FILE, 'w', encoding='utf - 8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def set_cookies(self, cookies_str):
        self.headers['cookie'] = cookies_str
        self._save_cookies()

    def get_cookies(self):
        return self.cookies

    def search_stock(self, page=1, size=60, text='', tags=None, orderField='createdAt', orderDirection='DESC',
                     underValued=True, inValued=False, overValued=False):
        data = {
            "page": page,
            "size": size,
            "text": text,
            "tags": tags,
            "orderField": orderField,
            "orderDirection": orderDirection,
            "underValued": underValued,
            "inValued": inValued,
            "overValued": overValued
        }
        response = requests.post(self.search_url, headers=self.headers, json=data)
        set_cookie = response.headers.get('Set-Cookie')
        if set_cookie:
            jwt_start = set_cookie.find('jwt=')
            if jwt_start!= -1:
                jwt_end = set_cookie.find(';', jwt_start)
                if jwt_end == -1:
                    jwt_end = len(set_cookie)
                new_jwt = set_cookie[jwt_start + 4:jwt_end]
                self.set_cookies(f'G_ENABLED_IDPS=google; CookieConsent=true; G_AUTHUSER_H=0; jwt={new_jwt}')
        return response.json()

    def fetch_all_stock_data(self, size=60, text='', tags=None, orderField='createdAt', orderDirection='DESC',
                           underValued=True, inValued=False, overValued=False) -> List[EVCData]:
        all_data = []
        page = 1
        while True:
            response_json = self.search_stock(page=page, size=size, text=text, tags=tags, orderField=orderField,
                                              orderDirection=orderDirection, underValued=underValued, inValued=inValued,
                                              overValued=overValued)
            all_data.extend(list(map(EVCData.from_dict, response_json['data'])))
            total_count = response_json['count']
            if len(all_data) >= total_count or response_json['page']!=page:
                break
            page += 1
        return all_data

    @sleep_and_retry
    @limits(calls = 1, period = 3)
    def stock_evc_info(self, symbol) -> EVCData:
        symbol = symbol[:-3]
        url = f'https://easyvaluecheck.com/api/v1/stock/s/{symbol}/evc_info'
        headers = {
        'sec-ch-ua-platform': 'macOS',
        'referer': f'https://easyvaluecheck.com/stock/{symbol}',
        'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'sec-ch-ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        'origin': 'https://easyvaluecheck.com',
        'priority': 'u=1, i',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site':'same-origin',
        }
        headers['cookie'] = self.headers['cookie']
        response = requests.get(url, headers = headers)
        return EVCData.from_dict(response.json())

    def get_stock_tags(self) -> List[TagData]:
        """获取所有股票标签"""
        response = requests.get(self.tag_url, headers=self.headers)
        return [TagData.from_dict(tag) for tag in response.json()]

class LongPortClient:
    def __init__(self, config: Config):
        self.config = config
        self.ctx = QuoteContext(self.config)
        self.trade_ctx = TradeContext(self.config)
        self.trade_ctx.subscribe([TopicType.Private])

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_static_info(self, symbol: str) -> dict:
        """获取标的基础信息，包括总股本等"""
        try:
            resp = self.ctx.static_info([symbol])
            if resp:
                return {
                    'total_shares': resp[0].total_shares,
                    'circulating_shares': resp[0].circulating_shares,
                    'lot_size': resp[0].lot_size,
                    'currency': resp[0].currency
                }
            return None
        except Exception as e:
            logging.error(f"获取{symbol}基础信息失败: {str(e)}")
            return None

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_quote(self, symbol: str) -> dict:
        """获取标的实时行情
        
        Args:
            symbol: 标的代码，如 'SPY.US'
            
        Returns:
            dict: 包含实时价格等信息
        """
        try:
            resp = self.ctx.quote([symbol])
            if resp:
                quote = resp[0]
                return {
                    'code': symbol,
                    'price': float(quote.last_done),         # 当前价格
                    'change': float(quote.last_done) - float(quote.prev_close),  # 价格变动
                    'percent_change': (float(quote.last_done) - float(quote.prev_close)) / float(quote.prev_close) * 100,  # 价格变动百分比
                    'high': float(quote.high),          # 当日最高
                    'low': float(quote.low),           # 当日最低
                    'open': float(quote.open),          # 开盘价
                    'prev_close': float(quote.prev_close),   # 昨日收盘价
                    'volume': quote.volume,           # 成交量
                    'turnover': float(quote.turnover),      # 成交额
                    'timestamp': quote.timestamp      # 时间戳
                }
            return None
        except Exception as e:
            logging.error(f"获取{symbol}实时行情失败: {str(e)}")
            return None

    @sleep_and_retry
    @limits(calls=30, period=30)
    def submit_order(self, 
        side: OrderSide,
        symbol: str,
        order_type: OrderType,
        submitted_price: float,
        submitted_quantity: int,
        time_in_force: TimeInForceType = TimeInForceType.Day,
        outside_rth: OutsideRTH = OutsideRTH.AnyTime,
        remark: str = ""
    ) -> dict:
        """提交订单"""
        try:
            resp = self.trade_ctx.submit_order(
                side=side,
                symbol=symbol,
                order_type=order_type,
                submitted_price=submitted_price,
                submitted_quantity=submitted_quantity,
                time_in_force=time_in_force,
                outside_rth=outside_rth,
                remark=remark
            )
            return resp
        except Exception as e:
            logging.error(f"提交订单失败: {str(e)}")
            raise

    @sleep_and_retry
    @limits(calls=30, period=30)
    def today_orders(self, symbol: str = None, side: OrderSide = None) -> List:
        """获取当日订单"""
        try:
            return self.trade_ctx.today_orders(symbol=symbol, side=side)
        except Exception as e:
            logging.error(f"获取当日订单失败: {str(e)}")
            return []

    @sleep_and_retry
    @limits(calls=30, period=30)
    def stock_positions(self) -> List:
        """获取持仓信息"""
        try:
            return self.trade_ctx.stock_positions()
        except Exception as e:
            logging.error(f"获取持仓信息失败: {str(e)}")
            return []

    @sleep_and_retry
    @limits(calls=30, period=30)
    def account_balance(self) -> dict:
        """获取账户资金"""
        try:
            return self.trade_ctx.account_balance()
        except Exception as e:
            logging.error(f"获取账户资金失败: {str(e)}")
            return None

    def set_on_order_changed(self, callback):
        """设置订单变更回调"""
        self.trade_ctx.set_on_order_changed(callback)

    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_candlesticks(self, symbol: str, period: Period, count: int, adjust_type: AdjustType = AdjustType.NoAdjust) -> List[dict]:
        """获取K线数据
        
        Args:
            symbol: 标的代码，如 'SPY.US'
            period: K线周期，如 Period.Day
            count: 获取的K线数量
            adjust_type: 复权类型，默认不复权
            
        Returns:
            List[dict]: K线数据列表，每个元素包含:
            {
                'close': float,      # 收盘价
                'open': float,       # 开盘价
                'high': float,       # 最高价
                'low': float,        # 最低价
                'volume': int,       # 成交量
                'turnover': float,   # 成交额
                'timestamp': datetime # 时间戳
            }
        """
        try:
            resp = self.ctx.candlesticks(symbol, period, count, adjust_type)
            if resp:
                return [{
                    'close': float(candle.close),
                    'open': float(candle.open),
                    'high': float(candle.high),
                    'low': float(candle.low),
                    'volume': candle.volume,
                    'turnover': float(candle.turnover),
                    'timestamp': candle.timestamp
                } for candle in resp]
            return []
        except Exception as e:
            logging.error(f"获取{symbol} K线数据失败: {str(e)}")
            return []
