from unittest import TestCase
from unittest.mock import patch


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.added = []

    def merge(self, _obj):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def execute(self, *_args, **_kwargs):
        return None

    def add(self, obj):
        self.added.append(obj)

    def close(self):
        self.closed = True


class _FailingEVCService:
    def get_stock_tags(self):
        return []

    def search_stock(self, **_kwargs):
        raise RuntimeError("EVC search_stock failed on page=1 size=60: connection reset")


class EVCStockFetchFailureTest(TestCase):
    def test_fetch_and_stocks_raises_when_stock_page_fetch_fails(self):
        from src.robot.evc_manager import EVCManager

        fake_session = _FakeSession()

        with patch("src.robot.evc_manager.Session", return_value=fake_session), patch(
            "src.robot.evc_manager.EVCService", return_value=_FailingEVCService()
        ):
            manager = EVCManager()

            with self.assertRaisesRegex(RuntimeError, "page 1"):
                manager.fetch_and_stocks()

        self.assertTrue(fake_session.closed)
        self.assertEqual([], fake_session.added)
