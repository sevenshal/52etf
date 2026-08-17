from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb
import numpy as np
import pandas as pd


TRADING_DAYS_PER_YEAR = 252.0
AMOUNT_YI_TO_TUSHARE_AMOUNT = 100000.0
MARKET_CAP_YI_TO_TUSHARE_MV = 10000.0


@dataclass(frozen=True)
class StrategyConfig:
    duckdb_path: str
    start_date: pd.Timestamp
    end_date: Optional[pd.Timestamp]
    warmup_days: int
    benchmark_symbol: str
    max_positions: int
    hold_days: int
    commission_bps: float
    sell_tax_bps: float
    min_listing_days: int
    min_amount_yi: float
    min_turnover_pct: float
    max_turnover_pct: float
    min_total_mv_yi: float
    max_total_mv_yi: float
    min_amount_ratio: float
    min_signal_score: float
    daily_top_n: int
    min_pct_chg: float
    max_pct_chg: float
    min_ret_5d: float
    max_ret_5d: float
    min_price_position_20: float
    max_price_position_20: float
    min_volatility_10d: float
    max_volatility_10d: float
    volatility_target_rank: float
    max_entry_gap_pct: float
    max_entry_drop_pct: float
    min_benchmark_ret_5d: float
    max_benchmark_ma20_gap_pct: float
    output_json: str
    trades_csv: str
    signals_csv: str
    equity_csv: str


