"""Portfolio backtest engine for A-share fear/greed index ETF proxies.

Signals are formed after the close and orders execute at the next tradable
open.  The engine is deliberately independent from API/Pydantic code so it can
be tested and reused by scheduled research jobs.
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


DEFAULT_EXCLUDED: tuple[str, ...] = ()
TRADING_DAYS = 252


@dataclass
class Position:
    etf_symbol: str
    index_symbol: str
    quantity: int
    cost_basis: float
    entry_price: float
    entry_date: str
    entry_fear: float
    high_water: float
    greed_reduced: bool = False
    volatility_monitoring: bool = False
    trailing_armed: bool = False


def abnormal_volume(
    volume: float,
    prior_mean: float,
    prior_std: float,
    std_multiplier: float = 1.0,
) -> tuple[bool, float]:
    """Kept for compatibility with the original range-strategy tests."""
    values = (volume, prior_mean, prior_std)
    if not all(np.isfinite(value) for value in values) or prior_mean <= 0 or prior_std < 0:
        return False, math.nan
    threshold = prior_mean + std_multiplier * prior_std
    score = (volume - prior_mean) / prior_std if prior_std > 0 else math.inf
    return volume > threshold, score


def target_mapping(excluded: set[str] | None = None, included: set[str] | None = None) -> dict[str, str]:
    excluded = {str(value).upper() for value in (excluded or set())}
    included = {str(value).upper() for value in (included or set())}
    return {
        str(item["symbol"]).upper(): str(item["proxy_etf"]).upper()
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
        if item.get("proxy_etf")
        and str(item["symbol"]).upper() not in excluded
        and (not included or str(item["symbol"]).upper() in included)
    }


def load_fear(sqlite_path: Union[Path, str], indexes: list[str], start: str, end: str) -> pd.DataFrame:
    if not indexes:
        return pd.DataFrame(columns=["index_symbol", "date", "score"])
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True) as connection:
        frame = pd.read_sql_query(
            """
            SELECT upper(symbol) AS index_symbol, date, score
            FROM etf_fear_greed_clone_history
            WHERE upper(symbol) IN ({}) AND date BETWEEN ? AND ?
            ORDER BY symbol, date
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
        return pd.DataFrame(columns=[
            "trade_date", "etf_symbol", "open", "high", "low", "close", "volume",
        ])
    frame = connection.execute(
        """
        SELECT trade_date, upper(symbol) AS etf_symbol, open, high, low, close, volume
        FROM a_stock_fund_daily_qfq
        WHERE upper(symbol) IN (SELECT * FROM unnest(?))
          AND trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY etf_symbol, trade_date
        """,
        [etfs, start, end],
    ).fetch_df()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["close"])


def prepare_market_features(
    bars: pd.DataFrame,
    *,
    volume_window: int,
    volatility_window: int,
    volatility_baseline_window: int,
    volatility_std_multiplier: float,
) -> pd.DataFrame:
    result = bars.sort_values(["etf_symbol", "trade_date"]).copy()
    grouped = result.groupby("etf_symbol", group_keys=False)
    result["prior_volume_mean"] = grouped["volume"].transform(
        lambda values: values.shift(1).rolling(volume_window, min_periods=volume_window).mean()
    )
    result["volume_ratio"] = result["volume"] / result["prior_volume_mean"]
    result["daily_return"] = grouped["close"].pct_change(fill_method=None)
    result["realized_volatility"] = grouped["daily_return"].transform(
        lambda values: values.rolling(volatility_window, min_periods=volatility_window).std(ddof=1)
        * math.sqrt(TRADING_DAYS)
    )
    vol_grouped = result.groupby("etf_symbol", group_keys=False)["realized_volatility"]
    result["volatility_mean"] = vol_grouped.transform(
        lambda values: values.shift(1).rolling(
            volatility_baseline_window, min_periods=volatility_baseline_window
        ).mean()
    )
    result["volatility_std"] = vol_grouped.transform(
        lambda values: values.shift(1).rolling(
            volatility_baseline_window, min_periods=volatility_baseline_window
        ).std(ddof=1)
    )
    result["volatility_threshold"] = (
        result["volatility_mean"] + volatility_std_multiplier * result["volatility_std"]
    )
    return result


