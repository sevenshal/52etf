#!/usr/bin/env python3
"""分析 log(成交量) 与 log_z 的分布特征（正态性 + 阈值实际分位数）。

检验对象：
1. log_vol = log(volume) 本身的分布（偏度/峰度/QQ）
2. log_z = (log_vol - rolling20均值) / rolling20标准差 的经验分布
   对比标准正态：P(Z<=-0.25)=40.1%, P(Z<=-0.5)=30.9%, P(Z<=-1.25)=10.6%, P(Z>=1.25)=10.6%
"""

import math
import sys

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf")

import lab.seesaw_pessimistic as sp
from lab.log_volume_signal import add_log_z

SYMBOLS = ["510880.SH", "588000.SH", "512480.SH", "159941.SZ", "518880.SH", "QQQ.US"]


def distribution_stats(name, x, quantiles=None):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    skew = stats.skew(x)
    kurt = stats.kurtosis(x, fisher=True)  # 超额峰度, 正态=0
    jb_stat, jb_p = stats.jarque_bera(x)
    sw_stat, sw_p = stats.shapiro(x[:2000])
    print(f"  [{name}] n={n} 偏度={skew:+.2f} 峰度(超额)={kurt:+.2f} JB_p={jb_p:.2e} SW_p={sw_p:.2e}")
    if quantiles:
        for q in quantiles:
            pct = np.mean(x <= q) * 100
            norm_pct = stats.norm.cdf(q) * 100
            print(f"    P(x <= {q:+.2f}) = {pct:5.1f}%  (正态={norm_pct:5.1f}%)")
    return skew, kurt


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(["000015.SH"], SYMBOLS)
    bars_map = add_log_z(bars_map)

    quantiles = [-2.0, -1.5, -1.25, -1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0, 1.25, 1.5, 2.0]

    print("=== log_vol 分布（原始 log 成交量，2013-03 起）===")
    for sym in SYMBOLS:
        df = bars_map[sym].sort_values("trade_date").reset_index(drop=True)
        lv = np.log(df["volume"].replace(0, np.nan))
        lv = lv[np.isfinite(lv)]
        print(f"\n{'-'*60}\n{sym} log(volume):")
        distribution_stats("log_vol", lv)

    print("\n\n=== log_z 分布（(log_vol - 前20日均) / 前20日std，即信号实际使用的量）===")
    for idx, sym in enumerate(SYMBOLS):
        df = bars_map[sym].sort_values("trade_date").reset_index(drop=True)
        lz = df["log_z"].dropna()
        print(f"\n{'-'*60}\n{sym} log_z (n={len(lz)}):")
        distribution_stats("log_z", lz, quantiles=quantiles)

        # Q-Q 对照：实际分位数 vs 正态理论分位数（关键点）
        print("    Q-Q 对照（实际 vs 正态）:")
        for p in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
            act = float(np.quantile(lz, p))
            theo = float(stats.norm.ppf(p))
            print(f"      p={p:.2f}: 实际={act:+.2f} 正态={theo:+.2f} 差={act - theo:+.2f}")
