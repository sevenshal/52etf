#!/usr/bin/env python3
"""v4 引擎：恐慌+放量信号买入持有；新标的出信号 → 匀出仓位给新标的，所有持仓均分 1/N；
无贪卖 70（不因贪婪卖出），持仓标的信号消失也不卖（永不完整清仓，仅均分时结构性卖出超配）。

对比基准：原引擎 378.01%/-9.80%/2.54；rebalance v2（有贪卖70）3 标的 362.78%。
"""

import itertools
import math
import sys

import numpy as np

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"
MED_ETF = "515120.SH"


def run_rebalance(pairs, fear_map, bars_map, trading_days, cost_pct=0.0):
    cash = 1000000.0
    positions = {}  # etf -> {"qty": int, "cost": float}
    trades = []
    curve = []
    last_close = {}

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
        fee = qty * op * cost_pct
        cash += qty * op - fee
        trades.append({"date": str(day), "action": "SELL", "symbol": etf, "qty": qty, "price": op,
                       "pnl": qty * op - fee - pos["cost"] * qty / pos["qty"]})
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
            fee = qty * op * cost_pct
            cash -= qty * op + fee
            if etf in positions:
                positions[etf]["qty"] += qty
                positions[etf]["cost"] += qty * op + fee
            else:
                positions[etf] = {"qty": qty, "cost": qty * op + fee}
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

        # 无贪卖。新信号标的加入持仓（已持仓的不因信号消失而卖）
        new_sigs = [p[2] for p in pairs if sigs[p[2]][0] and p[2] not in positions]
        if new_sigs:
            for etf in new_sigs:
                positions[etf] = {"qty": 0, "cost": 0.0}
            N = len(positions)
            eq = equity()
            target = eq / N
            # 卖超配（旧持仓匀出给新标的）
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
    print("基准：原引擎 378.01%/-9.80%/2.54；v2（有贪卖70）362.78%/-20.05%/2.18")
    print("\n== v4（无贪卖70，新信号均分 1/N）==")
    r = run_rebalance(base3, fear_map, bars_map, trading_days)
    print(f"  3标的: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    print("\n  4标的（+创新药）网格：")
    rows = []
    for mb, mv in itertools.product([20.0, 25.0, 30.0], [1.3, 1.6]):
        pairs = base3 + [("931152.CSI", MED_ETF, MED_ETF, "创新药", mb, mv, 70.0)]
        r = run_rebalance(pairs, fear_map, bars_map, trading_days)
        rows.append({"mb": mb, "mv": mv, **r})
        print(f"    创新药<={mb:g} 量比>={mv:g}: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    # 持仓演变（3 标的）：打印每个信号日的持仓标的数
    print("\n== v4 3标的 信号日与持仓演变 ==")
    positions_now = set()
    cash2 = 1000000.0
    for i in range(1, len(trading_days)):
        sd = trading_days[i - 1]
        sig_now = []
        for p in base3:
            fi, ve, te, nm, b, v, g = p
            f = fear_map.get(fi, {}).get(sd)
            row = bars_map[ve]
            s2 = row[row["trade_date"] == sd]
            vr = float(s2["volume_ratio"].iloc[0]) if not s2.empty else np.nan
            if f is not None and f <= b and np.isfinite(vr) and vr >= v:
                sig_now.append(nm)
        if sig_now:
            for nm in sig_now:
                positions_now.add(nm)
            print(f"  {sd}: 信号={sig_now} → 持仓池={sorted(positions_now)}")
