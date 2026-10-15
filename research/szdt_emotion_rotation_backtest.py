#!/usr/bin/env python3
"""Out-of-sample check for replacing the A-share rotation fear sources with SZDT.

The production state machine, prices, volume signals, valuation gates, next-open
execution, and pessimistic high/low fills are reused unchanged.  Only the
``fear_greed`` signal is replaced with SZDT ETF emotion history:

* 510880.SH <- SH.510880 (A股红利, exact)
* 512480.SH <- SH.512480 (半导体, exact)
* 159509.SZ <- SZ.159509 (纳指科技, exact)
* 516510.SH <- SH.515230 (软件) or SZ.159819 (人工智能), both disclosed proxies

SZDT reports scores on [-100, 100].  The engine expects [0, 100], so this
experiment uses ``(raw_score + 100) / 2``.  The reported thresholds are always
converted back to the native SZDT scale.

Run with QUANT_DB_READ_ONLY=true and production DB paths supplied through the
environment.  Remote SZDT requests use the project's existing API client; the
only writes are its ordinary local response cache and this script's research
outputs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
for item in (ROOT, BACKEND):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from src.app.api import soxl_fear_backtest as fb  # noqa: E402
from src.core.services.szdt import SZDTService  # noqa: E402


INITIAL_CAPITAL = 1_000_000.0
START = date(2025, 2, 7)  # First common date when the software proxy exists.
TRAIN_END = date(2025, 9, 30)
TEST_START = date(2025, 10, 1)
END = date(2026, 9, 30)

SYMBOL_TO_SZDT = {
    "510880.SH": "SH.510880",
    "512480.SH": "SH.512480",
    "159509.SZ": "SZ.159509",
}
CLOUD_PROXIES = {
    "software_proxy": "SH.515230",
    "ai_proxy": "SZ.159819",
}


def production_params() -> Any:
    """The user's production parameters, excluding the thresholds being searched."""
    return fb.SOXLFearStrategyParams(
        buy_threshold=35,
        greed_threshold=70,
        volume_ratio_threshold=1.6,
        volume_ratio_consecutive_days=1,
        volume_z_threshold=None,
        sell_shrink_z=-1,
        buy_position_pct=100,
        cooldown_days=0,
        trailing_stop_pct=0,
        sell_position_pct=100,
        sell_reduction_basis="holdings",
        sell_price_above_avg_cost=False,
        max_take_profit_sells_per_cycle=2,
        min_position_pct_after_take_profit=0,
        rebalance_threshold_pct=0,
        execute_next_open=True,
        slippage_pct=-1,
        stamp_duty_pct=0,
        buy_turn_signal_mode="legacy",
        sell_turn_signal_mode="legacy",
        sub_symbol="512480.SH",
        sub_fear_source="a_stock_000688_sh",
        sub_volume_signal_symbol="588000.SH",
        sub_buy_threshold=25,
        sub_volume_ratio_threshold=1.6,
        sub2_symbol="159509.SZ",
        sub2_fear_source="qqq_clone",
        sub2_volume_signal_symbol="QQQ.US",
        sub2_buy_threshold=25,
        sub2_volume_ratio_threshold=1.4,
        sub3_symbol="516510.SH",
        sub3_fear_source="a_stock_930851_csi",
        sub3_volume_signal_symbol=None,
        sub3_buy_threshold=20,
        sub3_volume_ratio_threshold=1.3,
        sell_ma5_confirm="non_main",
        swap_threshold=45,
        valuation_window=252,
        valuation_buy_max=None,
        valuation_sell_min=80,
        valuation_force_sell_greed=90,
    )


