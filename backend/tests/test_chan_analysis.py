from datetime import datetime, timedelta

import pytest

from src.core.services.chan_analysis import CZSC_VERSION, analyze_bars, rows_to_raw_bars


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


def test_analyze_bars_returns_plain_chart_structures():
    result = analyze_bars("000001.SZ", _bars(), "d")
    assert result["czsc_version"] == CZSC_VERSION == "1.0.1"
    assert result["bar_count"] == 800
    assert result["fractals"]
    assert result["strokes"]
    assert isinstance(result["centers"], list)
    assert all(item["type"] in {"一买", "二买", "三买", "一卖", "二卖", "三卖"} for item in result["signals"])


def test_rows_to_raw_bars_rejects_invalid_ohlc():
    rows = _bars(1)
    rows[0]["high"] = rows[0]["low"]
    with pytest.raises(ValueError, match="Inconsistent OHLC"):
        rows_to_raw_bars("000001.SZ", rows, "d")


def test_analyze_bars_requires_warmup_data():
    with pytest.raises(ValueError, match="At least 20 bars"):
        analyze_bars("000001.SZ", _bars(10), "d")
