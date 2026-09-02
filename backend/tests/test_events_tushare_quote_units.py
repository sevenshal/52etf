import asyncio
from unittest.mock import MagicMock, patch

import pandas as pd

from src.app.api.events import _fetch_missing_tushare_quotes


def test_tushare_realtime_quote_converts_shares_and_yuan_to_daily_kline_units():
    service = MagicMock()
    service.get_a_stock_realtime_rt_k_frame.return_value = pd.DataFrame([{
        "ts_code": "600584.SH",
        "trade_time": "2026-09-02 15:00:00",
        "close": 71.31,
        "pre_close": 73.29,
        "open": 71.75,
        "high": 72.49,
        "low": 71.01,
        "vol": 55_345_362,
        "amount": 3_962_423_250,
    }])
    service.get_a_stock_realtime_etf_rt_k_frame.return_value = pd.DataFrame()

    with patch("src.app.api.events.TushareService.get_instance", return_value=service):
        result = asyncio.run(_fetch_missing_tushare_quotes(["600584.SH"]))

    quote = result["600584.SH"]
    assert quote["volume"] == 553_453.62
    assert quote["amount"] == 3_962_423.25
