#!/usr/bin/env python3
"""扩展趋势候选池实验：把更多低相关/高弹性行业指数加进趋势槽位，用 gap 选最强。

对比：3 槽位（科创50+半导体+纳指）1266.50% / -20.33% / 2.56。
"""

import math
import sys

import numpy as np

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp
import lab.trend_fill as tf

TRADE_ETF = "159509.SZ"

# 主策略（fear 只需要这三个）
fear_syms = ["000015.SH", "000688.SH", "QQQ.US"]
# 所有 ETF bars（主策略 3 个交易标的 + 趋势候选 ETF）
etf_list = ["510880.SH", "588000.SH", "512480.SH", TRADE_ETF, "QQQ.US",
            "588220.SH", "588230.SH", "159941.SZ", "512660.SH", "515030.SH",
            "515120.SH", "512170.SH", "512400.SH", "512880.SH", "515220.SH",
            "512800.SH", "159928.SZ", "512890.SH"]
fear_map, bars_map, trading_days = sp.load_data(fear_syms, etf_list)

base3 = [
    ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
    ("000688.SH", "588000.SH", "512480.SH", "半导体(科创信号)", 25.0, 1.6, 70.0),
    ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
]

# 趋势槽位候选（指数 → ETF → 名称）
ALL_SLOTS = [
    ("000688.SH", "588000.SH", "科创50"),
    ("000698.SH", "588220.SH", "科创100"),
    ("000699.SH", "588230.SH", "科创200"),
    ("H30184.CSI", "512480.SH", "半导体"),
    ("QQQ.US", TRADE_ETF, "纳指科技"),
    ("QQQ.US", "159941.SZ", "纳指"),
    ("399967.SZ", "512660.SH", "军工"),
    ("930997.CSI", "515030.SH", "新能源车"),
    ("931152.CSI", "515120.SH", "创新药"),
    ("399989.SZ", "512170.SH", "医疗"),
    ("000819.SH", "512400.SH", "有色"),
    ("399975.SZ", "512880.SH", "证券"),
    ("399998.SZ", "515220.SH", "煤炭"),
    ("399986.SZ", "512800.SH", "银行"),
    ("000932.SH", "159928.SZ", "消费"),
    ("H30269.CSI", "512890.SH", "红利低波"),
    ("000015.SH", "510880.SH", "红利"),
]

# 探测指数数据可用性
import duckdb

src = duckdb.connect("/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True)
codes = list(dict.fromkeys(ic for ic, _, _ in ALL_SLOTS if ic != "QQQ.US"))
placeholders = ",".join(f"'{c.upper()}'" for c in codes)
df = src.execute(
    f"SELECT ts_code, COUNT(*) n FROM a_stock_index_daily WHERE upper(ts_code) IN ({placeholders}) GROUP BY ts_code"
).fetchdf()
src.close()
have = set(df["ts_code"].str.upper())
print("指数数据可用性:")
for ic, te, nm in ALL_SLOTS:
    ok = ic == "QQQ.US" or ic.upper() in have
    b_ok = te in bars_map and len(bars_map[te]) > 100
    print(f"  {nm:8s} {ic:12s} 指数{'✓' if ok else '✗'} ETF{'✓' if b_ok else '✗'}")

available = [s for s in ALL_SLOTS if s[0] == "QQQ.US" or s[0].upper() in have]
available = [s for s in available if s[1] in bars_map and len(bars_map[s[1]]) > 100]
print(f"\n可用槽位 {len(available)} 个")

# 跑不同槽位组合
from lab.trend_fill import run_case, load_index_close

print("\n== 槽位组合对比（MA20）==")
combos = {
    "用户17池(科创50/100/200+半导体+纳指/科技+军工/新能源/创新药/医疗/有色/证券/煤炭/银行/消费/红利低波/红利)": available,
    "17池-防御(去银行/煤炭/红利低波/红利)": [s for s in available if s[2] not in ("银行", "煤炭", "红利低波", "红利")],
    "17池-红利": [s for s in available if s[2] != "红利"],
    "3槽(科创50+半导体+纳指)": [s for s in available if s[2] in ("科创50", "半导体", "纳指")],
    "17池-防御-纳指科技": [s for s in available if s[2] not in ("银行", "煤炭", "红利低波", "红利", "纳指科技")],
    "15池(17池-红利-纳指159941)": [s for s in available if s[2] not in ("红利", "纳指")],
    "15池-防御(再去银行/煤炭/红利低波)": [s for s in available if s[2] not in ("红利", "纳指", "银行", "煤炭", "红利低波")],
}
for name, slots in combos.items():
    if not slots:
        print(f"  {name}: 无可用槽位")
        continue
    r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=slots, ma_win=20)
    print(f"  {name:45s} {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")
    if name == "全16槽+gap门槛1%":
        for mg in (0.005, 0.01, 0.02, 0.03):
            rr = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=available, ma_win=20, min_gap=mg)
            print(f"    全16槽 gap>={mg*100:.1f}%: {rr['total']:7.2f}% / {rr['mdd']:6.2f}% / {rr['sharpe']:.2f} 买卖 {rr['buys']}/{rr['sells']}")
