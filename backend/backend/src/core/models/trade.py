from dataclasses import dataclass
from typing import Optional
@dataclass
class TradeOperation:
    """交易操作数据模型"""
    symbol: str
    quantity: int
    price: Optional[float]
    reason: str 
