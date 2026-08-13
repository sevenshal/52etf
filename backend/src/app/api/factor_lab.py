import logging
import asyncio
import json
import math
import os
import re
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from functools import lru_cache
from bisect import bisect_left
from datetime import date, datetime, timedelta
from itertools import combinations, product
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, validator
from sqlalchemy import text
from sqlalchemy.orm import Session as ORMSession

from ...core.database import Session as DBSession
from ...core.database import (
    AStockInnovation100Constituent,
    AStockInnovation100Level,
    ETFFearGreedCloneHistory,
    FactorBacktestSearchResult,
    FactorBacktestSearchState,
    FactorLiveTradingConfig,
    FactorLiveTradingLog,
    USStockIndustrySnapshot,
    get_db,
    get_db_ctx,
)
from ...core.duckdb_utils import connect_duckdb
from ...core.event_stream import publish_event
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingSubAccount,
    get_external_trading_db,
    get_external_trading_db_ctx,
)
from ...core.services.factor_backtest_engine import (
    A_STOCK_INDEX_POOL_CODES,
    A_STOCK_INNO100_INDEX_CODE,
    A_STOCK_INNO100_POOL,
    A_STOCK_INNO100_SYMBOL,
    DEFAULT_ROTATION_MODE,
    CUSTOM_POOL_LABELS,
    CUSTOM_POOL_KEYS,
    FactorBacktestConfig as SharedFactorBacktestConfig,
    FactorBacktestLeg as SharedFactorBacktestLeg,
    POOL_ETFS as SHARED_POOL_ETFS,
    POOL_OPTIONS as SHARED_POOL_OPTIONS,
    get_max_a_stock_index_daily_date,
    load_universe_history,
    load_universe_weight_history,
    ROTATION_MODE_LABELS,
    SUPPORTED_ROTATION_MODES,
    normalize_a_stock_symbol,
    normalize_custom_pool_symbols,
    format_position_weights,
    normalize_position_weights,
    normalize_rotation_mode,
    build_factor_signal_plan as shared_build_factor_signal_plan,
    prepare_factor_backtest_base_data as shared_prepare_factor_backtest_base_data,
    run_factor_backtest as shared_run_factor_backtest,
    unsupported_factor_keys_for_pool,
    warm_backtest_search_factor_caches as shared_warm_backtest_search_factor_caches,
)
from ...core.services.factor_engine import (
    DEFAULT_MOMENTUM_WEIGHTS,
    FACTOR_DIRECTION_OPTIONS,
    FACTOR_REGISTRY,
    MIXED_WINDOW_KEY,
    MOMENTUM_FACTOR_SCORE_PREFIX,
    NEUTRALIZATION_OPTIONS,
    STANDARDIZATION_OPTIONS,
    SUPPORTED_MOMENTUM_WINDOWS,
    SUPPORTED_WINDOWS,
    TRADING_DAYS_PER_YEAR,
    FactorContext,
    FactorDefinition,
    _momentum_score_source_frame,
    _normalize_momentum_weights,
    _normalize_momentum_weights_payload,
    _prepare_factor_frame,
    _prepare_momentum_factor_frame_from_source,
)
from ...core.services.external_trading_executor import trigger_external_trading_executor
from ...core.services.external_trading_ledger import (
    STRATEGY_FACTOR_LIVE,
    get_ledger_positions,
    normalize_symbol as normalize_external_symbol,
    safe_float as external_safe_float,
    safe_int as external_safe_int,
    sync_target_positions,
)
from ...core.services.external_trading_valuation import (
    ExternalTradingValuationError,
    calculate_sub_account_net_asset,
    get_realtime_reference_prices,
)
from ...core.services.market import MarketService
from ...core.services.symbol_names import format_symbol_label, load_symbol_name_map, normalize_symbol_for_name
from ...core.services.tushare import TushareService
from ...core.utils import normalize_us_equity_symbol, send_alert_email
from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_NAMES, A_STOCK_ETF_DAILY_SYMBOLS, A_STOCK_INDEX_FEAR_GREED_TARGETS
from ...robot.us_stock_signal_virtual import (
    DEFAULT_REBALANCE_FREQUENCY,
    DEFAULT_SELL_RANK_MULTIPLIER,
    SUPPORTED_REBALANCE_FREQUENCIES,
    _is_rebalance_day,
    _normalize_rebalance_frequency,
)
from .account import valid_account


router = APIRouter(prefix="/api/factor-lab", tags=["Factor Lab"])
logger = logging.getLogger(__name__)

ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")
XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE = "xueqiu_cube_holdings_snapshots"
XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS = 5
try:
    FACTOR_LIVE_ALERT_COOLDOWN_SECONDS = int(os.getenv("FACTOR_LIVE_ALERT_COOLDOWN_SECONDS", "1800"))
except ValueError:
    FACTOR_LIVE_ALERT_COOLDOWN_SECONDS = 1800
_factor_live_alert_lock = threading.Lock()
_factor_live_alert_last_sent: Dict[str, float] = {}
FACTOR_LIVE_REBALANCE_FREQUENCY_LABELS = {
    "daily": "每日",
    "weekly": "每周",
    "monthly": "每月",
    "quarterly": "季度",
    "semiannual": "半年",
}
DEFAULT_FORWARD_WINDOWS = [5, 20, 60]
DEFAULT_START_DATE = date(2020, 1, 2)
DEFAULT_OOS_START_DATE = date(date.today().year - 1, 1, 1)
DEFAULT_MIN_LISTING_DAYS = 365
CNN_HISTORY_SYMBOL = "CNN*.US"
DEFAULT_NEUTRALIZATION = "none"
DEFAULT_STANDARDIZATION = "zscore"
DEFAULT_HEATMAP_METRIC = "non_overlap_annualized_median_pct"
DEFAULT_TIMING_HEATMAP_METRIC = "annualized_low_minus_high_avg_return_pct"
MAX_HEATMAP_CELLS = 20
FACTOR_DISTRIBUTION_BIN_COUNT = 40
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
FEAR_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_*.-]+$")
A_STOCK_INDEX_POOL_CODE_SET = set(A_STOCK_INDEX_POOL_CODES)
A_STOCK_FEAR_SOURCE_LABELS = {
    A_STOCK_INNO100_SYMBOL: "A创100 指数贪恐",
    **{
        str(item["symbol"]).upper(): (
            f"{item.get('ticker') or item.get('label') or item['symbol']} 指数贪恐"
        )
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    },
}
A_STOCK_ETF_DAILY_SYMBOL_SET = {str(symbol).upper() for symbol in A_STOCK_ETF_DAILY_SYMBOLS}
A_STOCK_FEAR_SOURCE_ORDER = {
    symbol: index
    for index, symbol in enumerate(
        [
            A_STOCK_INNO100_SYMBOL,
            *[str(item["symbol"]).upper() for item in A_STOCK_INDEX_FEAR_GREED_TARGETS],
        ]
    )
}
NEUTRALIZATION_ALIASES = {
    "industry": "sector",
    "industry_market_cap": "sector_market_cap",
}
HEATMAP_METRIC_OPTIONS = {
    "non_overlap_annualized_median_pct": {
        "label": "非重叠年化多空差",
        "kind": "percent",
    },
    "annualized_top_minus_bottom_avg_return_pct": {
        "label": "年化多空差",
        "kind": "percent",
    },
    "top_minus_bottom_avg_return_pct": {
        "label": "T+n 多空差",
        "kind": "percent",
    },
    "rank_ic_mean": {
        "label": "Rank IC 均值",
        "kind": "ic",
    },
    "rank_ic_t_stat": {
        "label": "Rank IC t-stat",
        "kind": "ic",
    },
    "non_overlap_mean_t_stat": {
        "label": "多空 t-stat",
        "kind": "ic",
    },
    "monotonicity_spearman": {
        "label": "单调性 Spearman",
        "kind": "ic",
    },
    "adjacent_hit_rate_pct": {
        "label": "相邻命中率",
        "kind": "percent",
    },
}
TIMING_HEATMAP_METRIC_OPTIONS = {
    "annualized_low_minus_high_avg_return_pct": {
        "label": "年化低-高桶差",
        "kind": "percent",
    },
    "low_minus_high_avg_return_pct": {
        "label": "T+n 低-高桶差",
        "kind": "percent",
    },
    "annualized_top_minus_bottom_avg_return_pct": {
        "label": "年化高-低桶差",
        "kind": "percent",
    },
    "top_minus_bottom_avg_return_pct": {
        "label": "T+n 高-低桶差",
        "kind": "percent",
    },
    "rank_ic_mean": {
        "label": "时间序列 IC",
        "kind": "ic",
    },
    "rank_ic_t_stat": {
        "label": "IC t-stat",
        "kind": "ic",
    },
    "monotonicity_spearman": {
        "label": "单调性 Spearman",
        "kind": "ic",
    },
    "adjacent_hit_rate_pct": {
        "label": "相邻命中率",
        "kind": "percent",
    },
}

BACKTEST_SEARCH_OBJECTIVE_OPTIONS = {
    "annualized_return": {"label": "全区间年化收益最大"},
    "total_return": {"label": "全区间总收益最大"},
    "sharpe": {"label": "全区间夏普最大"},
    "calmar": {"label": "全区间卡玛最大"},
    "in_sample_annualized_return": {"label": "样本内年化收益最大"},
    "in_sample_total_return": {"label": "样本内总收益最大"},
    "in_sample_sharpe": {"label": "样本内夏普最大"},
    "in_sample_calmar": {"label": "样本内卡玛最大"},
    "oos_annualized_return": {"label": "样本外年化收益最大"},
    "oos_total_return": {"label": "样本外总收益最大"},
    "oos_sharpe": {"label": "样本外夏普最大"},
    "oos_calmar": {"label": "样本外卡玛最大"},
}
DEFAULT_TIMING_MA_WINDOWS = [1, 5, 20]
DEFAULT_TIMING_TARGET_OPTIONS = [
    {"label": "SOXL", "value": "SOXL.US"},
    {"label": "TQQQ", "value": "TQQQ.US"},
    {"label": "QQQ", "value": "QQQ.US"},
    {"label": "SPY", "value": "SPY.US"},
    {"label": "SOXX", "value": "SOXX.US"},
    {"label": "A创100", "value": A_STOCK_INNO100_SYMBOL},
    {"label": "A500ETF 563360.SH", "value": "563360.SH"},
    {"label": "中证500ETF 510500.SH", "value": "510500.SH"},
    {"label": "中证全指 000985.SH", "value": "000985.SH"},
    {"label": "科创200ETF 588230.SH", "value": "588230.SH"},
    {"label": "创业板ETF 159915.SZ", "value": "159915.SZ"},
    {"label": "煤炭ETF 515220.SH", "value": "515220.SH"},
    {"label": "红利ETF 510880.SH", "value": "510880.SH"},
]
BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT = 8
BACKTEST_SEARCH_FACTOR_VALUES_CACHE_LIMIT = 8
BACKTEST_SEARCH_MIN_CPU_CORES = 4
BACKTEST_SEARCH_LOW_CPU_MAX_CASES_PER_CORE = 4
BACKTEST_SEARCH_JOBS_LOCK = threading.Lock()
BACKTEST_SEARCH_STATE_ID = 1
BACKTEST_SEARCH_ACTIVE_JOB: Optional[Dict[str, Any]] = None
BACKTEST_SEARCH_ACTIVE_THREAD: Optional[threading.Thread] = None


POOL_OPTIONS = SHARED_POOL_OPTIONS
POOL_ETFS = SHARED_POOL_ETFS
POOL_KEYS = set(POOL_ETFS)


def _effective_cpu_count() -> int:
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return len(affinity)
    except (AttributeError, OSError):
        pass
    return int(os.cpu_count() or 1)


def _ensure_backtest_search_cpu_capacity(total_cases: int) -> int:
    cpu_count = _effective_cpu_count()
    low_cpu_case_limit = cpu_count * BACKTEST_SEARCH_LOW_CPU_MAX_CASES_PER_CORE
    if cpu_count < BACKTEST_SEARCH_MIN_CPU_CORES and total_cases > low_cpu_case_limit:
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前机器可用 CPU 核心数为 {cpu_count}，批量搜参组合数为 {total_cases}，"
                f"超过低 CPU 机器限制的 {low_cpu_case_limit} 个组合，已拒绝执行"
            ),
        )
    return cpu_count


def _backtest_search_worker_count() -> int:
    """批量搜参并发数：可用 CPU 数量的 2/3（至少 1）。"""
    cpu_count = _effective_cpu_count()
    return max(1, int(cpu_count * 2 / 3))


BACKTEST_POOL_KEYS = set(POOL_KEYS).union(CUSTOM_POOL_KEYS)


def _validate_pool_key(value) -> str:
    pool = str(value or "SPY_QQQ").strip().upper()
    if pool not in POOL_KEYS:
        raise ValueError(f"股票池仅支持: {', '.join(sorted(POOL_KEYS))}")
    return pool


def _validate_backtest_pool_key(value) -> str:
    pool = str(value or "SPY_QQQ").strip().upper()
    if pool not in BACKTEST_POOL_KEYS:
        raise ValueError(f"股票池仅支持: {', '.join(sorted(BACKTEST_POOL_KEYS))}")
    return pool


def _allowed_windows_for_factor(factor_key: Any = None) -> List[int]:
    allowed = list(SUPPORTED_WINDOWS)
    factor_definition = FACTOR_REGISTRY.get(str(factor_key or ""))
    if factor_definition:
        allowed.extend(
            int(window)
            for window in (factor_definition.default_windows or [])
            if not (isinstance(window, str) and window == MIXED_WINDOW_KEY)
        )
    return list(dict.fromkeys(allowed))


class FactorLabAnalyzeRequest(BaseModel):
    pool: str = "SPY_QQQ"
    factor: str = "risk_adjusted_momentum"
    bucket_count: int = 10
    start_date: date = DEFAULT_START_DATE
    end_date: Optional[date] = None
    neutralization: str = DEFAULT_NEUTRALIZATION
    standardization: str = DEFAULT_STANDARDIZATION
    oos_start_date: Optional[date] = None
    heatmap_metric: str = DEFAULT_HEATMAP_METRIC
    heatmap_windows: List[Union[int, str]] = Field(default_factory=lambda: SUPPORTED_WINDOWS.copy())
    heatmap_forward_windows: List[int] = Field(default_factory=lambda: DEFAULT_FORWARD_WINDOWS.copy())
    momentum_weights: Dict[str, float] = Field(default_factory=lambda: DEFAULT_MOMENTUM_WEIGHTS.copy())
    min_listing_days: int = DEFAULT_MIN_LISTING_DAYS
    include_heatmap: bool = True

    @validator("pool", pre=True)
    def validate_pool(cls, value):
        return _validate_pool_key(value)

    @validator("factor")
    def validate_factor(cls, value, values):
        unsupported_keys = unsupported_factor_keys_for_pool(values.get("pool"))
        if value in unsupported_keys:
            label = FACTOR_REGISTRY.get(value).label if FACTOR_REGISTRY.get(value) else value
            raise ValueError(f"自定义股票池不支持因子: {label}")
        return value

    @validator("heatmap_windows", pre=True)
    def validate_windows(cls, value, values):
        items = value if isinstance(value, list) else [value]
        normalized: List[Union[int, str]] = []
        factor_definition = FACTOR_REGISTRY.get(str(values.get("factor") or ""))
        allowed_windows = _allowed_windows_for_factor(values.get("factor"))
        for item in items:
            if isinstance(item, str) and item.strip().lower() == MIXED_WINDOW_KEY:
                if MIXED_WINDOW_KEY not in normalized:
                    normalized.append(MIXED_WINDOW_KEY)
                continue
            try:
                window = int(item)
            except (TypeError, ValueError):
                raise ValueError("窗口必须是数字或 mixed")
            if window not in allowed_windows:
                raise ValueError(f"窗口只支持: {', '.join(str(item) for item in allowed_windows)}")
            if window not in normalized:
                normalized.append(window)
        if not normalized:
            raise ValueError("至少选择一个窗口")
        if factor_definition and not factor_definition.supports_windows:
            return factor_definition.default_windows
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

    @validator("neutralization")
    def validate_neutralization(cls, value):
        normalized = str(value or DEFAULT_NEUTRALIZATION)
        normalized = NEUTRALIZATION_ALIASES.get(normalized, normalized)
        if normalized not in NEUTRALIZATION_OPTIONS:
            raise ValueError(f"中性化仅支持: {', '.join(NEUTRALIZATION_OPTIONS)}")
        return normalized

    @validator("standardization")
    def validate_standardization(cls, value):
        normalized = str(value or DEFAULT_STANDARDIZATION)
        if normalized not in STANDARDIZATION_OPTIONS:
            raise ValueError(f"标准化仅支持: {', '.join(STANDARDIZATION_OPTIONS)}")
        return normalized

    @validator("heatmap_metric")
    def validate_heatmap_metric(cls, value):
        normalized = str(value or DEFAULT_HEATMAP_METRIC)
        if normalized not in HEATMAP_METRIC_OPTIONS:
            raise ValueError(f"热力图指标仅支持: {', '.join(HEATMAP_METRIC_OPTIONS)}")
        return normalized

    @validator("oos_start_date")
    def validate_oos_start_date(cls, value, values):
        if value is None:
            return value
        start = values.get("start_date")
        end = values.get("end_date") or date.today()
        if start is not None and value <= start:
            raise ValueError("样本外起始日期必须晚于开始日期")
        if end is not None and value >= end:
            raise ValueError("样本外起始日期必须早于结束日期")
        return value

    @validator("momentum_weights", pre=True)
    def validate_momentum_weights(cls, value):
        return _normalize_momentum_weights_payload(value)

    @validator("end_date")
    def validate_date_range(cls, value, values):
        start = values.get("start_date")
        if value is not None and start is not None and value <= start:
            raise ValueError("结束日期必须晚于开始日期")
        return value


class TimingFactorAnalyzeRequest(BaseModel):
    target_symbol: str = "SOXL.US"
    fear_symbol: str = CNN_HISTORY_SYMBOL
    ma_window: int = 1
    bucket_count: int = 10
    start_date: date = DEFAULT_START_DATE
    end_date: Optional[date] = None
    forward_window: int = 20
    heatmap_metric: str = DEFAULT_TIMING_HEATMAP_METRIC
    heatmap_forward_windows: List[int] = Field(default_factory=lambda: DEFAULT_FORWARD_WINDOWS.copy())
    heatmap_ma_windows: List[int] = Field(default_factory=lambda: DEFAULT_TIMING_MA_WINDOWS.copy())
    include_heatmap: bool = True

    @validator("target_symbol")
    def validate_target_symbol(cls, value):
        text = str(value or "").strip().upper()
        if not text or not SYMBOL_PATTERN.match(text):
            raise ValueError("目标标的代码格式不正确")
        return text

    @validator("fear_symbol")
    def validate_fear_symbol(cls, value):
        text = str(value or "").strip().upper()
        if not text or not FEAR_SYMBOL_PATTERN.match(text):
            raise ValueError("恐贪来源代码格式不正确")
        return text

    @validator("ma_window")
    def validate_ma_window(cls, value):
        window = int(value)
        if window < 1 or window > 252:
            raise ValueError("贪恐均线窗口必须在 1 到 252 之间")
        return window

    @validator("heatmap_ma_windows", pre=True)
    def validate_heatmap_ma_windows(cls, value):
        items = value if isinstance(value, list) else [value]
        normalized: List[int] = []
        for item in items:
            try:
                window = int(item)
            except (TypeError, ValueError):
                raise ValueError("热力图贪恐均线窗口必须是数字")
            if window < 1 or window > 252:
                raise ValueError("热力图贪恐均线窗口必须在 1 到 252 之间")
            if window not in normalized:
                normalized.append(window)
        if not normalized:
            raise ValueError("至少选择一个热力图贪恐均线窗口")
        return normalized[:8]

    @validator("bucket_count")
    def validate_bucket_count(cls, value):
        if value < 2 or value > 20:
            raise ValueError("分桶数必须在 2 到 20 之间")
        return int(value)

    @validator("forward_window")
    def validate_forward_window(cls, value):
        window = int(value)
        if window < 1 or window > 252:
            raise ValueError("收益窗口必须在 1 到 252 之间")
        return window

    @validator("heatmap_metric")
    def validate_timing_heatmap_metric(cls, value):
        normalized = str(value or DEFAULT_TIMING_HEATMAP_METRIC)
        if normalized not in TIMING_HEATMAP_METRIC_OPTIONS:
            raise ValueError(f"择时热力图指标仅支持: {', '.join(TIMING_HEATMAP_METRIC_OPTIONS)}")
        return normalized

    @validator("heatmap_forward_windows", pre=True)
    def validate_timing_heatmap_forward_windows(cls, value):
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
        return normalized[:8]

    @validator("end_date")
    def validate_date_range(cls, value, values):
        start = values.get("start_date")
        if value is not None and start is not None and value <= start:
            raise ValueError("结束日期必须晚于开始日期")
        return value


class CompositeFactorLeg(BaseModel):
    factor: str
    window: Union[int, str] = 20
    weight: float = 1.0
    neutralization: str = DEFAULT_NEUTRALIZATION
    standardization: str = "rank_percentile"
    momentum_weights: Dict[str, float] = Field(default_factory=lambda: DEFAULT_MOMENTUM_WEIGHTS.copy())

    @validator("window", pre=True)
    def validate_window(cls, value, values):
        if isinstance(value, str) and value.strip().lower() == MIXED_WINDOW_KEY:
            return MIXED_WINDOW_KEY
        try:
            window = int(value)
        except (TypeError, ValueError):
            raise ValueError("窗口必须是数字或 mixed")
        allowed_windows = _allowed_windows_for_factor(values.get("factor"))
        if window not in allowed_windows:
            raise ValueError(f"窗口只支持: {', '.join(str(item) for item in allowed_windows)}")
        return window

    @validator("weight")
    def validate_weight(cls, value):
        try:
            weight = float(value)
        except (TypeError, ValueError):
            raise ValueError("因子权重必须是数字")
        if not math.isfinite(weight) or abs(weight) > 100:
            raise ValueError("因子权重必须是有限数字，绝对值不超过100")
        return weight

    @validator("momentum_weights", pre=True)
    def validate_momentum_weights(cls, value):
        return _normalize_momentum_weights_payload(value)

    @validator("neutralization")
    def validate_neutralization(cls, value):
        normalized = str(value or DEFAULT_NEUTRALIZATION)
        normalized = NEUTRALIZATION_ALIASES.get(normalized, normalized)
        if normalized not in NEUTRALIZATION_OPTIONS:
            raise ValueError(f"中性化仅支持: {', '.join(NEUTRALIZATION_OPTIONS)}")
        return normalized

    @validator("standardization")
    def validate_standardization(cls, value):
        normalized = str(value or "rank_percentile")
        if normalized not in STANDARDIZATION_OPTIONS:
            raise ValueError(f"标准化仅支持: {', '.join(STANDARDIZATION_OPTIONS)}")
        return normalized


class CompositeFactorAnalyzeRequest(BaseModel):
    pool: str = "SPY_QQQ"
    bucket_count: int = 10
    start_date: date = DEFAULT_START_DATE
    end_date: Optional[date] = None
    oos_start_date: Optional[date] = None
    forward_window: int = 20
    min_listing_days: int = DEFAULT_MIN_LISTING_DAYS
    legs: List[CompositeFactorLeg] = Field(
        default_factory=lambda: [
            CompositeFactorLeg(factor="risk_adjusted_momentum", window=MIXED_WINDOW_KEY, weight=0.7, standardization="rank_percentile"),
            CompositeFactorLeg(factor="volume_z", window=20, weight=0.3, standardization="rank_percentile"),
        ]
    )

    @validator("pool", pre=True)
    def validate_pool(cls, value):
        return _validate_pool_key(value)

    @validator("bucket_count")
    def validate_bucket_count(cls, value):
        if value < 2 or value > 20:
            raise ValueError("分桶数必须在 2 到 20 之间")
        return int(value)

    @validator("forward_window", pre=True)
    def validate_forward_window(cls, value):
        try:
            window = int(value)
        except (TypeError, ValueError):
            raise ValueError("收益窗口必须是数字")
        if window < 1 or window > 252:
            raise ValueError("收益窗口必须在 1 到 252 之间")
        return window

    @validator("min_listing_days")
    def validate_min_listing_days(cls, value):
        days = int(value)
        if days < 0 or days > 3650:
            raise ValueError("上市天数过滤必须在 0 到 3650 天之间")
        return days

    @validator("oos_start_date")
    def validate_oos_start_date(cls, value, values):
        if value is None:
            return value
        start = values.get("start_date")
        end = values.get("end_date") or date.today()
        if start is not None and value <= start:
            raise ValueError("样本外起始日期必须晚于开始日期")
        if end is not None and value >= end:
            raise ValueError("样本外起始日期必须早于结束日期")
        return value

    @validator("end_date")
    def validate_date_range(cls, value, values):
        start = values.get("start_date")
        if value is not None and start is not None and value <= start:
            raise ValueError("结束日期必须晚于开始日期")
        return value

    @validator("legs")
    def validate_legs(cls, value, values):
        if len(value) < 2:
            raise ValueError("组合因子至少需要2个子因子")
        if len(value) > 8:
            raise ValueError("组合因子最多支持8个子因子")
        if sum(abs(float(item.weight)) for item in value) <= 0:
            raise ValueError("至少设置一个非0因子权重")
        unsupported_keys = unsupported_factor_keys_for_pool(values.get("pool"))
        used_keys = []
        for leg in value or []:
            factor_key = getattr(leg, "factor", None)
            if factor_key in unsupported_keys and factor_key not in used_keys:
                used_keys.append(factor_key)
        if used_keys:
            labels = [
                FACTOR_REGISTRY.get(key).label if FACTOR_REGISTRY.get(key) else key
                for key in used_keys
            ]
            raise ValueError(f"自定义股票池不支持因子: {', '.join(labels)}")
        return value


class FactorBacktestRequest(BaseModel):
    pool: str = "SPY_QQQ"
    custom_symbols: List[str] = Field(default_factory=list)
    start_date: date = DEFAULT_START_DATE
    end_date: Optional[date] = None
    oos_start_date: Optional[date] = None
    initial_capital: float = 100_000.0
    max_positions: int = 7
    position_weights: List[float] = Field(default_factory=list)
    sell_rank_multiplier: float = DEFAULT_SELL_RANK_MULTIPLIER
    rebalance_frequency: str = DEFAULT_REBALANCE_FREQUENCY
    rotation_mode: str = DEFAULT_ROTATION_MODE
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    lot_size: int = 1
    min_listing_days: int = DEFAULT_MIN_LISTING_DAYS
    legs: List[CompositeFactorLeg] = Field(
        default_factory=lambda: [
            CompositeFactorLeg(
                factor="risk_adjusted_momentum",
                window=MIXED_WINDOW_KEY,
                weight=0.6,
                neutralization=DEFAULT_NEUTRALIZATION,
                standardization="rank_percentile",
                momentum_weights=DEFAULT_MOMENTUM_WEIGHTS.copy(),
            ),
            CompositeFactorLeg(
                factor="index_weight",
                window=20,
                weight=0.4,
                neutralization=DEFAULT_NEUTRALIZATION,
                standardization="rank_percentile",
                momentum_weights=DEFAULT_MOMENTUM_WEIGHTS.copy(),
            ),
        ]
    )

    @validator("pool", pre=True)
    def validate_pool(cls, value):
        return _validate_backtest_pool_key(value)

    @validator("custom_symbols", pre=True, always=True)
    def validate_custom_symbols(cls, value, values):
        return normalize_custom_pool_symbols(values.get("pool"), value)

    @validator("position_weights", pre=True, always=True)
    def validate_position_weights(cls, value, values):
        return normalize_position_weights(value, values.get("max_positions") or 1)

    @validator("initial_capital")
    def validate_initial_capital(cls, value):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError("初始资金必须大于0")
        return number

    @validator("max_positions")
    def validate_max_positions(cls, value):
        number = int(value)
        if number < 1 or number > 100:
            raise ValueError("持仓数量必须在1到100之间")
        return number

    @validator("sell_rank_multiplier")
    def validate_sell_rank_multiplier(cls, value):
        number = float(value)
        if not math.isfinite(number) or number < 1 or number > 10:
            raise ValueError("卖出排名倍数必须在1到10之间")
        return number

    @validator("rebalance_frequency")
    def validate_rebalance_frequency(cls, value):
        normalized = _normalize_rebalance_frequency(value)
        if normalized not in SUPPORTED_REBALANCE_FREQUENCIES:
            raise ValueError(f"调仓频率仅支持: {', '.join(SUPPORTED_REBALANCE_FREQUENCIES)}")
        return normalized

    @validator("rotation_mode")
    def validate_rotation_mode(cls, value):
        normalized = normalize_rotation_mode(value)
        if normalized not in SUPPORTED_ROTATION_MODES:
            raise ValueError(f"调仓方式仅支持: {', '.join(SUPPORTED_ROTATION_MODES)}")
        return normalized

    @validator("commission_pct", "slippage_pct")
    def validate_cost_pct(cls, value):
        number = float(value)
        if not math.isfinite(number) or number < 0 or number > 10:
            raise ValueError("交易成本百分比必须在0到10之间")
        return number

    @validator("lot_size")
    def validate_lot_size(cls, value):
        number = int(value)
        if number < 1 or number > 10_000:
            raise ValueError("交易单位必须在1到10000之间")
        return number

    @validator("min_listing_days")
    def validate_min_listing_days(cls, value):
        days = int(value)
        if days < 0 or days > 3650:
            raise ValueError("上市天数过滤必须在 0 到 3650 天之间")
        return days

    @validator("end_date")
    def validate_date_range(cls, value, values):
        start = values.get("start_date")
        if value is not None and start is not None and value <= start:
            raise ValueError("结束日期必须晚于开始日期")
        return value

    @validator("oos_start_date")
    def validate_oos_start_date(cls, value, values):
        if value is None:
            return value
        start = values.get("start_date")
        end = values.get("end_date") or date.today()
        if start is not None and value <= start:
            raise ValueError("样本外起始日期必须晚于开始日期")
        if end is not None and value >= end:
            raise ValueError("样本外起始日期必须早于结束日期")
        return value

    @validator("legs")
    def validate_legs(cls, value, values):
        if len(value) < 1:
            raise ValueError("因子回测至少需要1个因子")
        if len(value) > 8:
            raise ValueError("因子回测最多支持8个因子")
        if sum(abs(float(item.weight)) for item in value) <= 0:
            raise ValueError("至少设置一个非0因子权重")
        unsupported_keys = unsupported_factor_keys_for_pool(values.get("pool"))
        used_keys = []
        for leg in value or []:
            factor_key = getattr(leg, "factor", None)
            if factor_key in unsupported_keys and factor_key not in used_keys:
                used_keys.append(factor_key)
        if used_keys:
            labels = [
                FACTOR_REGISTRY.get(key).label if FACTOR_REGISTRY.get(key) else key
                for key in used_keys
            ]
            raise ValueError(f"自定义股票池不支持因子: {', '.join(labels)}")
        return value


