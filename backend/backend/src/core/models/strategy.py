from dataclasses import dataclass

@dataclass
class StrategyCfg:
    """策略配置"""
    max_hold_amount_per_stock: float
    max_hold_stock_count: int
    undervalue_threshold: float
    next_fy_growth_threshold: float
    current_fy_hi_threshold: float = 1.0
    next_fy_median_threshold: float = 1.0
    auto_trading_enabled: bool = False

