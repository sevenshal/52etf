from datetime import date


DEFAULT_START_DATE = date(2020, 1, 1)
BENCHMARK_INDEXES = [
    {"ts_code": "000300.SH", "name": "沪深300"},
    {"ts_code": "000905.SH", "name": "中证500"},
]
A_STOCK_ETF_FEAR_GREED_TARGETS = [
    {
        "symbol": "563360.SH",
        "ticker": "A500ETF",
        "label": "A500ETF",
        "index_code": "000510.SH",
        "index_name": "中证A500",
        "option_underlyings": ["OP588000.SH", "OP588080.SH", "OP159915.SZ", "OP510500.SH", "OP159922.SZ"],
    },
    {
        "symbol": "510500.SH",
        "ticker": "中证500ETF",
        "label": "中证500",
        "index_code": "000905.SH",
        "index_name": "中证500",
        "option_underlyings": ["OP510500.SH", "OP159922.SZ"],
    },
    {
        "symbol": "588230.SH",
        "ticker": "科创200ETF",
        "label": "科创200",
        "index_code": "000699.SH",
        "index_name": "上证科创板200",
        "option_underlyings": ["OP588000.SH", "OP588080.SH"],
    },
    {
        "symbol": "159915.SZ",
        "ticker": "创业板ETF",
        "label": "创业板",
        "index_code": "399006.SZ",
        "index_name": "创业板指",
        "option_underlyings": ["OP159915.SZ"],
    },
    {
        "symbol": "515220.SH",
        "ticker": "煤炭ETF",
        "label": "煤炭",
        "index_code": "399998.SZ",
        "index_name": "中证煤炭",
        "option_underlyings": ["OP588000.SH", "OP588080.SH", "OP159915.SZ", "OP510500.SH", "OP159922.SZ"],
    },
    {
        "symbol": "510880.SH",
        "ticker": "红利ETF",
        "label": "红利",
        "index_code": "000015.SH",
        "index_name": "上证红利",
        "option_underlyings": ["OP588000.SH", "OP588080.SH", "OP159915.SZ", "OP510500.SH", "OP159922.SZ"],
    },
]
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
