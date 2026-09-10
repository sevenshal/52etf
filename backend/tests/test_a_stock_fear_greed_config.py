import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.a_stock_fear_greed_clone_service import ALL_A_STOCK_OPTIONS
from src.robot.a_stock_base_data_config import (
    A_STOCK_OPTION_PROXY_UNDERLYINGS,
    ADDITIONAL_A_STOCK_INDEX_FEAR_GREED_TARGETS,
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
    # 中证全指走全市场口径，不列举标的
    assert target["option_underlyings"] == ["*"]


def test_a_stock_fear_greed_targets_include_hs300_and_semiconductor_segments():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}
    expected = {
        "000300.SH": ("沪深300", "沪深300指数", "510300.SH", "沪深300ETF"),
        "950162.CSI": ("科创芯片设计", "上证科创板芯片设计主题指数", "588780.SH", "芯片设计ETF"),
        "931743.CSI": ("半导体材料设备", "中证半导体材料设备主题指数", "159516.SZ", "半导体设备ETF"),
    }

    for symbol, (ticker, index_name, proxy_etf, proxy_name) in expected.items():
        target = targets_by_symbol[symbol]
        assert target["ticker"] == ticker
        assert target["index_name"] == index_name
        assert target["proxy_etf"] == proxy_etf
        assert symbol in pools
        assert proxy_etf in A_STOCK_ETF_DAILY_SYMBOLS
        assert A_STOCK_ETF_DAILY_NAMES[proxy_etf] == proxy_name


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
    # 北证50 和所有期权指数零交集，候选照常声明，运行时权重全为 0、put/call 分项缺失。
    assert target["option_underlyings"] == A_STOCK_OPTION_PROXY_UNDERLYINGS
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


def test_a_stock_fear_greed_targets_include_star_100():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}

    target = targets_by_symbol["000698.SH"]
    assert target["ticker"] == "科创100"
    assert target["index_name"] == "上证科创板100指数"
    assert "000698.SH" in pools
    assert target["option_underlyings"] == A_STOCK_OPTION_PROXY_UNDERLYINGS
    assert target["proxy_etf"] == "588220.SH"
    assert target["proxy_etf"] in A_STOCK_ETF_DAILY_SYMBOLS
    assert A_STOCK_ETF_DAILY_NAMES[target["proxy_etf"]] == "科创100ETF"


def test_a_stock_fear_greed_targets_include_priority_sector_indexes():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}
    expected = {
        "399975.SZ": ("证券公司", "中证全指证券公司指数", "512880.SH", "证券ETF"),
        "H30184.CSI": ("半导体", "中证全指半导体产品与设备指数", "512480.SH", "半导体ETF"),
        "980022.SZ": ("机器人产业", "国证机器人产业指数", "159530.SZ", "机器人ETF易方达"),
        "399997.SZ": ("中证白酒", "中证白酒指数", "161725.SZ", "中证白酒LOF"),
        "399989.SZ": ("中证医疗", "中证医疗指数", "512170.SH", "医疗ETF"),
        "000819.SH": ("有色金属", "中证申万有色金属指数", "512400.SH", "有色ETF"),
    }

    for symbol, (ticker, index_name, proxy_etf, proxy_name) in expected.items():
        target = targets_by_symbol[symbol]
        assert target["ticker"] == ticker
        assert target["index_name"] == index_name
        assert target["option_underlyings"] == A_STOCK_OPTION_PROXY_UNDERLYINGS
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
        assert target["option_underlyings"] == A_STOCK_OPTION_PROXY_UNDERLYINGS
        assert target["proxy_etf"] == proxy_etf
        assert symbol in pools
        assert proxy_etf in A_STOCK_ETF_DAILY_SYMBOLS
        assert A_STOCK_ETF_DAILY_NAMES[proxy_etf] == proxy_name


def test_a_stock_fear_greed_targets_include_dividend_low_volatility():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}

    target = targets_by_symbol["H30269.CSI"]
    assert target["ticker"] == "红利低波"
    assert target["index_name"] == "中证红利低波动指数"
    assert target["proxy_etf"] == "512890.SH"
    assert "H30269.CSI" in pools
    assert target["proxy_etf"] in A_STOCK_ETF_DAILY_SYMBOLS
    assert A_STOCK_ETF_DAILY_NAMES[target["proxy_etf"]] == "红利低波ETF"


