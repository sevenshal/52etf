from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from src.core.services import chan_minute_data as minute_module
from src.core.services.chan_minute_data import (
    aggregate_minute_rows,
    fetch_historical_minute_batch,
    fetch_realtime_minute_rows,
    historical_minute_batch_size,
    incremental_minute_sync_groups,
    is_complete_a_share_minute_day,
    merge_minute_rows,
    normalize_minute_frame,
    plan_incremental_minute_groups,
)
from src.core.services.tushare import TushareService


def _minute_rows(count=60):
    start = datetime(2026, 8, 24, 9, 31)
    rows = []
    for index in range(count):
        value = 10 + index * 0.01
        rows.append(
            {
                "timestamp": start + timedelta(minutes=index),
                "open": value,
                "high": value + 0.02,
                "low": value - 0.02,
                "close": value + 0.01,
                "volume": 100,
                "turnover": 1000,
            }
        )
    return rows


def test_normalize_minute_frame_deduplicates_and_rejects_invalid_ohlc():
    frame = pd.DataFrame(
        [
            {"ts_code": "000001.sz", "trade_time": "2026-08-24 09:31:00", "open": 10, "high": 11, "low": 9, "close": 10.5, "vol": 1, "amount": 2},
            {"ts_code": "000001.sz", "trade_time": "2026-08-24 09:31:00", "open": 10, "high": 11, "low": 9, "close": 10.6, "vol": 1, "amount": 2},
            {"ts_code": "000002.SZ", "trade_time": "2026-08-24 09:31:00", "open": 10, "high": 9, "low": 8, "close": 10, "vol": 1, "amount": 2},
        ]
    )
    result = normalize_minute_frame(frame)
    assert len(result) == 1
    assert result.iloc[0]["ts_code"] == "000001.SZ"
    assert result.iloc[0]["close"] == 10.6


def test_aggregate_minute_rows_uses_a_share_buckets():
    result = aggregate_minute_rows("000001.SZ", _minute_rows(), "5m")
    assert result
    assert result[0]["timestamp"].minute % 5 == 0
    assert sum(item["volume"] for item in result) == 6000


def test_complete_a_share_minute_day_requires_all_241_regular_minutes():
    trade_date = date(2026, 8, 25)
    timestamps = [
        *pd.date_range(f"{trade_date} 09:30:00", f"{trade_date} 11:30:00", freq="1min"),
        *pd.date_range(f"{trade_date} 13:01:00", f"{trade_date} 15:00:00", freq="1min"),
    ]
    rows = [{"timestamp": timestamp} for timestamp in timestamps]

    assert len(rows) == 241
    assert is_complete_a_share_minute_day(rows, trade_date) is True
    assert is_complete_a_share_minute_day(rows[:-1], trade_date) is False


def test_merge_minute_rows_realtime_overrides_same_timestamp():
    timestamp = datetime(2026, 8, 25, 10, 0)
    historical = [{"timestamp": timestamp, "close": 10.0}]
    realtime = [
        {"timestamp": timestamp, "close": 10.2},
        {"timestamp": datetime(2026, 8, 25, 10, 1), "close": 10.3},
    ]

    result = merge_minute_rows(historical, realtime)

    assert [row["close"] for row in result] == [10.2, 10.3]


def test_historical_minute_batch_size_respects_row_limit():
    assert historical_minute_batch_size(1) == 32
    assert historical_minute_batch_size(2) == 16
    assert historical_minute_batch_size(3) == 10
    assert historical_minute_batch_size(32) == 1


def test_plan_incremental_minute_groups_adds_one_overlap_day_per_contiguous_gap():
    trading_dates = [
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 24),
    ]
    groups = plan_incremental_minute_groups(
        trading_dates,
        [
            ("000001.SZ", date(2026, 8, 24)),
            ("000002.SZ", date(2026, 8, 21)),
            ("000002.SZ", date(2026, 8, 24)),
            ("000003.SZ", date(2026, 8, 19)),
            ("000003.SZ", date(2026, 8, 21)),
        ],
    )

    planned = {
        (group["start_date"], group["end_date"], group["run_days"]): group["symbols"]
        for group in groups
    }
    assert planned[(date(2026, 8, 21), date(2026, 8, 24), 2)] == ["000001.SZ"]
    assert planned[(date(2026, 8, 20), date(2026, 8, 24), 3)] == ["000002.SZ"]
    assert planned[(date(2026, 8, 18), date(2026, 8, 19), 2)] == ["000003.SZ"]
    assert planned[(date(2026, 8, 20), date(2026, 8, 21), 2)] == ["000003.SZ"]


