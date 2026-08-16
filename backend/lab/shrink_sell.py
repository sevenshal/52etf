#!/usr/bin/env python3
"""卖出条件实验：贪恐≥70 且缩量 x 个标准差才卖（替代贪即卖）。

缩量 = log(vol[T]) < mean(log(vol[T-20..T-1])) - x*std(...)（即 log_z <= -x）。
放量沿用统一 k（三标的 k=1.5、4 标的 k=1.25，上实验最优）。
换仓（持仓恐贪>45 且别标的有信号）保持原逻辑不变。最悲观口径（滑点-1）。
"""

import math
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import numpy as np

import lab.seesaw_pessimistic as sp
from lab.log_volume_signal import add_log_z, build_gold_fear

TRADE_ETF = "159941.SZ"
GOLD = "518880.SH"
X_VALUES = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


def run_case(pairs, swap_threshold, fear_map, bars_map, trading_days, sell_shrink_x):
    """sell_shrink_x: 贪卖需缩量 log_z <= -x；None 表示贪即卖（原逻辑）。"""
    cash = 1000000.0
    position = None
    trades = []
    curve = []
    idle_days = 0
    last_close = {}

    def price(etf, day, kind="open"):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        return float(sub.iloc[0][kind])

    def logz_at(etf, day):
        row = bars_map[etf]
        s2 = row[row["trade_date"] == day]
        return float(s2["log_z"].iloc[0]) if not s2.empty else np.nan

    def do_buy(pair, day):
        nonlocal cash, position
        fi, ve, te, nm, b, k, g = pair
        op = price(te, day, "high")
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
        op = price(te, day, "low")
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
            fi, ve, te, nm, b, k, g = p
            f = fear_map.get(fi, {}).get(sd)
            row = bars_map[ve]
            s2 = row[row["trade_date"] == sd]
            logz = float(s2["log_z"].iloc[0]) if not s2.empty else np.nan
            sigs[te] = (f is not None and f <= b and np.isfinite(logz) and logz >= k, f)

        if position is None:
            cands = [p for p in pairs if sigs[p[2]][0]]
            if cands:
                t = min(cands, key=lambda p: sigs[p[2]][1])
                do_buy(t, ed)
        else:
            held_greed = pair_by_etf[position[0]][6]
            hf = sigs.get(position[0], (False, np.nan))[1]
            shrink_ok = True
            if sell_shrink_x is not None:
                held_logz = logz_at(position[0], sd)
                shrink_ok = np.isfinite(held_logz) and held_logz <= -sell_shrink_x
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
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "buys": len(buys), "sells": len(sells),
            "idle_days": idle_days, "idle_ratio": idle_days / len(trading_days) * 100,
            "buy_list": buys, "sell_list": sells}


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "512480.SH", TRADE_ETF, GOLD, "588000.SH", "QQQ.US"],
    )
    bars_map = add_log_z(bars_map)
    fear_map["GOLD_SELF"] = build_gold_fear(bars_map)

    def pairs_for(k, with_gold):
        p = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, k, 70.0),
            ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, k, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, k, 70.0),
        ]
        if with_gold:
            p.append(("GOLD_SELF", GOLD, GOLD, "黄金", 45.0, k, 70.0))
        return p

    # 基准（贪即卖）
    r_base3 = run_case(pairs_for(1.5, False), 45.0, fear_map, bars_map, trading_days, sell_shrink_x=None)
    r_base4 = run_case(pairs_for(1.25, True), 45.0, fear_map, bars_map, trading_days, sell_shrink_x=None)
    print(f"基准 贪即卖: 三标的(k=1.5) {r_base3['total']:.2f}% | 4标的(k=1.25) {r_base4['total']:.2f}%")

    print("\n== 三标的（k=1.5）贪+缩量卖 x 搜索 ==")
    for x in X_VALUES:
        r = run_case(pairs_for(1.5, False), 45.0, fear_map, bars_map, trading_days, sell_shrink_x=x)
        print(f"  x={x:.2f} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} "
              f"买卖 {r['buys']}/{r['sells']} 空仓 {r['idle_ratio']:.1f}%")

    # 三标的 x=0.25 明细
    r25 = run_case(pairs_for(1.5, False), 45.0, fear_map, bars_map, trading_days, sell_shrink_x=0.25)
    print(f"\n=== 三标的 x=0.25 {r25['total']:.2f}% 明细 ===")
    for t in r25["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in r25["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))

    # 旧体系（各自量比阈值 1.6/1.6/1.3）+ 缩量卖
    def run_case_old(pairs, swap_threshold, fear_map, bars_map, trading_days, sell_shrink_x):
        cash = 1000000.0
        position = None
        trades = []
        curve = []
        last_close = {}

        def price(etf, day, kind="open"):
            row = bars_map[etf]
            sub = row[row["trade_date"] == day]
            return float(sub.iloc[0][kind]) if not sub.empty else None

        def logz_at(etf, day):
            row = bars_map[etf]
            s2 = row[row["trade_date"] == day]
            return float(s2["log_z"].iloc[0]) if not s2.empty else np.nan

        def do_buy(pair, day):
            nonlocal cash, position
            fi, ve, te, nm, b, v, g = pair
            op = price(te, day, "high")
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
            op = price(te, day, "low")
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
                if sell_shrink_x is not None:
                    held_logz = logz_at(position[0], sd)
                    shrink_ok = np.isfinite(held_logz) and held_logz <= -sell_shrink_x
                if hf is not None and np.isfinite(hf) and hf >= held_greed and shrink_ok:
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
        return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "buys": sum(1 for t in trades if t["action"] == "BUY"),
                "sells": sum(1 for t in trades if t["action"] == "SELL")}

    pairs_old3 = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, 1.6, 70.0),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
    ]
    pairs_old4 = pairs_old3 + [("GOLD_SELF", GOLD, GOLD, "黄金", 45.0, 1.3, 70.0)]
    for label, ps in (("旧体系三标的", pairs_old3), ("旧体系4标的", pairs_old4)):
        r0 = run_case_old(ps, 45.0, fear_map, bars_map, trading_days, None)
        r1 = run_case_old(ps, 45.0, fear_map, bars_map, trading_days, 0.25)
        r2 = run_case_old(ps, 45.0, fear_map, bars_map, trading_days, 0.5)
        print(f"{label}: 贪即卖 {r0['total']:.2f}% | x=0.25 {r1['total']:.2f}% | x=0.5 {r2['total']:.2f}%")

    # 旧体系4标的 x=0.25 明细（309.84%）
    rb = run_case_old(pairs_old4, 45.0, fear_map, bars_map, trading_days, 0.25)
    print(f"\n=== 旧体系4标的 x=0.25 {rb['total']:.2f}% 明细 ===")
    # 需要 trades 明细：改造 run_case_old 返回 trades 太麻烦，用统一k版 run_case 打印（结构相同）
    rbk = run_case([("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
                    ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, 1.6, 70.0),
                    ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
                    ("GOLD_SELF", GOLD, GOLD, "黄金", 45.0, 1.3, 70.0)],
                   45.0, fear_map, bars_map, trading_days, sell_shrink_x=0.25)
    print(f"统一k同参数 {rbk['total']:.2f}% 明细（近似旧体系，量比列不同）：")
    for t in rbk["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in rbk["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))

    print("\n== 4 标的（k=1.25 + 黄金45）贪+缩量卖 x 搜索 ==")
    for x in X_VALUES:
        r = run_case(pairs_for(1.25, True), 45.0, fear_map, bars_map, trading_days, sell_shrink_x=x)
        print(f"  x={x:.2f} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} "
              f"买卖 {r['buys']}/{r['sells']} 空仓 {r['idle_ratio']:.1f}%")
