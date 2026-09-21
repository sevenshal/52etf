import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _no_live_prices(monkeypatch):
    """读取命中列表时会取最新价（真实环境调 tushare），测试里默认桩成"没有行情"。"""
    from src.core.services import market_alerts as module

    monkeypatch.setattr(module, "latest_price_map", lambda: {})

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
        close_ref=[9.0, 9.2, 9.4, 9.6, 10.0], open_ref4=9.3, open_ref1=9.7,
        vol_max_prev=3_000_000.0, listed_days=300, is_st=False,
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
    # 昨日收阳且连涨、今日收阳上涨、3 日涨幅 (10.5-9.4)/9.4≈11.7% 落在 5%~15%
    structure = _structure(td_up_prev=1)          # 加上当日 → 高计数 2，满足 COND1
    common = dict(price=10.5, pre_close=10.0, day_open=10.1, cum_amount=1.0e8, structure=structure)
    assert classify(volume_ratio=1.6, cum_volume=1_000_000, **common) == LABEL_ACTIVE
    # 强势：累计量 ≥ 前 8 日最大日量（与量比无关）
    assert classify(volume_ratio=1.6, cum_volume=3_000_000, **common) == LABEL_STRONG
    assert classify(volume_ratio=5.0, cum_volume=2_999_999, **common) == LABEL_ACTIVE
    # 没有前期最大量（新数据）时不判强势
    no_max = _structure(td_up_prev=1, vol_max_prev=None)
    assert classify(volume_ratio=1.6, cum_volume=9e9, price=10.5, pre_close=10.0, day_open=10.1,
                    cum_amount=1.0e8, structure=no_max) == LABEL_ACTIVE


def test_classify_active_needs_structure_and_gain_band():
    common = dict(pre_close=10.0, cum_amount=1.0e8, volume_ratio=2.0)
    # 当日收阴（价 < 开盘）→ COND1/COND2 都不成立
    assert classify(price=10.5, day_open=10.6, structure=_structure(), **common) is None
    # 3 日涨幅超过 15% → 不追
    assert classify(price=10.9, day_open=10.1, structure=_structure(close_ref=[9.0, 9.2, 9.4 * 0.9, 9.6, 10.0]),
                    **common) is None
    # 3 日涨幅不到 5% → 不算
    flat = _structure(close_ref=[10.2, 10.3, 10.2, 9.9, 10.0], open_ref4=10.0)
    assert classify(price=10.1, day_open=10.05, structure=flat, **common) is None
    # COND1 需要昨日收阳：昨日开盘高于昨收时，高计数 2 也不行；此时 COND2 也不满足（昨收高于 5 日前且高计数<2）
    yesterday_down = _structure(td_up_prev=0, open_ref1=10.2)
    assert classify(price=10.5, day_open=10.1, structure=yesterday_down, **common) is None


def test_classify_rejects_thin_volume_and_amount():
    structure = _structure(td_up_prev=1)
    common = dict(price=10.5, pre_close=10.0, day_open=10.1, structure=structure)
    assert classify(cum_amount=1.0e8, volume_ratio=1.2, **common) is None   # 量比不够（默认门槛 1.3）
    assert classify(cum_amount=0.5e8, volume_ratio=2.0, **common) is None   # 成交额不够
    # 基准缺失（新股/停牌）时不打多头标签，避免把"无基准"当成放量
    assert classify(cum_amount=1.0e8, volume_ratio=None, **common) is None


