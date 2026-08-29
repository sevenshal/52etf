"""A股T+1约束下，缠论一/二/三买到T+1收盘的分钟级收益统计。"""

from __future__ import annotations

import argparse
import itertools
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from src.core.services.chan_analysis import analyze_bars  # noqa: E402
from chan_signal_pair_backtest import (  # noqa: E402
    BacktestConfig,
    INDEX_CODES,
    _in_intervals,
    _load_bars,
    _load_daily_filters,
    _load_weights,
    _membership_intervals,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    ap.add_argument("--supplemental-weights", required=True)
    ap.add_argument("--start-date", default="2026-07-13")
    ap.add_argument("--end-date", default="2026-08-26")
    ap.add_argument("--output", default="lab/output/chan_signal_pair_backtest_20260826_marked/t1_close_summary.csv")
    ap.add_argument("--min-avg-amount", type=float, default=100_000.0)
    ap.add_argument("--liquidity-days", type=int, default=20)
    ap.add_argument("--buy-cost-bps", type=float, default=15.0)
    ap.add_argument("--sell-cost-bps", type=float, default=25.0)
    ap.add_argument("--symbol-limit", type=int)
    ap.add_argument("--symbol-offset", type=int, default=0)
    ap.add_argument("--freqs", nargs="+", choices=("1m", "5m", "30m"), default=["1m", "5m", "30m"])
    args = ap.parse_args()
    cfg = BacktestConfig(
        database=args.database, start_date=date.fromisoformat(args.start_date),
        end_date=date.fromisoformat(args.end_date), min_avg_amount=args.min_avg_amount,
        liquidity_days=args.liquidity_days, buy_cost_bps=args.buy_cost_bps,
        sell_cost_bps=args.sell_cost_bps, supplemental_weights=args.supplemental_weights,
    )
    con = duckdb.connect(cfg.database, read_only=True)
    try:
        weights = _load_weights(con, cfg)
        intervals = _membership_intervals(weights)
        symbols = sorted(intervals)
        symbols = symbols[args.symbol_offset:]
        if args.symbol_limit:
            symbols = symbols[:args.symbol_limit]
        filters = _load_daily_filters(con, symbols, cfg)
        filters["eligible"] = (
            filters["not_st"].fillna(False)
            & (filters["liquidity_observations"] >= cfg.liquidity_days)
            & (filters["avg_amount"] >= cfg.min_avg_amount)
        )
        eligible = {
            s: dict(zip(g.trade_date, g.eligible, strict=False))
            for s, g in filters.groupby("ts_code")
        }
        records: list[dict] = []
        incomplete: list[dict] = []
        for freq in args.freqs:
            for n, symbol in enumerate(symbols, 1):
                try:
                    bars = _load_bars(con, symbol, freq, cfg)
                    if len(bars) < 20:
                        continue
                    analysis = analyze_bars(symbol, bars.to_dict("records"), freq, include_history=True)
                    times = pd.to_datetime(bars.timestamp)
                    by_time: dict[pd.Timestamp, set[str]] = defaultdict(set)
                    for signal in analysis["signal_history"]:
                        by_time[pd.Timestamp(signal["bar_time"])].add(signal["type"])
                    idx = {pd.Timestamp(t): i for i, t in enumerate(times)}
                    dates = times.dt.date.to_numpy()
                    for signal_time, signal_types in by_time.items():
                        buy_types = sorted(set(signal_types) & {"一买", "二买", "三买"})
                        if not buy_types:
                            continue
                        i = idx.get(signal_time)
                        if i is None or i + 1 >= len(bars):
                            continue
                        signal_date = signal_time.date()
                        if not (_in_intervals(signal_date, intervals[symbol]) and eligible.get(symbol, {}).get(signal_date, False)):
                            continue
                        entry_i = i + 1
                        entry_date = dates[entry_i]
                        future_dates = sorted({d for d in dates[entry_i + 1:] if d > entry_date})
                        if not future_dates:
                            for buy_type in buy_types:
                                incomplete.append({"freq": freq, "symbol": symbol, "buy_type": buy_type, "reason": "no_T1_close"})
                            continue
                        t1_date = future_dates[0]
                        t1_indices = np.flatnonzero(dates == t1_date)
                        exit_i = int(t1_indices[-1])
                        entry = float(bars.iloc[entry_i].open)
                        exit_price = float(bars.iloc[exit_i].close)
                        net = exit_price * (1 - cfg.sell_cost_bps / 10_000) / (entry * (1 + cfg.buy_cost_bps / 10_000)) - 1
                        for buy_type in buy_types:
                            records.append({
                                "freq": freq, "buy_type": buy_type, "symbol": symbol,
                                "signal_time": signal_time, "entry_time": times[entry_i],
                                "t1_close_time": times[exit_i], "entry_price": entry,
                                "t1_close_price": exit_price, "net_return": net,
                            })
                except Exception:
                    continue
                if n % 200 == 0 or n == len(symbols):
                    print(f"{freq}: {n}/{len(symbols)} symbols, {len(records)} observations", flush=True)
    finally:
        con.close()
    frame = pd.DataFrame(records, columns=["freq", "buy_type", "symbol", "signal_time", "entry_time", "t1_close_time", "entry_price", "t1_close_price", "net_return"])
    rows = []
    for freq, buy_type in itertools.product(args.freqs, ("一买", "二买", "三买")):
        x = frame[(frame.freq == freq) & (frame.buy_type == buy_type)]
        r = x.net_return.astype(float)
        wins, losses = r[r > 0], r[r < 0]
        rows.append({
            "freq": freq, "buy_type": buy_type, "observation_count": len(x),
            "symbol_count": x.symbol.nunique(), "win_rate": (r > 0).mean() if len(r) else np.nan,
            "avg_net_return": r.mean() if len(r) else np.nan, "median_net_return": r.median() if len(r) else np.nan,
            "profit_factor": wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() else np.inf,
            "p05_return": r.quantile(.05) if len(r) else np.nan, "p95_return": r.quantile(.95) if len(r) else np.nan,
        })
    out = ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    frame.to_csv(out.with_name("t1_close_trades.csv"), index=False)
    pd.DataFrame(incomplete).to_csv(out.with_name("t1_close_incomplete.csv"), index=False)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
