from datetime import datetime
from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.external_trading_database import ExternalTradingBase, ExternalTradingOrder
from src.core.services import external_trading_executor as executor
from src.core.services.external_trading_ledger import record_submission_result
from src.core.services.external_trading_market import external_trading_market_close_time


class ExternalTradingProtectionDeadlineTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _add_parent_child_orders(self, db):
        original_deadline = datetime(2026, 6, 5, 10, 43, 10)
        parent = ExternalTradingOrder(
            account_id="acct",
            external_trading_account_id=2,
            allocation_role="PARENT",
            client_order_id="parent-client-id",
            symbol="301526.SZ",
            side="BUY",
            order_type="LIMIT",
            quantity=300,
            filled_quantity=0,
            remaining_quantity=300,
            status="CREATED",
            deadline_at=original_deadline,
        )
        db.add(parent)
        db.flush()
        child = ExternalTradingOrder(
            account_id="acct",
            external_trading_account_id=2,
            parent_order_id=parent.id,
            sub_account_id=19,
            allocation_role="CHILD",
            client_order_id="child-client-id",
            symbol="301526.SZ",
            side="BUY",
            order_type="LIMIT",
            quantity=300,
            filled_quantity=0,
            remaining_quantity=300,
            status="CREATED",
            deadline_at=original_deadline,
        )
        db.add(child)
        db.flush()
        return parent, child, original_deadline

    def test_market_close_time_uses_market_calendar(self):
        self.assertEqual(
            "2026-06-05T15:00:00+08:00",
            external_trading_market_close_time("A_STOCK", datetime(2026, 6, 5, 10, 0, 0)).isoformat(),
        )
        self.assertEqual(
            "2026-06-05T16:00:00-04:00",
            external_trading_market_close_time("US_STOCK", datetime(2026, 6, 5, 10, 0, 0)).isoformat(),
        )

    def test_reference_protection_price_uses_buy_tick_floor(self):
        order = {
            "symbol": "301526.SZ",
            "price_level": 5,
            "execution_policy": {"max_slippage_pct": 2.0},
            "allocations": [{"quantity": 300, "reference_price": 22.61}],
        }

        self.assertEqual(23.06, executor._reference_protection_limit_price(order, "BUY", "A_STOCK"))

    def test_reference_protection_price_uses_sell_tick_ceiling(self):
        order = {
            "symbol": "301526.SZ",
            "price_level": 5,
            "execution_policy": {"max_slippage_pct": 0.5},
            "allocations": [{"quantity": 300, "reference_price": 22.61}],
        }

        self.assertEqual(22.5, executor._reference_protection_limit_price(order, "SELL", "A_STOCK"))

    def test_protection_limited_limit_submission_sets_deadline_to_close(self):
        db = self._db_session()
        parent, child, _ = self._add_parent_child_orders(db)
        close_deadline = datetime(2026, 6, 5, 15, 0, 0)

        record_submission_result(
            db,
            external_trading_account_id=2,
            response_orders=[
                {
                    "client_order_id": "parent-client-id",
                    "ok": True,
                    "status": "SUCCESS",
                    "raw_status": "2",
                    "order_type": "LIMIT",
                    "order_id": "broker-order-id",
                    "entrust_no": "83931",
                    "submitted_price": 23.06,
                    "protection_limit_price": 23.06,
                    "price_source": "ask_level_5_capped_by_protection_limit",
                    "message": "BUY 301526.SZ, 数量: 300, 价格: 23.06",
                }
            ],
            protection_limit_deadline_at=close_deadline,
        )

        self.assertEqual(close_deadline, parent.deadline_at)
        self.assertEqual(close_deadline, child.deadline_at)
        self.assertEqual("ACKNOWLEDGED", parent.status)
        self.assertEqual("ACKNOWLEDGED", child.status)

    def test_non_protection_limit_submission_keeps_original_deadline(self):
        db = self._db_session()
        parent, child, original_deadline = self._add_parent_child_orders(db)

        record_submission_result(
            db,
            external_trading_account_id=2,
            response_orders=[
                {
                    "client_order_id": "parent-client-id",
                    "ok": True,
                    "status": "SUCCESS",
                    "raw_status": "2",
                    "order_type": "LIMIT",
                    "order_id": "broker-order-id",
                    "entrust_no": "83931",
                    "submitted_price": 23.01,
                    "protection_limit_price": 23.06,
                    "price_source": "ask_level_3",
                    "message": "BUY 301526.SZ, 数量: 300, 价格: 23.01",
                }
            ],
            protection_limit_deadline_at=datetime(2026, 6, 5, 15, 0, 0),
        )

        self.assertEqual(original_deadline, parent.deadline_at)
        self.assertEqual(original_deadline, child.deadline_at)
