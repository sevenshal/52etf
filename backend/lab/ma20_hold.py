#!/usr/bin/env python3
"""MA20 持有引擎 v3：恐慌+放量买入信号 → 全仓买入，持有到收盘价从 MA20 上方跌破 MA20 才卖出。

规则（用户指定）：
- 买入：恐慌≤b 且量比≥v 的信号（T-1 确认，T 日开盘成交），多标的信号选恐贪最低全仓买入。
- 持有：不做贪卖（≥70 不再卖）、不做换仓（>45 不再换）。
- 卖出：持仓 ETF 收盘价跌破 MA20，且必须是从 MA20 上方跌破（买入后一直在 MA20 下方则一直持有）。
- 跌破确认在收盘，次日开盘卖出。

对比基准：原引擎（贪卖70+换仓45）378.01% / -9.80% / 2.54；趋势补位版 1266.50% / -20.33% / 2.56。
"""

import itertools
import math
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"


def compute_ma(bars, win):
    df = bars.copy()
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["ma"] = df["close"].rolling(win).mean()
    out = {}
    for r in df.itertuples():
        ma = float(r.ma) if pd.notna(r.ma) else None
        out[str(r.trade_date)] = (float(r.close), ma)
    return out


def run_case(pairs, fear_map, bars_map, trading_days, ma_win=20, cost_pct=0.0, swap_threshold=None):
    cash = 1000000.0
    position = None
    prev_above = False
    pending_sell = False
    trades = []
    curve = []
    last_close = {}
    pair_by_etf = {p[2]: p for p in pairs}
    ma_map = {p[2]: compute_ma(bars_map[p[2]], ma_win) for p in pairs}

    def price(etf, day, kind="open"):
        row = bars_map[etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        return float(sub.iloc[0][kind])

    def do_buy(pair, day):
        nonlocal cash, position, prev_above, pending_sell
        fi, ve, te, nm, b, v, g = pair
        op = price(te, day)
        if op is None or op <= 0:
            return
        qty = int(cash // op)
        if qty >= 1:
            fee = qty * op * cost_pct
            cash -= qty * op + fee
            position = (te, nm, qty, qty * op + fee)
            trades.append({"date": str(day), "action": "BUY", "symbol": te, "qty": qty, "price": op})
            # 买入日收盘状态（用于"从上方跌破"判定）
            cm = ma_map.get(te, {}).get(str(day))
            if cm:
                close_t, ma_t = cm
                prev_above = ma_t is not None and close_t > ma_t
            else:
                prev_above = False
            pending_sell = False

    def do_sell(day):
        nonlocal cash, position
        te, nm, qty, cost = position
        op = price(te, day)
        if op is None or op <= 0:
            return
        fee = qty * op * cost_pct
        cash += qty * op - fee
        trades.append({"date": str(day), "action": "SELL", "symbol": te, "qty": qty, "price": op, "pnl": qty * op - fee - cost})
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
            fi, ve, te, nm, b, v, g = p
            f = fear_map.get(fi, {}).get(sd)
            row = bars_map[ve]
            s2 = row[row["trade_date"] == sd]
            vr = float(s2["volume_ratio"].iloc[0]) if not s2.empty else np.nan
            sigs[te] = (f is not None and f <= b and np.isfinite(vr) and vr >= v, f)

        # 1) 开盘：先执行昨日确认的 MA20 跌破卖出
        if pending_sell and position is not None:
            do_sell(ed)
            pending_sell = False

        # 2) 开盘：空仓则按信号买入
        if position is None:
            cands = [p for p in pairs if sigs[p[2]][0]]
            if cands:
                t = min(cands, key=lambda p: sigs[p[2]][1])
                do_buy(t, ed)
        elif swap_threshold is not None:
            # 换仓：持仓恐贪>swap 且其他有信号 → 卖出（次日? 原引擎同日开盘卖+买）
            hf = sigs.get(position[0], (False, np.nan))[1]
            if hf is not None and np.isfinite(hf) and hf > swap_threshold:
                others = [p for p in pairs if p[2] != position[0] and sigs[p[2]][0]]
                if others:
                    do_sell(ed)
                    t = min(others, key=lambda p: sigs[p[2]][1])
                    do_buy(t, ed)

        # 3) 收盘：更新 MA20 上方/下方状态，确认跌破信号
        if position is not None:
            te = position[0]
            cm = ma_map.get(te, {}).get(str(ed))
            if cm:
                close_t, ma_t = cm
                above = ma_t is not None and close_t > ma_t
                if prev_above and ma_t is not None and close_t < ma_t:
                    pending_sell = True  # 从上方跌破 → 次日开盘卖
                prev_above = above

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
        ["000015.SH", "000688.SH", "QQQ.US"],
        ["510880.SH", "588000.SH", "512480.SH", TRADE_ETF, "QQQ.US"],
    )
    base3 = [
        ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6, 70.0),
        ("000688.SH", "588000.SH", "512480.SH", "半导体(科创信号)", 25.0, 1.6, 70.0),
        ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", 20.0, 1.3, 70.0),
    ]
    print("基准：原引擎 378.01%/-9.80%/2.54；趋势补位 1266.50%/-20.33%/2.56")

    print("\n== MA20 持有引擎 v3（信号买入，跌破 MA20 卖）==")
    for win in (10, 20, 30):
        r = run_case(base3, fear_map, bars_map, trading_days, ma_win=win)
        print(f"  MA{win:2d}: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    # 信号参数微调网格（MA20 固定）
    print("\n== 混合版（信号买入 + 换仓45保留 + MA20跌破卖，无贪卖70）==")
    for win in (20, 30):
        r = run_case(base3, fear_map, bars_map, trading_days, ma_win=win, swap_threshold=45.0)
        print(f"  MA{win:2d} 换仓45: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")
    # 贪卖70+MA20跌破卖（双保险）
    r = run_case(base3, fear_map, bars_map, trading_days, ma_win=20, swap_threshold=None)
    # 手动:贪卖在持有分支加? 简化用 swap_threshold=70 试（>70换/卖逻辑不同）——跳过

    print("\n== MA20 + 信号参数网格 ==")
    rows = []
    for rb_, rv, kb, kv, nb, nv in itertools.product(
        [30.0], [1.6], [20.0, 25.0, 30.0], [1.3, 1.6], [15.0, 20.0, 25.0], [1.3]
    ):
        pairs = [
            ("000015.SH", "510880.SH", "510880.SH", "红利", rb_, rv, 70.0),
            ("000688.SH", "588000.SH", "512480.SH", "半导体(科创信号)", kb, kv, 70.0),
            ("QQQ.US", "QQQ.US", TRADE_ETF, "纳指", nb, nv, 70.0),
        ]
        r = run_case(pairs, fear_map, bars_map, trading_days, ma_win=20)
        rows.append({"kb": kb, "kv": kv, "nb": nb, **r})
    for r in sorted(rows, key=lambda r: -r["total"])[:8]:
        print(f"  科创<={r['kb']:g}/{r['kv']:g} 纳指<={r['nb']:g} {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    # 最优明细（MA20 基准参数）
    print("\n=== MA20 基准参数明细 ===")
    rb = run_case(base3, fear_map, bars_map, trading_days, ma_win=20)
    for t in rb["buy_list"]:
        print("  买", t["date"], t["symbol"], round(t["price"], 3))
    for t in rb["sell_list"]:
        print("  卖", t["date"], t["symbol"], round(t["price"], 3), "pnl", round(t["pnl"]))
