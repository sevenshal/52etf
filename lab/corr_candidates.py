"""候选 ETF 与现有标的（红利/科创50/半导体/纳指）日收益相关性分析。"""

import numpy as np
import pandas as pd

import lab.seesaw_pessimistic as sp

EXISTING = ["510880.SH", "588000.SH", "512480.SH", "159941.SZ", "159509.SZ", "QQQ.US"]
CANDS = [
    "518880.SH",  # 黄金
    "511010.SH",  # 国债
    "511260.SH",  # 国开债
    "511990.SH",  # 货币
    "159915.SZ",  # 创业板
    "510300.SH",  # 沪深300
    "510500.SH",  # 中证500
    "510050.SH",  # 上证50
    "512010.SH",  # 医药
    "159928.SZ",  # 消费
    "512660.SH",  # 军工
    "512000.SH",  # 券商
    "512800.SH",  # 银行
    "512690.SH",  # 酒
    "515030.SH",  # 新能源车
    "515790.SH",  # 光伏
    "512200.SH",  # 房地产
    "159920.SZ",  # 恒生
    "513100.SH",  # 纳指513100
]
bars_map = sp.load_data(["000015.SH"], CANDS + EXISTING)[1]

rets = {}
for sym, df in bars_map.items():
    s = df.set_index("trade_date")["close"].sort_index()
    rets[sym] = s.pct_change()

frame = pd.DataFrame(rets)
# 只算回测区间
frame.index = pd.to_datetime(frame.index)
frame = frame.loc[frame.index >= pd.Timestamp("2023-03-22")]

print("=== 候选 vs 现有标的 日收益相关系数（2023-03-22 ~ 2026-08-14）===")
for c in CANDS:
    row = [f"{c:>9}"]
    for e in EXISTING:
        if e in frame.columns:
            corr = frame[c].corr(frame[e])
            row.append(f"{e.replace('.SH','').replace('.SZ','')}:{corr:+.2f}")
        else:
            row.append(f"{e}:N/A")
    print(" ".join(row))

# 与所有现有标的相关系数绝对值的最大值（低=独立）
print("\n=== 与现有标的最大相关（绝对值）排序 ===")
res = []
for c in CANDS:
    cs = []
    for e in EXISTING:
        if e in frame.columns and frame[c].notna().sum() > 30:
            cs.append(abs(frame[c].corr(frame[e])))
    if cs:
        res.append((c, max(cs), np.mean(cs)))
for c, mx, mean in sorted(res, key=lambda x: x[1]):
    print(f"  {c:>9} 最大|ρ|={mx:.2f} 平均|ρ|={mean:.2f}")
