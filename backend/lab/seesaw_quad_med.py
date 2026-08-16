#!/usr/bin/env python3
"""4 标的轮动：红利 + 科创50信号→半导体交易 + 纳指159509 + 创新药。

搜索创新药候补参数（恐慌阈值 × 量比），其他标的固定最优参数：
红利 30/1.6、科创50信号 25/1.6→交易512480、纳指(QQQ恐贪) 20/1.3→交易159509，换仓45，贪70。
基准：3 标的（无创新药）378.01%/-9.80%/2.54。
创新药恐贪无期权组件（PCR 0/825），量比源=交易标的自身 515120.SH。
"""

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"
MED_ETF = "515120.SH"  # 创新药ETF


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
        ["000015.SH", "000688.SH", "QQQ.US", "931152.CSI"],
        ["510880.SH", "588000.SH", "512480.SH", TRADE_ETF, "QQQ.US", MED_ETF],
    )
    print(f"创新药恐贪可用天数: {len(fear_map.get('931152.CSI', {}))}, 量比源 {MED_ETF} 交易 {MED_ETF}")
    print("基准（3标的）: 378.01% / -9.80% / 2.54")

    base3 = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "半导体(科创信号)", 25.0, 1.6, 70.0),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
    ]
    r3 = run_case(base3, 45.0, fear_map, bars_map, trading_days)
    print(f"复跑3标的: {r3['total']:.2f}% / {r3['mdd']:.2f}% / {r3['sharpe']:.2f} 买卖 {r3['buys']}/{r3['sells']}")

    rows = []
    for mb, mv in itertools.product([15.0, 18.0, 20.0, 22.0, 25.0, 28.0, 30.0], [1.3, 1.6, 2.0, 2.5]):
        pairs = base3 + [("931152.CSI", MED_ETF, MED_ETF, "创新药", mb, mv, 70.0)]
        r = run_case(pairs, 45.0, fear_map, bars_map, trading_days)
        rows.append({"mb": mb, "mv": mv, **r})

    print("\n== 4标的（+创新药）全部 28 组 ==")
    for r in sorted(rows, key=lambda r: -r["total"]):
        tag = " <== 创新药未触发" if r["buys"] == r3["buys"] and r["sells"] == r3["sells"] else ""
        print(f"  创新药恐慌<={r['mb']:g} 量比>={r['mv']:g} "
              f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}{tag}")
    print("\n== 4标的 按夏普 top6 ==")
    for r in sorted(rows, key=lambda r: -r["sharpe"])[:6]:
        print(f"  创新药恐慌<={r['mb']:g} 量比>={r['mv']:g} "
              f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    # 触发组合明细（恐慌<=25 量比>=1.6，321.30%）
    best_pairs = base3 + [("931152.CSI", MED_ETF, MED_ETF, "创新药", 25.0, 1.6, 70.0)]
    rb = run_case(best_pairs, 45.0, fear_map, bars_map, trading_days)
    print(f"\n=== 创新药恐慌<=25 量比>=1.6 {rb['total']:.2f}% 明细 ===")
    for t in rb["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in rb["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))
