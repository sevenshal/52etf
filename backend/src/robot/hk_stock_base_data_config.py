from datetime import date


HK_STOCK_DEFAULT_START_DATE = date(2020, 1, 1)

HK_INDEX_FEAR_GREED_TARGETS = [
    {
        "symbol": "HSI.HK",
        "ticker": "恒生指数",
        "label": "恒生指数",
        "index_code": "HSI",
        "tushare_index_code": "HSI",
    },
    {
        "symbol": "HSTECH.HK",
        "ticker": "恒生科技",
        "label": "恒生科技",
        "index_code": "HSTECH",
        "tushare_index_code": "HKTECH",
    },
    {
        # 中证指数发布的港股主题指数：点位取 Tushare index_daily，
        # 权重取 Tushare index_weight 月度快照（恒生检讨公告里没有它）。
        "symbol": "931250.CSI",
        "ticker": "香港创新药",
        "label": "中证香港创新药",
        "index_code": "931250",
        "csi_index_code": "931250.CSI",
    },
]

# Tushare index_weight 从 2022-09 起才有 931250 的权重。
HK_CSI_INDEX_WEIGHT_DEFAULT_START_DATE = date(2022, 1, 1)

HK_INDEX_FEAR_GREED_TARGET_BY_SYMBOL = {
    item["symbol"]: item for item in HK_INDEX_FEAR_GREED_TARGETS
}

# Review announcement dates. The downloader probes both legacy midnight and
# current 17:45 timestamp URL forms. One release normally contains all three
# benchmark index appendices.
HANG_SENG_REVIEW_RELEASE_DATES = (
    "20200221",
    "20200515",
    "20200814",
    "20201113",
    "20210226",
    "20210521",
    "20210820",
    "20211119",
    "20220218",
    "20220520",
    "20220819",
    "20221118",
    "20230224",
    "20230512",
    "20230818",
    "20231117",
    "20240216",
    "20240517",
    "20240816",
    "20241122",
    "20250221",
    "20250516",
    "20250822",
    "20251121",
    "20260213",
    "20260522",
)