def test_classify_watch_and_avoid():
    # 九转低计数 2 → 观望；最近一根高计数≥1 的收盘 10.5，现价 9.8 回撤 6.7% 不到 7%
    watch = _structure(td_up_prev=0, td_down_prev=1, close_ref=[11.0, 10.8, 10.5, 10.2, 10.0], last_up_close=10.5)
    assert classify(price=9.8, pre_close=10.0, day_open=10.0, cum_amount=1e8,
                    volume_ratio=1.0, structure=watch) == LABEL_WATCH

    # 参照收盘 10.6：9.8 < 10.6×0.93=9.858，回撤超过 7% → 规避（与低计数多少、当日涨跌无关）
    avoid = _structure(td_up_prev=0, td_down_prev=1, close_ref=[11.0, 10.8, 10.5, 10.2, 10.0], last_up_close=10.6)
    assert classify(price=9.8, pre_close=10.0, day_open=10.0, cum_amount=1e8,
                    volume_ratio=1.0, structure=avoid) == LABEL_AVOID

    # 找不到参照（窗口内没有高计数）时只到观望
    no_ref = _structure(td_up_prev=0, td_down_prev=1, close_ref=[11.0, 10.8, 10.5, 10.2, 10.0], last_up_close=None)
    assert classify(price=9.0, pre_close=10.0, day_open=10.0, cum_amount=1e8,
                    volume_ratio=1.0, structure=no_ref) == LABEL_WATCH


def test_last_up_close_picks_most_recent_bar_above_ref4():
    from src.core.services.market_alerts import last_up_close

    # 第 5 根 12 > 第 1 根 10（高计数≥1），之后一路低于 4 根前
    assert last_up_close([10, 11, 11.5, 11.8, 12, 11, 10.5, 10, 9]) == 12
    assert last_up_close([10, 9, 8, 7, 6, 5]) is None


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
        "open": 10.1, "vol": 3_000_000, "amount": 1.2e8,
    }])
    structures = {"000001.SZ": _structure(td_up_prev=2)}
    baseline = {"000001.SZ": 1_200_000.0}             # 量比 2.5；累计量 = 前 8 日最大量 → 强势

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
    # 晚于最后一根的时刻退到最后一根（收盘后看当天基准），早于第一根才返回空
    assert baseline_at(four_days, "14:00")["000001.SZ"] == 500.0
    assert baseline_at(four_days, "09:00") == {}
    connection.close()


def test_baseline_at_falls_back_to_nearest_earlier_minute():
    """午休、收盘后或任何非交易分钟都要退到最近一个有基准的分钟，否则多头标签会整片消失。"""
    import pandas as pd

    from src.core.services.market_alerts import baseline_at

    baseline = pd.DataFrame(
        [[100.0, 200.0, 300.0]],
        index=["000001.SZ"],
        columns=["09:30", "11:30", "13:01"],
    )

    assert baseline_at(baseline, "11:30")["000001.SZ"] == 200.0    # 命中当分钟
    assert baseline_at(baseline, "12:57")["000001.SZ"] == 200.0    # 午休 → 退到 11:30
    assert baseline_at(baseline, "15:20")["000001.SZ"] == 300.0    # 收盘后 → 退到最后一根
    assert baseline_at(baseline, "09:20") == {}                    # 开盘前没有基准
    assert baseline_at(pd.DataFrame(), "10:00") == {}


def test_limits_does_not_cache_empty_result(monkeypatch):
    """盘前 stk_limit 还没发布时会返回空，不能把空结果缓存一整天。"""
    from datetime import date as date_cls

    from src.core.services import market_alerts as module

    calls = {"n": 0}

    class FakeService:
        def get_a_stock_stk_limit_frame(self, trade_date):
            calls["n"] += 1
            if calls["n"] == 1:
                return pd.DataFrame()      # 盘前：空
            return pd.DataFrame([{"ts_code": "000001.SZ", "up_limit": 11.0, "down_limit": 9.0}])

    monkeypatch.setattr(module.TushareService, "get_instance", classmethod(lambda cls: FakeService()))
    scanner = module.AlertScanner()
    today = date_cls(2026, 9, 21)

    assert scanner.limits(today) == {}
    assert scanner.limits(today) == {} and calls["n"] == 1     # 重试间隔内不再打接口
    scanner._limits_retry_at = 0                               # 模拟过了重试间隔
    assert scanner.limits(today)["000001.SZ"] == (11.0, 9.0)   # 重试拿到了
    assert calls["n"] == 2
    assert scanner.limits(today)["000001.SZ"] == (11.0, 9.0)   # 拿到之后才缓存
    assert calls["n"] == 2


