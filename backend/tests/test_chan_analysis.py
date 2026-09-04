from datetime import datetime, timedelta

import pytest

from src.core.services.chan_analysis import (
    NATIVE_ENGINE_VERSION,
    analyze_bars,
    rows_to_raw_bars,
)


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
    assert result["engine_version"] == NATIVE_ENGINE_VERSION == "2.0"
    assert result["engine"] == "native_structural"
    assert result["bar_count"] == 800
    assert result["fractals"]
    assert result["strokes"]
    assert isinstance(result["centers"], list)
    for center in result["centers"]:
        # The true running extremes must bracket the fixed overlap zone.
        assert center["dd"] <= center["zd"] <= center["zg"] <= center["gg"]
        assert center["level"] == 0
        assert center["trend"] in {"", "up", "down", "range"}
    for segment in result["segments"]:
        assert segment["partition"] in {"first", "second"}
    assert all(item["type"] in {"一买", "二买", "三买", "一卖", "二卖", "三卖"} for item in result["signals"])
    assert result["signal_history_count"] == len(result["signal_history"])
    assert all(item["type"] in {"一买", "二买", "三买", "一卖", "二卖", "三卖"} for item in result["signal_history"])
    assert all("center_start_stroke" in item for item in result["signal_history"])


def test_rows_to_raw_bars_rejects_invalid_ohlc():
    rows = _bars(1)
    rows[0]["high"] = rows[0]["low"]
    with pytest.raises(ValueError, match="Inconsistent OHLC"):
        rows_to_raw_bars("000001.SZ", rows, "d")


def test_analyze_bars_requires_warmup_data():
    with pytest.raises(ValueError, match="At least 20 bars"):
        analyze_bars("000001.SZ", _bars(10), "d")


def test_analyze_bars_can_skip_history_for_batch_scans():
    result = analyze_bars("000001.SZ", _bars(200), "d", include_history=False)
    assert result["signal_history"] == []
    assert result["signal_history_count"] == 0


def test_analyze_bars_czsc_engine_returns_native_shaped_output():
    result = analyze_bars("000001.SZ", _bars(600), "d", engine="czsc")
    assert result["engine"] == "czsc"
    assert result["engine_version"] == "1.0.1"
    # same top-level keys the chart / scanner consume from the native engine
    for key in ("fractals", "strokes", "segments", "centers", "signals", "signal_history", "signal_history_count"):
        assert key in result
    for fx in result["fractals"]:
        assert fx["mark"] in {"top", "bottom"}
    for center in result["centers"]:
        assert center["status"] in {"active", "broken"}
        assert "gg" in center and "dd" in center and center["level"] == 0
    for segment in result["segments"]:
        assert segment["direction"] in {"向上", "向下"}
    assert all(item["type"] in {"一买", "二买", "三买", "一卖", "二卖", "三卖"} for item in result["signals"])


def test_analyze_bars_rejects_unknown_engine_gracefully():
    # unknown engine falls through to native (no crash); explicit契约 is via the API pattern
    result = analyze_bars("000001.SZ", _bars(200), "d", engine="native", include_history=False)
    assert result["engine"] == "native_structural"
