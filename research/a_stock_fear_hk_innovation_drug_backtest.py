"""Read-only reproduction and sensitivity test for the live A-share fear/volume strategy.

Uses the production configuration verbatim, except in the proposed runs where
the third candidate is switched to 513120.SH and its fear/volume inputs are
explicitly specified.  No database writes or live-strategy mutations occur.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from src.app.api import soxl_fear_backtest as engine  # noqa: E402
from src.core.services import factor_backtest_engine as factor_engine  # noqa: E402


HK_FEAR_SOURCE = "hk_innovation_drug_clone"
HK_INDEX_VOLUME_SYMBOL = "931250.CSI"


@lru_cache(maxsize=None)
def _bars(analytics_path: str, symbol: str, start: date, end: date) -> pd.DataFrame:
    symbol = symbol.upper()
    db = duckdb.connect(analytics_path, read_only=True)
    try:
        if symbol == HK_INDEX_VOLUME_SYMBOL:
            sql, params = """
                SELECT trade_date AS date, open, high, low, close, vol AS volume, 0.0 AS turnover
                FROM hk_index_daily WHERE ts_code='931250' AND trade_date BETWEEN ? AND ?
            """, [start, end]
        elif symbol.endswith(".US"):
            sql, params = """
                SELECT trade_date AS date, open, high, low, close, volume, turnover
                FROM us_stock_daily WHERE upper(symbol)=? AND trade_date BETWEEN ? AND ?
            """, [symbol, start, end]
        else:
            sql, params = """
                SELECT trade_date AS date, open, high, low, close, vol AS volume, amount AS turnover
                FROM a_stock_fund_daily_qfq WHERE upper(ts_code)=? AND trade_date BETWEEN ? AND ?
            """, [symbol, start, end]
        frame = db.execute(sql, params).fetchdf()
    finally:
        db.close()
    if frame.empty:
        raise ValueError(f"No local bars for {symbol}")
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    for column in ("open", "high", "low", "close", "volume", "turnover"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


@lru_cache(maxsize=None)
def _fear(sqlite_path: str, symbol: str, start: date, end: date) -> pd.DataFrame:
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as db:
        rows = db.execute(
            """SELECT date, score FROM etf_fear_greed_clone_history
               WHERE upper(symbol)=? AND date BETWEEN ? AND ? ORDER BY date""",
            [symbol.upper(), start.isoformat(), end.isoformat()],
        ).fetchall()
    frame = pd.DataFrame(rows, columns=["date", "fear_greed"])
    if frame.empty:
        raise ValueError(f"No fear history for {symbol}")
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["fear_greed"] = pd.to_numeric(frame["fear_greed"], errors="coerce")
    return frame.dropna().sort_values("date")


def _patch_local_loaders(sqlite_path: str, analytics_path: str) -> None:
    """Avoid scanning production raw tables; all data remains local and read-only."""
    engine._fetch_local_price_history = lambda symbol, start, end: _bars(analytics_path, symbol, start, end)
    engine._fetch_price_history = lambda symbol, start, end: _bars(analytics_path, symbol, start, end)
    engine._fetch_signal_price_history = lambda symbol, start, end: _bars(analytics_path, symbol, start, end)
    factor_engine._existing_a_stock_fund_symbols = lambda symbols, connection=None: set()
    original_fetch_fear = engine._fetch_fear_history

    @lru_cache(maxsize=None)
    def fetch_fear(source: str, start: date, end: date):
        if source != HK_FEAR_SOURCE:
            return original_fetch_fear(source, start, end)
        frame = _fear(sqlite_path, "931250.CSI", start, end)
        return frame, {
            "fear_source": source, "fear_source_label": "中证香港创新药自算贪恐",
            "fear_points": len(frame), "fear_data_source": "local_history",
        }

    engine._fetch_fear_history = fetch_fear
    original_fetch_valuations = engine._fetch_valuation_positions

    @lru_cache(maxsize=None)
    def fetch_valuations(source: str, end: date, valuation_signal_symbol: str | None = None):
        return original_fetch_valuations(source, end, valuation_signal_symbol)

    engine._fetch_valuation_positions = fetch_valuations
    engine.FEAR_SOURCE_OPTIONS[HK_FEAR_SOURCE] = {
        "label": "中证香港创新药自算贪恐", "column": "etf_fear_greed", "symbol": None, "market": "hk",
    }


def _config(sqlite_path: str, config_id: int) -> dict:
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM a_stock_fear_strategy_configs WHERE id=?", [config_id]).fetchone()
    if not row:
        raise ValueError(f"Live config {config_id} not found")
    return dict(row)


def _params(config: dict, **overrides):
    names = engine.SOXLFearStrategyParams.__fields__.keys()
    values = {name: config.get(name) for name in names if config.get(name) is not None}
    values.update(overrides)
    values["execute_next_open"] = True  # live 09:30 execution uses the prior close's signal
    return engine.SOXLFearStrategyParams(**values)


def _run(config: dict, start: date, end: date, *, sub3_symbol: str, sub3_fear: str,
         sub3_volume: str | None, sub3_buy: float | None = None, sub3_vr: float | None = None) -> dict:
    params = _params(
        config,
        sub3_symbol=sub3_symbol, sub3_fear_source=sub3_fear, sub3_volume_signal_symbol=sub3_volume,
        sub3_buy_threshold=sub3_buy if sub3_buy is not None else config["sub3_buy_threshold"],
        sub3_volume_ratio_threshold=sub3_vr if sub3_vr is not None else config["sub3_volume_ratio_threshold"],
    )
    base, _, _, sub, _, sub2, _, sub3, _ = engine._prepare_search_dataframes(
        config["symbol"], start, end, [config["fear_source"]], config["volume_signal_symbol"],
        config["sub_symbol"], config["sub_fear_source"], config["sub_volume_signal_symbol"],
        config["sub2_symbol"], config["sub2_fear_source"], config["sub2_volume_signal_symbol"],
        sub3_symbol, sub3_fear, sub3_volume,
    )
    result = engine._run_seesaw_backtest(base[config["fear_source"]], sub, params, 1_000_000.0,
                                         sub2_base_df=sub2, sub3_base_df=sub3)
    return {key: result.get(key) for key in (
        "total_return", "annualized_return", "max_drawdown", "sharpe_ratio",
        "trade_count", "win_rate", "final_value", "execution_price_type",
    )}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--analytics", required=True)
    parser.add_argument("--config-id", type=int, default=1)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "research/output/a_stock_fear_hk_innovation_drug_backtest.json")
    args = parser.parse_args()
    config = _config(args.sqlite, args.config_id)
    _patch_local_loaders(args.sqlite, args.analytics)
    # Common sample begins when the proposed HK fear series starts, with 90-day feature warm-up.
    start, end = date(2023, 3, 24), date.today()
    baseline = _run(config, start, end, sub3_symbol=config["sub3_symbol"], sub3_fear=config["sub3_fear_source"], sub3_volume=config["sub3_volume_signal_symbol"])
    candidates = []
    if args.baseline_only:
        payload = {"period": [start.isoformat(), end.isoformat()], "live_config": config,
                   "baseline": baseline, "candidates": candidates}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    for volume_source in (HK_INDEX_VOLUME_SYMBOL, "513128.SH"):
        try:
            base = _run(config, start, end, sub3_symbol="513120.SH", sub3_fear=HK_FEAR_SOURCE, sub3_volume=volume_source)
            candidates.append({"volume_source": volume_source, "baseline_thresholds": base})
            for fear_threshold in (10, 15, 20, 25, 30):
                for volume_threshold in (0.8, 1.0, 1.2, 1.3, 1.4, 1.6, 1.8):
                    result = _run(config, start, end, sub3_symbol="513120.SH", sub3_fear=HK_FEAR_SOURCE,
                                  sub3_volume=volume_source, sub3_buy=fear_threshold, sub3_vr=volume_threshold)
                    candidates.append({"volume_source": volume_source, "sub3_buy_threshold": fear_threshold,
                                       "sub3_volume_ratio_threshold": volume_threshold, **result})
        except Exception as exc:
            candidates.append({"volume_source": volume_source, "error": str(exc)})
    payload = {"period": [start.isoformat(), end.isoformat()], "live_config": config,
               "baseline": baseline, "candidates": candidates}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
