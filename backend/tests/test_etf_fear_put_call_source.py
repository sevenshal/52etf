"""自算 ETF 贪恐 put/call 分量取值口径测试。

put/call 分量必须用 Cboe 全市场总 put/call 比（与 CNN 同源）；只有当 Cboe
取不到或覆盖过薄时，才回退到标的自身的 Barchart put/call 历史。
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.core.services.etf_fear_greed_clone_service import (
    CBOE_TOTAL_PUT_CALL_RATIO,
    ETFFearGreedCloneCalculator,
)


def _calculator():
    return ETFFearGreedCloneCalculator.__new__(ETFFearGreedCloneCalculator)


def _cboe_series(start, end, skip_every=None):
    index = pd.bdate_range(start, end)
    values = []
    days = []
    for position, day in enumerate(index):
        if skip_every and position % skip_every == 0:
            continue
        days.append(day)
        values.append(0.9)
    return pd.Series(values, index=pd.DatetimeIndex(days))


START = dt.date(2024, 1, 1)
END = dt.date(2024, 12, 31)


def test_prefers_cboe_total_ratio(monkeypatch):
    calc = _calculator()
    calls = {}

    def fake_cboe(start_date, end_date, ratio_name):
        calls["ratio_name"] = ratio_name
        return _cboe_series(start_date, end_date)

    def fake_db(*args, **kwargs):
        raise AssertionError("不应回退到标的自身 put/call")

    monkeypatch.setattr(calc, "_fetch_cboe_ratio", fake_cboe)
    monkeypatch.setattr(calc, "_fetch_db_put_call_ratio", fake_db)

    series = calc._fetch_market_put_call_ratio("SPY.US", START, END)

    assert calls["ratio_name"] == CBOE_TOTAL_PUT_CALL_RATIO
    assert len(series) > 200


def test_thin_cboe_coverage_falls_back_to_symbol_ratio(monkeypatch):
    calc = _calculator()
    symbol_series = pd.Series(
        [1.1, 1.2], index=pd.to_datetime(["2024-12-30", "2024-12-31"])
    )
    fallback_calls = []

    def fake_cboe(start_date, end_date, ratio_name):
        # 只覆盖少数几天，视为不可用
        return _cboe_series(start_date, end_date, skip_every=1)

    def fake_db(etf_symbol, start_date, end_date):
        fallback_calls.append(etf_symbol)
        return symbol_series

    monkeypatch.setattr(calc, "_fetch_cboe_ratio", fake_cboe)
    monkeypatch.setattr(calc, "_fetch_db_put_call_ratio", fake_db)

    series = calc._fetch_market_put_call_ratio("SOXX.US", START, END)

    assert fallback_calls == ["SOXX.US"]
    assert series.equals(symbol_series)


def test_cboe_failure_falls_back_to_symbol_ratio(monkeypatch):
    calc = _calculator()
    symbol_series = pd.Series(
        [1.5], index=pd.to_datetime(["2024-12-31"])
    )

    def fake_cboe(start_date, end_date, ratio_name):
        raise RuntimeError("cboe unavailable")

    monkeypatch.setattr(calc, "_fetch_cboe_ratio", fake_cboe)
    monkeypatch.setattr(calc, "_fetch_db_put_call_ratio", lambda *a, **k: symbol_series)

    series = calc._fetch_market_put_call_ratio("QQQ.US", START, END)

    assert series.equals(symbol_series)


def test_component_raw_is_negative_five_day_average_of_cboe_ratio(monkeypatch):
    """接线验证：put/call 分量原始值 = 新口径（Cboe 全市场）的 -5 日均值。"""
    from types import SimpleNamespace

    calc = _calculator()
    index = pd.bdate_range("2024-01-01", periods=200)
    prices = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1e6,
            "turnover": 1e6,
        },
        index=index,
    )
    ratios = pd.Series(np.linspace(0.5, 1.5, len(index)), index=index)

    monkeypatch.setattr(calc, "_fetch_price_history", lambda symbol, s, e: prices)
    monkeypatch.setattr(
        calc,
        "_fetch_fred",
        lambda ids: pd.DataFrame({series_id: 1.0 for series_id in ids}, index=index),
    )
    monkeypatch.setattr(
        calc, "_fetch_market_put_call_ratio", lambda symbol, s, e: ratios
    )
    monkeypatch.setattr(calc, "_unique_holdings", lambda by_date: [SimpleNamespace(symbol="AAA")])
    monkeypatch.setattr(
        calc, "_weighted_range_position", lambda *a, **k: pd.Series(0.5, index=index)
    )
    monkeypatch.setattr(
        calc, "_weighted_advancing_volume_ratio", lambda *a, **k: pd.Series(0.5, index=index)
    )

    raw = calc._build_raw_signals("SPY.US", {}, index[0].date(), index[-1].date())

    assert raw["put_call_options"].iloc[-1] == pytest.approx(-ratios.tail(5).mean())
