from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import duckdb
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StrategyConfig:
    pool: str
    sqlite_path: str
    duckdb_path: str
    index_code: str
    start_date: pd.Timestamp
    end_date: Optional[pd.Timestamp]
    warmup_days: int
    max_positions: int
    max_hold_days: int
    cost_bps: float
    stop_loss_pct: float
    min_listing_days: int
    gain_min_pct: float
    gain_max_pct: float
    volume_ratio_window: int
    min_volume_ratio: float
    turnover_min_pct: float
    turnover_max_pct: float
    market_cap_min_yi: float
    market_cap_max_yi: float
    volume_expansion_days: int
    volume_cv_window: int
    volume_cv_max: float
    ma_long_window: int
    ma_long_slope_days: int
    breakout_window: int
    breakout_tolerance_pct: float
    close_position_min: float
    output_json: str
    trades_csv: str
    signals_csv: str


def parse_args() -> StrategyConfig:
    parser = argparse.ArgumentParser(
        description="Backtest an A-stock tail momentum/liquidity proxy strategy."
    )
    parser.add_argument("--pool", choices=["INNO100", "ALL_A_STOCK"], default="INNO100")
    parser.add_argument("--sqlite-path", default=os.getenv("QUANT_SQLITE_PATH", "/var/lib/quant_robot/evc_stocks.db"))
    parser.add_argument("--duckdb-path", default=os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb"))
    parser.add_argument("--index-code", default="INNO100.CN")
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--warmup-days", type=int, default=260)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--max-hold-days", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.05)
    parser.add_argument("--min-listing-days", type=int, default=0)
    parser.add_argument("--gain-min-pct", type=float, default=3.0)
    parser.add_argument("--gain-max-pct", type=float, default=5.0)
    parser.add_argument("--volume-ratio-window", type=int, default=20)
    parser.add_argument("--min-volume-ratio", type=float, default=1.0)
    parser.add_argument("--turnover-min-pct", type=float, default=5.0)
    parser.add_argument("--turnover-max-pct", type=float, default=10.0)
    parser.add_argument("--market-cap-min-yi", type=float, default=50.0)
    parser.add_argument("--market-cap-max-yi", type=float, default=200.0)
    parser.add_argument("--volume-expansion-days", type=int, default=3)
    parser.add_argument("--volume-cv-window", type=int, default=5)
    parser.add_argument("--volume-cv-max", type=float, default=1.2)
    parser.add_argument("--ma-long-window", type=int, default=60)
    parser.add_argument("--ma-long-slope-days", type=int, default=10)
    parser.add_argument("--breakout-window", type=int, default=20)
    parser.add_argument("--breakout-tolerance-pct", type=float, default=0.5)
    parser.add_argument("--close-position-min", type=float, default=0.8)
    parser.add_argument("--output-json", default="/private/tmp/inno100_tail_momentum_backtest.json")
    parser.add_argument("--trades-csv", default="/private/tmp/inno100_tail_momentum_trades.csv")
    parser.add_argument("--signals-csv", default="/private/tmp/inno100_tail_momentum_signals.csv")
    args = parser.parse_args()
    return StrategyConfig(
        pool=str(args.pool).strip().upper(),
        sqlite_path=args.sqlite_path,
        duckdb_path=args.duckdb_path,
        index_code=args.index_code,
        start_date=pd.Timestamp(args.start_date).normalize(),
        end_date=pd.Timestamp(args.end_date).normalize() if args.end_date else None,
        warmup_days=max(80, int(args.warmup_days)),
        max_positions=max(1, int(args.max_positions)),
        max_hold_days=max(1, int(args.max_hold_days)),
        cost_bps=max(0.0, float(args.cost_bps)),
        stop_loss_pct=max(0.0, float(args.stop_loss_pct)),
        min_listing_days=max(0, int(args.min_listing_days)),
        gain_min_pct=float(args.gain_min_pct),
        gain_max_pct=float(args.gain_max_pct),
        volume_ratio_window=max(2, int(args.volume_ratio_window)),
        min_volume_ratio=float(args.min_volume_ratio),
        turnover_min_pct=float(args.turnover_min_pct),
        turnover_max_pct=float(args.turnover_max_pct),
        market_cap_min_yi=float(args.market_cap_min_yi),
        market_cap_max_yi=float(args.market_cap_max_yi),
        volume_expansion_days=max(1, int(args.volume_expansion_days)),
        volume_cv_window=max(2, int(args.volume_cv_window)),
        volume_cv_max=float(args.volume_cv_max),
        ma_long_window=max(20, int(args.ma_long_window)),
        ma_long_slope_days=max(1, int(args.ma_long_slope_days)),
        breakout_window=max(5, int(args.breakout_window)),
        breakout_tolerance_pct=max(0.0, float(args.breakout_tolerance_pct)),
        close_position_min=float(args.close_position_min),
        output_json=args.output_json,
        trades_csv=args.trades_csv,
        signals_csv=args.signals_csv,
    )


