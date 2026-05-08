import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Literal, Optional

import polars as pl
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session as ORMSession

from ...core.analytics_database import ANALYTICS_DB_PATH
from ...core.database import StockEVC, get_db
from ...robot.us_stock_signal_virtual import (
    DEFAULT_MOMENTUM_WEIGHTS,
    SUPPORTED_MOMENTUM_WINDOWS,
    load_universe_history,
)
from .account import valid_account


router = APIRouter(prefix="/api/factor-lab", tags=["Factor Lab"])
logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
SUPPORTED_WINDOWS = [20, 60, 120]
DEFAULT_FORWARD_WINDOWS = [5, 20, 60]
DEFAULT_START_DATE = date(2020, 1, 2)
DEFAULT_MIN_LISTING_DAYS = 365
MAX_HEATMAP_CELLS = 20
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
FACTOR_DIRECTION_OPTIONS = {
    "higher_is_better": {"sign": 1.0, "label": "高值更好"},
    "lower_is_better": {"sign": -1.0, "label": "低值更好"},
    "exploratory": {"sign": 1.0, "label": "探索方向"},
}


POOL_OPTIONS = [
    {
        "key": "QQQ",
        "label": "QQQ",
        "description": "纳指100成分股",
        "etfs": ["QQQ.US"],
    },
    {
        "key": "SPY",
        "label": "SPY",
        "description": "标普500成分股",
        "etfs": ["SPY.US"],
    },
    {
        "key": "SPY_QQQ",
        "label": "SPY+QQQ",
        "description": "标普500与纳指100成分股并集",
        "etfs": ["SPY.US", "QQQ.US"],
    },
]

POOL_ETFS = {item["key"]: item["etfs"] for item in POOL_OPTIONS}


class FactorLabAnalyzeRequest(BaseModel):
    pool: Literal["QQQ", "SPY", "SPY_QQQ"] = "SPY_QQQ"
    factor: str = "risk_adjusted_momentum"
    bucket_count: int = 10
    start_date: date = DEFAULT_START_DATE
    end_date: Optional[date] = None
    heatmap_windows: List[int] = Field(default_factory=lambda: SUPPORTED_WINDOWS.copy())
    heatmap_forward_windows: List[int] = Field(default_factory=lambda: DEFAULT_FORWARD_WINDOWS.copy())
    momentum_weights: Dict[str, float] = Field(default_factory=lambda: DEFAULT_MOMENTUM_WEIGHTS.copy())
    min_listing_days: int = DEFAULT_MIN_LISTING_DAYS
    include_heatmap: bool = True

    @validator("heatmap_windows", pre=True)
    def validate_windows(cls, value):
        items = value if isinstance(value, list) else [value]
        normalized: List[int] = []
        for item in items:
            try:
                window = int(item)
            except (TypeError, ValueError):
                raise ValueError("窗口必须是数字")
            if window not in SUPPORTED_WINDOWS:
                raise ValueError(f"窗口只支持: {', '.join(str(item) for item in SUPPORTED_WINDOWS)}")
            if window not in normalized:
                normalized.append(window)
        if not normalized:
            raise ValueError("至少选择一个窗口")
        return normalized

    @validator("heatmap_forward_windows", pre=True)
    def validate_heatmap_forward_windows(cls, value):
        items = value if isinstance(value, list) else [value]
        normalized: List[int] = []
        for item in items:
            try:
                window = int(item)
            except (TypeError, ValueError):
                raise ValueError("热力图收益窗口必须是数字")
            if window < 1 or window > 252:
                raise ValueError("热力图收益窗口必须在 1 到 252 之间")
            if window not in normalized:
                normalized.append(window)
        if not normalized:
            raise ValueError("至少选择一个热力图收益窗口")
        return normalized[:6]

    @validator("bucket_count")
    def validate_bucket_count(cls, value):
        if value < 2 or value > 20:
            raise ValueError("分桶数必须在 2 到 20 之间")
        return int(value)

    @validator("min_listing_days")
    def validate_min_listing_days(cls, value):
        days = int(value)
        if days < 0 or days > 3650:
            raise ValueError("上市天数过滤必须在 0 到 3650 天之间")
        return days

    @validator("momentum_weights", pre=True)
    def validate_momentum_weights(cls, value):
        return _normalize_momentum_weights_payload(value)

    @validator("end_date")
    def validate_date_range(cls, value, values):
        start = values.get("start_date")
        if value is not None and start is not None and value <= start:
            raise ValueError("结束日期必须晚于开始日期")
        return value


class FactorLabOptionsResponse(BaseModel):
    pools: List[Dict[str, Any]]
    factors: List[Dict[str, Any]]
    windows: List[int]
    forward_windows: List[int]
    default_request: Dict[str, Any]


@dataclass(frozen=True)
class FactorContext:
    windows: List[int]
    momentum_weights: Dict[int, float]
    db: ORMSession
    symbols: List[str]
    start_date: date
    end_date: date


@dataclass(frozen=True)
class FactorDefinition:
    key: str
    label: str
    group: str
    description: str
    default_windows: List[int]
    supports_windows: bool
    direction: str
    compute: Callable[[pl.DataFrame, FactorContext], pl.DataFrame]

    def to_option(self) -> Dict[str, Any]:
        direction = FACTOR_DIRECTION_OPTIONS.get(self.direction, FACTOR_DIRECTION_OPTIONS["exploratory"])
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "description": self.description,
            "default_windows": self.default_windows,
            "supports_windows": self.supports_windows,
            "direction": self.direction,
            "direction_label": direction["label"],
            "direction_sign": direction["sign"],
        }


