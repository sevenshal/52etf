#!/usr/bin/env python3
"""159509（景顺长城纳指科技ETF）替换 159941 的三标的轮动对比。

pairs = (fear_index, volume_etf, trade_etf, name, buy, vr)：
- 恐贪：QQQ.US（qqq_clone）
- 量比：QQQ.US 成交量
- 交易：159509.SZ（原 159941.SZ）
对比：159941 版 302.38%（滑点0）/ 193.92%（悲观-1）。
"""

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"  # 改为 159941.SZ 即原版


def load(trade_etf):
    return sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "588000.SH", trade_etf, "QQQ.US"],
    )


def run_case(pairs, greed_threshold, swap_threshold, pessimistic):
    import lab.seesaw_pessimistic as _sp
    import math

    import numpy as np

    fear_map, bars_map, trading_days = _sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"], ["510880.SH", "588000.SH", TRADE_ETF, "QQQ.US"]
    )
    cash = 1000000.0
    position = None
    trades = []
    curve = []
    last_close = {}

    def price(etf, day, kind):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        return float(sub.iloc[0][kind])

    def do_buy(pair, day):
        nonlocal cash, position
        fi, ve, te, nm, b, v = pair
        op = price(te, day, "high" if pessimistic else "open")
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
        op = price(te, day, "low" if pessimistic else "open")
        if op is None or op <= 0:
            return
        cash += qty * op
        trades.append({"date": str(day), "action": "SELL", "symbol": te, "qty": qty, "price": op, "pnl": qty * op - cost})
        position = None

    for i in range(1, len(trading_days)):
        ed = trading_days[i]
        sd = trading_days[i - 1]
        for p in pairs:
            sub = bars_map[p[2]][bars_map[p[2]]["trade_date"] == ed]
            if not sub.empty:
                last_close[p[2]] = float(sub.iloc[0]["close"])
        sigs = {}
        for p in pairs:
            fi, ve, te, nm, b, v = p
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
            hf = sigs.get(position[0], (False, np.nan))[1]
            if hf is not None and np.isfinite(hf) and hf >= greed_threshold:
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


def run(trade_etf_label):
    print(f"\n===== 交易标的 = {trade_etf_label}（恐贪 qqq_clone + 量比 QQQ.US）=====")
    # 推荐参数 + 纳指参数小幅网格
    for b3, v3, swap, pessimistic in itertools.product([15.0, 20.0, 25.0], [1.0, 1.3], [45.0], [False, True]):
        pairs = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6),
            ("000688.SH", "588000.SH", "588000.SH", "科创50", 25.0, 1.6),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指科技", b3, v3),
        ]
        r = run_case(pairs, 70.0, swap, pessimistic)
        tag = "悲观" if pessimistic else "正常"
        print(f"  纳指恐慌<={b3:g} 量比>={v3:g} 换仓{swap:g} [{tag}] "
              f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")


if __name__ == "__main__":
    print("对比基准（159941）：正常 302.38% / -7.89% / 2.57；悲观 193.92% / -7.89% / 2.18")
    run("159509.SZ")
    # 看 159509 的最优正常/悲观交易明细
    pairs = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6),
        ("000688.SH", "588000.SH", "588000.SH", "科创50", 25.0, 1.6),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指科技", 20.0, 1.3),
    ]
    r = run_case(pairs, 70.0, 45.0, False)
    print("\n=== 159509 正常（恐慌20/量比1.3/换仓45）交易明细 ===")
    for t in r["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in r["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))
