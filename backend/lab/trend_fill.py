#!/usr/bin/env python3
"""趋势补位实验：原引擎空仓时，若趋势标的（科创50/半导体/纳指）处于上升趋势则买入对应 ETF，
跌破均线或原信号出现时切换。目的：减少空仓、吃满上涨段。

对比基准：原引擎 378.01% / -9.80% / 2.54（空仓 67% 时间）。
"""

import math
import sys

import numpy as np

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp

TRADE_ETF = "159509.SZ"


def load_index_close():
    """从生产 duckdb 读指数日线（000688/H30184），返回 {code: {date: close}}。"""
    import duckdb

    src = duckdb.connect("/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True)
    df = src.execute(
        "SELECT ts_code, trade_date, close FROM a_stock_index_daily "
        "WHERE upper(ts_code) IN ('000688.SH','H30184.CSI','000015.SH') AND trade_date BETWEEN '2022-01-01' AND '2026-08-14'"
    ).fetchdf()
    src.close()
    out = {}
    for r in df.itertuples():
        out.setdefault(str(r.ts_code).upper(), {})[str(r.trade_date)[:10]] = float(r.close)
    return out


def run_case(pairs, swap_threshold, fear_map, bars_map, trading_days,
             trend_slots=None, ma_win=20, cost_pct=0.0, min_gap=0.0, trend_max_fear=None,
             greed_only_signal=False, trend_no_swap=False, cooldown_days=0):
    """trend_slots: [(fear_ignore, index_code, trade_etf, name), ...] 空仓时的趋势补位候选。"""
    cash = 1000000.0
    position = None
    trades = []
    curve = []
    last_close = {}
    pair_by_etf = {p[2]: p for p in pairs}
    held_days = 0
    cooldown_sell_date = {}

    def in_cooldown(etf, cur_i):
        s = cooldown_sell_date.get(etf)
        return s is not None and cooldown_days > 0 and (cur_i - s) <= cooldown_days

    index_close = load_index_close()
    # QQQ 收盘（趋势也用）
    qqq = bars_map["QQQ.US"]
    qqq_close = {str(r["trade_date"]): float(r["close"]) for _, r in qqq.iterrows()}
    idx_series = {"QQQ.US": qqq_close}
    idx_series.update(index_close)

    def ma(series, day, win):
        dates = sorted(series)
        vals = []
        for d in dates:
            if d > day:
                break
            vals.append(series[d])
            if len(vals) > win:
                vals.pop(0)
        if len(vals) >= win:
            return sum(vals) / len(vals)
        return None

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
            fee = qty * op * cost_pct
            cash -= qty * op + fee
            position = (te, nm, qty, qty * op + fee)
            trades.append({"date": str(day), "action": "BUY", "symbol": te, "qty": qty, "price": op})

    def do_buy_trend(te, nm, day):
        nonlocal cash, position
        op = price(te, day)
        if op is None or op <= 0:
            return
        qty = int(cash // op)
        if qty >= 1:
            fee = qty * op * cost_pct
            cash -= qty * op + fee
            position = (te, nm, qty, qty * op + fee)
            trades.append({"date": str(day), "action": "BUY", "symbol": te, "qty": qty, "price": op})

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
        # 更新所有可能持仓标的的收盘价（含趋势补位标的）
        all_etfs = set(p[2] for p in pairs)
        if trend_slots:
            all_etfs |= set(s[1] for s in trend_slots)
        for etf in all_etfs:
            sub = bars_map[etf][bars_map[etf]["trade_date"] == ed]
            if not sub.empty:
                last_close[etf] = float(sub.iloc[0]["close"])
        sigs = {}
        for p in pairs:
            fi, ve, te, nm, b, v, g = p
            f = fear_map.get(fi, {}).get(sd)
            row = bars_map[ve]
            s2 = row[row["trade_date"] == sd]
            vr = float(s2["volume_ratio"].iloc[0]) if not s2.empty else np.nan
            sigs[te] = (f is not None and f <= b and np.isfinite(vr) and vr >= v, f)

        if position is None:
            cands = [p for p in pairs if sigs[p[2]][0] and not in_cooldown(p[2], i)]
            if cands:
                t = min(cands, key=lambda p: sigs[p[2]][1])
                do_buy(t, ed)
            elif trend_slots:
                # 趋势补位：选趋势最强（相对均线偏离最大）且价格>均线的
                best = None
                best_gap = 0.0
                for ic, te, nm in trend_slots:
                    ser = idx_series.get(ic)
                    if not ser:
                        continue
                    if in_cooldown(te, i):
                        continue
                    if trend_max_fear is not None:
                        f = fear_map.get(ic, {}).get(sd)
                        if f is None or f >= trend_max_fear:
                            continue
                    close = ser.get(str(sd))
                    m = ma(ser, str(sd), ma_win)
                    if close is None or m is None:
                        continue
                    gap = close / m - 1
                    if close > m and gap >= min_gap and gap > best_gap:
                        best_gap = gap
                        best = (te, nm)
                if best:
                    do_buy_trend(best[0], f"趋势[{best[1]}]", ed)
        else:
            held_greed = pair_by_etf.get(position[0], (None, None, None, None, None, None, 70.0))[6]
            hf = sigs.get(position[0], (False, np.nan))[1]
            is_trend = position[1].startswith("趋势")
            do_greed = (not is_trend) or (not greed_only_signal)
            do_swap = (not is_trend) or (not trend_no_swap)
            if hf is not None and np.isfinite(hf) and hf >= held_greed and do_greed:
                cooldown_sell_date[position[0]] = i
                do_sell(ed)
            elif hf is not None and np.isfinite(hf) and hf > swap_threshold and do_swap:
                others = [p for p in pairs if p[2] != position[0] and sigs[p[2]][0]]
                if others:
                    t = min(others, key=lambda p: sigs[p[2]][1])
                    do_sell(ed)
                    do_buy(t, ed)
            elif is_trend:
                # 趋势持仓：跌破均线止损
                ic_map = {"科创50": "000688.SH", "半导体": "H30184.CSI", "纳指": "QQQ.US", "纳指科技": "QQQ.US"}
                nm = position[1].replace("趋势[", "").replace("]", "")
                ic = ic_map.get(nm)
                if ic:
                    ser = idx_series.get(ic)
                    close = ser.get(str(sd)) if ser else None
                    m = ma(ser, str(sd), ma_win) if ser else None
                    if close is not None and m is not None and close < m:
                        do_sell(ed)
        value = cash + (position[2] * last_close.get(position[0], 0) if position else 0)
        if position is not None:
            held_days += 1
        curve.append(value)

    v = np.array(curve)
    total = v[-1] / 1000000 - 1
    daily = np.diff(v) / v[:-1]
    mdd = float((v / np.maximum.accumulate(v) - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    return {"total": total * 100, "mdd": mdd, "sharpe": sharpe, "buys": len(buys), "sells": len(sells),
            "held_days": held_days, "total_days": len(trading_days) - 1,
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
    print("== 原引擎基准 ==")
    r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=None)
    print(f"  基准: {r['total']:.2f}% / {r['mdd']:.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    SLOTS = {
        "科创50": [("000688.SH", "588000.SH", "科创50")],
        "半导体": [("H30184.CSI", "512480.SH", "半导体")],
        "纳指": [("QQQ.US", TRADE_ETF, "纳指")],
        "科创50+半导体": [("000688.SH", "588000.SH", "科创50"), ("H30184.CSI", "512480.SH", "半导体")],
        "科创50+纳指": [("000688.SH", "588000.SH", "科创50"), ("QQQ.US", TRADE_ETF, "纳指")],
        "半导体+纳指": [("H30184.CSI", "512480.SH", "半导体"), ("QQQ.US", TRADE_ETF, "纳指")],
        "全": [("000688.SH", "588000.SH", "科创50"), ("H30184.CSI", "512480.SH", "半导体"), ("QQQ.US", TRADE_ETF, "纳指")],
        "全+红利槽位": [("000688.SH", "588000.SH", "科创50"), ("H30184.CSI", "512480.SH", "半导体"), ("QQQ.US", TRADE_ETF, "纳指"), ("000015.SH", "510880.SH", "红利")],
        "红利单独": [("000015.SH", "510880.SH", "红利")],
        "半导体+纳指+红利": [("H30184.CSI", "512480.SH", "半导体"), ("QQQ.US", TRADE_ETF, "纳指"), ("000015.SH", "510880.SH", "红利")],
    }
    print("\n== 趋势补位（价格>MA 买入，跌破 MA 卖）==")
    for ma_win in (20, 60):
        for name, slots in SLOTS.items():
            r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=slots, ma_win=ma_win)
            print(f"  MA{ma_win} {name:12s} {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    print("\n== MA20 全组合 成本敏感性 ==")
    full = SLOTS["全"]
    for cp in (0.0, 0.0005, 0.001, 0.002):
        r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=full, ma_win=20, cost_pct=cp)
        print(f"  单边成本 {cp*100:.2f}%: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

    print("\n== MA20 全组合 贪卖作用范围实验 ==")
    print(f"{'规则':22s} {'总收益':>9s} {'回撤':>7s} {'夏普':>6s} {'买卖':>7s} {'持仓天':>6s} {'空仓%':>7s}")
    for label, gos, tns in (("原版(贪卖对所有单)", False, False),
                            ("贪卖只对信号单", True, False),
                            ("贪卖只对信号单+趋势单不换仓", True, True),
                            ("趋势单不换仓(贪卖仍全生效)", False, True)):
        r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=full, ma_win=20,
                     greed_only_signal=gos, trend_no_swap=tns)
        flat = r["total_days"] - r["held_days"]
        print(f"{label:22s} {r['total']:8.2f}% {r['mdd']:6.2f}% {r['sharpe']:6.2f} {r['buys']:3d}/{r['sells']:<3d} {r['held_days']:6d} {flat/r['total_days']*100:6.1f}%")

    print("\n== MA20 全组合 贪卖冷却期 ==")
    print(f"{'冷却期':10s} {'总收益':>9s} {'回撤':>7s} {'夏普':>6s} {'买卖':>7s} {'持仓天':>6s} {'空仓%':>7s}")
    for cd in (0, 5, 10, 20, 30):
        r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=full, ma_win=20, cooldown_days=cd)
        flat = r["total_days"] - r["held_days"]
        print(f"{cd:5d}天    {r['total']:8.2f}% {r['mdd']:6.2f}% {r['sharpe']:6.2f} {r['buys']:3d}/{r['sells']:<3d} {r['held_days']:6d} {flat/r['total_days']*100:6.1f}%")

    print("\n== MA20 全组合 趋势买入加恐贪过滤 ==")
    print(f"{'恐贪条件':10s} {'总收益':>9s} {'回撤':>7s} {'夏普':>6s} {'买卖':>7s} {'持仓天':>6s} {'空仓天':>6s} {'空仓%':>7s}")
    for tmf in (None, 70.0, 60.0, 50.0, 40.0):
        r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=full, ma_win=20, trend_max_fear=tmf)
        flat = r["total_days"] - r["held_days"]
        print(f"{'<'+str(tmf) if tmf else '不限':10s} {r['total']:8.2f}% {r['mdd']:6.2f}% {r['sharpe']:6.2f} {r['buys']:3d}/{r['sells']:<3d} {r['held_days']:6d} {flat:6d} {flat/r['total_days']*100:6.1f}%")

    print("\n== 半导体+纳指 两槽位 明细（买卖配对） ==")
    two = SLOTS["半导体+纳指"]
    r = run_case(base3, 45.0, fear_map, bars_map, trading_days, trend_slots=two, ma_win=20)
    for b, s in zip(r["buy_list"], r["sell_list"]):
        print(f"  买{b['date']} {b['symbol']} @{b['price']:.3f} -> 卖{s['date']} @{s['price']:.3f} pnl {s['pnl']:,.0f}")
