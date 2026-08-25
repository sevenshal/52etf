"""八因子等权对生产 A 股情绪量能策略的影响与参数搜索。"""
from __future__ import annotations

import itertools
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
OUTPUT_DIR = Path("lab/output/eight_factor_live_strategy_study")
COMPONENT_COLUMNS = [
    "market_momentum_score", "stock_price_strength_score", "stock_price_breadth_score",
    "put_call_options_score", "market_volatility_score", "safe_haven_demand_score",
    "junk_bond_demand_score",
]
START = "2023-03-22"
END = "2026-08-19"


def initialize_import_environment() -> Path:
    handle, path_text = tempfile.mkstemp(prefix="eight_factor_live_", suffix=".db")
    os.close(handle)
    path = Path(path_text)
    os.environ["QUANT_SQLITE_PATH"] = str(path)
    os.environ["ANALYTICS_DB_PATH"] = str(PROD_DUCKDB)
    return path


def load_price(symbol: str) -> pd.DataFrame:
    connection = duckdb.connect(str(PROD_DUCKDB), read_only=True)
    try:
        if symbol.endswith(".US"):
            query = """
                SELECT trade_date AS date, open, high, low, close, volume
                FROM us_stock_daily WHERE upper(symbol)=? AND trade_date BETWEEN '2023-01-01' AND ?
                ORDER BY trade_date
            """
        else:
            query = """
                SELECT trade_date AS date, open, high, low, close, vol AS volume
                FROM a_stock_fund_daily_qfq WHERE upper(ts_code)=? AND trade_date BETWEEN '2023-01-01' AND ?
                ORDER BY trade_date
            """
        frame = connection.execute(query, [symbol.upper(), END]).fetchdf()
    finally:
        connection.close()
    frame["date"] = pd.to_datetime(frame["date"]).astype("datetime64[ns]")
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["close"]).sort_values("date")


def load_fear(
    index_symbol: str, *, eight_factor_etf: str | None = None,
    share_factor_direction: str = "positive",
) -> pd.DataFrame:
    connection = sqlite3.connect(f"file:{PROD_SQLITE}?mode=ro", uri=True)
    try:
        fields = ",".join(COMPONENT_COLUMNS)
        frame = pd.read_sql_query(
            f"SELECT date, score, {fields} FROM etf_fear_greed_clone_history "
            "WHERE upper(symbol)=? AND date BETWEEN '2023-01-01' AND ? ORDER BY date",
            connection, params=[index_symbol.upper(), END], parse_dates=["date"],
        )
    finally:
        connection.close()
    frame["fear_greed_7"] = pd.to_numeric(frame["score"], errors="coerce")
    frame["date"] = pd.to_datetime(frame["date"]).astype("datetime64[ns]")
    frame["fear_greed_8"] = frame["fear_greed_7"]
    if eight_factor_etf:
        indicators = pd.read_csv(INDICATOR_DAILY, parse_dates=["trade_date"])
        indicators = indicators[indicators["ts_code"] == eight_factor_etf].sort_values("trade_date").copy()
        indicators["date"] = indicators["trade_date"].shift(-1)
        frame = frame.merge(indicators[["date", "share_change_20d_score"]], on="date", how="left")
        count = frame[COMPONENT_COLUMNS].notna().sum(axis=1).clip(lower=1)
        factor = pd.to_numeric(frame["share_change_20d_score"], errors="coerce")
        if share_factor_direction == "negative":
            factor = 100.0 - factor
        elif share_factor_direction != "positive":
            raise ValueError(f"unsupported share factor direction: {share_factor_direction}")
        combined = (frame[COMPONENT_COLUMNS].sum(axis=1, min_count=1) + factor) / (count + 1)
        frame["fear_greed_8"] = combined.where(factor.notna(), frame["fear_greed_7"])
    return frame[["date", "fear_greed_7", "fear_greed_8"]]


def add_volume_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy().sort_values("date")
    log_volume = np.log(result["volume"].replace(0, np.nan))
    mean = log_volume.shift(1).rolling(20).mean()
    std = log_volume.shift(1).rolling(20).std(ddof=0).replace(0, np.nan)
    result["log_z"] = (log_volume - mean) / std
    result["volume_ma20"] = result["volume"].shift(1).rolling(20).mean()
    result["volume_ratio"] = result["volume"] / result["volume_ma20"]
    result["volume_ma20_excluding_recent_1"] = result["volume_ma20"]
    result["volume_ratio_consecutive_1"] = result["volume_ratio"]
    return result


