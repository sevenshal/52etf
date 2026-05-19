from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ALPHA_CORE_PATH = Path("/private/tmp/alpha_features_core_pkg")
alpha_core_path = Path(os.getenv("ALPHA_FEATURES_CORE_PATH", DEFAULT_ALPHA_CORE_PATH))
if alpha_core_path.exists():
    sys.path.insert(0, str(alpha_core_path))

import duckdb
import numpy as np
import pandas as pd
from alpha_features_core import Alpha191


@dataclass(frozen=True)
class ScreenConfig:
    sqlite_path: str
    duckdb_path: str
    index_code: str
    analysis_start: pd.Timestamp
    analysis_end: pd.Timestamp
    warm_start: pd.Timestamp
    forward_window: int
    min_listing_days: int
    min_daily_sample: int
    output_csv: str
    errors_csv: str


def parse_args() -> ScreenConfig:
    parser = argparse.ArgumentParser(
        description="Screen GTJA Alpha191 factors on the A股创新100 dynamic constituent pool."
    )
    parser.add_argument("--sqlite-path", default=os.getenv("QUANT_SQLITE_PATH", "/var/lib/quant_robot/evc_stocks.db"))
    parser.add_argument("--duckdb-path", default=os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb"))
    parser.add_argument("--index-code", default="INNO100.CN")
    parser.add_argument("--analysis-start", default="2024-01-01")
    parser.add_argument("--analysis-end", default="2026-05-18")
    parser.add_argument("--warm-start", default="2022-01-01")
    parser.add_argument("--forward-window", type=int, default=20)
    parser.add_argument("--min-listing-days", type=int, default=365)
    parser.add_argument("--min-daily-sample", type=int, default=20)
    parser.add_argument("--output-csv", default="/private/tmp/gtja_alpha191_inno100_screen.csv")
    parser.add_argument("--errors-csv", default="/private/tmp/gtja_alpha191_inno100_errors.csv")
    args = parser.parse_args()
    return ScreenConfig(
        sqlite_path=args.sqlite_path,
        duckdb_path=args.duckdb_path,
        index_code=args.index_code,
        analysis_start=pd.Timestamp(args.analysis_start).normalize(),
        analysis_end=pd.Timestamp(args.analysis_end).normalize(),
        warm_start=pd.Timestamp(args.warm_start).normalize(),
        forward_window=args.forward_window,
        min_listing_days=args.min_listing_days,
        min_daily_sample=args.min_daily_sample,
        output_csv=args.output_csv,
        errors_csv=args.errors_csv,
    )


def load_inno100_snapshots(config: ScreenConfig) -> tuple[list[pd.Timestamp], dict[pd.Timestamp, list[str]], list[str]]:
    with sqlite3.connect(config.sqlite_path) as conn:
        rebalances = pd.read_sql_query(
            """
            SELECT id, COALESCE(effective_date, rebalance_date) AS snapshot_date
            FROM a_stock_innovation100_rebalances
            WHERE index_code = ?
              AND COALESCE(effective_date, rebalance_date) <= ?
            ORDER BY snapshot_date, rebalance_date, id
            """,
            conn,
            params=[config.index_code, config.analysis_end.date().isoformat()],
        )
        if rebalances.empty:
            raise RuntimeError(f"{config.index_code} has no rebalance snapshots")

        rebalances["snapshot_date"] = pd.to_datetime(rebalances["snapshot_date"]).dt.normalize()
        selected = pd.concat(
            [
                rebalances[rebalances["snapshot_date"] < config.analysis_start].tail(1),
                rebalances[
                    (rebalances["snapshot_date"] >= config.analysis_start)
                    & (rebalances["snapshot_date"] <= config.analysis_end)
                ],
            ],
            ignore_index=True,
        ).drop_duplicates("snapshot_date", keep="last")
        selected = selected.sort_values("snapshot_date")
        id_to_date = dict(zip(selected["id"].astype(int), selected["snapshot_date"]))

        placeholders = ",".join(["?"] * len(id_to_date))
        constituents = pd.read_sql_query(
            f"""
            SELECT rebalance_id, ts_code, weight_pct
            FROM a_stock_innovation100_constituents
            WHERE index_code = ?
              AND rebalance_id IN ({placeholders})
            ORDER BY rebalance_id, weight_pct DESC
            """,
            conn,
            params=[config.index_code, *list(id_to_date)],
        )

    symbols_by_date: dict[pd.Timestamp, list[str]] = {}
    all_symbols: list[str] = []
    seen = set()
    for row in constituents.itertuples(index=False):
        snapshot_date = id_to_date.get(int(row.rebalance_id))
        symbol = str(row.ts_code or "").strip().upper()
        if not snapshot_date or not symbol:
            continue
        symbols_by_date.setdefault(snapshot_date, []).append(symbol)
        if symbol not in seen:
            seen.add(symbol)
            all_symbols.append(symbol)
    return sorted(symbols_by_date), symbols_by_date, all_symbols


def quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load_prices(config: ScreenConfig, symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        raise RuntimeError("No symbols available for the selected pool")
    symbol_sql = ", ".join(quote_sql_string(symbol) for symbol in symbols)
    query = f"""
        WITH first_dates AS (
            SELECT ts_code, MIN(trade_date) AS first_trade_date
            FROM a_stock_market_daily_qfq
            WHERE ts_code IN ({symbol_sql})
            GROUP BY ts_code
        )
        SELECT
            p.ts_code AS ticker,
            CAST(p.trade_date AS DATE) AS date,
            CAST(p.open AS DOUBLE) AS open,
            CAST(p.high AS DOUBLE) AS high,
            CAST(p.low AS DOUBLE) AS low,
            CAST(p.close AS DOUBLE) AS close,
            CAST(p.vol AS DOUBLE) AS volume,
            CAST(p.amount AS DOUBLE) AS amount,
            CAST(f.first_trade_date AS DATE) AS first_trade_date
        FROM a_stock_market_daily_qfq p
        LEFT JOIN first_dates f ON p.ts_code = f.ts_code
        WHERE p.ts_code IN ({symbol_sql})
          AND p.trade_date BETWEEN ? AND ?
          AND p.close IS NOT NULL
          AND p.close > 0
        ORDER BY p.ts_code, p.trade_date
    """
    with duckdb.connect(config.duckdb_path, read_only=True) as con:
        df = con.execute(query, [config.warm_start.date(), config.analysis_end.date()]).df()
    if df.empty:
        raise RuntimeError("No daily price rows found")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["first_trade_date"] = pd.to_datetime(df["first_trade_date"]).dt.normalize()
    df = df.sort_values(["ticker", "date"])
    df["past_return"] = df.groupby("ticker", sort=False)["close"].pct_change()
    df["forward_return"] = (
        df.groupby("ticker", sort=False)["close"].shift(-config.forward_window) / df["close"] - 1
    )
    df["listing_days"] = (df["date"] - df["first_trade_date"]).dt.days
    return df


def pivot_matrix(df: pd.DataFrame, tickers: list[str], dates: list[pd.Timestamp], column: str) -> np.ndarray:
    return (
        df.pivot(index="ticker", columns="date", values=column)
        .reindex(index=tickers, columns=dates)
        .to_numpy(dtype=np.float64)
    )


def rank_average(values: np.ndarray) -> np.ndarray:
    return pd.Series(values).rank(method="average").to_numpy(dtype=np.float64)


def spearman_ic(config: ScreenConfig, factor: np.ndarray, forward: np.ndarray) -> float:
    if len(factor) < config.min_daily_sample:
        return float("nan")
    if len(np.unique(factor)) < 2 or len(np.unique(forward)) < 2:
        return float("nan")
    corr = np.corrcoef(rank_average(factor), rank_average(forward))[0, 1]
    return float(corr) if math.isfinite(corr) else float("nan")


def evaluate_alpha(
    config: ScreenConfig,
    alpha_matrix: np.ndarray,
    forward_matrix: np.ndarray,
    base_mask: np.ndarray,
    alpha_name: str,
) -> dict:
    daily_ics: list[float] = []
    daily_counts: list[int] = []
    valid_mask = base_mask & np.isfinite(alpha_matrix) & np.isfinite(forward_matrix)
    for date_index in range(valid_mask.shape[1]):
        mask = valid_mask[:, date_index]
        count = int(mask.sum())
        if count < config.min_daily_sample:
            continue
        ic = spearman_ic(config, alpha_matrix[mask, date_index], forward_matrix[mask, date_index])
        if math.isfinite(ic):
            daily_ics.append(ic)
            daily_counts.append(count)
    if not daily_ics:
        return {}

    ic_arr = np.asarray(daily_ics, dtype=float)
    mean_ic = float(np.mean(ic_arr))
    std_ic = float(np.std(ic_arr, ddof=1)) if len(ic_arr) > 1 else float("nan")
    ic_t = mean_ic / std_ic * math.sqrt(len(ic_arr)) if std_ic and math.isfinite(std_ic) and std_ic > 0 else float("nan")
    return {
        "key": alpha_name,
        "mean_ic": mean_ic,
        "abs_ic": abs(mean_ic),
        "ic_t": ic_t,
        "positive_rate": float(np.mean(ic_arr > 0)),
        "days": int(len(ic_arr)),
        "samples": int(sum(daily_counts)),
        "coverage": float(valid_mask.sum() / base_mask.sum()) if base_mask.sum() else 0.0,
    }


def build_base_mask(
    config: ScreenConfig,
    tickers: list[str],
    dates: list[pd.Timestamp],
    snapshot_dates: list[pd.Timestamp],
    symbols_by_date: dict[pd.Timestamp, list[str]],
    listing_days: np.ndarray,
    forward_matrix: np.ndarray,
) -> np.ndarray:
    ticker_index = {ticker: idx for idx, ticker in enumerate(tickers)}
    date_index = {current_date: idx for idx, current_date in enumerate(dates)}
    pool_mask = np.zeros((len(tickers), len(dates)), dtype=bool)
    pointer = -1
    for current_date in dates:
        current_date = pd.Timestamp(current_date).normalize()
        while pointer + 1 < len(snapshot_dates) and snapshot_dates[pointer + 1] <= current_date:
            pointer += 1
        if pointer < 0 or current_date < config.analysis_start:
            continue
        snapshot_date = snapshot_dates[pointer]
        current_date_index = date_index[current_date]
        for ticker in symbols_by_date.get(snapshot_date, []):
            current_ticker_index = ticker_index.get(ticker)
            if current_ticker_index is not None:
                pool_mask[current_ticker_index, current_date_index] = True

    return (
        pool_mask
        & np.isfinite(forward_matrix)
        & np.isfinite(listing_days)
        & (listing_days >= config.min_listing_days)
    )


def main() -> None:
    config = parse_args()
    snapshot_dates, symbols_by_date, symbols = load_inno100_snapshots(config)
    price_df = load_prices(config, symbols)
    tickers = sorted(price_df["ticker"].unique())
    dates = sorted(price_df["date"].unique())

    matrices = {
        column: pivot_matrix(price_df, tickers, dates, column)
        for column in ["open", "high", "low", "close", "volume", "amount", "past_return", "forward_return", "listing_days"]
    }
    forward_matrix = matrices["forward_return"]
    base_mask = build_base_mask(
        config,
        tickers,
        dates,
        snapshot_dates,
        symbols_by_date,
        matrices["listing_days"],
        forward_matrix,
    )
    valid_dates = np.where(base_mask.any(axis=0))[0]
    latest_signal_date = dates[int(valid_dates[-1])].date().isoformat() if len(valid_dates) else "n/a"
    print(
        f"snapshots={len(snapshot_dates)} tickers={len(tickers)} price_rows={len(price_df)} "
        f"base_samples={int(base_mask.sum())} base_days={len(valid_dates)} latest_signal_date={latest_signal_date}",
        flush=True,
    )

    calc = Alpha191(
        open=matrices["open"],
        high=matrices["high"],
        low=matrices["low"],
        close=matrices["close"],
        volume=matrices["volume"],
        amount=matrices["amount"],
        returns=matrices["past_return"],
    )

    results = []
    errors = []
    for alpha_num in range(1, 192):
        alpha_name = f"alpha{alpha_num:03d}"
        try:
            alpha_matrix = calc.calculate(alpha_num)
            metric = evaluate_alpha(config, alpha_matrix, forward_matrix, base_mask, alpha_name)
            if metric:
                results.append(metric)
                print(f"{alpha_name} ic={metric['mean_ic']:.4f} t={metric['ic_t']:.2f} coverage={metric['coverage']:.3f}", flush=True)
            else:
                errors.append({"key": alpha_name, "error": "no_valid_ic"})
                print(f"{alpha_name} no_valid_ic", flush=True)
        except Exception as exc:
            errors.append({"key": alpha_name, "error": repr(exc)})
            print(f"{alpha_name} ERROR {exc!r}", flush=True)

    result_df = pd.DataFrame(results).sort_values(["abs_ic", "samples"], ascending=[False, False])
    error_df = pd.DataFrame(errors)
    result_df.to_csv(config.output_csv, index=False)
    error_df.to_csv(config.errors_csv, index=False)
    print(f"wrote {config.output_csv} rows={len(result_df)}", flush=True)
    print(f"wrote {config.errors_csv} rows={len(error_df)}", flush=True)
    print(result_df.head(30).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
