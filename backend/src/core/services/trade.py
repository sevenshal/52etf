from abc import ABC, abstractmethod
from typing import List, Dict
from enum import Enum

class OrderSide(Enum):
    Buy = "Buy"
    Sell = "Sell"

class OrderType(Enum):
    MO = "Market Order"
    LO = "Limit Order"

class TimeInForceType(Enum):
    Day = "Day"
    GTC = "Good Till Cancelled"

class OutsideRTH(Enum):
    AnyTime = "AnyTime"
    RTHOnly = "RTHOnly"

class TradeService(ABC):
    @abstractmethod
    def today_orders(self, symbol: str = None, side: OrderSide = None) -> List[Dict]:
        """获取当日订单
        参数:
          symbol: 股票代码
          side: 订单方向
        返回:
          当日订单列表
            字段:
              order_id: 订单ID
              symbol: 股票代码
              side: 订单方向
              quantity: 订单数量
              price: 订单价格
              status: 订单状态
        """
        pass

    @abstractmethod
    def stock_positions(self) -> List[Dict]:
        """获取持仓信息
        返回:
          持仓信息列表
            字段:
              symbol: 股票代码
              quantity: 持仓数量
              available_quantity: 可用数量
              cost_price: 成本价
              market: 市场
              currency: 货币
              init_quantity: 初始数量
        """
        pass

    @abstractmethod
    def account_balance(self) -> Dict:
        """获取账户余额
        返回:
          账户余额
            字段:
              available_balance: 可用余额
              frozen_balance: 冻结余额
              withdraw_cash: 可提现余额
              currency: 货币
        """
        pass

    @abstractmethod
    def get_position_info(self, symbol: str) -> Dict:
        """获取持仓信息
        参数:
          symbol: 股票代码
        返回:
          持仓信息
            字段:
              symbol: 股票代码
              quantity: 持仓数量
              available_quantity: 可用数量
              cost_price: 成本价
              market: 市场
              currency: 货币
              init_quantity: 初始数量
        """
        pass

    @abstractmethod
    def submit_order(self, side: OrderSide, symbol: str, order_type: OrderType,
                     submitted_price: float, submitted_quantity: int,
                     time_in_force: TimeInForceType, outside_rth: OutsideRTH, remark: str) -> str:
        """提交订单
        参数:
          side: 订单方向
          symbol: 股票代码
          order_type: 订单类型
          submitted_price: 提交价格
          submitted_quantity: 提交数量
          time_in_force: 时间限制
          outside_rth: 是否在交易时间外执行
          remark: 备注
        返回:
          订单ID
        """
        pass

    @abstractmethod
    def history_orders(self, symbol: str = None, side: OrderSide = None) -> List[Dict]:
        """获取历史订单
        参数:
          symbol: 股票代码
          side: 订单方向
        返回:
          历史订单列表
            字段:
              order_id: 订单ID
              symbol: 股票代码
              side: 订单方向
              submitted_at: 提交时间
              status: 订单状态
        """
        pass
