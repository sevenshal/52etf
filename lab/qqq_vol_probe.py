import sqlite3

import duckdb

# 1) QQQ.US 日线（含 volume）在哪张表
dq = duckdb.connect("/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True)
tables = dq.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_name LIKE '%stock%' OR table_name LIKE '%us%'"
).fetchall()
print("候选表:", [t[0] for t in tables])
for tbl in ("us_stock_daily", "us_stock_daily_qfq", "a_stock_market_daily_qfq"):
    try:
        df = dq.execute(
            f"SELECT symbol, COUNT(*) n, MIN(trade_date) mn, MAX(trade_date) mx, SUM(volume) vol "
            f"FROM {tbl} WHERE upper(symbol)='QQQ.US' GROUP BY symbol"
        ).fetchdf()
        print(tbl, df.to_dict("records"))
    except Exception as exc:
        print(tbl, "ERR:", str(exc)[:80])
dq.close()

# 2) CNN 贪恐数据（CNN*.US?）
conn = sqlite3.connect("file:/home/quantd/quant_prod/quant_robot/evc_stocks.db?mode=ro", uri=True)
rows = conn.execute(
    "SELECT symbol, COUNT(*), MIN(date), MAX(date) FROM etf_fear_greed_clone_history "
    "WHERE symbol LIKE 'CNN%' GROUP BY symbol"
).fetchall()
print("CNN 贪恐:", rows)
# CNN 最新值示例
rows2 = conn.execute(
    "SELECT date, score FROM etf_fear_greed_clone_history WHERE symbol LIKE 'CNN%' ORDER BY date DESC LIMIT 3"
).fetchall()
print("CNN 最新:", rows2)
conn.close()
