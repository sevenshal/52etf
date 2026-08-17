import sqlite3

import duckdb
import pandas as pd

# 1) QQQ.US 恐贪覆盖
conn = sqlite3.connect("file:/home/quantd/quant_prod/quant_robot/evc_stocks.db?mode=ro", uri=True)
fear = pd.read_sql_query(
    "SELECT symbol, date, score FROM etf_fear_greed_clone_history WHERE symbol='QQQ.US' ORDER BY date",
    conn, parse_dates=["date"],
)
conn.close()
print("QQQ.US 恐贪:", len(fear), fear.iloc[0]['date'].date(), "~", fear.iloc[-1]['date'].date())

# 2) 纳指 ETF 日线覆盖
dq = duckdb.connect("/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True)
df = dq.execute(
    """
    SELECT upper(symbol) AS symbol, COUNT(*) AS n, MIN(trade_date) AS mn, MAX(trade_date) AS mx
    FROM a_stock_fund_daily_qfq
    WHERE upper(symbol) IN ('159941.SZ','513100.SH','513500.SH','513300.SH','159660.SZ','161130.SZ')
    GROUP BY symbol ORDER BY symbol
    """
).fetch_df()
dq.close()
print(df.to_string(index=False))

# 3) 相关性：红利/科创50/QQQ
conn = sqlite3.connect("file:/home/quantd/quant_prod/quant_robot/evc_stocks.db?mode=ro", uri=True)
fear2 = pd.read_sql_query(
    "SELECT symbol, date, score FROM etf_fear_greed_clone_history "
    "WHERE date >= '2023-03-22' AND symbol IN ('000015.SH','000688.SH','QQQ.US') ORDER BY symbol, date",
    conn, parse_dates=["date"],
)
conn.close()
pivot = fear2.pivot_table(index="date", columns="symbol", values="score").apply(pd.to_numeric, errors="coerce")
print("\n相关性矩阵:")
print(pivot.corr().round(3).to_string())
