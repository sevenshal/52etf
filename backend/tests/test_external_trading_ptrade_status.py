from unittest import TestCase

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ptrade_robot import ptrade_client
from ptrade_robot.ptrade_client import get_order_filled_quantity, normalize_trade
from src.core.external_trading_database import (
    ExternalTradingBase,
    ExternalTradingEventLog,
    ExternalTradingOrder,
    ExternalTradingOrderFill,
)
from src.core.services.external_trading_ledger import (
    _apply_order_status_event,
    _is_trade_fill_event,
    order_event_filled_quantity,
    process_trade_events,
    ptrade_status_to_lifecycle,
    record_external_event_logs,
    record_submission_result,
)


class PTradeOrderStatusTest(TestCase):
    def test_order_status_8_without_reported_fill_is_not_filled(self):
        self.assertEqual("ACKNOWLEDGED", ptrade_status_to_lifecycle("8", 0, 100))

    def test_order_status_8_with_explicit_full_fill_is_filled(self):
        self.assertEqual("FILLED", ptrade_status_to_lifecycle("8", 100, 100))

    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _filled_parent_order(self):
        return ExternalTradingOrder(
            account_id="acct",
            external_trading_account_id=2,
            allocation_role="PARENT",
            client_order_id="parent-client-id",
            broker_order_id="broker-order-id",
            entrust_no="43486",
            symbol="301086.SZ",
            side="SELL",
            order_type="LIMIT",
            quantity=100,
            filled_quantity=100,
            remaining_quantity=0,
            status="FILLED",
            ptrade_status="8",
        )

    def test_order_event_uses_business_amount_as_fill_quantity(self):
        payload = {"status": "8", "business_amount": 100, "quantity": 100}

        self.assertEqual(100, order_event_filled_quantity(payload, "8", current=0))

    def test_order_event_uses_explicit_filled_amount(self):
        payload = {"status": "8", "filled_amount": 100, "quantity": 100}

        self.assertEqual(100, order_event_filled_quantity(payload, "8", current=0))

    def test_cancel_status_does_not_use_business_amount_as_fill_quantity(self):
        payload = {"status": "6", "business_amount": 100, "filled_quantity": 100}

        self.assertEqual(0, order_event_filled_quantity(payload, "6", current=0))

    def test_ptrade_client_order_normalizer_uses_business_amount_for_filled_status(self):
        order_item = {
            "status": "8",
            "amount": -100,
            "business_amount": 100,
        }

        self.assertEqual(100, get_order_filled_quantity(order_item))

    def test_ptrade_client_order_normalizer_ignores_business_amount_for_cancel_status(self):
        order_item = {
            "status": "6",
            "amount": -100,
            "business_amount": 100,
        }

        self.assertEqual(0, get_order_filled_quantity(order_item))

    def test_ptrade_client_order_normalizer_uses_filled_amount(self):
        order_item = {
            "status": "8",
            "amount": -100,
            "business_amount": 100,
            "filled_amount": 40,
        }

        self.assertEqual(40, get_order_filled_quantity(order_item))

    def test_ptrade_client_alias_registration_does_not_reset_existing_status(self):
        old_g = getattr(ptrade_client, "g", None)
        ptrade_client.g = SimpleNamespace(
            order_client_id_by_order_id={},
            order_last_known_status={"broker-order-id": "8"},
        )
        try:
            ptrade_client.remember_client_order_id("broker-order-id", "client-order-id")

            self.assertEqual("client-order-id", ptrade_client.g.order_client_id_by_order_id["broker-order-id"])
            self.assertEqual("8", ptrade_client.g.order_last_known_status["broker-order-id"])
        finally:
            if old_g is None:
                delattr(ptrade_client, "g")
            else:
                ptrade_client.g = old_g

    def test_ptrade_client_order_normalizer_resolves_entrust_alias(self):
        old_g = getattr(ptrade_client, "g", None)
        ptrade_client.g = SimpleNamespace(
            order_client_id_by_order_id={"43486": "client-order-id"},
            order_last_known_status={},
        )
        try:
            order = ptrade_client.normalize_order(
                {
                    "status": "6",
                    "amount": -100,
                    "entrust_no": "43486",
                    "stock_code": "301086.SZ",
                },
                datetime(2026, 5, 29, 10, 3, 44),
            )

            self.assertEqual("client-order-id", order["client_order_id"])
        finally:
            if old_g is None:
                delattr(ptrade_client, "g")
            else:
                ptrade_client.g = old_g

    def test_ptrade_trade_response_uses_business_amount(self):
        trade = normalize_trade(
            {
                "status": "8",
                "stock_code": "301086.XSHE",
                "business_amount": 100,
                "business_price": 163.87,
                "business_balance": 16387.0,
                "entrust_bs": "2",
                "order_id": "broker-order-id",
            },
            datetime(2026, 5, 29, 10, 3, 31),
        )

        self.assertEqual(100, trade["quantity"])
        self.assertEqual(163.87, trade["price"])
        self.assertTrue(_is_trade_fill_event(trade))

    def test_terminal_cancel_can_correct_false_filled_order_without_fill_rows(self):
        db = self._db_session()
        order = self._filled_parent_order()
        db.add(order)
        db.flush()
        child = ExternalTradingOrder(
            account_id="acct",
            external_trading_account_id=2,
            parent_order_id=order.id,
            allocation_role="CHILD",
            client_order_id="child-client-id",
            broker_order_id="broker-order-id",
            entrust_no="43486",
            symbol="301086.SZ",
            side="SELL",
            order_type="LIMIT",
            quantity=100,
            filled_quantity=0,
            remaining_quantity=100,
            status="SUBMITTED",
            ptrade_status="8",
        )
        db.add(child)
        db.flush()

        _apply_order_status_event(
            db,
            order,
            {
                "status": "6",
                "order_id": "broker-order-id",
                "entrust_no": "43486",
                "quantity": 100,
                "price": 163.87,
            },
            datetime(2026, 5, 29, 10, 3, 44),
        )

        self.assertEqual("CANCELED", order.status)
        self.assertEqual(0, order.filled_quantity)
        self.assertEqual(100, order.remaining_quantity)
        self.assertEqual("CANCELED", child.status)

    def test_terminal_cancel_does_not_clear_filled_order_with_fill_rows(self):
        db = self._db_session()
        order = self._filled_parent_order()
        db.add(order)
        db.flush()
        db.add(
            ExternalTradingOrderFill(
                account_id="acct",
                external_trading_account_id=2,
                order_id=order.id,
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                fill_key="fill-key",
                symbol="301086.SZ",
                side="SELL",
                quantity=100,
                price=163.87,
                amount=16387.0,
            )
        )
        db.flush()

        _apply_order_status_event(
            db,
            order,
            {"status": "6", "order_id": "broker-order-id", "entrust_no": "43486"},
            datetime(2026, 5, 29, 10, 3, 44),
        )

        self.assertEqual("FILLED", order.status)
        self.assertEqual(100, order.filled_quantity)
        self.assertEqual(0, order.remaining_quantity)

    def test_unmatched_trade_event_replays_after_submission_result_adds_broker_alias(self):
        db = self._db_session()
        order = ExternalTradingOrder(
            account_id="acct",
            external_trading_account_id=2,
            sub_account_id=88,
            allocation_role="DIRECT",
            client_order_id="client-order-id",
            symbol="301086.SZ",
            side="SELL",
            order_type="LIMIT",
            quantity=100,
            filled_quantity=0,
            remaining_quantity=100,
            status="CREATED",
        )
        db.add(order)
        db.flush()
        trade = {
            "order_id": "broker-order-id",
            "entrust_no": "43486",
            "symbol": "301086.SZ",
            "side": "SELL",
            "quantity": 100,
            "price": 163.87,
            "status": "8",
            "business_no": "fill-key",
            "traded_at": "2026-05-29 10:03:31",
        }
        event_logs = record_external_event_logs(
            db,
            external_trading_account_id=2,
            account_id="acct",
            account_name="PTrade-国盛实盘",
            event_type="trade_event",
            events=[trade],
        )

        inserted = process_trade_events(
            db,
            external_trading_account_id=2,
            trades=[trade],
            event_logs=event_logs,
        )
        self.assertEqual(0, inserted)
        self.assertEqual("UNMATCHED", event_logs[0].process_status)

        record_submission_result(
            db,
            external_trading_account_id=2,
            response_orders=[
                {
                    "client_order_id": "client-order-id",
                    "order_id": "broker-order-id",
                    "entrust_no": "43486",
                    "raw_status": "2",
                    "status": "SUCCESS",
                    "quantity": 100,
                    "submitted_price": 163.87,
                    "ok": True,
                }
            ],
        )

        log = db.query(ExternalTradingEventLog).first()
        fill = db.query(ExternalTradingOrderFill).filter(ExternalTradingOrderFill.order_id == order.id).first()
        self.assertEqual("PROCESSED", log.process_status)
        self.assertEqual(1, log.replay_count)
        self.assertEqual(order.id, log.matched_order_id)
        self.assertEqual(88, log.matched_sub_account_id)
        self.assertIsNotNone(fill)
        self.assertEqual(100, fill.quantity)
        self.assertEqual("FILLED", order.status)