def build_base(
    target_symbol: str, fear_symbol: str, signal_symbol: str,
    *, eight_factor_etf: str | None, share_factor_direction: str = "positive",
) -> pd.DataFrame:
    target = load_price(target_symbol)
    target["ma20"] = target["close"].rolling(20).mean()
    target_log = np.log(target["volume"].replace(0, np.nan))
    target["log_z_self"] = (
        target_log - target_log.shift(1).rolling(20).mean()
    ) / target_log.shift(1).rolling(20).std(ddof=0).replace(0, np.nan)
    target["execution_price"] = target["close"]

    signal = add_volume_features(load_price(signal_symbol))
    fear = load_fear(
        fear_symbol, eight_factor_etf=eight_factor_etf,
        share_factor_direction=share_factor_direction,
    )
    signal = pd.merge_asof(
        signal.sort_values("date"), fear.sort_values("date"), on="date", direction="backward"
    )
    signal["signal_date"] = signal["date"]
    signal["fear_date"] = signal["date"]
    signal["signal_volume"] = signal["volume"]
    signal["nine_turn_close"] = signal["close"]
    signal = signal[[
        "date", "signal_date", "fear_date", "fear_greed_7", "fear_greed_8",
        "signal_volume", "nine_turn_close", "volume_ma20", "volume_ratio", "log_z",
        "volume_ma20_excluding_recent_1", "volume_ratio_consecutive_1",
    ]]
    base = target.merge(signal, on="date", how="left")
    fill_columns = [
        "signal_date", "fear_date", "fear_greed_7", "fear_greed_8", "signal_volume", "nine_turn_close",
        "volume_ma20", "volume_ratio", "log_z", "volume_ma20_excluding_recent_1",
        "volume_ratio_consecutive_1",
    ]
    base[fill_columns] = base[fill_columns].ffill()
    base = base[(base["date"] >= START) & (base["date"] <= END)].dropna(
        subset=["fear_greed_7", "fear_greed_8", "ma20", "volume_ma20", "volume_ratio", "execution_price"]
    ).reset_index(drop=True)
    base.attrs["symbol"] = target_symbol
    return base


def score_version(base: pd.DataFrame, version: int) -> pd.DataFrame:
    result = base.copy()
    result["fear_greed"] = result[f"fear_greed_{version}"]
    result["cnn_fear_greed"] = result["fear_greed"]
    return result


def production_params(**overrides):
    from backend.src.app.api.soxl_fear_backtest import SOXLFearStrategyParams

    values = dict(
        buy_threshold=30.0, greed_threshold=70.0, volume_ratio_threshold=1.6,
        volume_ratio_consecutive_days=1, volume_z_threshold=None, sell_shrink_z=-1.0,
        buy_position_pct=100.0, cooldown_days=0, trailing_stop_pct=0.0,
        sell_position_pct=100.0, sell_reduction_basis="holdings", sell_price_above_avg_cost=False,
        max_take_profit_sells_per_cycle=2, min_position_pct_after_take_profit=0.0,
        rebalance_threshold_pct=0.0, execute_next_open=True, slippage_pct=-1.0, stamp_duty_pct=0.0,
        sub_symbol="512480.SH", sub_fear_source="a_stock_000688_sh",
        sub_volume_signal_symbol="588000.SH", sub_buy_threshold=25.0,
        sub_volume_ratio_threshold=1.6, swap_threshold=45.0,
        sub2_symbol="159941.SZ", sub2_fear_source="qqq_clone",
        sub2_volume_signal_symbol="QQQ.US", sub2_buy_threshold=20.0,
        sub2_volume_ratio_threshold=1.3,
    )
    values.update(overrides)
    return SOXLFearStrategyParams(**values)


