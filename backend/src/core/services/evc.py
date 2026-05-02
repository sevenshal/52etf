import requests
import logging
import json
import base64
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional, Tuple
from dateutil import parser

from ..database import EVCAccountConfig, Session

def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(str(value))

def _to_date(value) -> Optional[date]:
    if not value:
        return None
    return parser.parse(value).date()

def _to_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    return parser.parse(value)

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
class EVCOptionPutCallData:
    symbol: str
    date: Optional[date]
    name: Optional[str]
    asset_type: Optional[str]
    sort_group: Optional[int]
    tag_id: Optional[str]
    ordinal: Optional[int]
    put_call_vol: Optional[float]
    today_option_vol: Optional[float]
    put_call_oi_ratio: Optional[float]
    total_open_interest: Optional[float]
    today_percent_put_vol: Optional[float]
    today_percent_call_vol: Optional[float]

    @classmethod
    def from_dict(cls, data: dict) -> 'EVCOptionPutCallData':
        return cls(
            symbol=data['symbol'],
            date=_to_date(data.get('date')),
            name=data.get('name'),
            asset_type=data.get('type'),
            sort_group=data.get('sortGroup'),
            tag_id=data.get('tagId'),
            ordinal=data.get('ordinal'),
            put_call_vol=_to_float(data.get('putCallVol')),
            today_option_vol=_to_float(data.get('todayOptionVol')),
            put_call_oi_ratio=_to_float(data.get('putCallOIRatio')),
            total_open_interest=_to_float(data.get('totalOpenInterest')),
            today_percent_put_vol=_to_float(data.get('todayPercentPutVol')),
            today_percent_call_vol=_to_float(data.get('todayPercentCallVol'))
        )

@dataclass
class EVCFairValuePoint:
    date: Optional[date]
    lo: Optional[float]
    hi: Optional[float]

    @classmethod
    def from_dict(cls, data: dict) -> 'EVCFairValuePoint':
        return cls(
            date=_to_date(data.get('date')),
            lo=_to_float(data.get('lo')),
            hi=_to_float(data.get('hi'))
        )

