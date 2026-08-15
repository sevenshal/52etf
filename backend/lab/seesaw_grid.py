#!/usr/bin/env python3
"""跷跷板轮动网格搜索：候补池 × 信号参数 × 排序，寻找超过纯红利的组合。"""

from __future__ import annotations

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

from lab.seesaw_backtest import (
    POOL,
    load_data,
    run_backtest,
)

POOLS = {
    "all9": POOL,
    "low5": [item for item in POOL if item[0] in {"H30184.CSI", "399975.SZ", "000688.SH", "399006.SZ", "399967.SZ"}],
    "low3": [item for item in POOL if item[0] in {"H30184.CSI", "399975.SZ", "000688.SH"}],
    "tech": [item for item in POOL if item[0] in {"H30184.CSI", "000688.SH", "399006.SZ"}],
    "semi_sec": [item for item in POOL if item[0] in {"H30184.CSI", "399975.SZ"}],
}

fear_map, bars_map, trading_days = load_data()
red = run_backtest(fear_map, bars_map, trading_days, pool=[], vr_threshold=1.6)
print(f"纯红利基准: 总收益 {red['total_return_pct']:.2f}% 夏普 {red['sharpe']:.2f} 回撤 {red['max_drawdown_pct']:.2f}% 买卖 {red['buy_count']}/{red['sell_count']}")

rows = []
for pool_name, pool in POOLS.items():
    for buy_thr, vr, sort_by in itertools.product([25.0, 30.0, 35.0], [1.3, 1.6, 2.0, 2.5], ["fear", "volume"]):
        try:
            r = run_backtest(fear_map, bars_map, trading_days, pool=pool,
                             buy_threshold=buy_thr, vr_threshold=vr, sort_by=sort_by)
            rows.append({
                "pool": pool_name, "buy_thr": buy_thr, "vr": vr, "sort": sort_by,
                "total": r["total_return_pct"], "sharpe": r["sharpe"],
                "mdd": r["max_drawdown_pct"], "vol": r["volatility_pct"],
                "buys": r["buy_count"], "avg_pos": r["avg_position"],
                "result": r,
            })
        except Exception as exc:
            print("skip", pool_name, buy_thr, vr, sort_by, exc)

print(f"\n=== 网格 {len(rows)} 组，超过纯红利({red['total_return_pct']:.2f}%) 的有 ===")
better = [r for r in rows if r["total"] > red["total_return_pct"]]
better.sort(key=lambda r: -r["total"])
for r in better[:15]:
    print(f"  {r['pool']:<8} buy<={r['buy_thr']:g} vr>={r['vr']:g} {r['sort']:<6} "
          f"总收益 {r['total']:.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:.2f}% 波动 {r['vol']:.2f}% 买卖 {r['buys']}")

print(f"\n=== 全部按夏普排序 top10 ===")
rows_sorted = sorted(rows, key=lambda r: -r["sharpe"])
for r in rows_sorted[:10]:
    print(f"  {r['pool']:<8} buy<={r['buy_thr']:g} vr>={r['vr']:g} {r['sort']:<6} "
          f"总收益 {r['total']:.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:.2f}%")

if better:
    best = better[0]
    print(f"\n=== 最优组明细: {best['pool']} buy<={best['buy_thr']:g} vr>={best['vr']:g} {best['sort']} ===")
    r = best["result"]
    print("  买入:", r["buys"])
    print("  卖出:", r["sells"])