def test_a_stock_fear_greed_targets_include_csi_1000_and_2000():
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    pools = {str(item["index_code"]).upper() for item in A_STOCK_FACTOR_INDEX_POOLS}
    expected = {
        "000852.SH": ("中证1000", "中证1000指数", "512100.SH", "中证1000ETF"),
        "932000.CSI": ("中证2000", "中证2000指数", "563300.SH", "中证2000ETF"),
    }

    for symbol, (ticker, index_name, proxy_etf, proxy_name) in expected.items():
        target = targets_by_symbol[symbol]
        assert target["ticker"] == ticker
        assert target["index_name"] == index_name
        assert target["proxy_etf"] == proxy_etf
        assert symbol in pools
        assert proxy_etf in A_STOCK_ETF_DAILY_SYMBOLS
        assert A_STOCK_ETF_DAILY_NAMES[proxy_etf] == proxy_name

    # 中证1000 用中金所自己的股指期权(MO)；中证2000 没有任何期权，走统一代理候选集。
    assert targets_by_symbol["000852.SH"]["option_underlyings"] == ["OP000852.SH"]
    assert targets_by_symbol["932000.CSI"]["option_underlyings"] == A_STOCK_OPTION_PROXY_UNDERLYINGS


def test_indexes_with_their_own_options_use_them():
    """有自己期权的指数必须用自己的期权，不能再借别的标的当代理。

    全市场 12 个期权标的里只有 5 条指数有自己的期权：上证50、沪深300、中证500、
    科创50、创业板指、中证1000。其余指数（中证A500、中证全指、科创100/200、
    北证50、中证2000、微盘400 和各行业指数）确实没有，只能继续借代理。
    """
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    own_options = {
        # 中金所股指期权 + 对应的场内 ETF 期权
        "000016.SH": ["OP000016.SH", "OP510050.SH"],
        "000300.SH": ["OP000300.SH", "OP510300.SH", "OP159919.SZ"],
        "000852.SH": ["OP000852.SH"],
        # 只有 ETF 期权的
        "000905.SH": ["OP510500.SH", "OP159922.SZ"],
        "000688.SH": ["OP588000.SH", "OP588080.SH"],
        "399006.SZ": ["OP159915.SZ"],
    }
    for symbol, expected in own_options.items():
        assert targets_by_symbol[symbol]["option_underlyings"] == expected, symbol


def test_indexes_without_their_own_options_share_one_proxy_candidate_set():
    """没有自己期权的指数统一用同一组候选，具体权重在运行时按成分重叠度算。

    以前是给每条指数手挑标的，挑错了就往 put/call 分项里灌噪音（中证煤炭、上证红利
    曾经挂着科创50+创业板+中证500）。现在只声明候选，重叠为 0 的自动权重归零，
    北证50 这种零交集的会直接缺失该分项。
    """
    own_options = {"000016.SH", "000300.SH", "000852.SH", "000905.SH", "000688.SH", "399006.SZ"}
    whole_market = {"000985.SH"}
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        symbol = str(item["symbol"]).upper()
        if symbol in own_options or symbol in whole_market:
            continue
        assert item["option_underlyings"] == A_STOCK_OPTION_PROXY_UNDERLYINGS, symbol


def test_proxy_candidate_set_avoids_nested_and_unsynced_indexes():
    """候选集只放互斥的规模三档 + 两个板块口径。

    上证50 是沪深300 的子集，放进来纯粹重复计数；深证100 的成分权重没有同步，
    算不出重叠度。
    """
    from src.core.services.a_stock_fear_greed_clone_service import (
        OPTION_UNDERLYING_TRACKED_INDEX,
    )

    tracked = {OPTION_UNDERLYING_TRACKED_INDEX[code] for code in A_STOCK_OPTION_PROXY_UNDERLYINGS}
    assert tracked == {"000300.SH", "000905.SH", "000852.SH", "000688.SH", "399006.SZ"}
    assert "000016.SH" not in tracked
    assert "399330.SZ" not in tracked


