from ib_insync import IB, Stock, MarketOrder, LimitOrder
import asyncio
import math
import logging
import os
import re
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

class IBKRService:
    def __init__(self, host=None, port=None, client_id=None):
        self.host = host or os.getenv('IB_HOST', '127.0.0.1')
        self.port = int(port or os.getenv('IB_PORT', '4001'))
        self.client_id = int(client_id or os.getenv('IB_CLIENT_ID', '1'))
        self.ib = None

    async def connect(self, timeout: float = 15.0):
        if self.ib is not None and self.ib.isConnected():
            return  # 已连接，直接返回

        # 清理旧的损坏实例：旧的 IB() 对象内部可能持有一个已取消的 apiStart future，
        # 若不先 disconnect + 重建，再次 connectAsync 会立即被已取消的 future 触发 CancelledError/TimeoutError。
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:
                pass
            self.ib = None

        self.ib = IB()
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=timeout)
            logger.info(f"Connected to IB Gateway on {self.host}:{self.port}")
            # 3 表示请求延迟行情 (Delayed)，当没有实时行情订阅时很有用
            self.ib.reqMarketDataType(3)
        except Exception as e:
            logger.error(f"Failed to connect to IB Gateway: {e}")
            # 连接失败时也清理，避免下次复用损坏的实例
            try:
                self.ib.disconnect()
            except Exception:
                pass
            self.ib = None
            raise

    def disconnect(self):
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway")

    @staticmethod
    def _normalize_ib_equity_symbol(symbol: str) -> str:
        text = str(symbol or "").strip().upper().replace("/", ".")
        if text.startswith("US."):
            text = text[3:]
        if text.endswith(".US"):
            text = text[:-3]
        text = " ".join(text.split())
        if re.fullmatch(r"[A-Z0-9]+ [A-Z0-9]+", text):
            text = text.replace(" ", ".")
        return text

    @classmethod
    def _build_stock_symbol_candidates(cls, symbol: str) -> List[str]:
        normalized = cls._normalize_ib_equity_symbol(symbol)
        candidates = [normalized]
        if "." in normalized:
            candidates.append(normalized.replace(".", " "))
        unique = []
        for item in candidates:
            if item and item not in unique:
                unique.append(item)
        return unique

    async def _qualify_stock_contract(self, symbol: str, timeout: float = 15.0):
        normalized = self._normalize_ib_equity_symbol(symbol)
        last_error = None
        for candidate in self._build_stock_symbol_candidates(symbol):
            contract = Stock(candidate, 'SMART', 'USD')
            logger.debug(f"[{self.port}] Qualifying contract for {candidate} (requested {normalized})...")
            try:
                qualified = await asyncio.wait_for(self.ib.qualifyContractsAsync(contract), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error(f"[{self.port}] Contract qualification timed out for {candidate}")
                raise Exception(f"Contract qualification timed out for {normalized}")
            except Exception as exc:
                last_error = exc
                logger.error(f"[{self.port}] Contract qualification failed for {candidate}: {exc}")
                continue

            if qualified and getattr(contract, "conId", 0):
                if candidate != normalized:
                    logger.info(f"[{self.port}] Resolved {normalized} to IB contract symbol {candidate}")
                return contract

            logger.warning(f"[{self.port}] Contract candidate not found for {candidate}")

        if last_error is not None:
            raise last_error
        raise ValueError(f"Unknown IB contract for {normalized}")

    async def _await_order_submission(self, trade, symbol: str, timeout: float = 2.0):
        accepted_statuses = {"SUBMITTED", "PRESUBMITTED", "FILLED"}
        rejected_statuses = {"CANCELLED", "APICANCELLED", "INACTIVE"}
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            status = str(getattr(trade.orderStatus, "status", "") or "").upper()
            if status in accepted_statuses:
                return trade
            if status in rejected_statuses:
                raise RuntimeError(f"Order for {symbol} was rejected by IBKR with status {status}")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for IBKR order submission acknowledgement for {symbol}")
            await asyncio.sleep(0.1)

    def get_net_liquidation(self) -> float:
        """从 IB 账户实时同步的数据中获取净资产"""
        if not self.ib or not self.ib.isConnected(): return 0.0
        for v in self.ib.accountValues():
            if v.tag == 'NetLiquidation':
                return float(v.value or 0)
        return 0.0

    def get_available_cash(self) -> float:
        """获取可用资金"""
        if not self.ib or not self.ib.isConnected(): return 0.0
        for v in self.ib.accountValues():
            if v.tag == 'AvailableFunds':
                return float(v.value or 0)
        return 0.0

    def get_positions_dict(self) -> Dict[str, dict]:
        """获取当前最实时的持仓字典 (Symbol -> {qty, price})"""
        if not self.ib or not self.ib.isConnected(): 
            return {}
        pos_map = {}
        for p in self.ib.positions():
            symbol = self._normalize_ib_equity_symbol(p.contract.symbol)
            qty = float(p.position)
            price = float(p.marketPrice) if hasattr(p, 'marketPrice') and p.marketPrice and not math.isnan(p.marketPrice) else None
            avg_cost = float(p.avgCost) if hasattr(p, 'avgCost') and p.avgCost and not math.isnan(p.avgCost) else None
            
            if symbol in pos_map:
                pos_map[symbol]['qty'] += qty
                if pos_map[symbol].get('avg_cost') is None and avg_cost is not None:
                    pos_map[symbol]['avg_cost'] = avg_cost
            else:
                pos_map[symbol] = {'qty': qty, 'price': price, 'avg_cost': avg_cost}
        return pos_map

    def get_position(self, symbol: str) -> dict:
        """获取指定代码的实时持仓数据 {qty, price, avg_cost}"""
        pos_data = self.get_positions_dict().get(self._normalize_ib_equity_symbol(symbol))
        return pos_data if pos_data else {'qty': 0, 'price': None, 'avg_cost': None}

    def get_all_pending_qtys(self) -> Dict[str, float]:
        """批量获取所有代码的待成交订单数量字典"""
        if not self.ib or not self.ib.isConnected():
            return {}
        
        pending_map = {}
        for trade in self.ib.trades():
            symbol = self._normalize_ib_equity_symbol(trade.contract.symbol)
            status = trade.orderStatus.status
            if status in ('Submitted', 'PreSubmitted', 'PendingSubmit', 'PendingCancel'):
                qty = trade.order.totalQuantity
                if trade.order.action == 'SELL':
                    qty = -qty
                
                filled = trade.orderStatus.filled
                remaining = qty + filled if trade.order.action == 'SELL' else qty - filled
                
                pending_map[symbol] = pending_map.get(symbol, 0) + remaining
        return pending_map

    def get_pending_qty(self, symbol: str) -> float:
        """获取单个代码的待成交数量 (内部调用批量方法以保证逻辑统一)"""
        return self.get_all_pending_qtys().get(self._normalize_ib_equity_symbol(symbol), 0)

    def get_effective_position(self, symbol: str) -> float:
        """获取有效持仓 (当前持仓 + 待成交数量)"""
        pos_data = self.get_position(symbol)
        return pos_data['qty'] + self.get_pending_qty(symbol)

    async def place_market_order(self, symbol: str, action: str, quantity: int):
        """下市价单"""
        await self.connect()
        clean_symbol = self._normalize_ib_equity_symbol(symbol)
        contract = await self._qualify_stock_contract(symbol, timeout=15.0)
        logger.info(f"[{self.port}] Placing {action} market order for {quantity} {clean_symbol}")
        order = MarketOrder(action, quantity)
        trade = self.ib.placeOrder(contract, order)
        return await self._await_order_submission(trade, clean_symbol)

    async def place_limit_order(self, symbol: str, action: str, quantity: int, price: float, outside_rth: bool = False):
        """下限价单 (支持盘前盘后)"""
        await self.connect()
        clean_symbol = self._normalize_ib_equity_symbol(symbol)
        contract = await self._qualify_stock_contract(symbol, timeout=10.0)
        
        order = LimitOrder(action, quantity, price)
        if outside_rth:
            order.outsideRth = True
            
        trade = self.ib.placeOrder(contract, order)
        
        logger.info(f"[{self.port}] Placing {action} Limit order for {quantity} {clean_symbol} at ${price:.2f} (OutsideRth={outside_rth})")
        
        # 对于限价单，我们只等待提交成功，不一定要等待完全成交 (因为可能挂单)
        await asyncio.sleep(2)
        
        return trade

    async def get_market_price(self, symbol: str):
        """获取当前市场价格"""
        await self.connect()
        contract = await self._qualify_stock_contract(symbol, timeout=10.0)
        
        [ticker] = await self.ib.reqTickersAsync(contract)
        return ticker.marketPrice()

    async def get_market_prices(self, symbols: List[str]) -> Dict[str, float]:
        """批量获取当前市场价格"""
        await self.connect()
        normalized_symbols = []
        contracts = []
        for symbol in symbols:
            contracts.append(await self._qualify_stock_contract(symbol, timeout=10.0))
            normalized_symbols.append(self._normalize_ib_equity_symbol(symbol))
            
        if not contracts:
            return {}
            
        tickers = await self.ib.reqTickersAsync(*contracts)
        
        prices = {}
        for normalized_symbol, ticker in zip(normalized_symbols, tickers):
            prices[normalized_symbol] = ticker.marketPrice()
            
        return prices

    async def has_today_orders(self, symbol: str) -> bool:
        """检查今天是否有针对该代码的非取消订单 (包括待成交和已成交)"""
        await self.connect()
        clean_symbol = self._normalize_ib_equity_symbol(symbol)
        
        # 获取当前所有的 trades (包括活动的和最近完成的)
        trades = self.ib.trades()
        
        for trade in trades:
            if self._normalize_ib_equity_symbol(trade.contract.symbol) == clean_symbol:
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
