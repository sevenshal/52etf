import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.industry_relation import (
    aggregate_by_level,
    build_stock_rows,
    composite_score,
    sentiment_score,
)
from src.core.services.market_alerts import LABEL_ACTIVE, LABEL_STRONG, LABEL_WATCH, StockStructure


def _stock(**kwargs):
    base = dict(
        l1_name="电子", l2_name="半导体", l3_name="集成电路设计",
        pct=2.0, amount_yi=5.0, label=None, limit_up=False, limit_down=False, boards=0,
    )
    base.update(kwargs)
    return base


def test_aggregate_by_level_counts_labels_and_boards():
    stocks = [
        _stock(label=LABEL_STRONG, pct=10.0, limit_up=True, boards=1),    # 首板
        _stock(label=LABEL_STRONG, pct=10.0, limit_up=True, boards=2),    # 二板
        _stock(label=LABEL_ACTIVE, pct=10.0, limit_up=True, boards=4),    # 多板
        _stock(label=LABEL_WATCH, pct=-3.0),
        _stock(l3_name="半导体设备", pct=1.0),
    ]

    l3 = {row["name"]: row for row in aggregate_by_level(stocks, "l3")}
    ic = l3["集成电路设计"]
    assert ic["count"] == 4 and ic["up"] == 3 and ic["down"] == 1
    assert ic["lu"] == 3 and (ic["fb"], ic["eb"], ic["lb3"]) == (1, 1, 1)
    assert ic["st"] == 2 and ic["by"] == 1 and ic["gw"] == 1
    assert ic["parent"] == "半导体"

    # 一级行业把两个三级合并，均值按成分股算
    l1 = aggregate_by_level(stocks, "l1", min_count=1)
    assert len(l1) == 1 and l1[0]["count"] == 5
    assert l1[0]["rank_str"] == "1/1"


def test_sentiment_and_composite_scores_stay_in_range():
    hot = {"count": 10, "lu": 5, "fb": 2, "eb": 2, "lb3": 1, "ld": 0, "up": 9, "pct": 6.0}
    cold = {"count": 10, "lu": 0, "fb": 0, "eb": 0, "lb3": 0, "ld": 3, "up": 1, "pct": -4.0}
    assert sentiment_score(hot) == 100.0            # 上限裁剪
    assert sentiment_score(cold) == 0.0             # 下限裁剪
    hot_row = {**hot, "sentiment": sentiment_score(hot), "amount_yi": 100.0}
    cold_row = {**cold, "sentiment": sentiment_score(cold), "amount_yi": 10.0}
    assert composite_score(hot_row, 100.0) > composite_score(cold_row, 100.0)
    assert 0 <= composite_score(cold_row, 100.0) <= 100


def test_build_stock_rows_joins_industry_label_and_boards():
    quotes = pd.DataFrame([
        {"ts_code": "000001.SZ", "close": 11.0, "pre_close": 10.0, "open": 10.2, "vol": 2_000_000, "amount": 1.5e8},
        {"ts_code": "999999.SZ", "close": 5.0, "pre_close": 5.0, "open": 5.0, "vol": 1000, "amount": 1e6},
    ])
    members = pd.DataFrame([{"ts_code": "000001.SZ", "l1_name": "银行", "l2_name": "国有大型银行", "l3_name": "国有大型银行Ⅲ"}])
    structures = {"000001.SZ": StockStructure(
        ts_code="000001.SZ", name="平安银行", td_up_prev=2,
        close_ref=[9.6, 9.8, 10.0, 9.9, 10.0], open_ref4=9.7, open_ref1=9.8,
        vol_max_prev=1_500_000.0, listed_days=300)}

    rows = build_stock_rows(
        quotes, members, structures,
        baseline_minute={"000001.SZ": 800_000.0},       # 量比 2.5；累计量 200 万 ≥ 前 8 日最大 150 万 → 强势
        limits={"000001.SZ": (11.0, 9.0)},              # 现价 11.0 = 涨停价
        boards={"000001.SZ": 1},                        # 昨日已有 1 个板
    )

    assert len(rows) == 1                                # 不在申万成分里的股票被剔除
    row = rows[0]
    assert row["name"] == "平安银行" and row["l2_name"] == "国有大型银行"
    assert row["label"] == LABEL_STRONG
    assert row["limit_up"] is True and row["boards"] == 2   # 昨日1板 + 今日涨停 = 二板
    assert row["pct"] == 10.0


