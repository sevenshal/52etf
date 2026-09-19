"""卖方一致预期估值详情接口：盈利预测指引的「较上次」是附加信息，算不出来也不能让估值弹窗打不开。"""

import pytest

from src.app.api import evc


class _FakeSession:
    closed = False

    def close(self):
        self.closed = True


class _FakeSessionFactory:
    def __init__(self):
        self.session = _FakeSession()
        self.removed = False

    def __call__(self):
        return self.session

    def remove(self):
        self.removed = True


DETAIL = {
    "status": "ok",
    "organizations": [
        {"org_name": "华泰证券", "report_date": "2026-08-21", "author_name": "边文姣", "report_title": "中报点评",
         "forecasts": [{"quarter": "2026Q4", "fiscal_year": 2026, "eps": 1.05}]},
    ],
}


@pytest.fixture
def route(monkeypatch):
    factory = _FakeSessionFactory()
    monkeypatch.setattr(evc, "AnalyticsSession", factory)
    monkeypatch.setattr(evc, "load_a_stock_consensus_detail",
                        lambda db, symbol, use_pe_band=False: {**DETAIL, "organizations": [dict(o, forecasts=[dict(f) for f in o["forecasts"]]) for o in DETAIL["organizations"]]})
    return factory


def test_a_failing_revision_add_on_still_returns_the_valuation_detail(route, monkeypatch):
    def _boom(db, symbol, detail):
        raise RuntimeError("report_rc unavailable")

    monkeypatch.setattr(evc, "attach_consensus_eps_revisions", _boom)
    detail = evc.get_a_stock_consensus_detail("001301.SZ", use_pe_band=False, account_id="x")

    assert detail["status"] == "ok"
    assert "revision" not in detail["organizations"][0]["forecasts"][0]  # 没算出来：字段缺席，前端不加说明
    assert route.session.closed and route.removed


def test_revisions_are_attached_when_available(route, monkeypatch):
    def _attach(db, symbol, detail):
        for org in detail["organizations"]:
            for forecast in org["forecasts"]:
                forecast["revision"] = {"change_pct": 5.0, "match": "analyst"}
        return detail

    monkeypatch.setattr(evc, "attach_consensus_eps_revisions", _attach)
    detail = evc.get_a_stock_consensus_detail("001301.SZ", use_pe_band=False, account_id="x")

    assert detail["organizations"][0]["forecasts"][0]["revision"]["change_pct"] == 5.0
