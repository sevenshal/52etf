import requests
import pandas as pd
from datetime import datetime
from typing import Dict
from io import StringIO
from .base import ETFDataFetcher
from ...core.models.etf import ETFHolding, ETFHoldingsData

class QQQDataFetcher(ETFDataFetcher):
    """Invesco QQQ Trust (QQQ) 数据获取"""
    
    def __init__(self):
        super().__init__()
        self.url = 'https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0?audienceType=Investor&action=download&ticker=QQQ'
        self.name = '纳斯达克100ETF'

    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取QQQ持仓数据"""
        try:
            # 下载CSV文件
            response = requests.get(self.url, headers=self.headers)
            response.raise_for_status()
            
            # 读取CSV数据
            df = pd.read_csv(StringIO(response.text))
            
            holdings = []
            total_weight = 0
            update_date = None
            
            # 处理每一行数据
            for _, row in df.iterrows():
                try:
                    # 获取基本数据
                    ticker = str(row['Holding Ticker']).strip() + '.US'
                    name = str(row['Name']).strip()
                    shares = int(float(str(row['Shares/Par Value']).replace(',', '')))
                    weight = float(str(row['Weight']).replace('%', '')) / 100
                    market_value = float(str(row['MarketValue']).replace(',', ''))
                    asset_class = str(row['Class of Shares']).strip()
                    
                    # 获取日期（从第一行就可以）
                    if update_date is None:
                        date_str = str(row['Date']).strip()
                        update_date = datetime.strptime(date_str, "%m/%d/%Y").date()
                    
                    total_weight += weight
                    holdings.append(ETFHolding(
                        symbol=ticker,
                        name=name,
                        asset_class= "Equity" if asset_class == "Common Stock" or asset_class == "American Depository Receipt" else asset_class,
                        shares=shares,
                        weight=weight,
                        market_value=market_value,
                        price=market_value / shares if shares > 0 else 0
                    ))
                except (ValueError, KeyError) as e:
                    self.logger.warning(f"处理QQQ持仓数据行时出错: {str(e)}, row: {row}")
                    continue
            
            if not update_date:
                raise ValueError("无法找到更新日期")
            
            return ETFHoldingsData(
                holdings=holdings,
                update_date=update_date,
                total_shares=None,  # QQQ的固定总股数
                total_weight=total_weight
            )
            
        except Exception as e:
            self.logger.error(f"获取QQQ持仓数据失败: {str(e)}")
            raise 
