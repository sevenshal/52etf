#!/usr/bin/env python3
"""Test staged buys using the live strategy's already-generated signal trades.

The signal/exit/rotation dates come from the unmodified production state
machine.  Only execution changes: a BUY is spread over specified offsets in
trading days; any unfilled tranches are cancelled by the next SELL/rotation.
This isolates staged execution from signal changes.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import a_stock_fear_production_oi_replay as replay


INITIAL_CAPITAL = 1_000_000.0
OUT = ROOT / "research/output/a_stock_fear_split_entry_backtest.json"


@dataclass(frozen=True)
class Plan:
    name: str
    offsets: tuple[int, ...]
    weights: tuple[float, ...]


PLANS = (
    Plan("all_in", (0,), (1.0,)),
    Plan("half_day_0_day_3", (0, 2), (0.5, 0.5)),
    Plan("50_30_20_day_0_2_5", (0, 1, 4), (0.5, 0.3, 0.2)),
)


def _key(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def run_plan(frames: dict, baseline_trades: list[dict], plan: Plan) -> dict:
    """Replay signal trades with staged entry pricing and original exit dates."""
    main = frames["main"].sort_values("date").reset_index(drop=True)
    day_keys = [_key(value) for value in main["date"]]
    day_index = {value: index for index, value in enumerate(day_keys)}
    frame_by_symbol = {frame.attrs["symbol"]: frame.set_index(frame["date"].map(_key)) for frame in frames.values()}
    events: dict[str, list[dict]] = {}
    for trade in baseline_trades:
        events.setdefault(str(trade["date"]), []).append(trade)

    cash = INITIAL_CAPITAL
    shares = 0
    held_symbol: str | None = None
    pending: list[dict] = []
    trades: list[dict] = []
    equity_values = np.zeros(len(day_keys), dtype=float)

    def price(symbol: str, day: str, side: str) -> float:
        row = frame_by_symbol[symbol].loc[day]
        return float(row["high"] if side == "BUY" else row["low"])

    def execute_buy(item: dict, day: str) -> None:
        nonlocal cash, shares, held_symbol
        if (held_symbol is not None and held_symbol != item["symbol"]) or cash <= 0:
            return
        fill = price(item["symbol"], day, "BUY")
        requested = min(cash, item["budget"] * item["weight"])
        quantity = int(requested / fill // 100 * 100)
        if quantity <= 0:
            return
        amount = quantity * fill
        cash -= amount
        shares += quantity
        held_symbol = item["symbol"]
        trades.append({"date": day, "action": "BUY", "symbol": held_symbol, "quantity": quantity, "price": fill, "amount": amount})

    for index, day in enumerate(day_keys):
        # A sell (including a rotation sell) cancels unfilled tranches first.
        today_events = events.get(day, [])
        sell_events = [item for item in today_events if item["action"] == "SELL"]
        if sell_events and held_symbol is not None:
            matching = next((item for item in sell_events if item["symbol"] == held_symbol), None)
            if matching:
                fill = price(held_symbol, day, "SELL")
                amount = shares * fill
                trades.append({"date": day, "action": "SELL", "symbol": held_symbol, "quantity": shares, "price": fill, "amount": amount})
                cash += amount
                shares = 0
                held_symbol = None
                pending = []

        # New signal buys create fixed-notional tranches.  The first can fill today.
        for item in (entry for entry in today_events if entry["action"] == "BUY"):
            budget = cash
            for offset, weight in zip(plan.offsets, plan.weights):
                due_index = index + offset
                if due_index < len(day_keys):
                    pending.append({"due": due_index, "symbol": item["symbol"], "weight": weight, "budget": budget})

        # Pending additions only apply to the currently selected symbol; they are
        # cancelled above when the strategy exits or rotates.
        for item in sorted([item for item in pending if item["due"] == index], key=lambda value: value["weight"], reverse=True):
            execute_buy(item, day)
        pending = [item for item in pending if item["due"] > index]

        close = float(frame_by_symbol[held_symbol].loc[day, "close"]) if held_symbol else 0.0
        equity_values[index] = cash + shares * close

    metrics, _ = replay.fb._compute_equity_metrics(main["date"].tolist(), equity_values)
    return {
        "plan": {"offsets": plan.offsets, "weights": plan.weights},
        **{key: metrics[key] for key in ("total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "calmar_ratio")},
        "trade_count": len(trades),
        "trades": trades,
        "ending_value": float(equity_values[-1]),
    }


def main() -> None:
    replay.install_local_market_calendars(replay.END)
    frames, _ = replay.prepare_frames()
    baseline = replay.run(frames, replay.params_from_config(replay.live_config()), detailed=True)
    results = {plan.name: run_plan(frames, baseline["trades"], plan) for plan in PLANS}
    # The all-in execution overlay must reconcile with the source state machine.
    source = replay.compact(baseline)
    output = {"start": replay.START.isoformat(), "end": replay.END.isoformat(), "source_baseline": source, "plans": results}
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({name: {key: value for key, value in result.items() if key not in {"trades", "plan"}} for name, result in results.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
