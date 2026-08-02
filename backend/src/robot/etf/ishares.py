import requests
import csv
from io import StringIO
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from .base import ETFDataFetcher
from ...core.models.etf import ETFHolding, ETFHoldingsData
from ...core.utils import normalize_us_equity_symbol

class ISharesETFFetcher(ETFDataFetcher):
    """iShares ETF数据获取"""

    PRODUCT_DATA_API_URL = (
        "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
        "product-data/api/v2/get-product-data"
    )
    
    ETF_CONFIGS = {
        'SOXX.US': {
            'product_id': '239705',
            'url': 'https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax',
            'name': 'iShares费城半导体ETF'
        },
        'IWM.US': {
            'product_id': '239710',
            'url': 'https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax',
            'name': 'iShares罗素2000ETF'
        },
        'ITB.US': {
            'product_id': '239512',
            'url': 'https://www.ishares.com/us/products/239512/ishares-us-home-construction-etf/1467271812596.ajax',
            'name': 'iShares美国房屋建筑ETF'
        },
        'ITA.US': {
            'product_id': '239502',
            'url': 'https://www.ishares.com/us/products/239502/ishares-us-aerospace-defense-etf/1467271812596.ajax',
            'name': 'iShares美国航空航天防务ETF'
        },
        'IAK.US': {
            'product_id': '239515',
            'url': 'https://www.ishares.com/us/products/239515/ishares-us-insurance-etf/1467271812596.ajax',
            'name': 'iShares美国保险ETF'
        }
    }

    exchange_map = {
        "NASDAQ": ".US",
        "NYSE": ".US",
        "New York Stock Exchange Inc.": ".US",
        "Nyse Mkt Llc": ".US"
    }

    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取iShares ETF持仓数据"""
        if etf_symbol not in self.ETF_CONFIGS:
            raise ValueError(f"不支持的ETF: {etf_symbol}")
            
        self.name = self.ETF_CONFIGS[etf_symbol]['name']  # 设置当前ETF名称
        try:
            return self._get_holdings_from_product_data(etf_symbol)
        except Exception as product_data_error:
            self.logger.warning(
                "iShares product-data 持仓接口失败，尝试旧 CSV 接口: %s %s",
                etf_symbol,
                product_data_error,
            )
            return self._get_holdings_from_csv(etf_symbol)

    def _get_holdings_from_product_data(self, etf_symbol: str) -> ETFHoldingsData:
        config = self.ETF_CONFIGS[etf_symbol]
        response = requests.get(
            self.PRODUCT_DATA_API_URL,
            params={
                "appSubType": "ISHARES",
                "appType": "PRODUCT_PAGE",
                "component": "holdings.all",
                "locale": "en_US",
                "portfolioId": config["product_id"],
                "targetSite": "us-ishares",
                "userType": "individual",
                "excludeContent": "true",
                "includeConfig": "true",
            },
            headers={
                **self.headers,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        holdings_component = payload.get("componentsByNameMap", {}).get("holdings", {})
        data_points = (
            holdings_component.get("containersByNameMap", {})
            .get("all", {})
            .get("dataPointsByNameMap", {})
        )
        if not data_points:
            raise ValueError("iShares product-data 响应缺少 holdings.all 数据")

        # BlackRock returns the rows under ``holdings.all``, but the current
        # holdings date is a component-level data point (``holdings.asOfDate``).
        # Keep the row-level lookup for compatibility with older responses.
        component_data_points = holdings_component.get("dataPointsByNameMap", {})
        update_date = self._parse_product_data_date(
            self._data_point_value(component_data_points, "asOfDate")
            or self._data_point_value(data_points, "asOfDate")
        )
        if not update_date:
            raise ValueError("无法找到更新日期")

        tickers = self._data_point_list(data_points, "ticker")
        names = self._data_point_list(data_points, "issueName")
        asset_classes = self._data_point_list(data_points, "assetClass")
        weights = self._data_point_list(data_points, "holdingPercent")
        market_values = self._data_point_list(data_points, "marketValue")
        prices = self._data_point_list(data_points, "unitPrice")
        shares_list = self._data_point_list(data_points, "unitsHeld")
        exchanges = self._data_point_list(data_points, "exchange")
        row_count = max(
            len(tickers),
            len(names),
            len(asset_classes),
            len(weights),
            len(market_values),
            len(prices),
            len(shares_list),
            len(exchanges),
        )
        if row_count <= 0:
            raise ValueError("iShares product-data 持仓为空")

        holdings: List[ETFHolding] = []
        total_weight = 0.0
        for index in range(row_count):
            symbol = self._at(tickers, index, "")
            name = self._at(names, index, "")
            asset_class = self._at(asset_classes, index, "")
            weight = self._safe_float(self._at(weights, index, 0.0)) / 100
            market_value = self._safe_float(self._at(market_values, index, 0.0))
            price = self._safe_float(self._at(prices, index, 0.0))
            shares = int(self._safe_float(self._at(shares_list, index, 0.0)))
            exchange = self._at(exchanges, index, "")

            normalized_symbol, normalized_asset_class = self._normalize_holding_symbol(
                symbol=symbol,
                asset_class=asset_class,
                exchange=exchange,
            )
            if not normalized_symbol:
                continue
            total_weight += weight
            holdings.append(
                ETFHolding(
                    symbol=normalized_symbol,
                    name=str(name or "").strip(),
                    asset_class=normalized_asset_class,
                    shares=shares,
                    market_value=market_value,
                    weight=weight,
                    price=price if price else None,
                )
            )

        if not holdings:
            raise ValueError("iShares product-data 持仓为空")

        return ETFHoldingsData(
            holdings=holdings,
            update_date=update_date,
            total_shares=None,
            total_weight=total_weight,
        )

    def _get_holdings_from_csv(self, etf_symbol: str) -> ETFHoldingsData:
        url = self.ETF_CONFIGS[etf_symbol]['url']
        params = {
            'fileType': 'csv',
            'fileName': f'{etf_symbol}_holdings',
            'dataType': 'fund'
        }
        
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=30)
            response.raise_for_status()
            if response.text.lstrip().lower().startswith("<!doctype html") or response.text.lstrip().lower().startswith("<html"):
                raise ValueError("iShares CSV 接口返回 HTML 页面")
            
            csv_data = StringIO(response.text)
            reader = csv.reader(csv_data)
            
            # 读取文件头部信息
            update_date = None
            total_shares = None
            headers = None
            
            for row in reader:
                if not row:  # 跳过空行
                    continue
                
                first_cell = row[0].strip().strip('"').lstrip("\ufeff")
                if first_cell == "Fund Holdings as of":
                    # 解析日期字符串 (格式如: "Mar 21, 2025")
                    date_str = row[1].strip('"')
                    update_date = datetime.strptime(date_str, "%b %d, %Y").date()
                elif first_cell == "Shares Outstanding":
                    total_shares = float(row[1].strip('"').replace(',', ''))
                elif first_cell == "Ticker":  # 找到标题行
                    headers = row
                    break
            
            if not headers:
                raise ValueError("无法找到CSV标题行")
            if not update_date:
                raise ValueError("无法找到更新日期")
            
            holdings = []
            total_weight = 0
            
            # 读取所有持仓数据
            for row in reader:
                if not row or not row[0] or not row[0].strip('\xa0') or row[0].strip('"').startswith('The content'):
                    continue
                    
                holding = dict(zip(headers, row))
                try:
                    # 处理新格式中的引号
                    symbol = str(holding['Ticker']).strip('"')
                    name = str(holding['Name']).strip('"')
                    asset_class = str(holding['Asset Class']).strip('"')
                    
                    # 处理数值字段，移除引号和逗号
                    weight = float(holding['Weight (%)'].strip('"').replace(',', '')) / 100
                    market_value = float(holding['Market Value'].strip('"').replace(',', ''))
                    price = float(holding['Price'].strip('"').replace(',', ''))
                    shares = int(float(holding['Quantity'].strip('"').replace(',', '')))
                    exchange = holding['Exchange'].strip('"')

                    symbol, asset_class = self._normalize_holding_symbol(
                        symbol=symbol,
                        asset_class=asset_class,
                        exchange=exchange,
                    )
                    if not symbol:
                        self.logger.warning(f"跳过无法规范化的 iShares 股票代码: {holding['Ticker']}")
                        continue
                    total_weight += weight
                    
                    holdings.append(ETFHolding(
                        symbol=symbol,
                        name=name,
                        asset_class=asset_class,
                        shares=shares,
                        market_value=market_value,
                        weight=weight,
                        price=price if price else (market_value / shares if shares else None)
                    ))
                except (ValueError, KeyError) as e:
                    self.logger.warning(f"处理持仓数据时出错: {str(e)}, row: {row}")
                    continue
            
            return ETFHoldingsData(
                holdings=holdings,
                update_date=update_date,
                total_shares=total_shares,
                total_weight=total_weight
            )
            
        except Exception as e:
            self.logger.error(f"获取{etf_symbol}持仓数据失败: {str(e)}")
            raise

    def _normalize_holding_symbol(self, symbol: Any, asset_class: Any, exchange: Any) -> tuple[str, str]:
        symbol_text = str(symbol or "").strip().strip('"')
        asset_class_text = str(asset_class or "").strip().strip('"')
        exchange_text = str(exchange or "").strip().strip('"')
        is_equity = asset_class_text == 'Equity' and not any(
            char.isdigit() or char == '-' for char in symbol_text
        )
        market_suffix = self.exchange_map.get(exchange_text, '')
        if is_equity and market_suffix == '.US':
            normalized_symbol = normalize_us_equity_symbol(symbol_text)
            if not normalized_symbol:
                return "", asset_class_text
            return normalized_symbol, asset_class_text
        if is_equity:
            return symbol_text + market_suffix, asset_class_text
        return symbol_text, 'Other' if asset_class_text == 'Equity' else asset_class_text

    @staticmethod
    def _data_point_value(data_points: Dict[str, Any], key: str) -> Any:
        value = data_points.get(key, {}).get("value")
        if isinstance(value, list):
            return value[0] if value else None
        return value

    @staticmethod
    def _data_point_list(data_points: Dict[str, Any], key: str) -> List[Any]:
        value = data_points.get(key, {}).get("value")
        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value]

    @staticmethod
    def _at(values: List[Any], index: int, default: Any = None) -> Any:
        return values[index] if index < len(values) else default

    @staticmethod
    def _parse_product_data_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        text = str(value).strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%b %d, %Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return default
