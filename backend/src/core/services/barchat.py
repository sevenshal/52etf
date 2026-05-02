import time
import warnings
from typing import Any, Dict, List
from urllib.parse import unquote

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests


BASE_URL = "https://www.barchart.com"
QUOTE_PAGE_URL = f"{BASE_URL}/etfs-funds/quotes/{{symbol}}/{{page_name}}"
OPTIONS_HISTORICAL_URL = f"{BASE_URL}/proxies/core-api/v1/options-historical/get"
OPTIONS_EXPIRATIONS_URL = f"{BASE_URL}/proxies/core-api/v1/options-expirations/get"

PUT_CALL_RATIO_FIELDS = (
    "putVolume",
    "callVolume",
    "totalVolume",
    "putCallVolumeRatio",
    "putOpenInterest",
    "callOpenInterest",
    "totalOpenInterest",
    "putCallOpenInterestRatio",
    "date",
)

OPTION_EXPIRATION_FIELDS = (
    "expirationDate",
    "expirationType",
    "daysToExpiration",
    "putVolume",
    "callVolume",
    "totalVolume",
    "putCallVolumeRatio",
    "putOpenInterest",
    "callOpenInterest",
    "totalOpenInterest",
    "putCallOpenInterestRatio",
    "averageVolatility",
    "symbolType",
    "lastPrice",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


class BarchartService:
    """Small Barchart Core API client with automatic public-page cookies."""

    def __init__(self, timeout: float = 30.0, user_agent: str = DEFAULT_USER_AGENT):
        self.timeout = timeout
        self._xsrf_token = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            }
        )

    def close(self):
        self.session.close()

    def get_options_history(
        self,
        symbol: str,
        full: bool = False,
        page_limit: int = 1000,
        recent_limit: int = 10,
        sleep_seconds: float = 0.2,
    ) -> List[Dict[str, Any]]:
        limit = page_limit if full else recent_limit
        rows: List[Dict[str, Any]] = []
        page = 1

        while True:
            payload = self.get_options_history_page(symbol, page=page, limit=limit)
            page_rows = payload.get("data") or []
            rows.extend(page_rows)
            count = self._to_int(payload.get("count"), len(page_rows))

            if not full or count < limit:
                break

            page += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        return rows

    def get_options_history_page(self, symbol: str, page: int = 1, limit: int = 1000) -> Dict[str, Any]:
        return self._get_json(
            url=OPTIONS_HISTORICAL_URL,
            symbol=symbol,
            page_name="put-call-ratios",
            params={
                "symbol": symbol,
                "fields": ",".join(PUT_CALL_RATIO_FIELDS),
                "limit": limit,
                "page": page,
                "orderBy": "date",
                "orderDir": "desc",
            },
        )

    def get_options_expirations(
        self,
        symbol: str,
        page_limit: int = 100,
        sleep_seconds: float = 0.2,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        page = 1

        while True:
            payload = self.get_options_expirations_page(symbol, page=page, limit=page_limit)
            page_rows = payload.get("data") or []
            rows.extend(page_rows)
            count = self._to_int(payload.get("count"), len(page_rows))

            if count < page_limit:
                break

            page += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        return rows

    def get_options_expirations_page(self, symbol: str, page: int = 1, limit: int = 100) -> Dict[str, Any]:
        return self._get_json(
            url=OPTIONS_EXPIRATIONS_URL,
            symbol=symbol,
            page_name="put-call-ratios",
            params={
                "symbol": symbol,
                "fields": ",".join(OPTION_EXPIRATION_FIELDS),
                "page": page,
                "limit": limit,
            },
        )

    def refresh_session(self, symbol: str, page_name: str = "put-call-ratios") -> str:
        page_url = QUOTE_PAGE_URL.format(symbol=symbol, page_name=page_name)
        response = self.session.get(
            page_url,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Referer": BASE_URL,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()

        xsrf_token = self.session.cookies.get("XSRF-TOKEN")
        if not xsrf_token:
            raise RuntimeError(f"Barchart did not set XSRF-TOKEN for {symbol}")
        self._xsrf_token = unquote(xsrf_token)
        return self._xsrf_token

    def _get_json(
        self,
        url: str,
        symbol: str,
        page_name: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        xsrf_token = self._xsrf_token or self.refresh_session(symbol, page_name=page_name)
        headers = self._api_headers(symbol, page_name, xsrf_token)
        response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)

        if response.status_code in {401, 403, 419}:
            headers = self._api_headers(symbol, page_name, self.refresh_session(symbol, page_name=page_name))
            response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"Unexpected Barchart response for {symbol}")
        return payload

    @staticmethod
    def _api_headers(symbol: str, page_name: str, xsrf_token: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Priority": "u=1, i",
            "Referer": QUOTE_PAGE_URL.format(symbol=symbol, page_name=page_name),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-XSRF-TOKEN": xsrf_token,
        }

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return default
