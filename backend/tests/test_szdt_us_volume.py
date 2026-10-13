from datetime import date, datetime
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from src.core.services import szdt_us_volume
from src.core.services.szdt_us_volume import _as_new_york, _historical_cumulative_volumes


NEW_YORK = ZoneInfo("America/New_York")


def test_historical_baseline_uses_same_time_cumulative_regular_session_volume():
    candles = [
        {"timestamp": datetime(2026, 9, 30, 9, 29, tzinfo=NEW_YORK), "volume": 99},
        {"timestamp": datetime(2026, 9, 30, 9, 30, tzinfo=NEW_YORK), "volume": 10},
        {"timestamp": datetime(2026, 9, 30, 10, 30, tzinfo=NEW_YORK), "volume": 20},
        {"timestamp": datetime(2026, 9, 30, 15, 55, tzinfo=NEW_YORK), "volume": 30},
        {"timestamp": datetime(2026, 9, 30, 16, 1, tzinfo=NEW_YORK), "volume": 88},
        {"timestamp": datetime(2026, 10, 1, 9, 30, tzinfo=NEW_YORK), "volume": 77},
    ]

    result = _historical_cumulative_volumes(candles, before_date=date(2026, 10, 1))

    assert result[(date(2026, 9, 30), "10:30")] == 30
    assert result[(date(2026, 9, 30), "15:55")] == 60
    # 基准覆盖每个交易分钟：新 cron 落在 10:31 时无需等待另攒 20 天数据。
    assert result[(date(2026, 9, 30), "09:30")] == 10
    assert result[(date(2026, 9, 30), "09:31")] == 10
    assert result[(date(2026, 9, 30), "10:31")] == 30
    assert result[(date(2026, 9, 30), "15:59")] == 60
    assert (date(2026, 9, 30), "16:00") not in result


def test_naive_longport_minute_timestamp_is_converted_from_shanghai_to_new_york():
    # LongPort 以无时区上海时间返回：2026-10-10 03:55 = 纽约前一日 15:55。
    converted = _as_new_york(datetime(2026, 10, 10, 3, 55))

    assert converted == datetime(2026, 10, 9, 15, 55, tzinfo=NEW_YORK)


def test_us_list_metrics_uses_one_batch_quote_without_minute_kline_requests(monkeypatch):
    class FakeService:
        def get_quote_batch(self, symbols):
            assert symbols == ["TQQQ.US", "SOXL.US"]
            return [
                {"symbol": "TQQQ.US", "volume": 1234, "turnover": 5678.0},
                {"symbol": "SOXL.US", "volume": 4321, "turnover": 8765.0},
            ]

    class EmptyQuery:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return []

    class FakeDB:
        def query(self, *_args):
            return EmptyQuery()

    @contextmanager
    def fake_db_ctx():
        yield FakeDB()

    monkeypatch.setattr(szdt_us_volume, "_longport_account_id", lambda _account_id: "test")
    monkeypatch.setattr(szdt_us_volume.LongPortService, "get_instance", lambda _account_id: FakeService())
    monkeypatch.setattr(szdt_us_volume, "get_db_ctx", fake_db_ctx)

    result = szdt_us_volume.get_us_stock_list_metrics(
        "account", ["US.TQQQ", "US.SOXL"], now=datetime(2026, 10, 1, 11, 0, tzinfo=NEW_YORK),
    )

    assert result["US.TQQQ"] == {"volume": 1234, "turnover": 5678.0, "volume_ratio": None}
    assert result["US.SOXL"] == {"volume": 4321, "turnover": 8765.0, "volume_ratio": None}
