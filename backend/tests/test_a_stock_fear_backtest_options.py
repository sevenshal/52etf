from src.app.api.a_stock_fear_backtest import get_a_stock_fear_backtest_options
from src.app.api.a_stock_fear_volume_config import HK_INNOVATION_DRUG_FEAR_SOURCE_KEY
from src.app.api import soxl_fear_backtest as legacy


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
    # 一键预设：港股创新药ETF × 港股自算贪恐
    hk_pair = next(
        item for item in payload["a_stock_preset_pairs"]
        if item["fear_source"] == HK_INNOVATION_DRUG_FEAR_SOURCE_KEY
    )
    assert hk_pair["target_symbol"] == "513120.SH"
    assert hk_pair["fear_symbol"] == "931250.CSI"


def test_a_stock_backtest_params_accept_hk_innovation_drug_pair():
    """回测参数模型（复用执行引擎）要放行 513120.SH + 港股自算贪恐。"""
    params = legacy.SOXLFearRunParams(
        symbol="513120.SH",
        fear_source=HK_INNOVATION_DRUG_FEAR_SOURCE_KEY,
        params=legacy.SOXLFearStrategyParams(),
    )
    assert params.symbol == "513120.SH"
    assert params.fear_source == HK_INNOVATION_DRUG_FEAR_SOURCE_KEY