def test_indexes_with_their_own_options_use_them():
    """有自己期权的指数必须用自己的期权，不能再借别的标的当代理。

    全市场 12 个期权标的里只有 5 条指数有自己的期权：上证50、沪深300、中证500、
    科创50、创业板指、中证1000。其余指数（中证A500、中证全指、科创100/200、
    北证50、中证2000、微盘400 和各行业指数）确实没有，只能继续借代理。
    """
    targets_by_symbol = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    own_options = {
        # 中金所股指期权 + 对应的场内 ETF 期权
        "000016.SH": ["OP000016.SH", "OP510050.SH"],
        "000300.SH": ["OP000300.SH", "OP510300.SH", "OP159919.SZ"],
        "000852.SH": ["OP000852.SH"],
        # 只有 ETF 期权的
        "000905.SH": ["OP510500.SH", "OP159922.SZ"],
        "000688.SH": ["OP588000.SH", "OP588080.SH"],
        "399006.SZ": ["OP159915.SZ"],
    }
    for symbol, expected in own_options.items():
        assert targets_by_symbol[symbol]["option_underlyings"] == expected, symbol


def test_a_stock_fear_greed_proxy_etfs_stay_aligned_with_targets():
    proxy_symbols = [str(item).upper() for item in A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS]
    target_proxy_pairs = [
        (str(item["symbol"]).upper(), str(item["proxy_etf"]).upper())
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
        if item.get("proxy_etf")
    ]

    assert proxy_symbols == [proxy for _, proxy in target_proxy_pairs]
    assert target_proxy_pairs == [
        ("000300.SH", "510300.SH"),
        ("000016.SH", "510050.SH"),
        ("000510.SH", "563360.SH"),
        ("000905.SH", "510500.SH"),
        ("000852.SH", "512100.SH"),
        ("932000.CSI", "563300.SH"),
        ("000985.SH", "510300.SH"),
        ("000680.SH", "589000.SH"),
        ("000688.SH", "588000.SH"),
        ("000698.SH", "588220.SH"),
        ("000699.SH", "588230.SH"),
        ("399006.SZ", "159915.SZ"),
        ("399975.SZ", "512880.SH"),
        ("H30184.CSI", "512480.SH"),
        ("950162.CSI", "588780.SH"),
        ("931743.CSI", "159516.SZ"),
        ("980022.SZ", "159530.SZ"),
        ("399997.SZ", "161725.SZ"),
        ("399989.SZ", "512170.SH"),
        ("000819.SH", "512400.SH"),
        ("399967.SZ", "512660.SH"),
        ("930997.CSI", "515030.SH"),
        ("000932.SH", "159928.SZ"),
        ("399986.SZ", "512800.SH"),
        ("399998.SZ", "515220.SH"),
        ("000015.SH", "510880.SH"),
        ("H30269.CSI", "512890.SH"),
    ] + [
        (str(item["symbol"]).upper(), str(item["proxy_etf"]).upper())
        for item in ADDITIONAL_A_STOCK_INDEX_FEAR_GREED_TARGETS
    ]


def test_additional_industry_targets_are_complete_and_unique():
    symbols = [str(item["symbol"]).upper() for item in A_STOCK_INDEX_FEAR_GREED_TARGETS]
    additional_symbols = {
        str(item["symbol"]).upper()
        for item in ADDITIONAL_A_STOCK_INDEX_FEAR_GREED_TARGETS
    }

    assert len(ADDITIONAL_A_STOCK_INDEX_FEAR_GREED_TARGETS) == 36
    assert len(symbols) == len(set(symbols))
    assert additional_symbols <= set(symbols)
    for item in ADDITIONAL_A_STOCK_INDEX_FEAR_GREED_TARGETS:
        assert item["proxy_etf"] in A_STOCK_ETF_DAILY_SYMBOLS
        assert A_STOCK_ETF_DAILY_NAMES[item["proxy_etf"]]


def test_option_sync_covers_cffex_index_options():
    """中金所股指期权（IO/MO/HO）的 opt_code 与 ETF 期权同形，同步范围要带上 CFFEX。"""
    from src.core.services.tushare import OPTION_SUPPORTED_EXCHANGES
    from src.robot.a_stock_base_data_sync import A_STOCK_OPTION_DAILY_SYNC_EXCHANGES

    assert "CFFEX" in A_STOCK_OPTION_DAILY_SYNC_EXCHANGES
    assert set(A_STOCK_OPTION_DAILY_SYNC_EXCHANGES) <= OPTION_SUPPORTED_EXCHANGES
