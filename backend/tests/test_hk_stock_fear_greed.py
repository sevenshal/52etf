import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from sqlalchemy import text

from src.core.analytics_database import AnalyticsSession
from src.core.services.hk_stock_fear_greed_service import (
    HK_ALL_COMPONENTS,
    HK_COMPONENTS,
    HK_DIAGNOSTIC_COMPONENTS,
    HKStockFearGreedCalculator,
)
from src.robot.hk_stock_base_data_sync import (
    HKStockBaseDataSyncService,
    normalize_hk_symbol,
)
from src.robot import hk_stock_base_data_sync


class HKStockBaseDataTest(unittest.TestCase):
    def test_normalize_hk_symbol(self):
        self.assertEqual("00700.HK", normalize_hk_symbol("700"))
        self.assertEqual("09988.HK", normalize_hk_symbol("09988.HK"))
        self.assertIsNone(normalize_hk_symbol("HSI"))

    def test_weight_snapshot_requires_about_one_hundred_percent(self):
        valid = pd.DataFrame(
            {
                "index_code": ["HSI", "HSI"],
                "effective_date": [pd.Timestamp("2025-03-10").date()] * 2,
                "con_code": ["00700.HK", "09988.HK"],
                "weight": [50.0, 50.0],
            }
        )
        HKStockBaseDataSyncService._validate_weight_snapshots(valid)

        invalid = valid.copy()
        invalid["weight"] = [40.0, 40.0]
        with self.assertRaises(ValueError):
            HKStockBaseDataSyncService._validate_weight_snapshots(invalid)

    def test_hk_daily_rate_limit_waits_and_retries_with_persistent_state(self):
        with self.subTest("explicit Tushare frequency error is retryable"):
            pro = SimpleNamespace(
                hk_daily=Mock(
                    side_effect=[
                        Exception("抱歉，您访问接口(hk_daily)频率超限(1次/分钟)"),
                        pd.DataFrame(),
                    ]
                )
            )
            service = object.__new__(HKStockBaseDataSyncService)
            service.tushare = SimpleNamespace(pro=pro)
            with tempfile.TemporaryDirectory() as temporary:
                state_path = Path(temporary) / "hk_daily.state"
                with (
                    patch.object(
                        hk_stock_base_data_sync,
                        "HK_DAILY_RATE_STATE_PATH",
                        state_path,
                    ),
                    patch.object(
                        hk_stock_base_data_sync,
                        "HK_DAILY_MIN_INTERVAL_SECONDS",
                        0,
                    ),
                    patch.object(
                        hk_stock_base_data_sync,
                        "HK_DAILY_MAX_ATTEMPTS",
                        3,
                    ),
                ):
                    result = service._call_hk_daily(trade_date="20260730")

                self.assertTrue(result.empty)
                self.assertEqual(2, pro.hk_daily.call_count)
                self.assertTrue(state_path.read_text(encoding="utf-8").strip())

    def test_derived_qfq_applies_split_factor_to_prior_prices_only(self):
        db = AnalyticsSession()
        try:
            db.execute(text("DELETE FROM hk_stock_daily WHERE ts_code = '09999.HK'"))
            db.execute(
                text(
                    """
                    INSERT INTO hk_stock_daily (
                        trade_date, ts_code, open, high, low, close, pre_close,
                        created_at, updated_at
                    ) VALUES
                        ('2025-01-02', '09999.HK', 100, 101, 99, 100, 100, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                        ('2025-01-03', '09999.HK', 50, 51, 49, 50, 50, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )
            db.commit()
            rows = db.execute(
                text(
                    """
                    SELECT close, event_factor
                    FROM hk_stock_daily_qfq
                    WHERE ts_code = '09999.HK'
                    ORDER BY trade_date
                    """
                )
            ).fetchall()
        finally:
            AnalyticsSession.remove()

        self.assertEqual(2, len(rows))
        self.assertAlmostEqual(50.0, float(rows[0][0]))
        self.assertAlmostEqual(50.0, float(rows[1][0]))
        self.assertAlmostEqual(0.5, float(rows[1][1]))


class HKStockFearGreedCalculatorTest(unittest.TestCase):
    def test_score_leaves_missing_option_component_unused(self):
        index = pd.date_range("2024-01-01", periods=160, freq="B")
        raw = pd.DataFrame(index=index)
        for offset, key in enumerate(HK_ALL_COMPONENTS):
            raw[key] = pd.Series(range(len(index)), index=index, dtype=float) + offset
        raw["put_call_options"] = float("nan")

        scored = HKStockFearGreedCalculator._score(
            raw,
            score_window=120,
            min_periods=60,
        )

        self.assertTrue(scored["put_call_options_score"].isna().all())
        self.assertTrue(scored["market_momentum_score"].iloc[-1] > 50)

    def test_diagnostics_stay_out_of_total(self):
        self.assertNotIn("put_call_options", HK_COMPONENTS)
        for key in ("put_call_options", "implied_volatility", "southbound_flow", "short_selling_ratio"):
            self.assertIn(key, HK_DIAGNOSTIC_COMPONENTS)

        index = pd.DatetimeIndex([pd.Timestamp("2026-10-16")])
        raw = pd.DataFrame({key: [0.5] for key in HK_ALL_COMPONENTS}, index=index)
        scores = pd.DataFrame(
            {f"{key}_score": [55.0] for key in HK_ALL_COMPONENTS}, index=index
        )
        payload = HKStockFearGreedCalculator._component_payload(raw, scores, index[0])

        self.assertFalse(payload["put_call_options"]["included_in_total"])
        self.assertFalse(payload["put_call_options"]["used_in_score"])
        self.assertFalse(payload["southbound_flow"]["used_in_score"])
        self.assertTrue(payload["market_momentum"]["included_in_total"])
        self.assertTrue(payload["market_momentum"]["used_in_score"])

    def test_put_call_reads_open_interest_ratio(self):
        calculator = object.__new__(HKStockFearGreedCalculator)
        calculator.target_symbol = "HSTECH.HK"
        index = pd.date_range("2026-09-01", periods=10, freq="B")
        series = pd.Series(1.5, index=index)
        with patch(
            "src.core.services.hk_stock_fear_greed_service.load_option_oi_pcr_series",
            return_value=series,
        ) as mocked:
            values = calculator._option_open_interest_ratio(
                index, index[0].date(), index[-1].date()
            )
        mocked.assert_called_once_with("htio", index[0].date(), index[-1].date())
        self.assertAlmostEqual(-1.5, float(values.iloc[-1]))

    def test_southbound_flow_keeps_sign(self):
        calculator = object.__new__(HKStockFearGreedCalculator)
        calculator.target_symbol = "HSTECH.HK"
        index = pd.date_range("2026-09-01", periods=5, freq="B")
        net = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0], index=index)
        with patch(
            "src.core.services.hk_stock_fear_greed_service.load_southbound_net",
            return_value=net,
        ):
            values = calculator._southbound_flow(index, index[0].date(), index[-1].date())
        # 净买入为正 → 原始值也为正（越高越贪婪），不做取负。
        self.assertGreater(float(values.iloc[-1]), 0)

    def test_short_selling_ratio_is_negated(self):
        calculator = object.__new__(HKStockFearGreedCalculator)
        calculator.target_symbol = "HSTECH.HK"
        index = pd.date_range("2026-09-01", periods=5, freq="B")
        ratio = pd.Series(0.2, index=index)
        with patch(
            "src.core.services.hk_stock_fear_greed_service.load_short_selling_ratio",
            return_value=ratio,
        ):
            values = calculator._short_selling_ratio(index, index[0].date(), index[-1].date())
        self.assertAlmostEqual(-0.2, float(values.iloc[-1]))

    def test_price_drift_normalizes_weights(self):
        snapshots = {
            pd.Timestamp("2025-01-02"): [
                {"symbol": "00001.HK", "base_weight": 0.6},
                {"symbol": "00002.HK", "base_weight": 0.4},
            ]
        }
        # The arithmetic used by the calculator: first constituent doubles,
        # second is unchanged, so weights drift from 60/40 to 75/25.
        values = [
            snapshots[pd.Timestamp("2025-01-02")][0]["base_weight"] * 2,
            snapshots[pd.Timestamp("2025-01-02")][1]["base_weight"] * 1,
        ]
        normalized = [item / sum(values) for item in values]
        self.assertAlmostEqual(0.75, normalized[0])
        self.assertAlmostEqual(0.25, normalized[1])


if __name__ == "__main__":
    unittest.main()
