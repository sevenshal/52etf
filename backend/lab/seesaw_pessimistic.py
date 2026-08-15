#!/usr/bin/env python3
"""三标的实验：纳指信号用 QQQ 量比 / CNN 恐贪，交易 159941，支持最悲观成交。

pair 拆成 (fear_index, volume_etf, trade_etf, name, bthr, vthr)：
- 恐贪：fear_index（QQQ.US 或 CNN*.US 或 A股指数）
- 量比：volume_etf 的成交量 20 日量比
- 交易：trade_etf 的价格（159941）
悲观成交：买入按当天 high、卖出按当天 low（最差成交价）
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
    ("000015.SH", "510880.SH", "510880.SH", "红利", 30.0, 1.6),
    ("000688.SH", "588000.SH", "588000.SH", "科创50", 25.0, 1.6),
]


def load_data(fear_symbols, bar_symbols):
    start = date.fromisoformat(START)
    feature_start = (start - timedelta(days=90)).isoformat()
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True) as conn:
        fear = pd.read_sql_query(
            """
            SELECT upper(symbol) AS symbol, date, score
            FROM etf_fear_greed_clone_history
            WHERE upper(symbol) IN ({}) AND date BETWEEN ? AND ?
            ORDER BY symbol, date
            """.format(",".join("?" for _ in fear_symbols)),
            conn, params=(*fear_symbols, feature_start, END), parse_dates=["date"],
        )
    fear["date"] = fear["date"].dt.date
    fear["score"] = pd.to_numeric(fear["score"], errors="coerce")
    fear_map = {symbol: dict(zip(group["date"], group["score"])) for symbol, group in fear.groupby("symbol")}

    with duckdb.connect(DUCKDB_PATH, read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT symbol, trade_date, open, high, low, close, volume FROM (
                SELECT upper(symbol) AS symbol, trade_date, open, high, low, close, volume
                FROM a_stock_fund_daily_qfq
                WHERE upper(symbol) IN (SELECT * FROM unnest(?))
                UNION ALL
                SELECT upper(symbol) AS symbol, trade_date, open, high, low, close, volume
                FROM us_stock_daily
                WHERE upper(symbol) IN (SELECT * FROM unnest(?))
            )
            WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            ORDER BY symbol, trade_date
            """,
            [bar_symbols, bar_symbols, feature_start, END],
        ).fetch_df()
    rows["trade_date"] = pd.to_datetime(rows["trade_date"]).dt.date
    bars_map = {}
    for symbol, group in rows.groupby("symbol"):
        group = group.sort_values("trade_date").reset_index(drop=True)
        group["prior_mean"] = group["volume"].shift(1).rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).mean()
        group["volume_ratio"] = group["volume"] / group["prior_mean"]
        bars_map[symbol] = group
    trading_days = sorted(bars_map["510880.SH"]["trade_date"].unique())
    return fear_map, bars_map, trading_days


