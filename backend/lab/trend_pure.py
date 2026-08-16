#!/usr/bin/env python3
"""纯趋势策略：去掉贪恐信号主策略，只用 16 槽趋势（gap 选最强 + 跌破 MA20 卖）。

对比：完整版（贪恐信号 + 趋势补位）1266.50% / -20.33% / 2.56。
"""

import math
import sys

import numpy as np

sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

import lab.seesaw_pessimistic as sp
import lab.trend_fill as tf

TRADE_ETF = "159509.SZ"

fear_syms = ["000015.SH", "000688.SH", "QQQ.US"]
etf_list = ["510880.SH", "588000.SH", "512480.SH", TRADE_ETF, "QQQ.US",
            "512660.SH", "515030.SH", "515120.SH", "512170.SH", "512400.SH",
            "512880.SH", "515220.SH", "512800.SH", "159928.SZ", "512890.SH",
            "510300.SH", "510500.SH", "588220.SH"]
fear_map, bars_map, trading_days = sp.load_data(fear_syms, etf_list)

ALL_SLOTS = [
    ("000688.SH", "588000.SH", "科创50"),
    ("H30184.CSI", "512480.SH", "半导体"),
    ("QQQ.US", TRADE_ETF, "纳指"),
    ("399967.SZ", "512660.SH", "军工"),
    ("930997.CSI", "515030.SH", "新能源车"),
    ("931152.CSI", "515120.SH", "创新药"),
    ("399989.SZ", "512170.SH", "医疗"),
    ("000819.SH", "512400.SH", "有色"),
    ("399975.SZ", "512880.SH", "证券"),
    ("399998.SZ", "515220.SH", "煤炭"),
    ("399986.SZ", "512800.SH", "银行"),
    ("000932.SH", "159928.SZ", "消费"),
    ("H30269.CSI", "512890.SH", "红利低波"),
    ("000300.SH", "510300.SH", "沪深300"),
    ("000905.SH", "510500.SH", "中证500"),
    ("000698.SH", "588220.SH", "科创100"),
]
# 探测可用槽位
import duckdb

src = duckdb.connect("/home/quantd/quant_prod/quant_robot/analytics.duckdb", read_only=True)
codes = list(dict.fromkeys(ic for ic, _, _ in ALL_SLOTS if ic != "QQQ.US"))
placeholders = ",".join(f"'{c.upper()}'" for c in codes)
df = src.execute(
    f"SELECT ts_code FROM a_stock_index_daily WHERE upper(ts_code) IN ({placeholders})"
).fetchdf()
src.close()
have = set(df["ts_code"].str.upper())
slots16 = [s for s in ALL_SLOTS if s[0] == "QQQ.US" or s[0].upper() in have]
slots16 = [s for s in slots16 if s[1] in bars_map and len(bars_map[s[1]]) > 100]
slots8 = [s for s in slots16 if s[2] in ("科创50", "半导体", "纳指", "军工", "新能源车", "创新药", "医疗", "科创100")]
slots3 = [s for s in slots16 if s[2] in ("科创50", "半导体", "纳指")]


def run_pure_trend(slots, ma_win=20, min_gap=0.0, cost_pct=0.0):
    """纯趋势：空仓 → gap 最大槽位全仓买入；持仓 → 跌破 MA20 次日开盘卖。"""
    cash = 1000000.0
    position = None
    pending_sell = False
    trades = []
    curve = []
    last_close = {}
    index_close = tf.load_index_close()
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

    def do_buy(te, nm, day):
        nonlocal cash, position, pending_sell
        op = price(te, day)
        if op is None or op <= 0:
            return
        qty = int(cash // op)
        if qty >= 1:
            fee = qty * op * cost_pct
            cash -= qty * op + fee
            position = (te, nm, qty, qty * op + fee)
            trades.append({"date": str(day), "action": "BUY", "symbol": te, "qty": qty, "price": op})
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

    all_etfs = set(s[1] for s in slots)
    for i in range(1, len(trading_days)):
        ed = trading_days[i]
        sd = trading_days[i - 1]
        for etf in all_etfs:
            sub = bars_map[etf][bars_map[etf]["trade_date"] == ed]
            if not sub.empty:
                last_close[etf] = float(sub.iloc[0]["close"])

        # 1) 开盘：执行昨日确认的跌破 MA20 卖出
        if pending_sell and position is not None:
            do_sell(ed)
            pending_sell = False

        # 2) 空仓 → 选 gap 最大的槽位买入
        if position is None:
            best = None
            best_gap = 0.0
            for ic, te, nm in slots:
                ser = idx_series.get(ic)
                if not ser:
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
                do_buy(best[0], f"趋势[{best[1]}]", ed)

        # 3) 收盘：检查跌破 MA20（从上方跌破）
        if position is not None:
            te = position[0]
            nm = position[1].replace("趋势[", "").replace("]", "")
            ic = {s[2]: s[0] for s in slots}.get(nm)
            if ic:
                ser = idx_series.get(ic)
                close = ser.get(str(ed)) if ser else None
                m = ma(ser, str(ed), ma_win) if ser else None
                if close is not None and m is not None and close < m:
                    pending_sell = True

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


print("对比：完整版（贪恐信号+趋势补位）1266.50% / -20.33% / 2.56（77/77）")
print("纯趋势参考：3 槽补位那部分 ~1108%（去掉信号单后需重算）")
print("\n== 纯趋势策略（无贪恐信号）MA20 ==")
for name, slots in (("纯趋势16槽", slots16), ("纯趋势8槽", slots8), ("纯趋势3槽", slots3)):
    for mg in (0.0, 0.01):
        r = run_pure_trend(slots, ma_win=20, min_gap=mg)
        print(f"  {name:14s} gap>={mg*100:.0f}%: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

print("\n== 纯趋势 16槽 MA 参数 ==")
for win in (10, 20, 30, 60):
    r = run_pure_trend(slots16, ma_win=win)
    print(f"  MA{win:2d}: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']}")

print("\n== 纯趋势 16槽 成本敏感性 ==")
for cp in (0.0, 0.001, 0.002):
    r = run_pure_trend(slots16, ma_win=20, cost_pct=cp)
    print(f"  单边成本 {cp*100:.2f}%: {r['total']:7.2f}% / {r['mdd']:6.2f}% / {r['sharpe']:.2f}")

print("\n== 纯趋势 16槽 明细（大额 pnl 笔）==")
r = run_pure_trend(slots16, ma_win=20)
for b, s in zip(r["buy_list"], r["sell_list"]):
    if abs(s["pnl"]) >= 300000:
        print(f"  买{b['date']} {b['symbol']} @{b['price']:.3f} -> 卖{s['date']} @{s['price']:.3f} pnl {s['pnl']:,.0f}")
