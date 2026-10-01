"""离线重算 SPY 自算恐贪的 3 个价格类分量，与 CNN 分量对比。

只读：行情来自 DuckDB us_stock_daily，put/call 来自 SQLite etf_put_call_ratios，
打分方式与 etf_fear_greed_clone_service._score_etf_raw_signals 一致
（252 日滚动 z-score -> 正态 CDF -> 0-100）。
"""
import math
import sqlite3

import duckdb
import numpy as np
import pandas as pd

duck = duckdb.connect("/var/lib/quant_robot/analytics.duckdb", read_only=True)
spy = duck.execute(
    "SELECT trade_date, close FROM us_stock_daily WHERE symbol='SPY.US' ORDER BY trade_date"
).fetchall()
px = pd.DataFrame(spy, columns=["date", "close"]).set_index("date")["close"]
px.index = pd.to_datetime(px.index)

con = sqlite3.connect("file:/var/lib/quant_robot/evc_stocks.db?mode=ro", uri=True)
pc = pd.read_sql_query(
    "SELECT date, put_call_volume_ratio FROM etf_put_call_ratios WHERE symbol='SPY' ORDER BY date",
    con,
).set_index("date")["put_call_volume_ratio"]
pc.index = pd.to_datetime(pc.index)

# 原始信号
mom = px / px.rolling(125).mean() - 1.0
ret = px.pct_change()
rv = ret.rolling(20).std() * np.sqrt(252)
vol = -(rv / rv.rolling(50).mean() - 1.0)
pcr = -pc.reindex(px.index).ffill(limit=3).rolling(5, min_periods=3).mean()

raw = pd.DataFrame({"momo": mom, "vol": vol, "pcr": pcr})


def score(s, window=252, min_periods=120):
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std(ddof=0)
    z = (s - mean) / std.replace(0, np.nan)
    return 100.0 * z.map(lambda v: 0.5 * (1 + math.erf(v / math.sqrt(2))) if np.isfinite(v) else np.nan)


sc = raw.apply(score)
sc_str = {c: {str(i.date()): v for i, v in sc[c].items()} for c in sc.columns}

cnn = pd.read_sql_query(
    "SELECT date, index_value, market_momentum, market_volatility_vix, put_call_options "
    "FROM cnn_fear_greed_index ORDER BY date",
    con,
).set_index("date")
self_total = pd.read_sql_query(
    "SELECT date, score FROM etf_fear_greed_clone_history WHERE symbol='SPY.US' ORDER BY date",
    con,
).set_index("date")["score"]

print("=== 2025-10-15 ~ 2025-12-12 对比 ===")
print(f"{'date':11s} {'自总':>5s} {'CNN':>5s} | {'自momo':>6s} {'CNNmomo':>7s} | {'自vol':>6s} {'CNNvix':>6s} | {'自p/c':>6s} {'CNNp/c':>6s}")
for d in sorted(cnn.index):
    if not ("2025-10-15" <= d <= "2025-12-12"):
        continue
    f = lambda v: f"{v:6.1f}" if v is not None and not (isinstance(v, float) and math.isnan(v)) else "   ---"
    g = lambda c: sc_str[c].get(d)
    row = cnn.loc[d]
    print(f"{d} {f(self_total.get(d))} {f(row['index_value'])} | "
          f"{f(g('momo'))} {f(row['market_momentum'])} | "
          f"{f(g('vol'))} {f(row['market_volatility_vix'])} | "
          f"{f(g('pcr'))} {f(row['put_call_options'])}")

print()
print("=== 分量分数分布（2024-01 ~ 2026-08）===")
for col in ["momo", "vol", "pcr"]:
    s = sc[col].dropna()
    s = s[s.index >= "2024-01-01"]
    print(f"自算 {col}: min={s.min():.1f} p5={s.quantile(0.05):.1f} p25={s.quantile(0.25):.1f} "
          f"中位={s.median():.1f} p75={s.quantile(0.75):.1f} p95={s.quantile(0.95):.1f} max={s.max():.1f}")
cn = cnn.loc[cnn.index >= "2024-01-01"]
for col in ["market_momentum", "market_volatility_vix", "put_call_options"]:
    s = cn[col].dropna().astype(float)
    print(f"CNN {col}: min={s.min():.1f} p5={s.quantile(0.05):.1f} p25={s.quantile(0.25):.1f} "
          f"中位={s.median():.1f} p75={s.quantile(0.75):.1f} p95={s.quantile(0.95):.1f} max={s.max():.1f}")
