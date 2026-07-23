import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.robot.a_stock_base_data_config import (
    A_STOCK_ETF_DAILY_NAMES,
    A_STOCK_ETF_DAILY_SYMBOLS,
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


def test_a_stock_fear_greed_targets_include_bse_50():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}

    target = targets_by_symbol["899050.BJ"]
    assert target["ticker"] == "北证50"
    assert target["index_name"] == "北证50"
    assert "899050.BJ" in pools
    assert target["option_underlyings"] == []
    assert target.get("proxy_etf") is None


def test_a_stock_fear_greed_targets_include_star_50():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}

    target = targets_by_symbol["000688.SH"]
    assert target["ticker"] == "科创50"
    assert target["index_name"] == "上证科创板50成份指数"
    assert "000688.SH" in pools
    assert target["option_underlyings"] == ["OP588000.SH", "OP588080.SH"]
    assert target["proxy_etf"] == "588000.SH"


def test_a_stock_fear_greed_targets_include_priority_sector_indexes():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}
    expected = {
        "399975.SZ": ("证券公司", "中证全指证券公司指数", "512880.SH", "证券ETF"),
        "H30184.CSI": ("半导体", "中证全指半导体产品与设备指数", "512480.SH", "半导体ETF"),
        "399989.SZ": ("中证医疗", "中证医疗指数", "512170.SH", "医疗ETF"),
        "000819.SH": ("有色金属", "中证申万有色金属指数", "512400.SH", "有色ETF"),
    }

    for symbol, (ticker, index_name, proxy_etf, proxy_name) in expected.items():
        target = targets_by_symbol[symbol]
        assert target["ticker"] == ticker
        assert target["index_name"] == index_name
        assert target["option_underlyings"] == []
        assert target["proxy_etf"] == proxy_etf
        assert symbol in pools
        assert proxy_etf in A_STOCK_ETF_DAILY_SYMBOLS
        assert A_STOCK_ETF_DAILY_NAMES[proxy_etf] == proxy_name


def test_a_stock_fear_greed_targets_include_second_tier_sector_indexes():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}
    expected = {
        "399967.SZ": ("中证军工", "中证军工指数", "512660.SH", "军工ETF"),
        "930997.CSI": ("新能源车", "中证新能源汽车产业指数", "515030.SH", "新能源车ETF"),
        "000932.SH": ("主要消费", "中证主要消费指数", "159928.SZ", "消费ETF"),
        "399986.SZ": ("中证银行", "中证银行指数", "512800.SH", "银行ETF"),
    }

    for symbol, (ticker, index_name, proxy_etf, proxy_name) in expected.items():
        target = targets_by_symbol[symbol]
        assert target["ticker"] == ticker
        assert target["index_name"] == index_name
        assert target["option_underlyings"] == []
        assert target["proxy_etf"] == proxy_etf
        assert symbol in pools
        assert proxy_etf in A_STOCK_ETF_DAILY_SYMBOLS
        assert A_STOCK_ETF_DAILY_NAMES[proxy_etf] == proxy_name


def test_a_stock_fear_greed_proxy_etfs_stay_aligned_with_targets():
    proxy_symbols = [str(item).upper() for item in A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS]
    target_proxy_pairs = [
        (str(item["symbol"]).upper(), str(item["proxy_etf"]).upper())
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
        if item.get("proxy_etf")
    ]

    assert proxy_symbols == [proxy for _, proxy in target_proxy_pairs]
    assert target_proxy_pairs == [
        ("000510.SH", "563360.SH"),
        ("000905.SH", "510500.SH"),
        ("000985.SH", "510300.SH"),
        ("000688.SH", "588000.SH"),
        ("000699.SH", "588230.SH"),
        ("399006.SZ", "159915.SZ"),
        ("399975.SZ", "512880.SH"),
        ("H30184.CSI", "512480.SH"),
        ("399989.SZ", "512170.SH"),
        ("000819.SH", "512400.SH"),
        ("399967.SZ", "512660.SH"),
        ("930997.CSI", "515030.SH"),
        ("000932.SH", "159928.SZ"),
        ("399986.SZ", "512800.SH"),
        ("399998.SZ", "515220.SH"),
        ("000015.SH", "510880.SH"),
    ]
