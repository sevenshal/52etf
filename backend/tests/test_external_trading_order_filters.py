from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.app.api.external_trading_accounts import _apply_order_lifecycle_filters
from src.core.external_trading_database import ExternalTradingBase, ExternalTradingOrder
from src.core.services.external_trading_ledger import (
    ACTIVE_ORDER_STATUSES,
    STATUS_BLOCKED_INSUFFICIENT_POSITION,
)


class ExternalTradingOrderFilterTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _add_order(self, db, index, status, filled_quantity=0):
        db.add(
            ExternalTradingOrder(
                account_id="acct",
                external_trading_account_id=2,
                allocation_role="CHILD",
                client_order_id=f"order-{index}",
                symbol="510500.SH",
                side="BUY",
                quantity=100,
                filled_quantity=filled_quantity,
                remaining_quantity=max(100 - filled_quantity, 0),
                status=status,
            )
        )

    def test_active_order_filter_matches_executor_summary_statuses(self):
        db = self._db_session()
        terminal_statuses = [
            "FILLED",
            "CANCELED",
            "PARTIALLY_CANCELED",
            "REJECTED",
            "FAILED",
            "EXPIRED",
            STATUS_BLOCKED_INSUFFICIENT_POSITION,
        ]
        for index, status in enumerate(sorted(ACTIVE_ORDER_STATUSES) + terminal_statuses):
            self._add_order(db, index, status, filled_quantity=100 if status == "FILLED" else 0)
        db.commit()

        rows = _apply_order_lifecycle_filters(
            db.query(ExternalTradingOrder).order_by(ExternalTradingOrder.status.asc()),
            active_only=True,
        ).all()

        self.assertEqual(sorted(ACTIVE_ORDER_STATUSES), [row.status for row in rows])

    def test_unfilled_filter_is_broader_than_active_orders(self):
        db = self._db_session()
        self._add_order(db, 1, "SUBMITTED")
        self._add_order(db, 2, "CANCELED")
        self._add_order(db, 3, "FILLED", filled_quantity=100)
        db.commit()

        rows = _apply_order_lifecycle_filters(
            db.query(ExternalTradingOrder).order_by(ExternalTradingOrder.client_order_id.asc()),
            unfilled_only=True,
        ).all()

        self.assertEqual(["SUBMITTED", "CANCELED"], [row.status for row in rows])
