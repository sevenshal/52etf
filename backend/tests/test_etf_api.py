from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from fastapi import HTTPException

from src.app.api import etf


class GetEtfReportTest(IsolatedAsyncioTestCase):
    async def test_get_etf_report_returns_404_when_report_is_missing(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with self.assertRaises(HTTPException) as raised:
            await etf.get_etf_report("COO.US", account_id="acct", db=db)

        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("未找到ETF报告", raised.exception.detail)
