"""板块/主题/行业类恐贪指数成分股对全A股市值分层的覆盖度统计（只读）。

口径：排除宽基(沪深300/中证A500/中证500/中证全指)、因子(上证红利/红利低波)、
自建(A创100)，纳入板块(科创50/100/200、创业板指、北证50) + 全部行业/主题指数。
"""
import sqlite3
import time

import duckdb
import tushare as ts

DUCKDB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
SQLITE = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"

BROAD = {
    "000300.SH": "沪深300",
    "000510.SH": "中证A500",
    "000905.SH": "中证500",
    "000985.SH": "中证全指",
}
BOARD = {
    "000688.SH": "科创50",
    "000698.SH": "科创100",
    "000699.SH": "科创200",
    "399006.SZ": "创业板指",
    "899050.BJ": "北证50",
}
FACTOR = {
    "000015.SH": "上证红利",
    "H30269.CSI": "红利低波",
}
CUSTOM = {"INNO100.CN": "A创100"}

# 本次新加的 4 个指数（尚未同步进生产 DuckDB，用 Tushare 现取权重）
NEW = {
    "000994.CSI": "全指通信服务",
    "399995.SZ": "基建工程",
    "930618.CSI": "中证保险",
    "H30588.CSI": "中证证保",
}


def connect_duckdb():
    for i in range(12):
        try:
            return duckdb.connect(DUCKDB, read_only=True)
        except Exception as exc:
            if i == 11:
                raise
            time.sleep(3)


def load_prod_symbols():
    db = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT DISTINCT symbol FROM etf_fear_greed_clone_history ORDER BY symbol"
    ).fetchall()
    db.close()
    return [
        r[0] for r in rows
        if r[0].endswith((".SH", ".SZ", ".BJ", ".CSI", ".CN"))
        and not r[0].startswith(("CNN", "SPY", "QQQ", "DIA", "SOXX", "HSI", "HSCEI", "HSTECH"))
    ]


def main():
    con = connect_duckdb()

    latest = con.execute("SELECT MAX(trade_date) FROM a_stock_market_daily_qfq").fetchone()[0]
    mv_rows = con.execute(
        "SELECT ts_code, total_mv FROM a_stock_market_daily_qfq WHERE trade_date = ?",
        [latest],
    ).fetchall()
    mv = {r[0]: r[1] for r in mv_rows if r[1]}
    basic = con.execute("SELECT ts_code, name, list_status FROM a_stock_basic").fetchall()
    name = {r[0]: r[1] for r in basic}
    listed = {r[0] for r in basic if r[2] == "L"}
    universe = sorted([c for c in listed if c in mv], key=lambda c: -mv[c])
    rank = {c: i + 1 for i, c in enumerate(universe)}

    # 生产 DuckDB 里各指数最新权重快照
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
    con.close()

    prod_symbols = load_prod_symbols()

    # 新加的 4 个指数：从 Tushare 取最新权重
    db = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    token = db.execute("SELECT api_token FROM tushare_account_configs WHERE id=1").fetchone()[0]
    db.close()
    pro = ts.pro_api(token)
    new_by_index = {}
    for code in NEW:
        try:
            df = pro.index_weight(index_code=code, start_date="20250101", end_date="20261231")
            if df is not None and len(df):
                latest_w = df["trade_date"].max()
                cons = set(df[df["trade_date"] == latest_w]["con_code"])
                new_by_index[code] = cons
                print(f"Tushare {code} {NEW[code]}: 最新权重 {latest_w}, 成分 {len(cons)} 只")
            else:
                print(f"Tushare {code}: 无权重数据")
        except Exception as e:
            print(f"Tushare {code}: ERR {str(e)[:80]}")

    exclude = set(BROAD) | set(FACTOR) | set(CUSTOM)
    current_included = [s for s in prod_symbols if s not in exclude]  # 板块+主题+行业（现生产）
    current_included.sort()
    with_new_included = sorted(set(current_included) | set(NEW))      # 加上 4 个新指数

    def coverage(included, new_index_cons):
        union = set()
        for s in included:
            cons = set(by_index.get(s, []))
            if s in new_index_cons:
                cons = new_index_cons[s]
            union |= cons
        union &= set(universe)
        total_mv = sum(mv[c] for c in universe)
        rows = []
        for k in (100, 500, 1000, 2000):
            topk = set(universe[:k])
            hit = union & topk
            mv_hit = sum(mv[c] for c in hit)
            mv_topk = sum(mv[c] for c in topk)
            rows.append((k, len(hit), len(hit) / k * 100.0, mv_hit / mv_topk * 100.0))
        union_mv = sum(mv[c] for c in union)
        return len(union), len(universe), union_mv / total_mv * 100.0, rows

    for title, included, new_cons in (
        ("现生产（板块+主题+行业，49 指数）", current_included, {}),
        ("加上 4 个新指数（53 指数）", with_new_included, new_by_index),
    ):
        n, n_all, mv_pct, rows = coverage(included, new_cons)
        print(f"\n=== {title} ===")
        print(f"成分股并集: {n} 只 / 全A有市值 {n_all} 只 = {n/n_all*100:.1f}%（市值覆盖率 {mv_pct:.1f}%）")
        print(f"{'top':>7} {'覆盖只数':>8} {'只数覆盖%':>10} {'市值覆盖%':>10}")
        for k, hit, pct, mvk in rows:
            print(f"top{k:<5} {hit:>8} {pct:>9.1f}% {mvk:>9.1f}%")

    # top100 漏掉的（现生产口径）
    print("\n=== 现生产口径下 top100 漏掉的大市值股 ===")
    union_now = set()
    for s in current_included:
        union_now |= set(by_index.get(s, []))
    union_now &= set(universe)
    top100 = set(universe[:100])
    miss = sorted(top100 - union_now, key=lambda c: -mv[c])
    print(f"top100 漏掉 {len(miss)} 只：")
    for c in miss:
        print(f"  {c} {name.get(c,''):8s} 排名={rank[c]}")


if __name__ == "__main__":
    main()
