import requests
import pandas as pd
from datetime import datetime
from typing import Dict
from io import BytesIO
from .base import ETFDataFetcher
from ...core.models.etf import ETFHolding, ETFHoldingsData

class SPDRDataFetcher(ETFDataFetcher):
    """SPDR ETF数据获取"""
    
    ETF_CONFIGS = {
        'SPY.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx',
            'name': '标普500ETF'
        },
        'XBI.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xbi.xlsx',
            'name': '标普生物科技ETF'
        },
        'KRE.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-kre.xlsx',
            'name': '标普地区银行ETF'
        },
        'XLF.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlf.xlsx',
            'name': '标普金融ETF'
        },
        'XLV.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlv.xlsx',
            'name': '标普医疗保健ETF'
        },
        'XLK.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlk.xlsx',
            'name': '标普科技精选ETF'
        },
        'XRT.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xrt.xlsx',
            'name': '标普零售ETF'
        },
        'XLC.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlc.xlsx',
            'name': '标普通信服务ETF'
        },
        'XLE.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xle.xlsx',
            'name': '标普能源ETF'
        },
        'XLI.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xli.xlsx',
            'name': '标普工业ETF'
        },
        'XLP.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlp.xlsx',
            'name': '标普必需消费ETF'
        },
        'XLU.US': {
            'url': 'https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlu.xlsx',
            'name': '标普公用事业ETF'
        },
    }
    
    def __init__(self):
        super().__init__()
        self.name = None  # 将在get_holdings中设置

    def get_holdings(self, etf_symbol: str) -> ETFHoldingsData:
        """获取SPDR ETF持仓数据"""
        if etf_symbol not in self.ETF_CONFIGS:
            raise ValueError(f"不支持的ETF: {etf_symbol}")
            
        config = self.ETF_CONFIGS[etf_symbol]
        self.name = config['name']
        
        try:
            # 下载Excel文件
            response = requests.get(config['url'], headers=self.headers)
            response.raise_for_status()
            
            # 读取Excel文件
            df = pd.read_excel(BytesIO(response.content))
            
            # 查找更新日期
            update_date = None
            for idx, row in df.iterrows():
                first_col = row.iloc[0] if len(row) > 0 else None
                second_col = row.iloc[1] if len(row) > 1 else None
                if isinstance(first_col, str) and 'Holdings:' in first_col and isinstance(second_col, str):
                    # 格式如: "Holdings: As of 16-Jan-2025"
                    date_str = second_col.split('As of ')[-1].strip()
                    try:
                        update_date = datetime.strptime(date_str, "%d-%b-%Y").date()
                        break
                    except ValueError:
                        self.logger.warning(f"无法解析日期: {date_str}")
            
            if not update_date:
                raise ValueError("无法找到更新日期")
            
            # 查找表头行
            header_row = None
            for idx, row in df.iterrows():
                if 'Ticker' in row.values:
                    header_row = idx
                    break
                
            if header_row is None:
                raise ValueError("无法找到表头行")
            
            # 提取数据行
            data_df = df.iloc[header_row + 1:].copy()
            data_df.columns = df.iloc[header_row]

            # 获取列名映射
            ticker_col = 'Ticker'
            name_col = 'Name'
            shares_col = 'Shares Held'
            weight_col = 'Weight'
            
            holdings = []
            total_weight = 0
            
            # 处理每一行数据
            for _, row in data_df.iterrows():
                try:
                    # 检查是否为空行或无效数据
                    if pd.isna(row[ticker_col]) or pd.isna(row[shares_col]) or pd.isna(row[weight_col]):
                        continue
                        
                    shares = float(row[shares_col])
                    weight = float(row[weight_col]) / 100  # 转换为小数
                    
                    total_weight += weight
                    
                    # 判断是否为股票类型
                    ticker = str(row[ticker_col]).strip()
                    is_equity = not any(char.isdigit() or char == '-' for char in ticker)
                    ticker = ticker + '.US' if is_equity else ticker

                    is_usd = str(row[name_col]).strip().upper() == 'US DOLLAR'
                    holdings.append(ETFHolding(
                        symbol=ticker,
                        name=str(row[name_col]).strip(),
                        asset_class='Cash' if is_usd else ('Equity' if is_equity else 'Other'),
                        shares=int(shares),
                        weight=weight,
                        market_value=shares if is_usd else None,
                        price=1 if is_usd else None
                    ))
                except (ValueError, KeyError) as e:
                    self.logger.warning(f"处理SPDR ETF持仓数据行时出错: {str(e)}, row: {row}")
                    continue
            
            return ETFHoldingsData(
                holdings=holdings,
                update_date=update_date,
                total_shares=None,  # 使用 API 获取总股数
                total_weight=total_weight
            )
            
        except Exception as e:
            self.logger.error(f"获取{etf_symbol}持仓数据失败: {str(e)}")
            raise
