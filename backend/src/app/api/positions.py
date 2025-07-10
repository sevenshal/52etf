from fastapi import APIRouter, Depends
from typing import List, Dict
from datetime import datetime, timedelta
import numpy as np
from scipy.stats import norm
from ...core.services.trade import TradeService, OrderSide
from ...core.services.longport import LongPortService
from ...core.services.market import MarketService
from .account import valid_account
from itertools import groupby
from operator import itemgetter
from pydantic import BaseModel
from decimal import Decimal

router = APIRouter(prefix="/api/positions")

def is_option_symbol(symbol: str) -> bool:
    """判断是否为期权代码"""
    # 期权代码通常包含到期日信息,如 AAPL230915C150
    return len(symbol) > 10 and any(c in symbol for c in ['C', 'P'])

def parse_option_info(symbol: str) -> Dict:
    """解析期权代码信息"""
    try:
        # 移除可能的 .US 后缀
        symbol = symbol.split('.')[0]
        
        # 从后向前查找 'C' 或 'P' 的位置
        option_type_pos = -1
        for i, c in enumerate(reversed(symbol)):
            if c in ['C', 'P']:
                option_type_pos = len(symbol) - i - 1
                break
        
        if option_type_pos == -1:
            return None
            
        # 提取期权类型(认购/认沽)
        option_type = "Call" if symbol[option_type_pos] == 'C' else "Put"
        
        # 提取到期日 (位于期权类型标识符前的6位)
        date_str = symbol[option_type_pos-6:option_type_pos]
        expiry = datetime.strptime(f"20{date_str}", "%Y%m%d").date()
        
        # 提取行权价 (位于期权类型标识符后的所有数字)
        strike_price = float(symbol[option_type_pos+1:]) / 1000  # 转换为实际价格

        # 提取股票代码
        stock_symbol = symbol[:option_type_pos-6] + '.US'
        
        return {
            "expiry": expiry,
            "option_type": option_type,
            "strike_price": strike_price,
            "stock_symbol": stock_symbol
        }
    except Exception as e:
        return None

# 删除原来的 parse_stock_symbol 函数

def calculate_exercise_probability(
    stock_price: float,
    strike_price: float,
    days_to_expiry: float,
    implied_volatility: float,
    option_type: str,
    risk_free_rate: float = 0.05
) -> float:
    """计算期权行权概率"""
    try:
        T = days_to_expiry / 365  # 转换为年
        if T <= 0:
            return 0.0
            
        S = stock_price
        K = strike_price
        a = implied_volatility
        
        # 如果没有传入无风险利率，则使用默认值0.05
        if risk_free_rate is None:
            risk_free_rate = 0.05
            
        r = risk_free_rate
        
        d1 = (np.log(S/K) + (r + a**2/2)*T) / (a*np.sqrt(T))
        d2 = d1 - a*np.sqrt(T)
        
        # 根据期权类型计算概率
        if option_type == "Call":
            probability = norm.cdf(d2)
        else:  # Put
            probability = norm.cdf(-d2)
            
        return round(probability * 100, 2)  # 转换为百分比并保留两位小数
    except Exception as e:
        return 0.0

# 添加响应模型定义
class OptionPositionsResponse(BaseModel):
    positions: List[Dict]
    risk_free_rate: float
    rate_update_date: str

@router.get("/options")
async def get_option_positions(
    account_id: str = Depends(valid_account),
    option_type: str = None  # 可选值: "Call" 或 "Put"
):
    """获取期权持仓汇总"""
    trade_service: LongPortService = LongPortService(account_id)
    market_service = MarketService()
    
    # 获取最新的联邦基金利率
    fed_rate = await market_service.get_fed_rate()
    risk_free_rate = fed_rate['targetRateTo'] / 100 if fed_rate else 0.05
    
    positions = trade_service.stock_positions()
    
    # 获取所有期权的卖出订单历史
    sell_orders = trade_service.history_orders(side=OrderSide.Sell, time_interval=timedelta(days=540))
    # 按symbol分组，找出每个symbol最新的订单时间
    order_dates = {}
    for order in sell_orders:
        symbol = order['symbol']
        submitted_at = order['submitted_at']
        if symbol not in order_dates or submitted_at > order_dates[symbol]:
            order_dates[symbol] = submitted_at
    
    # 过滤出空头期权持仓
    option_positions = []
    stock_symbols = set()
    option_symbols = []
    
    for pos in positions:
        if is_option_symbol(pos["symbol"]) and pos["quantity"] < 0:
            option_info = parse_option_info(pos["symbol"])
            # 增加期权类型过滤
            if option_info and (option_type is None or option_info["option_type"] == option_type):
                stock_symbol = option_info["stock_symbol"]
                if stock_symbol:
                    stock_symbols.add(stock_symbol)
                    option_symbols.append(pos["symbol"])
                    option_positions.append({
                        **pos,
                        **option_info,
                        "strike_amount": float(pos["quantity"]) * float(option_info["strike_price"]) * 100,
                        "stock_symbol": stock_symbol,
                        "created_at": order_dates.get(pos["symbol"])
                    })
    
    # 批量获取实时价格
    stock_quotes = {
        quote["symbol"]: quote["price"] 
        for quote in trade_service.get_quote_batch(list(stock_symbols))
    }
    
    # 修改获取期权报价的部分
    option_quotes = {
        quote["symbol"]: quote 
        for quote in trade_service.get_option_quote_batch(option_symbols)
    }
    
    # 添加实时价格信息和行权概率
    current_date = datetime.now().date()
    for pos in option_positions:
        stock_price = stock_quotes.get(pos["stock_symbol"])
        option_quote = option_quotes.get(pos["symbol"])
        pos["stock_price"] = stock_price
        
        if option_quote:
            pos["market_price"] = option_quote["price"]
            pos["implied_volatility"] = option_quote["implied_volatility"]
            
            # 计算到期天数
            days_to_expiry = (option_quote['expiry_date'] - current_date).days
            
            # 计算行权概率
            if stock_price and pos["implied_volatility"]:
                pos["exercise_probability"] = calculate_exercise_probability(
                    stock_price=stock_price,
                    strike_price=pos["strike_price"],
                    days_to_expiry=days_to_expiry,
                    implied_volatility=pos["implied_volatility"],
                    option_type=pos["option_type"],
                    risk_free_rate=risk_free_rate
                )
            else:
                pos["exercise_probability"] = 0.0
        else:
            pos["market_price"] = pos["cost_price"]
            pos["implied_volatility"] = 0.0
            pos["exercise_probability"] = 0.0
    
    # 按到期日分组
    option_positions.sort(key=lambda x: x["expiry"])
    grouped_positions = []
    
    for expiry, positions in groupby(option_positions, key=lambda x: x["expiry"]):
        positions_list = list(positions)
        total_strike_value = sum(p["strike_amount"] for p in positions_list)
        
        grouped_positions.append({
            "expiry": expiry.isoformat(),
            "positions": positions_list,
            "total_strike_value": total_strike_value
        })
    
    # 修改返回部分
    return OptionPositionsResponse(
        positions=grouped_positions,
        risk_free_rate=risk_free_rate,
        rate_update_date=fed_rate['effectiveDate'] if fed_rate else None
    )


