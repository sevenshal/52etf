from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from src.app.api import factor_lab


class FactorLiveSignalDateTest(TestCase):
    def _config(self):
        return SimpleNamespace(
            request_payload={
                "pool": "SPY_QQQ",
                "rebalance_frequency": "weekly",
            }
        )

    def test_weekend_request_resolves_to_previous_weekly_signal_day(self):
        with patch.object(
            factor_lab,
            "_factor_live_trading_day_checker",
            return_value=lambda value: value.weekday() < 5,
        ):
            signal_date, latest_trading_date = factor_lab._factor_live_resolve_requested_signal_date(
                self._config(),
                date(2026, 5, 30),
            )

        self.assertEqual(date(2026, 5, 29), signal_date)
        self.assertEqual(date(2026, 5, 29), latest_trading_date)

    def test_trading_day_request_still_requires_that_day_to_be_signal_day(self):
        with patch.object(
            factor_lab,
            "_factor_live_trading_day_checker",
            return_value=lambda value: value.weekday() < 5,
        ):
            signal_date, latest_trading_date = factor_lab._factor_live_resolve_requested_signal_date(
                self._config(),
                date(2026, 6, 3),
            )

        self.assertIsNone(signal_date)
        self.assertEqual(date(2026, 6, 3), latest_trading_date)

    def test_not_signal_day_message_mentions_resolved_trading_day(self):
        message = factor_lab._factor_live_not_signal_day_message(
            self._config(),
            date(2026, 5, 30),
            date(2026, 5, 29),
        )

        self.assertEqual("2026-05-30 及之前最近交易日 2026-05-29 不是每周信号日", message)
