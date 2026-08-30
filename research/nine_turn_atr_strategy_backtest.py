#!/usr/bin/env python3
"""Per-stock backtest for low9->high2 buys and high9->low2/3/4 ATR sells."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from nine_turn_atr_sell_study import DEFAULT_DATABASE, _run_lengths


START_DATE = pd.Timestamp("2023-01-01")
SELL_LOW_COUNTS = (2, 3, 4)
SELL_ATR_THRESHOLD = 2.0
COMMISSION_RATE = 0.0003
STAMP_DUTY_RATE = 0.0005


def _backtest_stock(
    stock: pd.DataFrame,
    sell_low_counts: tuple[int, ...] = SELL_LOW_COUNTS,
    sell_atr_threshold: float = SELL_ATR_THRESHOLD,
) -> tuple[dict, list[dict], pd.DataFrame]:
    stock = stock.sort_values("trade_date").reset_index(drop=True).copy()
    lag4 = stock["close"].shift(4)
    stock["high_count"] = _run_lengths(stock["close"] > lag4)
    stock["low_count"] = _run_lengths(stock["close"] < lag4)
    previous_close = stock["close"].shift(1)
    stock["true_range"] = pd.concat(
        [stock["high"] - stock["low"], (stock["high"] - previous_close).abs(), (stock["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    stock["atr14"] = stock["true_range"].rolling(14, min_periods=14).mean()
    low_anchor = stock["low_count"] >= 9
    high_anchor = stock["high_count"] >= 9
    stock["last_low9_date"] = stock["trade_date"].where(low_anchor).ffill()
    stock["last_high9_date"] = stock["trade_date"].where(high_anchor).ffill()
    stock["last_high9_close"] = stock["close"].where(high_anchor).ffill()
    stock["sell_drawdown_atr"] = (stock["last_high9_close"] - stock["close"]) / stock["atr14"]

    capital = 1.0
    units = 0.0
    entry_capital = None
    entry_price = None
    entry_date = None
    entry_signal_date = None
    consumed_low9_date = None
    pending: dict | None = None
    trades: list[dict] = []
    equity_rows: list[dict] = []
    first_row = stock.loc[stock["trade_date"] >= START_DATE].iloc[0]
    benchmark_entry_open = float(first_row["open"])

    for index, row in stock.iterrows():
        if row["trade_date"] < START_DATE:
            continue

        if pending is not None:
            execution_price = float(row["open"])
            if pending["side"] == "buy":
                entry_capital = capital
                units = capital * (1 - COMMISSION_RATE) / execution_price
                capital = 0.0
                entry_price = execution_price
                entry_date = row["trade_date"]
                entry_signal_date = pending["signal_date"]
                consumed_low9_date = pending["low9_date"]
            else:
                gross_value = units * execution_price
                capital = gross_value * (1 - COMMISSION_RATE - STAMP_DUTY_RATE)
                net_return = capital / entry_capital - 1
                trades.append({
                    "ts_code": row["ts_code"],
                    "name": row["name"],
                    "buy_signal_date": entry_signal_date,
                    "buy_date": entry_date,
                    "buy_price": entry_price,
                    "sell_signal_date": pending["signal_date"],
                    "sell_date": row["trade_date"],
                    "sell_price": execution_price,
                    "sell_low_count": pending["low_count"],
                    "sell_drawdown_atr": pending["drawdown_atr"],
                    "holding_days": int((row["trade_date"] - entry_date).days),
                    "net_return": net_return,
                })
                units = 0.0
                entry_capital = entry_price = entry_date = entry_signal_date = None
            pending = None

        equity = capital if units == 0 else units * float(row["close"])
        equity_rows.append({
            "trade_date": row["trade_date"],
            "equity": equity,
            "buy_hold_equity": float(row["close"]) / benchmark_entry_open,
        })

        if index + 1 >= len(stock):
            continue
        if units == 0:
            low9_date = row["last_low9_date"]
            if (
                row["high_count"] == 2
                and pd.notna(low9_date)
                and low9_date < row["trade_date"]
                and low9_date != consumed_low9_date
            ):
                pending = {"side": "buy", "signal_date": row["trade_date"], "low9_date": low9_date}
        else:
            high9_date = row["last_high9_date"]
            if (
                row["low_count"] in sell_low_counts
                and pd.notna(high9_date)
                and high9_date >= entry_date
                and row["sell_drawdown_atr"] > sell_atr_threshold
            ):
                pending = {
                    "side": "sell",
                    "signal_date": row["trade_date"],
                    "low_count": int(row["low_count"]),
                    "drawdown_atr": float(row["sell_drawdown_atr"]),
                }

    final_close = float(stock.loc[stock["trade_date"] >= START_DATE, "close"].iloc[-1])
    final_equity = capital if units == 0 else units * final_close
    equity_frame = pd.DataFrame(equity_rows)
    running_max = equity_frame["equity"].cummax()
    max_drawdown = (equity_frame["equity"] / running_max - 1).min()
    completed = pd.DataFrame(trades)
    buy_hold_return = final_close / benchmark_entry_open - 1
    summary = {
        "ts_code": stock["ts_code"].iloc[0],
        "name": stock["name"].iloc[0],
        "strategy_return": final_equity - 1,
        "buy_hold_return": buy_hold_return,
        "excess_return": final_equity - 1 - buy_hold_return,
        "max_drawdown": max_drawdown,
        "completed_trades": len(trades),
        "win_rate": (completed["net_return"] > 0).mean() if len(completed) else np.nan,
        "median_trade_return": completed["net_return"].median() if len(completed) else np.nan,
        "open_position": bool(units > 0),
    }
    equity_frame["ts_code"] = summary["ts_code"]
    return summary, trades, equity_frame


def analyze(
    database: str = DEFAULT_DATABASE,
    output: str | Path | None = None,
    sell_low_counts: tuple[int, ...] = SELL_LOW_COUNTS,
    sell_atr_threshold: float = SELL_ATR_THRESHOLD,
) -> dict:
    connection = duckdb.connect(database, read_only=True)
    end_date = connection.execute("SELECT max(trade_date) FROM a_stock_market_daily").fetchone()[0]
    sql = """
    WITH eligible AS (
        SELECT m.ts_code, b.name
        FROM a_stock_market_daily m JOIN a_stock_basic b USING (ts_code)
        WHERE m.trade_date = ? AND b.list_status = 'L'
          AND m.total_mv >= 500000 AND m.amount >= 50000 AND m.vol > 0
          AND upper(coalesce(b.name, '')) NOT LIKE '%ST%'
          AND NOT EXISTS (
              SELECT 1 FROM a_stock_name_changes nc
              WHERE nc.ts_code = m.ts_code
                AND upper(coalesce(nc.name, '')) LIKE '%ST%'
                AND coalesce(nc.start_date, DATE '1900-01-01') <= ?
                AND coalesce(nc.end_date, DATE '2999-12-31') >= DATE '2023-01-01'
          )
    )
    SELECT q.ts_code, e.name, q.trade_date, q.open, q.high, q.low, q.close, q.vol
    FROM a_stock_market_daily_qfq q JOIN eligible e USING (ts_code)
    WHERE q.trade_date BETWEEN DATE '2022-10-01' AND ?
      AND q.open > 0 AND q.close > 0 AND q.high > 0 AND q.low > 0 AND q.vol > 0
    ORDER BY q.ts_code, q.trade_date
    """
    prices = connection.execute(sql, [end_date, end_date, end_date]).df()
    connection.close()

    stock_rows: list[dict] = []
    trade_rows: list[dict] = []
    equity_parts: list[pd.DataFrame] = []
    for _, stock in prices.groupby("ts_code", sort=False):
        if not (stock["trade_date"] >= START_DATE).any():
            continue
        summary, trades, equity = _backtest_stock(stock, sell_low_counts, sell_atr_threshold)
        stock_rows.append(summary)
        trade_rows.extend(trades)
        equity_parts.append(equity)
    stocks = pd.DataFrame(stock_rows)
    trades = pd.DataFrame(trade_rows)
    equity = pd.concat(equity_parts, ignore_index=True)
    equity_panel = equity.pivot(index="trade_date", columns="ts_code", values="equity").sort_index()
    equity_panel = equity_panel.ffill().fillna(1.0)
    buy_hold_panel = equity.pivot(index="trade_date", columns="ts_code", values="buy_hold_equity").sort_index()
    buy_hold_panel = buy_hold_panel.ffill().fillna(1.0)
    portfolio = equity_panel.mean(axis=1).reset_index(name="equal_weight_equity")
    portfolio["buy_hold_equity"] = buy_hold_panel.mean(axis=1).to_numpy()
    portfolio["return"] = portfolio["equal_weight_equity"] - 1
    portfolio["drawdown"] = portfolio["equal_weight_equity"] / portfolio["equal_weight_equity"].cummax() - 1
    portfolio["buy_hold_return"] = portfolio["buy_hold_equity"] - 1
    portfolio["buy_hold_drawdown"] = portfolio["buy_hold_equity"] / portfolio["buy_hold_equity"].cummax() - 1
    elapsed_years = max((pd.Timestamp(end_date) - START_DATE).days / 365.25, 1 / 365.25)
    distribution = {
        "stocks": len(stocks),
        "stocks_with_trades": int((stocks["completed_trades"] > 0).sum()),
        "completed_trades": len(trades),
        "mean_stock_return_pct": stocks["strategy_return"].mean() * 100,
        "median_stock_return_pct": stocks["strategy_return"].median() * 100,
        "positive_stock_pct": (stocks["strategy_return"] > 0).mean() * 100,
        "mean_buy_hold_return_pct": stocks["buy_hold_return"].mean() * 100,
        "median_excess_return_pct": stocks["excess_return"].median() * 100,
        "equal_weight_portfolio_return_pct": portfolio["return"].iloc[-1] * 100,
        "equal_weight_portfolio_cagr_pct": (portfolio["equal_weight_equity"].iloc[-1] ** (1 / elapsed_years) - 1) * 100,
        "equal_weight_portfolio_max_drawdown_pct": portfolio["drawdown"].min() * 100,
        "equal_weight_buy_hold_return_pct": portfolio["buy_hold_return"].iloc[-1] * 100,
        "equal_weight_buy_hold_cagr_pct": (portfolio["buy_hold_equity"].iloc[-1] ** (1 / elapsed_years) - 1) * 100,
        "equal_weight_buy_hold_max_drawdown_pct": portfolio["buy_hold_drawdown"].min() * 100,
        "median_stock_max_drawdown_pct": stocks["max_drawdown"].median() * 100,
        "trade_win_rate_pct": (trades["net_return"] > 0).mean() * 100 if len(trades) else np.nan,
        "median_trade_return_pct": trades["net_return"].median() * 100 if len(trades) else np.nan,
    }
    quantiles = stocks[["strategy_return", "buy_hold_return", "excess_return", "max_drawdown", "completed_trades"]].quantile(
        [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
    ).reset_index(names="quantile")
    metadata = {
        "source": database,
        "start_date": str(START_DATE.date()),
        "end_date": str(end_date),
        "buy_rule": "first high_count=2 after low_count>=9; execute next open",
        "sell_rule": f"low_count in {','.join(map(str, sell_low_counts))} after high_count>=9 and drawdown>{sell_atr_threshold:g} ATR14; execute next open",
        "commission_each_side": COMMISSION_RATE,
        "sell_stamp_duty": STAMP_DUTY_RATE,
        "mark_open_positions_at_final_close": True,
        **distribution,
    }
    result = {"stocks": stocks, "trades": trades, "portfolio": portfolio, "quantiles": quantiles, "metadata": metadata}
    if output is not None:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        stocks.sort_values("strategy_return", ascending=False).to_csv(destination / "stock_summary.csv", index=False)
        trades.to_csv(destination / "trades.csv", index=False)
        portfolio.to_csv(destination / "equal_weight_portfolio.csv", index=False)
        quantiles.to_csv(destination / "stock_return_quantiles.csv", index=False)
        (destination / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--output", default="research/output/nine_turn_atr_strategy_backtest")
    parser.add_argument("--sell-low-counts", default="2,3,4")
    parser.add_argument("--sell-atr-threshold", type=float, default=2.0)
    args = parser.parse_args()
    sell_low_counts = tuple(int(value.strip()) for value in args.sell_low_counts.split(",") if value.strip())
    result = analyze(args.database, args.output, sell_low_counts, args.sell_atr_threshold)
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=2, default=str))
    print(result["quantiles"].to_string(index=False))


if __name__ == "__main__":
    main()
