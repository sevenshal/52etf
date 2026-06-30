from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from src.core.services.longport import LongPortKlineQuotaExceeded, LongPortService
from src.robot.us_stock_base_data_sync import USStockBaseDataSyncService


class LongPortKlineQuotaTest(TestCase):
    def _service(self):
        service = object.__new__(LongPortService)
        service.invalid_kline_symbols = set()
        service.ctx = SimpleNamespace(
            candlesticks=object(),
            history_candlesticks_by_date=object(),
        )
        return service

    def test_date_range_quota_error_raises_without_retrying_smaller_windows(self):
        service = self._service()
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("OpenApiException: code=301607 history kline symbol count out of limit")

        with patch("src.core.services.longport._limited_candlestick_request", side_effect=fail_once):
            with self.assertRaises(LongPortKlineQuotaExceeded):
                service.get_candlesticks_by_date(
                    "AMTM.US",
                    date(2026, 5, 1),
                    date(2026, 5, 27),
                    "d",
                )

        self.assertEqual(1, len(calls))

    def test_count_based_quota_error_raises_without_count_downgrade(self):
        service = self._service()
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("OpenApiException: code=301607 history kline symbol count out of limit")

        with patch("src.core.services.longport._limited_candlestick_request", side_effect=fail_once):
            with self.assertRaises(LongPortKlineQuotaExceeded):
                service.get_candlesticks("AMTM.US", 1000, "d")

        self.assertEqual(1, len(calls))
        self.assertEqual(1000, calls[0]["count"])

    def test_us_daily_sync_records_quota_error_not_empty_symbol(self):
        class FakeLongPort:
            def get_candlesticks_by_date(self, symbol, start, end, period):
                raise LongPortKlineQuotaExceeded(f"quota exceeded for {symbol}")

        syncer = USStockBaseDataSyncService(longport_service=FakeLongPort())
        result = syncer._sync_daily_klines(
            ["AMTM.US"],
            date(2026, 5, 1),
            date(2026, 5, 27),
            datetime(2026, 5, 27, 12, 0, 0),
        )

        self.assertEqual(0, result["daily_empty_symbols"])
        self.assertEqual(1, len(result["daily_errors"]))
        self.assertEqual("AMTM.US", result["daily_errors"][0]["symbol"])
        self.assertEqual("longport_kline_quota_exceeded", result["daily_errors"][0]["error_type"])


class LongPortRealtimeQuoteTest(TestCase):
    def _service(self, quotes):
        service = object.__new__(LongPortService)
        service.ctx = SimpleNamespace(quote=lambda _symbols: quotes)
        return service

    def test_quote_batch_keeps_zero_prev_close_quote_without_dividing(self):
        quote = SimpleNamespace(
            symbol="AIN.US",
            last_done="12.5",
            prev_close="0",
            high="13",
            low="12",
            open="12.1",
            volume=1000,
            turnover="12500",
            timestamp=datetime(2026, 6, 29, 5, 30),
        )

        result = self._service([quote]).get_quote_batch(["AIN.US"])

        self.assertEqual(1, len(result))
        self.assertEqual("AIN.US", result[0]["symbol"])
        self.assertEqual(12.5, result[0]["price"])
        self.assertEqual(12.5, result[0]["change"])
        self.assertIsNone(result[0]["percent_change"])
        self.assertEqual(0.0, result[0]["prev_close"])

    def test_quote_batch_skips_bad_quote_and_keeps_other_symbols(self):
        bad_quote = SimpleNamespace(
            symbol="BAD.US",
            last_done="",
            prev_close="10",
            high="",
            low="",
            open="",
            volume=0,
            turnover="",
            timestamp=None,
        )
        good_quote = SimpleNamespace(
            symbol="SPSC.US",
            last_done="20",
            prev_close="10",
            high="21",
            low="19",
            open="19.5",
            volume=2000,
            turnover="40000",
            timestamp=datetime(2026, 6, 29, 5, 30),
        )

        result = self._service([bad_quote, good_quote]).get_quote_batch(["BAD.US", "SPSC.US"])

        self.assertEqual(["SPSC.US"], [row["symbol"] for row in result])
        self.assertEqual(100.0, result[0]["percent_change"])
