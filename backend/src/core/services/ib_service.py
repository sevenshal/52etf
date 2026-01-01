from ib_insync import IB, Stock, MarketOrder, LimitOrder
import asyncio
import logging
import os
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

class IBKRService:
    def __init__(self, host=None, port=None, client_id=None):
        self.host = host or os.getenv('IB_HOST', '127.0.0.1')
        self.port = int(port or os.getenv('IB_PORT', '4001'))
        self.client_id = int(client_id or os.getenv('IB_CLIENT_ID', '1'))
        self.ib = None
        self._positions: Dict[str, float] = {}
        self._available_cash: float = 0.0
        self._net_liquidation: float = 0.0

    async def connect(self, timeout: float = 4.0):
        if self.ib is None:
            self.ib = IB()
            
        if not self.ib.isConnected():
            try:
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=timeout)
                logger.info(f"Connected to IB Gateway on {self.host}:{self.port}")
                # 3 表示请求延迟行情 (Delayed)，当没有实时行情订阅时很有用
                self.ib.reqMarketDataType(3)
                await self.refresh_account_data()
            except Exception as e:
                logger.error(f"Failed to connect to IB Gateway: {e}")
                raise

    def disconnect(self):
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway")

    async def refresh_account_data(self):
        """刷新账户资金和持仓数据"""
        if not self.ib or not self.ib.isConnected():
            return
            
        try:
            # 刷新账户资金
            account_values = {v.tag: v.value for v in self.ib.accountValues()}
            self._net_liquidation = float(account_values.get('NetLiquidation', '0') or 0)
            self._available_cash = float(account_values.get('AvailableFunds', '0') or 0)
            
            # 刷新持仓
            self._positions = {}
            for p in self.ib.positions():
                symbol = p.contract.symbol
                self._positions[symbol] = self._positions.get(symbol, 0) + float(p.position)
                
            logger.info(f"Refreshed IB Account: NetLiq={self._net_liquidation:.2f}, Cash={self._available_cash:.2f}")
        except Exception as e:
            logger.error(f"Failed to refresh IB account data: {e}")

    @property
    def net_liquidation(self) -> float:
        return self._net_liquidation

    @property
    def available_cash(self) -> float:
        return self._available_cash

    def get_position(self, symbol: str) -> float:
        """从缓存获取指定代码的持仓数量"""
        return self._positions.get(symbol.replace('US.', ''), 0)

    async def place_market_order(self, symbol: str, action: str, quantity: int):
        """下市价单"""
        await self.connect()
        clean_symbol = symbol.replace('US.', '')
        contract = Stock(clean_symbol, 'SMART', 'USD')
        await self.ib.qualifyContractsAsync(contract)
        
        order = MarketOrder(action, quantity)
        trade = self.ib.placeOrder(contract, order)
        
        logger.info(f"Placing {action} order for {quantity} {clean_symbol}")
        
        # 等待订单完成或超时
        start_time = asyncio.get_event_loop().time()
        while not trade.isDone():
            await asyncio.sleep(0.5)
            if asyncio.get_event_loop().time() - start_time > 30: # 30秒超时
                logger.warning(f"Order for {clean_symbol} timed out")
                break
                
        # 下单后刷新一次持仓
        await self.refresh_account_data()
        return trade

    async def place_limit_order(self, symbol: str, action: str, quantity: int, price: float, outside_rth: bool = False):
        """下限价单 (支持盘前盘后)"""
        await self.connect()
        clean_symbol = symbol.replace('US.', '')
        contract = Stock(clean_symbol, 'SMART', 'USD')
        await self.ib.qualifyContractsAsync(contract)
        
        order = LimitOrder(action, quantity, price)
        if outside_rth:
            order.outsideRth = True
            
        trade = self.ib.placeOrder(contract, order)
        
        logger.info(f"Placing {action} Limit order for {quantity} {clean_symbol} at ${price:.2f} (OutsideRth={outside_rth})")
        
        # 对于限价单，我们只等待提交成功，不一定要等待完全成交 (因为可能挂单)
        # 但如果是 CopyTrading，我们通常希望尽快成交。
        # 这里简单等待几秒确认状态，不强制要求 Done
        await asyncio.sleep(2)
        
        return trade

    async def get_market_price(self, symbol: str):
        """获取当前市场价格"""
        await self.connect()
        clean_symbol = symbol.replace('US.', '')
        contract = Stock(clean_symbol, 'SMART', 'USD')
        await self.ib.qualifyContractsAsync(contract)
        
        [ticker] = await self.ib.reqTickersAsync(contract)
        return ticker.marketPrice()

    async def get_market_prices(self, symbols: List[str]) -> Dict[str, float]:
        """批量获取当前市场价格"""
        await self.connect()
        contracts = []
        for symbol in symbols:
            clean_symbol = symbol.replace('US.', '')
            contracts.append(Stock(clean_symbol, 'SMART', 'USD'))
            
        if not contracts:
            return {}
            
        await self.ib.qualifyContractsAsync(*contracts)
        tickers = await self.ib.reqTickersAsync(*contracts)
        
        prices = {}
        for ticker in tickers:
            prices[ticker.contract.symbol] = ticker.marketPrice()
            
        return prices

    async def has_today_orders(self, symbol: str) -> bool:
        """检查今天是否有针对该代码的非取消订单 (包括待成交和已成交)"""
        await self.connect()
        clean_symbol = symbol.replace('US.', '')
        
        # 获取当前所有的 trades (包括活动的和最近完成的)
        trades = self.ib.trades()
        
        for trade in trades:
            if trade.contract.symbol == clean_symbol:
                status = trade.orderStatus.status
                # 只要不是取消状态，都认为今天已经有操作了
                if status not in ('Cancelled', 'ApiCancelled', 'Inactive'):
                    logger.info(f"Found existing order for {clean_symbol} with status: {status}")
                    return True
        
        return False

    async def is_market_open(self, symbol: str = "SPY") -> bool:
        """
        通过查询 SPY 的 liquidHours (包含盘前盘后) 判断当前是否为交易时段。
        为避免服务器时区差异，统一基于 UTC 时间转换为交易所时区进行比较。
        """
        await self.connect()
        try:
            # 1. 获取合约详情
            contract = Stock(symbol, 'SMART', 'USD')
            details_list = await self.ib.reqContractDetailsAsync(contract)
            if not details_list:
                logger.warning(f"Could not get contract details for {symbol} to check market hours. Assuming Open.")
                return True
            
            details = details_list[0]
            
            # liquidHours Example: "20240101:CLOSED;20240102:0400-2000;..."
            liquid_hours_str = details.liquidHours
            time_zone_id = details.timeZoneId # e.g. "EST5EDT"
            
            import datetime
            import pytz
            
            # 映射 IB 时区 ID 到 pytz
            tz_map = {
                "EST5EDT": "US/Eastern",
                "CST6CDT": "US/Central",
                "PST8PDT": "US/Pacific",
                "HKT": "Asia/Hong_Kong",
                "GMT": "Europe/London"
            }
            tz_name = tz_map.get(time_zone_id, "US/Eastern")
            target_tz = pytz.timezone(tz_name)
            
            # 核心修改：使用 UTC 时间作为基准，然后转换到交易所时区
            # 这能确保无论服务器是 UTC、UTC+8 还是其他时区，只要系统 UTC 时间准确，结果的一致性。
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            now_exchange = now_utc.astimezone(target_tz)
            
            today_str = now_exchange.strftime("%Y%m%d")
            
            # 查找今天的规则
            today_rule = None
            for item in liquid_hours_str.split(';'):
                if item.startswith(today_str):
                    today_rule = item
                    break
            
            logger.info(f"Market Check for {symbol}: UTC={now_utc.strftime('%H:%M')}, Exchange({tz_name})={now_exchange.strftime('%H:%M')}, Rule='{today_rule}'")

            if not today_rule:
                logger.warning(f"No market hours found for today {today_str} (Exchange Time). Details: {liquid_hours_str}")
                return False
                
            if "CLOSED" in today_rule:
                return False
            
            # 解析规则, e.g. "20240101:0400-2000,2030-2200"
            if ':' in today_rule:
                time_ranges_str = today_rule.split(':')[1]
                now_hm = int(now_exchange.strftime("%H%M"))
                
                for segment in time_ranges_str.split(','):
                    if '-' in segment:
                        start_str, end_str = segment.split('-')
                        start_hm = int(start_str)
                        end_hm = int(end_str)
                        
                        if start_hm <= now_hm < end_hm:
                            return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking market hours: {e}")
            # 出错时默认开放，以免阻断
            return True