def install_local_market_calendars(analytics_db: str, start: date, end: date) -> None:
    """Resolve valuation dates only from the stored local market calendar."""
    import duckdb
    from src.core.services import index_valuation

    connection = duckdb.connect(analytics_db, read_only=True)
    try:
        china_days = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT trade_date FROM a_stock_index_daily WHERE trade_date BETWEEN ? AND ?",
                [(start - pd.Timedelta(days=10)).isoformat(), end.isoformat()],
            ).fetchall()
        }
        us_days = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT trade_date FROM us_stock_daily WHERE trade_date BETWEEN ? AND ?",
                [(start - pd.Timedelta(days=10)).isoformat(), end.isoformat()],
            ).fetchall()
        }
    finally:
        connection.close()
    index_valuation._is_china_trading_day = lambda check_date: check_date in china_days
    index_valuation._is_us_trading_day = lambda check_date: check_date in us_days


async def fetch_histories(codes: list[str]) -> dict[str, pd.DataFrame]:
    service = SZDTService()
    result: dict[str, pd.DataFrame] = {}
    for code in codes:
        response = await service.get_etf_emotion_history(code)
        rows = (response or {}).get("data") or []
        frame = pd.DataFrame(rows)
        if frame.empty or not {"date", "score"}.issubset(frame.columns):
            raise RuntimeError(f"SZDT history unavailable or malformed for {code}")
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
        frame = frame.dropna(subset=["date", "score"]).drop_duplicates("date", keep="last")
        result[code] = frame.sort_values("date").reset_index(drop=True)
    return result


def prepare_original_frames(start: date, end: date, analytics_db: str) -> dict[str, pd.DataFrame]:
    install_local_market_calendars(analytics_db, start, end)
    specs = {
        "510880.SH": ("a_stock_000015_sh", None),
        "512480.SH": ("a_stock_000688_sh", "588000.SH"),
        "159509.SZ": ("qqq_clone", "QQQ.US"),
        "516510.SH": ("a_stock_930851_csi", None),
    }
    frames: dict[str, pd.DataFrame] = {}
    for symbol, (fear_source, volume_source) in specs.items():
        frame, _ = fb._prepare_base_dataframe(symbol, start, end, fear_source, volume_source)
        frames[symbol] = frame
    return frames


def overlay_szdt(frame: pd.DataFrame, history: pd.DataFrame, source_code: str) -> tuple[pd.DataFrame, dict]:
    """Replace fear signal by exact-date SZDT observations without forward filling."""
    replacement = history[["date", "score"]].rename(columns={"score": "szdt_raw_score"})
    merged = frame.merge(replacement, on="date", how="left", validate="one_to_one")
    missing = int(merged["szdt_raw_score"].isna().sum())
    if missing:
        dates = merged.loc[merged["szdt_raw_score"].isna(), "date"]
        raise ValueError(f"{source_code} is missing {missing} strategy sessions ({dates.min()}..{dates.max()})")
    merged["fear_greed"] = (merged["szdt_raw_score"].astype(float) + 100.0) / 2.0
    merged.attrs = frame.attrs.copy()
    merged.attrs["fear_source_label"] = f"SZDT {source_code}"
    return merged, {
        "source": source_code,
        "sessions": int(len(merged)),
        "first": str(history["date"].min()),
        "last": str(history["date"].max()),
        "raw_min": float(history["score"].min()),
        "raw_max": float(history["score"].max()),
        "missing_strategy_sessions": missing,
    }


def run(frames: dict[str, pd.DataFrame], params: Any, *, detailed: bool = False) -> dict:
    return fb._run_seesaw_backtest(
        frames["510880.SH"], frames["512480.SH"], params, INITIAL_CAPITAL,
        detailed=detailed,
        sub2_base_df=frames["159509.SZ"],
        sub3_base_df=frames["516510.SH"],
    )


def subset(frames: dict[str, pd.DataFrame], start: date, end: date) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for key, frame in frames.items():
        item = frame.loc[(frame["date"] >= start) & (frame["date"] <= end)].copy()
        item.attrs = frame.attrs.copy()
        result[key] = item
    return result


