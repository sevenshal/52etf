#!/usr/bin/env python3
"""用重算的半导体恐贪（科创50期权代理）跑三标的：红利 + 半导体信号/交易 + 纳指159509。

对比：线上半导体恐贪（无期权）版 188.85%/-9.80%/2.07（量比1.6）。
重算版恐贪从 2023-12 起（期权数据2023-06起+120交易日滚动）。
"""

import itertools
import json
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
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "buys": len(buys), "sells": len(sells),
            "buy_list": buys, "sell_list": sells}


if __name__ == "__main__":
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "H30184.CSI", "QQQ.US"],
        ["510880.SH", "512480.SH", TRADE_ETF, "QQQ.US"],
    )
    # 用重算的半导体恐贪覆盖线上（2023-12 起）
    with open("/tmp/semi_fear_new.json") as f:
        payload = json.load(f)
    from datetime import date as _d
    new_semi = {_d.fromisoformat(d): s for d, s in zip(payload["date"], payload["score"])}
    print(f"半导体恐贪替换: {len(new_semi)} 天")

    def run_all(label, fear_override):
        fm = dict(fear_map)
        if fear_override is not None:
            fm["H30184.CSI"] = fear_override
        print(f"\n== {label} 三标的 top ==")
        for bs, vrs in itertools.product([25.0, 28.0, 30.0, 35.0], [1.3, 1.6]):
            pairs = [
                ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
                ("H30184.CSI", "512480.SH", "512480.SH", "半导体", bs, vrs, 70.0),
                ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
            ]
            r = run_case(pairs, 45.0, fm, bars_map, trading_days)
            print(f"  恐慌<={bs:g} 量比>={vrs:g} 总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    run_all("线上半导体恐贪（无期权）", None)
    run_all("重算半导体恐贪（带科创50期权）", new_semi)

    # 明细对比（线上 ≤28/1.3 vs 重算 ≤28/1.3）
    for label, ov in (("线上", None), ("重算", new_semi)):
        fm = dict(fear_map)
        if ov is not None:
            fm["H30184.CSI"] = ov
        pairs = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
            ("H30184.CSI", "512480.SH", "512480.SH", "半导体", 28.0, 1.3, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
        ]
        rb = run_case(pairs, 45.0, fm, bars_map, trading_days)
        print(f"\n=== {label}（恐慌<=28 量比>=1.3） {rb['total']:.2f}% 买卖 {rb['buys']}/{rb['sells']} ===")
        for t in rb["buy_list"]:
            print("  买", t["date"], t["symbol"], round(t["price"], 3))
        for t in rb["sell_list"]:
            print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))
    sys.exit(0)
    for t in rb["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in rb["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))
