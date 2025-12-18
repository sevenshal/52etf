import os
import finnhub
from datetime import datetime
from .base import ETFDataFetcher
from ...core.models.etf import ETFHolding, ETFHoldingsData

class QQQDataFetcher(ETFDataFetcher):
    """Invesco QQQ Trust (QQQ) 数据获取"""
    
    def __init__(self):
        super().__init__()
        self.name = '纳斯达克100ETF'
        self.finnhub_client = finnhub.Client(api_key='cu2k231r01qh0l7hek80cu2k231r01qh0l7hek8g')

    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取QQQ持仓数据"""
        try:
            # This fetcher is specifically for QQQ
            if etf_symbol.upper() != 'QQQ':
                self.logger.warning(f"This fetcher is for QQQ, but it was called with '{etf_symbol}'. Proceeding with QQQ.")
            
            data = self.finnhub_client.etf_holdings('QQQ')

            if not data or 'holdings' not in data:
                self.logger.error("Finnhub API did not return holdings data for QQQ.")
                raise ValueError("Finnhub API did not return holdings data for QQQ")
            
            holdings = []
            total_weight = 0
            
            update_date_str = data.get('atDate')
            if not update_date_str:
                raise ValueError("无法在Finnhub响应中找到更新日期 ('atDate')")
            update_date = datetime.strptime(update_date_str, "%Y-%m-%d").date()
            
            # Process each holding data
            for holding_data in data.get('holdings', []):
                try:
                    shares = int(holding_data['share'])
                    market_value = float(holding_data['value'])
                    weight = float(holding_data['percent']) / 100
                    
                    holdings.append(ETFHolding(
                        symbol=str(holding_data['symbol']).strip() + '.US',
                        name=str(holding_data['name']).strip(),
                        # Finnhub API doesn't provide 'asset_class', defaulting to 'Equity'
                        # as QQQ primarily holds common stocks.
                        asset_class="Equity",
                        shares=shares,
                        weight=weight,
                        market_value=market_value,
                        price=market_value / shares if shares > 0 else 0
                    ))
                    total_weight += weight
                except (ValueError, KeyError, TypeError) as e:
                    self.logger.warning(f"处理QQQ持仓数据行时出错: {str(e)}, row: {holding_data}")
                    continue
            
            return ETFHoldingsData(
                holdings=holdings,
                update_date=update_date,
                total_shares=None,  # Not provided by Finnhub
                total_weight=total_weight
            )
            
        except Exception as e:
            self.logger.error(f"通过Finnhub获取QQQ持仓数据失败: {str(e)}")
            raise
 
