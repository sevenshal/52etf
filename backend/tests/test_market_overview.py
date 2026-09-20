import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.market_overview import (
    MarketOverviewDataError,
    append_today_estimate,
    build_realtime_distribution,
    market_session,
    build_daily_amount,
    build_distribution,
    build_index_overview,
    keep_latest_session,
)


def _minute_frame(rows):
    frame = pd.DataFrame(rows, columns=["trade_time", "close"])
    frame["trade_time"] = pd.to_datetime(frame["trade_time"])
    return frame


def test_build_index_overview_builds_pct_spark_from_pre_close():
    quotes = pd.DataFrame(
        [{"ts_code": "000001.SH", "close": 101.0, "pre_close": 100.0}],
        columns=["ts_code", "close", "pre_close"],
    )
    minutes = {"000001.SH": _minute_frame([("2026-09-18 09:31:00", 100.5), ("2026-09-18 09:30:00", 100.0)])}

    items = {item["key"]: item for item in build_index_overview(quotes, minutes)["items"]}

    assert items["sh"]["price"] == 101.0
    assert items["sh"]["pct"] == 1.0
    assert items["sh"]["spark"] == [0.0, 0.5]  # 按时间排序，涨跌幅相对前收
    assert items["sh"]["spark_slots"] == 241
    # 没有行情的指数仍然占位，不会让整条概览条塌掉
    assert items["cyb"]["price"] is None and items["cyb"]["spark"] == []


def test_keep_latest_session_drops_earlier_days():
    frame = _minute_frame([("2026-09-17 15:00:00", 10.0), ("2026-09-18 09:30:00", 11.0)])
    kept = keep_latest_session(frame)
    assert list(kept["close"]) == [11.0]


def test_build_distribution_counts_limits_separately():
    rows = [
        (10.0, 2),      # 涨停
        (10.0, 3),      # 一字涨停
        (8.0, 1),
        (5.0, 1),
        (3.0, 1),       # 边界值归入 0~3%
        (0.0, 0),       # 平盘归入 0~-3%
        (-4.0, 4),
        (-9.0, 4),
        (-10.0, 5),     # 跌停
        (None, 1),      # 脏数据跳过
    ]
    result = build_distribution(rows, date(2026, 9, 18))

    assert result["names"] == ["涨停", ">7%", "3~7%", "0~3%", "0~-3%", "-3~-7%", "<-7%", "跌停"]
    assert result["counts"] == [2, 1, 1, 1, 1, 1, 1, 1]
    assert result["total"] == 9
    # 平盘不计入涨跌家数，但仍留在 0~-3% 档里（与 kpan 口径一致）
    assert result["up_count"] == 5 and result["down_count"] == 3 and result["flat_count"] == 1
    assert result["date"] == "2026-09-18"


def test_build_daily_amount_sums_two_indexes_and_marks_up_days():
    sh = pd.DataFrame(
        [("20260917", 1.0e6, -0.5), ("20260918", 1.2e6, 1.5), ("20260916", 1.1e6, 0.2)],
        columns=["trade_date", "amount", "pct_chg"],
    )
    sz = pd.DataFrame(
        [("20260917", 1.0e6, 0.0), ("20260918", 0.8e6, 0.0)],
        columns=["trade_date", "amount", "pct_chg"],
    )

    result = build_daily_amount(sh, sz, days=120)

    # amount 单位千元 → 亿元；09-16 深市缺数据，不参与
    assert result["dates"] == ["2026-09-17", "2026-09-18"]
    assert result["amounts"] == [20.0, 20.0]
    assert result["up"] == [0, 1]
    assert result["avg"] == 20.0 and result["latest"] == 20.0


def test_build_daily_amount_requires_both_indexes():
    frame = pd.DataFrame([("20260918", 1.0e6, 0.1)], columns=["trade_date", "amount", "pct_chg"])
    with pytest.raises(MarketOverviewDataError):
        build_daily_amount(frame, pd.DataFrame(), days=60)


def test_build_realtime_distribution_uses_limit_prices():
    quotes = pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "close": 11.0, "pre_close": 10.0},   # 涨停价 11.0 → 涨停
            {"ts_code": "600000.SH", "close": 9.0, "pre_close": 10.0},    # 跌停价 9.0 → 跌停
            {"ts_code": "300001.SZ", "close": 10.5, "pre_close": 10.0},   # +5% → 3~7%
            {"ts_code": "920001.BJ", "close": 10.0, "pre_close": 10.0},   # 平盘
        ]
    )
    limits = {
        "000001.SZ": (11.0, 9.0),
        "600000.SH": (11.0, 9.0),
        "300001.SZ": (12.0, 8.0),
        "920001.BJ": (13.0, 7.0),
    }

    result = build_realtime_distribution(quotes, limits, date(2026, 9, 18))

    assert result["mode"] == "realtime"
    assert result["limit_up"] == 1 and result["limit_down"] == 1
    assert result["counts"][2] == 1        # 3~7%
    assert result["flat_count"] == 1 and result["total"] == 4


def test_market_session_marks_pre_open_and_close():
    assert market_session(datetime(2026, 9, 18, 9, 0), True)["is_pre_open"] is True
    assert market_session(datetime(2026, 9, 18, 11, 0), True)["is_closed"] is False
    assert market_session(datetime(2026, 9, 18, 15, 1), True)["is_closed"] is True
    # 非交易日一律按已收盘处理，直接读快照
    assert market_session(datetime(2026, 9, 20, 11, 0), False)["is_closed"] is True


def test_append_today_estimate_adds_estimated_bar():
    base = {"dates": ["2026-09-17", "2026-09-18"], "amounts": [18231.3, 20771.0], "up": [0, 1]}
    intraday = {
        "target_date": "2026-09-21",
        "target_total": 9000.0,
        "estimated_total": 17500.0,
        "estimate_days": ["2026-09-14", "2026-09-18"],
        "diff": 120.0,
        "last_time": "11:30",
    }

    result = append_today_estimate(dict(base), intraday)

    assert result["dates"][-1] == "2026-09-21"
    assert result["amounts"][-1] == 17500.0
    assert result["today"]["is_estimated"] is True and result["today"]["actual"] == 9000.0
    # 当日已经落进 index_daily 时不再补柱
    unchanged = append_today_estimate(dict(base), {"target_date": "2026-09-18", "target_total": 1.0})
    assert unchanged["dates"] == base["dates"]
