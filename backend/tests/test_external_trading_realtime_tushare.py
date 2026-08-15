import asyncio
from datetime import date
import unittest
from unittest import TestCase
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBase,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
)
from src.core.services.external_trading_ledger import (
    _defer_orders_without_reference_price,
    build_netted_target_execution_plan,
)
from src.core.services.external_trading_valuation import (
    _fetch_tushare_realtime_quotes,
    _normalize_quote_map,
    _parse_tushare_realtime_frame,
)
from src.core.services.tushare import TushareService


TODAY = date(2026, 8, 17)  # 周一交易日


class TushareRealtimeFrameParseTest(TestCase):
    def test_keeps_today_rows_and_drops_stale_when_filter_enabled(self):
        frame = pd.DataFrame([
            {"ts_code": "510300.SH", "close": 4.70, "trade_time": "2026-08-17 10:00:00"},
            {"ts_code": "159941.SZ", "close": 1.70, "trade_time": "2026-08-14 17:00:00"},  # 昨日数据 → 丢弃
            {"ts_code": "600519.SH", "close": 0.0, "trade_time": "2026-08-17 10:00:00"},   # close 0 → 丢弃
            {"ts_code": "000001.SZ", "close": 11.0, "trade_time": None},                    # 无 trade_time → 丢弃
        ])
        result = _parse_tushare_realtime_frame(frame, today=TODAY, filter_stale_quotes=True)
        self.assertEqual({"510300.SH"}, set(result.keys()))
        self.assertEqual(4.7, result["510300.SH"]["price"])
        self.assertEqual("tushare_rt", result["510300.SH"]["source"])

    def test_default_no_filter_keeps_stale_rows(self):
        # 默认不过滤（估值/策略行情等场景）：昨日数据也保留
        frame = pd.DataFrame([
            {"ts_code": "159941.SZ", "close": 1.70, "trade_time": "2026-08-14 17:00:00"},
        ])
        result = _parse_tushare_realtime_frame(frame, today=TODAY)
        self.assertEqual({"159941.SZ"}, set(result.keys()))

    def test_empty_frame_returns_empty(self):
        self.assertEqual({}, _parse_tushare_realtime_frame(pd.DataFrame(), today=TODAY))
        self.assertEqual({}, _parse_tushare_realtime_frame(None, today=TODAY))


class TushareRealtimeQuotesFetchTest(TestCase):
    def _today_frame(self, rows):
        return pd.DataFrame(rows)

    def _mock_service(self, rt_k_frame, etf_frame):
        instance = unittest.mock.MagicMock()
        instance.get_a_stock_realtime_rt_k_frame.return_value = rt_k_frame
        instance.get_a_stock_realtime_etf_rt_k_frame.return_value = etf_frame
        return patch.object(TushareService, "get_instance", return_value=instance)

    def test_routes_stock_to_rt_k_and_etf_to_rt_etf_k(self):
        rt_k_frame = self._today_frame([
            {"ts_code": "600519.SH", "close": 1341.0, "trade_time": f"{TODAY} 10:00:00"},
        ])
        etf_frame = self._today_frame([
            {"ts_code": "510300.SH", "close": 4.7, "trade_time": f"{TODAY} 10:00:00"},
        ])
        with self._mock_service(rt_k_frame, etf_frame) as mock_get_instance:
            result = asyncio.run(_fetch_tushare_realtime_quotes(["510300.SH", "600519.SH"], today=TODAY))
        service = mock_get_instance.return_value
        service.get_a_stock_realtime_rt_k_frame.assert_called_once_with(["510300.SH", "600519.SH"])
        service.get_a_stock_realtime_etf_rt_k_frame.assert_called_once_with(["510300.SH", "600519.SH"])
        self.assertEqual({"510300.SH", "600519.SH"}, set(result.keys()))

    def test_skips_non_a_share_symbols(self):
        with self._mock_service(pd.DataFrame(), pd.DataFrame()) as mock_get_instance:
            result = asyncio.run(_fetch_tushare_realtime_quotes(["AAPL.US"]))
        service = mock_get_instance.return_value
        service.get_a_stock_realtime_rt_k_frame.assert_not_called()
        service.get_a_stock_realtime_etf_rt_k_frame.assert_not_called()
        self.assertEqual({}, result)

    def test_drops_stale_rows_when_filter_enabled(self):
        rt_k_frame = self._today_frame([
            {"ts_code": "600519.SH", "close": 1341.0, "trade_time": "2026-08-14 16:29:43"},  # 昨日
        ])
        etf_frame = self._today_frame([
            {"ts_code": "159941.SZ", "close": 1.7, "trade_time": "2026-08-14 17:00:12"},     # 昨日
        ])
        with self._mock_service(rt_k_frame, etf_frame):
            result = asyncio.run(_fetch_tushare_realtime_quotes(
                ["159941.SZ", "600519.SH"], today=TODAY, filter_stale_quotes=True,
            ))
        self.assertEqual({}, result)

    def test_default_no_filter_keeps_stale_rows(self):
        rt_k_frame = self._today_frame([
            {"ts_code": "600519.SH", "close": 1341.0, "trade_time": "2026-08-14 16:29:43"},  # 昨日
        ])
        etf_frame = self._today_frame([
            {"ts_code": "159941.SZ", "close": 1.7, "trade_time": "2026-08-14 17:00:12"},     # 昨日
        ])
        with self._mock_service(rt_k_frame, etf_frame):
            result = asyncio.run(_fetch_tushare_realtime_quotes(
                ["159941.SZ", "600519.SH"], today=TODAY,
            ))
        self.assertEqual({"159941.SZ", "600519.SH"}, set(result.keys()))