def test_incremental_minute_sync_groups_only_requires_dates_with_daily_bars(monkeypatch, tmp_path):
    import duckdb

    database_path = str(tmp_path / "minute-gaps.duckdb")
    connection = duckdb.connect(database_path)
    connection.execute("CREATE TABLE a_stock_basic (ts_code VARCHAR, list_status VARCHAR)")
    connection.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE)")
    connection.execute("CREATE TABLE a_stock_minute_bar (ts_code VARCHAR, trade_time TIMESTAMP)")
    connection.executemany(
        "INSERT INTO a_stock_basic VALUES (?, ?)",
        [
            ("000001.SZ", "L"),
            ("000002.SZ", "L"),
            ("000003.SZ", "L"),
            ("000004.SZ", "D"),
            ("000005.SZ", "L"),
        ],
    )
    connection.executemany(
        "INSERT INTO a_stock_market_daily VALUES (?, ?)",
        [
            ("000001.SZ", date(2026, 8, 20)),
            ("000001.SZ", date(2026, 8, 21)),
            ("000001.SZ", date(2026, 8, 24)),
            ("000002.SZ", date(2026, 8, 20)),
            ("000003.SZ", date(2026, 8, 21)),
            ("000003.SZ", date(2026, 8, 24)),
            ("000004.SZ", date(2026, 8, 24)),
            ("000005.SZ", date(2026, 8, 24)),
        ],
    )
    complete_days = [
        ("000001.SZ", date(2026, 8, 20)),
        ("000001.SZ", date(2026, 8, 21)),
        ("000002.SZ", date(2026, 8, 20)),
    ]
    connection.executemany(
        "INSERT INTO a_stock_minute_bar VALUES (?, ?)",
        [
            (symbol, datetime.combine(trade_date, datetime.min.time()) + timedelta(minutes=index))
            for symbol, trade_date in complete_days
            for index in range(200)
        ],
    )
    connection.execute(
        "INSERT INTO a_stock_minute_bar VALUES (?, ?)",
        ["000005.SZ", datetime(2026, 8, 24, 14, 59)],
    )
    connection.close()
    monkeypatch.setattr(minute_module, "ANALYTICS_DB_PATH", database_path)

    groups, start_date, end_date = incremental_minute_sync_groups(32)
    planned = {
        (group["start_date"], group["end_date"], group["run_days"]): group["symbols"]
        for group in groups
    }

    assert (start_date, end_date) == (date(2026, 8, 20), date(2026, 8, 24))
    assert planned[(date(2026, 8, 21), date(2026, 8, 24), 2)] == ["000001.SZ", "000005.SZ"]
    assert planned[(date(2026, 8, 20), date(2026, 8, 24), 3)] == ["000003.SZ"]
    assert all("000002.SZ" not in group["symbols"] for group in groups)
    assert all("000004.SZ" not in group["symbols"] for group in groups)


def test_fetch_historical_minute_batch_retries_missing_symbol():
    class FakeService:
        def __init__(self):
            self.calls = []

        def get_a_stock_historical_minute_batch_frame(self, symbols, *_args, **_kwargs):
            self.calls.append(symbols)
            returned = symbols[:1]
            if symbols == ["600000.SH"]:
                returned = symbols
            return pd.DataFrame(
                [{"ts_code": symbol, "trade_time": "2026-08-24 09:30:00"} for symbol in returned]
            )

    service = FakeService()
    result = fetch_historical_minute_batch(
        service,
        ["000001.SZ", "600000.SH"],
        date(2026, 8, 24),
        date(2026, 8, 24),
    )

    assert service.calls == [["000001.SZ", "600000.SH"], ["600000.SH"]]
    assert sorted(result["frame"]["ts_code"].tolist()) == ["000001.SZ", "600000.SH"]
    assert result["errors"] == []


