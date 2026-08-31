from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta

import duckdb
import polars as pl

from src.robot.eastmoney_holdings import (
    EastmoneyClient,
    EastmoneyCredentials,
    compute_eastmoney_sign,
    _ensure_schema,
    _save_rank_snapshot_and_load_rolling_pool,
    _rank_snapshot_exists,
    _normalize_rank_at,
    _parse_source_update_at,
)
from src.app.api.eastmoney_holdings import _attach_eastmoney_today_ratio


class EastmoneyHoldingsTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = EastmoneyClient(EastmoneyCredentials("ct", "ut", "user", "device"))

    async def asyncTearDown(self):
        await self.client.client.aclose()

    def test_sign_matches_reference_implementation(self):
        envelope = {"args": {"z": "ABC", "a": True}, "method": "Demo", "timestamp": 123}
        self.assertEqual(
            "bc32ae80c59c775074300b4739e7095383b25342f27f4f4f93c741622ad18215",
            compute_eastmoney_sign(envelope),
        )

    async def test_holdings_are_flattened_with_segment(self):
        self.client._post = AsyncMock(return_value={
            "code": 0,
            "data": [{"BlockName": "半导体", "data": [{"__code": "688001"}]}],
        })
        rows = await self.client.fetch_holdings(900000001)
        self.assertEqual("半导体", rows[0]["segment_name"])

    async def test_missing_follow_permission_follows_then_retries(self):
        self.client._post = AsyncMock(side_effect=[RuntimeError("请先关注该组合"), {"code": 0, "data": []}])
        self.client.follow_combination = AsyncMock()
        rows = await self.client.fetch_holdings(900000001)
        self.assertEqual([], rows)
        self.client.follow_combination.assert_awaited_once_with(900000001)
        self.assertEqual(2, self.client._post.await_count)

    async def test_follow_accepts_eastmoney_success_message_with_nonzero_code(self):
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.json = lambda: {"code": -1, "message": "加自选成功"}
        self.client.client.get = AsyncMock(return_value=response)
        await self.client.follow_combination(900000001)

    def test_rank_snapshots_keep_intraday_history_and_build_30_day_union(self):
        connection = duckdb.connect(":memory:")
        try:
            _ensure_schema(connection)
            first_at = datetime(2026, 8, 31, 10, 0)
            first = _save_rank_snapshot_and_load_rolling_pool(
                connection,
                rank_at=first_at,
                source_update_at=first_at,
                rankings=[{"combinationId": 1, "userName": "一", "profitRate": "1"}],
                now=first_at,
            )
            second = _save_rank_snapshot_and_load_rolling_pool(
                connection,
                rank_at=first_at + timedelta(minutes=30),
                source_update_at=first_at + timedelta(minutes=30, seconds=42),
                rankings=[{"combinationId": 2, "userName": "二", "profitRate": "2"}],
                now=first_at + timedelta(minutes=30),
            )
            self.assertEqual([1], [item["combinationId"] for item in first])
            self.assertEqual({1, 2}, {item["combinationId"] for item in second})
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM eastmoney_rank_snapshots").fetchone()[0])
            self.assertTrue(_rank_snapshot_exists(connection, first_at))
            self.assertFalse(_rank_snapshot_exists(connection, first_at + timedelta(minutes=15)))
        finally:
            connection.close()

    def test_api_update_time_preserves_source_and_normalizes_market_breaks(self):
        fallback = datetime(2026, 8, 31, 16, 2)
        self.assertEqual(
            datetime(2026, 8, 31, 16, 0, 32),
            _parse_source_update_at("2026-08-31 16:00:32", fallback),
        )
        self.assertEqual(
            datetime(2026, 8, 31, 15, 0),
            _normalize_rank_at(datetime(2026, 8, 31, 16, 0, 32)),
        )
        self.assertEqual(
            datetime(2026, 8, 31, 11, 30),
            _normalize_rank_at(datetime(2026, 8, 31, 12, 35, 18)),
        )
        self.assertEqual(
            datetime(2026, 8, 31, 14, 30),
            _normalize_rank_at(datetime(2026, 8, 31, 14, 47, 12)),
        )

    def test_today_ratio_uses_snapshot_realtime_price_and_previous_close(self):
        items = [{
            "stock_symbol": "SH.600000",
            "current_price": 11.0,
            "weight_multiple_today": 2.0,
        }]
        price_frame = pl.DataFrame({
            "symbol": ["600000.SH"],
            "trade_date": [datetime(2026, 9, 1).date()],
            "close": [10.0],
        })
        with patch("src.app.api.eastmoney_holdings._duckdb_table_exists", return_value=True), patch(
            "src.app.api.eastmoney_holdings._load_price_frame", return_value=price_frame
        ):
            _attach_eastmoney_today_ratio(object(), items, datetime(2026, 9, 1).date())
        self.assertEqual(1.1, items[0]["momentum_multiple_today"])
        self.assertEqual(1.82, items[0]["weight_price_ratio_today"])