def prepare_fear_features(fear: pd.DataFrame, bottom_ma_window: int) -> pd.DataFrame:
    result = fear.sort_values(["index_symbol", "date"]).copy()
    grouped = result.groupby("index_symbol", group_keys=False)["score"]
    result["fear_ma"] = grouped.transform(
        lambda values: values.rolling(bottom_ma_window, min_periods=bottom_ma_window).mean()
    )
    result["recent_fear_min"] = grouped.transform(
        lambda values: values.rolling(bottom_ma_window, min_periods=bottom_ma_window).min()
    )
    result["recent_fear_max"] = grouped.transform(
        lambda values: values.rolling(bottom_ma_window, min_periods=bottom_ma_window).max()
    )
    result["prior_fear_ma"] = result.groupby("index_symbol")["fear_ma"].shift(1)
    return result


def fear_reversal_flags(
    current_ma: float,
    previous_ma: float,
    recent_fear_min: float,
    recent_fear_max: float,
    *,
    bottom_threshold: float = 25.0,
    top_threshold: float = 75.0,
) -> tuple[bool, bool]:
    values = (current_ma, previous_ma, recent_fear_min, recent_fear_max)
    if not all(np.isfinite(value) for value in values):
        return False, False
    is_bottom = current_ma > previous_ma and recent_fear_min < bottom_threshold
    is_top = current_ma < previous_ma and recent_fear_max > top_threshold
    return bool(is_bottom), bool(is_top)


def build_signal_rows(
    bars: pd.DataFrame,
    fear: pd.DataFrame,
    mapping: dict[str, str],
    *,
    extreme_fear_threshold: float,
    volume_ratio_threshold: float,
    bottom_fear_threshold: float,
    extreme_buy_fraction: float,
    bottom_buy_fraction: float,
    start_date: str,
    end_date: str,
    sort_by_fear: bool = False,
) -> dict[Any, list[dict[str, Any]]]:
    pairs = pd.DataFrame(
        [{"index_symbol": index, "etf_symbol": etf} for index, etf in mapping.items()]
    )
    merged = pairs.merge(bars, on="etf_symbol").merge(
        fear, left_on=["index_symbol", "trade_date"], right_on=["index_symbol", "date"]
    )
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    merged = merged[(merged["trade_date"] >= start) & (merged["trade_date"] <= end)]
    signals: dict[Any, list[dict[str, Any]]] = {}
    for row in merged.itertuples(index=False):
        extreme = (
            row.score < extreme_fear_threshold
            and np.isfinite(row.volume_ratio)
            and row.volume_ratio >= volume_ratio_threshold
        )
        bottom, _ = fear_reversal_flags(
            row.fear_ma,
            row.prior_fear_ma,
            row.recent_fear_min,
            row.recent_fear_max,
            bottom_threshold=bottom_fear_threshold,
        )
        if not extreme and not bottom:
            continue
        reason = "extreme_fear_volume" if extreme else "fear_bottom_reversal"
        fraction = extreme_buy_fraction if extreme else bottom_buy_fraction
        signals.setdefault(row.trade_date, []).append({
            "index_symbol": row.index_symbol,
            "etf_symbol": row.etf_symbol,
            "fear_score": float(row.score),
            "fear_ma": float(row.fear_ma) if np.isfinite(row.fear_ma) else None,
            "recent_fear_min": (
                float(row.recent_fear_min) if np.isfinite(row.recent_fear_min) else None
            ),
            "recent_fear_max": (
                float(row.recent_fear_max) if np.isfinite(row.recent_fear_max) else None
            ),
            "volume_ratio": float(row.volume_ratio) if np.isfinite(row.volume_ratio) else None,
            "target_fraction": float(fraction),
            "reason": reason,
        })
    for day, candidates in signals.items():
        if sort_by_fear:
            # 恐慌优先：分数越低（越恐慌）越靠前，跷跷板轮动时买入最恐慌的标的
            candidates.sort(key=lambda item: (
                item["fear_score"], -(item["volume_ratio"] if item["volume_ratio"] is not None else -math.inf),
                item["etf_symbol"],
            ))
        else:
            candidates.sort(key=lambda item: (
                -(item["volume_ratio"] if item["volume_ratio"] is not None else -math.inf),
                item["fear_score"], item["etf_symbol"],
            ))
        # Several indexes can share an ETF proxy; only the strongest candidate is actionable.
        seen: set[str] = set()
        signals[day] = [item for item in candidates if not (item["etf_symbol"] in seen or seen.add(item["etf_symbol"]))]
    return signals


def max_drawdown(values: pd.Series) -> float:
    return float((values / values.cummax() - 1).min()) if len(values) else 0.0


