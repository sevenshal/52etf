#!/usr/bin/env python3
"""净利润断层回测：比较"加不加真缺口（T+1 最低价 > T 日最高价）"，并按跳空幅度分桶看 5/20 日收益与胜率。

事件口径与生产 ``earnings_gap`` 服务一致（但回测用全部历史财报，而不只是每只股票最近一期）：

- 每个 (股票, 报告期) 取首次公告日 ann_date 与首次公告的 netprofit_yoy，30% ≤ 增速 ≤ 3000%
- T+1（财报后第一个反应日）两种口径用 --t1 切换：after = ann_date 之后第一个交易日；
  on = ann_date 当天（若为交易日）或之后第一个交易日。tushare 的 ann_date 是正式披露日，
  盘后发布的公告通常记为次日，所以 ann_date 当天往往就是市场第一次反应的日子。T = T+1 的前一交易日。
- T+1 满足：上市满 120 个交易日、成交额 ≥ 3000 万、开盘较前收高开 ≥ 2%、
  收盘 > 开盘、收盘未封涨停
- 涨停价：分析库没有历史 stk_limit，按板块规则估算（主板 10%、创业/科创 20%、北交所 30%，
  名称含 ST 的主板股同时按 5% 判断），收盘 ≥ 估算涨停价且收盘 = 最高价视为封板
- 真缺口：T+1 最低价 > T 日最高价（T 日最高价按复权因子换算到 T+1 口径，避免除权日误判）
- 买入 = T+1 收盘；N 日收益 = 第 N 个交易日收盘（前复权）/ 买入价 − 1，停牌取之前最近一个收盘；
  超额 = 同期沪深300 收益之差

复权因子从 2018-07 开始有数据，所以样本从 2018-08 开始。

运行（生产库只读）::

    sg quantd -c ".venv/bin/python lab/earnings_gap_backtest.py"
"""

from __future__ import annotations

import argparse
import os

import duckdb
import numpy as np
import pandas as pd

DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/home/quantd/quant_prod/quant_robot/analytics.duckdb")
START = "2018-08-01"
HORIZONS = (5, 20, 90)
GAP_BUCKETS = [2, 3, 4, 5, 7, 10, 1000]

EVENTS_SQL = """
WITH cal AS (
    SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date) AS idx
    FROM a_stock_index_daily WHERE ts_code = '000300.SH'
),
ev AS (
    SELECT r.*,
           (SELECT MIN(trade_date) FROM cal WHERE cal.trade_date {t1_op} r.ann_date) AS t1
    FROM raw_events r
    WHERE r.np_yoy BETWEEN {min_yoy} AND {max_yoy}
),
ev2 AS (
    SELECT ev.*, c1.idx AS t1_idx, c0.trade_date AS t0,
           b.name, b.list_date,
           (SELECT MIN(idx) FROM cal WHERE cal.trade_date >= b.list_date) AS list_idx
    FROM ev
    JOIN cal c1 ON c1.trade_date = ev.t1
    JOIN cal c0 ON c0.idx = c1.idx - 1
    JOIN a_stock_basic b ON b.ts_code = ev.ts_code
)
SELECT e.ts_code, e.source, e.end_date, e.ann_date, e.np_yoy, e.t1, e.t1_idx, e.name,
       e.t1_idx - COALESCE(e.list_idx, 0) + 1 AS listed_days,
       m1.open, m1.high, m1.low, m1.close, m1.pre_close, m1.amount,
       m0.high AS t0_high, f1.adj_factor AS t1_adj, f0.adj_factor AS t0_adj,
       (SELECT n.name FROM a_stock_name_changes n
         WHERE n.ts_code = e.ts_code AND n.start_date <= e.t1
           AND (n.end_date IS NULL OR n.end_date >= e.t1)
         ORDER BY n.start_date DESC LIMIT 1) AS t1_name
FROM ev2 e
JOIN a_stock_market_daily m1 ON m1.ts_code = e.ts_code AND m1.trade_date = e.t1
LEFT JOIN a_stock_market_daily m0 ON m0.ts_code = e.ts_code AND m0.trade_date = e.t0
LEFT JOIN a_stock_adj_factor f1 ON f1.ts_code = e.ts_code AND f1.trade_date = e.t1
LEFT JOIN a_stock_adj_factor f0 ON f0.ts_code = e.ts_code AND f0.trade_date = e.t0
"""

