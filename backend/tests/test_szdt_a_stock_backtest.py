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
        [stock], sell_on_ema5_breakdown=True, initial_capital=100_000,
    ))

    assert [trade["side"] for trade in result["trades"]] == ["BUY", "SELL"]
    assert result["trades"][1]["date"] == "2026-01-07"
