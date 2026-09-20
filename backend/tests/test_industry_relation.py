import sys
from datetime import date
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
        close_ref=[9.0, 9.2, 9.4, 9.6, 10.0], open_ref4=9.3, listed_days=300)}

    rows = build_stock_rows(
        quotes, members, structures,
        baseline_minute={"000001.SZ": 800_000.0},       # 量比 2.5
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
