import duckdb

NAMES = {
    "000993.SH": "中证全指信息技术", "000680.SH": "科创综指", "950162.CSI": "科创芯片设计",
    "931743.CSI": "半导体材料设备", "000688.SH": "科创50", "000698.SH": "科创100",
    "000985.SH": "中证全指", "000905.SH": "中证500", "000510.SH": "中证A500",
    "000300.SH": "沪深300", "399006.SZ": "创业板指", "000699.SH": "科创200",
    "930598.CSI": "中证新能源", "980022.SZ": "机器人产业", "399967.SZ": "中证军工",
    "000819.SH": "有色金属", "000991.SH": "机械", "000989.SH": "环保",
    "980028.SZ": "机器人100", "000015.SH": "上证红利", "H30269.CSI": "红利低波",
}

d = duckdb.connect("/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True)
latest = d.execute("SELECT MAX(trade_date) FROM a_stock_index_weight WHERE upper(index_code)='H30184.CSI'").fetchone()[0]
semi_set = {r[0] for r in d.execute(
    "SELECT DISTINCT con_code FROM a_stock_index_weight WHERE upper(index_code)='H30184.CSI' AND trade_date=?", (latest,)).fetchall()}
print(f"H30184.CSI 半导体 最新成分({latest}): {len(semi_set)} 只\n")

idx = d.execute(
    "SELECT DISTINCT index_code, MAX(trade_date) FROM a_stock_index_weight "
    "WHERE upper(index_code) != 'H30184.CSI' GROUP BY index_code").fetchall()
rows = []
for code, latest2 in idx:
    cons = {r[0] for r in d.execute(
        "SELECT DISTINCT con_code FROM a_stock_index_weight "
        "WHERE index_code=? AND trade_date=(SELECT MAX(trade_date) FROM a_stock_index_weight WHERE index_code=?)",
        (code, code)).fetchall()}
    ov = len(semi_set & cons)
    rows.append((code, NAMES.get(code, "?"), len(cons), ov, ov / len(cons) * 100, ov / len(semi_set) * 100))

print(f"{'指数':<12} {'名称':<14} {'成分数':>5} {'重叠':>4} {'占该指数%':>8} {'覆盖半导体%':>10}")
for code, name, n, ov, pct1, pct2 in sorted(rows, key=lambda r: -r[3])[:15]:
    print(f"{code:<12} {name:<14} {n:>5} {ov:>4} {pct1:>7.1f}% {pct2:>9.1f}%")
d.close()
