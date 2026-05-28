from datetime import datetime
import os
from typing import Optional

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
        except Exception as exc:
            self.logger.error("通过 Invesco 获取 QQQ 持仓数据失败: %s", exc)
            raise

    def _get_holdings_response(self) -> httpx.Response:
        if self.PROXY:
            try:
                return self._fetch_holdings(proxy=self.PROXY)
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                self.logger.warning("通过代理获取 QQQ 持仓失败，尝试直连: %s", exc)

        return self._fetch_holdings(proxy=None)

    def _fetch_holdings(self, proxy: Optional[str] = None) -> httpx.Response:
        client_kwargs = {"timeout": 30}
        if proxy:
            client_kwargs["proxy"] = proxy

        # 服务端请求不需要 Origin；Invesco/Fastly 对不完整的 CORS 模拟请求会返回 406。
        with httpx.Client(**client_kwargs) as client:
            response = client.get(self.HOLDINGS_URL, headers=self.headers)
            response.raise_for_status()
            return response

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
