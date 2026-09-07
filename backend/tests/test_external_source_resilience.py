"""外部数据源瞬时故障不该带走整个长任务。

覆盖两处：港股指数复核公告的推测式探测，以及A股主力资金流的双数据源兜底。
"""
from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests


class HkReleaseProbeTest(TestCase):
    """hsi.com.hk 的公告探测：网络异常要和 404 同等处理，不能炸掉整个同步。"""

    @staticmethod
    def _probe(*side_effect):
        from src.robot.hk_index_review_automation import HKIndexReviewAutomation

        with patch("src.robot.hk_index_review_automation.requests.get") as mocked, \
             patch("src.robot.hk_index_review_automation.time.sleep"):
            mocked.side_effect = side_effect
            result = HKIndexReviewAutomation._probe_release_url("https://example.invalid/a.pdf")
        return result, mocked.call_count

    def test_connection_error_is_swallowed_after_retries(self):
        """8 个线程并发探测被对端 reset 是常态，探不到就当没探到。"""
        error = requests.exceptions.ConnectionError("Connection aborted.")
        result, calls = self._probe(error, error)
        self.assertIsNone(result)
        self.assertEqual(calls, 2)

    def test_retry_recovers_from_a_single_blip(self):
        response = MagicMock(status_code=200)
        result, calls = self._probe(requests.exceptions.ConnectionError("boom"), response)
        self.assertIs(result, response)
        self.assertEqual(calls, 2)

    def test_successful_response_is_returned_without_retry(self):
        response = MagicMock(status_code=404)
        result, calls = self._probe(response)
        self.assertIs(result, response)
        self.assertEqual(calls, 1)


class FundFlowFallbackTest(TestCase):
    """主力资金流：主源和兜底源同时挂掉时返回带 errors 的结果，而不是抛异常。"""

    def _sync(self, fallback):
        from src.robot import a_stock_fund_flow_sync as module

        with patch.object(module, "resolve_a_stock_fund_flow_trade_dates", side_effect=RuntimeError("tushare 空")), \
             patch.object(module, "sync_current_a_stock_fund_flow_from_eastmoney", side_effect=fallback):
            return module.sync_current_a_stock_fund_flow()

    def test_both_sources_down_returns_errors_instead_of_raising(self):
        result = self._sync(requests.exceptions.ConnectionError("Connection aborted."))
        self.assertEqual(result["saved_rows"], 0)
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("tushare 空", result["errors"][0]["error"])
        self.assertIn("Connection aborted.", result["errors"][1]["error"])

    def test_fallback_success_still_records_the_primary_error(self):
        result = self._sync(lambda: {"saved_rows": 12, "source": "eastmoney_push2"})
        self.assertEqual(result["saved_rows"], 12)
        self.assertIn("tushare 空", result["primary_error"])
        self.assertNotIn("errors", result)


class FundFlowFailureIsWarningTest(TestCase):
    """资金流失败只告警：任务不判失败，但告警要出现在结果消息最前面。"""

    @staticmethod
    def _message(**overrides):
        from src.robot.scheduled_tasks import _format_a_stock_base_data_sync_result

        result = {"status": "completed", "fund_flow_saved_rows": 6000, "fund_flow_errors": 0}
        result.update(overrides)
        return _format_a_stock_base_data_sync_result(result)

    def test_warning_is_prefixed_when_fund_flow_saved_nothing(self):
        message = self._message(
            fund_flow_saved_rows=0,
            fund_flow_errors=2,
            fund_flow_error_detail={"source": "eastmoney_push2", "error": "Connection aborted."},
        )
        self.assertTrue(message.startswith("[警告] 主力资金流同步失败"))
        # 前端结果预览只截前 96 个字符，告警必须落在这个范围里。
        self.assertIn("主力资金流同步失败", message[:96])

    def test_no_warning_on_a_healthy_run(self):
        self.assertFalse(self._message().startswith("[警告]"))

    def test_no_warning_when_rows_were_still_saved(self):
        message = self._message(fund_flow_errors=1, fund_flow_saved_rows=6000)
        self.assertFalse(message.startswith("[警告]"))
