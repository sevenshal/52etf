import math

from src.core.services.a_stock_fear_etf_backtest_engine import abnormal_volume, target_mapping


def test_abnormal_volume_uses_strict_mean_plus_one_std():
    assert abnormal_volume(121, 100, 20) == (True, 1.05)
    assert abnormal_volume(120, 100, 20) == (False, 1.0)


def test_zero_standard_deviation_allows_only_volume_above_mean():
    signal, score = abnormal_volume(101, 100, 0)
    assert signal is True
    assert math.isinf(score)


def test_mapping_excludes_csi500():
    mapping = target_mapping({"000905.SH"})
    assert "000905.SH" not in mapping
    assert mapping["000985.SH"] == "510300.SH"
