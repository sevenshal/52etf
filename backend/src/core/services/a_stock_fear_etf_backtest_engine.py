"""Reusable engine for the A-share fear/greed ETF range backtest.

This module is production code and must stay self-contained under ``src`` so
API imports do not depend on research scripts that are omitted from deploys.
All database access is read-only; signals formed at the close execute at the
next tradable open.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

import duckdb
import numpy as np
import pandas as pd

from ...robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS


DEFAULT_EXCLUDED = ("INNO100.CN", "000905.SH")


@dataclass
class Position:
    etf_symbol: str
    index_symbol: str
    quantity: int
    cost_basis: float
    entry_date: str
    entry_fear: float
    high_water: float
    low_water: float
    days_since_high: int = 0
    exit_armed: bool = False


def abnormal_volume(
    volume: float,
    prior_mean: float,
    prior_std: float,
    std_multiplier: float = 1.0,
) -> tuple[bool, float]:
    values = (volume, prior_mean, prior_std)
    if not all(np.isfinite(value) for value in values) or prior_mean <= 0 or prior_std < 0:
        return False, math.nan
    threshold = prior_mean + std_multiplier * prior_std
    score = (volume - prior_mean) / prior_std if prior_std > 0 else math.inf
    return volume > threshold, score


def target_mapping(excluded: set[str]) -> dict[str, str]:
    return {
        str(item["symbol"]).upper(): str(item["proxy_etf"]).upper()
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
        if item.get("proxy_etf") and str(item["symbol"]).upper() not in excluded
    }


def load_fear(
    sqlite_path: Union[Path, str],
    indexes: list[str],
    start: str,
    end: str,
) -> pd.DataFrame:
    if not indexes:
        return pd.DataFrame(columns=["index_symbol", "date", "score"])
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True) as connection:
        frame = pd.read_sql_query(
            """
            SELECT upper(symbol) AS index_symbol, date, score
            FROM etf_fear_greed_clone_history
            WHERE upper(symbol) IN ({})
              AND date BETWEEN ? AND ?
            ORDER BY date, symbol
            """.format(",".join("?" for _ in indexes)),
            connection,
            params=(*indexes, start, end),
            parse_dates=["date"],
        )
    frame["date"] = frame["date"].dt.date
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    return frame.dropna(subset=["score"])


def load_etf_bars(
    connection: duckdb.DuckDBPyConnection,
    etfs: list[str],
    start: str,
    end: str,
) -> pd.DataFrame:
    if not etfs:
        return pd.DataFrame()
    frame = connection.execute(
        """
        WITH featured AS (
            SELECT trade_date, upper(symbol) AS etf_symbol,
                   open, high, low, close, volume,
                   avg(volume) OVER (
                       PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
                   ) AS prior_volume_mean,
                   stddev_samp(volume) OVER (
                       PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
                   ) AS prior_volume_std,
                   count(volume) OVER (
                       PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
                   ) AS prior_volume_count
            FROM a_stock_fund_daily_qfq
            WHERE upper(symbol) IN (SELECT * FROM unnest(?))
              AND trade_date BETWEEN CAST(? AS DATE) - INTERVAL 40 DAY AND CAST(? AS DATE)
        )
        SELECT * FROM featured
        WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY trade_date, etf_symbol
        """,
        [etfs, start, end, start, end],
    ).fetch_df()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    for column in (
        "open", "high", "low", "close", "volume",
        "prior_volume_mean", "prior_volume_std",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def build_signal_rows(
    bars: pd.DataFrame,
    fear: pd.DataFrame,
    mapping: dict[str, str],
    fear_entry: float,
    std_multiplier: float,
) -> dict[Any, list[dict[str, Any]]]:
    inverse = {etf: index for index, etf in mapping.items()}
    working_bars = bars.copy()
    working_bars["index_symbol"] = working_bars["etf_symbol"].map(inverse)
    merged = working_bars.merge(
        fear,
        left_on=["trade_date", "index_symbol"],
        right_on=["date", "index_symbol"],
        how="inner",
    )
    signals: dict[Any, list[dict[str, Any]]] = {}
    for row in merged.itertuples(index=False):
        if row.prior_volume_count < 20 or row.score >= fear_entry:
            continue
        is_abnormal, z_score = abnormal_volume(
            row.volume, row.prior_volume_mean, row.prior_volume_std, std_multiplier
        )
        if not is_abnormal:
            continue
        signals.setdefault(row.trade_date, []).append(
            {
                "index_symbol": row.index_symbol,
                "etf_symbol": row.etf_symbol,
                "fear_score": float(row.score),
                "volume_z": float(z_score),
                "signal_close": float(row.close),
            }
        )
    for day in signals:
        signals[day].sort(
            key=lambda item: (-item["volume_z"], item["fear_score"], item["etf_symbol"])
        )
    return signals


def max_drawdown(values: pd.Series) -> float:
    return float((values / values.cummax() - 1).min()) if len(values) else 0.0


def run_backtest(
    bars: pd.DataFrame,
    fear: pd.DataFrame,
    signals: dict[Any, list[dict[str, Any]]],
    *,
    initial_capital: float,
    fear_greed_exit: float,
    no_new_high_days: int,
    commission_pct: float,
    slippage_pct: float,
    stamp_duty_pct: float,
    lot_size: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = sorted(bars["trade_date"].unique().tolist())
    bars_by_date = {
        day: group.set_index("etf_symbol")[["open", "high", "low", "close"]].to_dict(orient="index")
        for day, group in bars.groupby("trade_date", sort=True)
    }
    fear_by_date = {
        day: dict(zip(group["index_symbol"], group["score"]))
        for day, group in fear.groupby("date")
    }
    commission = commission_pct / 100
    slippage = slippage_pct / 100
    stamp = stamp_duty_pct / 100
    cash = float(initial_capital)
    position: Position | None = None
    pending_buy: dict[str, Any] | None = None
    pending_sell: dict[str, Any] | None = None
    last_close: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []

    for day in dates:
        day_bars = bars_by_date.get(day, {})
        for symbol, quote in day_bars.items():
            if np.isfinite(quote.get("close", np.nan)):
                last_close[symbol] = float(quote["close"])

        if position is not None and pending_sell is not None:
            quote = day_bars.get(position.etf_symbol)
            if quote and np.isfinite(quote.get("open", np.nan)):
                price = float(quote["open"]) * (1 - slippage)
                gross = position.quantity * price
                fee = gross * (commission + stamp)
                cash += gross - fee
                trades.append(
                    {
                        "date": str(day), "action": "sell",
                        "etf_symbol": position.etf_symbol,
                        "index_symbol": position.index_symbol,
                        "quantity": position.quantity, "price": price,
                        "gross": gross, "fee": fee,
                        "pnl": gross - fee - position.cost_basis,
                        "reason": "range_midpoint_after_greed",
                        **pending_sell,
                    }
                )
                position = None
                pending_sell = None

        if position is None and pending_buy is not None:
            quote = day_bars.get(pending_buy["etf_symbol"])
            if quote and np.isfinite(quote.get("open", np.nan)):
                price = float(quote["open"]) * (1 + slippage)
                quantity = int(cash / (price * (1 + commission)) / lot_size) * lot_size
                if quantity > 0:
                    gross = quantity * price
                    fee = gross * commission
                    cash -= gross + fee
                    position = Position(
                        etf_symbol=pending_buy["etf_symbol"],
                        index_symbol=pending_buy["index_symbol"],
                        quantity=quantity,
                        cost_basis=gross + fee,
                        entry_date=str(day),
                        entry_fear=pending_buy["fear_score"],
                        high_water=float(quote["high"]),
                        low_water=float(quote["low"]),
                    )
                    trades.append(
                        {
                            "date": str(day), "action": "buy",
                            "etf_symbol": position.etf_symbol,
                            "index_symbol": position.index_symbol,
                            "quantity": quantity, "price": price,
                            "gross": gross, "fee": fee, "pnl": None,
                            "reason": "fear_below_25_abnormal_volume",
                            "fear_score": pending_buy["fear_score"],
                            "volume_z": pending_buy["volume_z"],
                        }
                    )
                pending_buy = None

        if position is not None and position.entry_date != str(day):
            quote = day_bars.get(position.etf_symbol)
            if quote:
                high = float(quote["high"])
                low = float(quote["low"])
                close = float(quote["close"])
                if high > position.high_water:
                    position.high_water = high
                    position.days_since_high = 0
                else:
                    position.days_since_high += 1
                position.low_water = min(position.low_water, low)
                in_range = position.days_since_high >= no_new_high_days
                fear_score = fear_by_date.get(day, {}).get(position.index_symbol)
                if in_range and fear_score is not None and fear_score > fear_greed_exit:
                    position.exit_armed = True
                midpoint = (position.high_water + position.low_water) / 2
                if position.exit_armed and close >= midpoint and pending_sell is None:
                    pending_sell = {
                        "signal_date": str(day),
                        "fear_score": float(fear_score) if fear_score is not None else None,
                        "range_high": position.high_water,
                        "range_low": position.low_water,
                        "range_midpoint": midpoint,
                        "signal_close": close,
                        "days_since_high": position.days_since_high,
                    }

        if position is None and pending_buy is None:
            candidates = signals.get(day, [])
            if candidates:
                pending_buy = candidates[0]

        value = cash
        if position is not None:
            value += position.quantity * last_close.get(position.etf_symbol, 0)
        curve.append(
            {
                "date": str(day), "value": value, "cash": cash,
                "position": position.etf_symbol if position else None,
                "index_symbol": position.index_symbol if position else None,
                "exit_armed": position.exit_armed if position else False,
                "days_since_high": position.days_since_high if position else None,
            }
        )
    return pd.DataFrame(curve), pd.DataFrame(trades)


def summarize(curve: pd.DataFrame, trades: pd.DataFrame, initial_capital: float) -> dict[str, Any]:
    values = curve["value"].astype(float)
    start = pd.Timestamp(curve.iloc[0]["date"])
    end = pd.Timestamp(curve.iloc[-1]["date"])
    years = max((end - start).days / 365.25, 1 / 365.25)
    total_return = values.iloc[-1] / initial_capital - 1
    daily = values.pct_change().dropna()
    sells = trades[trades["action"] == "sell"] if len(trades) else trades
    result = {
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "initial_capital": initial_capital,
        "final_value": float(values.iloc[-1]),
        "total_return_pct": total_return * 100,
        "annualized_return_pct": ((1 + total_return) ** (1 / years) - 1) * 100,
        "max_drawdown_pct": max_drawdown(values) * 100,
        "annualized_volatility_pct": float(daily.std(ddof=1) * math.sqrt(252) * 100),
        "sharpe_zero_rf": (
            float(daily.mean() / daily.std(ddof=1) * math.sqrt(252))
            if daily.std(ddof=1) else None
        ),
        "buy_count": int((trades["action"] == "buy").sum()) if len(trades) else 0,
        "closed_trade_count": int(len(sells)),
        "closed_trade_win_rate_pct": float((sells["pnl"] > 0).mean() * 100) if len(sells) else None,
        "realized_pnl": float(sells["pnl"].sum()) if len(sells) else 0.0,
        "ending_position": curve.iloc[-1]["position"],
    }
    buys = trades[trades["action"] == "buy"] if len(trades) else trades
    if len(buys):
        first_buy_date = str(buys.iloc[0]["date"])
        active_curve = curve[curve["date"] >= first_buy_date]
        result["first_buy_date"] = first_buy_date
        result["active_period_return_pct"] = float((
            active_curve.iloc[-1]["value"] / active_curve.iloc[0]["value"] - 1
        ) * 100)
        if result["ending_position"]:
            latest_buy = buys.iloc[-1]
            open_cost = float(latest_buy["gross"]) + float(latest_buy["fee"])
            open_market_value = float(curve.iloc[-1]["value"]) - float(curve.iloc[-1]["cash"])
            result["ending_position_cost_basis"] = open_cost
            result["ending_position_unrealized_pnl"] = open_market_value - open_cost
    if "benchmark_value" in curve and curve["benchmark_value"].notna().any():
        benchmark = curve["benchmark_value"].dropna().astype(float)
        benchmark_return = benchmark.iloc[-1] / benchmark.iloc[0] - 1
        result.update(
            {
                "benchmark": "000300.SH",
                "benchmark_total_return_pct": benchmark_return * 100,
                "benchmark_max_drawdown_pct": max_drawdown(benchmark) * 100,
            }
        )
        if len(buys):
            active_benchmark = curve[curve["date"] >= first_buy_date]["benchmark_value"].dropna()
            result["benchmark_active_period_return_pct"] = float((
                active_benchmark.iloc[-1] / active_benchmark.iloc[0] - 1
            ) * 100)
    return result
