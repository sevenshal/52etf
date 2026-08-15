#!/usr/bin/env python3
"""跷跷板终版对比：纯红利 vs 最优候补组合，附每个候补板块单独贡献分析。"""

from __future__ import annotations

import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

from lab.seesaw_backtest import POOL, load_data, run_backtest

fear_map, bars_map, trading_days = load_data()

POOLS = {
    "半导体+科创50": [item for item in POOL if item[0] in {"H30184.CSI", "000688.SH"}],
    "半导体+证券": [item for item in POOL if item[0] in {"H30184.CSI", "399975.SZ"}],
    "半导体": [item for item in POOL if item[0] == "H30184.CSI"],
    "科创50": [item for item in POOL if item[0] == "000688.SH"],
    "证券": [item for item in POOL if item[0] == "399975.SZ"],
    "房地产": [item for item in POOL if item[0] == "931775.CSI"],
}

red = run_backtest(fear_map, bars_map, trading_days, pool=[], vr_threshold=1.6)
print(f"=== 纯红利基准: 总收益 {red['total_return_pct']:.2f}% 夏普 {red['sharpe']:.2f} 回撤 {red['max_drawdown_pct']:.2f}% 波动 {red['volatility_pct']:.2f}% 买卖 {red['buy_count']}/{red['sell_count']} 平均仓位 {red['avg_position']:.0%}")

print("\n=== 各池 × 量比（buy≤30）===")
for pool_name, pool in POOLS.items():
    for vr in (1.3, 1.6, 2.0):
        r = run_backtest(fear_map, bars_map, trading_days, pool=pool, buy_threshold=30.0, vr_threshold=vr, sort_by="fear")
        mark = " ★" if r["total_return_pct"] > red["total_return_pct"] else ""
        print(f"  {pool_name:<12} vr>={vr:g} 总收益 {r['total_return_pct']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['max_drawdown_pct']:6.2f}% 波动 {r['volatility_pct']:.2f}% 买卖 {r['buy_count']}/{r['sell_count']} 仓位 {r['avg_position']:.0%}{mark}")

print("\n=== 半导体+科创50（buy≤30 vr≥1.3）交易明细 ===")
r = run_backtest(fear_map, bars_map, trading_days, pool=[item for item in POOL if item[0] in {"H30184.CSI", "000688.SH"}], buy_threshold=30.0, vr_threshold=1.3, sort_by="fear")
for t in r["buys"]:
    print("  买", t)
for t in r["sells"]:
    print("  卖", t)
