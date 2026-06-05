import requests
from dateutil.parser import parse
from .base import ETFDataFetcher
from ...core.models.etf import ETFHolding, ETFHoldingsData
from ...core.utils import normalize_us_equity_symbol

class VanguardETFFetcher(ETFDataFetcher):
    """Vanguard ETF数据获取"""
    
    ETF_CONFIGS = {
        'VDC.US': {
            'url': 'https://investor.vanguard.com/vmf/api/VDC/portfolio-holding/stock.json',
            'name': 'Vanguard非周期性消费品ETF'
        },
        'VIG.US': {
            'url': 'https://investor.vanguard.com/vmf/api/VIG/portfolio-holding/stock.json',
            'name': 'Vanguard股息增长ETF'
        },
        'VOX.US': {
            'url': 'https://investor.vanguard.com/vmf/api/VOX/portfolio-holding/stock.json',
            'name': 'Vanguard通信服务ETF'
        },
        'VTI.US': {
            'url': 'https://investor.vanguard.com/vmf/api/VTI/portfolio-holding/stock.json',
            'name': 'Vanguard全市场ETF',
            'total_shares': 6028131279
        },
        'VTV.US': {
            'url': 'https://investor.vanguard.com/vmf/api/VTV/portfolio-holding/stock.json',
            'name': 'Vanguard价值ETF',
            'total_shares': 1090548953
        },
        'VUG.US': {
            'url': 'https://investor.vanguard.com/vmf/api/VUG/portfolio-holding/stock.json',
            'name': 'Vanguard成长ETF',
            'total_shares': 697502091
        }
    }

    def __init__(self):
        super().__init__()
        self.headers.update({
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'referer': 'https://investor.vanguard.com/',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin'
        })

    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取Vanguard ETF持仓数据"""
        if etf_symbol not in self.ETF_CONFIGS:
            raise ValueError(f"不支持的ETF: {etf_symbol}")
            
        config = self.ETF_CONFIGS[etf_symbol]
        self.name = config['name']  # 设置当前ETF名称
        
        try:
            response = requests.get(config['url'] + '?start=1&count=5000', headers=self.headers)
            response.raise_for_status()
            
            data = response.json()
            holdings = []
            total_weight = 0
            update_date = parse(data['asOfDate']).date()
            
            for entity in data['fund']['entity']:
                try:
                    ticker = normalize_us_equity_symbol(entity['ticker'])
                    if not ticker:
                        self.logger.warning(f"跳过无法规范化的 Vanguard 股票代码: {entity.get('ticker')}")
                        continue
                    name = str(entity['longName'])
                    shares = int(entity['sharesHeld'])
                    weight = float(entity['percentWeight']) / 100
                    market_value = float(entity['marketValue'])
                    
                    total_weight += weight
                    holdings.append(ETFHolding(
                        symbol=ticker,
                        name=name,
                        asset_class='Equity',
                        shares=shares,
                        weight=weight,
                        market_value=market_value,
                        price=market_value / shares if shares > 0 else 0
                    ))
                except Exception as e:
                    self.logger.warning(f"处理持仓数据行时出错: {str(e)}")
                    continue
            
            return ETFHoldingsData(
                holdings=holdings,
                total_weight=total_weight,
                total_shares=config.get('total_shares', None),
                update_date=update_date
            )
            
        except Exception as e:
            self.logger.error(f"获取Vanguard ETF {etf_symbol} 持仓数据失败: {str(e)}")
            raise
