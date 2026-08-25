"""ETF 份额变化与价格关系实验（只读生产数据与 Tushare）。"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import tushare as ts
from scipy.stats import norm


DEFAULT_SQLITE = Path("/home/quantd/quant_prod/quant_robot/evc_stocks.db")
DEFAULT_DUCKDB = Path("/home/quantd/quant_prod/quant_robot/analytics.duckdb")
DEFAULT_OUTPUT = Path("lab/output/fund_share_price_study")
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# 当前 A 股贪恐标的配置中有代理 ETF 的去重集合。
PROXY_ETFS = {
    "510300.SH": "沪深300",
    "563360.SH": "中证A500",
    "510500.SH": "中证500",
    "589000.SH": "科创综指",
    "588000.SH": "科创50",
    "588220.SH": "科创100",
    "588230.SH": "科创200",
    "159915.SZ": "创业板",
    "512880.SH": "证券",
    "512480.SH": "半导体",
    "588780.SH": "芯片设计",
    "159516.SZ": "半导体材料设备",
    "159530.SZ": "机器人",
    "161725.SZ": "白酒",
    "512170.SH": "医疗",
    "512400.SH": "有色",
    "512660.SH": "军工",
    "515030.SH": "新能源车",
    "159928.SZ": "消费",
    "512800.SH": "银行",
    "515220.SH": "煤炭",
    "510880.SH": "红利",
    "512890.SH": "红利低波",
}

# 追加配置中后来扩展的行业指数代理 ETF，保持实验覆盖与线上配置同步。
from backend.src.robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS

for _target in A_STOCK_INDEX_FEAR_GREED_TARGETS:
    _proxy = str(_target.get("proxy_etf") or "").upper()
    if _proxy:
        PROXY_ETFS.setdefault(
            _proxy,
            str(_target.get("ticker") or _target.get("label") or _proxy),
        )


def read_tushare_token(sqlite_path: Path) -> str:
    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT api_token FROM tushare_account_configs WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        raise RuntimeError("Tushare token 未配置")
    return str(row[0])


def load_prices(duckdb_path: Path, start: str, end: str) -> pd.DataFrame:
    symbols = list(PROXY_ETFS)
    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        frame = connection.execute(
            """
            SELECT trade_date, ts_code, close
            FROM a_stock_fund_daily_qfq
            WHERE ts_code IN (SELECT UNNEST(?))
              AND trade_date BETWEEN ? AND ?
            ORDER BY ts_code, trade_date
            """,
            [symbols, start, end],
        ).fetchdf()
    finally:
        connection.close()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["close"])


def load_shares(token: str, start: str, end: str) -> pd.DataFrame:
    pro = ts.pro_api(token)
    frames = []
    for symbol in PROXY_ETFS:
        frame = pro.etf_share_size(
            ts_code=symbol,
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
        )
        if frame is None or frame.empty:
            continue
        frames.append(frame[["trade_date", "ts_code", "total_share", "total_size"]])
    if not frames:
        raise RuntimeError("未获取到 ETF 份额数据")
    result = pd.concat(frames, ignore_index=True)
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    for column in ["total_share", "total_size"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna(subset=["total_share"])


def correlation(x: pd.Series, y: pd.Series, method: str) -> float:
    valid = pd.concat([x, y], axis=1).dropna()
    return float(valid.iloc[:, 0].corr(valid.iloc[:, 1], method=method)) if len(valid) >= 30 else np.nan


def analyze(prices: pd.DataFrame, shares: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    data = prices.merge(shares, on=["trade_date", "ts_code"], how="inner")
    data = data.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    grouped = data.groupby("ts_code", group_keys=False)
    data["price_return_1d"] = grouped["close"].pct_change(fill_method=None)
    data["share_change_1d"] = grouped["total_share"].pct_change(fill_method=None)
    data["share_change_5d"] = grouped["total_share"].pct_change(5, fill_method=None)
    data["past_return_5d"] = grouped["close"].pct_change(5, fill_method=None)
    data["past_return_20d"] = grouped["close"].pct_change(20, fill_method=None)
    data["future_return_1d"] = grouped["close"].shift(-1) / data["close"] - 1
    data["future_return_5d"] = grouped["close"].shift(-5) / data["close"] - 1
    data["future_return_20d"] = grouped["close"].shift(-20) / data["close"] - 1

    rows = []
    for symbol, frame in data.groupby("ts_code"):
        row = {
            "ts_code": symbol,
            "label": PROXY_ETFS[symbol],
            "start_date": frame["trade_date"].min().date(),
            "end_date": frame["trade_date"].max().date(),
            "observations": len(frame),
            "unchanged_share_pct": (frame["share_change_1d"].fillna(0).abs() < 1e-12).mean() * 100,
            "level_pearson": correlation(frame["total_share"], frame["close"], "pearson"),
        }
        for x_name in ["share_change_1d", "share_change_5d"]:
            for y_name in [
                "price_return_1d", "past_return_5d", "past_return_20d",
                "future_return_1d", "future_return_5d", "future_return_20d",
            ]:
                row[f"{x_name}__{y_name}__pearson"] = correlation(frame[x_name], frame[y_name], "pearson")
                row[f"{x_name}__{y_name}__spearman"] = correlation(frame[x_name], frame[y_name], "spearman")
        rows.append(row)
    by_etf = pd.DataFrame(rows)

    metric_columns = [column for column in by_etf if "__" in column]
    aggregate = []
    for column in metric_columns:
        values = by_etf[column].dropna()
        aggregate.append({
            "relationship": column,
            "etf_count": len(values),
            "median_correlation": values.median(),
            "mean_correlation": values.mean(),
            "positive_share_pct": (values > 0).mean() * 100,
            "min_correlation": values.min(),
            "max_correlation": values.max(),
        })
    aggregate_frame = pd.DataFrame(aggregate).sort_values("relationship")
    return data, by_etf, aggregate_frame


def build_indicator_correlations(daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按现有贪恐分项口径，把份额序列映射到 0~100 后与同期价格比较。"""
    frames = []
    rows = []
    for symbol, source in daily.groupby("ts_code"):
        frame = source.copy().sort_values("trade_date")
        log_share = np.log(frame["total_share"])
        log_price = np.log(frame["close"])
        raw_series = {
            "share_level": log_share,
            "share_change_5d": log_share.diff(5),
            "share_change_20d": log_share.diff(20),
        }
        for key, raw in raw_series.items():
            rolling_mean = raw.rolling(252, min_periods=120).mean()
            rolling_std = raw.rolling(252, min_periods=120).std(ddof=0).replace(0, np.nan)
            frame[f"{key}_score"] = norm.cdf((raw - rolling_mean) / rolling_std) * 100
        price_mean = log_price.rolling(252, min_periods=120).mean()
        price_std = log_price.rolling(252, min_periods=120).std(ddof=0).replace(0, np.nan)
        frame["price_score"] = norm.cdf((log_price - price_mean) / price_std) * 100

        for indicator in raw_series:
            score = frame[f"{indicator}_score"]
            rows.append({
                "ts_code": symbol,
                "label": PROXY_ETFS[symbol],
                "indicator": indicator,
                "observations": int(pd.concat([score, frame["price_score"]], axis=1).dropna().shape[0]),
                "vs_close_pearson": correlation(score, frame["close"], "pearson"),
                "vs_close_spearman": correlation(score, frame["close"], "spearman"),
                "vs_price_score_pearson": correlation(score, frame["price_score"], "pearson"),
                "vs_price_score_spearman": correlation(score, frame["price_score"], "spearman"),
            })
        frames.append(frame)
    return pd.concat(frames, ignore_index=True), pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-08-21")
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    parser.add_argument("--duckdb", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    prices = load_prices(args.duckdb, args.start, args.end)
    shares = load_shares(read_tushare_token(args.sqlite), args.start, args.end)
    daily, by_etf, aggregate = analyze(prices, shares)
    indicator_daily, indicator_correlations = build_indicator_correlations(daily)
    args.output.mkdir(parents=True, exist_ok=True)
    daily.to_csv(args.output / "daily_joined.csv", index=False)
    by_etf.to_csv(args.output / "correlation_by_etf.csv", index=False)
    aggregate.to_csv(args.output / "correlation_summary.csv", index=False)
    coverage = by_etf[["ts_code", "label", "start_date", "end_date", "observations", "unchanged_share_pct"]]
    coverage.to_csv(args.output / "coverage.csv", index=False)
    indicator_daily.to_csv(args.output / "indicator_daily.csv", index=False)
    indicator_correlations.to_csv(args.output / "indicator_price_correlations.csv", index=False)
    print(coverage.to_string(index=False))
    print("\nKey correlation summary")
    wanted = aggregate[aggregate["relationship"].isin([
        "share_change_1d__price_return_1d__spearman",
        "share_change_1d__past_return_5d__spearman",
        "share_change_1d__future_return_5d__spearman",
        "share_change_5d__past_return_20d__spearman",
        "share_change_5d__future_return_20d__spearman",
    ])]
    print(wanted.to_string(index=False))


if __name__ == "__main__":
    main()
