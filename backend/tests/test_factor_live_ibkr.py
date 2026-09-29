from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.app.api import factor_lab
from src.core import database


class _QueryStub:
    def __init__(self, value):
        self.value = value

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.value


class _DbStub:
    def __init__(self, value):
        self.value = value

    def query(self, *args, **kwargs):
        return _QueryStub(self.value)


class _FakeIBKRService:
    instances = []
    pending_quantities = {}

    def __init__(self, host=None, port=None, client_id=None):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.orders = []
        self.disconnected = False
        self.__class__.instances.append(self)

    async def connect(self):
        return None

    def disconnect(self):
        self.disconnected = True

    def get_positions_dict(self):
        return {"AAPL": {"qty": 10, "price": 100, "avg_cost": 90}}

    def get_all_pending_qtys(self):
        return dict(self.pending_quantities)

    async def get_market_prices(self, symbols):
        return {symbol: 100 for symbol in symbols}

    def get_net_liquidation(self):
        return 1000

    def get_cash_buy_budget(self, buffer_pct=0.005):
        return 500

    async def place_market_order(self, symbol, action, quantity, submission_timeout=8):
        self.orders.append((symbol, action, quantity))
        return SimpleNamespace(orderStatus=SimpleNamespace(status="Submitted"))


def _request_payload(**updates):
    request = factor_lab.FactorBacktestRequest(
        pool="SPY_QQQ",
        max_positions=1,
        position_weights=[1.0],
        rotation_mode="scheduled_rebalance",
        lot_size=1,
    )
    payload = request.model_dump()
    payload.update(updates)
    return payload


class FactorLiveIBKRTest(IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeIBKRService.instances.clear()
        _FakeIBKRService.pending_quantities = {}
        self.ib_account = SimpleNamespace(
            id=7,
            account_id="acct",
            name="IBKR Paper",
            ib_host="127.0.0.1",
            ib_port=4002,
        )

    async def test_signal_plan_reads_ibkr_holdings_with_us_symbol_suffix(self):
        config = SimpleNamespace(
            id=11,
            account_id="acct",
            account_type="ib",
            ib_account_id=7,
            external_trading_account_id=None,
            live_sub_account_id=None,
            request_payload=_request_payload(),
        )
        captured = {}

        def build_plan(*args, **kwargs):
            captured["holding_symbols"] = kwargs["holding_symbols"]
            return {"signal_date": "2026-09-28"}

        with (
            patch.object(factor_lab, "IBKRService", _FakeIBKRService),
            patch.object(factor_lab, "shared_build_factor_signal_plan", side_effect=build_plan),
        ):
            plan = await factor_lab._build_factor_live_signal_plan(
                _DbStub(self.ib_account),
                SimpleNamespace(),
                config,
            )

        self.assertEqual(["AAPL.US"], captured["holding_symbols"])
        self.assertEqual("ib", plan["account_type"])
        self.assertEqual(7, plan["ib_account_id"])
        self.assertTrue(_FakeIBKRService.instances[0].disconnected)

    async def test_execute_ibkr_rebalance_sells_before_buying(self):
        _FakeIBKRService.pending_quantities = {"MSFT": 2}
        config = SimpleNamespace(
            id=11,
            account_id="acct",
            account_type="ib",
            ib_account_id=7,
            external_trading_account_id=None,
            live_sub_account_id=None,
            request_payload=_request_payload(),
            last_signal_payload={
                "signal_date": "2026-09-28",
                "should_rebalance": True,
                "sell_symbols": ["AAPL.US"],
                "target_symbols": ["MSFT.US"],
                "buy_symbols": ["MSFT.US"],
                "target_weights": {"MSFT.US": 1.0},
            },
        )

        with patch.object(factor_lab, "IBKRService", _FakeIBKRService):
            result = await factor_lab._execute_factor_live_signal(
                _DbStub(self.ib_account),
                SimpleNamespace(),
                config,
            )

        service = _FakeIBKRService.instances[0]
        self.assertEqual(
            [("AAPL.US", "SELL", 10), ("MSFT.US", "BUY", 8)],
            service.orders,
        )
        self.assertEqual("OK", result["executor_result"]["status"])
        self.assertEqual("ib", result["account_type"])
        self.assertTrue(service.disconnected)


class FactorLiveIBKRSchemaUpgradeTest(TestCase):
    def test_old_factor_live_table_gets_ibkr_columns_idempotently(self):
        engine = create_engine("sqlite:///:memory:")
        database.Base.metadata.create_all(engine)
        LocalSession = sessionmaker(bind=engine)
        with LocalSession() as session:
            session.add(database.FactorLiveTradingConfig(
                account_id="acct",
                name="old config",
                enabled=False,
                request_payload=factor_lab.jsonable_encoder(_request_payload()),
                account_type="external",
            ))
            session.commit()

        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE factor_live_trading_configs DROP COLUMN account_type")
            conn.exec_driver_sql("ALTER TABLE factor_live_trading_configs DROP COLUMN ib_account_id")

        with patch.object(database, "engine", engine):
            database.ensure_table_columns()
            database.ensure_table_columns()

        with engine.connect() as conn:
            columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(factor_live_trading_configs)"))
            }
        self.assertIn("account_type", columns)
        self.assertIn("ib_account_id", columns)

        with LocalSession() as session:
            config = session.query(database.FactorLiveTradingConfig).one()
            self.assertEqual("external", config.account_type)
            self.assertIsNone(config.ib_account_id)
