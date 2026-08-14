"""A股盘中贪恐快照（intraday）计算的单元测试。

核心验证：calculate_intraday 复用日频 _build_raw_signals/_score_raw_signals，
仅注入今日盘中指数点位（rt_idx_k）与实时成分股行情；日频专属组件（期权 PCR、
信用利差、国债避险）通过 ffill 沿用最近日频值，最终 7 个组件全部参与评分。
"""
from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.core.services.a_stock_fear_greed_clone_service import (
    AStockInnovation100FearGreedCloneCalculator,
)


def _build_synthetic_context(calculator, n_days: int = 600):
    rng = np.random.default_rng(7)
    days = pd.bdate_range(end=date(2026, 1, 9), periods=n_days)
    today = date(2026, 1, 12)
    index = pd.DatetimeIndex(list(days) + [pd.Timestamp(today)], name="date")

    level = 3000.0 + np.cumsum(rng.normal(0, 15, n_days))
    levels = pd.DataFrame(
        {
            "open": level * 0.999,
            "high": level * 1.005,
            "low": level * 0.995,
            "level": level,
            "daily_return_pct": 0.0,
            "volume": 1e6,
            "turnover": 1e9,
        },
        index=days,
    )

    intraday = {
        "level": float(level[-1]) * 1.01,
        "daily_return_pct": 1.0,
        "open": float(level[-1]),
        "high": float(level[-1]) * 1.01,
        "low": float(level[-1]) * 0.99,
        "volume": 5e5,
        "turnover": 5e8,
        "quote_source": "rt_idx_k",
        "quote_time": None,
    }

    symbols = ("600000.SH", "000001.SZ")
    holdings = [
        {"symbol": "600000.SH", "name": "A", "weight": 0.6},
        {"symbol": "000001.SZ", "name": "B", "weight": 0.4},
    ]
    holdings_by_date = {ts: holdings for ts in index}
    holdings_as_of = {ts: ts.date() for ts in index}

    def make_frame(base: float) -> pd.DataFrame:
        close = base + np.cumsum(rng.normal(0, 0.3, n_days))
        frame = pd.DataFrame(
            {
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "pct_chg": np.r_[0.0, np.diff(close) / close[:-1] * 100.0],
                "amount": 1e8,
            },
            index=days,
        )
        # 与 _load_constituent_market_frames 一致：reindex 到含今日的 index，ffill OHLC
        frame = frame.reindex(index)
        for column in ("high", "low", "close"):
            frame[column] = frame[column].ffill()
        frame["amount"] = frame["amount"].fillna(0.0)
        return frame

    frames = {
        "600000.SH": make_frame(20.0),
        "000001.SZ": make_frame(15.0),
    }
    quotes = {
        "600000.SH": {
            "symbol": "600000.SH", "price": 21.0, "high": 21.2, "low": 20.8,
            "open": 20.5, "turnover": 1e8, "percent_change": 1.0,
        },
        "000001.SZ": {
            "symbol": "000001.SZ", "price": 15.5, "high": 15.7, "low": 15.3,
            "open": 15.1, "turnover": 5e7, "percent_change": -0.5,
        },
    }

    bond = pd.Series(np.linspace(100, 108, n_days), index=days)
    option_pcr = pd.Series(np.random.uniform(0.8, 1.4, n_days), index=days)
    credit_spread = pd.Series(np.random.uniform(0.5, 1.5, n_days), index=days)

    return {
        "today": today,
        "levels": levels,
        "intraday": intraday,
        "holdings_by_date": holdings_by_date,
        "holdings_as_of": holdings_as_of,
        "frames": frames,
        "quotes": quotes,
        "bond": bond,
        "option_pcr": option_pcr,
        "credit_spread": credit_spread,
    }


def test_calculate_intraday_uses_all_components():
    calculator = AStockInnovation100FearGreedCloneCalculator("000300.SH")
    ctx = _build_synthetic_context(calculator)

    with patch.object(calculator, "_load_levels", return_value=ctx["levels"]), \
         patch.object(calculator, "_fetch_intraday_index_level", return_value=ctx["intraday"]), \
         patch.object(calculator, "_build_holdings_by_date", return_value=(ctx["holdings_by_date"], ctx["holdings_as_of"])), \
         patch.object(calculator, "_load_constituent_market_frames", return_value=ctx["frames"]), \
         patch.object(calculator, "_realtime_quotes", return_value=ctx["quotes"]), \
         patch.object(calculator, "_load_index_close", return_value=ctx["bond"]), \
         patch.object(calculator, "_load_option_volume_pcr", return_value=ctx["option_pcr"]), \
         patch.object(calculator, "_load_credit_spread", return_value=ctx["credit_spread"]):
        result = calculator.calculate_intraday(now=pd.Timestamp(ctx["today"]))

    assert result["component_count"] == 7
    assert len(result["components_used"]) == 7
    assert 0.0 <= result["score"] <= 100.0
    assert result["quote_source"] == "rt_idx_k"
    assert result["index_level"] == ctx["intraday"]["level"]
    assert result["etf_price"]["close"] == round(ctx["intraday"]["level"], 6)


def test_append_intraday_level_replaces_today():
    calculator = AStockInnovation100FearGreedCloneCalculator("000300.SH")
    days = pd.bdate_range(end=date(2026, 1, 9), periods=130)
    levels = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "level": 1.0, "volume": 1.0, "turnover": 1.0},
        index=days,
    )
    intraday = {
        "level": 1234.5, "open": 1200.0, "high": 1250.0, "low": 1190.0,
        "volume": 9.0, "turnover": 8.0,
    }
    today = date(2026, 1, 12)
    result = calculator._append_intraday_level(levels, intraday, today)
    assert pd.Timestamp(today) in result.index
    assert result.loc[pd.Timestamp(today), "level"] == 1234.5
    assert result.loc[pd.Timestamp(today), "high"] == 1250.0
