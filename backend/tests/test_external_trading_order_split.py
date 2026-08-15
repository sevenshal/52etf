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
    DEFAULT_EXECUTOR_MAX_SINGLE_ORDER_AMOUNT,
    aggregate_execution_policy,
    compute_fee_break_even_amount,
    resolve_execution_policy,
)
from src.core.services.external_trading_ledger import (
    build_netted_target_execution_plan,
    _split_quantity_for_amount,
)


class ExternalTradingOrderSplitTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _add_account(
        self,
        db,
        *,
        max_single_order_amount=None,
        max_batch_amount=None,
        batch_interval_seconds=None,
        min_commission=5.0,
    ):
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
            executor_max_single_order_amount=max_single_order_amount,
            executor_max_batch_amount=max_batch_amount,
            executor_batch_interval_seconds=batch_interval_seconds,
        )
        db.add(account)
        return account

    def _add_sub_account(
        self,
        db,
        *,
        sub_account_id,
        name,
        max_single_order_amount=None,
        max_batch_amount=None,
        batch_interval_seconds=None,
    ):
        sub_account = ExternalTradingSubAccount(
            id=sub_account_id,
            account_id="acct",
            external_trading_account_id=1,
            name=name,
            enabled=True,
            executor_lot_size=100,
            executor_max_single_order_amount=max_single_order_amount,
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
        self._add_account(
            db,
            max_single_order_amount=500000.0,
            max_batch_amount=300000.0,
            batch_interval_seconds=30,
        )
        self._add_sub_account(
            db,
            sub_account_id=11,
            name="Growth",
            max_single_order_amount=200000.0,
            max_batch_amount=100000.0,
            batch_interval_seconds=60,
        )
        db.flush()
        account = db.query(ExternalTradingAccount).first()
        sub_account = db.query(ExternalTradingSubAccount).first()

        policy = resolve_execution_policy(account, sub_account)

        self.assertEqual(200000.0, policy["max_single_order_amount"])
        self.assertEqual(100000.0, policy["max_batch_amount"])
        self.assertEqual(60, policy["batch_interval_seconds"])
        self.assertAlmostEqual(20000.0, policy["fee_break_even_amount"])
        self.assertEqual("sub_account", policy["source"])

    def test_aggregate_execution_policy_takes_most_conservative(self):
        policies = [
            {
                "max_single_order_amount": 500000.0,
                "max_batch_amount": 300000.0,
                "batch_interval_seconds": 30,
                "fee_break_even_amount": 20000.0,
            },
            {
                "max_single_order_amount": 200000.0,
                "max_batch_amount": 100000.0,
                "batch_interval_seconds": 60,
                "fee_break_even_amount": 20000.0,
            },
        ]
        aggregated = aggregate_execution_policy(policies)
        self.assertEqual(200000.0, aggregated["max_single_order_amount"])
        self.assertEqual(100000.0, aggregated["max_batch_amount"])
        self.assertEqual(60, aggregated["batch_interval_seconds"])
        self.assertAlmostEqual(20000.0, aggregated["fee_break_even_amount"])

    # ---- 拆单 ----

    def test_plan_does_not_split_when_disabled(self):
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
        self.assertEqual([], plan["splits"])

    def test_plan_does_not_split_when_amount_below_limit(self):
        db = self._db_session()
        self._add_account(db, max_single_order_amount=500000.0)
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
        self.assertEqual([], plan["splits"])

    def test_plan_splits_large_order_into_multiple_pieces(self):
        db = self._db_session()
        self._add_account(db, max_single_order_amount=500000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_sub_account(db, sub_account_id=12, name="Value")
        self._add_target(db, sub_account_id=11, target_quantity=60000, reference_price=10.0)
        self._add_target(db, sub_account_id=12, target_quantity=40000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        orders = plan["external_orders"]
        self.assertEqual(2, len(orders))
        total_quantity = sum(safe_quantity(o) for o in orders)
        self.assertEqual(100000, total_quantity)
        for order in orders:
            # 每笔金额 ≤ 上限且 ≥ 佣金门槛（拆单不产生额外佣金）
            self.assertLessEqual(order["quantity"] * 10.0, 500000.0 + 1)
            self.assertGreaterEqual(order["quantity"] * 10.0, 20000.0)
        # allocations 守恒
        total_allocations = sum(
            safe_quantity(alloc)
            for order in orders
            for alloc in order.get("allocations") or []
        )
        self.assertEqual(100000, total_allocations)
        # splits 统计
        self.assertEqual(1, len(plan["splits"]))
        self.assertEqual(2, plan["splits"][0]["piece_count"])
        self.assertEqual([], plan["deferred"])

    def test_plan_does_not_split_when_amount_cannot_cover_fee_break_even(self):
        db = self._db_session()
        # 2.5 万元 > 1 万元上限，但最多只能拆 1 笔不亏费（2.5万/2万=1）
        self._add_account(db, max_single_order_amount=10000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=2500, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual(2500, plan["external_orders"][0]["quantity"])
        self.assertEqual([], plan["splits"])

    def test_plan_star_market_each_piece_at_least_200_shares(self):
        db = self._db_session()
        # 688xxx.SH 科创板：每笔必须 ≥ 200 股，否则触发 INVALID_LOT_SIZE 阻塞。
        # 无最低佣金（min_commission=0）排除费用约束干扰，专注股数下限。
        self._add_account(db, max_single_order_amount=2000.0, min_commission=0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, symbol="688001.SH", target_quantity=1000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"688001.SH": 10.0},
        )

        orders = plan["external_orders"]
        # 1 万元 → n_target=5，n_cap=1000//200=5 → 5 笔 200 股
        self.assertEqual(5, len(orders))
        for order in orders:
            self.assertGreaterEqual(order["quantity"], 200)

    def test_star_market_quantity_below_two_chunks_not_split(self):
        db = self._db_session()
        # 350 股被 lot_size 取整为 300 股，无法拆成两笔都 ≥ 200 股 → 不拆
        self._add_account(db, max_single_order_amount=1000.0, min_commission=0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, symbol="688001.SH", target_quantity=350, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"688001.SH": 10.0},
        )

        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual(300, plan["external_orders"][0]["quantity"])

    # ---- 跨轮分批（批次金额上限） ----

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

    def test_batch_amount_cap_lower_than_one_lot_defers_all(self):
        db = self._db_session()
        # 上限 500 元 < 1 手（1000 元）→ 本轮无法提交，全部推迟
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

        self.assertEqual([], plan["external_orders"])
        self.assertEqual(1, len(plan["deferred"]))
        self.assertEqual("batch_amount_cap", plan["deferred"][0]["reason"])

    def test_plan_combines_split_and_batch_cap(self):
        db = self._db_session()
        # 单笔上限 50 万，单轮提交上限 80 万：100 万股 → 拆 2 笔 50 万股
        # 第一笔 50 万 + 第二笔最多 30 万（合计 80 万），剩余 20 万推迟到下一轮
        self._add_account(db, max_single_order_amount=500000.0, max_batch_amount=800000.0)
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
        self.assertEqual(2, len(orders))
        self.assertEqual([50000, 30000], [o["quantity"] for o in orders])
        self.assertEqual(1, len(plan["deferred"]))
        self.assertEqual(20000, plan["deferred"][0]["quantity"])
        self.assertEqual("batch_amount_cap", plan["deferred"][0]["reason"])

    # ---- 拆单单元（数量切分） ----

    def test_split_quantity_preserves_total_and_lot_size(self):
        chunks = _split_quantity_for_amount(
            quantity=100001,
            price=10.0,
            lot_size=100,
            max_single_amount=300000.0,
            fee_break_even_amount=20000.0,
        )
        self.assertEqual(sum(chunks), 100001)
        for index, chunk in enumerate(chunks[:-1]):
            self.assertEqual(0, chunk % 100)
            self.assertLessEqual(chunk * 10.0, 300000.0 + 1)
        # 末笔补齐零头
        self.assertEqual(chunks[-1] * 10.0, 1000010.0 - sum(chunks[:-1]) * 10.0)


def safe_quantity(order):
    return int(order.get("quantity") or 0)


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