class DeferWithoutReferencePriceTest(TestCase):
    def _order(self, symbol):
        return {"symbol": symbol, "side": "BUY", "quantity": 1000, "execution_policy": {}}

    def test_defers_missing_and_keeps_priced(self):
        orders = [self._order("159941.SZ"), self._order("510300.SH")]
        kept, deferred = _defer_orders_without_reference_price(
            orders,
            reference_prices={"510300.SH": 4.7},
        )
        self.assertEqual(1, len(kept))
        self.assertEqual("510300.SH", kept[0]["symbol"])
        self.assertEqual(1, len(deferred))
        self.assertEqual("159941.SZ", deferred[0]["symbol"])
        self.assertEqual("no_reference_price", deferred[0]["reason"])

    def test_defers_all_when_reference_prices_empty(self):
        # 函数契约：无价即 defer；plan 集成层用 if reference_prices 保证
        # 整体查询失败（空 dict）时不会走到这里（见 plan 集成测试）
        orders = [self._order("159941.SZ")]
        kept, deferred = _defer_orders_without_reference_price(orders, reference_prices={})
        self.assertEqual(0, len(kept))
        self.assertEqual(1, len(deferred))
        self.assertEqual("no_reference_price", deferred[0]["reason"])


class QuoteMapTradeTimeFilterTest(TestCase):
    """longport/hub 返回的报价按时间戳过滤，避免停盘时的昨收价被当实时价。"""

    def test_keeps_today_timestamp_and_drops_yesterday_when_filter_enabled(self):
        today = date(2026, 8, 17)
        quotes = [
            {"symbol": "159941.SZ", "price": 1.70, "timestamp": "2026-08-17 10:00:00"},
            {"symbol": "510300.SH", "price": 4.70, "timestamp": "2026-08-14 16:29:58"},  # 昨日 → 丢弃
        ]
        result = _normalize_quote_map(quotes, "longport", today=today, filter_stale_quotes=True)
        self.assertEqual({"159941.SZ"}, set(result.keys()))

    def test_default_no_filter_keeps_all(self):
        today = date(2026, 8, 17)
        quotes = [
            {"symbol": "510300.SH", "price": 4.70, "timestamp": "2026-08-14 16:29:58"},  # 昨日，默认不过滤
        ]
        result = _normalize_quote_map(quotes, "longport", today=today)
        self.assertEqual({"510300.SH"}, set(result.keys()))

    def test_keeps_quote_without_timestamp(self):
        # 无时间字段的报价（如 hub 快照无时间戳）保持原样，信任源本身
        today = date(2026, 8, 17)
        quotes = [{"symbol": "510300.SH", "price": 4.70}]
        result = _normalize_quote_map(quotes, "hub", today=today, filter_stale_quotes=True)
        self.assertEqual({"510300.SH"}, set(result.keys()))

    def test_epoch_seconds_timestamp_filtered(self):
        today = date(2026, 8, 17)
        # epoch 秒：用 UTC 时刻构造，测试 int 解析
        from datetime import datetime, timezone
        epoch_today = int(datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc).timestamp())
        epoch_yesterday = int(datetime(2026, 8, 16, 10, 0, 0, tzinfo=timezone.utc).timestamp())
        quotes = [
            {"symbol": "600519.SH", "price": 1341.0, "timestamp": epoch_today},
            {"symbol": "000001.SZ", "price": 11.1, "timestamp": epoch_yesterday},
        ]
        result = _normalize_quote_map(quotes, "longport", today=today, filter_stale_quotes=True)
        self.assertEqual({"600519.SH"}, set(result.keys()))

    def test_non_a_share_symbols_not_filtered(self):
        today = date(2026, 8, 17)
        quotes = [{"symbol": "AAPL.US", "price": 220.0, "timestamp": "2026-08-16 20:00:00"}]
        result = _normalize_quote_map(quotes, "longport", today=today, filter_stale_quotes=True)
        self.assertEqual({"AAPL.US"}, set(result.keys()))


class PlanDeferWithoutReferencePriceTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _add_account(self, db):
        db.add(ExternalTradingAccount(
            id=1, account_id="acct", name="A", identifier="b", market_type="A_STOCK",
            enabled=True, executor_lot_size=100, commission_rate_pct=0.025, min_commission=5.0,
        ))

    def _add_sub(self, db):
        db.add(ExternalTradingSubAccount(
            id=11, account_id="acct", external_trading_account_id=1, name="G",
            enabled=True, executor_lot_size=100,
        ))

    def _add_target(self, db, symbol, target_quantity):
        db.add(ExternalTradingTargetPosition(
            account_id="acct", external_trading_account_id=1, sub_account_id=11,
            symbol=symbol, target_quantity=target_quantity, reference_price=10.0,
            reference_price_source="test", status="ACTIVE",
        ))

    def test_plan_defers_symbol_without_reference_price(self):
        db = self._db_session()
        self._add_account(db)
        self._add_sub(db)
        self._add_target(db, "159941.SZ", 10000)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"159941.SZ": 0.0},  # 查询链路成功但该标的无价（未开盘）
        )

        self.assertEqual([], plan["external_orders"])
        self.assertEqual(1, len(plan["deferred"]))
        self.assertEqual("no_reference_price", plan["deferred"][0]["reason"])

    def test_plan_does_not_defer_when_reference_prices_empty(self):
        db = self._db_session()
        self._add_account(db)
        self._add_sub(db)
        self._add_target(db, "159941.SZ", 10000)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={},  # 整体查询失败（异常路径）→ 保持现状照常提交
        )

        self.assertEqual(1, len(plan["external_orders"]))
        self.assertEqual([], plan["deferred"])

    def test_plan_keeps_priced_symbol_and_defers_unpriced(self):
        db = self._db_session()
        self._add_account(db)
        self._add_sub(db)
        self._add_target(db, "510300.SH", 10000)
        self._add_target(db, "159941.SZ", 10000)
        db.commit()

        plan = build_netted_target_execution_plan(
            db,
            account_id="acct",
            external_trading_account_id=1,
            reference_prices={"510300.SH": 4.7},
        )

        orders = {o["symbol"]: o for o in plan["external_orders"]}
        self.assertEqual({"510300.SH"}, set(orders.keys()))
        self.assertEqual(1, len(plan["deferred"]))
        self.assertEqual("159941.SZ", plan["deferred"][0]["symbol"])
        self.assertEqual("no_reference_price", plan["deferred"][0]["reason"])
