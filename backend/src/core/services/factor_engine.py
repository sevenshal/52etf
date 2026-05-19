import logging
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
import polars as pl
from sqlalchemy.orm import Session as ORMSession

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
SUPPORTED_MOMENTUM_WINDOWS = [20, 60, 120]
SUPPORTED_WINDOWS = [20, 60, 120]
MIXED_WINDOW_KEY = "mixed"
DEFAULT_MOMENTUM_WEIGHTS = {"20": 0.05, "60": 0.20, "120": 0.75}

FACTOR_DIRECTION_OPTIONS = {
    "higher_is_better": {"sign": 1.0, "label": "高值更好"},
    "lower_is_better": {"sign": -1.0, "label": "低值更好"},
    "exploratory": {"sign": 1.0, "label": "探索方向"},
}
NEUTRALIZATION_OPTIONS = {
    "none": {"label": "不做中性化"},
    "sector": {"label": "行业大类中性化（Sector）"},
    "sector_market_cap": {"label": "行业大类+市值中性化"},
    "fine_industry": {"label": "细行业中性化（Industry，小样本回退Sector）"},
    "fine_industry_market_cap": {"label": "细行业+市值中性化（小样本回退Sector）"},
}
STANDARDIZATION_OPTIONS = {
    "none": {"label": "不标准化"},
    "zscore": {"label": "截面 Z-Score"},
    "rank_percentile": {"label": "截面排名分位"},
}
MIN_FINE_INDUSTRY_NEUTRALIZATION_SIZE = 10
MOMENTUM_FACTOR_SCORE_PREFIX = {
    "risk_adjusted_momentum": "_ram",
    "raw_momentum": "_raw_mom",
}


@dataclass(frozen=True)
class FactorContext:
    windows: List[int]
    momentum_weights: Dict[int, float]
    db: ORMSession
    symbols: List[str]
    start_date: date
    end_date: date
    analysis_dates: List[date] = field(default_factory=list)
    industry_df: Optional[pl.DataFrame] = None
    candidate_etfs: List[str] = field(default_factory=list)
    valuation_df: Optional[pl.DataFrame] = None
    weight_history: Optional[Dict[str, Dict[date, Dict[str, float]]]] = None
    weight_history_loader: Optional[Callable[[ORMSession, List[str], date, date], Dict[str, Dict[date, Dict[str, float]]]]] = None


@dataclass(frozen=True)
class FactorDefinition:
    key: str
    label: str
    group: str
    description: str
    default_windows: List[int]
    supports_windows: bool
    supports_mixed_windows: bool
    direction: str
    compute: Callable[[pl.DataFrame, FactorContext], pl.DataFrame]
    unsupported_pool_types: List[str] = field(default_factory=list)

    def to_option(self) -> Dict[str, Any]:
        direction = FACTOR_DIRECTION_OPTIONS.get(self.direction, FACTOR_DIRECTION_OPTIONS["exploratory"])
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "description": self.description,
            "default_windows": self.default_windows,
            "supports_windows": self.supports_windows,
            "supports_mixed_windows": self.supports_mixed_windows,
            "direction": self.direction,
            "direction_label": direction["label"],
            "direction_sign": direction["sign"],
            "unsupported_pool_types": list(self.unsupported_pool_types),
        }


def _get_attr(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def normalize_momentum_weights(raw_weights: Dict[str, float], active_windows: List[int]) -> Dict[int, float]:
    active = list(dict.fromkeys(int(item) for item in active_windows))
    weights: Dict[int, float] = {}
    for window in active:
        raw_value = raw_weights.get(str(window), raw_weights.get(window, 0.0)) if isinstance(raw_weights, dict) else 0.0
        try:
            weights[window] = max(0.0, float(raw_value or 0))
        except (TypeError, ValueError):
            weights[window] = 0.0
    total = sum(weights.values())
    if total <= 0:
        return {window: 1.0 / len(active) for window in active}
    return {window: weight / total for window, weight in weights.items() if weight > 0}


def normalize_momentum_weights_payload(raw_weights: Dict[str, float], *, strict: bool = False) -> Dict[str, float]:
    raw = raw_weights if isinstance(raw_weights, dict) else DEFAULT_MOMENTUM_WEIGHTS
    normalized: Dict[str, float] = {}
    for window in SUPPORTED_MOMENTUM_WINDOWS:
        try:
            weight = float(raw.get(str(window), raw.get(window, 0)) or 0)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"{window}日动量权重必须是数字")
            weight = 0.0
        if weight < 0:
            if strict:
                raise ValueError(f"{window}日动量权重不能为负数")
            weight = 0.0
        normalized[str(window)] = weight
    if sum(normalized.values()) <= 0:
        if strict:
            raise ValueError("至少设置一个大于0的动量权重")
        return DEFAULT_MOMENTUM_WEIGHTS.copy()
    return normalized


_normalize_momentum_weights = normalize_momentum_weights


def _normalize_momentum_weights_payload(raw_weights: Dict[str, float]) -> Dict[str, float]:
    return normalize_momentum_weights_payload(raw_weights, strict=True)


def _ensure_base_columns(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.int_range(0, pl.len()).over("symbol").cast(pl.Float64).alias("_row_nr"),
            pl.col("close").log().alias("_log_close"),
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_daily_return"),
            pl.when(pl.col("volume").is_not_null() & (pl.col("volume") > 0))
            .then(pl.col("volume").log10())
            .otherwise(None)
            .alias("_log_volume"),
        )
    )


def _add_momentum_window_features(df: pl.DataFrame, window: int, prefix: str) -> pl.DataFrame:
    w = int(window)
    sum_x = w * (w - 1) / 2
    sum_x2 = w * (w - 1) * (2 * w - 1) / 6
    denominator = w * sum_x2 - sum_x * sum_x

    df = df.with_columns(
        pl.col("_log_close").rolling_sum(w, min_samples=w).over("symbol").alias(f"{prefix}_sum_y"),
        (pl.col("_log_close") ** 2).rolling_sum(w, min_samples=w).over("symbol").alias(f"{prefix}_sum_y2"),
        (pl.col("_row_nr") * pl.col("_log_close")).rolling_sum(w, min_samples=w).over("symbol").alias(f"{prefix}_sum_iy"),
        pl.col("_daily_return").rolling_std(w - 1, min_samples=w - 1).over("symbol").alias(f"{prefix}_daily_vol"),
        (pl.col("close") / pl.col("close").shift(w - 1).over("symbol") - 1).alias(f"{prefix}_window_return"),
    )
    df = df.with_columns(
        (
            pl.col(f"{prefix}_sum_iy")
            - (pl.col("_row_nr") - (w - 1)) * pl.col(f"{prefix}_sum_y")
        ).alias(f"{prefix}_sum_xy")
    )
    df = df.with_columns(
        (
            (w * pl.col(f"{prefix}_sum_xy") - sum_x * pl.col(f"{prefix}_sum_y"))
            / denominator
        ).alias(f"{prefix}_slope")
    )
    df = df.with_columns(
        ((pl.col(f"{prefix}_sum_y") - pl.col(f"{prefix}_slope") * sum_x) / w).alias(f"{prefix}_intercept"),
        (
            pl.col(f"{prefix}_sum_y2")
            - (pl.col(f"{prefix}_sum_y") ** 2 / w)
        ).alias(f"{prefix}_ss_tot"),
    )
    df = df.with_columns(
        (
            pl.col(f"{prefix}_sum_y2")
            - 2 * pl.col(f"{prefix}_intercept") * pl.col(f"{prefix}_sum_y")
            - 2 * pl.col(f"{prefix}_slope") * pl.col(f"{prefix}_sum_xy")
            + (pl.col(f"{prefix}_intercept") ** 2) * w
            + 2 * pl.col(f"{prefix}_intercept") * pl.col(f"{prefix}_slope") * sum_x
            + (pl.col(f"{prefix}_slope") ** 2) * sum_x2
        ).alias(f"{prefix}_ss_res")
    )
    return df.with_columns(
        pl.when(pl.col(f"{prefix}_ss_tot") > 0)
        .then((1 - pl.col(f"{prefix}_ss_res") / pl.col(f"{prefix}_ss_tot")).clip(0.0, 1.0))
        .otherwise(None)
        .alias(f"{prefix}_r_squared"),
        (pl.col(f"{prefix}_daily_vol") * math.sqrt(TRADING_DAYS_PER_YEAR) * 100).alias(f"{prefix}_annualized_vol_pct"),
        (pl.col(f"{prefix}_slope") * TRADING_DAYS_PER_YEAR * 100).alias(f"{prefix}_annualized_slope_pct"),
    )


