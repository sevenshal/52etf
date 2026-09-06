from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock

import polars as pl

from src.robot.a_stock_innovation100 import AStockInnovation100Builder


class AStockInnovation100IncrementalTest(TestCase):
    def test_incremental_backfills_quarter_end_already_persisted_as_level(self):
        builder = object.__new__(AStockInnovation100Builder)
        builder.db = MagicMock()
        query = builder.db.query.return_value.filter.return_value
        query.count.side_effect = [1, 2]
        query.first.return_value = None

        old_constituents = [{"ts_code": "OLD.SZ", "weight": 1.0, "circ_mv": 100.0}]
        new_constituents = [{"ts_code": "NEW.SZ", "weight": 1.0, "circ_mv": 120.0}]
        latest_level = SimpleNamespace(date=date(2026, 6, 30), level=1000.0)
        builder._load_incremental_state = MagicMock(
            return_value={
                "latest_level": latest_level,
                "level": 1000.0,
                "high_watermark": 1000.0,
                "current_constituents": old_constituents,
                "current_weight_map": {"OLD.SZ": 1.0},
                "pending_constituents": None,
                "pending_effective_date": None,
            }
        )
        trading_dates = [date(2026, 6, 30), date(2026, 7, 1)]
        builder._cached_market_trading_dates = MagicMock(return_value=trading_dates)
        builder._existing_market_day_stats = MagicMock(return_value={})
        builder._market_day_needs_refresh = MagicMock(return_value=False)
        builder._load_basic_map = MagicMock(return_value={})
        builder._load_st_intervals = MagicMock(return_value={})
        market_frame = pl.DataFrame({"ts_code": ["OLD.SZ"], "amount": [1.0]})
        builder._iter_market_frames_by_date = MagicMock(
            return_value=iter(
                [
                    (0, trading_dates[0], market_frame),
                    (1, trading_dates[1], market_frame),
                ]
            )
        )
        builder._update_amount_history = MagicMock()
        builder._rank_candidates = MagicMock(return_value=new_constituents)
        builder._select_constituents = MagicMock(return_value=new_constituents)
        builder._build_weighted_constituents = MagicMock(return_value=(new_constituents, 100.0))
        builder._save_rebalance = MagicMock()
        builder._advance_weights = MagicMock(
            side_effect=[(0.0, {"OLD.SZ": 1.0}), (0.0, {"NEW.SZ": 1.0})]
        )
        builder._bulk_upsert = MagicMock()
        builder._progress = MagicMock()

        result = builder.refresh_incremental(end_date=date(2026, 7, 1))

        builder._save_rebalance.assert_called_once()
        save_kwargs = builder._save_rebalance.call_args.kwargs
        self.assertEqual(save_kwargs["rebalance_date"], date(2026, 6, 30))
        self.assertEqual(save_kwargs["effective_date"], date(2026, 7, 1))
        # 生效日当天要用新成分的权重推进点位，而不是上一期的权重。
        self.assertEqual(
            builder._advance_weights.call_args_list[-1].args[:2],
            (market_frame, {"NEW.SZ": 1.0}),
        )
        self.assertEqual(result["levels_saved"], 1)
        self.assertEqual(result["rebalances_saved"], 1)

    def test_incremental_replays_weight_drift_since_current_effective_date(self):
        """续算要从当期成分生效日重放漂移，不能直接拿调仓日权重接最新点位。"""
        builder = object.__new__(AStockInnovation100Builder)
        builder.db = MagicMock()
        query = builder.db.query.return_value.filter.return_value
        query.count.side_effect = [3, 3]
        query.first.return_value = object()  # 区间内的调仓已经落库，不再重复保存

        constituents = [
            {"ts_code": "AAA.SZ", "weight": 0.5, "circ_mv": 100.0},
            {"ts_code": "BBB.SZ", "weight": 0.5, "circ_mv": 100.0},
        ]
        builder._load_incremental_state = MagicMock(
            return_value={
                "latest_level": SimpleNamespace(date=date(2026, 7, 2), level=1000.0),
                "level": 1000.0,
                "high_watermark": 1000.0,
                "current_constituents": constituents,
                "current_weight_map": {"AAA.SZ": 0.5, "BBB.SZ": 0.5},
                "current_effective_date": date(2026, 7, 1),
                "pending_constituents": None,
                "pending_effective_date": None,
            }
        )
        trading_dates = [date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
        builder._cached_market_trading_dates = MagicMock(return_value=trading_dates)
        builder._existing_market_day_stats = MagicMock(return_value={})
        builder._market_day_needs_refresh = MagicMock(return_value=False)
        builder._load_basic_map = MagicMock(return_value={})
        builder._load_st_intervals = MagicMock(return_value={})

        def frame_for(aaa_pct):
            return pl.DataFrame({
                "ts_code": ["AAA.SZ", "BBB.SZ"],
                "amount": [1.0, 1.0],
                "pct_chg": [aaa_pct, 0.0],
            })

        # AAA 在 7/1、7/2、7/3 各涨 10%，到 7/3 时它的权重应已从 0.5 漂到 0.5*1.1^2 的口径上。
        frames = [frame_for(0.0), frame_for(10.0), frame_for(10.0), frame_for(10.0)]
        builder._iter_market_frames_by_date = MagicMock(
            return_value=iter(list(zip(range(4), trading_dates, frames)))
        )
        builder._update_amount_history = MagicMock()
        builder._bulk_upsert = MagicMock()
        builder._progress = MagicMock()

        result = builder.refresh_incremental(end_date=date(2026, 7, 3))

        self.assertEqual(result["levels_saved"], 1)
        saved_levels = builder._bulk_upsert.call_args.args[1]
        self.assertEqual(len(saved_levels), 1)
        self.assertEqual(saved_levels[0]["date"], date(2026, 7, 3))
        # 6/30 在生效日之前不参与漂移；7/1、7/2 各推进一次后 AAA 权重 = 0.5*1.1^2 / (0.5*1.1^2 + 0.5)。
        # 权重固定不变的旧口径会得到 0.5 * 10% = 5.0，明显偏低。
        expected_aaa_weight = 0.5 * 1.1 * 1.1 / (0.5 * 1.1 * 1.1 + 0.5)
        self.assertAlmostEqual(
            saved_levels[0]["daily_return_pct"],
            round(expected_aaa_weight * 10.0, 6),
            places=6,
        )

