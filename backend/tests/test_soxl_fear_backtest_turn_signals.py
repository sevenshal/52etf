import numpy as np
import pandas as pd

from src.app.api.soxl_fear_backtest import (
    SOXLFearStrategyParams,
    _compute_turn_signal_arrays,
    _turn_signal_matches,
)


def _frame(scores, log_z=None, signal_dates=None):
    size = len(scores)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=size, freq="D").date,
        "signal_date": signal_dates or pd.date_range("2024-01-01", periods=size, freq="D").date,
        "fear_greed": scores,
        "log_z": log_z if log_z is not None else np.zeros(size),
    })


def test_turn_signal_modes_are_independent_for_buy_and_sell():
    assert _turn_signal_matches("volume", True, False)
    assert _turn_signal_matches("ma5", False, True)
    assert _turn_signal_matches("any", True, False)
    assert not _turn_signal_matches("all", True, False)
    assert _turn_signal_matches("all", True, True)
    assert not _turn_signal_matches("legacy", True, True)


def test_history_curve_ma5_and_volume_signal_rules_are_reused():
    params = SOXLFearStrategyParams(turn_signal_cooldown_days=0)
    frame = _frame(
        [40, 35, 30, 25, 20, 30, 70, 75, 80, 78, 70, 50],
        [0, 0, 0, 0, 1.5, 0, 0, 0, 0, -0.5, 0, 0],
    )
    signals = _compute_turn_signal_arrays(frame, params)

    assert signals["volume_bottom"][4]
    assert signals["ma5_bottom"][6]
    assert signals["volume_top"][9]
    assert signals["ma5_top"][11]


def test_forward_filled_cross_market_signal_date_only_fires_once():
    params = SOXLFearStrategyParams(volume_bottom_score=30, volume_expand_std=1.25, turn_signal_cooldown_days=0)
    dates = list(pd.date_range("2024-01-01", periods=5, freq="D").date)
    frame = _frame([40, 30, 30, 30, 40], [0, 1.5, 1.5, 1.5, 0], [dates[0], dates[1], dates[1], dates[1], dates[4]])
    signals = _compute_turn_signal_arrays(frame, params)

    assert signals["volume_bottom"].tolist() == [False, True, False, False, False]
