import requests
import csv
from io import StringIO
from datetime import datetime
from typing import Dict
from .base import ETFDataFetcher
from ...core.models.etf import ETFHolding, ETFHoldingsData

class ISharesETFFetcher(ETFDataFetcher):
    """iShares ETF数据获取"""
    
    ETF_CONFIGS = {
        'SOXX.US': {
            'url': 'https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax',
            'name': 'iShares费城半导体ETF'
        },
        'IWM.US': {
            'url': 'https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax',
            'name': 'iShares罗素2000ETF'
        },
        'ITB.US': {
            'url': 'https://www.ishares.com/us/products/239512/ishares-us-home-construction-etf/1467271812596.ajax',
            'name': 'iShares美国房屋建筑ETF'
        },
        'ITA.US': {
            'url': 'https://www.ishares.com/us/products/239502/ishares-us-aerospace-defense-etf/1467271812596.ajax',
            'name': 'iShares美国航空航天防务ETF'
        },
        'IAK.US': {
            'url': 'https://www.ishares.com/us/products/239515/ishares-us-insurance-etf/1467271812596.ajax',
            'name': 'iShares美国保险ETF'
        }
    }

    exchange_map = {
        "NASDAQ": ".US",
        "New York Stock Exchange Inc.": ".US",
        "Nyse Mkt Llc": ".US"
    }

    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取iShares ETF持仓数据"""
        if etf_symbol not in self.ETF_CONFIGS:
            raise ValueError(f"不支持的ETF: {etf_symbol}")
            
        self.name = self.ETF_CONFIGS[etf_symbol]['name']  # 设置当前ETF名称
        url = self.ETF_CONFIGS[etf_symbol]['url']
        params = {
            'fileType': 'csv',
            'fileName': f'{etf_symbol}_holdings',
            'dataType': 'fund'
        }
        
        try:
            response = requests.get(url, params=params, headers=self.headers)
            response.raise_for_status()
            
            csv_data = StringIO(response.text)
            reader = csv.reader(csv_data)
            
            # 读取文件头部信息
            update_date = None
            total_shares = None
            headers = None
            
            for row in reader:
                if not row:  # 跳过空行
                    continue
                
                if row[0] == "Fund Holdings as of":
                    # 解析日期字符串 (格式如: "Mar 21, 2025")
                    date_str = row[1].strip('"')
                    update_date = datetime.strptime(date_str, "%b %d, %Y").date()
                elif row[0] == "Shares Outstanding":
                    total_shares = float(row[1].strip('"').replace(',', ''))
                elif row[0] == "Ticker":  # 找到标题行
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
                    market_suffix = self.exchange_map.get(exchange, '')

                    # 判断是否为股票类型
                    is_equity = asset_class == 'Equity' and not any(char.isdigit() or char == '-' for char in symbol)
                    symbol = symbol + market_suffix if is_equity else symbol
                    total_weight += weight
                    
                    holdings.append(ETFHolding(
                        symbol=symbol,
                        name=name,
                        asset_class='Other' if not is_equity and asset_class == 'Equity' else asset_class,
                        shares=shares,
                        market_value=market_value,
                        weight=weight,
                        price=price if (is_equity or not shares) else (market_value / shares)
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
