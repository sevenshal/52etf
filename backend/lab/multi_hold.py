#!/usr/bin/env python3
"""多持仓轮动实验：空仓时所有出信号标的平均买入；每只持仓独立贪卖(>=70)/换仓(>45且有别处信号)。

对比基准：B 配置（统一log-z 1.25 + 贪即卖，单持仓最恐慌）= 212.47%。
悲观口径（滑点-1：买最高卖最低）。换仓卖出资金平均投入所有出信号的未持仓标的。
"""

import math
import sys

import numpy as np

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp
from lab.log_volume_signal import add_log_z

TRADE_ETF = "159941.SZ"


def run_multi(pairs, swap_threshold, fear_map, bars_map, trading_days, greed=70.0, k=1.25):
    """多持仓轮动。pairs: (fear_index, volume_etf, trade_etf, name, bthr, vthr) vthr=k(log-z)。"""
    cash = 1000000.0
    holdings = {}  # symbol -> {"shares":, "cost":}
    trades = []
    curve = []
    idle_days = 0
    last_close = {}

    def price(etf, day, kind):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        return float(sub.iloc[0][kind]) if not sub.empty else None

    def do_buy(symbol, amount, day):
        nonlocal cash
        op = price(symbol, day, "high")
        if op is None or op <= 0 or amount <= 0:
            return
        qty = int(amount // op)
        if qty < 1:
            return
        actual = qty * op
        cash -= actual
        h = holdings.setdefault(symbol, {"shares": 0, "cost": 0.0})
        h["cost"] = (h["cost"] * h["shares"] + actual) / (h["shares"] + qty)
        h["shares"] += qty
        trades.append({"date": str(day), "action": "BUY", "symbol": symbol, "qty": qty, "price": op})

    def do_sell(symbol, day):
        nonlocal cash
        h = holdings.get(symbol)
        if not h or h["shares"] < 1:
            return
        op = price(symbol, day, "low")
        if op is None or op <= 0:
            return
        proceeds = h["shares"] * op
        cash += proceeds
        pnl = proceeds - h["cost"] * h["shares"]
        trades.append({"date": str(day), "action": "SELL", "symbol": symbol, "qty": h["shares"],
                       "price": op, "pnl": pnl})
        del holdings[symbol]

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
            lz = float(s2["log_z"].iloc[0]) if not s2.empty else np.nan
            sigs[te] = {"fear": f, "signal": f is not None and f <= b and np.isfinite(lz) and lz >= v}

        # 卖出阶段：贪卖 + 换仓（独立评估每个持仓）
        swap_proceeds = 0.0
        for sym in list(holdings.keys()):
            hf = sigs.get(sym, {}).get("fear")
            if hf is None or not np.isfinite(hf):
                continue
            if hf >= greed:
                do_sell(sym, ed)
            elif hf > swap_threshold:
                others_signal = any(sigs[t]["signal"] for t in pair_by_etf if t not in holdings and sigs[t]["signal"])
                if others_signal:
                    before = cash
                    do_sell(sym, ed)
                    swap_proceeds += cash - before

        # 买入阶段：空仓（无持仓）平均买所有信号标的；换仓资金平均投入信号标的
        targets = [t for t in pair_by_etf if sigs[t]["signal"] and t not in holdings]
        if targets and swap_proceeds > 0:
            per = swap_proceeds / len(targets)
            for t in targets:
                do_buy(t, per, ed)
        elif targets and not holdings:
            per = cash / len(targets)
            for t in targets:
                do_buy(t, per, ed)

        if not holdings:
            idle_days += 1
        value = cash + sum(h["shares"] * last_close.get(sym, 0) for sym, h in holdings.items())
        curve.append(value)

    v = np.array(curve)
    total = (v[-1] / 1000000 - 1) * 100
    daily = np.diff(v) / v[:-1]
    mdd = float((v / np.maximum.accumulate(v) - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0
    return {"total": total, "mdd": mdd, "sharpe": sharpe,
            "idle_ratio": idle_days / len(trading_days) * 100,
            "buys": [t for t in trades if t["action"] == "BUY"],
            "sells": [t for t in trades if t["action"] == "SELL"]}


def run_single(pairs, swap_threshold, fear_map, bars_map, trading_days, greed=70.0):
    """单持仓（最恐慌）：空仓买最恐慌的；持仓贪卖/换仓。vthr 为 log-z 阈值。"""
    cash = 1000000.0
    position = None
    trades = []
    curve = []
    idle_days = 0
    last_close = {}

    def price(etf, day, kind):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        return float(sub.iloc[0][kind]) if not sub.empty else None

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
            lz = float(s2["log_z"].iloc[0]) if not s2.empty else np.nan
            sigs[te] = (f is not None and f <= b and np.isfinite(lz) and lz >= v, f)
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
        if position is None:
            idle_days += 1
        value = cash + (position[2] * last_close.get(position[0], 0) if position else 0)
        curve.append(value)

    v = np.array(curve)
    total = (v[-1] / 1000000 - 1) * 100
    daily = np.diff(v) / v[:-1]
    mdd = float((v / np.maximum.accumulate(v) - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0
    return {"total": total, "mdd": mdd, "sharpe": sharpe,
            "idle_ratio": idle_days / len(trading_days) * 100,
            "buys": [t for t in trades if t["action"] == "BUY"],
            "sells": [t for t in trades if t["action"] == "SELL"]}


def count_signals(pairs, fear_map, bars_map, trading_days):
    """统计信号日与同日多信号天数。"""
    multi = 0
    days = 0
    total = 0
    for i in range(1, len(trading_days)):
        sd = trading_days[i - 1]
        n = 0
        for fi, ve, te, nm, b, v, g in pairs:
            f = fear_map.get(fi, {}).get(sd)
            row = bars_map[ve]
            s2 = row[row["trade_date"] == sd]
            lz = float(s2["log_z"].iloc[0]) if not s2.empty else np.nan
            if f is not None and f <= b and np.isfinite(lz) and lz >= v:
                n += 1
        if n > 0:
            days += 1
            total += n
            if n >= 2:
                multi += 1
    return days, multi, total


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "512480.SH", TRADE_ETF, "588000.SH", "QQQ.US"],
    )
    bars_map = add_log_z(bars_map)
    GRIDS = [
        (30.0, 25.0, 20.0, "30/25/20(当前)"),
        (35.0, 30.0, 25.0, "35/30/25"),
        (40.0, 35.0, 30.0, "40/35/30(信号密集)"),
        (45.0, 40.0, 35.0, "45/40/35"),
    ]
    print("== 单持仓 vs 多持仓（统一log-z 1.25 + 贪即卖 + 换仓45，悲观口径）==")
    for b1, b2, b3, label in GRIDS:
        pairs = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", b1, 1.25, 70.0),
            ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", b2, 1.25, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", b3, 1.25, 70.0),
        ]
        days, multi, total = count_signals(pairs, fear_map, bars_map, trading_days)
        rs = run_single(pairs, 45.0, fear_map, bars_map, trading_days)
        rm = run_multi(pairs, 45.0, fear_map, bars_map, trading_days)
        print(f"\n恐慌 {label}: 信号日{days} 多信号日{multi} (总信号{total})")
        print(f"  单持仓: {rs['total']:8.2f}% 回撤 {rs['mdd']:6.2f}% 夏普 {rs['sharpe']:.2f} 空仓 {rs['idle_ratio']:.1f}% 买卖 {len(rs['buys'])}/{len(rs['sells'])}")
        print(f"  多持仓: {rm['total']:8.2f}% 回撤 {rm['mdd']:6.2f}% 夏普 {rm['sharpe']:.2f} 空仓 {rm['idle_ratio']:.1f}% 买卖 {len(rm['buys'])}/{len(rm['sells'])}")
        if multi > 0:
            print(f"  → 多持仓胜出: {rm['total'] - rs['total']:+.2f}pp")