def test_prepare_does_not_mark_ready_when_baseline_missing(monkeypatch):
    """分钟库没同步好时基准为空，不能标记成已准备，否则一整天都没有多头标签。"""
    from datetime import date as date_cls

    from src.core.services import market_alerts as module

    monkeypatch.setattr(module, "build_structures", lambda connection, today: {"000001.SZ": object()})
    monkeypatch.setattr(module, "build_volume_baseline", lambda connection, today, days=20: pd.DataFrame())

    scanner = module.AlertScanner()
    scanner.prepare(date_cls(2026, 9, 21), connection=object())

    assert scanner._baseline_date is None      # 下次调用还会重试


def test_alert_hits_sw_industry_columns_upgrade_from_old_schema():
    """存量库：旧 market_alert_hits 没有申万三级列 → ensure_table_columns 自动补齐，且可重复执行。"""
    from datetime import date as date_cls

    from sqlalchemy import text

    from src.core.database import MarketAlertHit, engine, ensure_table_columns, get_db_ctx

    new_columns = ("industry_l1", "industry_l2", "industry_l3", "entity_type", "last_change_time", "change_count")
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(market_alert_hits)")).fetchall()}
        for column in new_columns:
            if column in existing:
                conn.execute(text(f"ALTER TABLE market_alert_hits DROP COLUMN {column}"))

    ensure_table_columns()
    ensure_table_columns()   # 幂等：重复执行不报错
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(market_alert_hits)")).fetchall()}
    assert set(new_columns) <= cols

    # 补列后 ORM 读写正常
    trade_date = date_cls(2000, 1, 3)
    with get_db_ctx() as db:
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == trade_date).delete()
        db.add(MarketAlertHit(
            trade_date=trade_date, ts_code="000001.SZ", hit_time="09:40", label="活跃",
            industry="银行", industry_l1="银行", industry_l2="股份制银行Ⅱ", industry_l3="股份制银行Ⅲ",
        ))
    with get_db_ctx() as db:
        row = db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == trade_date).one()
        assert (row.industry_l1, row.industry_l3) == ("银行", "股份制银行Ⅲ")
        db.delete(row)


def test_summarize_hits_groups_by_requested_sw_level():
    from src.core.services.market_alerts import summarize_hits

    rows = [
        {"label": LABEL_ACTIVE, "hit_time": "09:40", "cum_pct": 1.0, "name": "A",
         "industry_l1": "电子", "industry_l2": "半导体", "industry_l3": "数字芯片设计"},
        {"label": LABEL_ACTIVE, "hit_time": "09:50", "cum_pct": 3.0, "name": "B",
         "industry_l1": "电子", "industry_l2": "半导体", "industry_l3": "半导体设备"},
        {"label": LABEL_STRONG, "hit_time": "10:10", "cum_pct": 2.0, "name": "C",
         "industry_l1": "电子", "industry_l2": "光学光电子", "industry_l3": "面板"},
    ]

    l1 = summarize_hits(rows)            # 默认一级
    assert l1["industries"] == [{"industry": "电子", "count": 3, "avg": 2.0}]

    l2 = {item["industry"]: item["count"] for item in summarize_hits(rows, "l2")["industries"]}
    assert l2 == {"半导体": 2, "光学光电子": 1}

    l3 = {item["industry"] for item in summarize_hits(rows, "l3")["industries"]}
    assert l3 == {"数字芯片设计", "半导体设备", "面板"}


