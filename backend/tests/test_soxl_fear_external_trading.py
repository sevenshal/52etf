import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.app.api.soxl_fear_strategy import (
    SoxlFearStrategyConfigPayload,
    _resolve_trading_account_id,
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
