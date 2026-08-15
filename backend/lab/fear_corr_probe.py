import sqlite3

import numpy as np
import pandas as pd

path = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
fear = pd.read_sql_query(
    """
    SELECT symbol, date, score
    FROM etf_fear_greed_clone_history
    WHERE date >= '2023-03-22'
    ORDER BY symbol, date
    """,
    conn,
    parse_dates=["date"],
)
conn.close()

pivot = fear.pivot_table(index="date", columns="symbol", values="score")
pivot = pivot.apply(pd.to_numeric, errors="coerce")
print("指数数量:", len(pivot.columns))
print("日期范围:", pivot.index.min().date(), "~", pivot.index.max().date(), "共", len(pivot), "天")
print("\n各指数数据点:")
for col in pivot.columns:
    print(f"  {col}: {int(pivot[col].notna().sum())}")

# 与上证红利的相关性
base = "000015.SH"
print(f"\n=== 与 {base}（上证红利）的恐贪相关性（跷跷板 = 相关性低/负）===")
corr = pivot.corr()[base].drop(labels=[base]).sort_values()
for idx, val in corr.items():
    print(f"  {idx}: {val:+.3f}")

# 只保留数据覆盖 >= 800 天的指数，输出候选
enough = [c for c in pivot.columns if pivot[c].notna().sum() >= 800]
print(f"\n数据覆盖>=800天的指数: {enough}")