def test_build_structures_takes_industry_from_sw_members():
    """行业口径与行业关联一致：用申万三级，不用 stock_basic 的单级 industry。"""
    import duckdb
    from datetime import date as date_cls, timedelta

    from src.core.services.market_alerts import build_structures

    con = duckdb.connect()
    con.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE, open DOUBLE, vol DOUBLE)")
    con.execute("CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR, list_date DATE)")
    con.execute("CREATE TABLE a_stock_sw_member (ts_code VARCHAR, l1_name VARCHAR, l2_name VARCHAR, l3_name VARCHAR)")
    start = date_cls(2026, 8, 1)
    con.executemany("INSERT INTO a_stock_market_daily VALUES ('600519.SH', ?, ?, ?, ?)",
                    [(start + timedelta(days=i), 10.0 + i, 9.5 + i, 1000.0 + i) for i in range(15)])
    con.execute("INSERT INTO a_stock_basic VALUES ('600519.SH', '贵州茅台', '白酒', DATE '2001-08-27')")
    con.execute("INSERT INTO a_stock_sw_member VALUES ('600519.SH', '食品饮料', '白酒Ⅱ', '白酒Ⅲ')")

    structure = build_structures(con, date_cls(2026, 8, 20))["600519.SH"]

    assert (structure.industry_l1, structure.industry_l2, structure.industry_l3) == ("食品饮料", "白酒Ⅱ", "白酒Ⅲ")
    assert structure.industry == "食品饮料"      # 兼容字段 = 申万一级，不是 stock_basic 的「白酒」
    assert structure.open_ref1 == 9.5 + 14
    assert structure.vol_max_prev == (1000.0 + 14) * 100   # 日线 vol 单位手 → 股
    con.close()


def _hit(code, label, *, price=10.0, pct=1.0, entity="stock", name="平安银行", l1="银行", l2="股份制银行Ⅱ"):
    return {
        "ts_code": code, "entity_type": entity, "name": name,
        "industry": l1, "industry_l1": l1, "industry_l2": l2, "industry_l3": "",
        "label": label, "score": 50.0, "price": price, "pct": pct, "amount_yi": 1.0,
        "volume_ratio": 1.6, "speed5": 0.2, "td_up": 2, "td_down": 0, "days_since_low9": None,
    }


def test_write_hits_overwrites_latest_label_and_logs_every_change():
    """标签盘中变化要覆盖成最新，但每次变化都在流水里留一条；命中价始终是首次命中的。"""
    from datetime import date as date_cls

    from src.core.database import MarketAlertEvent, MarketAlertHit, get_db_ctx
    from src.core.services.market_alerts import AlertScanner

    today = date_cls(2000, 1, 4)
    with get_db_ctx() as db:
        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == today).delete()
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == today).delete()

    scanner = AlertScanner()
    scanner._write_hits(today, "09:40", [_hit("000001.SZ", LABEL_WATCH, price=10.0, pct=-1.0)])
    scanner._write_hits(today, "09:41", [_hit("000001.SZ", LABEL_WATCH, price=10.1)])      # 没变：不记
    scanner._write_hits(today, "10:05", [_hit("000001.SZ", LABEL_ACTIVE, price=10.5)])     # 观望→活跃
    scanner._write_hits(today, "10:30", [])                                                 # 此刻无信号：不清空

    with get_db_ctx() as db:
        row = db.query(MarketAlertHit).filter_by(trade_date=today, ts_code="000001.SZ").one()
        assert row.label == LABEL_ACTIVE                        # 最新标签
        assert row.hit_time == "09:40"                          # 首次命中时刻不变
        assert row.last_change_time == "10:05" and row.change_count == 1
        assert row.price == 10.0                                # 命中价以首次为准，事后打分不漂移
        events = db.query(MarketAlertEvent).filter_by(trade_date=today, ts_code="000001.SZ") \
            .order_by(MarketAlertEvent.id).all()
        assert [(e.event_time, e.prev_label, e.label) for e in events] == [
            ("09:40", None, LABEL_WATCH),
            ("10:05", LABEL_WATCH, LABEL_ACTIVE),
        ]
        for event in events:
            db.delete(event)
        db.delete(row)


