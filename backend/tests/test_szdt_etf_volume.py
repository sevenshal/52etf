from datetime import datetime

import pandas as pd

from src.core.services import szdt_etf_volume
from src.core.services.szdt_etf_volume import normalize_etf_symbol


def test_normalize_szdt_etf_code_to_tushare_code():
    assert normalize_etf_symbol("SH.510300") == "510300.SH"
    assert normalize_etf_symbol("159941.SZ") == "159941.SZ"


def test_etf_volume_metrics_exposes_realtime_turnover(monkeypatch):
    class Service:
        def get_a_stock_realtime_etf_rt_k_frame(self, _symbols):
            return pd.DataFrame([{
                "ts_code": "510300.SH",
                "trade_time": datetime.now().replace(second=0, microsecond=0).isoformat(),
                "vol": 12_345_600,
                "amount": 45_678_900,
            }])

    monkeypatch.setattr(szdt_etf_volume, "ensure_etf_minute_history_async", lambda _codes: None)
    monkeypatch.setattr(szdt_etf_volume.TushareService, "get_instance", classmethod(lambda _cls: Service()))
    monkeypatch.setattr(szdt_etf_volume, "get_persisted_etf_intraday_volume_baseline", lambda *_args, **_kwargs: {"510300.SH": 10_000_000})
    monkeypatch.setattr(szdt_etf_volume, "_cache", {"at": 0.0, "metrics": {}})

    item = szdt_etf_volume.get_a_share_etf_volume_metrics(["SH.510300"])["510300.SH"]

    assert item["volume"] == 12_345_600
    assert item["turnover"] == 45_678_900
    assert item["volume_ratio"] == 1.235
    assert item["volume_ratio_mode"] == "same_time_cumulative_20d"


def test_etf_volume_metrics_uses_daily_fallback_for_stale_quote(monkeypatch):
    class Service:
        def get_a_stock_realtime_etf_rt_k_frame(self, _symbols):
            return pd.DataFrame([{
                "ts_code": "510300.SH", "trade_time": "2026-10-09T15:00:00", "vol": 0, "amount": 0,
            }])

    daily = {
        "510300.SH": {
            "volume": 1_000_000,
            "turnover": 2_000_000,
            "volume_ratio": 1.3,
            "volume_baseline_20d": 769_230,
            "volume_ratio_mode": "daily_20d",
            "trade_date": "2026-10-09",
        }
    }
    monkeypatch.setattr(szdt_etf_volume, "ensure_etf_minute_history_async", lambda _codes: None)
    monkeypatch.setattr(szdt_etf_volume.TushareService, "get_instance", classmethod(lambda _cls: Service()))
    monkeypatch.setattr(szdt_etf_volume, "_latest_daily_metrics", lambda _symbols: daily)
    monkeypatch.setattr(szdt_etf_volume, "_cache", {"at": 0.0, "metrics": {}})

    item = szdt_etf_volume.get_a_share_etf_volume_metrics(["SH.510300"])["510300.SH"]

    assert item == daily["510300.SH"]
