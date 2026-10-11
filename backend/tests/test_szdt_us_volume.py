from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.core.services.szdt_us_volume import _historical_cumulative_volumes


NEW_YORK = ZoneInfo("America/New_York")


def test_historical_baseline_uses_same_time_cumulative_regular_session_volume():
    candles = [
        {"timestamp": datetime(2026, 9, 30, 9, 29, tzinfo=NEW_YORK), "volume": 99},
        {"timestamp": datetime(2026, 9, 30, 9, 30, tzinfo=NEW_YORK), "volume": 10},
        {"timestamp": datetime(2026, 9, 30, 10, 30, tzinfo=NEW_YORK), "volume": 20},
        {"timestamp": datetime(2026, 9, 30, 15, 55, tzinfo=NEW_YORK), "volume": 30},
        {"timestamp": datetime(2026, 9, 30, 16, 1, tzinfo=NEW_YORK), "volume": 88},
        {"timestamp": datetime(2026, 10, 1, 9, 30, tzinfo=NEW_YORK), "volume": 77},
    ]

    result = _historical_cumulative_volumes(candles, before_date=date(2026, 10, 1))

    assert result[(date(2026, 9, 30), "10:30")] == 30
    assert result[(date(2026, 9, 30), "15:55")] == 60
