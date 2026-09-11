from datetime import date

from src.core.database import AStockConsensusPeBand, Session
from src.core.services import a_stock_consensus_pe_band as pe_band_service


def _clear():
    db = Session()
    try:
        db.query(AStockConsensusPeBand).delete()
        db.commit()
    finally:
        Session.remove()


def test_refresh_saves_json_safe_bands(monkeypatch):
    _clear()

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self._scalar = scalar
            self._rows = list(rows)

        def scalar(self):
            return self._scalar

        def all(self):
            return self._rows

    class FakeAnalyticsSession:
        def execute(self, statement, params=None):
            if "MAX(trade_date)" in str(statement):
                return FakeResult(scalar=date(2026, 9, 10))
            return FakeResult(rows=[("600612.SH",), ("000001.SZ",)])

        def close(self):
            pass

    class FakeAnalyticsSessionFactory:
        def __call__(self):
            return FakeAnalyticsSession()

        def remove(self):
            pass

    captured = {}

    def fake_compute(db, symbols, as_of):
        captured["symbols"] = list(symbols)
        return {
            "600612.SH": {"status": "available", "low_pe": 10.0, "high_pe": 20.0, "as_of": as_of},
            "000001.SZ": {"status": "unavailable", "reason": "insufficient_history", "as_of": as_of},
        }

    monkeypatch.setattr(pe_band_service, "AnalyticsSession", FakeAnalyticsSessionFactory())
    monkeypatch.setattr(pe_band_service, "compute_forward_pe_bands", fake_compute)

    result = pe_band_service.refresh_a_stock_consensus_pe_bands()

    assert result == {"as_of": "2026-09-10", "saved": 2, "available": 1}
    assert captured["symbols"] == ["600612.SH", "000001.SZ"]
    bands = pe_band_service.load_a_stock_consensus_pe_bands()
    assert bands["600612.SH"]["low_pe"] == 10.0
    assert bands["600612.SH"]["as_of"] == "2026-09-10"


def test_load_skips_stale_bands_and_filters_symbols():
    _clear()
    db = Session()
    try:
        db.add(AStockConsensusPeBand(symbol="600612.SH", as_of=date(2026, 9, 10), payload={"status": "available"}))
        db.add(AStockConsensusPeBand(symbol="000001.SZ", as_of=date(2026, 9, 5), payload={"status": "available"}))
        db.add(AStockConsensusPeBand(symbol="000002.SZ", as_of=date(2026, 8, 1), payload={"status": "available"}))
        db.commit()
    finally:
        Session.remove()

    assert set(pe_band_service.load_a_stock_consensus_pe_bands()) == {"600612.SH", "000001.SZ"}
    assert set(pe_band_service.load_a_stock_consensus_pe_bands(["600612"])) == {"600612.SH"}
