#!/usr/bin/env python3
"""信号均分再平衡引擎：只要出信号就必须买入，按信号数均分仓位。

规则（按用户要求）：
1. 只要标的有信号（恐贪≤b 且量比≥v）就持有该标的，目标仓位 = 总资产 / 信号数 N（均分）。
2. 现金不够 → 先卖出其他（非信号）持仓；非信号持仓即使不符合贪卖/换仓条件也全部卖出腾仓。
3. 信号持仓超配（市值 > 1/N）→ 卖出超配部分；欠配 → 用现金补齐。
4. 多个标的有信号 → 按买入后每只 1/N 均分。
5. 保留贪卖止盈：持仓恐贪 ≥70 全额卖出。
6. N=0（无信号）→ 持仓不动（保留贪卖）。

对比基准：原引擎（选恐贪最低全仓）3 标的 = 378.01% / -9.80% / 2.54。
"""

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"
MED_ETF = "515120.SH"
GREED = 70.0


def run_rebalance(pairs, fear_map, bars_map, trading_days):
    """信号均分再平衡 v2：
    1. 出信号买入后持有到恐贪≥70（极度贪婪）才清仓，信号消失不卖。
    2. 持仓中若有新标的出信号 → 分仓给新标的（卖超配旧仓腾钱），调仓后所有持仓标的均分。
    3. 多个信号同时出现 → 均分。
    """
    import math

    import numpy as np

    cash = 1000000.0
    positions = {}  # etf -> {"qty": int, "cost": float}
    trades = []
    curve = []
    last_close = {}
    pair_by_etf = {p[2]: p for p in pairs}

    def price(etf, day, kind="open"):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        return float(sub.iloc[0][kind])

    def do_sell(etf, qty, day):
        nonlocal cash
        op = price(etf, day)
        if op is None or op <= 0 or qty <= 0:
            return
        pos = positions.get(etf)
        if not pos or pos["qty"] <= 0:
            return
        qty = min(qty, pos["qty"])
        cash += qty * op
        trades.append({"date": str(day), "action": "SELL", "symbol": etf, "qty": qty, "price": op,
                       "pnl": qty * op - pos["cost"] * qty / pos["qty"]})
        pos["qty"] -= qty
        pos["cost"] *= pos["qty"] / (pos["qty"] + qty) if pos["qty"] + qty > 0 else 1
        if pos["qty"] <= 0:
            del positions[etf]

    def do_buy(etf, budget, day):
        nonlocal cash
        op = price(etf, day)
        if op is None or op <= 0 or budget <= 0:
            return
        qty = int(budget // op)
        if qty >= 1:
            cash -= qty * op
            if etf in positions:
                positions[etf]["qty"] += qty
                positions[etf]["cost"] += qty * op
            else:
                positions[etf] = {"qty": qty, "cost": qty * op}
            trades.append({"date": str(day), "action": "BUY", "symbol": etf, "qty": qty, "price": op})

    def equity():
        return cash + sum(pos["qty"] * last_close.get(etf, 0) for etf, pos in positions.items())

    for i in range(1, len(trading_days)):
        ed = trading_days[i]
        sd = trading_days[i - 1]
        for p in pairs:
            sub = bars_map[p[2]][bars_map[p[2]]["trade_date"] == ed]
            if not sub.empty:
                last_close[p[2]] = float(sub.iloc[0]["close"])
        sigs = {}
        for p in pairs:
            fi, ve, te, nm, b, v, g = p
            f = fear_map.get(fi, {}).get(sd)
            row = bars_map[ve]
            s2 = row[row["trade_date"] == sd]
            vr = float(s2["volume_ratio"].iloc[0]) if not s2.empty else np.nan
            sigs[te] = (f is not None and f <= b and np.isfinite(vr) and vr >= v, f)

        # 1) 极度贪婪清仓：恐贪 ≥70 全额卖出
        for etf in list(positions):
            g_th = pair_by_etf[etf][6]
            hf = sigs.get(etf, (False, np.nan))[1]
            if hf is not None and np.isfinite(hf) and hf >= g_th:
                do_sell(etf, positions[etf]["qty"], ed)

        # 2) 新信号标的加入持仓（已持仓的不因信号消失而卖）
        new_sigs = [p[2] for p in pairs if sigs[p[2]][0] and p[2] not in positions]
        if new_sigs:
            for etf in new_sigs:
                positions[etf] = {"qty": 0, "cost": 0.0}
            N = len(positions)
            eq = equity()
            target = eq / N
            # 卖超配（含旧持仓给新标的腾仓）
            for etf in list(positions):
                mv = positions[etf]["qty"] * last_close.get(etf, 0)
                if mv > target:
                    over_qty = int((mv - target) / last_close.get(etf, 1))
                    do_sell(etf, over_qty, ed)
            # 买欠配（含新标的）
            for etf in positions:
                cur = positions[etf]["qty"] * last_close.get(etf, 0)
                need = target - cur
                if need > 100:
                    do_buy(etf, min(need, cash), ed)

        value = equity()
        curve.append(value)

    v = np.array(curve)
    total = v[-1] / 1000000 - 1
    daily = np.diff(v) / v[:-1]
    mdd = float((v / np.maximum.accumulate(v) - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe,
            "buys": sum(1 for t in trades if t["action"] == "BUY"),
            "sells": sum(1 for t in trades if t["action"] == "SELL"),
            "buy_list": [t for t in trades if t["action"] == "BUY"],
            "sell_list": [t for t in trades if t["action"] == "SELL"]}


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US", "931152.CSI"],
        ["510880.SH", "588000.SH", "512480.SH", TRADE_ETF, "QQQ.US", MED_ETF],
    )

    base3 = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "半导体(科创信号)", 25.0, 1.6, 70.0),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
    ]
    print("== 信号均分再平衡引擎 ==")
    r = run_rebalance(base3, fear_map, bars_map, trading_days)
    print(f"3标的: {r['total']:.2f}% / {r['mdd']:.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}"
          f"（基准原引擎 378.01%/-9.80%/2.54）")

    print("\n== 4标的（+创新药）信号均分再平衡，创新药参数网格 ==")
    rows = []
    for mb, mv in itertools.product([15.0, 20.0, 25.0, 30.0], [1.3, 1.6, 2.0]):
        pairs = base3 + [("931152.CSI", MED_ETF, MED_ETF, "创新药", mb, mv, 70.0)]
        r = run_rebalance(pairs, fear_map, bars_map, trading_days)
        rows.append({"mb": mb, "mv": mv, **r})
        print(f"  创新药恐慌<={mb:g} 量比>={mv:g} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    # 3 标的 v2 明细
    rb = run_rebalance(base3, fear_map, bars_map, trading_days)
    print(f"\n=== v2 3标的 {rb['total']:.2f}% 明细 ===")
    for t in rb["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3), "qty", t["qty"])
    for t in rb["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "qty", t["qty"], "pnl", round(t["pnl"]))
    import sys as _sys
    _sys.exit(0)
    for t in rb["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3), "qty", t["qty"])
    for t in rb["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "qty", t["qty"], "pnl", round(t["pnl"]))
