import logging
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Set

logger = logging.getLogger(__name__)

class MarketService:
    """美股市场日历服务"""
    
    @staticmethod
    def get_eastern_now() -> datetime:
        return datetime.now(ZoneInfo('US/Eastern'))

    @staticmethod
    def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
        """获取某月的第几个星期几 (0=Monday)"""
        d = date(year, month, 1)
        count = 0
        while True:
            if d.weekday() == weekday:
                count += 1
                if count == n:
                    return d
            d += timedelta(days=1)

    @staticmethod
    def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
        """获取某月的最后一个星期几"""
        if month == 12:
            d = date(year, 12, 31)
        else:
            d = date(year, month + 1, 1) - timedelta(days=1)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d

    @staticmethod
    def observed_holiday(d: date) -> date:
        """法定假日顺延逻辑：周六提前到周五，周日延后到周一"""
        if d.weekday() == 5: # Saturday
            return d - timedelta(days=1)
        if d.weekday() == 6: # Sunday
            return d + timedelta(days=1)
        return d

    @staticmethod
    def get_easter_date(year: int) -> date:
        """计算复活节日期 (Anonymous Algorithm)"""
        a = year % 19
        b = year // 100
        c = year % 100
        d0 = (19 * a + b - b // 4 - ((b + 8) // 25) + 15) % 30
        e = (32 + 2 * (b % 4) + 2 * (c // 4) - d0 - (c % 4) - (a // 29)) % 7
        f = d0 + e + 114
        m = f // 31
        day = (f % 31) + 1
        return date(year, m, day)

    @classmethod
    def is_us_market_holiday(cls, d: date) -> bool:
        """判断是否为美股休市日"""
        y = d.year
        holidays: Set[date] = {
            cls.observed_holiday(date(y, 1, 1)),   # New Year's Day
            cls.nth_weekday_of_month(y, 1, 0, 3),  # Martin Luther King Jr. Day
            cls.nth_weekday_of_month(y, 2, 0, 3),  # Washington's Birthday
            cls.get_easter_date(y) - timedelta(days=2), # Good Friday
            cls.last_weekday_of_month(y, 5, 0),    # Memorial Day
            cls.observed_holiday(date(y, 6, 19)),  # Juneteenth
            cls.observed_holiday(date(y, 7, 4)),   # Independence Day
            cls.nth_weekday_of_month(y, 9, 0, 1),  # Labor Day
            cls.nth_weekday_of_month(y, 11, 3, 4), # Thanksgiving Day
            cls.observed_holiday(date(y, 12, 25)), # Christmas Day
        }
        return d in holidays

    @classmethod
    def get_us_market_close_time(cls, d: date) -> dtime:
        """获取美股权益市场收盘时间 (通常为 16:00，感恩节后一天及平安夜为 13:00)"""
        y = d.year
        thanksgiving = cls.nth_weekday_of_month(y, 11, 3, 4)
        # Black Friday
        if d == thanksgiving + timedelta(days=1):
            return dtime(13, 0)
        # Christmas Eve
        if d.month == 12 and d.day == 24 and d.weekday() < 5:
            return dtime(13, 0)
        return dtime(16, 0)

    @classmethod
    def is_us_market_open(cls) -> bool:
        """判断当前美股是否处于交易时段 (9:30 - 16:00 ET, 排除假日和周末)"""
        now = cls.get_eastern_now()
        if now.weekday() >= 5:
            return False
        if cls.is_us_market_holiday(now.date()):
            return False
            
        start = dtime(9, 30)
        end = cls.get_us_market_close_time(now.date())
        return start <= now.time() <= end

    @classmethod
    def is_market_closing_soon(cls, seconds_before_close: int = 10) -> bool:
        """判断是否接近美股收盘"""
        now = cls.get_eastern_now()
        if now.weekday() >= 5 or cls.is_us_market_holiday(now.date()):
            return False
            
        close_time = cls.get_us_market_close_time(now.date())
        target_datetime = datetime.combine(now.date(), close_time, tzinfo=ZoneInfo('US/Eastern'))
        trigger_time = target_datetime - timedelta(seconds=seconds_before_close)
        
        # 如果当前时间在触发时间后 10 秒内，认为触发
        return trigger_time <= now < (trigger_time + timedelta(seconds=10))