class FactorBacktestSearchRequest(BaseModel):
    request: FactorBacktestRequest
    objective: Literal[
        "annualized_return",
        "total_return",
        "sharpe",
        "calmar",
        "in_sample_annualized_return",
        "in_sample_total_return",
        "in_sample_sharpe",
        "in_sample_calmar",
        "oos_annualized_return",
        "oos_total_return",
        "oos_sharpe",
        "oos_calmar",
    ] = "annualized_return"
    window_weight_bucket_count: int = 20
    factor_weight_bucket_count: int = 20
    max_positions_candidates: Optional[List[int]] = None
    position_weight_candidates: Optional[List[List[float]]] = None
    sell_rank_multiplier_candidates: Optional[List[float]] = None
    rotation_mode_candidates: Optional[List[str]] = None

    @validator("window_weight_bucket_count", "factor_weight_bucket_count")
    def validate_bucket_count(cls, value):
        number = int(value)
        if number < 0 or number > 100:
            raise ValueError("权重分桶数必须在0到100之间")
        return number

    @validator("max_positions_candidates", pre=True)
    def validate_max_positions_candidates(cls, value, values):
        items = value if isinstance(value, list) else ([] if value is None else [value])
        normalized: List[int] = []
        for item in items:
            raw_number = float(item)
            if not math.isfinite(raw_number) or abs(raw_number - int(raw_number)) > 1e-9:
                raise ValueError("持仓数候选项必须是整数")
            number = int(raw_number)
            if number < 1 or number > 100:
                raise ValueError("持仓数候选项必须在1到100之间")
            if number not in normalized:
                normalized.append(number)
        if len(normalized) > 50:
            raise ValueError("持仓数候选项最多支持50个")
        return normalized

    @validator("position_weight_candidates", pre=True, always=True)
    def validate_position_weight_candidates(cls, value, values):
        request = values.get("request")
        fallback_max_positions = getattr(request, "max_positions", 1) if request is not None else 1
        raw_candidates: List[Any] = []
        if isinstance(value, str):
            raw_candidates = [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
        elif isinstance(value, list):
            scalar_number_list = True
            for item in value:
                if isinstance(item, (list, tuple, dict)):
                    scalar_number_list = False
                    break
                try:
                    float(item)
                except (TypeError, ValueError):
                    scalar_number_list = False
                    break
            if value and scalar_number_list:
                raw_candidates = [value]
            else:
                raw_candidates = value
        elif value is not None:
            raw_candidates = [value]

        if not raw_candidates and values.get("max_positions_candidates"):
            raw_candidates = [[1.0] * int(item) for item in values.get("max_positions_candidates") or []]
        if not raw_candidates and request is not None:
            raw_candidates = [request.position_weights or [1.0] * int(request.max_positions or 1)]

        normalized: List[List[float]] = []
        seen = set()
        for item in raw_candidates:
            weights = normalize_position_weights(item, fallback_max_positions)
            key = tuple(round(float(weight), 10) for weight in weights)
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(list(key))
        if len(normalized) > 50:
            raise ValueError("仓位候选项最多支持50个")
        return normalized

    @validator("sell_rank_multiplier_candidates", pre=True, always=True)
    def validate_sell_rank_multiplier_candidates(cls, value, values):
        request = values.get("request")
        items = value if isinstance(value, list) else ([] if value is None else [value])
        if not items and request is not None:
            items = [request.sell_rank_multiplier]
        normalized: List[float] = []
        for item in items:
            number = float(item)
            if not math.isfinite(number) or number < 1 or number > 10:
                raise ValueError("卖出倍数候选项必须在1到10之间")
            rounded = round(number, 6)
            if rounded not in normalized:
                normalized.append(rounded)
        if len(normalized) > 50:
            raise ValueError("卖出倍数候选项最多支持50个")
        return normalized

    @validator("rotation_mode_candidates", pre=True, always=True)
    def validate_rotation_mode_candidates(cls, value, values):
        request = values.get("request")
        if isinstance(value, str):
            items = [item.strip() for item in re.split(r"[,，\s]+", value) if item.strip()]
        elif isinstance(value, list):
            items = value
        elif value is None:
            items = []
        else:
            items = [value]
        if not items and request is not None:
            items = [request.rotation_mode]
        normalized: List[str] = []
        for item in items:
            mode = normalize_rotation_mode(item)
            if mode not in SUPPORTED_ROTATION_MODES:
                raise ValueError(f"调仓方式仅支持: {', '.join(SUPPORTED_ROTATION_MODES)}")
            if mode not in normalized:
                normalized.append(mode)
        return normalized or [DEFAULT_ROTATION_MODE]

    @validator("objective")
    def validate_objective(cls, value, values):
        request = values.get("request")
        if (
            request is not None
            and value.startswith(("in_sample_", "oos_"))
            and not request.oos_start_date
        ):
            raise ValueError("选择样本内/样本外目标时，请先设置样本外起始日期")
        return value


class FactorLiveTradingConfigPayload(BaseModel):
    name: str = Field(default="因子线上交易", min_length=1, max_length=100)
    enabled: bool = False
    request: FactorBacktestRequest = Field(default_factory=FactorBacktestRequest)
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None
    signal_time: str = "18:35"
    signal_timezone: str = "Asia/Shanghai"
    execution_time: str = "09:31"
    execution_timezone: str = "Asia/Shanghai"

    @validator("name", "signal_timezone", "execution_timezone", pre=True)
    def strip_live_config_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @validator("signal_time", "execution_time")
    def validate_live_time(cls, value):
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", text):
            raise ValueError("触发时间格式必须是 HH:MM")
        hour, minute = [int(item) for item in text.split(":")]
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("触发时间格式必须是 HH:MM")
        return text

    @validator("signal_timezone", "execution_timezone")
    def validate_live_timezone(cls, value):
        text = str(value or "Asia/Shanghai").strip()
        try:
            ZoneInfo(text)
        except ZoneInfoNotFoundError:
            raise ValueError(f"不支持的时区: {text}")
        return text


class FactorLiveSignalRequest(BaseModel):
    signal_date: Optional[date] = None
    rank_limit: int = Field(default=100, ge=1, le=500)


class FactorLiveExecutionRequest(BaseModel):
    signal_date: Optional[date] = None
    trigger_executor: bool = True
    force: bool = True
    timeout_seconds: float = Field(default=120.0, ge=10.0, le=3600.0)


class FactorLabOptionsResponse(BaseModel):
    pools: List[Dict[str, Any]]
    factors: List[Dict[str, Any]]
    windows: List[int]
    forward_windows: List[int]
    heatmap_metrics: List[Dict[str, Any]]
    timing_heatmap_metrics: List[Dict[str, Any]]
    timing_fear_sources: List[Dict[str, Any]]
    timing_target_options: List[Dict[str, Any]]
    backtest_search_objectives: List[Dict[str, Any]]
    neutralization_options: List[Dict[str, Any]]
    standardization_options: List[Dict[str, Any]]
    default_request: Dict[str, Any]
    default_composite_request: Dict[str, Any]
    default_backtest_request: Dict[str, Any]
    default_timing_request: Dict[str, Any]


def _quote_sql_string(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def _is_a_stock_symbol(symbol: str) -> bool:
    text = str(symbol or "").strip().upper()
    return text.endswith((".SH", ".SZ", ".BJ"))


def _is_a_stock_pool(pool: Optional[str]) -> bool:
    pool_key = str(pool or "").strip().upper()
    return pool_key == A_STOCK_INNO100_POOL or pool_key in A_STOCK_INDEX_POOL_CODE_SET


def _numeric_heatmap_windows(windows: List[Union[int, str]]) -> List[int]:
    numeric = [
        int(item)
        for item in windows
        if not (isinstance(item, str) and item == MIXED_WINDOW_KEY)
    ]
    return numeric or SUPPORTED_WINDOWS.copy()


def _factor_required_windows(
    windows: List[Union[int, str]],
    factor_definition: FactorDefinition,
) -> List[int]:
    if not factor_definition.supports_windows:
        return factor_definition.default_windows

    required = _numeric_heatmap_windows(windows)
    if MIXED_WINDOW_KEY in windows:
        required.extend(factor_definition.default_windows)
    return list(dict.fromkeys(int(item) for item in required))


def _window_label(window: Union[int, str]) -> str:
    return "多窗口合成" if window == MIXED_WINDOW_KEY else f"{int(window)}日"


def _active_windows_for_heatmap_row(
    window: Union[int, str],
    factor_definition: FactorDefinition,
) -> List[int]:
    if not factor_definition.supports_windows:
        return factor_definition.default_windows
    if window == MIXED_WINDOW_KEY:
        if not factor_definition.supports_mixed_windows:
            raise HTTPException(status_code=400, detail=f"{factor_definition.label} 不支持多窗口合成")
        return factor_definition.default_windows
    return [int(window)]


def _weights_for_heatmap_row(
    raw_weights: Dict[str, float],
    active_windows: List[int],
    window: Union[int, str],
) -> Dict[int, float]:
    if window == MIXED_WINDOW_KEY:
        return _normalize_momentum_weights(raw_weights, active_windows)
    return _normalize_momentum_weights({str(active_windows[0]): 1.0}, active_windows)


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


def _request_to_dict(request: Any) -> Dict[str, Any]:
    if isinstance(request, BaseModel):
        if hasattr(request, "model_dump"):
            return request.model_dump()
        return request.dict()
    return dict(vars(request))


def _put_limited_cache(cache: Dict[Any, Any], key: Any, value: Any, limit: int):
    if key in cache:
        cache[key] = value
        return
    while len(cache) >= max(1, int(limit)):
        cache.pop(next(iter(cache)))
    cache[key] = value


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
    _import_duckdb()
    try:
        return connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"DuckDB分析库当前不可读，可能正在同步写入或被其他进程占用: {exc}",
        ) from exc


def _duckdb_table_exists(connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def _duckdb_query_dicts(connection, query: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    cursor = connection.execute(query, params or [])
    columns = [column[0] for column in cursor.description or []]
    return [
        {key: _serialize_value(value) for key, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def _normalize_xueqiu_snapshot_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    if text in {"CASH", "CN_CASH"}:
        return "CASH"
    text = text.replace("_", ".")
    if "." in text:
        left, right = text.split(".", 1)
        if left in {"SH", "SZ", "BJ"} and re.fullmatch(r"\d{6}", right):
            return f"{left}.{right}"
        if right in {"SH", "SZ", "BJ"} and re.fullmatch(r"\d{6}", left):
            return f"{right}.{left}"
        return text
    if len(text) == 8 and text[:2] in {"SH", "SZ", "BJ"} and text[2:].isdigit():
        return f"{text[:2]}.{text[2:]}"
    return text


def _raw_xueqiu_snapshot_symbol(symbol: str) -> str:
    normalized = _normalize_xueqiu_snapshot_symbol(symbol)
    if normalized == "CASH":
        return "CASH"
    return normalized.replace(".", "")


def _xueqiu_symbol_to_ts_code(symbol: Any) -> str:
    normalized = _normalize_xueqiu_snapshot_symbol(symbol)
    match = re.fullmatch(r"(SH|SZ|BJ)\.(\d{6})", normalized)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    return normalize_a_stock_symbol(normalized)


def _xueqiu_top_holdings_snapshot_cte(active_only: bool) -> str:
    active_filter_sql = "WHERE COALESCE(is_active, FALSE)" if active_only else ""
    table = XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE
    return f"""
        WITH base_holdings AS (
            SELECT
                snapshot_date,
                snapshot_at,
                rank_type,
                year_rank,
                cube_symbol,
                cube_id,
                cube_name,
                screen_name,
                latest_rebalance_at,
                active_rebalance_at,
                holdings_source,
                active_rebalance_days,
                COALESCE(is_active, FALSE) AS is_active,
                stock_symbol,
                raw_stock_symbol,
                stock_name,
                stock_id,
                segment_name,
                CAST(weight_pct AS DOUBLE) AS weight_pct
            FROM {table}
            WHERE weight_pct IS NOT NULL
              AND weight_pct > 0
        ),
        cube_rows AS (
            SELECT
                snapshot_date,
                MAX(snapshot_at) AS snapshot_at,
                ANY_VALUE(rank_type) AS rank_type,
                cube_symbol,
                ANY_VALUE(year_rank) AS year_rank,
                ANY_VALUE(cube_id) AS cube_id,
                ANY_VALUE(cube_name) AS cube_name,
                ANY_VALUE(screen_name) AS screen_name,
                MAX(latest_rebalance_at) AS latest_rebalance_at,
                MAX(active_rebalance_at) AS active_rebalance_at,
                ANY_VALUE(holdings_source) AS holdings_source,
                MAX(active_rebalance_days) AS active_rebalance_days,
                BOOL_OR(COALESCE(is_active, FALSE)) AS is_active,
                SUM(weight_pct) AS stock_weight_pct
            FROM base_holdings
            GROUP BY snapshot_date, cube_symbol
        ),
        cash_holdings AS (
            SELECT
                snapshot_date,
                snapshot_at,
                rank_type,
                year_rank,
                cube_symbol,
                cube_id,
                cube_name,
                screen_name,
                latest_rebalance_at,
                active_rebalance_at,
                holdings_source,
                active_rebalance_days,
                is_active,
                'CASH' AS stock_symbol,
                'CASH' AS raw_stock_symbol,
                '现金' AS stock_name,
                CAST(NULL AS BIGINT) AS stock_id,
                '现金' AS segment_name,
                GREATEST(0.0, 100.0 - stock_weight_pct) AS weight_pct
            FROM cube_rows
            WHERE GREATEST(0.0, 100.0 - stock_weight_pct) > 0.005
        ),
        holding_union AS (
            SELECT * FROM base_holdings
            UNION ALL
            SELECT * FROM cash_holdings
        ),
        filtered_holdings AS (
            SELECT *
            FROM holding_union
            {active_filter_sql}
        )
    """


def _empty_xueqiu_top_holdings_latest(active_only: bool, limit: int, reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "active_only": active_only,
        "limit": limit,
        "snapshot_date": None,
        "rank_compare_snapshot_date": None,
        "rank_compare_trading_days": XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS,
        "snapshot_at": None,
        "cube_count": 0,
        "source_cube_count": 0,
        "active_cube_count": 0,
        "active_rebalance_days": None,
        "index_options": [],
        "items": [],
    }


def _attach_xueqiu_fear_index_memberships(
    connection,
    items: List[Dict[str, Any]],
    snapshot_date: Any,
) -> List[Dict[str, str]]:
    """Attach current memberships for A-share indexes with fear/greed calculations."""
    targets = [
        {
            "symbol": str(target["symbol"]).upper(),
            "label": str(target.get("ticker") or target.get("label") or target["symbol"]),
        }
        for target in A_STOCK_INDEX_FEAR_GREED_TARGETS
    ]
    memberships: Dict[str, List[Dict[str, str]]] = {}

    if items and targets and _duckdb_table_exists(connection, "a_stock_index_weight"):
        target_symbols = [target["symbol"] for target in targets]
        placeholders = ", ".join("?" for _ in target_symbols)
        rows = _duckdb_query_dicts(
            connection,
            f"""
            WITH latest_dates AS (
                SELECT index_code, MAX(trade_date) AS trade_date
                FROM a_stock_index_weight
                WHERE index_code IN ({placeholders})
                  AND trade_date <= ?
                GROUP BY index_code
            )
            SELECT weights.index_code, weights.con_code
            FROM a_stock_index_weight weights
            JOIN latest_dates
              ON latest_dates.index_code = weights.index_code
             AND latest_dates.trade_date = weights.trade_date
            """,
            [*target_symbols, snapshot_date],
        )
        target_by_symbol = {target["symbol"]: target for target in targets}
        for row in rows:
            target = target_by_symbol.get(str(row.get("index_code") or "").upper())
            constituent = _xueqiu_symbol_to_ts_code(row.get("con_code"))
            if target and constituent:
                memberships.setdefault(constituent, []).append(target)

    # A创100 is a locally maintained custom index rather than a Tushare index-weight series.
    try:
        snapshot_day = (
            snapshot_date
            if isinstance(snapshot_date, date)
            else date.fromisoformat(str(snapshot_date))
        )
        with DBSession() as db:
            latest_date = db.query(
                AStockInnovation100Constituent.rebalance_date
            ).filter(
                AStockInnovation100Constituent.index_code == A_STOCK_INNO100_INDEX_CODE,
                AStockInnovation100Constituent.rebalance_date <= snapshot_day,
            ).order_by(
                AStockInnovation100Constituent.rebalance_date.desc()
            ).first()
            if latest_date:
                custom_target = {
                    "symbol": A_STOCK_INNO100_SYMBOL,
                    "label": "A创100",
                }
                custom_rows = db.query(AStockInnovation100Constituent.ts_code).filter(
                    AStockInnovation100Constituent.index_code == A_STOCK_INNO100_INDEX_CODE,
                    AStockInnovation100Constituent.rebalance_date == latest_date[0],
                ).all()
                for (ts_code,) in custom_rows:
                    constituent = _xueqiu_symbol_to_ts_code(ts_code)
                    if constituent:
                        memberships.setdefault(constituent, []).append(custom_target)
    except Exception as exc:
        logger.warning("Unable to load A创100 membership for 雪球持仓: %s", exc)

    used_options: Dict[str, Dict[str, str]] = {}
    for item in items:
        stock_symbol = _xueqiu_symbol_to_ts_code(item.get("stock_symbol"))
        index_memberships = sorted(
            memberships.get(stock_symbol, []),
            key=lambda value: (value["label"], value["symbol"]),
        )
        item["fear_indexes"] = index_memberships
        for membership in index_memberships:
            used_options[membership["symbol"]] = membership
    return sorted(used_options.values(), key=lambda value: (value["label"], value["symbol"]))


def load_xueqiu_top_holdings_latest(active_only: bool = True, limit: int = 300) -> Dict[str, Any]:
    normalized_limit = max(1, min(int(limit or 300), 2000))
    connection = _connect_duckdb()
    try:
        if not _duckdb_table_exists(connection, XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE):
            return _empty_xueqiu_top_holdings_latest(active_only, normalized_limit, "snapshot_table_missing")

        cte = _xueqiu_top_holdings_snapshot_cte(active_only)
        metadata_rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            latest_snapshot AS (
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM cube_rows
            ),
            rank_dates AS (
                SELECT DISTINCT snapshot_date
                FROM filtered_holdings
            ),
            compare_snapshot AS (
                SELECT snapshot_date
                FROM (
                    SELECT
                        snapshot_date,
                        ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) AS snapshot_rank_desc
                    FROM rank_dates
                    WHERE snapshot_date < (SELECT snapshot_date FROM latest_snapshot)
                ) ranked_dates
                WHERE snapshot_rank_desc = {XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS}
            ),
            latest_cube_summary AS (
                SELECT
                    cube_rows.snapshot_date,
                    MAX(cube_rows.snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_rows.cube_symbol) AS source_cube_count,
                    COUNT(DISTINCT CASE WHEN cube_rows.is_active THEN cube_rows.cube_symbol END) AS active_cube_count,
                    MAX(cube_rows.active_rebalance_days) AS active_rebalance_days
                FROM cube_rows
                JOIN latest_snapshot ON cube_rows.snapshot_date = latest_snapshot.snapshot_date
                GROUP BY cube_rows.snapshot_date
            ),
            filtered_latest_summary AS (
                SELECT
                    filtered_holdings.snapshot_date,
                    COUNT(DISTINCT filtered_holdings.cube_symbol) AS cube_count,
                    COUNT(*) AS holding_row_count
                FROM filtered_holdings
                JOIN latest_snapshot ON filtered_holdings.snapshot_date = latest_snapshot.snapshot_date
                GROUP BY filtered_holdings.snapshot_date
            )
            SELECT
                latest_snapshot.snapshot_date,
                compare_snapshot.snapshot_date AS rank_compare_snapshot_date,
                latest_cube_summary.snapshot_at,
                COALESCE(filtered_latest_summary.cube_count, 0) AS cube_count,
                COALESCE(filtered_latest_summary.holding_row_count, 0) AS holding_row_count,
                COALESCE(latest_cube_summary.source_cube_count, 0) AS source_cube_count,
                COALESCE(latest_cube_summary.active_cube_count, 0) AS active_cube_count,
                latest_cube_summary.active_rebalance_days
            FROM latest_snapshot
            LEFT JOIN compare_snapshot ON TRUE
            LEFT JOIN latest_cube_summary ON latest_cube_summary.snapshot_date = latest_snapshot.snapshot_date
            LEFT JOIN filtered_latest_summary ON filtered_latest_summary.snapshot_date = latest_snapshot.snapshot_date
            """,
        )
        metadata = metadata_rows[0] if metadata_rows else {}
        snapshot_date = metadata.get("snapshot_date")
        if not snapshot_date:
            return _empty_xueqiu_top_holdings_latest(active_only, normalized_limit, "snapshot_empty")

        item_rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            latest_snapshot AS (
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM cube_rows
            ),
            rank_dates AS (
                SELECT DISTINCT snapshot_date
                FROM filtered_holdings
            ),
            compare_snapshot AS (
                SELECT snapshot_date
                FROM (
                    SELECT
                        snapshot_date,
                        ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) AS snapshot_rank_desc
                    FROM rank_dates
                    WHERE snapshot_date < (SELECT snapshot_date FROM latest_snapshot)
                ) ranked_dates
                WHERE snapshot_rank_desc = {XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS}
            ),
            snapshot_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                JOIN latest_snapshot ON filtered_holdings.snapshot_date = latest_snapshot.snapshot_date
            ),
            snapshot_summary AS (
                SELECT COUNT(DISTINCT cube_symbol) AS cube_count
                FROM snapshot_holdings
            ),
            stock_summary AS (
                SELECT
                    stock_symbol,
                    ANY_VALUE(raw_stock_symbol) AS raw_stock_symbol,
                    ANY_VALUE(stock_name) AS stock_name,
                    ANY_VALUE(segment_name) AS segment_name,
                    MIN(year_rank) AS best_year_rank,
                    COUNT(DISTINCT cube_symbol) AS holding_cube_count,
                    SUM(weight_pct) AS total_weight_pct,
                    SUM(weight_pct) / NULLIF(MAX(snapshot_summary.cube_count), 0) AS composite_weight_pct,
                    COUNT(DISTINCT cube_symbol) * 100.0 / NULLIF(MAX(snapshot_summary.cube_count), 0) AS holding_cube_ratio_pct,
                    SUM(weight_pct) / NULLIF(COUNT(DISTINCT cube_symbol), 0) AS average_weight_pct
                FROM snapshot_holdings
                CROSS JOIN snapshot_summary
                GROUP BY stock_symbol
            ),
            ranked AS (
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY total_weight_pct DESC, holding_cube_count DESC, stock_symbol DESC
                    ) AS composite_rank,
                    *
                FROM stock_summary
            ),
            compare_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                JOIN compare_snapshot ON filtered_holdings.snapshot_date = compare_snapshot.snapshot_date
            ),
            compare_snapshot_summary AS (
                SELECT COUNT(DISTINCT cube_symbol) AS cube_count
                FROM compare_holdings
            ),
            compare_stock_summary AS (
                SELECT
                    stock_symbol,
                    COUNT(DISTINCT cube_symbol) AS holding_cube_count,
                    SUM(weight_pct) AS total_weight_pct
                FROM compare_holdings
                CROSS JOIN compare_snapshot_summary
                GROUP BY stock_symbol
            ),
            compare_ranked AS (
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY total_weight_pct DESC, holding_cube_count DESC, stock_symbol DESC
                    ) AS composite_rank,
                    *
                FROM compare_stock_summary
            )
            SELECT
                ranked.*,
                compare_ranked.composite_rank AS rank_5d_ago,
                CASE
                    WHEN compare_ranked.composite_rank IS NULL THEN NULL
                    ELSE compare_ranked.composite_rank - ranked.composite_rank
                END AS rank_change_5d
            FROM ranked
            LEFT JOIN compare_ranked ON compare_ranked.stock_symbol = ranked.stock_symbol
            ORDER BY ranked.composite_rank
            LIMIT ?
            """,
            [normalized_limit],
        )
        index_options = _attach_xueqiu_fear_index_memberships(
            connection,
            item_rows,
            snapshot_date,
        )
        return {
            "available": True,
            "active_only": active_only,
            "limit": normalized_limit,
            "snapshot_date": snapshot_date,
            "rank_compare_snapshot_date": metadata.get("rank_compare_snapshot_date"),
            "rank_compare_trading_days": XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS,
            "snapshot_at": metadata.get("snapshot_at"),
            "cube_count": metadata.get("cube_count") or 0,
            "holding_row_count": metadata.get("holding_row_count") or 0,
            "source_cube_count": metadata.get("source_cube_count") or 0,
            "active_cube_count": metadata.get("active_cube_count") or 0,
            "active_rebalance_days": metadata.get("active_rebalance_days"),
            "index_options": index_options,
            "items": item_rows,
        }
    finally:
        connection.close()


def load_xueqiu_top_holdings_history(
    *,
    symbol: str,
    active_only: bool = True,
    limit: int = 500,
) -> Dict[str, Any]:
    normalized_symbol = _normalize_xueqiu_snapshot_symbol(symbol)
    raw_symbol = _raw_xueqiu_snapshot_symbol(normalized_symbol)
    normalized_limit = max(1, min(int(limit or 500), 2000))
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    connection = _connect_duckdb()
    try:
        if not _duckdb_table_exists(connection, XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE):
            return {
                "available": False,
                "reason": "snapshot_table_missing",
                "active_only": active_only,
                "symbol": normalized_symbol,
                "raw_symbol": raw_symbol,
                "limit": normalized_limit,
                "latest": None,
                "history": [],
            }

        cte = _xueqiu_top_holdings_snapshot_cte(active_only)
        rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            date_summary AS (
                SELECT
                    snapshot_date,
                    MAX(snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_symbol) AS cube_count,
                    MAX(active_rebalance_days) AS active_rebalance_days
                FROM filtered_holdings
                GROUP BY snapshot_date
            ),
            stock_summary AS (
                SELECT
                    filtered_holdings.snapshot_date,
                    MAX(date_summary.snapshot_at) AS snapshot_at,
                    MAX(date_summary.cube_count) AS cube_count,
                    MAX(date_summary.active_rebalance_days) AS active_rebalance_days,
                    filtered_holdings.stock_symbol,
                    ANY_VALUE(filtered_holdings.raw_stock_symbol) AS raw_stock_symbol,
                    ANY_VALUE(filtered_holdings.stock_name) AS stock_name,
                    ANY_VALUE(filtered_holdings.segment_name) AS segment_name,
                    MIN(filtered_holdings.year_rank) AS best_year_rank,
                    COUNT(DISTINCT filtered_holdings.cube_symbol) AS holding_cube_count,
                    SUM(filtered_holdings.weight_pct) AS total_weight_pct,
                    SUM(filtered_holdings.weight_pct) / NULLIF(MAX(date_summary.cube_count), 0) AS composite_weight_pct,
                    COUNT(DISTINCT filtered_holdings.cube_symbol) * 100.0 / NULLIF(MAX(date_summary.cube_count), 0) AS holding_cube_ratio_pct,
                    SUM(filtered_holdings.weight_pct) / NULLIF(COUNT(DISTINCT filtered_holdings.cube_symbol), 0) AS average_weight_pct
                FROM filtered_holdings
                JOIN date_summary ON filtered_holdings.snapshot_date = date_summary.snapshot_date
                GROUP BY filtered_holdings.snapshot_date, filtered_holdings.stock_symbol
            ),
            ranked AS (
                SELECT
                    ROW_NUMBER() OVER (
                        PARTITION BY snapshot_date
                        ORDER BY total_weight_pct DESC, holding_cube_count DESC, stock_symbol DESC
                    ) AS composite_rank,
                    *
                FROM stock_summary
            ),
            selected AS (
                SELECT *
                FROM ranked
                WHERE UPPER(stock_symbol) = ?
                   OR UPPER(raw_stock_symbol) = ?
                ORDER BY snapshot_date DESC
                LIMIT ?
            )
            SELECT *
            FROM selected
            ORDER BY snapshot_date
            """,
            [normalized_symbol, raw_symbol, normalized_limit],
        )
        return {
            "available": True,
            "active_only": active_only,
            "symbol": normalized_symbol,
            "raw_symbol": raw_symbol,
            "limit": normalized_limit,
            "latest": rows[-1] if rows else None,
            "history": rows,
        }
    finally:
        connection.close()


def load_xueqiu_top_holding_details(
    *,
    symbol: str,
    snapshot_date: date,
    active_only: bool = True,
    limit: int = 1000,
) -> Dict[str, Any]:
    normalized_symbol = _normalize_xueqiu_snapshot_symbol(symbol)
    raw_symbol = _raw_xueqiu_snapshot_symbol(normalized_symbol)
    normalized_limit = max(1, min(int(limit or 1000), 5000))
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    connection = _connect_duckdb()
    try:
        if not _duckdb_table_exists(connection, XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE):
            return {
                "available": False,
                "reason": "snapshot_table_missing",
                "active_only": active_only,
                "symbol": normalized_symbol,
                "raw_symbol": raw_symbol,
                "snapshot_date": snapshot_date.isoformat(),
                "limit": normalized_limit,
                "cube_count": 0,
                "holding_cube_count": 0,
                "holding_cube_ratio_pct": None,
                "total_weight_pct": 0.0,
                "average_weight_pct": None,
                "details": [],
            }

        cte = _xueqiu_top_holdings_snapshot_cte(active_only)
        summary_rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            date_summary AS (
                SELECT
                    snapshot_date,
                    MAX(snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_symbol) AS cube_count,
                    MAX(active_rebalance_days) AS active_rebalance_days
                FROM filtered_holdings
                WHERE snapshot_date = ?
                GROUP BY snapshot_date
            ),
            selected_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                WHERE snapshot_date = ?
                  AND (
                    UPPER(stock_symbol) = ?
                    OR UPPER(raw_stock_symbol) = ?
                  )
            )
            SELECT
                MAX(selected_holdings.snapshot_date) AS snapshot_date,
                MAX(date_summary.snapshot_at) AS snapshot_at,
                MAX(date_summary.cube_count) AS cube_count,
                MAX(date_summary.active_rebalance_days) AS active_rebalance_days,
                ANY_VALUE(selected_holdings.stock_symbol) AS stock_symbol,
                ANY_VALUE(selected_holdings.raw_stock_symbol) AS raw_stock_symbol,
                ANY_VALUE(selected_holdings.stock_name) AS stock_name,
                ANY_VALUE(selected_holdings.segment_name) AS segment_name,
                COUNT(DISTINCT selected_holdings.cube_symbol) AS holding_cube_count,
                SUM(selected_holdings.weight_pct) AS total_weight_pct,
                SUM(selected_holdings.weight_pct) / NULLIF(COUNT(DISTINCT selected_holdings.cube_symbol), 0) AS average_weight_pct
            FROM date_summary
            LEFT JOIN selected_holdings ON selected_holdings.snapshot_date = date_summary.snapshot_date
            """,
            [snapshot_date, snapshot_date, normalized_symbol, raw_symbol],
        )
        rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            date_summary AS (
                SELECT
                    snapshot_date,
                    MAX(snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_symbol) AS cube_count,
                    MAX(active_rebalance_days) AS active_rebalance_days
                FROM filtered_holdings
                WHERE snapshot_date = ?
                GROUP BY snapshot_date
            ),
            selected_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                WHERE snapshot_date = ?
                  AND (
                    UPPER(stock_symbol) = ?
                    OR UPPER(raw_stock_symbol) = ?
                  )
            )
            SELECT
                selected_holdings.snapshot_date,
                date_summary.snapshot_at,
                date_summary.cube_count,
                date_summary.active_rebalance_days,
                selected_holdings.stock_symbol,
                selected_holdings.raw_stock_symbol,
                selected_holdings.stock_name,
                selected_holdings.segment_name,
                selected_holdings.cube_symbol,
                selected_holdings.cube_id,
                selected_holdings.cube_name,
                selected_holdings.screen_name,
                selected_holdings.year_rank,
                selected_holdings.holdings_source,
                selected_holdings.is_active,
                selected_holdings.latest_rebalance_at,
                selected_holdings.active_rebalance_at,
                selected_holdings.weight_pct
            FROM selected_holdings
            JOIN date_summary ON selected_holdings.snapshot_date = date_summary.snapshot_date
            ORDER BY selected_holdings.weight_pct DESC, selected_holdings.year_rank ASC NULLS LAST, selected_holdings.cube_symbol
            LIMIT ?
            """,
            [snapshot_date, snapshot_date, normalized_symbol, raw_symbol, normalized_limit],
        )
        summary = summary_rows[0] if summary_rows else {}
        cube_count = summary.get("cube_count") or (rows[0].get("cube_count") if rows else 0)
        holding_cube_count = summary.get("holding_cube_count") or 0
        total_weight_pct = float(summary.get("total_weight_pct") or 0.0)
        return {
            "available": True,
            "active_only": active_only,
            "symbol": normalized_symbol,
            "raw_symbol": raw_symbol,
            "stock_name": summary.get("stock_name") or (rows[0].get("stock_name") if rows else None),
            "segment_name": summary.get("segment_name") or (rows[0].get("segment_name") if rows else None),
            "snapshot_date": snapshot_date.isoformat(),
            "snapshot_at": summary.get("snapshot_at") or (rows[0].get("snapshot_at") if rows else None),
            "active_rebalance_days": summary.get("active_rebalance_days") or (rows[0].get("active_rebalance_days") if rows else None),
            "limit": normalized_limit,
            "cube_count": cube_count or 0,
            "holding_cube_count": holding_cube_count,
            "holding_cube_ratio_pct": (
                holding_cube_count * 100.0 / cube_count
                if cube_count
                else None
            ),
            "total_weight_pct": total_weight_pct,
            "average_weight_pct": (
                total_weight_pct / holding_cube_count
                if holding_cube_count
                else None
            ),
            "details": rows,
        }
    finally:
        connection.close()


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


def _get_max_a_stock_market_date() -> Optional[date]:
    connection = _connect_duckdb()
    try:
        try:
            row = connection.execute("SELECT MAX(trade_date) FROM a_stock_market_daily_qfq").fetchone()
        except Exception:
            row = connection.execute("SELECT MAX(trade_date) FROM a_stock_market_daily").fetchone()
    finally:
        connection.close()
    value = row[0] if row else None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return datetime.fromisoformat(str(value)).date()
    return None


def _get_max_a_stock_fund_date(symbols: Optional[List[str]] = None) -> Optional[date]:
    safe_symbols = [
        str(symbol or "").strip().upper()
        for symbol in (symbols or list(A_STOCK_ETF_DAILY_SYMBOL_SET))
        if symbol and SYMBOL_PATTERN.match(str(symbol).strip().upper())
    ]
    if not safe_symbols:
        return None
    symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in list(dict.fromkeys(safe_symbols)))
    connection = _connect_duckdb()
    try:
        try:
            row = connection.execute(
                f"""
                SELECT MAX(trade_date)
                FROM a_stock_fund_daily_qfq
                WHERE ts_code IN ({symbol_sql})
                """
            ).fetchone()
        except Exception:
            row = connection.execute(
                f"""
                SELECT MAX(trade_date)
                FROM a_stock_fund_daily
                WHERE ts_code IN ({symbol_sql})
                """
            ).fetchone()
    finally:
        connection.close()
    value = row[0] if row else None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return datetime.fromisoformat(str(value)).date()
    return None


def _get_latest_inno100_level_date(db: ORMSession) -> Optional[date]:
    row = (
        db.query(AStockInnovation100Level.date)
        .filter(AStockInnovation100Level.index_code == A_STOCK_INNO100_INDEX_CODE)
        .order_by(AStockInnovation100Level.date.desc())
        .first()
    )
    value = row[0] if row else None
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _resolve_analysis_end_date(pool: str, requested_end: Optional[date], db: ORMSession) -> date:
    if requested_end:
        return requested_end
    pool_key = str(pool or "").strip().upper()
    if not _is_a_stock_pool(pool_key):
        return _get_max_trade_date()
    candidates = [_get_max_a_stock_market_date()]
    if pool_key == A_STOCK_INNO100_POOL:
        candidates.append(_get_latest_inno100_level_date(db))
    if pool_key in A_STOCK_INDEX_POOL_CODE_SET:
        candidates.append(get_max_a_stock_index_daily_date([pool_key]))
    candidates = [item for item in candidates if item is not None]
    return min(candidates) if candidates else date.today()


def _resolve_timing_end_date(request: TimingFactorAnalyzeRequest, db: ORMSession) -> date:
    if request.end_date:
        return request.end_date
    target = str(request.target_symbol or "").strip().upper()
    if target == A_STOCK_INNO100_SYMBOL:
        candidates = [item for item in [_get_latest_inno100_level_date(db), _get_max_a_stock_market_date()] if item is not None]
        return min(candidates) if candidates else date.today()
    if target in A_STOCK_ETF_DAILY_SYMBOL_SET:
        candidates = [item for item in [_get_max_a_stock_fund_date([target]), _get_max_a_stock_market_date()] if item is not None]
        return min(candidates) if candidates else date.today()
    if _is_a_stock_symbol(target):
        candidates = [item for item in [_get_max_a_stock_market_date()] if item is not None]
        return min(candidates) if candidates else date.today()
    return _get_max_trade_date()


def _load_price_frame(symbols: List[str], start_date: date, end_date: date) -> pl.DataFrame:
    safe_symbols = [
        symbol for symbol in list(dict.fromkeys(symbols))
        if symbol and SYMBOL_PATTERN.match(symbol)
    ]
    schema = {
        "symbol": pl.Utf8,
        "trade_date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "turnover": pl.Float64,
    }
    if not safe_symbols:
        return pl.DataFrame(schema=schema)

    frames: List[pl.DataFrame] = []
    connection = _connect_duckdb()
    try:
        us_symbols = [symbol for symbol in safe_symbols if str(symbol).upper().endswith(".US")]
        if us_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in us_symbols)
            query = f"""
                SELECT
                    symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
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
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))

        a_index_symbols = [symbol for symbol in safe_symbols if symbol in A_STOCK_INDEX_POOL_CODE_SET]
        if a_index_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_index_symbols)
            query = f"""
                SELECT
                    ts_code AS symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(vol AS DOUBLE) AS volume,
                    CAST(amount AS DOUBLE) AS turnover
                FROM a_stock_index_daily
                WHERE ts_code IN ({symbol_sql})
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY ts_code, trade_date
            """
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))

        a_fund_symbols = [
            symbol
            for symbol in safe_symbols
            if symbol in A_STOCK_ETF_DAILY_SYMBOL_SET
        ]
        if a_fund_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_fund_symbols)
            query = f"""
                SELECT
                    ts_code AS symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(vol AS DOUBLE) AS volume,
                    CAST(amount AS DOUBLE) AS turnover
                FROM a_stock_fund_daily_qfq
                WHERE ts_code IN ({symbol_sql})
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY ts_code, trade_date
            """
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))

        a_symbols = [
            symbol
            for symbol in safe_symbols
            if _is_a_stock_symbol(symbol) and symbol not in A_STOCK_INDEX_POOL_CODE_SET
            and symbol not in A_STOCK_ETF_DAILY_SYMBOL_SET
        ]
        if a_symbols:
            symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_symbols)
            query = f"""
                SELECT
                    ts_code AS symbol,
                    CAST(trade_date AS DATE) AS trade_date,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(vol AS DOUBLE) AS volume,
                    CAST(amount AS DOUBLE) AS turnover
                FROM a_stock_market_daily_qfq
                WHERE ts_code IN ({symbol_sql})
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY ts_code, trade_date
            """
            frames.append(pl.read_database(query, connection, execute_options={"parameters": [start_date, end_date]}))
    finally:
        connection.close()

    non_empty_frames = [frame for frame in frames if frame is not None and not frame.is_empty()]
    if not non_empty_frames:
        return pl.DataFrame(schema=schema)
    df = pl.concat(non_empty_frames, how="vertical_relaxed")
    if df.is_empty():
        return pl.DataFrame(schema=schema)
    return df.with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("turnover").cast(pl.Float64),
    ).sort(["symbol", "trade_date"]).with_columns(
        pl.min("trade_date").over("symbol").alias("_first_trade_date")
    )


