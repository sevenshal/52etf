from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from src.core.services.duckdb_analytics import load_tushare_realtime_daily_k


def test_realtime_daily_k_rejects_previous_trading_day_row():
    service = MagicMock()
    service.get_a_stock_realtime_rt_k_frame.return_value = pd.DataFrame([
        {
            "ts_code": "600001.SH",
            "trade_time": "2026-06-19 15:00:00",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
        }
    ])
    with patch("src.core.services.tushare.TushareService.get_instance", return_value=service):
        result = load_tushare_realtime_daily_k("600001.SH", date(2026, 6, 22))
    assert result is None


def test_realtime_daily_k_returns_valid_current_day_ohlc():
    service = MagicMock()
    service.get_a_stock_realtime_rt_k_frame.return_value = pd.DataFrame([
        {
            "ts_code": "600001.SH",
            "trade_time": "2026-06-22 10:35:00",
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.8,
        }
    ])
    with patch("src.core.services.tushare.TushareService.get_instance", return_value=service):
        result = load_tushare_realtime_daily_k("600001.SH", date(2026, 6, 22))
    assert result == {
        "trade_date": date(2026, 6, 22),
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.8,
        "source": "tushare_rt_k",
    }
