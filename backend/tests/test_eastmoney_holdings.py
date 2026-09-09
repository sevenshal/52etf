from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from datetime import date, datetime, timedelta

import duckdb
import httpx
import polars as pl

from src.robot.eastmoney_holdings import (
    EastmoneyClient,
    EastmoneyCredentials,
    compute_eastmoney_sign,
    _ensure_schema,
    _save_rank_snapshot_and_load_rolling_pool,
    _rank_snapshot_exists,
    _complete_holdings_snapshot_exists,
    _normalize_rank_at,
    _parse_source_update_at,
    _format_eastmoney_holdings_result,
    run_eastmoney_holdings_job,
)
from src.app.api.eastmoney_holdings import (
    _attach_eastmoney_history_5d_ratios,
    _attach_eastmoney_today_ratio,
    _eastmoney_5d_ratio_fields,
)


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

    def test_history_5d_ratio_anchors_on_global_fifth_prior_snapshot(self):
        rows = [
            {"snapshot_date": "2026-09-01", "composite_weight_pct": 10.0, "close_price": 100.0},
            {"snapshot_date": "2026-09-02", "composite_weight_pct": 10.5, "close_price": 102.0},
            {"snapshot_date": "2026-09-03", "composite_weight_pct": 11.0, "close_price": 104.0},
            {"snapshot_date": "2026-09-04", "composite_weight_pct": 11.5, "close_price": 106.0},
            {"snapshot_date": "2026-09-05", "composite_weight_pct": 11.8, "close_price": 108.0},
            {
                "snapshot_date": "2026-09-08",
                "composite_weight_pct": 12.0,
                "close_price": 110.0,
                "current_price": 108.0,
            },
        ]
        global_dates = [date.fromisoformat(row["snapshot_date"]) for row in rows]
        with patch("src.app.api.eastmoney_holdings._china_today", return_value=date(2026, 9, 8)):
            _attach_eastmoney_history_5d_ratios(rows, global_dates)
        # Only the 6th row has a full 5-trading-day lookback; rows[:5] stay empty.
        self.assertTrue(all(row["weight_price_ratio_5d"] is None for row in rows[:5]))
        self.assertTrue(all(row["direction_5d"] is None for row in rows[:5]))
        newest = rows[5]
        # Anchor is 2026-09-01 (5 global steps back), current price is the 时点价 108.
        self.assertEqual(108.0, newest["price_for_ratio_5d"])
        self.assertEqual(1.2, newest["weight_multiple_5d"])  # 12.0 / 10.0
        self.assertEqual(1.08, newest["momentum_multiple_5d"])  # 108 / 100
        self.assertEqual(1.11, newest["weight_price_ratio_5d"])  # round(1.2 / 1.08, 2)
        self.assertEqual("顺势加仓", newest["direction_5d"])

    def test_history_5d_ratio_falls_back_to_row_order_without_global_dates(self):
        rows = [
            {"snapshot_date": f"2026-09-0{day}", "composite_weight_pct": 10.0 + day, "close_price": 100.0 + day}
            for day in range(1, 7)
        ]
        # today not in the rows -> newest row uses its own close_price, not 时点价.
        with patch("src.app.api.eastmoney_holdings._china_today", return_value=date(2026, 9, 9)):
            _attach_eastmoney_history_5d_ratios(rows)
        self.assertTrue(all(row["weight_price_ratio_5d"] is None for row in rows[:5]))
        # rows[5] anchors on rows[0] (5 rows back) even without a global sequence.
        expected = _eastmoney_5d_ratio_fields(16.0, 11.0, 106.0, 101.0)
        self.assertEqual(expected["weight_price_ratio_5d"], rows[5]["weight_price_ratio_5d"])
        self.assertEqual(expected["direction_5d"], rows[5]["direction_5d"])
        self.assertIsNotNone(rows[5]["weight_price_ratio_5d"])

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

    def test_rank_snapshots_keep_intraday_history_and_build_same_day_union(self):
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
            self.assertFalse(_complete_holdings_snapshot_exists(connection, first_at))
        finally:
            connection.close()

    def test_rolling_pool_uses_latest_five_distinct_rank_dates(self):
        connection = duckdb.connect(":memory:")
        try:
            _ensure_schema(connection)
            pool = []
            for day, combination_id in enumerate(range(101, 107), start=1):
                rank_at = datetime(2026, 9, day, 15, 0)
                pool = _save_rank_snapshot_and_load_rolling_pool(
                    connection,
                    rank_at=rank_at,
                    source_update_at=rank_at,
                    rankings=[{
                        "combinationId": combination_id,
                        "userName": str(combination_id),
                        "profitRate": "1",
                    }],
                    now=rank_at,
                )
            self.assertEqual({102, 103, 104, 105, 106}, {item["combinationId"] for item in pool})
        finally:
            connection.close()

    def test_schema_migrates_legacy_daily_primary_key_to_intraday_key(self):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("""
                CREATE TABLE eastmoney_cube_holdings_snapshots (
                    snapshot_date DATE NOT NULL, snapshot_at TIMESTAMP NOT NULL,
                    rank_type VARCHAR NOT NULL, cube_symbol VARCHAR NOT NULL,
                    stock_symbol VARCHAR NOT NULL, weight_pct DOUBLE,
                    created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (snapshot_date, cube_symbol, stock_symbol)
                )
            """)
            connection.execute("""
                INSERT INTO eastmoney_cube_holdings_snapshots VALUES
                ('2026-08-31', '2026-08-31 13:00:00', 'rate', '9001', 'SH.600000', 10, NOW(), NOW())
            """)
            _ensure_schema(connection)
            primary_key_columns = [
                row[1]
                for row in sorted(
                    connection.execute("PRAGMA table_info('eastmoney_cube_holdings_snapshots')").fetchall(),
                    key=lambda row: row[5],
                )
                if row[5]
            ]
            self.assertEqual(["snapshot_at", "cube_symbol", "stock_symbol"], primary_key_columns)
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM eastmoney_cube_holdings_snapshots").fetchone()[0])
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


