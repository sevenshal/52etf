#!/usr/bin/env python3
"""统一 log 放量实验：放量 = log(vol[T]) > mean(log(vol[T-20..T-1])) + k*std(...)。

替换原"每个标的自有量比阈值"为统一 k（k 在 1 附近搜索）。最悲观口径（滑点-1）。
三标的：红利 + 科创50信号→半导体交易 + 纳指159941；4 标的再 +黄金518880（自算恐贪≤45）。
"""

import math
import sys
from datetime import date

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import numpy as np
import pandas as pd

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159941.SZ"
GOLD = "518880.SH"
K_VALUES = [1.0, 1.25, 1.5, 1.75, 2.0]


def normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def add_log_z(bars_map):
    """在 bars_map 每个标的上加 log_z 列（对数放量 z 值），并保留旧 volume_ratio。"""
    for sym, df in bars_map.items():
        s = df.sort_values("trade_date").reset_index(drop=True)
        s["log_vol"] = np.log(s["volume"].replace(0, np.nan))
        prior_log_mean = s["log_vol"].shift(1).rolling(20, min_periods=20).mean()
        prior_log_std = s["log_vol"].shift(1).rolling(20, min_periods=20).std(ddof=0)
        s["log_z"] = (s["log_vol"] - prior_log_mean) / prior_log_std.replace(0, np.nan)
        bars_map[sym] = s
    return bars_map


def build_gold_fear(bars_map):
    df = bars_map[GOLD].sort_values("trade_date").reset_index(drop=True)
    px = df["close"]
    mean = px.rolling(252, min_periods=120).mean()
    std = px.rolling(252, min_periods=120).std(ddof=0)
    z = (px - mean) / std.replace(0, float("nan"))
    return {d: normal_cdf(v) * 100 for d, v in zip(df["trade_date"], z) if v == v}


def run_case(pairs, swap_threshold, fear_map, bars_map, trading_days):
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

    # 旧体系基准（三标的，各自量比阈值 1.6/1.6/1.3）——直接跑旧 run_case 需要旧列，跳过：
    # 用 sp 已有 volume_ratio 的旧逻辑跑一遍基准
    def run_case_old(pairs, swap_threshold, fear_map, bars_map, trading_days):
        cash = 1000000.0
        position = None
        trades = []
        curve = []
        last_close = {}

        def price(etf, day, kind="open"):
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
        return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "buys": sum(1 for t in trades if t["action"] == "BUY"),
                "sells": sum(1 for t in trades if t["action"] == "SELL")}

    base3_old = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, 1.6, 70.0),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
    ]
    r_old = run_case_old(base3_old, 45.0, fear_map, bars_map, trading_days)
    print(f"旧体系基准 三标的（各自量比阈值）: {r_old['total']:.2f}% 回撤 {r_old['mdd']:.2f}% 夏普 {r_old['sharpe']:.2f}")

    print("\n== 三标的（红利 + 科创50信号→半导体 + 纳指159941）统一 k 搜索 ==")
    rows3 = []
    for k in K_VALUES:
        pairs3 = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, k, 70.0),
            ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, k, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, k, 70.0),
        ]
        r = run_case(pairs3, 45.0, fear_map, bars_map, trading_days)
        rows3.append({"k": k, **r})
    for r in sorted(rows3, key=lambda r: -r["total"]):
        print(f"  k={r['k']:.2f} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} "
              f"买卖 {r['buys']}/{r['sells']} 空仓 {r['idle_ratio']:.1f}%")

    print("\n== 4 标的（+黄金518880 恐慌≤45）统一 k 搜索 ==")
    rows4 = []
    for k in K_VALUES:
        pairs4 = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, k, 70.0),
            ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, k, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, k, 70.0),
            ("GOLD_SELF", GOLD, GOLD, "黄金", 45.0, k, 70.0),
        ]
        r = run_case(pairs4, 45.0, fear_map, bars_map, trading_days)
        rows4.append({"k": k, **r})
    for r in sorted(rows4, key=lambda r: -r["total"]):
        print(f"  k={r['k']:.2f} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} "
              f"买卖 {r['buys']}/{r['sells']} 空仓 {r['idle_ratio']:.1f}%")

    # 4 标的 k=1.25 明细
    pairs4b = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.25, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, 1.25, 70.0),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.25, 70.0),
        ("GOLD_SELF", GOLD, GOLD, "黄金", 45.0, 1.25, 70.0),
    ]
    rb = run_case(pairs4b, 45.0, fear_map, bars_map, trading_days)
    print(f"\n=== 4 标的 k=1.25 {rb['total']:.2f}% 明细 ===")
    for t in rb["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in rb["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))
