#!/usr/bin/env python3
"""跷跷板轮动回测：红利ETF为主，空仓时其他板块极恐放量替补。

语义（与 /fear-volume-backtest 用户参数对齐，无成本、floor 份额）：
- 信号于 T 日收盘形成，T+1 日开盘成交
- 买入：恐贪 <= buy_threshold 且 20日量比 >= vr_threshold
- 卖出：恐贪 >= greed_threshold 卖 100%（贪恐即卖，trailing=0）
- 状态机（最多持 1 只）：
  - 持有候补 x：红利出信号 → 卖 x 买红利；否则 x 极贪 → 卖 x（空仓）
  - 持有红利：红利极贪 → 卖
  - 空仓：红利出信号 → 买红利；否则候补池中极恐放量者 → 买最恐慌的
"""

from __future__ import annotations

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

# 主标的：红利
RED_INDEX = "000015.SH"
RED_ETF = "510880.SH"

# 跷跷板候补池（指数, ETF, 名称, 恐贪与红利相关性）
POOL = [
    ("H30184.CSI", "512480.SH", "半导体", -0.129),
    ("399975.SZ", "512880.SH", "证券", 0.138),
    ("000688.SH", "588000.SH", "科创50", 0.025),
    ("399006.SZ", "159915.SZ", "创业板", 0.255),
    ("399967.SZ", "512660.SH", "军工", 0.277),
    ("399989.SZ", "512170.SH", "医疗", 0.316),
    ("000905.SH", "510500.SH", "中证500", 0.337),
    ("930997.CSI", "515030.SH", "新能源车", 0.226),
    ("931775.CSI", "512200.SH", "房地产", 0.227),
]


def load_data() -> tuple[dict, dict, list]:
    indexes = [RED_INDEX] + [item[0] for item in POOL]
    etfs = [RED_ETF] + [item[1] for item in POOL]

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
            conn,
            params=(*indexes, feature_start, END),
            parse_dates=["date"],
        )
    fear["date"] = fear["date"].dt.date
    fear["score"] = pd.to_numeric(fear["score"], errors="coerce")
    fear_map: dict[str, dict[date, float]] = {}
    for symbol, group in fear.groupby("symbol"):
        fear_map[symbol] = dict(zip(group["date"], group["score"]))

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
    bars_map: dict[str, pd.DataFrame] = {}
    for symbol, group in bars.groupby("symbol"):
        group = group.sort_values("trade_date").reset_index(drop=True)
        group["prior_mean"] = group["volume"].shift(1).rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).mean()
        group["volume_ratio"] = group["volume"] / group["prior_mean"]
        bars_map[symbol] = group

    # 交易日序列 = 红利的交易日
    trading_days = sorted(bars_map[RED_ETF]["trade_date"].unique())
    return fear_map, bars_map, trading_days


