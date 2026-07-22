import logging
from datetime import date

import pandas as pd

from src.core.services.tushare import TushareService


class FakePro:
    def __init__(self):
        self.calls = []

    def index_daily(self, **kwargs):
        self.calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "ts_code": "000985.CSI",
                    "trade_date": "20260721",
                    "open": 5800,
                    "high": 5850,
                    "low": 5750,
                    "close": 5833.7268,
                    "pre_close": 5638.6186,
                    "change": 195.1082,
                    "pct_chg": 3.4603,
                    "vol": 1,
                    "amount": 2,
                }
            ]
        )


def test_index_daily_uses_provider_alias_and_preserves_canonical_symbol():
    service = object.__new__(TushareService)
    service.pro = FakePro()
    service.logger = logging.getLogger("test")

    frame = service.get_index_daily_range_frame(
        "000985.SH",
        date(2026, 7, 21),
        date(2026, 7, 21),
    )

    assert service.pro.calls[0]["ts_code"] == "000985.CSI"
    assert frame["ts_code"].tolist() == ["000985.SH"]
    assert frame["trade_date"].tolist() == [date(2026, 7, 21)]
