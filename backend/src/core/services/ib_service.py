from ib_insync import IB, Stock, MarketOrder
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
