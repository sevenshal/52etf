import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.market_alerts import (
    LABEL_ACTIVE,
    LABEL_AVOID,
    LABEL_STRONG,
    LABEL_WATCH,
    StockStructure,
    alert_score,
    classify,
    evaluate_snapshot,
    in_scan_window,
    summarize_hits,
    td_counts,
)


def _structure(**kwargs):
    base = dict(
        ts_code="000001.SZ", name="平安银行", industry="银行",
        td_up_prev=2, td_down_prev=0,
        close_ref=[9.0, 9.2, 9.4, 9.6, 10.0], open_ref4=9.3,
        listed_days=300, is_st=False,
    )
    base.update(kwargs)
    return StockStructure(**base)


def test_in_scan_window_skips_lunch_and_outside_hours():
    assert in_scan_window(datetime(2026, 9, 18, 9, 40)) is True
    assert in_scan_window(datetime(2026, 9, 18, 9, 30)) is False      # 集合竞价后、09:35 前不扫
    assert in_scan_window(datetime(2026, 9, 18, 12, 0)) is False      # 午休
    assert in_scan_window(datetime(2026, 9, 18, 15, 30)) is False


def test_td_counts_tracks_up_down_runs_and_last_low9():
    # 先连续走低制造「低9」，再连续 3 根高于各自 4 根之前的收盘
    closes = [20 - i for i in range(14)]                      # 连续下跌 → 低计数累积到 9 以上
    closes += [closes[-4] + 1, closes[-3] + 1, closes[-2] + 1]  # 逐根高于 4 根前 → 高计数 3
    up, down, since_low9 = td_counts(closes)
    assert up == 3 and down == 0
    assert since_low9 == 3          # 低9 完成在 3 根 K 线之前


def test_classify_active_and_strong():
    structure = _structure(td_up_prev=1)          # 加上当日 → 高计数 2
    common = dict(price=10.5, pre_close=10.0, day_open=10.1, cum_amount=1.0e8, structure=structure)
    assert classify(volume_ratio=1.6, **common) == LABEL_ACTIVE
    assert classify(volume_ratio=2.5, **common) == LABEL_ACTIVE        # 高计数 2 不够强势

    strong_structure = _structure(td_up_prev=2)   # 高计数 3，落在甜点区
    assert classify(volume_ratio=2.5, price=10.5, pre_close=10.0, day_open=10.1,
                    cum_amount=1.0e8, structure=strong_structure) == LABEL_STRONG


def test_classify_rejects_thin_volume_and_amount():
    structure = _structure(td_up_prev=1)
    common = dict(price=10.5, pre_close=10.0, day_open=10.1, structure=structure)
    assert classify(cum_amount=1.0e8, volume_ratio=1.2, **common) is None   # 量比不够
    assert classify(cum_amount=0.5e8, volume_ratio=2.0, **common) is None   # 成交额不够
    # 基准缺失（新股/停牌）时不打多头标签，避免把"无基准"当成放量
    assert classify(cum_amount=1.0e8, volume_ratio=None, **common) is None


def test_classify_watch_and_avoid():
    watch = _structure(td_up_prev=0, td_down_prev=1, close_ref=[11.0, 10.8, 10.5, 10.2, 10.0])
    assert classify(price=9.8, pre_close=10.0, day_open=10.0, cum_amount=1e8,
                    volume_ratio=1.0, structure=watch) == LABEL_WATCH

    avoid = _structure(td_up_prev=0, td_down_prev=3, close_ref=[11.0, 10.8, 10.5, 10.2, 10.0])
    assert classify(price=9.8, pre_close=10.0, day_open=10.0, cum_amount=1e8,
                    volume_ratio=1.0, structure=avoid) == LABEL_AVOID


def test_classify_skips_st_and_new_listings():
    st = _structure(name="ST康美", td_up_prev=2)   # is_st 由名称自动派生
    assert classify(price=10.5, pre_close=10.0, day_open=10.1, cum_amount=1e8,
                    volume_ratio=2.0, structure=st) is None
    new = _structure(listed_days=20, td_up_prev=2)
    assert classify(price=10.5, pre_close=10.0, day_open=10.1, cum_amount=1e8,
                    volume_ratio=2.0, structure=new) is None


