from typing import List, Dict
from datetime import datetime
from futu import (
    OpenSecTradeContext,
    SecurityFirm,
    TrdSide,
    TrdEnv,
    OrderType,
    TimeInForce,
    TrdMarket,
    RET_OK
)
from .trade import TradeService as CoreTradeService, OrderSide, OrderType as CoreOrderType, TimeInForceType, OutsideRTH


############################ 全局变量设置 ############################
FUTUOPEND_ADDRESS = '127.0.0.1'  # OpenD 监听地址
FUTUOPEND_PORT = 11111  # OpenD 监听端口

TRADING_ENVIRONMENT = TrdEnv.REAL  # 交易环境：真实 / 模拟
TRADING_MARKET = TrdMarket.US  # 交易市场权限，用于筛选对应交易市场权限的账户
TRADING_PWD = '042013'  # 交易密码，用于解锁交易

# 映射字典
order_side_map = {
    OrderSide.Buy: TrdSide.BUY,
    OrderSide.Sell: TrdSide.SELL
}

order_type_map = {
    CoreOrderType.MO: OrderType.MARKET,
    CoreOrderType.LO: OrderType.NORMAL
}

time_in_force_map = {
    TimeInForceType.Day: TimeInForce.DAY,
    TimeInForceType.GTC: TimeInForce.GTC
}

outside_rth_map = {
    OutsideRTH.AnyTime: True,
    OutsideRTH.RTHOnly: False
}

class FutuTradeService(CoreTradeService):
    """交易服务"""
    
    def __init__(self):
        self.ctx = OpenSecTradeContext(host=FUTUOPEND_ADDRESS, port=FUTUOPEND_PORT, security_firm=SecurityFirm.FUTUSECURITIES, filter_trdmarket = TrdMarket.US)
        self._unlock_trade()
    # 解锁交易
    def _unlock_trade(self):
        if TRADING_ENVIRONMENT == TrdEnv.REAL:
            ret, data = self.ctx.unlock_trade(TRADING_PWD)
            if ret != RET_OK:
                print('解锁交易失败：', data)
                return False
            print('解锁交易成功！')
        return True

    def _convert_symbol_to_futu(self, symbol: str) -> str:
        """将 symbol 转换为富途格式"""
        # 假设 symbol 格式为 'AAPL.US'，我们需要将其转换为 'US.AAPL'
        parts = symbol.split('.')
        if len(parts) != 2:
            raise ValueError(f"Invalid symbol format: {symbol}")
        return f"{parts[1]}.{parts[0]}"

    def _convert_symbol_from_futu(self, symbol: str) -> str:
        """将 symbol 从富途格式转换回原始格式"""
        # 假设 symbol 格式为 'US.AAPL'，我们需要将其转换为 'AAPL.US'
        parts = symbol.split('.')
        if len(parts) != 2:
            raise ValueError(f"Invalid symbol format: {symbol}")
        return f"{parts[1]}.{parts[0]}"

    def today_orders(self, symbol: str = None, side: OrderSide = None) -> List[Dict]:
        """获取当日订单"""
        side_code = order_side_map.get(side) if side else None
        symbol = self._convert_symbol_to_futu(symbol) if symbol else None
        
        ret, data = self.ctx.history_order_list_query(
            status_filter_list=[],
            code=symbol,
            start=datetime.now().strftime("%Y-%m-%d 00:00:00"),
            trd_env=TRADING_ENVIRONMENT
        )
        
        if ret != 0:
            raise Exception(f"获取当日订单失败: {data}")
        
        # 将 DataFrame 转换为 List[Dict] 并映射到接口定义的字段
        orders = data.to_dict('records')
        return list(filter(lambda order: order['side'] == side_code, [
            {
                'order_id': order['order_id'],
                'symbol': self._convert_symbol_from_futu(order['code']),
                'side': order['trd_side'],
                'quantity': order['qty'],
                'price': order['price'],
                'status': order['order_status']
            }
            for order in orders
        ]))
    
    def stock_positions(self) -> List[Dict]:
        """获取持仓信息"""
        ret, data = self.ctx.position_list_query(trd_env=TRADING_ENVIRONMENT, position_market=TrdMarket.US)
        if ret != 0:
            raise Exception(f"获取持仓信息失败: {data}")
        
        # 将 DataFrame 转换为 List[Dict] 并映射到接口定义的字段
        positions = data.to_dict('records')
        return [
            {
                'symbol': self._convert_symbol_from_futu(position['code']),
                'quantity': position['qty'],
                'available_quantity': position['can_sell_qty'],
                'cost_price': position['cost_price'],
                'market': position['position_market'],
                'currency': position['currency'],
                'init_quantity': position['qty']  # 假设初始数量等于当前数量
            }
            for position in positions
        ]
    
    def account_balance(self) -> Dict:
        """获取账户余额"""
        ret, data = self.ctx.accinfo_query(trd_env=TRADING_ENVIRONMENT)
        if ret != 0:
            raise Exception(f"获取账户余额失败: {data}")
        
        # 将 DataFrame 转换为 Dict 并映射到接口定义的字段
        balance = data.to_dict('records')[0]
        return {
            'available_balance': balance['available_funds'],
            'frozen_balance': balance['frozen_cash'],
            'withdraw_cash': balance['avl_withdrawal_cash'],
            'currency': balance['currency']
        }
    
    def get_position_info(self, symbol: str) -> Dict:
        """获取持仓信息"""
        symbol = self._convert_symbol_to_futu(symbol)
        ret, data = self.ctx.position_list_query(code=symbol, trd_env=TRADING_ENVIRONMENT)
        if ret != 0:
            raise Exception(f"获取持仓信息失败: {data}")
        
        # 将 DataFrame 转换为 Dict 并映射到接口定义的字段
        position = data.to_dict('records')[0] if not data.empty else {}
        return {
            'symbol': self._convert_symbol_from_futu(position.get('code', '')),
            'quantity': position.get('qty', 0),
            'available_quantity': position.get('can_sell_qty', 0),
            'cost_price': position.get('cost_price', 0.0),
            'market': position.get('position_market', ''),
            'currency': position.get('currency', ''),
            'init_quantity': position.get('qty', 0)  # 假设初始数量等于当前数量
        }
    
    def submit_order(self, side: OrderSide, symbol: str, order_type: CoreOrderType,
                     submitted_price: float, submitted_quantity: int,
                     time_in_force: TimeInForceType, outside_rth: OutsideRTH, remark: str) -> str:
        """提交订单"""
        side_code = order_side_map[side]
        order_type_code = order_type_map[order_type]
        time_in_force_code = time_in_force_map[time_in_force]
        fill_outside_rth = outside_rth_map[outside_rth]
        symbol = self._convert_symbol_to_futu(symbol)
        
        ret, data = self.ctx.place_order(
            price=submitted_price,
            qty=submitted_quantity,
            code=symbol,
            trd_side=side_code,
            order_type=order_type_code,
            trd_env=TRADING_ENVIRONMENT,
            remark=remark,
            time_in_force=time_in_force_code,
            fill_outside_rth=fill_outside_rth
        )
        
        if ret != 0:
            raise Exception(f"提交订单失败: {data}")
        
        # 返回订单ID
        return data.to_dict('records')[0]['order_id']
    