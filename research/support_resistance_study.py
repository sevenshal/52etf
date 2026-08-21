#!/usr/bin/env python3
"""Point-in-time A-share support/resistance breakout study.

Reads the production DuckDB in read-only mode and writes reproducible aggregate
results under ``research/output/support_resistance``.  No production state is
modified.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


DEFAULT_DB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"


@dataclass(frozen=True)
class Config:
    start_date: str = "2019-01-01"
    end_date: str = "2026-08-20"
    train_end: str = "2023-12-31"
    universe_size: int = 600
    min_history: int = 500
    lookback: int = 120
    candle_lookback: int = 252
    volume_ratio: float = 1.8
    breakout_atr: float = 0.15
    horizon: int = 20


def load_data(connection: duckdb.DuckDBPyConnection, config: Config) -> pd.DataFrame:
    """Select a liquid universe using training-period information only."""
    sql = """
    WITH eligible AS (
      SELECT ts_code,
             median(amount) AS median_amount,
             count(*) AS history_rows
      FROM a_stock_market_daily_qfq
      WHERE trade_date BETWEEN DATE '2019-01-01' AND DATE '2023-12-31'
        AND amount > 0 AND close > 0
      GROUP BY ts_code
      HAVING count(*) >= ?
      ORDER BY median_amount DESC
      LIMIT ?
    )
    SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
           d.volume, d.amount, d.turnover_rate, b.industry, b.market
    FROM a_stock_market_daily_qfq d
    JOIN eligible e USING (ts_code)
    LEFT JOIN a_stock_basic b USING (ts_code)
    WHERE d.trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
      AND d.open > 0 AND d.high >= greatest(d.open, d.close)
      AND d.low <= least(d.open, d.close) AND d.high >= d.low
      AND d.close > 0 AND d.volume >= 0
    ORDER BY d.ts_code, d.trade_date
    """
    return connection.execute(
        sql,
        [config.min_history, config.universe_size, config.start_date, config.end_date],
    ).fetch_df()


def add_features(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    previous_close = frame["close"].shift(1)
    true_range = np.maximum.reduce(
        [
            (frame["high"] - frame["low"]).to_numpy(),
            (frame["high"] - previous_close).abs().to_numpy(),
            (frame["low"] - previous_close).abs().to_numpy(),
        ]
    )
    frame["atr"] = pd.Series(true_range, index=frame.index).rolling(20).mean().shift(1)
    frame["volume_ma20"] = frame["volume"].rolling(20).mean().shift(1)
    frame["volume_ratio"] = frame["volume"] / frame["volume_ma20"]
    return frame


def nearest_volume_candle_level(
    history: pd.DataFrame, reference: float, side: str, threshold: float
) -> tuple[float, float] | None:
    candidates = history.loc[history["volume_ratio"] >= threshold, ["open", "close"]]
    if candidates.empty:
        return None
    lower = candidates.min(axis=1).to_numpy()
    upper = candidates.max(axis=1).to_numpy()
    if side == "up":
        valid = lower >= reference
        if not valid.any():
            return None
        index = np.where(valid)[0][np.argmin(lower[valid] - reference)]
    else:
        valid = upper <= reference
        if not valid.any():
            return None
        index = np.where(valid)[0][np.argmin(reference - upper[valid])]
    return float(lower[index]), float(upper[index])


def crossing_level(history: pd.DataFrame, reference: float, side: str, atr: float) -> tuple[float, float] | None:
    """Find the strongest price-crossing cluster, weighted by relative volume."""
    if not np.isfinite(atr) or atr <= 0:
        return None
    low = history["low"].to_numpy(float)
    high = history["high"].to_numpy(float)
    weights = np.clip(history["volume_ratio"].fillna(1).to_numpy(float), 0.25, 4.0)
    step = max(0.35 * atr, reference * 0.002)
    grid = np.arange(max(low.min(), reference - 8 * atr), min(high.max(), reference + 8 * atr) + step, step)
    if not len(grid):
        return None
    score = ((low[:, None] <= grid) & (high[:, None] >= grid)).T @ weights
    if side == "up":
        valid = grid >= reference + 0.25 * atr
    else:
        valid = grid <= reference - 0.25 * atr
    if not valid.any():
        return None
    valid_indices = np.where(valid)[0]
    best = valid_indices[np.argmax(score[valid])]
    center = float(grid[best])
    return center - 0.35 * atr, center + 0.35 * atr


def outcome(frame: pd.DataFrame, index: int, direction: int, level: float) -> dict[str, float | bool]:
    atr = float(frame.iloc[index]["atr"])
    entry = float(frame.iloc[index]["close"])
    future = frame.iloc[index + 1 : index + 21]
    if len(future) < 20:
        raise IndexError
    signed_close = direction * (future["close"].to_numpy(float) / entry - 1)
    signed_high = direction * ((future["high"] if direction > 0 else future["low"]).to_numpy(float) - entry)
    signed_low = direction * ((future["low"] if direction > 0 else future["high"]).to_numpy(float) - entry)
    favorable = signed_high / atr
    adverse = signed_low / atr
    hit_win = np.where(favorable >= 2.0)[0]
    hit_loss = np.where(adverse <= -1.0)[0]
    first_win = int(hit_win[0]) if len(hit_win) else 999
    first_loss = int(hit_loss[0]) if len(hit_loss) else 999
    day3 = frame.iloc[index + 3]
    retained = float(day3["close"]) > level if direction > 0 else float(day3["close"]) < level
    confirmed_entry = float(day3["close"])
    confirmed_future = frame.iloc[index + 4 : index + 24]
    if len(confirmed_future) < 20:
        raise IndexError
    confirmed_signed_close = direction * (confirmed_future["close"].to_numpy(float) / confirmed_entry - 1)
    confirmed_favorable = direction * (
        (confirmed_future["high"] if direction > 0 else confirmed_future["low"]).to_numpy(float)
        - confirmed_entry
    ) / atr
    confirmed_adverse = direction * (
        (confirmed_future["low"] if direction > 0 else confirmed_future["high"]).to_numpy(float)
        - confirmed_entry
    ) / atr
    confirmed_win = np.where(confirmed_favorable >= 2.0)[0]
    confirmed_loss = np.where(confirmed_adverse <= -1.0)[0]
    confirmed_first_win = int(confirmed_win[0]) if len(confirmed_win) else 999
    confirmed_first_loss = int(confirmed_loss[0]) if len(confirmed_loss) else 999
    return {
        "retained_3d": bool(retained),
        "win_before_loss": first_win < first_loss,
        "return_5d": float(signed_close[4]),
        "return_20d": float(signed_close[19]),
        "mfe_20d_atr": float(np.max(favorable)),
        "mae_20d_atr": float(np.min(adverse)),
        "confirmed_return_5d": float(confirmed_signed_close[4]),
        "confirmed_return_20d": float(confirmed_signed_close[19]),
        "confirmed_win_before_loss": confirmed_first_win < confirmed_first_loss,
    }


def study_symbol(symbol_frame: pd.DataFrame, config: Config) -> list[dict]:
    frame = add_features(symbol_frame.reset_index(drop=True))
    events: list[dict] = []
    cooldown: dict[tuple[str, int], int] = {}
    start = max(config.candle_lookback, config.lookback, 25)
    for index in range(start, len(frame) - config.horizon - 3):
        row = frame.iloc[index]
        prior = frame.iloc[index - 1]
        atr = float(row["atr"])
        if not np.isfinite(atr) or atr <= 0:
            continue
        levels: dict[str, dict[int, tuple[float, float] | None]] = {
            "donchian": {
                1: (np.nan, float(frame.iloc[index-config.lookback:index]["high"].max())),
                -1: (float(frame.iloc[index-config.lookback:index]["low"].min()), np.nan),
            },
            "volume_candle": {
                1: nearest_volume_candle_level(frame.iloc[index-config.candle_lookback:index], float(prior["close"]), "up", config.volume_ratio),
                -1: nearest_volume_candle_level(frame.iloc[index-config.candle_lookback:index], float(prior["close"]), "down", config.volume_ratio),
            },
            "crossing_density": {
                1: crossing_level(frame.iloc[index-config.lookback:index], float(prior["close"]), "up", atr),
                -1: crossing_level(frame.iloc[index-config.lookback:index], float(prior["close"]), "down", atr),
            },
        }
        for method, method_levels in levels.items():
            for direction, zone in method_levels.items():
                if zone is None:
                    continue
                level = float(zone[1] if direction > 0 else zone[0])
                if not np.isfinite(level):
                    continue
                crossed = (
                    float(prior["close"]) <= level
                    and float(row["close"]) > level + config.breakout_atr * atr
                    if direction > 0
                    else float(prior["close"]) >= level
                    and float(row["close"]) < level - config.breakout_atr * atr
                )
                key = (method, direction)
                if not crossed or index - cooldown.get(key, -999) < 20:
                    continue
                cooldown[key] = index
                try:
                    result = outcome(frame, index, direction, level)
                except IndexError:
                    continue
                events.append(
                    {
                        "ts_code": row["ts_code"],
                        "trade_date": row["trade_date"],
                        "industry": row.get("industry"),
                        "market": row.get("market"),
                        "method": method,
                        "direction": "up" if direction > 0 else "down",
                        "split": "train" if str(row["trade_date"]) <= config.train_end else "test",
                        "close": float(row["close"]),
                        "level": level,
                        "atr": atr,
                        "signal_volume_ratio": float(row["volume_ratio"]),
                        **result,
                    }
                )
    return events


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    return (
        events.groupby(["split", "method", "direction"], observed=True)
        .agg(
            signals=("ts_code", "size"),
            stocks=("ts_code", "nunique"),
            retained_3d=("retained_3d", "mean"),
            win_before_loss=("win_before_loss", "mean"),
            median_return_5d=("return_5d", "median"),
            median_return_20d=("return_20d", "median"),
            mean_return_20d=("return_20d", "mean"),
            median_mfe_20d_atr=("mfe_20d_atr", "median"),
            median_mae_20d_atr=("mae_20d_atr", "median"),
        )
        .reset_index()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=DEFAULT_DB)
    parser.add_argument("--output", default="research/output/support_resistance")
    parser.add_argument("--universe-size", type=int, default=600)
    args = parser.parse_args()
    config = Config(universe_size=args.universe_size)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(args.database, read_only=True) as connection:
        data = load_data(connection, config)
    all_events: list[dict] = []
    for position, (_, symbol_frame) in enumerate(data.groupby("ts_code", sort=True), start=1):
        all_events.extend(study_symbol(symbol_frame, config))
        if position % 50 == 0:
            print(f"processed {position}/{data.ts_code.nunique()} symbols; {len(all_events)} events", flush=True)
    events = pd.DataFrame(all_events)
    summary = summarize(events)
    events.to_csv(output / "events.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    metadata = {
        "config": asdict(config),
        "source": args.database,
        "source_table": "a_stock_market_daily_qfq",
        "rows_loaded": int(len(data)),
        "symbols_loaded": int(data["ts_code"].nunique()),
        "min_date": str(data["trade_date"].min()),
        "max_date": str(data["trade_date"].max()),
        "events": int(len(events)),
    }
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