@dataclass
class EVCStockFairValueData:
    symbol: str
    company: Optional[str]
    tags: List[str]
    fair_values: List[EVCFairValuePoint]
    last_price: Optional[float]
    watched: Optional[datetime]
    belled: Optional[bool]

    @classmethod
    def from_dict(cls, data: dict) -> 'EVCStockFairValueData':
        return cls(
            symbol=data['symbol'],
            company=data.get('company'),
            tags=data.get('tags', []) or [],
            fair_values=[EVCFairValuePoint.from_dict(item) for item in data.get('fairValues', [])],
            last_price=_to_float(data.get('lastPrice')),
            watched=_to_datetime(data.get('watched')),
            belled=data.get('belled')
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

    def __init__(self, account_id: Optional[str] = None):
        self.account_id = account_id
        self.login_url = 'https://easyvaluecheck.com/api/v1/auth/login'
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
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'cookie': 'G_ENABLED_IDPS=google; G_AUTHUSER_H=0'
        }
        cookie = self._load_cookie_from_db()
        if cookie:
            self.headers['cookie'] = cookie

    def _normalize_evc_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if normalized.endswith('.US'):
            return normalized[:-3]
        return normalized

    def _authenticated_get_json(self, url: str, referer: Optional[str] = None):
        self.ensure_authenticated()
        headers = self.headers.copy()
        if referer:
            headers['referer'] = referer

        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code in (401, 403):
            self.login()
            headers = self.headers.copy()
            if referer:
                headers['referer'] = referer
            response = requests.get(url, headers=headers, timeout=30)

        response.raise_for_status()
        return response.json()

    def _get_config_record(self, session):
        query = session.query(EVCAccountConfig)
        if self.account_id:
            query = query.filter(EVCAccountConfig.account_id == self.account_id)
        else:
            query = query.filter(EVCAccountConfig.evc_username.isnot(None))
        return query.order_by(EVCAccountConfig.updated_at.desc()).first()

    def _load_cookie_from_db(self):
        session = Session()
        try:
            config = self._get_config_record(session)
            return config.evc_cookie if config else None
        finally:
            session.close()

    def set_cookies(self, cookies_str):
        self.headers['cookie'] = cookies_str

    def get_cookies(self):
        return self.headers.get('cookie')

    def _decode_cookie_expiry(self, cookie_value: Optional[str]) -> Optional[datetime]:
        if not cookie_value:
            return None
        jwt_token = None
        for segment in cookie_value.split(';'):
            part = segment.strip()
            if part.startswith('jwt='):
                jwt_token = part[4:]
                break
        if not jwt_token:
            return None
        try:
            payload = jwt_token.split('.')[1]
            padding = len(payload) % 4
            if padding:
                payload += '=' * (4 - padding)
            decoded = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
            expires_at = decoded.get('expires')
            if expires_at:
                return parser.parse(expires_at)
            exp = decoded.get('exp')
            if exp:
                return datetime.fromtimestamp(exp, tz=timezone.utc)
        except Exception as exc:
            logging.warning(f"解析 EVC JWT 过期时间失败: {exc}")
        return None

    def _to_utc_naive(self, value: Optional[datetime]) -> Optional[datetime]:
        if not value:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def _from_db_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if not value:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _save_cookie_to_db(self, cookie_value: str, expires_at: Optional[datetime]):
        session = Session()
        try:
            config = self._get_config_record(session)
            if not config:
                return
            config.evc_cookie = cookie_value
            config.cookie_expires_at = self._to_utc_naive(expires_at)
            config.updated_at = datetime.now()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _is_cookie_valid(self, config: Optional[EVCAccountConfig]) -> bool:
        if not config or not config.evc_cookie:
            return False
        if not config.cookie_expires_at:
            return True
        expires_at = self._from_db_utc(config.cookie_expires_at)
        return datetime.now(timezone.utc) < expires_at - timedelta(minutes=3)

    def login(self) -> dict:
        session = Session()
        try:
            config = self._get_config_record(session)
            if not config or not config.evc_username or not config.evc_password:
                raise ValueError("未配置 EVC 账户，请先在“我的 -> 账户管理 -> EVC账户”中填写用户名和密码")

            response = requests.post(
                self.login_url,
                headers=self.headers,
                json={"name": config.evc_username, "password": config.evc_password},
                timeout=30,
            )
            response.raise_for_status()

            jwt_value = response.cookies.get('jwt')
            if not jwt_value:
                set_cookie = response.headers.get('Set-Cookie', '')
                for item in set_cookie.split(';'):
                    part = item.strip()
                    if part.startswith('jwt='):
                        jwt_value = part[4:]
                        break
            if not jwt_value:
                raise ValueError("登录成功但未获取到 jwt cookie")

            cookie_value = f'G_ENABLED_IDPS=google; G_AUTHUSER_H=0; jwt={jwt_value}'
            expires_at = self._decode_cookie_expiry(cookie_value)

            config.evc_cookie = cookie_value
            config.cookie_expires_at = self._to_utc_naive(expires_at)
            config.updated_at = datetime.now()
            session.commit()

            self.headers['cookie'] = cookie_value
            return {
                "cookie": cookie_value,
                "cookie_expires_at": self._from_db_utc(config.cookie_expires_at).isoformat() if config.cookie_expires_at else None
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_authenticated(self, force_login: bool = False):
        session = Session()
        try:
            config = self._get_config_record(session)
            has_valid_cookie = self._is_cookie_valid(config)
            if not force_login and has_valid_cookie and config.evc_cookie:
                self.headers['cookie'] = config.evc_cookie
                return
        finally:
            session.close()
        self.login()

    def get_stock_tags(self) -> List[TagData]:
        """获取股票标签列表"""
        try:
            self.ensure_authenticated()
            response = requests.get(self.tag_url, headers=self.headers)
            response.raise_for_status()
            return [TagData.from_dict(item) for item in response.json()]
        except Exception as e:
            logging.error(f"获取股票标签失败: {str(e)}")
            return []

    def search_stock(
        self,
        page: int = 1,
        size: int = 60,
        text: str = "",
        tags: List[str] = None,
        orderField: str = "createdAt",
        orderDirection: str = "DESC",
        underValued: bool = None,
        inValued: bool = None,
        overValued: bool = None
    ) -> Tuple[List[EVCData], int, int]:
        """搜索股票"""
        try:
            self.ensure_authenticated()
            params = {
                'page': page,
                'size': size,
                'text': text,
                'orderField': orderField,
                'orderDirection': orderDirection,
            }
            if tags is not None:
                params['tags'] = tags
            if underValued is not None:
                params['underValued'] = underValued
            if inValued is not None:
                params['inValued'] = inValued
            if overValued is not None:
                params['overValued'] = overValued
            response = requests.post(self.search_url, headers=self.headers, json=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if (
                page == 1 and
                data.get('count', 0) > len(data.get('data', [])) and
                len(data.get('data', [])) <= 12
            ):
                # 未登录/过期时 upstream 常退化为预览数据，重登一次再重试。
                self.login()
                response = requests.post(self.search_url, headers=self.headers, json=params, timeout=30)
                response.raise_for_status()
                data = response.json()
            return list(map(EVCData.from_dict, data['data'])), data['page'], data['count']
        except Exception as e:
            logging.error(f"搜索股票失败: {str(e)}")
            return [], 0, 0
    
    def stock_evc_info(self, symbol) -> EVCData:
        symbol = self._normalize_evc_symbol(symbol)
        url = f'https://easyvaluecheck.com/api/v1/stock/s/{symbol}/evc_info'
        data = self._authenticated_get_json(url, referer=f'https://easyvaluecheck.com/stock/{symbol}')
        return EVCData.from_dict(data)

    def option_put_call_history(self, symbol: str) -> List[EVCOptionPutCallData]:
        symbol = self._normalize_evc_symbol(symbol)
        url = f'https://easyvaluecheck.com/api/v1/admin/data/opc/{symbol}/all'
        data = self._authenticated_get_json(url, referer='https://easyvaluecheck.com/option_put_call')
        return [EVCOptionPutCallData.from_dict(item) for item in data]

    def option_put_call_snapshot(self) -> List[EVCOptionPutCallData]:
        url = 'https://easyvaluecheck.com/api/v1/admin/data/opc'
        data = self._authenticated_get_json(url, referer='https://easyvaluecheck.com/option_put_call')
        return [EVCOptionPutCallData.from_dict(item) for item in data]

    def current_option_put_call(self, symbol: str) -> Optional[EVCOptionPutCallData]:
        symbol = self._normalize_evc_symbol(symbol)
        for item in self.option_put_call_snapshot():
            if item.symbol.upper() == symbol:
                return item
        return None

    def stock_current_fy_fair_values(self, symbol: str) -> EVCStockFairValueData:
        symbol = self._normalize_evc_symbol(symbol)
        url = f'https://easyvaluecheck.com/api/v1/stock/s/{symbol}'
        data = self._authenticated_get_json(url, referer=f'https://easyvaluecheck.com/stock/{symbol}')
        return EVCStockFairValueData.from_dict(data)