def _commission(gross: float, rate: float, minimum: float) -> float:
    return max(minimum, gross * rate) if gross > 0 else 0.0


def build_top_signals(
    fear: pd.DataFrame,
    mapping: dict[str, str],
    *,
    top_threshold: float,
    bottom_ma_window: int,
    start_date: str,
    end_date: str,
) -> dict[Any, set[str]]:
    """Fear-top reversal sell signals: MA rolls over after touching extreme greed.

    Mirrors the bottom-reversal buy flag: is_top = fear_ma turning down while
    recent_fear_max exceeded top_threshold within the MA window.
    Returns {trade_date: {index_symbol}}.
    """
    pairs = pd.DataFrame(
        [{"index_symbol": index} for index in mapping]
    )
    merged = pairs.merge(fear, on="index_symbol")
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    merged = merged[(merged["date"] >= start) & (merged["date"] <= end)]
    result: dict[Any, set[str]] = {}
    for index_symbol, group in merged.groupby("index_symbol"):
        group = group.sort_values("date")
        ma = group["score"].rolling(bottom_ma_window, min_periods=bottom_ma_window).mean()
        recent_max = group["score"].rolling(bottom_ma_window, min_periods=bottom_ma_window).max()
        prior_ma = ma.shift(1)
        for row, ma_val, prior_val, rmax in zip(
            group.itertuples(index=False), ma, prior_ma, recent_max
        ):
            if not all(
                np.isfinite(v) for v in (ma_val, prior_val, rmax) if v is not None
            ):
                continue
            if ma_val < prior_val and rmax > top_threshold:
                result.setdefault(row.date, set()).add(index_symbol)
    return result


