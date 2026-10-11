from datetime import date

import pandas as pd

from src.core.services import szdt_us_backtest as module


def test_us_backtest_uses_same_close_and_production_factor(monkeypatch):
    stock = {
        "code": "US.SPY", "name": "标普", "enabled": True, "type": 2,
        "when_buy": -60, "when_sell": 60, "max_position": 100,
        "buy_amount": 10_000, "sell_amount": 10_000, "buy_factor": 2, "sell_factor": 2,
        "buy_volume_ratio": 1.3,
    }
    history = pd.DataFrame([
        {"date": date(2026, 1, 2), "score": -80},
        {"date": date(2026, 1, 5), "score": 80},
        {"date": date(2026, 1, 6), "score": 0},
    ])
    prices = pd.DataFrame([
        {"code": "US.SPY", "date": date(2026, 1, 2), "close": 100, "volume": 100, "volume_ratio": 1.5},
        {"code": "US.SPY", "date": date(2026, 1, 5), "close": 110, "volume": 100, "volume_ratio": 1.5},
        {"code": "US.SPY", "date": date(2026, 1, 6), "close": 110, "volume": 100, "volume_ratio": 1.5},
    ])

    async def histories(_stocks):
        return {"US.SPY": history}

    async def daily(_codes, **_kwargs):
        return prices

    monkeypatch.setattr(module, "_load_emotion_histories", histories)
    monkeypatch.setattr(module, "_load_us_daily_frame", daily)

    result = __import__("asyncio").run(module.run_szdt_us_backtest([stock], account_id="test", initial_capital=100_000))

    assert [trade["side"] for trade in result["trades"]] == ["BUY", "SELL"]
    assert result["trades"][0]["date"] == "2026-01-02"
    # buy: 10,000 * 3 ** (0.5 ** 2) = 13,161.64..., not 10,000 * 2.
    assert result["trades"][0]["amount"] == 13_100.0
    assert result["rules"]["execution"] == "same_close; sell_before_buy"
    assert result["coverage"]["types"] == [1, 2, 7]
