"""A 股日 K 接口的换手率口径：tushare 给的是百分数，接口统一返回小数。"""

from datetime import date

import pytest

from src.core.services.a_stock_consensus import load_a_stock_klines


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement, params=None):
        return _Result(self.rows if "a_stock_market_daily_qfq" in str(statement) else [])


def test_turnover_rate_is_converted_from_tushare_percent_to_fraction():
    # 生产数据：300750.SZ 2026-09-08 的 daily_basic.turnover_rate = 1.22，即 1.22%
    rows = [
        {
            "trade_date": date(2026, 9, 8), "open": 400.0, "high": 410.0, "low": 395.0,
            "close": 405.0, "volume": 500000.0, "turnover": 2.0e7, "turnover_rate": 1.22,
        },
        {
            "trade_date": date(2026, 9, 9), "open": 405.0, "high": 409.0, "low": 401.0,
            "close": 402.0, "volume": 450000.0, "turnover": 1.8e7, "turnover_rate": None,
        },
    ]

    result = load_a_stock_klines(
        _FakeDb(rows), "300750", start_date=date(2026, 9, 1), end_date=date(2026, 9, 10)
    )

    # 前端换手衰减按小数计算，1.22 原样透传会被截成 1、每天清空成交分布
    assert result[0]["turnover_rate"] == pytest.approx(0.0122)
    assert result[1]["turnover_rate"] is None


def test_batch_loader_uses_the_same_turnover_fraction():
    """选股系统的批量 K 线和个股详情页共用行格式，换手率同样是小数口径。"""
    from src.core.services.a_stock_consensus import load_a_stock_klines_batch

    rows = [{
        "ts_code": "300750.SZ", "trade_date": date(2026, 9, 8), "open": 400.0, "high": 410.0, "low": 395.0,
        "close": 405.0, "volume": 500000.0, "turnover": 2.0e7, "turnover_rate": 1.22,
    }]
    result = load_a_stock_klines_batch(
        _FakeDb(rows), ["300750.SZ"], start_date=date(2026, 9, 1), end_date=date(2026, 9, 10)
    )
    assert result["300750.SZ"][0]["turnover_rate"] == pytest.approx(0.0122)