class _ReusableConnection:
    """任务里每一段都会 close 连接，测试要在这些段之间复用同一个内存库。"""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        pass


class EastmoneyHoldingsResilienceTest(IsolatedAsyncioTestCase):
    """外部 HTTP 抖动不该带走整轮快照。"""

    def setUp(self):
        self.client = EastmoneyClient(EastmoneyCredentials("ct", "ut", "user", "device"))

    async def asyncTearDown(self):
        await self.client.client.aclose()

    async def test_transport_error_is_retried_before_failing_the_combination(self):
        ok = AsyncMock()
        ok.raise_for_status = lambda: None
        ok.json = lambda: {"code": 0, "data": [{"BlockName": "半导体", "data": [{"__code": "688001"}]}]}
        self.client.client.post = AsyncMock(
            side_effect=[httpx.ReadTimeout("timeout"), httpx.ConnectError("reset"), ok]
        )
        with patch("src.robot.eastmoney_holdings.asyncio.sleep", new=AsyncMock()):
            rows = await self.client.fetch_holdings(900000001)
        self.assertEqual("半导体", rows[0]["segment_name"])
        self.assertEqual(3, self.client.client.post.await_count)

    async def test_client_error_is_not_retried(self):
        response = httpx.Response(403, request=httpx.Request("POST", "https://example.com"))
        failing = AsyncMock()
        failing.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        )
        self.client.client.post = AsyncMock(return_value=failing)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.client.fetch_holdings(900000001)
        self.assertEqual(1, self.client.client.post.await_count)

    def test_partial_failure_keeps_the_snapshot_and_fronts_the_warning(self):
        message = _format_eastmoney_holdings_result({
            "snapshot_at": "2026-09-08 10:30:00", "rank_count": 200,
            "rolling_combination_count": 139, "holding_rows": 1200,
            "failed_count": 14, "warning": "东方财富持仓失败偏多 14/139", "written": True,
        })
        self.assertTrue(message.startswith("[告警] 东方财富持仓失败偏多 14/139"))
        self.assertIn("rows=1200", message)

    def test_degraded_run_does_not_overwrite_a_more_complete_snapshot(self):
        message = _format_eastmoney_holdings_result({
            "snapshot_at": "2026-09-08 10:30:00", "rank_count": 200,
            "rolling_combination_count": 139, "holding_rows": 10,
            "failed_count": 100, "warning": None, "written": False,
        })
        self.assertIn("同时点已有更完整快照", message[:96])

    async def test_job_keeps_the_snapshot_when_a_tenth_of_the_combinations_fail(self):
        connection = _ReusableConnection(duckdb.connect(":memory:"))
        combinations = [{"combinationId": index, "userName": f"u{index}"} for index in range(1, 140)]

        async def fetch_holdings(combination_id):
            if combination_id <= 14:
                raise httpx.ReadTimeout("timeout")
            return [{"__code": "600000", "market": "1", "positionRateDetail": "10", "__zxjg": "9.9"}]

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.fetch_rank_page.return_value = {
            "data": {"updateTime": "2026-09-08 10:31:02", "totalPages": 1, "pages": combinations}
        }
        client.fetch_holdings.side_effect = fetch_holdings
        with patch("src.robot.eastmoney_holdings._load_credentials", return_value=None), patch(
            "src.robot.eastmoney_holdings.EastmoneyClient", return_value=client
        ), patch("src.robot.eastmoney_holdings.connect_duckdb", return_value=connection):
            result = await run_eastmoney_holdings_job(force=True)

        # 14/139 超过一成的容忍度，但抓到的 125 个组合必须落库，任务不能整体判失败。
        self.assertEqual(14, result["failed_count"])
        self.assertTrue(result["written"])
        self.assertIn("14/139", result["warning"])
        self.assertEqual(125, connection.execute(
            "SELECT COUNT(DISTINCT cube_symbol) FROM eastmoney_cube_holdings_snapshots"
        ).fetchone()[0])

    async def test_job_still_fails_when_every_combination_fails(self):
        connection = _ReusableConnection(duckdb.connect(":memory:"))
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.fetch_rank_page.return_value = {
            "data": {
                "updateTime": "2026-09-08 10:31:02", "totalPages": 1,
                "pages": [{"combinationId": index, "userName": "u"} for index in range(1, 21)],
            }
        }
        client.fetch_holdings.side_effect = RuntimeError("东方财富接口失败: -1 凭据过期")
        with patch("src.robot.eastmoney_holdings._load_credentials", return_value=None), patch(
            "src.robot.eastmoney_holdings.EastmoneyClient", return_value=client
        ), patch("src.robot.eastmoney_holdings.connect_duckdb", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "东方财富持仓全部失败"):
                await run_eastmoney_holdings_job(force=True)
