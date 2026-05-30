from datetime import date
from unittest import TestCase
from unittest.mock import patch


class ScheduledTaskETFHoldingsTest(TestCase):
    def test_etf_holdings_sync_without_start_date_runs_latest_ingest(self):
        from src.robot import scheduled_tasks

        instances = []

        class FakeLatestIngest:
            def __init__(self):
                self.closed = False
                instances.append(self)

            def sync_latest(self):
                return {"saved": 3, "skipped": 0, "errors": [], "saved_dates": {"SPY.US": "2026-05-29"}}

            def close(self):
                self.closed = True

        with patch("src.robot.etf_holdings_backfill.ETFHoldingsLatestIngest", FakeLatestIngest):
            result = scheduled_tasks._run_etf_holdings_sync()

        assert result is None
        assert len(instances) == 1
        assert instances[0].closed is True

    def test_etf_holdings_sync_with_start_date_runs_historical_backfill(self):
        from src.robot import scheduled_tasks

        instances = []

        class FakeHistoricalBackfill:
            def __init__(self):
                self.closed = False
                self.start_date = None
                instances.append(self)

            def backfill(self, start_date=None):
                self.start_date = start_date
                return {
                    "start_date": "2026-05-01",
                    "end_date": "2026-05-30",
                    "saved": 5,
                    "skipped": 0,
                    "errors": [],
                }

            def close(self):
                self.closed = True

        with patch("src.robot.etf_holdings_backfill.ETFHistoricalHoldingsBackfill", FakeHistoricalBackfill):
            result = scheduled_tasks._run_etf_holdings_sync(start_date="2026-05-01")

        assert result == "ETF historical holdings backfill range=2026-05-01~2026-05-30 saved=5 skipped=0"
        assert len(instances) == 1
        assert instances[0].start_date == date(2026, 5, 1)
        assert instances[0].closed is True

    def test_etf_holdings_backfill_is_single_start_date_enabled_task(self):
        from src.robot.scheduled_tasks import ScheduledTaskManager

        manager = ScheduledTaskManager()
        assert "etf_holdings_backfill" in manager.task_definitions
        assert "etf_historical_holdings_backfill" not in manager.task_definitions

        config = {
            "task_key": "etf_holdings_backfill",
            "name": "美股ETF持仓同步",
            "description": "test",
            "enabled": True,
            "schedule_time": "05:30",
            "cron_rule": "30 5 * * *",
            "timezone": "Asia/Shanghai",
            "allow_queue": True,
            "sort_order": 15,
            "last_trigger_source": None,
            "last_run_started_at": None,
            "last_run_finished_at": None,
            "last_run_status": None,
            "last_run_message": None,
            "last_duration_seconds": None,
            "updated_by": None,
            "created_at": None,
            "updated_at": None,
        }

        snapshot = manager._serialize_task(config, jobs=[], is_running=False)
        assert snapshot["supports_start_date"] is True
