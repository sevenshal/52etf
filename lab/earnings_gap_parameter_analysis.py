#!/usr/bin/env python3
"""净利润断层业绩参数回测工具。

在既定技术形态条件下生成全部历史候选信号，再按财报/快报/预告各自可提供的字段，
评估净利同比、净利环比、营收同比和营收环比阈值。分析库只读，不写生产数据。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import duckdb
import numpy as np
import pandas as pd

import earnings_gap_backtest as base


DB_PATH = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
HORIZONS = (5, 20, 90)
SOURCE_PRIORITY = {"report": 0, "express": 1, "forecast": 2}
METRIC_FILTERS = (
    ("np_yoy", "min_profit_yoy", "max_profit_yoy", {"report", "express", "forecast"}),
    ("np_qoq", "min_profit_qoq", "max_profit_qoq", {"report"}),
    ("or_yoy", "min_revenue_yoy", "max_revenue_yoy", {"report", "express"}),
    ("or_qoq", "min_revenue_qoq", "max_revenue_qoq", {"report"}),
)

LIVE_GROWTH_CONFIG = {
    "min_profit_yoy": 25.0,
    "max_profit_yoy": 3000.0,
}
RECOMMENDED_GROWTH_CONFIG = {
    "min_profit_yoy": 80.0,
    "max_profit_yoy": 1000.0,
    "min_profit_qoq": 0.0,
    "max_profit_qoq": 1000.0,
    "min_revenue_yoy": 20.0,
    "max_revenue_yoy": None,
    "min_revenue_qoq": None,
    "max_revenue_qoq": 100.0,
}


def build_signal_frame(
    db_path: str = DB_PATH,
    *,
    min_gap_pct: float = 1.5,
    min_amount_ratio: float = 1.3,
    amount_ratio_days: int = 20,
    min_amount_yuan: float = 3e7,
    min_listed_trade_days: int = 120,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """按线上技术条件生成宽松业绩范围内的历史信号与远期收益。"""
    connection = duckdb.connect(db_path, read_only=True)
    try:
        raw_events = base.load_raw_events(connection, ["report", "forecast", "express"])
        connection.register("raw_events", raw_events)
        events = connection.execute(base.EVENTS_SQL.format(
            t1_op=">", min_yoy=-1000, max_yoy=1_000_000,
        )).fetchdf()

        events = events[events["listed_days"] >= min_listed_trade_days]
        events = events[events["amount"] * 1000 >= min_amount_yuan]
        events["gap_pct"] = (events["open"] / events["pre_close"] - 1) * 100
        events = events[(events["gap_pct"] >= min_gap_pct) & (events["close"] > events["open"])].copy()
        events["sealed"] = [base.is_sealed(row) for row in events.itertuples(index=False)]
        events = events[~events["sealed"]].copy()

        adjustment_ratio = (events["t0_adj"] / events["t1_adj"]).where(
            events["t0_adj"].notna() & events["t1_adj"].notna(), 1.0,
        )
        events["t0_high_adj"] = events["t0_high"] * adjustment_ratio
        events["true_gap"] = events["low"] > events["t0_high_adj"] + 1e-9
        events = events.dropna(subset=["t1_adj"])

        connection.register("ratio_sig", events[["ts_code", "t1", "t1_idx"]])
        average_amount = connection.execute(
            """
            WITH cal AS (
                SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date) AS idx
                FROM a_stock_index_daily WHERE ts_code = '000300.SH'
            )
            SELECT s.ts_code, s.t1, AVG(m.amount) AS avg_amount, COUNT(*) AS amount_days
            FROM ratio_sig s
            JOIN cal c ON c.idx >= s.t1_idx - ? AND c.idx < s.t1_idx
            JOIN a_stock_market_daily m ON m.ts_code = s.ts_code AND m.trade_date = c.trade_date
            GROUP BY s.ts_code, s.t1
            """,
            [amount_ratio_days],
        ).fetchdf()
        events = events.merge(average_amount, on=["ts_code", "t1"], how="left")
        events["amount_ratio"] = events["amount"] / events["avg_amount"]
        if min_amount_ratio:
            events = events[
                events["amount_ratio"].isna() | (events["amount_ratio"] >= min_amount_ratio)
            ].copy()

        connection.register("sig", events[["ts_code", "t1", "t1_idx"]].drop_duplicates())
        forward = connection.execute(base.FORWARD_SQL, [list(HORIZONS)]).fetchdf()
        for horizon in HORIZONS:
            part = forward[forward["horizon"] == horizon][
                ["ts_code", "t1", "exit_hfq", "idx_t1", "idx_exit"]
            ]
            events = events.merge(part.rename(columns={
                "exit_hfq": f"exit{horizon}",
                "idx_t1": f"idx_t1_{horizon}",
                "idx_exit": f"idx_exit{horizon}",
            }), on=["ts_code", "t1"], how="left")
            events[f"ret{horizon}"] = (
                events[f"exit{horizon}"] / (events["close"] * events["t1_adj"]) - 1
            )
            events[f"exc{horizon}"] = events[f"ret{horizon}"] - (
                events[f"idx_exit{horizon}"] / events[f"idx_t1_{horizon}"] - 1
            )

        report_metrics = connection.execute(
            """
            SELECT ts_code, end_date, ann_date, or_yoy,
                   q_netprofit_qoq AS np_qoq, q_sales_qoq AS or_qoq
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY ts_code, end_date ORDER BY ann_date
                ) AS rn
                FROM a_stock_fina_indicator
                WHERE ann_date >= DATE '2018-08-01'
            )
            WHERE rn = 1
            """
        ).fetchdf()
        express_metrics = connection.execute(
            """
            SELECT ts_code, end_date, ann_date, yoy_sales AS or_yoy
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY ts_code, end_date ORDER BY ann_date
                ) AS rn
                FROM a_stock_express
                WHERE ann_date >= DATE '2018-08-01'
            )
            WHERE rn = 1
            """
        ).fetchdf()
        freshness = {
            table: connection.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
            for table, column in (
                ("a_stock_fina_indicator", "ann_date"),
                ("a_stock_forecast", "ann_date"),
                ("a_stock_express", "ann_date"),
                ("a_stock_market_daily", "trade_date"),
                ("a_stock_adj_factor", "trade_date"),
            )
        }
    finally:
        connection.close()

    for frame in (events, report_metrics, express_metrics):
        for column in ("end_date", "ann_date"):
            frame[column] = pd.to_datetime(frame[column])
    events = events.merge(
        report_metrics.assign(source="report"),
        on=["ts_code", "end_date", "ann_date", "source"],
        how="left",
    )
    events = events.merge(
        express_metrics.assign(source="express").rename(columns={"or_yoy": "or_yoy_express"}),
        on=["ts_code", "end_date", "ann_date", "source"],
        how="left",
    )
    events["or_yoy"] = events["or_yoy"].fillna(events["or_yoy_express"])
    events = events.drop(columns="or_yoy_express")
    events["year"] = pd.to_datetime(events["t1"]).dt.year
    events["quarter"] = pd.to_datetime(events["end_date"]).dt.month.map(base.QUARTER_LABELS)
    events["source_rank"] = events["source"].map(SOURCE_PRIORITY)
    metadata = {
        "raw_event_count": len(raw_events),
        "technical_signal_count": len(events),
        "freshness": freshness,
        "technical_config": {
            "min_gap_pct": min_gap_pct,
            "min_amount_ratio": min_amount_ratio,
            "amount_ratio_days": amount_ratio_days,
            "min_amount_yuan": min_amount_yuan,
            "min_listed_trade_days": min_listed_trade_days,
        },
    }
    return events, metadata


def apply_growth_config(
    frame: pd.DataFrame,
    config: Dict[str, Optional[float]],
    *,
    source_aware: bool = True,
) -> pd.DataFrame:
    """应用业绩阈值并按生产规则保留同一股票/报告期最早触发的一条信号。"""
    mask = pd.Series(True, index=frame.index, dtype=bool)
    for column, min_key, max_key, supported_sources in METRIC_FILTERS:
        lower, upper = config.get(min_key), config.get(max_key)
        if lower is None and upper is None:
            continue
        applicable = (
            frame["source"].isin(supported_sources)
            if source_aware
            else pd.Series(True, index=frame.index, dtype=bool)
        )
        passed = frame[column].notna()
        if lower is not None:
            passed &= frame[column] >= lower
        if upper is not None:
            passed &= frame[column] <= upper
        mask &= ~applicable | passed
    return (
        frame[mask]
        .sort_values(["ts_code", "end_date", "t1", "source_rank"])
        .drop_duplicates(["ts_code", "end_date"], keep="first")
        .copy()
    )


def summarize(frame: pd.DataFrame) -> Dict[str, float]:
    result: Dict[str, float] = {"signals": float(len(frame))}
    for horizon in HORIZONS:
        returns = frame[f"ret{horizon}"].dropna()
        excess = frame[f"exc{horizon}"].dropna()
        result.update({
            f"n{horizon}": float(len(returns)),
            f"ret{horizon}_mean_pct": returns.mean() * 100,
            f"ret{horizon}_median_pct": returns.median() * 100,
            f"ret{horizon}_win_pct": (returns > 0).mean() * 100,
            f"exc{horizon}_mean_pct": excess.mean() * 100,
        })
    if frame["ret20"].notna().any():
        lower, upper = frame["ret20"].quantile([0.01, 0.99])
        result["ret20_trimmed_pct"] = frame["ret20"].clip(lower, upper).mean() * 100
    return result


def compare_configs(
    frame: pd.DataFrame,
    configs: Dict[str, Dict[str, Optional[float]]],
    *,
    source_aware: bool = True,
) -> pd.DataFrame:
    rows = []
    for name, config in configs.items():
        filtered = apply_growth_config(frame, config, source_aware=source_aware)
        row: Dict[str, Any] = {"方案": name, **summarize(filtered)}
        row["事件源"] = "/".join(
            f"{source}:{count}" for source, count in filtered["source"].value_counts().items()
        )
        for period, selection in (
            ("训练2018-2022", filtered["year"] <= 2022),
            ("验证2023-2024", filtered["year"].between(2023, 2024)),
            ("测试2025-2026", filtered["year"] >= 2025),
        ):
            period_summary = summarize(filtered[selection])
            row[f"{period}样本"] = period_summary["n20"]
            row[f"{period}超额%"] = period_summary["exc20_mean_pct"]
        rows.append(row)
    return pd.DataFrame(rows).set_index("方案")


def monthly_cluster_bootstrap(
    frame: pd.DataFrame,
    current_config: Dict[str, Optional[float]],
    proposed_config: Dict[str, Optional[float]],
    *,
    draws: int = 10_000,
    seed: int = 20260926,
) -> pd.DataFrame:
    """按信号月份做簇自助法，保留同月信号相关性并估计方案差异区间。"""
    current = apply_growth_config(frame, current_config)
    proposed = apply_growth_config(frame, proposed_config)
    months = pd.period_range(
        pd.to_datetime(frame["t1"]).min().to_period("M"),
        pd.to_datetime(frame["t1"]).max().to_period("M"),
        freq="M",
    )
    aggregates = []
    for data in (current, proposed):
        monthly = (
            data.assign(month=pd.to_datetime(data["t1"]).dt.to_period("M"))
            .groupby("month")
            .agg(
                return_sum=("ret20", "sum"), return_count=("ret20", "count"),
                excess_sum=("exc20", "sum"), excess_count=("exc20", "count"),
            )
            .reindex(months, fill_value=0)
        )
        aggregates.append(monthly)
    rng = np.random.default_rng(seed)
    weights = rng.multinomial(
        len(months), np.repeat(1 / len(months), len(months)), size=draws,
    )
    estimates = []
    for monthly in aggregates:
        estimates.append(
            (weights @ monthly["return_sum"].to_numpy())
            / (weights @ monthly["return_count"].to_numpy()) * 100
        )
        estimates.append(
            (weights @ monthly["excess_sum"].to_numpy())
            / (weights @ monthly["excess_count"].to_numpy()) * 100
        )
    values = np.stack(estimates, axis=1)
    return pd.DataFrame({
        "指标": ["当前收益", "当前超额", "推荐收益", "推荐超额", "收益提升", "超额提升"],
        "P2.5": [
            *np.quantile(values, 0.025, axis=0),
            np.quantile(values[:, 2] - values[:, 0], 0.025),
            np.quantile(values[:, 3] - values[:, 1], 0.025),
        ],
        "中位": [
            *np.quantile(values, 0.5, axis=0),
            np.quantile(values[:, 2] - values[:, 0], 0.5),
            np.quantile(values[:, 3] - values[:, 1], 0.5),
        ],
        "P97.5": [
            *np.quantile(values, 0.975, axis=0),
            np.quantile(values[:, 2] - values[:, 0], 0.975),
            np.quantile(values[:, 3] - values[:, 1], 0.975),
        ],
        "P(>0)": [
            *(values > 0).mean(axis=0),
            (values[:, 2] - values[:, 0] > 0).mean(),
            (values[:, 3] - values[:, 1] > 0).mean(),
        ],
    })


def _print_table(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    print(frame[list(columns)].round(2).to_string())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--csv", help="可选：输出技术条件达标的宽松业绩候选明细")
    args = parser.parse_args()
    signals, metadata = build_signal_frame(args.db)
    if args.csv:
        output = Path(args.csv)
        output.parent.mkdir(parents=True, exist_ok=True)
        signals.to_csv(output, index=False)
    comparison = compare_configs(signals, {
        "线上现配置": LIVE_GROWTH_CONFIG,
        "稳健推荐": RECOMMENDED_GROWTH_CONFIG,
    })
    print(metadata)
    _print_table(comparison, (
        "n20", "ret5_mean_pct", "ret20_mean_pct", "ret20_median_pct",
        "ret20_win_pct", "exc20_mean_pct", "ret90_mean_pct",
        "训练2018-2022超额%", "验证2023-2024超额%", "测试2025-2026超额%",
    ))
    print(monthly_cluster_bootstrap(
        signals, LIVE_GROWTH_CONFIG, RECOMMENDED_GROWTH_CONFIG,
    ).round(2).to_string(index=False))


if __name__ == "__main__":
    main()
