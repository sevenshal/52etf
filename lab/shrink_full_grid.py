#!/usr/bin/env python3
"""旧体系（各自量比阈值）完整缩量 x 网格：三标的 + 4 标的，悲观口径。

补全 x=0.75~2.0（此前旧体系只测了 0.25/0.5）。x=None=贪即卖。
"""

import math
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf")

import numpy as np

import lab.seesaw_pessimistic as sp
from lab.log_volume_signal import add_log_z

TRADE_ETF = "159941.SZ"
GOLD = "518880.SH"
X_VALUES = [None, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


def run_case(pairs, swap_threshold, fear_map, bars_map, trading_days, shrink_x, slippage=-1.0):
    cash = 1000000.0
    position = None
    trades = []
    curve = []
    idle_days = 0
    last_close = {}

    def price(etf, day, kind):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        return float(sub.iloc[0][kind])

    def fill(etf, day, side):
        if slippage < 0:
            if slippage <= -1.5:
                return price(etf, day, "low" if side == "buy" else "high")
            return price(etf, day, "high" if side == "buy" else "low")
        return price(etf, day, "open")

    def logz_at(etf, day):
        row = bars_map[etf]
        s2 = row[row["trade_date"] == day]
        return float(s2["log_z"].iloc[0]) if not s2.empty else np.nan

    def do_buy(pair, day):
        nonlocal cash, position
        fi, ve, te, nm, b, v, g = pair
        op = fill(te, day, "buy")
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
        op = fill(te, day, "sell")
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
            shrink_ok = True
            if shrink_x is not None:
                held_logz = logz_at(position[0], sd)
                shrink_ok = np.isfinite(held_logz) and held_logz <= -shrink_x
            if hf is not None and np.isfinite(hf) and hf >= held_greed and shrink_ok:
                do_sell(ed)
            elif hf is not None and np.isfinite(hf) and hf > swap_threshold:
                others = [p for p in pairs if p[2] != position[0] and sigs[p[2]][0]]
                if others:
                    t = min(others, key=lambda p: sigs[p[2]][1])
                    do_sell(ed)
                    do_buy(t, ed)
        if position is None:
            idle_days += 1
        value = cash + (position[2] * last_close.get(position[0], 0) if position else 0)
        curve.append(value)

    v = np.array(curve)
    total = v[-1] / 1000000 - 1
    daily = np.diff(v) / v[:-1]
    mdd = float((v / np.maximum.accumulate(v) - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe,
            "idle_ratio": idle_days / len(trading_days) * 100,
            "buys": len(buys), "sells": len(sells)}


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "512480.SH", TRADE_ETF, GOLD, "588000.SH", "QQQ.US"],
    )
    bars_map = add_log_z(bars_map)

    pairs3 = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, 1.6, 70.0),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
    ]
    from lab.log_volume_signal import build_gold_fear
    fear_map["GOLD_SELF"] = build_gold_fear(bars_map)
    pairs4 = pairs3 + [("GOLD_SELF", GOLD, GOLD, "黄金", 45.0, 1.3, 70.0)]

    for label, ps in (("三标的", pairs3), ("4 标的", pairs4)):
        print(f"\n== 旧体系 {label} 缩量 x 全网格（悲观口径）==")
        for x in X_VALUES:
            r = run_case(ps, 45.0, fear_map, bars_map, trading_days, shrink_x=x)
            xl = "贪即卖" if x is None else f"x={x:.2f}"
            print(f"  {xl:>8} 总收益 {r['total']:8.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} "
                  f"买卖 {r['buys']}/{r['sells']} 空仓 {r['idle_ratio']:.1f}%")
