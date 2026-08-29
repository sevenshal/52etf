"""1m缠论买点 + 已完成30m/日K上行过滤的T+1收盘实验。"""
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
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "lab")]
from src.core.services.chan_analysis import analyze_bars  # noqa: E402
from chan_signal_pair_backtest import (  # noqa: E402
    BacktestConfig, _in_intervals, _load_bars, _load_daily_filters,
    _load_weights, _membership_intervals,
)


BUY_TYPES = ("一买", "二买", "三买")


def _uptrend_state(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        return pd.DataFrame(columns=["timestamp", "uptrend"])
    out = bars[["timestamp", "close"]].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    out["ema20"] = out["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    out["uptrend"] = (out["close"] > out["ema20"]) & (out["ema20"] > out["ema20"].shift(1))
    return out[["timestamp", "uptrend"]]


def _state_before(states: pd.DataFrame, timestamp: pd.Timestamp) -> bool:
    if states.empty:
        return False
    i = states["timestamp"].searchsorted(timestamp, side="left") - 1
    return bool(states.iloc[i]["uptrend"]) if i >= 0 else False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    ap.add_argument("--supplemental-weights", required=True)
    ap.add_argument("--start-date", default="2020-01-01")
    ap.add_argument("--end-date", default="2026-08-26")
    ap.add_argument("--output", default="lab/output/chan_signal_pair_backtest_20260826_marked/multilevel_1m_t1_summary.csv")
    args = ap.parse_args()
    cfg = BacktestConfig(args.database, date.fromisoformat(args.start_date), date.fromisoformat(args.end_date), 100_000, 20, 15, 25, args.supplemental_weights)
    con = duckdb.connect(cfg.database, read_only=True)
    records: list[dict] = []
    try:
        weights = _load_weights(con, cfg)
        intervals = _membership_intervals(weights)
        symbols = sorted(intervals)
        filt = _load_daily_filters(con, symbols, cfg)
        filt["eligible"] = filt["not_st"].fillna(False) & (filt["liquidity_observations"] >= 20) & (filt["avg_amount"] >= 100_000)
        eligible = {s: dict(zip(g.trade_date, g.eligible, strict=False)) for s, g in filt.groupby("ts_code")}
        for n, symbol in enumerate(symbols, 1):
            try:
                bars = _load_bars(con, symbol, "1m", cfg)
                higher30 = _uptrend_state(_load_bars(con, symbol, "30m", cfg))
                higherd = _uptrend_state(_load_bars(con, symbol, "d", cfg))
                if len(bars) < 20:
                    continue
                analysis = analyze_bars(symbol, bars.to_dict("records"), "1m", include_history=True)
                times = pd.to_datetime(bars["timestamp"])
                dates = times.dt.date.to_numpy()
                index = {pd.Timestamp(t): i for i, t in enumerate(times)}
                for signal in analysis["signal_history"]:
                    buy_type = signal["type"]
                    if buy_type not in BUY_TYPES:
                        continue
                    signal_time = pd.Timestamp(signal["bar_time"])
                    i = index.get(signal_time)
                    if i is None or i + 1 >= len(bars):
                        continue
                    signal_date = signal_time.date()
                    if not (_in_intervals(signal_date, intervals[symbol]) and eligible.get(symbol, {}).get(signal_date, False)):
                        continue
                    # Strictly earlier completed higher-timeframe bars only.
                    if not (_state_before(higher30, signal_time) and _state_before(higherd, signal_time)):
                        continue
                    entry_i = i + 1
                    entry_date = dates[entry_i]
                    next_dates = sorted({d for d in dates[entry_i + 1:] if d > entry_date})
                    if not next_dates:
                        continue
                    t1 = next_dates[0]
                    exit_i = int(np.flatnonzero(dates == t1)[-1])
                    entry = float(bars.iloc[entry_i].open)
                    close = float(bars.iloc[exit_i].close)
                    net = close * (1 - cfg.sell_cost_bps / 10_000) / (entry * (1 + cfg.buy_cost_bps / 10_000)) - 1
                    records.append({"freq": "1m", "buy_type": buy_type, "symbol": symbol, "signal_time": signal_time, "entry_time": times.iloc[entry_i], "t1_close_time": times.iloc[exit_i], "net_return": net})
            except Exception:
                continue
            if n % 200 == 0 or n == len(symbols):
                print(f"1m multilevel: {n}/{len(symbols)} symbols, {len(records)} observations", flush=True)
    finally:
        con.close()
    frame = pd.DataFrame(records, columns=["freq", "buy_type", "symbol", "signal_time", "entry_time", "t1_close_time", "net_return"])
    rows = []
    for buy_type in BUY_TYPES:
        x = frame[frame.buy_type == buy_type]
        r = x.net_return.astype(float)
        wins, losses = r[r > 0], r[r < 0]
        rows.append({"freq": "1m", "filter": "30m_uptrend_AND_daily_uptrend", "buy_type": buy_type, "observation_count": len(x), "symbol_count": x.symbol.nunique(), "win_rate": (r > 0).mean() if len(r) else np.nan, "avg_net_return": r.mean() if len(r) else np.nan, "median_net_return": r.median() if len(r) else np.nan, "profit_factor": wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() else np.inf})
    out = ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    frame.to_csv(out.with_name("multilevel_1m_t1_trades.csv"), index=False)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
