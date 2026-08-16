#!/usr/bin/env python3
"""平分仓位版三标的：买入时有多个标的出信号 → 均分现金买入。

对比基准：科创50信号→半导体交易 + 纳指159509（原引擎选恐贪最低全仓）= 378.01%。

pairs = (fear_index, volume_etf, trade_etf, name, buy, vr, greed)
"""

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"


def run_original(pairs, swap_threshold, fear_map, bars_map, trading_days):
    """原引擎：空仓时选恐贪最低的全仓买入。"""
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
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe,
            "buys": sum(1 for t in trades if t["action"] == "BUY"),
            "sells": sum(1 for t in trades if t["action"] == "SELL"),
            "buy_list": [t for t in trades if t["action"] == "BUY"],
            "sell_list": [t for t in trades if t["action"] == "SELL"]}


def run_split(pairs, swap_threshold, fear_map, bars_map, trading_days):
    """平分版：多标的持仓；买入时多个信号均分现金；各标的独立贪卖/换仓。"""
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

    def do_sell(etf, day):
        nonlocal cash
        pos = positions[etf]
        op = price(etf, day)
        if op is None or op <= 0:
            return
        cash += pos["qty"] * op
        trades.append({"date": str(day), "action": "SELL", "symbol": etf,
                       "qty": pos["qty"], "price": op, "pnl": pos["qty"] * op - pos["cost"]})
        del positions[etf]

    def do_buy(pair, budget, day):
        nonlocal cash
        fi, ve, te, nm, b, v, g = pair
        op = price(te, day)
        if op is None or op <= 0 or budget <= 0:
            return
        qty = int(budget // op)
        if qty >= 1:
            cash -= qty * op
            positions[te] = {"qty": qty, "cost": qty * op}
            trades.append({"date": str(day), "action": "BUY", "symbol": te, "qty": qty, "price": op})

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

        # 1) 贪卖 + 换仓（各标的独立）
        to_sell = []
        for etf in list(positions):
            g_th = pair_by_etf[etf][6]
            hf = sigs.get(etf, (False, np.nan))[1]
            if hf is not None and np.isfinite(hf):
                if hf >= g_th:
                    to_sell.append(etf)
                elif hf > swap_threshold and any(sigs[o[2]][0] for o in pairs if o[2] != etf):
                    to_sell.append(etf)
        for etf in to_sell:
            do_sell(etf, ed)

        # 2) 买入：出信号且未持仓的均分现金
        pending = [p for p in pairs if sigs[p[2]][0] and p[2] not in positions and cash > 0]
        if pending:
            per = cash / len(pending)
            for p in pending:
                do_buy(p, per, ed)

        value = cash + sum(pos["qty"] * last_close.get(etf, 0) for etf, pos in positions.items())
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
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "588000.SH", "512480.SH", TRADE_ETF, "QQQ.US"],
    )

    def make_pairs(bs, vrs):
        return [
            ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
            ("000688.SH", "588000.SH", "512480.SH", "半导体(科创信号)", bs, vrs, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
        ]

    print("== 原引擎（选恐贪最低全仓）参考 ==")
    for bs, vrs in itertools.product([20.0, 25.0, 30.0], [1.3, 1.6]):
        r = run_original(make_pairs(bs, vrs), 45.0, fear_map, bars_map, trading_days)
        print(f"  恐慌<={bs:g} 量比>={vrs:g} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    print("\n== 平分版（多信号均分现金）换仓45 ==")
    rows = []
    for bs, vrs in itertools.product([20.0, 25.0, 30.0], [1.3, 1.6]):
        r = run_split(make_pairs(bs, vrs), 45.0, fear_map, bars_map, trading_days)
        rows.append({"bs": bs, "vrs": vrs, **r})
        print(f"  恐慌<={bs:g} 量比>={vrs:g} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    print("\n== 平分版（多信号均分现金）换仓60 ==")
    rows60 = []
    for bs, vrs in itertools.product([20.0, 25.0, 30.0], [1.3, 1.6]):
        r = run_split(make_pairs(bs, vrs), 60.0, fear_map, bars_map, trading_days)
        rows60.append({"bs": bs, "vrs": vrs, **r})
        print(f"  恐慌<={bs:g} 量比>={vrs:g} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    # 最优平分版明细（换仓45 恐慌25 量比1.6）
    print("\n=== 平分版 恐慌25/量比1.6/换仓45 明细 ===")
    rb = run_split(make_pairs(25.0, 1.6), 45.0, fear_map, bars_map, trading_days)
    for t in rb["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in rb["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))
