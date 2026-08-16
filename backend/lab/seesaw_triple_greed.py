#!/usr/bin/env python3
"""每标独立极端贪婪卖出阈值搜索（三标的轮动）。

pairs = (fear_index, volume_etf, trade_etf, name, buy, vr, greed)：
持有 X 时 X 恐贪 >= X 的 greed → 卖出。买入参数用推荐值，只搜三个 greed。
交易标的分 159941（当前推荐）与 159509 两版。
"""

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp


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
            held_pair = pair_by_etf[position[0]]
            held_greed = held_pair[6]
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


def make_pairs(gm, gs, g2, trade_etf):
    return [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, gm),
        ("000688.SH", "588000.SH", "588000.SH", "科创50", 25.0, 1.6, gs),
        ("QQQ.US", "QQQ.US", trade_etf, "纳指", 20.0, 1.3, g2),
    ]


if __name__ == "__main__":
    for trade_etf, base in (("159941.SZ", "302.38%/-7.89%/2.57"), ("159509.SZ", "337.38%/-9.80%/2.50")):
        print(f"\n===== 交易标的 = {trade_etf}（对比：统一 greed70 {base}）=====")
        fear_map, bars_map, trading_days = sp.load_data(
            ["000015.SH", "000688.SH", "QQQ.US"], ["510880.SH", "588000.SH", trade_etf, "QQQ.US"]
        )
        rows = []
        for gm, gs, g2 in itertools.product([60.0, 65.0, 70.0, 75.0, 80.0], [60.0, 65.0, 70.0, 75.0, 80.0], [55.0, 60.0, 65.0, 70.0, 75.0]):
            r = run_case(make_pairs(gm, gs, g2, trade_etf), 45.0, fear_map, bars_map, trading_days)
            rows.append({"gm": gm, "gs": gs, "g2": g2, **r})
        print("== 按总收益 top8 ==")
        for r in sorted(rows, key=lambda r: -r["total"])[:8]:
            print(f"  红利贪>{r['gm']:g} 科创贪>{r['gs']:g} 纳指贪>{r['g2']:g} "
                  f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")
        print("== 按夏普 top6 ==")
        for r in sorted(rows, key=lambda r: -r["sharpe"])[:6]:
            print(f"  红利贪>{r['gm']:g} 科创贪>{r['gs']:g} 纳指贪>{r['g2']:g} "
                  f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")
