#!/usr/bin/env python3
"""贪恐>=70 的日子：log(成交量) 与 log_z 的分布（对比全样本与标准正态）。

配对：恐贪源 → 持仓交易标的（缩量用其 log_z）
  红利 000015.SH → 510880.SH
  科创50 000688.SH → 512480.SH（交易半导体）
  纳指 QQQ.US → 159941.SZ
  黄金 GOLD_SELF(518880 自算) → 518880.SH
"""

import math
import sys

import numpy as np
from scipy import stats

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf")

import lab.seesaw_pessimistic as sp
from lab.log_volume_signal import add_log_z, build_gold_fear

PAIRS = [
    ("000015.SH", "510880.SH", "红利"),
    ("000688.SH", "512480.SH", "科创50信号→半导体"),
    ("QQQ.US", "159941.SZ", "纳指"),
    ("GOLD_SELF", "518880.SH", "黄金"),
]


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def report(tag, x, quantiles):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    skew = stats.skew(x)
    kurt = stats.kurtosis(x, fisher=True)
    jb_p = stats.jarque_bera(x).pvalue
    print(f"    n={n} 偏度={skew:+.2f} 峰度(超额)={kurt:+.2f} JB_p={jb_p:.2e}")
    for q in quantiles:
        pct = np.mean(x <= q) * 100
        norm = stats.norm.cdf(q) * 100
        print(f"      P(x <= {q:+.2f}) = {pct:5.1f}%  (正态={norm:5.1f}%, 全样本={full_pct.get(q, float('nan')):.1f}%)")


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "512480.SH", "159941.SZ", "518880.SH", "588000.SH", "QQQ.US"],
    )
    bars_map = add_log_z(bars_map)
    fear_map["GOLD_SELF"] = build_gold_fear(bars_map)

    full_pct = {}
    quantiles = [-2.0, -1.5, -1.25, -1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0, 1.25, 1.5, 2.0]

    for fear_sym, trade_sym, label in PAIRS:
        fear = fear_map.get(fear_sym, {})
        df = bars_map[trade_sym].sort_values("trade_date").reset_index(drop=True)
        lz = df["log_z"].to_numpy(dtype=float)
        lv = np.log(df["volume"].replace(0, np.nan)).to_numpy(dtype=float)
        dates = df["trade_date"].to_numpy()

        # 全样本分位数（该标的 log_z）
        lz_all = lz[np.isfinite(lz)]
        for q in quantiles:
            full_pct[q] = np.mean(lz_all <= q) * 100

        # 贪恐>=70 的日子
        greedy_dates = [d for d, s in fear.items() if s >= 70.0]
        mask = np.isin(dates, greedy_dates)
        g_lz = lz[mask]
        g_lv = lv[mask]
        g_dates = dates[mask]

        print(f"\n{'='*70}")
        print(f"{label}：恐贪源 {fear_sym} >=70 的日子 {len(greedy_dates)} 个（交易标的 {trade_sym}）")
        print(f"  贪>=70 日期示例: {[str(d) for d in g_dates[:8]]}")

        print(f"\n  -- 贪>=70 子集 log_z 分布 --")
        report("greedy_logz", g_lz, quantiles)
        print(f"\n  -- 贪>=70 子集 log(volume) 原始分布（未标准化） --")
        gl = g_lv[np.isfinite(g_lv)]
        print(f"    n={len(gl)} 均值={np.mean(gl):.3f} 标准差={np.std(gl):.3f} "
              f"偏度={stats.skew(gl):+.2f} 峰度={stats.kurtosis(gl, fisher=True):+.2f} JB_p={stats.jarque_bera(gl).pvalue:.2e}")
        # 与全样本 log_z 分位数对比
        print(f"  全样本 log_z 分位数（对比）:")
        for q in (-1.25, -0.25, 0.0, 0.25, 1.25):
            print(f"      P(x <= {q:+.2f}) = {full_pct[q]:5.1f}%")
