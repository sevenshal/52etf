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
        builder._weighted_daily_return = MagicMock(return_value=0.0)
        builder._bulk_upsert = MagicMock()
        builder._progress = MagicMock()

        result = builder.refresh_incremental(end_date=date(2026, 7, 1))

        builder._save_rebalance.assert_called_once()
        save_kwargs = builder._save_rebalance.call_args.kwargs
        self.assertEqual(save_kwargs["rebalance_date"], date(2026, 6, 30))
        self.assertEqual(save_kwargs["effective_date"], date(2026, 7, 1))
        builder._weighted_daily_return.assert_called_once_with(
            market_frame,
            {"NEW.SZ": 1.0},
        )
        self.assertEqual(result["levels_saved"], 1)
        self.assertEqual(result["rebalances_saved"], 1)

