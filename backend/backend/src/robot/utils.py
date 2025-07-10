import os
from decimal import Decimal
from typing import Dict, Any, List, Tuple
from ..core.models.strategy import StrategyCfg
from ..core.database import StockEVC

import statistics
def is_stock_undervalued(
    stock: StockEVC, 
    undervalue_threshold: float, 
    next_fy_growth_threshold: float
) -> bool:
    """
    判断股票是否低估
    
    Args:
        stock: 股票数据
        undervalue_threshold: 低估阈值，例如0.9表示股价低于估值的90%
        next_fy_growth_threshold: 下一财年增长阈值，例如1.1表示增长至少10%
    
    Returns:
        bool: 是否低估
    """
    
    if stock.last_price is None or stock.fair_value_lo is None or stock.forward_next_fy_lo is None:
        return False
    
    # 计算价格低估比率和增长率
    undervalue_ratio = stock.last_price / stock.fair_value_lo if stock.fair_value_lo > 0 else float('inf')
    next_fy_growth = stock.forward_next_fy_lo / stock.fair_value_lo if stock.fair_value_lo > 0 else 0
    
    return undervalue_ratio <= undervalue_threshold and next_fy_growth >= next_fy_growth_threshold

def is_stock_overvalued(
    stock: StockEVC, 
    current_fy_hi_threshold: float = 1.0,
    next_fy_median_threshold: float = 1.0
) -> bool:
    """
    判断股票是否高估
    
    Args:
        stock: 股票数据
        current_fy_hi_threshold: 当前财年估值上限阈值，默认1.0表示股价高于当前估值上限
        next_fy_median_threshold: 下财年估值中位数阈值，默认1.0表示股价高于下财年估值中位数
    
    Returns:
        bool: 是否高估
    """
    if stock.last_price is None or stock.fair_value_hi is None:
        return False
    
    # 计算下财年估值中位数
    next_fy_median = ((stock.forward_next_fy_lo or 0.0) + (stock.forward_next_fy_hi or 0.0)) / 2
    
    # 股价超过当前财年估值上限阈值 且 超过下财年估值中位数阈值
    return (stock.last_price > stock.fair_value_hi * current_fy_hi_threshold and 
            stock.last_price > next_fy_median * next_fy_median_threshold)

def filter_stock_value_info_list(
    stocks: List[StockEVC],
    strategy_cfg: StrategyCfg
) -> Tuple[List[StockEVC], List[StockEVC], List[StockEVC]]:
    """
    过滤股票列表，将其分为低估、高估和合理估值三类
    
    Args:
        stocks: 股票数据列表
        undervalue_threshold: 低估阈值
        next_fy_growth_threshold: 下一财年增长阈值
        current_fy_hi_threshold: 当前财年估值上限阈值
        next_fy_median_threshold: 下财年估值中位数阈值
    Returns:
        Tuple[List[StockEvcInfo], List[StockEvcInfo], List[StockEvcInfo]]: 
        (低估股票列表, 高估股票列表, 合理估值股票列表)
    """
    undervalued = []
    overvalued = []
    fairvalued = []

    for stock in stocks:
        if is_stock_undervalued(
            stock, 
            strategy_cfg.undervalue_threshold, 
            strategy_cfg.next_fy_growth_threshold
        ):
            undervalued.append(stock)
        elif is_stock_overvalued(stock, strategy_cfg.current_fy_hi_threshold, strategy_cfg.next_fy_median_threshold):
            overvalued.append(stock)
        else:
            fairvalued.append(stock)

    return undervalued, overvalued, fairvalued

def _filter_stop_loss(kline):
    sorted_data = sorted(kline, key=lambda x: x['timestamp'])
    last_k = sorted_data[-1]
    open_price = float(last_k['open_price'])
    close_price = float(last_k['close_price'])
    high_price = float(last_k['high_price'])
    low_price = float(last_k['low_price'])
    turnover = float(last_k['turnover'])
    lst_avg_turnover = statistics.mean([float(item['turnover']) for item in sorted_data[0:-1]])
    return (open_price + close_price) > (high_price+low_price) and \
        (min(open_price, close_price)-low_price) > abs(open_price-close_price) * 1.5  and \
        turnover > lst_avg_turnover * 1.5

def get_stop_loss(kline_list: List[Dict]) -> List[str]:
    """
    获取止跌形态股票列表
    
    Args:
        kline_list: K线数据列表，每个元素包含 'code' 和 'kline_data' 字段
        
    Returns:
        List[str]: 符合止跌形态的股票代码列表
    """
    return list(map(lambda x: x['code'], filter(lambda x: _filter_stop_loss(x['kline_data']), kline_list)))
