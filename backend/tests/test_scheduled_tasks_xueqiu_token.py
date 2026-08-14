from dataclasses import dataclass
from datetime import datetime, timedelta
from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch


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

    def test_process_xueqiu_token_freshness_uses_session_safe_snapshots(self):
        from src.core.services.xueqiu_token_monitor import process_xueqiu_token_freshness_check_for_robot

        state = {"closed": False}

        class ExpiringRow:
            def __init__(self):
                self._data = {
                    "account_id": "account-1",
                    "xueqiu_cookie": "xq_a_token=fresh;",
                    "updated_at": datetime.now() - timedelta(hours=1),
                }

            def __getattr__(self, name):
                if name in self._data:
                    if state["closed"]:
                        raise RuntimeError("detached row access")
                    return self._data[name]
                raise AttributeError(name)

        class FakeQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def all(self):
                return [ExpiringRow()]

        class FakeDB:
            def query(self, *_args, **_kwargs):
                return FakeQuery()

        @contextmanager
        def fake_get_db_ctx():
            try:
                yield FakeDB()
            finally:
                state["closed"] = True

        with patch("src.core.services.xueqiu_token_monitor.get_db_ctx", fake_get_db_ctx):
            result = process_xueqiu_token_freshness_check_for_robot(max_age_hours=2)

        self.assertIn("status=OK", result)
        self.assertIn("max_age=2h", result)

    def test_scheduled_task_replaces_ptrade_heartbeat_check(self):
        from src.robot.scheduled_tasks import ScheduledTaskManager

        manager = ScheduledTaskManager()

        self.assertIn("xueqiu_token_freshness_check", manager.task_definitions)
        self.assertNotIn("snowball_ptrade_heartbeat_check", manager.task_definitions)
        task = manager.task_definitions["xueqiu_token_freshness_check"]
        self.assertEqual("09:00", task.default_time)
        self.assertEqual("0 9 * * *", task.default_cron_rule)

    def test_xueqiu_cache_refresh_task_exposes_editable_parameters(self):
        from src.robot.scheduled_tasks import ScheduledTaskManager

        manager = ScheduledTaskManager()
        task = manager.task_definitions["xueqiu_top_holdings_cache_refresh"]
        schema_keys = [definition.key for definition in task.parameter_schema]

        self.assertIn("rank_limit", schema_keys)
        self.assertIn("rank_drift_min_overlap_pct", schema_keys)
        self.assertIn("activity_cache_ttl_hours", schema_keys)
        self.assertNotIn("active_rebalance_days", schema_keys)

        params = manager._normalize_task_parameters(
            task,
            {
                "rank_limit": 800,
                "rank_drift_min_overlap_pct": 65,
                "activity_cache_ttl_hours": 12,
            },
            strict=True,
        )

        self.assertEqual(800, params["rank_limit"])
        self.assertEqual(65.0, params["rank_drift_min_overlap_pct"])
        self.assertEqual(12.0, params["activity_cache_ttl_hours"])

        with self.assertRaisesRegex(ValueError, "榜单Top N"):
            manager._normalize_task_parameters(task, {"rank_limit": 20}, strict=True)

    def test_xueqiu_rebalance_task_exposes_top_n_and_active_days_parameters(self):
        from src.robot.scheduled_tasks import ScheduledTaskManager

        manager = ScheduledTaskManager()
        task = manager.task_definitions["xueqiu_top_holdings_rebalance"]
        schema_keys = [definition.key for definition in task.parameter_schema]

        self.assertIn("top_n", schema_keys)
        self.assertIn("active_rebalance_days", schema_keys)
        self.assertIn("sell_rank", schema_keys)

        params = manager._normalize_task_parameters(
            task,
            {
                "top_n": 8,
                "active_rebalance_days": 380,
                "sell_rank": 13,
            },
            strict=True,
        )

        self.assertEqual(8, params["top_n"])
        self.assertEqual(380, params["active_rebalance_days"])
        self.assertEqual(13, params["sell_rank"])

        with self.assertRaisesRegex(ValueError, "目标Top N"):
            manager._normalize_task_parameters(task, {"top_n": 100}, strict=True)

    def test_all_scheduled_tasks_expose_editable_parameters(self):
        from src.robot.scheduled_tasks import ScheduledTaskManager

        manager = ScheduledTaskManager()
        expected_keys = {
            "evc_stock_fetch": {"page_size", "max_pages", "fetch_tags"},
            "evc_static_info_sync": {"start_date", "end_date"},
            "us_stock_industry_sync": {"candidate_etfs", "limit", "force_refresh"},
            "etf_fair_value_analysis": {"symbols"},
            "etf_holdings_backfill": {"start_date"},
            "etf_put_call_ratio_sync": {"full", "page_limit", "recent_limit", "expirations_limit", "sleep_seconds"},
            "cnn_fear_greed_fetch": {"start_date"},
            "soxx_fear_greed_backfill": {
                "start_date",
                "end_date",
                "recent_days",
            },
            "a_stock_base_data_sync": {"start_date", "end_date", "incremental"},
            "a_stock_innovation100_rebuild": {"start_date", "end_date", "full_rebuild"},
            "a_stock_etf_fear_greed_backfill": {
                "start_date",
                "end_date",
                "recent_days",
            },
            "a_stock_index_valuation_refresh": set(),
            "a_stock_fear_greed_intraday": {"symbols"},
            "hk_stock_base_data_sync": {
                "start_date",
                "end_date",
                "max_market_days",
                "weight_manifest_path",
                "download_review_documents",
                "review_cache_dir",
                "auto_process_reviews",
                "review_discovery_lookback_days",
            },
            "hk_index_fear_greed_backfill": {
                "start_date",
                "end_date",
                "recent_days",
            },
            "external_trading_fee_reconcile": {"check_date", "skip_if_already_succeeded"},
            "xueqiu_token_freshness_check": {"max_age_hours"},
            "external_trading_sub_account_nav_snapshot": {"trading_date", "timeout_seconds"},
            "xueqiu_top_holdings_rebalance": {"top_n", "active_rebalance_days", "sell_rank"},
            "xueqiu_top_holdings_cache_refresh": {
                "rank_limit",
                "rank_drift_min_overlap_pct",
                "activity_cache_ttl_hours",
                "activity_request_min_interval_ms",
            },
        }

        self.assertEqual(set(manager.task_definitions), set(expected_keys))
        for task_key, keys in expected_keys.items():
            task = manager.task_definitions[task_key]
            schema_keys = {definition.key for definition in task.parameter_schema}
            self.assertEqual(keys, schema_keys, task_key)
