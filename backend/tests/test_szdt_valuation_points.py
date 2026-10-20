from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api import szdt


def test_a_stock_etf_valuation_points_normalize_codes_and_skip_unsupported(monkeypatch):
    requested_symbols = []

    def fake_load(symbols):
        requested_symbols.extend(symbols)
        return {
            "000016.SH": {"status": "available", "valuation_position_pct": 82.5},
            "930707.CSI": {"status": "available", "valuation_position_pct": 19.0},
        }

    monkeypatch.setattr(szdt, "load_a_stock_index_valuations", fake_load)
    app = FastAPI()
    app.include_router(szdt.router)
    response = TestClient(app).get(
        "/api/quant/etf/valuation-points",
        params={"codes": "SH.510050,SZ.159865,SH.518880,SH.513500"},
    )

    assert response.status_code == 200
    assert requested_symbols == ["000016.SH", "930707.CSI"]
    assert response.json()["data"] == {
        "510050.SH": {"symbol": "000016.SH", "status": "available", "valuation_position_pct": 82.5},
        "159865.SZ": {"symbol": "930707.CSI", "status": "available", "valuation_position_pct": 19.0},
    }