def test_small_industries_are_listed_but_not_ranked():
    """只有两三只成分股的小行业不参与排名，否则一只涨停就能顶到榜首。"""
    stocks = [
        _stock(l3_name="小行业", pct=10.0, limit_up=True, boards=1),
        _stock(l3_name="小行业", pct=9.0),
        *[_stock(l3_name="大行业", pct=1.0) for _ in range(8)],
    ]

    rows = {row["name"]: row for row in aggregate_by_level(stocks, "l3", min_count=5)}

    assert rows["小行业"]["rank"] is None and rows["小行业"]["rank_str"] == "成分<5"
    assert rows["大行业"]["rank"] == 1 and rows["大行业"]["rank_str"] == "1/1"
    # 小行业的统计值仍然算出来了，只是不排名
    assert rows["小行业"]["count"] == 2 and rows["小行业"]["lu"] == 1


def test_load_members_current_and_historical():
    """当前归属读 a_stock_sw_member；给了日期就用变更历史还原那天的归属。"""
    import duckdb

    from src.core.services.industry_relation import load_members

    con = duckdb.connect()
    con.execute("CREATE TABLE a_stock_sw_member (ts_code VARCHAR, l1_name VARCHAR, l2_name VARCHAR, l3_name VARCHAR)")
    con.execute("INSERT INTO a_stock_sw_member VALUES ('600519.SH', '食品饮料', '白酒Ⅱ', '白酒Ⅲ')")
    con.execute("CREATE TABLE a_stock_sw_industry (index_code VARCHAR, industry_name VARCHAR, industry_code VARCHAR, level VARCHAR, parent_code VARCHAR)")
    con.executemany("INSERT INTO a_stock_sw_industry VALUES (?, ?, ?, ?, ?)", [
        ("801120.SI", "食品饮料", "120000", "L1", "0"),
        ("801125.SI", "白酒Ⅱ", "120100", "L2", "120000"),
        ("851251.SI", "白酒Ⅲ", "120101", "L3", "120100"),
    ])
    con.execute("CREATE TABLE a_stock_sw_member_change (index_code VARCHAR, con_code VARCHAR, in_date DATE, out_date DATE, is_new VARCHAR)")
    con.executemany("INSERT INTO a_stock_sw_member_change VALUES (?, ?, ?, ?, ?)", [
        ("851251.SI", "600519.SH", date(2001, 7, 31), None, "Y"),
        ("851251.SI", "000001.SZ", date(2015, 1, 1), date(2020, 1, 1), "N"),   # 2020 年已移出
    ])

    current = load_members(con)
    assert list(current["ts_code"]) == ["600519.SH"]

    old = load_members(con, as_of=date(2018, 6, 30))
    assert set(old["ts_code"]) == {"600519.SH", "000001.SZ"}       # 当年两只都在
    assert set(old["l1_name"]) == {"食品饮料"}

    now = load_members(con, as_of=date(2026, 9, 18))
    assert set(now["ts_code"]) == {"600519.SH"}                     # 移出的不再计入
    con.close()


def _daily_frame(rows):
    frame = pd.DataFrame(rows, columns=["ts_code", "trade_date", "open", "close", "pre_close", "vol", "amount", "limit_status"])
    return frame


