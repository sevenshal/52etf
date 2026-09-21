import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.database import MarketAlertEvent, MarketAlertHit, get_db_ctx
from src.core.services.market_alerts import LABEL_ACTIVE
from src.core.services.market_alerts_replay import (
    build_minute_panels,
    replay_day,
    replay_minutes,
    snapshot_at,
)


def _minute_labels():
    labels = []
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60 + 1, 15 * 60)):
        labels += [f"{m // 60:02d}:{m % 60:02d}" for m in range(start, end + 1)]
    return labels   # 241 根


def test_replay_minutes_follows_live_scan_window():
    minutes = replay_minutes(_minute_labels())
    assert minutes[0] == "09:35"            # 盘中 09:35 才开始扫
    assert "11:30" in minutes and "11:31" not in minutes
    assert "13:01" in minutes and minutes[-1] == "15:00"
    assert not any("09:30" <= m < "09:35" for m in minutes)


def test_snapshot_at_rebuilds_cumulative_state_like_live_quotes():
    frame = pd.DataFrame([
        ("A.SZ", "09:30", 10.0, 10.1, 100.0, 1000.0),
        ("A.SZ", "09:31", 10.1, 10.2, 200.0, 2000.0),
        ("A.SZ", "09:33", 10.2, 10.3, 300.0, 3000.0),     # 09:32 缺一根（停牌分钟）
        ("B.SZ", "09:33", 5.0, 5.1, 50.0, 500.0),          # 09:33 才开出第一笔
    ], columns=["ts_code", "t", "open", "close", "vol", "amount"])
    panels = build_minute_panels(frame)
    pre = {"A.SZ": 10.0, "B.SZ": 5.0}

    at_0932 = snapshot_at(panels, pre, "09:32").set_index("ts_code")
    assert list(at_0932.index) == ["A.SZ"]                  # B 此刻还没有价格，不参与
    assert at_0932.loc["A.SZ", "close"] == 10.2             # 缺的分钟沿用上一价
    assert at_0932.loc["A.SZ", "vol"] == 300.0 and at_0932.loc["A.SZ", "amount"] == 3000.0
    assert at_0932.loc["A.SZ", "open"] == 10.0              # 今开 = 首根开盘

    at_0933 = snapshot_at(panels, pre, "09:33").set_index("ts_code")
    assert at_0933.loc["A.SZ", "vol"] == 600.0 and at_0933.loc["B.SZ", "close"] == 5.1


@pytest.fixture
def synthetic_market():
    """一只持续上涨的股票：前 20 天每分钟 100 股，回放日 10:00 起放量到每分钟 1000 股。

    同时段量比 = 当日累计 ÷ 前 20 日同分钟累计均值：
    10:00 时 (3000+1000)/3100 = 1.29 < 1.3，10:01 时 5000/3200 = 1.56 ≥ 1.3 → 首次命中应为 10:01 活跃。
    九转高计数在长期上涨里远大于 4，所以只会到活跃、不会到强势。
    """
    con = duckdb.connect()
    days = [date(2001, 1, 1) + timedelta(days=i) for i in range(100)]
    days = [d for d in days if d.weekday() < 5][:75]
    target = days[-1]

    con.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE)")
    con.executemany("INSERT INTO a_stock_market_daily VALUES ('000001.SZ', ?, ?, ?, ?)", [
        (d, 5.0 + 0.05 * i, 5.0 + 0.05 * i + 0.02, 5.0 + 0.05 * (i - 1) + 0.02) for i, d in enumerate(days)
    ])
    con.execute("CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, list_date DATE)")
    con.execute("INSERT INTO a_stock_basic VALUES ('000001.SZ', '测试股份', DATE '1999-01-01')")
    con.execute("CREATE TABLE a_stock_sw_member (ts_code VARCHAR, l1_name VARCHAR, l2_name VARCHAR, l3_name VARCHAR)")
    con.execute("INSERT INTO a_stock_sw_member VALUES ('000001.SZ', '银行', '股份制银行Ⅱ', '股份制银行Ⅲ')")
    # 申万部分留空：回放应自动关闭行业信号而不是报错
    con.execute("CREATE TABLE a_stock_sw_industry (index_code VARCHAR, industry_name VARCHAR, industry_code VARCHAR, level VARCHAR, parent_code VARCHAR)")
    con.execute("CREATE TABLE a_stock_sw_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE, open DOUBLE)")
    con.execute("CREATE TABLE a_stock_sw_minute_bar (ts_code VARCHAR, trade_time TIMESTAMP, open DOUBLE, close DOUBLE, vol DOUBLE, amount DOUBLE)")

    con.execute("CREATE TABLE a_stock_minute_bar (ts_code VARCHAR, trade_time TIMESTAMP, open DOUBLE, close DOUBLE, vol DOUBLE, amount DOUBLE)")
    rows = []
    labels = _minute_labels()
    for d in days[-21:]:
        is_target = d == target
        day_open = 5.0 + 0.05 * (len(days) - 1) if is_target else 5.0
        for index, label in enumerate(labels):
            ts = datetime.combine(d, datetime.strptime(label, "%H:%M").time())
            vol = 1000.0 if (is_target and label >= "10:00") else 100.0
            price = day_open + 0.001 * index if is_target else 5.0
            rows.append(("000001.SZ", ts, day_open, price, vol, 1e7))
    con.executemany("INSERT INTO a_stock_minute_bar VALUES (?, ?, ?, ?, ?, ?)", rows)

    # 覆盖检查要求当天至少 1000 只股票有分钟线，测试里放宽
    import src.core.services.market_alerts_replay as replay_module
    original = replay_module.MIN_STOCKS_WITH_MINUTES
    replay_module.MIN_STOCKS_WITH_MINUTES = 1
    yield con, target
    replay_module.MIN_STOCKS_WITH_MINUTES = original
    with get_db_ctx() as db:
        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == target).delete()
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == target).delete()
    con.close()


def test_replay_day_reproduces_first_hit_minute(synthetic_market):
    con, target = synthetic_market

    result = replay_day(con, target, baseline_days=20)

    assert result["status"] == "done" and result["sw"] == "off"
    with get_db_ctx() as db:
        row = db.query(MarketAlertHit).filter_by(trade_date=target, ts_code="000001.SZ").one()
        assert row.label == LABEL_ACTIVE
        assert row.hit_time == "10:01"                       # 正好是量比越过 1.3 的那一分钟
        assert row.volume_ratio == pytest.approx(1.56, abs=0.01)
        assert row.industry_l1 == "银行"
        assert row.cum_pct is None                            # 命中后涨幅不再落库，读取时现算
        events = db.query(MarketAlertEvent).filter_by(trade_date=target, ts_code="000001.SZ").all()
        assert [(e.event_time, e.label) for e in events] == [("10:01", LABEL_ACTIVE)]


def test_replay_day_is_idempotent_and_respects_overwrite(synthetic_market):
    con, target = synthetic_market

    replay_day(con, target, baseline_days=20)
    again = replay_day(con, target, baseline_days=20, overwrite=True)
    assert again["status"] == "done" and again["records"] == 1 and again["events"] == 1   # 重跑不重复

    skipped = replay_day(con, target, baseline_days=20, overwrite=False)
    assert skipped["status"] == "skipped" and "未勾选覆盖" in skipped["reason"]


def test_replay_day_skips_when_baseline_history_is_short(synthetic_market):
    con, target = synthetic_market

    result = replay_day(con, target, baseline_days=30)     # 库里只有前 20 天分钟线

    assert result["status"] == "skipped"
    assert "只有 20 个交易日" in result["reason"] and "需要 30 个" in result["reason"]
