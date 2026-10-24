from unittest import TestCase
from unittest.mock import patch


class AStockMarginScheduledTaskTest(TestCase):
    def test_single_margin_task_syncs_summary_and_detail(self):
        from src.robot import scheduled_tasks

        summary = {
            "mode": "incremental",
            "start_date": "2026-10-01",
            "end_date": "2026-10-24",
            "saved_rows": 9,
            "trade_dates": 3,
        }
        detail = {
            "start_date": "2025-09-19",
            "end_date": "2026-10-24",
            "saved_rows": 99,
            "trade_dates": 3,
            "errors": [],
        }
        with patch("src.robot.a_stock_base_data_sync.sync_a_stock_margin_data", return_value=summary) as sync_summary, patch(
            "src.robot.a_stock_base_data_sync.sync_a_stock_margin_detail_data", return_value=detail
        ) as sync_detail:
            message = scheduled_tasks._run_a_stock_margin_sync()

        sync_summary.assert_called_once_with(start_date=None, end_date=None, incremental=True)
        sync_detail.assert_called_once_with(start_date=None, end_date=None)
        self.assertIn("summary_rows=9", message)
        self.assertIn("detail_rows=99", message)

    def test_margin_task_runs_weekdays_at_0930_without_base_data_dependency(self):
        from src.robot.scheduled_tasks import ScheduledTaskManager

        tasks = ScheduledTaskManager().task_definitions
        task = tasks["a_stock_margin_sync"]
        self.assertTrue(task.default_enabled)
        self.assertEqual("09:30", task.default_time)
        self.assertEqual("30 9 * * mon-fri", task.default_cron_rule)
        self.assertEqual((), task.depends_on)
        self.assertNotIn("a_stock_margin_detail_sync", tasks)