def _add_risk_adjusted_momentum_score(df: pl.DataFrame, window: int) -> pl.DataFrame:
    w = int(window)
    prefix = f"_ram_{w}"
    df = _add_momentum_window_features(df, w, prefix)
    return df.with_columns(
        pl.when(pl.col(f"{prefix}_annualized_vol_pct") > 0)
        .then(pl.col(f"{prefix}_annualized_slope_pct") * pl.col(f"{prefix}_r_squared") / pl.col(f"{prefix}_annualized_vol_pct") * 100)
        .otherwise(None)
        .alias(f"{prefix}_score")
    )


def _add_raw_momentum_score(df: pl.DataFrame, window: int) -> pl.DataFrame:
    w = int(window)
    prefix = f"_raw_mom_{w}"
    df = _add_momentum_window_features(df, w, prefix)
    return df.with_columns(
        pl.when(pl.col(f"{prefix}_r_squared").is_not_null())
        .then(pl.col(f"{prefix}_annualized_slope_pct") * pl.col(f"{prefix}_r_squared"))
        .otherwise(None)
        .alias(f"{prefix}_score")
    )


def _compute_risk_adjusted_momentum(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    result = _ensure_base_columns(df)
    for window in context.momentum_weights:
        result = _add_risk_adjusted_momentum_score(result, window)
    factor_expr = None
    for window, weight in context.momentum_weights.items():
        expr = pl.col(f"_ram_{window}_score") * float(weight)
        factor_expr = expr if factor_expr is None else factor_expr + expr
    return result.with_columns((factor_expr if factor_expr is not None else pl.lit(None, dtype=pl.Float64)).alias("factor_value"))


def _compute_raw_momentum(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    result = _ensure_base_columns(df)
    for window in context.momentum_weights:
        result = _add_raw_momentum_score(result, window)
    factor_expr = None
    for window, weight in context.momentum_weights.items():
        expr = pl.col(f"_raw_mom_{window}_score") * float(weight)
        factor_expr = expr if factor_expr is None else factor_expr + expr
    return result.with_columns((factor_expr if factor_expr is not None else pl.lit(None, dtype=pl.Float64)).alias("factor_value"))


def _build_momentum_score_source_frame(price_df: pl.DataFrame, factor_key: str, windows: List[int]) -> pl.DataFrame:
    if price_df.is_empty():
        return price_df
    result = _ensure_base_columns(price_df)
    score_columns: List[str] = []
    for window in list(dict.fromkeys(int(item) for item in windows)):
        if factor_key == "risk_adjusted_momentum":
            result = _add_risk_adjusted_momentum_score(result, window)
            score_columns.append(f"_ram_{window}_score")
        elif factor_key == "raw_momentum":
            result = _add_raw_momentum_score(result, window)
            score_columns.append(f"_raw_mom_{window}_score")
    base_columns = [column for column in ["symbol", "trade_date", "open", "high", "low", "close", "volume", "turnover", "_first_trade_date"] if column in result.columns]
    return result.select([*base_columns, *score_columns])


def _momentum_score_source_frame(
    price_df: pl.DataFrame,
    factor_key: str,
    windows: List[int],
    raw_factor_cache: Optional[Dict[Any, pl.DataFrame]],
) -> pl.DataFrame:
    cache_key = (factor_key, tuple(sorted(list(dict.fromkeys(int(item) for item in windows)))))
    if raw_factor_cache is not None and cache_key in raw_factor_cache:
        return raw_factor_cache[cache_key]
    source = _build_momentum_score_source_frame(price_df, factor_key, list(cache_key[1]))
    if raw_factor_cache is not None:
        raw_factor_cache[cache_key] = source
    return source


def _compute_volume_z(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    window = int(context.windows[0])
    short_window = max(int(window / 20), 1)
    result = _ensure_base_columns(df)
    return (
        result.with_columns(
            pl.col("_log_volume").rolling_mean(short_window, min_samples=short_window).over("symbol").alias("_volume_short_avg"),
            pl.col("_log_volume").shift(short_window).rolling_mean(window, min_samples=window).over("symbol").alias("_volume_long_avg"),
            pl.col("_log_volume").shift(short_window).rolling_std(window, min_samples=window).over("symbol").alias("_volume_long_std"),
        )
        .with_columns(
            pl.when(pl.col("_volume_long_std") > 0)
            .then((pl.col("_volume_short_avg") - pl.col("_volume_long_avg")) / pl.col("_volume_long_std"))
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_volume_ratio(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    window = int(context.windows[0])
    short_window = max(int(window / 20), 1)
    result = _ensure_base_columns(df)
    return (
        result.with_columns(
            pl.when(pl.col("volume").is_not_null() & (pl.col("volume") > 0))
            .then(pl.col("volume").cast(pl.Float64))
            .otherwise(None)
            .alias("_volume_for_ratio")
        )
        .with_columns(
            pl.col("_volume_for_ratio").rolling_mean(short_window, min_samples=short_window).over("symbol").alias("_volume_short_avg"),
            pl.col("_volume_for_ratio").shift(short_window).rolling_mean(window, min_samples=window).over("symbol").alias("_volume_long_avg"),
        )
        .with_columns(
            pl.when(pl.col("_volume_long_avg") > 0)
            .then(pl.col("_volume_short_avg") / pl.col("_volume_long_avg"))
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_log_volume_ratio(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    ratio_df = _compute_volume_ratio(df, context)
    if ratio_df.is_empty() or "factor_value" not in ratio_df.columns:
        return ratio_df
    return ratio_df.with_columns(
        pl.when(pl.col("factor_value") > 0)
        .then(pl.col("factor_value").log10())
        .otherwise(None)
        .alias("factor_value")
    )


def _compute_volatility(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    window = int(context.windows[0])
    result = _ensure_base_columns(df)
    return result.with_columns(
        (pl.col("_daily_return").rolling_std(window, min_samples=window).over("symbol") * math.sqrt(TRADING_DAYS_PER_YEAR) * 100).alias("factor_value")
    )


def _with_cross_section_rank_percentile(df: pl.DataFrame, source_column: str, output_column: str) -> pl.DataFrame:
    if df.is_empty() or source_column not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias(output_column))
    valid_column = f"_{output_column}_valid"
    rank_column = f"_{output_column}_rank"
    count_column = f"_{output_column}_count"
    return (
        df.with_columns(
            pl.when(pl.col(source_column).is_not_null() & pl.col(source_column).is_finite())
            .then(pl.col(source_column).cast(pl.Float64))
            .otherwise(None)
            .alias(valid_column)
        )
        .with_columns(
            pl.col(valid_column).rank("average").over("trade_date").alias(rank_column),
            pl.col(valid_column).count().over("trade_date").alias(count_column),
        )
        .with_columns(
            pl.when(pl.col(valid_column).is_null())
            .then(None)
            .when(pl.col(count_column) <= 1)
            .then(1.0)
            .otherwise((pl.col(rank_column) - 1) / (pl.col(count_column) - 1))
            .alias(output_column)
        )
        .drop([valid_column, rank_column, count_column])
    )


def _rolling_ts_rank_percentile_expr(column: str, window: int) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.Float64)
        .rolling_map(
            lambda values: float(values.rank("average")[-1] / len(values)),
            window_size=int(window),
            min_samples=int(window),
        )
        .over("symbol")
    )


def _decay_linear_expr(column: str, window: int) -> pl.Expr:
    w = int(window)
    denominator = w * (w + 1) / 2
    expr = None
    for offset in range(w):
        weight = (w - offset) / denominator
        term = pl.col(column).shift(offset).over("symbol") * weight
        expr = term if expr is None else expr + term
    return expr if expr is not None else pl.lit(None, dtype=pl.Float64)


def _sma_cn_expr(column: str, window: int, weight: int) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.Float64)
        .ewm_mean(alpha=float(weight) / float(window), adjust=False)
        .over("symbol")
    )


def _compute_alpha021(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    window = 6
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.int_range(0, pl.len()).over("symbol").cast(pl.Float64).alias("_alpha021_row_nr"),
            pl.col("close").rolling_mean(window, min_samples=window).over("symbol").alias("_alpha021_ma6"),
        )
        .with_columns(
            pl.col("_alpha021_row_nr").rolling_sum(window, min_samples=window).over("symbol").alias("_alpha021_sum_x"),
            (pl.col("_alpha021_row_nr") ** 2).rolling_sum(window, min_samples=window).over("symbol").alias("_alpha021_sum_x2"),
            pl.col("_alpha021_ma6").rolling_sum(window, min_samples=window).over("symbol").alias("_alpha021_sum_y"),
            (pl.col("_alpha021_row_nr") * pl.col("_alpha021_ma6")).rolling_sum(window, min_samples=window).over("symbol").alias("_alpha021_sum_xy"),
        )
        .with_columns(
            (window * pl.col("_alpha021_sum_x2") - pl.col("_alpha021_sum_x") ** 2).alias("_alpha021_denominator")
        )
        .with_columns(
            pl.when(pl.col("_alpha021_denominator") != 0)
            .then(
                (window * pl.col("_alpha021_sum_xy") - pl.col("_alpha021_sum_x") * pl.col("_alpha021_sum_y"))
                / pl.col("_alpha021_denominator")
            )
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_alpha042(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "high" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    result = (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.col("high").cast(pl.Float64).rolling_std(10, min_samples=10).over("symbol").alias("_alpha042_std_high_10"),
            pl.rolling_corr(
                pl.col("high").cast(pl.Float64),
                pl.col("volume").cast(pl.Float64),
                window_size=10,
                min_samples=10,
            ).over("symbol").alias("_alpha042_high_volume_corr_10"),
        )
    )
    result = _with_cross_section_rank_percentile(result, "_alpha042_std_high_10", "_alpha042_std_high_rank")
    return result.with_columns(
        pl.when(
            pl.col("_alpha042_std_high_rank").is_not_null()
            & pl.col("_alpha042_high_volume_corr_10").is_not_null()
            & pl.col("_alpha042_high_volume_corr_10").is_finite()
        )
        .then(-pl.col("_alpha042_std_high_rank") * pl.col("_alpha042_high_volume_corr_10"))
        .otherwise(None)
        .alias("factor_value")
    )


def _compute_alpha005(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"high", "volume"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            _rolling_ts_rank_percentile_expr("volume", 5).alias("_alpha005_ts_rank_volume_5"),
            _rolling_ts_rank_percentile_expr("high", 5).alias("_alpha005_ts_rank_high_5"),
        )
        .with_columns(
            pl.rolling_corr(
                pl.col("_alpha005_ts_rank_volume_5"),
                pl.col("_alpha005_ts_rank_high_5"),
                window_size=5,
                min_samples=5,
            )
            .over("symbol")
            .alias("_alpha005_corr_5")
        )
        .with_columns(
            (-pl.col("_alpha005_corr_5").rolling_max(3, min_samples=3).over("symbol")).alias("factor_value")
        )
    )


def _compute_alpha052(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"high", "low", "close"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(((pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias("_alpha052_typical_price"))
        .with_columns(pl.col("_alpha052_typical_price").shift(1).over("symbol").alias("_alpha052_typical_price_lag_1"))
        .with_columns(
            pl.max_horizontal(pl.col("high") - pl.col("_alpha052_typical_price_lag_1"), pl.lit(0.0)).alias("_alpha052_up_force"),
            pl.max_horizontal(pl.col("_alpha052_typical_price_lag_1") - pl.col("low"), pl.lit(0.0)).alias("_alpha052_down_force"),
        )
        .with_columns(
            pl.col("_alpha052_up_force").rolling_sum(26, min_samples=26).over("symbol").alias("_alpha052_up_sum_26"),
            pl.col("_alpha052_down_force").rolling_sum(26, min_samples=26).over("symbol").alias("_alpha052_down_sum_26"),
        )
        .with_columns(
            pl.when(pl.col("_alpha052_down_sum_26") != 0)
            .then(pl.col("_alpha052_up_sum_26") / pl.col("_alpha052_down_sum_26") * 100)
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_alpha059(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"low", "close"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("close").shift(1).over("symbol").alias("_alpha059_close_lag_1"))
        .with_columns(
            pl.when(pl.col("close") == pl.col("_alpha059_close_lag_1"))
            .then(0.0)
            .when(pl.col("close") > pl.col("_alpha059_close_lag_1"))
            .then(pl.col("close") - pl.min_horizontal(pl.col("low"), pl.col("_alpha059_close_lag_1")))
            .when(pl.col("close") < pl.col("_alpha059_close_lag_1"))
            .then(pl.col("close") - pl.max_horizontal(pl.col("low"), pl.col("_alpha059_close_lag_1")))
            .otherwise(None)
            .alias("_alpha059_price_flow")
        )
        .with_columns(pl.col("_alpha059_price_flow").rolling_sum(20, min_samples=20).over("symbol").alias("factor_value"))
    )


def _compute_alpha024(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns((pl.col("close") - pl.col("close").shift(5).over("symbol")).alias("_alpha024_delta_5"))
        .with_columns(_sma_cn_expr("_alpha024_delta_5", 5, 1).alias("factor_value"))
    )


def _compute_alpha027(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            (
                (pl.col("close") - pl.col("close").shift(3).over("symbol"))
                / pl.col("close").shift(3).over("symbol")
                * 100
                + (pl.col("close") - pl.col("close").shift(6).over("symbol"))
                / pl.col("close").shift(6).over("symbol")
                * 100
            ).alias("_alpha027_momentum")
        )
        .with_columns(_decay_linear_expr("_alpha027_momentum", 12).alias("factor_value"))
    )


def _compute_alpha046(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            (
                (
                    pl.col("close").rolling_mean(3, min_samples=3).over("symbol")
                    + pl.col("close").rolling_mean(6, min_samples=6).over("symbol")
                    + pl.col("close").rolling_mean(12, min_samples=12).over("symbol")
                    + pl.col("close").rolling_mean(24, min_samples=24).over("symbol")
                )
                / (4 * pl.col("close"))
            ).alias("factor_value")
        )
    )


def _compute_alpha088(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return df.sort(["symbol", "trade_date"]).with_columns(
        ((pl.col("close") - pl.col("close").shift(20).over("symbol")) / pl.col("close").shift(20).over("symbol") * 100).alias("factor_value")
    )


def _compute_alpha093(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"open", "low"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("open").shift(1).over("symbol").alias("_alpha093_open_lag_1"))
        .with_columns(
            pl.when(pl.col("_alpha093_open_lag_1").is_null())
            .then(None)
            .when(pl.col("open") >= pl.col("_alpha093_open_lag_1"))
            .then(0.0)
            .otherwise(pl.max_horizontal(pl.col("open") - pl.col("low"), pl.col("open") - pl.col("_alpha093_open_lag_1")))
            .alias("_alpha093_open_down_probe")
        )
        .with_columns(pl.col("_alpha093_open_down_probe").rolling_sum(20, min_samples=20).over("symbol").alias("factor_value"))
    )


def _compute_alpha106(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return df.sort(["symbol", "trade_date"]).with_columns(
        (pl.col("close") - pl.col("close").shift(20).over("symbol")).alias("factor_value")
    )


def _compute_alpha118(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"high", "open", "low"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            (pl.col("high") - pl.col("open")).rolling_sum(20, min_samples=20).over("symbol").alias("_alpha118_upper_sum"),
            (pl.col("open") - pl.col("low")).rolling_sum(20, min_samples=20).over("symbol").alias("_alpha118_lower_sum"),
        )
        .with_columns(
            pl.when(pl.col("_alpha118_lower_sum") != 0)
            .then(pl.col("_alpha118_upper_sum") / pl.col("_alpha118_lower_sum") * 100)
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_alpha122(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.when(pl.col("close") > 0)
            .then(pl.col("close").log())
            .otherwise(None)
            .alias("_alpha122_log_close")
        )
        .with_columns(_sma_cn_expr("_alpha122_log_close", 13, 2).alias("_alpha122_sma_1"))
        .with_columns(_sma_cn_expr("_alpha122_sma_1", 13, 2).alias("_alpha122_sma_2"))
        .with_columns(_sma_cn_expr("_alpha122_sma_2", 13, 2).alias("_alpha122_sma_3"))
        .with_columns(pl.col("_alpha122_sma_3").shift(1).over("symbol").alias("_alpha122_sma_3_lag_1"))
        .with_columns(
            pl.when(pl.col("_alpha122_sma_3_lag_1") != 0)
            .then((pl.col("_alpha122_sma_3") - pl.col("_alpha122_sma_3_lag_1")) / pl.col("_alpha122_sma_3_lag_1"))
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_alpha129(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("close").shift(1).over("symbol").alias("_alpha129_close_lag_1"))
        .with_columns(
            pl.when(pl.col("_alpha129_close_lag_1").is_null())
            .then(None)
            .when(pl.col("close") < pl.col("_alpha129_close_lag_1"))
            .then((pl.col("close") - pl.col("_alpha129_close_lag_1")).abs())
            .otherwise(0.0)
            .alias("_alpha129_down_delta")
        )
        .with_columns(pl.col("_alpha129_down_delta").rolling_sum(12, min_samples=12).over("symbol").alias("factor_value"))
    )


def _compute_alpha132(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "turnover" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.when(pl.col("turnover").is_not_null() & (pl.col("turnover") > 0))
            .then(pl.col("turnover").cast(pl.Float64))
            .otherwise(None)
            .alias("_alpha132_amount")
        )
        .with_columns(pl.col("_alpha132_amount").rolling_mean(20, min_samples=20).over("symbol").alias("factor_value"))
    )


def _compute_alpha134(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"close", "volume"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("close").shift(12).over("symbol").alias("_alpha134_close_lag_12"))
        .with_columns(
            pl.when(pl.col("_alpha134_close_lag_12") != 0)
            .then((pl.col("close") - pl.col("_alpha134_close_lag_12")) / pl.col("_alpha134_close_lag_12") * pl.col("volume"))
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_alpha135(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            (pl.col("close") / pl.col("close").shift(20).over("symbol")).shift(1).over("symbol").alias("_alpha135_delayed_ratio")
        )
        .with_columns(_sma_cn_expr("_alpha135_delayed_ratio", 20, 1).alias("factor_value"))
    )


def _compute_alpha139(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"open", "volume"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            (
                -pl.rolling_corr(
                    pl.col("open").cast(pl.Float64),
                    pl.col("volume").cast(pl.Float64),
                    window_size=10,
                    min_samples=10,
                ).over("symbol")
            ).alias("factor_value")
        )
    )


def _compute_alpha145(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "volume" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.col("volume").rolling_mean(9, min_samples=9).over("symbol").alias("_alpha145_volume_ma9"),
            pl.col("volume").rolling_mean(12, min_samples=12).over("symbol").alias("_alpha145_volume_ma12"),
            pl.col("volume").rolling_mean(26, min_samples=26).over("symbol").alias("_alpha145_volume_ma26"),
        )
        .with_columns(
            pl.when(pl.col("_alpha145_volume_ma12") != 0)
            .then((pl.col("_alpha145_volume_ma9") - pl.col("_alpha145_volume_ma26")) / pl.col("_alpha145_volume_ma12") * 100)
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_alpha147(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    window = 12
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.int_range(0, pl.len()).over("symbol").cast(pl.Float64).alias("_alpha147_row_nr"),
            pl.col("close").rolling_mean(window, min_samples=window).over("symbol").alias("_alpha147_ma12"),
        )
        .with_columns(
            pl.col("_alpha147_row_nr").rolling_sum(window, min_samples=window).over("symbol").alias("_alpha147_sum_x"),
            (pl.col("_alpha147_row_nr") ** 2).rolling_sum(window, min_samples=window).over("symbol").alias("_alpha147_sum_x2"),
            pl.col("_alpha147_ma12").rolling_sum(window, min_samples=window).over("symbol").alias("_alpha147_sum_y"),
            (pl.col("_alpha147_row_nr") * pl.col("_alpha147_ma12")).rolling_sum(window, min_samples=window).over("symbol").alias("_alpha147_sum_xy"),
        )
        .with_columns((window * pl.col("_alpha147_sum_x2") - pl.col("_alpha147_sum_x") ** 2).alias("_alpha147_denominator"))
        .with_columns(
            pl.when(pl.col("_alpha147_denominator") != 0)
            .then(
                (window * pl.col("_alpha147_sum_xy") - pl.col("_alpha147_sum_x") * pl.col("_alpha147_sum_y"))
                / pl.col("_alpha147_denominator")
            )
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_alpha151(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns((pl.col("close") - pl.col("close").shift(20).over("symbol")).alias("_alpha151_delta_20"))
        .with_columns(_sma_cn_expr("_alpha151_delta_20", 20, 1).alias("factor_value"))
    )


def _compute_alpha158(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"high", "low", "close"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return df.sort(["symbol", "trade_date"]).with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("factor_value")
    )


def _compute_alpha160(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("_alpha160_close_lag_1"),
            pl.col("close").rolling_std(20, min_samples=20).over("symbol").alias("_alpha160_close_std_20"),
        )
        .with_columns(
            pl.when(pl.col("close") <= pl.col("_alpha160_close_lag_1"))
            .then(pl.col("_alpha160_close_std_20"))
            .otherwise(0.0)
            .alias("_alpha160_down_std")
        )
        .with_columns(_sma_cn_expr("_alpha160_down_std", 20, 1).alias("factor_value"))
    )


def _true_range_expr(prefix: str) -> pl.Expr:
    return pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col(f"_{prefix}_close_lag_1") - pl.col("high")).abs(),
        (pl.col(f"_{prefix}_close_lag_1") - pl.col("low")).abs(),
    )


def _compute_alpha161(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"high", "low", "close"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("close").shift(1).over("symbol").alias("_alpha161_close_lag_1"))
        .with_columns(_true_range_expr("alpha161").alias("_alpha161_true_range"))
        .with_columns(pl.col("_alpha161_true_range").rolling_mean(12, min_samples=12).over("symbol").alias("factor_value"))
    )


def _compute_alpha169(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns((pl.col("close") - pl.col("close").shift(1).over("symbol")).alias("_alpha169_delta_1"))
        .with_columns(_sma_cn_expr("_alpha169_delta_1", 9, 1).shift(1).over("symbol").alias("_alpha169_delayed_sma"))
        .with_columns(
            pl.col("_alpha169_delayed_sma").rolling_mean(12, min_samples=12).over("symbol").alias("_alpha169_mean_12"),
            pl.col("_alpha169_delayed_sma").rolling_mean(26, min_samples=26).over("symbol").alias("_alpha169_mean_26"),
        )
        .with_columns((pl.col("_alpha169_mean_12") - pl.col("_alpha169_mean_26")).alias("_alpha169_diff"))
        .with_columns(_sma_cn_expr("_alpha169_diff", 10, 1).alias("factor_value"))
    )


def _compute_alpha167(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("close").shift(1).over("symbol").alias("_alpha167_close_lag_1"))
        .with_columns(
            pl.when(pl.col("_alpha167_close_lag_1").is_null())
            .then(None)
            .when(pl.col("close") > pl.col("_alpha167_close_lag_1"))
            .then(pl.col("close") - pl.col("_alpha167_close_lag_1"))
            .otherwise(0.0)
            .alias("_alpha167_up_delta")
        )
        .with_columns(pl.col("_alpha167_up_delta").rolling_sum(12, min_samples=12).over("symbol").alias("factor_value"))
    )


def _compute_alpha174(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("_alpha174_close_lag_1"),
            pl.col("close").rolling_std(20, min_samples=20).over("symbol").alias("_alpha174_close_std_20"),
        )
        .with_columns(
            pl.when(pl.col("close") > pl.col("_alpha174_close_lag_1"))
            .then(pl.col("_alpha174_close_std_20"))
            .otherwise(0.0)
            .alias("_alpha174_up_std")
        )
        .with_columns(_sma_cn_expr("_alpha174_up_std", 20, 1).alias("factor_value"))
    )


def _compute_alpha175(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"high", "low", "close"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("close").shift(1).over("symbol").alias("_alpha175_close_lag_1"))
        .with_columns(_true_range_expr("alpha175").alias("_alpha175_true_range"))
        .with_columns(pl.col("_alpha175_true_range").rolling_mean(6, min_samples=6).over("symbol").alias("factor_value"))
    )


def _compute_alpha187(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or not {"open", "high"}.issubset(df.columns):
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("open").shift(1).over("symbol").alias("_alpha187_open_lag_1"))
        .with_columns(
            pl.when(pl.col("open") > pl.col("_alpha187_open_lag_1"))
            .then(pl.max_horizontal(pl.col("high") - pl.col("open"), pl.col("open") - pl.col("_alpha187_open_lag_1")))
            .otherwise(0.0)
            .alias("_alpha187_up_break")
        )
        .with_columns(pl.col("_alpha187_up_break").rolling_sum(20, min_samples=20).over("symbol").alias("factor_value"))
    )