# 添加新的响应模型
class StockPositionsResponse(BaseModel):
    positions: List[Dict]
    summary: Dict

# 添加新的路由处理函数
@router.get("/stocks")
async def get_stock_positions(account_id: str = Depends(valid_account)):
    """获取正股持仓汇总"""
    trade_service: LongPortService = LongPortService(account_id)
    
    # 获取所有持仓
    positions = trade_service.stock_positions()
    
    # 分别计算正股和期权持仓
    stock_positions = []
    option_positions = []
    stock_symbols = []
    option_symbols = []
    total_cost = Decimal('0')
    
    for pos in positions:
        if is_option_symbol(pos["symbol"]):
            option_positions.append(pos)
            option_symbols.append(pos["symbol"])
        else:
            stock_symbols.append(pos["symbol"])
            position_cost = pos["quantity"] * pos["cost_price"]
            total_cost += position_cost
            stock_positions.append({
                **pos,
                "position_cost": float(position_cost)
            })
    
    # 获取账户资金信息
    balance = trade_service.account_balance()
    
    # 获取实时价格
    quotes = {
        quote["symbol"]: quote 
        for quote in trade_service.get_quote_batch(stock_symbols)
    }
    
    # 计算持仓市值和盈亏
    total_market_value = Decimal('0')
    for pos in stock_positions:
        quote = quotes.get(pos["symbol"])
        if quote:
            current_price = Decimal(str(quote["price"]))
            market_value = current_price * Decimal(str(pos["quantity"]))
            total_market_value += market_value
            pos.update({
                "current_price": float(current_price),
                "market_value": float(market_value),
                "unrealized_pnl": float(market_value - Decimal(str(pos["position_cost"]))),
                "unrealized_pnl_percent": float((market_value / Decimal(str(pos["position_cost"])) - 1) * 100)
            })
    
    # 计算期权持仓市值
    option_market_value = Decimal('0')
    if option_positions:
        option_quotes = {
            quote["symbol"]: quote 
            for quote in trade_service.get_option_quote_batch(option_symbols)
        }
        
        for pos in option_positions:
            quote = option_quotes.get(pos["symbol"])
            if quote:
                market_value = Decimal(str(quote["price"])) * Decimal(str(pos["quantity"])) * 100  # 乘以100是因为期权单位是100股
                option_market_value += market_value

    # 现金 = 冻结资金+可用余额
    cash_balance = balance.get("available_balance", 0) + balance.get("frozen_balance", 0)
    # 计算总资产（包含期权市值）
    total_assets = total_market_value + cash_balance + option_market_value
    
    # 计算每个持仓的占比
    for pos in stock_positions:
        pos["position_ratio"] = float(Decimal(str(pos["market_value"])) / total_assets * 100)
    
    # 按市值排序
    stock_positions.sort(key=lambda x: x["market_value"], reverse=True)
    
    # 计算无风险资产（短期国债ETF）
    risk_free_amount = 0
    risk_free_symbols = ['TBIL.US', 'SGOV.US']
    for pos in stock_positions:
        if pos["symbol"] in risk_free_symbols:
            risk_free_amount += Decimal(str(pos["market_value"]))

    # 汇总信息
    summary = {
        "stock_count": len(stock_positions),
        "total_cost": float(total_cost),
        "total_market_value": float(total_market_value),
        "total_unrealized_pnl": float(total_market_value - total_cost),
        "total_unrealized_pnl_percent": float((total_market_value / total_cost - 1) * 100) if total_cost > 0 else 0,
        "cash_balance": cash_balance,
        "option_market_value": float(option_market_value),  # 添加期权市值
        "risk_amount": float(total_market_value - risk_free_amount),
        "risk_free_amount": float(risk_free_amount),
        "total_assets": float(total_assets)  # 更新后的总资产（包含期权市值）
    }
    
    return StockPositionsResponse(
        positions=stock_positions,
        summary=summary
    )