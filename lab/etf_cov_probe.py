import duckdb

conn = duckdb.connect(
    "/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True
)
candidates = [
    ("950162.CSI", "588780.SH", "芯片设计"),
    ("H30184.CSI", "512480.SH", "半导体"),
    ("931743.CSI", "159516.SZ", "半导体设备"),
    ("000699.SH", "588230.SH", "科创200"),
    ("000688.SH", "588000.SH", "科创50"),
    ("000698.SH", "588220.SH", "科创100"),
    ("980022.SZ", "159530.SZ", "机器人"),
    ("399975.SZ", "512880.SH", "证券"),
    ("399006.SZ", "159915.SZ", "创业板"),
    ("399967.SZ", "512660.SH", "军工"),
    ("930997.CSI", "515030.SH", "新能源车"),
    ("399989.SZ", "512170.SH", "医疗"),
    ("000905.SH", "510500.SH", "中证500"),
    ("931775.CSI", "512200.SH", "房地产"),
    ("000015.SH", "510880.SH", "红利(主)"),
]
etfs = [c[1] for c in candidates]
df = conn.execute(
    """
    SELECT upper(symbol) AS symbol, COUNT(*) AS n, MIN(trade_date) AS mn, MAX(trade_date) AS mx
    FROM a_stock_fund_daily_qfq
    WHERE upper(symbol) IN (SELECT * FROM unnest(?))
    GROUP BY symbol ORDER BY symbol
    """,
    [etfs],
).fetch_df()
conn.close()
print(f"{'指数':<14}{'ETF':<12}{'名称':<8}{'日线数':>7}  区间")
for idx, etf, name in candidates:
    row = df[df["symbol"] == etf]
    if row.empty:
        print(f"{idx:<14}{etf:<12}{name:<8}{'无数据':>7}")
    else:
        r = row.iloc[0]
        print(f"{idx:<14}{etf:<12}{name:<8}{int(r['n']):>7}  {r['mn']} ~ {r['mx']}")
