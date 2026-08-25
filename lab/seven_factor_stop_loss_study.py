"""七因子现金空仓策略：固定止损且无当日新买入信号才退出。"""
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

OUTPUT_DIR = Path("lab/output/seven_factor_stop_loss_study")
SYMBOLS = ("510880.SH", "512480.SH", "159941.SZ")
BUY_THRESHOLDS = (30.0, 25.0, 20.0)
VOLUME_THRESHOLDS = (1.6, 1.6, 1.3)


def simulate(baseline_trades, bases, prices, dates, stop_loss_pct):
    original_actions = defaultdict(list)
    for trade in baseline_trades:
        original_actions[pd.Timestamp(trade["date"]).strftime("%Y-%m-%d")].append(trade)
    price_maps = {
        symbol: frame.assign(day=frame["date"].dt.strftime("%Y-%m-%d")).set_index("day").to_dict("index")
        for symbol, frame in prices.items()
    }
    signal_maps = {
        symbol: frame.assign(day=frame["date"].dt.strftime("%Y-%m-%d")).set_index("day")
        for symbol, frame in zip(SYMBOLS, bases)
    }
    date_strings = [pd.Timestamp(value).strftime("%Y-%m-%d") for value in dates]
    next_day = dict(zip(date_strings[:-1], date_strings[1:]))
    pending = defaultdict(list)
    cash, position, shares, entry_price = 1_000_000.0, None, 0, None
    records, trades = [], []

    def sell(day, reason):
        nonlocal cash, position, shares, entry_price
        price = float(price_maps[position][day]["low"])
        cash += shares * price
        trades.append({"date": day, "action": "SELL", "symbol": position, "price": price,
                       "shares": shares, "reason": reason})
        position, shares, entry_price = None, 0, None

    def buy(day, symbol, reason):
        nonlocal cash, position, shares, entry_price
        price = float(price_maps[symbol][day]["high"])
        qty = int(np.floor(cash / price))
        if qty:
            cash -= qty * price
            position, shares, entry_price = symbol, qty, price
            trades.append({"date": day, "action": "BUY", "symbol": symbol, "price": price,
                           "shares": qty, "reason": reason})

    for day in date_strings:
        # 原策略成交优先；被止损后，原来针对旧仓位的卖出自然失效。
        for action in original_actions.get(day, []):
            symbol = action["symbol"]
            if action["action"] == "SELL" and position == symbol:
                sell(day, "original_sell")
            elif action["action"] == "BUY" and position is None:
                buy(day, symbol, "original_buy")
        for action in pending.pop(day, []):
            if action["action"] == "SELL" and position == action["symbol"]:
                sell(day, "stop_loss")
            elif action["action"] == "BUY" and position is None:
                buy(day, action["symbol"], "new_signal_after_stop")

        signals = []
        for symbol, buy_threshold, volume_threshold in zip(SYMBOLS, BUY_THRESHOLDS, VOLUME_THRESHOLDS):
            frame = signal_maps[symbol]
            if day not in frame.index:
                continue
            row = frame.loc[day]
            if float(row["fear_greed"]) <= buy_threshold and float(row["volume_ratio"]) >= volume_threshold:
                signals.append((float(row["fear_greed"]), symbol))

        tomorrow = next_day.get(day)
        if tomorrow and position is not None and stop_loss_pct is not None:
            close = float(price_maps[position][day]["close"])
            if close / entry_price - 1 <= -stop_loss_pct / 100 and not signals:
                pending[tomorrow].append({"action": "SELL", "symbol": position})
        elif tomorrow and position is None and signals:
            # 止损后等待下一次新信号；多标的同时触发时买恐贪最低者。
            pending[tomorrow].append({"action": "BUY", "symbol": min(signals)[1]})

        close = float(price_maps[position][day]["close"]) if position else 0.0
        records.append({"date": day, "value": cash + shares * close, "position": position or "CASH"})
    return pd.DataFrame(records), pd.DataFrame(trades)


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
        prices = {symbol: load_price(symbol) for symbol in SYMBOLS}
        dates = bases[0]["date"].tolist()
        rows = []
        for threshold in (None, 3, 4, 5, 6, 7, 8, 9, 10):
            equity, trades = simulate(baseline["trades"], bases, prices, dates, threshold)
            metrics, _ = _compute_equity_metrics(equity["date"].tolist(), equity["value"].to_numpy(float))
            rows.append({"stop_loss_pct": threshold or 0, **metrics,
                         "buy_count": int((trades["action"] == "BUY").sum()),
                         "sell_count": int((trades["action"] == "SELL").sum()),
                         "stop_count": int((trades["reason"] == "stop_loss").sum())})
            trades.to_csv(OUTPUT_DIR / f"trades_stop_{threshold or 0}.csv", index=False)
            equity.to_csv(OUTPUT_DIR / f"equity_stop_{threshold or 0}.csv", index=False)
        result = pd.DataFrame(rows)
        result.to_csv(OUTPUT_DIR / "comparison.csv", index=False)
        print(result[["stop_loss_pct", "total_return", "annualized_return", "max_drawdown",
                      "sharpe_ratio", "sortino_ratio", "calmar_ratio", "buy_count",
                      "sell_count", "stop_count"]].to_string(index=False))
    finally:
        temp_db.unlink(missing_ok=True)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    main()
