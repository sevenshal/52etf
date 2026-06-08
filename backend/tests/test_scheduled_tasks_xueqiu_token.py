from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest import TestCase


@dataclass
class FakeSnowballAccountConfig:
    account_id: str
    xueqiu_cookie: str
    updated_at: datetime


class XueqiuTokenMonitorTest(TestCase):
    def test_evaluate_xueqiu_token_freshness_ok_with_recent_token(self):
        from src.core.services.xueqiu_token_monitor import evaluate_xueqiu_token_freshness

        now = datetime(2026, 6, 8, 9, 0, 0)
        result = evaluate_xueqiu_token_freshness(
            [
                FakeSnowballAccountConfig(
                    account_id="account-1",
                    xueqiu_cookie="xq_a_token=fresh;",
                    updated_at=now - timedelta(hours=6),
                )
            ],
            now=now,
        )

        self.assertEqual("OK", result["status"])
        self.assertEqual(6.0, result["age_hours"])

    def test_evaluate_xueqiu_token_freshness_stale_after_24_hours(self):
        from src.core.services.xueqiu_token_monitor import evaluate_xueqiu_token_freshness

        now = datetime(2026, 6, 8, 9, 0, 0)
        result = evaluate_xueqiu_token_freshness(
            [
                FakeSnowballAccountConfig(
                    account_id="account-1",
                    xueqiu_cookie="xq_a_token=old;",
                    updated_at=now - timedelta(hours=24, minutes=1),
                )
            ],
            now=now,
        )

        self.assertEqual("STALE", result["status"])
        self.assertEqual(24.02, result["age_hours"])

    def test_evaluate_xueqiu_token_freshness_missing_when_no_cookie(self):
        from src.core.services.xueqiu_token_monitor import evaluate_xueqiu_token_freshness

        result = evaluate_xueqiu_token_freshness(
            [],
            now=datetime(2026, 6, 8, 9, 0, 0),
        )

        self.assertEqual("MISSING", result["status"])
        self.assertIsNone(result["updated_at"])
        self.assertIsNone(result["age_hours"])

    def test_evaluate_xueqiu_token_freshness_missing_when_cookie_has_no_xq_token(self):
        from src.core.services.xueqiu_token_monitor import evaluate_xueqiu_token_freshness

        result = evaluate_xueqiu_token_freshness(
            [
                FakeSnowballAccountConfig(
                    account_id="account-1",
                    xueqiu_cookie="device_id=abc;",
                    updated_at=datetime(2026, 6, 8, 8, 0, 0),
                )
            ],
            now=datetime(2026, 6, 8, 9, 0, 0),
        )

        self.assertEqual("MISSING", result["status"])
        self.assertIsNone(result["updated_at"])
        self.assertIsNone(result["age_hours"])

    def test_scheduled_task_replaces_ptrade_heartbeat_check(self):
        from src.robot.scheduled_tasks import ScheduledTaskManager

        manager = ScheduledTaskManager()

        self.assertIn("xueqiu_token_freshness_check", manager.task_definitions)
        self.assertNotIn("snowball_ptrade_heartbeat_check", manager.task_definitions)
        task = manager.task_definitions["xueqiu_token_freshness_check"]
        self.assertEqual("09:00", task.default_time)
        self.assertEqual("0 9 * * *", task.default_cron_rule)
