import logging
import threading
from unittest.mock import MagicMock

import pandas as pd

from src.core.services.tushare import TushareService


def _service_with_pro(pro):
    service = TushareService.__new__(TushareService)
    service.logger = logging.getLogger("test-tushare-realtime-cache")
    service.pro = pro
    service._realtime_cache_lock = threading.RLock()
    service._realtime_fetch_lock = threading.Lock()
    service._rt_k_market_cache = (0.0, None)
    service._rt_k_symbol_cache = {}
    service._rt_etf_symbol_cache = {}
    service._rt_k_backoff_until = 0.0
    service._rt_k_rate_limiter = MagicMock()
    return service


def _frame(*codes):
    return pd.DataFrame([
        {
            "ts_code": code,
            "close": 10.0,
            "pre_close": 9.5,
            "open": 9.8,
            "high": 10.2,
            "low": 9.7,
            "vol": 1000,
            "amount": 10000,
            "trade_time": "2026-09-29 10:00:00",
            "ask_price1": 10.01,
            "bid_price1": 9.99,
        }
        for code in codes
    ])


def test_full_market_snapshot_serves_later_symbol_queries_without_new_request():
    pro = MagicMock()
    pro.rt_k.return_value = _frame("600000.SH", "000001.SZ")
    service = _service_with_pro(pro)

    market = service.get_a_stock_realtime_market_frame()
    symbol = service.get_a_stock_realtime_rt_k_frame(["600000.SH"])

    assert set(market["ts_code"]) == {"600000.SH", "000001.SZ"}
    assert list(symbol["ts_code"]) == ["600000.SH"]
    assert symbol.iloc[0]["ask_price1"] == 10.01
    pro.rt_k.assert_called_once()


def test_repeated_symbol_query_uses_shared_symbol_cache():
    pro = MagicMock()
    pro.rt_k.return_value = _frame("600000.SH")
    service = _service_with_pro(pro)

    first = service.get_a_stock_realtime_rt_k_frame(["600000.SH"])
    second = service.get_a_stock_realtime_rt_k_frame(["600000.SH"])

    assert not first.empty and not second.empty
    pro.rt_k.assert_called_once()


def test_rate_limit_error_starts_backoff_and_suppresses_follow_up_requests():
    pro = MagicMock()
    pro.rt_k.side_effect = Exception("抱歉，您访问接口(rt_k)频率超限(50次/分钟)")
    service = _service_with_pro(pro)

    first = service.get_a_stock_realtime_market_frame()
    second = service.get_a_stock_realtime_market_frame()

    assert first.empty and second.empty
    assert service._rt_k_backoff_until > 0
    pro.rt_k.assert_called_once()


def test_repeated_etf_query_uses_shared_symbol_cache():
    pro = MagicMock()
    pro.rt_etf_k.return_value = _frame("510300.SH")
    service = _service_with_pro(pro)

    first = service.get_a_stock_realtime_etf_rt_k_frame(["510300.SH"])
    second = service.get_a_stock_realtime_etf_rt_k_frame(["510300.SH"])

    assert not first.empty and not second.empty
    pro.rt_etf_k.assert_called_once()
