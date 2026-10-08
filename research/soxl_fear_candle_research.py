"""Isolated SOXL fear/volume/candlestick strategy research.

This is intentionally outside the backend runtime.  It reads the production
market-data snapshots in read-only mode and does not register an API parameter
or change the live strategy.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import requests


START = date(2021, 1, 4)
END = date(2026, 10, 7)
INITIAL_CAPITAL = 1_000_000.0
ANALYTICS_DB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
MAIN_DB = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_HEADERS = {
    "accept": "*/*",
    "origin": "https://www.cnn.com",
    "referer": "https://www.cnn.com/",
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
}


def _cnn_history() -> pd.DataFrame:
    response = requests.get(f"{CNN_URL}/{START.isoformat()}", headers=CNN_HEADERS, timeout=30)
    response.raise_for_status()
    rows = response.json().get("fear_and_greed_historical", {}).get("data", [])
    values = []
    for row in rows:
        score = row.get("y")
        stamp = row.get("x")
        if score is None or stamp is None:
            continue
        day = datetime.fromtimestamp(float(stamp) / 1000, tz=timezone.utc).date()
        if START <= day <= END:
            values.append((day, float(score)))
    return pd.DataFrame(values, columns=["date", "fear_greed"]).drop_duplicates("date", keep="last")


def _prices() -> pd.DataFrame:
    connection = duckdb.connect(ANALYTICS_DB, read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT symbol, trade_date AS date, open, high, low, close, volume
            FROM us_stock_daily
            WHERE symbol IN ('SOXL.US', 'SOXX.US')
              AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [START, END],
        ).fetchdf()
    finally:
        connection.close()
    soxl = rows[rows.symbol.eq("SOXL.US")].drop(columns="symbol")
    soxx = rows[rows.symbol.eq("SOXX.US")][["date", "close"]].rename(columns={"close": "benchmark_close"})
    result = soxl.merge(soxx, on="date", how="left")
    result["date"] = pd.to_datetime(result["date"]).dt.date
    return result


def _valuation_positions() -> pd.DataFrame:
    """Reproduce the available 252-day SOXX valuation point series.

    As in the production runner, dates before the valuation series exists do
    not block buys.  The full production data-cleaning rules are deliberately
    not reimplemented here, so this series is only used as a gate comparison.
    """
    connection = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    try:
        rows = pd.read_sql_query(
            """
            SELECT date, current_price, forward_stocks_value_lo,
                   forward_stocks_value_hi
            FROM etf_analysis
            WHERE symbol = 'SOXX.US'
            ORDER BY date
            """,
            connection,
        )
    finally:
        connection.close()
    rows["date"] = pd.to_datetime(rows["date"]).dt.date - pd.Timedelta(days=1)
    rows = rows[rows.date.map(lambda value: value.weekday() < 5)].copy()
    rows["gap"] = (
        (rows.forward_stocks_value_lo + rows.forward_stocks_value_hi) / 2 / rows.current_price - 1
    )
    rows = rows.replace([np.inf, -np.inf], np.nan).dropna(subset=["gap"])
    rows = rows.drop_duplicates("date", keep="first")
    rows["valuation_position_252"] = rows["gap"].rolling(252, min_periods=20).rank(pct=True) * 100
    return rows[["date", "valuation_position_252"]]


def _prepare_frame() -> pd.DataFrame:
    frame = _prices().sort_values("date").reset_index(drop=True)
    frame["volume_ma20"] = frame.volume.shift(1).rolling(20).mean()
    frame["volume_ratio"] = frame.volume / frame.volume_ma20
    frame = frame.merge(_cnn_history(), on="date", how="left")
    frame["fear_greed"] = frame.fear_greed.ffill()
    frame = frame.merge(_valuation_positions(), on="date", how="left")
    return frame.dropna(subset=["volume_ratio", "fear_greed", "benchmark_close"]).reset_index(drop=True)


def _metrics(values: np.ndarray, dates: list[date]) -> dict:
    returns = np.diff(values) / values[:-1]
    peaks = np.maximum.accumulate(values)
    drawdowns = values / peaks - 1
    max_drawdown = -float(drawdowns.min()) * 100
    duration = 0
    longest = 0
    for drawdown in drawdowns:
        duration = duration + 1 if drawdown < -1e-10 else 0
        longest = max(longest, duration)
    volatility = float(np.std(returns)) * math.sqrt(252) * 100
    sharpe = float(np.mean(returns) / np.std(returns) * math.sqrt(252)) if np.std(returns) else 0.0
    downside = np.minimum(returns, 0)
    sortino = float(np.mean(returns) / np.sqrt(np.mean(downside ** 2)) * math.sqrt(252)) if np.any(downside) else 0.0
    annual = ((values[-1] / values[0]) ** (252 / len(values)) - 1) * 100
    nonzero = returns[np.abs(returns) > 1e-12]
    gains, losses = nonzero[nonzero > 0], nonzero[nonzero < 0]
    return {
        "total_return": (values[-1] / values[0] - 1) * 100,
        "annualized_return": annual,
        "annualized_volatility": volatility,
        "max_drawdown": max_drawdown,
        "max_drawdown_duration_trading_days": longest,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": annual / max_drawdown if max_drawdown else 0.0,
        "daily_win_rate": len(gains) / len(nonzero) * 100 if len(nonzero) else 0.0,
        "daily_profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else None,
        "ending_value": float(values[-1]),
    }


def run(
    frame: pd.DataFrame,
    *,
    require_green_buy: bool,
    require_red_sell: bool,
    buy_fear_max: float = 30.0,
    greed_min: float = 40.0,
) -> dict:
    cash, shares, avg_cost = INITIAL_CAPITAL, 0, 0.0
    cooldown, greed_peak, sells_in_cycle = 0, None, 0
    values, benchmark_values, trades = [], [], []
    benchmark_shares = math.floor(INITIAL_CAPITAL / float(frame.iloc[0].benchmark_close))
    benchmark_cash = INITIAL_CAPITAL - benchmark_shares * float(frame.iloc[0].benchmark_close)

    for row in frame.itertuples(index=False):
        close, high, low, opening = map(float, (row.close, row.high, row.low, row.open))
        fear, ratio = float(row.fear_greed), float(row.volume_ratio)
        valuation = row.valuation_position_252
        valuation_ok = pd.isna(valuation) or float(valuation) <= 50.0
        green = close > opening
        red = close < opening
        can_trade = cooldown == 0
        if cooldown:
            cooldown -= 1

        # Production settings: threshold 30, ratio 1.37, 50% target, 10-day cooldown,
        # 7% trailing exit, 50% portfolio reduction, price-above-average protection, 1% slippage.
        if shares and fear >= greed_min:
            greed_peak = max(greed_peak or high, high)
        elif shares:
            greed_peak, sells_in_cycle = None, 0

        if shares and can_trade and greed_peak and sells_in_cycle < 2:
            drawdown = (greed_peak - close) / greed_peak * 100
            exit_signal = drawdown >= 7 and (red or not require_red_sell)
            if exit_signal and close > avg_cost:
                fill = close * 0.99
                portfolio = cash + shares * fill
                quantity = min(shares, math.floor(portfolio * 0.50 / fill))
                if quantity:
                    proceeds = quantity * fill
                    cash += proceeds
                    shares -= quantity
                    trades.append({"date": str(row.date), "action": "SELL", "price": fill, "fear": fear, "ratio": ratio, "drawdown": drawdown})
                    cooldown, sells_in_cycle, greed_peak = 10, sells_in_cycle + 1, high if shares else None
                    if not shares:
                        avg_cost = 0.0

        buy_signal = fear <= buy_fear_max and ratio >= 1.37 and valuation_ok and (green or not require_green_buy)
        if can_trade and buy_signal:
            fill = close * 1.01
            portfolio = cash + shares * fill
            amount = min(cash, portfolio * 0.50)
            quantity = math.floor(amount / fill)
            if quantity:
                cost = quantity * fill
                avg_cost = (shares * avg_cost + cost) / (shares + quantity)
                shares += quantity
                cash -= cost
                cooldown, greed_peak, sells_in_cycle = 10, None, 0
                trades.append({"date": str(row.date), "action": "BUY", "price": fill, "fear": fear, "ratio": ratio})

        values.append(cash + shares * close)
        benchmark_values.append(benchmark_cash + benchmark_shares * float(row.benchmark_close))

    values_array, benchmark_array = np.array(values), np.array(benchmark_values)
    return {
        "metrics": _metrics(values_array, frame.date.tolist()),
        "benchmark_metrics": _metrics(benchmark_array, frame.date.tolist()),
        "buy_count": sum(trade["action"] == "BUY" for trade in trades),
        "sell_count": sum(trade["action"] == "SELL" for trade in trades),
        "trades": trades,
        "effective_end_date": str(frame.iloc[-1].date),
        "trading_days": len(frame),
    }


def search_green_buy_thresholds(frame: pd.DataFrame) -> list[dict]:
    """Coarse, interpretable grid for the candle-filtered rule.

    Thresholds are deliberately few and rounded so a selected result remains
    robust enough to validate on a held-out period rather than a one-point fit.
    """
    candidates = []
    for buy_fear_max in range(10, 41, 5):
        for greed_min in range(35, 71, 5):
            result = run(
                frame,
                require_green_buy=True,
                require_red_sell=False,
                buy_fear_max=float(buy_fear_max),
                greed_min=float(greed_min),
            )
            metrics = result["metrics"]
            candidates.append({
                "buy_fear_max": buy_fear_max,
                "greed_min": greed_min,
                "buy_count": result["buy_count"],
                "sell_count": result["sell_count"],
                **{key: metrics[key] for key in (
                    "total_return", "annualized_return", "max_drawdown",
                    "sharpe", "sortino", "calmar",
                )},
            })
    # A risk-adjusted score prevents a one-off high-return cell winning solely
    # by accepting a materially worse drawdown.
    return sorted(candidates, key=lambda row: (row["sharpe"], row["calmar"], row["annualized_return"]), reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("research/output/soxl_fear_candle_research.json"))
    args = parser.parse_args()
    frame = _prepare_frame()
    result = {
        "parameters": {"buy_fear_max": 30, "valuation_buy_max": 50, "volume_ratio_min": 1.37, "buy_position_pct": 50, "cooldown_days": 10, "greed_min": 40, "trailing_stop_pct": 7, "sell_position_pct": 50, "slippage_pct": 1, "same_day_close": True},
        "baseline": run(frame, require_green_buy=False, require_red_sell=False),
        "green_buy_only": run(frame, require_green_buy=True, require_red_sell=False),
        "candle_filtered": run(frame, require_green_buy=True, require_red_sell=True),
        "green_buy_threshold_search_top20": search_green_buy_thresholds(frame)[:20],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
