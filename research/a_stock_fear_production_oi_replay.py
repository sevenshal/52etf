#!/usr/bin/env python3
"""Replay the live A-share fear strategy against the production score history.

This is deliberately a *historical counterfactual*: it applies the currently
enabled live configuration to all available historical production data.  The
volume-P/C leg uses the score persisted by the live calculator.  The OI-P/C
leg uses the same persisted total after replacing its one equal-weighted
component, so every non-option component is byte-for-byte the live score.

Run read-only, for example:
  QUANT_DB_READ_ONLY=true QUANT_SQLITE_PATH=/home/quantd/quant_prod/quant_robot/evc_stocks.db \\
  ANALYTICS_DB_PATH=/home/quantd/quant_prod/quant_robot/analytics.duckdb \\
  .venv/bin/python research/a_stock_fear_production_oi_replay.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Install the import-time read-only guards before loading the production API.
from lab import seesaw_nine_turn_backtest as guarded  # noqa: E402

fb = guarded.fb

# 000688's OI-PCR starts on this date.  Both variants use this same common
# window so a result is not driven by a different sample.
START = date.fromisoformat(os.environ.get("OI_REPLAY_START", "2023-12-01"))
END = date.fromisoformat(os.environ.get("OI_REPLAY_END", date.today().isoformat()))
DB_PATH = os.environ.get("QUANT_SQLITE_PATH", "/home/quantd/quant_prod/quant_robot/evc_stocks.db")
OUT = ROOT / "research/output/a_stock_fear_production_oi_replay.json"
INITIAL_CAPITAL = 1_000_000.0


def install_local_market_calendars(end: date) -> None:
    """Avoid network calendar lookups while retaining the production sessions."""
    import duckdb
    from backend.src.core.services import index_valuation

    analytics_path = os.environ["ANALYTICS_DB_PATH"]
    conn = duckdb.connect(analytics_path, read_only=True)
    try:
        china_days = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT trade_date FROM a_stock_index_daily WHERE trade_date BETWEEN ? AND ?",
                [(START - pd.Timedelta(days=10)).isoformat(), end.isoformat()],
            ).fetchall()
        }
        us_days = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT trade_date FROM us_stock_daily WHERE trade_date BETWEEN ? AND ?",
                [(START - pd.Timedelta(days=10)).isoformat(), end.isoformat()],
            ).fetchall()
        }
    finally:
        conn.close()
    index_valuation._is_china_trading_day = lambda value: value in china_days
    index_valuation._is_us_trading_day = lambda value: value in us_days


@dataclass
class Leg:
    key: str
    symbol: str
    fear_source: str
    volume_symbol: str | None
    history_symbol: str | None


LEGS = (
    Leg("main", "510880.SH", "a_stock_000015_sh", None, "000015.SH"),
    Leg("sub", "512480.SH", "a_stock_000688_sh", "588000.SH", "000688.SH"),
    Leg("sub2", "159509.SZ", "qqq_clone", "QQQ.US", None),
    Leg("sub3", "516510.SH", "a_stock_930851_csi", None, "930851.CSI"),
)


def live_config() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM a_stock_fear_strategy_configs WHERE enabled = 1 ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError("production database has no enabled A-share fear strategy config")
    return dict(row)


def params_from_config(config: dict[str, Any]) -> Any:
    names = set(fb.SOXLFearStrategyParams.model_fields)
    values = {key: value for key, value in config.items() if key in names}
    # The live executor is a 09:30 next-session execution.  The model stores
    # this in run_time rather than an explicit field.
    values.update(execute_next_open=True, slippage_pct=-1.0, stamp_duty_pct=0.0)
    return fb.SOXLFearStrategyParams(**values)


def score_history(symbol: str) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        result = pd.read_sql_query(
            """
            SELECT date, score, put_call_options_score, components
            FROM etf_fear_greed_clone_history
            WHERE symbol = ? AND date >= ?
            ORDER BY date
            """,
            conn,
            params=(symbol, START.isoformat()),
        )
    finally:
        conn.close()
    result["date"] = pd.to_datetime(result["date"]).dt.date
    result["oi_score"] = result["components"].map(
        lambda value: (json.loads(value).get("put_call_oi_options") or {}).get("score") if value else None
    )
    return result.drop(columns="components")


def prepare_frames() -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, float]]]:
    frames: dict[str, pd.DataFrame] = {}
    checks: dict[str, dict[str, float]] = {}
    for leg in LEGS:
        frame, _ = fb._prepare_base_dataframe(leg.symbol, START, END, leg.fear_source, leg.volume_symbol)
        if leg.history_symbol is None:
            frames[leg.key] = frame
            continue
        history = score_history(leg.history_symbol)
        attrs = dict(frame.attrs)
        merged = frame.merge(history, on="date", how="left", suffixes=("", "_production"))
        merged.attrs.update(attrs)
        comparable = merged["score"].notna() & merged["fear_greed"].notna()
        delta = (merged.loc[comparable, "fear_greed"] - merged.loc[comparable, "score"]).abs()
        checks[leg.key] = {
            "overlapping_days": int(comparable.sum()),
            "max_abs_score_delta": float(delta.max()) if len(delta) else float("nan"),
            "mean_abs_score_delta": float(delta.mean()) if len(delta) else float("nan"),
            "oi_available_days": int(merged.loc[comparable, "oi_score"].notna().sum()),
        }
        if not comparable.any():
            raise RuntimeError(f"no production score overlap for {leg.key}")
        # For volume P/C, production's persisted score is the controlling series.
        merged["fear_greed"] = merged["score"].where(merged["score"].notna(), merged["fear_greed"])
        frames[leg.key] = merged.drop(columns=["score"], errors="ignore")
    return frames, checks


def oi_frames(volume_frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    converted = {key: value.copy() for key, value in volume_frames.items()}
    for leg in LEGS:
        if leg.history_symbol is None:
            continue
        frame = converted[leg.key]
        missing = frame["oi_score"].isna() & frame["date"].ge(START)
        if missing.any():
            raise RuntimeError(f"OI score missing for {leg.key} on {int(missing.sum())} replay rows")
        # Production total has six equally weighted included components.
        frame["fear_greed"] = frame["fear_greed"] + (frame["oi_score"] - frame["put_call_options_score"]) / 6.0
    return converted


def run(frames: dict[str, pd.DataFrame], params: Any, detailed: bool = False) -> dict:
    return fb._run_seesaw_backtest(
        frames["main"], frames["sub"], params, INITIAL_CAPITAL, detailed=detailed,
        sub2_base_df=frames["sub2"], sub3_base_df=frames["sub3"],
    )


def compact(result: dict) -> dict[str, float | int]:
    return {
        key: result.get(key)
        for key in ("total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "calmar_ratio", "trade_count", "buy_count", "sell_count", "idle_days")
    }


def ending_position(result: dict) -> dict[str, Any]:
    """Return the last transaction; a final BUY means the model remains long."""
    trades = result.get("trades") or []
    last = trades[-1] if trades else None
    return {
        "as_of": (result.get("equity_curve") or [{}])[-1].get("date"),
        "last_trade": last,
        "is_holding": bool(last and last.get("action") == "BUY"),
    }


def grid_search(frames: dict[str, pd.DataFrame], live: Any) -> list[dict[str, Any]]:
    """Full-period grid around the live configuration; QQQ-only parameters stay live."""
    candidates = product(
        (15, 20, 25, 30, 35), (1.3, 1.6, 1.9),
        (5, 9, 13, 17), (1.3, 1.6, 1.9),
        (5, 8, 12, 16), (1.3, 1.6, 1.9),
        (65, 70, 75, 80),
    )
    rows: list[dict[str, Any]] = []
    for main_buy, main_vr, sub_buy, sub_vr, sub3_buy, sub3_vr, greed in candidates:
        params = live.copy(update={
            "buy_threshold": main_buy, "volume_ratio_threshold": main_vr,
            "sub_buy_threshold": sub_buy, "sub_volume_ratio_threshold": sub_vr,
            "sub3_buy_threshold": sub3_buy, "sub3_volume_ratio_threshold": sub3_vr,
            "greed_threshold": greed,
        })
        result = run(frames, params)
        if int(result.get("trade_count") or 0) < 12:
            continue
        rows.append({
            "params": {
                "buy_threshold": main_buy, "volume_ratio_threshold": main_vr,
                "sub_buy_threshold": sub_buy, "sub_volume_ratio_threshold": sub_vr,
                "sub3_buy_threshold": sub3_buy, "sub3_volume_ratio_threshold": sub3_vr,
                "greed_threshold": greed,
            },
            **compact(result),
        })
    return sorted(rows, key=lambda item: (item["annualized_return"], -item["max_drawdown"]), reverse=True)


def main() -> None:
    config = live_config()
    params = params_from_config(config)
    install_local_market_calendars(END)
    frames, checks = prepare_frames()
    volume_result = run(frames, params, detailed=True)
    volume_only = os.environ.get("OI_VOLUME_ONLY") == "1"
    oi = None if volume_only else oi_frames(frames)
    oi_result = None if oi is None else run(oi, params, detailed=True)
    best_oi_params = None if oi is None else params.model_copy(update={
        "buy_threshold": 30, "volume_ratio_threshold": 1.9,
        "sub_buy_threshold": 9, "sub_volume_ratio_threshold": 1.6,
        "sub3_buy_threshold": 8, "sub3_volume_ratio_threshold": 1.6,
        "greed_threshold": 75,
    })
    best_oi_result = None if best_oi_params is None else run(oi, best_oi_params, detailed=True)
    search = grid_search(oi, params) if oi is not None and os.environ.get("OI_SKIP_SEARCH") != "1" else []
    output = {
        "as_of": date.today().isoformat(), "start": START.isoformat(),
        "live_config_id": config["id"], "live_params": params.dict(),
        "production_score_checks": checks,
        "volume_pcr_live_params": compact(volume_result),
        "oi_pcr_live_params": compact(oi_result) if oi_result is not None else None,
        "oi_pcr_best_params": best_oi_params.model_dump() if best_oi_params is not None else None,
        "oi_pcr_best_params_result": compact(best_oi_result) if best_oi_result is not None else None,
        "oi_pcr_grid": {
            "searched": 0 if os.environ.get("OI_SKIP_SEARCH") == "1" else 5 * 3 * 4 * 3 * 4 * 3 * 4,
            "eligible": len(search), "top_20": search[:20],
        },
        "volume_trades": volume_result.get("trades", []),
        "oi_trades": oi_result.get("trades", []) if oi_result is not None else [],
        "oi_best_trades": best_oi_result.get("trades", []) if best_oi_result is not None else [],
        "ending_positions": {
            "volume": ending_position(volume_result),
            "oi_live": ending_position(oi_result) if oi_result is not None else None,
            "oi_best": ending_position(best_oi_result) if best_oi_result is not None else None,
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({key: output[key] for key in (
        "start", "live_config_id", "production_score_checks", "volume_pcr_live_params", "oi_pcr_live_params", "oi_pcr_grid"
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