def run_backtest(
    fear_map: dict,
    bars_map: dict,
    trading_days: list,
    *,
    pool: list,
    buy_threshold: float = 30.0,
    greed_threshold: float = 70.0,
    vr_threshold: float = 1.6,
    # 候补（跷跷板板块）独立买入门槛；None 则与红利相同
    sub_buy_threshold: float | None = None,
    sub_vr_threshold: float | None = None,
    sort_by: str = "fear",  # fear=最恐慌 / volume=量比最高
) -> dict:
    sub_buy = float(sub_buy_threshold) if sub_buy_threshold is not None else float(buy_threshold)
    sub_vr = float(sub_vr_threshold) if sub_vr_threshold is not None else float(vr_threshold)
    cash = float(INITIAL_CAPITAL)
    position = None  # (etf_symbol, index_symbol, quantity, cost)
    trades: list[dict] = []
    curve: list[dict] = []
    last_close: dict[str, float] = {}

    def red_signal(day: date) -> bool:
        fear = fear_map[RED_INDEX].get(day)
        row = bars_map[RED_ETF]
        vr = float(row.loc[row["trade_date"] == day, "volume_ratio"].iloc[0]) if not row[row["trade_date"] == day].empty else np.nan
        return (
            fear is not None and fear <= buy_threshold
            and np.isfinite(vr) and vr >= vr_threshold
        )

    def etf_signal(index: str, etf: str, day: date) -> bool:
        fear = fear_map.get(index, {}).get(day)
        row = bars_map.get(etf)
        if row is None:
            return False
        sub = row[row["trade_date"] == day]
        vr = float(sub["volume_ratio"].iloc[0]) if not sub.empty else np.nan
        return (
            fear is not None and fear <= sub_buy
            and np.isfinite(vr) and vr >= sub_vr
        )

    def greedy(index: str, day: date) -> bool:
        fear = fear_map.get(index, {}).get(day)
        return fear is not None and fear >= greed_threshold

    def buy(etf: str, index: str, day: date):
        nonlocal cash, position
        open_price = float(bars_map[etf].loc[bars_map[etf]["trade_date"] == day, "open"].iloc[0])
        qty = int(cash // open_price)
        if qty >= 1:
            cost = qty * open_price
            cash -= cost
            position = (etf, index, qty, cost)
            trades.append({"date": str(day), "action": "BUY", "symbol": etf, "qty": qty, "price": open_price})

    def sell(day: date):
        nonlocal cash, position
        etf, index, qty, cost = position
        open_price = float(bars_map[etf].loc[bars_map[etf]["trade_date"] == day, "open"].iloc[0])
        proceeds = qty * open_price
        cash += proceeds
        trades.append({"date": str(day), "action": "SELL", "symbol": etf, "qty": qty, "price": open_price, "pnl": proceeds - cost})
        position = None

    for i in range(1, len(trading_days)):
        exec_day = trading_days[i]
        signal_day = trading_days[i - 1]
        for etf in set([RED_ETF] + [item[1] for item in pool]):
            row = bars_map[etf]
            sub = row[row["trade_date"] == exec_day]
            if not sub.empty:
                last_close[etf] = float(sub.iloc[0]["close"])

        if position is None:
            if red_signal(signal_day):
                buy(RED_ETF, RED_INDEX, exec_day)
            else:
                candidates = [
                    (index, etf) for index, etf, _name, _corr in pool
                    if etf_signal(index, etf, signal_day)
                ]
                if candidates:
                    if sort_by == "fear":
                        candidates.sort(key=lambda item: fear_map.get(item[0], {}).get(signal_day, math.inf))
                    else:
                        candidates.sort(
                            key=lambda item: -float(
                                bars_map[item[1]].loc[bars_map[item[1]]["trade_date"] == signal_day, "volume_ratio"].iloc[0]
                            )
                        )
                    buy(candidates[0][1], candidates[0][0], exec_day)
        elif position[1] == RED_INDEX:
            if greedy(RED_INDEX, signal_day):
                sell(exec_day)
        else:
            # 持有候补：红利出信号 → 换回红利；否则候补极贪 → 卖出
            if red_signal(signal_day):
                sell(exec_day)
                buy(RED_ETF, RED_INDEX, exec_day)
            elif greedy(position[1], signal_day):
                sell(exec_day)

        value = cash + (position[2] * last_close.get(position[0], 0.0) if position else 0.0)
        curve.append({"date": str(exec_day), "value": value, "position": position[0] if position else None})

    df = pd.DataFrame(curve)
    values = df["value"].astype(float)
    total_return = values.iloc[-1] / INITIAL_CAPITAL - 1
    years = (pd.Timestamp(df.iloc[-1]["date"]) - pd.Timestamp(df.iloc[0]["date"])).days / 365.25
    daily = values.pct_change().dropna()
    max_dd = float((values / values.cummax() - 1).min()) * 100
    sharpe = float(daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)) if len(daily) > 1 and daily.std() > 0 else 0.0
    annualized = ((1 + total_return) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    vol = float(daily.std() * np.sqrt(TRADING_DAYS)) * 100 if len(daily) > 1 else 0.0
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    holding_days = (df["position"].notna()).sum()
    return {
        "total_return_pct": total_return * 100,
        "annualized_return_pct": annualized,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "volatility_pct": vol,
        "buy_count": len(buys),
        "sell_count": len(sells),
        "avg_position": df["position"].notna().mean(),
        "realized_pnl": sum(t.get("pnl", 0) for t in sells),
        "final_value": float(values.iloc[-1]),
        "buys": [(t["date"], t["symbol"], t["qty"], t["price"]) for t in buys],
        "sells": [(t["date"], t["symbol"], t["qty"], t["price"]) for t in sells],
        "curve": df,
    }


def show(name: str, r: dict, detail: bool = False):
    print(f"\n===== {name} =====")
    for key in ("total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sharpe", "volatility_pct", "buy_count", "sell_count", "avg_position", "realized_pnl", "final_value"):
        print(f"  {key}: {r[key]:.2f}" if isinstance(r[key], float) else f"  {key}: {r[key]}")
    if detail:
        print("  买入:", r["buys"])
        print("  卖出:", r["sells"])


if __name__ == "__main__":
    fear_map, bars_map, trading_days = load_data()
    print(f"交易日: {trading_days[0]} ~ {trading_days[-1]} 共 {len(trading_days)} 天")

    # 0) 纯红利（复现用户 105.74%）
    red_pool = []
    r_red = run_backtest(fear_map, bars_map, trading_days, pool=red_pool, vr_threshold=1.6)
    show("纯红利（复现）", r_red)

    # 1) 跷跷板全池（9个候补）
    r_all = run_backtest(fear_map, bars_map, trading_days, pool=POOL, vr_threshold=1.6)
    show("跷跷板全池(9)", r_all, detail=True)

    # 2) 跷跷板核心池（相关性最低的前5：半导体/证券/科创50/创业板/军工）
    core_pool = [item for item in POOL if item[0] in {"H30184.CSI", "399975.SZ", "000688.SH", "399006.SZ", "399967.SZ"}]
    r_core = run_backtest(fear_map, bars_map, trading_days, pool=core_pool, vr_threshold=1.6)
    show("跷跷板核心池(5)", r_core, detail=True)

    # 3) 量比 1.3 变体
    r_all_13 = run_backtest(fear_map, bars_map, trading_days, pool=POOL, vr_threshold=1.3)
    show("跷跷板全池 量比1.3", r_all_13)

    r_core_13 = run_backtest(fear_map, bars_map, trading_days, pool=core_pool, vr_threshold=1.3)
    show("跷跷板核心池 量比1.3", r_core_13)
