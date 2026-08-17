"""49 个行业/主题指数（不含板块/宽基/因子/自建）成分股覆盖度统计。

按用户给定的明确清单映射到指数代码，从生产 DuckDB 取最新权重快照，
统计其成分股并集对全 A 股 top100/500/1000/2000 市值的覆盖。
"""
import time

import duckdb

DUCKDB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"

# 基础行业主题（13）
BASIC = {
    "399975.SZ": "证券", "H30184.CSI": "半导体", "950162.CSI": "芯片设计",
    "931743.CSI": "半导体材料设备", "980022.SZ": "机器人", "399997.SZ": "白酒",
    "399989.SZ": "医疗", "000819.SH": "有色", "399967.SZ": "军工",
    "930997.CSI": "新能源车", "000932.SH": "消费", "399986.SZ": "银行",
    "399998.SZ": "煤炭",
}
# 一级行业（7）
L1 = {
    "000987.SH": "原材料", "000989.SH": "可选消费", "000991.SH": "医药卫生",
    "000993.SH": "信息技术", "000994.CSI": "通信服务", "000995.CSI": "公用事业",
    "931775.CSI": "房地产",
}
# 二级行业（24）
L2 = {
    "H30199.CSI": "电力", "931994.CSI": "电网设备", "H30198.CSI": "石油石化",
    "930606.CSI": "钢铁", "000813.CSI": "化工", "931009.CSI": "建材",
    "931752.CSI": "工程机械", "399995.SZ": "基建", "H30171.CSI": "运输物流",
    "980028.SZ": "家电", "399971.SZ": "文化传媒", "930633.CSI": "旅游酒店",
    "000933.SH": "综合医药", "930641.CSI": "中药", "931152.CSI": "创新药",
    "930726.CSI": "生物医药", "H30217.CSI": "医疗器械", "931160.CSI": "通信设备",
    "H30202.CSI": "软件服务", "930651.CSI": "电脑设备", "000949.CSI": "农业",
    "930707.CSI": "畜牧养殖", "930618.CSI": "保险", "H30588.CSI": "证券保险",
}
# 三级细分（5）
L3 = {
    "931151.CSI": "光伏", "930598.CSI": "稀土", "930901.CSI": "动漫游戏",
    "930851.CSI": "云计算", "931230.CSI": "汽车零部件",
}

ALL = {**BASIC, **L1, **L2, **L3}


def connect_duckdb():
    for i in range(15):
        try:
            return duckdb.connect(DUCKDB, read_only=True)
        except Exception:
            if i == 14:
                raise
            time.sleep(3)


def main():
    con = connect_duckdb()
    latest = con.execute("SELECT MAX(trade_date) FROM a_stock_market_daily_qfq").fetchone()[0]
    mv_rows = con.execute(
        "SELECT ts_code, total_mv FROM a_stock_market_daily_qfq WHERE trade_date = ?", [latest]
    ).fetchall()
    mv = {r[0]: r[1] for r in mv_rows if r[1]}
    listed = {r[0] for r in con.execute("SELECT ts_code FROM a_stock_basic WHERE list_status='L'").fetchall()}
    universe = sorted([c for c in listed if c in mv], key=lambda c: -mv[c])

    wrows = con.execute(
        """
        SELECT index_code, con_code, weight
        FROM a_stock_index_weight
        WHERE (index_code, trade_date) IN (
            SELECT index_code, MAX(trade_date) FROM a_stock_index_weight GROUP BY index_code
        )
        """
    ).fetchall()
    con.close()

    by_index = {}
    for idx, cc, w in wrows:
        if w and w > 0:
            by_index.setdefault(idx, []).append(cc)

    print(f"清单指数数: {len(ALL)}（基础{len(BASIC)}+一级{len(L1)}+二级{len(L2)}+三级{len(L3)}）")
    print(f"行情日期: {latest}  全A有市值: {len(universe)}")

    missing = [c for c in ALL if not by_index.get(c)]
    if missing:
        print("⚠ 无权重快照的指数:", {c: ALL[c] for c in missing})

    union = set()
    per = {}
    for code, label in ALL.items():
        cons = set(by_index.get(code, [])) & set(universe)
        per[code] = cons
        union |= cons

    total_mv = sum(mv[c] for c in universe)
    union_mv = sum(mv[c] for c in union)
    print(f"\n成分股并集: {len(union)} 只（全A {len(universe)} 只，占 {len(union)/len(universe)*100:.1f}%）")
    print(f"并集市值覆盖: {union_mv/total_mv*100:.1f}%")

    print(f"\n{'阈值':>8} {'覆盖只数':>9} {'只数覆盖率':>10} {'市值覆盖率':>10}")
    for k in (100, 500, 1000, 2000):
        topk = set(universe[:k])
        hit = union & topk
        print(f"top{k:<5} {len(hit):>7}/{k:<4} {len(hit)/k*100:>9.1f}% {sum(mv[c] for c in hit)/sum(mv[c] for c in topk)*100:>9.1f}%")

    # 各指数贡献的"新"股票数（按 group 顺序累计）
    print("\n=== 各指数去重后新增覆盖（按清单顺序累计） ===")
    seen = set()
    for code, label in ALL.items():
        new = per[code] - seen
        seen |= per[code]
        print(f"  {code:12s} {label:8s} 成分 {len(per[code]):4d}  新增 {len(new):4d}")


if __name__ == "__main__":
    main()
