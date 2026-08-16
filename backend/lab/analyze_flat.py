#!/usr/bin/env python3
"""分析原引擎（3标的最优参数）的空仓时间段 + 期间市场表现。

空仓 = 持仓 None 的日子。输出空仓段，并统计各指数在空仓段的区间涨跌，
看有没有明显的机会段可被低相关 ETF 覆盖。
"""

import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

import numpy as np

TRADE_ETF = "159509.SZ"
pairs = [
    ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
    ("000688.SH", "588000.SH", "512480.SH", "半导体(科创信号)", 25.0, 1.6, 70.0),
    ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
]

fear_map, bars_map, trading_days = sp.load_data(
    ["000015.SH", "000688.SH", "QQQ.US"],
    ["510880.SH", "588000.SH", "512480.SH", TRADE_ETF, "QQQ.US"],
)

# 复刻原引擎，记录持仓状态
cash = 1000000.0
position = None
last_close = {}
pair_by_etf = {p[2]: p for p in pairs}
hold_days = []  # (date, etf) 持仓中

for i in range(1, len(trading_days)):
    ed = trading_days[i]
    sd = trading_days[i - 1]
    for p in pairs:
        sub = bars_map[p[2]][bars_map[p[2]]["trade_date"] == ed]
        if not sub.empty:
            last_close[p[2]] = float(sub.iloc[0]["close"])
    sigs = {}
    for p in pairs:
        fi, ve, te, nm, b, v, g = p
        f = fear_map.get(fi, {}).get(sd)
        row = bars_map[ve]
        s2 = row[row["trade_date"] == sd]
        vr = float(s2["volume_ratio"].iloc[0]) if not s2.empty else np.nan
        sigs[te] = (f is not None and f <= b and np.isfinite(vr) and vr >= v, f)

    if position is None:
        cands = [p for p in pairs if sigs[p[2]][0]]
        if cands:
            t = min(cands, key=lambda p: sigs[p[2]][1])
            op = float(bars_map[t[2]][bars_map[t[2]]["trade_date"] == ed].iloc[0]["open"])
            qty = int(cash // op)
            cash -= qty * op
            position = (t[2], t[3], qty, qty * op)
    else:
        held_greed = pair_by_etf[position[0]][6]
        hf = sigs.get(position[0], (False, np.nan))[1]
        if hf is not None and np.isfinite(hf) and hf >= held_greed:
            op = float(bars_map[position[0]][bars_map[position[0]]["trade_date"] == ed].iloc[0]["open"])
            cash += position[2] * op
            position = None
        elif hf is not None and np.isfinite(hf) and hf > 45.0:
            others = [p for p in pairs if p[2] != position[0] and sigs[p[2]][0]]
            if others:
                op = float(bars_map[position[0]][bars_map[position[0]]["trade_date"] == ed].iloc[0]["open"])
                cash += position[2] * op
                position = None
                t = min(others, key=lambda p: sigs[p[2]][1])
                op2 = float(bars_map[t[2]][bars_map[t[2]]["trade_date"] == ed].iloc[0]["open"])
                qty = int(cash // op2)
                cash -= qty * op2
                position = (t[2], t[3], qty, qty * op2)
    hold_days.append((str(ed), position[0] if position else None))

# 空仓段
segments = []
start = None
prev = None
for d, etf in hold_days:
    if etf is None:
        if start is None:
            start = d
        prev = d
    else:
        if start is not None:
            segments.append((start, prev, etf))
            start = None
if start is not None:
    segments.append((start, prev, None))

total_days = len(hold_days)
flat_days = sum(1 for _, e in hold_days if e is None)
print(f"总交易日 {total_days}, 空仓天数 {flat_days} ({flat_days/total_days*100:.1f}%), 持仓天数 {total_days-flat_days}")
print(f"\n== 空仓段（共 {len(segments)} 段）==")
for s, e, nxt in segments:
    print(f"  {s} ~ {e}（{(s,e)}）→ 之后买入 {nxt}")

# 空仓段里市场表现：各指数/ETF 区间涨跌
print("\n== 各空仓段区间表现（%涨跌）==")
probe = {
    "沪深300": "000300.SH", "中证500": "000905.SH", "科创50": "000688.SH",
    "半导体": "H30184.CSI", "中证医疗": "399989.SZ", "创新药": "931152.CSI",
    "红利": "000015.SH", "军工": "399967.SZ", "煤炭": "399998.SZ",
    "银行": "399986.SZ", "证券": "399975.SZ", "新能源车": "930997.CSI",
    "白酒": "399997.SZ", "纳斯达克100": "QQQ.US", "中证国债": "H11006.CSI",
}
# 从生产 duckdb 读指数行情
import duckdb

src = duckdb.connect("/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True)
index_df = src.execute(
    "SELECT ts_code, trade_date, close FROM a_stock_index_daily "
    "WHERE trade_date BETWEEN '2023-01-01' AND '2026-08-14'"
).fetchdf()
src.close()
index_close = {}
for r in index_df.itertuples():
    index_close.setdefault(str(r.ts_code).upper(), {})[str(r.trade_date)] = float(r.close)

# QQQ 用 bars_map
qqq = bars_map["QQQ.US"]
qqq_close = {str(r["trade_date"]): float(r["close"]) for _, r in qqq.iterrows()}
# H11006 在 index_close 里

import bisect


def pct(series, d1, d2):
    dates = sorted(series)
    if not dates:
        return None
    i1 = bisect.bisect_right(dates, d1) - 1
    i2 = bisect.bisect_left(dates, d2)
    if i1 < 0 or i2 >= len(dates):
        return None
    c1 = series[dates[i1]]
    c2 = series[dates[i2]]
    return (c2 / c1 - 1) * 100


for s, e, nxt in segments:
    line = f"  {s}~{e} ({nxt}): "
    vals = []
    for name, code in probe.items():
        series = qqq_close if code == "QQQ.US" else index_close.get(code.upper(), {})
        p = pct(series, s, e)
        if p is not None:
            vals.append(f"{name}{p:+.1f}%")
    print(line + " ".join(vals))
