import datetime
import sqlite3

conn = sqlite3.connect("file:/home/quantd/quant_prod/quant_robot/evc_stocks.db?mode=ro", uri=True)
rows = conn.execute(
    "SELECT date, score FROM etf_fear_greed_clone_history WHERE symbol='QQQ.US' ORDER BY date"
).fetchall()
conn.close()
print("总条数:", len(rows))
weekends = [r for r in rows if datetime.date.fromisoformat(r[0]).weekday() >= 5]
print("周六日记录数:", len(weekends))
for r in weekends[:8]:
    print("  ", r)
july4 = [r for r in rows if r[0].startswith("2024-07")][:10]
print("2024-07 前10条（7/4美国独立日休市）:")
for r in july4:
    print("  ", r)
aug = [r[0] for r in rows if "2024-08-0" in r[0]]
print("2024-08-0x（8/5周一股灾）:", aug)
