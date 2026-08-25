"""跨 A 股指数验证基金份额候选第八因子。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
PROD_SQLITE = Path("/home/quantd/quant_prod/quant_robot/evc_stocks.db")
PROD_DUCKDB = Path("/home/quantd/quant_prod/quant_robot/analytics.duckdb")
INDICATOR_DAILY = Path("lab/output/fund_share_price_study/indicator_daily.csv")
OUTPUT_DIR = Path("lab/output/fund_share_cross_index_study")
COMPONENT_COLUMNS = [
    "market_momentum_score", "stock_price_strength_score", "stock_price_breadth_score",
    "put_call_options_score", "market_volatility_score", "safe_haven_demand_score",
    "junk_bond_demand_score",
]


def mappings() -> list[dict[str, str]]:
    from backend.src.robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS

    seen = set()
    result = []
    for target in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        index_symbol = str(target.get("symbol") or "").upper()
        etf_symbol = str(target.get("proxy_etf") or "").upper()
        key = (index_symbol, etf_symbol)
        if not index_symbol or not etf_symbol or key in seen:
            continue
        seen.add(key)
        result.append({
            "index_symbol": index_symbol,
            "etf_symbol": etf_symbol,
            "label": str(target.get("ticker") or target.get("label") or index_symbol),
        })
    return result


def create_working_db() -> Path:
    handle, path_text = tempfile.mkstemp(prefix="fund_share_cross_index_", suffix=".db")
    os.close(handle)
    path = Path(path_text)
    os.environ["QUANT_SQLITE_PATH"] = str(path)
    os.environ["ANALYTICS_DB_PATH"] = str(PROD_DUCKDB)
    return path


def load_all_data(targets: list[dict[str, str]]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    etfs = sorted({item["etf_symbol"] for item in targets})
    indexes = sorted({item["index_symbol"] for item in targets})
    connection = duckdb.connect(str(PROD_DUCKDB), read_only=True)
    try:
        bars = connection.execute(
            """
            SELECT ts_code AS etf_symbol, trade_date AS date, open, high, low, close, vol AS volume
            FROM a_stock_fund_daily_qfq
            WHERE ts_code IN (SELECT UNNEST(?)) AND trade_date >= '2020-01-01'
            ORDER BY ts_code, trade_date
            """,
            [etfs],
        ).fetchdf()
    finally:
        connection.close()
    db = sqlite3.connect(f"file:{PROD_SQLITE}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in indexes)
        fields = ",".join(COMPONENT_COLUMNS)
        fear = pd.read_sql_query(
            f"SELECT symbol AS index_symbol, date, score, {fields} "
            f"FROM etf_fear_greed_clone_history WHERE symbol IN ({placeholders}) ORDER BY symbol,date",
            db, params=indexes, parse_dates=["date"],
        )
    finally:
        db.close()
    indicators = pd.read_csv(INDICATOR_DAILY, parse_dates=["trade_date"])
    indicators = indicators.sort_values(["ts_code", "trade_date"])
    indicators["available_date"] = indicators.groupby("ts_code")["trade_date"].shift(-1)
    return bars, fear, indicators


def make_base(
    target: dict[str, str], bars_all: pd.DataFrame, fear_all: pd.DataFrame, indicators: pd.DataFrame,
) -> pd.DataFrame:
    bars = bars_all[bars_all["etf_symbol"] == target["etf_symbol"]].copy().sort_values("date")
    if bars.empty:
        return bars
    bars["date"] = pd.to_datetime(bars["date"])
    for column in ["open", "high", "low", "close", "volume"]:
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    bars["ma20"] = bars["close"].rolling(20).mean()
    log_volume = np.log(bars["volume"].replace(0, np.nan))
    prior_mean = log_volume.shift(1).rolling(20).mean()
    prior_std = log_volume.shift(1).rolling(20).std(ddof=0).replace(0, np.nan)
    bars["log_z"] = (log_volume - prior_mean) / prior_std
    bars["log_z_self"] = bars["log_z"]
    bars["signal_volume"] = bars["volume"]
    bars["volume_ma20"] = bars["volume"].shift(1).rolling(20).mean()
    bars["volume_ratio"] = bars["volume"] / bars["volume_ma20"]
    bars["volume_ma20_excluding_recent_1"] = bars["volume_ma20"]
    bars["volume_ratio_consecutive_1"] = bars["volume_ratio"]
    bars["signal_date"] = bars["date"]
    bars["fear_date"] = bars["date"]
    bars["execution_price"] = bars["close"]

    fear = fear_all[fear_all["index_symbol"] == target["index_symbol"]].copy()
    fear["component_count"] = fear[COMPONENT_COLUMNS].notna().sum(axis=1)
    factor_columns = ["share_level_score", "share_change_5d_score", "share_change_20d_score"]
    indicator = indicators[indicators["ts_code"] == target["etf_symbol"]][
        ["available_date", *factor_columns]
    ]
    base = bars.merge(
        fear[["date", "score", "component_count", *COMPONENT_COLUMNS]],
        on="date", how="inner",
    )
    base = base.merge(indicator, left_on="date", right_on="available_date", how="left")
    base = base.dropna(subset=[
        "score", "ma20", "volume_ma20", "volume_ratio", "execution_price", *factor_columns,
    ]).reset_index(drop=True)
    base["fear_greed"] = base["score"]
    base["cnn_fear_greed"] = base["score"]
    count = base["component_count"].fillna(7).clip(lower=1)
    factor_map = {
        "level": "share_level_score",
        "change5": "share_change_5d_score",
        "change20": "share_change_20d_score",
    }
    for factor_name, factor_column in factor_map.items():
        factor_raw = base[factor_column]
        for direction, factor in [("direct", factor_raw), ("inverse", 100 - factor_raw)]:
            for weight in (0.5, 1.0, 2.0):
                variant = f"{factor_name}_{direction}_w{weight:g}"
                base[f"fear_greed__{variant}"] = (
                    base["score"] * count + factor * weight
                ) / (count + weight)
    return base


def params_for_modes(buy_mode: str, sell_mode: str):
    from backend.src.app.api.soxl_fear_backtest import SOXLFearStrategyParams

    return SOXLFearStrategyParams(
        buy_threshold=30, greed_threshold=70, volume_ratio_threshold=1.6,
        volume_ratio_consecutive_days=1, volume_z_threshold=None, sell_shrink_z=-1,
        buy_position_pct=100, cooldown_days=0, trailing_stop_pct=0,
        sell_position_pct=100, sell_reduction_basis="holdings", sell_price_above_avg_cost=False,
        max_take_profit_sells_per_cycle=2, min_position_pct_after_take_profit=0,
        execute_next_open=True, slippage_pct=-1, stamp_duty_pct=0,
        buy_turn_signal_mode=buy_mode, sell_turn_signal_mode=sell_mode,
    )


def main() -> None:
    working_db = create_working_db()
    try:
        from backend.src.app.api.soxl_fear_backtest import _run_backtest

        targets = mappings()
        bars, fear, indicators = load_all_data(targets)
        rows = []
        coverage = []
        correlation_rows = []
        modes = [("legacy", "legacy"), ("any", "any"), ("volume", "volume")]
        variants = [("original", "fear_greed")]
        for factor_name in ("level", "change5", "change20"):
            for direction in ("direct", "inverse"):
                for weight in (0.5, 1.0, 2.0):
                    variant = f"{factor_name}_{direction}_w{weight:g}"
                    variants.append((variant, f"fear_greed__{variant}"))
        periods = {"full": (None, None), "test_2025_2026": ("2025-01-01", "2026-08-19")}
        for target in targets:
            base = make_base(target, bars, fear, indicators)
            if base.empty or len(base) < 120:
                coverage.append({**target, "observations": len(base), "status": "insufficient"})
                continue
            coverage.append({
                **target, "observations": len(base), "start_date": base["date"].min().date(),
                "end_date": base["date"].max().date(), "status": "used",
            })
            for factor_name, factor_column in [
                ("share_level", "share_level_score"),
                ("share_change_5d", "share_change_5d_score"),
                ("share_change_20d", "share_change_20d_score"),
            ]:
                row = {
                    **target,
                    "factor": factor_name,
                    "observations": len(base),
                    "vs_fear_score_pearson": base[factor_column].corr(base["score"], method="pearson"),
                    "vs_fear_score_spearman": base[factor_column].corr(base["score"], method="spearman"),
                }
                for component in COMPONENT_COLUMNS:
                    row[f"vs_{component}_spearman"] = base[factor_column].corr(
                        base[component], method="spearman"
                    )
                correlation_rows.append(row)
            for period, (start, end) in periods.items():
                active = base.copy()
                if start:
                    active = active[(active["date"] >= start) & (active["date"] <= end)].reset_index(drop=True)
                if len(active) < 100:
                    continue
                for buy_mode, sell_mode in modes:
                    for variant, score_column in variants:
                        frame = active.copy()
                        frame["fear_greed"] = frame[score_column]
                        frame["cnn_fear_greed"] = frame[score_column]
                        result = _run_backtest(frame, params_for_modes(buy_mode, sell_mode), 1_000_000, False)
                        rows.append({
                            **target, "period": period, "variant": variant,
                            "buy_mode": buy_mode, "sell_mode": sell_mode,
                            "observations": len(frame), "total_return_pct": result["total_return"],
                            "annualized_return_pct": result["annualized_return"],
                            "max_drawdown_pct": result["max_drawdown"], "sharpe_ratio": result["sharpe_ratio"],
                            "sortino_ratio": result["sortino_ratio"], "calmar_ratio": result["calmar_ratio"],
                            "buy_count": result["buy_count"], "sell_count": result["sell_count"],
                        })
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        result_frame = pd.DataFrame(rows)
        result_frame.to_csv(OUTPUT_DIR / "cross_index_backtests.csv", index=False)
        pd.DataFrame(coverage).to_csv(OUTPUT_DIR / "coverage.csv", index=False)
        correlation_frame = pd.DataFrame(correlation_rows)
        correlation_frame.to_csv(OUTPUT_DIR / "factor_correlations.csv", index=False)
        correlation_summary = correlation_frame.groupby("factor").agg(
            index_count=("index_symbol", "count"),
            median_fear_pearson=("vs_fear_score_pearson", "median"),
            mean_fear_pearson=("vs_fear_score_pearson", "mean"),
            median_fear_spearman=("vs_fear_score_spearman", "median"),
            mean_fear_spearman=("vs_fear_score_spearman", "mean"),
            positive_spearman_rate=("vs_fear_score_spearman", lambda values: float((values > 0).mean() * 100)),
            min_fear_spearman=("vs_fear_score_spearman", "min"),
            max_fear_spearman=("vs_fear_score_spearman", "max"),
        ).reset_index()
        correlation_summary.to_csv(OUTPUT_DIR / "factor_correlation_summary.csv", index=False)

        baseline = result_frame[result_frame["variant"] == "original"].set_index(
            ["index_symbol", "etf_symbol", "period", "buy_mode", "sell_mode"]
        )
        candidates = result_frame[result_frame["variant"] != "original"].copy()
        candidates = candidates.join(
            baseline[["total_return_pct", "max_drawdown_pct", "sharpe_ratio"]],
            on=["index_symbol", "etf_symbol", "period", "buy_mode", "sell_mode"],
            rsuffix="_original",
        )
        candidates["delta_total_return_pct"] = candidates["total_return_pct"] - candidates["total_return_pct_original"]
        candidates["delta_sharpe_ratio"] = candidates["sharpe_ratio"] - candidates["sharpe_ratio_original"]
        candidates["delta_max_drawdown_pct"] = candidates["max_drawdown_pct"] - candidates["max_drawdown_pct_original"]
        variant_summary = candidates.groupby(["period", "buy_mode", "sell_mode", "variant"]).agg(
            index_count=("index_symbol", "count"),
            return_win_rate=("delta_total_return_pct", lambda values: float((values > 0).mean() * 100)),
            sharpe_win_rate=("delta_sharpe_ratio", lambda values: float((values > 0).mean() * 100)),
            median_return_delta=("delta_total_return_pct", "median"),
            median_sharpe_delta=("delta_sharpe_ratio", "median"),
            median_drawdown_delta=("delta_max_drawdown_pct", "median"),
        ).reset_index()
        variant_summary.to_csv(OUTPUT_DIR / "variant_summary.csv", index=False)

        keys = ["index_symbol", "etf_symbol", "period", "buy_mode", "sell_mode"]
        selected = result_frame[result_frame["variant"].isin(["original", "change20_direct_w0.5"])]
        comparison = selected.pivot(index=keys, columns="variant", values=[
            "total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sharpe_ratio",
            "sortino_ratio", "calmar_ratio", "buy_count", "sell_count",
        ]).reset_index()
        comparison.columns = ["__".join(value).strip("_") if isinstance(value, tuple) else value for value in comparison.columns]
        for metric in ["total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sharpe_ratio", "sortino_ratio", "calmar_ratio"]:
            comparison[f"delta_{metric}"] = (
                comparison[f"{metric}__change20_direct_w0.5"] - comparison[f"{metric}__original"]
            )
        comparison.to_csv(OUTPUT_DIR / "cross_index_comparison.csv", index=False)
        summary = comparison.groupby(["period", "buy_mode", "sell_mode"]).agg(
            index_count=("index_symbol", "count"),
            return_win_rate=("delta_total_return_pct", lambda values: float((values > 0).mean() * 100)),
            sharpe_win_rate=("delta_sharpe_ratio", lambda values: float((values > 0).mean() * 100)),
            drawdown_improve_rate=("delta_max_drawdown_pct", lambda values: float((values < 0).mean() * 100)),
            median_return_delta=("delta_total_return_pct", "median"),
            median_sharpe_delta=("delta_sharpe_ratio", "median"),
            median_drawdown_delta=("delta_max_drawdown_pct", "median"),
        ).reset_index()
        summary.to_csv(OUTPUT_DIR / "summary.csv", index=False)
        print(summary.to_string(index=False))
    finally:
        working_db.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
