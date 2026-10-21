"""宽基 ETF 净申赎压力研究。

份额变化反映 ETF 一级市场的净创设/赎回，不能识别实际投资者，更不能直接认定为
“国家队”交易。本模块只提供可复核的宽基净申赎代理及其历史检验，始终不参与贪恐总分。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from sqlalchemy import text

from ..analytics_database import AnalyticsSession


ETF_SHARE_FLOW_BASKET = [
    {"symbol": "510300.SH", "name": "沪深300ETF"},
    {"symbol": "510050.SH", "name": "上证50ETF"},
    {"symbol": "510500.SH", "name": "中证500ETF"},
    {"symbol": "512100.SH", "name": "中证1000ETF"},
    {"symbol": "159915.SZ", "name": "创业板ETF"},
    {"symbol": "588000.SH", "name": "科创50ETF"},
]
BENCHMARK_INDEX = "000300.SH"
SCORE_WINDOW = 252
SCORE_MIN_PERIODS = 120


def _round(value, digits=4):
    return round(float(value), digits) if value is not None and np.isfinite(value) else None


def _normal_cdf(values: pd.Series) -> pd.Series:
    return values.map(lambda item: 0.5 * (1.0 + math.erf(float(item) / math.sqrt(2.0))))


class AStockEtfShareFlowService:
    """构造份额变化子分数，并提供无前视及事后阶段顶底两类检验。"""

    @staticmethod
    def calculate_flow_frame(share_frame: pd.DataFrame, price_frame: pd.DataFrame) -> pd.DataFrame:
        shares = share_frame.copy()
        prices = price_frame.copy()
        if shares.empty or prices.empty:
            return pd.DataFrame()
        for frame in (shares, prices):
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            frame["ts_code"] = frame["ts_code"].astype(str).str.upper()
        shares["fd_share"] = pd.to_numeric(shares["fd_share"], errors="coerce")
        prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
        merged = shares.merge(prices[["ts_code", "trade_date", "close"]], on=["ts_code", "trade_date"], how="inner")
        merged = merged.dropna(subset=["fd_share", "close"]).sort_values(["ts_code", "trade_date"])
        if merged.empty:
            return pd.DataFrame()
        merged["previous_share"] = merged.groupby("ts_code")["fd_share"].shift(1)
        merged["previous_close"] = merged.groupby("ts_code")["close"].shift(1)
        # fd_share 的单位为万份；以收盘价估算创设/赎回对应的资金量。
        merged["estimated_flow_amount"] = (merged["fd_share"] - merged["previous_share"]) * 10000.0 * merged["close"]
        merged["previous_aum"] = merged["previous_share"] * 10000.0 * merged["previous_close"]
        daily = merged.groupby("trade_date", as_index=False).agg(
            estimated_flow_amount=("estimated_flow_amount", "sum"),
            previous_aum=("previous_aum", "sum"),
            etf_count=("ts_code", "count"),
        )
        daily = daily[(daily["etf_count"] >= 3) & (daily["previous_aum"] > 0)].copy()
        daily["flow_ratio"] = daily["estimated_flow_amount"] / daily["previous_aum"]
        daily["flow_5d_amount"] = daily["estimated_flow_amount"].rolling(5, min_periods=3).sum()
        daily["aum_5d"] = daily["previous_aum"].rolling(5, min_periods=3).mean()
        daily["flow_5d_ratio"] = daily["flow_5d_amount"] / daily["aum_5d"]
        rolling_mean = daily["flow_5d_ratio"].rolling(SCORE_WINDOW, min_periods=SCORE_MIN_PERIODS).mean()
        rolling_std = daily["flow_5d_ratio"].rolling(SCORE_WINDOW, min_periods=SCORE_MIN_PERIODS).std(ddof=0)
        daily["z_score"] = (daily["flow_5d_ratio"] - rolling_mean) / rolling_std.replace(0, np.nan)
        daily["score"] = (_normal_cdf(daily["z_score"].clip(-4, 4)) * 100.0).clip(0, 100)
        return daily.replace([np.inf, -np.inf], np.nan)

    @staticmethod
    def analyze(flow_frame: pd.DataFrame, benchmark_frame: pd.DataFrame) -> Dict[str, Any]:
        if flow_frame.empty or benchmark_frame.empty:
            return {"future_returns": [], "phase_distribution": [], "sample_count": 0}
        flow = flow_frame.copy()
        benchmark = benchmark_frame.copy()
        benchmark["trade_date"] = pd.to_datetime(benchmark["trade_date"])
        benchmark["close"] = pd.to_numeric(benchmark["close"], errors="coerce")
        benchmark = benchmark.dropna(subset=["close"]).sort_values("trade_date").drop_duplicates("trade_date")
        # 份额清算结果当日收盘后才可知，收益从下一交易日开盘可得的近似价格开始计算。
        benchmark["execution_close"] = benchmark["close"].shift(-1)
        for horizon in (20, 60):
            benchmark[f"future_{horizon}d_return"] = benchmark["close"].shift(-horizon - 1) / benchmark["execution_close"] - 1.0
        # 这个标签明确是回顾性定义，不能作为当日交易信号。
        rolling_low = benchmark["close"].rolling(61, center=True, min_periods=61).min()
        rolling_high = benchmark["close"].rolling(61, center=True, min_periods=61).max()
        benchmark["phase"] = np.where(
            benchmark["close"].eq(rolling_low), "bottom",
            np.where(benchmark["close"].eq(rolling_high), "top", None),
        )
        data = flow.merge(benchmark, on="trade_date", how="inner")
        data = data.dropna(subset=["score"])
        if data.empty:
            return {"future_returns": [], "phase_distribution": [], "sample_count": 0}
        data["score_bucket"] = pd.cut(
            data["score"], bins=[-0.01, 20, 40, 60, 80, 100.01],
            labels=["0-20", "20-40", "40-60", "60-80", "80-100"],
        )
        future = []
        for bucket, group in data.groupby("score_bucket", observed=False):
            item = {"bucket": str(bucket), "count": int(len(group))}
            for horizon in (20, 60):
                values = group[f"future_{horizon}d_return"].dropna()
                item[f"return_{horizon}d_mean_pct"] = _round(values.mean() * 100, 2) if len(values) else None
                item[f"return_{horizon}d_median_pct"] = _round(values.median() * 100, 2) if len(values) else None
                item[f"return_{horizon}d_positive_rate_pct"] = _round((values > 0).mean() * 100, 1) if len(values) else None
            future.append(item)
        phases = []
        for phase in ("bottom", "top"):
            values = data.loc[data["phase"] == phase, "score"].dropna()
            phases.append({
                "phase": phase,
                "count": int(len(values)),
                "mean": _round(values.mean(), 2) if len(values) else None,
                "median": _round(values.median(), 2) if len(values) else None,
                "p25": _round(values.quantile(.25), 2) if len(values) else None,
                "p75": _round(values.quantile(.75), 2) if len(values) else None,
            })
        return {"future_returns": future, "phase_distribution": phases, "sample_count": int(len(data))}

    def _load_frames(self):
        codes = [item["symbol"] for item in ETF_SHARE_FLOW_BASKET]
        params = {f"code_{index}": code for index, code in enumerate(codes)}
        placeholders = ", ".join(f":{key}" for key in params)
        db = AnalyticsSession()
        try:
            with db.bind.connect() as conn:
                shares = pd.read_sql(text(f"SELECT ts_code, trade_date, fd_share FROM a_stock_fund_share WHERE ts_code IN ({placeholders})"), conn, params=params)
                prices = pd.read_sql(text(f"SELECT ts_code, trade_date, close FROM a_stock_fund_daily WHERE ts_code IN ({placeholders})"), conn, params=params)
                benchmark = pd.read_sql(text("SELECT trade_date, close FROM a_stock_index_daily WHERE ts_code = :code ORDER BY trade_date"), conn, params={"code": BENCHMARK_INDEX})
            return shares, prices, benchmark
        finally:
            db.close()

    def get_payload(self, history_days: int = 504) -> Dict[str, Any]:
        shares, prices, benchmark = self._load_frames()
        flow = self.calculate_flow_frame(shares, prices)
        analysis = self.analyze(flow, benchmark)
        records = []
        for row in flow.tail(max(1, min(int(history_days), 2000))).itertuples(index=False):
            records.append({
                "date": row.trade_date.date().isoformat(),
                "score": _round(row.score, 2),
                "flow_5d_ratio_pct": _round(row.flow_5d_ratio * 100, 3),
                "estimated_flow_amount_yi": _round(row.flow_5d_amount / 100000000, 2),
                "etf_count": int(row.etf_count),
            })
        latest = records[-1] if records else None
        return {
            "name": "宽基ETF净申赎压力",
            "included_in_total": False,
            "proxy_note": "ETF 份额变化仅反映一级市场净创设/赎回，无法识别资金主体；不可直接推断国家队买卖。",
            "score_direction": "高分=相对历史更强的净申购/创设压力；低分=净赎回压力。",
            "basket": ETF_SHARE_FLOW_BASKET,
            "benchmark": {"symbol": BENCHMARK_INDEX, "name": "沪深300"},
            "latest": latest,
            "history": records,
            "analysis": analysis,
        }