def _compute_alpha189(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "close" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(pl.col("close").rolling_mean(6, min_samples=6).over("symbol").alias("_alpha189_close_ma6"))
        .with_columns((pl.col("close") - pl.col("_alpha189_close_ma6")).abs().alias("_alpha189_abs_ma6_gap"))
        .with_columns(pl.col("_alpha189_abs_ma6_gap").rolling_mean(6, min_samples=6).over("symbol").alias("factor_value"))
    )


def _compute_alpha095(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty() or "turnover" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .with_columns(
            pl.when(pl.col("turnover").is_not_null() & (pl.col("turnover") > 0))
            .then(pl.col("turnover").cast(pl.Float64))
            .otherwise(None)
            .alias("_alpha095_amount")
        )
        .with_columns(
            pl.col("_alpha095_amount")
            .rolling_std(20, min_samples=20)
            .over("symbol")
            .alias("factor_value")
        )
    )


def load_valuation_frame(
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    from ..database import StockEVC

    if not symbols:
        return pl.DataFrame()
    rows = (
        db.query(
            StockEVC.symbol,
            StockEVC.date,
            StockEVC.fair_value_lo,
            StockEVC.fair_value_hi,
            StockEVC.forward_pe_ratio,
            StockEVC.pe_ratio,
        )
        .filter(
            StockEVC.symbol.in_(symbols),
            StockEVC.date >= start_date,
            StockEVC.date <= end_date,
        )
        .all()
    )
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(
            {
                "symbol": [row.symbol for row in rows],
                "valuation_date": [row.date for row in rows],
                "fair_value_lo": [row.fair_value_lo for row in rows],
                "fair_value_hi": [row.fair_value_hi for row in rows],
                "forward_pe_ratio": [row.forward_pe_ratio for row in rows],
                "pe_ratio": [row.pe_ratio for row in rows],
            }
        )
        .with_columns(
            pl.col("valuation_date").cast(pl.Date),
            pl.col("fair_value_lo").cast(pl.Float64),
            pl.col("fair_value_hi").cast(pl.Float64),
            pl.col("forward_pe_ratio").cast(pl.Float64),
            pl.col("pe_ratio").cast(pl.Float64),
        )
        .with_columns(
            pl.coalesce(
                [
                    (pl.col("fair_value_lo") + pl.col("fair_value_hi")) / 2,
                    pl.col("fair_value_hi"),
                    pl.col("fair_value_lo"),
                ]
            ).alias("_fair_value_mid")
        )
        .filter(pl.col("_fair_value_mid").is_not_null() & (pl.col("_fair_value_mid") > 0))
        .sort(["symbol", "valuation_date"])
    )


