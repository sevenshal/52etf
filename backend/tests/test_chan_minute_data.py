from datetime import datetime, timedelta

import pandas as pd

from src.core.services.chan_minute_data import aggregate_minute_rows, normalize_minute_frame


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
