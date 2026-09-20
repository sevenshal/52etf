import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.market_overview import (
    MarketOverviewDataError,
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
    assert result["up_count"] == 5 and result["down_count"] == 4
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
