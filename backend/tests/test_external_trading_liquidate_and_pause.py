from contextlib import contextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBase,
    ExternalTradingLedgerPosition,
    ExternalTradingOrder,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
)
from src.core.services.external_trading_executor import liquidate_and_pause_sub_account


class LiquidateAndPauseSubAccountTest(IsolatedAsyncioTestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        with self.Session() as db:
            db.add(ExternalTradingAccount(
                id=1,
                account_id="acct",
                name="Broker",
                identifier="broker",
                market_type="A_STOCK",
                enabled=True,
            ))
            db.add(ExternalTradingSubAccount(
                id=11,
                account_id="acct",
                external_trading_account_id=1,
                name="Strategy",
                enabled=True,
            ))
            db.add(ExternalTradingLedgerPosition(
                account_id="acct",
                external_trading_account_id=1,
                sub_account_id=11,
                symbol="510300.SH",
                quantity=100,
                available_quantity=100,
            ))
            db.add(ExternalTradingTargetPosition(
                account_id="acct",
                external_trading_account_id=1,
                sub_account_id=11,
                symbol="510300.SH",
                target_quantity=100,
                status="ACTIVE",
            ))
            db.commit()

    @contextmanager
    def _db_ctx(self):
        db = self.Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _add_active_order(self, status="SUBMITTED", broker_order_id="B-1"):
        with self.Session() as db:
            db.add(ExternalTradingOrder(
                account_id="acct",
                external_trading_account_id=1,
                sub_account_id=11,
                allocation_role="DIRECT",
                client_order_id="old-order",
                broker_order_id=broker_order_id,
                symbol="510300.SH",
                side="BUY",
                quantity=100,
                remaining_quantity=100,
                status=status,
            ))
            db.commit()

    async def test_cancels_active_order_before_liquidation_and_pauses_immediately(self):
        self._add_active_order()
        response = {"orders": [{
            "ok": True,
            "client_order_id": "old-order",
            "order_id": "B-1",
            "status": "CANCEL_REQUESTED",
        }]}
        with patch(
            "src.core.services.external_trading_executor.get_external_trading_db_ctx",
            self._db_ctx,
        ), patch(
            "src.core.services.external_trading_executor.external_trading_hub.get_status",
            return_value={"connected": True},
        ), patch(
            "src.core.services.external_trading_executor.external_trading_hub.cancel_orders",
            new=AsyncMock(return_value=response),
        ) as cancel_orders:
            result = await liquidate_and_pause_sub_account(
                account_id="acct", external_account_id=1, sub_account_id=11,
                schedule_continuation=False,
            )

        self.assertEqual("CANCEL_REQUESTED", result["status"])
        cancel_orders.assert_awaited_once()
        with self.Session() as db:
            sub_account = db.get(ExternalTradingSubAccount, 11)
            target = db.query(ExternalTradingTargetPosition).filter_by(sub_account_id=11).one()
            order = db.query(ExternalTradingOrder).filter_by(client_order_id="old-order").one()
            self.assertFalse(sub_account.enabled)
            self.assertEqual(0, target.target_quantity)
            self.assertEqual("CANCEL_PENDING", order.status)

    async def test_waits_for_cancel_confirmation_without_submitting_new_order(self):
        self._add_active_order(status="CANCEL_PENDING")
        with patch(
            "src.core.services.external_trading_executor.get_external_trading_db_ctx",
            self._db_ctx,
        ), patch(
            "src.core.services.external_trading_executor.external_trading_hub.cancel_orders",
            new=AsyncMock(),
        ) as cancel_orders:
            result = await liquidate_and_pause_sub_account(
                account_id="acct", external_account_id=1, sub_account_id=11,
                schedule_continuation=False,
            )

        self.assertEqual("WAITING_CANCEL", result["status"])
        cancel_orders.assert_not_awaited()
