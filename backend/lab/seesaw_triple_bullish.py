#!/usr/bin/env python3
"""三标的轮动 + 阳线过滤：恐慌≤阈值 且 量比≥阈值 且 信号日收阳（close>open）才买入。

pairs = (fear_index, volume_etf, trade_etf, name, buy, vr)：量比用 volume_etf（纳指用 QQQ），
交易用 trade_etf（159941），阳线看 trade_etf 信号日 close>open。
对比基准：无阳线过滤 302.38%（量比QQQ，滑点0）。
"""

from __future__ import annotations

import itertools
import sys

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

# 三标的：红利/科创50（A股量比+交易），纳指（QQQ量比 + 159941交易）
BASE_TWO = [
    ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6),
    ("000688.SH", "588000.SH", "588000.SH", "科创50", 25.0, 1.6),
]
NASDAQ = ("QQQ.US", "QQQ.US", "159941.SZ", "纳指", 20.0, 1.3)


def signal_with_bullish(pair, day, fear_map, bars_map, require_bullish, bullish_on="trade"):
    fear_index, volume_etf, trade_etf, _name, bthr, vthr = pair
    f = fear_map.get(fear_index, {}).get(day)
    row = bars_map.get(volume_etf)
    if row is None:
        return False, np.nan if False else float("nan")
    sub = row[row["trade_date"] == day]
    vr = float(sub["volume_ratio"].iloc[0]) if not sub.empty else float("nan")
    if not (f is not None and f <= bthr and (vr == vr) and vr >= vthr):
        return False, f if f is not None else float("nan")
    if require_bullish:
        bsrc = volume_etf if bullish_on == "volume" else trade_etf
        tro = bars_map.get(bsrc)
        ts = tro[tro["trade_date"] == day] if tro is not None else None
        if ts is None or ts.empty or not (float(ts.iloc[0]["close"]) > float(ts.iloc[0]["open"])):
            return False, f
    return True, f


def run(pairs, fear_map, bars_map, trading_days, greed_threshold=70.0, swap_threshold=45.0, require_bullish=True, bullish_on="trade"):
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
        fi, ve, te, nm, b, v = pair
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

    for i in range(1, len(trading_days)):
        ed = trading_days[i]
        sd = trading_days[i - 1]
        for p in pairs:
            sub = bars_map[p[2]][bars_map[p[2]]["trade_date"] == ed]
            if not sub.empty:
                last_close[p[2]] = float(sub.iloc[0]["close"])
        sigs = {p[2]: signal_with_bullish(p, sd, fear_map, bars_map, require_bullish, bullish_on) for p in pairs}

        if position is None:
            cands = [p for p in pairs if sigs[p[2]][0]]
            if cands:
                t = min(cands, key=lambda p: sigs[p[2]][1])
                do_buy(t, ed)
        else:
            hf = sigs.get(position[0], (False, float("nan")))[1]
            if hf == hf and hf >= greed_threshold:
                do_sell(ed)
            elif hf == hf and hf > swap_threshold:
                others = [p for p in pairs if p[2] != position[0] and sigs[p[2]][0]]
                if others:
                    t = min(others, key=lambda p: sigs[p[2]][1])
                    do_sell(ed)
                    do_buy(t, ed)
        value = cash + (position[2] * last_close.get(position[0], 0) if position else 0)
        curve.append(value)

    v = __import__("numpy").array(curve)
    total = v[-1] / 1000000 - 1
    import math
    daily = __import__("numpy").diff(v) / v[:-1]
    mdd = float((v / __import__("numpy").maximum.accumulate(v) - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "buys": len(buys), "sells": len(sells),
            "buy_list": buys, "sell_list": sells}


if __name__ == "__main__":
    import os
    BULLISH_ON = os.environ.get("BULLISH_ON", "trade")
    print(f"阳线口径: {BULLISH_ON} | 对比基准（无阳线过滤）：302.38% / 回撤7.89% / 夏普2.57（量比QQQ 滑点0）")
    fear_map, bars_map, trading_days = sp.load_data(
        ["000015.SH", "000688.SH", "QQQ.US"], ["510880.SH", "588000.SH", "159941.SZ", "QQQ.US"]
    )
    rows = []
    for buy1, vr1, buy2, vr2, buy3, vr3, swap in itertools.product(
        [25.0, 30.0, 35.0], [1.3, 1.6],      # 红利
        [20.0, 25.0, 30.0], [1.3, 1.6],      # 科创50
        [15.0, 20.0, 25.0], [1.0, 1.3],      # 纳指
        [45.0, 55.0],
    ):
        pairs = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", buy1, vr1),
            ("000688.SH", "588000.SH", "588000.SH", "科创50", buy2, vr2),
            ("QQQ.US", "QQQ.US", "159941.SZ", "纳指", buy3, vr3),
        ]
        r = run(pairs, fear_map, bars_map, trading_days, swap_threshold=swap, require_bullish=True, bullish_on=BULLISH_ON)
        rows.append({"buy1": buy1, "vr1": vr1, "buy2": buy2, "vr2": vr2, "buy3": buy3, "vr3": vr3, "swap": swap, **r})

    print(f"\n=== 阳线过滤网格 {len(rows)} 组 top12（按总收益）===")
    for r in sorted(rows, key=lambda r: -r["total"])[:12]:
        print(f"  红利{buy1 if False else r['buy1']:g}/{r['vr1']:g} 科创{r['buy2']:g}/{r['vr2']:g} 纳指{r['buy3']:g}/{r['vr3']:g} 换仓{r['swap']:g} "
              f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    print(f"\n=== 按夏普 top8 ===")
    for r in sorted(rows, key=lambda r: -r["sharpe"])[:8]:
        print(f"  红利{r['buy1']:g}/{r['vr1']:g} 科创{r['buy2']:g}/{r['vr2']:g} 纳指{r['buy3']:g}/{r['vr3']:g} 换仓{r['swap']:g} "
              f"总收益 {r['total']:7.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")