def test_fetch_alerts_attaches_and_filters_by_industry_labels():
    from datetime import date as date_cls

    from src.core.database import MarketAlertEvent, MarketAlertHit, get_db_ctx
    from src.core.services.market_alerts import AlertScanner, fetch_alerts

    today = date_cls(2000, 1, 5)
    with get_db_ctx() as db:
        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == today).delete()
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == today).delete()
    AlertScanner()._write_hits(today, "09:40", [
        _hit("000001.SZ", LABEL_ACTIVE, l1="银行", l2="股份制银行Ⅱ"),
        _hit("600519.SH", LABEL_ACTIVE, name="贵州茅台", l1="食品饮料", l2="白酒Ⅱ"),
        _hit("801780.SI", LABEL_STRONG, entity="sw_l1", name="银行", l1="银行", l2=""),
        _hit("801783.SI", LABEL_WATCH, entity="sw_l2", name="股份制银行Ⅱ", l1="银行", l2="股份制银行Ⅱ"),
    ])

    result = fetch_alerts(today)
    by_code = {row["ts_code"]: row for row in result["rows"]}
    assert set(by_code) == {"000001.SZ", "600519.SH"}                 # 列表只放个股
    assert by_code["000001.SZ"]["l1_label"] == LABEL_STRONG
    assert by_code["000001.SZ"]["l2_label"] == LABEL_WATCH
    assert by_code["600519.SH"]["l1_label"] is None                   # 食品饮料当天没信号
    assert {row["name"] for row in result["sw_signals"]} == {"银行", "股份制银行Ⅱ"}

    # 组合过滤：个股活跃 + 一级强势
    assert [r["ts_code"] for r in fetch_alerts(today, label=LABEL_ACTIVE, l1_label=LABEL_STRONG)["rows"]] == ["000001.SZ"]
    # 一级无信号
    assert [r["ts_code"] for r in fetch_alerts(today, l1_label="none")["rows"]] == ["600519.SH"]
    # 二级观望
    assert [r["ts_code"] for r in fetch_alerts(today, l2_label=LABEL_WATCH)["rows"]] == ["000001.SZ"]

    with get_db_ctx() as db:
        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == today).delete()
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == today).delete()


def test_build_sw_structures_and_sw_baseline_share_stock_logic():
    import duckdb
    from datetime import date as date_cls, timedelta

    from src.core.services.market_alerts import baseline_at, build_sw_structures, build_volume_baseline

    con = duckdb.connect()
    con.execute("CREATE TABLE a_stock_sw_industry (index_code VARCHAR, industry_name VARCHAR, industry_code VARCHAR, level VARCHAR, parent_code VARCHAR)")
    con.executemany("INSERT INTO a_stock_sw_industry VALUES (?, ?, ?, ?, ?)", [
        ("801080.SI", "电子", "270000", "L1", "0"),
        ("801081.SI", "半导体", "270100", "L2", "270000"),
        ("850812.SI", "数字芯片设计", "270101", "L3", "270100"),      # 三级不参与
    ])
    con.execute("CREATE TABLE a_stock_sw_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE, open DOUBLE, vol DOUBLE)")
    start = date_cls(2026, 8, 1)
    for code in ("801080.SI", "801081.SI", "850812.SI"):
        con.executemany("INSERT INTO a_stock_sw_daily VALUES (?, ?, ?, ?, ?)",
                        [(code, start + timedelta(days=i), 100.0 + i, 100.0 + i, 50.0) for i in range(20)])

    structures = build_sw_structures(con, date_cls(2026, 9, 1))
    assert set(structures) == {"801080.SI", "801081.SI"}
    assert structures["801080.SI"].vol_max_prev == 50.0 * 10_000       # 申万日线 vol 单位万股 → 股
    assert structures["801080.SI"].entity_type == "sw_l1" and structures["801080.SI"].industry_l1 == "电子"
    semi = structures["801081.SI"]
    assert semi.entity_type == "sw_l2" and (semi.industry_l1, semi.industry_l2) == ("电子", "半导体")
    assert semi.td_up_prev > 0                      # 持续上涨，九转高计数累积

    con.execute("CREATE TABLE a_stock_sw_minute_bar (ts_code VARCHAR, trade_time TIMESTAMP, vol DOUBLE)")
    con.executemany("INSERT INTO a_stock_sw_minute_bar VALUES (?, ?, ?)", [
        ("801080.SI", "2026-08-31 09:31:00", 100.0), ("801080.SI", "2026-08-28 09:31:00", 300.0),
    ])
    baseline = build_volume_baseline(con, date_cls(2026, 9, 1), days=20, table="a_stock_sw_minute_bar")
    assert baseline_at(baseline, "09:31")["801080.SI"] == 200.0
    with pytest.raises(ValueError):
        build_volume_baseline(con, date_cls(2026, 9, 1), table="some_other_table")
    con.close()


