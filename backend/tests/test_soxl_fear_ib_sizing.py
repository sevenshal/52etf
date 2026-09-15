import asyncio
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import (
    Base,
    SoxlFearStrategyConfig,
    SoxlFearStrategyLog,
    SoxlFearStrategyState,
)
from src.robot.soxl_fear_strategy_trader import BrokerSnapshot, SoxlFearStrategyTrader


class FakeIBService:
    """保证金账户快照：数值取自线上 SOXL IB 账户 2026-09-16 实际读数。"""

    def __init__(self, shares=659, net_liquidation=67600.86, available_funds=14843.13, total_cash=606.92):
        self.shares = shares
        self.net_liquidation = net_liquidation
        self.available_funds = available_funds
        self.total_cash = total_cash

    def get_position(self, _symbol):
        return {"qty": self.shares, "price": None, "avg_cost": 159.65}

    def get_net_liquidation(self):
        return self.net_liquidation

    def get_available_cash(self):
        return self.available_funds

    def get_total_cash_value(self):
        return self.total_cash

    async def has_today_orders(self, _symbol):
        return False


def _ib_config_db_ctx():
    ib_config = SimpleNamespace(ib_port=4004)
    query = SimpleNamespace(filter=lambda *_args: SimpleNamespace(first=lambda: ib_config))

    @contextmanager
    def ctx():
        yield SimpleNamespace(query=lambda *_args: query)

    return ctx


class SoxlFearIBSnapshotTest(TestCase):
    def _build(self, service, current_price=101.66):
        trader = SoxlFearStrategyTrader()
        config = SimpleNamespace(id=2, account_id="acct", ib_account_id=6, symbol="SOXL.US")

        async def fake_connect(_self, _port, _client_id):
            return service

        with patch("src.robot.soxl_fear_strategy_trader.get_db_ctx", _ib_config_db_ctx()), patch.object(
            SoxlFearStrategyTrader, "_ensure_ib_connected", fake_connect
        ):
            return asyncio.run(trader._build_ib_snapshot(config, current_price))

    def test_margin_headroom_does_not_inflate_portfolio_or_cash(self):
        snapshot = self._build(FakeIBService())

        # 旧逻辑是 max(净值, AvailableFunds + 持仓市值) = 81837，可用资金 14843，下一次买入会融资
        self.assertAlmostEqual(67600.86, snapshot.portfolio_value)
        self.assertAlmostEqual(606.92, snapshot.available_cash)

    def test_negative_cash_means_nothing_available(self):
        snapshot = self._build(FakeIBService(total_cash=-3200.0))

        self.assertEqual(0.0, snapshot.available_cash)

    def test_available_funds_below_cash_is_respected(self):
        snapshot = self._build(FakeIBService(available_funds=500.0, total_cash=606.92))

        self.assertAlmostEqual(500.0, snapshot.available_cash)

    def test_missing_total_cash_falls_back_to_nav_minus_position(self):
        snapshot = self._build(FakeIBService(total_cash=None), current_price=100.0)

        self.assertAlmostEqual(67600.86 - 65900.0, snapshot.available_cash)


class SoxlFearIBBuySizingTest(TestCase):
    def _run_buy_signal(self, snapshot_kwargs, current_price):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            engine,
            tables=[
                SoxlFearStrategyConfig.__table__,
                SoxlFearStrategyState.__table__,
                SoxlFearStrategyLog.__table__,
            ],
        )
        session_factory = sessionmaker(bind=engine)
        db = session_factory()
        db.add(
            SoxlFearStrategyConfig(
                id=2,
                account_id="acct",
                enabled=True,
                symbol="SOXL.US",
                account_type="ib",
                buy_threshold=40.0,
                greed_threshold=41.0,
                volume_ratio_threshold=1.37,
                buy_position_pct=50.0,
                cooldown_days=10,
                rebalance_threshold_pct=5.0,
            )
        )
        db.commit()
        db.close()

        @contextmanager
        def main_db_ctx():
            session = session_factory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        async def fake_snapshot(_self, _config, price):
            return BrokerSnapshot(current_price=price, has_today_order=False, order_service=None, **snapshot_kwargs)

        placed_orders = []

        async def fake_place_order(_self, _config, _snapshot, action, quantity, price, **_kwargs):
            placed_orders.append((action, quantity, price))
            return "4"

        config = SimpleNamespace(
            id=2,
            account_id="acct",
            enabled=True,
            symbol="SOXL.US",
            account_type="ib",
            ib_account_id=6,
            longport_account_id=None,
            buy_threshold=40.0,
            greed_threshold=41.0,
            volume_ratio_threshold=1.37,
            buy_position_pct=50.0,
            cooldown_days=10,
            trailing_stop_pct=7.0,
            sell_position_pct=50.0,
            sell_reduction_basis="portfolio",
            max_take_profit_sells_per_cycle=2,
            min_position_pct_after_take_profit=5.0,
            rebalance_threshold_pct=5.0,
        )

        with patch("src.robot.soxl_fear_strategy_trader.get_db_ctx", main_db_ctx), patch.object(
            SoxlFearStrategyTrader,
            "_fetch_latest_cnn_score",
            return_value=(31.09, datetime(2026, 9, 14, 19, 53, 28)),
        ), patch.object(
            SoxlFearStrategyTrader,
            "_build_realtime_dataframe",
            return_value=(
                None,
                {
                    "current_price": current_price,
                    "volume_ratio": 1.77,
                    "raw_volume_ratio": 1.74,
                    "quote_timestamp": datetime(2026, 9, 14, 15, 57),
                    "volume_projection_source": "test",
                },
            ),
        ), patch.object(SoxlFearStrategyTrader, "_build_broker_snapshot", fake_snapshot), patch.object(
            SoxlFearStrategyTrader, "_place_order", fake_place_order
        ):
            asyncio.run(SoxlFearStrategyTrader().run_config_once(config, trigger_source="auto"))

        db = session_factory()
        try:
            return placed_orders, db.query(SoxlFearStrategyLog).one().message
        finally:
            db.close()

    def test_buy_is_capped_by_cash_with_buffer(self):
        # 买入前：452 股，现金约 21604，目标 50% 净值 ≈ 33725，只能按现金买
        orders, message = self._run_buy_signal(
            dict(shares=452, available_shares=452, avg_cost=179.0, available_cash=21604.0, portfolio_value=67450.0),
            current_price=101.43,
        )

        self.assertEqual([("BUY", 211, 101.43)], orders)  # floor(21604 * 0.995 / 101.43)
        self.assertLessEqual(211 * 101.43, 21604.0)
        self.assertIn("不融资", message)

    def test_fully_invested_account_does_not_buy_on_margin(self):
        orders, message = self._run_buy_signal(
            dict(shares=659, available_shares=659, avg_cost=159.65, available_cash=606.92, portfolio_value=67600.86),
            current_price=101.66,
        )

        self.assertEqual([], orders)
        self.assertIn("可买数量过小", message)
