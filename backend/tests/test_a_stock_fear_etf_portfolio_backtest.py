from datetime import date, timedelta

import pandas as pd

from src.core.services.a_stock_fear_etf_backtest_engine import (
    build_signal_rows,
    fear_reversal_flags,
    prepare_fear_features,
    run_backtest,
)


def test_bottom_signal_uses_any_extreme_fear_day_in_recent_window():
    dates = [date(2024, 1, 2) + timedelta(days=index) for index in range(6)]
    fear = pd.DataFrame({
        "index_symbol": ["IDX"] * 6,
        "date": dates,
        # The current score is above 25, but the latest five rows contain 20.
        # Since 31 replaces 30, MA5 turns upward on the final row.
        "score": [30, 20, 21, 22, 23, 31],
    })
    featured_fear = prepare_fear_features(fear, 5)
    bars = pd.DataFrame({
        "trade_date": dates,
        "etf_symbol": ["ETF"] * 6,
        "volume_ratio": [1.0] * 6,
        "risk_adjusted_momentum": [2.0] * 6,
    })
    signals = build_signal_rows(
        bars, featured_fear, {"IDX": "ETF"},
        extreme_fear_threshold=0, volume_ratio_threshold=99,
        bottom_fear_threshold=25, extreme_buy_fraction=1,
        bottom_buy_fraction=0.5, start_date=str(dates[0]), end_date=str(dates[-1]),
    )
    assert dates[5] in signals
    assert signals[dates[5]][0]["fear_score"] == 31
    assert signals[dates[5]][0]["recent_fear_min"] == 20
    assert signals[dates[5]][0]["reason"] == "fear_bottom_reversal"
    assert signals[dates[5]][0]["target_fraction"] == 0.5


def test_top_signal_uses_any_extreme_greed_day_in_recent_window():
    dates = [date(2024, 2, 1) + timedelta(days=index) for index in range(6)]
    fear = pd.DataFrame({
        "index_symbol": ["IDX"] * 6,
        "date": dates,
        # The current score is below 75, but the latest five rows contain 80.
        # Since 69 replaces 70, MA5 turns downward on the final row.
        "score": [70, 80, 79, 78, 77, 69],
    })
    row = prepare_fear_features(fear, 5).iloc[-1]
    is_bottom, is_top = fear_reversal_flags(
        row["fear_ma"], row["prior_fear_ma"],
        row["recent_fear_min"], row["recent_fear_max"],
    )
    assert is_bottom is False
    assert is_top is True
    assert row["score"] == 69
    assert row["recent_fear_max"] == 80


def test_orders_execute_next_open_and_trailing_stop_waits_for_greed_reduction():
    dates = [date(2024, 1, day) for day in range(2, 7)]
    bars = pd.DataFrame({
        "trade_date": dates,
        "etf_symbol": ["ETF"] * 5,
        "open": [99, 100, 106, 101, 98],
        "high": [100, 102, 110, 109, 100],
        "low": [98, 99, 104, 98, 96],
        "close": [99, 101, 108, 100, 97],
        "realized_volatility": [0.1, 0.1, 0.1, 0.4, 0.3],
        "volatility_threshold": [0.2] * 5,
    })
    fear = pd.DataFrame({
        "date": dates,
        "index_symbol": ["IDX"] * 5,
        "score": [20, 20, 80, 60, 50],
    })
    signals = {dates[0]: [{
        "index_symbol": "IDX", "etf_symbol": "ETF", "fear_score": 20,
        "fear_ma": 22, "volume_ratio": 1.3, "risk_adjusted_momentum": 3,
        "target_fraction": 0.5, "reason": "fear_bottom_reversal",
    }]}
    curve, trades = run_backtest(
        bars, fear, signals, start_date=str(dates[0]), end_date=str(dates[-1]),
        initial_capital=120_000, greed_threshold=75, greed_sell_fraction=0.5,
        trailing_drawdown=0.07, commission_pct=0, min_commission=0,
        slippage_pct=0, stamp_duty_pct=0, lot_size=100, max_positions=2,
    )
    assert trades[["signal_date", "date", "action", "reason"]].to_dict("records") == [
        {"signal_date": str(dates[0]), "date": str(dates[1]), "action": "buy", "reason": "fear_bottom_reversal"},
        {"signal_date": str(dates[2]), "date": str(dates[3]), "action": "sell", "reason": "extreme_greed_partial"},
        {"signal_date": str(dates[3]), "date": str(dates[4]), "action": "sell", "reason": "volatility_trailing_stop"},
    ]
    assert trades.iloc[0]["quantity"] == 600
    assert trades.iloc[1]["quantity"] == 300
    assert curve.iloc[-1]["holding_count"] == 0
