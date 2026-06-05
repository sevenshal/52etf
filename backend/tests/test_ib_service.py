from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from src.core.services.ib_service import IBKRService


class _FakeTrade:
    def __init__(self, contract, action="BUY", quantity=1, status="Submitted"):
        self.contract = contract
        self.order = SimpleNamespace(action=action, totalQuantity=quantity)
        self.orderStatus = SimpleNamespace(status=status, filled=0)


class _FakeIB:
    def __init__(self):
        self.qualified_symbols = []
        self.place_order_calls = []
        self.positions_data = []
        self.trades_data = []

    async def qualifyContractsAsync(self, contract):
        self.qualified_symbols.append(contract.symbol)
        if contract.symbol == "BRK B":
            contract.conId = 12345
            return [contract]
        if contract.symbol == "AAPL":
            contract.conId = 1
            return [contract]
        return []

    def placeOrder(self, contract, order):
        self.place_order_calls.append((contract, order))
        return _FakeTrade(contract, action=order.action, quantity=order.totalQuantity, status="Submitted")

    def positions(self):
        return list(self.positions_data)

    def trades(self):
        return list(self.trades_data)


class IBKRServiceAsyncTest(IsolatedAsyncioTestCase):
    async def test_place_market_order_retries_class_share_symbol_with_ib_format(self):
        service = IBKRService(port=4001)
        service.ib = _FakeIB()
        service.connect = AsyncMock()

        trade = await service.place_market_order("BRK.B.US", "BUY", 10)

        self.assertEqual(["BRK.B", "BRK B"], service.ib.qualified_symbols)
        self.assertEqual("BRK B", trade.contract.symbol)
        self.assertEqual(1, len(service.ib.place_order_calls))

    async def test_place_market_order_raises_when_contract_cannot_be_qualified(self):
        service = IBKRService(port=4001)
        service.ib = _FakeIB()
        service.connect = AsyncMock()

        with self.assertRaisesRegex(ValueError, "Unknown IB contract for ZZZZ.Z"):
            await service.place_market_order("ZZZZ.Z.US", "BUY", 10)

        self.assertEqual(0, len(service.ib.place_order_calls))

    async def test_place_market_order_raises_when_ib_cancels_immediately(self):
        service = IBKRService(port=4001)
        service.ib = _FakeIB()
        service.connect = AsyncMock()
        service.ib.placeOrder = lambda contract, order: _FakeTrade(
            contract,
            action=order.action,
            quantity=order.totalQuantity,
            status="Cancelled",
        )

        with self.assertRaisesRegex(RuntimeError, "rejected by IBKR"):
            await service.place_market_order("AAPL.US", "BUY", 1)


class IBKRServiceSyncTest(TestCase):
    def test_position_and_pending_maps_normalize_ib_class_share_symbols(self):
        service = IBKRService(port=4001)
        service.ib = SimpleNamespace(
            isConnected=lambda: True,
            positions=lambda: [
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="BRK B"),
                    position=5,
                    marketPrice=450.0,
                    avgCost=400.0,
                )
            ],
            trades=lambda: [
                _FakeTrade(SimpleNamespace(symbol="BRK B"), quantity=2, status="Submitted")
            ],
        )

        self.assertEqual(
            {"BRK.B": {"qty": 5.0, "price": 450.0, "avg_cost": 400.0}},
            service.get_positions_dict(),
        )
        self.assertEqual(5.0, service.get_position("BRK.B.US")["qty"])
        self.assertEqual(2, service.get_pending_qty("BRK.B.US"))