def quote_sql_string(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def normalize_date(value) -> pd.Timestamp:
    return pd.Timestamp(value).normalize()


def load_inno100_snapshots(
    config: StrategyConfig,
    analysis_end: pd.Timestamp,
) -> Tuple[List[pd.Timestamp], Dict[pd.Timestamp, List[str]], List[str]]:
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
            params=[config.index_code, analysis_end.date().isoformat()],
        )
        if rebalances.empty:
            raise RuntimeError(f"{config.index_code} has no rebalance snapshots")

        rebalances["snapshot_date"] = pd.to_datetime(rebalances["snapshot_date"]).dt.normalize()
        selected = pd.concat(
            [
                rebalances[rebalances["snapshot_date"] < config.start_date].tail(1),
                rebalances[
                    (rebalances["snapshot_date"] >= config.start_date)
                    & (rebalances["snapshot_date"] <= analysis_end)
                ],
            ],
            ignore_index=True,
        ).drop_duplicates("snapshot_date", keep="last")
        selected = selected.sort_values("snapshot_date")
        id_to_date = dict(zip(selected["id"].astype(int), selected["snapshot_date"]))
        if not id_to_date:
            raise RuntimeError("No usable rebalance snapshots for selected range")

        placeholders = ",".join(["?"] * len(id_to_date))
        constituents = pd.read_sql_query(
            f"""
            SELECT rebalance_id, ts_code
            FROM a_stock_innovation100_constituents
            WHERE index_code = ?
              AND rebalance_id IN ({placeholders})
            ORDER BY rebalance_id, rank
            """,
            conn,
            params=[config.index_code, *list(id_to_date)],
        )

    symbols_by_date: Dict[pd.Timestamp, List[str]] = {}
    all_symbols: List[str] = []
    seen: Set[str] = set()
    for row in constituents.itertuples(index=False):
        snapshot_date = id_to_date.get(int(row.rebalance_id))
        symbol = str(row.ts_code or "").strip().upper()
        if snapshot_date is None or not symbol:
            continue
        symbols_by_date.setdefault(snapshot_date, []).append(symbol)
        if symbol not in seen:
            seen.add(symbol)
            all_symbols.append(symbol)
    return sorted(symbols_by_date), symbols_by_date, all_symbols


def latest_available_end(config: StrategyConfig) -> pd.Timestamp:
    with duckdb.connect(config.duckdb_path, read_only=True) as duck_conn:
        market_row = duck_conn.execute("SELECT MAX(trade_date) FROM a_stock_market_daily_qfq").fetchone()
    if config.pool == "ALL_A_STOCK":
        if not market_row or market_row[0] is None:
            raise RuntimeError("No available market end date")
        return normalize_date(market_row[0])

    with sqlite3.connect(config.sqlite_path) as sqlite_conn:
        level_row = sqlite_conn.execute(
            """
            SELECT MAX(date)
            FROM a_stock_innovation100_levels
            WHERE index_code = ?
            """,
            [config.index_code],
        ).fetchone()
    candidates = [row[0] for row in [level_row, market_row] if row and row[0] is not None]
    if not candidates:
        raise RuntimeError("No available market end date")
    return min(normalize_date(item) for item in candidates)


