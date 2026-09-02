from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from src.app.api.stock import _load_current_a_stock_kline


def _service_with_stock_frame(rows):
    service = MagicMock()
    service.get_a_stock_realtime_rt_k_frame.return_value = pd.DataFrame(rows)
    service.get_a_stock_realtime_etf_rt_k_frame.return_value = pd.DataFrame()
    return service


def test_current_a_stock_kline_uses_today_rt_k_ohlcv():
    today = date(2026, 9, 2)
    service = _service_with_stock_frame([{
        "ts_code": "600519.SH",
        "trade_time": "2026-09-02 10:35:00",
        "open": 1400.0,
        "high": 1420.0,
        "low": 1395.0,
        "close": 1410.0,
        "vol": 12345.0,
        "amount": 1730000.0,
    }])
    with patch("src.app.api.stock.TushareService.get_instance", return_value=service):
        result = _load_current_a_stock_kline("600519.SH", today)

    assert result == {
        "timestamp": datetime(2026, 9, 2, 15),
        "open": 1400.0,
        "high": 1420.0,
        "low": 1395.0,
        "close": 1410.0,
        "volume": 12345.0,
        "turnover": 1730000.0,
        "turnover_rate": None,
    }


def test_current_a_stock_kline_rejects_previous_trading_day():
    service = _service_with_stock_frame([{
        "ts_code": "600519.SH",
        "trade_time": "2026-09-01 15:00:00",
        "open": 1400.0,
        "high": 1420.0,
        "low": 1395.0,
        "close": 1410.0,
    }])
    with patch("src.app.api.stock.TushareService.get_instance", return_value=service):
        result = _load_current_a_stock_kline("600519.SH", date(2026, 9, 2))

    assert result is None
