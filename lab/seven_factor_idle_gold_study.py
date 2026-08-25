"""当前七因子生产策略：现金空仓期改为持有黄金ETF。"""
from __future__ import annotations

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

OUTPUT_DIR = Path("lab/output/seven_factor_idle_gold_study")
GOLD_SYMBOLS = ["518880.SH", "159934.SZ", "518800.SH", "159937.SZ"]


def simulate(trades, price_frames, dates, initial_capital, idle_symbol=None, idle_allocation_pct=100):
    actions = defaultdict(list)
    for trade in trades:
        actions[pd.Timestamp(trade["date"]).strftime("%Y-%m-%d")].append(trade)
    maps = {
        symbol: frame.assign(day=frame["date"].dt.strftime("%Y-%m-%d")).set_index("day").to_dict("index")
        for symbol, frame in price_frames.items()
    }
    cash = float(initial_capital)
    position = None
    shares = 0
    records = []
    simulated_trades = []

    def sell(day, symbol):
        nonlocal cash, position, shares
        price = float(maps[symbol][day]["low"])
        cash += shares * price
        simulated_trades.append({"date": day, "action": "SELL", "symbol": symbol, "price": price, "shares": shares})
        position, shares = None, 0

    def buy(day, symbol, allocation_pct=100):
        nonlocal cash, position, shares
        price = float(maps[symbol][day]["high"])
        budget = cash * (float(allocation_pct) / 100.0)
        qty = int(np.floor(budget / price)) if price > 0 else 0
        if qty > 0:
            cash -= qty * price
            position, shares = symbol, qty
            simulated_trades.append({"date": day, "action": "BUY", "symbol": symbol, "price": price, "shares": qty})

    first_day = pd.Timestamp(dates[0]).strftime("%Y-%m-%d")
    if idle_symbol:
        buy(first_day, idle_symbol, idle_allocation_pct)

    for timestamp in dates:
        day = pd.Timestamp(timestamp).strftime("%Y-%m-%d")
        day_actions = actions.get(day, [])
        has_strategy_buy = any(item["action"] == "BUY" for item in day_actions)
        for action in day_actions:
            symbol = action["symbol"]
            if action["action"] == "SELL":
                if position == symbol:
                    sell(day, symbol)
            elif action["action"] == "BUY":
                if idle_symbol and position == idle_symbol:
                    sell(day, idle_symbol)
                if position is None:
                    buy(day, symbol)
        if idle_symbol and not has_strategy_buy and position is None:
            buy(day, idle_symbol, idle_allocation_pct)
        close = float(maps[position][day]["close"]) if position else 0.0
        records.append({
            "date": day, "value": cash + shares * close,
            "cash": cash, "position": position or "CASH", "shares": shares,
        })
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
        symbols = ["510880.SH", "512480.SH", "159941.SZ", *GOLD_SYMBOLS]
        prices = {symbol: load_price(symbol) for symbol in symbols}
        rows = []
        scenarios = [(None, 0)] + [("518880.SH", pct) for pct in range(10, 101, 10)]
        scenarios += [(symbol, 100) for symbol in GOLD_SYMBOLS[1:]]
        for idle_symbol, idle_allocation_pct in scenarios:
            equity, simulated_trades = simulate(
                baseline["trades"], prices, dates, 1_000_000, idle_symbol=idle_symbol,
                idle_allocation_pct=idle_allocation_pct,
            )
            label = f"{idle_symbol}_{idle_allocation_pct}" if idle_symbol else "CASH"
            for period, start, end in (
                ("full", "2023-03-22", "2026-08-19"),
                ("train_2023_2024", "2023-03-22", "2024-12-31"),
                ("test_2025_2026", "2025-01-01", "2026-08-19"),
            ):
                segment = equity[(equity["date"] >= start) & (equity["date"] <= end)]
                metrics, _ = _compute_equity_metrics(
                    segment["date"].tolist(), segment["value"].to_numpy(dtype=float),
                )
                rows.append({
                    "idle_asset": idle_symbol or "CASH", "idle_allocation_pct": idle_allocation_pct,
                    "period": period,
                    "total_return_pct": metrics["total_return"],
                    "annualized_return_pct": metrics["annualized_return"],
                    "annualized_volatility_pct": metrics["annualized_volatility"],
                    "max_drawdown_pct": metrics["max_drawdown"],
                    "max_drawdown_duration_days": metrics["max_drawdown_duration_days"],
                    "sharpe_ratio": metrics["sharpe_ratio"],
                    "sortino_ratio": metrics["sortino_ratio"],
                    "calmar_ratio": metrics["calmar_ratio"],
                    "ending_value": metrics["ending_value"],
                })
            equity.to_csv(OUTPUT_DIR / f"{label.replace('.', '_')}_equity.csv", index=False)
            simulated_trades.to_csv(OUTPUT_DIR / f"{label.replace('.', '_')}_trades.csv", index=False)
        result = pd.DataFrame(rows)
        result.to_csv(OUTPUT_DIR / "comparison.csv", index=False)
        print("engine baseline", {key: baseline[key] for key in [
            "total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
        ]})
        print(result.to_string(index=False))
    finally:
        temp_db.unlink(missing_ok=True)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    main()
