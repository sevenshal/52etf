import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.sw_minute_data import (
    BARS_PER_DAY,
    CODES_PER_BATCH,
    DAYS_PER_BATCH,
    ROW_LIMIT,
    fetch_batch,
    plan_batches,
)


def test_plan_batches_never_exceeds_row_limit():
    codes = [f"8010{i:02d}.SI" for i in range(10)]
    dates = [date(2026, 9, day) for day in range(1, 13)]

    batches = plan_batches(codes, dates)

    assert CODES_PER_BATCH * DAYS_PER_BATCH * BARS_PER_DAY < ROW_LIMIT
    for batch_codes, start, end in batches:
        days = sum(1 for day in dates if start <= day <= end)
        assert len(batch_codes) * days * BARS_PER_DAY <= ROW_LIMIT
    # 每个 (指数, 日期) 恰好被覆盖一次
    covered = {(code, day) for batch_codes, start, end in batches
               for code in batch_codes for day in dates if start <= day <= end}
    assert covered == {(code, day) for code in codes for day in dates}


class _FakeService:
    """模拟 sw_mins 的静默截断：超过 5000 行只返回前 5000 行。"""

    def __init__(self):
        self.calls = []

    def get_sw_minute_frame(self, codes, start_time, end_time):
        self.calls.append((tuple(codes), start_time.date(), end_time.date()))
        days = pd.bdate_range(start_time.date(), end_time.date())
        rows = [
            {"ts_code": code, "trade_time": pd.Timestamp(day) + pd.Timedelta(minutes=570 + minute), "close": 1.0}
            for code in codes for day in days for minute in range(BARS_PER_DAY)
        ]
        return pd.DataFrame(rows[:ROW_LIMIT])


def test_fetch_batch_splits_when_result_hits_the_silent_truncation_limit():
    service = _FakeService()
    codes = ["801010.SI", "801030.SI", "801040.SI", "801050.SI", "801080.SI", "801110.SI"]

    # 6 指数 × 5 天 = 7230 行，一次拉会被截到 5000，必须自动拆开
    frame = fetch_batch(service, codes, date(2026, 9, 14), date(2026, 9, 18))

    assert len(frame) == 6 * 5 * BARS_PER_DAY          # 拿全了，没丢数据
    assert len(service.calls) > 1                      # 确实拆批了
    assert frame.groupby("ts_code").size().eq(5 * BARS_PER_DAY).all()


def test_fetch_batch_single_batch_when_under_limit():
    service = _FakeService()
    frame = fetch_batch(service, ["801080.SI"], date(2026, 9, 14), date(2026, 9, 18))
    assert len(frame) == 5 * BARS_PER_DAY and len(service.calls) == 1
