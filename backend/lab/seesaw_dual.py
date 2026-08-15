#!/usr/bin/env python3
"""红利 × 科创50 对称双轮动回测 + 参数搜索。

规则：
- 空仓：红利或科创50 任一"恐慌<=buy_threshold 且量比>=vr_threshold" → 买入更恐慌的那个
- 持有 A：A 恐贪 >= greed_threshold → 卖出（空仓）
- 持有 A：A 恐贪 > swap_threshold 且 B 有买入信号 → 卖出 A 换到 B
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

# 双标的：红利 + 科创50
PAIRS = [
    ("000015.SH", "510880.SH", "红利"),
    ("000688.SH", "588000.SH", "科创50"),
]


def load_data(pairs=None):
    pairs = pairs or PAIRS
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


def run_backtest(fear_map, bars_map, trading_days, *, pairs=None,
                 buy1, vr1, buy2, vr2, greed_threshold, swap_threshold):
    pairs = pairs or PAIRS
    cash = float(INITIAL_CAPITAL)
    position = None  # (etf, index, qty, cost)
    trades = []
    curve = []
    last_close = {}

    def signal(index, etf, day, bthr, vthr):
        f = fear_map.get(index, {}).get(day)
        row = bars_map.get(etf)
        if row is None:
            return False, np.nan, np.nan
        sub = row[row["trade_date"] == day]
        vr = float(sub["volume_ratio"].iloc[0]) if not sub.empty else np.nan
        return f is not None and f <= bthr and np.isfinite(vr) and vr >= vthr, f, vr

    def buy(etf, index, day):
        nonlocal cash, position
        op = float(bars_map[etf].loc[bars_map[etf]["trade_date"] == day, "open"].iloc[0])
        qty = int(cash // op)
        if qty >= 1:
            cash -= qty * op
            position = (etf, index, qty, qty * op)
            trades.append({"date": str(day), "action": "BUY", "symbol": etf, "qty": qty, "price": op})

    def sell(day):
        nonlocal cash, position
        etf, index, qty, cost = position
        op = float(bars_map[etf].loc[bars_map[etf]["trade_date"] == day, "open"].iloc[0])
        cash += qty * op
        trades.append({"date": str(day), "action": "SELL", "symbol": etf, "qty": qty, "price": op, "pnl": qty * op - cost})
        position = None

    for i in range(1, len(trading_days)):
        exec_day = trading_days[i]
        signal_day = trading_days[i - 1]
        for etf in [p[1] for p in pairs]:
            sub = bars_map[etf][bars_map[etf]["trade_date"] == exec_day]
            if not sub.empty:
                last_close[etf] = float(sub.iloc[0]["close"])

        sig_a, fear_a, vr_a = signal(pairs[0][0], pairs[0][1], signal_day, buy1, vr1)
        sig_b, fear_b, vr_b = signal(pairs[1][0], pairs[1][1], signal_day, buy2, vr2)

        if position is None:
            if sig_a and sig_b:
                # 两个都触发：买更恐慌的
                target = 0 if fear_a <= fear_b else 1
                buy(pairs[target][1], pairs[target][0], exec_day)
            elif sig_a:
                buy(pairs[0][1], pairs[0][0], exec_day)
            elif sig_b:
                buy(pairs[1][1], pairs[1][0], exec_day)
        else:
            held_index = position[1]
            other_index = pairs[1][0] if held_index == pairs[0][0] else pairs[0][0]
            other_sig = sig_b if held_index == pairs[0][0] else sig_a
            held_fear = fear_a if held_index == pairs[0][0] else fear_b
            if held_fear is not None and held_fear >= greed_threshold:
                sell(exec_day)  # 极度贪婪 → 卖出空仓
            elif held_fear is not None and held_fear > swap_threshold and other_sig:
                # 恐贪>swap 且另一标的有信号 → 换仓
                sell(exec_day)
                other_etf = pairs[1][1] if held_index == pairs[0][0] else pairs[0][1]
                other_index_code = other_index
                buy(other_etf, other_index_code, exec_day)

        value = cash + (position[2] * last_close.get(position[0], 0.0) if position else 0.0)
        curve.append({"date": str(exec_day), "value": value, "position": position[0] if position else None})

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
        "buys": len(buys), "sells": len(sells), "avg_pos": df["position"].notna().mean(),
        "buy_list": buys, "sell_list": sells,
    }


if __name__ == "__main__":
    # 红利 × 半导体 对称双轮动（半导体恐慌日量比通常 <1.6，量比档用 1.0/1.3）
    SEMI_PAIRS = [
        ("000015.SH", "510880.SH", "红利"),
        ("H30184.CSI", "512480.SH", "半导体"),
    ]
    fear_map, bars_map, trading_days = load_data(SEMI_PAIRS)
    print(f"交易日 {trading_days[0]} ~ {trading_days[-1]} 共 {len(trading_days)} 天")
    print("对比基准：纯红利 105.75% / 2.08 / -6.71%；红利主+科创50候补 162.96% / 2.42 / -6.71%；红利×科创50双轮动 211.55% / 2.49 / -6.71%")

    rows = []
    for buy1, vr1, buy2, vr2, swap in itertools.product(
        [25.0, 30.0, 35.0], [1.3, 1.6],   # 红利独立参数
        [20.0, 25.0, 28.0, 30.0], [1.0, 1.3],   # 半导体独立参数（量比 1.6 基本不触发）
        [45.0, 55.0, 70.0],
    ):
        r = run_backtest(fear_map, bars_map, trading_days, pairs=SEMI_PAIRS, buy1=buy1, vr1=vr1, buy2=buy2, vr2=vr2,
                         greed_threshold=70.0, swap_threshold=swap)
        rows.append({"buy1": buy1, "vr1": vr1, "buy2": buy2, "vr2": vr2, "swap": swap, **r})

    print(f"\n=== 网格 {len(rows)} 组（红利/半导体 独立参数）top15（按总收益）===")
    for r in sorted(rows, key=lambda r: -r["total"])[:15]:
        print(f"  红利 恐慌<={r['buy1']:g} 量比>={r['vr1']:g} | 半导体 恐慌<={r['buy2']:g} 量比>={r['vr2']:g} | 换仓>{r['swap']:g} "
              f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 波动 {r['vol']:.2f}% 买卖 {r['buys']}/{r['sells']} 仓位 {r['avg_pos']:.0%}")

    print(f"\n=== 按夏普 top10 ===")
    for r in sorted(rows, key=lambda r: -r["sharpe"])[:10]:
        print(f"  红利 恐慌<={r['buy1']:g} 量比>={r['vr1']:g} | 半导体 恐慌<={r['buy2']:g} 量比>={r['vr2']:g} | 换仓>{r['swap']:g} "
              f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 买卖 {r['buys']}/{r['sells']}")
