from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import duckdb

from src.app.api import factor_lab


class FactorLabXueqiuTopHoldingsTest(TestCase):
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

    def test_latest_uses_active_cubes_and_synthesizes_cash(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)

            with patch.object(factor_lab, "ANALYTICS_DB_PATH", db_path):
                result = factor_lab.load_xueqiu_top_holdings_latest(active_only=True, limit=10)

        self.assertTrue(result["available"])
        self.assertEqual("2026-06-22", result["snapshot_date"])
        self.assertEqual(2, result["cube_count"])
        self.assertEqual(3, result["source_cube_count"])
        self.assertEqual(2, result["active_cube_count"])

        by_symbol = {row["stock_symbol"]: row for row in result["items"]}
        self.assertEqual(1, by_symbol["SH.600001"]["composite_rank"])
        self.assertEqual(35.0, by_symbol["SH.600001"]["composite_weight_pct"])
        self.assertEqual(30.0, by_symbol["SH.600003"]["composite_weight_pct"])
        self.assertEqual(15.0, by_symbol["CASH"]["composite_weight_pct"])
        self.assertEqual(2, by_symbol["CASH"]["holding_cube_count"])

    def test_history_returns_weight_and_rank_by_snapshot_date(self):
        with TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/analytics.duckdb"
            self._create_snapshot_db(db_path)

            with patch.object(factor_lab, "ANALYTICS_DB_PATH", db_path):
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
