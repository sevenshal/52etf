from datetime import date


DEFAULT_START_DATE = date(2020, 1, 1)
BENCHMARK_INDEXES = [
    {"ts_code": "000300.SH", "name": "沪深300"},
    {"ts_code": "000905.SH", "name": "中证500"},
]
MIN_MARKET_DAILY_ROWS = 3500
MAX_MARKET_DAILY_OHL_ZERO_PCT = 1.0
RAW_FETCH_LOOKBACK_DAYS = 180
