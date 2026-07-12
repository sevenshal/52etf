import asyncio
from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.app.api.soxl_fear_strategy import (
    SoxlFearStrategyConfigPayload,
    SoxlFearStrategyStatePayload,
    get_soxl_fear_strategy_state_by_config,
    update_soxl_fear_strategy_state_by_config,
    _resolve_trading_account_id,
)
from src.core.database import (
    Base,
    SoxlFearStrategyConfig,
    SoxlFearStrategyLog,
    SoxlFearStrategyState,
)
from src.core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBase,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
)
from src.core.services.external_trading_ledger import STRATEGY_SOXL_FEAR
from src.robot.soxl_fear_strategy_trader import BrokerSnapshot, SoxlFearStrategyTrader


class SoxlFearExternalTradingTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def test_resolve_external_trading_account_requires_us_market(self):
        db = self._db_session()
        db.add(
            ExternalTradingAccount(
                id=1,
                account_id="acct",
                name="A Share",
                identifier="ptrade-a",
                market_type="A_STOCK",
                enabled=True,
            )
        )
        db.add(
            ExternalTradingSubAccount(
                id=2,
                account_id="acct",
                external_trading_account_id=1,
                name="SOXL",
                enabled=True,
            )
        )
        db.commit()

        payload = SoxlFearStrategyConfigPayload(
            account_type="external",
            external_trading_account_id=1,
            live_sub_account_id=2,
        )

        with self.assertRaises(HTTPException) as ctx:
            _resolve_trading_account_id(payload, "acct", db, db)
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("美股", ctx.exception.detail)

    def test_sync_external_target_order_writes_target_and_triggers_executor(self):
        db = self._db_session()
        db.add(
            ExternalTradingSubAccount(
                id=9,
                account_id="acct",
                external_trading_account_id=3,
                name="SOXL live",
                strategy_type=STRATEGY_SOXL_FEAR,
                strategy_config_id=7,
                cash_allocated=10000,
                cash_available=10000,
                enabled=True,
            )
        )
        db.commit()

        @contextmanager
        def db_ctx():
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

        async def fake_trigger(**kwargs):
            return {
                "status": "OK",
                "accounts": [
                    {"account_id": kwargs["external_account_id"], "external_order_count": 1},
                ],
            }

        trader = SoxlFearStrategyTrader()
        config = SimpleNamespace(
            id=7,
            account_id="acct",
            symbol="SOXL.US",
        )
        snapshot = BrokerSnapshot(
            shares=10,
            available_shares=10,
            avg_cost=20,
            current_price=25,
            available_cash=10000,
            portfolio_value=10250,
            has_today_order=False,
            order_service=None,
            external_trading_account_id=3,
            live_sub_account_id=9,
        )

        with patch("src.robot.soxl_fear_strategy_trader.get_external_trading_db_ctx", db_ctx), patch(
            "src.robot.soxl_fear_strategy_trader.trigger_external_trading_executor",
            fake_trigger,
        ):
            result = asyncio.run(
                trader._sync_external_target_order(
                    config,
                    snapshot,
                    "BUY",
                    4,
                    25,
                    "manual",
                )
            )

        target = db.query(ExternalTradingTargetPosition).one()
        self.assertEqual("SOXL.US", target.symbol)
        self.assertEqual(14, target.target_quantity)
        self.assertEqual(STRATEGY_SOXL_FEAR, target.strategy_type)
        self.assertEqual(7, target.strategy_config_id)
        self.assertIn("executor=OK", result)
        self.assertIn("orders=1", result)

    def test_run_config_once_places_order_outside_main_db_context(self):
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
                account_type="external",
                buy_threshold=40.0,
                greed_threshold=80.0,
                volume_ratio_threshold=1.0,
                buy_position_pct=50.0,
                cooldown_days=5,
                rebalance_threshold_pct=1.0,
            )
        )
        db.commit()
        db.close()

        main_db_context_depth = {"value": 0}
        order_context_depth = {"value": None}

        @contextmanager
        def main_db_ctx():
            session = session_factory()
            main_db_context_depth["value"] += 1
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                main_db_context_depth["value"] -= 1
                session.close()

        async def fake_snapshot(_self, _config, current_price):
            return BrokerSnapshot(
                shares=0,
                available_shares=0,
                avg_cost=0,
                current_price=current_price,
                available_cash=10000,
                portfolio_value=10000,
                has_today_order=False,
                order_service=None,
                external_trading_account_id=3,
                live_sub_account_id=9,
            )

        async def fake_place_order(_self, *_args, **_kwargs):
            order_context_depth["value"] = main_db_context_depth["value"]
            return "order-1"

        trader = SoxlFearStrategyTrader()
        config = SimpleNamespace(
            id=2,
            account_id="acct",
            enabled=True,
            symbol="SOXL.US",
            account_type="external",
            ib_account_id=None,
            longport_account_id=None,
            buy_threshold=40.0,
            greed_threshold=80.0,
            volume_ratio_threshold=1.0,
            buy_position_pct=50.0,
            cooldown_days=5,
            trailing_stop_pct=5.0,
            sell_position_pct=50.0,
            sell_reduction_basis="portfolio",
            max_take_profit_sells_per_cycle=2,
            min_position_pct_after_take_profit=10.0,
            rebalance_threshold_pct=1.0,
        )

        with patch("src.robot.soxl_fear_strategy_trader.get_db_ctx", main_db_ctx), patch.object(
            SoxlFearStrategyTrader,
            "_fetch_latest_cnn_score",
            return_value=(30.0, datetime(2026, 6, 9, 19, 53, 39)),
        ), patch.object(
            SoxlFearStrategyTrader,
            "_build_realtime_dataframe",
            return_value=(
                None,
                {
                    "current_price": 100.0,
                    "volume_ratio": 2.0,
                    "raw_volume_ratio": 1.9,
                    "quote_timestamp": datetime(2026, 6, 9, 15, 58),
                    "volume_projection_source": "test",
                },
            ),
        ), patch.object(
            SoxlFearStrategyTrader,
            "_build_broker_snapshot",
            fake_snapshot,
        ), patch.object(
            SoxlFearStrategyTrader,
            "_place_order",
            fake_place_order,
        ):
            asyncio.run(trader.run_config_once(config, trigger_source="auto"))

        self.assertEqual(0, order_context_depth["value"])
        db = session_factory()
        try:
            log = db.query(SoxlFearStrategyLog).one()
            state = db.query(SoxlFearStrategyState).one()
            persisted_config = db.query(SoxlFearStrategyConfig).one()
            self.assertEqual("BUY", log.action)
            self.assertEqual("SUCCESS", log.status)
            self.assertEqual(5, state.cooldown_remaining_days)
            self.assertEqual("SUCCESS", persisted_config.last_run_status)
        finally:
            db.close()

    def test_send_rebalance_notification_includes_timezone_context(self):
        trader = SoxlFearStrategyTrader()
        fixed_notification_time = datetime(2026, 7, 3, 3, 58, 34, 442533, tzinfo=ZoneInfo("Asia/Shanghai"))

        with patch(
            "src.robot.soxl_fear_strategy_trader.send_configured_email"
        ) as mock_send, patch.object(
            SoxlFearStrategyTrader,
            "_get_notification_time",
            return_value=fixed_notification_time,
        ):
            trader._send_rebalance_notification(
                config_id=2,
                masked_account_id="***ijkl",
                account_type="external",
                symbol="SOXL.US",
                trigger_source="auto",
                action="BUY",
                quantity=196,
                price=179.4050,
                position_ratio_before=56.52,
                position_ratio_after=99.79,
                cnn_score=30.86,
                cnn_timestamp=datetime(2026, 7, 2, 19, 52, 0),
                volume_ratio=1.4203,
                raw_volume_ratio=1.3978,
                market_snapshot={
                    "volume_projection_source": "fallback_close_curve",
                    "quote_timestamp": datetime(2026, 7, 2, 15, 57, 33, tzinfo=ZoneInfo("US/Eastern")),
                },
                trade_message="外部目标仓位已同步",
            )

        self.assertEqual(1, mock_send.call_count)
        _, _, body = mock_send.call_args[0]
        self.assertIn(
            "CNN 更新时间: 2026-07-02 19:52:00 (UTC, UTC+00:00) | 上海 2026-07-03 03:52:00 (Asia/Shanghai, UTC+08:00)",
            body,
        )
        self.assertIn(
            "行情时间: 2026-07-02 15:57:33 (US/Eastern, UTC-04:00) | 上海 2026-07-03 03:57:33 (Asia/Shanghai, UTC+08:00)",
            body,
        )
        self.assertIn(
            "通知时间: 2026-07-03 03:58:34 (Asia/Shanghai, UTC+08:00)",
            body,
        )

    def test_persist_run_result_retries_sqlite_lock(self):
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
                id=22,
                account_id="acct",
                enabled=True,
                symbol="SOXL.US",
                account_type="external",
                cooldown_days=5,
            )
        )
        db.commit()
        db.close()

        attempts = {"value": 0}

        @contextmanager
        def flaky_main_db_ctx():
            session = session_factory()
            attempts["value"] += 1
            try:
                yield session
                if attempts["value"] == 1:
                    session.rollback()
                    raise OperationalError("INSERT", {}, Exception("database is locked"))
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        trader = SoxlFearStrategyTrader()
        state_values = SimpleNamespace(
            last_processed_date=date(2026, 6, 10),
            cooldown_remaining_days=5,
            greed_peak_price=None,
            take_profit_cycle_sell_count=0,
        )
        with patch("src.robot.soxl_fear_strategy_trader.get_db_ctx", flaky_main_db_ctx), patch(
            "src.robot.soxl_fear_strategy_trader.time.sleep",
            lambda _seconds: None,
        ):
            trader._persist_run_result(
                config_id=22,
                account_id="acct",
                symbol="SOXL.US",
                trigger_source="auto",
                action="BUY",
                status="SUCCESS",
                message="filled",
                state_values=state_values,
                quantity=10,
            )

        self.assertEqual(2, attempts["value"])
        db = session_factory()
        try:
            state = db.query(SoxlFearStrategyState).one()
            log = db.query(SoxlFearStrategyLog).one()
            self.assertEqual(5, state.cooldown_remaining_days)
            self.assertEqual(date(2026, 6, 10), state.last_processed_date)
            self.assertEqual("BUY", log.action)
        finally:
            db.close()

    def test_soxl_state_api_reads_default_and_updates_state(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(
            engine,
            tables=[
                SoxlFearStrategyConfig.__table__,
                SoxlFearStrategyState.__table__,
            ],
        )
        session_factory = sessionmaker(bind=engine)
        db = session_factory()
        db.add(
            SoxlFearStrategyConfig(
                id=31,
                account_id="acct",
                enabled=True,
                symbol="SOXL.US",
                account_type="external",
            )
        )
        db.commit()

        default_state = get_soxl_fear_strategy_state_by_config(31, account_id="acct", db=db)
        self.assertFalse(default_state.has_state)
        self.assertEqual(0, default_state.cooldown_remaining_days)

        updated_state = update_soxl_fear_strategy_state_by_config(
            31,
            SoxlFearStrategyStatePayload(
                last_processed_date=date(2026, 6, 10),
                cooldown_remaining_days=7,
                greed_peak_price=210.5,
                take_profit_cycle_sell_count=1,
            ),
            account_id="acct",
            db=db,
        )
        self.assertTrue(updated_state.has_state)
        self.assertEqual(7, updated_state.cooldown_remaining_days)
        self.assertEqual(210.5, updated_state.greed_peak_price)

        row = db.query(SoxlFearStrategyState).one()
        self.assertEqual(date(2026, 6, 10), row.last_processed_date)
        self.assertEqual(1, row.take_profit_cycle_sell_count)
        db.close()

    def test_run_config_once_trailing_take_profit_uses_intraday_high_as_peak(self):
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
                id=3,
                account_id="acct",
                enabled=True,
                symbol="SOXL.US",
                account_type="external",
                buy_threshold=40.0,
                greed_threshold=80.0,
                volume_ratio_threshold=1.0,
                buy_position_pct=50.0,
                cooldown_days=5,
                rebalance_threshold_pct=1.0,
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

        async def fake_snapshot(_self, _config, current_price):
            return BrokerSnapshot(
                shares=100,
                available_shares=100,
                avg_cost=80,
                current_price=current_price,
                available_cash=0,
                portfolio_value=10000,
                has_today_order=False,
                order_service=None,
                external_trading_account_id=3,
                live_sub_account_id=9,
            )

        placed_orders = []

        async def fake_place_order(_self, _config, _snapshot, action, quantity, price, **_kwargs):
            placed_orders.append((action, quantity, price))
            return "order-2"

        trader = SoxlFearStrategyTrader()
        config = SimpleNamespace(
            id=3,
            account_id="acct",
            enabled=True,
            symbol="SOXL.US",
            account_type="external",
            ib_account_id=None,
            longport_account_id=None,
            buy_threshold=40.0,
            greed_threshold=80.0,
            volume_ratio_threshold=1.0,
            buy_position_pct=50.0,
            cooldown_days=5,
            trailing_stop_pct=5.0,
            sell_position_pct=50.0,
            sell_reduction_basis="portfolio",
            max_take_profit_sells_per_cycle=2,
            min_position_pct_after_take_profit=10.0,
            rebalance_threshold_pct=1.0,
        )

        with patch("src.robot.soxl_fear_strategy_trader.get_db_ctx", main_db_ctx), patch.object(
            SoxlFearStrategyTrader,
            "_fetch_latest_cnn_score",
            return_value=(90.0, datetime(2026, 6, 9, 19, 53, 39)),
        ), patch.object(
            SoxlFearStrategyTrader,
            "_build_realtime_dataframe",
            return_value=(
                None,
                {
                    "current_price": 100.0,
                    "current_high": 106.0,
                    "volume_ratio": 0.5,
                    "raw_volume_ratio": 0.5,
                    "quote_timestamp": datetime(2026, 6, 9, 15, 58),
                    "volume_projection_source": "test",
                },
            ),
        ), patch.object(
            SoxlFearStrategyTrader,
            "_build_broker_snapshot",
            fake_snapshot,
        ), patch.object(
            SoxlFearStrategyTrader,
            "_place_order",
            fake_place_order,
        ):
            asyncio.run(trader.run_config_once(config, trigger_source="auto"))

        self.assertEqual([("SELL", 50, 100.0)], placed_orders)
        db = session_factory()
        try:
            log = db.query(SoxlFearStrategyLog).one()
            state = db.query(SoxlFearStrategyState).one()
            self.assertEqual("SELL", log.action)
            self.assertEqual("SUCCESS", log.status)
            self.assertIn("回撤 5.66%", log.message)
            self.assertEqual(1, state.take_profit_cycle_sell_count)
            self.assertEqual(106.0, state.greed_peak_price)
        finally:
            db.close()