def test_industry_label_matches():
    from src.core.services.market_alerts import industry_label_matches

    assert industry_label_matches(LABEL_STRONG, "") and industry_label_matches(None, None)
    assert industry_label_matches(LABEL_STRONG, LABEL_STRONG)
    assert not industry_label_matches(LABEL_ACTIVE, LABEL_STRONG)
    assert industry_label_matches(None, "none") and not industry_label_matches(LABEL_WATCH, "none")


@pytest.mark.parametrize("current, new, allowed", [
    # 强势可覆盖其余三个
    (LABEL_ACTIVE, LABEL_STRONG, True), (LABEL_WATCH, LABEL_STRONG, True), (LABEL_AVOID, LABEL_STRONG, True),
    # 活跃可覆盖观望与规避
    (LABEL_WATCH, LABEL_ACTIVE, True), (LABEL_AVOID, LABEL_ACTIVE, True),
    # 规避可覆盖观望
    (LABEL_WATCH, LABEL_AVOID, True),
    # 其余一律不能覆盖
    (LABEL_STRONG, LABEL_ACTIVE, False), (LABEL_STRONG, LABEL_WATCH, False), (LABEL_STRONG, LABEL_AVOID, False),
    (LABEL_ACTIVE, LABEL_WATCH, False), (LABEL_ACTIVE, LABEL_AVOID, False),
    (LABEL_AVOID, LABEL_WATCH, False),
    # 同一标签不算变更
    (LABEL_WATCH, LABEL_WATCH, False), (LABEL_STRONG, LABEL_STRONG, False),
])
def test_can_override_follows_signal_priority(current, new, allowed):
    from src.core.services.market_alerts import can_override

    assert can_override(current, new) is allowed


def test_write_hits_ignores_downgrades_and_resets_each_day():
    """当天只允许往更强的方向覆盖；往弱变不改标签也不记流水；隔日从头开始。"""
    from datetime import date as date_cls

    from src.core.database import MarketAlertEvent, MarketAlertHit, get_db_ctx
    from src.core.services.market_alerts import AlertScanner

    day1, day2 = date_cls(2000, 1, 6), date_cls(2000, 1, 7)
    with get_db_ctx() as db:
        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date.in_([day1, day2])).delete(synchronize_session=False)
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date.in_([day1, day2])).delete(synchronize_session=False)

    scanner = AlertScanner()
    code = "000001.SZ"
    for minute, label in (
        ("09:40", LABEL_WATCH),     # 首次出现
        ("09:50", LABEL_AVOID),     # 规避覆盖观望 ✓
        ("10:00", LABEL_WATCH),     # 观望不能覆盖规避 ✗
        ("10:10", LABEL_ACTIVE),    # 活跃覆盖规避 ✓
        ("10:20", LABEL_AVOID),     # 规避不能覆盖活跃 ✗
        ("10:30", LABEL_STRONG),    # 强势覆盖活跃 ✓
        ("10:40", LABEL_ACTIVE),    # 活跃不能覆盖强势 ✗
    ):
        scanner._write_hits(day1, minute, [_hit(code, label)])

    # 次日重新开始：前一天是强势，今天出观望照样记录，不和前一天比较
    scanner._write_hits(day2, "09:40", [_hit(code, LABEL_WATCH)])

    with get_db_ctx() as db:
        row1 = db.query(MarketAlertHit).filter_by(trade_date=day1, ts_code=code).one()
        assert row1.label == LABEL_STRONG and row1.change_count == 3
        assert row1.last_change_time == "10:30" and row1.hit_time == "09:40"
        events = db.query(MarketAlertEvent).filter_by(trade_date=day1, ts_code=code).order_by(MarketAlertEvent.id).all()
        assert [(e.event_time, e.prev_label, e.label) for e in events] == [
            ("09:40", None, LABEL_WATCH),
            ("09:50", LABEL_WATCH, LABEL_AVOID),
            ("10:10", LABEL_AVOID, LABEL_ACTIVE),
            ("10:30", LABEL_ACTIVE, LABEL_STRONG),
        ]

        row2 = db.query(MarketAlertHit).filter_by(trade_date=day2, ts_code=code).one()
        assert row2.label == LABEL_WATCH and row2.change_count == 0
        assert db.query(MarketAlertEvent).filter_by(trade_date=day2, ts_code=code).count() == 1

        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date.in_([day1, day2])).delete(synchronize_session=False)
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date.in_([day1, day2])).delete(synchronize_session=False)


