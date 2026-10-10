from src.core.services.szdt_etf_volume import normalize_etf_symbol


def test_normalize_szdt_etf_code_to_tushare_code():
    assert normalize_etf_symbol("SH.510300") == "510300.SH"
    assert normalize_etf_symbol("159941.SZ") == "159941.SZ"