def slice_base(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    attrs = dict(frame.attrs)
    result = frame[(frame["date"] >= start) & (frame["date"] <= end)].reset_index(drop=True)
    result.attrs.update(attrs)
    return result


def metric_row(name: str, version: int, period: str, params, result: dict) -> dict:
    return {
        "name": name, "factor_version": version, "period": period,
        "total_return_pct": result["total_return"], "annualized_return_pct": result["annualized_return"],
        "max_drawdown_pct": result["max_drawdown"], "sharpe_ratio": result["sharpe_ratio"],
        "sortino_ratio": result["sortino_ratio"], "calmar_ratio": result["calmar_ratio"],
        "buy_count": result["buy_count"], "sell_count": result["sell_count"],
        **{key: getattr(params, key) for key in [
            "buy_threshold", "greed_threshold", "volume_ratio_threshold", "sub_buy_threshold",
            "sub_volume_ratio_threshold", "sub2_buy_threshold", "sub2_volume_ratio_threshold", "swap_threshold",
        ]},
    }


def main() -> None:
    working_db = initialize_import_environment()
    try:
        from backend.src.app.api.soxl_fear_backtest import _run_seesaw_backtest

        main_base = build_base("510880.SH", "000015.SH", "510880.SH", eight_factor_etf="510880.SH")
        sub_base = build_base("512480.SH", "000688.SH", "588000.SH", eight_factor_etf="588000.SH")
        sub2_base = build_base("159941.SZ", "QQQ.US", "QQQ.US", eight_factor_etf=None)
        periods = {
            "full": (START, END),
            "train_2023_2024": (START, "2024-12-31"),
            "test_2025_2026": ("2025-01-01", END),
        }
        fixed_rows = []
        fixed_details = {}
        for version in (7, 8):
            params = production_params()
            for period, (period_start, period_end) in periods.items():
                result = _run_seesaw_backtest(
                    slice_base(score_version(main_base, version), period_start, period_end),
                    slice_base(score_version(sub_base, version), period_start, period_end),
                    params, 1_000_000, detailed=period == "full",
                    sub2_base_df=slice_base(score_version(sub2_base, version), period_start, period_end),
                )
                fixed_rows.append(metric_row("production_params", version, period, params, result))
                if period == "full":
                    fixed_details[version] = result

        grid = itertools.product(
            [25.0, 30.0, 35.0], [65.0, 70.0, 75.0], [1.4, 1.6, 1.8],
            [20.0, 25.0, 30.0], [1.4, 1.6],
            [15.0, 20.0, 25.0], [1.2, 1.3, 1.4], [40.0, 45.0, 50.0],
        )
        train_main = slice_base(score_version(main_base, 8), START, "2024-12-31")
        train_sub = slice_base(score_version(sub_base, 8), START, "2024-12-31")
        train_sub2 = slice_base(score_version(sub2_base, 8), START, "2024-12-31")
        search_rows = []
        for values in grid:
            params = production_params(
                buy_threshold=values[0], greed_threshold=values[1], volume_ratio_threshold=values[2],
                sub_buy_threshold=values[3], sub_volume_ratio_threshold=values[4],
                sub2_buy_threshold=values[5], sub2_volume_ratio_threshold=values[6], swap_threshold=values[7],
            )
            result = _run_seesaw_backtest(
                train_main, train_sub, params, 1_000_000, False, sub2_base_df=train_sub2,
            )
            search_rows.append(metric_row("grid", 8, "train_2023_2024", params, result))
        search = pd.DataFrame(search_rows)
        eligible = search[(search["buy_count"] >= 2) & (search["sell_count"] >= 2)].copy()
        top = eligible.sort_values(
            ["sharpe_ratio", "annualized_return_pct", "calmar_ratio"], ascending=False
        ).head(50)

        validation_rows = []
        for rank, (_, item) in enumerate(top.iterrows(), 1):
            params = production_params(**{
                key: item[key] for key in [
                    "buy_threshold", "greed_threshold", "volume_ratio_threshold", "sub_buy_threshold",
                    "sub_volume_ratio_threshold", "sub2_buy_threshold", "sub2_volume_ratio_threshold", "swap_threshold",
                ]
            })
            for period, (period_start, period_end) in {
                "full": (START, END), "test_2025_2026": ("2025-01-01", END),
            }.items():
                result = _run_seesaw_backtest(
                    slice_base(score_version(main_base, 8), period_start, period_end),
                    slice_base(score_version(sub_base, 8), period_start, period_end),
                    params, 1_000_000, False,
                    sub2_base_df=slice_base(score_version(sub2_base, 8), period_start, period_end),
                )
                row = metric_row(f"train_rank_{rank}", 8, period, params, result)
                row["train_rank"] = rank
                validation_rows.append(row)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(fixed_rows).to_csv(OUTPUT_DIR / "production_params_comparison.csv", index=False)
        search.to_csv(OUTPUT_DIR / "eight_factor_train_grid.csv", index=False)
        validation = pd.DataFrame(validation_rows)
        validation.to_csv(OUTPUT_DIR / "eight_factor_top50_validation.csv", index=False)
        for version, result in fixed_details.items():
            pd.DataFrame(result.get("trades", [])).to_csv(OUTPUT_DIR / f"production_v{version}_trades.csv", index=False)
            pd.DataFrame(result.get("equity_curve", [])).to_csv(OUTPUT_DIR / f"production_v{version}_equity.csv", index=False)
        print(pd.DataFrame(fixed_rows).to_string(index=False))
        print("\nTop validation")
        print(validation[validation["period"] == "test_2025_2026"].sort_values(
            ["sharpe_ratio", "total_return_pct"], ascending=False
        ).head(15).to_string(index=False))
    finally:
        working_db.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