def test_focus_matches_labels_and_boards():
    from src.core.services.industry_relation import focus_matches

    limit_first = {"limit_up": True, "boards": 1, "label": LABEL_ACTIVE}
    limit_third = {"limit_up": True, "boards": 3, "label": LABEL_STRONG}
    watch = {"limit_up": False, "boards": 0, "label": LABEL_WATCH}

    assert focus_matches(limit_first, "") is True            # 不过滤
    assert focus_matches(limit_first, "lu") is True
    assert focus_matches(limit_first, "fb") is True and focus_matches(limit_third, "fb") is False
    assert focus_matches(limit_third, "lb") is True and focus_matches(limit_first, "lb") is False
    assert focus_matches(limit_third, "st") is True and focus_matches(watch, "st") is False
    assert focus_matches(watch, "gw") is True
    # 多选取并集；st_up / by_up 仅当前标签强于最近一次标签时命中
    assert focus_matches(watch, "lu,gw") is True and focus_matches(limit_first, "lb,gw") is False
    assert focus_matches({"label": LABEL_STRONG, "previous_label": LABEL_ACTIVE}, "st_up") is True
    assert focus_matches({"label": LABEL_STRONG, "previous_label": LABEL_STRONG}, "st_up") is False
    assert focus_matches({"label": LABEL_STRONG, "previous_label": None}, "st_up") is False
    assert focus_matches({"label": LABEL_ACTIVE, "previous_label": LABEL_WATCH}, "st_up,by_up") is True
    assert focus_matches({"label": LABEL_ACTIVE, "previous_label": LABEL_STRONG}, "by_up") is False


def test_load_universe_codes_by_board_index_and_micro():
    import duckdb

    from src.core.services.industry_relation import load_universe_codes

    con = duckdb.connect()
    con.execute("CREATE TABLE a_stock_sw_member (ts_code VARCHAR, l1_name VARCHAR, l2_name VARCHAR, l3_name VARCHAR)")
    con.executemany("INSERT INTO a_stock_sw_member VALUES (?, '行业', '二级', '三级')",
                    [("600000.SH",), ("688001.SH",), ("000001.SZ",), ("300001.SZ",), ("920001.BJ",)])
    con.execute("CREATE TABLE a_stock_index_weight (index_code VARCHAR, con_code VARCHAR, trade_date DATE)")
    con.executemany("INSERT INTO a_stock_index_weight VALUES ('000300.SH', ?, DATE '2026-09-18')",
                    [("600000.SH",), ("000001.SZ",)])
    con.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, total_mv DOUBLE)")
    con.executemany("INSERT INTO a_stock_market_daily VALUES (?, DATE '2026-09-18', ?)",
                    [("600000.SH", 900.0), ("300001.SZ", 100.0), ("000001.SZ", 500.0)])

    today = date(2026, 9, 20)
    assert load_universe_codes(con, "all", today) is None                 # 全A 不过滤
    assert load_universe_codes(con, "star", today) == {"688001.SH"}
    assert load_universe_codes(con, "sh_main", today) == {"600000.SH"}    # 科创板不算上证主板
    assert load_universe_codes(con, "gem", today) == {"300001.SZ"}
    assert load_universe_codes(con, "bj", today) == {"920001.BJ"}
    assert load_universe_codes(con, "hs300", today) == {"600000.SH", "000001.SZ"}
    micro = load_universe_codes(con, "micro", today)
    assert "300001.SZ" in micro                                            # 市值最小的进微盘
    con.close()