def parse_args() -> StrategyConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Approximate the Xiaohongshu ChiNext overnight strategy: "
            "abnormal activity, multi-factor score, timing filter, max two names, sell next day."
        )
    )
    parser.add_argument("--duckdb-path", default=os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb"))
    parser.add_argument("--start-date", default="2023-01-03")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--warmup-days", type=int, default=260)
    parser.add_argument("--benchmark-symbol", default="399006.SZ")
    parser.add_argument("--max-positions", type=int, default=2)
    parser.add_argument("--hold-days", type=int, default=1)
    parser.add_argument("--commission-bps", type=float, default=3.0)
    parser.add_argument("--sell-tax-bps", type=float, default=5.0)
    parser.add_argument("--min-listing-days", type=int, default=120)
    parser.add_argument("--min-amount-yi", type=float, default=1.0)
    parser.add_argument("--min-turnover-pct", type=float, default=2.0)
    parser.add_argument("--max-turnover-pct", type=float, default=30.0)
    parser.add_argument("--min-total-mv-yi", type=float, default=25.0)
    parser.add_argument("--max-total-mv-yi", type=float, default=900.0)
    parser.add_argument("--min-amount-ratio", type=float, default=2.0)
    parser.add_argument("--min-signal-score", type=float, default=0.90)
    parser.add_argument("--daily-top-n", type=int, default=2)
    parser.add_argument("--min-pct-chg", type=float, default=-6.0)
    parser.add_argument("--max-pct-chg", type=float, default=12.0)
    parser.add_argument("--min-ret-5d", type=float, default=-0.02)
    parser.add_argument("--max-ret-5d", type=float, default=0.30)
    parser.add_argument("--min-price-position-20", type=float, default=0.20)
    parser.add_argument("--max-price-position-20", type=float, default=0.96)
    parser.add_argument("--min-volatility-10d", type=float, default=0.25)
    parser.add_argument("--max-volatility-10d", type=float, default=1.80)
    parser.add_argument("--volatility-target-rank", type=float, default=0.70)
    parser.add_argument("--max-entry-gap-pct", type=float, default=1.0)
    parser.add_argument("--max-entry-drop-pct", type=float, default=7.0)
    parser.add_argument("--min-benchmark-ret-5d", type=float, default=-0.04)
    parser.add_argument("--max-benchmark-ma20-gap-pct", type=float, default=6.0)
    parser.add_argument("--output-json", default="/private/tmp/xhs_chinext_overnight_backtest.json")
    parser.add_argument("--trades-csv", default="/private/tmp/xhs_chinext_overnight_trades.csv")
    parser.add_argument("--signals-csv", default="/private/tmp/xhs_chinext_overnight_signals.csv")
    parser.add_argument("--equity-csv", default="/private/tmp/xhs_chinext_overnight_equity.csv")
    args = parser.parse_args()
    return StrategyConfig(
        duckdb_path=args.duckdb_path,
        start_date=pd.Timestamp(args.start_date).normalize(),
        end_date=pd.Timestamp(args.end_date).normalize() if args.end_date else None,
        warmup_days=max(120, int(args.warmup_days)),
        benchmark_symbol=str(args.benchmark_symbol).strip().upper(),
        max_positions=max(1, int(args.max_positions)),
        hold_days=max(1, int(args.hold_days)),
        commission_bps=max(0.0, float(args.commission_bps)),
        sell_tax_bps=max(0.0, float(args.sell_tax_bps)),
        min_listing_days=max(0, int(args.min_listing_days)),
        min_amount_yi=max(0.0, float(args.min_amount_yi)),
        min_turnover_pct=float(args.min_turnover_pct),
        max_turnover_pct=float(args.max_turnover_pct),
        min_total_mv_yi=max(0.0, float(args.min_total_mv_yi)),
        max_total_mv_yi=max(0.0, float(args.max_total_mv_yi)),
        min_amount_ratio=float(args.min_amount_ratio),
        min_signal_score=float(args.min_signal_score),
        daily_top_n=max(1, int(args.daily_top_n)),
        min_pct_chg=float(args.min_pct_chg),
        max_pct_chg=float(args.max_pct_chg),
        min_ret_5d=float(args.min_ret_5d),
        max_ret_5d=float(args.max_ret_5d),
        min_price_position_20=float(args.min_price_position_20),
        max_price_position_20=float(args.max_price_position_20),
        min_volatility_10d=float(args.min_volatility_10d),
        max_volatility_10d=float(args.max_volatility_10d),
        volatility_target_rank=min(1.0, max(0.0, float(args.volatility_target_rank))),
        max_entry_gap_pct=max(0.0, float(args.max_entry_gap_pct)),
        max_entry_drop_pct=max(0.0, float(args.max_entry_drop_pct)),
        min_benchmark_ret_5d=float(args.min_benchmark_ret_5d),
        max_benchmark_ma20_gap_pct=max(0.0, float(args.max_benchmark_ma20_gap_pct)),
        output_json=args.output_json,
        trades_csv=args.trades_csv,
        signals_csv=args.signals_csv,
        equity_csv=args.equity_csv,
    )


def latest_available_end(config: StrategyConfig) -> pd.Timestamp:
    with duckdb.connect(config.duckdb_path, read_only=True) as conn:
        row = conn.execute("SELECT MAX(trade_date) FROM a_stock_market_daily_qfq").fetchone()
    if not row or row[0] is None:
        raise RuntimeError("No a_stock_market_daily_qfq rows available")
    return pd.Timestamp(row[0]).normalize()


def load_chinext_prices(config: StrategyConfig, fetch_start: pd.Timestamp, fetch_end: pd.Timestamp) -> pd.DataFrame:
    query = """
        WITH first_dates AS (
            SELECT ts_code, MIN(trade_date) AS first_trade_date
            FROM a_stock_market_daily_qfq
            GROUP BY ts_code
        )
        SELECT
            p.ts_code AS symbol,
            b.name AS name,
            b.industry AS industry,
            CAST(p.trade_date AS DATE) AS trade_date,
            CAST(p.open AS DOUBLE) AS open,
            CAST(p.high AS DOUBLE) AS high,
            CAST(p.low AS DOUBLE) AS low,
            CAST(p.close AS DOUBLE) AS close,
            CAST(p.pre_close AS DOUBLE) AS pre_close,
            CAST(p.pct_chg AS DOUBLE) AS pct_chg,
            CAST(p.vol AS DOUBLE) AS volume,
            CAST(p.amount AS DOUBLE) AS amount,
            CAST(p.total_mv AS DOUBLE) AS total_mv,
            CAST(p.circ_mv AS DOUBLE) AS circ_mv,
            CAST(p.turnover_rate AS DOUBLE) AS turnover_rate,
            CAST(f.first_trade_date AS DATE) AS first_trade_date,
            CAST(b.list_date AS DATE) AS list_date
        FROM a_stock_market_daily_qfq p
        JOIN a_stock_basic b ON p.ts_code = b.ts_code
        LEFT JOIN first_dates f ON p.ts_code = f.ts_code
        WHERE p.trade_date BETWEEN ? AND ?
          AND b.market = '创业板'
          AND b.exchange = 'SZSE'
          AND b.list_date IS NOT NULL
          AND date_diff('day', b.list_date, p.trade_date) >= ?
          AND (b.delist_date IS NULL OR b.delist_date > p.trade_date)
          AND (b.list_status IS NULL OR b.list_status = 'L')
          AND (b.name IS NULL OR (upper(b.name) NOT LIKE '%ST%' AND b.name NOT LIKE '%退%'))
          AND NOT EXISTS (
              SELECT 1
              FROM a_stock_name_changes n
              WHERE n.ts_code = p.ts_code
                AND p.trade_date BETWEEN COALESCE(n.start_date, DATE '1900-01-01')
                                     AND COALESCE(n.end_date, DATE '2099-12-31')
                AND (
                    upper(COALESCE(n.name, '')) LIKE '%ST%'
                    OR COALESCE(n.name, '') LIKE '%退%'
                    OR upper(COALESCE(n.change_reason, '')) LIKE '%ST%'
                    OR COALESCE(n.change_reason, '') LIKE '%终止上市%'
                )
          )
          AND p.open IS NOT NULL
          AND p.high IS NOT NULL
          AND p.low IS NOT NULL
          AND p.close IS NOT NULL
          AND p.open > 0
          AND p.high > 0
          AND p.low > 0
          AND p.close > 0
        ORDER BY p.ts_code, p.trade_date
    """
    with duckdb.connect(config.duckdb_path, read_only=True) as conn:
        df = conn.execute(
            query,
            [fetch_start.date(), fetch_end.date(), config.min_listing_days],
        ).df()
    if df.empty:
        raise RuntimeError("No ChiNext daily price rows found")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df["first_trade_date"] = pd.to_datetime(df["first_trade_date"]).dt.normalize()
    df["list_date"] = pd.to_datetime(df["list_date"]).dt.normalize()
    return df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def load_benchmark(config: StrategyConfig, fetch_start: pd.Timestamp, fetch_end: pd.Timestamp) -> pd.DataFrame:
    query = """
        SELECT
            CAST(trade_date AS DATE) AS trade_date,
            CAST(open AS DOUBLE) AS open,
            CAST(high AS DOUBLE) AS high,
            CAST(low AS DOUBLE) AS low,
            CAST(close AS DOUBLE) AS close,
            CAST(pct_chg AS DOUBLE) AS pct_chg
        FROM a_stock_index_daily
        WHERE ts_code = ?
          AND trade_date BETWEEN ? AND ?
          AND close IS NOT NULL
          AND close > 0
        ORDER BY trade_date
    """
    with duckdb.connect(config.duckdb_path, read_only=True) as conn:
        df = conn.execute(query, [config.benchmark_symbol, fetch_start.date(), fetch_end.date()]).df()
    if df.empty:
        raise RuntimeError(f"No benchmark rows found for {config.benchmark_symbol}")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["benchmark_return"] = df["close"].pct_change()
    df["benchmark_ret_5d"] = df["close"] / df["close"].shift(5) - 1
    df["benchmark_ret_20d"] = df["close"] / df["close"].shift(20) - 1
    df["benchmark_ma10"] = df["close"].rolling(10, min_periods=10).mean()
    df["benchmark_ma20"] = df["close"].rolling(20, min_periods=20).mean()
    df["benchmark_vol_10d"] = df["benchmark_return"].rolling(10, min_periods=10).std() * math.sqrt(TRADING_DAYS_PER_YEAR)
    ma20_gap = df["close"] / df["benchmark_ma20"] - 1
    df["market_timing_pass"] = (
        (df["benchmark_ret_5d"] >= config.min_benchmark_ret_5d)
        & (ma20_gap >= -config.max_benchmark_ma20_gap_pct / 100.0)
        & (df["pct_chg"] >= -4.5)
    )
    return df


def add_group_rolling(
    df: pd.DataFrame,
    group_key: str,
    source: str,
    window: int,
    min_periods: Optional[int] = None,
    agg: str = "mean",
) -> pd.Series:
    min_periods = window if min_periods is None else min_periods
    grouped = df.groupby(group_key, sort=False)[source]
    if agg == "mean":
        return grouped.transform(lambda s: s.rolling(window, min_periods=min_periods).mean())
    if agg == "std":
        return grouped.transform(lambda s: s.rolling(window, min_periods=min_periods).std())
    if agg == "sum":
        return grouped.transform(lambda s: s.rolling(window, min_periods=min_periods).sum())
    if agg == "max":
        return grouped.transform(lambda s: s.rolling(window, min_periods=min_periods).max())
    if agg == "min":
        return grouped.transform(lambda s: s.rolling(window, min_periods=min_periods).min())
    raise ValueError(f"Unsupported rolling agg: {agg}")


def add_features(config: StrategyConfig, price_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> pd.DataFrame:
    df = price_df.copy()
    grouped = df.groupby("symbol", sort=False)
    df["return_1d"] = df["close"] / grouped["close"].shift(1) - 1
    for window in (2, 3, 5, 10, 20, 60):
        df[f"ret_{window}d"] = df["close"] / grouped["close"].shift(window) - 1

    df["amount_ma3"] = add_group_rolling(df, "symbol", "amount", 3)
    df["amount_ma5"] = add_group_rolling(df, "symbol", "amount", 5)
    df["amount_ma20_prev"] = grouped["amount"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    df["amount_std20_prev"] = grouped["amount"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).std())
    df["amount_ratio_5_20"] = df["amount_ma5"] / df["amount_ma20_prev"]
    df["amount_ratio_3_20"] = df["amount_ma3"] / df["amount_ma20_prev"]
    df["amount_z20"] = (df["amount"] - df["amount_ma20_prev"]) / df["amount_std20_prev"]

    signed_amount = df["amount"].fillna(0) * np.sign(df["return_1d"].fillna(0))
    df["_signed_amount"] = signed_amount
    df["signed_amount_5"] = add_group_rolling(df, "symbol", "_signed_amount", 5, agg="sum")
    df["amount_sum_5"] = add_group_rolling(df, "symbol", "amount", 5, agg="sum")
    df["money_strength_5"] = df["signed_amount_5"] / df["amount_sum_5"]

    df["turnover_ma5"] = add_group_rolling(df, "symbol", "turnover_rate", 5)
    df["turnover_ma20_prev"] = grouped["turnover_rate"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    df["turnover_ratio_5_20"] = df["turnover_ma5"] / df["turnover_ma20_prev"]

    df["volatility_10d"] = grouped["return_1d"].transform(lambda s: s.rolling(10, min_periods=10).std()) * math.sqrt(
        TRADING_DAYS_PER_YEAR
    )
    df["volatility_20d"] = grouped["return_1d"].transform(lambda s: s.rolling(20, min_periods=20).std()) * math.sqrt(
        TRADING_DAYS_PER_YEAR
    )
    df["range_pct"] = (df["high"] - df["low"]) / df["close"]
    df["range_ma10"] = add_group_rolling(df, "symbol", "range_pct", 10)

    df["high_10"] = add_group_rolling(df, "symbol", "high", 10, agg="max")
    df["high_20"] = add_group_rolling(df, "symbol", "high", 20, agg="max")
    df["low_20"] = add_group_rolling(df, "symbol", "low", 20, agg="min")
    high_low_range = (df["high_20"] - df["low_20"]).replace(0, np.nan)
    df["price_position_20"] = ((df["close"] - df["low_20"]) / high_low_range).clip(lower=0.0, upper=1.0)
    df["pullback_from_10d_high"] = (df["high_10"] / df["close"] - 1).clip(lower=0.0, upper=0.20)
    intraday_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_position"] = ((df["close"] - df["low"]) / intraday_range).clip(lower=0.0, upper=1.0)
    df["close_position"] = df["close_position"].fillna(0.5)

    for window in (5, 10, 20):
        df[f"ma{window}"] = add_group_rolling(df, "symbol", "close", window)
    df["ma_alignment"] = (df["close"] > df["ma5"]) & (df["ma5"] > df["ma10"]) & (df["ma10"] >= df["ma20"] * 0.98)

    benchmark_cols = [
        "trade_date",
        "benchmark_return",
        "benchmark_ret_5d",
        "benchmark_ret_20d",
        "benchmark_ma10",
        "benchmark_ma20",
        "benchmark_vol_10d",
        "market_timing_pass",
    ]
    df = df.merge(benchmark_df[benchmark_cols], on="trade_date", how="left")
    df["relative_strength_3d"] = df["ret_3d"] - (df["benchmark_ret_5d"] / 5.0 * 3.0)
    df["relative_strength_5d"] = df["ret_5d"] - df["benchmark_ret_5d"]
    df["relative_strength_20d"] = df["ret_20d"] - df["benchmark_ret_20d"]
    df["listing_days"] = (df["trade_date"] - df["list_date"]).dt.days
    return df.drop(columns=["_signed_amount"], errors="ignore")


def percentile_rank_by_date(df: pd.DataFrame, column: str, *, ascending: bool = True) -> pd.Series:
    return df.groupby("trade_date", sort=False)[column].rank(pct=True, ascending=ascending)


def finite_clip(series: pd.Series, lower: float = 0.0, upper: float = 1.0) -> pd.Series:
    return series.replace([np.inf, -np.inf], np.nan).clip(lower=lower, upper=upper)


def add_signal_scores(config: StrategyConfig, feature_df: pd.DataFrame) -> pd.DataFrame:
    df = feature_df.copy()
    df["activity_rank"] = (
        percentile_rank_by_date(df, "amount_ratio_5_20").fillna(0) * 0.40
        + percentile_rank_by_date(df, "amount_z20").fillna(0) * 0.30
        + percentile_rank_by_date(df, "turnover_ratio_5_20").fillna(0) * 0.30
    )
    df["momentum_rank"] = (
        percentile_rank_by_date(df, "relative_strength_3d").fillna(0) * 0.30
        + percentile_rank_by_date(df, "relative_strength_5d").fillna(0) * 0.45
        + percentile_rank_by_date(df, "relative_strength_20d").fillna(0) * 0.25
    )
    df["money_rank"] = (
        percentile_rank_by_date(df, "money_strength_5").fillna(0) * 0.60
        + percentile_rank_by_date(df, "amount_ratio_3_20").fillna(0) * 0.40
    )
    df["volatility_rank"] = percentile_rank_by_date(df, "volatility_10d").fillna(0)
    df["volatility_preference"] = (
        1.0 - (df["volatility_rank"] - config.volatility_target_rank).abs() / max(config.volatility_target_rank, 0.01)
    ).clip(lower=0.0, upper=1.0)
    df["pullback_score"] = (
        finite_clip(df["pullback_from_10d_high"] / 0.12) * 0.55
        + finite_clip(1.0 - (df["close_position"] - 0.35).abs() / 0.65) * 0.45
    )
    df["liquidity_score"] = (
        percentile_rank_by_date(df, "amount").fillna(0) * 0.45
        + percentile_rank_by_date(df, "turnover_rate").fillna(0) * 0.35
        + percentile_rank_by_date(df, "total_mv", ascending=False).fillna(0) * 0.20
    )

    df["signal_score"] = (
        df["activity_rank"] * 0.24
        + df["momentum_rank"] * 0.20
        + df["money_rank"] * 0.18
        + df["volatility_preference"] * 0.14
        + df["pullback_score"] * 0.12
        + df["liquidity_score"] * 0.12
    )
    return df


def add_filter_columns(config: StrategyConfig, scored_df: pd.DataFrame, analysis_start: pd.Timestamp) -> pd.DataFrame:
    df = scored_df.copy()
    total_mv_min = config.min_total_mv_yi * MARKET_CAP_YI_TO_TUSHARE_MV
    total_mv_max = config.max_total_mv_yi * MARKET_CAP_YI_TO_TUSHARE_MV
    amount_min = config.min_amount_yi * AMOUNT_YI_TO_TUSHARE_AMOUNT
    filters = [
        ("analysis_window", df["trade_date"] >= analysis_start),
        ("market_timing", df["market_timing_pass"].eq(True)),
        ("liquidity_amount", df["amount"] >= amount_min),
        (
            "turnover_band",
            df["turnover_rate"].between(config.min_turnover_pct, config.max_turnover_pct, inclusive="both"),
        ),
        ("market_cap_band", df["total_mv"].between(total_mv_min, total_mv_max, inclusive="both")),
        ("amount_ratio", df["amount_ratio_5_20"] >= config.min_amount_ratio),
        ("pct_chg_band", df["pct_chg"].between(config.min_pct_chg, config.max_pct_chg, inclusive="both")),
        ("ret_5d_band", df["ret_5d"].between(config.min_ret_5d, config.max_ret_5d, inclusive="both")),
        (
            "price_position_band",
            df["price_position_20"].between(
                config.min_price_position_20,
                config.max_price_position_20,
                inclusive="both",
            ),
        ),
        (
            "volatility_band",
            df["volatility_10d"].between(config.min_volatility_10d, config.max_volatility_10d, inclusive="both"),
        ),
        ("not_new_stock", df["listing_days"] >= config.min_listing_days),
        ("valid_score", df["signal_score"].notna() & (df["signal_score"] >= config.min_signal_score)),
    ]
    cumulative = pd.Series(True, index=df.index)
    for name, condition in filters:
        df[f"pass_{name}"] = condition.fillna(False)
        cumulative &= df[f"pass_{name}"]
        df[f"pass_through_{name}"] = cumulative
    df["eligible_signal"] = cumulative
    return df


def select_daily_signals(config: StrategyConfig, filtered_df: pd.DataFrame, analysis_end: pd.Timestamp) -> pd.DataFrame:
    candidates = filtered_df[
        (filtered_df["trade_date"] <= analysis_end) & filtered_df["eligible_signal"]
    ].copy()
    if candidates.empty:
        return candidates
    candidates = candidates.sort_values(["trade_date", "signal_score"], ascending=[True, False])
    candidates["daily_rank"] = candidates.groupby("trade_date", sort=False).cumcount() + 1
    return candidates[candidates["daily_rank"] <= config.daily_top_n].copy()


def price_lookup_frame(feature_df: pd.DataFrame) -> pd.DataFrame:
    return feature_df.set_index(["trade_date", "symbol"], drop=False).sort_index()


def get_price_row(price_lookup: pd.DataFrame, trade_date: pd.Timestamp, symbol: str) -> Optional[pd.Series]:
    try:
        row = price_lookup.loc[(trade_date, symbol)]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):
        if row.empty:
            return None
        return row.iloc[-1]
    return row


def summarize_equity(equity_df: pd.DataFrame, prefix: str = "") -> Dict[str, Any]:
    if equity_df.empty:
        return {
            f"{prefix}start": None,
            f"{prefix}end": None,
            f"{prefix}days": 0,
            f"{prefix}total_return_pct": None,
            f"{prefix}annualized_return_pct": None,
            f"{prefix}max_drawdown_pct": None,
            f"{prefix}sharpe": None,
            f"{prefix}positive_day_rate_pct": None,
        }
    equity = pd.to_numeric(equity_df["equity"], errors="coerce").dropna()
    returns = equity.pct_change().dropna()
    years = len(equity) / TRADING_DAYS_PER_YEAR
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) > 1 else 0.0
    ann_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 and equity.iloc[-1] > 0 else float("nan")
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    std = returns.std(ddof=1)
    sharpe = returns.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR) if len(returns) > 1 and std > 0 else float("nan")
    return {
        f"{prefix}start": equity_df["trade_date"].iloc[0].date().isoformat(),
        f"{prefix}end": equity_df["trade_date"].iloc[-1].date().isoformat(),
        f"{prefix}days": int(len(equity_df)),
        f"{prefix}total_return_pct": round(total_return * 100.0, 4),
        f"{prefix}annualized_return_pct": round(float(ann_return) * 100.0, 4) if math.isfinite(float(ann_return)) else None,
        f"{prefix}max_drawdown_pct": round(float(drawdown.min()) * 100.0, 4),
        f"{prefix}sharpe": round(float(sharpe), 4) if math.isfinite(float(sharpe)) else None,
        f"{prefix}positive_day_rate_pct": round(float((returns > 0).mean()) * 100.0, 2) if len(returns) else None,
    }


