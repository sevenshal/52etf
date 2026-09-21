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


def test_incremental_sync_does_not_prune_long_history(monkeypatch):
    """增量同步不清理：手动全量回补的长历史要保留，与个股分钟线策略一致。"""
    from src.core.services import sw_minute_data as module

    class FakeConnection:
        def execute(self, sql, params=None):
            class Result:
                def __init__(self, rows):
                    self.rows = rows
                def fetchall(self):
                    return self.rows
                def fetchone(self):
                    return self.rows[0]
            if "a_stock_sw_industry" in sql:
                return Result([("801080.SI",)])
            if "a_stock_market_daily" in sql:
                return Result([(date(2026, 9, 17),), (date(2026, 9, 18),)])
            return Result([(date(2026, 9, 17),)])          # 库里最新一天
        def close(self):
            pass

    pruned = []
    monkeypatch.setattr(module, "connect_duckdb", lambda *args, **kwargs: FakeConnection())
    monkeypatch.setattr(module, "fetch_batch", lambda service, codes, start, end: pd.DataFrame())
    monkeypatch.setattr(module, "prune_sw_minutes", lambda keep_from: pruned.append(keep_from) or 0)

    module.sync_sw_minute_bars(32, full=False, service=object())
    assert pruned == []                                   # 增量：不清理

    module.sync_sw_minute_bars(32, full=True, service=object())
    assert pruned == [date(2026, 9, 17)]                  # 全量：清理到窗口起点


def test_sw_minute_task_runner_incremental_by_default_and_full_when_manual(monkeypatch):
    """定时执行走增量（滚动窗口）；手动执行由 API 带 full=True，按参数回补 N 个交易日。"""
    from src.core.services import sw_minute_data as module
    from src.core.services.chan_minute_data import ROLLING_TRADING_DAYS
    from src.robot.scheduled_tasks import _run_sw_minute_sync

    calls = []
    monkeypatch.setattr(module, "sync_sw_minute_bars", lambda days, full=False: calls.append((days, full)) or {
        "saved_rows": 10, "requests": 1, "mode": "full" if full else "incremental", "errors": []})

    _run_sw_minute_sync(trading_days=60)
    _run_sw_minute_sync(full=True, trading_days=60)
    assert calls == [(ROLLING_TRADING_DAYS, False), (60, True)]
