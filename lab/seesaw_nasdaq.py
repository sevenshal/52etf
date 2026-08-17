#!/usr/bin/env python3
"""三标的轮动：红利 + 科创50 + 纳指ETF(159941.SZ, 恐贪用 QQQ.US 自算贪恐)。"""

from __future__ import annotations

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf")

from lab.seesaw_triple import BASE_TWO, load_data, run_backtest

# 纳指：恐贪=QQQ.US（自算贪恐，2023-10-20 起），ETF=159941.SZ
NASDAQ = ("QQQ.US", "159941.SZ", "纳指")

print("对比基准：双轮动(红利30/1.6 + 科创50 25/1.6 + 换仓45) = 211.55% / 2.49 / -6.71%")
print("注：QQQ 恐贪 2023-10-20 起，之前纳指无信号（红利/科创50 照常）")

pairs = [*BASE_TWO, (*NASDAQ, 20.0, 1.3)]
fear_map, bars_map, trading_days = load_data(pairs)
print(f"交易日 {trading_days[0]} ~ {trading_days[-1]} 共 {len(trading_days)} 天")

rows = []
for bthr, vthr, swap in itertools.product([20.0, 25.0, 30.0], [1.0, 1.3, 1.6], [45.0, 55.0, 70.0]):
    nasdaq_pair = (*NASDAQ, bthr, vthr)
    r = run_backtest(fear_map, bars_map, trading_days, [*BASE_TWO, nasdaq_pair], 70.0, swap)
    rows.append({"bthr": bthr, "vthr": vthr, "swap": swap, **r})

print(f"\n=== 网格 {len(rows)} 组（纳指独立参数）top12（按总收益）===")
for r in sorted(rows, key=lambda r: -r["total"])[:12]:
    print(f"  纳指 恐慌<={r['bthr']:g} 量比>={r['vthr']:g} 换仓>{r['swap']:g} "
          f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 波动 {r['vol']:.2f}% 买卖 {r['buys']}/{r['sells']}")

print(f"\n=== 按夏普 top10 ===")
for r in sorted(rows, key=lambda r: -r["sharpe"])[:10]:
    print(f"  纳指 恐慌<={r['bthr']:g} 量比>={r['vthr']:g} 换仓>{r['swap']:g} "
          f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 买卖 {r['buys']}/{r['sells']}")
