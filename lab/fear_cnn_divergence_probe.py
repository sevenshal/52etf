"""只读分析：CNN 官方恐贪 vs QQQ/SPY/SOXX 自算恐贪，谁的方向对。

用法：任意有 sqlite3 的 python3 直接跑，纯标准库，只读打开线上库。
"""
import sqlite3

import duckdb

SQLITE_PATH = "file:/var/lib/quant_robot/evc_stocks.db?mode=ro"
DUCKDB_PATH = "/var/lib/quant_robot/analytics.duckdb"

con = sqlite3.connect(SQLITE_PATH, uri=True)
cur = con.cursor()

duck = duckdb.connect(DUCKDB_PATH, read_only=True)


def load(symbol):
    cur.execute(
        "SELECT date, score, etf_close FROM etf_fear_greed_clone_history "
        "WHERE symbol=? ORDER BY date",
        (symbol,),
    )
    return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def load_close(symbol):
    rows = duck.execute(
        "SELECT trade_date, close FROM us_stock_daily WHERE symbol=? ORDER BY trade_date",
        (symbol,),
    ).fetchall()
    return {str(d): c for d, c in rows if c}


cnn = load("CNN*.US")


def corr(a, b):
    pairs = [
        (x, y)
        for x, y in zip(a, b)
        if x is not None and y is not None and x == x and y == y
    ]
    n = len(pairs)
    if n < 2:
        return n, float("nan")
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return n, float("nan")
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    return n, cov / ((vx * vy) ** 0.5)


COMP_COLS = [
    "market_momentum_score",
    "stock_price_strength_score",
    "stock_price_breadth_score",
    "put_call_options_score",
    "market_volatility_score",
    "safe_haven_demand_score",
    "junk_bond_demand_score",
]

for sym in ["SPY.US", "QQQ.US", "SOXX.US"]:
    d = load(sym)
    common = sorted(set(cnn) & set(d))
    n, c = corr([cnn[x][0] for x in common], [d[x][0] for x in common])

    closes = {x: c for x, c in load_close(sym).items() if x in d and x in cnn}
    cdates = sorted(closes)
    rets, self_chg, cnn_chg = [], [], []
    for i in range(1, len(cdates)):
        prev, cur_ = cdates[i - 1], cdates[i]
        r = closes[cur_] / closes[prev] - 1
        rets.append(r)
        self_chg.append(d[cur_][0] - d[prev][0])
        cnn_chg.append(cnn[cur_][0] - cnn[prev][0])

    _, cr_self = corr(rets, self_chg)
    _, cr_cnn = corr(rets, cnn_chg)
    denom = sum(1 for a, b in zip(self_chg, cnn_chg) if a * b != 0)
    same = sum(1 for a, b in zip(self_chg, cnn_chg) if a * b > 0) / denom

    comp_data = {col: {} for col in COMP_COLS}
    cur.execute(
        "SELECT date, " + ", ".join(COMP_COLS) +
        " FROM etf_fear_greed_clone_history WHERE symbol=? ORDER BY date", (sym,))
    for row in cur.fetchall():
        for i, col in enumerate(COMP_COLS):
            comp_data[col][row[0]] = row[1 + i]

    print(f"== {sym} ==")
    print(f"与CNN水平相关: n={n} r={c:.3f}")
    print(f"自算Δ vs 当日收益 r={cr_self:.3f} | CNNΔ vs 当日收益 r={cr_cnn:.3f} | 日变化同向率={same:.2%}")
    for col in COMP_COLS:
        cd = comp_data[col]
        cdates2 = sorted(set(cd) & set(closes))
        chgs, rs = [], []
        for i in range(1, len(cdates2)):
            prev, cur2 = cdates2[i - 1], cdates2[i]
            if cd[prev] is None or cd[cur2] is None:
                continue
            chgs.append(cd[cur2] - cd[prev])
            rs.append(closes[cur2] / closes[prev] - 1)
        if len(chgs) > 30:
            _, r_c = corr(rs, chgs)
            print(f"  分量 {col:32s} Δscore vs 当日收益 r={r_c:+.3f}")

    diffs = sorted(common, key=lambda x: -abs(cnn[x][0] - d[x][0]))[:6]
    print("水平差距最大日 (日期, CNN, 自算, close):")
    for x in diffs:
        print(f"  {x}  CNN={cnn[x][0]:6.1f}  自算={d[x][0]:6.1f}  close={d[x][1]}")
    print()
