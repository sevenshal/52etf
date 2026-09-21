import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.intraday_minutes import build_minute_series, group_by_day


def _row(timestamp, close, volume=1000.0, amount=10000.0):
    return {"timestamp": pd.Timestamp(timestamp), "close": close, "volume": volume, "turnover": amount}


def test_group_by_day_sorts_and_drops_bad_rows():
    rows = [
        _row("2026-09-18 09:31", 10.2),
        _row("2026-09-18 09:30", 10.0),
        _row("2026-09-17 15:00", 9.8),
        {"timestamp": "not-a-time", "close": 1.0},
        _row("2026-09-18 09:32", None),
    ]
    grouped = group_by_day(rows)
    assert [item["time"] for item in grouped["2026-09-18"]] == ["09:30", "09:31"]
    assert grouped["2026-09-17"][0]["close"] == 9.8


def test_build_minute_series_uses_previous_day_close_as_pre_close():
    rows = [
        _row("2026-09-17 14:59", 9.9),
        _row("2026-09-17 15:00", 10.0),      # 前一日收盘 → 次日前收
        _row("2026-09-18 09:30", 10.5, volume=2000.0, amount=21000.0),
        _row("2026-09-18 09:31", 10.2, volume=1000.0, amount=10200.0),
    ]

    series = build_minute_series(rows, days=1)

    assert [day["date"] for day in series["days"]] == ["2026-09-18"]
    day = series["days"][0]
    assert day["pre_close"] == 10.0
    assert day["pct"] == 2.0                      # 收在 10.2，相对前收 +2%
    assert day["volume"] == 3000.0                # 当日累计量
    assert day["start_index"] == 0 and day["count"] == 2
    assert [point["pct"] for point in series["points"]] == [5.0, 2.0]
    assert series["today"] == "2026-09-18"


def test_build_minute_series_keeps_last_days_and_indexes_each_day():
    rows = []
    for offset, day in enumerate(("2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18")):
        rows.append(_row(f"{day} 09:30", 10.0 + offset))
        rows.append(_row(f"{day} 15:00", 10.5 + offset))

    series = build_minute_series(rows, days=2)

    assert [day["date"] for day in series["days"]] == ["2026-09-17", "2026-09-18"]
    # 第二天的起始下标接在第一天之后，前端用它画分日分隔线
    assert series["days"][0]["start_index"] == 0
    assert series["days"][1]["start_index"] == series["days"][0]["count"]
    assert len(series["points"]) == 4
    # 最早保留日的前收来自再往前一天的收盘（多取一天只用于基准）
    assert series["days"][0]["pre_close"] == 11.5


def test_build_minute_series_handles_empty_rows():
    assert build_minute_series([], days=5) == {"days": [], "points": [], "today": None}