def _load_inno100_level_price_frame(db: ORMSession, start_date: date, end_date: date) -> pl.DataFrame:
    rows = (
        db.query(AStockInnovation100Level.date, AStockInnovation100Level.level)
        .filter(
            AStockInnovation100Level.index_code == A_STOCK_INNO100_INDEX_CODE,
            AStockInnovation100Level.date >= start_date,
            AStockInnovation100Level.date <= end_date,
            AStockInnovation100Level.level.isnot(None),
        )
        .order_by(AStockInnovation100Level.date.asc())
        .all()
    )
    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "trade_date": pl.Date,
                "open": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
                "turnover": pl.Float64,
                "_first_trade_date": pl.Date,
            }
        )
    return (
        pl.DataFrame(
            {
                "symbol": [A_STOCK_INNO100_SYMBOL for _ in rows],
                "trade_date": [row.date for row in rows],
                "open": [row.level for row in rows],
                "close": [row.level for row in rows],
                "volume": [0.0 for _ in rows],
                "turnover": [0.0 for _ in rows],
            }
        )
        .with_columns(
            pl.col("trade_date").cast(pl.Date),
            pl.col("open").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
            pl.col("turnover").cast(pl.Float64),
        )
        .sort(["symbol", "trade_date"])
        .with_columns(pl.min("trade_date").over("symbol").alias("_first_trade_date"))
    )


def _load_target_price_frame(db: ORMSession, symbol: str, start_date: date, end_date: date) -> pl.DataFrame:
    target = str(symbol or "").strip().upper()
    if target == A_STOCK_INNO100_SYMBOL:
        return _load_inno100_level_price_frame(db, start_date, end_date)
    return _load_price_frame([target], start_date, end_date)


def _fear_source_label(symbol: str) -> str:
    symbol = str(symbol or "").strip().upper()
    if symbol == CNN_HISTORY_SYMBOL:
        return "CNN Fear & Greed"
    if symbol in A_STOCK_FEAR_SOURCE_LABELS:
        return A_STOCK_FEAR_SOURCE_LABELS[symbol]
    if symbol.endswith(".US"):
        return f"{symbol[:-3]} 自算贪恐"
    return symbol


def _load_fear_history_frame(
    db: ORMSession,
    symbol: str,
    start_date: date,
    end_date: date,
    lookback_days: int = 420,
) -> pl.DataFrame:
    history_start = start_date - timedelta(days=max(0, int(lookback_days)))
    rows = (
        db.query(
            ETFFearGreedCloneHistory.symbol,
            ETFFearGreedCloneHistory.date,
            ETFFearGreedCloneHistory.score,
            ETFFearGreedCloneHistory.rating,
            ETFFearGreedCloneHistory.method,
        )
        .filter(
            ETFFearGreedCloneHistory.symbol == symbol,
            ETFFearGreedCloneHistory.date >= history_start,
            ETFFearGreedCloneHistory.date <= end_date,
            ETFFearGreedCloneHistory.score.isnot(None),
        )
        .order_by(ETFFearGreedCloneHistory.date.asc())
        .all()
    )
    if not rows:
        return pl.DataFrame(
            schema={
                "trade_date": pl.Date,
                "score": pl.Float64,
                "rating": pl.Utf8,
                "method": pl.Utf8,
            }
        )
    return (
        pl.DataFrame(
            [
                {
                    "trade_date": row.date,
                    "score": float(row.score),
                    "rating": row.rating,
                    "method": row.method,
                }
                for row in rows
                if row.score is not None
            ]
        )
        .with_columns(
            pl.col("trade_date").cast(pl.Date),
            pl.col("score").cast(pl.Float64),
        )
        .unique(subset=["trade_date"], keep="last")
        .sort("trade_date")
    )


def _timing_ma_label(ma_window: int) -> str:
    return "原始值" if int(ma_window) <= 1 else f"{int(ma_window)}日均值"


