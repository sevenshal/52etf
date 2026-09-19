"""K 线事件标记：研报按"篇"计数、目标价与 K 线同为前复权口径、财报用首次披露日。"""

from datetime import date, timedelta

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


def _report(report_date, org, title, quarter, *, eps=None, np=None, low=None, high=None, rating="买入",
            author="分析师", created=None):
    return {
        "ts_code": SYMBOL, "report_name": "中芯国际", "report_date": report_date,
        "report_title": title, "org_name": org, "author_name": author, "quarter": quarter,
        "eps": eps, "pe": None, "np": np, "rating": rating,
        "max_price": high, "min_price": low, "create_time": created,
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
    def __init__(self, reports=None, factors=None):
        self.report_params = None
        self.reports = REPORTS if reports is None else reports
        self.factors = FACTORS if factors is None else factors

    def execute(self, statement, params=None):
        sql = str(statement)
        if "a_stock_adj_factor" in sql:
            return _Result(self.factors)
        if "a_stock_income" in sql:
            return _Result(DISCLOSURES)
        if "a_stock_report_rc" in sql:
            self.report_params = params
            return _Result(self.reports)
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
    # 往窗口前多取一段研报，只用来给窗口里最早的研报找"上一篇"
    assert db.report_params["report_start"] == START - timedelta(days=events.REVISION_LOOKBACK_DAYS)
    assert db.report_params["report_end"] == END


def test_blank_symbol_returns_empty():
    assert events.load_a_stock_chart_events(FakeDb(), "", start=START, end=END) == {
        "ts_code": "", "research_days": [], "financial_reports": [],
    }


# --------------------------------------------------------------------------
# 盈利预测修正：同机构同预测年度，优先同分析师，否则退到同机构上一篇
# --------------------------------------------------------------------------
# 2026-06-01 10 送 5：复权因子 1.0 → 1.5
REVISION_FACTORS = [
    {"ts_code": SYMBOL, "trade_date": date(2025, 1, 2), "adj_factor": 1.0},
    {"ts_code": SYMBOL, "trade_date": date(2026, 6, 1), "adj_factor": 1.5},
]
HT = "华泰证券"
REVISION_REPORTS = [
    # 窗口(2026-01-01 起)之前的一篇：只作为"上一篇"参与比对。原始 EPS 1.5 → 前复权 1.0
    _report(date(2025, 11, 10), HT, "三季报点评", "2026Q4", eps=1.5, author="刘俊,边文姣"),
    # 与上一篇共同分析师 刘俊：1.35 → 0.9，较上次 -10%
    _report(date(2026, 3, 10), HT, "年报点评", "2026Q4", eps=1.35, author="刘俊,邵梓洋"),
    # 整个团队换了人：退到同机构上一篇(年报点评 0.9)比较，1.5 → 1.0，+11.11%
    _report(date(2026, 5, 20), HT, "深度报告", "2026Q4", eps=1.5, author="王新人"),
    # 送转之后写的，原始 EPS 1.05 就是前复权口径。和它有共同分析师(边文姣)的是更早的三季报点评，
    # 比 1.0 是 +5%；如果拿原始 EPS 比(1.5 → 1.05)会凭空显示成 -30%
    _report(date(2026, 8, 21), HT, "中报点评", "2026Q4", eps=1.05, author="边文姣,赵某"),
    # 同一天两篇：按入库时间分先后
    _report(date(2026, 9, 1), HT, "调研纪要上午", "2026Q4", eps=1.10, author="刘俊", created="2026-09-01 09:00:00"),
    _report(date(2026, 9, 1), HT, "调研纪要下午", "2026Q4", eps=1.12, author="刘俊", created="2026-09-01 15:00:00"),
    # 另一家机构只有一篇
    _report(date(2026, 4, 1), "招商证券", "首次覆盖", "2026Q4", eps=2.0, author="李某"),
]


def _revisions():
    db = FakeDb(reports=REVISION_REPORTS, factors=REVISION_FACTORS)
    return events.load_a_stock_chart_events(db, SYMBOL, start=START, end=END)


def _forecast(result, title, year=2026):
    for day in result["research_days"]:
        for report in day["reports"]:
            if report["report_title"] == title:
                return next(item for item in report["forecasts"] if item["fiscal_year"] == year)
    raise AssertionError(f"report not found: {title}")


def test_lookback_reports_are_only_used_as_the_previous_one():
    result = _revisions()
    assert "2025-11-10" not in [day["date"] for day in result["research_days"]]
    revision = _forecast(result, "年报点评")["revision"]
    assert revision["prev_date"] == "2025-11-10"


def test_a_shared_analyst_counts_as_the_same_analysts():
    revision = _forecast(_revisions(), "年报点评")["revision"]

    assert revision["match"] == "analyst"
    assert revision["prev_eps"] == pytest.approx(1.0)
    assert revision["prev_eps_raw"] == pytest.approx(1.5)
    assert revision["change_pct"] == pytest.approx(-10.0)


def test_a_new_team_falls_back_to_the_previous_report_of_the_same_broker():
    revision = _forecast(_revisions(), "深度报告")["revision"]

    assert revision["match"] == "org"
    assert revision["prev_date"] == "2026-03-10"
    assert revision["change_pct"] == pytest.approx(11.11)


def test_an_older_same_analyst_report_wins_over_newer_ones_from_other_teams():
    revision = _forecast(_revisions(), "中报点评")["revision"]

    assert revision["match"] == "analyst"
    assert revision["prev_date"] == "2025-11-10"  # 跳过更近但换了人的深度报告、年报点评
    assert revision["prev_authors"] == "刘俊,边文姣"


def test_revisions_compare_forward_adjusted_eps_across_a_bonus_issue():
    """10 送 5 前后：比前复权 EPS 是 +5%，比原始 EPS 会凭空显示成 -30%。"""
    forecast = _forecast(_revisions(), "中报点评")

    assert forecast["eps_raw"] == pytest.approx(1.05)
    assert forecast["revision"]["prev_eps_raw"] == pytest.approx(1.5)
    assert forecast["revision"]["change_pct"] == pytest.approx(5.0)


def test_disclosures_between_the_two_forecasts_are_listed():
    assert _forecast(_revisions(), "中报点评")["revision"]["disclosed_between"] == ["2025年报", "2026中报"]
    assert _forecast(_revisions(), "年报点评")["revision"]["disclosed_between"] == []


def test_same_day_reports_are_ordered_by_entry_time():
    result = _revisions()
    afternoon = _forecast(result, "调研纪要下午")["revision"]
    morning = _forecast(result, "调研纪要上午")["revision"]

    assert afternoon["prev_date"] == "2026-09-01"
    assert afternoon["prev_eps"] == pytest.approx(1.10)
    assert afternoon["change_pct"] == pytest.approx(1.82)
    # 上午那篇往前找：中报点评、深度报告都没有 刘俊，年报点评有
    assert morning["prev_date"] == "2026-03-10"
    assert morning["change_pct"] == pytest.approx(22.22)


def test_history_lists_the_brokers_forecasts_and_marks_shared_analysts():
    history = _forecast(_revisions(), "中报点评")["history"]

    assert [row["report_date"] for row in history] == ["2025-11-10", "2026-03-10", "2026-05-20", "2026-08-21"]
    assert [row["shares_analyst"] for row in history] == [True, False, False, True]
    assert [row["change_pct"] for row in history] == [None, pytest.approx(-10.0), pytest.approx(11.11), pytest.approx(5.0)]
    assert history[-1]["disclosed_between"] == ["2026中报"]


def test_a_broker_with_a_single_report_has_no_revision():
    forecast = _forecast(_revisions(), "首次覆盖")

    assert forecast["revision"] is None
    assert len(forecast["history"]) == 1
