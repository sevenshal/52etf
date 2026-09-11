"""K 线事件标记：研报按"篇"计数、目标价与 K 线同为前复权口径、财报用首次披露日。"""

from datetime import date

import pytest

from src.core.services import a_stock_chart_events as events

SYMBOL = "688981.SH"
START, END = date(2026, 1, 1), date(2026, 9, 10)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


def _report(report_date, org, title, quarter, *, eps=None, np=None, low=None, high=None, rating="买入"):
    return {
        "ts_code": SYMBOL, "report_name": "中芯国际", "report_date": report_date,
        "report_title": title, "org_name": org, "author_name": "分析师", "quarter": quarter,
        "eps": eps, "pe": None, "np": np, "rating": rating,
        "max_price": high, "min_price": low, "create_time": None,
    }


# 6 月 1 日 10 送 5：复权因子 1.0 → 1.5，最新因子 1.5
FACTORS = [
    {"ts_code": SYMBOL, "trade_date": date(2025, 12, 1), "adj_factor": 1.0},
    {"ts_code": SYMBOL, "trade_date": date(2026, 6, 1), "adj_factor": 1.5},
]
REPORTS = [
    # 同一篇研报按预测年份拆成三行，只能算一篇
    _report(date(2026, 3, 10), "光大证券", "2025年报点评", "2026Q4", eps=1.5, np=500000.0, low=30.0, high=30.0),
    _report(date(2026, 3, 10), "光大证券", "2025年报点评", "2027Q4", eps=1.8, np=600000.0, low=30.0, high=30.0),
    _report(date(2026, 3, 10), "光大证券", "2025年报点评", "2028Q4", eps=2.1, np=700000.0, low=30.0, high=30.0),
    # 同一天另一家机构，没给目标价
    _report(date(2026, 3, 10), "招商证券", "业绩超预期", "2026Q4", eps=1.4),
    # 除权之后写的研报：因子已是 1.5，不需要换算
    _report(date(2026, 8, 21), "华泰证券", "2Q超预期", "2026Q4", eps=1.2, low=150.0, high=160.0),
    # 只有季度预测的点评：没有全年预测，但这篇研报本身要列出来
    _report(date(2026, 8, 21), "中金公司", "中报快评", "2026Q2", eps=0.4),
]
DISCLOSURES = [
    # MIN(ann_date) 已在 SQL 里算好：这是首次披露日
    {"ts_code": SYMBOL, "end_date": date(2025, 12, 31), "ann_date": date(2026, 3, 27)},
    {"ts_code": SYMBOL, "end_date": date(2026, 6, 30), "ann_date": date(2026, 8, 21)},
    # 窗口之前披露的，不该出现
    {"ts_code": SYMBOL, "end_date": date(2025, 9, 30), "ann_date": date(2025, 10, 28)},
]


class FakeDb:
    def __init__(self):
        self.report_params = None

    def execute(self, statement, params=None):
        sql = str(statement)
        if "a_stock_adj_factor" in sql:
            return _Result(FACTORS)
        if "a_stock_income" in sql:
            return _Result(DISCLOSURES)
        if "a_stock_report_rc" in sql:
            self.report_params = params
            return _Result(REPORTS)
        raise AssertionError(f"unexpected query: {sql[:80]}")


@pytest.fixture(autouse=True)
def fake_financials(monkeypatch):
    def _load(symbol, periods):
        return {"ts_code": symbol, "periods": [{
            "end_date": "2026-06-30",
            "income": {"revenue": 19_527_223_323.13, "n_income_attr_p": 844_644_275.04},
            "indicator": {"or_yoy": 4.96, "netprofit_yoy": 79.41, "profit_dedt": 808_334_196.16,
                          "dt_netprofit_yoy": 84.73, "grossprofit_margin": 13.9, "roe_waa": 2.92},
            "cashflow": {"n_cashflow_act": 2_964_426_853.22},
        }]}
    monkeypatch.setattr(events, "load_a_stock_financials", _load)


def _load():
    db = FakeDb()
    return db, events.load_a_stock_chart_events(db, "688981.sh", start=START, end=END)