def _rank_average(values: List[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index + 1
        while end < len(indexed) and indexed[end][1] == indexed[index][1]:
            end += 1
        avg_rank = (index + 1 + end) / 2.0
        for offset in range(index, end):
            ranks[indexed[offset][0]] = avg_rank
        index = end
    return ranks


def _pearson_corr(left: List[float], right: List[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_arr = np.array(left, dtype=float)
    right_arr = np.array(right, dtype=float)
    if not np.isfinite(left_arr).all() or not np.isfinite(right_arr).all():
        return None
    left_std = float(left_arr.std(ddof=1))
    right_std = float(right_arr.std(ddof=1))
    if left_std <= 0 or right_std <= 0:
        return None
    return float(np.corrcoef(left_arr, right_arr)[0, 1])


def _spearman_corr_from_records(records: List[Dict[str, Any]]) -> Optional[float]:
    pairs = [
        (float(item["factor_value"]), float(item["forward_return"]))
        for item in records
        if item.get("factor_value") is not None
        and item.get("forward_return") is not None
        and math.isfinite(float(item["factor_value"]))
        and math.isfinite(float(item["forward_return"]))
    ]
    if len(pairs) < 3:
        return None
    factor_ranks = _rank_average([item[0] for item in pairs])
    return_ranks = _rank_average([item[1] for item in pairs])
    return _pearson_corr(factor_ranks, return_ranks)


def _compute_timing_ic_series(sample_df: pl.DataFrame, window: int = 60, min_periods: int = 20) -> pl.DataFrame:
    if sample_df.is_empty():
        return pl.DataFrame()

    records = sample_df.sort("trade_date").select(
        "trade_date",
        "factor_value",
        "forward_return",
    ).to_dicts()
    rows = []
    for index, item in enumerate(records):
        start = max(0, index - window + 1)
        window_records = records[start:index + 1]
        rank_ic = _spearman_corr_from_records(window_records) if len(window_records) >= min_periods else None
        rows.append(
            {
                "trade_date": item["trade_date"],
                "samples": len(window_records),
                "rank_ic": _safe_float(rank_ic, 6),
            }
        )

    ic_df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date)).sort("trade_date")
    return (
        ic_df.with_columns(
            pl.col("rank_ic").rolling_mean(window_size=20, min_samples=5).alias("rank_ic_ma20"),
            pl.col("rank_ic").rolling_mean(window_size=60, min_samples=10).alias("rank_ic_ma60"),
            pl.col("rank_ic").fill_null(0).cum_sum().alias("cumulative_rank_ic"),
        )
        .with_columns(
            pl.col("rank_ic_ma20").round(6),
            pl.col("rank_ic_ma60").round(6),
            pl.col("cumulative_rank_ic").round(6),
        )
    )


def _prepare_timing_sample(
    price_df: pl.DataFrame,
    fear_df: pl.DataFrame,
    request: TimingFactorAnalyzeRequest,
    forward_window: int,
    ma_window: int,
) -> pl.DataFrame:
    if price_df.is_empty() or fear_df.is_empty():
        return pl.DataFrame()

    price = (
        price_df.select("symbol", "trade_date", "close")
        .sort("trade_date")
        .with_columns(
            pl.col("close").shift(-int(forward_window)).over("symbol").alias("_future_close")
        )
    )
    factor_source = (
        fear_df.sort("trade_date")
        .with_columns(
            (
                pl.col("score")
                if int(ma_window) <= 1
                else pl.col("score").rolling_mean(window_size=int(ma_window), min_samples=int(ma_window))
            ).alias("factor_value")
        )
        .select(
            "trade_date",
            pl.col("score").alias("fear_score"),
            "factor_value",
        )
        .sort("trade_date")
    )
    sample = (
        price.join_asof(factor_source, on="trade_date", strategy="backward")
        .with_columns(
            (pl.col("_future_close") / pl.col("close") - 1).alias("forward_return"),
        )
        .with_columns(pl.col("forward_return").mean().alias("_mean_forward_return"))
        .with_columns((pl.col("forward_return") - pl.col("_mean_forward_return")).alias("forward_excess_return"))
        .filter(
            (pl.col("trade_date") >= request.start_date)
            & pl.col("factor_value").is_not_null()
            & pl.col("factor_value").is_finite()
            & pl.col("forward_return").is_not_null()
            & pl.col("forward_return").is_finite()
        )
        .select(
            "symbol",
            "trade_date",
            "close",
            "fear_score",
            "factor_value",
            pl.col("factor_value").alias("factor_value_raw"),
            "forward_return",
            "forward_excess_return",
        )
        .sort("trade_date")
    )
    if sample.is_empty():
        return sample

    sample_count = sample.height
    return (
        sample.with_columns(
            pl.col("factor_value").rank(method="ordinal").alias("_factor_rank")
        )
        .with_columns(
            (
                ((pl.col("_factor_rank") - 1) * int(request.bucket_count) / sample_count).floor() + 1
            )
            .clip(1, int(request.bucket_count))
            .cast(pl.Int64)
            .alias("bucket")
        )
    )


def _compute_timing_non_overlapping_stats(
    sample_df: pl.DataFrame,
    bucket_count: int,
    forward_window: int,
) -> Dict[str, Any]:
    if sample_df.is_empty():
        return {"summary": {}, "offsets": []}
    step = max(1, int(forward_window))
    rows = []
    source = sample_df.sort("trade_date").with_row_index("_date_index").with_columns(
        (pl.col("_date_index") % step).cast(pl.Int64).alias("offset")
    )
    for offset in range(step):
        part = source.filter(pl.col("offset") == offset)
        if part.is_empty():
            continue
        bucket_df = _compute_bucket_report(part)
        top_row = bucket_df.filter(pl.col("bucket") == bucket_count)
        bottom_row = bucket_df.filter(pl.col("bucket") == 1)
        if not top_row.height or not bottom_row.height:
            continue
        top_return = _safe_float(top_row.select("avg_return_pct").item(), 6)
        bottom_return = _safe_float(bottom_row.select("avg_return_pct").item(), 6)
        spread = None if top_return is None or bottom_return is None else bottom_return - top_return
        rows.append(
            {
                "offset": offset,
                "periods": int(part.height),
                "start_date": part.select(pl.min("trade_date")).item(),
                "end_date": part.select(pl.max("trade_date")).item(),
                "avg_top_minus_bottom_return_pct": _safe_float(spread, 4),
                "annualized_top_minus_bottom_return_pct": _annualize_period_return_pct(spread, forward_window),
                "positive_periods": 1 if spread is not None and spread > 0 else 0,
                "positive_period_rate_pct": 100.0 if spread is not None and spread > 0 else 0.0,
                "t_stat": None,
            }
        )

    if not rows:
        return {"summary": {}, "offsets": []}
    annualized_values = [
        item["annualized_top_minus_bottom_return_pct"]
        for item in rows
        if item.get("annualized_top_minus_bottom_return_pct") is not None
    ]
    avg_values = [
        item["avg_top_minus_bottom_return_pct"]
        for item in rows
        if item.get("avg_top_minus_bottom_return_pct") is not None
    ]
    positive = sum(1 for item in rows if (item.get("avg_top_minus_bottom_return_pct") or 0) > 0)
    summary = {
        "forward_window": int(forward_window),
        "offsets": len(rows),
        "total_periods": sum(int(item["periods"]) for item in rows),
        "avg_period_return_pct": _safe_float(_mean(avg_values), 4),
        "annualized_mean_pct": _safe_float(_mean(annualized_values), 4),
        "annualized_median_pct": _safe_float(_median(annualized_values), 4),
        "positive_period_rate_pct": _safe_float(positive * 100 / len(rows), 2),
        "mean_t_stat": None,
    }
    return {"summary": summary, "offsets": [_serialize_timing_record(item) for item in rows]}


def _serialize_timing_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _serialize_value(value) for key, value in record.items()}


def _compute_timing_yearly_stability(
    sample_df: pl.DataFrame,
    bucket_count: int,
    forward_window: int,
) -> pl.DataFrame:
    if sample_df.is_empty():
        return pl.DataFrame()

    rows = []
    for year in sorted(sample_df.select(pl.col("trade_date").dt.year()).unique().to_series().to_list()):
        part = sample_df.filter(pl.col("trade_date").dt.year() == year)
        bucket_df = _compute_bucket_report(part)
        top_row = bucket_df.filter(pl.col("bucket") == bucket_count)
        bottom_row = bucket_df.filter(pl.col("bucket") == 1)
        top_return = _safe_float(top_row.select("avg_return_pct").item(), 6) if top_row.height else None
        bottom_return = _safe_float(bottom_row.select("avg_return_pct").item(), 6) if bottom_row.height else None
        spread = None if top_return is None or bottom_return is None else bottom_return - top_return
        ic = _spearman_corr_from_records(part.select("factor_value", "forward_return").to_dicts())
        rows.append(
            {
                "year": int(year),
                "samples": int(part.height),
                "trade_dates": int(part.select(pl.n_unique("trade_date")).item()),
                "symbols": 1,
                "avg_top_minus_bottom_return_pct": _safe_float(spread, 4),
                "annualized_top_minus_bottom_return_pct": _annualize_period_return_pct(spread, forward_window),
                "non_overlap_annualized_median_pct": None,
                "avg_rank_ic": _safe_float(ic, 6),
                "positive_ic_rate_pct": 100.0 if ic is not None and ic > 0 else 0.0,
                "positive_spread_rate_pct": 100.0 if spread is not None and spread > 0 else 0.0,
            }
        )
    return pl.DataFrame(rows).sort("year")


def _summarize_timing_sample(
    sample_df: pl.DataFrame,
    bucket_df: pl.DataFrame,
    ic_df: pl.DataFrame,
    request: TimingFactorAnalyzeRequest,
    forward_window: int,
    elapsed_ms: float,
    non_overlap_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    records = sample_df.select("factor_value", "forward_return").to_dicts() if not sample_df.is_empty() else []
    global_ic = _safe_float(_spearman_corr_from_records(records), 6)
    rolling_ics = [
        float(value)
        for value in (ic_df.select("rank_ic").drop_nulls().to_series().to_list() if not ic_df.is_empty() else [])
        if value is not None and math.isfinite(float(value))
    ]
    ic_std = _safe_float(float(np.std(rolling_ics, ddof=1)), 6) if len(rolling_ics) > 1 else None
    ic_avg = _safe_float(_mean(rolling_ics), 6)
    icir = _safe_float(ic_avg / ic_std, 4) if ic_avg is not None and ic_std is not None and ic_std > 0 else None
    sample_n = len(records)
    rank_ic_t_stat = None
    if global_ic is not None and sample_n > 2 and abs(global_ic) < 1:
        rank_ic_t_stat = _safe_float(
            global_ic * math.sqrt((sample_n - 2) / max(1e-12, 1 - global_ic * global_ic)),
            4,
        )

    top_return = None
    bottom_return = None
    spread_return = None
    if not bucket_df.is_empty():
        top_row = bucket_df.filter(pl.col("bucket") == request.bucket_count)
        bottom_row = bucket_df.filter(pl.col("bucket") == 1)
        top_return = _safe_float(top_row.select("avg_return_pct").item(), 4) if top_row.height else None
        bottom_return = _safe_float(bottom_row.select("avg_return_pct").item(), 4) if bottom_row.height else None
        if top_return is not None and bottom_return is not None:
            spread_return = round(top_return - bottom_return, 4)
    monotonicity = _compute_monotonicity(bucket_df)
    summary = {
        "samples": int(sample_df.height),
        "trade_dates": int(sample_df.select(pl.n_unique("trade_date")).item()) if not sample_df.is_empty() else 0,
        "symbols": 1 if not sample_df.is_empty() else 0,
        "rank_ic_mean": global_ic,
        "rank_ic_std": ic_std,
        "icir": icir,
        "rank_ic_t_stat": rank_ic_t_stat,
        "top_bucket_avg_return_pct": top_return,
        "bottom_bucket_avg_return_pct": bottom_return,
        "top_minus_bottom_avg_return_pct": spread_return,
        "annualized_top_minus_bottom_avg_return_pct": _annualize_period_return_pct(spread_return, forward_window),
        "low_minus_high_avg_return_pct": _safe_float(-spread_return, 4) if spread_return is not None else None,
        "annualized_low_minus_high_avg_return_pct": _annualize_period_return_pct(
            _safe_float(-spread_return, 6) if spread_return is not None else None,
            forward_window,
        ),
        "elapsed_ms": round(elapsed_ms, 1),
    } | monotonicity
    if non_overlap_summary:
        summary.update(
            {
                "non_overlap_annualized_median_pct": non_overlap_summary.get("annualized_median_pct"),
                "non_overlap_annualized_mean_pct": non_overlap_summary.get("annualized_mean_pct"),
                "non_overlap_positive_period_rate_pct": non_overlap_summary.get("positive_period_rate_pct"),
                "non_overlap_offsets": non_overlap_summary.get("offsets"),
                "spread_t_stat": non_overlap_summary.get("mean_t_stat"),
            }
        )
    return summary


def _compute_timing_heatmap(
    price_df: pl.DataFrame,
    fear_df: pl.DataFrame,
    request: TimingFactorAnalyzeRequest,
) -> List[Dict[str, Any]]:
    if not request.include_heatmap:
        return []
    records = []
    for ma_window in request.heatmap_ma_windows:
        for forward_window in request.heatmap_forward_windows:
            sample = _prepare_timing_sample(price_df, fear_df, request, int(forward_window), int(ma_window))
            if sample.is_empty():
                continue
            bucket_df = _compute_bucket_report(sample)
            summary = _summarize_timing_sample(
                sample,
                bucket_df,
                _compute_timing_ic_series(sample),
                request,
                int(forward_window),
                0,
            )
            records.append(
                {
                    "ma_window": int(ma_window),
                    "ma_window_label": _timing_ma_label(int(ma_window)),
                    "forward_window": int(forward_window),
                    "samples": summary.get("samples"),
                    "rank_ic_mean": summary.get("rank_ic_mean"),
                    "rank_ic_t_stat": summary.get("rank_ic_t_stat"),
                    "top_minus_bottom_avg_return_pct": summary.get("top_minus_bottom_avg_return_pct"),
                    "annualized_top_minus_bottom_avg_return_pct": summary.get("annualized_top_minus_bottom_avg_return_pct"),
                    "low_minus_high_avg_return_pct": summary.get("low_minus_high_avg_return_pct"),
                    "annualized_low_minus_high_avg_return_pct": summary.get("annualized_low_minus_high_avg_return_pct"),
                    "monotonicity_spearman": summary.get("monotonicity_spearman"),
                    "adjacent_hit_rate_pct": summary.get("adjacent_hit_rate_pct"),
                    "heatmap_value": summary.get("annualized_low_minus_high_avg_return_pct"),
                    "selected_heatmap_value": summary.get(request.heatmap_metric),
                }
            )
    return records


def _get_timing_fear_sources(db: ORMSession) -> List[Dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                symbol,
                MIN(date) AS start_date,
                MAX(date) AS end_date,
                COUNT(*) AS points
            FROM "etf_fear_greed_clone_history"
            WHERE symbol IS NOT NULL
            GROUP BY symbol
            ORDER BY symbol
            """
        )
    ).mappings().all()
    sources = [
        {
            "label": _fear_source_label(row["symbol"]),
            "value": row["symbol"],
            "symbol": row["symbol"],
            "start_date": row["start_date"].isoformat() if hasattr(row["start_date"], "isoformat") else str(row["start_date"] or ""),
            "end_date": row["end_date"].isoformat() if hasattr(row["end_date"], "isoformat") else str(row["end_date"] or ""),
            "points": int(row["points"] or 0),
        }
        for row in rows
    ]
    sources.sort(
        key=lambda item: (
            0 if str(item["symbol"] or "").upper() == CNN_HISTORY_SYMBOL else 1,
            A_STOCK_FEAR_SOURCE_ORDER.get(str(item["symbol"] or "").upper(), 10_000),
            item["label"],
        )
    )
    return sources


def _normalize_symbol_search_limit(value: Any, default: int = 20) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(50, number))


def _search_a_stock_fund_pool_symbols(connection: Any, search_text: str, limit: int) -> List[Dict[str, Any]]:
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT
                d.ts_code,
                COALESCE(b.name, '') AS name
            FROM a_stock_fund_daily d
            LEFT JOIN a_stock_fund_basic b
              ON d.ts_code = b.ts_code
            """
        ).fetchall()
    except Exception:
        rows = connection.execute(
            """
            SELECT DISTINCT ts_code, '' AS name
            FROM a_stock_fund_daily
            """
        ).fetchall()

    options: List[Dict[str, Any]] = []
    for ts_code, name in rows:
        symbol = normalize_a_stock_symbol(ts_code)
        fallback_name = A_STOCK_ETF_DAILY_NAMES.get(symbol)
        display_name = str(name or fallback_name or "").strip()
        searchable = f"{symbol} {display_name}".upper()
        if search_text and search_text not in searchable:
            continue
        if display_name:
            label = f"{display_name} {symbol}"
        else:
            label = f"ETF {symbol}"
        if search_text and symbol.upper() == search_text:
            score = 0
        elif search_text and symbol.upper().startswith(search_text):
            score = 1
        elif search_text and display_name.upper().find(search_text) >= 0:
            score = 2
        else:
            score = 3
        options.append({"label": label, "value": symbol, "name": display_name or None, "_score": score})

    options.sort(key=lambda item: (item.get("_score", 99), item["value"]))
    return [{key: value for key, value in item.items() if key != "_score"} for item in options[:limit]]


def _search_a_stock_pool_symbols(query: str, limit: int) -> List[Dict[str, Any]]:
    search_text = str(query or "").strip().upper()
    like_pattern = f"%{search_text}%"
    connection = _connect_duckdb()
    try:
        stock_rows = connection.execute(
            f"""
            SELECT DISTINCT
                ts_code,
                COALESCE(name, '') AS name
            FROM a_stock_basic
            WHERE list_status = 'L'
              AND (
                    UPPER(ts_code) LIKE ?
                 OR UPPER(symbol) LIKE ?
                 OR UPPER(COALESCE(name, '')) LIKE ?
              )
            ORDER BY
                CASE
                    WHEN UPPER(ts_code) = ? THEN 0
                    WHEN UPPER(ts_code) LIKE ? THEN 1
                    WHEN UPPER(COALESCE(name, '')) LIKE ? THEN 2
                    ELSE 3
                END,
                ts_code
            LIMIT {limit}
            """,
            [like_pattern, like_pattern, like_pattern, search_text, f"{search_text}%", like_pattern],
        ).fetchall()
        fund_options = _search_a_stock_fund_pool_symbols(connection, search_text, limit)
    finally:
        connection.close()

    stock_options: List[Dict[str, Any]] = []
    seen_symbols = set()
    for ts_code, name in stock_rows:
        symbol = normalize_a_stock_symbol(ts_code)
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        display_name = str(name or "").strip()
        label = f"{display_name} {symbol}".strip() if display_name else symbol
        stock_options.append({"label": label, "value": symbol, "name": display_name or None})

    deduped_fund_options: List[Dict[str, Any]] = []
    for item in fund_options:
        symbol = item.get("value")
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        deduped_fund_options.append(item)

    groups: List[Dict[str, Any]] = []
    if stock_options:
        groups.append({"label": "A股个股", "options": stock_options})
    if deduped_fund_options:
        groups.append({"label": "A股ETF", "options": deduped_fund_options})
    return groups


def _search_us_stock_symbols(query: str, limit: int, db: ORMSession) -> List[Dict[str, Any]]:
    search_text = str(query or "").strip().upper()
    like_pattern = f"%{search_text}%"
    connection = _connect_duckdb()
    try:
        rows = connection.execute(
            f"""
            SELECT DISTINCT symbol
            FROM us_stock_daily
            WHERE UPPER(symbol) LIKE ?
            ORDER BY
                CASE
                    WHEN UPPER(symbol) = ? THEN 0
                    WHEN UPPER(symbol) LIKE ? THEN 1
                    ELSE 2
                END,
                symbol
            LIMIT {limit}
            """,
            [like_pattern, search_text, f"{search_text}%"],
        ).fetchall()
    finally:
        connection.close()

    normalized_symbols = []
    for (symbol,) in rows:
        normalized = normalize_us_equity_symbol(symbol)
        if normalized:
            normalized_symbols.append(normalized)
    name_map = load_symbol_name_map(normalized_symbols, db)
    options = []
    for normalized in normalized_symbols:
        name = name_map.get(normalized)
        options.append({
            "label": f"{name} {normalized}" if name else normalized,
            "value": normalized,
            "name": name,
        })
    return [{"label": "美股", "options": options}] if options else []


@router.get("/symbol-search")
def search_factor_lab_symbols(
    market: str = Query(..., description="a_stock 或 us_stock"),
    q: str = Query("", description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    market_key = str(market or "").strip().lower()
    normalized_limit = _normalize_symbol_search_limit(limit, 20)
    if market_key == "a_stock":
        options = _search_a_stock_pool_symbols(q, normalized_limit)
    elif market_key == "us_stock":
        options = _search_us_stock_symbols(q, normalized_limit, db)
    else:
        raise HTTPException(status_code=400, detail="market 仅支持 a_stock 或 us_stock")
    return {
        "market": market_key,
        "query": q,
        "limit": normalized_limit,
        "options": options,
    }


@router.get("/xueqiu-top-holdings/latest")
def get_xueqiu_top_holdings_latest(
    active_only: bool = Query(True, description="只统计主理人活跃组合"),
    limit: int = Query(300, ge=1, le=2000),
    _: str = Depends(valid_account),
):
    return load_xueqiu_top_holdings_latest(active_only=active_only, limit=limit)


@router.get("/xueqiu-top-holdings/history")
def get_xueqiu_top_holdings_history(
    symbol: str = Query(..., min_length=1),
    active_only: bool = Query(True, description="只统计主理人活跃组合"),
    limit: int = Query(500, ge=1, le=2000),
    _: str = Depends(valid_account),
):
    return load_xueqiu_top_holdings_history(
        symbol=symbol,
        active_only=active_only,
        limit=limit,
    )


@router.get("/xueqiu-top-holdings/details")
def get_xueqiu_top_holding_details(
    symbol: str = Query(..., min_length=1),
    snapshot_date: date = Query(...),
    active_only: bool = Query(True, description="只统计主理人活跃组合"),
    limit: int = Query(1000, ge=1, le=5000),
    _: str = Depends(valid_account),
):
    return load_xueqiu_top_holding_details(
        symbol=symbol,
        snapshot_date=snapshot_date,
        active_only=active_only,
        limit=limit,
    )


def _load_industry_frame(
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    if not symbols:
        return pl.DataFrame()

    frames: List[pl.DataFrame] = []
    us_symbols = [symbol for symbol in symbols if str(symbol).upper().endswith(".US")]
    if us_symbols:
        rows = (
            db.query(
                USStockIndustrySnapshot.symbol,
                USStockIndustrySnapshot.date,
                USStockIndustrySnapshot.sector,
                USStockIndustrySnapshot.industry_group,
                USStockIndustrySnapshot.industry,
                USStockIndustrySnapshot.sub_industry,
                USStockIndustrySnapshot.market_cap,
            )
            .filter(
                USStockIndustrySnapshot.symbol.in_(us_symbols),
                USStockIndustrySnapshot.provider == "fmp",
            )
            .all()
        )
        if rows:
            frames.append(
                pl.DataFrame(
                    {
                        "symbol": [row.symbol for row in rows],
                        "industry_date": [row.date for row in rows],
                        "sector": [row.sector for row in rows],
                        "industry_group": [row.industry_group for row in rows],
                        "industry": [row.industry for row in rows],
                        "sub_industry": [row.sub_industry for row in rows],
                        "market_cap": [row.market_cap for row in rows],
                    }
                )
                .with_columns(
                    pl.col("industry_date").cast(pl.Date),
                    pl.col("market_cap").cast(pl.Float64),
                )
                .sort(["symbol", "industry_date"])
                .unique(subset=["symbol"], keep="last")
            )

    a_symbols = [symbol for symbol in symbols if _is_a_stock_symbol(symbol)]
    if a_symbols:
        symbol_sql = ", ".join(_quote_sql_string(symbol) for symbol in a_symbols)
        query = f"""
            SELECT
                ts_code AS symbol,
                industry AS sector,
                industry AS industry_group,
                industry AS industry,
                industry AS sub_industry,
                CAST(NULL AS DOUBLE) AS market_cap
            FROM a_stock_basic
            WHERE ts_code IN ({symbol_sql})
        """
        connection = _connect_duckdb()
        try:
            a_frame = pl.read_database(query, connection)
        finally:
            connection.close()
        if not a_frame.is_empty():
            frames.append(
                a_frame.with_columns(
                    pl.lit(end_date).cast(pl.Date).alias("industry_date"),
                    pl.col("market_cap").cast(pl.Float64),
                )
            )

    non_empty_frames = [frame for frame in frames if frame is not None and not frame.is_empty()]
    if not non_empty_frames:
        return pl.DataFrame()

    return (
        pl.concat(non_empty_frames, how="vertical_relaxed")
        .select(["symbol", "industry_date", "sector", "industry_group", "industry", "sub_industry", "market_cap"])
        .sort("symbol")
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


def _empty_like(df: pl.DataFrame) -> pl.DataFrame:
    return pl.DataFrame(schema=df.schema)


def _split_sample_for_oos(
    df: pl.DataFrame,
    analysis_dates: List[date],
    oos_start_date: Optional[date],
    forward_window: int,
) -> Dict[str, pl.DataFrame]:
    if df.is_empty() or oos_start_date is None or not analysis_dates:
        return {"analysis": df, "oos": _empty_like(df)}

    split_index = bisect_left(analysis_dates, oos_start_date)
    if split_index >= len(analysis_dates):
        return {"analysis": df, "oos": _empty_like(df)}

    # Purge one full forward-return window before the OOS start date so the
    # training metric does not use outcomes realized inside the OOS period.
    train_cutoff_index = max(0, split_index - max(1, int(forward_window)))
    train_dates = analysis_dates[:train_cutoff_index]
    oos_dates = analysis_dates[split_index:]

    analysis_df = (
        df.filter(pl.col("trade_date").is_in(train_dates))
        if train_dates
        else _empty_like(df)
    )
    oos_df = (
        df.filter(pl.col("trade_date").is_in(oos_dates))
        if oos_dates
        else _empty_like(df)
    )
    return {"analysis": analysis_df, "oos": oos_df}


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


def _compute_factor_value_distribution(
    df: pl.DataFrame,
    bin_count: int = FACTOR_DISTRIBUTION_BIN_COUNT,
) -> List[Dict[str, Any]]:
    if df.is_empty() or "factor_value" not in df.columns:
        return []

    effective_bin_count = max(1, int(bin_count))
    series_specs = [
        ("raw", "原始因子值", "factor_value_raw" if "factor_value_raw" in df.columns else "factor_value"),
        ("analysis", "标准化/分析值", "factor_value"),
    ]
    series_frames: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []

    for series_key, series_label, column in series_specs:
        if column not in df.columns:
            continue

        value_df = (
            df.select(pl.col(column).cast(pl.Float64, strict=False).alias("value"))
            .filter(pl.col("value").is_not_null() & pl.col("value").is_finite())
        )
        total = value_df.height
        if total <= 0:
            continue

        stats = value_df.select(
            pl.min("value").alias("min_value"),
            pl.max("value").alias("max_value"),
            pl.mean("value").alias("mean_value"),
            pl.std("value").alias("std_value"),
        ).to_dicts()[0]
        min_value = _safe_float(stats.get("min_value"))
        max_value = _safe_float(stats.get("max_value"))
        mean_value = _safe_float(stats.get("mean_value"), 6)
        std_value = _safe_float(stats.get("std_value"), 6)
        if min_value is None or max_value is None:
            continue

        series_frames.append(
            {
                "series_key": series_key,
                "series_label": series_label,
                "value_df": value_df,
                "total": int(total),
                "min_value": min_value,
                "max_value": max_value,
                "mean_value": mean_value,
                "std_value": std_value,
            }
        )

    if not series_frames:
        return []

    for item in series_frames:
        series_key = item["series_key"]
        series_label = item["series_label"]
        value_df = item["value_df"]
        total = item["total"]
        min_value = item["min_value"]
        max_value = item["max_value"]
        mean_value = item["mean_value"]
        std_value = item["std_value"]

        if max_value == min_value:
            records.append(
                {
                    "series": series_key,
                    "series_label": series_label,
                    "bucket_id": 1,
                    "bucket_count": 1,
                    "value_from": _safe_float(min_value, 6),
                    "value_to": _safe_float(max_value, 6),
                    "samples": int(total),
                    "pct": 100.0,
                    "total_samples": int(total),
                    "mean_value": mean_value,
                    "std_value": std_value,
                }
            )
            continue

        width = (max_value - min_value) / effective_bin_count
        count_df = (
            value_df.with_columns(
                (
                    ((pl.col("value") - min_value) / width).floor() + 1
                )
                .clip(1, effective_bin_count)
                .cast(pl.Int64)
                .alias("bucket_id")
            )
            .group_by("bucket_id")
            .agg(pl.len().alias("samples"))
        )
        counts = {
            int(row["bucket_id"]): int(row["samples"])
            for row in count_df.to_dicts()
        }

        for bucket_id in range(1, effective_bin_count + 1):
            bucket_from = min_value + (bucket_id - 1) * width
            bucket_to = min_value + bucket_id * width
            samples = counts.get(bucket_id, 0)
            records.append(
                {
                    "series": series_key,
                    "series_label": series_label,
                    "bucket_id": bucket_id,
                    "bucket_count": effective_bin_count,
                    "value_from": _safe_float(bucket_from, 6),
                    "value_to": _safe_float(bucket_to, 6),
                    "samples": samples,
                    "pct": _safe_float(samples * 100.0 / total, 4),
                    "total_samples": int(total),
                    "mean_value": mean_value,
                    "std_value": std_value,
                }
            )

    return records


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
    rank_ic_t_stat = None
    if ic_mean is not None and ic_std is not None and ic_std > 0 and ic_df.height > 1:
        rank_ic_t_stat = round(ic_mean / ic_std * math.sqrt(ic_df.height), 4)

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
        "rank_ic_t_stat": rank_ic_t_stat,
        "top_bucket_avg_return_pct": top_return,
        "bottom_bucket_avg_return_pct": bottom_return,
        "top_minus_bottom_avg_return_pct": spread_return,
        "annualized_top_minus_bottom_avg_return_pct": annualized_spread_return,
        "elapsed_ms": round(elapsed_ms, 1),
    } | monotonicity


def _compute_sample_artifacts(
    factor_sample: pl.DataFrame,
    request: FactorLabAnalyzeRequest,
    forward_window: int,
) -> Dict[str, Any]:
    if factor_sample.is_empty():
        return {
            "factor_sample": factor_sample,
            "bucket_df": pl.DataFrame(),
            "ic_df": pl.DataFrame(),
            "non_overlapping_summary": {},
            "non_overlapping_offsets": [],
            "yearly_stability": pl.DataFrame(),
            "factor_distribution": [],
            "summary": {},
        }

    bucket_df = _compute_bucket_report(factor_sample)
    ic_df = _compute_rank_ic(factor_sample)
    summary = _summarize(bucket_df, ic_df, request, factor_sample, forward_window, 0)
    non_overlap = _compute_non_overlapping_stats(
        factor_sample,
        int(request.bucket_count),
        int(forward_window),
    )
    yearly_df = _compute_yearly_stability(
        factor_sample,
        ic_df,
        int(request.bucket_count),
        int(forward_window),
    )
    factor_distribution = _compute_factor_value_distribution(factor_sample)

    non_overlap_summary = non_overlap["summary"]
    if non_overlap_summary:
        summary.update(
            {
                "non_overlap_annualized_median_pct": non_overlap_summary.get("annualized_median_pct"),
                "non_overlap_annualized_mean_pct": non_overlap_summary.get("annualized_mean_pct"),
                "non_overlap_positive_period_rate_pct": non_overlap_summary.get("positive_period_rate_pct"),
                "non_overlap_offsets": non_overlap_summary.get("offsets"),
                "spread_t_stat": non_overlap_summary.get("mean_t_stat"),
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
        "factor_distribution": factor_distribution,
        "summary": summary,
    }


def _compute_parameter_heatmap(
    price_df: pl.DataFrame,
    universe_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    request: FactorLabAnalyzeRequest,
    context_base: FactorContext,
) -> List[Dict[str, Any]]:
    if not request.include_heatmap or price_df.is_empty() or universe_df.is_empty():
        return []

    windows = (
        request.heatmap_windows
        if factor_definition.supports_windows
        else [factor_definition.default_windows[0]]
    )
    forward_windows = request.heatmap_forward_windows
    if len(windows) * len(forward_windows) > MAX_HEATMAP_CELLS:
        forward_windows = forward_windows[: max(1, MAX_HEATMAP_CELLS // max(1, len(windows)))]

    records: List[Dict[str, Any]] = []
    raw_factor_cache: Dict[Any, pl.DataFrame] = {}
    for window in windows:
        active_windows = _active_windows_for_heatmap_row(window, factor_definition)
        heatmap_context = FactorContext(
            windows=active_windows,
            momentum_weights=_weights_for_heatmap_row(request.momentum_weights, active_windows, window),
            db=context_base.db,
            symbols=context_base.symbols,
            start_date=context_base.start_date,
            end_date=context_base.end_date,
            analysis_dates=context_base.analysis_dates,
            industry_df=context_base.industry_df,
            candidate_etfs=context_base.candidate_etfs,
            weight_history_loader=load_universe_weight_history,
        )
        factor_df = _prepare_cached_leg_factor_frame(
            price_df,
            factor_definition,
            heatmap_context,
            request,
            raw_factor_cache,
        )
        for forward_window in forward_windows:
            sample = _assign_buckets(
                _prepare_factor_sample(factor_df, universe_df, request, forward_window),
                request.bucket_count,
            )
            split_sample = _split_sample_for_oos(
                sample,
                context_base.analysis_dates,
                request.oos_start_date,
                int(forward_window),
            )
            analysis_sample = split_sample["analysis"]
            bucket_df = _compute_bucket_report(analysis_sample)
            ic_df = _compute_rank_ic(analysis_sample)
            summary = _summarize(bucket_df, ic_df, request, analysis_sample, int(forward_window), 0)
            non_overlap_summary = {}
            if not analysis_sample.is_empty():
                non_overlap_summary = _compute_non_overlapping_stats(
                    analysis_sample,
                    int(request.bucket_count),
                    int(forward_window),
                    include_offsets=False,
                )["summary"]

            non_overlap_annualized_value = non_overlap_summary.get("annualized_median_pct")
            record = {
                "window": window,
                "window_label": _window_label(window),
                "windows": active_windows,
                "forward_window": int(forward_window),
                "top_minus_bottom_avg_return_pct": summary.get("top_minus_bottom_avg_return_pct"),
                "annualized_top_minus_bottom_avg_return_pct": summary.get("annualized_top_minus_bottom_avg_return_pct"),
                "non_overlap_annualized_top_minus_bottom_pct": non_overlap_annualized_value,
                "non_overlap_annualized_median_pct": non_overlap_annualized_value,
                "non_overlap_annualized_mean_pct": non_overlap_summary.get("annualized_mean_pct"),
                "non_overlap_mean_t_stat": non_overlap_summary.get("mean_t_stat"),
                "rank_ic_mean": summary.get("rank_ic_mean"),
                "rank_ic_std": summary.get("rank_ic_std"),
                "rank_ic_t_stat": summary.get("rank_ic_t_stat"),
                "icir": summary.get("icir"),
                "monotonicity_spearman": summary.get("monotonicity_spearman"),
                "adjacent_hit_rate_pct": summary.get("adjacent_hit_rate_pct"),
                "top_bucket_avg_return_pct": summary.get("top_bucket_avg_return_pct"),
                "bottom_bucket_avg_return_pct": summary.get("bottom_bucket_avg_return_pct"),
                "samples": int(analysis_sample.height),
                "trade_dates": int(analysis_sample.select(pl.n_unique("trade_date")).item()) if not analysis_sample.is_empty() else 0,
                "full_samples": int(sample.height),
                "oos_samples": int(split_sample["oos"].height),
            }
            metric_value = record.get(request.heatmap_metric)
            record["heatmap_value"] = metric_value
            record["heatmap_value_pct"] = metric_value
            records.append(
                record
            )
    return records


def _select_best_combo(
    request: FactorLabAnalyzeRequest,
    factor_definition: FactorDefinition,
    heatmap_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    metric_key = request.heatmap_metric
    valid_records = [
        item for item in heatmap_records
        if item.get(metric_key) is not None
    ]
    selected_record = max(
        valid_records,
        key=lambda item: _record_float(item, metric_key, -1e18),
    ) if valid_records else None

    selected_window = request.heatmap_windows[0]
    selected_forward_window = request.heatmap_forward_windows[0]
    if selected_record:
        selected_window = selected_record["window"]
        selected_forward_window = int(selected_record["forward_window"])

    selected_windows = _active_windows_for_heatmap_row(selected_window, factor_definition)
    return {
        "window": selected_window,
        "window_label": _window_label(selected_window),
        "forward_window": int(selected_forward_window),
        "windows": selected_windows,
        "selection_mode": "best",
        "heatmap_metric": metric_key,
        "heatmap_metric_label": HEATMAP_METRIC_OPTIONS.get(metric_key, {}).get("label", metric_key),
        "reason": f"max_{metric_key}" if selected_record else "fallback_first_combo",
    }


def _compute_factor_analysis_for_combo(
    price_df: pl.DataFrame,
    universe_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    request: FactorLabAnalyzeRequest,
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
    combo: Dict[str, Any],
    momentum_weights: Dict[str, float],
    analysis_dates: List[date],
    industry_df: Optional[pl.DataFrame],
    candidate_etfs: List[str],
) -> Dict[str, Any]:
    forward_window = int(combo["forward_window"])
    active_windows = (
        _active_windows_for_heatmap_row(combo["window"], factor_definition)
        if factor_definition.supports_windows
        else factor_definition.default_windows
    )
    context = FactorContext(
        windows=active_windows,
        momentum_weights=_weights_for_heatmap_row(momentum_weights, active_windows, combo["window"]),
        db=db,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        analysis_dates=analysis_dates,
        industry_df=industry_df,
        candidate_etfs=candidate_etfs,
        weight_history_loader=load_universe_weight_history,
    )
    factor_df = _prepare_cached_leg_factor_frame(
        price_df,
        factor_definition,
        context,
        request,
        {},
    )
    full_sample = _assign_buckets(
        _prepare_factor_sample(factor_df, universe_df, request, forward_window),
        int(request.bucket_count),
    )
    split_sample = _split_sample_for_oos(
        full_sample,
        analysis_dates,
        request.oos_start_date,
        forward_window,
    )
    analysis_sample = split_sample["analysis"]
    if analysis_sample.is_empty():
        detail = (
            "样本外切分后样本内训练样本不足，请调早样本外起始日期或缩短收益窗口"
            if request.oos_start_date
            else "没有可用于分桶的因子样本，请调整日期、窗口或股票池"
        )
        raise HTTPException(status_code=400, detail=detail)

    analysis_result = _compute_sample_artifacts(analysis_sample, request, forward_window)
    oos_result = _compute_sample_artifacts(split_sample["oos"], request, forward_window)

    return {
        **analysis_result,
        "full_factor_sample": full_sample,
        "oos_factor_sample": split_sample["oos"],
        "oos_summary": oos_result["summary"],
        "oos_bucket_df": oos_result["bucket_df"],
        "oos_ic_df": oos_result["ic_df"],
        "oos_non_overlapping_summary": oos_result["non_overlapping_summary"],
        "oos_non_overlapping_offsets": oos_result["non_overlapping_offsets"],
        "oos_yearly_stability": oos_result["yearly_stability"],
        "oos_factor_distribution": oos_result["factor_distribution"],
    }


def _resolve_composite_leg(
    leg: CompositeFactorLeg,
    index: int,
) -> Dict[str, Any]:
    factor_definition = FACTOR_REGISTRY.get(leg.factor)
    if not factor_definition:
        raise HTTPException(status_code=400, detail=f"未注册的组合子因子: {leg.factor}")
    if leg.window == MIXED_WINDOW_KEY and not factor_definition.supports_mixed_windows:
        raise HTTPException(status_code=400, detail=f"{factor_definition.label} 不支持多窗口合成")

    active_windows = (
        _active_windows_for_heatmap_row(leg.window, factor_definition)
        if factor_definition.supports_windows
        else factor_definition.default_windows
    )
    return {
        "index": index,
        "key": f"component_{index + 1}",
        "column": f"_component_{index + 1}",
        "factor_definition": factor_definition,
        "factor": factor_definition.to_option(),
        "window": leg.window,
        "window_label": _window_label(leg.window) if factor_definition.supports_windows else "固定",
        "windows": active_windows,
        "raw_weight": float(leg.weight),
        "neutralization": leg.neutralization,
        "neutralization_label": NEUTRALIZATION_OPTIONS[leg.neutralization]["label"],
        "standardization": leg.standardization,
        "standardization_label": STANDARDIZATION_OPTIONS[leg.standardization]["label"],
        "momentum_weights": leg.momentum_weights,
    }


def _resolve_factor_legs(legs: List[CompositeFactorLeg]) -> List[Dict[str, Any]]:
    resolved = [_resolve_composite_leg(leg, index) for index, leg in enumerate(legs)]
    total_abs_weight = sum(abs(item["raw_weight"]) for item in resolved)
    if total_abs_weight <= 0:
        raise HTTPException(status_code=400, detail="至少设置一个非0因子权重")
    return [
        {
            **item,
            "weight": item["raw_weight"] / total_abs_weight,
        }
        for item in resolved
    ]


def _resolve_composite_legs(request: CompositeFactorAnalyzeRequest) -> List[Dict[str, Any]]:
    return _resolve_factor_legs(request.legs)


def _required_windows_for_composite_legs(legs: List[Dict[str, Any]]) -> List[int]:
    required: List[int] = []
    for leg in legs:
        required.extend(int(item) for item in leg["windows"])
    return list(dict.fromkeys(required)) or SUPPORTED_WINDOWS.copy()


def _leg_momentum_weights(leg: Dict[str, Any]) -> Dict[int, float]:
    return _weights_for_heatmap_row(
        leg["momentum_weights"],
        leg["windows"],
        leg["window"],
    )


def _backtest_leg_factor_cache_key(leg: Dict[str, Any]) -> Any:
    momentum_weights = _leg_momentum_weights(leg)
    return (
        leg["factor"]["key"],
        str(leg["window"]),
        tuple(int(item) for item in leg["windows"]),
        leg["neutralization"],
        leg["standardization"],
        tuple((int(window), round(float(weight), 10)) for window, weight in sorted(momentum_weights.items())),
    )


def _prepare_cached_leg_factor_frame(
    price_df: pl.DataFrame,
    factor_definition: FactorDefinition,
    leg_context: FactorContext,
    leg_request: FactorLabAnalyzeRequest,
    raw_factor_cache: Optional[Dict[Any, pl.DataFrame]],
) -> pl.DataFrame:
    if factor_definition.key in MOMENTUM_FACTOR_SCORE_PREFIX:
        source_df = _momentum_score_source_frame(
            price_df,
            factor_definition.key,
            leg_context.windows,
            raw_factor_cache,
        )
        return _prepare_momentum_factor_frame_from_source(
            source_df,
            factor_definition,
            leg_context,
            leg_request,
        )

    return _prepare_factor_frame(price_df, factor_definition, leg_context, leg_request)


def _prepare_composite_factor_frame(
    price_df: pl.DataFrame,
    request: CompositeFactorAnalyzeRequest,
    db: ORMSession,
    symbols: List[str],
    start_date: date,
    end_date: date,
    analysis_dates: List[date],
    industry_df: Optional[pl.DataFrame],
    resolved_legs: List[Dict[str, Any]],
    candidate_etfs: List[str],
    valuation_df: Optional[pl.DataFrame] = None,
    weight_history: Optional[Dict[str, Dict[date, Dict[str, float]]]] = None,
    raw_factor_cache: Optional[Dict[Any, pl.DataFrame]] = None,
    component_factor_cache: Optional[Dict[Any, pl.DataFrame]] = None,
) -> pl.DataFrame:
    if price_df.is_empty():
        return price_df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    active_legs = [
        leg
        for leg in resolved_legs
        if abs(float(leg.get("weight") or 0)) > 1e-12
    ]
    base_columns = [
        column
        for column in ["symbol", "trade_date", "open", "high", "low", "close", "volume", "turnover", "_first_trade_date"]
        if column in price_df.columns
    ]
    composite_df = price_df.select(base_columns).unique(subset=["symbol", "trade_date"])

    for leg in active_legs:
        factor_definition = leg["factor_definition"]
        momentum_weights = _leg_momentum_weights(leg)
        leg_context = FactorContext(
            windows=leg["windows"],
            momentum_weights=momentum_weights,
            db=db,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            analysis_dates=analysis_dates,
            industry_df=industry_df,
            candidate_etfs=candidate_etfs,
            valuation_df=valuation_df,
            weight_history=weight_history,
            weight_history_loader=load_universe_weight_history,
        )
        leg_request_payload = _request_to_dict(request)
        leg_request_payload.update(
            {
                "neutralization": leg["neutralization"],
                "standardization": leg["standardization"],
            }
        )
        leg_request = SimpleNamespace(**leg_request_payload)
        leg_cache_key = _backtest_leg_factor_cache_key(leg)
        cache_component = leg["window"] != MIXED_WINDOW_KEY
        factor_df = (
            component_factor_cache.get(leg_cache_key)
            if cache_component and component_factor_cache is not None
            else None
        )
        if factor_df is None:
            factor_df = _prepare_cached_leg_factor_frame(
                price_df,
                factor_definition,
                leg_context,
                leg_request,
                raw_factor_cache,
            ).select("symbol", "trade_date", "factor_value")
            if cache_component and component_factor_cache is not None:
                _put_limited_cache(
                    component_factor_cache,
                    leg_cache_key,
                    factor_df,
                    BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT,
                )
        composite_df = composite_df.join(
            factor_df.select(
                "symbol",
                "trade_date",
                pl.col("factor_value").alias(leg["column"]),
            ),
            on=["symbol", "trade_date"],
            how="left",
        )

    composite_expr = None
    for leg in active_legs:
        if leg["column"] not in composite_df.columns:
            continue
        expr = pl.col(leg["column"]) * float(leg["weight"])
        composite_expr = expr if composite_expr is None else composite_expr + expr

    if composite_expr is None:
        return composite_df.with_columns(pl.lit(None, dtype=pl.Float64).alias("factor_value"))

    return composite_df.with_columns(
        composite_expr.alias("factor_value")
    ).with_columns(
        pl.col("factor_value").alias("factor_value_raw")
    )


def _compute_component_ic_summary(
    analysis_sample: pl.DataFrame,
    resolved_legs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if analysis_sample.is_empty():
        return []

    records: List[Dict[str, Any]] = []
    for leg in resolved_legs:
        column = leg["column"]
        if column not in analysis_sample.columns:
            continue
        leg_sample = (
            analysis_sample
            .filter(pl.col(column).is_not_null() & pl.col(column).is_finite())
            .with_columns(pl.col(column).alias("factor_value"))
        )
        ic_df = _compute_rank_ic(leg_sample)
        ic_mean = _safe_float(ic_df.select(pl.mean("rank_ic")).item(), 6) if not ic_df.is_empty() else None
        ic_std = _safe_float(ic_df.select(pl.std("rank_ic")).item(), 6) if not ic_df.is_empty() and ic_df.height > 1 else None
        icir = round(ic_mean / ic_std * math.sqrt(TRADING_DAYS_PER_YEAR), 4) if ic_mean is not None and ic_std and ic_std > 0 else None
        t_stat = round(ic_mean / ic_std * math.sqrt(ic_df.height), 4) if ic_mean is not None and ic_std and ic_std > 0 and ic_df.height > 1 else None
        records.append(
            {
                "component_key": leg["key"],
                "component_label": leg["factor"]["label"],
                "factor": leg["factor"]["key"],
                "window": leg["window"],
                "window_label": leg["window_label"],
                "raw_weight": _safe_float(leg["raw_weight"], 4),
                "weight": _safe_float(leg["weight"], 4),
                "neutralization": leg["neutralization"],
                "neutralization_label": leg["neutralization_label"],
                "standardization": leg["standardization"],
                "standardization_label": leg["standardization_label"],
                "samples": int(leg_sample.height),
                "trade_dates": int(leg_sample.select(pl.n_unique("trade_date")).item()) if not leg_sample.is_empty() else 0,
                "rank_ic_mean": ic_mean,
                "rank_ic_std": ic_std,
                "icir": icir,
                "rank_ic_t_stat": t_stat,
            }
        )
    return records


def _compute_component_correlation(
    analysis_sample: pl.DataFrame,
    resolved_legs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if analysis_sample.is_empty() or len(resolved_legs) < 2:
        return []

    rows: List[Dict[str, Any]] = []
    for left in resolved_legs:
        row = {
            "component_key": left["key"],
            "component_label": left["factor"]["label"],
        }
        for right in resolved_legs:
            left_col = left["column"]
            right_col = right["column"]
            if left_col not in analysis_sample.columns or right_col not in analysis_sample.columns:
                corr = None
            elif left_col == right_col:
                corr = 1.0
            else:
                try:
                    corr = analysis_sample.select(pl.corr(left_col, right_col).alias("corr")).item()
                except Exception:
                    corr = None
            row[right["key"]] = _safe_float(corr, 4)
        rows.append(row)
    return rows


def _to_shared_backtest_config(request: FactorBacktestRequest) -> SharedFactorBacktestConfig:
    pool_key = str(request.pool or "").strip().upper()
    return SharedFactorBacktestConfig(
        pool=pool_key,
        pool_label=CUSTOM_POOL_LABELS.get(
            pool_key,
            next((item["label"] for item in POOL_OPTIONS if item["key"] == pool_key), pool_key),
        ),
        candidate_etfs=POOL_ETFS.get(pool_key, []),
        custom_symbols=list(request.custom_symbols or []),
        position_weights=list(request.position_weights or []),
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        max_positions=request.max_positions,
        sell_rank_multiplier=request.sell_rank_multiplier,
        rebalance_frequency=request.rebalance_frequency,
        rotation_mode=request.rotation_mode,
        commission_pct=request.commission_pct,
        slippage_pct=request.slippage_pct,
        lot_size=request.lot_size,
        min_listing_days=request.min_listing_days,
        legs=[
            SharedFactorBacktestLeg(
                factor=leg.factor,
                window=leg.window,
                weight=leg.weight,
                neutralization=leg.neutralization,
                standardization=leg.standardization,
                momentum_weights=leg.momentum_weights,
            )
            for leg in request.legs
        ],
        mode="factor_backtest",
        strategy="factor_lab_top_n_rotation",
    )


def _prepare_factor_backtest_base_data(
    request: FactorBacktestRequest,
    db: ORMSession,
    resolved_legs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return shared_prepare_factor_backtest_base_data(
        _to_shared_backtest_config(request),
        db,
    )


def _warm_backtest_search_factor_caches(
    search_request: FactorBacktestSearchRequest,
    prepared_data: Dict[str, Any],
    db: ORMSession,
):
    return shared_warm_backtest_search_factor_caches(
        _to_shared_backtest_config(search_request.request),
        prepared_data,
        db,
    )


def _iso_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _floor_to_lot(quantity: float, lot_size: int) -> int:
    lot = max(1, int(lot_size or 1))
    number = int(max(0.0, float(quantity or 0)))
    return (number // lot) * lot


def _sanitize_live_backtest_request_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    request_payload = dict(payload or {})
    request_payload["end_date"] = None
    request_payload["oos_start_date"] = None
    return request_payload


def _live_backtest_request_payload(request: FactorBacktestRequest) -> Dict[str, Any]:
    return _sanitize_live_backtest_request_payload(_backtest_request_payload(request))


def _request_payload_from_live_config(config: FactorLiveTradingConfig) -> FactorBacktestRequest:
    return FactorBacktestRequest(**_sanitize_live_backtest_request_payload(config.request_payload))


def _factor_pool_label(pool: Any) -> str:
    pool_key = str(pool or "").strip().upper()
    return CUSTOM_POOL_LABELS.get(
        pool_key,
        next((item["label"] for item in POOL_OPTIONS if item["key"] == pool_key), pool_key),
    )


def _signal_payload_with_symbol_names(payload: Optional[Dict[str, Any]], db: Optional[ORMSession]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return payload
    if db is None and payload.get("symbol_names"):
        return payload
    symbols = set()
    for key in ("current_holdings", "selected_symbols", "sell_rank_symbols", "sell_symbols", "target_symbols", "buy_symbols"):
        symbols.update(str(item or "").strip().upper() for item in (payload.get(key) or []) if item)
    for item in payload.get("ranked") or []:
        if isinstance(item, dict) and item.get("symbol"):
            symbols.add(str(item.get("symbol")).strip().upper())
    if not symbols:
        return payload
    symbol_names = {
        **load_symbol_name_map(symbols, db),
        **(payload.get("symbol_names") or {}),
    }
    next_payload = dict(payload)
    next_payload["symbol_names"] = symbol_names
    ranked = []
    for item in payload.get("ranked") or []:
        if not isinstance(item, dict):
            ranked.append(item)
            continue
        row = dict(item)
        symbol = normalize_symbol_for_name(row.get("symbol"))
        row["symbol_name"] = symbol_names.get(symbol)
        ranked.append(row)
    if ranked:
        next_payload["ranked"] = ranked
    return next_payload


def _serialize_factor_live_config(
    config: FactorLiveTradingConfig,
    db: Optional[ORMSession] = None,
    external_db: Optional[ORMSession] = None,
) -> Dict[str, Any]:
    request_payload = _sanitize_live_backtest_request_payload(config.request_payload)
    position_weights = normalize_position_weights(
        request_payload.get("position_weights"),
        request_payload.get("max_positions") or 1,
    )
    custom_symbols = [str(item or "").strip().upper() for item in (request_payload.get("custom_symbols") or []) if item]
    custom_symbol_names = load_symbol_name_map(custom_symbols, db) if custom_symbols else {}
    last_signal_payload = _signal_payload_with_symbol_names(config.last_signal_payload, db)
    external_account_name = None
    external_account_identifier = None
    live_sub_account_name = None
    if external_db is not None:
        if config.external_trading_account_id:
            external_account = external_db.query(ExternalTradingAccount).filter(
                ExternalTradingAccount.id == config.external_trading_account_id,
                ExternalTradingAccount.account_id == config.account_id,
            ).first()
            if external_account:
                external_account_name = external_account.name
                external_account_identifier = external_account.identifier
        if config.live_sub_account_id:
            live_sub_account = external_db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == config.live_sub_account_id,
                ExternalTradingSubAccount.account_id == config.account_id,
            ).first()
            if live_sub_account:
                live_sub_account_name = live_sub_account.name
    return {
        "id": config.id,
        "account_id": config.account_id,
        "name": config.name,
        "enabled": bool(config.enabled),
        "request": request_payload,
        "request_summary": {
            "pool": request_payload.get("pool"),
            "pool_label": _factor_pool_label(request_payload.get("pool")),
            "custom_symbols": custom_symbols,
            "custom_symbol_names": custom_symbol_names,
            "custom_symbol_count": len(request_payload.get("custom_symbols") or []),
            "max_positions": len(position_weights),
            "position_weights_label": format_position_weights(position_weights),
            "rebalance_frequency": request_payload.get("rebalance_frequency"),
            "rotation_mode": request_payload.get("rotation_mode"),
            "rotation_mode_label": ROTATION_MODE_LABELS.get(request_payload.get("rotation_mode"), request_payload.get("rotation_mode")),
            "lot_size": request_payload.get("lot_size"),
        },
        "external_trading_account_id": config.external_trading_account_id,
        "external_trading_account_name": external_account_name,
        "external_trading_account_identifier": external_account_identifier,
        "external_trading_account_label": (
            f"{external_account_name}（{external_account_identifier}）"
            if external_account_name and external_account_identifier
            else external_account_name
        ),
        "live_sub_account_id": config.live_sub_account_id,
        "live_sub_account_name": live_sub_account_name,
        "signal_time": config.signal_time,
        "signal_timezone": config.signal_timezone,
        "execution_time": config.execution_time,
        "execution_timezone": config.execution_timezone,
        "last_signal_date": _iso_value(config.last_signal_date),
        "last_signal_at": _iso_value(config.last_signal_at),
        "last_signal_status": config.last_signal_status,
        "last_signal_message": config.last_signal_message,
        "last_signal_payload": last_signal_payload,
        "last_execution_signal_date": _iso_value(config.last_execution_signal_date),
        "last_execution_at": _iso_value(config.last_execution_at),
        "last_execution_status": config.last_execution_status,
        "last_execution_message": config.last_execution_message,
        "last_execution_payload": config.last_execution_payload,
        "created_at": _iso_value(config.created_at),
        "updated_at": _iso_value(config.updated_at),
    }


def _serialize_factor_live_log(row: FactorLiveTradingLog) -> Dict[str, Any]:
    return {
        "id": row.id,
        "config_id": row.config_id,
        "account_id": row.account_id,
        "timestamp": _iso_value(row.timestamp),
        "action": row.action,
        "status": row.status,
        "signal_date": _iso_value(row.signal_date),
        "message": row.message,
        "payload": row.payload,
    }


def _add_factor_live_log(
    db: ORMSession,
    config: FactorLiveTradingConfig,
    *,
    action: str,
    status: str,
    message: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    signal_date: Optional[date] = None,
) -> None:
    db.add(
        FactorLiveTradingLog(
            config_id=config.id,
            account_id=config.account_id,
            action=action,
            status=status,
            message=(message or "")[:1000],
            payload=jsonable_encoder(payload or {}),
            signal_date=signal_date,
            timestamp=datetime.now(),
        )
    )


def _factor_live_error_message(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail or exc or exc.__class__.__name__)


def _factor_live_config_snapshot(config: FactorLiveTradingConfig) -> SimpleNamespace:
    return SimpleNamespace(
        id=config.id,
        account_id=config.account_id,
        name=config.name,
        enabled=config.enabled,
        request_payload=config.request_payload,
        external_trading_account_id=config.external_trading_account_id,
        live_sub_account_id=config.live_sub_account_id,
        signal_time=config.signal_time,
        signal_timezone=config.signal_timezone,
        execution_time=config.execution_time,
        execution_timezone=config.execution_timezone,
        last_signal_date=config.last_signal_date,
        last_signal_at=config.last_signal_at,
        last_signal_status=config.last_signal_status,
        last_signal_message=config.last_signal_message,
        last_signal_payload=config.last_signal_payload,
        last_execution_signal_date=config.last_execution_signal_date,
        last_execution_at=config.last_execution_at,
        last_execution_status=config.last_execution_status,
        last_execution_message=config.last_execution_message,
        last_execution_payload=config.last_execution_payload,
    )


def _load_enabled_factor_live_config_snapshots() -> List[SimpleNamespace]:
    with get_db_ctx() as db:
        configs = (
            db.query(FactorLiveTradingConfig)
            .filter(FactorLiveTradingConfig.enabled == True)  # noqa: E712
            .order_by(FactorLiveTradingConfig.id.asc())
            .all()
        )
        return [_factor_live_config_snapshot(config) for config in configs]


def _apply_factor_live_snapshot_updates(config: SimpleNamespace, updates: Dict[str, Any]) -> None:
    for key, value in updates.items():
        setattr(config, key, value)


def _persist_factor_live_signal_success(config_id: int, plan: Dict[str, Any]) -> Dict[str, Any]:
    signal_date_value = date.fromisoformat(plan["signal_date"])
    payload = jsonable_encoder(plan)
    now = datetime.now()
    with get_db_ctx() as db:
        config = db.query(FactorLiveTradingConfig).filter(FactorLiveTradingConfig.id == config_id).first()
        if not config:
            raise ValueError("因子线上交易配置不存在")
        config.last_signal_date = signal_date_value
        config.last_signal_at = now
        config.last_signal_status = plan.get("status")
        config.last_signal_message = plan.get("message")
        config.last_signal_payload = payload
        config.updated_at = now
        _add_factor_live_log(
            db,
            config,
            action="SIGNAL",
            status=plan.get("status") or "OK",
            message=plan.get("message"),
            payload=plan,
            signal_date=signal_date_value,
        )
    return {
        "last_signal_date": signal_date_value,
        "last_signal_at": now,
        "last_signal_status": plan.get("status"),
        "last_signal_message": plan.get("message"),
        "last_signal_payload": payload,
    }


def _persist_factor_live_signal_failure(config_id: int, error_message: str) -> Dict[str, Any]:
    now = datetime.now()
    with get_db_ctx() as db:
        config = db.query(FactorLiveTradingConfig).filter(FactorLiveTradingConfig.id == config_id).first()
        if not config:
            logger.warning("Factor live config not found while persisting signal failure: %s", config_id)
            return {}
        config.last_signal_at = now
        config.last_signal_status = "FAILED"
        config.last_signal_message = error_message[:1000]
        config.updated_at = now
        _add_factor_live_log(
            db,
            config,
            action="SIGNAL",
            status="FAILED",
            message=error_message,
            payload={},
        )
    return {
        "last_signal_at": now,
        "last_signal_status": "FAILED",
        "last_signal_message": error_message[:1000],
    }


def _persist_factor_live_execution_result(
    config_id: int,
    execution_result: Dict[str, Any],
    execution_status: str,
) -> Dict[str, Any]:
    signal_date_value = _normalize_signal_payload_date(execution_result.get("signal") or {})
    payload = jsonable_encoder(execution_result)
    now = datetime.now()
    with get_db_ctx() as db:
        config = db.query(FactorLiveTradingConfig).filter(FactorLiveTradingConfig.id == config_id).first()
        if not config:
            raise ValueError("因子线上交易配置不存在")
        config.last_execution_signal_date = signal_date_value
        config.last_execution_at = now
        config.last_execution_status = execution_status
        config.last_execution_message = execution_result.get("message")
        config.last_execution_payload = payload
        config.updated_at = now
        _add_factor_live_log(
            db,
            config,
            action="EXECUTE",
            status=execution_status,
            message=execution_result.get("message"),
            payload=execution_result,
            signal_date=signal_date_value,
        )
    return {
        "last_execution_signal_date": signal_date_value,
        "last_execution_at": now,
        "last_execution_status": execution_status,
        "last_execution_message": execution_result.get("message"),
        "last_execution_payload": payload,
    }


def _persist_factor_live_execution_failure(
    config_id: int,
    error_message: str,
    signal_date_value: Optional[date],
) -> Dict[str, Any]:
    now = datetime.now()
    with get_db_ctx() as db:
        config = db.query(FactorLiveTradingConfig).filter(FactorLiveTradingConfig.id == config_id).first()
        if not config:
            logger.warning("Factor live config not found while persisting execution failure: %s", config_id)
            return {}
        config.last_execution_at = now
        config.last_execution_status = "FAILED"
        config.last_execution_message = error_message[:1000]
        config.updated_at = now
        _add_factor_live_log(
            db,
            config,
            action="EXECUTE",
            status="FAILED",
            message=error_message,
            payload={"signal_date": signal_date_value.isoformat() if signal_date_value else None},
            signal_date=signal_date_value,
        )
    return {
        "last_execution_at": now,
        "last_execution_status": "FAILED",
        "last_execution_message": error_message[:1000],
    }


def _send_factor_live_automation_alert(
    config: FactorLiveTradingConfig,
    *,
    action: str,
    status: str,
    message: str,
    signal_date: Optional[date] = None,
    payload: Optional[Dict[str, Any]] = None,
    exc: Optional[Exception] = None,
) -> None:
    action_label = "信号生成" if action == "SIGNAL" else "执行"
    signal_date_text = signal_date.isoformat() if signal_date else "-"
    dedupe_message = str(message or "")[:300]
    alert_key = f"{config.id}:{action}:{signal_date_text}:{status}:{dedupe_message}"
    now_ts = time.time()
    with _factor_live_alert_lock:
        last_sent = _factor_live_alert_last_sent.get(alert_key)
        if last_sent and now_ts - last_sent < FACTOR_LIVE_ALERT_COOLDOWN_SECONDS:
            return
        _factor_live_alert_last_sent[alert_key] = now_ts

    subject = f"因子线上交易自动{action_label}失败: {config.name or config.id}"
    body_parts = [
        f"配置: {config.name or '-'} (ID: {config.id})",
        f"账号: {config.account_id}",
        f"动作: {action}",
        f"状态: {status}",
        f"信号日: {signal_date_text}",
        f"错误: {message}",
        f"时间: {datetime.now().isoformat()}",
    ]
    if payload:
        try:
            payload_text = json.dumps(jsonable_encoder(payload), ensure_ascii=False, indent=2, default=str)
        except Exception:
            payload_text = str(payload)
        body_parts.extend(["", "Payload:", payload_text])
    if exc is not None:
        body_parts.extend(["", "Traceback:", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))])
    send_alert_email(
        subject,
        "\n".join(body_parts),
        scenario_key="factor_live_trading_error",
    )


def _factor_live_executor_failure_payload(execution_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    executor_result = (execution_result or {}).get("executor_result") or {}
    status = str(executor_result.get("status") or "").upper()
    try:
        failed_count = int(executor_result.get("failed") or 0)
    except (TypeError, ValueError):
        failed_count = 0
    failed_accounts = [
        item
        for item in (executor_result.get("accounts") or [])
        if str((item or {}).get("status") or "").upper() in {"FAILED", "PARTIAL_FAILED"}
        or (item or {}).get("error")
    ]
    if status in {"FAILED", "PARTIAL_FAILED"} or failed_count > 0 or failed_accounts:
        return {
            "status": status or "FAILED",
            "failed": failed_count,
            "failed_accounts": failed_accounts[:5],
        }
    return None


def _get_factor_live_config_or_404(
    db: ORMSession,
    account_id: str,
    config_id: int,
) -> FactorLiveTradingConfig:
    config = db.query(FactorLiveTradingConfig).filter(
        FactorLiveTradingConfig.id == config_id,
        FactorLiveTradingConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="未找到因子线上交易配置")
    return config


def _get_external_sub_account_for_live_config(
    external_db: ORMSession,
    config: FactorLiveTradingConfig,
) -> ExternalTradingSubAccount:
    if not config.external_trading_account_id or not config.live_sub_account_id:
        raise HTTPException(status_code=400, detail="请先选择外部交易账户和子账户")
    account = external_db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == config.external_trading_account_id,
        ExternalTradingAccount.account_id == config.account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="外部交易账户不存在")
    sub_account = external_db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == config.live_sub_account_id,
        ExternalTradingSubAccount.account_id == config.account_id,
        ExternalTradingSubAccount.external_trading_account_id == account.id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=404, detail="外部交易子账户不存在")
    if not account.enabled or not sub_account.enabled:
        raise HTTPException(status_code=400, detail="外部交易账户或子账户未启用")
    return sub_account


def _bind_factor_live_sub_account(
    external_db: ORMSession,
    config: FactorLiveTradingConfig,
    *,
    previous_sub_account_id: Optional[int] = None,
) -> None:
    if previous_sub_account_id and previous_sub_account_id != config.live_sub_account_id:
        previous = external_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == previous_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
        ).first()
        if (
            previous
            and previous.strategy_type == STRATEGY_FACTOR_LIVE
            and previous.strategy_config_id == config.id
        ):
            previous.strategy_type = None
            previous.strategy_config_id = None
            previous.updated_at = datetime.now()

    if not config.external_trading_account_id or not config.live_sub_account_id:
        return
    sub_account = external_db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == config.live_sub_account_id,
        ExternalTradingSubAccount.account_id == config.account_id,
        ExternalTradingSubAccount.external_trading_account_id == config.external_trading_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=404, detail="外部交易子账户不存在")
    if not sub_account.enabled:
        raise HTTPException(status_code=400, detail="该外部交易子账户未启用")
    if (
        sub_account.strategy_type
        and not (
            sub_account.strategy_type == STRATEGY_FACTOR_LIVE
            and sub_account.strategy_config_id == config.id
        )
    ):
        raise HTTPException(status_code=400, detail="该外部交易子账户已绑定其他策略")
    sub_account.strategy_type = STRATEGY_FACTOR_LIVE
    sub_account.strategy_config_id = config.id
    sub_account.updated_at = datetime.now()


def _current_holding_symbols(external_db: ORMSession, sub_account: ExternalTradingSubAccount) -> List[str]:
    positions = get_ledger_positions(external_db, sub_account.id)
    return [
        symbol
        for symbol, row in positions.items()
        if external_safe_int(getattr(row, "quantity", 0)) > 0
    ]


def _update_factor_live_config_from_payload(
    config: FactorLiveTradingConfig,
    payload: FactorLiveTradingConfigPayload,
) -> None:
    config.name = payload.name
    config.enabled = payload.enabled
    config.request_payload = _live_backtest_request_payload(payload.request)
    config.external_trading_account_id = payload.external_trading_account_id
    config.live_sub_account_id = payload.live_sub_account_id
    config.signal_time = payload.signal_time
    config.signal_timezone = payload.signal_timezone
    config.execution_time = payload.execution_time
    config.execution_timezone = payload.execution_timezone
    config.updated_at = datetime.now()


def _build_factor_live_signal_plan(
    db: ORMSession,
    external_db: ORMSession,
    config: FactorLiveTradingConfig,
    *,
    signal_date: Optional[date] = None,
    rank_limit: int = 100,
) -> Dict[str, Any]:
    request = _request_payload_from_live_config(config)
    sub_account = _get_external_sub_account_for_live_config(external_db, config)
    holding_symbols = _current_holding_symbols(external_db, sub_account)
    next_trading_day_resolver = _factor_live_next_trading_day_resolver(config)
    plan = shared_build_factor_signal_plan(
        _to_shared_backtest_config(request),
        db,
        holding_symbols=holding_symbols,
        signal_date=signal_date,
        rank_limit=rank_limit,
        next_trading_day_resolver=next_trading_day_resolver,
    )
    plan["external_trading_account_id"] = config.external_trading_account_id
    plan["live_sub_account_id"] = config.live_sub_account_id
    plan["live_sub_account_name"] = sub_account.name
    return plan


def _normalize_signal_payload_date(payload: Dict[str, Any]) -> Optional[date]:
    value = (payload or {}).get("signal_date")
    if not value:
        return None
    return date.fromisoformat(str(value))


def _cash_fill_weights_from_signal(signal_payload: Dict[str, Any], symbols: List[str]) -> Dict[str, float]:
    configured = signal_payload.get("buy_weights") or {}
    result = {}
    for symbol in symbols:
        value = _safe_float(configured.get(symbol))
        if value is not None and value > 0:
            result[symbol] = value
    total = sum(result.values())
    if total > 0:
        return {symbol: weight / total for symbol, weight in result.items()}
    if not symbols:
        return {}
    return {symbol: 1.0 / len(symbols) for symbol in symbols}


async def _execute_factor_live_signal(
    db: Optional[ORMSession],
    external_db: ORMSession,
    config: FactorLiveTradingConfig,
    *,
    signal_date: Optional[date] = None,
    trigger_executor: bool = True,
    force: bool = True,
    timeout_seconds: float = 120.0,
) -> Dict[str, Any]:
    request = _request_payload_from_live_config(config)
    signal_payload = config.last_signal_payload or {}
    actual_signal_date = _normalize_signal_payload_date(signal_payload)
    if signal_date and actual_signal_date and signal_date != actual_signal_date:
        raise HTTPException(status_code=400, detail="指定信号日与最近信号不一致，请先重新生成信号")
    if not actual_signal_date:
        raise HTTPException(status_code=400, detail="没有可执行的信号，请先生成信号")
    if not signal_payload.get("should_rebalance"):
        return {
            "status": "SKIPPED",
            "message": signal_payload.get("message") or "最近信号无需调仓",
            "signal": signal_payload,
            "targets": [],
            "executor_result": None,
        }

    account = external_db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == config.external_trading_account_id,
        ExternalTradingAccount.account_id == config.account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="外部交易账户不存在")
    sub_account = _get_external_sub_account_for_live_config(external_db, config)
    positions = get_ledger_positions(external_db, sub_account.id)
    current_quantities = {
        normalize_external_symbol(symbol): external_safe_int(getattr(row, "quantity", 0))
        for symbol, row in positions.items()
        if external_safe_int(getattr(row, "quantity", 0)) > 0
    }
    sell_symbols = [normalize_external_symbol(symbol) for symbol in (signal_payload.get("sell_symbols") or []) if symbol]
    target_symbols = [normalize_external_symbol(symbol) for symbol in (signal_payload.get("target_symbols") or []) if symbol]
    buy_symbols = [normalize_external_symbol(symbol) for symbol in (signal_payload.get("buy_symbols") or []) if symbol]
    symbols = sorted({
        *current_quantities.keys(),
        *sell_symbols,
        *target_symbols,
        *buy_symbols,
    })
    if not symbols:
        return {
            "status": "SKIPPED",
            "message": "没有需要同步的目标标的",
            "signal": signal_payload,
            "targets": [],
            "executor_result": None,
        }

    try:
        reference_prices = await get_realtime_reference_prices(
            account.id,
            symbols,
            timeout=min(float(timeout_seconds or 120.0), 30.0),
        )
    except ExternalTradingValuationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    reference_prices = {
        normalize_external_symbol(symbol): external_safe_float(price)
        for symbol, price in (reference_prices or {}).items()
        if external_safe_float(price) and external_safe_float(price) > 0
    }
    missing_prices = [symbol for symbol in symbols if not reference_prices.get(symbol)]
    if missing_prices:
        raise HTTPException(status_code=409, detail=f"无法获取以下标的执行参考价: {', '.join(missing_prices)}")

    try:
        valuation = await calculate_sub_account_net_asset(
            external_db,
            sub_account,
            positions=list(positions.values()),
            update_positions=True,
        )
        net_asset = external_safe_float(valuation.get("net_asset"))
        cash_available = external_safe_float(valuation.get("cash_available"), external_safe_float(sub_account.cash_available))
    except ExternalTradingValuationError:
        cash_available = external_safe_float(sub_account.cash_available)
        net_asset = cash_available + sum(
            quantity * reference_prices.get(symbol, 0.0)
            for symbol, quantity in current_quantities.items()
        )

    lot_size = max(1, int(request.lot_size or 1))
    rotation_mode = normalize_rotation_mode(request.rotation_mode)
    targets_by_symbol: Dict[str, Dict[str, Any]] = {}

    def set_target(symbol: str, quantity: int, target_weight: Optional[float] = None, target_value: Optional[float] = None):
        price = reference_prices.get(symbol) or 0.0
        targets_by_symbol[symbol] = {
            "symbol": symbol,
            "target_quantity": max(0, int(quantity or 0)),
            "target_weight_pct": _safe_float(target_weight * 100, 4) if target_weight is not None else None,
            "target_value": _safe_float(target_value if target_value is not None else max(0, int(quantity or 0)) * price, 2),
            "reference_price": _safe_float(price, 4),
        }

    if rotation_mode == "cash_fill_rebalance":
        for symbol, quantity in current_quantities.items():
            set_target(symbol, quantity)
        estimated_sell_cash = 0.0
        for symbol in sell_symbols:
            quantity = current_quantities.get(symbol, 0)
            if quantity <= 0:
                continue
            estimated_sell_cash += quantity * reference_prices[symbol]
            set_target(symbol, 0, 0.0, 0.0)
        buy_weights = _cash_fill_weights_from_signal(signal_payload, buy_symbols)
        available_cash = max(0.0, cash_available + estimated_sell_cash)
        for symbol in buy_symbols:
            price = reference_prices.get(symbol) or 0.0
            weight = float(buy_weights.get(symbol) or 0.0)
            if price <= 0 or weight <= 0:
                continue
            add_quantity = _floor_to_lot(available_cash * weight / price, lot_size)
            set_target(
                symbol,
                current_quantities.get(symbol, 0) + add_quantity,
                None,
                add_quantity * price,
            )
    else:
        weights = signal_payload.get("target_weights") or {}
        for symbol in target_symbols:
            price = reference_prices.get(symbol) or 0.0
            weight = _safe_float(weights.get(symbol))
            if price <= 0 or weight is None or weight <= 0:
                continue
            target_value = max(0.0, net_asset * weight)
            set_target(
                symbol,
                _floor_to_lot(target_value / price, lot_size),
                weight,
                target_value,
            )
        for symbol in current_quantities:
            if symbol not in targets_by_symbol:
                set_target(symbol, 0, 0.0, 0.0)

    targets = list(targets_by_symbol.values())
    targets.sort(key=lambda item: (0 if item["target_quantity"] == 0 else 1, item["symbol"]))
    signal_id = f"factor_live:{config.id}:{actual_signal_date.isoformat()}"
    signal_version = datetime.now().strftime("%Y%m%d%H%M%S")
    sync_target_positions(
        external_db,
        sub_account=sub_account,
        targets=targets,
        signal_id=signal_id,
        signal_version=signal_version,
    )
    external_db.commit()

    executor_result = None
    if trigger_executor:
        executor_result = await trigger_external_trading_executor(
            account_id=config.account_id,
            external_account_id=account.id,
            trigger_source=f"factor_live_trading:{config.id}",
            force=force,
            lot_size=lot_size,
            order_timeout_seconds=int(timeout_seconds or 120),
        )

    return {
        "status": "OK",
        "message": "已按信号同步目标仓位" + ("并触发外部执行器" if trigger_executor else ""),
        "signal": signal_payload,
        "targets": targets,
        "reference_prices": reference_prices,
        "net_asset": _safe_float(net_asset, 2),
        "cash_available": _safe_float(cash_available, 2),
        "lot_size": lot_size,
        "signal_id": signal_id,
        "signal_version": signal_version,
        "executor_result": executor_result,
    }


def _is_time_reached(now: datetime, time_text: str) -> bool:
    try:
        hour, minute = [int(item) for item in str(time_text or "00:00").split(":")]
    except Exception:
        return False
    return (now.hour, now.minute) >= (hour, minute)


@lru_cache(maxsize=512)
def _is_china_trading_day(check_date: date) -> bool:
    if check_date.weekday() >= 5:
        return False
    try:
        calendar = TushareService.get_instance().get_trade_calendar_frame(check_date, check_date)
        if not calendar.empty:
            row = calendar.iloc[0]
            return int(row.get("is_open") or 0) == 1
    except Exception as exc:
        logger.warning("A-share trading calendar check failed for %s: %s", check_date, exc)
    return True


@lru_cache(maxsize=512)
def _is_us_trading_day(check_date: date) -> bool:
    return check_date.weekday() < 5 and not MarketService.is_us_market_holiday(check_date)


def _next_trading_day_after(check_date: date, is_trading_day: Callable[[date], bool]) -> Optional[date]:
    next_day = check_date + timedelta(days=1)
    for _ in range(370):
        if is_trading_day(next_day):
            return next_day
        next_day += timedelta(days=1)
    return None


def _previous_or_same_trading_day(check_date: date, is_trading_day: Callable[[date], bool]) -> Optional[date]:
    candidate = check_date
    for _ in range(370):
        if is_trading_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return None


def _factor_live_trading_day_checker(config: FactorLiveTradingConfig) -> Callable[[date], bool]:
    market_kind = _factor_live_market_kind(config)
    return _is_us_trading_day if market_kind == "us" else _is_china_trading_day


def _factor_live_next_trading_day_resolver(config: FactorLiveTradingConfig) -> Callable[[date], Optional[date]]:
    is_trading_day = _factor_live_trading_day_checker(config)
    return lambda check_date: _next_trading_day_after(check_date, is_trading_day)


def _factor_live_next_execution_date(
    config: FactorLiveTradingConfig,
    signal_payload: Dict[str, Any],
    signal_date: date,
) -> Optional[date]:
    value = (signal_payload or {}).get("next_trading_date")
    if value:
        try:
            next_trading_date = date.fromisoformat(str(value))
            if next_trading_date > signal_date:
                return next_trading_date
        except ValueError:
            pass
    return _factor_live_next_trading_day_resolver(config)(signal_date)


def _factor_live_is_calendar_signal_day(config: FactorLiveTradingConfig, check_date: date) -> bool:
    is_trading_day = _factor_live_trading_day_checker(config)
    if not is_trading_day(check_date):
        return False
    request = _request_payload_from_live_config(config)
    rebalance_frequency = _normalize_rebalance_frequency(request.rebalance_frequency)
    next_trading_day = _factor_live_next_trading_day_resolver(config)(check_date)
    if not next_trading_day:
        return False
    return _is_rebalance_day([check_date, next_trading_day], 0, rebalance_frequency)


def _factor_live_resolve_requested_signal_date(
    config: FactorLiveTradingConfig,
    requested_date: date,
) -> Tuple[Optional[date], Optional[date]]:
    is_trading_day = _factor_live_trading_day_checker(config)
    latest_trading_day = _previous_or_same_trading_day(requested_date, is_trading_day)
    if not latest_trading_day:
        return None, None
    if not _factor_live_is_calendar_signal_day(config, latest_trading_day):
        return None, latest_trading_day
    return latest_trading_day, latest_trading_day


def _factor_live_not_signal_day_message(
    config: FactorLiveTradingConfig,
    check_date: date,
    resolved_trading_date: Optional[date] = None,
) -> str:
    request = _request_payload_from_live_config(config)
    rebalance_frequency = _normalize_rebalance_frequency(request.rebalance_frequency)
    label = FACTOR_LIVE_REBALANCE_FREQUENCY_LABELS.get(rebalance_frequency, rebalance_frequency)
    if resolved_trading_date and resolved_trading_date != check_date:
        return f"{check_date.isoformat()} 及之前最近交易日 {resolved_trading_date.isoformat()} 不是{label}信号日"
    return f"{check_date.isoformat()} 不是{label}信号日"


def _factor_live_market_kind(config: FactorLiveTradingConfig) -> str:
    request = _request_payload_from_live_config(config)
    pool_key = str(request.pool or "").strip().upper()
    if pool_key == "CUSTOM_US_STOCK":
        return "us"
    if pool_key == "CUSTOM_A_STOCK":
        return "cn"
    candidate_etfs = [str(item or "").strip().upper() for item in SHARED_POOL_ETFS.get(pool_key, []) if item]
    if candidate_etfs and all(symbol.endswith(".US") for symbol in candidate_etfs):
        return "us"
    return "cn"


def process_factor_live_trading_automation_for_robot() -> Dict[str, Any]:
    result = {
        "checked": 0,
        "signals": [],
        "signal_waiting": 0,
        "executions": [],
        "execution_waiting": 0,
        "errors": [],
    }
    configs = _load_enabled_factor_live_config_snapshots()
    result["checked"] = len(configs)
    for config in configs:
        market_kind = _factor_live_market_kind(config)
        try:
            signal_now = datetime.now(ZoneInfo(config.signal_timezone or "Asia/Shanghai"))
        except ZoneInfoNotFoundError:
            signal_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        signal_already_checked_today = (
            config.last_signal_date
            and config.last_signal_date == signal_now.date()
        )
        signal_trading_day = _is_us_trading_day(signal_now.date()) if market_kind == "us" else _is_china_trading_day(signal_now.date())
        calendar_signal_day = (
            signal_trading_day
            and _is_time_reached(signal_now, config.signal_time)
            and _factor_live_is_calendar_signal_day(config, signal_now.date())
        )
        if (
            signal_trading_day
            and _is_time_reached(signal_now, config.signal_time)
            and calendar_signal_day
            and not signal_already_checked_today
        ):
            try:
                with get_db_ctx() as db, get_external_trading_db_ctx() as external_db:
                    plan = _build_factor_live_signal_plan(
                        db,
                        external_db,
                        config,
                        signal_date=signal_now.date(),
                        rank_limit=100,
                    )
                signal_date_value = date.fromisoformat(plan["signal_date"])
                if not plan.get("is_signal_day"):
                    continue
                _apply_factor_live_snapshot_updates(
                    config,
                    _persist_factor_live_signal_success(config.id, plan),
                )
                result["signals"].append({"config_id": config.id, "status": plan.get("status"), "signal_date": plan.get("signal_date")})
            except Exception as exc:
                error_message = _factor_live_error_message(exc)
                _apply_factor_live_snapshot_updates(
                    config,
                    _persist_factor_live_signal_failure(config.id, error_message),
                )
                _send_factor_live_automation_alert(
                    config,
                    action="SIGNAL",
                    status="FAILED",
                    message=error_message,
                    exc=exc,
                )
                result["errors"].append({"config_id": config.id, "action": "SIGNAL", "error": error_message})
        else:
            if not (signal_trading_day and _is_time_reached(signal_now, config.signal_time) and not calendar_signal_day):
                result["signal_waiting"] += 1

        signal_payload = config.last_signal_payload or {}
        signal_date_value = _normalize_signal_payload_date(signal_payload)
        next_execution_date = (
            _factor_live_next_execution_date(config, signal_payload, signal_date_value)
            if signal_date_value
            else None
        )
        try:
            execution_now = datetime.now(ZoneInfo(config.execution_timezone or "Asia/Shanghai"))
        except ZoneInfoNotFoundError:
            execution_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        execution_trading_day = _is_us_trading_day(execution_now.date()) if market_kind == "us" else _is_china_trading_day(execution_now.date())
        signal_generated_date = config.last_signal_at.date() if config.last_signal_at else None
        should_execute = (
            signal_date_value
            and signal_payload.get("should_rebalance")
            and config.last_execution_signal_date != signal_date_value
            and execution_trading_day
            and next_execution_date
            and execution_now.date() == next_execution_date
            and (not signal_generated_date or execution_now.date() > signal_generated_date)
            and _is_time_reached(execution_now, config.execution_time)
        )
        if should_execute:
            try:
                with get_external_trading_db_ctx() as external_db:
                    execution_result = asyncio.run(
                        _execute_factor_live_signal(
                            None,
                            external_db,
                            config,
                            signal_date=signal_date_value,
                            trigger_executor=True,
                            force=True,
                        )
                    )
                    executor_failure = _factor_live_executor_failure_payload(execution_result)
                    execution_status = (
                        executor_failure.get("status")
                        if executor_failure
                        else execution_result.get("status")
                    ) or "OK"
                _apply_factor_live_snapshot_updates(
                    config,
                    _persist_factor_live_execution_result(config.id, execution_result, execution_status),
                )
                if executor_failure:
                    alert_message = f"外部交易执行器返回异常状态: {executor_failure.get('status')}"
                    _send_factor_live_automation_alert(
                        config,
                        action="EXECUTE",
                        status=execution_status,
                        message=alert_message,
                        signal_date=signal_date_value,
                        payload=execution_result,
                    )
                    result["errors"].append({
                        "config_id": config.id,
                        "action": "EXECUTE",
                        "error": alert_message,
                    })
                result["executions"].append({"config_id": config.id, "status": execution_status, "signal_date": signal_date_value.isoformat()})
            except Exception as exc:
                error_message = _factor_live_error_message(exc)
                _apply_factor_live_snapshot_updates(
                    config,
                    _persist_factor_live_execution_failure(config.id, error_message, signal_date_value),
                )
                _send_factor_live_automation_alert(
                    config,
                    action="EXECUTE",
                    status="FAILED",
                    message=error_message,
                    signal_date=signal_date_value,
                    exc=exc,
                )
                result["errors"].append({"config_id": config.id, "action": "EXECUTE", "error": error_message})
        else:
            result["execution_waiting"] += 1
    return result


def _run_factor_backtest(
    request: FactorBacktestRequest,
    db: ORMSession,
    prepared_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        result = shared_run_factor_backtest(
            _to_shared_backtest_config(request),
            db,
            prepared_data=prepared_data,
        )
        metadata = result.setdefault("metadata", {})
        metadata["oos_start_date"] = request.oos_start_date.isoformat() if request.oos_start_date else None
        result["meta"] = metadata
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _composition_count(total: int, parts: int) -> int:
    if parts <= 1:
        return 1
    return math.comb(int(total) + int(parts) - 1, int(parts) - 1)


def _fixed_factor_weights(legs: List[CompositeFactorLeg]) -> List[float]:
    return [float(leg.weight or 0) for leg in legs]


def _simplex_weight_grid(parts: int, bucket_count: int) -> List[List[float]]:
    parts = int(parts)
    bucket_count = int(bucket_count)
    if parts <= 1:
        return [[1.0]]

    result: List[List[float]] = []

    def walk(remaining: int, slots: int, prefix: List[int]):
        if slots == 1:
            result.append([*(value / bucket_count for value in prefix), remaining / bucket_count])
            return
        for value in range(remaining + 1):
            walk(remaining - value, slots - 1, [*prefix, value])

    walk(bucket_count, parts, [])
    return result


def _estimate_backtest_search_cases(request: FactorBacktestSearchRequest) -> int:
    legs = list(request.request.legs)
    leg_count = len(legs)
    if leg_count <= 0:
        return 0

    mixed_indexes = {
        index
        for index, leg in enumerate(legs)
        if leg.window == MIXED_WINDOW_KEY
    }
    window_case_count = (
        1
        if int(request.window_weight_bucket_count) <= 0
        else _composition_count(request.window_weight_bucket_count, len(SUPPORTED_MOMENTUM_WINDOWS))
    )
    factor_bucket_count = int(request.factor_weight_bucket_count)
    if factor_bucket_count <= 0:
        fixed_weights = _fixed_factor_weights(legs)
        active_mixed_count = sum(
            1
            for index in mixed_indexes
            if abs(float(fixed_weights[index])) > 1e-12
        )
        total = window_case_count ** active_mixed_count
    else:
        total = 0

        for active_count in range(1, leg_count + 1):
            factor_cases_for_subset = math.comb(factor_bucket_count - 1, active_count - 1)
            if factor_cases_for_subset <= 0:
                continue
            for active_indexes in combinations(range(leg_count), active_count):
                active_mixed_count = sum(1 for index in active_indexes if index in mixed_indexes)
                total += factor_cases_for_subset * (window_case_count ** active_mixed_count)
    return int(
        total
        * max(1, len(request.position_weight_candidates or []))
        * max(1, len(request.sell_rank_multiplier_candidates or []))
        * max(1, len(request.rotation_mode_candidates or []))
    )


def _backtest_request_payload(request: FactorBacktestRequest) -> Dict[str, Any]:
    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    return jsonable_encoder(payload)


def _backtest_leg_payload(leg: CompositeFactorLeg) -> Dict[str, Any]:
    payload = leg.model_dump() if hasattr(leg, "model_dump") else leg.dict()
    return jsonable_encoder(payload)


def _format_weight(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _format_backtest_search_params(
    request: FactorBacktestRequest,
    legs: List[Dict[str, Any]],
) -> str:
    factor_parts = [
        f"{leg.get('factor')}={_format_weight(float(leg.get('weight') or 0))}"
        for leg in legs
    ]
    window_parts = []
    for leg in legs:
        if leg.get("window") != MIXED_WINDOW_KEY:
            continue
        weights = _normalize_momentum_weights_payload(leg.get("momentum_weights"))
        window_parts.append(
            f"{leg.get('factor')}窗口 "
            + "/".join(f"{window}:{_format_weight(weights[str(window)])}" for window in SUPPORTED_MOMENTUM_WINDOWS)
        )
    return "；".join(
        item
        for item in [
            f"仓位 {format_position_weights(request.position_weights)}",
            f"调仓 {ROTATION_MODE_LABELS.get(request.rotation_mode, request.rotation_mode)}",
            f"卖出倍数 {_format_weight(float(request.sell_rank_multiplier))}",
            "因子 " + " / ".join(factor_parts),
            "；".join(window_parts),
        ]
        if item
    )


def _iter_backtest_search_requests(
    search_request: FactorBacktestSearchRequest,
):
    base_payload = _backtest_request_payload(search_request.request)
    leg_payloads = [_backtest_leg_payload(leg) for leg in search_request.request.legs]
    leg_count = len(leg_payloads)
    factor_weight_grid = (
        [_fixed_factor_weights(search_request.request.legs)]
        if search_request.factor_weight_bucket_count <= 0
        else _simplex_weight_grid(leg_count, search_request.factor_weight_bucket_count)
        if leg_count > 1
        else [[1.0]]
    )
    position_weight_grid = search_request.position_weight_candidates or [search_request.request.position_weights]
    sell_multiplier_grid = search_request.sell_rank_multiplier_candidates or [search_request.request.sell_rank_multiplier]
    rotation_mode_grid = search_request.rotation_mode_candidates or [search_request.request.rotation_mode]
    for factor_weights in factor_weight_grid:
        window_grids = []
        for index, leg in enumerate(leg_payloads):
            if (
                leg.get("window") == MIXED_WINDOW_KEY
                and abs(float(factor_weights[index])) > 1e-12
                and search_request.window_weight_bucket_count > 0
            ):
                window_grids.append(_simplex_weight_grid(len(SUPPORTED_MOMENTUM_WINDOWS), search_request.window_weight_bucket_count))
            else:
                window_grids.append([None])
        for window_weights_tuple in product(*window_grids):
            next_legs: List[Dict[str, Any]] = []
            for index, leg in enumerate(leg_payloads):
                next_leg = dict(leg)
                next_leg["weight"] = factor_weights[index]
                window_weights = window_weights_tuple[index]
                if window_weights is not None:
                    next_leg["momentum_weights"] = {
                        str(window): float(window_weights[window_index])
                        for window_index, window in enumerate(SUPPORTED_MOMENTUM_WINDOWS)
                    }
                next_legs.append(next_leg)
            for position_weights, sell_rank_multiplier, rotation_mode in product(position_weight_grid, sell_multiplier_grid, rotation_mode_grid):
                next_payload = dict(base_payload)
                next_payload["position_weights"] = list(position_weights)
                next_payload["max_positions"] = len(position_weights)
                next_payload["sell_rank_multiplier"] = float(sell_rank_multiplier)
                next_payload["rotation_mode"] = rotation_mode
                next_payload["legs"] = next_legs
                yield FactorBacktestRequest(**next_payload), next_legs


def _equity_segment_metrics(
    equity_curve: List[Dict[str, Any]],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[str, Optional[float]]:
    rows = []
    for item in equity_curve or []:
        raw_date = item.get("date")
        raw_value = item.get("value")
        if raw_date is None or raw_value is None:
            continue
        try:
            item_date = date.fromisoformat(str(raw_date))
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        if start_date is not None and item_date < start_date:
            continue
        if end_date is not None and item_date > end_date:
            continue
        rows.append((item_date, value))

    if len(rows) < 2:
        return {
            "total_return": None,
            "annualized_return": None,
            "annualized_volatility": None,
            "sharpe": None,
            "calmar": None,
            "max_drawdown": None,
        }

    start_dt, start_value = rows[0]
    end_dt, end_value = rows[-1]
    total_return = (end_value / start_value - 1) * 100 if start_value > 0 else None
    elapsed_days = max(0, (end_dt - start_dt).days)
    annualized_return = None
    if total_return is not None and elapsed_days > 0 and total_return > -100:
        annualized_return = ((1 + total_return / 100) ** (365 / elapsed_days) - 1) * 100

    peak = start_value
    max_drawdown = 0.0
    returns = []
    previous_value = start_value
    for _, value in rows[1:]:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, (value / peak - 1) * 100)
        if previous_value > 0:
            returns.append(value / previous_value - 1)
        previous_value = value

    annualized_volatility = None
    sharpe = None
    if len(returns) > 1:
        mean_return = sum(returns) / len(returns)
        std_return = float(np.std(returns, ddof=1))
        if std_return > 0:
            annualized_volatility = std_return * math.sqrt(TRADING_DAYS_PER_YEAR) * 100
            sharpe = mean_return / std_return * math.sqrt(TRADING_DAYS_PER_YEAR)

    calmar = None
    if annualized_return is not None and max_drawdown < 0:
        calmar = annualized_return / abs(max_drawdown)

    return {
        "total_return": _safe_float(total_return, 4),
        "annualized_return": _safe_float(annualized_return, 4),
        "annualized_volatility": _safe_float(annualized_volatility, 4),
        "sharpe": _safe_float(sharpe, 6),
        "calmar": _safe_float(calmar, 6),
        "max_drawdown": _safe_float(max_drawdown, 4),
    }


def _split_backtest_metrics(result: Dict[str, Any], request: FactorBacktestRequest) -> Dict[str, Optional[float]]:
    oos_start = request.oos_start_date
    if not oos_start:
        return {}
    in_sample_end = oos_start - timedelta(days=1)
    equity_curve = result.get("equity_curve") or []
    in_sample = _equity_segment_metrics(equity_curve, request.start_date, in_sample_end)
    oos = _equity_segment_metrics(equity_curve, oos_start, request.end_date)
    return {
        **{f"in_sample_{key}": value for key, value in in_sample.items()},
        **{f"oos_{key}": value for key, value in oos.items()},
    }


def _objective_value_from_row(row: Dict[str, Any], objective: str) -> Optional[float]:
    value = row.get(objective)
    if value is None:
        return None
    return _safe_float(value, 6)


def _search_row_from_backtest_result(
    index: int,
    request: FactorBacktestRequest,
    legs: List[Dict[str, Any]],
    result: Dict[str, Any],
    objective: str,
) -> Dict[str, Any]:
    metrics = result.get("metrics") or {}
    split_metrics = _split_backtest_metrics(result, request)
    row = {
        "case_index": index,
        "objective": objective,
        "params_label": _format_backtest_search_params(request, legs),
        "max_positions": request.max_positions,
        "position_weights": request.position_weights,
        "position_weights_label": format_position_weights(request.position_weights),
        "sell_rank_multiplier": request.sell_rank_multiplier,
        "rotation_mode": request.rotation_mode,
        "rotation_mode_label": ROTATION_MODE_LABELS.get(request.rotation_mode, request.rotation_mode),
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "sharpe": metrics.get("sharpe"),
        "calmar": metrics.get("calmar"),
        "annualized_volatility": metrics.get("annualized_volatility"),
        "max_drawdown": metrics.get("max_drawdown"),
        "ending_value": metrics.get("ending_value"),
        "trade_count": metrics.get("trade_count"),
        "win_rate": metrics.get("win_rate"),
        "rebalance_count": metrics.get("rebalance_count"),
        "holding_count": metrics.get("holding_count"),
        "request": _backtest_request_payload(request),
    }
    row.update(split_metrics)
    row["objective_value"] = _objective_value_from_row(row, objective)
    return row


def _backtest_search_row_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rank": row.get("rank"),
        "case_index": row.get("case_index"),
        "objective": row.get("objective"),
        "objective_value": row.get("objective_value"),
        "params_label": row.get("params_label"),
        "max_positions": row.get("max_positions"),
        "position_weights": row.get("position_weights"),
        "position_weights_label": row.get("position_weights_label"),
        "sell_rank_multiplier": row.get("sell_rank_multiplier"),
        "rotation_mode": row.get("rotation_mode"),
        "rotation_mode_label": row.get("rotation_mode_label"),
        "total_return": row.get("total_return"),
        "annualized_return": row.get("annualized_return"),
        "sharpe": row.get("sharpe"),
        "calmar": row.get("calmar"),
        "annualized_volatility": row.get("annualized_volatility"),
        "max_drawdown": row.get("max_drawdown"),
        "in_sample_total_return": row.get("in_sample_total_return"),
        "in_sample_annualized_return": row.get("in_sample_annualized_return"),
        "in_sample_sharpe": row.get("in_sample_sharpe"),
        "in_sample_calmar": row.get("in_sample_calmar"),
        "in_sample_annualized_volatility": row.get("in_sample_annualized_volatility"),
        "in_sample_max_drawdown": row.get("in_sample_max_drawdown"),
        "oos_total_return": row.get("oos_total_return"),
        "oos_annualized_return": row.get("oos_annualized_return"),
        "oos_sharpe": row.get("oos_sharpe"),
        "oos_calmar": row.get("oos_calmar"),
        "oos_annualized_volatility": row.get("oos_annualized_volatility"),
        "oos_max_drawdown": row.get("oos_max_drawdown"),
        "ending_value": row.get("ending_value"),
        "trade_count": row.get("trade_count"),
        "win_rate": row.get("win_rate"),
        "rebalance_count": row.get("rebalance_count"),
        "holding_count": row.get("holding_count"),
        "request": row.get("request"),
    }


def _db_search_row_to_dict(row: FactorBacktestSearchResult) -> Dict[str, Any]:
    request_payload = row.request_payload or {}
    position_weights = request_payload.get("position_weights") or normalize_position_weights(None, row.max_positions or 1)
    rotation_mode = normalize_rotation_mode(request_payload.get("rotation_mode"))
    return {
        "id": row.id,
        "rank": row.rank,
        "case_index": row.case_index,
        "objective": row.objective,
        "objective_value": row.objective_value,
        "params_label": row.params_label,
        "max_positions": row.max_positions,
        "position_weights": position_weights,
        "position_weights_label": format_position_weights(position_weights),
        "sell_rank_multiplier": row.sell_rank_multiplier,
        "rotation_mode": rotation_mode,
        "rotation_mode_label": ROTATION_MODE_LABELS.get(rotation_mode, rotation_mode),
        "total_return": row.total_return,
        "annualized_return": row.annualized_return,
        "sharpe": row.sharpe,
        "calmar": row.calmar,
        "annualized_volatility": row.annualized_volatility,
        "max_drawdown": row.max_drawdown,
        "in_sample_total_return": row.in_sample_total_return,
        "in_sample_annualized_return": row.in_sample_annualized_return,
        "in_sample_sharpe": row.in_sample_sharpe,
        "in_sample_calmar": row.in_sample_calmar,
        "in_sample_annualized_volatility": row.in_sample_annualized_volatility,
        "in_sample_max_drawdown": row.in_sample_max_drawdown,
        "oos_total_return": row.oos_total_return,
        "oos_annualized_return": row.oos_annualized_return,
        "oos_sharpe": row.oos_sharpe,
        "oos_calmar": row.oos_calmar,
        "oos_annualized_volatility": row.oos_annualized_volatility,
        "oos_max_drawdown": row.oos_max_drawdown,
        "ending_value": row.ending_value,
        "trade_count": row.trade_count,
        "win_rate": row.win_rate,
        "rebalance_count": row.rebalance_count,
        "holding_count": row.holding_count,
        "request": request_payload,
    }


def _get_backtest_search_state(db: ORMSession) -> Optional[FactorBacktestSearchState]:
    return db.query(FactorBacktestSearchState).filter(FactorBacktestSearchState.id == BACKTEST_SEARCH_STATE_ID).first()


def _persist_backtest_search_job(
    db: ORMSession,
    job: Dict[str, Any],
):
    now = datetime.now()
    state = _get_backtest_search_state(db)
    if not state:
        state = FactorBacktestSearchState(id=BACKTEST_SEARCH_STATE_ID, created_at=now)
        db.add(state)
    state.account_id = job.get("account_id")
    state.status = job.get("status", "idle")
    state.objective = job.get("objective", "annualized_return")
    state.request_payload = job.get("request_payload")
    state.search_params = job.get("search_params")
    state.total_cases = int(job.get("total_cases") or 0)
    state.submitted_cases = int(job.get("submitted_cases") or 0)
    state.completed_cases = int(job.get("completed_cases") or 0)
    state.failed_cases = int(job.get("failed_cases") or 0)
    state.top_n = None
    state.worker_count = int(job.get("worker_count") or 1)
    state.current_case = job.get("current_case")
    state.error = job.get("error")
    state.cancel_requested = bool(job.get("cancel_requested"))
    state.created_at = job.get("created_at") or state.created_at or now
    state.started_at = job.get("started_at")
    state.finished_at = job.get("finished_at")
    state.updated_at = now
    db.commit()


def _clear_backtest_search_results(db: ORMSession):
    db.query(FactorBacktestSearchResult).filter(
        FactorBacktestSearchResult.search_id == BACKTEST_SEARCH_STATE_ID
    ).delete()
    db.commit()


def _insert_backtest_search_result(db: ORMSession, row: Dict[str, Any]):
    payload = _backtest_search_row_payload(row)
    db.add(FactorBacktestSearchResult(
        search_id=BACKTEST_SEARCH_STATE_ID,
        rank=None,
        case_index=payload.get("case_index"),
        objective=payload.get("objective"),
        objective_value=payload.get("objective_value"),
        params_label=payload.get("params_label"),
        max_positions=payload.get("max_positions"),
        sell_rank_multiplier=payload.get("sell_rank_multiplier"),
        total_return=payload.get("total_return"),
        annualized_return=payload.get("annualized_return"),
        sharpe=payload.get("sharpe"),
        calmar=payload.get("calmar"),
        annualized_volatility=payload.get("annualized_volatility"),
        max_drawdown=payload.get("max_drawdown"),
        in_sample_total_return=payload.get("in_sample_total_return"),
        in_sample_annualized_return=payload.get("in_sample_annualized_return"),
        in_sample_sharpe=payload.get("in_sample_sharpe"),
        in_sample_calmar=payload.get("in_sample_calmar"),
        in_sample_annualized_volatility=payload.get("in_sample_annualized_volatility"),
        in_sample_max_drawdown=payload.get("in_sample_max_drawdown"),
        oos_total_return=payload.get("oos_total_return"),
        oos_annualized_return=payload.get("oos_annualized_return"),
        oos_sharpe=payload.get("oos_sharpe"),
        oos_calmar=payload.get("oos_calmar"),
        oos_annualized_volatility=payload.get("oos_annualized_volatility"),
        oos_max_drawdown=payload.get("oos_max_drawdown"),
        ending_value=payload.get("ending_value"),
        trade_count=payload.get("trade_count"),
        win_rate=payload.get("win_rate"),
        rebalance_count=payload.get("rebalance_count"),
        holding_count=payload.get("holding_count"),
        request_payload=payload.get("request"),
    ))


def _serialize_backtest_search_status_from_record(state: Optional[FactorBacktestSearchState]) -> Dict[str, Any]:
    if not state:
        return {
            "status": "idle",
            "objective": "annualized_return",
            "objective_label": BACKTEST_SEARCH_OBJECTIVE_OPTIONS["annualized_return"]["label"],
            "total_cases": 0,
            "submitted_cases": 0,
            "completed_cases": 0,
            "failed_cases": 0,
            "result_count": 0,
            "progress_pct": 0,
            "summary": {},
        }
    objective_label = BACKTEST_SEARCH_OBJECTIVE_OPTIONS.get(state.objective, {}).get("label", state.objective)
    elapsed_ms = None
    if state.started_at:
        end_time = state.finished_at or datetime.now()
        elapsed_ms = _safe_float((end_time - state.started_at).total_seconds() * 1000, 1)
    return {
        "status": state.status,
        "created_at": state.created_at.isoformat() if state.created_at else None,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "finished_at": state.finished_at.isoformat() if state.finished_at else None,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        "objective": state.objective,
        "objective_label": objective_label,
        "search_params": state.search_params or {},
        "request": state.request_payload,
        "window_weight_bucket_count": (state.search_params or {}).get("window_weight_bucket_count"),
        "factor_weight_bucket_count": (state.search_params or {}).get("factor_weight_bucket_count"),
        "max_positions_candidates": (state.search_params or {}).get("max_positions_candidates"),
        "position_weight_candidates": (state.search_params or {}).get("position_weight_candidates"),
        "sell_rank_multiplier_candidates": (state.search_params or {}).get("sell_rank_multiplier_candidates"),
        "rotation_mode_candidates": (state.search_params or {}).get("rotation_mode_candidates"),
        "worker_count": state.worker_count,
        "total_cases": state.total_cases,
        "submitted_cases": state.submitted_cases,
        "completed_cases": state.completed_cases,
        "failed_cases": state.failed_cases,
        "result_count": max(0, (state.completed_cases or 0) - (state.failed_cases or 0)),
        "progress_pct": _safe_float((state.completed_cases or 0) * 100 / state.total_cases, 2) if state.total_cases else 0,
        "current_case": state.current_case,
        "error": state.error,
        "summary": {
            "cases": state.completed_cases,
            "total_cases": state.total_cases,
            "submitted_cases": state.submitted_cases,
            "failed_cases": state.failed_cases,
            "worker_count": state.worker_count,
            "elapsed_ms": elapsed_ms,
            "objective": state.objective,
            "objective_label": objective_label,
        },
    }


def _job_datetime_iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _serialize_backtest_search_status_from_job(job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not job:
        return _serialize_backtest_search_status_from_record(None)
    objective = job.get("objective", "annualized_return")
    objective_label = BACKTEST_SEARCH_OBJECTIVE_OPTIONS.get(objective, {}).get("label", objective)
    started_at = job.get("started_at")
    finished_at = job.get("finished_at")
    elapsed_ms = None
    if isinstance(started_at, datetime):
        end_time = finished_at if isinstance(finished_at, datetime) else datetime.now()
        elapsed_ms = _safe_float((end_time - started_at).total_seconds() * 1000, 1)
    total_cases = int(job.get("total_cases") or 0)
    completed_cases = int(job.get("completed_cases") or 0)
    failed_cases = int(job.get("failed_cases") or 0)
    return {
        "status": job.get("status", "idle"),
        "created_at": _job_datetime_iso(job.get("created_at")),
        "started_at": _job_datetime_iso(started_at),
        "finished_at": _job_datetime_iso(finished_at),
        "updated_at": _job_datetime_iso(job.get("updated_at")),
        "objective": objective,
        "objective_label": objective_label,
        "search_params": job.get("search_params") or {},
        "request": job.get("request_payload"),
        "window_weight_bucket_count": (job.get("search_params") or {}).get("window_weight_bucket_count"),
        "factor_weight_bucket_count": (job.get("search_params") or {}).get("factor_weight_bucket_count"),
        "max_positions_candidates": (job.get("search_params") or {}).get("max_positions_candidates"),
        "position_weight_candidates": (job.get("search_params") or {}).get("position_weight_candidates"),
        "sell_rank_multiplier_candidates": (job.get("search_params") or {}).get("sell_rank_multiplier_candidates"),
        "rotation_mode_candidates": (job.get("search_params") or {}).get("rotation_mode_candidates"),
        "worker_count": int(job.get("worker_count") or 1),
        "total_cases": total_cases,
        "submitted_cases": int(job.get("submitted_cases") or 0),
        "completed_cases": completed_cases,
        "failed_cases": failed_cases,
        "result_count": int(job.get("result_count") or max(0, completed_cases - failed_cases)),
        "progress_pct": _safe_float(completed_cases * 100 / total_cases, 2) if total_cases else 0,
        "current_case": job.get("current_case"),
        "error": job.get("error"),
        "summary": {
            "cases": completed_cases,
            "total_cases": total_cases,
            "submitted_cases": int(job.get("submitted_cases") or 0),
            "failed_cases": failed_cases,
            "worker_count": int(job.get("worker_count") or 1),
            "elapsed_ms": elapsed_ms,
            "objective": objective,
            "objective_label": objective_label,
        },
    }


def _publish_backtest_search_job(job: Optional[Dict[str, Any]]) -> None:
    if not job:
        return
    with BACKTEST_SEARCH_JOBS_LOCK:
        snapshot = dict(job)
    publish_event(
        snapshot.get("account_id"),
        "factor_backtest_search",
        _serialize_backtest_search_status_from_job(snapshot),
    )


def _serialize_backtest_search_status(db: ORMSession) -> Dict[str, Any]:
    with BACKTEST_SEARCH_JOBS_LOCK:
        active_job = BACKTEST_SEARCH_ACTIVE_JOB
        active_thread = BACKTEST_SEARCH_ACTIVE_THREAD
        if active_job and active_job.get("status") not in {"queued", "running"}:
            return _serialize_backtest_search_status_from_job(dict(active_job))
        if (
            active_job
            and active_job.get("status") in {"queued", "running"}
            and active_thread
            and active_thread.is_alive()
        ):
            return _serialize_backtest_search_status_from_job(dict(active_job))
    state = _get_backtest_search_state(db)
    if state and state.status in {"queued", "running"}:
        now = datetime.now()
        state.status = "interrupted"
        state.cancel_requested = False
        state.current_case = None
        state.finished_at = state.finished_at or now
        state.updated_at = now
        state.error = state.error or "任务进程已中断，请重新启动搜索"
        db.commit()
    return _serialize_backtest_search_status_from_record(state)


BACKTEST_SEARCH_RESULT_SORT_COLUMNS = {
    "rank": FactorBacktestSearchResult.id,
    "case_index": FactorBacktestSearchResult.case_index,
    "objective_value": FactorBacktestSearchResult.objective_value,
    "max_positions": FactorBacktestSearchResult.max_positions,
    "sell_rank_multiplier": FactorBacktestSearchResult.sell_rank_multiplier,
    "total_return": FactorBacktestSearchResult.total_return,
    "annualized_return": FactorBacktestSearchResult.annualized_return,
    "sharpe": FactorBacktestSearchResult.sharpe,
    "calmar": FactorBacktestSearchResult.calmar,
    "annualized_volatility": FactorBacktestSearchResult.annualized_volatility,
    "max_drawdown": FactorBacktestSearchResult.max_drawdown,
    "in_sample_total_return": FactorBacktestSearchResult.in_sample_total_return,
    "in_sample_annualized_return": FactorBacktestSearchResult.in_sample_annualized_return,
    "in_sample_sharpe": FactorBacktestSearchResult.in_sample_sharpe,
    "in_sample_calmar": FactorBacktestSearchResult.in_sample_calmar,
    "in_sample_annualized_volatility": FactorBacktestSearchResult.in_sample_annualized_volatility,
    "in_sample_max_drawdown": FactorBacktestSearchResult.in_sample_max_drawdown,
    "oos_total_return": FactorBacktestSearchResult.oos_total_return,
    "oos_annualized_return": FactorBacktestSearchResult.oos_annualized_return,
    "oos_sharpe": FactorBacktestSearchResult.oos_sharpe,
    "oos_calmar": FactorBacktestSearchResult.oos_calmar,
    "oos_annualized_volatility": FactorBacktestSearchResult.oos_annualized_volatility,
    "oos_max_drawdown": FactorBacktestSearchResult.oos_max_drawdown,
    "ending_value": FactorBacktestSearchResult.ending_value,
    "trade_count": FactorBacktestSearchResult.trade_count,
    "win_rate": FactorBacktestSearchResult.win_rate,
    "rebalance_count": FactorBacktestSearchResult.rebalance_count,
    "holding_count": FactorBacktestSearchResult.holding_count,
}


def _parse_backtest_search_filters(raw_filters: Optional[str]) -> Dict[str, Dict[str, float]]:
    if not raw_filters:
        return {}
    try:
        data = json.loads(raw_filters)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="过滤条件格式不正确") from exc
    if not isinstance(data, dict):
        return {}
    normalized: Dict[str, Dict[str, float]] = {}
    for field, bounds in data.items():
        if field not in BACKTEST_SEARCH_RESULT_SORT_COLUMNS or not isinstance(bounds, dict):
            continue
        item: Dict[str, float] = {}
        for key in ("min", "max"):
            value = bounds.get(key)
            if value in (None, ""):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{field} 过滤值必须是数字") from exc
            if math.isfinite(number):
                item[key] = number
        if item:
            normalized[field] = item
    return normalized


def _query_backtest_search_results(
    db: ORMSession,
    page: int,
    page_size: int,
    sort_field: Optional[str],
    sort_order: Optional[str],
    raw_filters: Optional[str],
) -> Dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = min(500, max(1, int(page_size or 20)))
    query = db.query(FactorBacktestSearchResult).filter(
        FactorBacktestSearchResult.search_id == BACKTEST_SEARCH_STATE_ID
    )
    filters = _parse_backtest_search_filters(raw_filters)
    for field, bounds in filters.items():
        column = BACKTEST_SEARCH_RESULT_SORT_COLUMNS[field]
        if "min" in bounds:
            query = query.filter(column >= bounds["min"])
        if "max" in bounds:
            query = query.filter(column <= bounds["max"])

    total = query.count()
    column = BACKTEST_SEARCH_RESULT_SORT_COLUMNS.get(sort_field or "objective_value")
    if column is None:
        column = FactorBacktestSearchResult.objective_value
    order = (sort_order or "descend").lower()
    descending = order not in {"asc", "ascend"}
    order_by = [column.is_(None).asc(), column.desc() if descending else column.asc()]
    if column is not FactorBacktestSearchResult.id:
        if column is not FactorBacktestSearchResult.annualized_return:
            order_by.extend([
                FactorBacktestSearchResult.annualized_return.is_(None).asc(),
                FactorBacktestSearchResult.annualized_return.desc(),
            ])
        if column is not FactorBacktestSearchResult.total_return:
            order_by.extend([
                FactorBacktestSearchResult.total_return.is_(None).asc(),
                FactorBacktestSearchResult.total_return.desc(),
            ])
    order_by.append(FactorBacktestSearchResult.id.asc())
    rows = (
        query.order_by(*order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    records = []
    for index, row in enumerate(rows, start=(page - 1) * page_size + 1):
        item = _db_search_row_to_dict(row)
        item["rank"] = index
        records.append(item)
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "sort_field": sort_field or "objective_value",
        "sort_order": "descend" if descending else "ascend",
        "filters": filters,
        "rows": records,
    }


def _snapshot_backtest_search_job(job: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = dict(job)
    return snapshot


def _persist_active_backtest_search_job(
    db: ORMSession,
    job: Dict[str, Any],
):
    with BACKTEST_SEARCH_JOBS_LOCK:
        snapshot = _snapshot_backtest_search_job(job)
    _persist_backtest_search_job(db, snapshot)


def _run_backtest_search_case(
    index: int,
    case_request: FactorBacktestRequest,
    legs: List[Dict[str, Any]],
    objective: str,
    prepared_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """在线程池 worker 中执行单个搜参回测；每个 worker 使用独立的短事务 DB 会话。"""
    db = DBSession()
    try:
        result = _run_factor_backtest(case_request, db, prepared_data=prepared_data)
        return _search_row_from_backtest_result(index, case_request, legs, result, objective)
    finally:
        db.close()
        DBSession.remove()


def _run_backtest_search_job(search_request: FactorBacktestSearchRequest, job: Dict[str, Any]):
    db = DBSession()
    prepared_data: Optional[Dict[str, Any]] = None
    try:
        worker_count = _backtest_search_worker_count()
        with BACKTEST_SEARCH_JOBS_LOCK:
            job["status"] = "running"
            job["started_at"] = datetime.now()
            job["started_monotonic"] = time.time()
            job["worker_count"] = worker_count
            job["current_case"] = "准备基础数据和基础因子"
            job["updated_at"] = datetime.now()
        _persist_active_backtest_search_job(db, job)
        _publish_backtest_search_job(job)

        base_resolved_legs = _resolve_factor_legs(search_request.request.legs)
        prepared_data = _prepare_factor_backtest_base_data(search_request.request, db, base_resolved_legs)
        _warm_backtest_search_factor_caches(search_request, prepared_data, db)

        cancelled = False
        case_iter = _iter_backtest_search_requests(search_request)
        pending: List[Tuple[int, Future]] = []
        index = 0
        in_flight_limit = max(worker_count * 2, 4)
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="factor-backtest-search") as executor:
            while True:
                while not cancelled and len(pending) < in_flight_limit:
                    item = next(case_iter, None)
                    if item is None:
                        break
                    case_request, legs = item
                    index += 1
                    with BACKTEST_SEARCH_JOBS_LOCK:
                        job["submitted_cases"] = index
                        job["updated_at"] = datetime.now()
                        job["current_case"] = _format_backtest_search_params(case_request, legs)
                    pending.append(
                        (
                            index,
                            executor.submit(
                                _run_backtest_search_case,
                                index,
                                case_request,
                                legs,
                                search_request.objective,
                                prepared_data,
                            ),
                        )
                    )
                if not pending:
                    break

                done, _ = wait([future for _, future in pending], return_when=FIRST_COMPLETED)
                case_index_by_future = {id(future): case_index for case_index, future in pending}
                for done_future in done:
                    case_index = case_index_by_future[id(done_future)]
                    pending = [(case_i, future) for case_i, future in pending if future is not done_future]
                    try:
                        row = done_future.result()
                        _insert_backtest_search_result(db, row)
                        with BACKTEST_SEARCH_JOBS_LOCK:
                            job["completed_cases"] += 1
                            job["result_count"] += 1
                            job["updated_at"] = datetime.now()
                    except Exception as exc:
                        detail = getattr(exc, "detail", None)
                        logger.warning("Factor backtest search case %s failed: %s", case_index, detail or exc)
                        with BACKTEST_SEARCH_JOBS_LOCK:
                            job["completed_cases"] += 1
                            job["failed_cases"] += 1
                            job["updated_at"] = datetime.now()
                    _persist_active_backtest_search_job(db, job)
                    _publish_backtest_search_job(job)

                with BACKTEST_SEARCH_JOBS_LOCK:
                    if job.get("cancel_requested"):
                        job["cancel_requested"] = False
                        cancelled = True
                        job["status"] = "cancelled"
                        job["finished_at"] = datetime.now()
                        job["updated_at"] = datetime.now()
                        job["current_case"] = None
                        cancel_snapshot = _snapshot_backtest_search_job(job)
                    else:
                        cancel_snapshot = None
                if cancel_snapshot is not None:
                    _persist_backtest_search_job(db, cancel_snapshot)
                    _publish_backtest_search_job(cancel_snapshot)
                    break

        if not cancelled:
            with BACKTEST_SEARCH_JOBS_LOCK:
                job["status"] = "completed"
                job["finished_at"] = datetime.now()
                job["updated_at"] = datetime.now()
                job["current_case"] = None
                job["cancel_requested"] = False
            _persist_active_backtest_search_job(db, job)
            _publish_backtest_search_job(job)
    except Exception as exc:
        logger.exception("Factor backtest search job failed")
        with BACKTEST_SEARCH_JOBS_LOCK:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["finished_at"] = datetime.now()
            job["updated_at"] = datetime.now()
            job["current_case"] = None
            job["cancel_requested"] = False
        try:
            _persist_active_backtest_search_job(db, job)
            _publish_backtest_search_job(job)
        except Exception:
            logger.exception("Persist factor backtest search failure state failed")
    finally:
        if prepared_data is not None:
            prepared_data.clear()
        db.close()
        DBSession.remove()


def _start_backtest_search_job(search_request: FactorBacktestSearchRequest, account_id: str) -> Dict[str, Any]:
    global BACKTEST_SEARCH_ACTIVE_JOB, BACKTEST_SEARCH_ACTIVE_THREAD

    total_cases = _estimate_backtest_search_cases(search_request)
    available_cpu_cores = _ensure_backtest_search_cpu_capacity(total_cases)
    with BACKTEST_SEARCH_JOBS_LOCK:
        active_job = BACKTEST_SEARCH_ACTIVE_JOB
        active_thread = BACKTEST_SEARCH_ACTIVE_THREAD
        if (
            active_job
            and active_job.get("status") in {"queued", "running"}
            and active_thread
            and active_thread.is_alive()
        ):
            raise HTTPException(status_code=409, detail="批量搜索正在运行，请先取消或等待完成")

    job = {
        "account_id": account_id,
        "status": "queued",
        "created_at": datetime.now(),
        "started_at": None,
        "finished_at": None,
        "updated_at": datetime.now(),
        "objective": search_request.objective,
        "request_payload": _backtest_request_payload(search_request.request),
        "search_params": {
            "window_weight_bucket_count": search_request.window_weight_bucket_count,
            "factor_weight_bucket_count": search_request.factor_weight_bucket_count,
            "max_positions_candidates": search_request.max_positions_candidates,
            "position_weight_candidates": search_request.position_weight_candidates,
            "sell_rank_multiplier_candidates": search_request.sell_rank_multiplier_candidates,
            "rotation_mode_candidates": search_request.rotation_mode_candidates,
        },
        "total_cases": total_cases,
        "submitted_cases": 0,
        "completed_cases": 0,
        "failed_cases": 0,
        "result_count": 0,
        "worker_count": _backtest_search_worker_count(),
        "current_case": None,
        "error": None,
        "cancel_requested": False,
        "available_cpu_cores": available_cpu_cores,
    }
    db = DBSession()
    try:
        _persist_backtest_search_job(db, job)
        _clear_backtest_search_results(db)
        response = _serialize_backtest_search_status_from_job(job)
    finally:
        db.close()
        DBSession.remove()

    thread = threading.Thread(
        target=_run_backtest_search_job,
        args=(search_request, job),
        daemon=True,
        name="factor-backtest-search",
    )
    with BACKTEST_SEARCH_JOBS_LOCK:
        BACKTEST_SEARCH_ACTIVE_JOB = job
        BACKTEST_SEARCH_ACTIVE_THREAD = thread
    thread.start()
    _publish_backtest_search_job(job)
    return response


def _run_timing_factor_analysis(
    request: TimingFactorAnalyzeRequest,
    db: ORMSession,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    end_date = _resolve_timing_end_date(request, db)
    if request.start_date >= end_date:
        raise HTTPException(status_code=400, detail="开始日期必须早于实际结束日期")

    max_forward_window = max([request.forward_window, *request.heatmap_forward_windows])
    fetch_end = end_date + timedelta(days=max(30, max_forward_window * 4))
    price_df = _load_target_price_frame(db, request.target_symbol, request.start_date, fetch_end)
    if price_df.is_empty():
        raise HTTPException(status_code=400, detail=f"{request.target_symbol} 没有可用日行情数据")

    fear_df = _load_fear_history_frame(db, request.fear_symbol, request.start_date, end_date)
    if fear_df.is_empty():
        raise HTTPException(
            status_code=400,
            detail=f"{request.fear_symbol} 没有可用恐贪历史数据，请先同步 etf_fear_greed_clone_history",
        )

    heatmap_records = _compute_timing_heatmap(price_df, fear_df, request)
    selected_ma_window = int(request.ma_window)
    selected_forward_window = int(request.forward_window)
    if request.include_heatmap and heatmap_records:
        best_record = max(
            heatmap_records,
            key=lambda item: _record_float(item, request.heatmap_metric, -1e18),
        )
        if best_record.get(request.heatmap_metric) is not None:
            selected_ma_window = int(best_record["ma_window"])
            selected_forward_window = int(best_record["forward_window"])

    sample = _prepare_timing_sample(
        price_df=price_df,
        fear_df=fear_df,
        request=request,
        forward_window=selected_forward_window,
        ma_window=selected_ma_window,
    )
    if sample.is_empty():
        raise HTTPException(
            status_code=400,
            detail="目标标的行情与恐贪指数没有足够重叠样本，请调整日期、均线窗口或收益窗口",
        )

    bucket_df = _compute_bucket_report(sample)
    ic_df = _compute_timing_ic_series(sample)
    non_overlap = _compute_timing_non_overlapping_stats(
        sample,
        int(request.bucket_count),
        selected_forward_window,
    )
    yearly_df = _compute_timing_yearly_stability(sample, int(request.bucket_count), selected_forward_window)
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    summary = _summarize_timing_sample(
        sample,
        bucket_df,
        ic_df,
        request,
        selected_forward_window,
        elapsed_ms,
        non_overlap.get("summary"),
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

    fear_dates = fear_df.filter((pl.col("trade_date") >= request.start_date) & (pl.col("trade_date") <= end_date))
    heatmap_metric_meta = TIMING_HEATMAP_METRIC_OPTIONS.get(request.heatmap_metric, {})
    symbol_names = load_symbol_name_map([request.target_symbol], db)
    normalized_target_symbol = normalize_symbol_for_name(request.target_symbol) or request.target_symbol
    metadata = {
        "mode": "timing",
        "target_symbol": request.target_symbol,
        "target_symbol_name": symbol_names.get(normalized_target_symbol),
        "target_symbol_label": format_symbol_label(request.target_symbol, symbol_names),
        "symbol_names": symbol_names,
        "fear_symbol": request.fear_symbol,
        "fear_label": _fear_source_label(request.fear_symbol),
        "ma_window": selected_ma_window,
        "ma_window_label": _timing_ma_label(selected_ma_window),
        "selected_combo": {
            "ma_window": selected_ma_window,
            "ma_window_label": _timing_ma_label(selected_ma_window),
            "forward_window": selected_forward_window,
            "selection_mode": "auto" if request.include_heatmap and heatmap_records else "manual",
            "reason": f"best_heatmap_{request.heatmap_metric}" if request.include_heatmap and heatmap_records else "request",
        },
        "factor": {
            "key": "fear_greed_timing",
            "label": "择时：恐贪指数",
            "group": "择时",
            "description": "时间序列择时因子：同一天只有一个市场状态值，按日期分桶检验未来收益。低桶代表恐慌，高桶代表贪婪。",
            "direction": "exploratory",
        },
        "bucket_count": request.bucket_count,
        "forward_window": selected_forward_window,
        "heatmap_metric": request.heatmap_metric,
        "heatmap_metric_label": heatmap_metric_meta.get("label", request.heatmap_metric),
        "heatmap_metric_kind": heatmap_metric_meta.get("kind", "number"),
        "heatmap_forward_windows": request.heatmap_forward_windows,
        "heatmap_ma_windows": request.heatmap_ma_windows,
        "start_date": request.start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "effective_start_date": sample.select(pl.min("trade_date")).item().isoformat(),
        "effective_end_date": sample.select(pl.max("trade_date")).item().isoformat(),
        "price_rows": int(price_df.filter(pl.col("trade_date") <= end_date).height),
        "fear_points": int(fear_dates.height),
        "engine": "polars",
    }

    return {
        "metadata": metadata,
        "summary": summary,
        "bucket_returns": _records(bucket_df),
        "factor_distribution": _compute_factor_value_distribution(sample),
        "rank_ic_series": _records(ic_df),
        "non_overlapping_summary": non_overlap.get("summary") or {},
        "non_overlapping_offsets": non_overlap.get("offsets") or [],
        "yearly_stability": _records(yearly_df),
        "parameter_heatmap": heatmap_records,
    }


def _run_composite_factor_analysis(
    request: CompositeFactorAnalyzeRequest,
    db: ORMSession,
) -> Dict[str, Any]:
    resolved_legs = _resolve_composite_legs(request)
    required_windows = _required_windows_for_composite_legs(resolved_legs)

    started_at = time.perf_counter()
    end_date = _resolve_analysis_end_date(request.pool, request.end_date, db)
    if request.oos_start_date and request.oos_start_date >= end_date:
        raise HTTPException(status_code=400, detail="样本外起始日期必须早于实际结束日期")

    max_factor_window = max(required_windows)
    forward_window = int(request.forward_window)
    fetch_start = request.start_date - timedelta(
        days=max(370, request.min_listing_days + 30, max_factor_window * 4)
    )
    fetch_end = end_date + timedelta(days=max(30, forward_window * 4))
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

    needs_industry = any(leg["neutralization"] != "none" for leg in resolved_legs)
    industry_df = (
        _load_industry_frame(
            db,
            universe_history.all_symbols,
            request.start_date - timedelta(days=3650),
            end_date,
        )
        if needs_industry
        else None
    )

    composite_df = _prepare_composite_factor_frame(
        price_df=price_df,
        request=request,
        db=db,
        symbols=universe_history.all_symbols,
        start_date=request.start_date,
        end_date=end_date,
        analysis_dates=analysis_dates,
        industry_df=industry_df,
        resolved_legs=resolved_legs,
        candidate_etfs=candidate_etfs,
    )
    full_sample = _assign_buckets(
        _prepare_factor_sample(composite_df, universe_df, request, forward_window),
        int(request.bucket_count),
    )
    split_sample = _split_sample_for_oos(
        full_sample,
        analysis_dates,
        request.oos_start_date,
        forward_window,
    )
    analysis_sample = split_sample["analysis"]
    if analysis_sample.is_empty():
        detail = (
            "样本外切分后样本内训练样本不足，请调早样本外起始日期或缩短收益窗口"
            if request.oos_start_date
            else "没有可用于分桶的组合因子样本，请调整日期、窗口或股票池"
        )
        raise HTTPException(status_code=400, detail=detail)

    analysis_result = _compute_sample_artifacts(analysis_sample, request, forward_window)
    oos_result = _compute_sample_artifacts(split_sample["oos"], request, forward_window)
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    analysis_result["summary"]["elapsed_ms"] = round(elapsed_ms, 1)

    industry_rows = int(industry_df.height) if industry_df is not None else 0
    neutralization_warning = (
        "行业快照为空，中性化未生效"
        if needs_industry and industry_rows == 0
        else None
    )

    component_metadata = [
        {
            "component_key": leg["key"],
            "factor": leg["factor"],
            "factor_key": leg["factor"]["key"],
            "factor_label": leg["factor"]["label"],
            "window": leg["window"],
            "window_label": leg["window_label"],
            "windows": leg["windows"],
            "raw_weight": _safe_float(leg["raw_weight"], 4),
            "weight": _safe_float(leg["weight"], 4),
            "neutralization": leg["neutralization"],
            "neutralization_label": leg["neutralization_label"],
            "standardization": leg["standardization"],
            "standardization_label": leg["standardization_label"],
            "momentum_weights": leg["momentum_weights"],
        }
        for leg in resolved_legs
    ]
    symbol_names = load_symbol_name_map(candidate_etfs, db)
    metadata = {
        "mode": "composite",
        "pool": request.pool,
        "pool_label": next(item["label"] for item in POOL_OPTIONS if item["key"] == request.pool),
        "candidate_etfs": candidate_etfs,
        "symbol_names": symbol_names,
        "factor": {
            "key": "composite",
            "label": "组合因子",
            "group": "组合因子",
            "description": "多个子因子先独立方向调整、中性化、标准化，再按权重线性合成。",
            "direction": "higher_is_better",
        },
        "components": component_metadata,
        "neutralization": "per_leg",
        "neutralization_label": "子因子独立中性化",
        "neutralization_effective": needs_industry and industry_rows > 0,
        "neutralization_warning": neutralization_warning,
        "standardization": "per_leg",
        "standardization_label": "子因子独立标准化",
        "factor_combination_method": "weighted_standardized_factor_values",
        "factor_combination_method_label": "子因子标准化后加权",
        "windows": required_windows,
        "forward_window": forward_window,
        "bucket_count": request.bucket_count,
        "min_listing_days": request.min_listing_days,
        "oos_start_date": request.oos_start_date.isoformat() if request.oos_start_date else None,
        "analysis_scope": "in_sample" if request.oos_start_date else "full",
        "analysis_scope_label": "样本内" if request.oos_start_date else "全样本",
        "start_date": request.start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "universe_symbols": len(universe_history.all_symbols),
        "holdings_date_count": universe_history.holdings_date_count,
        "industry_rows": industry_rows,
        "industry_snapshot_mode": "latest_snapshot" if industry_rows else None,
        "price_rows": int(price_df.height),
        "engine": "polars",
    }

    return {
        "metadata": metadata,
        "summary": analysis_result["summary"],
        "bucket_returns": _records(analysis_result["bucket_df"]),
        "factor_distribution": analysis_result["factor_distribution"],
        "rank_ic_series": _records(analysis_result["ic_df"]),
        "non_overlapping_summary": analysis_result["non_overlapping_summary"],
        "non_overlapping_offsets": analysis_result["non_overlapping_offsets"],
        "yearly_stability": _records(analysis_result["yearly_stability"]),
        "oos_summary": oos_result["summary"],
        "oos_bucket_returns": _records(oos_result["bucket_df"]),
        "oos_factor_distribution": oos_result["factor_distribution"],
        "oos_rank_ic_series": _records(oos_result["ic_df"]),
        "oos_non_overlapping_summary": oos_result["non_overlapping_summary"],
        "oos_non_overlapping_offsets": oos_result["non_overlapping_offsets"],
        "oos_yearly_stability": _records(oos_result["yearly_stability"]),
        "component_ic": _compute_component_ic_summary(analysis_sample, resolved_legs),
        "component_correlation": _compute_component_correlation(analysis_sample, resolved_legs),
        "parameter_heatmap": [],
    }


def _run_factor_analysis(
    request: FactorLabAnalyzeRequest,
    db: ORMSession,
) -> Dict[str, Any]:
    factor_definition = FACTOR_REGISTRY.get(request.factor)
    if not factor_definition:
        raise HTTPException(status_code=400, detail=f"未注册的因子: {request.factor}")
    if MIXED_WINDOW_KEY in request.heatmap_windows and not factor_definition.supports_mixed_windows:
        raise HTTPException(status_code=400, detail=f"{factor_definition.label} 不支持多窗口合成")

    started_at = time.perf_counter()
    end_date = _resolve_analysis_end_date(request.pool, request.end_date, db)
    if request.oos_start_date and request.oos_start_date >= end_date:
        raise HTTPException(status_code=400, detail="样本外起始日期必须早于实际结束日期")
    max_forward_window = max(request.heatmap_forward_windows)
    required_windows = _factor_required_windows(request.heatmap_windows, factor_definition)
    max_factor_window = max(required_windows)
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

    industry_df = (
        _load_industry_frame(
            db,
            universe_history.all_symbols,
            request.start_date - timedelta(days=3650),
            end_date,
        )
        if request.neutralization != "none"
        else None
    )

    heatmap_context = FactorContext(
        windows=required_windows,
        momentum_weights=_normalize_momentum_weights(
            request.momentum_weights,
            required_windows,
        ),
        db=db,
        symbols=universe_history.all_symbols,
        start_date=request.start_date,
        end_date=end_date,
        analysis_dates=analysis_dates,
        industry_df=industry_df,
        candidate_etfs=candidate_etfs,
        weight_history_loader=load_universe_weight_history,
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
        analysis_dates=analysis_dates,
        industry_df=industry_df,
        candidate_etfs=candidate_etfs,
    )
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    factor_analysis["summary"]["elapsed_ms"] = round(elapsed_ms, 1)
    heatmap_metric_meta = HEATMAP_METRIC_OPTIONS.get(request.heatmap_metric, {})
    industry_rows = int(industry_df.height) if industry_df is not None else 0
    neutralization_warning = (
        "行业快照为空，中性化未生效"
        if request.neutralization != "none" and industry_rows == 0
        else None
    )

    symbol_names = load_symbol_name_map(candidate_etfs, db)
    metadata = {
        "pool": request.pool,
        "pool_label": next(item["label"] for item in POOL_OPTIONS if item["key"] == request.pool),
        "candidate_etfs": candidate_etfs,
        "symbol_names": symbol_names,
        "factor": factor_definition.to_option(),
        "factor_direction": factor_definition.direction,
        "factor_direction_label": FACTOR_DIRECTION_OPTIONS.get(
            factor_definition.direction,
            FACTOR_DIRECTION_OPTIONS["exploratory"],
        )["label"],
        "factor_direction_adjusted": factor_definition.direction == "lower_is_better",
        "neutralization": request.neutralization,
        "neutralization_label": NEUTRALIZATION_OPTIONS[request.neutralization]["label"],
        "neutralization_effective": request.neutralization != "none" and industry_rows > 0,
        "neutralization_warning": neutralization_warning,
        "standardization": request.standardization,
        "standardization_label": STANDARDIZATION_OPTIONS[request.standardization]["label"],
        "heatmap_metric": request.heatmap_metric,
        "heatmap_metric_label": heatmap_metric_meta.get("label", request.heatmap_metric),
        "heatmap_metric_kind": heatmap_metric_meta.get("kind", "number"),
        "windows": selected_combo["windows"],
        "forward_window": selected_combo["forward_window"],
        "selected_combo": selected_combo,
        "bucket_count": request.bucket_count,
        "min_listing_days": request.min_listing_days,
        "oos_start_date": request.oos_start_date.isoformat() if request.oos_start_date else None,
        "analysis_scope": "in_sample" if request.oos_start_date else "full",
        "analysis_scope_label": "样本内" if request.oos_start_date else "全样本",
        "start_date": request.start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "universe_symbols": len(universe_history.all_symbols),
        "holdings_date_count": universe_history.holdings_date_count,
        "industry_rows": industry_rows,
        "industry_snapshot_mode": "latest_snapshot" if industry_rows else None,
        "price_rows": int(price_df.height),
        "engine": "polars",
    }

    return {
        "metadata": metadata,
        "summary": factor_analysis["summary"],
        "bucket_returns": _records(factor_analysis["bucket_df"]),
        "factor_distribution": factor_analysis["factor_distribution"],
        "rank_ic_series": _records(factor_analysis["ic_df"]),
        "non_overlapping_summary": factor_analysis["non_overlapping_summary"],
        "non_overlapping_offsets": factor_analysis["non_overlapping_offsets"],
        "yearly_stability": _records(factor_analysis["yearly_stability"]),
        "oos_summary": factor_analysis["oos_summary"],
        "oos_bucket_returns": _records(factor_analysis["oos_bucket_df"]),
        "oos_factor_distribution": factor_analysis["oos_factor_distribution"],
        "oos_rank_ic_series": _records(factor_analysis["oos_ic_df"]),
        "oos_non_overlapping_summary": factor_analysis["oos_non_overlapping_summary"],
        "oos_non_overlapping_offsets": factor_analysis["oos_non_overlapping_offsets"],
        "oos_yearly_stability": _records(factor_analysis["oos_yearly_stability"]),
        "parameter_heatmap": heatmap_records,
    }


@router.get("/options", response_model=FactorLabOptionsResponse)
async def get_factor_lab_options(
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    timing_target_names = load_symbol_name_map(
        [item.get("value") for item in DEFAULT_TIMING_TARGET_OPTIONS],
        db,
    )
    timing_target_options = [
        {
            **item,
            "label": format_symbol_label(item.get("value"), timing_target_names) or item.get("label"),
            "name": timing_target_names.get(normalize_symbol_for_name(item.get("value")) or ""),
        }
        for item in DEFAULT_TIMING_TARGET_OPTIONS
    ]
    return FactorLabOptionsResponse(
        pools=POOL_OPTIONS,
        factors=[definition.to_option() for definition in FACTOR_REGISTRY.values()],
        windows=SUPPORTED_WINDOWS,
        forward_windows=DEFAULT_FORWARD_WINDOWS,
        heatmap_metrics=[
            {"key": key, **value}
            for key, value in HEATMAP_METRIC_OPTIONS.items()
        ],
        timing_heatmap_metrics=[
            {"key": key, **value}
            for key, value in TIMING_HEATMAP_METRIC_OPTIONS.items()
        ],
        timing_fear_sources=_get_timing_fear_sources(db),
        timing_target_options=timing_target_options,
        backtest_search_objectives=[
            {"key": key, **value}
            for key, value in BACKTEST_SEARCH_OBJECTIVE_OPTIONS.items()
        ],
        neutralization_options=[
            {"key": key, **value}
            for key, value in NEUTRALIZATION_OPTIONS.items()
        ],
        standardization_options=[
            {"key": key, **value}
            for key, value in STANDARDIZATION_OPTIONS.items()
        ],
        default_request={
            "pool": "QQQ",
            "factor": "risk_adjusted_momentum",
            "bucket_count": 10,
            "start_date": DEFAULT_START_DATE.isoformat(),
            "neutralization": DEFAULT_NEUTRALIZATION,
            "standardization": DEFAULT_STANDARDIZATION,
            "oos_start_date": DEFAULT_OOS_START_DATE.isoformat(),
            "heatmap_metric": DEFAULT_HEATMAP_METRIC,
            "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS,
            "min_listing_days": DEFAULT_MIN_LISTING_DAYS,
            "include_heatmap": True,
            "heatmap_windows": SUPPORTED_WINDOWS,
            "heatmap_forward_windows": DEFAULT_FORWARD_WINDOWS,
        },
        default_composite_request={
            "pool": "QQQ",
            "bucket_count": 10,
            "start_date": DEFAULT_START_DATE.isoformat(),
            "oos_start_date": DEFAULT_OOS_START_DATE.isoformat(),
            "forward_window": 20,
            "min_listing_days": DEFAULT_MIN_LISTING_DAYS,
            "legs": [
                {
                    "factor": "risk_adjusted_momentum",
                    "window": MIXED_WINDOW_KEY,
                    "weight": 0.7,
                    "neutralization": DEFAULT_NEUTRALIZATION,
                    "standardization": "rank_percentile",
                    "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS,
                },
                {
                    "factor": "volume_z",
                    "window": 20,
                    "weight": 0.3,
                    "neutralization": DEFAULT_NEUTRALIZATION,
                    "standardization": "rank_percentile",
                    "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS,
                },
            ],
        },
        default_backtest_request={
            "pool": "QQQ",
            "custom_symbols": [],
            "position_weights": [],
            "rotation_mode": DEFAULT_ROTATION_MODE,
            "start_date": DEFAULT_START_DATE.isoformat(),
            "end_date": None,
            "oos_start_date": DEFAULT_OOS_START_DATE.isoformat(),
            "initial_capital": 100_000.0,
            "max_positions": 7,
            "sell_rank_multiplier": DEFAULT_SELL_RANK_MULTIPLIER,
            "rebalance_frequency": DEFAULT_REBALANCE_FREQUENCY,
            "commission_pct": 0.03,
            "slippage_pct": 0.02,
            "lot_size": 1,
            "min_listing_days": DEFAULT_MIN_LISTING_DAYS,
            "legs": [
                {
                    "factor": "risk_adjusted_momentum",
                    "window": MIXED_WINDOW_KEY,
                    "weight": 0.6,
                    "neutralization": DEFAULT_NEUTRALIZATION,
                    "standardization": "rank_percentile",
                    "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS,
                },
                {
                    "factor": "index_weight",
                    "window": 20,
                    "weight": 0.4,
                    "neutralization": DEFAULT_NEUTRALIZATION,
                    "standardization": "rank_percentile",
                    "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS,
                },
            ],
        },
        default_timing_request={
            "target_symbol": "SOXL.US",
            "fear_symbol": CNN_HISTORY_SYMBOL,
            "ma_window": 1,
            "bucket_count": 10,
            "start_date": DEFAULT_START_DATE.isoformat(),
            "end_date": None,
            "forward_window": 20,
            "heatmap_metric": DEFAULT_TIMING_HEATMAP_METRIC,
            "heatmap_forward_windows": DEFAULT_FORWARD_WINDOWS,
            "heatmap_ma_windows": DEFAULT_TIMING_MA_WINDOWS,
            "include_heatmap": True,
        },
    )


@router.get("/live-configs")
async def list_factor_live_trading_configs(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
    external_db: ORMSession = Depends(get_external_trading_db),
):
    rows = (
        db.query(FactorLiveTradingConfig)
        .filter(FactorLiveTradingConfig.account_id == account_id)
        .order_by(FactorLiveTradingConfig.updated_at.desc(), FactorLiveTradingConfig.id.desc())
        .all()
    )
    return [_serialize_factor_live_config(row, db, external_db) for row in rows]


@router.post("/live-configs")
async def create_factor_live_trading_config(
    payload: FactorLiveTradingConfigPayload,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
    external_db: ORMSession = Depends(get_external_trading_db),
):
    config = FactorLiveTradingConfig(account_id=account_id)
    _update_factor_live_config_from_payload(config, payload)
    db.add(config)
    db.flush()
    _bind_factor_live_sub_account(external_db, config)
    external_db.commit()
    db.commit()
    db.refresh(config)
    return _serialize_factor_live_config(config, db, external_db)


@router.get("/live-configs/{config_id}")
async def get_factor_live_trading_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
    external_db: ORMSession = Depends(get_external_trading_db),
):
    return _serialize_factor_live_config(_get_factor_live_config_or_404(db, account_id, config_id), db, external_db)


@router.put("/live-configs/{config_id}")
async def update_factor_live_trading_config(
    config_id: int,
    payload: FactorLiveTradingConfigPayload,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
    external_db: ORMSession = Depends(get_external_trading_db),
):
    config = _get_factor_live_config_or_404(db, account_id, config_id)
    previous_sub_account_id = config.live_sub_account_id
    _update_factor_live_config_from_payload(config, payload)
    _bind_factor_live_sub_account(external_db, config, previous_sub_account_id=previous_sub_account_id)
    external_db.commit()
    db.commit()
    db.refresh(config)
    return _serialize_factor_live_config(config, db, external_db)


@router.delete("/live-configs/{config_id}")
async def delete_factor_live_trading_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
    external_db: ORMSession = Depends(get_external_trading_db),
):
    config = _get_factor_live_config_or_404(db, account_id, config_id)
    if config.live_sub_account_id:
        sub_account = external_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == account_id,
        ).first()
        if sub_account:
            sync_target_positions(external_db, sub_account=sub_account, targets=[], signal_id=f"factor_live:{config.id}:deleted")
            if sub_account.strategy_type == STRATEGY_FACTOR_LIVE and sub_account.strategy_config_id == config.id:
                sub_account.strategy_type = None
                sub_account.strategy_config_id = None
                sub_account.updated_at = datetime.now()
    db.query(FactorLiveTradingLog).filter(FactorLiveTradingLog.config_id == config.id).delete(synchronize_session=False)
    db.delete(config)
    external_db.commit()
    db.commit()
    return {"message": "Deleted successfully"}


@router.get("/live-configs/{config_id}/logs")
async def list_factor_live_trading_logs(
    config_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    _get_factor_live_config_or_404(db, account_id, config_id)
    rows = (
        db.query(FactorLiveTradingLog)
        .filter(
            FactorLiveTradingLog.config_id == config_id,
            FactorLiveTradingLog.account_id == account_id,
        )
        .order_by(FactorLiveTradingLog.timestamp.desc(), FactorLiveTradingLog.id.desc())
        .limit(limit)
        .all()
    )
    return [_serialize_factor_live_log(row) for row in rows]


@router.post("/live-configs/{config_id}/signal")
async def generate_factor_live_trading_signal(
    config_id: int,
    payload: Optional[FactorLiveSignalRequest] = None,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
    external_db: ORMSession = Depends(get_external_trading_db),
):
    config = _get_factor_live_config_or_404(db, account_id, config_id)
    payload = payload or FactorLiveSignalRequest()
    if payload.signal_date:
        requested_signal_date = payload.signal_date
    else:
        try:
            requested_signal_date = datetime.now(ZoneInfo(config.signal_timezone or "Asia/Shanghai")).date()
        except ZoneInfoNotFoundError:
            requested_signal_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    signal_date, latest_trading_date = _factor_live_resolve_requested_signal_date(config, requested_signal_date)
    if not signal_date:
        raise HTTPException(
            status_code=400,
            detail=_factor_live_not_signal_day_message(config, requested_signal_date, latest_trading_date),
        )
    try:
        plan = _build_factor_live_signal_plan(
            db,
            external_db,
            config,
            signal_date=signal_date,
            rank_limit=payload.rank_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    signal_date_value = date.fromisoformat(plan["signal_date"])
    if not plan.get("is_signal_day"):
        raise HTTPException(status_code=400, detail=plan.get("message") or "当前不是信号日")
    config.last_signal_date = signal_date_value
    config.last_signal_at = datetime.now()
    config.last_signal_status = plan.get("status")
    config.last_signal_message = plan.get("message")
    config.last_signal_payload = jsonable_encoder(plan)
    config.updated_at = datetime.now()
    _add_factor_live_log(
        db,
        config,
        action="SIGNAL",
        status=plan.get("status") or "OK",
        message=plan.get("message"),
        payload=plan,
        signal_date=signal_date_value,
    )
    db.commit()
    db.refresh(config)
    return {"config": _serialize_factor_live_config(config, db, external_db), "signal": plan}


@router.post("/live-configs/{config_id}/execute")
async def execute_factor_live_trading_signal(
    config_id: int,
    payload: Optional[FactorLiveExecutionRequest] = None,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
    external_db: ORMSession = Depends(get_external_trading_db),
):
    config = _get_factor_live_config_or_404(db, account_id, config_id)
    payload = payload or FactorLiveExecutionRequest()
    config_snapshot = _factor_live_config_snapshot(config)
    db.expunge(config)
    db.rollback()
    result = await _execute_factor_live_signal(
        None,
        external_db,
        config_snapshot,
        signal_date=payload.signal_date,
        trigger_executor=payload.trigger_executor,
        force=payload.force,
        timeout_seconds=payload.timeout_seconds,
    )
    config = _get_factor_live_config_or_404(db, account_id, config_id)
    signal_date_value = _normalize_signal_payload_date(result.get("signal") or {})
    config.last_execution_signal_date = signal_date_value
    config.last_execution_at = datetime.now()
    config.last_execution_status = result.get("status")
    config.last_execution_message = result.get("message")
    config.last_execution_payload = jsonable_encoder(result)
    config.updated_at = datetime.now()
    _add_factor_live_log(
        db,
        config,
        action="EXECUTE",
        status=result.get("status") or "OK",
        message=result.get("message"),
        payload=result,
        signal_date=signal_date_value,
    )
    db.commit()
    db.refresh(config)
    return {"config": _serialize_factor_live_config(config, db, external_db), "execution": result}


@router.post("/analyze")
async def analyze_factor(
    payload: FactorLabAnalyzeRequest,
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _run_factor_analysis(payload, db)


@router.post("/analyze-composite")
async def analyze_composite_factor(
    payload: CompositeFactorAnalyzeRequest,
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _run_composite_factor_analysis(payload, db)


@router.post("/analyze-timing")
async def analyze_timing_factor(
    payload: TimingFactorAnalyzeRequest,
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _run_timing_factor_analysis(payload, db)


@router.post("/backtest")
async def backtest_factor_strategy(
    payload: FactorBacktestRequest,
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _run_factor_backtest(payload, db)


@router.post("/backtest-search/start")
async def start_factor_backtest_search(
    payload: FactorBacktestSearchRequest,
    account_id: str = Depends(valid_account),
):
    return _start_backtest_search_job(payload, account_id)


@router.get("/backtest-search/status")
async def get_factor_backtest_search_status(
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _serialize_backtest_search_status(db)


@router.get("/backtest-search/history")
async def get_factor_backtest_search_history(
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _serialize_backtest_search_status(db)


@router.get("/backtest-search/results")
async def get_factor_backtest_search_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    sort_field: Optional[str] = None,
    sort_order: Optional[str] = None,
    filters: Optional[str] = None,
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    return _query_backtest_search_results(
        db,
        page=page,
        page_size=page_size,
        sort_field=sort_field,
        sort_order=sort_order,
        raw_filters=filters,
    )


@router.post("/backtest-search/cancel")
async def cancel_factor_backtest_search_job(
    _: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    active_thread_alive = False
    with BACKTEST_SEARCH_JOBS_LOCK:
        if BACKTEST_SEARCH_ACTIVE_THREAD:
            active_thread_alive = BACKTEST_SEARCH_ACTIVE_THREAD.is_alive()
        if (
            BACKTEST_SEARCH_ACTIVE_JOB
            and BACKTEST_SEARCH_ACTIVE_JOB.get("status") in {"queued", "running"}
            and active_thread_alive
        ):
            BACKTEST_SEARCH_ACTIVE_JOB["cancel_requested"] = True

    state = _get_backtest_search_state(db)
    if state and state.status in {"queued", "running"}:
        if active_thread_alive:
            state.cancel_requested = True
        else:
            state.status = "cancelled"
            state.cancel_requested = False
            state.current_case = None
            state.finished_at = datetime.now()
        state.updated_at = datetime.now()
        db.commit()
    return _serialize_backtest_search_status(db)
