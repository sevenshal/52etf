import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.robot.a_stock_base_data_config import (
    A_STOCK_FACTOR_INDEX_POOLS,
    A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS,
    A_STOCK_INDEX_FEAR_GREED_TARGETS,
)


def test_a_stock_fear_greed_targets_include_csi_all_share():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}

    target = targets_by_symbol["000985.SH"]
    assert target["ticker"] == "中证全指"
    assert target["index_name"] == "中证全指"
    assert "000985.SH" in pools
    assert target["option_underlyings"] == [
        "OP510300.SH",
        "OP159919.SZ",
        "OP510500.SH",
        "OP159922.SZ",
        "OP159915.SZ",
    ]


def test_a_stock_fear_greed_proxy_etfs_stay_aligned_with_targets():
    target_symbols = [str(item["symbol"]).upper() for item in A_STOCK_INDEX_FEAR_GREED_TARGETS]
    proxy_symbols = [str(item).upper() for item in A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS]

    assert len(proxy_symbols) == len(target_symbols)
    assert list(zip(target_symbols, proxy_symbols)) == [
        ("000510.SH", "563360.SH"),
        ("000905.SH", "510500.SH"),
        ("000985.SH", "510300.SH"),
        ("000699.SH", "588230.SH"),
        ("399006.SZ", "159915.SZ"),
        ("399998.SZ", "515220.SH"),
        ("000015.SH", "510880.SH"),
    ]
