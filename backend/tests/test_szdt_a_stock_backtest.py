from datetime import date

import pandas as pd

from src.core.services import szdt_a_stock_backtest as module


def test_backtest_waits_for_ema5_breakdown_before_greed_sell(monkeypatch):
    stock = {
        "code": "SH.510300", "name": "沪深300", "enabled": True,
        "when_buy": -60, "when_sell": 60, "max_position": 100,
        "buy_amount": 20_000, "sell_amount": 20_000, "buy_factor": 1, "sell_factor": 1,
        "buy_volume_ratio": 1.3,
    }
    history = pd.DataFrame([
        {"date": date(2026, 1, 2), "score": -70},
        {"date": date(2026, 1, 5), "score": 70},
        {"date": date(2026, 1, 6), "score": 70},
        {"date": date(2026, 1, 7), "score": 0},
    ])
    prices = pd.DataFrame([
        {"code": "SH.510300", "ts_code": "510300.SH", "trade_date": date(2026, 1, 2), "open": 10, "close": 10, "vol": 100, "volume_ratio": 1.5, "ema5": 10},
        {"code": "SH.510300", "ts_code": "510300.SH", "trade_date": date(2026, 1, 5), "open": 10, "close": 11, "vol": 100, "volume_ratio": 1.5, "ema5": 10},
        {"code": "SH.510300", "ts_code": "510300.SH", "trade_date": date(2026, 1, 6), "open": 11, "close": 9, "vol": 100, "volume_ratio": 1.5, "ema5": 10},
        {"code": "SH.510300", "ts_code": "510300.SH", "trade_date": date(2026, 1, 7), "open": 9, "close": 9, "vol": 100, "volume_ratio": 1.5, "ema5": 10},
    ])

    async def histories(_stocks):
        return {"SH.510300": history}

    monkeypatch.setattr(module, "_load_emotion_histories", histories)
    monkeypatch.setattr(module, "_load_daily_frame", lambda _symbols: prices)

    result = __import__("asyncio").run(module.run_szdt_a_stock_backtest(
        [stock], sell_on_ema5_breakdown=True, initial_capital=100_000, slippage_bps=0,
    ))

    assert [trade["side"] for trade in result["trades"]] == ["BUY", "SELL"]
    assert result["trades"][1]["date"] == "2026-01-06"
    assert result["rules"]["execution"] == "same_close; sell_before_buy"
    assert result["metrics"]["max_leverage_ratio"] == 0.2788


def test_backtest_rotates_greedy_holding_to_fund_extreme_fear_buy(monkeypatch):
    stocks = [
        {"code": "SH.510300", "name": "沪深300", "enabled": True, "when_buy": -60, "when_sell": 80,
         "max_position": 100, "buy_amount": 10_000, "sell_amount": 10_000, "buy_factor": 1, "sell_factor": 1, "buy_volume_ratio": 0},
        {"code": "SH.510500", "name": "中证500", "enabled": True, "when_buy": -60, "when_sell": 80,
         "max_position": 100, "buy_amount": 10_000, "sell_amount": 10_000, "buy_factor": 1, "sell_factor": 1, "buy_volume_ratio": 0},
    ]
    days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    histories = {
        "SH.510300": pd.DataFrame([{"date": days[0], "score": -70}, {"date": days[1], "score": 10}, {"date": days[2], "score": 10}]),
        "SH.510500": pd.DataFrame([{"date": days[0], "score": 10}, {"date": days[1], "score": -70}, {"date": days[2], "score": 10}]),
    }
    prices = pd.DataFrame([
        {"code": code, "ts_code": code.split(".")[1] + ".SH", "trade_date": day, "open": 10, "close": 10, "vol": 100, "volume_ratio": 1, "ema5": 10}
        for code in histories for day in days
    ])

    async def load_histories(_stocks):
        return histories

    monkeypatch.setattr(module, "_load_emotion_histories", load_histories)
    monkeypatch.setattr(module, "_load_daily_frame", lambda _symbols: prices)
    result = __import__("asyncio").run(module.run_szdt_a_stock_backtest(
        stocks, sell_on_ema5_breakdown=False, rotation_enabled=True, rotation_threshold=-30,
        initial_capital=10_000,
    ))

    assert result["rules"]["rotation_enabled"] is True
    assert result["metrics"]["rotation_sell_count"] == 1
    assert any(trade["side"] == "ROTATE_SELL" and trade["code"] == "SH.510300" for trade in result["trades"])


def test_backtest_caps_borrowed_exposure_at_configured_max_leverage(monkeypatch):
    stock = {
        "code": "SH.510300", "name": "沪深300", "enabled": True,
        "when_buy": -90, "when_sell": 80, "max_position": 100,
        "buy_amount": 100_000, "sell_amount": 10_000, "buy_factor": 1, "sell_factor": 1,
        "buy_volume_ratio": 0,
    }
    day = date(2026, 1, 2)
    next_day = date(2026, 1, 5)
    history = pd.DataFrame([{"date": day, "score": -100}, {"date": next_day, "score": 0}])
    prices = pd.DataFrame([{
        "code": "SH.510300", "ts_code": "510300.SH", "trade_date": item_day,
        "open": 10, "close": 10, "vol": 100, "volume_ratio": 1, "ema5": 10,
    } for item_day in (day, next_day)])

    async def histories(_stocks):
        return {"SH.510300": history}

    monkeypatch.setattr(module, "_load_emotion_histories", histories)
    monkeypatch.setattr(module, "_load_daily_frame", lambda _symbols: prices)
    result = __import__("asyncio").run(module.run_szdt_a_stock_backtest(
        [stock], sell_on_ema5_breakdown=False, initial_capital=100_000,
        max_leverage_ratio=1.5, slippage_bps=0,
    ))

    assert result["rules"]["max_leverage_ratio"] == 1.5
    assert result["metrics"]["max_leverage_ratio"] <= 1.5
    assert result["metrics"]["max_leverage_ratio"] > 1.4
