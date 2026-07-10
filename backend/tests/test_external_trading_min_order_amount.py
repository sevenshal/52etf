from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBase,
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
)
from src.core.services.external_trading_execution_policy import resolve_execution_policy
from src.core.services.external_trading_ledger import build_netted_target_execution_plan


class ExternalTradingMinOrderAmountTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _add_account(self, db, *, min_order_amount=0.0):
        account = ExternalTradingAccount(
            id=1,
            account_id="acct",
            name="US Broker",
            identifier="broker-us",
            market_type="US_STOCK",
            enabled=True,
            executor_lot_size=1,
            executor_min_order_amount=min_order_amount,
        )
        db.add(account)
        return account

    def _add_sub_account(self, db, *, sub_account_id, name, min_order_amount=None):
        sub_account = ExternalTradingSubAccount(
            id=sub_account_id,
            account_id="acct",
            external_trading_account_id=1,
            name=name,
            enabled=True,
            executor_lot_size=1,
            executor_min_order_amount=min_order_amount,
        )
        db.add(sub_account)
        return sub_account

    def _add_target(self, db, *, sub_account_id, symbol="AAPL.US", target_quantity, reference_price=None):
        db.add(
            ExternalTradingTargetPosition(
                account_id="acct",
                external_trading_account_id=1,
                sub_account_id=sub_account_id,
                symbol=symbol,
                target_quantity=target_quantity,
                reference_price=reference_price,
                reference_price_source="test_quote" if reference_price else None,
                status="ACTIVE",
            )
        )

    def _add_position(self, db, *, sub_account_id, symbol="AAPL.US", quantity, available_quantity=None, market_price=None):
        db.add(
            ExternalTradingLedgerPosition(
                account_id="acct",
                external_trading_account_id=1,
                sub_account_id=sub_account_id,
                symbol=symbol,
                quantity=quantity,
                available_quantity=quantity if available_quantity is None else available_quantity,
                market_price=market_price,
                market_value=(market_price or 0) * quantity if market_price else None,
            )
        )

    def test_resolve_execution_policy_prefers_sub_account_min_order_amount(self):
        db = self._db_session()
        account = self._add_account(db, min_order_amount=1200.0)
        sub_account = self._add_sub_account(db, sub_account_id=11, name="Growth", min_order_amount=600.0)
        db.flush()

        policy = resolve_execution_policy(account, sub_account)

        self.assertEqual(600.0, policy["min_order_amount"])

    def test_plan_skips_small_buy_below_min_order_amount(self):
        db = self._db_session()
        self._add_account(db, min_order_amount=1000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=20, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            lot_size=1,
        )

        self.assertEqual([], plan["external_orders"])
        self.assertEqual("SKIPPED_MIN_ORDER_AMOUNT", plan["skipped"][0]["reason"])
        self.assertEqual(200.0, plan["skipped"][0]["estimated_notional"])
        self.assertEqual(1000.0, plan["skipped"][0]["min_order_amount"])

    def test_plan_keeps_full_liquidation_sell_even_if_below_min_order_amount(self):
        db = self._db_session()
        self._add_account(db, min_order_amount=1000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_position(db, sub_account_id=11, quantity=20, market_price=10.0)
        self._add_target(db, sub_account_id=11, target_quantity=0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            lot_size=1,
        )

        self.assertEqual([], plan["skipped"])
        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual("SELL", plan["external_orders"][0]["side"])
        self.assertEqual(20, plan["external_orders"][0]["quantity"])

    def test_plan_skips_small_partial_sell_below_min_order_amount(self):
        db = self._db_session()
        self._add_account(db, min_order_amount=1000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_position(db, sub_account_id=11, quantity=20, market_price=10.0)
        self._add_target(db, sub_account_id=11, target_quantity=10)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            lot_size=1,
        )

        self.assertEqual([], plan["external_orders"])
        self.assertEqual("SELL", plan["skipped"][0]["side"])
        self.assertEqual("SKIPPED_MIN_ORDER_AMOUNT", plan["skipped"][0]["reason"])
        self.assertEqual(100.0, plan["skipped"][0]["estimated_notional"])

    def test_plan_does_not_skip_when_price_estimate_missing(self):
        db = self._db_session()
        self._add_account(db, min_order_amount=1000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=20)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            lot_size=1,
        )

        self.assertEqual([], plan["skipped"])
        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual("BUY", plan["external_orders"][0]["side"])
        self.assertEqual(20, plan["external_orders"][0]["quantity"])

    def test_plan_filters_small_sub_account_before_parent_order(self):
        db = self._db_session()
        self._add_account(db, min_order_amount=0.0)
        self._add_sub_account(db, sub_account_id=11, name="Small", min_order_amount=500.0)
        self._add_sub_account(db, sub_account_id=12, name="Large")
        self._add_target(db, sub_account_id=11, target_quantity=10, reference_price=10.0)
        self._add_target(db, sub_account_id=12, target_quantity=60, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            lot_size=1,
        )

        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual(60, plan["external_orders"][0]["quantity"])
        self.assertEqual([12], [item["sub_account_id"] for item in plan["external_orders"][0]["allocations"]])
        self.assertEqual("SKIPPED_MIN_ORDER_AMOUNT", plan["skipped"][0]["reason"])
        self.assertEqual(11, plan["skipped"][0]["sub_account_id"])