def test_attach_latest_returns_same_day_uses_raw_prices():
    from datetime import date as date_cls

    from src.core.services.market_alerts import attach_latest_returns

    day = date_cls(2026, 9, 21)
    rows = [
        {"ts_code": "000001.SZ", "entity_type": "stock", "price": 10.0},
        {"ts_code": "801080.SI", "entity_type": "sw_l1", "price": 9000.0},
        {"ts_code": "600000.SH", "entity_type": "stock", "price": 8.0},     # 没有最新价
    ]
    prices = {"000001.SZ": (11.0, day), "801080.SI": (8910.0, day)}

    attach_latest_returns(rows, day, prices=prices)

    assert rows[0]["cum_pct"] == 10.0 and rows[0]["last_price"] == 11.0 and rows[0]["price_date"] == "2026-09-21"
    assert rows[1]["cum_pct"] == -1.0
    assert rows[2]["cum_pct"] is None and rows[2]["last_price"] is None


def test_attach_latest_returns_adjusts_across_ex_rights(monkeypatch):
    """命中后发生十送十：原始价从 20 变成 10.5，直接相除是 −47.5%，按复权换算才是 +5%。"""
    import duckdb
    from datetime import date as date_cls

    from src.core.services import market_alerts as module

    hit_day, quote_day = date_cls(2026, 9, 1), date_cls(2026, 9, 18)
    con = duckdb.connect()
    con.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    con.executemany("INSERT INTO a_stock_adj_factor VALUES (?, ?, ?)", [
        ("000001.SZ", hit_day, 1.0), ("000001.SZ", quote_day, 2.0),     # 除权后因子翻倍
        ("600000.SH", hit_day, 3.0), ("600000.SH", quote_day, 3.0),     # 期间没除权
    ])
    class _KeepOpen:
        """被测代码用完会 close；测试里要复用同一个内存库，所以包一层让 close 什么也不做。"""
        def execute(self, *args, **kwargs):
            return con.execute(*args, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(module, "connect_analytics_db", lambda: _KeepOpen())

    rows = [
        {"ts_code": "000001.SZ", "entity_type": "stock", "price": 20.0},
        {"ts_code": "600000.SH", "entity_type": "stock", "price": 10.0},
        {"ts_code": "801080.SI", "entity_type": "sw_l1", "price": 9000.0},   # 指数没有除权，不换算
    ]
    prices = {
        "000001.SZ": (10.5, quote_day),
        "600000.SH": (11.0, quote_day),
        "801080.SI": (9450.0, quote_day),
    }

    module.attach_latest_returns(rows, hit_day, prices=prices)

    assert rows[0]["cum_pct"] == 5.0          # 20 × 1/2 = 10 → 10.5
    assert rows[1]["cum_pct"] == 10.0
    assert rows[2]["cum_pct"] == 5.0


def test_write_hits_no_longer_persists_returns():
    """现价与命中后涨幅不再每轮写库。"""
    from datetime import date as date_cls

    from src.core.database import MarketAlertEvent, MarketAlertHit, get_db_ctx
    from src.core.services.market_alerts import AlertScanner

    day = date_cls(2000, 1, 10)
    with get_db_ctx() as db:
        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == day).delete()
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == day).delete()
    AlertScanner()._write_hits(day, "09:40", [_hit("000001.SZ", LABEL_ACTIVE, price=10.0)])
    with get_db_ctx() as db:
        row = db.query(MarketAlertHit).filter_by(trade_date=day, ts_code="000001.SZ").one()
        assert row.last_price is None and row.cum_pct is None and row.price == 10.0
        db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == day).delete()
        db.delete(row)
