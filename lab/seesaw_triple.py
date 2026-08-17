#!/usr/bin/env python3
"""三标的对称轮动：红利 + 科创50 + 第三候选（证券/煤炭/房地产/新能源车/白酒）。

规则：
- 空仓：任一标的"恐慌<=各自阈值 且 量比>=各自阈值" → 买恐贪最低的
- 持有 X：X恐贪>=greed → 卖出；X恐贪>换仓阈值 且 任一其他标的有信号 → 换到恐贪最低的有信号标的
- 信号日收盘决策，次日开盘成交；无成本、floor 份额
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd

DB_PATH = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
DUCKDB_PATH = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
START = "2023-03-22"
END = "2026-08-14"
INITIAL_CAPITAL = 1_000_000.0
VOLUME_WINDOW = 20
TRADING_DAYS = 252

BASE_TWO = [
    ("000015.SH", "510880.SH", "红利", 30.0, 1.6),
    ("000688.SH", "588000.SH", "科创50", 25.0, 1.6),
]
THIRDS = [
    ("399975.SZ", "512880.SH", "证券"),
    ("399998.SZ", "515220.SH", "煤炭"),
    ("931775.CSI", "512200.SH", "房地产"),
    ("930997.CSI", "515030.SH", "新能源车"),
    ("399997.SZ", "161725.SZ", "白酒"),
]


def load_data(pairs):
    indexes = [p[0] for p in pairs]
    etfs = [p[1] for p in pairs]
    start = date.fromisoformat(START)
    feature_start = (start - timedelta(days=90)).isoformat()
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True) as conn:
        fear = pd.read_sql_query(
            """
            SELECT upper(symbol) AS symbol, date, score
            FROM etf_fear_greed_clone_history
            WHERE upper(symbol) IN ({}) AND date BETWEEN ? AND ?
            ORDER BY symbol, date
            """.format(",".join("?" for _ in indexes)),
            conn, params=(*indexes, feature_start, END), parse_dates=["date"],
        )
    fear["date"] = fear["date"].dt.date
    fear["score"] = pd.to_numeric(fear["score"], errors="coerce")
    fear_map = {symbol: dict(zip(group["date"], group["score"])) for symbol, group in fear.groupby("symbol")}

    with duckdb.connect(DUCKDB_PATH, read_only=True) as conn:
        bars = conn.execute(
            """
            SELECT trade_date, upper(symbol) AS symbol, open, high, low, close, volume
            FROM a_stock_fund_daily_qfq
            WHERE upper(symbol) IN (SELECT * FROM unnest(?))
              AND trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            ORDER BY symbol, trade_date
            """,
            [etfs, feature_start, END],
        ).fetch_df()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.date
    bars_map = {}
    for symbol, group in bars.groupby("symbol"):
        group = group.sort_values("trade_date").reset_index(drop=True)
        group["prior_mean"] = group["volume"].shift(1).rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).mean()
        group["volume_ratio"] = group["volume"] / group["prior_mean"]
        bars_map[symbol] = group
    trading_days = sorted(bars_map[pairs[0][1]]["trade_date"].unique())
    return fear_map, bars_map, trading_days


def run_backtest(fear_map, bars_map, trading_days, pairs, greed_threshold, swap_threshold):
    cash = float(INITIAL_CAPITAL)
    position = None  # (index, etf, name, qty, cost)
    trades = []
    curve = []
    last_close = {}

    def signal(pair, day):
        index, etf, _name, bthr, vthr = pair
        f = fear_map.get(index, {}).get(day)
        row = bars_map.get(etf)
        if row is None:
            return False, np.nan
        sub = row[row["trade_date"] == day]
        vr = float(sub["volume_ratio"].iloc[0]) if not sub.empty else np.nan
        return (f is not None and f <= bthr and np.isfinite(vr) and vr >= vthr), f

    def open_price(etf, day):
        row = bars_map[etf]
        return float(row.loc[row["trade_date"] == day, "open"].iloc[0])

    def do_buy(pair, day):
        nonlocal cash, position
        index, etf, name, _b, _v = pair
        op = open_price(etf, day)
        qty = int(cash // op)
        if qty >= 1:
            cash -= qty * op
            position = (index, etf, name, qty, qty * op)
            trades.append({"date": str(day), "action": "BUY", "symbol": etf, "qty": qty, "price": op})

    def do_sell(day):
        nonlocal cash, position
        index, etf, name, qty, cost = position
        op = open_price(etf, day)
        cash += qty * op
        trades.append({"date": str(day), "action": "SELL", "symbol": etf, "qty": qty, "price": op, "pnl": qty * op - cost})
        position = None

    for i in range(1, len(trading_days)):
        exec_day = trading_days[i]
        signal_day = trading_days[i - 1]
        for p in pairs:
            sub = bars_map[p[1]][bars_map[p[1]]["trade_date"] == exec_day]
            if not sub.empty:
                last_close[p[1]] = float(sub.iloc[0]["close"])

        sigs = {p[1]: signal(p, signal_day) for p in pairs}

        if position is None:
            candidates = [p for p in pairs if sigs[p[1]][0]]
            if candidates:
                target = min(candidates, key=lambda p: sigs[p[1]][1])
                do_buy(target, exec_day)
        else:
            held_etf = position[1]
            held_fear = sigs.get(held_etf, (False, np.nan))[1]
            if held_fear is not None and np.isfinite(held_fear) and held_fear >= greed_threshold:
                do_sell(exec_day)
            elif held_fear is not None and np.isfinite(held_fear) and held_fear > swap_threshold:
                others = [p for p in pairs if p[1] != held_etf and sigs[p[1]][0]]
                if others:
                    target = min(others, key=lambda p: sigs[p[1]][1])
                    do_sell(exec_day)
                    do_buy(target, exec_day)

        value = cash + (position[3] * last_close.get(position[1], 0.0) if position else 0.0)
        curve.append({"date": str(exec_day), "value": value})

    df = pd.DataFrame(curve)
    values = df["value"].astype(float)
    total = values.iloc[-1] / INITIAL_CAPITAL - 1
    years = (pd.Timestamp(df.iloc[-1]["date"]) - pd.Timestamp(df.iloc[0]["date"])).days / 365.25
    daily = values.pct_change().dropna()
    mdd = float((values / values.cummax() - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)) if len(daily) > 1 and daily.std() > 0 else 0.0
    ann = ((1 + total) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    vol = float(daily.std() * np.sqrt(TRADING_DAYS)) * 100 if len(daily) > 1 else 0.0
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    return {
        "total": total * 100, "ann": ann, "mdd": mdd, "sharpe": sharpe, "vol": vol,
        "buys": len(buys), "sells": len(sells), "buy_list": buys, "sell_list": sells,
    }


if __name__ == "__main__":
    print("对比基准：双轮动(红利30/1.6 + 科创50 25/1.6 + 换仓45) = 211.55% / 2.49 / -6.71%")

    for third in THIRDS:
        pairs = [*BASE_TWO, third]
        fear_map, bars_map, trading_days = load_data(pairs)
        best = None
        for bthr, vthr, swap in itertools.product([20.0, 25.0, 30.0], [1.3, 1.6], [45.0, 55.0]):
            third_pair = (third[0], third[1], third[2], bthr, vthr)
            r = run_backtest(fear_map, bars_map, trading_days, [*BASE_TWO, third_pair], 70.0, swap)
            if best is None or r["total"] > best[0]["total"]:
                best = (r, bthr, vthr, swap)
        r, bthr, vthr, swap = best
        print(f"\n=== 第三标的={third[2]} 最优: 恐慌<={bthr:g} 量比>={vthr:g} 换仓>{swap:g} ===")
        print(f"  总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 波动 {r['vol']:.2f}% 买卖 {r['buys']}/{r['sells']}")
