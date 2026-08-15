import sqlite3

import pandas as pd

path = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
fear = pd.read_sql_query(
    """
    SELECT symbol, date, score
    FROM etf_fear_greed_clone_history
    WHERE date >= '2023-03-22' AND symbol IN (
      '000015.SH','000688.SH','H30184.CSI','399975.SZ','399006.SZ','399967.SZ',
      '399989.SZ','930997.CSI','000905.SH','931775.CSI','000698.SH','980022.SZ',
      '399997.SZ','000932.SH','399998.SZ','000819.SH','H30269.CSI'
    )
    ORDER BY symbol, date
    """,
    conn,
    parse_dates=["date"],
)
conn.close()
pivot = fear.pivot_table(index="date", columns="symbol", values="score")
pivot = pivot.apply(pd.to_numeric, errors="coerce")

print("=== 各候选与 红利(000015.SH) / 科创50(000688.SH) 的恐贪相关性 ===")
names = {
    "000015.SH": "红利", "000688.SH": "科创50", "H30184.CSI": "半导体",
    "399975.SZ": "证券", "399006.SZ": "创业板", "399967.SZ": "军工",
    "399989.SZ": "医疗", "930997.CSI": "新能源车", "000905.SH": "中证500",
    "931775.CSI": "房地产", "000698.SH": "科创100", "980022.SZ": "机器人",
    "399997.SZ": "白酒", "000932.SH": "消费", "399998.SZ": "煤炭",
    "000819.SH": "有色", "H30269.CSI": "红利低波",
}
corr_red = pivot.corr()["000015.SH"]
corr_kc = pivot.corr()["000688.SH"]
rows = []
for idx in pivot.columns:
    if idx in ("000015.SH", "000688.SH"):
        continue
    rows.append((idx, names.get(idx, idx), corr_red[idx], corr_kc[idx]))

print(f"{'标的':<10}{'相关(红利)':>12}{'相关(科创50)':>14}{'平均绝对值':>12}")
for idx, name, cr, ck in sorted(rows, key=lambda r: (abs(r[2]) + abs(r[3]))):
    print(f"{name:<10}{cr:>12.3f}{ck:>14.3f}{((abs(cr)+abs(ck))/2):>12.3f}")