_load_valuation_frame = load_valuation_frame


def _compute_valuation_gap(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    valuation_df = context.valuation_df
    if valuation_df is None:
        valuation_df = load_valuation_frame(context.db, context.symbols, context.start_date - timedelta(days=540), context.end_date)
    if valuation_df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    return (
        df.sort(["symbol", "trade_date"])
        .join_asof(valuation_df, left_on="trade_date", right_on="valuation_date", by="symbol", strategy="backward")
        .with_columns(((pl.col("_fair_value_mid") / pl.col("close")) - 1).alias("factor_value"))
    )


def _compute_index_weight(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    candidate_etfs = list(
        dict.fromkeys(
            context.candidate_etfs
            or list((context.weight_history or {}).keys())
            or []
        )
    )
    analysis_dates = context.analysis_dates or (
        df.filter((pl.col("trade_date") >= context.start_date) & (pl.col("trade_date") <= context.end_date))
        .select("trade_date")
        .unique()
        .sort("trade_date")
        .to_series()
        .to_list()
    )
    weight_history = context.weight_history
    if weight_history is None and context.weight_history_loader is not None:
        weight_history = context.weight_history_loader(context.db, candidate_etfs, context.start_date, context.end_date)
    if not analysis_dates or not weight_history or not candidate_etfs:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    etf_weight = 1.0 / len(candidate_etfs)
    sorted_weight_dates = {etf_symbol: sorted(history.keys()) for etf_symbol, history in weight_history.items()}
    records: List[Dict[str, Any]] = []
    for current_date in analysis_dates:
        combined_weights: Dict[str, float] = {}
        for etf_symbol in candidate_etfs:
            snapshot_dates = sorted_weight_dates.get(etf_symbol) or []
            date_index = bisect_right(snapshot_dates, current_date) - 1
            if date_index < 0:
                continue
            snapshot_date = snapshot_dates[int(date_index)]
            for symbol, weight in weight_history.get(etf_symbol, {}).get(snapshot_date, {}).items():
                combined_weights[symbol] = combined_weights.get(symbol, 0.0) + float(weight or 0.0) * etf_weight
        for symbol, weight in combined_weights.items():
            records.append({"trade_date": current_date, "symbol": symbol, "factor_value": weight})

    if not records:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    weight_df = pl.DataFrame(records).with_columns(pl.col("trade_date").cast(pl.Date), pl.col("factor_value").cast(pl.Float64))
    return df.join(weight_df, on=["symbol", "trade_date"], how="left")


def _compute_custom_momentum_volume(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    momentum = _compute_risk_adjusted_momentum(df, context).select(["symbol", "trade_date", "factor_value"])
    volume_context = FactorContext(
        windows=[20],
        momentum_weights=context.momentum_weights,
        db=context.db,
        symbols=context.symbols,
        start_date=context.start_date,
        end_date=context.end_date,
        candidate_etfs=context.candidate_etfs,
    )
    volume = _compute_volume_z(df, volume_context).select(["symbol", "trade_date", pl.col("factor_value").alias("_volume_z")])
    return (
        df.join(momentum.rename({"factor_value": "_momentum_score"}), on=["symbol", "trade_date"], how="left")
        .join(volume, on=["symbol", "trade_date"], how="left")
        .with_columns(
            (
                pl.col("_momentum_score").rank("average").over("trade_date")
                + pl.col("_volume_z").rank("average").over("trade_date") * 0.25
            ).alias("factor_value")
        )
    )


FACTOR_REGISTRY: Dict[str, FactorDefinition] = {
    "raw_momentum": FactorDefinition(
        key="raw_momentum",
        label="动量：原始动量",
        group="动量",
        description="与风险调整动量同源：ln(close) 回归斜率 * R2，不除以波动率，用来和风险调整版直接对照。",
        default_windows=SUPPORTED_MOMENTUM_WINDOWS.copy(),
        supports_windows=True,
        supports_mixed_windows=True,
        direction="higher_is_better",
        compute=_compute_raw_momentum,
    ),
    "risk_adjusted_momentum": FactorDefinition(
        key="risk_adjusted_momentum",
        label="动量：风险调整动量",
        group="动量",
        description="与多因子动量腿同源：ln(close) 回归斜率 * R2 / 年化波动；热力图按每个滑动窗口单独测试。",
        default_windows=SUPPORTED_MOMENTUM_WINDOWS.copy(),
        supports_windows=True,
        supports_mixed_windows=True,
        direction="higher_is_better",
        compute=_compute_risk_adjusted_momentum,
    ),
    "volume_z": FactorDefinition(
        key="volume_z",
        label="成交量：对数成交量Z分数",
        group="成交量",
        description="短窗口均值相对长窗口均值的 log10(volume) Z 分数；短窗口 M=max(N/20,1)，长窗口为更早的 N 天，与短窗口不重叠。",
        default_windows=SUPPORTED_WINDOWS.copy(),
        supports_windows=True,
        supports_mixed_windows=False,
        direction="exploratory",
        compute=_compute_volume_z,
    ),
    "volume_ratio": FactorDefinition(
        key="volume_ratio",
        label="成交量：均量比",
        group="成交量",
        description="短窗口平均成交量 / 长窗口平均成交量；短窗口 M=max(N/20,1)，长窗口为更早的 N 天，与短窗口不重叠。",
        default_windows=SUPPORTED_WINDOWS.copy(),
        supports_windows=True,
        supports_mixed_windows=False,
        direction="exploratory",
        compute=_compute_volume_ratio,
    ),
    "log_volume_ratio": FactorDefinition(
        key="log_volume_ratio",
        label="成交量：log均量比",
        group="成交量",
        description="log10(短窗口平均成交量 / 长窗口平均成交量)；短窗口 M=max(N/20,1)，长窗口为更早的 N 天，与短窗口不重叠。",
        default_windows=SUPPORTED_WINDOWS.copy(),
        supports_windows=True,
        supports_mixed_windows=False,
        direction="exploratory",
        compute=_compute_log_volume_ratio,
    ),
    "volatility": FactorDefinition(
        key="volatility",
        label="波动：年化波动率",
        group="波动",
        description="过去窗口日收益标准差年化；按低波更好进行方向调整。",
        default_windows=SUPPORTED_WINDOWS.copy(),
        supports_windows=True,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_volatility,
    ),
    "alpha005": FactorDefinition(
        key="alpha005",
        label="Alpha005：高点量能共振背离",
        group="国君191",
        description="原 Alpha005：-TSMAX(CORR(TSRANK(VOLUME,5),TSRANK(HIGH,5),5),3)，刻画高点与量能短期同步性；筛选样本高值更优。",
        default_windows=[5],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_alpha005,
    ),
    "alpha021": FactorDefinition(
        key="alpha021",
        label="Alpha021：6日均价趋势斜率",
        group="国君191",
        description="原 Alpha021：REGBETA(MEAN(CLOSE,6),SEQUENCE(6))，刻画6日均价的短期趋势斜率；筛选样本低值更优。",
        default_windows=[6],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha021,
    ),
    "alpha024": FactorDefinition(
        key="alpha024",
        label="Alpha024：5日平滑反转动量",
        group="国君191",
        description="原 Alpha024：SMA(CLOSE-DELAY(CLOSE,5),5,1)，刻画5日价格变化的平滑值；筛选样本低值更优。",
        default_windows=[5],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha024,
    ),
    "alpha027": FactorDefinition(
        key="alpha027",
        label="Alpha027：短周期动量摆动",
        group="国君191",
        description="原 Alpha027：WMA(3日与6日收益率之和,12)，刻画短周期动量摆动；筛选样本低值更优。",
        default_windows=[12],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha027,
    ),
    "alpha042": FactorDefinition(
        key="alpha042",
        label="Alpha042：高点量价背离",
        group="国君191",
        description="原 Alpha042：(-1 * RANK(STD(HIGH,10))) * CORR(HIGH,VOLUME,10)，刻画高点波动与成交量相关性的背离特征。",
        default_windows=[10],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_alpha042,
    ),
    "alpha046": FactorDefinition(
        key="alpha046",
        label="Alpha046：多均线乖离反转",
        group="国君191",
        description="原 Alpha046：(MA3+MA6+MA12+MA24)/(4*CLOSE)，价格低于多条均线时取值更高；筛选样本高值更优。",
        default_windows=[24],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_alpha046,
    ),
    "alpha052": FactorDefinition(
        key="alpha052",
        label="Alpha052：上下推动强弱比",
        group="国君191",
        description="原 Alpha052：SUM(MAX(0,HIGH-DELAY(TP,1)),26)/SUM(MAX(0,DELAY(TP,1)-LOW),26)*100，TP=(HIGH+LOW+CLOSE)/3；A创100样本低值更优。",
        default_windows=[26],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha052,
    ),
    "alpha059": FactorDefinition(
        key="alpha059",
        label="Alpha059：价格摆动累积",
        group="国君191",
        description="原 Alpha059：按收盘涨跌选择 CLOSE-MIN(LOW,DELAY(CLOSE,1)) 或 CLOSE-MAX(LOW,DELAY(CLOSE,1)) 后求20日和；A创100样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha059,
    ),
    "alpha088": FactorDefinition(
        key="alpha088",
        label="Alpha088：20日涨幅反转",
        group="国君191",
        description="原 Alpha088：(CLOSE-DELAY(CLOSE,20))/DELAY(CLOSE,20)*100，刻画20日涨幅；筛选样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha088,
    ),
    "alpha093": FactorDefinition(
        key="alpha093",
        label="Alpha093：开盘下探强度",
        group="国君191",
        description="原 Alpha093：SUM(IF(OPEN>=DELAY(OPEN,1),0,MAX(OPEN-LOW,OPEN-DELAY(OPEN,1))),20)，刻画开盘低于前开后的下探幅度累计；A创100长样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha093,
    ),
    "alpha095": FactorDefinition(
        key="alpha095",
        label="Alpha095：成交额波动率",
        group="国君191",
        description="原 Alpha095：STD(AMOUNT,20)，刻画20日成交额波动；A股检验中低成交额波动更优，默认按低值更好处理。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha095,
    ),
    "alpha106": FactorDefinition(
        key="alpha106",
        label="Alpha106：20日价差反转",
        group="国君191",
        description="原 Alpha106：CLOSE-DELAY(CLOSE,20)，刻画20日绝对价差；筛选样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha106,
    ),
    "alpha118": FactorDefinition(
        key="alpha118",
        label="Alpha118：上影下影强弱比",
        group="国君191",
        description="原 Alpha118：SUM(HIGH-OPEN,20)/SUM(OPEN-LOW,20)*100，刻画20日上行影线相对下行影线强弱；筛选样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha118,
    ),
    "alpha122": FactorDefinition(
        key="alpha122",
        label="Alpha122：三重平滑对数趋势",
        group="国君191",
        description="原 Alpha122：三重SMA(LOG(CLOSE),13,2)的一阶变化率，刻画平滑后的价格趋势变化；A创100样本低值更优。",
        default_windows=[13],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha122,
    ),
    "alpha129": FactorDefinition(
        key="alpha129",
        label="Alpha129：12日下跌幅累积",
        group="国君191",
        description="原 Alpha129：SUM(IF(CLOSE-DELAY(CLOSE,1)<0,ABS(CLOSE-DELAY(CLOSE,1)),0),12)，刻画近12日下跌价差累计；A创100长样本低值更优。",
        default_windows=[12],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha129,
    ),
    "alpha132": FactorDefinition(
        key="alpha132",
        label="Alpha132：20日成交额均值",
        group="国君191",
        description="原 Alpha132：MEAN(AMOUNT,20)，刻画20日平均成交额；2021后A创100与中证500样本均偏低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha132,
    ),
    "alpha134": FactorDefinition(
        key="alpha134",
        label="Alpha134：12日价量反转",
        group="国君191",
        description="原 Alpha134：(CLOSE-DELAY(CLOSE,12))/DELAY(CLOSE,12)*VOLUME，刻画12日涨跌幅与成交量的复合强度；筛选样本低值更优。",
        default_windows=[12],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha134,
    ),
    "alpha135": FactorDefinition(
        key="alpha135",
        label="Alpha135：平滑20日动量",
        group="国君191",
        description="原 Alpha135：SMA(DELAY(CLOSE/DELAY(CLOSE,20),1),20,1)，刻画平滑后的20日动量比值；筛选样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha135,
    ),
    "alpha139": FactorDefinition(
        key="alpha139",
        label="Alpha139：开盘量价背离",
        group="国君191",
        description="原 Alpha139：-CORR(OPEN,VOLUME,10)，刻画开盘价与成交量的短期相关性背离；A创100与中证500样本均为高值更优。",
        default_windows=[10],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_alpha139,
    ),
    "alpha145": FactorDefinition(
        key="alpha145",
        label="Alpha145：成交量均线背离",
        group="国君191",
        description="原 Alpha145：(MA(VOLUME,9)-MA(VOLUME,26))/MA(VOLUME,12)*100，刻画成交量短长均线背离；筛选样本低值更优。",
        default_windows=[26],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha145,
    ),
    "alpha147": FactorDefinition(
        key="alpha147",
        label="Alpha147：12日均价趋势斜率",
        group="国君191",
        description="原 Alpha147：REGBETA(MEAN(CLOSE,12),SEQUENCE(12))，刻画12日均价趋势斜率；A创100长样本低值更优。",
        default_windows=[12],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha147,
    ),
    "alpha151": FactorDefinition(
        key="alpha151",
        label="Alpha151：平滑20日价差反转",
        group="国君191",
        description="原 Alpha151：SMA(CLOSE-DELAY(CLOSE,20),20,1)，刻画20日绝对价差的平滑动量；A创100样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha151,
    ),
    "alpha158": FactorDefinition(
        key="alpha158",
        label="Alpha158：日内振幅率",
        group="国君191",
        description="原 Alpha158：((HIGH-SMA(CLOSE,15,2))-(LOW-SMA(CLOSE,15,2)))/CLOSE，等价于日内振幅/收盘价；筛选样本低值更优。",
        default_windows=[15],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha158,
    ),
    "alpha160": FactorDefinition(
        key="alpha160",
        label="Alpha160：下跌波动平滑",
        group="国君191",
        description="原 Alpha160：SMA(IF(CLOSE<=DELAY(CLOSE,1),STD(CLOSE,20),0),20,1)，刻画下跌日价格波动的平滑强度；A创100长样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha160,
    ),
    "alpha161": FactorDefinition(
        key="alpha161",
        label="Alpha161：12日真实波幅",
        group="国君191",
        description="原 Alpha161：MEAN(MAX(MAX(HIGH-LOW,ABS(DELAY(CLOSE,1)-HIGH)),ABS(DELAY(CLOSE,1)-LOW)),12)，刻画12日平均真实波幅；A创100与中证500长样本低值更优。",
        default_windows=[12],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha161,
    ),
    "alpha167": FactorDefinition(
        key="alpha167",
        label="Alpha167：12日上涨幅累积",
        group="国君191",
        description="原 Alpha167：SUM(IF(CLOSE>DELAY(CLOSE,1),CLOSE-DELAY(CLOSE,1),0),12)，刻画近12日上涨价差累计；A创100长样本低值更优。",
        default_windows=[12],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha167,
    ),
    "alpha169": FactorDefinition(
        key="alpha169",
        label="Alpha169：平滑差分动量",
        group="国君191",
        description="原 Alpha169：SMA(MA(DELAY(SMA(CLOSE-DELAY(CLOSE,1),9,1),1),12)-MA(...,26),10,1)，刻画平滑价差动量；筛选样本低值更优。",
        default_windows=[26],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha169,
    ),
    "alpha174": FactorDefinition(
        key="alpha174",
        label="Alpha174：上涨波动平滑",
        group="国君191",
        description="原 Alpha174：SMA(IF(CLOSE>DELAY(CLOSE,1),STD(CLOSE,20),0),20,1)，刻画上涨日价格波动的平滑强度；A创100长样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha174,
    ),
    "alpha175": FactorDefinition(
        key="alpha175",
        label="Alpha175：6日真实波幅",
        group="国君191",
        description="原 Alpha175：MEAN(MAX(MAX(HIGH-LOW,ABS(DELAY(CLOSE,1)-HIGH)),ABS(DELAY(CLOSE,1)-LOW)),6)，刻画6日平均真实波幅；A创100长样本低值更优。",
        default_windows=[6],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha175,
    ),
    "alpha187": FactorDefinition(
        key="alpha187",
        label="Alpha187：开盘向上突破强度",
        group="国君191",
        description="原 Alpha187：SUM(IF(OPEN<=DELAY(OPEN,1),0,MAX(HIGH-OPEN,OPEN-DELAY(OPEN,1))),20)，刻画20日开盘向上突破强度；筛选样本低值更优。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha187,
    ),
    "alpha189": FactorDefinition(
        key="alpha189",
        label="Alpha189：6日均价偏离度",
        group="国君191",
        description="原 Alpha189：MEAN(ABS(CLOSE-MEAN(CLOSE,6)),6)，刻画收盘价相对6日均价的平均偏离；A创100与中证500长样本低值更优。",
        default_windows=[6],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="lower_is_better",
        compute=_compute_alpha189,
    ),
    "valuation_gap": FactorDefinition(
        key="valuation_gap",
        label="估值：安全边际",
        group="估值",
        description="使用最近一次EVC估值中值 / 当日收盘价 - 1，越高代表相对低估。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_valuation_gap,
    ),
    "index_weight": FactorDefinition(
        key="index_weight",
        label="指数：成分权重",
        group="指数",
        description="股票在所选股票池ETF中的成分权重；SPY+QQQ按两个ETF等权合成后再做截面排名。",
        default_windows=[20],
        supports_windows=False,
        supports_mixed_windows=False,
        direction="higher_is_better",
        compute=_compute_index_weight,
        unsupported_pool_types=["custom"],
    ),
    "custom_momentum_volume": FactorDefinition(
        key="custom_momentum_volume",
        label="自定义：动量+成交量示例",
        group="自定义",
        description="示例注册因子：风险调整混合动量截面排名 + 0.25 * 成交量Z分数截面排名。",
        default_windows=SUPPORTED_MOMENTUM_WINDOWS.copy(),
        supports_windows=True,
        supports_mixed_windows=True,
        direction="higher_is_better",
        compute=_compute_custom_momentum_volume,
    ),
}


def register_factor(definition: FactorDefinition):
    FACTOR_REGISTRY[definition.key] = definition


def _apply_factor_direction(df: pl.DataFrame, factor_definition: FactorDefinition) -> pl.DataFrame:
    if df.is_empty() or "factor_value" not in df.columns:
        return df
    direction = FACTOR_DIRECTION_OPTIONS.get(factor_definition.direction, FACTOR_DIRECTION_OPTIONS["exploratory"])
    sign = float(direction["sign"])
    return df.with_columns(
        pl.col("factor_value").alias("factor_value_raw"),
        (pl.col("factor_value") * sign).alias("factor_value_directional"),
        (pl.col("factor_value") * sign).alias("factor_value"),
    )


def _with_neutralization_columns(
    df: pl.DataFrame,
    industry_df: Optional[pl.DataFrame],
    neutralization: str,
) -> pl.DataFrame:
    source = df
    if industry_df is not None and not industry_df.is_empty():
        source = source.sort(["symbol", "trade_date"]).join(industry_df, on="symbol", how="left")
    for column in ["industry_group", "industry", "sector", "sub_industry", "market_cap"]:
        if column not in source.columns:
            source = source.with_columns(pl.lit(None).alias(column))
    source = source.with_columns(
        pl.coalesce([pl.col("sector"), pl.lit("Unknown")]).alias("_neutralization_sector"),
        pl.coalesce([pl.col("industry"), pl.col("sector"), pl.lit("Unknown")]).alias("_neutralization_fine_industry"),
        pl.when(pl.col("market_cap").is_not_null() & (pl.col("market_cap") > 0))
        .then(pl.col("market_cap").cast(pl.Float64))
        .otherwise(None)
        .alias("_neutralization_market_cap"),
    )
    if neutralization.startswith("fine_industry"):
        source = source.with_columns(
            pl.when(pl.col("factor_value").is_not_null() & pl.col("factor_value").is_finite())
            .then(1)
            .otherwise(0)
            .sum()
            .over(["trade_date", "_neutralization_fine_industry"])
            .alias("_fine_industry_sample_count")
        )
        return source.with_columns(
            pl.when(
                pl.col("industry").is_not_null()
                & (pl.col("_fine_industry_sample_count") >= MIN_FINE_INDUSTRY_NEUTRALIZATION_SIZE)
            )
            .then(pl.col("_neutralization_fine_industry"))
            .otherwise(pl.col("_neutralization_sector"))
            .alias("_neutralization_industry")
        )
    return source.with_columns(pl.col("_neutralization_sector").alias("_neutralization_industry"))


def _neutralize_group(group: pl.DataFrame, mode: str) -> pl.DataFrame:
    if group.is_empty() or "factor_value" not in group.columns:
        return group
    y = group.get_column("factor_value").cast(pl.Float64).to_numpy()
    finite_y = np.isfinite(y)
    if finite_y.sum() < 2:
        return group.with_columns(pl.Series("factor_value_neutralized", y))

    labels = group.get_column("_neutralization_industry").fill_null("Unknown").cast(pl.Utf8).to_list()
    _, label_inverse = np.unique(np.asarray(labels, dtype=str), return_inverse=True)

    x_parts = [np.ones((group.height, 1), dtype=float)]
    category_count = int(label_inverse.max()) + 1 if label_inverse.size else 0
    if category_count > 1:
        x_parts.append(np.eye(category_count, dtype=float)[label_inverse][:, 1:])

    if mode.endswith("_market_cap") and "_neutralization_market_cap" in group.columns:
        market_cap = group.get_column("_neutralization_market_cap").cast(pl.Float64).to_numpy()
        finite_market_cap = np.isfinite(market_cap) & (market_cap > 0)
        if finite_market_cap.any():
            median_market_cap = float(np.nanmedian(market_cap[finite_market_cap]))
            filled_market_cap = np.where(finite_market_cap, market_cap, median_market_cap)
            log_market_cap = np.log(np.clip(filled_market_cap, 1.0, None))
            log_market_cap = log_market_cap - float(np.nanmean(log_market_cap))
            if np.nanstd(log_market_cap) > 1e-12:
                x_parts.append(log_market_cap.reshape(-1, 1))

    x = np.hstack(x_parts)
    fit_mask = finite_y & np.all(np.isfinite(x), axis=1)
    if fit_mask.sum() < max(3, x.shape[1] + 1):
        residual = y - float(np.nanmean(y[finite_y]))
    else:
        try:
            beta, *_ = np.linalg.lstsq(x[fit_mask], y[fit_mask], rcond=None)
            residual = y - x @ beta
        except np.linalg.LinAlgError:
            residual = y - float(np.nanmean(y[finite_y]))

    residual = np.where(np.isfinite(residual), residual, y)
    return group.with_columns(pl.Series("factor_value_neutralized", residual))


def _apply_factor_neutralization(
    df: pl.DataFrame,
    neutralization: str,
    industry_df: Optional[pl.DataFrame],
) -> pl.DataFrame:
    if df.is_empty() or neutralization == "none":
        return df
    if industry_df is None or industry_df.is_empty():
        logger.warning("Factor neutralization requested but industry snapshot data is empty")
        return df.with_columns(pl.col("factor_value").alias("factor_value_neutralized"))
    return (
        _with_neutralization_columns(df, industry_df, neutralization)
        .group_by("trade_date", maintain_order=True)
        .map_groups(lambda group: _neutralize_group(group, neutralization))
        .with_columns(pl.col("factor_value_neutralized").alias("factor_value"))
    )


def _apply_factor_standardization(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "factor_value" not in df.columns:
        return df
    return (
        df.with_columns(
            pl.when(pl.col("factor_value").is_finite())
            .then(pl.col("factor_value"))
            .otherwise(None)
            .alias("_factor_for_standardization")
        )
        .with_columns(
            pl.mean("_factor_for_standardization").over("trade_date").alias("_factor_mean"),
            pl.std("_factor_for_standardization").over("trade_date").alias("_factor_std"),
        )
        .with_columns(
            pl.when((pl.col("_factor_std") > 0) & pl.col("factor_value").is_finite())
            .then((pl.col("factor_value") - pl.col("_factor_mean")) / pl.col("_factor_std"))
            .otherwise(pl.col("factor_value"))
            .alias("factor_value_standardized")
        )
        .with_columns(pl.col("factor_value_standardized").alias("factor_value"))
        .drop(["_factor_for_standardization", "_factor_mean", "_factor_std"])
    )


def _apply_factor_rank_percentile(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "factor_value" not in df.columns:
        return df
    return _with_cross_section_rank_percentile(df, "factor_value", "factor_value_rank_percentile").with_columns(
        pl.col("factor_value_rank_percentile").alias("factor_value")
    )


def _apply_factor_transformations(df: pl.DataFrame, request: Any, context: FactorContext) -> pl.DataFrame:
    result = df
    neutralization = _get_attr(request, "neutralization", "none")
    standardization = _get_attr(request, "standardization", "rank_percentile")
    if neutralization != "none":
        result = _apply_factor_neutralization(result, neutralization, context.industry_df)
    if standardization == "zscore":
        result = _apply_factor_standardization(result)
    elif standardization == "rank_percentile":
        result = _apply_factor_rank_percentile(result)
    return result


def _prepare_factor_frame(price_df: pl.DataFrame, factor_definition: FactorDefinition, context: FactorContext, request: Any) -> pl.DataFrame:
    return _apply_factor_transformations(_apply_factor_direction(factor_definition.compute(price_df, context), factor_definition), request, context)


def _prepare_momentum_factor_frame_from_source(
    source_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    context: FactorContext,
    request: Any,
) -> pl.DataFrame:
    prefix = MOMENTUM_FACTOR_SCORE_PREFIX.get(factor_definition.key)
    if source_df.is_empty() or not prefix:
        return source_df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))
    base_columns = [column for column in ["symbol", "trade_date", "open", "high", "low", "close", "volume", "turnover", "_first_trade_date"] if column in source_df.columns]
    result = source_df.select(base_columns).unique(subset=["symbol", "trade_date"])
    factor_expr = None
    raw_factor_expr = None
    for window, weight in context.momentum_weights.items():
        column = f"{prefix}_{int(window)}_score"
        if column not in source_df.columns:
            continue
        window_factor_column = f"_window_factor_{int(window)}"
        window_raw_factor_column = f"_window_factor_raw_{int(window)}"
        window_df = source_df.select([*base_columns, column]).with_columns(pl.col(column).alias("factor_value"))
        window_df = _apply_factor_transformations(
            _apply_factor_direction(window_df, factor_definition),
            request,
            context,
        ).select(
            "symbol",
            "trade_date",
            pl.col("factor_value").alias(window_factor_column),
            pl.col("factor_value_raw").alias(window_raw_factor_column),
        )
        result = result.join(window_df, on=["symbol", "trade_date"], how="left")
        expr = pl.col(window_factor_column) * float(weight)
        factor_expr = expr if factor_expr is None else factor_expr + expr
        raw_expr = pl.col(window_raw_factor_column) * float(weight)
        raw_factor_expr = raw_expr if raw_factor_expr is None else raw_factor_expr + raw_expr
    result = result.with_columns(
        (factor_expr if factor_expr is not None else pl.lit(None, dtype=pl.Float64)).alias("factor_value"),
        (raw_factor_expr if raw_factor_expr is not None else pl.lit(None, dtype=pl.Float64)).alias("factor_value_raw"),
    )
    return result.select([*base_columns, "factor_value", "factor_value_raw"])
