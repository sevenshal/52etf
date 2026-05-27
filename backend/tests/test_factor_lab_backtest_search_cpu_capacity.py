from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException

from src.app.api import factor_lab


class FactorBacktestSearchCpuCapacityTest(TestCase):
    def test_low_cpu_allows_cases_within_per_core_limit(self):
        with patch.object(factor_lab, "_effective_cpu_count", return_value=2):
            self.assertEqual(2, factor_lab._ensure_backtest_search_cpu_capacity(8))

    def test_low_cpu_rejects_cases_above_per_core_limit(self):
        with patch.object(factor_lab, "_effective_cpu_count", return_value=2):
            with self.assertRaises(HTTPException) as raised:
                factor_lab._ensure_backtest_search_cpu_capacity(9)

        self.assertEqual(409, raised.exception.status_code)
        self.assertIn("批量搜参组合数为 9", raised.exception.detail)

    def test_four_or_more_cpus_allow_large_search(self):
        with patch.object(factor_lab, "_effective_cpu_count", return_value=4):
            self.assertEqual(4, factor_lab._ensure_backtest_search_cpu_capacity(100))