def _quote_sql_string(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def _normalize_momentum_weights(raw_weights: Dict[str, float], active_windows: List[int]) -> Dict[int, float]:
    active = list(dict.fromkeys(int(item) for item in active_windows))
    weights: Dict[int, float] = {}
    for window in active:
        raw_value = raw_weights.get(str(window), raw_weights.get(window, 0.0))
        try:
            weights[window] = max(0.0, float(raw_value or 0))
        except (TypeError, ValueError):
            weights[window] = 0.0
    total = sum(weights.values())
    if total <= 0:
        return {window: 1.0 / len(active) for window in active}
    return {window: weight / total for window, weight in weights.items() if weight > 0}


def _normalize_momentum_weights_payload(raw_weights: Dict[str, float]) -> Dict[str, float]:
    raw = raw_weights if isinstance(raw_weights, dict) else DEFAULT_MOMENTUM_WEIGHTS
    normalized: Dict[str, float] = {}
    for window in SUPPORTED_MOMENTUM_WINDOWS:
        try:
            weight = float(raw.get(str(window), raw.get(window, 0)) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{window}日动量权重必须是数字")
        if weight < 0:
            raise ValueError(f"{window}日动量权重不能为负数")
        normalized[str(window)] = weight
    if sum(normalized.values()) <= 0:
        raise ValueError("至少设置一个大于0的动量权重")
    return normalized


def _safe_float(value: Any, digits: Optional[int] = None) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits) if digits is not None else number


def _annualize_period_return_pct(period_return_pct: Any, forward_window: int) -> Optional[float]:
    period_return = _safe_float(period_return_pct)
    if period_return is None or forward_window <= 0:
        return None
    base = 1 + period_return / 100
    if base <= 0:
        return None
    annualized = (base ** (TRADING_DAYS_PER_YEAR / forward_window) - 1) * 100
    return _safe_float(annualized, 4)


def _mean(values: List[float]) -> Optional[float]:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _median(values: List[float]) -> Optional[float]:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not clean:
        return None
    middle = len(clean) // 2
    if len(clean) % 2:
        return clean[middle]
    return (clean[middle - 1] + clean[middle]) / 2


def _record_float(record: Dict[str, Any], key: str, fallback: float) -> float:
    value = record.get(key)
    if value is None:
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _serialize_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return _safe_float(value)
    return value


def _records(df: pl.DataFrame, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if df.is_empty():
        return []
    source = df.head(limit) if limit else df
    return [
        {key: _serialize_value(value) for key, value in row.items()}
        for row in source.to_dicts()
    ]


def _import_duckdb():
    try:
        import duckdb
    except Exception as exc:
        raise HTTPException(status_code=500, detail="DuckDB依赖不可用") from exc
    return duckdb


def _connect_duckdb():
    duckdb = _import_duckdb()
    try:
        return duckdb.connect(database=ANALYTICS_DB_PATH, read_only=True)
    except Exception as exc:
        logger.warning("Read-only DuckDB connection failed, retrying writable connection: %s", exc)
        return duckdb.connect(database=ANALYTICS_DB_PATH, read_only=False)


def _get_max_trade_date() -> date:
    connection = _connect_duckdb()
    try:
        row = connection.execute("SELECT MAX(trade_date) FROM us_stock_daily").fetchone()
    finally:
        connection.close()
    value = row[0] if row else None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return datetime.fromisoformat(str(value)).date()
    return date.today()


def _load_price_frame(symbols: List[str], start_date: date, end_date: date) -> pl.DataFrame:
    safe_symbols = [
        symbol for symbol in list(dict.fromkeys(symbols))
        if symbol and SYMBOL_PATTERN.match(symbol)
    ]
    if not safe_symbols:
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "trade_date": pl.Date,
                "close": pl.Float64,
                "volume": pl.Float64,
                "turnover": pl.Float64,
            }
        )

    symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in safe_symbols)
    query = f"""
        SELECT
            symbol,
            CAST(trade_date AS DATE) AS trade_date,
            CAST(close AS DOUBLE) AS close,
            CAST(volume AS DOUBLE) AS volume,
            CAST(turnover AS DOUBLE) AS turnover
        FROM us_stock_daily
        WHERE symbol IN ({symbol_sql})
          AND trade_date BETWEEN ? AND ?
          AND close IS NOT NULL
          AND close > 0
        ORDER BY symbol, trade_date
    """
    connection = _connect_duckdb()
    try:
        df = pl.read_database(
            query,
            connection,
            execute_options={"parameters": [start_date, end_date]},
        )
    finally:
        connection.close()

    if df.is_empty():
        return df
    return df.with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("turnover").cast(pl.Float64),
    ).sort(["symbol", "trade_date"]).with_columns(
        pl.min("trade_date").over("symbol").alias("_first_trade_date")
    )


def _load_valuation_frame(
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
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
        .then(
            pl.col(f"{prefix}_annualized_slope_pct")
            * pl.col(f"{prefix}_r_squared")
            / pl.col(f"{prefix}_annualized_vol_pct")
            * 100
        )
        .otherwise(None)
        .alias(f"{prefix}_score")
    )


