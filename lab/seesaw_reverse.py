#!/usr/bin/env python3
"""跷跷板反转：半导体为主标的，红利为候补。搜索主/候补参数。"""

from __future__ import annotations

import itertools
import math
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


def load_data(main_index: str, main_etf: str, sub_index: str, sub_etf: str):
    indexes = [main_index, sub_index]
    etfs = [main_etf, sub_etf]
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
    trading_days = sorted(bars_map[main_etf]["trade_date"].unique())
    return fear_map, bars_map, trading_days


def run_backtest(fear_map, bars_map, trading_days, *, main_index, main_etf, sub_index, sub_etf,
                 buy_threshold, greed_threshold, vr_threshold, sub_buy_threshold, sub_vr_threshold,
                 sort_by="fear"):
    cash = float(INITIAL_CAPITAL)
    position = None
    trades = []
    curve = []
    last_close = {}

    def signal(index, etf, day, bthr, vthr):
        f = fear_map.get(index, {}).get(day)
        row = bars_map.get(etf)
        if row is None:
            return False
        sub = row[row["trade_date"] == day]
        vr = float(sub["volume_ratio"].iloc[0]) if not sub.empty else np.nan
        return f is not None and f <= bthr and np.isfinite(vr) and vr >= vthr

    def greedy(index, day):
        f = fear_map.get(index, {}).get(day)
        return f is not None and f >= greed_threshold

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
        for etf in (main_etf, sub_etf):
            sub = bars_map[etf][bars_map[etf]["trade_date"] == exec_day]
            if not sub.empty:
                last_close[etf] = float(sub.iloc[0]["close"])

        main_sig = signal(main_index, main_etf, signal_day, buy_threshold, vr_threshold)
        sub_sig = signal(sub_index, sub_etf, signal_day, sub_buy_threshold, sub_vr_threshold)

        if position is None:
            if main_sig:
                buy(main_etf, main_index, exec_day)
            elif sub_sig:
                buy(sub_etf, sub_index, exec_day)
        elif position[1] == main_index:
            if greedy(main_index, signal_day):
                sell(exec_day)
        else:
            # 持有候补（红利）：主（半导体）出信号 → 换回；候补（红利）极贪 → 卖出
            if main_sig:
                sell(exec_day)
                buy(main_etf, main_index, exec_day)
            elif greedy(sub_index, signal_day):
                sell(exec_day)

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
    # 主=半导体，候补=红利
    fear_map, bars_map, trading_days = load_data("H30184.CSI", "512480.SH", "000015.SH", "510880.SH")
    print(f"交易日 {trading_days[0]} ~ {trading_days[-1]} 共 {len(trading_days)} 天")

    # 基准：纯半导体（无候补）
    base = run_backtest(fear_map, bars_map, trading_days, main_index="H30184.CSI", main_etf="512480.SH",
                        sub_index="000015.SH", sub_etf="510880.SH",
                        buy_threshold=30.0, greed_threshold=70.0, vr_threshold=1.6,
                        sub_buy_threshold=30.0, sub_vr_threshold=1.6)
    print(f"纯半导体基准(buy≤30 vr≥1.6): {base['total']:.2f}% 夏普 {base['sharpe']:.2f} 回撤 {base['mdd']:.2f}% 买卖 {base['buys']}/{base['sells']}")

    rows = []
    for main_buy, main_vr, sub_buy, sub_vr in itertools.product(
        [20.0, 25.0, 28.0, 30.0, 35.0], [1.3, 1.6, 2.0, 2.5], [25.0, 30.0, 35.0], [1.3, 1.6]
    ):
        r = run_backtest(fear_map, bars_map, trading_days, main_index="H30184.CSI", main_etf="512480.SH",
                         sub_index="000015.SH", sub_etf="510880.SH",
                         buy_threshold=main_buy, greed_threshold=70.0, vr_threshold=main_vr,
                         sub_buy_threshold=sub_buy, sub_vr_threshold=sub_vr)
        rows.append({"main_buy": main_buy, "main_vr": main_vr, "sub_buy": sub_buy, "sub_vr": sub_vr, **r})

    print(f"\n=== 网格 {len(rows)} 组 top15（按总收益）===")
    for r in sorted(rows, key=lambda r: -r["total"])[:15]:
        print(f"  半导体主 恐慌<={r['main_buy']:g} 量比>={r['main_vr']:g} | 红利候补 恐慌<={r['sub_buy']:g} 量比>={r['sub_vr']:g} "
              f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 波动 {r['vol']:.2f}% 买卖 {r['buys']}/{r['sells']}")

    print(f"\n=== 按夏普 top10 ===")
    for r in sorted(rows, key=lambda r: -r["sharpe"])[:10]:
        print(f"  半导体主 恐慌<={r['main_buy']:g} 量比>={r['main_vr']:g} | 红利候补 恐慌<={r['sub_buy']:g} 量比>={r['sub_vr']:g} "
              f"总收益 {r['total']:7.2f}% 夏普 {r['sharpe']:.2f} 回撤 {r['mdd']:6.2f}% 买卖 {r['buys']}/{r['sells']}")
