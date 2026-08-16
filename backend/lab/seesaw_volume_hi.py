#!/usr/bin/env python3
"""三标的高量比阈值搜索：恐慌阈值保持推荐，量比档位提高。

红利恐慌30 × 量比{1.6,2.0,2.5}；科创50 恐慌25 × 量比{1.6,2.0,2.5}；
纳指(159509) 恐慌20 × 量比{1.3,1.6,2.0}；贪恐统一70，换仓45。
对比基准：红利30/1.6 + 科创25/1.6 + 纳指20/1.3 = 337.38% / -9.80% / 2.50（159509）。
"""

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"


def run_case(pairs, swap_threshold, fear_map, bars_map, trading_days):
    import math

    import numpy as np

    cash = 1000000.0
    position = None
    trades = []
    curve = []
    last_close = {}

    def price(etf, day, kind="open"):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        return float(sub.iloc[0][kind])

    def do_buy(pair, day):
        nonlocal cash, position
        fi, ve, te, nm, b, v, g = pair
        op = price(te, day)
        if op is None or op <= 0:
            return
        qty = int(cash // op)
        if qty >= 1:
            cash -= qty * op
            position = (te, nm, qty, qty * op)
            trades.append({"date": str(day), "action": "BUY", "symbol": te, "qty": qty, "price": op})

    def do_sell(day):
        nonlocal cash, position
        te, nm, qty, cost = position
        op = price(te, day)
        if op is None or op <= 0:
            return
        cash += qty * op
        trades.append({"date": str(day), "action": "SELL", "symbol": te, "qty": qty, "price": op, "pnl": qty * op - cost})
        position = None

    pair_by_etf = {p[2]: p for p in pairs}
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

        if position is None:
            cands = [p for p in pairs if sigs[p[2]][0]]
            if cands:
                t = min(cands, key=lambda p: sigs[p[2]][1])
                do_buy(t, ed)
        else:
            held_greed = pair_by_etf[position[0]][6]
            hf = sigs.get(position[0], (False, np.nan))[1]
            if hf is not None and np.isfinite(hf) and hf >= held_greed:
                do_sell(ed)
            elif hf is not None and np.isfinite(hf) and hf > swap_threshold:
                others = [p for p in pairs if p[2] != position[0] and sigs[p[2]][0]]
                if others:
                    t = min(others, key=lambda p: sigs[p[2]][1])
                    do_sell(ed)
                    do_buy(t, ed)
        value = cash + (position[2] * last_close.get(position[0], 0) if position else 0)
        curve.append(value)

    v = np.array(curve)
    total = v[-1] / 1000000 - 1
    daily = np.diff(v) / v[:-1]
    mdd = float((v / np.maximum.accumulate(v) - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "buys": len(buys), "sells": len(sells)}


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "588000.SH", TRADE_ETF, "QQQ.US"],
    )
    print(f"=== 高量比阈值搜索（纳指 {TRADE_ETF}，贪70，换仓45）===")
    print("对比基准：红利30/1.6 科创25/1.6 纳指20/1.3 = 337.38% / -9.80% / 2.50")
    rows = []
    for vr1, vr2, vr3 in itertools.product([1.6, 2.0, 2.5], [1.6, 2.0, 2.5], [1.3, 1.6, 2.0]):
        pairs = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, vr1, 70.0),
            ("000688.SH", "588000.SH", "588000.SH", "科创50", 25.0, vr2, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, vr3, 70.0),
        ]
        r = run_case(pairs, 45.0, fear_map, bars_map, trading_days)
        rows.append({"vr1": vr1, "vr2": vr2, "vr3": vr3, **r})

    print("== 按总收益 top12 ==")
    for r in sorted(rows, key=lambda r: -r["total"])[:12]:
        print(f"  红利量比>={r['vr1']:g} 科创量比>={r['vr2']:g} 纳指量比>={r['vr3']:g} "
              f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")
    print("== 按夏普 top8 ==")
    for r in sorted(rows, key=lambda r: -r["sharpe"])[:8]:
        print(f"  红利量比>={r['vr1']:g} 科创量比>={r['vr2']:g} 纳指量比>={r['vr3']:g} "
              f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")
