from datetime import date


DEFAULT_START_DATE = date(2020, 1, 1)
BENCHMARK_INDEXES = [
    {"ts_code": "000300.SH", "name": "沪深300"},
    {"ts_code": "000905.SH", "name": "中证500"},
]
A_STOCK_FACTOR_INDEX_POOLS = [
    {"index_code": "000510.SH", "name": "中证A500"},
    {"index_code": "000905.SH", "name": "中证500"},
    {"index_code": "000699.SH", "name": "科创200"},
    {"index_code": "399006.SZ", "name": "创业板指"},
    {"index_code": "399998.SZ", "name": "中证煤炭"},
    {"index_code": "000015.SH", "name": "上证红利"},
]
A_STOCK_INDEX_FEAR_GREED_TARGETS = [
    {
        "symbol": "000510.SH",
        "ticker": "中证A500",
        "label": "中证A500",
        "index_name": "中证A500",
        "option_underlyings": ["OP588000.SH", "OP588080.SH", "OP159915.SZ", "OP510500.SH", "OP159922.SZ"],
    },
    {
        "symbol": "000905.SH",
        "ticker": "中证500",
        "label": "中证500",
        "index_name": "中证500",
        "option_underlyings": ["OP510500.SH", "OP159922.SZ"],
    },
    {
        "symbol": "000699.SH",
        "ticker": "科创200",
        "label": "科创200",
        "index_name": "上证科创板200",
        "option_underlyings": ["OP588000.SH", "OP588080.SH"],
    },
    {
        "symbol": "399006.SZ",
        "ticker": "创业板指",
        "label": "创业板",
        "index_name": "创业板指",
        "option_underlyings": ["OP159915.SZ"],
    },
    {
        "symbol": "399998.SZ",
        "ticker": "中证煤炭",
        "label": "煤炭",
        "index_name": "中证煤炭",
        "option_underlyings": ["OP588000.SH", "OP588080.SH", "OP159915.SZ", "OP510500.SH", "OP159922.SZ"],
    },
    {
        "symbol": "000015.SH",
        "ticker": "上证红利",
        "label": "红利",
        "index_name": "上证红利",
        "option_underlyings": ["OP588000.SH", "OP588080.SH", "OP159915.SZ", "OP510500.SH", "OP159922.SZ"],
    },
]
A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS = (
    "563360.SH",
    "510500.SH",
    "588230.SH",
    "159915.SZ",
    "515220.SH",
    "510880.SH",
)
A_STOCK_ETF_DAILY_NAMES = {
    "563360.SH": "A500ETF",
    "510500.SH": "中证500ETF",
    "588230.SH": "科创200ETF",
    "159915.SZ": "创业板ETF",
    "515220.SH": "煤炭ETF",
    "510880.SH": "红利ETF",
    "513100.SH": "纳指ETF",
    "518880.SH": "黄金ETF",
    "510300.SH": "沪深300ETF",
}
A_STOCK_ETF_DAILY_SYMBOLS = tuple(
    dict.fromkeys(
        [
            *A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS,
            # W20 风险调整动量虚拟盘默认标的和基准。
            "513100.SH",
            "518880.SH",
            "510300.SH",
        ]
    )
)
A_STOCK_FEAR_SAFE_HAVEN_INDEXES = [
    {"ts_code": "H11006.CSI", "name": "中证国债"},
]
CHINABOND_CREDIT_CURVES = [
    {
        "curve_id": "2c9081880fa9d507010fb8505b393fe7",
        "curve_name": "中债中短期票据收益率曲线(AAA)",
        "category": "中短期票据",
        "rating": "AAA",
        "pair_key": "medium_note",
    },
    {
        "curve_id": "2c9081e50a2f9606010a30acdae40176",
        "curve_name": "中债中短期票据收益率曲线(AA)",
        "category": "中短期票据",
        "rating": "AA",
        "pair_key": "medium_note",
    },
    {
        "curve_id": "2c9081e50a2f9606010a309f4af50111",
        "curve_name": "中债企业债收益率曲线(AAA)",
        "category": "企业债",
        "rating": "AAA",
        "pair_key": "enterprise_bond",
    },
    {
        "curve_id": "2c90818812b319130112c279222836c3",
        "curve_name": "中债企业债收益率曲线(AA)",
        "category": "企业债",
        "rating": "AA",
        "pair_key": "enterprise_bond",
    },
    {
        "curve_id": "2c9081e91b55cc84011be3c53b710598",
        "curve_name": "中债城投债收益率曲线(AAA)",
        "category": "城投债",
        "rating": "AAA",
        "pair_key": "urban_investment_bond",
    },
    {
        "curve_id": "2c9081e91b55cc84011c07e9991e15c9",
        "curve_name": "中债城投债收益率曲线(AA)",
        "category": "城投债",
        "rating": "AA",
        "pair_key": "urban_investment_bond",
    },
]
MIN_MARKET_DAILY_ROWS = 3500
MAX_MARKET_DAILY_OHL_ZERO_PCT = 1.0
RAW_FETCH_LOOKBACK_DAYS = 180
