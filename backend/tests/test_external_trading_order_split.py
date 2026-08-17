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

    def _add_account(self, db, *, max_batch_amount=None, batch_interval_seconds=None, min_commission=5.0,
                     etf_commission_rate_pct=None, etf_min_commission=None, etf_stamp_tax_rate_pct=None):
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
            etf_commission_rate_pct=etf_commission_rate_pct,
            etf_min_commission=etf_min_commission,
            etf_stamp_tax_rate_pct=etf_stamp_tax_rate_pct,
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

    def test_fee_break_even_etf_rates_when_configured(self):
        db = self._db_session()
        # 股票佣金万 2.5/最低 5 → 门槛 2 万；ETF 佣金万 0.5/最低 1 → 门槛 2 万；
        # 若 ETF 最低佣金也是 5 → 门槛 = 5/(0.005/100) = 10 万
        self._add_account(db, etf_commission_rate_pct=0.005, etf_min_commission=5.0)
        db.flush()
        account = db.query(ExternalTradingAccount).first()
        self.assertAlmostEqual(20000.0, compute_fee_break_even_amount(account))
        self.assertAlmostEqual(100000.0, compute_fee_break_even_amount(account, etf=True))

    def test_fee_break_even_etf_inherits_stock_rates_when_unset(self):
        db = self._db_session()
        self._add_account(db)
        db.flush()
        account = db.query(ExternalTradingAccount).first()
        # 未配置 ETF 费率 → ETF 门槛与股票一致
        self.assertAlmostEqual(
            compute_fee_break_even_amount(account),
            compute_fee_break_even_amount(account, etf=True),
        )

    def test_estimate_fee_totals_uses_etf_rates(self):
        from src.core.services.external_trading_ledger import _estimate_fee_totals

        db = self._db_session()
        self._add_account(
            db,
            etf_commission_rate_pct=0.005,
            etf_min_commission=1.0,
            etf_stamp_tax_rate_pct=0.0,
        )
        db.flush()
        account = db.query(ExternalTradingAccount).first()
        # ETF 卖出：佣金 100000*0.005/100=5 元（≥最低 1），印花税 0
        totals = _estimate_fee_totals(account, "SELL", 100000.0, symbol="510300.SH")
        self.assertAlmostEqual(5.0, totals["commission"])
        self.assertAlmostEqual(0.0, totals["stamp_tax"])
        self.assertAlmostEqual(5.0, totals["fee_total"])
        # 股票卖出：佣金 25 元 + 印花税 50 元
        totals = _estimate_fee_totals(account, "SELL", 100000.0, symbol="600519.SH")
        self.assertAlmostEqual(25.0, totals["commission"])
        self.assertAlmostEqual(50.0, totals["stamp_tax"])
        self.assertAlmostEqual(75.0, totals["fee_total"])

    def test_estimate_fee_totals_etf_inherits_stock_rates_when_unset(self):
        from src.core.services.external_trading_ledger import _estimate_fee_totals

        db = self._db_session()
        self._add_account(db)
        db.flush()
        account = db.query(ExternalTradingAccount).first()
        # 未配置 ETF 费率 → ETF 卖出仍按股票费率计印花税
        totals = _estimate_fee_totals(account, "SELL", 100000.0, symbol="510300.SH")
        self.assertAlmostEqual(25.0, totals["commission"])
        self.assertAlmostEqual(50.0, totals["stamp_tax"])


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

    def test_batch_amount_cap_keeps_whole_order_when_tail_below_fee_break_even(self):
        db = self._db_session()
        # 跨轮分批最后一轮场景：剩余 31 万，单轮上限 30 万，佣金门槛 2 万。
        # 截断 30 万后剩余 1 万 < 门槛 → 本轮整笔提交 31 万，避免尾单被最低佣金吃掉
        self._add_account(db, max_batch_amount=300000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, target_quantity=31000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        orders = plan["external_orders"]
        self.assertEqual(1, len(orders))
        self.assertEqual(31000, orders[0]["quantity"])  # 整笔 31 万全提交
        self.assertEqual([], plan["deferred"])

    def test_batch_amount_cap_uses_etf_break_even_for_etf_symbols(self):
        db = self._db_session()
        # 股票佣金门槛 2 万；ETF 佣金万 0.5/最低 5 → 门槛 10 万。
        # 单轮上限 5 万：高于股票门槛（可分）但低于 ETF 门槛（不可分）→
        # ETF 标的整笔提交，股票标的正常拆分。
        self._add_account(
            db,
            max_batch_amount=50000.0,
            etf_commission_rate_pct=0.005,
            etf_min_commission=5.0,
        )
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, symbol="510300.SH", target_quantity=10000, reference_price=10.0)
        self._add_target(db, sub_account_id=11, symbol="600519.SH", target_quantity=10000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0, "600519.SH": 10.0},
        )

        orders = {o["symbol"]: o for o in plan["external_orders"]}
        deferred = {d["symbol"]: d for d in plan["deferred"]}
        # ETF：整笔 10 万全提交（低于 ETF 佣金门槛 10 万，拆分会被最低佣金吃掉）
        self.assertEqual(10000, orders["510300.SH"]["quantity"])
        self.assertNotIn("510300.SH", deferred)
        # 股票：门槛 2 万 < 截断 5 万 → 本轮提交 5 万，剩余 5 万留待下轮
        self.assertEqual(5000, orders["600519.SH"]["quantity"])
        self.assertEqual(5000, deferred["600519.SH"]["quantity"])

    def test_batch_amount_cap_etf_break_even_inherits_stock_when_unset(self):
        db = self._db_session()
        # 未配置 ETF 费率：ETF 标的按股票门槛 2 万拆分
        self._add_account(db, max_batch_amount=50000.0)
        self._add_sub_account(db, sub_account_id=11, name="Growth")
        self._add_target(db, sub_account_id=11, symbol="510300.SH", target_quantity=10000, reference_price=10.0)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 10.0},
        )

        orders = plan["external_orders"]
        self.assertEqual(1, len(orders))
        self.assertEqual(5000, orders[0]["quantity"])
        self.assertEqual(5000, plan["deferred"][0]["quantity"])

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
