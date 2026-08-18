import asyncio
from datetime import date, datetime
import os
import tempfile
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base, XueqiuCubeActivityCache, XueqiuCubeRankCache, XueqiuTopHoldingsRun
from src.core.duckdb_utils import connect_duckdb
from src.robot.xueqiu_top_holdings_report import (
    CubeInfo,
    CubeActivityResult,
    CubeCurrentResult,
    CubeFetchResult,
    ACTIVE_REBALANCE_ACTIVITY_TYPE,
    XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE,
    XUEQIU_CUBE_RANK_HISTORY_TABLE,
    aggregate_holdings,
    append_rank_acceleration_email_section,
    append_weight_price_ratio_email_section,
    build_equal_top10_top12_buffer_plan,
    build_rank_acceleration_buffer_plan,
    build_weight_price_ratio_buffer_plan,
    build_report,
    build_report_html,
    build_rebalance_payload,
    describe_rebalance_quote_rejection,
    ensure_xueqiu_current_fetch_quality,
    execute_rank_acceleration_target_rebalance,
    execute_weight_price_ratio_target_rebalance,
    fetch_cube_manager_activity,
    latest_manager_rebalance_from_events,
    load_cached_cube_activity,
    load_or_refresh_year_top_cubes,
    load_cached_year_top_cubes,
    load_recent_xueqiu_rank_history_cube_sets,
    load_rank_acceleration_strategy_history,
    load_xueqiu_rank_comparison_snapshot,
    load_xueqiu_rank_drift_baselines,
    manager_rebalance_from_show_origin,
    rounded_rebalance_weights,
    process_xueqiu_top_holdings_rebalance_for_robot,
    resolve_fear_greed_target_count,
    resolve_xueqiu_strategy_position_target,
    save_cube_activity_cache,
    save_xueqiu_cube_rank_history_to_duckdb,
    save_xueqiu_cube_holdings_snapshots_to_duckdb,
    save_year_top_cubes,
    validate_xueqiu_rank_cache_drift,
    WEIGHT_PRICE_RATIO_STRATEGY_NAME,
    WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL,
)