def _add_raw_momentum_score(df: pl.DataFrame, window: int) -> pl.DataFrame:
    w = int(window)
    prefix = f"_raw_mom_{w}"
    df = _add_momentum_window_features(df, w, prefix)
    return df.with_columns(
        pl.when(pl.col(f"{prefix}_r_squared").is_not_null())
        .then(
            pl.col(f"{prefix}_annualized_slope_pct")
            * pl.col(f"{prefix}_r_squared")
        )
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
    return result.with_columns(factor_expr.alias("factor_value"))


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
    return result.with_columns(factor_expr.alias("factor_value"))


def _compute_volume_z(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    window = int(context.windows[0])
    result = _ensure_base_columns(df)
    return (
        result.with_columns(
            pl.col("_log_volume").shift(1).rolling_mean(window, min_samples=window).over("symbol").alias("_volume_avg"),
            pl.col("_log_volume").shift(1).rolling_std(window, min_samples=window).over("symbol").alias("_volume_std"),
        )
        .with_columns(
            pl.when(pl.col("_volume_std") > 0)
            .then((pl.col("_log_volume") - pl.col("_volume_avg")) / pl.col("_volume_std"))
            .otherwise(None)
            .alias("factor_value")
        )
    )


def _compute_volatility(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    window = int(context.windows[0])
    result = _ensure_base_columns(df)
    return result.with_columns(
        (
            pl.col("_daily_return").rolling_std(window, min_samples=window).over("symbol")
            * math.sqrt(TRADING_DAYS_PER_YEAR)
            * 100
        ).alias("factor_value")
    )


def _compute_valuation_gap(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    valuation_df = _load_valuation_frame(
        context.db,
        context.symbols,
        context.start_date - timedelta(days=540),
        context.end_date,
    )
    if valuation_df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    result = (
        df.sort(["symbol", "trade_date"])
        .join_asof(
            valuation_df,
            left_on="trade_date",
            right_on="valuation_date",
            by="symbol",
            strategy="backward",
        )
        .with_columns(((pl.col("_fair_value_mid") / pl.col("close")) - 1).alias("factor_value"))
    )
    return result


def _compute_custom_momentum_volume(df: pl.DataFrame, context: FactorContext) -> pl.DataFrame:
    momentum = _compute_risk_adjusted_momentum(df, context).select(["symbol", "trade_date", "factor_value"])
    volume_context = FactorContext(
        windows=[20],
        momentum_weights=context.momentum_weights,
        db=context.db,
        symbols=context.symbols,
        start_date=context.start_date,
        end_date=context.end_date,
    )
    volume = _compute_volume_z(df, volume_context).select(
        ["symbol", "trade_date", pl.col("factor_value").alias("_volume_z")]
    )
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
        default_windows=[20, 60, 120],
        supports_windows=True,
        direction="higher_is_better",
        compute=_compute_raw_momentum,
    ),
    "risk_adjusted_momentum": FactorDefinition(
        key="risk_adjusted_momentum",
        label="动量：风险调整动量",
        group="动量",
        description="与美股风险调整混合动量虚拟盘同源：ln(close) 回归斜率 * R2 / 年化波动；热力图按每个滑动窗口单独测试。",
        default_windows=[20, 60, 120],
        supports_windows=True,
        direction="higher_is_better",
        compute=_compute_risk_adjusted_momentum,
    ),
    "volume_z": FactorDefinition(
        key="volume_z",
        label="成交量：对数成交量Z分数",
        group="成交量",
        description="log10(volume) 相对过去窗口均值和标准差的异常程度，窗口不含当天；方向先作为探索项观察。",
        default_windows=[20],
        supports_windows=True,
        direction="exploratory",
        compute=_compute_volume_z,
    ),
    "volatility": FactorDefinition(
        key="volatility",
        label="波动：年化波动率",
        group="波动",
        description="过去窗口日收益标准差年化；按低波更好进行方向调整。",
        default_windows=[20],
        supports_windows=True,
        direction="exploratory",
        compute=_compute_volatility,
    ),
    "valuation_gap": FactorDefinition(
        key="valuation_gap",
        label="估值：安全边际",
        group="估值",
        description="使用最近一次EVC估值中值 / 当日收盘价 - 1，越高代表相对低估。",
        default_windows=[20],
        supports_windows=False,
        direction="higher_is_better",
        compute=_compute_valuation_gap,
    ),
    "custom_momentum_volume": FactorDefinition(
        key="custom_momentum_volume",
        label="自定义：动量+成交量示例",
        group="自定义",
        description="示例注册因子：风险调整混合动量截面排名 + 0.25 * 成交量Z分数截面排名。",
        default_windows=[20, 60, 120],
        supports_windows=True,
        direction="higher_is_better",
        compute=_compute_custom_momentum_volume,
    ),
}


def register_factor(definition: FactorDefinition):
    """Register a Python factor function for Factor Lab.

    Add local custom factors by creating a FactorDefinition and calling this
    function during app startup/import.
    """
    FACTOR_REGISTRY[definition.key] = definition


def _apply_factor_direction(df: pl.DataFrame, factor_definition: FactorDefinition) -> pl.DataFrame:
    if df.is_empty() or "factor_value" not in df.columns:
        return df

    direction = FACTOR_DIRECTION_OPTIONS.get(factor_definition.direction, FACTOR_DIRECTION_OPTIONS["exploratory"])
    sign = float(direction["sign"])
    return df.with_columns(
        pl.col("factor_value").alias("factor_value_raw"),
        (pl.col("factor_value") * sign).alias("factor_value"),
    )


def _add_forward_return(df: pl.DataFrame, forward_window: int) -> pl.DataFrame:
    return df.with_columns(
        pl.col("close").shift(-forward_window).over("symbol").alias("_future_close")
    ).with_columns(
        ((pl.col("_future_close") / pl.col("close")) - 1).alias("forward_return"),
        (pl.col("_future_close").log() - pl.col("close").log()).alias("forward_log_return"),
    )


def _filter_min_listing_days(df: pl.DataFrame, min_listing_days: int) -> pl.DataFrame:
    if df.is_empty() or min_listing_days <= 0:
        return df

    source = df
    if "_first_trade_date" not in source.columns:
        source = source.with_columns(pl.min("trade_date").over("symbol").alias("_first_trade_date"))

    return (
        source.with_columns(
            (pl.col("trade_date") - pl.col("_first_trade_date"))
            .dt.total_days()
            .alias("_listing_days")
        )
        .filter(pl.col("_listing_days") >= int(min_listing_days))
    )


def _build_universe_frame(universe_history, trade_dates: List[date]) -> pl.DataFrame:
    date_values: List[date] = []
    symbol_values: List[str] = []
    for trade_date in trade_dates:
        symbols = universe_history.symbols_for_date(trade_date)
        if not symbols:
            continue
        date_values.extend([trade_date] * len(symbols))
        symbol_values.extend(symbols)

    if not date_values:
        return pl.DataFrame(schema={"trade_date": pl.Date, "symbol": pl.Utf8})
    return pl.DataFrame({"trade_date": date_values, "symbol": symbol_values}).with_columns(
        pl.col("trade_date").cast(pl.Date)
    )


def _prepare_factor_sample(
    factor_df: pl.DataFrame,
    universe_df: pl.DataFrame,
    request: FactorLabAnalyzeRequest,
    forward_window: int,
) -> pl.DataFrame:
    if factor_df.is_empty() or universe_df.is_empty():
        return pl.DataFrame()

    df = (
        _filter_min_listing_days(
            _add_forward_return(factor_df.sort(["symbol", "trade_date"]), forward_window),
            request.min_listing_days,
        )
        .filter(
            (pl.col("trade_date") >= request.start_date)
            & (pl.col("trade_date") <= (request.end_date or date.today()))
            & pl.col("factor_value").is_not_null()
            & pl.col("factor_value").is_finite()
            & pl.col("forward_return").is_not_null()
            & pl.col("forward_return").is_finite()
        )
        .join(universe_df, on=["trade_date", "symbol"], how="inner")
    )
    return df


def _assign_buckets(df: pl.DataFrame, bucket_count: int) -> pl.DataFrame:
    if df.is_empty():
        return df

    return (
        df.with_columns(pl.len().over("trade_date").alias("_date_count"))
        .filter(pl.col("_date_count") >= bucket_count)
        .with_columns(
            pl.col("factor_value").rank(method="ordinal").over("trade_date").alias("_factor_rank")
        )
        .with_columns(
            (
                ((pl.col("_factor_rank") - 1) * bucket_count / pl.col("_date_count")).floor() + 1
            )
            .clip(1, bucket_count)
            .cast(pl.Int64)
            .alias("bucket")
        )
        .with_columns(
            pl.mean("forward_return").over("trade_date").alias("_cross_section_return"),
            (pl.col("forward_return") - pl.mean("forward_return").over("trade_date")).alias("forward_excess_return"),
        )
    )


def _compute_bucket_report(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame()

    return (
        df.group_by("bucket")
        .agg(
            pl.len().alias("samples"),
            pl.n_unique("trade_date").alias("trade_dates"),
            (pl.mean("factor_value")).alias("avg_factor_value"),
            (
                pl.mean("factor_value_raw")
                if "factor_value_raw" in df.columns
                else pl.mean("factor_value")
            ).alias("avg_factor_value_raw"),
            (pl.mean("forward_return") * 100).alias("avg_return_pct"),
            (pl.mean("forward_excess_return") * 100).alias("avg_excess_return_pct"),
            ((pl.col("forward_return") > 0).cast(pl.Float64).mean() * 100).alias("win_rate_pct"),
            ((pl.col("forward_excess_return") > 0).cast(pl.Float64).mean() * 100).alias("excess_win_rate_pct"),
        )
        .with_columns(
            pl.col("avg_factor_value").round(4),
            pl.col("avg_factor_value_raw").round(4),
            pl.col("avg_return_pct").round(4),
            pl.col("avg_excess_return_pct").round(4),
            pl.col("win_rate_pct").round(2),
            pl.col("excess_win_rate_pct").round(2),
        )
        .sort("bucket")
    )


def _compute_rank_ic(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame()

    return (
        df.with_columns(
            pl.col("factor_value").rank(method="average").over("trade_date").alias("_factor_rank_ic"),
            pl.col("forward_return").rank(method="average").over("trade_date").alias("_return_rank_ic"),
        )
        .group_by("trade_date")
        .agg(
            pl.len().alias("samples"),
            pl.corr("_factor_rank_ic", "_return_rank_ic").alias("rank_ic"),
        )
        .filter(pl.col("rank_ic").is_not_null() & pl.col("rank_ic").is_finite())
        .sort("trade_date")
        .with_columns(
            pl.col("rank_ic").rolling_mean(window_size=20, min_samples=5).alias("rank_ic_ma20"),
            pl.col("rank_ic").rolling_mean(window_size=60, min_samples=10).alias("rank_ic_ma60"),
            pl.col("rank_ic").cum_sum().alias("cumulative_rank_ic"),
        )
        .with_columns(
            pl.col("rank_ic").round(6),
            pl.col("rank_ic_ma20").round(6),
            pl.col("rank_ic_ma60").round(6),
            pl.col("cumulative_rank_ic").round(6),
        )
    )


def _compute_daily_top_bottom_returns(df: pl.DataFrame, bucket_count: int) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame()

    bucket_returns = (
        df.group_by(["trade_date", "bucket"])
        .agg(
            pl.len().alias("samples"),
            pl.mean("forward_return").alias("bucket_return"),
        )
    )
    top = (
        bucket_returns.filter(pl.col("bucket") == bucket_count)
        .select(
            "trade_date",
            pl.col("samples").alias("top_samples"),
            pl.col("bucket_return").alias("top_return"),
        )
    )
    bottom = (
        bucket_returns.filter(pl.col("bucket") == 1)
        .select(
            "trade_date",
            pl.col("samples").alias("bottom_samples"),
            pl.col("bucket_return").alias("bottom_return"),
        )
    )
    return (
        top.join(bottom, on="trade_date", how="inner")
        .with_columns((pl.col("top_return") - pl.col("bottom_return")).alias("top_minus_bottom_return"))
        .filter(pl.col("top_minus_bottom_return").is_not_null() & pl.col("top_minus_bottom_return").is_finite())
        .sort("trade_date")
    )


def _with_non_overlapping_offsets(daily_spread_df: pl.DataFrame, forward_window: int) -> pl.DataFrame:
    if daily_spread_df.is_empty():
        return daily_spread_df
    step = max(1, int(forward_window))
    return (
        daily_spread_df.sort("trade_date")
        .with_row_index("_date_index")
        .with_columns((pl.col("_date_index") % step).cast(pl.Int64).alias("offset"))
    )


def _annualized_return_expr(return_expr: pl.Expr, forward_window: int) -> pl.Expr:
    step = max(1, int(forward_window))
    return (
        pl.when((1 + return_expr) > 0)
        .then(((1 + return_expr) ** (TRADING_DAYS_PER_YEAR / step) - 1) * 100)
        .otherwise(None)
    )


def _compute_non_overlapping_stats(
    df: pl.DataFrame,
    bucket_count: int,
    forward_window: int,
    include_offsets: bool = True,
) -> Dict[str, Any]:
    daily_spread = _compute_daily_top_bottom_returns(df, bucket_count)
    if daily_spread.is_empty():
        return {"summary": {}, "offsets": []}

    offset_source = _with_non_overlapping_offsets(daily_spread, forward_window)
    offset_df = (
        offset_source.group_by("offset")
        .agg(
            pl.len().alias("periods"),
            pl.min("trade_date").alias("start_date"),
            pl.max("trade_date").alias("end_date"),
            pl.mean("top_minus_bottom_return").alias("_avg_return"),
            pl.std("top_minus_bottom_return").alias("_std_return"),
            ((pl.col("top_minus_bottom_return") > 0).cast(pl.Int64).sum()).alias("positive_periods"),
        )
        .with_columns(
            (_annualized_return_expr(pl.col("_avg_return"), forward_window)).alias("_annualized_return_pct"),
            pl.when((pl.col("_std_return") > 0) & (pl.col("periods") > 1))
            .then(pl.col("_avg_return") / pl.col("_std_return") * pl.col("periods").cast(pl.Float64).sqrt())
            .otherwise(None)
            .alias("_t_stat"),
        )
        .with_columns(
            (pl.col("_avg_return") * 100).round(4).alias("avg_top_minus_bottom_return_pct"),
            pl.col("_annualized_return_pct").round(4).alias("annualized_top_minus_bottom_return_pct"),
            (pl.col("positive_periods") * 100.0 / pl.col("periods")).round(2).alias("positive_period_rate_pct"),
            pl.col("_t_stat").round(4).alias("t_stat"),
        )
        .select(
            "offset",
            "periods",
            "start_date",
            "end_date",
            "avg_top_minus_bottom_return_pct",
            "annualized_top_minus_bottom_return_pct",
            "positive_periods",
            "positive_period_rate_pct",
            "t_stat",
        )
        .sort("offset")
    )

    offset_records = _records(offset_df) if include_offsets else []
    raw_records = offset_df.to_dicts()
    annualized_values = [
        item.get("annualized_top_minus_bottom_return_pct")
        for item in raw_records
        if item.get("annualized_top_minus_bottom_return_pct") is not None
    ]
    avg_values = [
        item.get("avg_top_minus_bottom_return_pct")
        for item in raw_records
        if item.get("avg_top_minus_bottom_return_pct") is not None
    ]
    t_values = [item.get("t_stat") for item in raw_records if item.get("t_stat") is not None]
    periods = [int(item.get("periods") or 0) for item in raw_records]
    positive_periods = [int(item.get("positive_periods") or 0) for item in raw_records]

    best = max(
        raw_records,
        key=lambda item: _record_float(item, "annualized_top_minus_bottom_return_pct", -1e18),
    ) if annualized_values else None
    worst = min(
        raw_records,
        key=lambda item: _record_float(item, "annualized_top_minus_bottom_return_pct", 1e18),
    ) if annualized_values else None

    total_periods = sum(periods)
    total_positive = sum(positive_periods)
    summary = {
        "forward_window": int(forward_window),
        "offsets": len(raw_records),
        "total_periods": total_periods,
        "median_periods_per_offset": _safe_float(_median(periods), 2),
        "avg_period_return_pct": _safe_float(_mean(avg_values), 4),
        "annualized_mean_pct": _safe_float(_mean(annualized_values), 4),
        "annualized_median_pct": _safe_float(_median(annualized_values), 4),
        "best_offset": int(best["offset"]) if best else None,
        "best_offset_annualized_pct": _safe_float(best.get("annualized_top_minus_bottom_return_pct"), 4) if best else None,
        "worst_offset": int(worst["offset"]) if worst else None,
        "worst_offset_annualized_pct": _safe_float(worst.get("annualized_top_minus_bottom_return_pct"), 4) if worst else None,
        "positive_period_rate_pct": _safe_float(total_positive * 100 / total_periods, 2) if total_periods else None,
        "mean_t_stat": _safe_float(_mean(t_values), 4),
    }
    return {"summary": summary, "offsets": offset_records}


def _compute_monotonicity(bucket_df: pl.DataFrame) -> Dict[str, Any]:
    if bucket_df.is_empty() or bucket_df.height < 2:
        return {
            "monotonicity_spearman": None,
            "adjacent_hit_rate_pct": None,
            "adjacent_up_count": 0,
            "adjacent_pair_count": 0,
        }

    mono_df = (
        bucket_df.select("bucket", "avg_return_pct")
        .filter(pl.col("avg_return_pct").is_not_null() & pl.col("avg_return_pct").is_finite())
        .sort("bucket")
    )
    if mono_df.height < 2:
        return {
            "monotonicity_spearman": None,
            "adjacent_hit_rate_pct": None,
            "adjacent_up_count": 0,
            "adjacent_pair_count": 0,
        }

    corr_row = (
        mono_df.with_columns(pl.col("avg_return_pct").rank(method="average").alias("_return_rank"))
        .select(pl.corr("bucket", "_return_rank").alias("corr"))
        .to_dicts()[0]
    )
    returns = [float(value) for value in mono_df.select("avg_return_pct").to_series().to_list()]
    pair_count = max(0, len(returns) - 1)
    up_count = sum(1 for index in range(1, len(returns)) if returns[index] > returns[index - 1])
    return {
        "monotonicity_spearman": _safe_float(corr_row.get("corr"), 4),
        "adjacent_hit_rate_pct": _safe_float(up_count * 100 / pair_count, 2) if pair_count else None,
        "adjacent_up_count": up_count,
        "adjacent_pair_count": pair_count,
    }


def _compute_yearly_stability(
    df: pl.DataFrame,
    ic_df: pl.DataFrame,
    bucket_count: int,
    forward_window: int,
) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame()

    sample_year = (
        df.with_columns(pl.col("trade_date").dt.year().alias("year"))
        .group_by("year")
        .agg(
            pl.len().alias("samples"),
            pl.n_unique("trade_date").alias("trade_dates"),
            pl.n_unique("symbol").alias("symbols"),
        )
    )

    daily_spread = _compute_daily_top_bottom_returns(df, bucket_count)
    if daily_spread.is_empty():
        return sample_year.sort("year")

    yearly_spread = (
        daily_spread.with_columns(pl.col("trade_date").dt.year().alias("year"))
        .group_by("year")
        .agg(
            pl.len().alias("spread_periods"),
            pl.mean("top_minus_bottom_return").alias("_avg_spread"),
            ((pl.col("top_minus_bottom_return") > 0).cast(pl.Float64).mean() * 100).alias("positive_spread_rate_pct"),
        )
        .with_columns(
            (pl.col("_avg_spread") * 100).alias("avg_top_minus_bottom_return_pct"),
            _annualized_return_expr(pl.col("_avg_spread"), forward_window).alias("annualized_top_minus_bottom_return_pct"),
        )
    )

    offset_source = _with_non_overlapping_offsets(daily_spread, forward_window)
    yearly_non_overlap = (
        offset_source.with_columns(pl.col("trade_date").dt.year().alias("year"))
        .group_by(["year", "offset"])
        .agg(
            pl.len().alias("periods"),
            pl.mean("top_minus_bottom_return").alias("_avg_spread"),
        )
        .with_columns(_annualized_return_expr(pl.col("_avg_spread"), forward_window).alias("_annualized_spread"))
        .filter(pl.col("_annualized_spread").is_not_null() & pl.col("_annualized_spread").is_finite())
        .group_by("year")
        .agg(
            pl.n_unique("offset").alias("non_overlap_offsets"),
            pl.median("_annualized_spread").alias("non_overlap_annualized_median_pct"),
            pl.mean("_annualized_spread").alias("non_overlap_annualized_mean_pct"),
            pl.min("_annualized_spread").alias("non_overlap_annualized_min_pct"),
            pl.max("_annualized_spread").alias("non_overlap_annualized_max_pct"),
        )
    )

    if ic_df.is_empty():
        yearly_ic = pl.DataFrame(
            schema={
                "year": pl.Int32,
                "ic_periods": pl.UInt32,
                "avg_rank_ic": pl.Float64,
                "positive_ic_rate_pct": pl.Float64,
            }
        )
    else:
        yearly_ic = (
            ic_df.with_columns(pl.col("trade_date").dt.year().alias("year"))
            .group_by("year")
            .agg(
                pl.len().alias("ic_periods"),
                pl.mean("rank_ic").alias("avg_rank_ic"),
                ((pl.col("rank_ic") > 0).cast(pl.Float64).mean() * 100).alias("positive_ic_rate_pct"),
            )
        )

    return (
        sample_year.join(yearly_spread, on="year", how="left")
        .join(yearly_non_overlap, on="year", how="left")
        .join(yearly_ic, on="year", how="left")
        .with_columns(
            pl.col("avg_top_minus_bottom_return_pct").round(4),
            pl.col("annualized_top_minus_bottom_return_pct").round(4),
            pl.col("positive_spread_rate_pct").round(2),
            pl.col("non_overlap_annualized_median_pct").round(4),
            pl.col("non_overlap_annualized_mean_pct").round(4),
            pl.col("non_overlap_annualized_min_pct").round(4),
            pl.col("non_overlap_annualized_max_pct").round(4),
            pl.col("avg_rank_ic").round(6),
            pl.col("positive_ic_rate_pct").round(2),
        )
        .sort("year")
    )


def _summarize(
    bucket_df: pl.DataFrame,
    ic_df: pl.DataFrame,
    request: FactorLabAnalyzeRequest,
    factor_sample: pl.DataFrame,
    forward_window: int,
    elapsed_ms: float,
) -> Dict[str, Any]:
    ic_mean = _safe_float(ic_df.select(pl.mean("rank_ic")).item(), 6) if not ic_df.is_empty() else None
    ic_std = _safe_float(ic_df.select(pl.std("rank_ic")).item(), 6) if not ic_df.is_empty() and ic_df.height > 1 else None
    icir = None
    if ic_mean is not None and ic_std is not None and ic_std > 0:
        icir = round(ic_mean / ic_std * math.sqrt(TRADING_DAYS_PER_YEAR), 4)

    top_return = None
    bottom_return = None
    spread_return = None
    if not bucket_df.is_empty():
        top_row = bucket_df.filter(pl.col("bucket") == request.bucket_count)
        bottom_row = bucket_df.filter(pl.col("bucket") == 1)
        if top_row.height:
            top_return = _safe_float(top_row.select("avg_return_pct").item(), 4)
        if bottom_row.height:
            bottom_return = _safe_float(bottom_row.select("avg_return_pct").item(), 4)
        if top_return is not None and bottom_return is not None:
            spread_return = round(top_return - bottom_return, 4)
    annualized_spread_return = _annualize_period_return_pct(spread_return, forward_window)
    monotonicity = _compute_monotonicity(bucket_df)

    return {
        "samples": int(factor_sample.height),
        "trade_dates": int(factor_sample.select(pl.n_unique("trade_date")).item()) if not factor_sample.is_empty() else 0,
        "symbols": int(factor_sample.select(pl.n_unique("symbol")).item()) if not factor_sample.is_empty() else 0,
        "rank_ic_mean": ic_mean,
        "rank_ic_std": ic_std,
        "icir": icir,
        "top_bucket_avg_return_pct": top_return,
        "bottom_bucket_avg_return_pct": bottom_return,
        "top_minus_bottom_avg_return_pct": spread_return,
        "annualized_top_minus_bottom_avg_return_pct": annualized_spread_return,
        "elapsed_ms": round(elapsed_ms, 1),
    } | monotonicity


def _compute_parameter_heatmap(
    price_df: pl.DataFrame,
    universe_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    request: FactorLabAnalyzeRequest,
    context_base: FactorContext,
) -> List[Dict[str, Any]]:
    if not request.include_heatmap or price_df.is_empty() or universe_df.is_empty():
        return []

    windows = request.heatmap_windows if factor_definition.supports_windows else [request.heatmap_windows[0]]
    forward_windows = request.heatmap_forward_windows
    if len(windows) * len(forward_windows) > MAX_HEATMAP_CELLS:
        forward_windows = forward_windows[: max(1, MAX_HEATMAP_CELLS // max(1, len(windows)))]

    records: List[Dict[str, Any]] = []
    for window in windows:
        heatmap_context = FactorContext(
            windows=[window],
            momentum_weights=_normalize_momentum_weights(request.momentum_weights, [window]),
            db=context_base.db,
            symbols=context_base.symbols,
            start_date=context_base.start_date,
            end_date=context_base.end_date,
        )
        factor_df = _apply_factor_direction(
            factor_definition.compute(price_df, heatmap_context),
            factor_definition,
        )
        for forward_window in forward_windows:
            sample = _assign_buckets(
                _prepare_factor_sample(factor_df, universe_df, request, forward_window),
                request.bucket_count,
            )
            bucket_df = _compute_bucket_report(sample)
            value = None
            annualized_value = None
            non_overlap_annualized_value = None
            top = None
            bottom = None
            if not bucket_df.is_empty():
                top_row = bucket_df.filter(pl.col("bucket") == request.bucket_count)
                bottom_row = bucket_df.filter(pl.col("bucket") == 1)
                if top_row.height:
                    top = _safe_float(top_row.select("avg_return_pct").item(), 4)
                if bottom_row.height:
                    bottom = _safe_float(bottom_row.select("avg_return_pct").item(), 4)
                if top is not None and bottom is not None:
                    value = round(top - bottom, 4)
                    annualized_value = _annualize_period_return_pct(value, int(forward_window))
                non_overlap = _compute_non_overlapping_stats(
                    sample,
                    int(request.bucket_count),
                    int(forward_window),
                    include_offsets=False,
                )
                non_overlap_annualized_value = non_overlap["summary"].get("annualized_median_pct")
            records.append(
                {
                    "window": int(window),
                    "forward_window": int(forward_window),
                    "top_minus_bottom_avg_return_pct": value,
                    "annualized_top_minus_bottom_avg_return_pct": annualized_value,
                    "non_overlap_annualized_top_minus_bottom_pct": non_overlap_annualized_value,
                    "heatmap_value_pct": non_overlap_annualized_value if non_overlap_annualized_value is not None else annualized_value,
                    "top_bucket_avg_return_pct": top,
                    "bottom_bucket_avg_return_pct": bottom,
                    "samples": int(sample.height),
                    "trade_dates": int(sample.select(pl.n_unique("trade_date")).item()) if not sample.is_empty() else 0,
                }
            )
    return records


def _select_best_combo(
    request: FactorLabAnalyzeRequest,
    factor_definition: FactorDefinition,
    heatmap_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    valid_records = [
        item for item in heatmap_records
        if item.get("heatmap_value_pct") is not None
    ]
    selected_record = max(
        valid_records,
        key=lambda item: _record_float(item, "heatmap_value_pct", -1e18),
    ) if valid_records else None

    selected_window = request.heatmap_windows[0]
    selected_forward_window = request.heatmap_forward_windows[0]
    if selected_record:
        selected_window = int(selected_record["window"])
        selected_forward_window = int(selected_record["forward_window"])

    selected_windows = (
        [int(selected_window)]
        if factor_definition.supports_windows
        else factor_definition.default_windows
    )
    return {
        "window": int(selected_window),
        "forward_window": int(selected_forward_window),
        "windows": selected_windows,
        "selection_mode": "best",
        "reason": "max_non_overlap_annualized_top_minus_bottom_pct" if selected_record else "fallback_first_combo",
    }


def _compute_factor_analysis_for_combo(
    price_df: pl.DataFrame,
    universe_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    request,
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
    combo: Dict[str, Any],
    momentum_weights: Dict[str, float],
) -> Dict[str, Any]:
    active_windows = (
        [int(combo["window"])]
        if factor_definition.supports_windows
        else factor_definition.default_windows
    )
    context = FactorContext(
        windows=active_windows,
        momentum_weights=_normalize_momentum_weights(momentum_weights, active_windows),
        db=db,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )
    factor_df = _apply_factor_direction(
        factor_definition.compute(price_df, context),
        factor_definition,
    )
    factor_sample = _assign_buckets(
        _prepare_factor_sample(factor_df, universe_df, request, int(combo["forward_window"])),
        int(request.bucket_count),
    )
    if factor_sample.is_empty():
        raise HTTPException(status_code=400, detail="没有可用于分桶的因子样本，请调整日期、窗口或股票池")

    bucket_df = _compute_bucket_report(factor_sample)
    ic_df = _compute_rank_ic(factor_sample)
    summary = _summarize(bucket_df, ic_df, request, factor_sample, int(combo["forward_window"]), 0)
    non_overlap = _compute_non_overlapping_stats(
        factor_sample,
        int(request.bucket_count),
        int(combo["forward_window"]),
    )
    yearly_df = _compute_yearly_stability(
        factor_sample,
        ic_df,
        int(request.bucket_count),
        int(combo["forward_window"]),
    )
    non_overlap_summary = non_overlap["summary"]
    if non_overlap_summary:
        summary.update(
            {
                "non_overlap_annualized_median_pct": non_overlap_summary.get("annualized_median_pct"),
                "non_overlap_annualized_mean_pct": non_overlap_summary.get("annualized_mean_pct"),
                "non_overlap_positive_period_rate_pct": non_overlap_summary.get("positive_period_rate_pct"),
                "non_overlap_offsets": non_overlap_summary.get("offsets"),
            }
        )
    if not yearly_df.is_empty():
        total_years = yearly_df.height
        spread_years = yearly_df.filter(pl.col("annualized_top_minus_bottom_return_pct") > 0).height
        ic_years = yearly_df.filter(pl.col("avg_rank_ic") > 0).height
        summary.update(
            {
                "positive_spread_years": int(spread_years),
                "positive_ic_years": int(ic_years),
                "total_years": int(total_years),
            }
        )
    return {
        "factor_sample": factor_sample,
        "bucket_df": bucket_df,
        "ic_df": ic_df,
        "non_overlapping_summary": non_overlap_summary,
        "non_overlapping_offsets": non_overlap["offsets"],
        "yearly_stability": yearly_df,
        "summary": summary,
    }


def _run_factor_analysis(
    request: FactorLabAnalyzeRequest,
    db: ORMSession,
) -> Dict[str, Any]:
    factor_definition = FACTOR_REGISTRY.get(request.factor)
    if not factor_definition:
        raise HTTPException(status_code=400, detail=f"未注册的因子: {request.factor}")

    started_at = time.perf_counter()
    end_date = request.end_date or _get_max_trade_date()
    max_forward_window = max(request.heatmap_forward_windows)
    max_factor_window = max(request.heatmap_windows)
    fetch_start = request.start_date - timedelta(
        days=max(370, request.min_listing_days + 30, max_factor_window * 4)
    )
    fetch_end = end_date + timedelta(days=max(30, max_forward_window * 4))
    candidate_etfs = POOL_ETFS[request.pool]

    universe_history = load_universe_history(db, candidate_etfs, request.start_date, end_date)
    if not universe_history.all_symbols:
        raise HTTPException(status_code=400, detail="股票池没有可用成分股数据")

    price_df = _load_price_frame(universe_history.all_symbols, fetch_start, fetch_end)
    if price_df.is_empty():
        raise HTTPException(status_code=400, detail="股票池没有可用日行情数据")

    analysis_dates = (
        price_df.filter((pl.col("trade_date") >= request.start_date) & (pl.col("trade_date") <= end_date))
        .select("trade_date")
        .unique()
        .sort("trade_date")
        .to_series()
        .to_list()
    )
    universe_df = _build_universe_frame(universe_history, analysis_dates)
    if universe_df.is_empty():
        raise HTTPException(status_code=400, detail="分析区间内没有可用股票池截面")

    heatmap_context = FactorContext(
        windows=request.heatmap_windows if factor_definition.supports_windows else factor_definition.default_windows,
        momentum_weights=_normalize_momentum_weights(
            request.momentum_weights,
            request.heatmap_windows if factor_definition.supports_windows else factor_definition.default_windows,
        ),
        db=db,
        symbols=universe_history.all_symbols,
        start_date=request.start_date,
        end_date=end_date,
    )
    heatmap_records = _compute_parameter_heatmap(price_df, universe_df, factor_definition, request, heatmap_context)
    selected_combo = _select_best_combo(request, factor_definition, heatmap_records)
    factor_analysis = _compute_factor_analysis_for_combo(
        price_df=price_df,
        universe_df=universe_df,
        factor_definition=factor_definition,
        request=request,
        db=db,
        symbols=universe_history.all_symbols,
        start_date=request.start_date,
        end_date=end_date,
        combo=selected_combo,
        momentum_weights=request.momentum_weights,
    )
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    factor_analysis["summary"]["elapsed_ms"] = round(elapsed_ms, 1)

    metadata = {
        "pool": request.pool,
        "pool_label": next(item["label"] for item in POOL_OPTIONS if item["key"] == request.pool),
        "candidate_etfs": candidate_etfs,
        "factor": factor_definition.to_option(),
        "factor_direction": factor_definition.direction,
        "factor_direction_label": FACTOR_DIRECTION_OPTIONS.get(
            factor_definition.direction,
            FACTOR_DIRECTION_OPTIONS["exploratory"],
        )["label"],
        "factor_direction_adjusted": factor_definition.direction == "lower_is_better",
        "windows": selected_combo["windows"],
        "forward_window": selected_combo["forward_window"],
        "selected_combo": selected_combo,
        "bucket_count": request.bucket_count,
        "min_listing_days": request.min_listing_days,
        "start_date": request.start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "universe_symbols": len(universe_history.all_symbols),
        "holdings_date_count": universe_history.holdings_date_count,
        "price_rows": int(price_df.height),
        "engine": "polars",
    }

    return {
        "metadata": metadata,
        "summary": factor_analysis["summary"],
        "bucket_returns": _records(factor_analysis["bucket_df"]),
        "rank_ic_series": _records(factor_analysis["ic_df"]),
        "non_overlapping_summary": factor_analysis["non_overlapping_summary"],
        "non_overlapping_offsets": factor_analysis["non_overlapping_offsets"],
        "yearly_stability": _records(factor_analysis["yearly_stability"]),
        "parameter_heatmap": heatmap_records,
    }


@router.get("/options", response_model=FactorLabOptionsResponse)
async def get_factor_lab_options(_: str = Depends(valid_account)):
    return FactorLabOptionsResponse(
        pools=POOL_OPTIONS,
        factors=[definition.to_option() for definition in FACTOR_REGISTRY.values()],
        windows=SUPPORTED_WINDOWS,
        forward_windows=DEFAULT_FORWARD_WINDOWS,
        default_request={
            "pool": "SPY_QQQ",
            "factor": "risk_adjusted_momentum",
            "bucket_count": 10,
            "start_date": DEFAULT_START_DATE.isoformat(),
            "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS,
            "min_listing_days": DEFAULT_MIN_LISTING_DAYS,
            "include_heatmap": True,
            "heatmap_windows": SUPPORTED_WINDOWS,
            "heatmap_forward_windows": DEFAULT_FORWARD_WINDOWS,
        },
    )


@router.post("/analyze")
async def analyze_factor(
    payload: FactorLabAnalyzeRequest,
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _run_factor_analysis(payload, db)
