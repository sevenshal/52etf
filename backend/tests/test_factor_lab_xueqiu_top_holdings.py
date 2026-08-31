from datetime import date
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import polars as pl

import duckdb

from src.app.api import xueqiu_holdings as factor_lab


class FactorLabXueqiuTopHoldingsTest(TestCase):
    def test_today_ratio_uses_snapshot_price_and_previous_close(self):
        items = [{
            "stock_symbol": "SH.600000",
            "current_price": 11.0,
            "weight_multiple_today": 2.0,
        }]
        price_frame = pl.DataFrame({
            "symbol": ["600000.SH"],
            "trade_date": [date(2026, 8, 28)],
            "close": [10.0],
        })
        with patch.object(factor_lab, "_duckdb_table_exists", return_value=True), patch.object(
            factor_lab, "_load_price_frame", return_value=price_frame
        ):
            factor_lab._attach_xueqiu_today_ratio(object(), items, date(2026, 8, 28))
        self.assertEqual(1.1, items[0]["momentum_multiple_today"])
        self.assertEqual(1.82, items[0]["weight_price_ratio_today"])

    def setUp(self):
        self.assertEqual(date(2026, 6, 25), factor_lab.XUEQIU_HOLDINGS_VALID_FROM)
        cutoff_patch = patch.object(
            factor_lab,
            "XUEQIU_HOLDINGS_VALID_FROM",
            date(2000, 1, 1),
        )
        cutoff_patch.start()
        self.addCleanup(cutoff_patch.stop)

    class _CatalogRow:
        ts_code = "885001.TI"
        name = "测试细分板块"
        index_type = "N"

    class _CatalogQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return FactorLabXueqiuTopHoldingsTest._CatalogRow()

        def all(self):
            return [FactorLabXueqiuTopHoldingsTest._CatalogRow()]

    class _CatalogSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, *_args):
            return FactorLabXueqiuTopHoldingsTest._CatalogQuery()

    def _create_snapshot_db(self, path: str) -> None:
        connection = duckdb.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE xueqiu_cube_holdings_snapshots (
                    snapshot_date DATE NOT NULL,
                    snapshot_at TIMESTAMP NOT NULL,
                    rank_type VARCHAR NOT NULL,
                    year_rank INTEGER,
                    cube_symbol VARCHAR NOT NULL,
                    cube_id BIGINT,
                    cube_name VARCHAR,
                    screen_name VARCHAR,
                    latest_rebalance_at TIMESTAMP,
                    latest_rebalance_id BIGINT,
                    latest_rebalance_status VARCHAR,
                    active_rebalance_at TIMESTAMP,
                    active_rebalance_id BIGINT,
                    active_rebalance_status VARCHAR,
                    active_rebalance_category VARCHAR,
                    active_rebalance_source VARCHAR,
                    holdings_source VARCHAR,
                    active_rebalance_days INTEGER,
                    is_active BOOLEAN,
                    stock_symbol VARCHAR NOT NULL,
                    raw_stock_symbol VARCHAR,
                    stock_name VARCHAR,
                    stock_id BIGINT,
                    segment_name VARCHAR,
                    weight_pct DOUBLE,
                    raw_holding_json VARCHAR,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE a_stock_index_weight (
                    index_code VARCHAR NOT NULL,
                    trade_date DATE NOT NULL,
                    con_code VARCHAR NOT NULL,
                    weight DOUBLE
                )
                """
            )
            connection.executemany(
                "INSERT INTO a_stock_index_weight VALUES (?, ?, ?, ?)",
                [
                    ("000510.SH", "2026-06-20", "600001.SH", 1.2),
                    ("000905.SH", "2026-06-20", "600001.SH", 0.8),
                    ("000905.SH", "2026-06-20", "600002.SH", 0.7),
                ],
            )
            rows = [
                ("2026-06-21", "2026-06-21 14:50:00", 1, "ZH1", "组合一", True, "SH.600001", "SH600001", "股票A", 60.0),
                ("2026-06-21", "2026-06-21 14:50:00", 1, "ZH1", "组合一", True, "SH.600002", "SH600002", "股票B", 20.0),
                ("2026-06-21", "2026-06-21 14:50:00", 2, "ZH2", "组合二", True, "SH.600001", "SH600001", "股票A", 30.0),
                ("2026-06-22", "2026-06-22 14:50:00", 1, "ZH1", "组合一", True, "SH.600001", "SH600001", "股票A", 50.0),
                ("2026-06-22", "2026-06-22 14:50:00", 1, "ZH1", "组合一", True, "SH.600002", "SH600002", "股票B", 40.0),
                ("2026-06-22", "2026-06-22 14:50:00", 2, "ZH2", "组合二", True, "SH.600001", "SH600001", "股票A", 20.0),
                ("2026-06-22", "2026-06-22 14:50:00", 2, "ZH2", "组合二", True, "SH.600003", "SH600003", "股票C", 60.0),
                ("2026-06-22", "2026-06-22 14:50:00", 3, "ZH3", "组合三", False, "SH.600001", "SH600001", "股票A", 100.0),
            ]
            connection.executemany(
                """
                INSERT INTO xueqiu_cube_holdings_snapshots (
                    snapshot_date,
                    snapshot_at,
                    rank_type,
                    year_rank,
                    cube_symbol,
                    cube_id,
                    cube_name,
                    screen_name,
                    latest_rebalance_at,
                    latest_rebalance_id,
                    latest_rebalance_status,
                    active_rebalance_at,
                    active_rebalance_id,
                    active_rebalance_status,
                    active_rebalance_category,
                    active_rebalance_source,
                    holdings_source,
                    active_rebalance_days,
                    is_active,
                    stock_symbol,
                    raw_stock_symbol,
                    stock_name,
                    stock_id,
                    segment_name,
                    weight_pct,
                    raw_holding_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'year', ?, ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'current', 360, ?, ?, ?, ?, NULL, NULL, ?, '{}', ?, ?)
                """,
                [
                    (*row, "2026-06-22 15:00:00", "2026-06-22 15:00:00")
                    for row in rows
                ],
            )
        finally:
            connection.close()

    def _create_snapshot_db_with_rank_trend(self, path: str) -> None:
        connection = duckdb.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE xueqiu_cube_holdings_snapshots (
                    snapshot_date DATE NOT NULL,
                    snapshot_at TIMESTAMP NOT NULL,
                    rank_type VARCHAR NOT NULL,
                    year_rank INTEGER,
                    cube_symbol VARCHAR NOT NULL,
                    cube_id BIGINT,
                    cube_name VARCHAR,
                    screen_name VARCHAR,
                    latest_rebalance_at TIMESTAMP,
                    latest_rebalance_id BIGINT,
                    latest_rebalance_status VARCHAR,
                    active_rebalance_at TIMESTAMP,
                    active_rebalance_id BIGINT,
                    active_rebalance_status VARCHAR,
                    active_rebalance_category VARCHAR,
                    active_rebalance_source VARCHAR,
                    holdings_source VARCHAR,
                    active_rebalance_days INTEGER,
                    is_active BOOLEAN,
                    stock_symbol VARCHAR NOT NULL,
                    raw_stock_symbol VARCHAR,
                    stock_name VARCHAR,
                    stock_id BIGINT,
                    segment_name VARCHAR,
                    weight_pct DOUBLE,
                    raw_holding_json VARCHAR,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            base_rows = [
                ("2026-06-15", "SH.600001", "SH600001", "股票A", 10.0),
                ("2026-06-15", "SH.600002", "SH600002", "股票B", 60.0),
                ("2026-06-15", "SH.600003", "SH600003", "股票C", 30.0),
                ("2026-06-16", "SH.600001", "SH600001", "股票A", 20.0),
                ("2026-06-16", "SH.600002", "SH600002", "股票B", 55.0),
                ("2026-06-16", "SH.600003", "SH600003", "股票C", 25.0),
                ("2026-06-17", "SH.600001", "SH600001", "股票A", 30.0),
                ("2026-06-17", "SH.600002", "SH600002", "股票B", 50.0),
                ("2026-06-17", "SH.600003", "SH600003", "股票C", 20.0),
                ("2026-06-18", "SH.600001", "SH600001", "股票A", 40.0),
                ("2026-06-18", "SH.600002", "SH600002", "股票B", 35.0),
                ("2026-06-18", "SH.600003", "SH600003", "股票C", 25.0),
                ("2026-06-19", "SH.600001", "SH600001", "股票A", 45.0),
                ("2026-06-19", "SH.600002", "SH600002", "股票B", 30.0),
                ("2026-06-19", "SH.600003", "SH600003", "股票C", 25.0),
                ("2026-06-22", "SH.600001", "SH600001", "股票A", 55.0),
                ("2026-06-22", "SH.600002", "SH600002", "股票B", 25.0),
                ("2026-06-22", "SH.600003", "SH600003", "股票C", 20.0),
            ]
            rows = [
                (
                    snapshot_date,
                    f"{snapshot_date} 14:50:00",
                    1,
                    "ZH1",
                    "趋势组合",
                    True,
                    stock_symbol,
                    raw_stock_symbol,
                    stock_name,
                    weight_pct,
                )
                for snapshot_date, stock_symbol, raw_stock_symbol, stock_name, weight_pct in base_rows
            ]
            connection.executemany(
                """
                INSERT INTO xueqiu_cube_holdings_snapshots (
                    snapshot_date,
                    snapshot_at,
                    rank_type,
                    year_rank,
                    cube_symbol,
                    cube_id,
                    cube_name,
                    screen_name,
                    latest_rebalance_at,
                    latest_rebalance_id,
                    latest_rebalance_status,
                    active_rebalance_at,
                    active_rebalance_id,
                    active_rebalance_status,
                    active_rebalance_category,
                    active_rebalance_source,
                    holdings_source,
                    active_rebalance_days,
                    is_active,
                    stock_symbol,
                    raw_stock_symbol,
                    stock_name,
                    stock_id,
                    segment_name,
                    weight_pct,
                    raw_holding_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'year', ?, ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'current', 360, ?, ?, ?, ?, NULL, NULL, ?, '{}', ?, ?)
                """,
                [
                    (*row, f"{row[0]} 15:00:00", f"{row[0]} 15:00:00")
                    for row in rows
                ],
            )
        finally:
            connection.close()

    def test_latest_uses_active_cubes_and_synthesizes_cash(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_latest(active_only=True, limit=10)

        self.assertTrue(result["available"])
        self.assertEqual("2026-06-22", result["snapshot_date"])
        self.assertIsNone(result["rank_compare_snapshot_date"])
        self.assertEqual(2, result["cube_count"])
        self.assertEqual(3, result["source_cube_count"])
        self.assertEqual(2, result["active_cube_count"])

        by_symbol = {row["stock_symbol"]: row for row in result["items"]}
        self.assertEqual(1, by_symbol["SH.600001"]["composite_rank"])
        self.assertEqual(35.0, by_symbol["SH.600001"]["composite_weight_pct"])
        self.assertEqual(30.0, by_symbol["SH.600003"]["composite_weight_pct"])
        self.assertEqual(15.0, by_symbol["CASH"]["composite_weight_pct"])
        self.assertEqual(2, by_symbol["CASH"]["holding_cube_count"])
        self.assertCountEqual(
            ["000510.SH", "000905.SH"],
            [row["symbol"] for row in by_symbol["SH.600001"]["fear_indexes"]],
        )
        self.assertCountEqual(
            ["000510.SH", "000905.SH"],
            [row["symbol"] for row in result["index_options"]],
        )

    def test_latest_can_calculate_an_explicit_historical_snapshot(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_latest(
                    active_only=True,
                    limit=10,
                    snapshot_date=date(2026, 6, 21),
                )

        self.assertTrue(result["available"])
        self.assertEqual("2026-06-21", result["snapshot_date"])
        self.assertEqual(2, result["cube_count"])
        self.assertIn("SH.600001", {row["stock_symbol"] for row in result["items"]})

    def test_latest_includes_rank_change_vs_five_trading_days_ago(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db_with_rank_trend(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_latest(active_only=True, limit=10)

        self.assertTrue(result["available"])
        self.assertEqual("2026-06-22", result["snapshot_date"])
        self.assertEqual("2026-06-15", result["rank_compare_snapshot_date"])
        self.assertEqual(5, result["rank_compare_trading_days"])

        by_symbol = {row["stock_symbol"]: row for row in result["items"]}
        self.assertEqual(1, by_symbol["SH.600001"]["composite_rank"])
        self.assertEqual(3, by_symbol["SH.600001"]["rank_5d_ago"])
        self.assertEqual(2, by_symbol["SH.600001"]["rank_change_5d"])
        self.assertEqual(-1, by_symbol["SH.600002"]["rank_change_5d"])
        self.assertEqual(-1, by_symbol["SH.600003"]["rank_change_5d"])
        # Holding-cube counts for the compare date (2026-06-15) are attached too.
        self.assertEqual(1, by_symbol["SH.600001"]["cube_count_5d_ago"])
        self.assertEqual(1, by_symbol["SH.600002"]["cube_count_5d_ago"])
        self.assertEqual(1, by_symbol["SH.600003"]["cube_count_5d_ago"])
        # No price tables in this DB → momentum/ratio stay None
        self.assertEqual(10.0, by_symbol["SH.600001"]["weight_5d_ago"])
        self.assertEqual(45.0, by_symbol["SH.600001"]["weight_change_5d"])
        self.assertEqual(5.5, by_symbol["SH.600001"]["weight_multiple_5d"])
        for symbol in ("SH.600001", "SH.600002", "SH.600003"):
            self.assertIsNone(by_symbol[symbol]["momentum_5d"])
            self.assertIsNone(by_symbol[symbol]["momentum_multiple_5d"])
            self.assertIsNone(by_symbol[symbol]["weight_price_ratio_5d"])

    def test_latest_includes_weight_change_and_momentum_ratio(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db_with_rank_trend(db_path)
            self._create_price_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_latest(active_only=True, limit=10)

        self.assertTrue(result["available"])
        by_symbol = {row["stock_symbol"]: row for row in result["items"]}
        # 5-day-ago composite weights from 2026-06-15 snapshot
        self.assertEqual(10.0, by_symbol["SH.600001"]["weight_5d_ago"])
        self.assertEqual(60.0, by_symbol["SH.600002"]["weight_5d_ago"])
        self.assertEqual(30.0, by_symbol["SH.600003"]["weight_5d_ago"])
        self.assertEqual(45.0, by_symbol["SH.600001"]["weight_change_5d"])
        self.assertEqual(-35.0, by_symbol["SH.600002"]["weight_change_5d"])
        self.assertEqual(-10.0, by_symbol["SH.600003"]["weight_change_5d"])
        # weight multiple = current weight / weight 5 trading days ago
        self.assertEqual(5.5, by_symbol["SH.600001"]["weight_multiple_5d"])  # 55/10
        self.assertEqual(0.417, by_symbol["SH.600002"]["weight_multiple_5d"])  # 25/60
        self.assertEqual(0.667, by_symbol["SH.600003"]["weight_multiple_5d"])  # 20/30
        # momentum = close(2026-06-22) / close(2026-06-15) - 1, in pct
        self.assertEqual(-10.0, by_symbol["SH.600001"]["momentum_5d"])  # 90/100-1
        self.assertEqual(10.0, by_symbol["SH.600002"]["momentum_5d"])  # 110/100-1
        self.assertIsNone(by_symbol["SH.600003"]["momentum_5d"])  # no price data
        # price multiple = close(latest) / close(compare)
        self.assertEqual(0.9, by_symbol["SH.600001"]["momentum_multiple_5d"])
        self.assertEqual(1.1, by_symbol["SH.600002"]["momentum_multiple_5d"])
        self.assertIsNone(by_symbol["SH.600003"]["momentum_multiple_5d"])
        # ratio = weight multiple / price multiple; ≈1 means weight rise is price-driven
        self.assertEqual(6.11, by_symbol["SH.600001"]["weight_price_ratio_5d"])  # 5.5/0.9, 权重升但股价跌
        self.assertEqual(0.38, by_symbol["SH.600002"]["weight_price_ratio_5d"])  # 0.4167/1.1, 权重降股价升
        self.assertIsNone(by_symbol["SH.600003"]["weight_price_ratio_5d"])
        # deterministic direction labels (weight up>5% AND ratio>1.05, or weight down>5% AND ratio<0.95)
        self.assertEqual("逆势吸筹", by_symbol["SH.600001"]["direction"])  # 权重升 5.5x + 股价跌 0.9x
        self.assertEqual("借涨减仓", by_symbol["SH.600002"]["direction"])  # 权重降 0.417x + 股价升 1.1x
        self.assertEqual("持平", by_symbol["SH.600003"]["direction"])  # 无价格数据兑底

    def test_latest_weight_change_null_for_new_entries(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)
            self._create_price_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_latest(active_only=True, limit=10)

        self.assertTrue(result["available"])
        self.assertIsNone(result["rank_compare_snapshot_date"])  # < 5 trading days of history
        by_symbol = {row["stock_symbol"]: row for row in result["items"]}
        self.assertIsNone(by_symbol["SH.600001"]["weight_5d_ago"])
        self.assertIsNone(by_symbol["SH.600001"]["weight_change_5d"])
        self.assertIsNone(by_symbol["SH.600001"]["weight_multiple_5d"])
        self.assertIsNone(by_symbol["SH.600001"]["momentum_5d"])
        self.assertIsNone(by_symbol["SH.600001"]["momentum_multiple_5d"])
        self.assertIsNone(by_symbol["SH.600001"]["weight_price_ratio_5d"])
        self.assertIsNone(by_symbol["SH.600001"]["cube_count_5d_ago"])
        self.assertEqual("新进", by_symbol["SH.600001"]["direction"])  # 无5日前权重 = 新进

    def test_direction_classification_is_deterministic(self):
        direction = factor_lab._xueqiu_direction
        cases = [
            # (weight_gain, price_gain, ratio, expected)
            (None, 1.02, None, "新进"),
            (1.4, 1.05, 1.33, "顺势加仓"),
            (1.2, 0.9, 1.33, "逆势吸筹"),
            (10.0, 1.2, 10.0, "顺势加仓"),
            (0.9, 1.0, 0.9, "借涨减仓"),
            (0.72, 0.9, 0.8, "减仓"),
            (0.5, 1.3, 0.38, "借涨减仓"),
            # 双条件缺一不可 -> 持平
            (1.5, 1.5, 1.0, "持平"),
            (1.06, 1.0, 1.04, "持平"),
            (1.05, 1.0, 1.05, "持平"),
            (1.0, 1.1, 0.91, "持平"),
            (0.96, 0.9, 1.07, "持平"),
            (1.2, None, None, "持平"),
        ]
        for weight, price, ratio, expected in cases:
            self.assertEqual(expected, direction(weight, price, ratio), (weight, price, ratio))

    def test_latest_cube_count_5d_ago_null_for_stock_new_on_latest_date(self):
        """A stock held only on the latest snapshot keeps current cube count but
        exposes None cube_count_5d_ago / weight / rank change fields."""
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db_with_new_entry(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_latest(active_only=True, limit=10)

        self.assertTrue(result["available"])
        self.assertEqual("2026-06-22", result["snapshot_date"])
        self.assertEqual("2026-06-15", result["rank_compare_snapshot_date"])
        by_symbol = {row["stock_symbol"]: row for row in result["items"]}
        # 600003 appears only on the latest date -> new entry
        self.assertEqual(2, by_symbol["SH.600003"]["holding_cube_count"])
        self.assertIsNone(by_symbol["SH.600003"]["cube_count_5d_ago"])
        self.assertIsNone(by_symbol["SH.600003"]["weight_5d_ago"])
        self.assertIsNone(by_symbol["SH.600003"]["rank_5d_ago"])
        # 600001/600002 were held by two cubes on the compare date (ZH1+ZH2),
        # only one cube on the latest date -> 5d-ago cube count present.
        self.assertEqual(1, by_symbol["SH.600001"]["holding_cube_count"])
        self.assertEqual(2, by_symbol["SH.600001"]["cube_count_5d_ago"])
        self.assertEqual(1, by_symbol["SH.600002"]["holding_cube_count"])
        self.assertEqual(2, by_symbol["SH.600002"]["cube_count_5d_ago"])

    def _create_snapshot_db_with_new_entry(self, path: str) -> None:
        """Six snapshot dates with SH.600003 appearing only on the latest one."""
        connection = duckdb.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE xueqiu_cube_holdings_snapshots (
                    snapshot_date DATE NOT NULL,
                    snapshot_at TIMESTAMP NOT NULL,
                    rank_type VARCHAR NOT NULL,
                    year_rank INTEGER,
                    cube_symbol VARCHAR NOT NULL,
                    cube_id BIGINT,
                    cube_name VARCHAR,
                    screen_name VARCHAR,
                    latest_rebalance_at TIMESTAMP,
                    latest_rebalance_id BIGINT,
                    latest_rebalance_status VARCHAR,
                    active_rebalance_at TIMESTAMP,
                    active_rebalance_id BIGINT,
                    active_rebalance_status VARCHAR,
                    active_rebalance_category VARCHAR,
                    active_rebalance_source VARCHAR,
                    holdings_source VARCHAR,
                    active_rebalance_days INTEGER,
                    is_active BOOLEAN,
                    stock_symbol VARCHAR NOT NULL,
                    raw_stock_symbol VARCHAR,
                    stock_name VARCHAR,
                    stock_id BIGINT,
                    segment_name VARCHAR,
                    weight_pct DOUBLE,
                    raw_holding_json VARCHAR,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            base_rows = [
                ("2026-06-15", "ZH1", "SH.600001", "SH600001", "股票A", 50.0),
                ("2026-06-15", "ZH1", "SH.600002", "SH600002", "股票B", 50.0),
                ("2026-06-15", "ZH2", "SH.600001", "SH600001", "股票A", 30.0),
                ("2026-06-15", "ZH2", "SH.600002", "SH600002", "股票B", 70.0),
                ("2026-06-16", "ZH1", "SH.600001", "SH600001", "股票A", 50.0),
                ("2026-06-16", "ZH1", "SH.600002", "SH600002", "股票B", 50.0),
                ("2026-06-17", "ZH1", "SH.600001", "SH600001", "股票A", 50.0),
                ("2026-06-17", "ZH1", "SH.600002", "SH600002", "股票B", 50.0),
                ("2026-06-18", "ZH1", "SH.600001", "SH600001", "股票A", 50.0),
                ("2026-06-18", "ZH1", "SH.600002", "SH600002", "股票B", 50.0),
                ("2026-06-19", "ZH1", "SH.600001", "SH600001", "股票A", 50.0),
                ("2026-06-19", "ZH1", "SH.600002", "SH600002", "股票B", 50.0),
                ("2026-06-22", "ZH1", "SH.600001", "SH600001", "股票A", 40.0),
                ("2026-06-22", "ZH1", "SH.600002", "SH600002", "股票B", 40.0),
                ("2026-06-22", "ZH1", "SH.600003", "SH600003", "股票C", 20.0),
                ("2026-06-22", "ZH2", "SH.600003", "SH600003", "股票C", 30.0),
            ]
            rows = [
                (
                    snapshot_date,
                    f"{snapshot_date} 14:50:00",
                    1,
                    cube_symbol,
                    "趋势组合",
                    True,
                    stock_symbol,
                    raw_stock_symbol,
                    stock_name,
                    weight_pct,
                )
                for snapshot_date, cube_symbol, stock_symbol, raw_stock_symbol, stock_name, weight_pct in base_rows
            ]
            connection.executemany(
                """
                INSERT INTO xueqiu_cube_holdings_snapshots (
                    snapshot_date,
                    snapshot_at,
                    rank_type,
                    year_rank,
                    cube_symbol,
                    cube_id,
                    cube_name,
                    screen_name,
                    latest_rebalance_at,
                    latest_rebalance_id,
                    latest_rebalance_status,
                    active_rebalance_at,
                    active_rebalance_id,
                    active_rebalance_status,
                    active_rebalance_category,
                    active_rebalance_source,
                    holdings_source,
                    active_rebalance_days,
                    is_active,
                    stock_symbol,
                    raw_stock_symbol,
                    stock_name,
                    stock_id,
                    segment_name,
                    weight_pct,
                    raw_holding_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'year', ?, ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'current', 360, ?, ?, ?, ?, NULL, NULL, ?, '{}', ?, ?)
                """,
                [
                    (*row, f"{row[0]} 15:00:00", f"{row[0]} 15:00:00")
                    for row in rows
                ],
            )
        finally:
            connection.close()


    def _create_price_db(self, path: str) -> None:
        connection = duckdb.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE a_stock_market_daily_qfq (
                    ts_code VARCHAR NOT NULL,
                    trade_date DATE NOT NULL,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    vol DOUBLE,
                    amount DOUBLE
                )
                """
            )
            connection.executemany(
                "INSERT INTO a_stock_market_daily_qfq VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("600001.SH", "2026-06-15", 100.0, 100.0, 100.0, 100.0, 0.0, 0.0),
                    ("600001.SH", "2026-06-22", 90.0, 90.0, 90.0, 90.0, 0.0, 0.0),
                    ("600002.SH", "2026-06-15", 100.0, 100.0, 100.0, 100.0, 0.0, 0.0),
                    ("600002.SH", "2026-06-22", 110.0, 110.0, 110.0, 110.0, 0.0, 0.0),
                ],
            )
        finally:
            connection.close()

    def test_history_returns_weight_and_rank_by_snapshot_date(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_history(
                    symbol="SH600001",
                    active_only=True,
                    limit=10,
                )

        self.assertTrue(result["available"])
        self.assertEqual("SH.600001", result["symbol"])
        self.assertEqual(["2026-06-21", "2026-06-22"], [row["snapshot_date"] for row in result["history"]])
        self.assertEqual([45.0, 35.0], [row["composite_weight_pct"] for row in result["history"]])
        self.assertEqual([1, 1], [row["composite_rank"] for row in result["history"]])
        self.assertEqual("2026-06-22", result["latest"]["snapshot_date"])

    def test_history_returns_latest_ohlc_on_or_before_snapshot_date(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)
            self._create_price_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_history(
                    symbol="SH600001",
                    active_only=True,
                    limit=10,
                )

        self.assertEqual([100.0, 90.0], [row["close_price"] for row in result["history"]])
        self.assertEqual([100.0, 90.0], [row["open_price"] for row in result["history"]])
        self.assertEqual([100.0, 90.0], [row["high_price"] for row in result["history"]])
        self.assertEqual([100.0, 90.0], [row["low_price"] for row in result["history"]])
        self.assertEqual(90.0, result["latest"]["close_price"])

    def test_details_returns_holding_cubes_for_symbol_and_snapshot_date(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holding_details(
                    symbol="SH600001",
                    snapshot_date=date(2026, 6, 22),
                    active_only=True,
                    limit=1,
                )

        self.assertTrue(result["available"])
        self.assertEqual("SH.600001", result["symbol"])
        self.assertEqual("2026-06-22", result["snapshot_date"])
        self.assertEqual(2, result["cube_count"])
        self.assertEqual(2, result["holding_cube_count"])
        self.assertEqual(70.0, result["total_weight_pct"])
        self.assertEqual(35.0, result["average_weight_pct"])
        self.assertEqual(["ZH1"], [row["cube_symbol"] for row in result["details"]])
        self.assertEqual([50.0], [row["weight_pct"] for row in result["details"]])

    def test_details_returns_synthesized_cash_by_cube(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)

            with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holding_details(
                    symbol="CASH",
                    snapshot_date=date(2026, 6, 22),
                    active_only=True,
                    limit=10,
                )

        self.assertTrue(result["available"])
        self.assertEqual("CASH", result["symbol"])
        self.assertEqual(2, result["holding_cube_count"])
        self.assertEqual(30.0, result["total_weight_pct"])
        self.assertEqual(15.0, result["average_weight_pct"])
        self.assertEqual(["ZH2", "ZH1"], [row["cube_symbol"] for row in result["details"]])
        self.assertEqual([20.0, 10.0], [row["weight_pct"] for row in result["details"]])

    def test_latest_aggregates_contrarian_ths_board(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db_with_rank_trend(db_path)
            connection = duckdb.connect(db_path)
            try:
                connection.execute(
                    "INSERT INTO xueqiu_cube_holdings_snapshots "
                    "SELECT * REPLACE ('ZH2' AS cube_symbol) "
                    "FROM xueqiu_cube_holdings_snapshots WHERE cube_symbol = 'ZH1'"
                )
                connection.execute(
                    "INSERT INTO xueqiu_cube_holdings_snapshots "
                    "SELECT * REPLACE ('ZH3' AS cube_symbol) "
                    "FROM xueqiu_cube_holdings_snapshots WHERE cube_symbol = 'ZH1'"
                )
                connection.execute(
                    "UPDATE xueqiu_cube_holdings_snapshots SET weight_pct = 60 "
                    "WHERE snapshot_date = '2026-06-22' AND stock_symbol = 'SH.600003'"
                )
                connection.execute(
                    """
                    CREATE TABLE a_stock_ths_member (
                        ths_code VARCHAR, con_code VARCHAR, con_name VARCHAR,
                        weight DOUBLE, in_date DATE, out_date DATE, is_new VARCHAR,
                        updated_at TIMESTAMP
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO a_stock_ths_member VALUES (?, ?, ?, NULL, NULL, NULL, 'Y', NOW())",
                    [
                        ("885001.TI", "600001.SH", "股票A"),
                        ("885001.TI", "600002.SH", "股票B"),
                        ("885001.TI", "600003.SH", "股票C"),
                    ],
                )
                connection.execute(
                    """
                    CREATE TABLE a_stock_ths_daily (
                        ths_code VARCHAR, trade_date DATE, open DOUBLE,
                        high DOUBLE, low DOUBLE, close DOUBLE
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO a_stock_ths_daily VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("885001.TI", "2026-06-15", 98.0, 102.0, 97.0, 100.0),
                        ("885001.TI", "2026-06-22", 95.0, 96.0, 89.0, 90.0),
                    ],
                )
                connection.execute(
                    """
                    CREATE TABLE a_stock_market_daily_qfq (
                        ts_code VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE,
                        low DOUBLE, close DOUBLE, vol DOUBLE, amount DOUBLE
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO a_stock_market_daily_qfq VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (symbol, trade_date, close, close, close, close, 1.0, 1.0)
                        for symbol in ("600001.SH", "600002.SH", "600003.SH")
                        for trade_date, close in (("2026-06-15", 100.0), ("2026-06-22", 90.0))
                    ],
                )
            finally:
                connection.close()

            with (
                patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", db_path),
                patch.object(factor_lab, "DBSession", self._CatalogSession),
                patch.object(factor_lab, "XUEQIU_CONTRARIAN_BOARD_MIN_STOCKS", 3),
            ):
                result = factor_lab.load_xueqiu_top_holdings_latest(active_only=True, limit=10)
                board_holdings = factor_lab.load_xueqiu_board_holding_symbols(
                    "885001.TI",
                    active_only=True,
                )
                board_history = factor_lab.load_xueqiu_board_history(
                    "885001.TI",
                    active_only=True,
                )

        self.assertEqual(1, len(result["board_items"]))
        board = result["board_items"][0]
        self.assertEqual("测试细分板块", board["name"])
        self.assertEqual("逆势吸筹", board["direction"])
        self.assertEqual(3, board["stock_count"])
        self.assertEqual(2, board["contrarian_stock_count"])
        self.assertEqual(66.67, board["contrarian_stock_ratio_pct"])
        self.assertGreater(board["weight_price_ratio_5d"], 1.05)
        self.assertEqual(["885001.TI"], [item["ths_code"] for item in result["contrarian_boards"]])
        self.assertEqual("测试细分板块", board_holdings["name"])
        self.assertEqual(
            ["SH.600001", "SH.600002", "SH.600003"],
            board_holdings["stock_symbols"],
        )
        board_history_rows = board_history["history"]
        self.assertEqual("2026-06-15", str(board_history_rows[0]["snapshot_date"]))
        self.assertEqual("2026-06-22", str(board_history_rows[-1]["snapshot_date"]))
        self.assertEqual(100.0, board_history_rows[0]["close_price"])
        self.assertEqual(90.0, board_history_rows[-1]["close_price"])
        self.assertEqual(98.0, board_history_rows[0]["open_price"])
        self.assertEqual(95.0, board_history_rows[-1]["open_price"])
        self.assertEqual(102.0, board_history_rows[0]["high_price"])
        self.assertEqual(96.0, board_history_rows[-1]["high_price"])
        self.assertEqual(97.0, board_history_rows[0]["low_price"])
        self.assertEqual(89.0, board_history_rows[-1]["low_price"])
        self.assertTrue(all(row["composite_weight_pct"] is not None for row in board_history_rows))
