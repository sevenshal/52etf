"""个股详情行情头部接口：把当日估值快照原样带出来（含市销率）。"""

import pytest

from src.app.api import stock as stock_api

LATEST = {
    "trade_date": "2026-09-18", "close": 50.59, "total_share": 36500.0, "float_share": 26000.0,
    "pe": 19.50, "pe_ttm": 21.29, "pb": 2.56, "ps": 1.83, "ps_ttm": 1.77,
    "dv_ratio": 1.10, "dv_ttm": 1.13,
}


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, latest):
        self.latest = latest
        self.closed = False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "a_stock_basic" in sql:
            return _Result({"name": "尚太科技"})
        if "a_stock_market_daily" in sql:
            assert "ps" in sql and "ps_ttm" in sql  # 市销率要真的查出来，不能只在返回里写死
            return _Result(self.latest)
        raise AssertionError(f"unexpected query: {sql[:60]}")

    def close(self):
        self.closed = True


class _FakeSessionFactory:
    def __init__(self, latest):
        self.session = _FakeSession(latest)

    def __call__(self):
        return self.session

    def remove(self):
        pass


@pytest.fixture
def summary(monkeypatch):
    def _call(latest=LATEST, financials=None):
        monkeypatch.setattr(stock_api, "AnalyticsSession", _FakeSessionFactory(latest))
        monkeypatch.setattr(stock_api, "get_static_info_snapshot_map", lambda db, symbols: {})
        monkeypatch.setattr(
            stock_api, "load_a_stock_financials",
            financials or (lambda symbol, periods: {"periods": [{
                "end_date": "2025-12-31", "is_annual": True, "period_label": "2025年报",
                "indicator": {"profit_dedt": 1_500_000_000.0},
                "balancesheet": {"total_hldr_eqy_exc_min_int": 10_000_000_000.0},
            }]}),
        )
        return stock_api.get_a_stock_summary("001301.sz", _="account", db=object())
    return _call


def test_price_to_sales_is_returned_alongside_pe_and_pb(summary):
    payload = summary()

    assert payload["symbol"] == "001301.SZ"
    assert payload["pe_ttm"] == pytest.approx(21.29)
    assert payload["pb"] == pytest.approx(2.56)
    assert payload["ps"] == pytest.approx(1.83)
    assert payload["ps_ttm"] == pytest.approx(1.77)


def test_missing_price_to_sales_comes_back_as_none_not_zero(summary):
    payload = summary({**LATEST, "ps": None, "ps_ttm": None})

    assert payload["ps"] is None
    assert payload["ps_ttm"] is None
    assert payload["pb"] == pytest.approx(2.56)


def test_no_valuation_row_at_all_still_returns_the_header(summary):
    payload = summary(None)

    assert payload["name"] == "尚太科技"
    assert payload["ps_ttm"] is None
    assert payload["valuation_close"] is None


def test_deducted_roe_ttm_is_returned_with_its_reporting_period(summary):
    payload = summary()

    assert payload["roe_dt_ttm_pct"] == pytest.approx(15.0)  # 15 亿 / 100 亿
    assert payload["roe_dt_ttm_period"] == "2025年报"


def test_a_failing_financials_read_does_not_break_the_price_header(summary):
    def _boom(symbol, periods):
        raise RuntimeError("analytics db unavailable")

    payload = summary(financials=_boom)

    assert payload["pe_ttm"] == pytest.approx(21.29)  # 行情头部照常
    assert payload["roe_dt_ttm_pct"] is None
