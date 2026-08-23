from datetime import date, timedelta

import pandas as pd
import pytest

from src.core.services.a_stock_fear_etf_backtest_engine import (
    build_signal_rows,
    fear_reversal_flags,
    prepare_fear_features,
    run_backtest,
)


def test_bottom_signal_uses_any_extreme_fear_day_in_recent_window():
    dates = [date(2024, 1, 2) + timedelta(days=index) for index in range(7)]
    fear = pd.DataFrame({
        "index_symbol": ["IDX"] * 7,
        "date": dates,
        # The current score is above 25, but the latest five rows contain 20.
        # Since 31 replaces 30, MA5 turns upward on the final row.
        "score": [35, 30, 20, 21, 22, 23, 31],
    })
    featured_fear = prepare_fear_features(fear, 5)
    bars = pd.DataFrame({
        "trade_date": dates,
        "etf_symbol": ["ETF"] * 7,
        "volume_ratio": [1.0] * 7,
    })
    signals = build_signal_rows(
        bars, featured_fear, {"IDX": "ETF"},
        extreme_fear_threshold=0, volume_ratio_threshold=99,
        bottom_fear_threshold=25, extreme_buy_fraction=1,
        bottom_buy_fraction=0.5, start_date=str(dates[0]), end_date=str(dates[-1]),
    )
    assert dates[6] in signals
    assert signals[dates[6]][0]["fear_score"] == 31
    assert signals[dates[6]][0]["recent_fear_min"] == 20
    assert signals[dates[6]][0]["reason"] == "fear_bottom_reversal"
    assert signals[dates[6]][0]["target_fraction"] == 0.5


def test_top_signal_uses_any_extreme_greed_day_in_recent_window():
    dates = [date(2024, 2, 1) + timedelta(days=index) for index in range(7)]
    fear = pd.DataFrame({
        "index_symbol": ["IDX"] * 7,
        "date": dates,
        # The current score is below 75, but the latest five rows contain 80.
        # Since 69 replaces 70, MA5 turns downward on the final row.
        "score": [65, 70, 80, 79, 78, 77, 69],
    })
    row = prepare_fear_features(fear, 5).iloc[-1]
    is_bottom, is_top = fear_reversal_flags(
        row["fear_ma"], row["prior_fear_ma"],
        row["prior_prior_fear_ma"],
        row["recent_fear_min"], row["recent_fear_max"],
    )
    assert is_bottom is False
    assert is_top is True
    assert row["score"] == 69
    assert row["recent_fear_max"] == 80


def test_same_day_buy_signals_prioritize_highest_volume_ratio():
    signal_date = date(2024, 3, 1)
    fear = pd.DataFrame({
        "index_symbol": ["IDX_LOW_VOLUME", "IDX_HIGH_VOLUME"],
        "date": [signal_date, signal_date],
        "score": [18, 22],
        "fear_ma": [20, 23],
        "prior_fear_ma": [21, 24],
        "prior_prior_fear_ma": [22, 25],
        "recent_fear_min": [18, 22],
        "recent_fear_max": [23, 25],
    })
    bars = pd.DataFrame({
        "trade_date": [signal_date, signal_date],
        "etf_symbol": ["ETF_LOW_VOLUME", "ETF_HIGH_VOLUME"],
        "volume_ratio": [1.4, 1.8],
    })

    signals = build_signal_rows(
        bars,
        fear,
        {
            "IDX_LOW_VOLUME": "ETF_LOW_VOLUME",
            "IDX_HIGH_VOLUME": "ETF_HIGH_VOLUME",
        },
        extreme_fear_threshold=25,
        volume_ratio_threshold=1.3,
        bottom_fear_threshold=25,
        extreme_buy_fraction=1,
        bottom_buy_fraction=0.5,
        start_date=str(signal_date),
        end_date=str(signal_date),
    )

    assert [item["etf_symbol"] for item in signals[signal_date]] == [
        "ETF_HIGH_VOLUME",
        "ETF_LOW_VOLUME",
    ]


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
        "fear_ma": 22, "volume_ratio": 1.3,
        "target_fraction": 0.5, "reason": "fear_bottom_reversal",
    }]}
    curve, trades = run_backtest(
        bars, fear, signals, start_date=str(dates[0]), end_date=str(dates[-1]),
        initial_capital=120_000, greed_threshold=75, greed_sell_fraction=0.5,
        stop_loss=0.1, stop_cooldown_days=20,
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


def test_stop_loss_exits_next_open_and_blocks_new_buys_for_twenty_trading_days():
    dates = [value.date() for value in pd.bdate_range("2024-04-01", periods=25)]
    bars = pd.DataFrame({
        "trade_date": dates,
        "etf_symbol": ["ETF"] * len(dates),
        "open": [100] * len(dates),
        "high": [101] * len(dates),
        "low": [98] * len(dates),
        "close": [100, 100, 89] + [100] * (len(dates) - 3),
        "realized_volatility": [0.1] * len(dates),
        "volatility_threshold": [0.2] * len(dates),
    })
    fear = pd.DataFrame({
        "date": dates,
        "index_symbol": ["IDX"] * len(dates),
        "score": [50] * len(dates),
    })
    buy_signal = {
        "index_symbol": "IDX", "etf_symbol": "ETF", "fear_score": 20,
        "fear_ma": 22, "volume_ratio": 1.5,
        "target_fraction": 1.0, "reason": "extreme_fear_volume",
    }
    signals = {
        dates[0]: [buy_signal],
        dates[4]: [buy_signal],
        dates[23]: [buy_signal],
    }

    curve, trades = run_backtest(
        bars, fear, signals, start_date=str(dates[0]), end_date=str(dates[-1]),
        initial_capital=100_000, greed_threshold=75, greed_sell_fraction=0.5,
        stop_loss=0.1, stop_cooldown_days=20,
        trailing_drawdown=0.07, commission_pct=0, min_commission=0,
        slippage_pct=0, stamp_duty_pct=0, lot_size=100, max_positions=1,
    )

    assert trades[["date", "action", "reason"]].to_dict("records") == [
        {"date": str(dates[1]), "action": "buy", "reason": "extreme_fear_volume"},
        {"date": str(dates[3]), "action": "sell", "reason": "stop_loss"},
        {"date": str(dates[24]), "action": "buy", "reason": "extreme_fear_volume"},
    ]
    stop_trade = trades.iloc[1]
    assert stop_trade["drawdown_from_entry_pct"] == pytest.approx(-11)
    assert curve.loc[curve["date"] == str(dates[3]), "buy_cooldown_days_remaining"].item() == 20