class XueqiuTopHoldingsReportTest(TestCase):
    def test_fear_greed_regime_selects_target_position_count_at_boundaries(self):
        self.assertEqual((10, "fear"), resolve_fear_greed_target_count({"score": 24.99}))
        self.assertEqual(
            (3, "neutral_keep_3"),
            resolve_fear_greed_target_count({"score": 25}, current_holding_count=3),
        )
        self.assertEqual(
            (10, "neutral_keep_10"),
            resolve_fear_greed_target_count({"score": 75}, current_holding_count=10),
        )
        self.assertEqual(
            (10, "neutral_keep_10"),
            resolve_fear_greed_target_count({"score": 50}, current_holding_count=7),
        )
        self.assertEqual((3, "greed"), resolve_fear_greed_target_count({"score": 75.01}))
        self.assertEqual((10, "missing_fallback"), resolve_fear_greed_target_count(None))

    def test_position_target_volume_bottom_requires_fear_plus_volume_expansion(self):
        # 量能底：恐贪≤30 且放量（z>1.25）→ 扩仓到10只
        self.assertEqual(
            (10, "volume_bottom"),
            resolve_xueqiu_strategy_position_target(
                {"score": 20.0, "log_volume_z": 1.5},
                current_holding_count=3,
            ),
        )
        # 恐贪≤30 但缩量 → 无信号，维持当前3只
        self.assertEqual(
            (3, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 20.0, "log_volume_z": -1.5},
                current_holding_count=3,
            ),
        )
        # 恐贪≤30 但放量不足（z=0.5 < 1.25）→ 维持
        self.assertEqual(
            (3, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 20.0, "log_volume_z": 0.5},
                current_holding_count=3,
            ),
        )
        # 无 log量比 → 无信号，维持当前
        self.assertEqual(
            (3, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 20.0},
                current_holding_count=3,
            ),
        )

    def test_position_target_volume_top_requires_greed_plus_volume_shrink(self):
        # 量能顶：恐贪≥75 且缩量（z<-0.25）→ 收缩到3只
        self.assertEqual(
            (3, "volume_top"),
            resolve_xueqiu_strategy_position_target(
                {"score": 80.0, "log_volume_z": -1.0},
                current_holding_count=10,
            ),
        )
        # 恐贪≥75 但放量 → 无信号，维持当前10只
        self.assertEqual(
            (10, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 80.0, "log_volume_z": 1.5},
                current_holding_count=10,
            ),
        )
        # 恐贪≥75 但缩量不足（z=-0.1 > -0.25）→ 维持
        self.assertEqual(
            (10, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 80.0, "log_volume_z": -0.1},
                current_holding_count=10,
            ),
        )

    def test_position_target_ma5_cross_signals(self):
        # MA5底：MA5上穿（当日>前一日）且最近5日任意恐贪≤25
        self.assertEqual(
            (10, "ma5_bottom"),
            resolve_xueqiu_strategy_position_target(
                {
                    "score": 40.0,
                    "log_volume_z": 0.0,
                    # 最近6日：前5日低企稳，第6日(今日)跳升 → MA5上穿；窗口内含 20≤25
                    "recent_scores": [
                        {"date": "2026-07-01", "score": 20.0},
                        {"date": "2026-07-02", "score": 22.0},
                        {"date": "2026-07-03", "score": 24.0},
                        {"date": "2026-07-06", "score": 26.0},
                        {"date": "2026-07-07", "score": 28.0},
                        {"date": "2026-07-08", "score": 60.0},
                    ],
                },
                current_holding_count=3,
            ),
        )
        # MA5顶：MA5下穿（当日<前一日）且最近5日任意恐贪≥75
        self.assertEqual(
            (3, "ma5_top"),
            resolve_xueqiu_strategy_position_target(
                {
                    "score": 60.0,
                    "log_volume_z": 0.0,
                    "recent_scores": [
                        {"date": "2026-07-01", "score": 78.0},
                        {"date": "2026-07-02", "score": 76.0},
                        {"date": "2026-07-03", "score": 74.0},
                        {"date": "2026-07-06", "score": 72.0},
                        {"date": "2026-07-07", "score": 70.0},
                        {"date": "2026-07-08", "score": 40.0},
                    ],
                },
                current_holding_count=10,
            ),
        )

    def test_position_target_ma5_cross_requires_extreme_score_in_window(self):
        # MA5上穿但最近5日无 ≤25 的分数 → 无MA5底信号
        self.assertEqual(
            (5, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {
                    "score": 50.0,
                    "log_volume_z": 0.0,
                    "recent_scores": [
                        {"date": "2026-07-01", "score": 30.0},
                        {"date": "2026-07-02", "score": 32.0},
                        {"date": "2026-07-03", "score": 34.0},
                        {"date": "2026-07-06", "score": 36.0},
                        {"date": "2026-07-07", "score": 38.0},
                        {"date": "2026-07-08", "score": 60.0},
                    ],
                },
                current_holding_count=5,
            ),
        )
        # 最近5日有 ≤25 但 MA5 没有上穿（走平）→ 无信号
        self.assertEqual(
            (5, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {
                    "score": 40.0,
                    "log_volume_z": 0.0,
                    "recent_scores": [
                        {"date": "2026-07-01", "score": 20.0},
                        {"date": "2026-07-02", "score": 30.0},
                        {"date": "2026-07-03", "score": 40.0},
                        {"date": "2026-07-06", "score": 50.0},
                        {"date": "2026-07-07", "score": 40.0},
                        {"date": "2026-07-08", "score": 40.0},
                    ],
                },
                current_holding_count=5,
            ),
        )

    def test_position_target_both_signal_types_and_keep_semantics(self):
        # 量能底 + MA5底 同时触发 → bottom_both，仍扩仓
        self.assertEqual(
            (10, "bottom_both"),
            resolve_xueqiu_strategy_position_target(
                {
                    "score": 20.0,
                    "log_volume_z": 1.5,
                    "recent_scores": [
                        {"date": "2026-07-01", "score": 20.0},
                        {"date": "2026-07-02", "score": 22.0},
                        {"date": "2026-07-03", "score": 24.0},
                        {"date": "2026-07-06", "score": 26.0},
                        {"date": "2026-07-07", "score": 28.0},
                        {"date": "2026-07-08", "score": 60.0},
                    ],
                },
                current_holding_count=3,
            ),
        )
        # 无任何信号 → 维持当前（非空仓）
        self.assertEqual(
            (7, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 50.0, "log_volume_z": 0.0},
                current_holding_count=7,
            ),
        )
        # 无信号且空仓 → 回退默认建仓数
        self.assertEqual(
            (10, "neutral_keep_default"),
            resolve_xueqiu_strategy_position_target(
                {"score": 50.0, "log_volume_z": 0.0},
                current_holding_count=0,
            ),
        )

    def test_position_target_custom_thresholds_and_fallbacks(self):
        # 自定义阈值：恐贪 28≤30 且放量 z=2.5>2.0 → 量能底
        self.assertEqual(
            (10, "volume_bottom"),
            resolve_xueqiu_strategy_position_target(
                {"score": 28.0, "log_volume_z": 2.5},
                current_holding_count=3,
                fear_threshold=30.0,
                fear_volume_std=2.0,
            ),
        )
        self.assertEqual((10, "missing_fallback"), resolve_xueqiu_strategy_position_target(None))
        self.assertEqual(
            (10, "invalid_fallback"),
            resolve_xueqiu_strategy_position_target({"score": None}),
        )

    def test_position_target_uses_configurable_position_counts(self):
        # x→y / y→x：量能底扩到 fear_target_count，量能顶收到 greed_target_count
        self.assertEqual(
            (15, "volume_bottom"),
            resolve_xueqiu_strategy_position_target(
                {"score": 20.0, "log_volume_z": 1.5},
                current_holding_count=5,
                fear_target_count=15,
                greed_target_count=5,
            ),
        )
        self.assertEqual(
            (5, "volume_top"),
            resolve_xueqiu_strategy_position_target(
                {"score": 80.0, "log_volume_z": -1.0},
                current_holding_count=15,
                fear_target_count=15,
                greed_target_count=5,
            ),
        )
        # 无信号 → 维持当前，但至少顶信号目标仓位（y=5）
        self.assertEqual(
            (5, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 50.0, "log_volume_z": 0.0},
                current_holding_count=4,
                fear_target_count=15,
                greed_target_count=5,
            ),
        )
        self.assertEqual(
            (8, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 50.0, "log_volume_z": 0.0},
                current_holding_count=8,
                fear_target_count=15,
                greed_target_count=5,
            ),
        )

    def test_top3_regime_keeps_holdings_until_one_falls_out_of_top12(self):
        ranking = [
            {"stock_symbol": f"SH.{600000 + index}", "stock_name": str(index)}
            for index in range(15)
        ]
        current_holdings = [
            {"stock_symbol": "SH600000", "weight": 33.34},
            {"stock_symbol": "SH600004", "weight": 33.33},
            {"stock_symbol": "SH600011", "weight": 33.33},
        ]
        stable_plan = build_equal_top10_top12_buffer_plan(
            ranking=ranking,
            current_holdings=current_holdings,
            top_n=3,
            sell_rank=12,
            target_total_weight_pct=30.0,
        )
        self.assertFalse(stable_plan["component_changed"])
        self.assertEqual(["SH.600000", "SH.600004", "SH.600011"], stable_plan["final_symbols"])
        self.assertEqual(30.0, sum(item["rebalance_weight_pct"] for item in stable_plan["target_items"]))
        self.assertEqual(70.0, stable_plan["target_cash_weight_pct"])

        ranking[11], ranking[12] = ranking[12], ranking[11]
        changed_plan = build_equal_top10_top12_buffer_plan(
            ranking=ranking,
            current_holdings=current_holdings,
            top_n=3,
            sell_rank=12,
            target_total_weight_pct=30.0,
        )
        self.assertTrue(changed_plan["component_changed"])
        self.assertIn("SH.600011", changed_plan["removed_symbols"])
        self.assertIn("SH.600001", changed_plan["added_symbols"])

    @staticmethod
    def _rank_acceleration_fixture():
        ranking = []
        comparison_items = [{} for _ in range(100)]
        comparison_by_symbol = {}
        for index in range(1, 16):
            symbol = f"SH.{600000 + index:06d}"
            ranking.append(
                {
                    "composite_rank": index,
                    "stock_symbol": symbol,
                    "stock_name": f"加速股票{index}",
                    "segment_name": f"行业{index % 4}",
                    "holding_cube_count": 12,
                    "total_weight_pct": 500.0 - index,
                    "composite_weight_pct": 5.0 - index / 100.0,
                }
            )
            if index > 1:
                comparison_by_symbol[symbol] = {
                    "composite_rank": index + 30,
                    "holding_cube_count": 8,
                    "total_weight_pct": 300.0 - index,
                }
        return ranking, {
            "available": True,
            "reason": None,
            "compare_snapshot_date": date(2026, 7, 3),
            "trading_days": 5,
            "items": comparison_items,
            "by_symbol": comparison_by_symbol,
        }

    @staticmethod
    def _rank_acceleration_turnover_fixture():
        ranking = []
        comparison_items = [{} for _ in range(100)]
        comparison_by_symbol = {}
        for index in range(1, 41):
            symbol = f"SH.{600000 + index:06d}"
            ranking.append(
                {
                    "composite_rank": index,
                    "stock_symbol": symbol,
                    "stock_name": f"换仓股票{index}",
                    "segment_name": "",
                    "holding_cube_count": 12,
                    "total_weight_pct": 500.0 - index,
                }
            )
            comparison_by_symbol[symbol] = {
                "composite_rank": index + 40 if index <= 10 else index,
                "holding_cube_count": 8 if index <= 10 else 12,
                "total_weight_pct": 300.0 - index if index <= 10 else 500.0 - index,
            }
        current_symbols = [f"SH.{600000 + index:06d}" for index in range(31, 41)]
        current_holdings = [
            {
                "stock_symbol": symbol.replace(".", ""),
                "stock_name": f"旧持仓{index}",
                "weight": 10.0,
            }
            for index, symbol in enumerate(current_symbols, start=1)
        ]
        comparison = {
            "available": True,
            "reason": None,
            "compare_snapshot_date": date(2026, 7, 3),
            "trading_days": 5,
            "items": comparison_items,
            "by_symbol": comparison_by_symbol,
        }
        return ranking, comparison, current_holdings, current_symbols

    @staticmethod
    def _strategy_history_entry(
        run_date,
        *,
        buy_signals=None,
        exit_signals=None,
        added_symbols=None,
        replacement_count=0,
        executed=False,
    ):
        return {
            "run_date": run_date,
            "strategy_plan": {
                "daily_buy_signal_symbols": buy_signals or [],
                "normal_exit_signal_symbols": exit_signals or [],
                "added_symbols": added_symbols or [],
            },
            "rebalance_executed": executed,
            "added_symbols": added_symbols or [],
            "replacement_count": replacement_count,
        }

    def test_save_year_top_cubes_deduplicates_symbols_and_reassigns_ranks(self):
        with TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{tmpdir}/rank_cache.db")
            Base.metadata.create_all(engine, tables=[XueqiuCubeRankCache.__table__])
            session_factory = sessionmaker(bind=engine)
            fetched_at = datetime.now()

            cubes = [
                CubeInfo(year_rank=1, symbol="ZH000001", cube_name="best-symbol"),
                CubeInfo(year_rank=2, symbol="ZH000002", cube_name="second"),
                CubeInfo(year_rank=4, symbol="ZH000001", cube_name="duplicate-symbol"),
                CubeInfo(year_rank=0, symbol="ZH000003", cube_name="missing-rank"),
                CubeInfo(year_rank=2, symbol="ZH000004", cube_name="duplicate-rank"),
                CubeInfo(year_rank=5, symbol="SH000001", cube_name="not-a-cube"),
                CubeInfo(year_rank=6, symbol="zh000005", cube_name="lowercase-cube"),
            ]

            with patch("src.robot.xueqiu_top_holdings_report.SessionLocal", session_factory):
                save_year_top_cubes(cubes, fetched_at)
                loaded, cached_at = load_cached_year_top_cubes(limit=10, max_age_days=30)

            self.assertEqual(fetched_at, cached_at)
            self.assertEqual(["ZH000001", "ZH000002", "ZH000004", "ZH000003", "ZH000005"], [cube.symbol for cube in loaded])
            self.assertEqual([1, 2, 3, 4, 5], [cube.year_rank for cube in loaded])
            self.assertEqual("best-symbol", loaded[0].cube_name)
            self.assertEqual("duplicate-rank", loaded[2].cube_name)

    def test_load_cached_year_top_cubes_ignores_severely_shortened_large_cache(self):
        with TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{tmpdir}/rank_cache.db")
            Base.metadata.create_all(engine, tables=[XueqiuCubeRankCache.__table__])
            session_factory = sessionmaker(bind=engine)
            fetched_at = datetime(2026, 6, 24, 18, 0, 0)
            cubes = [
                CubeInfo(year_rank=index, symbol=f"SH{index:06d}", cube_name=f"invalid-{index}")
                for index in range(1, 101)
            ]
            cubes.extend([
                CubeInfo(year_rank=101 + index, symbol=f"ZH{index:06d}", cube_name=f"valid-{index}")
                for index in range(1, 5)
            ])

            with patch("src.robot.xueqiu_top_holdings_report.SessionLocal", session_factory):
                save_year_top_cubes(cubes, fetched_at)
                loaded, cached_at = load_cached_year_top_cubes(limit=100, max_age_days=30)

            self.assertEqual(fetched_at, cached_at)
            self.assertEqual([], loaded)

    def test_current_fetch_quality_rejects_bad_snapshot_before_writing(self):
        results = [
            CubeCurrentResult(cube=CubeInfo(year_rank=index, symbol=f"ZH{index:06d}"), holdings=[], error="HTTP 400")
            for index in range(1, 12)
        ]
        results.extend([
            CubeCurrentResult(
                cube=CubeInfo(year_rank=100 + index, symbol=f"ZH{100 + index:06d}"),
                holdings=[{"stock_symbol": "SZ300308", "stock_name": "中际旭创", "weight": 10.0}],
            )
            for index in range(1, 3)
        ])

        with self.assertRaisesRegex(RuntimeError, "before saving snapshot"):
            ensure_xueqiu_current_fetch_quality(results, source_count=100)

    def test_rank_cache_drift_guard_accepts_recent_snapshot_overlap(self):
        cubes = [
            CubeInfo(year_rank=index, symbol=f"ZH{index:06d}")
            for index in range(1, 101)
        ]
        baseline_symbols = {f"ZH{index:06d}" for index in range(1, 91)}
        baseline_symbols.update({f"ZH9{index:05d}" for index in range(1, 11)})

        summary = validate_xueqiu_rank_cache_drift(
            cubes,
            [("snapshot:2026-06-23", baseline_symbols)],
            min_overlap_ratio=0.50,
            min_symbol_count=50,
        )

        self.assertTrue(summary["checked"])
        self.assertEqual("snapshot:2026-06-23", summary["best_label"])
        self.assertEqual(90, summary["best_overlap_count"])

    def test_rank_cache_drift_guard_rejects_low_overlap(self):
        cubes = [
            CubeInfo(year_rank=index, symbol=f"ZH{index:06d}")
            for index in range(1, 101)
        ]
        baseline_symbols = {f"ZH9{index:05d}" for index in range(1, 101)}

        with self.assertRaisesRegex(RuntimeError, "drift too large"):
            validate_xueqiu_rank_cache_drift(
                cubes,
                [("snapshot:2026-06-23", baseline_symbols)],
                min_overlap_ratio=0.50,
                min_symbol_count=50,
            )

    def test_rank_drift_baseline_includes_rank_history_snapshots_and_cache(self):
        history_symbols = {f"ZH{index:06d}" for index in range(1, 101)}
        snapshot_symbols = {f"ZH{index:06d}" for index in range(1, 101)}
        cached_cubes = [
            CubeInfo(year_rank=index, symbol=f"ZH9{index:05d}")
            for index in range(1, 101)
        ]

        with patch(
            "src.robot.xueqiu_top_holdings_report.load_recent_xueqiu_rank_history_cube_sets",
            return_value=[("rank_history:2026-06-22", history_symbols)],
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_recent_xueqiu_snapshot_cube_sets",
            return_value=[("snapshot:2026-06-23", snapshot_symbols)],
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_cached_year_top_cubes",
            return_value=(cached_cubes, datetime(2026, 6, 24, 18, 0, 0)),
        ):
            baselines = load_xueqiu_rank_drift_baselines(limit=100)

        self.assertEqual(
            [
                ("rank_history:2026-06-22", history_symbols),
                ("snapshot:2026-06-23", snapshot_symbols),
                (
                    "rank_cache:2026-06-24 18:00:00",
                    {f"ZH9{index:05d}" for index in range(1, 101)},
                ),
            ],
            baselines,
        )

    def test_load_or_refresh_year_top_cubes_rejects_rank_drift_before_writing_cache(self):
        cubes = [
            CubeInfo(year_rank=index, symbol=f"ZH{index:06d}")
            for index in range(1, 101)
        ]
        baseline_symbols = {f"ZH9{index:05d}" for index in range(1, 101)}

        with patch(
            "src.robot.xueqiu_top_holdings_report.fetch_year_top_cubes",
            new=AsyncMock(return_value=cubes),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_rank_drift_baselines",
            return_value=[("snapshot:2026-06-23", baseline_symbols)],
        ), patch("src.robot.xueqiu_top_holdings_report.save_year_top_cubes") as save_cache:
            with self.assertRaisesRegex(RuntimeError, "drift too large"):
                asyncio.run(
                    load_or_refresh_year_top_cubes(
                        cookie="xq_a_token=test;",
                        force_refresh=True,
                        limit=100,
                    )
                )

        save_cache.assert_not_called()

    def test_load_or_refresh_year_top_cubes_uses_custom_overlap_threshold(self):
        cubes = [
            CubeInfo(year_rank=index, symbol=f"ZH{index:06d}")
            for index in range(1, 101)
        ]
        baseline_symbols = {f"ZH{index:06d}" for index in range(1, 61)}
        baseline_symbols.update({f"ZH9{index:05d}" for index in range(61, 101)})

        with patch(
            "src.robot.xueqiu_top_holdings_report.fetch_year_top_cubes",
            new=AsyncMock(return_value=cubes),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_rank_drift_baselines",
            return_value=[("snapshot:2026-06-23", baseline_symbols)],
        ), patch("src.robot.xueqiu_top_holdings_report.save_year_top_cubes") as save_cache:
            with self.assertRaisesRegex(RuntimeError, "threshold=70%"):
                asyncio.run(
                    load_or_refresh_year_top_cubes(
                        cookie="xq_a_token=test;",
                        force_refresh=True,
                        limit=100,
                        min_overlap_ratio=0.70,
                    )
                )

        save_cache.assert_not_called()

    def test_load_or_refresh_year_top_cubes_writes_history_before_rank_cache(self):
        cubes = [
            CubeInfo(year_rank=index, symbol=f"ZH{index:06d}")
            for index in range(1, 101)
        ]
        baseline_symbols = {f"ZH{index:06d}" for index in range(1, 101)}
        calls = []

        def record_history(_cubes, _fetched_at):
            calls.append("history")
            return {"saved_rows": len(_cubes)}

        def record_cache(_cubes, _fetched_at):
            calls.append("cache")

        with patch(
            "src.robot.xueqiu_top_holdings_report.fetch_year_top_cubes",
            new=AsyncMock(return_value=cubes),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_rank_drift_baselines",
            return_value=[("rank_history:2026-06-23", baseline_symbols)],
        ), patch(
            "src.robot.xueqiu_top_holdings_report.save_xueqiu_cube_rank_history_to_duckdb",
            side_effect=record_history,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.save_year_top_cubes",
            side_effect=record_cache,
        ):
            loaded, _fetched_at, refreshed = asyncio.run(
                load_or_refresh_year_top_cubes(
                    cookie="xq_a_token=test;",
                    force_refresh=True,
                    limit=100,
                )
            )

        self.assertTrue(refreshed)
        self.assertEqual(cubes, loaded)
        self.assertEqual(["history", "cache"], calls)

    def test_fetch_cube_manager_activity_reuses_previous_activity_when_rb_id_unchanged(self):
        class FakeResponse:
            status_code = 200
            text = ""

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def get(self, url, params=None):
                self.calls.append((url, params or {}))
                return FakeResponse({"data": {"cube": {"last_user_rb_gid": 12345}}})

        client = FakeClient()
        previous_activity = CubeActivityResult(
            symbol="ZH000001",
            latest_rebalance_at=datetime(2026, 6, 1, 12, 0, 0),
            latest_rebalance_id=12345,
            latest_rebalance_status="success",
            latest_rebalance_category="user_rebalancing",
            cache_hit=True,
        )

        activity = asyncio.run(
            fetch_cube_manager_activity(
                client,
                CubeInfo(year_rank=1, symbol="ZH000001"),
                retries=1,
                previous_activity=previous_activity,
            )
        )

        self.assertEqual(1, len(client.calls))
        self.assertIn("/cubes/show.json", client.calls[0][0])
        self.assertFalse(activity.cache_hit)
        self.assertEqual(12345, activity.latest_rebalance_id)
        self.assertEqual(previous_activity.latest_rebalance_at, activity.latest_rebalance_at)

    def test_latest_manager_rebalance_uses_user_rebalancing_updated_at(self):
        events = [
            {
                "id": 227195415,
                "category": "sys_rebalancing",
                "status": "success",
                "created_at": 1780522357000,
                "updated_at": 1780522357000,
            },
            {
                "id": 109183073,
                "category": "user_rebalancing",
                "status": "success",
                "created_at": 1637900893000,
                "updated_at": 1637902858000,
            },
        ]

        activity = latest_manager_rebalance_from_events("ZH1319424", events, pages_fetched=1)

        self.assertEqual(109183073, activity.latest_rebalance_id)
        self.assertEqual("user_rebalancing", activity.latest_rebalance_category)
        self.assertEqual("success", activity.latest_rebalance_status)
        self.assertEqual("2021-11-26T13:00:58+08:00", activity.latest_rebalance_at.isoformat())

    def test_manager_rebalance_from_show_origin_uses_updated_at(self):
        activity = manager_rebalance_from_show_origin(
            "ZH1319424",
            rb_id=109183073,
            origin_payload={
                "rebalancing": {
                    "id": 109183073,
                    "category": "user_rebalancing",
                    "status": "success",
                    "created_at": 1637900893000,
                    "updated_at": 1637902858000,
                }
            },
            checked_at=datetime(2026, 6, 23, 18, 0, 0),
        )

        self.assertEqual(109183073, activity.latest_rebalance_id)
        self.assertEqual("user_rebalancing", activity.latest_rebalance_category)
        self.assertEqual("success", activity.latest_rebalance_status)
        self.assertEqual("2021-11-26T13:00:58+08:00", activity.latest_rebalance_at.isoformat())

    def test_save_and_load_cube_activity_cache(self):
        with TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{tmpdir}/activity_cache.db")
            Base.metadata.create_all(engine, tables=[XueqiuCubeActivityCache.__table__])
            session_factory = sessionmaker(bind=engine)
            latest_at = datetime(2026, 5, 22, 13, 0, 58)
            checked_at = datetime(2026, 6, 1, 9, 30, 0)

            with patch("src.robot.xueqiu_top_holdings_report.SessionLocal", session_factory):
                saved = save_cube_activity_cache(
                    [
                        CubeActivityResult(
                            symbol="ZH000001",
                            latest_rebalance_at=latest_at,
                            latest_rebalance_id=123,
                            latest_rebalance_status="success",
                            latest_rebalance_category="user_rebalancing",
                            pages_fetched=1,
                            checked_at=checked_at,
                        )
                    ]
                )
                loaded = load_cached_cube_activity(
                    [CubeInfo(year_rank=1, symbol="ZH000001")],
                    activity_type=ACTIVE_REBALANCE_ACTIVITY_TYPE,
                    min_checked_at=datetime(2026, 5, 31, 0, 0, 0),
                )

            self.assertEqual(1, saved)
            self.assertIn("ZH000001", loaded)
            self.assertTrue(loaded["ZH000001"].cache_hit)
            self.assertEqual(123, loaded["ZH000001"].latest_rebalance_id)
            self.assertEqual("2026-05-22T13:00:58+08:00", loaded["ZH000001"].latest_rebalance_at.isoformat())

    def test_strategy_config_defaults_and_upsert(self):
        from src.core.database import XueqiuStrategyConfig
        from src.robot.xueqiu_top_holdings_report import load_xueqiu_strategy_config

        with TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{tmpdir}/strategy_config.db")
            Base.metadata.create_all(engine, tables=[XueqiuStrategyConfig.__table__])
            session_factory = sessionmaker(bind=engine)

            with patch("src.robot.xueqiu_top_holdings_report.SessionLocal", session_factory):
                # 未配置时回退默认值（仅含雪球组合参数，信号参数已统一到 fear_greed_signal_configs）
                defaults = load_xueqiu_strategy_config("rank_acceleration")
                self.assertEqual(10, defaults["fear_target_count"])
                self.assertEqual(3, defaults["greed_target_count"])
                self.assertEqual(8, defaults["min_holding_cubes"])
                self.assertNotIn("fear_threshold", defaults)

                # 写入配置后读取生效
                db = session_factory()
                db.add(
                    XueqiuStrategyConfig(
                        strategy_key="rank_acceleration",
                        fear_target_count=15,
                        greed_target_count=5,
                        min_holding_cubes=6,
                    )
                )
                db.commit()
                db.close()
                saved = load_xueqiu_strategy_config("rank_acceleration")
                self.assertEqual(15, saved["fear_target_count"])
                self.assertEqual(5, saved["greed_target_count"])
                self.assertEqual(6, saved["min_holding_cubes"])
                # 其他策略不受影响
                self.assertEqual(10, load_xueqiu_strategy_config("buffer")["fear_target_count"])

    def test_position_target_uses_configured_volume_thresholds(self):
        # 恐慌但量比未达到配置的 1.5 标准差 → 维持
        self.assertEqual(
            (3, "neutral_keep_current"),
            resolve_xueqiu_strategy_position_target(
                {"score": 20.0, "log_volume_z": 1.2},
                current_holding_count=3,
                fear_threshold=25.0,
                fear_volume_std=1.5,
            ),
        )
        self.assertEqual(
            (10, "volume_bottom"),
            resolve_xueqiu_strategy_position_target(
                {"score": 20.0, "log_volume_z": 1.6},
                current_holding_count=3,
                fear_threshold=25.0,
                fear_volume_std=1.5,
            ),
        )

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

    def test_rank_acceleration_plan_selects_strong_new_and_breadth_confirmed_risers(self):
        ranking, comparison = self._rank_acceleration_fixture()
        buy_signals = [item["stock_symbol"] for item in ranking[:10]]

        plan = build_rank_acceleration_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=[],
            current_snapshot_date=date(2026, 7, 10),
            strategy_history=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                )
            ],
        )

        self.assertTrue(plan["component_changed"])
        self.assertEqual(10, len(plan["final_symbols"]))
        self.assertEqual("SH.600001", plan["final_symbols"][0])
        first_item = next(item for item in plan["target_items"] if item["stock_symbol"] == "SH.600001")
        self.assertTrue(first_item["is_new_5d"])
        self.assertTrue(first_item["strong_new_entry"])
        self.assertEqual(10.0, first_item["rebalance_weight_pct"])
        self.assertTrue(all(item["buy_eligible"] for item in plan["target_items"]))

    def test_rank_acceleration_plan_limits_full_portfolio_to_two_replacements(self):
        ranking, comparison, current_holdings, current_symbols = self._rank_acceleration_turnover_fixture()
        buy_signals = [item["stock_symbol"] for item in ranking[:10]]

        plan = build_rank_acceleration_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=current_holdings,
            current_snapshot_date=date(2026, 7, 10),
            strategy_history=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                    exit_signals=current_symbols,
                )
            ],
        )

        self.assertEqual(2, len(plan["removed_symbols"]))
        self.assertEqual(2, len(plan["added_symbols"]))
        self.assertEqual(10, len(plan["final_symbols"]))
        self.assertEqual(100.0, round(sum(item["rebalance_weight_pct"] for item in plan["target_items"]), 2))

    def test_rank_acceleration_requires_buy_and_sell_confirmation(self):
        ranking, comparison, current_holdings, current_symbols = self._rank_acceleration_turnover_fixture()

        plan = build_rank_acceleration_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=current_holdings,
            current_snapshot_date=date(2026, 7, 10),
        )

        self.assertEqual([], plan["confirmed_buy_symbols"])
        self.assertEqual(current_symbols, plan["normal_exit_signal_symbols"])
        self.assertEqual([], plan["confirmed_normal_exit_symbols"])
        self.assertFalse(plan["component_changed"])

    def test_rank_acceleration_minimum_holding_period_blocks_normal_exit(self):
        ranking, comparison, current_holdings, current_symbols = self._rank_acceleration_turnover_fixture()
        buy_signals = [item["stock_symbol"] for item in ranking[:10]]
        history = [
            self._strategy_history_entry(
                date(2026, 7, 9),
                buy_signals=buy_signals,
                exit_signals=current_symbols,
            ),
            self._strategy_history_entry(date(2026, 7, 8)),
            self._strategy_history_entry(date(2026, 7, 7)),
            self._strategy_history_entry(
                date(2026, 7, 6),
                added_symbols=current_symbols,
                executed=True,
            ),
        ]

        plan = build_rank_acceleration_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=current_holdings,
            strategy_history=history,
            current_snapshot_date=date(2026, 7, 10),
        )

        self.assertEqual(current_symbols, plan["confirmed_normal_exit_symbols"])
        self.assertEqual(current_symbols, plan["min_holding_blocked_symbols"])
        self.assertFalse(plan["component_changed"])

    def test_rank_acceleration_rolling_limit_reduces_and_then_blocks_replacements(self):
        ranking, comparison, current_holdings, current_symbols = self._rank_acceleration_turnover_fixture()
        buy_signals = [item["stock_symbol"] for item in ranking[:10]]
        history = [
            self._strategy_history_entry(
                date(2026, 7, 9),
                buy_signals=buy_signals,
                exit_signals=current_symbols,
                replacement_count=1,
                executed=True,
            ),
            self._strategy_history_entry(
                date(2026, 7, 8),
                replacement_count=1,
                executed=True,
            ),
            self._strategy_history_entry(date(2026, 7, 7)),
            self._strategy_history_entry(date(2026, 7, 6)),
        ]

        limited_plan = build_rank_acceleration_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=current_holdings,
            strategy_history=history,
            current_snapshot_date=date(2026, 7, 10),
        )

        self.assertEqual(2, limited_plan["rolling_prior_replacements"])
        self.assertEqual(1, limited_plan["replacement_count"])
        self.assertEqual(1, len(limited_plan["removed_symbols"]))

        history[2] = self._strategy_history_entry(
            date(2026, 7, 7),
            replacement_count=1,
            executed=True,
        )
        blocked_plan = build_rank_acceleration_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=current_holdings,
            strategy_history=history,
            current_snapshot_date=date(2026, 7, 10),
        )

        self.assertEqual(3, blocked_plan["rolling_prior_replacements"])
        self.assertEqual(0, blocked_plan["replacement_count"])
        self.assertFalse(blocked_plan["component_changed"])

    def test_rank_acceleration_hard_exit_ignores_confirmation_and_holding_period(self):
        ranking, comparison, current_holdings, current_symbols = self._rank_acceleration_turnover_fixture()
        hard_exit_symbol = current_symbols[0]
        hard_exit_item = next(item for item in ranking if item["stock_symbol"] == hard_exit_symbol)
        hard_exit_item["composite_rank"] = 151

        plan = build_rank_acceleration_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=current_holdings,
            current_snapshot_date=date(2026, 7, 10),
        )

        self.assertEqual([hard_exit_symbol], plan["hard_exit_symbols"])
        self.assertEqual([hard_exit_symbol], plan["removed_symbols"])
        self.assertTrue(plan["component_changed"])
        self.assertEqual(9, len(plan["final_symbols"]))

        rounded_items = rounded_rebalance_weights(plan["target_items"])
        self.assertEqual(90.0, sum(item["rebalance_weight_pct"] for item in rounded_items))

    def test_rank_acceleration_history_loads_signals_and_executed_replacements(self):
        with TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{tmpdir}/history.db")
            Base.metadata.create_all(engine, tables=[XueqiuTopHoldingsRun.__table__])
            session_factory = sessionmaker(bind=engine)
            db = session_factory()
            try:
                db.add_all(
                    [
                        XueqiuTopHoldingsRun(
                            run_at=datetime(2026, 7, 9, 14, 50),
                            target_cube_symbol="ZH3644546",
                            status="SUCCESS",
                            dry_run=False,
                            top_n=10,
                            top_holdings=[
                                {
                                    "stock_symbol": "SH.600001",
                                    "strategy_action": "buy",
                                }
                            ],
                            rebalance_payload={
                                "strategy_plan": {
                                    "daily_buy_signal_symbols": ["SH.600001"],
                                    "added_symbols": ["SH.600001"],
                                }
                            },
                            rebalance_response={"id": 12345},
                        ),
                        XueqiuTopHoldingsRun(
                            run_at=datetime(2026, 7, 8, 14, 50),
                            target_cube_symbol="ZH3644546",
                            status="SKIPPED",
                            dry_run=False,
                            top_n=10,
                            top_holdings=[],
                            rebalance_response={
                                "skipped": True,
                                "strategy_plan": {
                                    "normal_exit_signal_symbols": ["SH.600031"]
                                },
                            },
                        ),
                        XueqiuTopHoldingsRun(
                            run_at=datetime(2026, 7, 7, 14, 50),
                            target_cube_symbol="ZH3644546",
                            status="SUCCESS",
                            dry_run=False,
                            top_n=10,
                            top_holdings=[
                                {
                                    "stock_symbol": f"SH.{600000 + index:06d}",
                                    "strategy_action": "buy",
                                }
                                for index in range(1, 11)
                            ],
                            rebalance_payload={
                                "strategy_plan": {
                                    "top_n": 10,
                                    "current_symbols": ["SH.511880"],
                                    "added_symbols": [
                                        f"SH.{600000 + index:06d}"
                                        for index in range(1, 11)
                                    ],
                                }
                            },
                            rebalance_response={"id": 12344},
                        ),
                    ]
                )
                db.commit()
            finally:
                db.close()

            with patch("src.robot.xueqiu_top_holdings_report.SessionLocal", session_factory):
                history = load_rank_acceleration_strategy_history(
                    target_cube_symbol="ZH3644546",
                    current_snapshot_date=date(2026, 7, 10),
                )

        self.assertEqual(
            [date(2026, 7, 9), date(2026, 7, 8), date(2026, 7, 7)],
            [entry["run_date"] for entry in history],
        )
        self.assertEqual(1, history[0]["replacement_count"])
        self.assertEqual(["SH.600001"], history[0]["added_symbols"])
        self.assertEqual(
            ["SH.600031"],
            history[1]["strategy_plan"]["normal_exit_signal_symbols"],
        )
        self.assertEqual(0, history[2]["replacement_count"])

    def test_rank_comparison_snapshot_uses_fifth_prior_snapshot(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            with patch("src.robot.xueqiu_top_holdings_report.ANALYTICS_DB_PATH", path):
                for day in range(1, 6):
                    save_xueqiu_cube_holdings_snapshots_to_duckdb(
                        run_at=datetime(2026, 6, day, 14, 50, 0),
                        active_rebalance_days=360,
                        current_results=[
                            CubeCurrentResult(
                                cube=CubeInfo(year_rank=1, symbol="ZH000001", cube_name="组合一"),
                                holdings=[
                                    {
                                        "stock_symbol": "SH600001",
                                        "stock_name": "股票一",
                                        "weight": 60.0,
                                    }
                                ],
                                active=True,
                            )
                        ],
                    )

                comparison = load_xueqiu_rank_comparison_snapshot(
                    current_snapshot_date=date(2026, 6, 6),
                    trading_days=5,
                )

            self.assertTrue(comparison["available"])
            self.assertEqual("2026-06-01", comparison["compare_snapshot_date"])
            self.assertEqual(1, comparison["by_symbol"]["SH.600001"]["holding_cube_count"])
            self.assertEqual(60.0, comparison["by_symbol"]["SH.600001"]["total_weight_pct"])
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_robot_rebalance_enables_rank_acceleration_target_in_same_job(self):
        result = {
            "skipped": False,
            "record_id": 1,
            "target_cube_symbol": "ZH3630096",
            "target_cube_id": 3664154,
            "success_count": 1000,
            "failed_count": 0,
            "stock_count": 600,
            "rebalance_response": {"skipped": True},
            "active_filter": {"active_cube_count": 600, "source_cube_count": 1000},
            "holdings_snapshot": {"saved_rows": 3000},
            "rank_acceleration_target": {
                "target_cube_symbol": "ZH3644546",
                "record_id": 2,
                "status": "SKIPPED",
                "rebalance_response": {"skipped": True},
            },
        }
        with patch(
            "src.robot.xueqiu_top_holdings_report.run_top_holdings_job",
            new=AsyncMock(return_value=result),
        ) as run_job, patch.dict(os.environ, {}, clear=True):
            message = process_xueqiu_top_holdings_rebalance_for_robot()

        self.assertEqual("ZH3644546", run_job.await_args.kwargs["rank_acceleration_target_cube_symbol"])
        self.assertIn("rank_acceleration_target=ZH3644546", message)

    def test_rank_acceleration_target_rebalance_uses_target_cube_in_same_run(self):
        ranking, comparison = self._rank_acceleration_fixture()
        buy_signals = [item["stock_symbol"] for item in ranking[:10]]
        aggregate = {
            "ranking": ranking,
            "failed_results": [],
            "success_count": 100,
        }

        async def fake_payload(**kwargs):
            return {
                "target_cube_symbol": kwargs["target_cube_symbol"],
                "cube_id": kwargs["target_cube_id"],
                "holdings": [{"stock_symbol": "SH600001", "weight": 10.0}],
                "top_items": kwargs["top_items"],
                "skipped_items": [],
            }

        with patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_rank_comparison_snapshot",
            return_value=comparison,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.fetch_target_cube_current_payload",
            new=AsyncMock(return_value={"last_rb": {"cube_id": 3644546, "holdings": []}}),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_latest_csi_all_share_fear_greed",
            return_value=None,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_rank_acceleration_strategy_history",
            return_value=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                )
            ],
        ), patch(
            "src.robot.xueqiu_top_holdings_report.build_rebalance_payload",
            new=AsyncMock(side_effect=fake_payload),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.create_xueqiu_rebalance",
            new=AsyncMock(return_value={"id": 12345, "status": "success"}),
        ) as create_rebalance:
            result = asyncio.run(
                execute_rank_acceleration_target_rebalance(
                    cookie="xq_a_token=test;",
                    aggregate=aggregate,
                    current_snapshot_date=date(2026, 7, 10),
                    target_cube_symbol="ZH3644546",
                    target_cube_id=None,
                    active_filter_summary=None,
                    dry_run=False,
                    timeout=10.0,
                )
            )

        self.assertEqual("ZH3644546", result["target_cube_symbol"])
        self.assertEqual(3644546, result["target_cube_id"])
        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual(10, len(result["top_items"]))
        self.assertEqual("missing_fallback", (result["strategy_plan"] or {}).get("fear_greed_regime"))
        create_rebalance.assert_awaited_once()

    def test_rank_acceleration_greed_regime_targets_three_positions(self):
        ranking, comparison, current_holdings, current_symbols = self._rank_acceleration_turnover_fixture()
        buy_signals = [item["stock_symbol"] for item in ranking[:10]]
        aggregate = {
            "ranking": ranking,
            "failed_results": [],
            "success_count": 100,
        }

        with patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_rank_comparison_snapshot",
            return_value=comparison,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.fetch_target_cube_current_payload",
            new=AsyncMock(return_value={"last_rb": {"cube_id": 3644546, "holdings": current_holdings}}),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_latest_csi_all_share_fear_greed",
            return_value={"score": 80.0, "rating": "贪婪", "date": "2026-07-10", "log_volume_z": -2.0},
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_rank_acceleration_strategy_history",
            return_value=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                )
            ],
        ), patch(
            "src.robot.xueqiu_top_holdings_report.build_rebalance_payload",
            new=AsyncMock(side_effect=lambda **kwargs: {
                "target_cube_symbol": kwargs["target_cube_symbol"],
                "cube_id": kwargs["target_cube_id"],
                "holdings": [{"stock_symbol": "SH600031", "weight": 33.33}],
                "top_items": kwargs["top_items"],
                "skipped_items": [],
            }),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.create_xueqiu_rebalance",
            new=AsyncMock(return_value={"id": 12346, "status": "success"}),
        ):
            result = asyncio.run(
                execute_rank_acceleration_target_rebalance(
                    cookie="xq_a_token=test;",
                    aggregate=aggregate,
                    current_snapshot_date=date(2026, 7, 10),
                    target_cube_symbol="ZH3644546",
                    target_cube_id=None,
                    active_filter_summary=None,
                    dry_run=False,
                    timeout=10.0,
                )
            )

        self.assertEqual("SUCCESS", result["status"])
        # 贪婪（score=80）→ 目标3只，从10只收缩
        self.assertEqual(3, len(result["top_items"]))
        self.assertEqual("volume_top", (result["strategy_plan"] or {}).get("fear_greed_regime"))
        self.assertEqual(7, len((result["strategy_plan"] or {}).get("trim_removed_symbols") or []))

    def test_rank_acceleration_is_appended_to_same_email(self):
        result = {
            "target_cube_symbol": "ZH3644546",
            "target_cube_id": 3644546,
            "status": "SUCCESS",
            "comparison_snapshot": {"compare_snapshot_date": "2026-07-03"},
            "strategy_plan": {
                "eligible_buy_count": 11,
                "eligible_retain_count": 15,
                "buy_rule": "买入规则样例",
                "sell_rule": "卖出规则样例",
                "execution_weight_rule": "等权规则样例",
                "summary": {
                    "current": [],
                    "retained": [],
                    "removed": [],
                    "added": ["SZ.000938(紫光股份)"],
                    "final": ["SZ.000938(紫光股份)"],
                },
            },
            "rebalance_payload": {"cash": 0.0},
            "rebalance_response": {"id": 12345, "status": "success"},
            "top_items": [
                {
                    "strategy_rank": 1,
                    "stock_symbol": "SZ.000938",
                    "stock_name": "紫光股份",
                    "composite_rank": 15,
                    "rank_5d_ago": None,
                    "is_new_5d": True,
                    "acceleration_rank_change_5d": 671,
                    "holding_cube_count": 12,
                    "holding_cube_count_change_5d": 12,
                    "rebalance_weight_pct": 10.0,
                    "strategy_action": "buy",
                }
            ],
        }

        combined = append_rank_acceleration_email_section(
            "<html><body><h1>星澜壹号</h1></body></html>",
            result,
        )

        self.assertEqual(1, combined.count("</body>"))
        self.assertLess(combined.index("星澜壹号"), combined.index("星澜贰号"))
        self.assertIn("ZH3644546", combined)
        self.assertIn("2026-07-03", combined)
        self.assertIn("紫光股份", combined)
        self.assertIn("新进", combined)
        self.assertIn("+671", combined)
        self.assertIn("+12", combined)
        self.assertIn("12345", combined)

    @staticmethod
    def _weight_price_ratio_fixture():
        ranking = []
        comparison_items = [{} for _ in range(100)]
        comparison_by_symbol = {}
        ratios = {1: 1.2, 2: 2.5, 3: 0.9}
        for index in range(1, 16):
            symbol = f"SH.{600000 + index:06d}"
            ranking.append(
                {
                    "composite_rank": index,
                    "stock_symbol": symbol,
                    "stock_name": f"权价比股票{index}",
                    "segment_name": f"行业{index % 4}",
                    "holding_cube_count": 12,
                    "total_weight_pct": 500.0 - index,
                    "composite_weight_pct": 5.0 - index / 100.0,
                    "weight_price_ratio_5d": ratios.get(index, 1.5 + (10 - index) * 0.01),
                    "weight_multiple_5d": 2.0,
                    "momentum_multiple_5d": 1.0,
                }
            )
            if index > 1:
                comparison_by_symbol[symbol] = {
                    "composite_rank": index + 30,
                    "holding_cube_count": 8,
                    "total_weight_pct": 300.0 - index,
                }
        return ranking, {
            "available": True,
            "reason": None,
            "compare_snapshot_date": date(2026, 7, 3),
            "trading_days": 5,
            "items": comparison_items,
            "by_symbol": comparison_by_symbol,
        }

    def test_weight_price_ratio_plan_selects_high_ratio_accumulators(self):
        ranking, comparison = self._weight_price_ratio_fixture()
        ratio_sorted = sorted(
            ranking,
            key=lambda item: item["weight_price_ratio_5d"],
            reverse=True,
        )
        top10 = ratio_sorted[:10]
        buy_signals = [item["stock_symbol"] for item in top10]

        plan = build_weight_price_ratio_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=[],
            current_snapshot_date=date(2026, 7, 10),
            strategy_history=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                )
            ],
        )

        self.assertTrue(plan["component_changed"])
        self.assertEqual(10, len(plan["final_symbols"]))
        # 按 5日权价比 排序：权价比最高（2.5）的排第一，而不是综合排名第1
        self.assertEqual("SH.600002", plan["final_symbols"][0])
        # 权价比 0.9（低于 1.15 门槛）不入选
        self.assertNotIn("SH.600003", plan["final_symbols"])
        self.assertEqual(WEIGHT_PRICE_RATIO_STRATEGY_NAME, plan["strategy_name"])
        self.assertEqual("weight_price_ratio", plan["metric"])
        first_item = next(item for item in plan["target_items"] if item["stock_symbol"] == "SH.600002")
        self.assertEqual(1, first_item["strategy_rank"])
        self.assertEqual(10.0, first_item["rebalance_weight_pct"])

    def test_weight_price_ratio_plan_applies_fear_greed_target_count(self):
        ranking, comparison = self._weight_price_ratio_fixture()
        ratio_sorted = sorted(
            ranking,
            key=lambda item: item["weight_price_ratio_5d"],
            reverse=True,
        )
        top3 = ratio_sorted[:3]
        buy_signals = [item["stock_symbol"] for item in top3]
        current_holdings = [
            {
                "stock_symbol": item["stock_symbol"].replace(".", ""),
                "stock_name": item["stock_name"],
                "weight": 33.33,
            }
            for item in top3
        ]

        plan = build_weight_price_ratio_buffer_plan(
            ranking=ranking,
            comparison_snapshot=comparison,
            current_holdings=current_holdings,
            current_snapshot_date=date(2026, 7, 10),
            strategy_history=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                )
            ],
            top_n=3,
            fear_greed_regime="greed",
        )

        self.assertEqual(3, plan["top_n"])
        self.assertEqual("greed", plan["fear_greed_regime"])
        self.assertEqual(3, len(plan["final_symbols"]))
        self.assertEqual(100.0, round(sum(item["rebalance_weight_pct"] for item in plan["target_items"]), 2))

    def test_robot_rebalance_enables_weight_price_ratio_target_in_same_job(self):
        result = {
            "skipped": False,
            "record_id": 1,
            "target_cube_symbol": "ZH3630096",
            "target_cube_id": 3664154,
            "success_count": 1000,
            "failed_count": 0,
            "stock_count": 600,
            "rebalance_response": {"skipped": True},
            "active_filter": {"active_cube_count": 600, "source_cube_count": 1000},
            "holdings_snapshot": {"saved_rows": 3000},
            "rank_acceleration_target": {
                "target_cube_symbol": "ZH3644546",
                "record_id": 2,
                "status": "SKIPPED",
                "rebalance_response": {"skipped": True},
            },
            "weight_price_ratio_target": {
                "target_cube_symbol": "ZH3664736",
                "record_id": 3,
                "status": "SKIPPED",
                "rebalance_response": {"skipped": True},
            },
        }
        with patch(
            "src.robot.xueqiu_top_holdings_report.run_top_holdings_job",
            new=AsyncMock(return_value=result),
        ) as run_job, patch.dict(os.environ, {}, clear=True):
            message = process_xueqiu_top_holdings_rebalance_for_robot()

        self.assertEqual("ZH3664736", run_job.await_args.kwargs["weight_price_ratio_target_cube_symbol"])
        self.assertIn("weight_price_ratio_target=ZH3664736", message)

    def test_weight_price_ratio_target_rebalance_uses_target_cube_in_same_run(self):
        ranking, comparison = self._weight_price_ratio_fixture()
        ratio_sorted = sorted(
            ranking,
            key=lambda item: item["weight_price_ratio_5d"],
            reverse=True,
        )
        buy_signals = [item["stock_symbol"] for item in ratio_sorted[:10]]
        aggregate = {
            "ranking": ranking,
            "failed_results": [],
            "success_count": 100,
        }
        ratio_by_symbol = {
            item["stock_symbol"]: {
                "weight_price_ratio_5d": item["weight_price_ratio_5d"],
                "weight_multiple_5d": item.get("weight_multiple_5d"),
                "momentum_multiple_5d": item.get("momentum_multiple_5d"),
            }
            for item in ranking
        }

        async def fake_payload(**kwargs):
            return {
                "target_cube_symbol": kwargs["target_cube_symbol"],
                "cube_id": kwargs["target_cube_id"],
                "holdings": [{"stock_symbol": "SH600002", "weight": 10.0}],
                "top_items": kwargs["top_items"],
                "skipped_items": [],
            }

        with patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_rank_comparison_snapshot",
            return_value=comparison,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_weight_price_ratio_map",
            return_value=ratio_by_symbol,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.fetch_target_cube_current_payload",
            new=AsyncMock(return_value={"last_rb": {"cube_id": 3664736, "holdings": []}}),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_latest_csi_all_share_fear_greed",
            return_value={"score": 20.0, "rating": "极度恐慌", "date": "2026-07-10", "log_volume_z": 2.0},
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_rank_acceleration_strategy_history",
            return_value=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                )
            ],
        ), patch(
            "src.robot.xueqiu_top_holdings_report.build_rebalance_payload",
            new=AsyncMock(side_effect=fake_payload),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.create_xueqiu_rebalance",
            new=AsyncMock(return_value={"id": 22222, "status": "success"}),
        ) as create_rebalance:
            result = asyncio.run(
                execute_weight_price_ratio_target_rebalance(
                    cookie="xq_a_token=test;",
                    aggregate=aggregate,
                    current_snapshot_date=date(2026, 7, 10),
                    target_cube_symbol=WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL,
                    target_cube_id=None,
                    active_filter_summary=None,
                    dry_run=False,
                    timeout=10.0,
                )
            )

        self.assertEqual(WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL, result["target_cube_symbol"])
        self.assertEqual(3664736, result["target_cube_id"])
        self.assertEqual("SUCCESS", result["status"])
        # 恐慌（score=20）→ 目标10只
        self.assertEqual(10, len(result["top_items"]))
        create_rebalance.assert_awaited_once()

    def test_weight_price_ratio_greed_regime_targets_three_positions(self):
        ranking, comparison = self._weight_price_ratio_fixture()
        ratio_sorted = sorted(
            ranking,
            key=lambda item: item["weight_price_ratio_5d"],
            reverse=True,
        )
        buy_signals = [item["stock_symbol"] for item in ratio_sorted[:10]]
        aggregate = {
            "ranking": ranking,
            "failed_results": [],
            "success_count": 100,
        }
        ratio_by_symbol = {
            item["stock_symbol"]: {
                "weight_price_ratio_5d": item["weight_price_ratio_5d"],
                "weight_multiple_5d": item.get("weight_multiple_5d"),
                "momentum_multiple_5d": item.get("momentum_multiple_5d"),
            }
            for item in ranking
        }
        current_holdings = [
            {"stock_symbol": item["stock_symbol"].replace(".", ""), "weight": 10.0}
            for item in ratio_sorted[:10]
        ]

        with patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_rank_comparison_snapshot",
            return_value=comparison,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_xueqiu_weight_price_ratio_map",
            return_value=ratio_by_symbol,
        ), patch(
            "src.robot.xueqiu_top_holdings_report.fetch_target_cube_current_payload",
            new=AsyncMock(return_value={
                "last_rb": {"cube_id": 3664736, "holdings": current_holdings}
            }),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_latest_csi_all_share_fear_greed",
            return_value={"score": 80.0, "rating": "贪婪", "date": "2026-07-10", "log_volume_z": -2.0},
        ), patch(
            "src.robot.xueqiu_top_holdings_report.load_rank_acceleration_strategy_history",
            return_value=[
                self._strategy_history_entry(
                    date(2026, 7, 9),
                    buy_signals=buy_signals,
                )
            ],
        ), patch(
            "src.robot.xueqiu_top_holdings_report.build_rebalance_payload",
            new=AsyncMock(side_effect=lambda **kwargs: {
                "target_cube_symbol": kwargs["target_cube_symbol"],
                "cube_id": kwargs["target_cube_id"],
                "holdings": [{"stock_symbol": "SH600002", "weight": 33.33}],
                "top_items": kwargs["top_items"],
                "skipped_items": [],
            }),
        ), patch(
            "src.robot.xueqiu_top_holdings_report.create_xueqiu_rebalance",
            new=AsyncMock(return_value={"id": 22223, "status": "success"}),
        ):
            result = asyncio.run(
                execute_weight_price_ratio_target_rebalance(
                    cookie="xq_a_token=test;",
                    aggregate=aggregate,
                    current_snapshot_date=date(2026, 7, 10),
                    target_cube_symbol=WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL,
                    target_cube_id=None,
                    active_filter_summary=None,
                    dry_run=False,
                    timeout=10.0,
                )
            )

        self.assertEqual("SUCCESS", result["status"])
        # 贪婪（score=80）→ 目标3只，从10只收缩
        self.assertEqual(3, len(result["top_items"]))
        self.assertEqual("volume_top", (result["strategy_plan"] or {}).get("fear_greed_regime"))
        self.assertEqual(7, len((result["strategy_plan"] or {}).get("trim_removed_symbols") or []))

    def test_weight_price_ratio_is_appended_to_same_email(self):
        result = {
            "target_cube_symbol": "ZH3664736",
            "target_cube_id": 3664736,
            "status": "SUCCESS",
            "comparison_snapshot": {"compare_snapshot_date": "2026-07-03"},
            "strategy_plan": {
                "top_n": 3,
                "fear_greed_regime": "greed",
                "fear_greed": {"score": 82.0, "rating": "贪婪", "date": "2026-07-10"},
                "eligible_retain_count": 15,
                "buy_rule": "买入规则样例",
                "sell_rule": "卖出规则样例",
                "execution_weight_rule": "等权规则样例",
                "summary": {
                    "current": [],
                    "retained": [],
                    "removed": [],
                    "added": ["SH.600002(权价比股票2)"],
                    "final": ["SH.600002(权价比股票2)"],
                },
            },
            "rebalance_payload": {"cash": 0.0},
            "rebalance_response": {"id": 22222, "status": "success"},
            "top_items": [
                {
                    "strategy_rank": 1,
                    "stock_symbol": "SH.600002",
                    "stock_name": "权价比股票2",
                    "composite_rank": 2,
                    "weight_multiple_5d": 2.5,
                    "momentum_multiple_5d": 1.0,
                    "weight_price_ratio_5d": 2.5,
                    "holding_cube_count": 12,
                    "holding_cube_count_change_5d": 4,
                    "rebalance_weight_pct": 33.33,
                    "strategy_action": "buy",
                }
            ],
        }

        combined = append_weight_price_ratio_email_section(
            "<html><body><h1>星澜壹号</h1></body></html>",
            result,
        )

        self.assertEqual(1, combined.count("</body>"))
        self.assertLess(combined.index("星澜壹号"), combined.index("星澜叁号"))
        self.assertIn("ZH3664736", combined)
        self.assertIn("2026-07-03", combined)
        self.assertIn("权价比股票2", combined)
        self.assertIn("2.50", combined)
        self.assertIn("+4", combined)
        self.assertIn("22222", combined)

    def test_build_rebalance_payload_skips_etf_quote_type_13_into_cash(self):
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

        self.assertEqual(40.0, payload["cash"])
        self.assertEqual(1, len(payload["holdings"]))
        self.assertEqual(["SH600000"], [row["stock_symbol"] for row in payload["holdings"]])
        self.assertEqual([60.0], [row["weight"] for row in payload["holdings"]])
        self.assertEqual(1, len(payload["skipped_items"]))
        self.assertEqual("SH.511880", payload["skipped_items"][0]["stock_symbol"])
        self.assertEqual("quote_type=13 blocked", payload["skipped_items"][0]["rebalance_skip_reason"])

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

    def test_aggregate_adds_cash_as_special_holding_and_report_always_shows_it(self):
        run_at = datetime(2026, 6, 22, 12, 0, 0)
        cubes = [
            CubeInfo(year_rank=1, symbol="ZH000001", cube_name="组合一"),
            CubeInfo(year_rank=2, symbol="ZH000002", cube_name="组合二"),
        ]
        aggregate = aggregate_holdings(
            [
                CubeFetchResult(
                    cube=cubes[0],
                    holdings=[
                        {
                            "stock_symbol": "SH600000",
                            "stock_name": "浦发银行",
                            "weight": 80.0,
                        }
                    ],
                ),
                CubeFetchResult(
                    cube=cubes[1],
                    holdings=[
                        {
                            "stock_symbol": "SH600000",
                            "stock_name": "浦发银行",
                            "weight": 90.0,
                        }
                    ],
                ),
            ]
        )

        report = build_report(
            run_at=run_at,
            cubes=cubes,
            aggregate=aggregate,
            top_n=1,
            rank_cache_fetched_at=None,
            rank_cache_refreshed=False,
        )
        html_report = build_report_html(
            run_at=run_at,
            cubes=cubes,
            aggregate=aggregate,
            top_n=1,
            rank_cache_fetched_at=None,
            rank_cache_refreshed=False,
        )

        cash_item = aggregate["cash_item"]
        self.assertEqual("CASH", cash_item["stock_symbol"])
        self.assertEqual("现金", cash_item["stock_name"])
        self.assertTrue(cash_item["is_cash"])
        self.assertEqual(30.0, cash_item["total_weight_pct"])
        self.assertEqual(15.0, cash_item["composite_weight_pct"])
        self.assertEqual(2, cash_item["holding_cube_count"])
        self.assertEqual(2, cash_item["composite_rank"])
        self.assertEqual([20.0, 10.0], [
            row["weight_pct"]
            for row in aggregate["holding_rows"]
            if row.get("is_cash")
        ])
        self.assertIn("非现金持仓合计权重: 85.00%", report)
        self.assertIn("现金综合权重: 15.00%", report)
        self.assertIn("| 2 | CASH | 现金 | 15.00%", report)
        self.assertIn("现金综合权重", html_report)
        self.assertIn("CASH", html_report)
        self.assertIn("15.00%", html_report)

    def test_report_shows_top12_even_when_strategy_targets_top10(self):
        run_at = datetime(2026, 6, 22, 12, 0, 0)
        cubes = [CubeInfo(year_rank=1, symbol="ZH000001", cube_name="组合一")]
        ranking = [
            {
                "composite_rank": index,
                "stock_symbol": f"SH.{600000 + index:06d}",
                "stock_name": f"股票{index}",
                "composite_weight_pct": 13.0 - index,
                "holding_cube_count": 1,
                "holding_cube_ratio_pct": 100.0,
                "average_weight_pct": 13.0 - index,
                "example_cubes": ["组合一"],
            }
            for index in range(1, 13)
        ]
        target_items = [
            {
                **item,
                "strategy_rank": item["composite_rank"],
                "top_normalized_weight_pct": 10.0,
                "rebalance_weight_pct": 10.0,
                "strategy_action": "keep",
                "current_weight_pct": 10.0,
            }
            for item in ranking[:10]
        ]
        aggregate = {
            "ranking": ranking,
            "failed_results": [],
            "success_count": 1,
            "total_stock_weight_pct": 100.0,
        }
        strategy_plan = {
            "strategy_name": "Top10等权 + 跌出Top12才卖",
            "top_n": 10,
            "sell_rank": 12,
            "target_items": target_items,
            "summary": {},
        }

        report = build_report(
            run_at=run_at,
            cubes=cubes,
            aggregate=aggregate,
            top_n=10,
            rank_cache_fetched_at=None,
            rank_cache_refreshed=False,
            strategy_plan=strategy_plan,
        )
        html_report = build_report_html(
            run_at=run_at,
            cubes=cubes,
            aggregate=aggregate,
            top_n=10,
            rank_cache_fetched_at=None,
            rank_cache_refreshed=False,
            strategy_plan=strategy_plan,
        )

        self.assertIn("雪球年榜1000组合综合持仓权重 Top12", report)
        self.assertIn("邮件展示: 综合排名 Top12；目标权重只计算 Top10。", report)
        self.assertIn("| 10 | SH.600010 | 股票10 | 3.00% | 10.00% | 10.00% | keep | 10.00%", report)
        self.assertIn("| 11 | SH.600011 | 股票11 | 2.00% | - | - |  | -", report)
        self.assertIn("| 12 | SH.600012 | 股票12 | 1.00% | - | - |  | -", report)
        self.assertIn("<h1>雪球年榜1000组合综合持仓权重 Top12</h1>", html_report)
        self.assertIn("<td class=\"num\">11</td><td>SH.600011</td>", html_report)
        self.assertIn("<td class=\"num\">12</td><td>SH.600012</td>", html_report)

    def test_save_xueqiu_cube_rank_history_to_duckdb_replaces_same_day_rank(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        fetched_at = datetime(2026, 6, 23, 18, 0, 0)
        try:
            with patch("src.robot.xueqiu_top_holdings_report.ANALYTICS_DB_PATH", path):
                first_result = save_xueqiu_cube_rank_history_to_duckdb(
                    [
                        CubeInfo(year_rank=1, symbol="ZH000001", cube_id=101, cube_name="旧一"),
                        CubeInfo(year_rank=2, symbol="ZH000002", cube_id=102, cube_name="旧二"),
                    ],
                    fetched_at,
                )
                second_result = save_xueqiu_cube_rank_history_to_duckdb(
                    [
                        CubeInfo(year_rank=1, symbol="ZH000002", cube_id=102, cube_name="新二"),
                        CubeInfo(year_rank=2, symbol="ZH000003", cube_id=103, cube_name="新三"),
                    ],
                    fetched_at,
                )
                baselines = load_recent_xueqiu_rank_history_cube_sets(
                    limit=5,
                    exclude_date=date(2026, 6, 24),
                )

                connection = connect_duckdb(path, prefer_read_only=False)
                try:
                    rows = connection.execute(
                        f"""
                        SELECT
                            CAST(rank_date AS VARCHAR),
                            year_rank,
                            cube_symbol,
                            cube_id,
                            cube_name
                        FROM {XUEQIU_CUBE_RANK_HISTORY_TABLE}
                        ORDER BY year_rank
                        """
                    ).fetchall()
                finally:
                    connection.close()

            self.assertEqual(2, first_result["saved_rows"])
            self.assertEqual(2, second_result["saved_rows"])
            self.assertEqual(
                [
                    ("2026-06-23", 1, "ZH000002", 102, "新二"),
                    ("2026-06-23", 2, "ZH000003", 103, "新三"),
                ],
                rows,
            )
            self.assertEqual(
                [("rank_history:2026-06-23", {"ZH000002", "ZH000003"})],
                baselines,
            )
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

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
