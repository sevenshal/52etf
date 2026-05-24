from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .market import MarketService


EXTERNAL_TRADING_MARKET_A_STOCK = "A_STOCK"
EXTERNAL_TRADING_MARKET_US_STOCK = "US_STOCK"
ALLOWED_EXTERNAL_TRADING_MARKET_TYPES = {
    EXTERNAL_TRADING_MARKET_A_STOCK,
    EXTERNAL_TRADING_MARKET_US_STOCK,
}

CHINA_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("US/Eastern")
A_SHARE_OPEN = dtime(9, 30)
A_SHARE_MORNING_CLOSE = dtime(11, 30)
A_SHARE_AFTERNOON_OPEN = dtime(13, 0)
A_SHARE_CLOSE = dtime(15, 0)
US_MARKET_OPEN = dtime(9, 30)

_china_trading_day_cache = {}


def normalize_external_trading_market_type(
    value: Optional[str],
    default: str = EXTERNAL_TRADING_MARKET_A_STOCK,
) -> str:
    text = str(value or "").strip().upper()
    compact = text.replace("-", "_").replace(" ", "_")
    if compact in {"US", "USA", "US_STOCK", "USSTOCK", "U_STOCK", "AMERICA", "AMERICAN", "美股"}:
        return EXTERNAL_TRADING_MARKET_US_STOCK
    if compact in {"A", "CN", "CHINA", "A_STOCK", "ASTOCK", "A_SHARE", "A股", "ASHARE"}:
        return EXTERNAL_TRADING_MARKET_A_STOCK
    if default in ALLOWED_EXTERNAL_TRADING_MARKET_TYPES:
        return default
    return EXTERNAL_TRADING_MARKET_A_STOCK


def external_trading_market_label(market_type: Optional[str]) -> str:
    normalized = normalize_external_trading_market_type(market_type)
    if normalized == EXTERNAL_TRADING_MARKET_US_STOCK:
        return "美股"
    return "A股"


def external_trading_market_timezone(market_type: Optional[str]) -> ZoneInfo:
    normalized = normalize_external_trading_market_type(market_type)
    if normalized == EXTERNAL_TRADING_MARKET_US_STOCK:
        return US_TZ
    return CHINA_TZ


def _is_china_trading_day(check_date: date) -> bool:
    if check_date in _china_trading_day_cache:
        return _china_trading_day_cache[check_date]
    if check_date.weekday() >= 5:
        _china_trading_day_cache[check_date] = False
        return False
    try:
        from .tushare import TushareService

        calendar = TushareService.get_instance().get_trade_calendar_frame(check_date, check_date)
        if not calendar.empty:
            row = calendar.iloc[0]
            is_open = int(row.get("is_open") or 0) == 1
            _china_trading_day_cache[check_date] = is_open
            return is_open
    except Exception:
        pass
    _china_trading_day_cache[check_date] = True
    return True


def _is_us_trading_day(check_date: date) -> bool:
    return check_date.weekday() < 5 and not MarketService.is_us_market_holiday(check_date)


def _market_now(market_type: Optional[str], now: Optional[datetime] = None) -> datetime:
    timezone = external_trading_market_timezone(market_type)
    if now is None:
        return datetime.now(timezone)
    if now.tzinfo:
        return now.astimezone(timezone)
    return now.replace(tzinfo=timezone)


def is_external_trading_market_open(market_type: Optional[str], now: Optional[datetime] = None) -> bool:
    normalized = normalize_external_trading_market_type(market_type)
    current = _market_now(normalized, now)
    if normalized == EXTERNAL_TRADING_MARKET_US_STOCK:
        if not _is_us_trading_day(current.date()):
            return False
        return US_MARKET_OPEN <= current.time() <= MarketService.get_us_market_close_time(current.date())

    if not _is_china_trading_day(current.date()):
        return False
    current_time = current.time()
    return (
        A_SHARE_OPEN <= current_time <= A_SHARE_MORNING_CLOSE
        or A_SHARE_AFTERNOON_OPEN <= current_time <= A_SHARE_CLOSE
    )


def next_external_trading_time(market_type: Optional[str], now: Optional[datetime] = None) -> datetime:
    normalized = normalize_external_trading_market_type(market_type)
    current = _market_now(normalized, now)
    if normalized == EXTERNAL_TRADING_MARKET_US_STOCK:
        if _is_us_trading_day(current.date()) and current.time() < US_MARKET_OPEN:
            return datetime.combine(current.date(), US_MARKET_OPEN, tzinfo=US_TZ)
        next_day = current.date() + timedelta(days=1)
        while not _is_us_trading_day(next_day):
            next_day += timedelta(days=1)
        return datetime.combine(next_day, US_MARKET_OPEN, tzinfo=US_TZ)

    if _is_china_trading_day(current.date()):
        current_time = current.time()
        if current_time < A_SHARE_OPEN:
            return current.replace(hour=A_SHARE_OPEN.hour, minute=A_SHARE_OPEN.minute, second=0, microsecond=0)
        if A_SHARE_MORNING_CLOSE < current_time < A_SHARE_AFTERNOON_OPEN:
            return current.replace(
                hour=A_SHARE_AFTERNOON_OPEN.hour,
                minute=A_SHARE_AFTERNOON_OPEN.minute,
                second=0,
                microsecond=0,
            )
    next_day = current.date() + timedelta(days=1)
    while not _is_china_trading_day(next_day):
        next_day += timedelta(days=1)
    return datetime.combine(next_day, A_SHARE_OPEN, tzinfo=CHINA_TZ)


def next_external_trading_day_open(market_type: Optional[str], now: Optional[datetime] = None) -> datetime:
    normalized = normalize_external_trading_market_type(market_type)
    current = _market_now(normalized, now)
    next_day = current.date() + timedelta(days=1)
    if normalized == EXTERNAL_TRADING_MARKET_US_STOCK:
        while not _is_us_trading_day(next_day):
            next_day += timedelta(days=1)
        return datetime.combine(next_day, US_MARKET_OPEN, tzinfo=US_TZ)

    while not _is_china_trading_day(next_day):
        next_day += timedelta(days=1)
    return datetime.combine(next_day, A_SHARE_OPEN, tzinfo=CHINA_TZ)
