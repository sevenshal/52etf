#!/usr/bin/env python3
"""x=0.25 完整明细：旧体系三标的(300.30%) / 4标的(309.84%)，逐笔买卖 + 年度收益 + 口径对比。

滑点口径：-1=买当日最高卖当日最低（最悲观）；-2=买最低卖最高（最乐观）；0=按开盘价。
"""

import math
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import numpy as np

import lab.seesaw_pessimistic as sp
from lab.log_volume_signal import add_log_z, build_gold_fear

TRADE_ETF = "159941.SZ"
GOLD = "518880.SH"
SHRINK_X = 0.25


def run_case(pairs, swap_threshold, fear_map, bars_map, trading_days, slippage=-1.0):
    """slippage: -2 乐观 / -1 悲观 / 0 开盘价。卖出条件 = 贪70 且缩量 0.25σ。"""
    cash = 1000000.0
    position = None
    trades = []
    curve = []
    idle_days = 0
    last_close = {}
    entry_date = None
    entry_price = None

    def price(etf, day, kind):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        return float(sub.iloc[0][kind])

    def fill(etf, day, side):
        if slippage < 0:
            if slippage <= -1.5:  # -2 乐观
                return price(etf, day, "low" if side == "buy" else "high")
            return price(etf, day, "high" if side == "buy" else "low")  # -1 悲观
        return price(etf, day, "open")

    def logz_at(etf, day):
        row = bars_map[etf]
        s2 = row[row["trade_date"] == day]
        return float(s2["log_z"].iloc[0]) if not s2.empty else np.nan

    def do_buy(pair, day):
        nonlocal cash, position, entry_date, entry_price
        fi, ve, te, nm, b, v, g = pair
        op = fill(te, day, "buy")
        if op is None or op <= 0:
            return
        qty = int(cash // op)
        if qty >= 1:
            cash -= qty * op
            position = (te, nm, qty, qty * op)
            entry_date = day
            entry_price = op
            trades.append({"date": str(day), "action": "BUY", "symbol": te, "qty": qty, "price": op})

    def do_sell(day):
        nonlocal cash, position, entry_date, entry_price
        te, nm, qty, cost = position
        op = fill(te, day, "sell")
        if op is None or op <= 0:
            return
        cash += qty * op
        trades.append({"date": str(day), "action": "SELL", "symbol": te, "qty": qty, "price": op,
                       "pnl": qty * op - cost, "pnl_pct": (op / entry_price - 1) * 100,
                       "entry_date": str(entry_date), "entry_price": entry_price,
                       "hold_days": (day - entry_date).days})
        position = None
        entry_date = None
        entry_price = None

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
            held_logz = logz_at(position[0], sd)
            shrink_ok = np.isfinite(held_logz) and held_logz <= -SHRINK_X
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
    ann = (v[-1] / v[0]) ** (252 / len(v)) - 1 if v[0] > 0 else 0
    # 年度收益
    yearly = {}
    for j, dt in enumerate(trading_days[1:]):
        y = str(dt)[:4]
        yearly.setdefault(y, []).append(v[j])
    yearly_ret = {}
    prev_end = 1000000.0
    years_sorted = sorted(yearly.keys())
    for idx, y in enumerate(years_sorted):
        seg = v[[k for k, d in enumerate(trading_days[1:]) if str(d)[:4] == y]]
        if len(seg):
            yearly_ret[y] = (seg[-1] / prev_end - 1) * 100
            prev_end = seg[-1]
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "ann": ann * 100,
            "idle_ratio": idle_days / len(trading_days) * 100,
            "buy_list": buys, "sell_list": sells, "yearly": yearly_ret}


def print_detail(label, pairs):
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "512480.SH", TRADE_ETF, GOLD, "588000.SH", "QQQ.US"],
    )
    bars_map = add_log_z(bars_map)
    fear_map["GOLD_SELF"] = build_gold_fear(bars_map)
    print(f"\n########## {label} ##########")
    # 口径对比
    for sl, sl_label in ((-2.0, "最乐观(买最低卖最高)"), (-1.0, "最悲观(买最高卖最低)"), (0.0, "按开盘价成交")):
        r = run_case(pairs, 45.0, fear_map, bars_map, trading_days, slippage=sl)
        print(f"  [{sl_label}] 总收益 {r['total']:8.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} "
              f"空仓 {r['idle_ratio']:.1f}% 年度: " + " ".join(f"{y}:{v:.0f}%" for y, v in r["yearly"].items()))
    r = run_case(pairs, 45.0, fear_map, bars_map, trading_days, slippage=-1.0)
    print(f"\n  悲观口径逐笔明细（买入 {len(r['buy_list'])} / 卖出 {len(r['sell_list'])}）:")
    for s in r["sell_list"]:
        b = next((t for t in r["buy_list"] if t["date"] == s["entry_date"] and t["symbol"] == s["symbol"]), None)
        print(f"    {s['entry_date']} 买 {s['symbol']} @{s['entry_price']:.3f}  →  {s['date']} 卖 @{s['price']:.3f}  "
              f"持有{s['hold_days']}天 收益率{s['pnl_pct']:+.1f}% 盈亏{s['pnl']:+,.0f}")
    # 期末持仓
    last_pos = [t for t in r["buy_list"] if not any(t["date"] == s["entry_date"] and t["symbol"] == s["symbol"] for s in r["sell_list"])]
    if last_pos:
        for t in last_pos:
            print(f"    {t['date']} 买 {t['symbol']} @{t['price']:.3f} （期末仍持有，未平仓）")


if __name__ == "__main__":
    TRADE = "159941.SZ"
    pairs3 = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "科创50信号→半导体", 25.0, 1.6, 70.0),
        ("QQQ.US", "QQQ.US", TRADE, "纳指", 20.0, 1.3, 70.0),
    ]
    pairs4 = pairs3 + [("GOLD_SELF", GOLD, GOLD, "黄金", 45.0, 1.3, 70.0)]
    print_detail("三标的 x=0.25（旧体系各自量比阈值）", pairs3)
    print_detail("4 标的 x=0.25（旧体系各自量比阈值）", pairs4)
