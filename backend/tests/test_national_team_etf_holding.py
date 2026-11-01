from datetime import date
from unittest.mock import patch


def test_holding_dashboard_uses_complete_four_etf_basket_and_calculates_percentile():
    from src.core.services import national_team_etf_holding as service

    history = [
        {"trade_date": date(2026, 10, 20), "etf_count": 4, "component_count": 300,
         "etf_market_value_yi": 1_000.0, "hs300_float_market_value_yi": 100_000.0, "holding_ratio_pct": 1.0},
        {"trade_date": date(2026, 10, 21), "etf_count": 4, "component_count": 300,
         "etf_market_value_yi": 1_200.0, "hs300_float_market_value_yi": 100_000.0, "holding_ratio_pct": 1.2},
    ]
    funds = [{"ts_code": symbol, "market_value_yi": 300.0, "fd_share": 100.0, "close": 3.0}
             for symbol, _ in service.HS300_ETFS]
    with patch.object(service, "_load_history", return_value=history), patch.object(service, "_load_latest_funds", return_value=funds):
        payload = service.fetch_national_team_etf_holding(days=20)

    assert [item[0] for item in service.HS300_ETFS] == ["510300.SH", "510310.SH", "510330.SH", "159919.SZ"]
    assert payload["latest"]["holding_ratio_pct_percentile_5y"] == 100.0
    assert payload["latest"]["funds"] == funds
    assert payload["methodology"]["name"] == "国家队ETF持仓占比"


def test_holding_dashboard_requires_data():
    from src.core.services import national_team_etf_holding as service

    with patch.object(service, "_load_history", return_value=[]):
        try:
            service.fetch_national_team_etf_holding()
        except service.NationalTeamHoldingDataError:
            pass
        else:
            raise AssertionError("empty history must raise a clear data error")
