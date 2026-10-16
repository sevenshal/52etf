from src.app.api.a_stock_fear_backtest import get_a_stock_fear_backtest_options
from src.app.api.a_stock_fear_volume_config import HK_INNOVATION_DRUG_FEAR_SOURCE_KEY


def test_a_stock_backtest_options_include_hk_innovation_drug_only_for_research():
    payload = get_a_stock_fear_backtest_options(account_id="test")

    assert any(item["value"] == "513120.SH" for item in payload["symbol_options"])
    assert any(item["value"] == "513120.SH" for item in payload["volume_signal_symbol_options"])
    hk_source = next(
        item for item in payload["fear_source_options"]
        if item["value"] == HK_INNOVATION_DRUG_FEAR_SOURCE_KEY
    )
    assert hk_source["symbol"] == "931250.CSI"
    assert hk_source["market"] == "hk"
