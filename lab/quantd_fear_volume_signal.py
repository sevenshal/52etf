#!/usr/bin/env python3
"""中证全指 恐贪×量能 阶段顶底左右侧信号验证脚本（可复跑）。

最终参数（详见 quantd_fear_volume_signal.md）：
- 大级别顶底：±20 交易日窗口局部极值 + 严格交替（两顶间无底只留更高者，反之亦然）
  → 6 年（2020-05 ~ 2026-08）共 19 个阶段底、20 个阶段顶。
- 成交量代理：中证全指无直接 ETF，用 沪深300(510300)+中证500(510500)+
  中证1000(512100)+中证2000(563300) 四只宽基 ETF 当日成交额加总。
- 量能指标：z = (ln(V_t) - mean(ln(V 过去20日,不含今天))) / std(...)；
  量比 = V_t / median(V 过去20日,不含今天)。
- 左侧底信号  fg<=30 且 z>1.25          （20日冷却去重后 n=21，覆盖 11/19）
- 左侧顶信号  fg>=75 且 z<-0.25         （n=13，覆盖 5/20）
- 右侧底信号  恐贪MA5拐头向上 + 近5日任意一天恐贪<=30（n=43，覆盖 12/19，命中 32.6%）
- 右侧顶信号  恐贪MA5拐头向下 + 近5日任意一天恐贪>=75（n=26，覆盖 9/20，命中 42.3%）
- 所有信号加 5 日冷却期（同类型信号 5 天内不重复触发）

用法：
  sg quantd -c 'cd /home/sevenshal/Dev/github/quant/52etf && .venv/bin/python lab/quantd_fear_volume_signal.py'
  或设置 QUANT_SQLITE_PATH / ANALYTICS_DB_PATH 环境变量后直接运行。
"""

import os
import sqlite3

import duckdb
import numpy as np
import pandas as pd

QUANT_SQLITE_PATH = os.getenv(
    "QUANT_SQLITE_PATH", "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
)
ANALYTICS_DB_PATH = os.getenv(
    "ANALYTICS_DB_PATH", "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
)

INDEX_CODE = "000985.SH"          # 中证全指
AGGREGATE_ETFS = ["510300.SH", "510500.SH", "512100.SH", "563300.SH"]
EXTREMA_WINDOW = 20               # 阶段顶底判定窗口（前后交易日）
LEFT_K = 5                        # 左侧信号命中窗口（极值前 K 天）
RIGHT_K = 10                      # 右侧信号命中窗口（极值当天或之后 K 天）
COOLDOWN = 5                      # 同类型信号冷却天数（去重）
VALIDATION_CUT = "2024-01-01"     # 样本外划分点