def test_alert_score_peaks_at_sweet_spot_count():
    assert alert_score(5.0, 2.0, 3) > alert_score(5.0, 2.0, 1)
    assert alert_score(5.0, 2.0, 4) > alert_score(5.0, 2.0, 7)
    assert 0 <= alert_score(-1.0, 0.5, 0) <= 100


def test_evaluate_snapshot_uses_same_time_baseline_and_speed():
    quotes = pd.DataFrame([{
        "ts_code": "000001.SZ", "close": 10.5, "pre_close": 10.0,
        "open": 10.1, "vol": 2_000_000, "amount": 1.2e8,
    }])
    structures = {"000001.SZ": _structure(td_up_prev=2)}
    baseline = {"000001.SZ": 800_000.0}               # 量比 2.5

    rows = evaluate_snapshot(quotes, structures, baseline, "10:00", previous_pct={"000001.SZ": 3.0})

    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == LABEL_STRONG
    assert row["volume_ratio"] == 2.5
    assert row["speed5"] == 2.0                        # 当前 +5% 减 5 轮前 +3%
    assert row["td_up"] == 3

    # 没有基准（停牌/新股）时不打多头标签
    assert evaluate_snapshot(quotes, structures, {}, "10:01") == []


def test_summarize_hits_groups_by_label_and_time():
    rows = [
        {"label": LABEL_STRONG, "hit_time": "09:40", "cum_pct": 2.0, "name": "A", "industry": "电子"},
        {"label": LABEL_STRONG, "hit_time": "10:10", "cum_pct": -1.0, "name": "B", "industry": "电子"},
        {"label": LABEL_ACTIVE, "hit_time": "13:10", "cum_pct": 0.5, "name": "C", "industry": "银行"},
        {"label": LABEL_WATCH, "hit_time": "14:40", "cum_pct": None, "name": "D", "industry": "银行"},
    ]
    summary = summarize_hits(rows)

    assert summary["total"] == 4 and summary["scored"] == 3
    assert summary["by_label"][LABEL_STRONG] == {"count": 2, "avg": 0.5, "win_rate": 50.0}
    assert summary["best"]["name"] == "A" and summary["worst"]["name"] == "B"
    first_bucket = next(b for b in summary["time_buckets"] if b["label"] == "09:35~10:00")
    assert first_bucket["count"] == 1 and first_bucket["avg"] == 2.0
    assert summary["industries"][0]["industry"] == "电子"


def test_build_volume_baseline_averages_requested_trading_days():
    """基准取「前 N 个有数据的交易日」，且不含当日。"""
    import duckdb

    connection = duckdb.connect()
    connection.execute("CREATE TABLE a_stock_minute_bar (ts_code VARCHAR, trade_time TIMESTAMP, vol DOUBLE)")
    rows = []
    for day, vol in (("2026-09-14", 100), ("2026-09-15", 200), ("2026-09-16", 300), ("2026-09-17", 400)):
        rows.append(("000001.SZ", f"{day} 09:31:00", vol))
        rows.append(("000001.SZ", f"{day} 09:32:00", vol))     # 累计到 09:32 为 2×vol
    rows.append(("000001.SZ", "2026-09-18 09:31:00", 9999))    # 当日数据必须被排除
    connection.executemany("INSERT INTO a_stock_minute_bar VALUES (?, ?, ?)", rows)

    from src.core.services.market_alerts import build_volume_baseline

    from src.core.services.market_alerts import baseline_at

    two_days = build_volume_baseline(connection, date(2026, 9, 18), days=2)
    assert baseline_at(two_days, "09:31")["000001.SZ"] == 350     # (300+400)/2
    assert baseline_at(two_days, "09:32")["000001.SZ"] == 700     # 累计两根

    four_days = build_volume_baseline(connection, date(2026, 9, 18), days=4)
    assert baseline_at(four_days, "09:31")["000001.SZ"] == 250    # (100+200+300+400)/4
    assert baseline_at(four_days, "14:00") == {}                  # 没有数据的时刻返回空
    connection.close()
