from dataclasses import dataclass
from datetime import date
from typing import List, Optional

@dataclass
class ETFHolding:
    """ETF持仓数据模型"""
    symbol: str                # 股票代码
    name: str                  # 股票名称
    asset_class: str          # 资产类别(Equity/Bond/Cash等)
    shares: int               # 持有数量
    weight: float             # 权重(0-1)
    market_value: float       # 市值
    price: Optional[float]    # 价格

@dataclass
class ETFHoldingsData:
    """ETF完整持仓数据"""
    holdings: List[ETFHolding]  # 持仓列表
    update_date: date           # 更新日期
    total_shares: Optional[float]  # ETF总股数
    total_weight: float        # 总权重 
