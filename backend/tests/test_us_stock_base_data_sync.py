import os
import subprocess
import sys
import textwrap
from datetime import date
from io import BytesIO
from unittest import TestCase

import pandas as pd
from unittest.mock import patch

from src.robot.etf.spdr import SPDRDataFetcher


class USStockBaseDataSyncTest(TestCase):
    def test_module_import_smoke(self):
        code = textwrap.dedent(
            """
            from src.robot.us_stock_base_data_sync import sync_us_stock_base_data

            assert sync_us_stock_base_data is not None
            """
        )
        with tempfile_analytics_db() as path:
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path
            subprocess.run([sys.executable, "-c", code], env=env, check=True)


class SPDRDataFetcherTest(TestCase):
    def test_get_holdings_reads_excel_payload_with_openpyxl_dependency(self):
        rows = [
            ["HEADER", "HEADER2", "HEADER3", "HEADER4"],
            ["Holdings:", "As of 16-Jun-2026", None, None],
            [None, None, None, None],
            ["Ticker", "Name", "Shares Held", "Weight"],
            ["AAPL.US", "Apple", 123456, 12.5],
            ["USD", "US DOLLAR", 1000, 87.5],
        ]
        workbook = BytesIO()
        pd.DataFrame(rows).to_excel(workbook, index=False, header=False)
        workbook.seek(0)
        body = workbook.getvalue()

        class FakeResponse:
            status_code = 200

            def __init__(self, content: bytes):
                self.content = content

            def raise_for_status(self):
                return None

        with patch("src.robot.etf.spdr.requests.get", return_value=FakeResponse(body)):
            result = SPDRDataFetcher().get_holdings("SPY.US")

        assert result.update_date == date(2026, 6, 16)
        assert len(result.holdings) == 2
        assert result.holdings[0].symbol == "AAPL.US"
        assert result.holdings[0].weight == 0.125
        assert result.holdings[1].asset_class == "Cash"
        assert result.total_weight == 1.0


def tempfile_analytics_db():
    import tempfile

    class _TempDBPath:
        def __init__(self):
            self.fd, self.path = tempfile.mkstemp(suffix=".duckdb")

        def __enter__(self):
            import os

            os.close(self.fd)
            os.unlink(self.path)
            return self.path

        def __exit__(self, exc_type, exc_val, exc_tb):
            try:
                import os

                if os.path.exists(self.path):
                    os.unlink(self.path)
            except OSError:
                pass

    return _TempDBPath()