def test_tushare_historical_minute_batch_joins_codes_with_commas():
    captured = {}

    class FakeLimiter:
        def wait(self):
            return None

    class FakePro:
        def stk_mins(self, **kwargs):
            captured.update(kwargs)
            return pd.DataFrame(
                [
                    {
                        "ts_code": symbol,
                        "trade_time": "2026-08-24 09:30:00",
                        "open": 10,
                        "close": 10.1,
                        "high": 10.2,
                        "low": 9.9,
                        "vol": 100,
                        "amount": 1000,
                    }
                    for symbol in ("000001.SZ", "600000.SH")
                ]
            )

    service = object.__new__(TushareService)
    service.pro = FakePro()
    service._minute_rate_limiter = FakeLimiter()
    result = service.get_a_stock_historical_minute_batch_frame(
        ["sz000001", "600000.SH"],
        datetime(2026, 8, 24),
        datetime(2026, 8, 24, 23, 59, 59),
        raise_on_error=True,
    )

    assert captured["ts_code"] == "000001.SZ,600000.SH"
    assert result.groupby("ts_code").size().to_dict() == {"000001.SZ": 1, "600000.SH": 1}


def test_fetch_realtime_minute_rows_batches_without_writing(monkeypatch):
    calls = []

    class FakeService:
        def get_a_stock_realtime_minute_frame(self, symbol, freq):
            calls.append((symbol, freq))
            return pd.DataFrame(
                [
                    {
                        "time": f"2026-08-25 {time_value}",
                        "open": 10,
                        "close": 10.1,
                        "high": 10.2,
                        "low": 9.9,
                        "vol": 100,
                        "amount": 1000,
                    }
                    for time_value in ("09:30:00", "09:35:00", "09:40:00")
                ]
            )

    monkeypatch.setattr(
        minute_module.TushareService,
        "get_instance",
        classmethod(lambda cls: FakeService()),
    )
    symbols = ["000001.SZ", "600000.SH", "000002.SZ"]

    result = fetch_realtime_minute_rows(symbols, "5MIN")

    assert sorted(symbol for symbol, _ in calls) == sorted(symbols)
    assert all(freq == "5MIN" for _, freq in calls)
    assert len(result) == 3
    assert len(result["000001.SZ"]) == 3
    assert result["000001.SZ"][0]["timestamp"] == datetime(2026, 8, 25, 9, 30)


def test_tushare_realtime_minute_batch_joins_codes_and_uses_target_freq():
    captured = {}

    class FakeLimiter:
        def wait(self):
            return None

    class FakePro:
        def rt_min(self, **kwargs):
            captured.update(kwargs)
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "time": "2026-08-25 14:55:00",
                        "open": 10,
                        "close": 10.1,
                        "high": 10.2,
                        "low": 9.9,
                        "vol": 100,
                        "amount": 1000,
                    }
                ]
            )

    service = object.__new__(TushareService)
    service.pro = FakePro()
    service._minute_rate_limiter = FakeLimiter()
    result = service.get_a_stock_realtime_minute_batch_frame(
        ["sz000001", "600000.SH"],
        "5min",
    )

    assert captured["ts_code"] == "000001.SZ,600000.SH"
    assert captured["freq"] == "5MIN"
    assert result.iloc[0]["trade_time"] == pd.Timestamp("2026-08-25 14:55:00")


def test_tushare_realtime_minute_batch_rejects_more_than_300_symbols():
    service = object.__new__(TushareService)
    symbols = [f"{index:06d}.SZ" for index in range(1, 302)]

    with pytest.raises(ValueError, match="最多300只"):
        service.get_a_stock_realtime_minute_batch_frame(symbols, "1MIN")


def test_tushare_realtime_minute_daily_returns_complete_day_and_uses_limiter():
    captured = {}

    class FakeLimiter:
        def __init__(self):
            self.calls = 0

        def wait(self):
            self.calls += 1

    class FakePro:
        def rt_min_daily(self, **kwargs):
            captured.update(kwargs)
            return pd.DataFrame(
                [
                    {
                        "time": time_value,
                        "open": 10,
                        "close": 10.1,
                        "high": 10.2,
                        "low": 9.9,
                        "vol": 100,
                        "amount": 1000,
                    }
                    for time_value in ("2026-08-25 09:30:00", "2026-08-25 09:31:00")
                ]
            )

    limiter = FakeLimiter()
    service = object.__new__(TushareService)
    service.pro = FakePro()
    service._minute_rate_limiter = limiter
    result = service.get_a_stock_realtime_minute_frame("600000.SH", "1min")

    assert limiter.calls == 1
    assert captured["ts_code"] == "600000.SH"
    assert captured["freq"] == "1MIN"
    assert result["time"].tolist() == [
        pd.Timestamp("2026-08-25 09:30:00"),
        pd.Timestamp("2026-08-25 09:31:00"),
    ]
