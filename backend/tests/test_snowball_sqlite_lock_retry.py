import asyncio
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from src.app.api import snowball


class SnowballSqliteLockRetryTest(TestCase):
    def test_write_main_db_with_retry_retries_locked_database(self):
        attempts = {"value": 0}

        def writer():
            attempts["value"] += 1
            if attempts["value"] == 1:
                raise OperationalError("UPDATE", {}, Exception("database is locked"))
            return "ok"

        with patch("src.app.api.snowball.time.sleep", lambda _seconds: None):
            result = snowball._write_snowball_main_db_with_retry("test snowball write", writer)

        self.assertEqual("ok", result)
        self.assertEqual(2, attempts["value"])

    def test_sync_returns_failure_when_failure_marker_hits_sqlite_lock(self):
        item = {
            "id": 28,
            "combination_id": "ZH123",
            "external_trading_account_id": 7,
            "account_id": "acct",
        }

        async def fail_sync(_item, *, trigger_source):
            raise ValueError("database is locked")

        def fail_marker(_config_id, _message):
            raise OperationalError("UPDATE", {}, Exception("database is locked"))

        with patch("src.app.api.snowball._load_snowball_external_sync_items", return_value=[item]), patch(
            "src.app.api.snowball._sync_one_snowball_external_target",
            fail_sync,
        ), patch("src.app.api.snowball._mark_snowball_external_sync_failure", fail_marker):
            result = asyncio.run(
                snowball.sync_snowball_external_trading_config_ids(
                    trigger_source="manual",
                    trigger_executor=False,
                )
            )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual(1, result["failed"])
        self.assertEqual("database is locked", result["items"][0]["error"])
