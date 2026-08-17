"""生产环境 A股恐贪指数成分股覆盖度分析（只读）。

口径：
- 生产 SQLite 里实际有恐贪历史的 A 股指数。
- 按是否「行业相关」分类，测算成分股并集对全 A 股市值 top-N 的覆盖。
"""
import sqlite3

import duckdb

DUCKDB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
SQLITE = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"

BROAD = {  # 宽基 / 行业无关（用户点名 + 中证A500）
    "000300.SH": "沪深300",
    "000510.SH": "中证A500",
    "000905.SH": "中证500",
    "000985.SH": "中证全指",
}
BOARD = {  # 板块宽基（覆盖全行业，非行业主题）
    "000688.SH": "科创50",
    "000698.SH": "科创100",
    "000699.SH": "科创200",
    "399006.SZ": "创业板指",
    "899050.BJ": "北证50",
}
FACTOR = {  # 因子/策略（非行业）
    "000015.SH": "上证红利",
    "H30269.CSI": "红利低波",
}
CUSTOM = {  # 自建创新指数（非行业）
    "INNO100.CN": "A创100",
}


def wanyi(x):
    return f"{x / 1e8:.2f}万亿"


def yi(x):
    return f"{x / 1e4:.0f}亿"


def load_fg_symbols():
    db = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT DISTINCT symbol FROM etf_fear_greed_clone_history ORDER BY symbol"
    ).fetchall()
    db.close()
    return [r[0] for r in rows]


def main():
    con = duckdb.connect(DUCKDB, read_only=True)

    latest = con.execute(
        "SELECT MAX(trade_date) FROM a_stock_market_daily_qfq"
    ).fetchone()[0]

    mv_rows = con.execute(
        "SELECT ts_code, total_mv FROM a_stock_market_daily_qfq WHERE trade_date = ?",
        [latest],
    ).fetchall()
    mv = {r[0]: r[1] for r in mv_rows if r[1]}

    basic = con.execute(
        "SELECT ts_code, name, list_status FROM a_stock_basic"
    ).fetchall()
    name = {r[0]: r[1] for r in basic}
    listed = {r[0] for r in basic if r[2] == "L"}

    universe = sorted([c for c in listed if c in mv], key=lambda c: -mv[c])
    total_mv_all = sum(mv[c] for c in universe)
    rank_of = {c: i + 1 for i, c in enumerate(universe)}

    fg = load_fg_symbols()
    a_syms = [
        s for s in fg
        if s.endswith((".SH", ".SZ", ".BJ", ".CSI", ".CN"))
        and not s.startswith(("CNN", "SPY", "QQQ", "DIA", "SOXX", "HSI", "HSCEI", "HSTECH"))
    ]

    weight_rows = con.execute(
        """
        SELECT index_code, con_code, weight
        FROM a_stock_index_weight
        WHERE (index_code, trade_date) IN (
            SELECT index_code, MAX(trade_date) FROM a_stock_index_weight GROUP BY index_code
        )
        """
    ).fetchall()
    by_index = {}
    for idx, con_code, w in weight_rows:
        if w and w > 0:
            by_index.setdefault(idx, []).append(con_code)

    # INNO100 成分在 SQLite 表，不在 DuckDB 权重表
    inno = set()
    try:
        db = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
        inno = {
            r[0] for r in db.execute(
                "SELECT DISTINCT ts_code FROM a_stock_innovation100_constituents"
            ).fetchall()
        }
        db.close()
    except Exception as e:
        print("INNO100 读取失败:", e)

    print(f"行情日期: {latest}   全A已上市: {len(listed)}  有市值: {len(universe)}")
    print(f"全A总市值: {wanyi(total_mv_all)}")
    print(f"生产恐贪 A股指数数: {len(a_syms)}  (有权重/成分快照: {len(by_index)} + INNO100)\n")

    def report(title, included):
        union = set()
        for s in included:
            cons = by_index.get(s) or (inno if s == "INNO100.CN" else set())
            union |= set(cons)
        union &= set(universe)
        N = len(union)
        topN = set(universe[:N])
        overlap = union & topN
        miss = topN - union
        extra = union - topN
        mv_union = sum(mv[c] for c in union)
        mv_topN = sum(mv[c] for c in topN)
        print(f"=== {title} ===")
        print(f"  指数数: {len(included)}  成分股并集(去重): {N}")
        print(f"  相对全A股数覆盖: {N}/{len(universe)} = {N/len(universe)*100:.1f}%")
        print(f"  相对全A总市值覆盖: {wanyi(mv_union)} / {wanyi(total_mv_all)} = {mv_union/total_mv_all*100:.1f}%")
        print(f"  top-{N} 市值覆盖: {len(overlap)}/{N} = {len(overlap)/N*100:.1f}%")
        print(f"  漏掉 top-{N} 内大市值: {len(miss)} 只; 并集多出 top-{N} 外小市值: {len(extra)} 只")
        # 漏掉的大市值按排名分桶
        buckets = {}
        for c in miss:
            r = rank_of[c]
            b = "1-50" if r <= 50 else "51-200" if r <= 200 else "201-500" if r <= 500 else "501-1000" if r <= 1000 else "1001-"+str(N)
            buckets[b] = buckets.get(b, 0) + 1
        print(f"  漏掉的 {len(miss)} 只市值排名分布: {dict(sorted(buckets.items(), key=lambda x: list(['1-50','51-200','201-500','501-1000']).index(x[0]) if x[0] in ['1-50','51-200','201-500','501-1000'] else 99))}")
        return union, N, miss

    # 口径1：行业/主题（排除宽基+板块+因子+自建）
    industry = [s for s in a_syms if s not in set(BROAD) | set(BOARD) | set(FACTOR) | set(CUSTOM)]
    union1, N1, miss1 = report("口径1：仅行业/主题指数（44个，排除宽基/板块/因子/自建）", industry)

    # 口径2：所有非宽基（排除4个宽基，含板块/因子/自建）
    non_broad = [s for s in a_syms if s not in BROAD]
    union2, N2, miss2 = report("口径2：所有非宽基指数（排除沪深300/中证A500/中证500/中证全指）", non_broad)

    # 口径3：全部 A 股恐贪指数
    union3, N3, miss3 = report("口径3：全部 A 股恐贪指数（含宽基）", a_syms)

    # 漏掉的最大市值股票（口径1）
    miss_sorted = sorted(miss1, key=lambda c: -mv[c])
    print(f"\n口径1 漏掉的 top-{N1} 里市值最大的 25 只:")
    for c in miss_sorted[:25]:
        print(f"  {c} {name.get(c,''):8s} 市值={yi(mv[c]):>8s} 排名={rank_of[c]}")

    con.close()


if __name__ == "__main__":
    main()
