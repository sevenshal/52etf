#!/usr/bin/env python3
"""半导体单独候补：细网格 + 触发日分析。"""

from __future__ import annotations

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf")

from lab.seesaw_backtest import POOL, load_data, run_backtest

fear_map, bars_map, trading_days = load_data()
semi = [item for item in POOL if item[0] == "H30184.CSI"]

red = run_backtest(fear_map, bars_map, trading_days, pool=[], buy_threshold=30.0, vr_threshold=1.6)
print(f"纯红利基准: {red['total_return_pct']:.2f}% 夏普 {red['sharpe']:.2f} 回撤 {red['max_drawdown_pct']:.2f}% 买卖 {red['buy_count']}/{red['sell_count']}")

print("\n=== 半导体单独候补 细网格（红利固定 buy≤30 vr≥1.6）===")
rows = []
for sub_buy, sub_vr, sort_by in itertools.product(
    [15.0, 18.0, 20.0, 22.0, 25.0, 28.0, 30.0], [1.3, 1.6, 2.0, 2.5, 3.0], ["fear", "volume"]
):
    r = run_backtest(fear_map, bars_map, trading_days, pool=semi, buy_threshold=30.0, vr_threshold=1.6,
                     sub_buy_threshold=sub_buy, sub_vr_threshold=sub_vr, sort_by=sort_by)
    rows.append({"sub_buy": sub_buy, "sub_vr": sub_vr, "sort": sort_by, "total": r["total_return_pct"],
                 "sharpe": r["sharpe"], "mdd": r["max_drawdown_pct"], "buys": r["buy_count"], "result": r})

for r in sorted(rows, key=lambda r: -r["total"])[:12]:
    mark = " ★超基准" if r["total"] > red["total_return_pct"] else ""
    print(f"  半导体 恐慌<={r['sub_buy']:g} 量比>={r['sub_vr']:g} {r['sort']:<6} "
          f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 买卖 {r['buys']}{mark}")

# 半导体所有"红利空仓且半导体恐慌<=30 且量比>=1.3"的日子 + 之后一个月收益
print("\n=== 半导体极恐放量触发日分析（红利恐慌>30 = 红利空仓期）===")
import numpy as np
idx, etf = "H30184.CSI", "512480.SH"
semi_bars = bars_map[etf].set_index("trade_date")
for day in trading_days:
    semi_fear = fear_map[idx].get(day)
    if semi_fear is None or semi_fear > 30:
        continue
    if day not in semi_bars.index:
        continue
    vr = float(semi_bars.loc[day, "volume_ratio"])
    if not np.isfinite(vr) or vr < 1.3:
        continue
    red_fear = fear_map["000015.SH"].get(day)
    # 后 20 个交易日收益
    day_idx = list(semi_bars.index).index(day)
    future = semi_bars.iloc[day_idx:day_idx + 21]
    ret_5 = future["close"].iloc[min(5, len(future) - 1)] / semi_bars.loc[day, "close"] - 1 if len(future) > 1 else 0
    ret_20 = future["close"].iloc[-1] / semi_bars.loc[day, "close"] - 1 if len(future) > 1 else 0
    print(f"  {day} 半导体恐慌={semi_fear:.1f} 量比={vr:.2f} | 红利恐慌={red_fear:.1f} | 后5日 {ret_5*100:+.1f}% 后20日 {ret_20*100:+.1f}%")