REPORT_EVENTS_SQL = """
SELECT ts_code, 'report' AS source, end_date, ann_date, netprofit_yoy AS np_yoy
FROM (
    SELECT ts_code, end_date, ann_date, netprofit_yoy,
           ROW_NUMBER() OVER (PARTITION BY ts_code, end_date ORDER BY ann_date) AS rn
    FROM a_stock_fina_indicator
    WHERE ann_date IS NOT NULL AND ann_date >= DATE '{start}'
)
WHERE rn = 1
"""

FORWARD_SQL = """
WITH cal AS (
    SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date) AS idx
    FROM a_stock_index_daily WHERE ts_code = '000300.SH'
),
targets AS (
    SELECT s.ts_code, s.t1, h.h AS horizon, c.trade_date AS target_date
    FROM sig s
    CROSS JOIN (SELECT UNNEST(?) AS h) h
    JOIN cal c ON c.idx = s.t1_idx + h.h
),
px AS (
    SELECT m.ts_code, m.trade_date, m.close * f.adj_factor AS hfq
    FROM a_stock_market_daily m
    JOIN a_stock_adj_factor f ON f.ts_code = m.ts_code AND f.trade_date = m.trade_date
    WHERE m.ts_code IN (SELECT DISTINCT ts_code FROM sig)
)
SELECT t.ts_code, t.t1, t.horizon, t.target_date, p.hfq AS exit_hfq, i1.close AS idx_t1, i2.close AS idx_exit
FROM targets t
ASOF LEFT JOIN px p ON p.ts_code = t.ts_code AND t.target_date >= p.trade_date
LEFT JOIN a_stock_index_daily i1 ON i1.ts_code = '000300.SH' AND i1.trade_date = t.t1
LEFT JOIN a_stock_index_daily i2 ON i2.ts_code = '000300.SH' AND i2.trade_date = t.target_date
"""


SOURCE_LABELS = {"report": "财报", "forecast": "预告", "express": "快报"}
QUARTER_LABELS = {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}
EVENT_PARQUET_DIR = os.getenv("EARNINGS_GAP_EVENT_DIR", "")


def load_raw_events(con, sources) -> pd.DataFrame:
    """三个事件源统一成 ts_code/source/end_date/ann_date/np_yoy。

    财报读分析库；预告/快报读 `EARNINGS_GAP_EVENT_DIR` 下由 tushare forecast_vip/express_vip
    拉好的 parquet（生产分析库还没有这两张表时用）。同一 (股票, 报告期, 事件源) 取首次公告。
    """
    frames = []
    if "report" in sources:
        frames.append(con.execute(REPORT_EVENTS_SQL.format(start=START)).fetchdf())
    for name in ("forecast", "express"):
        if name not in sources:
            continue
        path = os.path.join(EVENT_PARQUET_DIR, f"{name}.parquet")
        if not os.path.exists(path):
            raise SystemExit(f"缺少 {path}；先用 forecast_vip/express_vip 拉好历史再跑")
        frame = pd.read_parquet(path)
        if name == "forecast":
            # 预告只有变动幅度区间，取下限（最保守）
            frame["np_yoy"] = pd.to_numeric(frame["p_change_min"], errors="coerce")
        else:
            base = pd.to_numeric(frame["yoy_net_profit"], errors="coerce")
            current = pd.to_numeric(frame["n_income"], errors="coerce")
            frame["np_yoy"] = np.where(base > 0, (current / base - 1) * 100,
                                       pd.to_numeric(frame["yoy_dedu_np"], errors="coerce"))
        frame["source"] = name
        frame = frame.dropna(subset=["ts_code", "end_date", "ann_date", "np_yoy"])
        frame = frame.sort_values("ann_date").drop_duplicates(subset=["ts_code", "end_date"], keep="first")
        frames.append(frame[["ts_code", "source", "end_date", "ann_date", "np_yoy"]])
    events = pd.concat(frames, ignore_index=True)
    events = events.dropna(subset=["np_yoy"])
    events["ann_date"] = pd.to_datetime(events["ann_date"]).dt.date
    events["end_date"] = pd.to_datetime(events["end_date"]).dt.date
    return events[events["ann_date"] >= pd.Timestamp(START).date()]