def load_prices(
    config: StrategyConfig,
    symbols: Sequence[str],
    fetch_start: pd.Timestamp,
    fetch_end: pd.Timestamp,
) -> pd.DataFrame:
    if not symbols:
        raise RuntimeError("No symbols available for price loading")
    symbol_sql = ", ".join(quote_sql_string(symbol) for symbol in symbols)
    query = f"""
        SELECT
            ts_code AS symbol,
            CAST(trade_date AS DATE) AS trade_date,
            CAST(open AS DOUBLE) AS open,
            CAST(high AS DOUBLE) AS high,
            CAST(low AS DOUBLE) AS low,
            CAST(close AS DOUBLE) AS close,
            CAST(pct_chg AS DOUBLE) AS pct_chg,
            CAST(vol AS DOUBLE) AS volume,
            CAST(amount AS DOUBLE) AS amount,
            CAST(total_mv AS DOUBLE) AS total_mv,
            CAST(circ_mv AS DOUBLE) AS circ_mv,
            CAST(turnover_rate AS DOUBLE) AS turnover_rate
        FROM a_stock_market_daily_qfq
        WHERE ts_code IN ({symbol_sql})
          AND trade_date BETWEEN ? AND ?
          AND close IS NOT NULL
          AND close > 0
        ORDER BY ts_code, trade_date
    """
    with duckdb.connect(config.duckdb_path, read_only=True) as conn:
        df = conn.execute(query, [fetch_start.date(), fetch_end.date()]).df()
    if df.empty:
        raise RuntimeError("No daily price rows found")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    return df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def load_all_a_stock_prices(
    config: StrategyConfig,
    fetch_start: pd.Timestamp,
    fetch_end: pd.Timestamp,
) -> pd.DataFrame:
    query = """
        SELECT
            p.ts_code AS symbol,
            CAST(p.trade_date AS DATE) AS trade_date,
            CAST(p.open AS DOUBLE) AS open,
            CAST(p.high AS DOUBLE) AS high,
            CAST(p.low AS DOUBLE) AS low,
            CAST(p.close AS DOUBLE) AS close,
            CAST(p.pct_chg AS DOUBLE) AS pct_chg,
            CAST(p.vol AS DOUBLE) AS volume,
            CAST(p.amount AS DOUBLE) AS amount,
            CAST(p.total_mv AS DOUBLE) AS total_mv,
            CAST(p.circ_mv AS DOUBLE) AS circ_mv,
            CAST(p.turnover_rate AS DOUBLE) AS turnover_rate
        FROM a_stock_market_daily_qfq p
        JOIN a_stock_basic b ON p.ts_code = b.ts_code
        WHERE p.trade_date BETWEEN ? AND ?
          AND p.close IS NOT NULL
          AND p.close > 0
          AND b.exchange IN ('SSE', 'SZSE')
          AND b.list_date IS NOT NULL
          AND date_diff('day', b.list_date, p.trade_date) >= ?
          AND (b.delist_date IS NULL OR b.delist_date > p.trade_date)
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
        ORDER BY p.ts_code, p.trade_date
    """
    with duckdb.connect(config.duckdb_path, read_only=True) as conn:
        df = conn.execute(query, [fetch_start.date(), fetch_end.date(), config.min_listing_days]).df()
    if df.empty:
        raise RuntimeError("No all-A-stock daily price rows found")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    return df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def load_benchmark(config: StrategyConfig, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    with sqlite3.connect(config.sqlite_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT date AS trade_date, level, daily_return_pct
            FROM a_stock_innovation100_levels
            WHERE index_code = ?
              AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            conn,
            params=[config.index_code, start_date.date().isoformat(), end_date.date().isoformat()],
        )
    if df.empty:
        raise RuntimeError("No benchmark rows found")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df["index_ret_5d"] = df["level"] / df["level"].shift(5) - 1
    return df


def build_equal_weight_benchmark(price_df: pd.DataFrame, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    daily = (
        price_df[(price_df["trade_date"] >= start_date) & (price_df["trade_date"] <= end_date)]
        .groupby("trade_date", as_index=False)["pct_chg"]
        .mean()
        .rename(columns={"pct_chg": "daily_return_pct"})
        .sort_values("trade_date")
    )
    if daily.empty:
        raise RuntimeError("No rows available for all-A-stock equal-weight benchmark")
    daily["level"] = (1 + daily["daily_return_pct"].fillna(0) / 100.0).cumprod() * 1000.0
    daily["index_ret_5d"] = daily["level"] / daily["level"].shift(5) - 1
    return daily


def build_universe_membership(
    trading_dates: Sequence[pd.Timestamp],
    snapshot_dates: Sequence[pd.Timestamp],
    symbols_by_date: Dict[pd.Timestamp, List[str]],
) -> pd.DataFrame:
    records: List[Dict[str, object]] = []
    pointer = -1
    for current_date in trading_dates:
        while pointer + 1 < len(snapshot_dates) and snapshot_dates[pointer + 1] <= current_date:
            pointer += 1
        if pointer < 0:
            continue
        snapshot_date = snapshot_dates[pointer]
        for symbol in symbols_by_date.get(snapshot_date, []):
            records.append({"trade_date": current_date, "symbol": symbol, "in_universe": True})
    return pd.DataFrame(records)


def add_features(config: StrategyConfig, price_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> pd.DataFrame:
    df = price_df.copy()
    grouped = df.groupby("symbol", sort=False)
    df["volume_ma_prev"] = grouped["volume"].transform(
        lambda s: s.shift(1).rolling(config.volume_ratio_window, min_periods=config.volume_ratio_window).mean()
    )
    df["volume_ratio"] = df["volume"] / df["volume_ma_prev"]
    df["volume_cv"] = grouped["volume"].transform(
        lambda s: s.rolling(config.volume_cv_window, min_periods=config.volume_cv_window).std()
        / s.rolling(config.volume_cv_window, min_periods=config.volume_cv_window).mean()
    )
    volume_up = pd.Series(True, index=df.index)
    for offset in range(config.volume_expansion_days - 1):
        volume_up &= grouped["volume"].shift(offset).gt(grouped["volume"].shift(offset + 1))
    df["volume_expanding"] = volume_up

    for window in [5, 10, 20, config.ma_long_window]:
        df[f"ma{window}"] = grouped["close"].transform(lambda s, w=window: s.rolling(w, min_periods=w).mean())
    df["ma_long_lag"] = grouped[f"ma{config.ma_long_window}"].shift(config.ma_long_slope_days)
    df["ma_long_rising"] = df[f"ma{config.ma_long_window}"] > df["ma_long_lag"]
    df["ret_5d"] = grouped["close"].transform(lambda s: s / s.shift(5) - 1)
    df["rolling_high"] = grouped["close"].transform(
        lambda s: s.rolling(config.breakout_window, min_periods=config.breakout_window).max()
    )
    intraday_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_position"] = ((df["close"] - df["low"]) / intraday_range).clip(lower=0, upper=1)
    df["close_position"] = df["close_position"].fillna(1.0)

    benchmark_cols = benchmark_df[["trade_date", "daily_return_pct", "index_ret_5d"]].rename(
        columns={"daily_return_pct": "benchmark_pct_chg"}
    )
    df = df.merge(benchmark_cols, on="trade_date", how="left")
    df["relative_strength_1d"] = df["pct_chg"] - df["benchmark_pct_chg"]
    df["relative_strength_5d"] = df["ret_5d"] - df["index_ret_5d"]
    return df


def add_signal_columns(config: StrategyConfig, feature_df: pd.DataFrame, membership_df: pd.DataFrame) -> pd.DataFrame:
    df = feature_df.merge(membership_df, on=["trade_date", "symbol"], how="left")
    df["in_universe"] = df["in_universe"].eq(True)

    total_mv_min = config.market_cap_min_yi * 10000.0
    total_mv_max = config.market_cap_max_yi * 10000.0
    breakout_floor = df["rolling_high"] * (1 - config.breakout_tolerance_pct / 100.0)

    conditions = [
        ("in_universe", df["in_universe"]),
        ("gain_3_5", df["pct_chg"].between(config.gain_min_pct, config.gain_max_pct, inclusive="both")),
        ("volume_ratio_ge_1", df["volume_ratio"] >= config.min_volume_ratio),
        ("turnover_5_10", df["turnover_rate"].between(config.turnover_min_pct, config.turnover_max_pct, inclusive="both")),
        ("market_cap_range", df["total_mv"].between(total_mv_min, total_mv_max, inclusive="both")),
        ("volume_expanding_stable", df["volume_expanding"] & (df["volume_cv"] <= config.volume_cv_max)),
        (
            "ma_alignment",
            (df["close"] > df["ma5"])
            & (df["ma5"] > df["ma10"])
            & (df["ma10"] > df["ma20"])
            & (df["close"] > df[f"ma{config.ma_long_window}"])
            & df["ma_long_rising"],
        ),
        ("relative_strength", (df["relative_strength_1d"] > 0) & (df["relative_strength_5d"] > 0)),
        ("tail_breakout", (df["close"] >= breakout_floor) & (df["close_position"] >= config.close_position_min)),
    ]
    cumulative = pd.Series(True, index=df.index)
    for name, condition in conditions:
        df[f"pass_{name}"] = condition.fillna(False)
        cumulative &= df[f"pass_{name}"]
        df[f"pass_through_{name}"] = cumulative
    df["signal"] = cumulative
    df["signal_score"] = (
        df["volume_ratio"].clip(0, 5).fillna(0) * 0.35
        + df["relative_strength_5d"].fillna(0) * 100 * 0.35
        + df["close_position"].fillna(0) * 0.30
    )
    return df


def build_price_lookup(feature_df: pd.DataFrame) -> pd.DataFrame:
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


def compute_signal_forward_returns(
    signals: pd.DataFrame,
    feature_df: pd.DataFrame,
    trading_dates: Sequence[pd.Timestamp],
    horizons: Sequence[int] = (1, 3, 5, 10),
) -> Dict[str, object]:
    date_index = {date_value: idx for idx, date_value in enumerate(trading_dates)}
    open_lookup = feature_df.set_index(["trade_date", "symbol"])["open"].sort_index()
    rows: List[Dict[str, object]] = []
    for row in signals.itertuples(index=False):
        signal_idx = date_index.get(row.trade_date)
        if signal_idx is None or signal_idx + 1 >= len(trading_dates):
            continue
        entry_date = trading_dates[signal_idx + 1]
        try:
            entry_open = open_lookup.loc[(entry_date, row.symbol)]
        except KeyError:
            entry_open = None
        if not entry_open or not math.isfinite(float(entry_open)) or float(entry_open) <= 0:
            continue
        item = {"signal_date": row.trade_date, "symbol": row.symbol, "entry_date": entry_date}
        for horizon in horizons:
            exit_idx = signal_idx + 1 + int(horizon)
            if exit_idx >= len(trading_dates):
                item[f"ret_{horizon}d_pct"] = None
                continue
            try:
                exit_open = open_lookup.loc[(trading_dates[exit_idx], row.symbol)]
            except KeyError:
                exit_open = None
            item[f"ret_{horizon}d_pct"] = (
                (float(exit_open) / float(entry_open) - 1) * 100
                if exit_open and math.isfinite(float(exit_open)) and float(exit_open) > 0
                else None
            )
        rows.append(item)
    forward_df = pd.DataFrame(rows)
    summary: Dict[str, object] = {"events": int(len(forward_df))}
    if not forward_df.empty:
        for horizon in horizons:
            col = f"ret_{horizon}d_pct"
            values = forward_df[col].dropna()
            summary[col] = {
                "count": int(values.count()),
                "mean_pct": round(float(values.mean()), 4) if not values.empty else None,
                "median_pct": round(float(values.median()), 4) if not values.empty else None,
                "positive_rate_pct": round(float((values > 0).mean() * 100), 2) if not values.empty else None,
            }
    return {"summary": summary, "rows": rows}


def annualized_return(equity: pd.Series) -> float:
    if len(equity) < 2:
        return float("nan")
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
    years = len(equity) / 252.0
    if years <= 0 or equity.iloc[0] <= 0 or equity.iloc[-1] <= 0:
        return float("nan")
    return (1 + total_return) ** (1 / years) - 1


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    return float(drawdown.min())


def sharpe_ratio(equity: pd.Series) -> float:
    returns = equity.pct_change().dropna()
    std = returns.std(ddof=1)
    if returns.empty or not std or not math.isfinite(float(std)) or std <= 0:
        return float("nan")
    return float(returns.mean() / std * math.sqrt(252))


def summarize_equity(equity_df: pd.DataFrame, prefix: str = "") -> Dict[str, object]:
    equity = equity_df["equity"].dropna()
    daily_returns = equity.pct_change().dropna()
    return {
        f"{prefix}start": equity_df["trade_date"].iloc[0].date().isoformat() if not equity_df.empty else None,
        f"{prefix}end": equity_df["trade_date"].iloc[-1].date().isoformat() if not equity_df.empty else None,
        f"{prefix}days": int(len(equity)),
        f"{prefix}total_return_pct": round(float((equity.iloc[-1] / equity.iloc[0] - 1) * 100), 4) if len(equity) else None,
        f"{prefix}annualized_return_pct": round(float(annualized_return(equity) * 100), 4) if len(equity) else None,
        f"{prefix}max_drawdown_pct": round(float(max_drawdown(equity) * 100), 4) if len(equity) else None,
        f"{prefix}sharpe": round(float(sharpe_ratio(equity)), 4) if len(equity) else None,
        f"{prefix}positive_day_rate_pct": round(float((daily_returns > 0).mean() * 100), 2) if len(daily_returns) else None,
    }


def backtest_portfolio(
    config: StrategyConfig,
    feature_df: pd.DataFrame,
    signals: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    trading_dates: Sequence[pd.Timestamp],
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object]]:
    price_lookup = build_price_lookup(feature_df)
    date_index = {date_value: index for index, date_value in enumerate(trading_dates)}
    signal_map: Dict[pd.Timestamp, List[dict]] = {}
    for row in signals.sort_values(["trade_date", "signal_score"], ascending=[True, False]).itertuples(index=False):
        signal_map.setdefault(row.trade_date, []).append(row._asdict())

    cost_rate = config.cost_bps / 10000.0
    cash = 1.0
    positions: Dict[str, dict] = {}
    pending_buys: Dict[pd.Timestamp, List[dict]] = {}
    pending_sells: Dict[pd.Timestamp, List[Tuple[str, str]]] = {}
    equity_rows: List[Dict[str, object]] = []
    trade_rows: List[Dict[str, object]] = []

    analysis_dates = [date_value for date_value in trading_dates if config.start_date <= date_value]
    for date_value in analysis_dates:
        for symbol, reason in pending_sells.pop(date_value, []):
            position = positions.pop(symbol, None)
            price_row = get_price_row(price_lookup, date_value, symbol)
            if position is None or price_row is None:
                continue
            open_price = float(price_row.get("open") or 0)
            if open_price <= 0:
                positions[symbol] = position
                continue
            gross_value = float(position["shares"]) * open_price
            cash += gross_value * (1 - cost_rate)
            trade_rows.append(
                {
                    "trade_date": date_value,
                    "symbol": symbol,
                    "side": "SELL",
                    "price": open_price,
                    "reason": reason,
                    "signal_date": position.get("signal_date"),
                    "entry_date": position.get("entry_date"),
                    "holding_days": position.get("holding_days", 0),
                    "gross_value": gross_value,
                }
            )

        for signal in pending_buys.pop(date_value, []):
            if len(positions) >= config.max_positions:
                break
            symbol = str(signal.get("symbol"))
            if symbol in positions:
                continue
            price_row = get_price_row(price_lookup, date_value, symbol)
            if price_row is None:
                continue
            open_price = float(price_row.get("open") or 0)
            entry_floor = float(signal.get("ma5") or 0)
            if open_price <= 0 or entry_floor <= 0 or open_price < entry_floor:
                continue
            open_equity = cash
            for held_symbol, position in positions.items():
                held_row = get_price_row(price_lookup, date_value, held_symbol)
                held_price = held_row.get("open") if held_row is not None else None
                open_equity += float(position["shares"]) * float(held_price or position.get("last_price", 0))
            target_value = open_equity / config.max_positions
            gross_value = min(cash, target_value)
            if gross_value <= 0:
                continue
            shares = gross_value * (1 - cost_rate) / open_price
            cash -= gross_value
            positions[symbol] = {
                "shares": shares,
                "entry_price": open_price,
                "entry_date": date_value,
                "signal_date": signal.get("trade_date"),
                "holding_days": 0,
                "last_price": open_price,
            }
            trade_rows.append(
                {
                    "trade_date": date_value,
                    "symbol": symbol,
                    "side": "BUY",
                    "price": open_price,
                    "reason": "signal",
                    "signal_date": signal.get("trade_date"),
                    "entry_date": date_value,
                    "holding_days": 0,
                    "gross_value": gross_value,
                }
            )

        close_equity = cash
        for symbol, position in list(positions.items()):
            price_row = get_price_row(price_lookup, date_value, symbol)
            if price_row is None:
                close_price = float(position.get("last_price") or 0)
            else:
                close_price = float(price_row.get("close") or 0)
                if close_price > 0:
                    position["last_price"] = close_price
            close_equity += float(position["shares"]) * close_price
            position["holding_days"] = int(position.get("holding_days", 0)) + 1

        next_idx = date_index.get(date_value, -2) + 1
        next_date = trading_dates[next_idx] if 0 <= next_idx < len(trading_dates) else None
        if next_date is not None:
            for symbol, position in list(positions.items()):
                price_row = get_price_row(price_lookup, date_value, symbol)
                if price_row is None:
                    continue
                close_price = float(price_row.get("close") or 0)
                ma10 = float(price_row.get("ma10") or 0)
                entry_price = float(position.get("entry_price") or 0)
                exit_reason = None
                if entry_price > 0 and close_price <= entry_price * (1 - config.stop_loss_pct):
                    exit_reason = "stop_loss"
                elif ma10 > 0 and close_price < ma10:
                    exit_reason = "below_ma10"
                elif int(position.get("holding_days", 0)) >= config.max_hold_days:
                    exit_reason = "max_hold"
                if exit_reason:
                    pending_sells.setdefault(next_date, []).append((symbol, exit_reason))

            day_signals = signal_map.get(date_value) or []
            available_slots = config.max_positions - len(positions)
            if day_signals and available_slots > 0:
                held_symbols = set(positions)
                candidates = [item for item in day_signals if item.get("symbol") not in held_symbols]
                pending_buys[next_date] = candidates[:available_slots]

        equity_rows.append(
            {
                "trade_date": date_value,
                "equity": close_equity,
                "cash": cash,
                "positions": len(positions),
            }
        )

    equity_df = pd.DataFrame(equity_rows)
    trades_df = pd.DataFrame(trade_rows)
    benchmark = benchmark_df[benchmark_df["trade_date"].isin(equity_df["trade_date"])].copy()
    if not benchmark.empty:
        benchmark = benchmark.sort_values("trade_date")
        benchmark["equity"] = benchmark["level"] / benchmark["level"].iloc[0]
    benchmark_summary = summarize_equity(benchmark[["trade_date", "equity"]], prefix="benchmark_") if not benchmark.empty else {}
    strategy_summary = summarize_equity(equity_df[["trade_date", "equity"]])
    summary = {
        **strategy_summary,
        **benchmark_summary,
        "alpha_total_return_pct": (
            round(strategy_summary["total_return_pct"] - benchmark_summary["benchmark_total_return_pct"], 4)
            if strategy_summary.get("total_return_pct") is not None and benchmark_summary.get("benchmark_total_return_pct") is not None
            else None
        ),
        "trade_count": int(len(trades_df)),
        "buy_count": int((trades_df["side"] == "BUY").sum()) if not trades_df.empty else 0,
        "sell_count": int((trades_df["side"] == "SELL").sum()) if not trades_df.empty else 0,
        "avg_positions": round(float(equity_df["positions"].mean()), 4) if not equity_df.empty else None,
        "max_positions": int(equity_df["positions"].max()) if not equity_df.empty else 0,
    }
    return equity_df, trades_df, summary


def condition_funnel(signals_df: pd.DataFrame) -> List[Dict[str, object]]:
    columns = [column for column in signals_df.columns if column.startswith("pass_through_")]
    result = []
    for column in columns:
        label = column.replace("pass_through_", "")
        count = int(signals_df[column].sum())
        days = int(signals_df.loc[signals_df[column], "trade_date"].nunique())
        result.append({"step": label, "rows": count, "days": days})
    return result


def json_default(value):
    if isinstance(value, (pd.Timestamp,)):
        return value.date().isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if pd.isna(value):
        return None
    raise TypeError(f"Unsupported JSON value: {value!r}")


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    config = parse_args()
    analysis_end = config.end_date or latest_available_end(config)
    fetch_start = config.start_date - pd.Timedelta(days=config.warmup_days)
    fetch_end = analysis_end + pd.Timedelta(days=max(20, config.max_hold_days * 4))

    if config.pool == "ALL_A_STOCK":
        snapshot_dates: List[pd.Timestamp] = []
        symbols_by_date: Dict[pd.Timestamp, List[str]] = {}
        price_df = load_all_a_stock_prices(config, fetch_start, fetch_end)
        symbols = sorted(price_df["symbol"].dropna().unique().tolist())
        benchmark_df = build_equal_weight_benchmark(price_df, config.start_date, fetch_end)
    else:
        snapshot_dates, symbols_by_date, symbols = load_inno100_snapshots(config, analysis_end)
        price_df = load_prices(config, symbols, fetch_start, fetch_end)
        benchmark_df = load_benchmark(config, config.start_date, fetch_end)
    feature_df = add_features(config, price_df, benchmark_df)
    trading_dates = pd.to_datetime(
        feature_df[
            (feature_df["trade_date"] >= config.start_date)
            & (feature_df["trade_date"] <= analysis_end)
        ]["trade_date"].drop_duplicates().sort_values()
    ).to_list()
    if config.pool == "ALL_A_STOCK":
        membership_df = (
            feature_df[
                (feature_df["trade_date"] >= config.start_date)
                & (feature_df["trade_date"] <= analysis_end)
            ][["trade_date", "symbol"]]
            .drop_duplicates()
            .assign(in_universe=True)
        )
    else:
        membership_df = build_universe_membership(trading_dates, snapshot_dates, symbols_by_date)
    signal_frame = add_signal_columns(config, feature_df, membership_df)
    eligible_frame = signal_frame[
        (signal_frame["trade_date"] >= config.start_date)
        & (signal_frame["trade_date"] <= analysis_end)
        & signal_frame["in_universe"]
    ].copy()
    signals = eligible_frame[eligible_frame["signal"]].sort_values(
        ["trade_date", "signal_score"],
        ascending=[True, False],
    )

    equity_df, trades_df, portfolio_summary = backtest_portfolio(
        config,
        signal_frame,
        signals,
        benchmark_df,
        trading_dates,
    )
    forward_result = compute_signal_forward_returns(signals, signal_frame, trading_dates)

    signal_export_cols = [
        "trade_date",
        "symbol",
        "pct_chg",
        "volume_ratio",
        "turnover_rate",
        "total_mv",
        "volume_cv",
        "close",
        "ma5",
        "ma10",
        "ma20",
        f"ma{config.ma_long_window}",
        "relative_strength_1d",
        "relative_strength_5d",
        "close_position",
        "signal_score",
    ]
    ensure_parent(config.output_json)
    ensure_parent(config.trades_csv)
    ensure_parent(config.signals_csv)
    signals[signal_export_cols].to_csv(config.signals_csv, index=False)
    trades_df.to_csv(config.trades_csv, index=False)

    result = {
        "config": {
            **asdict(config),
            "start_date": config.start_date.date().isoformat(),
            "end_date": analysis_end.date().isoformat(),
        },
        "data": {
            "pool": config.pool,
            "symbols": len(symbols),
            "price_rows": int(len(price_df)),
            "analysis_days": int(len(trading_dates)),
            "snapshot_count": int(len(snapshot_dates)),
        },
        "proxy_notes": [
            "No local historical 14:30/minute/order-flow table was found; pct_chg/close/high-low are daily proxies.",
            "Signal is formed after close and executed at the next trading day's open to avoid same-day lookahead.",
            "Market cap thresholds use total_mv in Tushare ten-thousand-RMB units.",
        ],
        "condition_funnel": condition_funnel(eligible_frame),
        "signal_count": int(len(signals)),
        "signal_days": int(signals["trade_date"].nunique()) if not signals.empty else 0,
        "portfolio_summary": portfolio_summary,
        "signal_forward_returns": forward_result["summary"],
        "latest_signals": signals[signal_export_cols].tail(20).to_dict("records"),
    }
    with open(config.output_json, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=json_default)

    print(f"wrote {config.output_json}")
    print(f"wrote {config.trades_csv}")
    print(f"wrote {config.signals_csv}")
    print(json.dumps(result["portfolio_summary"], ensure_ascii=False, sort_keys=True))
    print(json.dumps(result["signal_forward_returns"], ensure_ascii=False, sort_keys=True))
    print(pd.DataFrame(result["condition_funnel"]).to_string(index=False))


if __name__ == "__main__":
    main()
