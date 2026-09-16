#!/usr/bin/env python3
"""独立复核 sector_nine_turn_fear_gate_backtest 的规则与账目。

两层检查：

1. **不变量**：直接在导出的 CSV 上核对"买在信号次日""同一次低 9 只用一次""卖出回撤 > 2 ATR"等规则；
2. **交叉复算**：用 pandas 重新实现一遍九转计数/ATR（不调用生产函数），在抽样交易上比对
   买卖信号日的 ``highCount`` / ``lowCount`` / 回撤，确认生产口径函数没被误用。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.sector_nine_turn_fear_gate_backtest import (  # noqa: E402
    COMMISSION_RATE,
    DEFAULT_ANALYTICS_DB,
    DEFAULT_OUTPUT,
    STAMP_DUTY_RATE,
)

SAMPLE_SIZE = 200


def _run_lengths(flag: pd.Series) -> pd.Series:
    normalized = flag.fillna(False).astype(bool)
    groups = (~normalized).cumsum()
    return normalized.astype(int).groupby(groups).cumsum().where(normalized, 0).astype(int)


def _independent_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """不依赖 indicators.py 的九转/ATR 复算。"""
    frame = bars.sort_values("trade_date").reset_index(drop=True).copy()
    close = pd.to_numeric(frame["close"], errors="coerce")
    lag4 = close.shift(4)
    frame["high_count"] = _run_lengths(close > lag4)
    frame["low_count"] = _run_lengths(close < lag4)
    previous_close = close.shift(1)
    frame["atr14"] = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1).rolling(14, min_periods=14).mean()
    red = frame["high_count"] >= 2
    frame["last_red_close"] = close.where(red).ffill()
    frame["drawdown_atr"] = (frame["last_red_close"] - close) / frame["atr14"]
    return frame.set_index(frame["trade_date"].dt.date)


def validate(output: Path = Path(DEFAULT_OUTPUT), analytics_db: str = DEFAULT_ANALYTICS_DB,
             sample_size: int = SAMPLE_SIZE) -> dict[str, object]:
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    trades = pd.read_csv(output / "trades_full.csv", parse_dates=[
        "sector_signal_date", "buy_signal_date", "buy_date", "sell_signal_date", "sell_date",
    ])
    triggers = pd.read_csv(output / "sector_triggers.csv", parse_dates=["signal_date"])
    summary = pd.read_csv(output / "variant_summary.csv")
    curves = pd.read_csv(output / "equity_curves.csv", index_col=0, parse_dates=True)

    closed = trades[trades["closed"]]
    checks: dict[str, bool] = {
        "buy_after_signal": bool((trades["buy_date"] > trades["buy_signal_date"]).all()),
        "sector_signal_not_after_stock_signal": bool(
            (trades["sector_signal_date"] <= trades["buy_signal_date"]).all()
        ),
        "sector_fear_within_gate": bool(
            (trades["sector_fear_score"] <= metadata["fear_threshold"]).all()
        ),
        "sell_after_signal": bool((closed["sell_date"] > closed["sell_signal_date"]).all()),
        "sell_drawdown_over_threshold": bool(
            (closed["sell_drawdown_atr"] > metadata["sell_atr_threshold"]).all()
        ),
        "no_duplicate_open_signal": bool(
            not trades.duplicated(["ts_code", "buy_signal_date"]).any()
        ),
        "net_return_matches_prices": bool(np.allclose(
            trades["net_return"],
            trades["sell_price"] / trades["buy_price"]
            * (1 - COMMISSION_RATE) * (1 - COMMISSION_RATE - STAMP_DUTY_RATE) - 1,
            atol=1e-9,
        )),
        "triggers_inside_window": bool(
            (triggers["signal_date"].dt.date >= pd.Timestamp(metadata["start"]).date()).all()
            and (triggers["signal_date"].dt.date <= pd.Timestamp(metadata["end"]).date()).all()
        ),
        "gated_trigger_count_matches": bool(
            int(triggers["fear_pass"].sum()) == metadata["sector_triggers_fear_pass"]
        ),
        "equity_curves_positive": bool((curves.dropna(how="all").fillna(1.0) > 0).all().all()),
        "summary_has_all_variants": bool(len(summary) == summary["variant"].nunique()),
    }

    sample = trades.sample(min(sample_size, len(trades)), random_state=7)
    symbols = sorted(sample["ts_code"].unique())
    start = pd.Timestamp(metadata["start"]).date() - timedelta(days=400)
    end = pd.Timestamp(metadata["end"]).date()
    connection = duckdb.connect(analytics_db, read_only=True)
    try:
        connection.register("sample_codes", pd.DataFrame({"ts_code": symbols}))
        prices = connection.execute(
            """
            SELECT q.ts_code, q.trade_date, q.open, q.high, q.low, q.close
            FROM a_stock_market_daily_qfq q
            JOIN sample_codes c USING (ts_code)
            WHERE q.trade_date BETWEEN ? AND ?
              AND q.open > 0 AND q.high > 0 AND q.low > 0 AND q.close > 0 AND q.vol > 0
            ORDER BY q.ts_code, q.trade_date
            """,
            [start, end],
        ).df()
    finally:
        connection.close()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    frames = {code: _independent_frame(group) for code, group in prices.groupby("ts_code", sort=True)}

    buy_high2 = buy_prior_low9 = sell_low2 = sell_drawdown = sell_prior_high9 = 0
    checked_buys = checked_sells = 0
    for row in sample.itertuples(index=False):
        frame = frames.get(row.ts_code)
        if frame is None:
            continue
        signal_day = row.buy_signal_date.date()
        if signal_day not in frame.index:
            continue
        checked_buys += 1
        position = frame.index.get_loc(signal_day)
        buy_high2 += int(frame["high_count"].iloc[position] == 2)
        window = frame["low_count"].iloc[max(0, position - 60):position]
        buy_prior_low9 += int((window >= 9).any())
        if not row.closed or pd.isna(row.sell_signal_date):
            continue
        sell_day = row.sell_signal_date.date()
        if sell_day not in frame.index:
            continue
        checked_sells += 1
        sell_position = frame.index.get_loc(sell_day)
        sell_low2 += int(frame["low_count"].iloc[sell_position] == 2)
        sell_drawdown += int(float(frame["drawdown_atr"].iloc[sell_position]) > metadata["sell_atr_threshold"])
        entry_position = frame.index.get_loc(row.buy_date.date()) if row.buy_date.date() in frame.index else position + 1
        sell_prior_high9 += int((frame["high_count"].iloc[entry_position:sell_position] >= 9).any())

    checks.update({
        "recomputed_buy_is_high2": checked_buys > 0 and buy_high2 == checked_buys,
        "recomputed_buy_has_prior_low9": checked_buys > 0 and buy_prior_low9 == checked_buys,
        "recomputed_sell_is_low2": checked_sells > 0 and sell_low2 == checked_sells,
        "recomputed_sell_drawdown_over_threshold": checked_sells > 0 and sell_drawdown == checked_sells,
        "recomputed_sell_has_prior_high9": checked_sells > 0 and sell_prior_high9 == checked_sells,
    })

    report = {
        "checks": checks,
        "checked_buys": checked_buys,
        "checked_sells": checked_sells,
        "trades": int(len(trades)),
        "all_passed": all(checks.values()),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["all_passed"]:
        raise SystemExit(1)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--analytics-db", default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--sample-size", type=int, default=SAMPLE_SIZE)
    args = parser.parse_args()
    validate(Path(args.output), args.analytics_db, args.sample_size)


if __name__ == "__main__":
    main()
