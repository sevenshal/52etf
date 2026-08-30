#!/usr/bin/env python3
"""Analyze recent A-share volatility shape and volatility-scaled take-profit levels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats


DEFAULT_DATABASE = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"


def _distribution_stats(values: pd.Series, label: str) -> dict:
    x = values.dropna().to_numpy(dtype=float)
    normal = stats.normaltest(x)
    jb = stats.jarque_bera(x)
    return {
        "screen": label,
        "stock_count": len(x),
        "mean_ann_vol_pct": x.mean() * 100,
        "std_ann_vol_pct": x.std(ddof=1) * 100,
        "median_ann_vol_pct": np.median(x) * 100,
        "p10_ann_vol_pct": np.quantile(x, 0.10) * 100,
        "p25_ann_vol_pct": np.quantile(x, 0.25) * 100,
        "p75_ann_vol_pct": np.quantile(x, 0.75) * 100,
        "p90_ann_vol_pct": np.quantile(x, 0.90) * 100,
        "skewness": stats.skew(x, bias=False),
        "excess_kurtosis": stats.kurtosis(x, bias=False),
        "dagostino_k2": normal.statistic,
        "dagostino_p": normal.pvalue,
        "jarque_bera": jb.statistic,
        "jarque_bera_p": jb.pvalue,
    }


def analyze(database: str = DEFAULT_DATABASE, output_dir: str | Path | None = None) -> dict:
    con = duckdb.connect(database, read_only=True)
    end_date = con.execute("SELECT max(trade_date) FROM a_stock_market_daily").fetchone()[0]
    start_date = con.execute(
        """SELECT min(trade_date) FROM (
               SELECT DISTINCT trade_date FROM a_stock_market_daily
               WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 63
           )""",
        [end_date],
    ).fetchone()[0]

    cross_section_sql = """
    WITH calendar AS (
        SELECT count(DISTINCT trade_date) AS n
        FROM a_stock_market_daily WHERE trade_date BETWEEN ? AND ?
    ), eligible AS (
        SELECT m.ts_code
        FROM a_stock_market_daily m JOIN a_stock_basic b USING (ts_code)
        WHERE m.trade_date = ? AND b.list_status = 'L'
          AND m.total_mv >= 500000 AND {liquidity_filter} AND m.vol > 0
          AND upper(coalesce(b.name, '')) NOT LIKE '%ST%'
          AND NOT EXISTS (
              SELECT 1 FROM a_stock_name_changes nc
              WHERE nc.ts_code = m.ts_code
                AND upper(coalesce(nc.name, '')) LIKE '%ST%'
                AND coalesce(nc.start_date, DATE '1900-01-01') <= ?
                AND coalesce(nc.end_date, DATE '2999-12-31') >= ?
          )
    ), prices AS (
        SELECT d.ts_code, d.trade_date, d.close,
               lag(d.close) OVER (PARTITION BY d.ts_code ORDER BY d.trade_date) AS prev_close
        FROM a_stock_market_daily_qfq d JOIN eligible e USING (ts_code)
        WHERE d.trade_date BETWEEN ? AND ? AND d.close > 0 AND d.vol > 0
    ), aggregated AS (
        SELECT ts_code, count(*) AS return_count,
               stddev_samp(ln(close / prev_close)) * sqrt(252) AS annualized_volatility
        FROM prices WHERE prev_close > 0 GROUP BY ts_code
    )
    SELECT * FROM aggregated
    WHERE return_count >= (SELECT ceil(n * 0.9) - 1 FROM calendar)
    """
    params = [start_date, end_date, end_date, end_date, start_date, start_date, end_date]
    amount_screen = con.execute(
        cross_section_sql.format(liquidity_filter="m.amount >= 50000"), params
    ).df()
    shares_screen = con.execute(
        cross_section_sql.format(liquidity_filter="m.vol >= 500000"), params
    ).df()

    distributions = pd.DataFrame(
        [
            _distribution_stats(amount_screen["annualized_volatility"], "成交额≥5000万元"),
            _distribution_stats(shares_screen["annualized_volatility"], "成交股数≥5000万股"),
        ]
    )

    history_start = con.execute(
        """SELECT min(trade_date) FROM (
               SELECT DISTINCT trade_date FROM a_stock_market_daily
               WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 105
           )""",
        [end_date],
    ).fetchone()[0]
    path_sql = """
    SELECT q.ts_code, q.trade_date, q.close, q.high,
           m.amount, m.vol, m.total_mv,
           CASE WHEN upper(coalesce(b.name, '')) LIKE '%ST%'
                  OR EXISTS (
                      SELECT 1 FROM a_stock_name_changes nc
                      WHERE nc.ts_code = q.ts_code
                        AND upper(coalesce(nc.name, '')) LIKE '%ST%'
                        AND coalesce(nc.start_date, DATE '1900-01-01') <= q.trade_date
                        AND coalesce(nc.end_date, DATE '2999-12-31') >= q.trade_date
                  ) THEN 1 ELSE 0 END AS is_st
    FROM a_stock_market_daily_qfq q
    JOIN a_stock_market_daily m USING (ts_code, trade_date)
    JOIN a_stock_basic b USING (ts_code)
    WHERE q.trade_date BETWEEN ? AND ? AND b.list_status = 'L'
      AND q.close > 0 AND q.high > 0
    """
    paths = con.execute(path_sql, [history_start, end_date]).df().sort_values(
        ["ts_code", "trade_date"]
    )
    grouped = paths.groupby("ts_code", group_keys=False)
    paths["log_return"] = grouped["close"].transform(lambda x: np.log(x / x.shift()))
    paths["sigma20"] = grouped["log_return"].transform(
        lambda x: x.rolling(20, min_periods=15).std()
    )
    eligible = (
        (paths["trade_date"] >= pd.Timestamp(start_date))
        & (paths["amount"] >= 50000)
        & (paths["total_mv"] >= 500000)
        & (paths["vol"] > 0)
        & (paths["is_st"] == 0)
        & paths["sigma20"].notna()
    )

    hit_rows: list[dict] = []
    mfe_rows: list[dict] = []
    for horizon in (5, 10, 20):
        future_high = grouped["high"].transform(
            lambda x, h=horizon: x.shift(-1).iloc[::-1].rolling(h, min_periods=h).max().iloc[::-1]
        )
        mfe = future_high / paths["close"] - 1
        scaled = mfe / (paths["sigma20"] * np.sqrt(horizon))
        valid = eligible & mfe.notna() & np.isfinite(scaled)
        mfe_valid = mfe[valid]
        scaled_valid = scaled[valid]
        mfe_rows.append(
            {
                "horizon_days": horizon,
                "sample_count": int(valid.sum()),
                **{f"mfe_p{int(q*100)}_pct": np.quantile(mfe_valid, q) * 100 for q in (0.5, 0.7, 0.75, 0.8, 0.9)},
                **{f"scaled_p{int(q*100)}": np.quantile(scaled_valid, q) for q in (0.5, 0.7, 0.75, 0.8, 0.9)},
            }
        )
        for multiple in (0.50, 0.75, 1.00, 1.25, 1.50, 2.00):
            hit_rows.append(
                {
                    "horizon_days": horizon,
                    "volatility_multiple": multiple,
                    "hit_rate_pct": (scaled_valid >= multiple).mean() * 100,
                    "sample_count": int(valid.sum()),
                }
            )

    mfe_summary = pd.DataFrame(mfe_rows)
    hit_rates = pd.DataFrame(hit_rows)
    result = {
        "database": database,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "trading_days": 63,
        "distribution_summary": distributions,
        "mfe_summary": mfe_summary,
        "hit_rates": hit_rates,
        "amount_volatility": amount_screen,
        "shares_volatility": shares_screen,
    }

    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        distributions.to_csv(out / "distribution_summary.csv", index=False)
        mfe_summary.to_csv(out / "mfe_summary.csv", index=False)
        hit_rates.to_csv(out / "take_profit_hit_rates.csv", index=False)
        amount_screen.to_csv(out / "amount_screen_volatility.csv", index=False)
        metadata = {
            "database_source": "production analytics.duckdb (read-only)",
            "start_date": str(start_date),
            "end_date": str(end_date),
            "screen": "非ST、上市、最新总市值≥50亿元、最新成交额≥5000万元、非停牌，近63日有效收益率覆盖≥90%",
            "units": {"total_mv": "万元", "amount": "千元", "vol": "手"},
            "return_metric": "前复权收盘价对数收益率",
            "volatility_metric": "日收益率样本标准差×sqrt(252)",
            "take_profit_metric": "未来H日最高价相对入场收盘价的最大有利波动；阈值=k×20日波动率×sqrt(H)",
        }
        (out / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = analyze(args.database, args.output_dir)
    print(result["distribution_summary"].to_string(index=False))
    print(result["mfe_summary"].to_string(index=False))
    print(result["hit_rates"].to_string(index=False))


if __name__ == "__main__":
    main()
