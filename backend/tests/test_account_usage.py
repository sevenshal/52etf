from datetime import date
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.app.api import account as account_api
from src.core.database import Base, WebAccount, WebAccountDailyUsage


class AccountUsageTest(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.session_patch = patch.object(account_api, "SessionLocal", self.session_factory)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.engine.dispose)

        db = self.session_factory()
        try:
            db.add(WebAccount(account_id="enabled-account", enabled=True))
            db.add(WebAccount(account_id="disabled-account", enabled=False))
            db.commit()
        finally:
            db.close()

    def test_records_and_aggregates_daily_requests_for_enabled_accounts(self):
        usage_date = date(2026, 8, 9)
        with patch.object(account_api, "_shanghai_today", return_value=usage_date):
            account_api.record_account_request("enabled-account")
            account_api.record_account_request("enabled-account")
            account_api.record_account_request("disabled-account")
            account_api.record_account_request("unknown-account")

        db = self.session_factory()
        try:
            usage = db.get(WebAccountDailyUsage, ("enabled-account", usage_date))
            self.assertIsNotNone(usage)
            self.assertEqual(2, usage.request_count)
            self.assertIsNone(db.get(WebAccountDailyUsage, ("disabled-account", usage_date)))
            self.assertIsNone(db.get(WebAccountDailyUsage, ("unknown-account", usage_date)))
        finally:
            db.close()

    def test_returns_persisted_history_for_a_selected_account_and_date_range(self):
        with patch.object(account_api, "_shanghai_today", return_value=date(2026, 8, 8)):
            account_api.record_account_request("enabled-account")
        with patch.object(account_api, "_shanghai_today", return_value=date(2026, 8, 9)):
            account_api.record_account_request("enabled-account")
            account_api.record_account_request("enabled-account")

        rows = account_api.list_account_usage(
            account_id="enabled-account",
            start_date=date(2026, 8, 8),
            end_date=date(2026, 8, 9),
            _="admin-account",
        )

        self.assertEqual(
            [
                ("2026-08-09", 2),
                ("2026-08-08", 1),
            ],
            [(item.usage_date, item.request_count) for item in rows],
        )