def load_data() -> pd.DataFrame:
    """中证全指收盘价 + 4ETF 聚合成交额 + 恐贪分数，按日期对齐。"""
    con = duckdb.connect(ANALYTICS_DB_PATH, read_only=True)
    idx = con.execute(
        "SELECT trade_date, close FROM a_stock_index_daily WHERE ts_code=? ORDER BY trade_date",
        [INDEX_CODE],
    ).fetchdf()
    m = idx.copy()
    m["date"] = pd.to_datetime(m.trade_date)
    for code in AGGREGATE_ETFS:
        d = con.execute(
            "SELECT trade_date, amount FROM a_stock_fund_daily WHERE ts_code=? ORDER BY trade_date",
            [code],
        ).fetchdf()
        d.columns = ["date", "amt_" + code.split(".")[0]]
        d["date"] = pd.to_datetime(d.date)
        m = m.merge(d, on="date", how="left")
    con.close()
    m = m.drop_duplicates("date").reset_index(drop=True)
    for code in AGGREGATE_ETFS:
        col = "amt_" + code.split(".")[0]
        m[col] = pd.to_numeric(m[col], errors="coerce")
    m["vol"] = m[[f"amt_{c.split('.')[0]}" for c in AGGREGATE_ETFS]].sum(
        axis=1, min_count=1
    )

    s = sqlite3.connect("file:" + QUANT_SQLITE_PATH + "?mode=ro", uri=True)
    fgd = pd.read_sql(
        "SELECT date, score FROM etf_fear_greed_clone_history WHERE symbol=? ORDER BY date",
        s,
        params=[INDEX_CODE],
    )
    s.close()
    fgd["date"] = pd.to_datetime(fgd["date"])
    df = m.merge(fgd, on="date").reset_index(drop=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """z（log 量 20 日滚动）、量比（今日/前20日中位数）、恐贪 MA5。"""
    n = len(df)
    vol = df["vol"].values
    z = np.full(n, np.nan)
    ratio = np.full(n, np.nan)
    for i in range(20, n):
        past = vol[i - 20 : i]  # 不含今天的前 20 个交易日
        lnp = np.log(past)
        z[i] = (np.log(vol[i]) - lnp.mean()) / lnp.std()
        ratio[i] = vol[i] / np.median(past)
    df["z"] = z
    df["ratio"] = ratio
    df["fma5"] = pd.Series(df["score"].values).rolling(5).mean().values
    return df


def find_extrema(c: np.ndarray, window: int = EXTREMA_WINDOW):
    """±window 窗口局部极值 + 严格交替（同类型相邻只保留更极端者）。"""
    n = len(c)
    cands = []
    for i in range(window, n - window):
        wl, wr = c[i - window : i], c[i + 1 : i + window + 1]
        if c[i] >= max(wl.max(), wr.max()):
            cands.append((i, "H"))
        if c[i] <= min(wl.min(), wr.min()):
            cands.append((i, "L"))
    alt = []
    for idx, typ in cands:
        if not alt or alt[-1][1] != typ:
            alt.append([idx, typ])
        else:
            if typ == "H" and c[idx] > c[alt[-1][0]]:
                alt[-1] = [idx, typ]
            if typ == "L" and c[idx] < c[alt[-1][0]]:
                alt[-1] = [idx, typ]
    return [i for i, t in alt if t == "H"], [i for i, t in alt if t == "L"]


def dedup(days, cooldown: int = COOLDOWN):
    """同类型信号冷却去重：距上次触发 <= cooldown 天的重复触发去掉。"""
    kept = []
    for t in sorted(days):
        if not kept or t - kept[-1] > cooldown:
            kept.append(t)
    return np.array(kept)


def report_left(days, extrema, n_ext, label, cut_idx=None, seg=None):
    """左侧信号评估：触发日在极值前 LEFT_K 天内视为命中。"""
    if seg == "te":
        days = days[days >= cut_idx]
    ns = len(days)
    win = np.zeros(n, bool)
    for e in extrema:
        win[max(0, e - LEFT_K) : e + 1] = True
    hit = sum(1 for t in days if win[t])
    ext_used = (
        [e for e in extrema]
        if seg is None
        else ([e for e in extrema if e >= cut_idx] if seg == "te" else [e for e in extrema if e < cut_idx])
    )
    cov = sum(1 for e in ext_used if any(t >= max(0, e - LEFT_K) and t <= e for t in days))
    print(
        "  %-36s n=%3d 命中%3d(%.1f%%) 覆盖%2d/%d"
        % (label, ns, hit, 100 * hit / max(ns, 1), cov, len(ext_used))
    )
    return cov, len(ext_used)


def report_right(days, extrema, n_ext, label, cut_idx=None, seg=None):
    """右侧信号评估：触发日在极值当天或之后 RIGHT_K 天内为右侧命中，极值前为提前误报。"""
    if seg == "te":
        days = days[days >= cut_idx]
    ns = len(days)
    win = np.zeros(n, bool)
    early = np.zeros(n, bool)
    for e in extrema:
        win[max(0, e) : min(n, e + RIGHT_K + 1)] = True
        early[max(0, e - RIGHT_K) : e] = True
    hit = sum(1 for t in days if win[t])
    err = sum(1 for t in days if early[t] and not win[t])
    ext_used = (
        [e for e in extrema]
        if seg is None
        else ([e for e in extrema if e >= cut_idx] if seg == "te" else [e for e in extrema if e < cut_idx])
    )
    cov = sum(1 for e in ext_used if any(t >= e and t <= min(n, e + RIGHT_K) for t in days))
    pre = sum(1 for e in ext_used if any(t >= max(0, e - RIGHT_K) and t < e for t in days))
    print(
        "  %-36s n=%3d 右侧命中%3d(%.1f%%) 提前误报%3d(%.1f%%) 覆盖%2d/%d 提前数%2d"
        % (label, ns, hit, 100 * hit / max(ns, 1), err, 100 * err / max(ns, 1), cov, len(ext_used), pre)
    )
    return cov, len(ext_used)


def main():
    df = load_data()
    df = add_indicators(df)
    global n
    n = len(df)
    c = df["close"].values
    f = df["score"].values
    z = df["z"].values
    fma5 = df["fma5"].values
    dates = df["date"].values
    print("数据范围: %s ~ %s (%d 个交易日)" % (str(dates[0])[:10], str(dates[-1])[:10], n))

    peaks, troughs = find_extrema(c)
    cut_idx = int(np.searchsorted(dates, np.datetime64(VALIDATION_CUT)))
    print(
        "\n[1] 大级别顶底 (±%d 交易日 + 严格交替): 顶=%d 底=%d | 验证段(>=%s): 顶=%d 底=%d"
        % (EXTREMA_WINDOW, len(peaks), len(troughs), VALIDATION_CUT,
           sum(1 for e in peaks if e >= cut_idx), sum(1 for e in troughs if e >= cut_idx))
    )
    print("  底:", ", ".join(str(dates[e])[:10] for e in troughs))
    print("  顶:", ", ".join(str(dates[e])[:10] for e in peaks))

    # ---- 左侧信号（冷却去重）----
    buy_days = dedup(np.where((f <= 30) & (z > 1.25))[0])
    sell_days = dedup(np.where((f >= 75) & (z < -0.25))[0])

    # ---- 右侧信号（MA5 拐头 + 近5日任意一天极端，冷却去重）----
    rb_raw = [
        t for t in range(5, n)
        if fma5[t] > fma5[t - 1] and np.nanmin(f[t - 4 : t + 1]) <= 30
    ]
    rs_raw = [
        t for t in range(5, n)
        if fma5[t] < fma5[t - 1] and np.nanmax(f[t - 4 : t + 1]) >= 75
    ]
    rb_days = dedup(rb_raw)
    rs_days = dedup(rs_raw)

    print("\n[2] 左侧信号（%d 日冷却）" % COOLDOWN)
    report_left(buy_days, troughs, len(troughs), "底: fg<=30 且 z>1.25")
    report_left(buy_days, troughs, len(troughs), "底(验证段2024+)", cut_idx=cut_idx, seg="te")
    report_left(sell_days, peaks, len(peaks), "顶: fg>=75 且 z<-0.25")
    report_left(sell_days, peaks, len(peaks), "顶(验证段2024+)", cut_idx=cut_idx, seg="te")

    print("\n[3] 右侧信号（MA5 拐头 + 近5日任意一天极端，%d 日冷却）" % COOLDOWN)
    report_right(rb_days, troughs, len(troughs), "底: MA5拐头向上+近5日任意fg<=30")
    report_right(rb_days, troughs, len(troughs), "底(验证段2024+)", cut_idx=cut_idx, seg="te")
    report_right(rs_days, peaks, len(peaks), "顶: MA5拐头向下+近5日任意fg>=75")
    report_right(rs_days, peaks, len(peaks), "顶(验证段2024+)", cut_idx=cut_idx, seg="te")

    print("\n[4] 左右并集覆盖")
    cov_l = sum(1 for e in troughs if any(t >= max(0, e - LEFT_K) and t <= e for t in buy_days))
    cov_r = sum(1 for e in troughs if any(t >= e and t <= min(n, e + RIGHT_K) for t in rb_days))
    both = sum(
        1
        for e in troughs
        if any(t >= max(0, e - LEFT_K) and t <= e for t in buy_days)
        or any(t >= e and t <= min(n, e + RIGHT_K) for t in rb_days)
    )
    print("  底部: 左侧%2d | 右侧%2d | 并集%2d/%d" % (cov_l, cov_r, both, len(troughs)))
    cov_l2 = sum(1 for e in peaks if any(t >= max(0, e - LEFT_K) and t <= e for t in sell_days))
    cov_r2 = sum(1 for e in peaks if any(t >= e and t <= min(n, e + RIGHT_K) for t in rs_days))
    both_h = sum(
        1
        for e in peaks
        if any(t >= max(0, e - LEFT_K) and t <= e for t in sell_days)
        or any(t >= e and t <= min(n, e + RIGHT_K) for t in rs_days)
    )
    print("  顶部: 左侧%2d | 右侧%2d | 并集%2d/%d" % (cov_l2, cov_r2, both_h, len(peaks)))

    print("\n[5] 信号触发日清单（冷却 %d 日去重后）" % COOLDOWN)
    for name, days in [
        ("左侧底", buy_days), ("左侧顶", sell_days),
        ("右侧底", rb_days), ("右侧顶", rs_days),
    ]:
        print("  %s (%d次):" % (name, len(days)))
        for t in days:
            print("    %s fg=%5.1f z=%+.2f" % (str(dates[t])[:10], f[t], z[t]))


if __name__ == "__main__":
    main()
