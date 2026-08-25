from datetime import date, datetime, time, timedelta

import pandas as pd

from src.app.api import chan_analysis as api


TEST_DATE = date(2026, 8, 25)


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(TEST_DATE.year, TEST_DATE.month, TEST_DATE.day)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(TEST_DATE.year, TEST_DATE.month, TEST_DATE.day, 10, 0)
        return value.replace(tzinfo=tz) if tz else value


def _bar(timestamp, close=10.0):
    return {
        "timestamp": timestamp,
        "open": close,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": 100,
        "turnover": 1000,
    }


def _complete_day_rows():
    timestamps = [
        *pd.date_range(f"{TEST_DATE} 09:30:00", f"{TEST_DATE} 11:30:00", freq="1min"),
        *pd.date_range(f"{TEST_DATE} 13:01:00", f"{TEST_DATE} 15:00:00", freq="1min"),
    ]
    return [_bar(timestamp.to_pydatetime()) for timestamp in timestamps]


def test_chart_skips_realtime_when_local_today_is_complete(monkeypatch):
    captured = {}
    monkeypatch.setattr(api, "date", FixedDate)
    monkeypatch.setattr(api, "datetime", FixedDateTime)
    monkeypatch.setattr(api, "load_minute_rows", lambda *_args: _complete_day_rows())
    monkeypatch.setattr(
        api,
        "fetch_realtime_minute_rows",
        lambda *_args: (_ for _ in ()).throw(AssertionError("rt_min_daily should not be called")),
    )
    monkeypatch.setattr(
        api,
        "analyze_bars",
        lambda *_args, **kwargs: captured.update(kwargs) or {"signals": []},
    )

    result = api.get_chan_chart(
        "000001.SZ",
        freq="1m",
        start_date=TEST_DATE,
        end_date=TEST_DATE,
        _="admin",
    )

    assert result["historical_today_complete"] is True
    assert result["realtime_merged"] is False
    assert len(result["bars"]) == 241
    assert captured["confirmed"] is True


def test_chart_merges_current_day_realtime_when_local_today_is_incomplete(monkeypatch):
    historical = [
        _bar(datetime.combine(TEST_DATE - timedelta(days=1), time(9, 30)) + timedelta(minutes=index))
        for index in range(25)
    ]
    collision = datetime.combine(TEST_DATE, time(9, 30))
    historical.append(_bar(collision, 10.0))
    realtime = [_bar(collision, 10.2), _bar(collision + timedelta(minutes=1), 10.3)]
    calls = []
    captured = {}
    monkeypatch.setattr(api, "date", FixedDate)
    monkeypatch.setattr(api, "datetime", FixedDateTime)
    monkeypatch.setattr(api, "load_minute_rows", lambda *_args: historical)
    monkeypatch.setattr(
        api,
        "fetch_realtime_minute_rows",
        lambda symbols, freq: calls.append((symbols, freq)) or {"000001.SZ": realtime},
    )
    monkeypatch.setattr(
        api,
        "analyze_bars",
        lambda *_args, **kwargs: captured.update(kwargs) or {"signals": []},
    )

    result = api.get_chan_chart(
        "000001.SZ",
        freq="1m",
        start_date=TEST_DATE - timedelta(days=1),
        end_date=TEST_DATE,
        _="admin",
    )

    assert calls == [(["000001.SZ"], "1MIN")]
    assert result["historical_today_complete"] is False
    assert result["realtime_merged"] is True
    assert result["bars"][-2]["close"] == 10.2
    assert result["bars"][-1]["close"] == 10.3
    assert captured["confirmed"] is False