def test_load_recent_labels_returns_last_labeled_days_with_boards(monkeypatch):
    import duckdb

    from src.core.services import industry_relation
    from src.core.services.industry_relation import load_recent_labels

    con = duckdb.connect()
    con.execute("""CREATE TABLE a_stock_market_daily (
        ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE,
        vol DOUBLE, amount DOUBLE, limit_status INTEGER)""")
    rows = []
    price = 10.0
    for i in range(30):
        day = date(2026, 8, 1) + timedelta(days=i)
        prev = price
        price = round(price * 1.02, 2)          # 持续上涨 → 九转高计数累积
        status = None if i == 26 else (2 if i >= 28 else 1)  # 缺失状态不能让历史标签接口 500
        rows.append(("000001.SZ", day, prev, price, prev, 1_000_000, 200_000, status))
    con.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR)")
    con.execute("INSERT INTO a_stock_basic VALUES ('000001.SZ', '平安银行')")

    # 最近 5 个交易日内，第 1、3、4、5 天有标签；只保留其中最近 3 个，跳过无标签日。
    labels = {
        round(rows[25][3], 2): LABEL_WATCH,
        round(rows[27][3], 2): LABEL_ACTIVE,
        round(rows[28][3], 2): LABEL_STRONG,
        round(rows[29][3], 2): LABEL_STRONG,
    }
    monkeypatch.setattr(industry_relation, "classify", lambda *, price, **_: labels.get(round(price, 2)))

    history = load_recent_labels(con, date(2026, 8, 31), days=3)["000001.SZ"]

    assert len(history) == 3
    assert [item["d"] for item in history] == ["2026-08-28", "2026-08-29", "2026-08-30"]
    assert [item["boards"] for item in history] == [0, 1, 2]      # 连板数递增
    assert [item["label"] for item in history] == [LABEL_ACTIVE, LABEL_STRONG, LABEL_STRONG]
    assert all(item["d"] for item in history)
    con.close()


def test_cache_ttl_short_in_session_long_off_hours():
    from datetime import datetime as dt

    from src.core.services.industry_relation import (
        CACHE_TTL_SECONDS, OFF_HOURS_CACHE_TTL_SECONDS, cache_ttl_seconds,
    )

    assert cache_ttl_seconds(dt(2026, 9, 22, 10, 0)) == CACHE_TTL_SECONDS       # 盘中
    assert cache_ttl_seconds(dt(2026, 9, 22, 9, 20)) == CACHE_TTL_SECONDS       # 集合竞价
    assert cache_ttl_seconds(dt(2026, 9, 22, 12, 0)) == OFF_HOURS_CACHE_TTL_SECONDS   # 午休
    assert cache_ttl_seconds(dt(2026, 9, 22, 0, 50)) == OFF_HOURS_CACHE_TTL_SECONDS   # 半夜
    assert cache_ttl_seconds(dt(2026, 9, 26, 10, 0)) == OFF_HOURS_CACHE_TTL_SECONDS   # 周六


def test_load_today_hits_falls_back_to_last_trading_day_when_market_closed():
    """休市日没有命中记录，要回退到最近一个有记录的交易日，与行情快照口径一致。

    否则周末打开行业关联，所有个股都没有标签，默认的「强势*/活跃*」过滤会把页面清空。
    """
    from datetime import date as date_cls

    from src.core.database import MarketAlertHit, get_db_ctx
    from src.core.services.industry_relation import _load_today_hits
    from src.core.services.market_alerts import LABEL_STRONG

    friday, sunday = date_cls(2000, 2, 4), date_cls(2000, 2, 6)
    with get_db_ctx() as db:
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date.in_([friday, sunday])).delete()
        db.add(MarketAlertHit(
            trade_date=friday, ts_code="000001.SZ", entity_type="stock", hit_time="09:40",
            last_change_time="10:05", change_count=1, name="平安银行", industry="银行",
            industry_l1="银行", industry_l2="股份制银行Ⅱ", industry_l3="",
            label=LABEL_STRONG, score=50.0, price=10.0, pct=1.0,
        ))

    stocks, _, _, label_date = _load_today_hits(sunday)
    assert label_date == friday
    assert stocks["000001.SZ"]["label"] == LABEL_STRONG
    assert stocks["000001.SZ"]["change_count"] == 1      # 升级过的记录在休市日也认得出来

    with get_db_ctx() as db:
        db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == friday).delete()