def metric_row(name: str, result: dict) -> dict:
    return {
        "strategy": name,
        "total_return_pct": float(result["total_return"]),
        "annualized_return_pct": float(result["annualized_return"]),
        "annualized_volatility_pct": float(result["annualized_volatility"]),
        "max_drawdown_pct": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe_ratio"]),
        "sortino": float(result["sortino_ratio"]),
        "calmar": float(result["calmar_ratio"]),
        "trade_count": int(result["trade_count"]),
        "buy_count": int(result["buy_count"]),
        "sell_count": int(result["sell_count"]),
        "idle_days": int(result["idle_days"]),
        "total_days": int(result["total_trading_days"]),
    }


def native(raw_engine_score: float) -> float:
    return raw_engine_score * 2.0 - 100.0


@dataclass(frozen=True)
class Candidate:
    main_buy: float
    sub_buy: float
    sub2_buy: float
    sub3_buy: float
    greed: float
    swap: float

    def as_updates(self) -> dict[str, float]:
        return {
            "buy_threshold": self.main_buy,
            "sub_buy_threshold": self.sub_buy,
            "sub2_buy_threshold": self.sub2_buy,
            "sub3_buy_threshold": self.sub3_buy,
            "greed_threshold": self.greed,
            "swap_threshold": self.swap,
        }

    def row(self) -> dict[str, float]:
        return {
            "main_buy_szdt_raw_lte": native(self.main_buy),
            "sub_buy_szdt_raw_lte": native(self.sub_buy),
            "sub2_buy_szdt_raw_lte": native(self.sub2_buy),
            "sub3_buy_szdt_raw_lte": native(self.sub3_buy),
            "sell_szdt_raw_gte": native(self.greed),
            "swap_szdt_raw_gt": native(self.swap),
        }


