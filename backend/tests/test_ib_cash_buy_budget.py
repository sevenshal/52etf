import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from src.core.services import trading_strategy
from src.core.services.ib_service import IBKRService
from src.robot import szdt_us_trader
from src.robot.szdt_us_trader import SZDTUSTrader

# 保证金账户满仓快照：数值取自线上 IB 账户 2026-09-16 实际读数
MARGIN_SNAPSHOT = dict(net_liquidation=67600.86, available_funds=14843.13, total_cash=606.92)


class _AccountIB:
    def __init__(self, *, net_liquidation, available_funds, total_cash, positions=(), connected=True):
        values = {"NetLiquidation": net_liquidation, "AvailableFunds": available_funds, "TotalCashValue": total_cash}
        self._values = [SimpleNamespace(tag=tag, value=str(value)) for tag, value in values.items() if value is not None]
        self._positions = list(positions)
        self._connected = connected

    def isConnected(self):
        return self._connected

    def accountValues(self):
        return self._values

    def positions(self):
        return self._positions

    def disconnect(self):
        self._connected = False


def _ib_service(positions=(), **snapshot):
    service = IBKRService(port=4004, client_id=1)
    service.ib = _AccountIB(positions=positions, **{**MARGIN_SNAPSHOT, **snapshot})
    return service


def _fully_invested_positions():
    return [SimpleNamespace(contract=SimpleNamespace(symbol="SOXL"), position=659.0, avgCost=159.65)]


class CashBuyBudgetTest(TestCase):
    def test_margin_account_budget_is_real_cash_not_available_funds(self):
        self.assertAlmostEqual(606.92 * 0.995, _ib_service().get_cash_buy_budget())

    def test_negative_cash_means_no_budget(self):
        self.assertEqual(0.0, _ib_service(total_cash=-3200.0).get_cash_buy_budget())

    def test_available_funds_below_cash_is_respected(self):
        self.assertAlmostEqual(500.0 * 0.995, _ib_service(available_funds=500.0).get_cash_buy_budget())

    def test_missing_total_cash_means_no_budget(self):
        self.assertEqual(0.0, _ib_service(total_cash=None).get_cash_buy_budget())


class SZDTUSTraderCashSizingTest(TestCase):
    def _run_buy_signal(self, service):
        trader = SZDTUSTrader()
        stock = {
            "code": "TQQQ.US",
            "name": "TQQQ",
            "type": 1,
            "when_buy": 20,
            "when_sell": 80,
            "max_position": 10,
            "buy_amount": 5000,
            "sell_amount": 5000,
            "buy_factor": 1,
            "sell_factor": 1,
            "lever": 3,
            "emo_area": 0,
        }
        emotion = {"status": 1, "data": {"score": 10, "price": 50.0}}
        placed_orders = []
        alerts = []

        async def fake_place_market_order(symbol, action, quantity):
            placed_orders.append((symbol, action, quantity))
            return SimpleNamespace(order=SimpleNamespace(orderId=7))

        service.place_market_order = fake_place_market_order

        @contextmanager
        def fake_db_ctx():
            yield SimpleNamespace(add=lambda _row: None)

        async def fake_connect(_self, _port, _client_id):
            return service

        with patch.object(szdt_us_trader, "get_db_ctx", fake_db_ctx), patch.object(
            szdt_us_trader, "send_alert_email", lambda *args, **_kwargs: alerts.append(args)
        ), patch.object(SZDTUSTrader, "_ensure_ib_connected", fake_connect), patch.object(
            SZDTUSTrader, "_get_next_stock", return_value=stock
        ), patch.object(
            trader.szdt, "get_fresh_emotion_from_list", AsyncMock(return_value=emotion)
        ):
            asyncio.run(trader.run_once(SimpleNamespace(id=1, account_id="acct"), 4004))

        self.assertEqual([], alerts)
        return placed_orders

    def test_fully_invested_margin_account_buys_only_with_real_cash(self):
        orders = self._run_buy_signal(_ib_service(positions=_fully_invested_positions()))

        # 旧逻辑按 AvailableFunds 14843 封顶，会买 109 股（约 5480，远超现金 606.92）
        self.assertEqual([("TQQQ.US", "BUY", 12)], orders)  # int(606.92 * 0.995 / 50)
        self.assertLessEqual(12 * 50.0, 606.92)

    def test_borrowed_margin_account_does_not_buy(self):
        orders = self._run_buy_signal(_ib_service(positions=_fully_invested_positions(), total_cash=-3200.0))

        self.assertEqual([], orders)

    def test_cash_account_sizing_unchanged(self):
        orders = self._run_buy_signal(_ib_service(net_liquidation=10000.0, available_funds=10000.0, total_cash=10000.0))

        self.assertEqual([("TQQQ.US", "BUY", 109)], orders)  # 5000 * 3 ** (10 / 120)


class TradingStrategyCashSizingTest(TestCase):
    def _run_buy_signal(self, service):
        config = SimpleNamespace(
            account_id="acct", etf_code="QQQ.US", short_window=5, long_window=20, ib_port=4004, target_ratio=100.0
        )
        placed_orders = []
        trade_logs = []

        async def fake_place_market_order(symbol, action, quantity):
            placed_orders.append((symbol, action, quantity))
            return SimpleNamespace(orderStatus=SimpleNamespace(status="Submitted"), isActive=lambda: True)

        service.place_market_order = fake_place_market_order
        service.has_today_orders = AsyncMock(return_value=False)
        service.get_market_price = AsyncMock(return_value=100.0)

        with patch.object(trading_strategy, "calculate_ma_signal", AsyncMock(return_value="BUY")), patch.object(
            trading_strategy, "_load_trading_config_snapshot", return_value=config
        ), patch.object(trading_strategy, "_append_trade_log", lambda **kwargs: trade_logs.append(kwargs)), patch.object(
            trading_strategy, "IBKRService", lambda port, client_id: service
        ):
            asyncio.run(trading_strategy.execute_trading_strategy("acct"))

        return placed_orders, trade_logs

    def test_fully_invested_margin_account_buys_only_with_real_cash(self):
        orders, logs = self._run_buy_signal(_ib_service(positions=_fully_invested_positions()))

        # 旧逻辑按 AvailableFunds 14843 封顶，会买 148 股（14800，远超现金 606.92）
        self.assertEqual([("QQQ.US", "BUY", 6)], orders)  # int(606.92 * 0.995 / 100)
        self.assertLessEqual(6 * 100.0, 606.92)
        self.assertEqual("BUY", logs[-1]["action"])

    def test_borrowed_margin_account_does_not_buy(self):
        orders, logs = self._run_buy_signal(_ib_service(positions=_fully_invested_positions(), total_cash=-3200.0))

        self.assertEqual([], orders)
        self.assertEqual("HOLD", logs[-1]["action"])
