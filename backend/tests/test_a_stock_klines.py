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
    def __init__(self, rows, fund_rows=None):
        self.rows = rows
        self.fund_rows = fund_rows or []

    def execute(self, statement, params=None):
        query = str(statement)
        if "a_stock_market_daily_qfq" in query:
            return _Result(self.rows)
        if "a_stock_fund_daily_qfq" in query:
            return _Result(self.fund_rows)
        return _Result([])


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


def test_etf_daily_history_falls_back_to_fund_daily_table():
    """ETF 不在个股日线表时，历史 K 线不能只剩实时当天一根。"""
    fund_rows = [{
        "trade_date": date(2024, 6, 17), "open": 3.8, "high": 3.9, "low": 3.7,
        "close": 3.85, "volume": 123456.0, "turnover": 456789.0, "turnover_rate": None,
    }]

    result = load_a_stock_klines(
        _FakeDb([], fund_rows=fund_rows), "510300.SH",
        start_date=date(2024, 6, 1), end_date=date(2024, 6, 30),
    )

    assert len(result) == 1
    assert result[0]["volume"] == 123456.0