def board_limit_ratio(ts_code: str) -> float:
    code = ts_code.split(".")[0]
    if ts_code.endswith(".BJ"):
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def is_sealed(row) -> bool:
    if row.close < row.high - 1e-6:
        return False
    ratios = [board_limit_ratio(row.ts_code)]
    name = str(row.t1_name or row.name or "")
    if "ST" in name.upper() and ratios[0] == 0.10:
        ratios.append(0.05)
    return any(row.close >= round(row.pre_close * (1 + ratio) + 1e-9, 2) - 1e-6 for ratio in ratios)


def summarize(frame: pd.DataFrame) -> dict:
    out = {"样本": len(frame)}
    for h in HORIZONS:
        ret = frame[f"ret{h}"].dropna()
        exc = frame[f"exc{h}"].dropna()
        out[f"{h}日均值%"] = ret.mean() * 100
        out[f"{h}日中位%"] = ret.median() * 100
        out[f"{h}日胜率%"] = (ret > 0).mean() * 100
        out[f"{h}日超额%"] = exc.mean() * 100
        out[f"{h}日超额胜率%"] = (exc > 0).mean() * 100
    return out


def table(groups) -> pd.DataFrame:
    return pd.DataFrame({name: summarize(frame) for name, frame in groups}).T.round(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="把事件明细写到这个 CSV")
    parser.add_argument(
        "--t1",
        choices=("after", "on"),
        default="after",
        help="after: 公告日之后第一个交易日；on: 公告日当天或之后第一个交易日（A股财报多为盘后发布，默认 after）",
    )
    parser.add_argument("--sources", default="report,forecast,express", help="逗号分隔：report/forecast/express")
    parser.add_argument("--min-yoy", type=float, default=30.0)
    parser.add_argument("--max-yoy", type=float, default=3000.0)
    args = parser.parse_args()
    sources = [item.strip() for item in args.sources.split(",") if item.strip()]
    print(f"T+1 口径: {'公告日之后第一个交易日' if args.t1 == 'after' else '公告日当天(若为交易日)或之后第一个交易日'}")
    print(f"事件源: {'/'.join(SOURCE_LABELS.get(item, item) for item in sources)}；净利同比 {args.min_yoy}%~{args.max_yoy}%")

    con = duckdb.connect(DB_PATH, read_only=True)
    raw_events = load_raw_events(con, sources)
    print("原始事件数:", raw_events.groupby("source").size().to_dict())
    con.register("raw_events", raw_events)
    events = con.execute(EVENTS_SQL.format(
        t1_op=">" if args.t1 == "after" else ">=", min_yoy=args.min_yoy, max_yoy=args.max_yoy,
    )).fetchdf()
    print(f"净利同比达标且 T+1 有日K: {len(events)}")

    events = events[events["listed_days"] >= 120]
    events = events[events["amount"] >= 30000]  # 千元
    events["gap_pct"] = (events["open"] / events["pre_close"] - 1) * 100
    events = events[(events["gap_pct"] >= 2) & (events["close"] > events["open"])].copy()
    events["sealed"] = [is_sealed(row) for row in events.itertuples(index=False)]
    events = events[~events["sealed"]].copy()
    # T 日最高价换算到 T+1 的价格口径（除权日 T+1 的 pre_close 已调整）
    ratio = (events["t0_adj"] / events["t1_adj"]).where(events["t0_adj"].notna() & events["t1_adj"].notna(), 1.0)
    events["t0_high_adj"] = events["t0_high"] * ratio
    events["true_gap"] = events["low"] > events["t0_high_adj"] + 1e-9
    events = events.dropna(subset=["t1_adj"])
    print(f"满足全部条件（高开≥2%、收阳、未封板、成交额、上市天数）: {len(events)}，其中真缺口 {int(events['true_gap'].sum())}")

    con.register("sig", events[["ts_code", "t1", "t1_idx"]].drop_duplicates())
    fwd = con.execute(FORWARD_SQL, [list(HORIZONS)]).fetchdf()
    for h in HORIZONS:
        part = fwd[fwd["horizon"] == h][["ts_code", "t1", "exit_hfq", "idx_t1", "idx_exit"]]
        events = events.merge(part.rename(columns={
            "exit_hfq": f"exit{h}", "idx_t1": f"idx_t1_{h}", "idx_exit": f"idx_exit{h}",
        }), on=["ts_code", "t1"], how="left")
        events[f"ret{h}"] = events[f"exit{h}"] / (events["close"] * events["t1_adj"]) - 1
        events[f"exc{h}"] = events[f"ret{h}"] - (events[f"idx_exit{h}"] / events[f"idx_t1_{h}"] - 1)

    events["source_label"] = events["source"].map(SOURCE_LABELS)
    events["quarter"] = pd.to_datetime(events["end_date"]).dt.month.map(QUARTER_LABELS)
    events["year"] = pd.to_datetime(events["t1"]).dt.year
    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 40)

    print("\n== 按事件源 ==")
    print(table([
        (SOURCE_LABELS.get(name, name), frame) for name, frame in events.groupby("source", observed=True)
    ]).to_string())

    print("\n== 事件源 × 报告期 ==")
    rows = {}
    for (source, quarter), frame in events.groupby(["source", "quarter"], observed=True):
        rows[f"{SOURCE_LABELS.get(source, source)}-{quarter}"] = summarize(frame)
    print(pd.DataFrame(rows).T.round(2).to_string())

    print("\n== 事件源 × 年度（20日均值% / 胜率% / 样本） ==")
    rows = {}
    for year, frame in events.groupby("year"):
        row = {}
        for source in sources:
            part = frame[frame["source"] == source]
            label = SOURCE_LABELS.get(source, source)
            ret = part["ret20"].dropna()
            row[f"{label}样本"] = len(part)
            row[f"{label}均值%"] = round(ret.mean() * 100, 2) if len(ret) else None
            row[f"{label}胜率%"] = round((ret > 0).mean() * 100, 1) if len(ret) else None
        rows[year] = row
    print(pd.DataFrame(rows).T.to_string())

    print("\n== 真缺口 × 事件源 ==")
    print(table([
        (f"{SOURCE_LABELS.get(name, name)}-{'真缺口' if gap else '无缺口'}", frame)
        for (name, gap), frame in events.groupby(["source", "true_gap"], observed=True)
    ]).to_string())

    print("\n== 按高开幅度分桶（全部事件源） ==")
    events["gap_bucket"] = pd.cut(events["gap_pct"], GAP_BUCKETS, right=False,
                                  labels=[f"{a}~{b}%" if b < 1000 else f"≥{a}%" for a, b in zip(GAP_BUCKETS, GAP_BUCKETS[1:])])
    print(table([(str(name), frame) for name, frame in events.groupby("gap_bucket", observed=True)]).to_string())

    if args.csv:
        events.to_csv(args.csv, index=False)
        print(f"\n明细已写入 {args.csv}")


if __name__ == "__main__":
    main()
