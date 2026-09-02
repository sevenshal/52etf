"""Tests for the CZSC comparison oracle (research only)."""
from datetime import datetime, timedelta

from czsc_oracle import _extract_activation_events, analyze_bars_czsc_legacy, rows_to_raw_bars


def _bars(count=800):
    rows = []
    price = 10.0
    for index in range(count):
        close = price + (0.18 if index % 16 < 8 else -0.18)
        rows.append(
            {
                "timestamp": datetime(2020, 1, 1) + timedelta(days=index),
                "open": price,
                "close": close,
                "high": max(price, close) + 0.08,
                "low": min(price, close) - 0.08,
                "volume": 1000 + index,
                "turnover": 10000 + index,
            }
        )
        price = close
    return rows


def test_analyze_bars_czsc_legacy_returns_plain_dicts():
    result = analyze_bars_czsc_legacy("000001.SZ", _bars(), "d")
    assert result["czsc_version"] == "1.0.1"
    assert isinstance(result["strokes"], list)
    assert isinstance(result["centers"], list)
    assert result["signal_history_count"] == len(result["signal_history"])


def test_signal_replay_marks_only_each_activation_transition():
    bars = rows_to_raw_bars("000001.SZ", _bars(6), "d")
    descriptor = {
        "name": "cxt_first_buy_V221126",
        "key": "日线_D1B_BUY1",
        "expected": {"一买"},
    }
    rows = [
        {"id": "1", descriptor["key"]: "其他_任意_任意_0"},
        {"id": "2", descriptor["key"]: "一买_5笔_任意_0"},
        {"id": "3", descriptor["key"]: "一买_5笔_任意_0"},
        {"id": "4", descriptor["key"]: "其他_任意_任意_0"},
        {"id": "5", descriptor["key"]: "一买_7笔_任意_0"},
    ]

    events = _extract_activation_events(
        rows,
        [descriptor],
        {int(bar.id): bar for bar in bars},
        latest_bar_id=5,
        latest_confirmed=False,
    )

    assert [item["detail"] for item in events] == ["5笔", "7笔"]
    assert [item["bar_time"] for item in events] == [bars[2].dt.isoformat(), bars[5].dt.isoformat()]
    assert [item["confirmed"] for item in events] == [True, False]
