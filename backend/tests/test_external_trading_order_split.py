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
from src.core.services.external_trading_execution_policy import (
    aggregate_execution_policy,
    compute_fee_break_even_amount,
    resolve_execution_policy,
)
from src.core.services.external_trading_ledger import build_netted_target_execution_plan


class ExternalTradingBatchAmountTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _add_account(self, db, *, max_batch_amount=None, batch_interval_seconds=None, min_commission=5.0):
        account = ExternalTradingAccount(
            id=1,
            account_id="acct",
            name="A Share Broker",
            identifier="broker-cn",
            market_type="A_STOCK",
            enabled=True,
            executor_lot_size=100,
            commission_rate_pct=0.025,
            min_commission=min_commission,
            stamp_tax_rate_pct=0.05,
            executor_max_batch_amount=max_batch_amount,
            executor_batch_interval_seconds=batch_interval_seconds,
        )
        db.add(account)
        return account

    def _add_sub_account(self, db, *, sub_account_id, name, max_batch_amount=None, batch_interval_seconds=None):
        sub_account = ExternalTradingSubAccount(
            id=sub_account_id,
            account_id="acct",
            external_trading_account_id=1,
            name=name,
            enabled=True,
            executor_lot_size=100,
            executor_max_batch_amount=max_batch_amount,
            executor_batch_interval_seconds=batch_interval_seconds,
        )
        db.add(sub_account)
        return sub_account

    def _add_target(self, db, *, sub_account_id, symbol="510300.SH", target_quantity, reference_price=None):
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

    def _add_position(self, db, *, sub_account_id, symbol="510300.SH", quantity, market_price=None):
        db.add(
            ExternalTradingLedgerPosition(
                account_id="acct",
                external_trading_account_id=1,
                sub_account_id=sub_account_id,
                symbol=symbol,
                quantity=quantity,
                available_quantity=quantity,
                market_price=market_price,
                market_value=(market_price or 0) * quantity if market_price else None,
            )
        )

    # ---- 策略解析 ----

    def test_fee_break_even_derived_from_commission(self):
        db = self._db_session()
        self._add_account(db)
        db.flush()
        account = db.query(ExternalTradingAccount).first()
        self.assertAlmostEqual(20000.0, compute_fee_break_even_amount(account))
        account.min_commission = 0
        self.assertEqual(0.0, compute_fee_break_even_amount(account))

    def test_resolve_execution_policy_sub_account_overrides_account(self):
        db = self._db_session()
        self._add_account(db, max_batch_amount=300000.0, batch_interval_seconds=30)
        self._add_sub_account(db, sub_account_id=11, name="Growth", max_batch_amount=100000.0, batch_interval_seconds=60)
        db.flush()
        account = db.query(ExternalTradingAccount).first()
        sub_account = db.query(ExternalTradingSubAccount).first()

        policy = resolve_execution_policy(account, sub_account)

        self.assertEqual(100000.0, policy["max_batch_amount"])
        self.assertEqual(60, policy["batch_interval_seconds"])
        self.assertAlmostEqual(20000.0, policy["fee_break_even_amount"])
        self.assertEqual("sub_account", policy["source"])

    def test_aggregate_execution_policy_takes_most_conservative(self):
        policies = [
            {"max_batch_amount": 300000.0, "batch_interval_seconds": 30, "fee_break_even_amount": 20000.0},
            {"max_batch_amount": 100000.0, "batch_interval_seconds": 60, "fee_break_even_amount": 20000.0},
        ]
        aggregated = aggregate_execution_policy(policies)
        self.assertEqual(100000.0, aggregated["max_batch_amount"])
        self.assertEqual(60, aggregated["batch_interval_seconds"])
        self.assertAlmostEqual(20000.0, aggregated["fee_break_even_amount"])

    # ---- 批次金额上限（跨轮分批） ----

    def test_plan_does_not_defer_when_batch_limit_disabled(self):
        db = self._db_session()
        self._add_account(db)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=100000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual(100000, plan["external_orders"][0]["quantity"])
        self.assertEqual([], plan["deferred"])

    def test_plan_batch_amount_cap_defers_overflow_to_next_round(self):
        db = self._db_session()
        self._add_account(db, max_batch_amount=300000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=100000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        orders = plan["external_orders"]
        self.assertEqual(1, len(orders))
        self.assertEqual(30000, orders[0]["quantity"])  # 本轮只提交 30 万元
        deferred = plan["deferred"]
        self.assertEqual(1, len(deferred))
        self.assertEqual("batch_amount_cap", deferred[0]["reason"])
        self.assertEqual(70000, deferred[0]["quantity"])
        self.assertEqual(700000.0, deferred[0]["estimated_amount"])

    def test_batch_amount_cap_keeps_whole_order_when_cut_below_fee_break_even(self):
        db = self._db_session()
        # 单轮上限 1.5 万 < 佣金门槛 2 万：截断出来的每笔都会被最低佣金吃掉
        # → 整笔提交（宁可不分批也不额外亏费）
        self._add_account(db, max_batch_amount=15000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=10000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        orders = plan["external_orders"]
        self.assertEqual(1, len(orders))
        self.assertEqual(10000, orders[0]["quantity"])  # 整笔 10 万全提交
        self.assertEqual([], plan["deferred"])

    def test_batch_amount_cap_lower_than_one_lot_keeps_whole_order(self):
        db = self._db_session()
        # 上限 500 元 < 1 手（1000 元）：截断部分不足一个最小交易单位 → 整笔提交
        self._add_account(db, max_batch_amount=500.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=10000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual(10000, plan["external_orders"][0]["quantity"])
        self.assertEqual([], plan["deferred"])

    def test_batch_amount_cap_shares_capacity_across_symbols(self):
        db = self._db_session()
        # 两个 symbol 各自独立额度
        self._add_account(db, max_batch_amount=300000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, symbol="510300.SH", target_quantity=100000, reference_price=10.0)
        self._add_target(db, sub_account_id=11, symbol="159915.SZ", target_quantity=50000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0, "159915.SZ": 10.0},
        )

        orders = {o["symbol"]: o for o in plan["external_orders"]}
        self.assertEqual(30000, orders["510300.SH"]["quantity"])
        self.assertEqual(30000, orders["159915.SZ"]["quantity"])
        self.assertEqual(2, len(plan["deferred"]))


class ExternalTradingBatchIntervalTest(TestCase):
    def setUp(self):
        from src.core.services import external_trading_executor as executor

        self.executor = executor
        self.executor._last_submit_at.clear()

    def _order(self, symbol="510300.SH", side="BUY", interval=60, client_order_id="ord-1"):
        return {
            "symbol": symbol,
            "side": side,
            "quantity": 1000,
            "client_order_id": client_order_id,
            "execution_policy": {"batch_interval_seconds": interval},
        }

    def test_skips_order_when_within_interval(self):
        now = 1000.0
        self.executor._last_submit_at[(1, "510300.SH", "BUY")] = now - 30
        kept, deferred = self.executor._filter_orders_by_batch_interval(
            [self._order()], account_pk=1, now_ts=now
        )
        self.assertEqual([], kept)
        self.assertEqual(1, len(deferred))
        self.assertEqual("batch_interval", deferred[0]["reason"])

    def test_keeps_order_after_interval_elapsed(self):
        now = 1000.0
        self.executor._last_submit_at[(1, "510300.SH", "BUY")] = now - 120
        kept, deferred = self.executor._filter_orders_by_batch_interval(
            [self._order()], account_pk=1, now_ts=now
        )
        self.assertEqual(1, len(kept))
        self.assertEqual([], deferred)

    def test_keeps_order_when_interval_disabled(self):
        now = 1000.0
        self.executor._last_submit_at[(1, "510300.SH", "BUY")] = now - 5
        kept, deferred = self.executor._filter_orders_by_batch_interval(
            [self._order(interval=0)], account_pk=1, now_ts=now
        )
        self.assertEqual(1, len(kept))
        self.assertEqual([], deferred)

    def test_different_symbols_are_independent(self):
        now = 1000.0
        self.executor._last_submit_at[(1, "510300.SH", "BUY")] = now - 10
        kept, deferred = self.executor._filter_orders_by_batch_interval(
            [
                self._order(symbol="510300.SH", interval=60),
                self._order(symbol="159915.SZ", interval=60),
            ],
            account_pk=1,
            now_ts=now,
        )
        self.assertEqual(1, len(kept))
        self.assertEqual("159915.SZ", kept[0]["symbol"])
        self.assertEqual(1, len(deferred))

    def test_record_submit_time_updates_state(self):
        self.executor._record_batch_submit_times(1, [self._order()], [], 1234.0)
        self.assertEqual(1234.0, self.executor._last_submit_at[(1, "510300.SH", "BUY")])

    def test_record_submit_time_skips_failed_orders(self):
        self.executor._record_batch_submit_times(
            1,
            [self._order()],
            [{"client_order_id": "ord-1", "ok": False}],
            1234.0,
        )
        self.assertNotIn((1, "510300.SH", "BUY"), self.executor._last_submit_at)

    def test_record_submit_time_keeps_acknowledged_orders(self):
        self.executor._record_batch_submit_times(
            1,
            [self._order(), self._order(symbol="159915.SZ", client_order_id="ord-2")],
            [{"client_order_id": "ord-1", "ok": True}, {"client_order_id": "ord-2"}],
            1234.0,
        )
        self.assertEqual(1234.0, self.executor._last_submit_at[(1, "510300.SH", "BUY")])
        self.assertEqual(1234.0, self.executor._last_submit_at[(1, "159915.SZ", "BUY")])
