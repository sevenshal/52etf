from datetime import datetime
import inspect
import os
import re
from typing import Optional

from bs4 import BeautifulSoup
import httpx

from .base import ETFDataFetcher
from ...core.models.etf import ETFHolding, ETFHoldingsData
from ...core.utils import normalize_us_equity_symbol


class QQQDataFetcher(ETFDataFetcher):
    """Invesco QQQ Trust (QQQ) 数据获取"""

    HOLDINGS_URL = (
        "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/"
        "QQQ/holdings/fund?idType=ticker&interval=monthly&productType=ETF"
    )
    COMPANIES_MARKET_CAP_HOLDINGS_URL = "https://companiesmarketcap.com/invesco-qqq-trust/holdings/"
    MIN_FALLBACK_EQUITY_HOLDINGS = 80
    PROXY = os.getenv("ETF_QQQ_PROXY", "socks5://127.0.0.1:7891")

    def __init__(self):
        super().__init__()
        self.name = "纳斯达克100ETF"
        self.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "referer": "https://www.invesco.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36"
                ),
            }
        )

    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取 QQQ 持仓数据"""
        normalized_symbol = etf_symbol.upper().replace(".US", "")
        if normalized_symbol != "QQQ":
            self.logger.warning(
                "This fetcher is for QQQ, but it was called with '%s'. Proceeding with QQQ.",
                etf_symbol,
            )

        try:
            return self._get_invesco_holdings()
        except Exception as invesco_exc:
            self.logger.warning(
                "通过 Invesco 获取 QQQ 持仓数据失败，尝试 CompaniesMarketCap 备用源: %s",
                invesco_exc,
            )
            try:
                return self._get_companies_market_cap_holdings()
            except Exception as fallback_exc:
                self.logger.error(
                    "通过 Invesco 和 CompaniesMarketCap 获取 QQQ 持仓数据均失败: invesco=%s, fallback=%s",
                    invesco_exc,
                    fallback_exc,
                )
                raise fallback_exc from invesco_exc

    def _get_invesco_holdings(self) -> ETFHoldingsData:
        response = self._get_holdings_response()
        data = response.json()

        if not data or "holdings" not in data:
            raise ValueError("Invesco API did not return holdings data for QQQ")

        update_date_str = data.get("effectiveBusinessDate") or data.get("effectiveDate")
        if not update_date_str:
            raise ValueError("无法在 Invesco 响应中找到更新日期")
        update_date = datetime.strptime(update_date_str, "%Y-%m-%d").date()

        holdings = []
        total_weight = 0.0

        for holding_data in data.get("holdings", []):
            try:
                ticker = str(holding_data.get("ticker") or "").strip()
                issuer_name = str(holding_data.get("issuerName") or ticker).strip()
                shares = int(float(holding_data.get("units") or 0))
                weight = float(holding_data.get("percentageOfTotalNetAssets") or 0) / 100

                if not ticker:
                    self.logger.warning("跳过无 ticker 的 QQQ 持仓行: %s", holding_data)
                    continue

                asset_class = self._map_asset_class(holding_data)
                symbol = normalize_us_equity_symbol(ticker) if asset_class == "Equity" else ticker
                if asset_class == "Equity" and not symbol:
                    self.logger.warning("跳过无法规范化的 QQQ 股票代码: %s", ticker)
                    continue
                holdings.append(
                    ETFHolding(
                        symbol=symbol,
                        name=issuer_name,
                        asset_class=asset_class,
                        shares=shares,
                        weight=weight,
                        # Invesco 这个接口未直接返回持仓市值和价格，交给上层分析阶段补齐。
                        market_value=0.0,
                        price=None,
                    )
                )
                total_weight += weight
            except (ValueError, KeyError, TypeError) as exc:
                self.logger.warning("处理 QQQ 持仓数据行时出错: %s, row: %s", exc, holding_data)
                continue

        return ETFHoldingsData(
            holdings=holdings,
            update_date=update_date,
            total_shares=None,
            total_weight=total_weight,
        )

    def _get_companies_market_cap_holdings(self) -> ETFHoldingsData:
        html = self._fetch_companies_market_cap_holdings_html()
        return self._parse_companies_market_cap_holdings_html(html)

    def _fetch_companies_market_cap_holdings_html(self) -> str:
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "User-Agent": self.headers.get("User-Agent", ""),
        }
        with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
            response = client.get(self.COMPANIES_MARKET_CAP_HOLDINGS_URL)
            response.raise_for_status()
            return response.text

    def _parse_companies_market_cap_holdings_html(self, html: str) -> ETFHoldingsData:
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        date_match = re.search(r"Etf holdings as of\s+([A-Za-z]+ \d{1,2}, \d{4})", page_text)
        if not date_match:
            raise ValueError("无法在 CompaniesMarketCap 响应中找到 QQQ 持仓日期")
        update_date = datetime.strptime(date_match.group(1), "%B %d, %Y").date()

        table = self._find_companies_market_cap_holdings_table(soup)
        holdings = []
        total_weight = 0.0

        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            ticker = cells[2].get_text(" ", strip=True).replace("$", "").strip().upper()
            name = cells[1].get_text(" ", strip=True)
            weight = self._parse_companies_market_cap_weight(cells[0]) / 100
            shares = self._safe_int_text(cells[3].get_text(" ", strip=True))
            symbol = normalize_us_equity_symbol(ticker)

            if not symbol or weight <= 0:
                continue

            holdings.append(
                ETFHolding(
                    symbol=symbol,
                    name=name,
                    asset_class="Equity",
                    shares=shares,
                    weight=weight,
                    market_value=0.0,
                    price=None,
                )
            )
            total_weight += weight

        if len(holdings) < self.MIN_FALLBACK_EQUITY_HOLDINGS:
            raise ValueError(
                "CompaniesMarketCap QQQ 持仓数据看起来不完整: "
                f"equity_count={len(holdings)}, expected>={self.MIN_FALLBACK_EQUITY_HOLDINGS}"
            )

        return ETFHoldingsData(
            holdings=holdings,
            update_date=update_date,
            total_shares=None,
            total_weight=total_weight,
        )

    @staticmethod
    def _find_companies_market_cap_holdings_table(soup: BeautifulSoup):
        expected_headers = {"Weight %", "Name", "Ticker", "Shares Held"}
        for table in soup.find_all("table"):
            headers = {cell.get_text(" ", strip=True) for cell in table.find_all("th")}
            if expected_headers.issubset(headers):
                return table
        raise ValueError("无法在 CompaniesMarketCap 响应中找到 QQQ 持仓表")

    @staticmethod
    def _parse_companies_market_cap_weight(cell) -> float:
        progress_bar = cell.select_one(".progress-bar")
        if progress_bar and progress_bar.get("aria-valuenow"):
            return float(progress_bar["aria-valuenow"])

        text = cell.get_text(" ", strip=True)
        match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", text)
        if not match:
            raise ValueError(f"无法解析 CompaniesMarketCap 权重: {text}")
        return float(match.group(1))

    @staticmethod
    def _safe_int_text(value: str) -> int:
        normalized = re.sub(r"[^\d.-]", "", value or "")
        return int(float(normalized or 0))

    def _get_holdings_response(self) -> httpx.Response:
        if self.PROXY:
            try:
                return self._fetch_holdings(proxy=self.PROXY)
            except Exception as exc:
                if not self._should_retry_direct_after_proxy_error(exc):
                    raise
                self.logger.warning("通过代理获取 QQQ 持仓失败，尝试直连: %s", exc)

        return self._fetch_holdings(proxy=None)

    def _fetch_holdings(self, proxy: Optional[str] = None) -> httpx.Response:
        client_kwargs = {"timeout": 30}
        if proxy:
            client_kwargs.update(self._proxy_client_kwargs(proxy))

        # 服务端请求不需要 Origin；Invesco/Fastly 对不完整的 CORS 模拟请求会返回 406。
        with httpx.Client(**client_kwargs) as client:
            response = client.get(self.HOLDINGS_URL, headers=self.headers)
            response.raise_for_status()
            return response

    @staticmethod
    def _proxy_client_kwargs(proxy: str) -> dict:
        parameters = inspect.signature(httpx.Client.__init__).parameters
        if "proxy" in parameters:
            return {"proxy": proxy}
        if "proxies" in parameters:
            return {"proxies": proxy}
        raise TypeError("httpx.Client does not support proxy configuration")

    @staticmethod
    def _should_retry_direct_after_proxy_error(exc: Exception) -> bool:
        if isinstance(exc, (httpx.RequestError, httpx.HTTPStatusError, TypeError)):
            return True
        message = str(exc).lower()
        return "socksio" in message and "socks" in message

    def _map_asset_class(self, holding_data: dict) -> str:
        ticker = str(holding_data.get("ticker") or "").strip().upper()
        issuer_name = str(holding_data.get("issuerName") or "").strip()
        issuer_name_lower = issuer_name.lower()
        security_type_code = str(holding_data.get("securityTypeCode") or "").upper()
        security_type_name = str(holding_data.get("securityTypeName") or "").lower()

        if ticker in {"USD", "CASH"} or "cash" in issuer_name_lower or "equivalent" in issuer_name_lower:
            return "Cash"
        if (
            security_type_code in {"FUT", "FUTURE"}
            or any(char.isdigit() or char == "-" for char in ticker)
            or "future" in issuer_name_lower
            or "future" in security_type_name
        ):
            return "Other"
        if security_type_code in {"COM", "ADR", "ETF", "REIT"}:
            return "Equity"
        if security_type_code in {"CASH", "CUR"} or "cash" in security_type_name:
            return "Cash"
        if security_type_code in {"BND", "NOTE"} or "bond" in security_type_name:
            return "Bond"
        return "Equity"