def test_a_report_split_into_yearly_rows_counts_once():
    _, result = _load()
    march = next(day for day in result["research_days"] if day["date"] == "2026-03-10")

    assert march["count"] == 2  # 光大一篇 + 招商一篇，不是 4 行
    everbright = next(r for r in march["reports"] if r["org_name"] == "光大证券")
    assert [f["fiscal_year"] for f in everbright["forecasts"]] == [2026, 2027, 2028]


def test_target_price_and_eps_are_converted_to_the_forward_adjusted_kline_basis():
    """3 月的研报写在 10 送 5 之前，目标价 30 元在前复权 K 线上对应 20 元。"""
    _, result = _load()
    march = next(day for day in result["research_days"] if day["date"] == "2026-03-10")
    everbright = next(r for r in march["reports"] if r["org_name"] == "光大证券")

    assert everbright["target_low_raw"] == pytest.approx(30.0)
    assert everbright["target_low"] == pytest.approx(20.0)
    assert everbright["price_scale"] == pytest.approx(0.6667, abs=1e-4)
    assert everbright["forecasts"][0]["eps"] == pytest.approx(1.0)
    assert everbright["forecasts"][0]["eps_raw"] == pytest.approx(1.5)
    assert everbright["forecasts"][0]["np"] == pytest.approx(500000.0)  # 净利润不随送转变


def test_reports_written_after_the_split_are_not_rescaled():
    _, result = _load()
    august = next(day for day in result["research_days"] if day["date"] == "2026-08-21")
    huatai = next(r for r in august["reports"] if r["org_name"] == "华泰证券")

    assert huatai["price_scale"] == pytest.approx(1.0)
    assert (huatai["target_low"], huatai["target_high"]) == (150.0, 160.0)


def test_a_report_without_full_year_forecasts_is_still_listed():
    """标记数的是研报篇数；季度点评没有全年预测，但它确实是那天发的一篇研报。"""
    _, result = _load()
    august = next(day for day in result["research_days"] if day["date"] == "2026-08-21")
    cicc = next(r for r in august["reports"] if r["raw_org_name"] == "中金公司")

    assert august["count"] == 2
    assert cicc["forecasts"] == []
    assert cicc["target_low"] is None
    # 机构名走和估值去重同一套别名归一：弹窗里看到的机构名和估值详情里对得上
    assert cicc["org_name"] == "中金"


def test_missing_target_price_is_none_not_zero():
    _, result = _load()
    march = next(day for day in result["research_days"] if day["date"] == "2026-03-10")
    cmb = next(r for r in march["reports"] if r["org_name"] == "招商证券")

    assert cmb["target_low"] is None and cmb["target_high"] is None


def test_financial_markers_use_first_disclosure_and_carry_summary_values():
    _, result = _load()
    reports = result["financial_reports"]

    assert [r["date"] for r in reports] == ["2026-03-27", "2026-08-21"]  # 窗口外的 2025 三季报不出现
    interim = reports[1]
    assert interim["period_label"] == "2026中报"
    assert interim["is_annual"] is False
    assert interim["revenue"] == pytest.approx(19_527_223_323.13)
    assert interim["netprofit_yoy"] == pytest.approx(79.41)
    assert interim["profit_dedt"] == pytest.approx(808_334_196.16)


def test_a_disclosure_without_synced_values_is_flagged_not_dropped():
    _, result = _load()
    annual = result["financial_reports"][0]

    assert annual["period_label"] == "2025年报"
    assert annual["has_values"] is False
    assert annual["revenue"] is None


def test_report_query_is_scoped_to_the_symbol_and_window():
    db, result = _load()

    assert result["ts_code"] == SYMBOL
    assert db.report_params["symbol_0"] == SYMBOL
    assert db.report_params["report_start"] == START
    assert db.report_params["report_end"] == END


def test_blank_symbol_returns_empty():
    assert events.load_a_stock_chart_events(FakeDb(), "", start=START, end=END) == {
        "ts_code": "", "research_days": [], "financial_reports": [],
    }
