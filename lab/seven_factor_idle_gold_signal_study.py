"""七因子空仓期：仅在黄金ETF低位放量时买入黄金。"""
from __future__ import annotations

import itertools
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lab.eight_factor_live_strategy_study import (
    build_base, initialize_import_environment, load_price, production_params, score_version,
)

OUTPUT_DIR = Path("lab/output/seven_factor_idle_gold_signal_study")
GOLD = "518880.SH"


def add_gold_signals(frame):
    result = frame.sort_values("date").reset_index(drop=True).copy()
    close = result["close"]
    mean = close.rolling(252, min_periods=120).mean()
    std = close.rolling(252, min_periods=120).std(ddof=0).replace(0, np.nan)
    z = (close - mean) / std
    result["gold_fear"] = z.map(
        lambda value: 50.0 * (1.0 + math.erf(value / math.sqrt(2.0))) if np.isfinite(value) else np.nan
    )
    result["volume_ratio"] = result["volume"] / result["volume"].shift(1).rolling(20).mean()
    return result


def simulate(trades, price_frames, dates, initial_capital, gold_signal, allocation_pct):
    actions = defaultdict(list)
    for trade in trades:
        actions[pd.Timestamp(trade["date"]).strftime("%Y-%m-%d")].append(trade)
    maps = {
        symbol: frame.assign(day=frame["date"].dt.strftime("%Y-%m-%d")).set_index("day").to_dict("index")
        for symbol, frame in price_frames.items()
    }
    cash, position, shares = float(initial_capital), None, 0
    records, simulated_trades = [], []

    def sell(day, symbol):
        nonlocal cash, position, shares
        price = float(maps[symbol][day]["low"])
        cash += shares * price
        simulated_trades.append({"date": day, "action": "SELL", "symbol": symbol, "price": price, "shares": shares})
        position, shares = None, 0

    def buy(day, symbol, pct=100):
        nonlocal cash, position, shares
        price = float(maps[symbol][day]["high"])
        qty = int(np.floor(cash * float(pct) / 100.0 / price)) if price > 0 else 0
        if qty:
            cash -= qty * price
            position, shares = symbol, qty
            simulated_trades.append({"date": day, "action": "BUY", "symbol": symbol, "price": price, "shares": qty})

    for timestamp in dates:
        day = pd.Timestamp(timestamp).strftime("%Y-%m-%d")
        day_actions = actions.get(day, [])
        has_strategy_buy = any(item["action"] == "BUY" for item in day_actions)
        for action in day_actions:
            symbol = action["symbol"]
            if action["action"] == "SELL" and position == symbol:
                sell(day, symbol)
            elif action["action"] == "BUY":
                if position == GOLD:
                    sell(day, GOLD)
                if position is None:
                    buy(day, symbol)
        if not has_strategy_buy and position is None and gold_signal.get(day, False):
            buy(day, GOLD, allocation_pct)
        close = float(maps[position][day]["close"]) if position else 0.0
        records.append({"date": day, "value": cash + shares * close, "position": position or "CASH"})
    return pd.DataFrame(records), pd.DataFrame(simulated_trades)


def main():
    temp_db = initialize_import_environment()
    try:
        from backend.src.app.api.soxl_fear_backtest import _compute_equity_metrics, _run_seesaw_backtest

        bases = tuple(score_version(base, 7) for base in (
            build_base("510880.SH", "000015.SH", "510880.SH", eight_factor_etf="510880.SH"),
            build_base("512480.SH", "000688.SH", "588000.SH", eight_factor_etf="588000.SH"),
            build_base("159941.SZ", "QQQ.US", "QQQ.US", eight_factor_etf=None),
        ))
        baseline = _run_seesaw_backtest(
            bases[0], bases[1], production_params(), 1_000_000, True, sub2_base_df=bases[2],
        )
        dates = bases[0]["date"].tolist()
        symbols = ["510880.SH", "512480.SH", "159941.SZ", GOLD]
        prices = {symbol: load_price(symbol) for symbol in symbols}
        gold = add_gold_signals(prices[GOLD])
        gold["execution_date"] = gold["date"].shift(-1)
        rows = []
        curves = {}
        for fear_threshold, volume_threshold, allocation_pct in itertools.product(
            range(15, 51, 5), [1.0, 1.2, 1.4, 1.6, 1.8, 2.0], [20, 30, 50, 100],
        ):
            signal_rows = gold[
                (gold["gold_fear"] <= fear_threshold)
                & (gold["volume_ratio"] >= volume_threshold)
                & gold["execution_date"].notna()
            ]
            signal = {
                pd.Timestamp(day).strftime("%Y-%m-%d"): True for day in signal_rows["execution_date"]
            }
            equity, simulated_trades = simulate(
                baseline["trades"], prices, dates, 1_000_000, signal, allocation_pct,
            )
            key = (fear_threshold, volume_threshold, allocation_pct)
            curves[key] = (equity, simulated_trades)
            for period, start, end in (
                ("full", "2023-03-22", "2026-08-19"),
                ("train_2023_2024", "2023-03-22", "2024-12-31"),
                ("test_2025_2026", "2025-01-01", "2026-08-19"),
            ):
                segment = equity[(equity["date"] >= start) & (equity["date"] <= end)]
                metrics, _ = _compute_equity_metrics(segment["date"].tolist(), segment["value"].to_numpy(float))
                gold_buys = int(((simulated_trades.get("action") == "BUY") & (simulated_trades.get("symbol") == GOLD)).sum())
                rows.append({
                    "period": period, "gold_fear_threshold": fear_threshold,
                    "gold_volume_ratio_threshold": volume_threshold, "gold_allocation_pct": allocation_pct,
                    "gold_buy_count": gold_buys, "total_return_pct": metrics["total_return"],
                    "annualized_return_pct": metrics["annualized_return"],
                    "max_drawdown_pct": metrics["max_drawdown"], "sharpe_ratio": metrics["sharpe_ratio"],
                    "sortino_ratio": metrics["sortino_ratio"], "calmar_ratio": metrics["calmar_ratio"],
                })
        result = pd.DataFrame(rows)
        result.to_csv(OUTPUT_DIR / "grid_results.csv", index=False)
        full = result[result["period"] == "full"].sort_values(["sharpe_ratio", "total_return_pct"], ascending=False)
        train = result[result["period"] == "train_2023_2024"].sort_values(["sharpe_ratio", "total_return_pct"], ascending=False)
        full.head(50).to_csv(OUTPUT_DIR / "full_top50.csv", index=False)
        train.head(50).to_csv(OUTPUT_DIR / "train_top50.csv", index=False)
        for label, item in (("full_best", full.iloc[0]), ("train_best", train.iloc[0])):
            key = (int(item["gold_fear_threshold"]), float(item["gold_volume_ratio_threshold"]), int(item["gold_allocation_pct"]))
            curves[key][0].to_csv(OUTPUT_DIR / f"{label}_equity.csv", index=False)
            curves[key][1].to_csv(OUTPUT_DIR / f"{label}_trades.csv", index=False)
        print("Full best")
        print(full.head(15).to_string(index=False))
        train_key = train.iloc[0][["gold_fear_threshold", "gold_volume_ratio_threshold", "gold_allocation_pct"]]
        mask = (
            (result["gold_fear_threshold"] == train_key.iloc[0])
            & (result["gold_volume_ratio_threshold"] == train_key.iloc[1])
            & (result["gold_allocation_pct"] == train_key.iloc[2])
        )
        print("\nTrain-selected periods")
        print(result[mask].to_string(index=False))
    finally:
        temp_db.unlink(missing_ok=True)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    main()
