"""自算 ETF 贪恐直接分量（CNN 官方恐贪 / 守猪逮兔贪恐）测试。

直接分量外部已是 0-100 分数：不做滚动 z-score 归一化，缺失时不阻塞历史，
聚合阶段自动从等权平均中剔除。
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.core.services.etf_fear_greed_clone_service import (
    DIRECT_ETF_COMPONENT_KEYS,
    ETF_COMPONENTS,
    ETFFearGreedCloneCalculator,
    Z_SCORED_ETF_COMPONENT_KEYS,
)


def _calculator():
    calc = ETFFearGreedCloneCalculator.__new__(ETFFearGreedCloneCalculator)
    calc._szdt_history_cache = {}
    return calc


def _raw_frame(periods=300):
    index = pd.bdate_range("2024-01-01", periods=periods)
    raw = pd.DataFrame(index=index)
    for key in Z_SCORED_ETF_COMPONENT_KEYS:
        raw[key] = np.linspace(-1.0, 1.0, periods)
    raw["cnn_fear_greed"] = 25.0
    raw["szdt_fear_greed"] = 75.0
    return raw


def test_component_registry_split():
    assert set(ETF_COMPONENTS) == set(Z_SCORED_ETF_COMPONENT_KEYS) | set(
        DIRECT_ETF_COMPONENT_KEYS
    )
    assert DIRECT_ETF_COMPONENT_KEYS == ["cnn_fear_greed", "szdt_fear_greed"]
    assert len(Z_SCORED_ETF_COMPONENT_KEYS) == 7


def test_direct_components_bypass_z_score_normalization():
    calc = _calculator()
    raw = _raw_frame()
    scored = calc._score_etf_raw_signals(raw, score_window=252, min_periods=120)

    assert scored["cnn_fear_greed_score"].iloc[-1] == pytest.approx(25.0)
    assert scored["szdt_fear_greed_score"].iloc[-1] == pytest.approx(75.0)
    # 超界值截取到 0-100
    raw.loc[raw.index[-1], "cnn_fear_greed"] = 130.0
    raw.loc[raw.index[-1], "szdt_fear_greed"] = -20.0
    scored = calc._score_etf_raw_signals(raw, score_window=252, min_periods=120)
    assert scored["cnn_fear_greed_score"].iloc[-1] == pytest.approx(100.0)
    assert scored["szdt_fear_greed_score"].iloc[-1] == pytest.approx(0.0)


def test_szdt_score_linear_mapping(monkeypatch):
    """守猪逮兔 -100..100 线性映射为 0-100。"""
    import src.core.services.szdt as szdt_module

    class FakeSZDTService:
        def get_etf_emotion_history_sync(self, code):
            assert code == "US.SPY"
            return {
                "status": 1,
                "data": [
                    {"date": "2024-01-02", "score": -100},
                    {"date": "2024-01-03", "score": 0},
                    {"date": "2024-01-04", "score": 100},
                ],
            }

    monkeypatch.setattr(szdt_module, "SZDTService", FakeSZDTService)
    calc = _calculator()
    series = calc._fetch_szdt_fear_greed_series(
        "SPY.US", dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    )

    assert series.loc[pd.Timestamp("2024-01-02")] == pytest.approx(0.0)
    assert series.loc[pd.Timestamp("2024-01-03")] == pytest.approx(50.0)
    assert series.loc[pd.Timestamp("2024-01-04")] == pytest.approx(100.0)


def test_szdt_empty_response_raises(monkeypatch):
    import src.core.services.szdt as szdt_module

    class FakeSZDTService:
        def get_etf_emotion_history_sync(self, code):
            return None

    monkeypatch.setattr(szdt_module, "SZDTService", FakeSZDTService)
    calc = _calculator()
    from src.core.services.etf_fear_greed_clone_service import FearGreedCloneError

    with pytest.raises(FearGreedCloneError):
        calc._fetch_szdt_fear_greed_series(
            "SOXX.US", dt.date(2024, 1, 1), dt.date(2024, 1, 31)
        )


def test_safe_direct_component_series_degrades_to_empty():
    calc = _calculator()

    def boom():
        raise RuntimeError("network down")

    series = calc._safe_direct_component_series("CNN fear_greed", boom)
    assert series.empty

    empty = calc._safe_direct_component_series(
        "SZDT emotion", lambda: pd.Series(dtype=float)
    )
    assert empty.empty


def test_component_payload_marks_missing_direct_component():
    calc = _calculator()
    raw = _raw_frame(periods=5)
    raw["szdt_fear_greed"] = np.nan
    scored = calc._score_etf_raw_signals(raw, score_window=3, min_periods=2)
    latest = raw.index[-1]

    payload = calc._component_payload(raw, scored, latest)

    assert payload["cnn_fear_greed"]["score"] == pytest.approx(25.0)
    assert payload["szdt_fear_greed"]["score"] is None
    assert payload["szdt_fear_greed"]["raw_value"] is None
    assert payload["szdt_fear_greed"]["rating"] is None
