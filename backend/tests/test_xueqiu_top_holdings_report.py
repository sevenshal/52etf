import asyncio
from datetime import datetime
import os
import tempfile
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base, XueqiuCubeRankCache
from src.core.duckdb_utils import connect_duckdb
from src.robot.xueqiu_top_holdings_report import (
    CubeInfo,
    CubeCurrentResult,
    XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE,
    build_equal_top10_top12_buffer_plan,
    build_rebalance_payload,
    describe_rebalance_quote_rejection,
    load_cached_year_top_cubes,
    rounded_rebalance_weights,
    save_xueqiu_cube_holdings_snapshots_to_duckdb,
    save_year_top_cubes,
)


class XueqiuTopHoldingsReportTest(TestCase):
    def test_save_year_top_cubes_deduplicates_symbols_and_reassigns_ranks(self):
        with TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{tmpdir}/rank_cache.db")
            Base.metadata.create_all(engine, tables=[XueqiuCubeRankCache.__table__])
            session_factory = sessionmaker(bind=engine)
            fetched_at = datetime(2026, 5, 30, 15, 0, 0)

            cubes = [
                CubeInfo(year_rank=1, symbol="ZH000001", cube_name="best-symbol"),
                CubeInfo(year_rank=2, symbol="ZH000002", cube_name="second"),
                CubeInfo(year_rank=4, symbol="ZH000001", cube_name="duplicate-symbol"),
                CubeInfo(year_rank=0, symbol="ZH000003", cube_name="missing-rank"),
                CubeInfo(year_rank=2, symbol="ZH000004", cube_name="duplicate-rank"),
            ]

            with patch("src.robot.xueqiu_top_holdings_report.SessionLocal", session_factory):
                save_year_top_cubes(cubes, fetched_at)
                loaded, cached_at = load_cached_year_top_cubes(limit=10, max_age_days=30)

            self.assertEqual(fetched_at, cached_at)
            self.assertEqual(["ZH000001", "ZH000002", "ZH000004", "ZH000003"], [cube.symbol for cube in loaded])
            self.assertEqual([1, 2, 3, 4], [cube.year_rank for cube in loaded])
            self.assertEqual("best-symbol", loaded[0].cube_name)
            self.assertEqual("duplicate-rank", loaded[2].cube_name)

    def test_buffer_plan_keeps_retained_weights_and_allocates_sold_weight_to_new_buy(self):
        ranking_symbols = [
            ("SZ.300757", "罗博特科"),
            ("SZ.002384", "东山精密"),
            ("SZ.300394", "天孚通信"),
            ("SZ.300502", "新易盛"),
            ("SH.603083", "剑桥科技"),
            ("SZ.300308", "中际旭创"),
            ("SZ.300476", "胜宏科技"),
            ("SH.603986", "兆易创新"),
            ("SH.688008", "澜起科技"),
            ("SH.601138", "工业富联"),
            ("SZ.000001", "样例一"),
            ("SZ.000002", "样例二"),
        ]
        ranking = [
            {
                "stock_symbol": symbol,
                "stock_name": name,
                "composite_weight_pct": 12.0 - index,
            }
            for index, (symbol, name) in enumerate(ranking_symbols)
        ]
        current_holdings = [
            {"stock_symbol": "SZ300757", "stock_name": "罗博特科", "weight": 10.13},
            {"stock_symbol": "SZ002384", "stock_name": "东山精密", "weight": 10.01},
            {"stock_symbol": "SZ300394", "stock_name": "天孚通信", "weight": 10.48},
            {"stock_symbol": "SZ300502", "stock_name": "新易盛", "weight": 10.16},
            {"stock_symbol": "SZ300136", "stock_name": "信维通信", "weight": 8.65},
            {"stock_symbol": "SH603083", "stock_name": "剑桥科技", "weight": 10.50},
            {"stock_symbol": "SZ300308", "stock_name": "中际旭创", "weight": 10.06},
            {"stock_symbol": "SZ300476", "stock_name": "胜宏科技", "weight": 10.15},
            {"stock_symbol": "SH603986", "stock_name": "兆易创新", "weight": 9.94},
            {"stock_symbol": "SH688008", "stock_name": "澜起科技", "weight": 9.91},
        ]

        plan = build_equal_top10_top12_buffer_plan(
            ranking=ranking,
            current_holdings=current_holdings,
            top_n=10,
            sell_rank=12,
        )
        rounded_items = rounded_rebalance_weights(plan["target_items"])
        weights = {
            item["stock_symbol"]: item["rebalance_weight_pct"]
            for item in rounded_items
        }

        self.assertTrue(plan["component_changed"])
        self.assertEqual(["SZ.300136"], plan["removed_symbols"])
        self.assertEqual(["SH.601138"], plan["added_symbols"])
        self.assertEqual(100.0, round(sum(weights.values()), 2))
        self.assertEqual(10.13, weights["SZ.300757"])
        self.assertEqual(10.01, weights["SZ.002384"])
        self.assertEqual(10.48, weights["SZ.300394"])
        self.assertEqual(10.50, weights["SH.603083"])
        self.assertEqual(9.91, weights["SH.688008"])
        self.assertEqual(8.66, weights["SH.601138"])

    def test_buffer_plan_adjusts_retained_weight_only_when_it_exceeds_tolerance(self):
        ranking_symbols = [
            "SZ.300757",
            "SZ.002384",
            "SZ.300394",
            "SZ.300502",
            "SH.603083",
            "SZ.300308",
            "SZ.300476",
            "SH.603986",
            "SH.688008",
            "SH.601138",
            "SZ.000001",
            "SZ.000002",
        ]
        ranking = [
            {
                "stock_symbol": symbol,
                "stock_name": symbol,
                "composite_weight_pct": 12.0 - index,
            }
            for index, symbol in enumerate(ranking_symbols)
        ]
        current_holdings = [
            {"stock_symbol": "SZ300757", "weight": 10.13},
            {"stock_symbol": "SZ002384", "weight": 10.01},
            {"stock_symbol": "SZ300394", "weight": 11.20},
            {"stock_symbol": "SZ300502", "weight": 10.16},
            {"stock_symbol": "SZ300136", "weight": 8.65},
            {"stock_symbol": "SH603083", "weight": 10.50},
            {"stock_symbol": "SZ300308", "weight": 10.06},
            {"stock_symbol": "SZ300476", "weight": 10.15},
            {"stock_symbol": "SH603986", "weight": 9.94},
            {"stock_symbol": "SH688008", "weight": 9.91},
        ]

        plan = build_equal_top10_top12_buffer_plan(
            ranking=ranking,
            current_holdings=current_holdings,
            top_n=10,
            sell_rank=12,
        )
        rounded_items = rounded_rebalance_weights(plan["target_items"])
        by_symbol = {item["stock_symbol"]: item for item in rounded_items}

        self.assertEqual("adjust", by_symbol["SZ.300394"]["strategy_action"])
        self.assertEqual(10.0, by_symbol["SZ.300394"]["rebalance_weight_pct"])
        self.assertEqual("keep", by_symbol["SZ.300757"]["strategy_action"])
        self.assertEqual(9.14, by_symbol["SH.601138"]["rebalance_weight_pct"])

    def test_build_rebalance_payload_allows_etf_quote_type_13(self):
        top_items = [
            {
                "stock_symbol": "SH.600000",
                "stock_name": "浦发银行",
                "rebalance_weight_pct": 60.0,
            },
            {
                "stock_symbol": "SH.511880",
                "stock_name": "银华日利",
                "rebalance_weight_pct": 40.0,
            },
        ]
        quotes = {
            "SH600000": {
                "price": 10.0,
                "name": "浦发银行",
                "quote": {"symbol": "SH600000", "current": 10.0, "type": 11, "status": 1},
            },
            "SH511880": {
                "price": 100.0,
                "name": "银华日利",
                "quote": {"symbol": "SH511880", "current": 100.0, "type": 13, "status": 1},
            },
        }
        metadata = {
            "SH600000": {"stock_id": 1, "stock_name": "浦发银行", "segment_name": "银行"},
            "SH511880": {"stock_id": 2, "stock_name": "银华日利", "segment_name": "货币基金"},
        }

        with patch(
            "src.robot.xueqiu_top_holdings_report.fetch_batch_quotes",
            new=AsyncMock(return_value=quotes),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.fetch_stock_metadata_map",
            new=AsyncMock(return_value=metadata),
        ):
            payload = asyncio.run(
                build_rebalance_payload(
                    cookie="xq_a_token=test;",
                    target_cube_symbol="ZH3630096",
                    target_cube_id=3664154,
                    top_items=top_items,
                )
            )

        self.assertEqual(0.0, payload["cash"])
        self.assertEqual(2, len(payload["holdings"]))
        self.assertEqual(["SH600000", "SH511880"], [row["stock_symbol"] for row in payload["holdings"]])
        self.assertEqual([60.0, 40.0], [row["weight"] for row in payload["holdings"]])
        self.assertEqual([], payload["skipped_items"])

    def test_rebalance_quote_validation_does_not_require_allowlisted_type(self):
        self.assertIsNone(
            describe_rebalance_quote_rejection(
                "SH999999",
                {"symbol": "SH999999", "current": 10.0, "type": 99, "status": 1},
            )
        )

    def test_build_rebalance_payload_skips_blocked_quote_type_into_cash(self):
        top_items = [
            {
                "stock_symbol": "SH.600000",
                "stock_name": "浦发银行",
                "rebalance_weight_pct": 60.0,
            },
            {
                "stock_symbol": "SH.204001",
                "stock_name": "GC001",
                "rebalance_weight_pct": 40.0,
            },
        ]
        quotes = {
            "SH600000": {
                "price": 10.0,
                "name": "浦发银行",
                "quote": {"symbol": "SH600000", "current": 10.0, "type": 11, "status": 1},
            },
            "SH204001": {
                "price": 1.5,
                "name": "GC001",
                "quote": {"symbol": "SH204001", "current": 1.5, "type": 17, "status": 1},
            },
        }
        metadata = {
            "SH600000": {"stock_id": 1, "stock_name": "浦发银行", "segment_name": "银行"},
            "SH204001": {"stock_id": 2, "stock_name": "GC001", "segment_name": "国债逆回购"},
        }

        with patch(
            "src.robot.xueqiu_top_holdings_report.fetch_batch_quotes",
            new=AsyncMock(return_value=quotes),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.fetch_stock_metadata_map",
            new=AsyncMock(return_value=metadata),
        ):
            payload = asyncio.run(
                build_rebalance_payload(
                    cookie="xq_a_token=test;",
                    target_cube_symbol="ZH3630096",
                    target_cube_id=3664154,
                    top_items=top_items,
                )
            )

        self.assertEqual(40.0, payload["cash"])
        self.assertEqual(1, len(payload["holdings"]))
        self.assertEqual("SH600000", payload["holdings"][0]["stock_symbol"])
        self.assertEqual(60.0, payload["holdings"][0]["weight"])
        self.assertEqual(1, len(payload["skipped_items"]))
        self.assertEqual("SH.204001", payload["skipped_items"][0]["stock_symbol"])
        self.assertEqual("quote_type=17 blocked", payload["skipped_items"][0]["rebalance_skip_reason"])
        by_symbol = {item["stock_symbol"]: item for item in payload["top_items"]}
        self.assertEqual("quote_type=17 blocked", by_symbol["SH.204001"]["rebalance_skip_reason"])
        self.assertFalse(any(row["stock_symbol"] == "SH204001" for row in payload["holdings"]))

    def test_save_xueqiu_cube_holdings_snapshots_to_duckdb_replaces_same_day_cube_snapshot(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        run_at = datetime(2026, 6, 3, 14, 40, 0)
        cube = CubeInfo(
            year_rank=1,
            symbol="ZH000001",
            cube_id=123,
            cube_name="星澜样例",
            screen_name="炼金小铁匠",
        )
        try:
            with patch("src.robot.xueqiu_top_holdings_report.ANALYTICS_DB_PATH", path):
                first_result = save_xueqiu_cube_holdings_snapshots_to_duckdb(
                    run_at=run_at,
                    active_rebalance_days=90,
                    current_results=[
                        CubeCurrentResult(
                            cube=cube,
                            holdings=[
                                {
                                    "stock_symbol": "SZ300308",
                                    "stock_name": "中际旭创",
                                    "weight": 10.5,
                                    "stock_id": 1003439,
                                    "segment_name": "通信",
                                },
                                {
                                    "stock_symbol": "SZ300502",
                                    "stock_name": "新易盛",
                                    "weight": 8.0,
                                },
                            ],
                            latest_rebalance_at=datetime(2026, 6, 2, 15, 0, 0),
                            latest_rebalance_id=456,
                            latest_rebalance_status="success",
                            holdings_source="last_success_rb",
                            active=True,
                        )
                    ],
                )
                second_result = save_xueqiu_cube_holdings_snapshots_to_duckdb(
                    run_at=run_at,
                    active_rebalance_days=90,
                    current_results=[
                        CubeCurrentResult(
                            cube=cube,
                            holdings=[
                                {
                                    "stock_symbol": "SZ300308",
                                    "stock_name": "中际旭创",
                                    "weight": 12.0,
                                },
                            ],
                            latest_rebalance_at=datetime(2026, 6, 3, 14, 30, 0),
                            latest_rebalance_id=789,
                            latest_rebalance_status="success",
                            holdings_source="last_success_rb",
                            active=True,
                        )
                    ],
                )

                connection = connect_duckdb(path, prefer_read_only=False)
                try:
                    rows = connection.execute(
                        f"""
                        SELECT
                            CAST(snapshot_date AS VARCHAR),
                            cube_symbol,
                            stock_symbol,
                            stock_name,
                            weight_pct,
                            latest_rebalance_id,
                            is_active
                        FROM {XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE}
                        ORDER BY cube_symbol, stock_symbol
                        """
                    ).fetchall()
                finally:
                    connection.close()

            self.assertEqual(2, first_result["saved_rows"])
            self.assertEqual(1, second_result["saved_rows"])
            self.assertEqual(
                [
                    (
                        "2026-06-03",
                        "ZH000001",
                        "SZ.300308",
                        "中际旭创",
                        12.0,
                        789,
                        True,
                    )
                ],
                rows,
            )
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