def search(training_frames: dict[str, pd.DataFrame], *, top_k: int = 20) -> list[tuple[Candidate, dict]]:
    # Native SZDT score grids.  The sell and swap thresholds remain common,
    # preserving production state-machine semantics.
    # A deliberately coarse 4×4×4×4×3×3 grid (4,608 combinations across
    # both cloud proxies) keeps the complete state-machine replay auditable
    # while avoiding a high-dimensional in-sample parameter hunt.
    buy_grid = [-90, -60, -30, 0]
    greed_grid = [40, 60, 80]
    swap_grid = [0, 30, 60]
    base = production_params()
    winners: list[tuple[float, Candidate, dict]] = []
    for values in product(buy_grid, buy_grid, buy_grid, buy_grid, greed_grid, swap_grid):
        raw_main, raw_sub, raw_sub2, raw_sub3, raw_greed, raw_swap = values
        candidate = Candidate(*[((v + 100.0) / 2.0) for v in values])
        result = run(training_frames, base.copy(update=candidate.as_updates()))
        # Same primary target as the requested backtest (annualized return).
        # Exclude degenerate one-trade paths before choosing candidates to test.
        if result["buy_count"] < 2:
            continue
        objective = float(result["annualized_return"])
        winners.append((objective, candidate, result))
    winners.sort(key=lambda item: (item[0], -item[2]["max_drawdown"]), reverse=True)
    return [(candidate, result) for _, candidate, result in winners[:top_k]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analytics-db", default=os.getenv("ANALYTICS_DB_PATH", "/home/quantd/quant_prod/quant_robot/analytics.duckdb"))
    parser.add_argument("--output", type=Path, default=ROOT / "research/output/szdt_emotion_rotation_backtest")
    args = parser.parse_args()
    if os.getenv("QUANT_DB_READ_ONLY", "").lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("Set QUANT_DB_READ_ONLY=true before running this research script.")

    args.output.mkdir(parents=True, exist_ok=True)
    histories = asyncio.run(fetch_histories([*SYMBOL_TO_SZDT.values(), *CLOUD_PROXIES.values()]))
    base_frames = prepare_original_frames(START, END, args.analytics_db)

    baseline_full = run(base_frames, production_params(), detailed=True)
    baseline_train = run(subset(base_frames, START, TRAIN_END), production_params())
    baseline_test = run(subset(base_frames, TEST_START, END), production_params())
    rows = [
        {"period": "full_common_window", **metric_row("original_fear_fixed_params", baseline_full)},
        {"period": "train", **metric_row("original_fear_fixed_params", baseline_train)},
        {"period": "test", **metric_row("original_fear_fixed_params", baseline_test)},
    ]
    quality: list[dict] = []
    parameter_rows: list[dict] = []
    detailed_runs: dict[str, dict] = {"original_fear_fixed_params": baseline_full}

    for proxy_name, cloud_code in CLOUD_PROXIES.items():
        frames: dict[str, pd.DataFrame] = {}
        for symbol, szdt_code in SYMBOL_TO_SZDT.items():
            frames[symbol], report = overlay_szdt(base_frames[symbol], histories[szdt_code], szdt_code)
            quality.append({"leg": symbol, "mapping": "exact", **report})
        frames["516510.SH"], report = overlay_szdt(base_frames["516510.SH"], histories[cloud_code], cloud_code)
        quality.append({"leg": "516510.SH", "mapping": proxy_name, **report})

        ranked = search(subset(frames, START, TRAIN_END))
        for rank, (candidate, train_result) in enumerate(ranked, start=1):
            test_result = run(subset(frames, TEST_START, END), production_params().copy(update=candidate.as_updates()))
            parameter_rows.append({
                "proxy": proxy_name, "rank_by_train_ann_return": rank,
                **candidate.row(),
                "train_annualized_return_pct": float(train_result["annualized_return"]),
                "train_max_drawdown_pct": float(train_result["max_drawdown"]),
                "train_sharpe": float(train_result["sharpe_ratio"]),
                "train_buys": int(train_result["buy_count"]),
                "test_annualized_return_pct": float(test_result["annualized_return"]),
                "test_max_drawdown_pct": float(test_result["max_drawdown"]),
                "test_sharpe": float(test_result["sharpe_ratio"]),
                "test_buys": int(test_result["buy_count"]),
            })

        # Strictly lock parameters using the training result only.  The test
        # period is evaluated exactly once and never participates in selection.
        candidate, train_result = ranked[0]
        test_result = run(
            subset(frames, TEST_START, END),
            production_params().copy(update=candidate.as_updates()),
        )
        full_result = run(frames, production_params().copy(update=candidate.as_updates()), detailed=True)
        name = f"SZDT_{proxy_name}_train_selected"
        detailed_runs[name] = full_result
        for period, result in (("full_common_window", full_result), ("train", train_result), ("test", test_result)):
            rows.append({"period": period, **metric_row(name, result), **candidate.row()})

    pd.DataFrame(rows).to_csv(args.output / "comparison.csv", index=False)
    pd.DataFrame(parameter_rows).to_csv(args.output / "train_ranked_shortlists.csv", index=False)
    pd.DataFrame(quality).drop_duplicates().to_csv(args.output / "data_quality.csv", index=False)
    for name, result in detailed_runs.items():
        pd.DataFrame(result["equity_curve"]).to_csv(args.output / f"{name}_equity_curve.csv", index=False)
        pd.DataFrame(result["trades"]).to_csv(args.output / f"{name}_trades.csv", index=False)
    artifact = {
        "window": {"start": str(START), "train_end": str(TRAIN_END), "test_start": str(TEST_START), "end": str(END)},
        "score_transform": "engine_score = (szdt_raw_score + 100) / 2",
        "mappings": {**SYMBOL_TO_SZDT, "516510.SH": CLOUD_PROXIES},
        "search": {"buy_grid_native_szdt": [-90, -60, -30, 0], "greed_grid_native_szdt": [40, 60, 80], "swap_grid_native_szdt": [0, 30, 60], "selection": "choose rank 1 by train annualized return (at least two buys); test period never participates in selection"},
        "notes": ["No forward filling of SZDT scores.", "Valuation gates and all price/volume/execution assumptions are retained from the original strategy.", "516510.SH has no exact SZDT ETF history in the supplied list; software and AI ETFs are explicitly proxy tests."],
    }
    (args.output / "artifact.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"OUTPUT={args.output}")


if __name__ == "__main__":
    main()