def run_backtest(
    bars: pd.DataFrame,
    fear: pd.DataFrame,
    signals: dict[Any, list[dict[str, Any]]],
    *,
    start_date: str,
    end_date: str,
    initial_capital: float,
    greed_threshold: float,
    greed_sell_fraction: float,
    stop_loss: float,
    stop_cooldown_days: int,
    trailing_drawdown: float,
    commission_pct: float,
    min_commission: float,
    slippage_pct: float,
    stamp_duty_pct: float,
    lot_size: int,
    max_positions: int,
    buy_when_flat_only: bool = False,
    top_signals: dict[Any, set[str]] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    active_bars = bars[(bars["trade_date"] >= start) & (bars["trade_date"] <= end)].copy()
    dates = sorted(active_bars["trade_date"].unique().tolist())
    quote_columns = [
        "open", "high", "low", "close", "realized_volatility", "volatility_threshold",
    ]
    bars_by_date = {
        day: group.set_index("etf_symbol")[quote_columns].to_dict(orient="index")
        for day, group in active_bars.groupby("trade_date", sort=True)
    }
    fear_active = fear[(fear["date"] >= start) & (fear["date"] <= end)]
    fear_by_date = {
        day: dict(zip(group["index_symbol"], group["score"]))
        for day, group in fear_active.groupby("date")
    }
    commission_rate = commission_pct / 100
    slippage = slippage_pct / 100
    stamp_rate = stamp_duty_pct / 100
    cash = float(initial_capital)
    positions: dict[str, Position] = {}
    pending_buy: dict[str, Any] | None = None
    pending_sells: dict[str, dict[str, Any]] = {}
    last_close: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    buy_blocked_until_index = -1

    for day_index, day in enumerate(dates):
        day_bars = bars_by_date.get(day, {})

        # Orders created after the previous close execute at today's open. Sells
        # run first so their cash is available to a simultaneous buy.
        for symbol, order in list(pending_sells.items()):
            position = positions.get(symbol)
            quote = day_bars.get(symbol)
            if position is None:
                pending_sells.pop(symbol, None)
                continue
            if not quote or not np.isfinite(quote.get("open", np.nan)):
                continue
            fraction = float(order["fraction"])
            if fraction >= 1:
                quantity = position.quantity
            else:
                quantity = int(position.quantity * fraction / lot_size) * lot_size
                quantity = min(position.quantity, max(quantity, min(lot_size, position.quantity)))
            price = float(quote["open"]) * (1 - slippage)
            gross = quantity * price
            fee = _commission(gross, commission_rate, min_commission) + gross * stamp_rate
            allocated_cost = position.cost_basis * quantity / position.quantity
            cash += gross - fee
            position.quantity -= quantity
            position.cost_basis -= allocated_cost
            trades.append({
                "date": str(day), "signal_date": order["signal_date"], "action": "sell",
                "etf_symbol": symbol, "index_symbol": position.index_symbol,
                "quantity": quantity, "price": price, "gross": gross, "fee": fee,
                "pnl": gross - fee - allocated_cost, "reason": order["reason"],
                "fear_score": order.get("fear_score"),
                "realized_volatility": order.get("realized_volatility"),
                "volatility_threshold": order.get("volatility_threshold"),
                "drawdown_from_high_pct": order.get("drawdown_from_high_pct"),
                "drawdown_from_entry_pct": order.get("drawdown_from_entry_pct"),
            })
            if order["reason"] == "stop_loss":
                buy_blocked_until_index = max(
                    buy_blocked_until_index,
                    day_index + stop_cooldown_days,
                )
                pending_buy = None
            if position.quantity <= 0:
                positions.pop(symbol, None)
            elif order["reason"] == "extreme_greed_partial":
                position.greed_reduced = True
                position.volatility_monitoring = True
            pending_sells.pop(symbol, None)

        if pending_buy is not None:
            symbol = pending_buy["etf_symbol"]
            quote = day_bars.get(symbol)
            if quote and np.isfinite(quote.get("open", np.nan)):
                if symbol not in positions and len(positions) < max_positions:
                    nav = cash + sum(
                        pos.quantity * last_close.get(pos.etf_symbol, 0.0)
                        for pos in positions.values()
                    )
                    budget = min(cash, nav * float(pending_buy["target_fraction"]))
                    price = float(quote["open"]) * (1 + slippage)
                    quantity = int(budget / price / lot_size) * lot_size
                    while quantity > 0:
                        gross = quantity * price
                        fee = _commission(gross, commission_rate, min_commission)
                        if gross + fee <= budget + 1e-8:
                            break
                        quantity -= lot_size
                    if quantity > 0:
                        gross = quantity * price
                        fee = _commission(gross, commission_rate, min_commission)
                        cash -= gross + fee
                        positions[symbol] = Position(
                            etf_symbol=symbol, index_symbol=pending_buy["index_symbol"],
                            quantity=quantity, cost_basis=gross + fee, entry_price=price,
                            entry_date=str(day),
                            entry_fear=pending_buy["fear_score"], high_water=float(quote["high"]),
                        )
                        trades.append({
                            "date": str(day), "signal_date": pending_buy["signal_date"],
                            "action": "buy", "etf_symbol": symbol,
                            "index_symbol": pending_buy["index_symbol"], "quantity": quantity,
                            "price": price, "gross": gross, "fee": fee, "pnl": None,
                            "reason": pending_buy["reason"],
                            "fear_score": pending_buy["fear_score"],
                            "fear_ma": pending_buy.get("fear_ma"),
                            "recent_fear_min": pending_buy.get("recent_fear_min"),
                            "recent_fear_max": pending_buy.get("recent_fear_max"),
                            "volume_ratio": pending_buy.get("volume_ratio"),
                            "target_fraction": pending_buy["target_fraction"],
                        })
                pending_buy = None

        for symbol, quote in day_bars.items():
            if np.isfinite(quote.get("close", np.nan)):
                last_close[symbol] = float(quote["close"])

        # Exit signals are evaluated at the close and queued for the next open.
        for symbol, position in list(positions.items()):
            quote = day_bars.get(symbol)
            if not quote or position.entry_date == str(day) or symbol in pending_sells:
                continue
            position.high_water = max(position.high_water, float(quote["high"]))
            fear_score = fear_by_date.get(day, {}).get(position.index_symbol)
            drawdown_from_entry = float(quote["close"]) / position.entry_price - 1
            if drawdown_from_entry < -stop_loss:
                pending_sells[symbol] = {
                    "signal_date": str(day), "reason": "stop_loss",
                    "fraction": 1.0,
                    "fear_score": float(fear_score) if fear_score is not None else None,
                    "drawdown_from_entry_pct": drawdown_from_entry * 100,
                }
                continue
            if (
                top_signals
                and position.index_symbol in top_signals.get(day, set())
            ):
                # 恐贪见顶反转（MA 转跌 + 近期触及极端贪婪）→ 清仓逃顶
                pending_sells[symbol] = {
                    "signal_date": str(day), "reason": "fear_top_reversal",
                    "fraction": 1.0,
                    "fear_score": float(fear_score) if fear_score is not None else None,
                }
                continue
            if not position.greed_reduced and fear_score is not None and fear_score > greed_threshold:
                pending_sells[symbol] = {
                    "signal_date": str(day), "reason": "extreme_greed_partial",
                    "fraction": greed_sell_fraction, "fear_score": float(fear_score),
                }
                continue
            realized_vol = quote.get("realized_volatility")
            vol_threshold = quote.get("volatility_threshold")
            if (
                position.volatility_monitoring and not position.trailing_armed
                and np.isfinite(realized_vol) and np.isfinite(vol_threshold)
                and float(realized_vol) > float(vol_threshold)
            ):
                position.trailing_armed = True
            drawdown = float(quote["close"]) / position.high_water - 1
            if position.trailing_armed and drawdown <= -trailing_drawdown:
                pending_sells[symbol] = {
                    "signal_date": str(day), "reason": "volatility_trailing_stop",
                    "fraction": 1.0, "fear_score": float(fear_score) if fear_score is not None else None,
                    "realized_volatility": float(realized_vol),
                    "volatility_threshold": float(vol_threshold),
                    "drawdown_from_high_pct": drawdown * 100,
                }

        # At most one new ETF is selected each close. Candidates are already
        # ranked by the signal day's volume ratio, using only data available then.
        stop_loss_pending = any(
            order["reason"] == "stop_loss" for order in pending_sells.values()
        )
        cooldown_active = day_index < buy_blocked_until_index
        slots_available = (
            len(positions) == 0 if buy_when_flat_only else len(positions) < max_positions
        )
        if (
            pending_buy is None and not stop_loss_pending and not cooldown_active
            and cash > 0 and slots_available
        ):
            candidates = [item for item in signals.get(day, []) if item["etf_symbol"] not in positions]
            if candidates:
                pending_buy = {**candidates[0], "signal_date": str(day)}

        market_value = sum(
            position.quantity * last_close.get(symbol, 0.0)
            for symbol, position in positions.items()
        )
        value = cash + market_value
        curve.append({
            "date": str(day), "value": value, "cash": cash,
            "market_value": market_value,
            "exposure_pct": market_value / value * 100 if value else 0.0,
            "holding_count": len(positions),
            "positions": ",".join(sorted(positions)),
            "trailing_armed_count": sum(item.trailing_armed for item in positions.values()),
            "buy_cooldown_days_remaining": max(0, buy_blocked_until_index - day_index),
        })
    return pd.DataFrame(curve), pd.DataFrame(trades)


def summarize(curve: pd.DataFrame, trades: pd.DataFrame, initial_capital: float) -> dict[str, Any]:
    if curve.empty:
        raise ValueError("回测区间没有交易日")
    values = curve["value"].astype(float)
    start = pd.Timestamp(curve.iloc[0]["date"])
    end = pd.Timestamp(curve.iloc[-1]["date"])
    years = max((end - start).days / 365.25, 1 / 365.25)
    total_return = values.iloc[-1] / initial_capital - 1
    daily = values.pct_change().dropna()
    daily_std = float(daily.std(ddof=1)) if len(daily) else math.nan
    sells = trades[trades["action"] == "sell"] if len(trades) else trades
    gross_turnover = float(trades["gross"].sum()) if len(trades) else 0.0
    return {
        "start_date": str(start.date()), "end_date": str(end.date()),
        "initial_capital": initial_capital, "final_value": float(values.iloc[-1]),
        "total_return_pct": total_return * 100,
        "annualized_return_pct": ((1 + total_return) ** (1 / years) - 1) * 100,
        "max_drawdown_pct": max_drawdown(values) * 100,
        "annualized_volatility_pct": daily_std * math.sqrt(TRADING_DAYS) * 100 if np.isfinite(daily_std) else None,
        "sharpe_zero_rf": float(daily.mean() / daily_std * math.sqrt(TRADING_DAYS)) if daily_std > 0 else None,
        "buy_count": int((trades["action"] == "buy").sum()) if len(trades) else 0,
        "sell_count": int(len(sells)),
        "closed_trade_win_rate_pct": float((sells["pnl"] > 0).mean() * 100) if len(sells) else None,
        "realized_pnl": float(sells["pnl"].sum()) if len(sells) else 0.0,
        "turnover_pct": gross_turnover / initial_capital * 100,
        "average_exposure_pct": float(curve["exposure_pct"].mean()),
        "average_holding_count": float(curve["holding_count"].mean()),
        "ending_positions": [item for item in str(curve.iloc[-1]["positions"]).split(",") if item],
    }
