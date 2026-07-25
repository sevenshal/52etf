#!/usr/bin/env python3
"""A-share ETF fear/volume portfolio backtest.

Signals are observed at the close and executed at the next trading-day open.
The script is intentionally self-contained and read-only with respect to the
project databases.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


SQLITE_PATH = Path("/var/lib/quant_robot/evc_stocks.db")
DUCKDB_PATH = Path("/var/lib/quant_robot/analytics.duckdb")
INITIAL_CAPITAL = 1_000_000.0
COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
SLIPPAGE_RATE = 0.0005
LOT_SIZE = 100
MAX_POSITIONS = 3
MAX_WEIGHT = 0.50
MIN_HOLD_DAYS = 5
COOLDOWN_DAYS = 5

TARGETS = {
    "000510.SH": ("563360.SH", "A500"),
    "000905.SH": ("510500.SH", "中证500"),
    "000985.SH": ("510300.SH", "沪深300代理"),
    "000688.SH": ("588000.SH", "科创50"),
    "000699.SH": ("588230.SH", "科创200"),
    "399006.SZ": ("159915.SZ", "创业板"),
    "399975.SZ": ("512880.SH", "证券"),
    "H30184.CSI": ("512480.SH", "半导体"),
    "399989.SZ": ("512170.SH", "医疗"),
    "000819.SH": ("512400.SH", "有色"),
    "399967.SZ": ("512660.SH", "军工"),
    "930997.CSI": ("515030.SH", "新能源车"),
    "000932.SH": ("159928.SZ", "消费"),
    "399986.SZ": ("512800.SH", "银行"),
    "399998.SZ": ("515220.SH", "煤炭"),
    "000015.SH": ("510880.SH", "红利"),
}
ETF_TO_INDEX = {etf: index_symbol for index_symbol, (etf, _) in TARGETS.items()}
ETF_NAMES = {etf: name for _, (etf, name) in TARGETS.items()}


@dataclass(frozen=True)
class Params:
    buy_fear: float = 25.0
    volume_ratio: float = 1.1
    rotate_fear: float = 40.0
    cash_fear: float = 75.0
    require_rebound: bool = False

    @property
    def key(self) -> str:
        suffix = "rebound" if self.require_rebound else "raw"
        return (
            f"b{self.buy_fear:g}_v{self.volume_ratio:g}_"
            f"r{self.rotate_fear:g}_c{self.cash_fear:g}_{suffix}"
        )


def load_data(start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    symbols = list(TARGETS)
    placeholders = ",".join("?" for _ in symbols)
    with sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro&immutable=1", uri=True) as conn:
        fear = pd.read_sql_query(
            f"""
            SELECT symbol AS index_symbol, date, score, etf_volume AS index_volume
            FROM etf_fear_greed_clone_history
            WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ?
            ORDER BY symbol, date
            """,
            conn,
            params=[*symbols, start, end],
            parse_dates=["date"],
        )
    fear["etf"] = fear["index_symbol"].map(lambda value: TARGETS[value][0])
    fear["score"] = pd.to_numeric(fear["score"], errors="coerce")
    fear["index_volume"] = pd.to_numeric(fear["index_volume"], errors="coerce")
    fear["volume_ma20"] = fear.groupby("index_symbol")["index_volume"].transform(
        lambda values: values.shift(1).rolling(20, min_periods=20).mean()
    )
    fear["volume_ratio"] = fear["index_volume"] / fear["volume_ma20"]
    fear["previous_score"] = fear.groupby("index_symbol")["score"].shift(1)
    fear["rebound"] = fear["score"] > fear["previous_score"]
    fear = fear.set_index(["date", "etf"]).sort_index()

    etfs = [item[0] for item in TARGETS.values()]
    placeholders = ",".join("?" for _ in etfs)
    db = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        prices = db.execute(
            f"""
            SELECT trade_date AS date, ts_code AS etf, open, close
            FROM a_stock_fund_daily_qfq
            WHERE ts_code IN ({placeholders}) AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date, ts_code
            """,
            [*etfs, start, end],
        ).df()
    finally:
        db.close()
    prices["date"] = pd.to_datetime(prices["date"])
    prices["open"] = pd.to_numeric(prices["open"], errors="coerce")
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    prices["ma20"] = prices.groupby("etf")["close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    prices = prices.set_index(["date", "etf"]).sort_index()
    return fear, prices


def commission(notional: float) -> float:
    return max(MIN_COMMISSION, abs(notional) * COMMISSION_RATE)


def metrics(equity: pd.Series, trades: pd.DataFrame, initial: float) -> dict:
    equity = equity.dropna()
    returns = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    total_return = equity.iloc[-1] / initial - 1
    cagr = (equity.iloc[-1] / initial) ** (1 / years) - 1
    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min())
    sharpe = (
        float(np.sqrt(252) * returns.mean() / returns.std(ddof=1))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else 0.0
    sells = trades[trades["side"] == "sell"] if not trades.empty else trades
    wins = int((sells["pnl"] > 0).sum()) if not sells.empty else 0
    return {
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
        "ending_equity": round(float(equity.iloc[-1]), 2),
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "calmar": float(calmar),
        "trades": int(len(trades)),
        "sell_trades": int(len(sells)),
        "win_rate": float(wins / len(sells)) if len(sells) else 0.0,
        "turnover": float(trades["notional"].abs().sum() / equity.mean()) if not trades.empty else 0.0,
        "fees": float(trades["fee"].sum()) if not trades.empty else 0.0,
    }


def run_backtest(
    fear: pd.DataFrame,
    prices: pd.DataFrame,
    params: Params,
    start: str,
    end: str,
) -> tuple[dict, pd.Series, pd.DataFrame]:
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    price_lookup = prices.attrs.get("record_lookup")
    if price_lookup is None:
        price_lookup = prices.to_dict(orient="index")
        prices.attrs["record_lookup"] = price_lookup
    fear_lookup = fear.attrs.get("record_lookup")
    if fear_lookup is None:
        fear_lookup = fear.to_dict(orient="index")
        fear.attrs["record_lookup"] = fear_lookup
    all_dates = prices.attrs.get("trading_dates")
    if all_dates is None:
        all_dates = prices.index.get_level_values("date").unique().sort_values()
        prices.attrs["trading_dates"] = all_dates
    dates = all_dates[(all_dates >= start_ts) & (all_dates <= end_ts)]
    cash = INITIAL_CAPITAL
    positions: dict[str, dict] = {}
    cooldown_until: dict[str, int] = {}
    pending_sells: dict[str, str] = {}
    pending_buys: list[str] = []
    records: list[dict] = []
    trade_records: list[dict] = []

    def price_value(day: pd.Timestamp, etf: str, field: str) -> float | None:
        row = price_lookup.get((day, etf))
        if row is None:
            return None
        value = row.get(field)
        return float(value) if pd.notna(value) and float(value) > 0 else None

    def signal_value(day: pd.Timestamp, etf: str, field: str) -> float | bool | None:
        row = fear_lookup.get((day, etf))
        if row is None:
            return None
        value = row.get(field)
        return value if pd.notna(value) else None

    for day_number, day in enumerate(dates):
        # Execute yesterday's close-generated orders at today's open.
        membership_changed = bool(pending_sells or pending_buys)
        for etf, reason in list(pending_sells.items()):
            if etf not in positions:
                continue
            open_price = price_value(day, etf, "open")
            if open_price is None:
                continue
            fill_price = open_price * (1 - SLIPPAGE_RATE)
            shares = positions[etf]["shares"]
            notional = shares * fill_price
            fee = commission(notional)
            cash += notional - fee
            pnl = notional - fee - positions[etf]["cost"]
            trade_records.append(
                {
                    "date": day,
                    "etf": etf,
                    "name": ETF_NAMES[etf],
                    "side": "sell",
                    "reason": reason,
                    "shares": shares,
                    "price": fill_price,
                    "notional": notional,
                    "fee": fee,
                    "pnl": pnl,
                }
            )
            del positions[etf]
            cooldown_until[etf] = day_number + COOLDOWN_DAYS

        valid_buys = [
            etf
            for etf in pending_buys
            if etf not in positions
            and len(positions) < MAX_POSITIONS
            and price_value(day, etf, "open") is not None
        ][: max(0, MAX_POSITIONS - len(positions))]
        opens = {
            etf: price_value(day, etf, "open")
            for etf in set(positions).union(valid_buys)
        }
        equity_at_open = cash + sum(
            position["shares"] * opens.get(etf, 0.0)
            for etf, position in positions.items()
            if opens.get(etf) is not None
        )
        final_count = len(positions) + len(valid_buys)
        target_weight = min(MAX_WEIGHT, 1.0 / final_count) if final_count else 0.0
        target_notional = equity_at_open * target_weight

        # When membership changes, rebalance existing holdings down first so a
        # third position can enter at approximately one-third weight.
        if membership_changed and target_notional > 0:
            for etf, position in list(positions.items()):
                open_price = opens.get(etf)
                if open_price is None:
                    continue
                desired_shares = (
                    math.floor(target_notional / open_price / LOT_SIZE) * LOT_SIZE
                )
                sell_shares = max(0, position["shares"] - desired_shares)
                if sell_shares <= 0:
                    continue
                fill_price = open_price * (1 - SLIPPAGE_RATE)
                notional = sell_shares * fill_price
                fee = commission(notional)
                cost_released = position["cost"] * sell_shares / position["shares"]
                cash += notional - fee
                position["shares"] -= sell_shares
                position["cost"] -= cost_released
                trade_records.append(
                    {
                        "date": day,
                        "etf": etf,
                        "name": ETF_NAMES[etf],
                        "side": "sell",
                        "reason": "dynamic_weight_trim",
                        "shares": sell_shares,
                        "price": fill_price,
                        "notional": notional,
                        "fee": fee,
                        "pnl": notional - fee - cost_released,
                    }
                )

        for etf in valid_buys:
            open_price = price_value(day, etf, "open")
            fill_price = open_price * (1 + SLIPPAGE_RATE)
            affordable = min(target_notional, cash / (1 + COMMISSION_RATE))
            shares = math.floor(affordable / fill_price / LOT_SIZE) * LOT_SIZE
            if shares <= 0:
                continue
            notional = shares * fill_price
            fee = commission(notional)
            if notional + fee > cash:
                shares -= LOT_SIZE
                notional = shares * fill_price
                fee = commission(notional) if shares > 0 else 0.0
            if shares <= 0:
                continue
            cash -= notional + fee
            positions[etf] = {
                "shares": shares,
                "cost": notional + fee,
                "entry_day": day_number,
            }
            trade_records.append(
                {
                    "date": day,
                    "etf": etf,
                    "name": ETF_NAMES[etf],
                    "side": "buy",
                    "reason": "fear_volume",
                    "shares": shares,
                    "price": fill_price,
                    "notional": notional,
                    "fee": fee,
                    "pnl": np.nan,
                }
            )

        # Reinvest sale proceeds into surviving holdings up to the new dynamic
        # target (50% with one/two holdings, one-third with three).
        if membership_changed and target_notional > 0:
            for etf, position in positions.items():
                if etf in valid_buys:
                    continue
                open_price = opens.get(etf)
                if open_price is None:
                    continue
                fill_price = open_price * (1 + SLIPPAGE_RATE)
                current_notional = position["shares"] * open_price
                affordable = min(
                    max(0.0, target_notional - current_notional),
                    cash / (1 + COMMISSION_RATE),
                )
                buy_shares = (
                    math.floor(affordable / fill_price / LOT_SIZE) * LOT_SIZE
                )
                if buy_shares <= 0:
                    continue
                notional = buy_shares * fill_price
                fee = commission(notional)
                if notional + fee > cash:
                    buy_shares -= LOT_SIZE
                    notional = buy_shares * fill_price
                    fee = commission(notional) if buy_shares > 0 else 0.0
                if buy_shares <= 0:
                    continue
                cash -= notional + fee
                position["shares"] += buy_shares
                position["cost"] += notional + fee
                trade_records.append(
                    {
                        "date": day,
                        "etf": etf,
                        "name": ETF_NAMES[etf],
                        "side": "buy",
                        "reason": "dynamic_weight_topup",
                        "shares": buy_shares,
                        "price": fill_price,
                        "notional": notional,
                        "fee": fee,
                        "pnl": np.nan,
                    }
                )

        pending_sells = {}
        pending_buys = []

        close_values = {
            etf: price_value(day, etf, "close")
            for etf in positions
        }
        equity = cash + sum(
            position["shares"] * close_values.get(etf, 0.0)
            for etf, position in positions.items()
            if close_values.get(etf) is not None
        )
        max_position_weight = max(
            (
                position["shares"] * close_values.get(etf, 0.0) / equity
                for etf, position in positions.items()
                if close_values.get(etf) is not None and equity > 0
            ),
            default=0.0,
        )
        records.append(
            {
                "date": day,
                "equity": equity,
                "cash": cash,
                "positions": len(positions),
                "max_position_weight": max_position_weight,
            }
        )

        # Generate orders using information available at today's close.
        forced_sells: dict[str, str] = {}
        for etf, position in positions.items():
            score = signal_value(day, etf, "score")
            if score is not None and float(score) > params.cash_fear:
                forced_sells[etf] = "fear_above_cash_threshold"

        candidates = []
        for etf in TARGETS.values():
            etf_symbol = etf[0]
            if etf_symbol in positions or day_number < cooldown_until.get(etf_symbol, -1):
                continue
            score = signal_value(day, etf_symbol, "score")
            ratio = signal_value(day, etf_symbol, "volume_ratio")
            rebound = signal_value(day, etf_symbol, "rebound")
            if score is None or ratio is None:
                continue
            if float(score) >= params.buy_fear or float(ratio) < params.volume_ratio:
                continue
            if params.require_rebound and rebound is not True and not bool(rebound):
                continue
            candidates.append((float(score), -float(ratio), etf_symbol))
        candidates.sort()

        projected_count = len(positions) - len(forced_sells)
        slots = MAX_POSITIONS - projected_count
        desired_count = min(len(candidates), MAX_POSITIONS)
        extra_slots_needed = max(0, desired_count - slots)
        rotation_pool = []
        for etf, position in positions.items():
            if etf in forced_sells:
                continue
            score = signal_value(day, etf, "score")
            close_price = price_value(day, etf, "close")
            price_row = price_lookup.get((day, etf), {})
            try:
                ma20 = float(price_row.get("ma20", np.nan))
            except (TypeError, ValueError):
                ma20 = np.nan
            held_days = day_number - position["entry_day"]
            if (
                score is not None
                and float(score) > params.rotate_fear
                and held_days >= MIN_HOLD_DAYS
            ):
                below_ma = bool(np.isfinite(ma20) and close_price is not None and close_price < ma20)
                rotation_pool.append((not below_ma, -float(score), etf))
        rotation_pool.sort()
        rotation_sells = {
            etf: "rotate_to_lower_fear"
            for _, _, etf in rotation_pool[:extra_slots_needed]
        }
        pending_sells = {**forced_sells, **rotation_sells}
        final_slots = MAX_POSITIONS - (len(positions) - len(pending_sells))
        pending_buys = [etf for _, _, etf in candidates[: max(0, final_slots)]]

    equity_series = pd.DataFrame(records).set_index("date")["equity"]
    record_frame = pd.DataFrame(records).set_index("date")
    trades = pd.DataFrame(trade_records)
    result = metrics(equity_series, trades, INITIAL_CAPITAL)
    result["average_positions"] = float(record_frame["positions"].mean())
    result["maximum_positions_observed"] = int(record_frame["positions"].max())
    result["maximum_close_weight_observed"] = float(
        record_frame["max_position_weight"].max()
    )
    result["average_cash_weight"] = float(
        (record_frame["cash"] / record_frame["equity"]).mean()
    )
    return result, equity_series, trades


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-03-22")
    parser.add_argument("--end", default="2026-07-23")
    parser.add_argument("--train-end", default="2025-03-31")
    parser.add_argument("--test-start", default="2025-04-01")
    parser.add_argument("--output", default="/private/tmp/a_stock_fear_volume_backtest.json")
    args = parser.parse_args()

    lookback_start = (pd.Timestamp(args.start) - pd.Timedelta(days=60)).date().isoformat()
    fear, prices = load_data(lookback_start, args.end)
    fixed = [
        Params(require_rebound=False),
        Params(require_rebound=True),
    ]
    full_results = []
    for params in fixed:
        result, _, trades = run_backtest(fear, prices, params, args.start, args.end)
        result.update({"params": params.__dict__, "key": params.key})
        if not trades.empty:
            result["buy_counts"] = (
                trades[trades["side"] == "buy"]["name"].value_counts().to_dict()
            )
            result["sell_reasons"] = (
                trades[trades["side"] == "sell"]["reason"].value_counts().to_dict()
            )
        full_results.append(result)

    grid_results = []
    for values in itertools.product(
        [20.0, 25.0, 30.0],
        [1.1, 1.3, 1.5],
        [40.0, 50.0, 60.0],
        [70.0, 75.0, 80.0],
        [False, True],
    ):
        params = Params(*values)
        train_result, _, _ = run_backtest(fear, prices, params, args.start, args.train_end)
        train_result.update({"params": params.__dict__, "key": params.key})
        grid_results.append(train_result)

    eligible = [item for item in grid_results if item["sell_trades"] >= 8]
    eligible.sort(
        key=lambda item: (item["calmar"], item["sharpe"], item["cagr"]),
        reverse=True,
    )
    selected = eligible[:10]
    out_of_sample = []
    for train_item in selected:
        params = Params(**train_item["params"])
        test_result, _, _ = run_backtest(
            fear, prices, params, args.test_start, args.end
        )
        test_result.update(
            {
                "params": params.__dict__,
                "key": params.key,
                "train_calmar": train_item["calmar"],
                "train_cagr": train_item["cagr"],
                "train_max_drawdown": train_item["max_drawdown"],
            }
        )
        out_of_sample.append(test_result)

    payload = {
        "assumptions": {
            "initial_capital": INITIAL_CAPITAL,
            "signal_execution": "close signal, next trading-day open execution",
            "commission_rate": COMMISSION_RATE,
            "minimum_commission": MIN_COMMISSION,
            "slippage_each_side": SLIPPAGE_RATE,
            "lot_size": LOT_SIZE,
            "max_positions": MAX_POSITIONS,
            "max_target_weight": MAX_WEIGHT,
            "dynamic_weighting": (
                "one holding 50%; two holdings 50% each; "
                "three holdings approximately one-third each"
            ),
            "minimum_hold_days_for_rotation": MIN_HOLD_DAYS,
            "cooldown_days_after_sale": COOLDOWN_DAYS,
            "volume_ma": "previous 20 observations, excluding signal day",
            "rebound_definition": "fear score strictly above previous observation",
        },
        "fixed_full_sample": full_results,
        "top_train_then_oos": out_of_sample,
        "grid_size": len(grid_results),
    }
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
