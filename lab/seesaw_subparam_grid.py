#!/usr/bin/env python3
"""跷跷板候补独立参数网格：红利固定（buy≤30, vr≥1.6, greed≥70即卖），
科创50/半导体单独搜恐慌阈值与量比阈值。"""

from __future__ import annotations

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf")

from lab.seesaw_backtest import POOL, load_data, run_backtest

fear_map, bars_map, trading_days = load_data()

# 红利固定参数（用户那套）
RED_BUY, RED_VR = 30.0, 1.6

POOLS = {
    "科创50": [item for item in POOL if item[0] == "000688.SH"],
    "半导体": [item for item in POOL if item[0] == "H30184.CSI"],
    "科创50+半导体": [item for item in POOL if item[0] in {"000688.SH", "H30184.CSI"}],
}

red = run_backtest(fear_map, bars_map, trading_days, pool=[], buy_threshold=RED_BUY, vr_threshold=RED_VR)
print(f"=== 纯红利基准（红利参数固定）: 总收益 {red['total_return_pct']:.2f}% 夏普 {red['sharpe']:.2f} "
      f"回撤 {red['max_drawdown_pct']:.2f}% 波动 {red['volatility_pct']:.2f}% 买卖 {red['buy_count']}/{red['sell_count']}")

rows = []
for pool_name, pool in POOLS.items():
    for sub_buy, sub_vr, sort_by in itertools.product(
        [15.0, 20.0, 25.0, 30.0], [1.0, 1.3, 1.6, 2.0], ["fear", "volume"]
    ):
        r = run_backtest(
            fear_map, bars_map, trading_days, pool=pool,
            buy_threshold=RED_BUY, vr_threshold=RED_VR,
            sub_buy_threshold=sub_buy, sub_vr_threshold=sub_vr, sort_by=sort_by,
        )
        rows.append({
            "pool": pool_name, "sub_buy": sub_buy, "sub_vr": sub_vr, "sort": sort_by,
            "total": r["total_return_pct"], "sharpe": r["sharpe"],
            "mdd": r["max_drawdown_pct"], "vol": r["volatility_pct"],
            "buys": r["buy_count"], "sells": r["sell_count"], "avg_pos": r["avg_position"],
            "result": r,
        })

print(f"\n=== 网格 {len(rows)} 组（红利固定），按总收益 top12 ===")
for r in sorted(rows, key=lambda r: -r["total"])[:12]:
    mark = " ★超基准" if r["total"] > red["total_return_pct"] else ""
    print(f"  {r['pool']:<10} 候补恐慌<={r['sub_buy']:g} 候补量比>={r['sub_vr']:g} {r['sort']:<6} "
          f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 波动 {r['vol']:.2f}% 买卖 {r['buys']}/{r['sells']}{mark}")

print(f"\n=== 按夏普 top10 ===")
for r in sorted(rows, key=lambda r: -r["sharpe"])[:10]:
    print(f"  {r['pool']:<10} 候补恐慌<={r['sub_buy']:g} 候补量比>={r['sub_vr']:g} {r['sort']:<6} "
          f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 波动 {r['vol']:.2f}% 买卖 {r['buys']}/{r['sells']}")

# 最优（总收益超基准且回撤相对小的几个）的明细
print("\n=== 候选组合明细 ===")
for r in sorted(rows, key=lambda r: -r["total"])[:3]:
    if r["total"] <= red["total_return_pct"]:
        continue
    print(f"\n[{r['pool']} 候补恐慌<={r['sub_buy']:g} 量比>={r['sub_vr']:g} {r['sort']}] "
          f"总收益 {r['total']:.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:.2f}%")
    for t in r["result"]["buys"]:
        print("  买", t)
    for t in r["result"]["sells"]:
        print("  卖", t)