def run_backtest(fear_map, bars_map, trading_days, pairs, greed_threshold, swap_threshold, pessimistic=False):
    cash = float(INITIAL_CAPITAL)
    position = None  # (trade_etf, name, qty, cost)
    trades = []
    curve = []
    last_close = {}

    def signal(pair, day):
        fear_index, volume_etf, _trade_etf, _name, bthr, vthr = pair
        f = fear_map.get(fear_index, {}).get(day)
        row = bars_map.get(volume_etf)
        if row is None:
            return False, np.nan
        sub = row[row["trade_date"] == day]
        vr = float(sub["volume_ratio"].iloc[0]) if not sub.empty else np.nan
        return (f is not None and f <= bthr and np.isfinite(vr) and vr >= vthr), f

    def price(trade_etf, day, kind):
        row = bars_map[trade_etf]
        sub = row[row["trade_date"] == day]
        if sub.empty:
            return None
        if kind == "open":
            return float(sub.iloc[0]["open"])
        if kind == "high":
            return float(sub.iloc[0]["high"])
        if kind == "low":
            return float(sub.iloc[0]["low"])
        return float(sub.iloc[0]["close"])

    def do_buy(pair, day):
        nonlocal cash, position
        _fi, _ve, trade_etf, name, _b, _v = pair
        op = price(trade_etf, day, "high" if pessimistic else "open")
        if op is None or op <= 0:
            return
        qty = int(cash // op)
        if qty >= 1:
            cash -= qty * op
            position = (trade_etf, name, qty, qty * op)
            trades.append({"date": str(day), "action": "BUY", "symbol": trade_etf, "qty": qty, "price": op})

    def do_sell(day):
        nonlocal cash, position
        trade_etf, name, qty, cost = position
        op = price(trade_etf, day, "low" if pessimistic else "open")
        if op is None or op <= 0:
            return
        cash += qty * op
        trades.append({"date": str(day), "action": "SELL", "symbol": trade_etf, "qty": qty, "price": op, "pnl": qty * op - cost})
        position = None

    for i in range(1, len(trading_days)):
        exec_day = trading_days[i]
        signal_day = trading_days[i - 1]
        for p in pairs:
            sub = bars_map[p[2]][bars_map[p[2]]["trade_date"] == exec_day]
            if not sub.empty:
                last_close[p[2]] = float(sub.iloc[0]["close"])

        sigs = {p[2]: signal(p, signal_day) for p in pairs}

        if position is None:
            cands = [p for p in pairs if sigs[p[2]][0]]
            if cands:
                target = min(cands, key=lambda p: sigs[p[2]][1])
                do_buy(target, exec_day)
        else:
            held_etf = position[0]
            held_fear = sigs.get(held_etf, (False, np.nan))[1]
            if held_fear is not None and np.isfinite(held_fear) and held_fear >= greed_threshold:
                do_sell(exec_day)
            elif held_fear is not None and np.isfinite(held_fear) and held_fear > swap_threshold:
                others = [p for p in pairs if p[2] != held_etf and sigs[p[2]][0]]
                if others:
                    target = min(others, key=lambda p: sigs[p[2]][1])
                    do_sell(exec_day)
                    do_buy(target, exec_day)

        value = cash + (position[2] * last_close.get(position[0], 0.0) if position else 0.0)
        curve.append({"date": str(exec_day), "value": value})

    df = pd.DataFrame(curve)
    values = df["value"].astype(float)
    total = values.iloc[-1] / INITIAL_CAPITAL - 1
    years = (pd.Timestamp(df.iloc[-1]["date"]) - pd.Timestamp(df.iloc[0]["date"])).days / 365.25
    daily = values.pct_change().dropna()
    mdd = float((values / values.cummax() - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)) if len(daily) > 1 and daily.std() > 0 else 0.0
    ann = ((1 + total) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    return {
        "total": total * 100, "ann": ann, "mdd": mdd, "sharpe": sharpe,
        "buys": len(buys), "sells": len(sells), "buy_list": buys, "sell_list": sells,
    }


def run_case(fear_map, bars_map, trading_days, nasdaq_pair, label, pessimistic=False):
    pairs = [*BASE_TWO, nasdaq_pair]
    r = run_backtest(fear_map, bars_map, trading_days, pairs, 70.0, 45.0, pessimistic=pessimistic)
    tag = "悲观" if pessimistic else "正常"
    print(f"  {label:<40} 总收益 {r['total']:7.2f}% 年化 {r['ann']:6.2f}% 回撤 {r['mdd']:6.2f}% 夏普 {r['sharpe']:.2f} 买卖 {r['buys']}/{r['sells']} [{tag}]")
    return r


if __name__ == "__main__":
    # 加载：恐贪 QQQ.US / CNN*.US / A股两指数；bars 510880/588000/159941/QQQ.US
    fear_map, bars_map, trading_days = load_data(
        ["000015.SH", "000688.SH", "QQQ.US", "CNN*.US"],
        ["510880.SH", "588000.SH", "159941.SZ", "QQQ.US"],
    )
    print(f"交易日 {trading_days[0]} ~ {trading_days[-1]} 共 {len(trading_days)} 天")
    print("基准：量比=159941 + 恐贪=QQQ(自算) + 正常成交 应≈233%\n")

    # pair = (fear_index, volume_etf, trade_etf, name, bthr, vthr)
    qqq_clone = ("QQQ.US", "QQQ.US", "159941.SZ", "纳指", 20.0, 1.3)
    cnn_fear = ("CNN*.US", "QQQ.US", "159941.SZ", "纳指", 20.0, 1.3)
    cnn_fear_159941_vol = ("CNN*.US", "159941.SZ", "159941.SZ", "纳指", 20.0, 1.3)

    print("=== A. 恐贪=QQQ自算 | 量比=159941（复现基准）===")
    run_case(fear_map, bars_map, trading_days, ("QQQ.US", "159941.SZ", "159941.SZ", "纳指", 20.0, 1.3), "量比159941")

    print("\n=== B. 恐贪=QQQ自算 | 量比=QQQ本身 ===")
    run_case(fear_map, bars_map, trading_days, qqq_clone, "量比QQQ")

    print("\n=== C. 恐贪=CNN | 量比=159941 ===")
    run_case(fear_map, bars_map, trading_days, cnn_fear_159941_vol, "量比159941")

    print("\n=== D. 恐贪=CNN | 量比=QQQ ===")
    run_case(fear_map, bars_map, trading_days, cnn_fear, "量比QQQ")

    print("\n=== E. 最优组合 + 全部悲观成交（买=当日最高 卖=当日最低）===")
    run_case(fear_map, bars_map, trading_days, qqq_clone, "恐贪QQQ/量比QQQ", pessimistic=True)
    run_case(fear_map, bars_map, trading_days, cnn_fear, "恐贪CNN/量比QQQ", pessimistic=True)
    run_case(fear_map, bars_map, trading_days, cnn_fear_159941_vol, "恐贪CNN/量比159941", pessimistic=True)