def build_benchmark_equity(
    benchmark_df: pd.DataFrame,
    analysis_start: pd.Timestamp,
    analysis_end: pd.Timestamp,
) -> pd.DataFrame:
    bench = benchmark_df[
        (benchmark_df["trade_date"] >= analysis_start) & (benchmark_df["trade_date"] <= analysis_end)
    ].copy()
    if bench.empty:
        return pd.DataFrame(columns=["trade_date", "equity"])
    bench["equity"] = bench["close"] / bench["close"].iloc[0]
    return bench[["trade_date", "equity"]]


def backtest_portfolio(
    config: StrategyConfig,
    feature_df: pd.DataFrame,
    signals: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    analysis_start: pd.Timestamp,
    analysis_end: pd.Timestamp,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    trading_dates = pd.to_datetime(
        feature_df[
            (feature_df["trade_date"] >= analysis_start) & (feature_df["trade_date"] <= analysis_end)
        ]["trade_date"]
        .drop_duplicates()
        .sort_values()
    ).to_list()
    date_index = {date_value: idx for idx, date_value in enumerate(trading_dates)}
    price_lookup = price_lookup_frame(feature_df)

    pending_entries: Dict[pd.Timestamp, List[dict]] = {}
    for row in signals.sort_values(["trade_date", "signal_score"], ascending=[True, False]).itertuples(index=False):
        signal_date = row.trade_date
        signal_idx = date_index.get(signal_date)
        if signal_idx is None:
            continue
        entry_idx = signal_idx + 1
        exit_idx = entry_idx + config.hold_days
        if entry_idx >= len(trading_dates) or exit_idx >= len(trading_dates):
            continue
        entry_date = trading_dates[entry_idx]
        exit_date = trading_dates[exit_idx]
        if exit_date > analysis_end:
            continue
        item = row._asdict()
        item["entry_date"] = entry_date
        item["exit_date"] = exit_date
        pending_entries.setdefault(entry_date, []).append(item)

    cash = 1.0
    positions: Dict[str, dict] = {}
    equity_rows: List[Dict[str, Any]] = []
    trade_rows: List[Dict[str, Any]] = []
    buy_cost_rate = config.commission_bps / 10000.0
    sell_cost_rate = (config.commission_bps + config.sell_tax_bps) / 10000.0
    slot_fraction = 1.0 / float(config.max_positions)

    for date_value in trading_dates:
        for symbol, position in list(positions.items()):
            if position.get("exit_date") != date_value:
                continue
            price_row = get_price_row(price_lookup, date_value, symbol)
            if price_row is None:
                continue
            sell_open = float(price_row.get("open") or 0)
            if sell_open <= 0:
                continue
            cash_received = float(position["shares"]) * sell_open * (1.0 - sell_cost_rate)
            cash += cash_received
            invested_cash = float(position.get("invested_cash") or 0)
            pnl = cash_received - invested_cash
            trade_rows.append(
                {
                    "trade_date": date_value,
                    "symbol": symbol,
                    "name": position.get("name"),
                    "side": "SELL",
                    "price": sell_open,
                    "gross_value": float(position["shares"]) * sell_open,
                    "cash_after_cost": cash_received,
                    "signal_date": position.get("signal_date"),
                    "entry_date": position.get("entry_date"),
                    "exit_date": date_value,
                    "holding_days": config.hold_days,
                    "signal_score": position.get("signal_score"),
                    "trade_return_pct": (pnl / invested_cash * 100.0) if invested_cash > 0 else None,
                    "reason": "overnight_exit",
                }
            )
            positions.pop(symbol, None)

        entries = pending_entries.get(date_value, [])
        entries = sorted(entries, key=lambda item: float(item.get("signal_score") or -999), reverse=True)
        for signal in entries:
            if len(positions) >= config.max_positions:
                break
            symbol = str(signal.get("symbol") or "")
            if not symbol or symbol in positions:
                continue
            price_row = get_price_row(price_lookup, date_value, symbol)
            if price_row is None:
                continue
            entry_open = float(price_row.get("open") or 0)
            prev_close = float(signal.get("close") or 0)
            if entry_open <= 0 or prev_close <= 0:
                continue
            entry_gap = entry_open / prev_close - 1.0
            if entry_gap > config.max_entry_gap_pct / 100.0:
                continue
            if entry_gap < -config.max_entry_drop_pct / 100.0:
                continue

            open_equity = cash
            for held_symbol, held_position in positions.items():
                held_row = get_price_row(price_lookup, date_value, held_symbol)
                held_open = held_row.get("open") if held_row is not None else held_position.get("last_price")
                open_equity += float(held_position["shares"]) * float(held_open or 0)
            gross_value = min(cash, open_equity * slot_fraction)
            if gross_value <= 0:
                continue
            shares = gross_value * (1.0 - buy_cost_rate) / entry_open
            cash -= gross_value
            positions[symbol] = {
                "symbol": symbol,
                "name": signal.get("name"),
                "shares": shares,
                "entry_price": entry_open,
                "invested_cash": gross_value,
                "entry_date": date_value,
                "exit_date": signal.get("exit_date"),
                "signal_date": signal.get("trade_date"),
                "signal_score": signal.get("signal_score"),
                "last_price": entry_open,
            }
            trade_rows.append(
                {
                    "trade_date": date_value,
                    "symbol": symbol,
                    "name": signal.get("name"),
                    "side": "BUY",
                    "price": entry_open,
                    "gross_value": gross_value,
                    "cash_after_cost": gross_value * (1.0 - buy_cost_rate),
                    "signal_date": signal.get("trade_date"),
                    "entry_date": date_value,
                    "exit_date": signal.get("exit_date"),
                    "holding_days": 0,
                    "signal_score": signal.get("signal_score"),
                    "entry_gap_pct": entry_gap * 100.0,
                    "reason": "signal",
                }
            )

        position_value = 0.0
        for symbol, position in positions.items():
            price_row = get_price_row(price_lookup, date_value, symbol)
            close_price = price_row.get("close") if price_row is not None else position.get("last_price")
            close_price = float(close_price or 0)
            if close_price > 0:
                position["last_price"] = close_price
            position_value += float(position["shares"]) * close_price
        equity = cash + position_value
        exposure = position_value / equity if equity > 0 else 0.0
        equity_rows.append(
            {
                "trade_date": date_value,
                "equity": equity,
                "cash": cash,
                "position_value": position_value,
                "exposure": exposure,
                "positions": len(positions),
            }
        )

    equity_df = pd.DataFrame(equity_rows)
    trades_df = pd.DataFrame(trade_rows)
    benchmark_equity = build_benchmark_equity(benchmark_df, analysis_start, analysis_end)
    strategy_summary = summarize_equity(equity_df[["trade_date", "equity"]])
    benchmark_summary = summarize_equity(benchmark_equity, prefix="benchmark_")

    sell_trades = trades_df[trades_df["side"] == "SELL"].copy() if not trades_df.empty else pd.DataFrame()
    trade_returns = pd.to_numeric(sell_trades.get("trade_return_pct", pd.Series(dtype=float)), errors="coerce").dropna()
    week_count = max(len(equity_df) / 5.0, 1.0)
    summary = {
        **strategy_summary,
        **benchmark_summary,
        "alpha_total_return_pct": (
            round(strategy_summary["total_return_pct"] - benchmark_summary["benchmark_total_return_pct"], 4)
            if strategy_summary.get("total_return_pct") is not None
            and benchmark_summary.get("benchmark_total_return_pct") is not None
            else None
        ),
        "signal_count": int(len(signals)),
        "signal_days": int(signals["trade_date"].nunique()) if not signals.empty else 0,
        "trade_count": int(len(trades_df)),
        "buy_count": int((trades_df["side"] == "BUY").sum()) if not trades_df.empty else 0,
        "sell_count": int((trades_df["side"] == "SELL").sum()) if not trades_df.empty else 0,
        "trade_win_rate_pct": round(float((trade_returns > 0).mean()) * 100.0, 2) if len(trade_returns) else None,
        "avg_trade_return_pct": round(float(trade_returns.mean()), 4) if len(trade_returns) else None,
        "median_trade_return_pct": round(float(trade_returns.median()), 4) if len(trade_returns) else None,
        "avg_exposure_pct": round(float(equity_df["exposure"].mean()) * 100.0, 4) if not equity_df.empty else None,
        "max_exposure_pct": round(float(equity_df["exposure"].max()) * 100.0, 4) if not equity_df.empty else None,
        "avg_positions": round(float(equity_df["positions"].mean()), 4) if not equity_df.empty else None,
        "max_positions_seen": int(equity_df["positions"].max()) if not equity_df.empty else 0,
        "trades_per_week": round(float((trades_df["side"] == "BUY").sum()) / week_count, 4) if not trades_df.empty else 0.0,
    }
    return equity_df, trades_df, summary


def condition_funnel(filtered_df: pd.DataFrame, analysis_start: pd.Timestamp, analysis_end: pd.Timestamp) -> List[Dict[str, Any]]:
    working = filtered_df[(filtered_df["trade_date"] >= analysis_start) & (filtered_df["trade_date"] <= analysis_end)]
    columns = [column for column in working.columns if column.startswith("pass_through_")]
    result: List[Dict[str, Any]] = []
    for column in columns:
        mask = working[column].fillna(False)
        result.append(
            {
                "step": column.replace("pass_through_", ""),
                "rows": int(mask.sum()),
                "days": int(working.loc[mask, "trade_date"].nunique()),
                "symbols": int(working.loc[mask, "symbol"].nunique()),
            }
        )
    return result


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if pd.isna(value):
        return None
    raise TypeError(f"Unsupported JSON value: {value!r}")


def export_columns() -> List[str]:
    return [
        "trade_date",
        "symbol",
        "name",
        "industry",
        "close",
        "pct_chg",
        "ret_3d",
        "ret_5d",
        "ret_20d",
        "relative_strength_5d",
        "amount",
        "amount_ratio_5_20",
        "amount_z20",
        "turnover_rate",
        "money_strength_5",
        "volatility_10d",
        "price_position_20",
        "pullback_from_10d_high",
        "close_position",
        "activity_rank",
        "momentum_rank",
        "money_rank",
        "volatility_preference",
        "pullback_score",
        "liquidity_score",
        "signal_score",
        "daily_rank",
        "benchmark_ret_5d",
        "market_timing_pass",
    ]


def main() -> None:
    config = parse_args()
    analysis_end = config.end_date or latest_available_end(config)
    fetch_start = config.start_date - pd.Timedelta(days=config.warmup_days)
    fetch_end = analysis_end + pd.Timedelta(days=config.hold_days + 5)

    price_df = load_chinext_prices(config, fetch_start, fetch_end)
    benchmark_df = load_benchmark(config, fetch_start, fetch_end)
    feature_df = add_features(config, price_df, benchmark_df)
    scored_df = add_signal_scores(config, feature_df)
    filtered_df = add_filter_columns(config, scored_df, config.start_date)
    signals = select_daily_signals(config, filtered_df, analysis_end)
    equity_df, trades_df, portfolio_summary = backtest_portfolio(
        config,
        filtered_df,
        signals,
        benchmark_df,
        config.start_date,
        analysis_end,
    )

    signal_export_cols = [column for column in export_columns() if column in signals.columns]
    ensure_parent(config.output_json)
    ensure_parent(config.trades_csv)
    ensure_parent(config.signals_csv)
    ensure_parent(config.equity_csv)
    signals[signal_export_cols].to_csv(config.signals_csv, index=False)
    trades_df.to_csv(config.trades_csv, index=False)
    equity_df.to_csv(config.equity_csv, index=False)

    result = {
        "source_interpretation": {
            "post": "ChiNext-only, ultra-short-term, sell next day, max two holdings, trade only when signal appears.",
            "comment": "Find recently abnormal movers, then use multiple factors for next-day low-buy/high-sell.",
            "image": "The shared account screenshot shows low average exposure, modest win rate, and many small overnight-style trades.",
        },
        "limits": [
            "No minute bars, order-book flow, or QMT intraday execution records were available locally.",
            "Daily amount/turnover/relative strength are proxies for fund-flow and abnormal-activity factors.",
            "Signals are formed after close and executed at next open; exit is the following open to avoid lookahead.",
        ],
        "config": {
            **asdict(config),
            "start_date": config.start_date.date().isoformat(),
            "end_date": analysis_end.date().isoformat(),
        },
        "data": {
            "symbols": int(price_df["symbol"].nunique()),
            "price_rows": int(len(price_df)),
            "analysis_days": int(len(equity_df)),
            "benchmark": config.benchmark_symbol,
        },
        "condition_funnel": condition_funnel(filtered_df, config.start_date, analysis_end),
        "portfolio_summary": portfolio_summary,
        "latest_signals": signals[signal_export_cols].tail(20).to_dict("records"),
        "artifacts": {
            "json": config.output_json,
            "signals_csv": config.signals_csv,
            "trades_csv": config.trades_csv,
            "equity_csv": config.equity_csv,
        },
    }
    with open(config.output_json, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=json_default)

    print(f"wrote {config.output_json}")
    print(f"wrote {config.signals_csv}")
    print(f"wrote {config.trades_csv}")
    print(f"wrote {config.equity_csv}")
    print(json.dumps(result["portfolio_summary"], ensure_ascii=False, sort_keys=True))
    print(pd.DataFrame(result["condition_funnel"]).to_string(index=False))


if __name__ == "__main__":
    main()
