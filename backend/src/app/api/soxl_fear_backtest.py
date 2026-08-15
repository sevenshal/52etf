import logging
import os
import re
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from heapq import heappush, heapreplace
from itertools import product
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, validator

from ...core.database import ETFFearGreedCloneHistory, Session
from ...core.event_stream import publish_event
from ...core.services.factor_backtest_engine import load_price_frame
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from ...robot.a_stock_base_data_config import (
    A_STOCK_ETF_DAILY_NAMES,
    A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS,
    A_STOCK_INDEX_FEAR_GREED_TARGETS,
)
from ...robot.cnn_fear_index import CNN_HISTORY_SYMBOL
from .account import valid_account

router = APIRouter(prefix="/api/fear-volume-backtest", tags=["Fear Volume Backtest"])
logger = logging.getLogger(__name__)
SEARCH_JOBS: Dict[str, Dict] = {}
SEARCH_JOBS_LOCK = threading.Lock()
SEARCH_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="soxl-fear-search")
SEARCH_EVAL_MAX_WORKERS = min(8, max(2, (os.cpu_count() or 4) // 2))
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_*.-]+$")
SIGNAL_LOOKBACK_DAYS = 90
VOLUME_LOOKBACK_DAYS = 20
MAX_VOLUME_RATIO_CONSECUTIVE_DAYS = 20
A_STOCK_INNO100_FEAR_SYMBOL = "INNO100.CN"
A_STOCK_FEAR_VOLUME_EXTRA_TARGET_ETFS = ("501225.SH",)

US_TARGET_OPTIONS = [
    {"label": "SOXL.US", "value": "SOXL.US", "market": "us"},
    {"label": "TQQQ.US", "value": "TQQQ.US", "market": "us"},
    {"label": "UPRO.US", "value": "UPRO.US", "market": "us"},
    {"label": "SOXX.US", "value": "SOXX.US", "market": "us"},
    {"label": "QQQ.US", "value": "QQQ.US", "market": "us"},
    {"label": "SPY.US", "value": "SPY.US", "market": "us"},
]


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _volume_ratio_consecutive_column(days: int) -> str:
    return f"volume_ratio_consecutive_{days}"


def _volume_ma_excluding_recent_column(days: int) -> str:
    return f"volume_ma20_excluding_recent_{days}"


def _fear_source_key_for_symbol(symbol: str) -> str:
    normalized = _normalize_symbol(symbol)
    return "a_stock_" + re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")


def _a_stock_fear_label(target: Dict[str, Any]) -> str:
    return f"{target.get('ticker') or target.get('label') or target['symbol']} 指数贪恐"


def _build_a_stock_fear_sources() -> Dict[str, Dict[str, Any]]:
    inno100_target = {
        "symbol": A_STOCK_INNO100_FEAR_SYMBOL,
        "ticker": "A创100",
        "label": "A创100",
    }
    sources: Dict[str, Dict[str, Any]] = {}
    for target in [inno100_target, *A_STOCK_INDEX_FEAR_GREED_TARGETS]:
        symbol = _normalize_symbol(target["symbol"])
        sources[_fear_source_key_for_symbol(symbol)] = {
            "label": _a_stock_fear_label(target),
            "column": "etf_fear_greed",
            "symbol": symbol,
            "market": "a_stock",
        }
    return sources


def _build_a_stock_target_options() -> List[Dict[str, Any]]:
    options = []
    for symbol in [*A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS, *A_STOCK_FEAR_VOLUME_EXTRA_TARGET_ETFS]:
        normalized = _normalize_symbol(symbol)
        name = A_STOCK_ETF_DAILY_NAMES.get(normalized, normalized)
        options.append({
            "label": f"{name} {normalized}",
            "value": normalized,
            "market": "a_stock",
        })
    return options


def _build_a_stock_preset_pairs() -> List[Dict[str, Any]]:
    pairs = []
    for target in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        etf_symbol = target.get("proxy_etf")
        if not etf_symbol:
            continue
        fear_symbol = _normalize_symbol(target["symbol"])
        target_symbol = _normalize_symbol(etf_symbol)
        target_label = f"{A_STOCK_ETF_DAILY_NAMES.get(target_symbol, target_symbol)} {target_symbol}"
        fear_label = _a_stock_fear_label(target)
        pairs.append({
            "key": f"{target_symbol}:{fear_symbol}",
            "target_symbol": target_symbol,
            "target_label": target_label,
            "fear_source": _fear_source_key_for_symbol(fear_symbol),
            "fear_symbol": fear_symbol,
            "fear_label": fear_label,
            "label": f"{target_label} × {fear_label}",
        })
    return pairs


A_STOCK_FEAR_SOURCE_OPTIONS = _build_a_stock_fear_sources()
A_STOCK_TARGET_OPTIONS = _build_a_stock_target_options()
A_STOCK_PRESET_PAIRS = _build_a_stock_preset_pairs()

FEAR_SOURCE_OPTIONS = {
    "cnn": {
        "label": "CNN贪恐",
        "column": "cnn_fear_greed",
        "market": "us",
    },
    "soxx_clone": {
        "label": "SOXX 半导体自算贪恐",
        "column": "etf_fear_greed",
        "symbol": "SOXX.US",
        "market": "us",
    },
    "spy_clone": {
        "label": "SPY 标普500自算贪恐",
        "column": "etf_fear_greed",
        "symbol": "SPY.US",
        "market": "us",
    },
    "qqq_clone": {
        "label": "QQQ 纳指100自算贪恐",
        "column": "etf_fear_greed",
        "symbol": "QQQ.US",
        "market": "us",
    },
    "dia_clone": {
        "label": "DIA 道琼斯自算贪恐",
        "column": "etf_fear_greed",
        "symbol": "DIA.US",
        "market": "us",
    },
    **A_STOCK_FEAR_SOURCE_OPTIONS,
}

TARGET_OPTIONS = [*US_TARGET_OPTIONS, *A_STOCK_TARGET_OPTIONS]


class SOXLFearStrategyParams(BaseModel):
    buy_threshold: float = 40.0
    greed_threshold: float = 41.0
    volume_ratio_threshold: float = 1.38
    volume_ratio_consecutive_days: int = 1
    buy_position_pct: float = 60.0
    cooldown_days: int = 10
    trailing_stop_pct: float = 5.0
    sell_position_pct: float = 50.0
    sell_reduction_basis: str = "portfolio"
    sell_price_above_avg_cost: bool = True
    max_take_profit_sells_per_cycle: int = 2
    min_position_pct_after_take_profit: float = 5.0
    rebalance_threshold_pct: float = 5.0
    # True = 信号日收盘决策，次日开盘成交；False = 信号日收盘价成交（默认，保持旧行为）
    execute_next_open: bool = False
    # 跷跷板候补标的（可选）：主标的空仓时，候补极恐放量则买入候补；主标的出信号立即换回。
    sub_symbol: Optional[str] = None
    sub_fear_source: str = "cnn"
    sub_volume_signal_symbol: Optional[str] = None
    sub_buy_threshold: float = 25.0
    sub_volume_ratio_threshold: float = 1.6
    # 第二候补（可选）：三标的对称轮动用（与 sub 一起参与空仓买最恐慌/换仓）
    sub2_symbol: Optional[str] = None
    sub2_fear_source: str = "cnn"
    sub2_volume_signal_symbol: Optional[str] = None
    sub2_buy_threshold: float = 20.0
    sub2_volume_ratio_threshold: float = 1.3
    # 换仓阈值（可选）：None=主辅跷跷板模式；有值=对称双轮动（多标的）——
    # 空仓时任一标的极恐放量都买（都触发买更恐慌的）；持有 X 时若 X 恐贪 > 该阈值且另一标的有买入信号则换仓。
    swap_threshold: Optional[float] = None

    @validator("sub2_buy_threshold")
    def validate_sub2_buy_threshold(cls, value):
        if value < 0 or value > 100:
            raise ValueError("第二候补恐慌阈值必须在 0 到 100 之间")
        return value

    @validator("sub2_volume_ratio_threshold")
    def validate_sub2_volume_ratio_threshold(cls, value):
        if value <= 0 or value > 20:
            raise ValueError("第二候补量比阈值必须大于 0 且不超过 20")
        return value

    @validator("sub2_fear_source")
    def validate_sub2_fear_source(cls, value):
        if value not in FEAR_SOURCE_OPTIONS:
            raise ValueError("第二候补恐贪来源包含不支持的来源")
        return value

    @validator("swap_threshold")
    def validate_swap_threshold(cls, value):
        if value is None:
            return None
        if value < 0 or value > 100:
            raise ValueError("换仓阈值必须在 0 到 100 之间")
        return value

    @validator("sub_buy_threshold")
    def validate_sub_buy_threshold(cls, value):
        if value < 0 or value > 100:
            raise ValueError("候补恐慌阈值必须在 0 到 100 之间")
        return value

    @validator("sub_volume_ratio_threshold")
    def validate_sub_volume_ratio_threshold(cls, value):
        if value <= 0 or value > 20:
            raise ValueError("候补量比阈值必须大于 0 且不超过 20")
        return value

    @validator("sub_fear_source")
    def validate_sub_fear_source(cls, value):
        if value not in FEAR_SOURCE_OPTIONS:
            raise ValueError("候补恐贪来源包含不支持的来源")
        return value

    @validator("buy_position_pct", "sell_position_pct")
    def validate_positive_percent(cls, value):
        if value <= 0 or value > 100:
            raise ValueError("百分比参数必须在 0 到 100 之间")
        return value

    @validator("trailing_stop_pct")
    def validate_trailing_stop_pct(cls, value):
        # 0 = 不等待回撤，到达贪恐阈值（>= greed_threshold）即卖出
        if value < 0 or value > 100:
            raise ValueError("移动止盈回撤必须在 0 到 100 之间（0=到达贪恐阈值即卖）")
        return value

    @validator("min_position_pct_after_take_profit")
    def validate_min_position_pct_after_take_profit(cls, value):
        if value < 0 or value > 100:
            raise ValueError("百分比参数必须在 0 到 100 之间")
        return value

    @validator("rebalance_threshold_pct")
    def validate_rebalance_threshold_pct(cls, value):
        if value < 0 or value > 100:
            raise ValueError("调仓阈值必须在 0 到 100 之间")
        return value

    @validator("cooldown_days")
    def validate_cooldown(cls, value):
        if value < 0:
            raise ValueError("冷却天数不能小于 0")
        return value

    @validator("buy_threshold", "greed_threshold")
    def validate_threshold(cls, value):
        if value < 0 or value > 100:
            raise ValueError("阈值必须在 0 到 100 之间")
        return value

    @validator("max_take_profit_sells_per_cycle")
    def validate_max_take_profit_sells_per_cycle(cls, value):
        if value < 1 or value > 20:
            raise ValueError("同轮止盈最多卖出次数必须在 1 到 20 之间")
        return value

    @validator("volume_ratio_consecutive_days")
    def validate_volume_ratio_consecutive_days(cls, value):
        if value < 1 or value > MAX_VOLUME_RATIO_CONSECUTIVE_DAYS:
            raise ValueError(f"连续量比天数必须在 1 到 {MAX_VOLUME_RATIO_CONSECUTIVE_DAYS} 之间")
        return value

    @validator("sell_reduction_basis")
    def validate_sell_reduction_basis(cls, value):
        if value not in {"portfolio", "holdings"}:
            raise ValueError("止盈减仓口径仅支持 portfolio 或 holdings")
        return value

class SOXLFearSearchParams(BaseModel):
    symbol: str = "SOXL.US"
    volume_signal_symbol: Optional[str] = None
    fear_source_values: List[str] = Field(default_factory=lambda: ["cnn"])
    initial_capital: float = 100000.0
    start_date: str = "2021-01-01"
    end_date: Optional[str] = None
    top_n: int = 20
    objective: str = "annualized_return"
    eval_workers: Optional[int] = None
    rebalance_threshold_pct: float = 5.0
    buy_threshold_values: List[float] = Field(default_factory=lambda: [35.0, 40.0, 45.0])
    greed_threshold_values: List[float] = Field(default_factory=lambda: [40.0, 41.0, 42.0])
    volume_ratio_threshold_values: List[float] = Field(default_factory=lambda: [1.3, 1.38, 1.45])
    volume_ratio_consecutive_days_values: List[int] = Field(default_factory=lambda: [1])
    buy_position_pct_values: List[float] = Field(default_factory=lambda: [50.0, 60.0, 70.0])
    cooldown_days_values: List[int] = Field(default_factory=lambda: [5, 10, 15])
    trailing_stop_pct_values: List[float] = Field(default_factory=lambda: [3.0, 5.0, 7.0])
    sell_position_pct_values: List[float] = Field(default_factory=lambda: [40.0, 50.0, 60.0])
    sell_reduction_basis_values: List[str] = Field(default_factory=lambda: ["portfolio", "holdings"])
    sell_price_above_avg_cost_values: List[bool] = Field(default_factory=lambda: [True, False])
    max_take_profit_sells_per_cycle_values: List[int] = Field(default_factory=lambda: [1, 2, 3])
    min_position_pct_after_take_profit_values: List[float] = Field(default_factory=lambda: [5.0, 10.0, 15.0])
    execute_next_open_values: List[bool] = Field(default_factory=lambda: [False, True])
    # 跷跷板候补（sub_symbol 固定，阈值参与组合搜索）
    sub_symbol: Optional[str] = None
    sub_fear_source: str = "cnn"
    sub_volume_signal_symbol: Optional[str] = None
    sub_buy_threshold_values: List[float] = Field(default_factory=lambda: [25.0])
    sub_volume_ratio_threshold_values: List[float] = Field(default_factory=lambda: [1.6])
    # 换仓阈值候选：None（关闭=主辅跷跷板）或 0~100（对称双轮动）
    swap_threshold_values: List[Optional[float]] = Field(default_factory=lambda: [None, 45.0])
    # 第二候补（固定标的 + 阈值参与组合搜索）
    sub2_symbol: Optional[str] = None
    sub2_fear_source: str = "cnn"
    sub2_volume_signal_symbol: Optional[str] = None
    sub2_buy_threshold_values: List[float] = Field(default_factory=lambda: [20.0])
    sub2_volume_ratio_threshold_values: List[float] = Field(default_factory=lambda: [1.3])

    @validator("sub2_symbol")
    def validate_sub2_symbol(cls, value):
        if not value:
            return None
        symbol = _normalize_symbol(value)
        if not SYMBOL_PATTERN.match(symbol):
            raise ValueError("sub2_symbol 格式不正确")
        return symbol

    @validator("sub2_volume_signal_symbol")
    def validate_sub2_volume_signal_symbol(cls, value):
        if not value:
            return None
        symbol = _normalize_symbol(value)
        if not SYMBOL_PATTERN.match(symbol):
            raise ValueError("sub2_volume_signal_symbol 格式不正确")
        return symbol

    @validator("sub2_fear_source")
    def validate_search_sub2_fear_source(cls, value):
        if value not in FEAR_SOURCE_OPTIONS:
            raise ValueError("第二候补恐贪来源包含不支持的来源")
        return value

    @validator("sub2_buy_threshold_values", "sub2_volume_ratio_threshold_values")
    def validate_sub2_threshold_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("第二候补阈值候选至少一个值")
        return normalized

    @validator("sub2_volume_ratio_threshold_values")
    def validate_sub2_vr_values(cls, value):
        for item in value:
            if item <= 0 or item > 20:
                raise ValueError("第二候补量比阈值必须大于 0 且不超过 20")
        return value

    @validator("sub2_buy_threshold_values")
    def validate_sub2_buy_values(cls, value):
        for item in value:
            if item < 0 or item > 100:
                raise ValueError("第二候补恐慌阈值必须在 0 到 100 之间")
        return value

    @validator("sub_buy_threshold_values", "sub_volume_ratio_threshold_values")
    def validate_sub_threshold_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("候补阈值候选至少一个值")
        return normalized

    @validator("sub_volume_ratio_threshold_values")
    def validate_sub_vr_values(cls, value):
        for item in value:
            if item <= 0 or item > 20:
                raise ValueError("候补量比阈值必须大于 0 且不超过 20")
        return value

    @validator("sub_buy_threshold_values")
    def validate_sub_buy_values(cls, value):
        for item in value:
            if item < 0 or item > 100:
                raise ValueError("候补恐慌阈值必须在 0 到 100 之间")
        return value

    @validator("swap_threshold_values")
    def validate_swap_threshold_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("换仓阈值候选至少一个值（用 null 表示关闭）")
        for item in normalized:
            if item is not None and (item < 0 or item > 100):
                raise ValueError("换仓阈值必须在 0 到 100 之间或为空")
        return normalized

    @validator("sub_symbol")
    def validate_sub_symbol(cls, value):
        if not value:
            return None
        symbol = _normalize_symbol(value)
        if not SYMBOL_PATTERN.match(symbol):
            raise ValueError("sub_symbol 格式不正确")
        return symbol

    @validator("sub_volume_signal_symbol")
    def validate_sub_volume_signal_symbol(cls, value):
        if not value:
            return None
        symbol = _normalize_symbol(value)
        if not SYMBOL_PATTERN.match(symbol):
            raise ValueError("sub_volume_signal_symbol 格式不正确")
        return symbol

    @validator("sub_fear_source")
    def validate_search_sub_fear_source(cls, value):
        if value not in FEAR_SOURCE_OPTIONS:
            raise ValueError("候补恐贪来源包含不支持的来源")
        return value

    @validator("symbol")
    def validate_symbol(cls, value):
        symbol = _normalize_symbol(value)
        if not symbol or not SYMBOL_PATTERN.match(symbol):
            raise ValueError("symbol 格式不正确")
        return symbol

    @validator("volume_signal_symbol")
    def validate_volume_signal_symbol(cls, value):
        if not value:
            return None
        symbol = _normalize_symbol(value)
        if not SYMBOL_PATTERN.match(symbol):
            raise ValueError("volume_signal_symbol 格式不正确")
        return symbol

    @validator("volume_ratio_consecutive_days_values", each_item=True)
    def validate_volume_ratio_consecutive_days_values(cls, value):
        if value < 1 or value > MAX_VOLUME_RATIO_CONSECUTIVE_DAYS:
            raise ValueError(f"连续量比天数必须在 1 到 {MAX_VOLUME_RATIO_CONSECUTIVE_DAYS} 之间")
        return value

    @validator("top_n")
    def validate_top_n(cls, value):
        if value <= 0 or value > 500:
            raise ValueError("top_n 必须在 1 到 500 之间")
        return value

    @validator("objective")
    def validate_objective(cls, value):
        if value not in {"annualized_return", "sharpe_ratio"}:
            raise ValueError("objective 仅支持 annualized_return 或 sharpe_ratio")
        return value

    @validator("fear_source_values")
    def validate_fear_source_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("至少选择一个贪恐来源")
        invalid = [item for item in normalized if item not in FEAR_SOURCE_OPTIONS]
        if invalid:
            raise ValueError("fear_source_values 包含不支持的来源")
        return normalized

    @validator("sell_price_above_avg_cost_values")
    def validate_sell_price_above_avg_cost_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("至少选择一个卖出价高于均价开关")
        return normalized

    @validator("execute_next_open_values")
    def validate_execute_next_open_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("至少选择一个次日开盘成交开关")
        return normalized

    @validator("eval_workers")
    def validate_eval_workers(cls, value):
        if value is None:
            return value
        if value < 1 or value > 16:
            raise ValueError("eval_workers 必须在 1 到 16 之间")
        return value

    @validator("rebalance_threshold_pct")
    def validate_search_rebalance_threshold_pct(cls, value):
        if value < 0 or value > 100:
            raise ValueError("调仓阈值必须在 0 到 100 之间")
        return value


class SOXLFearSearchJobCreated(BaseModel):
    task_id: str
    status: str
    total_combinations: int


class SOXLFearSearchJobStatus(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    processed_combinations: int = 0
    total_combinations: int = 0
    skipped_combinations: int = 0
    message: Optional[str] = None
    result: Optional[Dict] = None
    error: Optional[str] = None


def _parse_date(value: Optional[str], default: Optional[date] = None) -> date:
    if not value:
        if default is None:
            raise ValueError("日期不能为空")
        return default
    return datetime.strptime(value, "%Y-%m-%d").date()


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    return float(value)


def _round_or_none(value, digits: int = 4):
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return round(numeric, digits)


def _floor_share_count(value: float) -> int:
    if value <= 0:
        return 0
    return int(np.floor(value))


def _ceil_share_count(value: float) -> int:
    if value <= 0:
        return 0
    return int(np.ceil(value))


def _describe_df_range(df: pd.DataFrame, date_col: str = "date") -> str:
    if df is None or df.empty or date_col not in df.columns:
        return "0 rows"
    return f"{len(df)} rows, {df.iloc[0][date_col]} ~ {df.iloc[-1][date_col]}"


def _symbol_label(symbol: str) -> str:
    normalized = _normalize_symbol(symbol)
    option = next((item for item in TARGET_OPTIONS if _normalize_symbol(item.get("value")) == normalized), None)
    if option:
        return str(option.get("label") or normalized)
    return f"{A_STOCK_ETF_DAILY_NAMES.get(normalized, normalized)} {normalized}" if not normalized.endswith(".US") else normalized


def _normalize_price_dates(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame.sort_values("date").reset_index(drop=True)


def _fetch_cnn_history(start_date: date, end_date: date) -> pd.DataFrame:
    db = Session()
    try:
        rows = (
            db.query(ETFFearGreedCloneHistory)
            .filter(
                ETFFearGreedCloneHistory.symbol == CNN_HISTORY_SYMBOL,
                ETFFearGreedCloneHistory.date >= start_date,
                ETFFearGreedCloneHistory.date <= end_date,
            )
            .order_by(ETFFearGreedCloneHistory.date.asc())
            .all()
        )
        records = [
            {
                "date": row.date,
                "cnn_fear_greed": float(row.score),
            }
            for row in rows
            if row.score is not None
        ]
    finally:
        Session.remove()

    df = pd.DataFrame(records)
    if df.empty:
        raise ValueError(
            "指定区间内没有 CNN 恐贪数据，请先执行 CNN Fear & Greed 抓取任务"
        )
    return df.drop_duplicates(subset=["date"], keep="last").sort_values("date")


def _fetch_etf_clone_history(fear_source: str, start_date: date, end_date: date) -> pd.DataFrame:
    source_config = FEAR_SOURCE_OPTIONS[fear_source]
    symbol = source_config.get("symbol")
    if not symbol:
        raise ValueError(f"{source_config['label']} 未配置贪恐标的")

    db = Session()
    try:
        rows = (
            db.query(ETFFearGreedCloneHistory)
            .filter(
                ETFFearGreedCloneHistory.symbol == symbol,
                ETFFearGreedCloneHistory.date >= start_date,
                ETFFearGreedCloneHistory.date <= end_date,
            )
            .order_by(ETFFearGreedCloneHistory.date.asc())
            .all()
        )
        records = [
            {
                "date": row.date,
                "etf_fear_greed": float(row.score),
            }
            for row in rows
            if row.score is not None
        ]
    finally:
        Session.remove()

    df = pd.DataFrame(records)
    if df.empty:
        raise ValueError(
            f"指定区间内没有 {source_config['label']} 数据，请先执行 {symbol} 贪恐回跑入库"
        )
    return df.drop_duplicates(subset=["date"], keep="last").sort_values("date")


def _fetch_fear_history(
    fear_source: str,
    start_date: date,
    end_date: date,
) -> Tuple[pd.DataFrame, Dict]:
    if fear_source not in FEAR_SOURCE_OPTIONS:
        raise ValueError("fear_source 包含不支持的来源")

    source_config = FEAR_SOURCE_OPTIONS[fear_source]
    if fear_source == "cnn":
        df = _fetch_cnn_history(start_date, end_date)
    else:
        df = _fetch_etf_clone_history(fear_source, start_date, end_date)

    score_column = source_config["column"]
    df = df.rename(columns={score_column: "fear_greed"})
    df["fear_greed"] = pd.to_numeric(df["fear_greed"], errors="coerce")
    df = df.dropna(subset=["fear_greed"]).sort_values("date")
    if df.empty:
        raise ValueError(f"{source_config['label']} 在指定区间内没有可用数据")

    return df[["date", "fear_greed"]], {
        "fear_source": fear_source,
        "fear_source_label": source_config["label"],
        "fear_points": int(len(df)),
    }


def _fetch_local_price_history(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    frame = load_price_frame([symbol], start_date, end_date)
    records = frame.to_dicts() if frame is not None and not frame.is_empty() else []
    if not records:
        raise ValueError(f"{symbol} 在本地分析库指定区间内没有行情数据，请先执行对应基础数据同步")

    rows = []
    for item in records:
        trade_date = item.get("trade_date")
        close_value = _safe_float(item.get("close"))
        if close_value is None or close_value <= 0:
            continue
        open_value = _safe_float(item.get("open")) or close_value
        high_value = _safe_float(item.get("high")) or max(open_value, close_value)
        low_value = _safe_float(item.get("low")) or min(open_value, close_value)
        rows.append({
            "date": trade_date,
            "open": open_value,
            "high": high_value,
            "low": low_value,
            "close": close_value,
            "volume": _safe_float(item.get("volume")) or 0.0,
            "turnover": _safe_float(item.get("turnover")) or 0.0,
        })

    df = pd.DataFrame(rows).sort_values("date") if rows else pd.DataFrame()
    if df.empty:
        raise ValueError(f"{symbol} 在本地分析库指定区间内没有可用行情")
    return _normalize_price_dates(df)


def _fetch_price_history(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    symbol = _normalize_symbol(symbol)
    if not symbol.endswith(".US"):
        return _fetch_local_price_history(symbol, start_date, end_date)

    quote_service = QuoteService(LongPortService.get_instance())
    klines = quote_service.get_klines(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
    )
    if not klines:
        raise ValueError(f"{symbol} 在指定区间内没有 K 线数据")

    df = pd.DataFrame([
        {
            "date": item["timestamp"].date() if hasattr(item["timestamp"], "date") else item["timestamp"],
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
            "volume": float(item["volume"]),
            "turnover": float(item["turnover"]),
        }
        for item in klines
    ]).sort_values("date")
    if df.empty:
        raise ValueError(f"{symbol} 在指定区间内没有可用 K 线")
    return _normalize_price_dates(df)


def _fetch_signal_price_history(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    symbol = _normalize_symbol(symbol)
    try:
        return _fetch_local_price_history(symbol, start_date, end_date)
    except ValueError:
        if symbol.endswith(".US"):
            return _fetch_price_history(symbol, start_date, end_date)
        raise


def _prepare_base_dataframe(
    symbol: str,
    start_date: date,
    end_date: date,
    fear_source: str = "cnn",
    volume_signal_symbol: Optional[str] = None,
) -> Tuple[pd.DataFrame, Dict]:
    symbol = _normalize_symbol(symbol)
    signal_symbol = _normalize_symbol(volume_signal_symbol or symbol)
    lookback_start_date = start_date - timedelta(days=SIGNAL_LOOKBACK_DAYS)
    price_df = _fetch_price_history(symbol, lookback_start_date, end_date)
    fear_df, fear_meta = _fetch_fear_history(fear_source, lookback_start_date, end_date)

    price_df = _normalize_price_dates(price_df)
    price_df["ma20"] = pd.to_numeric(price_df["close"], errors="coerce").rolling(20).mean()
    price_df["date_ts"] = pd.to_datetime(price_df["date"])

    if signal_symbol == symbol:
        signal_price_df = price_df[["date", "volume"]].copy()
    else:
        signal_price_df = _fetch_signal_price_history(signal_symbol, lookback_start_date, end_date)[["date", "volume"]].copy()
    signal_price_df = _normalize_price_dates(signal_price_df)
    signal_price_df["signal_volume"] = pd.to_numeric(signal_price_df["volume"], errors="coerce")
    signal_price_df["volume_ma20"] = signal_price_df["signal_volume"].shift(1).rolling(VOLUME_LOOKBACK_DAYS).mean()
    signal_price_df["volume_ratio"] = np.where(
        signal_price_df["volume_ma20"] > 0,
        signal_price_df["signal_volume"] / signal_price_df["volume_ma20"],
        np.nan,
    )
    for days in range(1, MAX_VOLUME_RATIO_CONSECUTIVE_DAYS + 1):
        baseline_column = _volume_ma_excluding_recent_column(days)
        ratio_column = _volume_ratio_consecutive_column(days)
        recent_min_volume = signal_price_df["signal_volume"].rolling(days, min_periods=days).min()
        signal_price_df[baseline_column] = (
            signal_price_df["signal_volume"]
            .shift(days)
            .rolling(VOLUME_LOOKBACK_DAYS, min_periods=VOLUME_LOOKBACK_DAYS)
            .mean()
        )
        signal_price_df[ratio_column] = np.where(
            signal_price_df[baseline_column] > 0,
            recent_min_volume / signal_price_df[baseline_column],
            np.nan,
        )
    signal_price_df["signal_date"] = signal_price_df["date"]
    signal_price_df["date_ts"] = pd.to_datetime(signal_price_df["date"])

    fear_df = fear_df.copy()
    fear_df["fear_date"] = fear_df["date"]
    fear_df["date_ts"] = pd.to_datetime(fear_df["date"])
    fear_df = fear_df.sort_values("date_ts")
    signal_df = pd.merge_asof(
        signal_price_df.sort_values("date_ts"),
        fear_df[["date_ts", "fear_greed", "fear_date"]].sort_values("date_ts"),
        on="date_ts",
        direction="backward",
    )
    signal_df["fear_greed"] = pd.to_numeric(signal_df["fear_greed"], errors="coerce")
    signal_df = signal_df.dropna(subset=["fear_greed", "volume_ma20", "volume_ratio", "signal_volume"])
    signal_df = signal_df[[
        "date_ts",
        "signal_date",
        "fear_date",
        "fear_greed",
        "signal_volume",
        "volume_ma20",
        "volume_ratio",
        *[
            column
            for days in range(1, MAX_VOLUME_RATIO_CONSECUTIVE_DAYS + 1)
            for column in (
                _volume_ma_excluding_recent_column(days),
                _volume_ratio_consecutive_column(days),
            )
        ],
    ]].rename(columns={"date_ts": "signal_ts"})

    merged_df = pd.merge(
        price_df.sort_values("date_ts"),
        signal_df.sort_values("signal_ts"),
        left_on="date_ts",
        right_on="signal_ts",
        how="left",
    )
    merged_df = merged_df[merged_df["date"] >= start_date].sort_values("date").reset_index(drop=True)
    merged_df["fear_greed"] = pd.to_numeric(merged_df["fear_greed"], errors="coerce")
    merged_df["cnn_fear_greed"] = merged_df["fear_greed"]
    merged_df["execution_price"] = pd.to_numeric(merged_df["close"], errors="coerce")
    base_df = merged_df.dropna(
        subset=["fear_greed", "ma20", "volume_ma20", "volume_ratio", "execution_price", "signal_date"]
    ).reset_index(drop=True)

    if base_df.empty:
        diagnostics = (
            f"price={_describe_df_range(price_df)}; "
            f"fear={_describe_df_range(fear_df)}; "
            f"signal_price={_describe_df_range(signal_price_df)}; "
            f"signal={_describe_df_range(signal_df, 'signal_date')}; "
            f"merged={_describe_df_range(merged_df)}; "
            f"merged_non_null_fear={int(merged_df['fear_greed'].notna().sum()) if 'fear_greed' in merged_df else 0}; "
            f"merged_non_null_ma20={int(merged_df['ma20'].notna().sum()) if 'ma20' in merged_df else 0}; "
            f"merged_non_null_volume_ma20={int(merged_df['volume_ma20'].notna().sum()) if 'volume_ma20' in merged_df else 0}"
        )
        raise ValueError(
            f"{symbol} 与 {fear_meta['fear_source_label']} 没有足够重叠的数据区间。"
            f" 请求区间: {start_date} ~ {end_date}。"
            f" 诊断: {diagnostics}"
        )

    meta = {
        "requested_symbol": symbol,
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "effective_start_date": base_df.iloc[0]["date"].isoformat(),
        "effective_end_date": base_df.iloc[-1]["date"].isoformat(),
        "trading_days": int(len(base_df)),
        "cnn_points": int(fear_meta["fear_points"]) if fear_meta["fear_source"] == "cnn" else 0,
        "fear_points": int(fear_meta["fear_points"]),
        "fear_source": fear_meta["fear_source"],
        "fear_source_label": fear_meta["fear_source_label"],
        "price_points": int(len(price_df)),
        "volume_signal_symbol": signal_symbol,
        "volume_signal_label": _symbol_label(signal_symbol),
        "volume_signal_points": int(len(signal_price_df)),
        "execution_price_type": "same_day_close",
        "execution_price_label": "信号日收盘价",
        "signal_lag_label": "使用信号当天数据",
    }
    base_df.attrs["fear_source"] = fear_meta["fear_source"]
    base_df.attrs["fear_source_label"] = fear_meta["fear_source_label"]
    base_df.attrs["symbol"] = symbol
    base_df.attrs["volume_signal_symbol"] = signal_symbol
    base_df.attrs["volume_signal_label"] = _symbol_label(signal_symbol)
    base_df.attrs["execution_price_label"] = "信号日收盘价"
    return base_df, meta


def _prepare_search_dataframes(
    symbol: str,
    start_date: date,
    end_date: date,
    fear_sources: List[str],
    volume_signal_symbol: Optional[str] = None,
    sub_symbol: Optional[str] = None,
    sub_fear_source: Optional[str] = None,
    sub_volume_signal_symbol: Optional[str] = None,
    sub2_symbol: Optional[str] = None,
    sub2_fear_source: Optional[str] = None,
    sub2_volume_signal_symbol: Optional[str] = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Dict], Dict, Optional[pd.DataFrame], Optional[Dict], Optional[pd.DataFrame], Optional[Dict]]:
    base_dfs: Dict[str, pd.DataFrame] = {}
    source_metas: Dict[str, Dict] = {}
    meta_items: List[Dict] = []

    for fear_source in fear_sources:
        base_df, meta = _prepare_base_dataframe(
            symbol,
            start_date,
            end_date,
            fear_source,
            volume_signal_symbol,
        )
        base_dfs[fear_source] = base_df
        source_metas[fear_source] = meta
        meta_items.append(meta)

    # 跷跷板候补数据（可选）
    sub_base_df: Optional[pd.DataFrame] = None
    sub_meta: Optional[Dict] = None
    if sub_symbol:
        sub_base_df, sub_meta = _prepare_base_dataframe(
            sub_symbol,
            start_date,
            end_date,
            sub_fear_source or "cnn",
            sub_volume_signal_symbol,
        )

    # 第二候补数据（可选）
    sub2_base_df: Optional[pd.DataFrame] = None
    sub2_meta: Optional[Dict] = None
    if sub2_symbol:
        sub2_base_df, sub2_meta = _prepare_base_dataframe(
            sub2_symbol,
            start_date,
            end_date,
            sub2_fear_source or "cnn",
            sub2_volume_signal_symbol,
        )

    if not meta_items:
        raise ValueError("没有可用的贪恐来源")

    summary_meta_items: List[Dict] = []
    unique_source_labels: List[str] = []
    seen_sources = set()
    for item in meta_items:
        source_key = str(item["fear_source"])
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        summary_meta_items.append(item)
        unique_source_labels.append(str(item["fear_source_label"]))

    return base_dfs, source_metas, {
        "requested_symbol": symbol,
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "effective_start_date": min(item["effective_start_date"] for item in meta_items),
        "effective_end_date": max(item["effective_end_date"] for item in meta_items),
        "trading_days": sum(int(item["trading_days"]) for item in summary_meta_items),
        "price_points": max(int(item["price_points"]) for item in meta_items),
        "volume_signal_symbol": meta_items[0].get("volume_signal_symbol"),
        "volume_signal_label": meta_items[0].get("volume_signal_label"),
        "volume_signal_points": max(int(item.get("volume_signal_points") or 0) for item in meta_items),
        "execution_price_type": meta_items[0].get("execution_price_type"),
        "execution_price_label": meta_items[0].get("execution_price_label"),
        "signal_lag_label": meta_items[0].get("signal_lag_label"),
        "cnn_points": sum(int(item.get("cnn_points") or 0) for item in summary_meta_items),
        "fear_points": sum(int(item["fear_points"]) for item in summary_meta_items),
        "fear_sources": fear_sources,
        "fear_source_labels": "、".join(unique_source_labels),
        "fear_source_details": meta_items,
        "sub_symbol": sub_symbol,
        "sub_fear_source": sub_fear_source,
        "sub_volume_signal_symbol": sub_volume_signal_symbol,
        "sub_meta": sub_meta,
        "sub2_symbol": sub2_symbol,
        "sub2_fear_source": sub2_fear_source,
        "sub2_volume_signal_symbol": sub2_volume_signal_symbol,
        "sub2_meta": sub2_meta,
    }, sub_base_df, sub_meta, sub2_base_df, sub2_meta


def _build_fear_series_payload(base_dfs: Dict[str, pd.DataFrame]) -> Dict:
    records_by_date: Dict[str, Dict] = {}
    sources = []
    for fear_source, frame in base_dfs.items():
        label = FEAR_SOURCE_OPTIONS[fear_source]["label"]
        sources.append({"key": fear_source, "label": label})
        for _, row in frame.iterrows():
            day = row["date"]
            day_text = day.isoformat() if hasattr(day, "isoformat") else str(day)
            if day_text not in records_by_date:
                records_by_date[day_text] = {"date": day_text}
            records_by_date[day_text][fear_source] = _round_or_none(row.get("fear_greed"), 4)

    return {
        "sources": sources,
        "data": [records_by_date[key] for key in sorted(records_by_date.keys())],
    }


def _compute_yearly_returns(equity_df: pd.DataFrame) -> List[Dict]:
    if equity_df.empty:
        return []
    yearly = []
    grouped = equity_df.groupby(equity_df["date"].str[:4])
    for year, group in grouped:
        start_value = float(group.iloc[0]["value"])
        end_value = float(group.iloc[-1]["value"])
        yearly.append({
            "year": year,
            "return": ((end_value / start_value) - 1) * 100 if start_value > 0 else 0.0,
        })
    return yearly


def _compute_yearly_returns_from_arrays(date_strings: List[str], equity_values: np.ndarray) -> List[Dict]:
    if len(date_strings) == 0 or len(equity_values) == 0:
        return []

    yearly = []
    current_year = date_strings[0][:4]
    start_value = float(equity_values[0])
    last_value = float(equity_values[0])

    for date_str, value in zip(date_strings, equity_values):
        year = date_str[:4]
        numeric_value = float(value)
        if year != current_year:
            yearly.append({
                "year": current_year,
                "return": ((last_value / start_value) - 1) * 100 if start_value > 0 else 0.0,
            })
            current_year = year
            start_value = numeric_value
        last_value = numeric_value

    yearly.append({
        "year": current_year,
        "return": ((last_value / start_value) - 1) * 100 if start_value > 0 else 0.0,
    })
    return yearly


def _merge_yearly_returns(
    strategy_yearly_returns: List[Dict],
    benchmark_yearly_returns: List[Dict],
) -> List[Dict]:
    yearly_map: Dict[str, Dict] = {}

    for item in strategy_yearly_returns:
        year = str(item["year"])
        strategy_return = float(item["return"])
        yearly_map[year] = {
            "year": year,
            "strategy_return": strategy_return,
            "benchmark_return": 0.0,
            "excess_return": strategy_return,
        }

    for item in benchmark_yearly_returns:
        year = str(item["year"])
        benchmark_return = float(item["return"])
        if year not in yearly_map:
            yearly_map[year] = {
                "year": year,
                "strategy_return": 0.0,
                "benchmark_return": benchmark_return,
                "excess_return": -benchmark_return,
            }
        else:
            yearly_map[year]["benchmark_return"] = benchmark_return
            yearly_map[year]["excess_return"] = float(yearly_map[year]["strategy_return"]) - benchmark_return

    return [yearly_map[year] for year in sorted(yearly_map.keys())]


def _compute_yearly_trade_stats(trades: List[Dict]) -> Dict[str, Dict]:
    yearly_trade_stats: Dict[str, Dict] = {}
    for trade in trades:
        year = str(trade.get("date", ""))[:4]
        if len(year) != 4:
            continue
        if year not in yearly_trade_stats:
            yearly_trade_stats[year] = {
                "trade_count": 0,
                "buy_count": 0,
                "sell_count": 0,
            }
        yearly_trade_stats[year]["trade_count"] += 1
        if trade.get("action") == "BUY":
            yearly_trade_stats[year]["buy_count"] += 1
        elif trade.get("action") == "SELL":
            yearly_trade_stats[year]["sell_count"] += 1
    return yearly_trade_stats


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def _compute_max_drawdown_duration_days(date_values: List, equity_values: np.ndarray) -> int:
    if len(date_values) == 0 or len(equity_values) == 0:
        return 0

    values = np.asarray(equity_values, dtype=float)
    peak_index = 0
    peak_value = float(values[0])
    drawdown_start_index = None
    max_duration_days = 0
    tolerance = 1e-10

    for index in range(1, len(values)):
        value = float(values[index])
        if value >= peak_value * (1 - tolerance):
            if drawdown_start_index is not None:
                duration_days = (_to_date(date_values[index]) - _to_date(date_values[drawdown_start_index])).days
                max_duration_days = max(max_duration_days, int(max(0, duration_days)))
                drawdown_start_index = None
            if value >= peak_value:
                peak_value = value
            peak_index = index
            continue

        if drawdown_start_index is None:
            drawdown_start_index = peak_index
        duration_days = (_to_date(date_values[index]) - _to_date(date_values[drawdown_start_index])).days
        max_duration_days = max(max_duration_days, int(max(0, duration_days)))

    return max_duration_days


def _compute_equity_metrics(date_values: List, equity_values: np.ndarray) -> Tuple[Dict, np.ndarray]:
    values = np.asarray(equity_values, dtype=float)
    if len(values) == 0:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_duration_days": 0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "win_rate": 0.0,
            "profit_loss_ratio": None,
            "ending_value": 0.0,
        }, np.array([])

    start_value = float(values[0])
    end_value = float(values[-1])
    total_return = ((end_value / start_value) - 1) * 100 if start_value > 0 else 0.0
    annualized_return = 0.0
    if start_value > 0 and len(values) > 1:
        annualized_return = ((end_value / start_value) ** (252 / len(values)) - 1) * 100

    cumulative_peaks = np.maximum.accumulate(values)
    drawdowns = (values / cumulative_peaks) - 1
    max_drawdown = abs(float(drawdowns.min())) * 100 if len(drawdowns) > 0 else 0.0
    max_drawdown_duration_days = _compute_max_drawdown_duration_days(date_values, values)
    returns = np.diff(values) / values[:-1] if len(values) > 1 else np.array([])
    return_std = float(np.std(returns)) if len(returns) > 1 else 0.0
    annualized_volatility = return_std * float(np.sqrt(252)) * 100

    sharpe_ratio = 0.0
    if len(returns) > 1 and return_std > 0:
        sharpe_ratio = float((float(np.mean(returns)) / return_std) * np.sqrt(252))

    sortino_ratio = 0.0
    if len(returns) > 1:
        downside_returns = np.minimum(returns, 0.0)
        downside_deviation = float(np.sqrt(np.mean(np.square(downside_returns))))
        if downside_deviation > 0:
            sortino_ratio = float((float(np.mean(returns)) / downside_deviation) * np.sqrt(252))

    calmar_ratio = float(annualized_return / max_drawdown) if max_drawdown > 0 else 0.0
    nonzero_returns = returns[np.abs(returns) > 1e-12] if len(returns) > 0 else np.array([])
    positive_returns = nonzero_returns[nonzero_returns > 0] if len(nonzero_returns) > 0 else np.array([])
    negative_returns = nonzero_returns[nonzero_returns < 0] if len(nonzero_returns) > 0 else np.array([])
    daily_win_rate = (len(positive_returns) / len(nonzero_returns) * 100) if len(nonzero_returns) > 0 else 0.0
    daily_profit_loss_ratio = None
    if len(positive_returns) > 0 and len(negative_returns) > 0:
        avg_daily_profit = float(np.mean(positive_returns))
        avg_daily_loss = abs(float(np.mean(negative_returns)))
        daily_profit_loss_ratio = float(avg_daily_profit / avg_daily_loss) if avg_daily_loss > 0 else None

    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "max_drawdown": max_drawdown,
        "max_drawdown_duration_days": max_drawdown_duration_days,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "calmar_ratio": calmar_ratio,
        "win_rate": daily_win_rate,
        "profit_loss_ratio": daily_profit_loss_ratio,
        "ending_value": end_value,
    }, drawdowns


def _run_backtest(base_df: pd.DataFrame, params: SOXLFearStrategyParams, initial_capital: float, detailed: bool = False) -> Dict:
    dates = base_df["date"].tolist()
    date_strings = [item.isoformat() if hasattr(item, "isoformat") else str(item) for item in dates]
    open_prices = base_df["open"].to_numpy(dtype=float, copy=False)
    high_prices = base_df["high"].to_numpy(dtype=float, copy=False)
    low_prices = base_df["low"].to_numpy(dtype=float, copy=False)
    close_prices = base_df["close"].to_numpy(dtype=float, copy=False)
    if "execution_price" in base_df.columns:
        execution_prices = base_df["execution_price"].to_numpy(dtype=float, copy=False)
    else:
        execution_prices = close_prices
    volumes = base_df["volume"].to_numpy(dtype=float, copy=False)
    ma20_values = base_df["ma20"].to_numpy(dtype=float, copy=False)
    volume_ma20_values = base_df["volume_ma20"].to_numpy(dtype=float, copy=False)
    volume_ratios = base_df["volume_ratio"].to_numpy(dtype=float, copy=False)
    volume_ratio_consecutive_days = int(params.volume_ratio_consecutive_days)
    buy_volume_ratio_column = _volume_ratio_consecutive_column(volume_ratio_consecutive_days)
    buy_volume_ma_column = _volume_ma_excluding_recent_column(volume_ratio_consecutive_days)
    if buy_volume_ratio_column in base_df.columns:
        buy_volume_ratios = base_df[buy_volume_ratio_column].to_numpy(dtype=float, copy=False)
    elif volume_ratio_consecutive_days == 1:
        buy_volume_ratios = volume_ratios
    else:
        raise ValueError(f"缺少连续 {volume_ratio_consecutive_days} 天量比信号列")
    if buy_volume_ma_column in base_df.columns:
        buy_volume_ma_values = base_df[buy_volume_ma_column].to_numpy(dtype=float, copy=False)
    elif volume_ratio_consecutive_days == 1:
        buy_volume_ma_values = volume_ma20_values
    else:
        raise ValueError(f"缺少连续 {volume_ratio_consecutive_days} 天量比基准列")
    fear_values = base_df["fear_greed"].to_numpy(dtype=float, copy=False)
    signal_volumes = (
        base_df["signal_volume"].to_numpy(dtype=float, copy=False)
        if "signal_volume" in base_df.columns
        else volumes
    )
    signal_dates = base_df["signal_date"].tolist() if "signal_date" in base_df.columns else dates
    fear_dates = base_df["fear_date"].tolist() if "fear_date" in base_df.columns else signal_dates
    fear_source_label = str(base_df.attrs.get("fear_source_label") or "贪恐")
    execution_price_label = str(base_df.attrs.get("execution_price_label") or "收盘价")

    # 成交模式：
    #   execute_next_open=False（默认）：信号日收盘决策、信号日收盘价成交（旧行为）；
    #   execute_next_open=True：信号日收盘决策，下一交易日开盘价成交。
    # 实现方式：把决策用数据（恐贪、量比、止盈峰值参考高低价、信号日期等）整体前移一个交易日，
    # 成交价用当日开盘价，交易日期即成交日，信号日期为前一交易日。
    use_next_open = bool(params.execute_next_open)
    if use_next_open:
        execution_prices = open_prices
        decision_close_prices = np.concatenate([[np.nan], close_prices[:-1]])
        decision_high_prices = np.concatenate([[np.nan], high_prices[:-1]])
        decision_fear_values = np.concatenate([[np.nan], fear_values[:-1]])
        decision_volume_ratios = np.concatenate([[np.nan], volume_ratios[:-1]])
        decision_buy_volume_ratios = np.concatenate([[np.nan], buy_volume_ratios[:-1]])
        decision_signal_volumes = np.concatenate([[np.nan], signal_volumes[:-1]])
        decision_signal_dates = [None] + list(signal_dates[:-1])
        decision_fear_dates = [None] + list(fear_dates[:-1])
        execution_price_label = "信号日次日开盘价"
    else:
        decision_close_prices = close_prices
        decision_high_prices = high_prices
        decision_fear_values = fear_values
        decision_volume_ratios = volume_ratios
        decision_buy_volume_ratios = buy_volume_ratios
        decision_signal_volumes = signal_volumes
        decision_signal_dates = signal_dates
        decision_fear_dates = fear_dates

    cash = float(initial_capital)
    shares = 0
    avg_cost = 0.0
    cooldown_remaining = 0
    greed_peak_price = None
    take_profit_sell_count_in_cycle = 0
    trades: List[Dict] = []
    equity_curve: List[Dict] = []
    daily_data: List[Dict] = []
    closed_trade_count = 0
    winning_trade_count = 0
    equity_values = np.empty(len(close_prices), dtype=float)
    benchmark_values = np.empty(len(close_prices), dtype=float)

    first_execution_price = float(execution_prices[0])
    benchmark_shares = _floor_share_count(initial_capital / first_execution_price) if first_execution_price > 0 else 0
    benchmark_cash = initial_capital - benchmark_shares * first_execution_price if first_execution_price > 0 else initial_capital

    for index in range(len(close_prices)):
        current_date = date_strings[index]
        close_price = float(close_prices[index])
        high_price = float(decision_high_prices[index])
        execution_price = float(execution_prices[index])
        signal_date = decision_signal_dates[index]
        signal_date_text = signal_date.isoformat() if hasattr(signal_date, "isoformat") else str(signal_date) if signal_date is not None else None
        fear_date = decision_fear_dates[index]
        fear_date_text = fear_date.isoformat() if hasattr(fear_date, "isoformat") else str(fear_date) if fear_date is not None else None
        fear_score = float(decision_fear_values[index])
        volume_ratio = float(decision_volume_ratios[index])
        buy_volume_ratio = float(decision_buy_volume_ratios[index])
        buy_volume_signal_ok = (
            np.isfinite(buy_volume_ratio)
            and buy_volume_ratio >= float(params.volume_ratio_threshold)
        )
        can_trade = cooldown_remaining == 0
        action_taken = False

        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            can_trade = False

        is_fear = fear_score <= params.buy_threshold
        is_greedy = fear_score >= params.greed_threshold

        if shares > 0:
            if is_greedy:
                greed_peak_price = max(greed_peak_price or high_price, high_price)
            else:
                greed_peak_price = None
                take_profit_sell_count_in_cycle = 0
        else:
            greed_peak_price = None
            take_profit_sell_count_in_cycle = 0

        if (
            shares > 0
            and is_greedy
            and can_trade
            and greed_peak_price
            and take_profit_sell_count_in_cycle < params.max_take_profit_sells_per_cycle
        ):
            # 决策价 = 信号形成时的收盘价（同收盘成交模式下即成交价）
            decision_price = float(decision_close_prices[index])
            if params.trailing_stop_pct <= 0:
                # 移动止盈 = 0：不等待回撤，到达贪恐阈值（>= greed_threshold）即卖出
                drawdown_from_peak = 0.0
                drawdown_reached = True
                trailing_reason = "到达贪恐阈值即卖（移动止盈=0）"
            else:
                drawdown_from_peak = ((greed_peak_price - decision_price) / greed_peak_price) * 100 if greed_peak_price > 0 else 0.0
                drawdown_reached = drawdown_from_peak >= params.trailing_stop_pct
                trailing_reason = f"回撤 {drawdown_from_peak:.2f}% 触发移动止盈"
            sell_price_guard_passed = (not params.sell_price_above_avg_cost) or decision_price > float(avg_cost)
            if drawdown_reached and sell_price_guard_passed:
                portfolio_value = cash + shares * execution_price
                current_position_pct = (shares * execution_price / portfolio_value * 100) if portfolio_value > 0 else 0.0
                min_hold_shares = (
                    portfolio_value * (params.min_position_pct_after_take_profit / 100.0) / execution_price
                    if portfolio_value > 0 and execution_price > 0
                    else 0.0
                )
                max_sell_shares = max(0, shares - _floor_share_count(min_hold_shares))
                if params.sell_reduction_basis == "holdings":
                    requested_sell_shares = _floor_share_count(shares * (params.sell_position_pct / 100.0))
                else:
                    requested_sell_shares = _floor_share_count(
                        portfolio_value * (params.sell_position_pct / 100.0) / execution_price
                    )
                sell_shares = min(shares, requested_sell_shares, max_sell_shares)

                if current_position_pct > params.min_position_pct_after_take_profit and sell_shares >= 1:
                    sell_amount = sell_shares * execution_price
                    sell_adjustment_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                    if sell_adjustment_pct <= params.rebalance_threshold_pct:
                        sell_shares = 0

                if current_position_pct > params.min_position_pct_after_take_profit and sell_shares >= 1:
                    sell_amount = sell_shares * execution_price
                    cost_amount = sell_shares * avg_cost
                    profit = sell_amount - cost_amount
                    profit_pct = ((execution_price / avg_cost) - 1) * 100 if avg_cost > 0 else 0.0

                    cash += sell_amount
                    shares -= sell_shares
                    holdings_value_after = shares * execution_price
                    net_value_after = cash + holdings_value_after
                    if shares <= 0:
                        shares = 0
                        avg_cost = 0.0
                        greed_peak_price = None
                        take_profit_sell_count_in_cycle = 0
                    else:
                        # Reset the trailing-stop anchor after a partial take-profit.
                        # This avoids repeatedly selling against the same historical peak
                        # without a fresh rebound/new high inside the take-profit regime.
                        greed_peak_price = high_price
                        take_profit_sell_count_in_cycle += 1

                    cooldown_remaining = params.cooldown_days
                    action_taken = True
                    closed_trade_count += 1
                    if profit > 0:
                        winning_trade_count += 1

                    trades.append({
                        "date": current_date,
                        "action": "SELL",
                        "price": execution_price,
                        "execution_price": execution_price,
                        "mark_price": close_price,
                        "execution_price_label": execution_price_label,
                        "signal_date": signal_date_text,
                        "fear_date": fear_date_text,
                        "shares": sell_shares,
                        "amount": sell_amount,
                        "cash_after": cash,
                        "position_after": shares,
                        "holdings_value_after": holdings_value_after,
                        "net_value_after": net_value_after,
                        "position_pct_after": (holdings_value_after / net_value_after * 100) if net_value_after > 0 else 0.0,
                        "avg_cost_after": avg_cost,
                        "profit": profit,
                        "profit_pct": profit_pct,
                        "reason": (
                            f"{fear_source_label} {fear_score:.2f} 进入止盈区后{trailing_reason}"
                            f"，本轮第 {take_profit_sell_count_in_cycle} 次卖出"
                            f"，均价保护{'开启' if params.sell_price_above_avg_cost else '关闭'}"
                        ),
                        "fear_score": fear_score,
                        "cnn_score": fear_score,
                        "volume_ratio": volume_ratio,
                        "signal_volume": float(decision_signal_volumes[index]),
                    })

        if not action_taken and is_fear and buy_volume_signal_ok and can_trade:
            portfolio_value = cash + shares * execution_price
            buy_amount = min(cash, portfolio_value * (params.buy_position_pct / 100.0))
            if buy_amount > 0:
                buy_shares = min(_floor_share_count(buy_amount / execution_price), _floor_share_count(cash / execution_price))
                if buy_shares >= 1:
                    actual_buy_amount = buy_shares * execution_price
                    buy_adjustment_pct = (actual_buy_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                    if buy_adjustment_pct <= params.rebalance_threshold_pct:
                        buy_shares = 0
                if buy_shares >= 1:
                    actual_buy_amount = buy_shares * execution_price
                    total_cost = shares * avg_cost + actual_buy_amount
                    shares += buy_shares
                    avg_cost = total_cost / shares if shares > 0 else 0.0
                    cash -= actual_buy_amount
                    cooldown_remaining = params.cooldown_days
                    greed_peak_price = None
                    take_profit_sell_count_in_cycle = 0
                    holdings_value_after = shares * execution_price
                    net_value_after = cash + holdings_value_after

                    if volume_ratio_consecutive_days == 1:
                        buy_reason = f"{fear_source_label} {fear_score:.2f} 进入买入区 + 成交量放大 {buy_volume_ratio:.2f}"
                    else:
                        buy_reason = (
                            f"{fear_source_label} {fear_score:.2f} 进入买入区"
                            f" + 连续 {volume_ratio_consecutive_days} 天成交量放大 {buy_volume_ratio:.2f}"
                        )

                    trades.append({
                        "date": current_date,
                        "action": "BUY",
                        "price": execution_price,
                        "execution_price": execution_price,
                        "mark_price": close_price,
                        "execution_price_label": execution_price_label,
                        "signal_date": signal_date_text,
                        "fear_date": fear_date_text,
                        "shares": buy_shares,
                        "amount": actual_buy_amount,
                        "cash_after": cash,
                        "position_after": shares,
                        "holdings_value_after": holdings_value_after,
                        "net_value_after": net_value_after,
                        "position_pct_after": (holdings_value_after / net_value_after * 100) if net_value_after > 0 else 0.0,
                        "avg_cost_after": avg_cost,
                        "profit": 0.0,
                        "profit_pct": 0.0,
                        "reason": buy_reason,
                        "fear_score": fear_score,
                        "cnn_score": fear_score,
                        "volume_ratio": buy_volume_ratio,
                        "single_day_volume_ratio": volume_ratio,
                        "buy_volume_ratio": buy_volume_ratio,
                        "volume_ratio_consecutive_days": volume_ratio_consecutive_days,
                        "signal_volume": float(decision_signal_volumes[index]),
                    })

        equity_value = cash + shares * close_price
        benchmark_value = benchmark_cash + benchmark_shares * close_price
        equity_values[index] = equity_value
        benchmark_values[index] = benchmark_value

        if detailed:
            equity_curve.append({
                "date": current_date,
                "value": equity_value,
                "benchmark_value": benchmark_value,
            })
            daily_data.append({
                "date": current_date,
                "open": float(open_prices[index]),
                "high": float(high_prices[index]),
                "low": float(low_prices[index]),
                "close": close_price,
                "execution_price": execution_price,
                "execution_price_label": execution_price_label,
                "volume": float(volumes[index]),
                "signal_date": signal_date_text,
                "fear_date": fear_date_text,
                "signal_volume": float(signal_volumes[index]),
                "ma20": float(ma20_values[index]),
                "volume_ma20": float(volume_ma20_values[index]),
                "volume_ratio": None if not np.isfinite(volume_ratio) else volume_ratio,
                "buy_volume_ma20": float(buy_volume_ma_values[index]),
                "buy_volume_ratio": None if not np.isfinite(buy_volume_ratio) else buy_volume_ratio,
                "volume_ratio_consecutive_days": volume_ratio_consecutive_days,
                "fear_greed": float(fear_values[index]),
                "cnn_fear_greed": float(fear_values[index]),
                "equity": equity_value,
                "cash": cash,
                "shares": shares,
                "avg_cost": avg_cost,
                "benchmark_value": benchmark_value,
            })

    strategy_metrics, drawdowns = _compute_equity_metrics(dates, equity_values)
    benchmark_metrics, benchmark_drawdowns = _compute_equity_metrics(dates, benchmark_values)
    trade_win_rate = (winning_trade_count / closed_trade_count * 100) if closed_trade_count > 0 else 0.0
    closed_profits = [float(item.get("profit") or 0.0) for item in trades if item.get("action") == "SELL"]
    winning_profits = [item for item in closed_profits if item > 0]
    losing_profits = [item for item in closed_profits if item < 0]
    trade_profit_loss_ratio = None
    if winning_profits and losing_profits:
        avg_profit = sum(winning_profits) / len(winning_profits)
        avg_loss = abs(sum(losing_profits) / len(losing_profits))
        trade_profit_loss_ratio = float(avg_profit / avg_loss) if avg_loss > 0 else None
    yearly_returns = _merge_yearly_returns(
        _compute_yearly_returns_from_arrays(date_strings, equity_values),
        _compute_yearly_returns_from_arrays(date_strings, benchmark_values),
    )
    yearly_trade_stats = _compute_yearly_trade_stats(trades)
    for item in yearly_returns:
        stats = yearly_trade_stats.get(str(item["year"]), {})
        item["trade_count"] = int(stats.get("trade_count", 0))
        item["buy_count"] = int(stats.get("buy_count", 0))
        item["sell_count"] = int(stats.get("sell_count", 0))

    result = {
        "params": params.dict(),
        **strategy_metrics,
        "trade_win_rate": trade_win_rate,
        "trade_profit_loss_ratio": trade_profit_loss_ratio,
        "trade_count": len(trades),
        "buy_count": sum(1 for item in trades if item["action"] == "BUY"),
        "sell_count": sum(1 for item in trades if item["action"] == "SELL"),
        "ending_cash": cash,
        "ending_shares": shares,
        "execution_price_type": "next_day_open" if use_next_open else "same_day_close",
        "execution_price_label": "信号日次日开盘价" if use_next_open else "信号日收盘价",
        "benchmark_metrics": benchmark_metrics,
        "yearly_returns": yearly_returns,
    }
    if detailed:
        for index, item in enumerate(equity_curve):
            item["drawdown"] = float(drawdowns[index]) * 100 if len(drawdowns) > index else 0.0
            item["benchmark_drawdown"] = (
                float(benchmark_drawdowns[index]) * 100 if len(benchmark_drawdowns) > index else 0.0
            )
        result["trades"] = trades
        result["equity_curve"] = equity_curve
        result["daily_data"] = daily_data
    return result


def _run_seesaw_backtest(
    base_df: pd.DataFrame,
    sub_base_df: pd.DataFrame,
    params: SOXLFearStrategyParams,
    initial_capital: float,
    detailed: bool = False,
    sub2_base_df: Optional[pd.DataFrame] = None,
) -> Dict:
    """跷跷板轮动回测：主标的优先，候补只在主标的空仓时极恐放量买入。

    - 主标的：恐贪 <= buy_threshold 且量比 >= volume_ratio_threshold → 买入；恐贪 >= greed_threshold 卖出
    - 候补：恐贪 <= sub_buy_threshold 且量比 >= sub_volume_ratio_threshold → 买入（仅主标的空仓时）；
      主标的出信号立即卖出候补换回；候补恐贪 >= greed_threshold 卖出（保持空仓）
    - 最多持 1 只；信号日收盘决策，成交价按 execute_next_open 语义
    """
    use_next_open = bool(params.execute_next_open)
    execution_price_label = "信号日次日开盘价" if use_next_open else "信号日收盘价"
    execution_price_type = "next_day_open" if use_next_open else "same_day_close"
    volume_ratio_consecutive_days = int(params.volume_ratio_consecutive_days)
    buy_volume_ratio_column = _volume_ratio_consecutive_column(volume_ratio_consecutive_days)

    def _frame_arrays(df: pd.DataFrame) -> Dict:
        dates = df["date"].tolist()
        date_texts = [item.isoformat() if hasattr(item, "isoformat") else str(item) for item in dates]
        open_p = df["open"].to_numpy(dtype=float, copy=False)
        high_p = df["high"].to_numpy(dtype=float, copy=False)
        close_p = df["close"].to_numpy(dtype=float, copy=False)
        fear = df["fear_greed"].to_numpy(dtype=float, copy=False)
        if buy_volume_ratio_column in df.columns:
            buy_vr = df[buy_volume_ratio_column].to_numpy(dtype=float, copy=False)
        else:
            buy_vr = df["volume_ratio"].to_numpy(dtype=float, copy=False)
        if use_next_open:
            exec_p = open_p
            d_fear = np.concatenate([[np.nan], fear[:-1]])
            d_buy_vr = np.concatenate([[np.nan], buy_vr[:-1]])
        else:
            exec_p = (
                df["execution_price"].to_numpy(dtype=float, copy=False)
                if "execution_price" in df.columns else close_p
            )
            d_fear = fear
            d_buy_vr = buy_vr
        by_date: Dict[str, Dict] = {}
        for index, day_text in enumerate(date_texts):
            by_date[day_text] = {
                "open": float(open_p[index]),
                "high": float(high_p[index]),
                "low": float(df["low"].to_numpy(dtype=float, copy=False)[index]),
                "close": float(close_p[index]),
                "exec": float(exec_p[index]),
                "fear": float(d_fear[index]),
                "buy_vr": float(d_buy_vr[index]),
                "volume": float(df["volume"].to_numpy(dtype=float, copy=False)[index]),
            }
        return {"dates": dates, "date_texts": date_texts, "by_date": by_date}

    main = _frame_arrays(base_df)
    sub = _frame_arrays(sub_base_df)
    sub2 = _frame_arrays(sub2_base_df) if sub2_base_df is not None else None
    main_dates = main["date_texts"]
    main_symbol = _normalize_symbol(str(base_df.attrs.get("symbol") or "") or "") or _normalize_symbol(str(params.symbol or "")) or ""
    sub_symbol = _normalize_symbol(str(sub_base_df.attrs.get("symbol") or "") or "") or _normalize_symbol(str(params.sub_symbol or "")) or ""
    sub2_symbol = (
        _normalize_symbol(str(sub2_base_df.attrs.get("symbol") or "") or "") or _normalize_symbol(str(params.sub2_symbol or "")) or ""
    ) if sub2 is not None else ""
    main_fear_label = str(base_df.attrs.get("fear_source_label") or "主恐贪")
    sub_fear_label = str(sub_base_df.attrs.get("fear_source_label") or "候补恐贪")
    sub2_fear_label = str(sub2_base_df.attrs.get("fear_source_label") or "第二候补恐贪") if sub2 is not None else ""

    cash = float(initial_capital)
    position_symbol = None  # "main" | "sub" | None
    shares = 0
    avg_cost = 0.0
    cooldown_remaining = 0
    greed_peak_price = None
    take_profit_cycle_sell_count = 0
    trades: List[Dict] = []
    equity_curve: List[Dict] = []
    daily_data: List[Dict] = []
    closed_trade_count = 0
    winning_trade_count = 0
    equity_values = np.empty(len(main_dates), dtype=float)
    benchmark_values = np.empty(len(main_dates), dtype=float)

    first_exec = float(main["by_date"][main_dates[0]]["exec"])
    benchmark_shares = _floor_share_count(initial_capital / first_exec) if first_exec > 0 else 0
    benchmark_cash = initial_capital - benchmark_shares * first_exec if first_exec > 0 else initial_capital
    benchmark_shares_qty = benchmark_shares

    def _holder(which: str):
        if which == "main":
            return main
        if which == "sub":
            return sub
        return sub2

    def _holder_symbol(which: str) -> str:
        if which == "main":
            return main_symbol
        if which == "sub":
            return sub_symbol
        return sub2_symbol

    def current_close(day_text: str) -> float:
        holder = _holder(position_symbol) if position_symbol else main
        info = holder["by_date"].get(day_text)
        return float(info["close"]) if info else 0.0

    def current_exec(day_text: str, which: str) -> float:
        holder = _holder(which)
        info = holder["by_date"].get(day_text)
        return float(info["exec"]) if info else 0.0

    def do_sell(day_text: str) -> Optional[Dict]:
        nonlocal cash, shares, avg_cost, position_symbol, greed_peak_price, take_profit_cycle_sell_count, cooldown_remaining, closed_trade_count, winning_trade_count
        which = position_symbol
        symbol = _holder_symbol(which) if which else ""
        exec_price = current_exec(day_text, which) if which else 0.0
        if exec_price <= 0 or shares <= 0:
            return None
        sell_qty = shares
        if float(params.sell_position_pct) < 100:
            requested = _floor_share_count(shares * (float(params.sell_position_pct) / 100.0))
            min_hold = _floor_share_count(
                (cash + shares * exec_price) * (float(params.min_position_pct_after_take_profit) / 100.0) / exec_price
            ) if exec_price > 0 else 0
            sell_qty = min(shares, requested)
            sell_qty = max(0, min(sell_qty, shares - min_hold))
        if sell_qty < 1:
            return None
        proceeds = sell_qty * exec_price
        cost_amount = sell_qty * avg_cost
        cash += proceeds
        shares -= sell_qty
        closed_trade_count += 1
        if proceeds - cost_amount > 0:
            winning_trade_count += 1
        if shares <= 0:
            position_symbol = None
            shares = 0
            avg_cost = 0.0
            greed_peak_price = None
            take_profit_cycle_sell_count = 0
        else:
            greed_peak_price = current_close(day_text)
            take_profit_cycle_sell_count += 1
        cooldown_remaining = int(params.cooldown_days)
        return {
            "date": day_text, "action": "SELL", "symbol": symbol,
            "quantity": sell_qty, "price": exec_price, "amount": proceeds,
            "profit": proceeds - cost_amount,
        }

    def do_buy(day_text: str, which: str) -> Optional[Dict]:
        nonlocal cash, shares, avg_cost, position_symbol, greed_peak_price, take_profit_cycle_sell_count, cooldown_remaining
        symbol = _holder_symbol(which)
        exec_price = current_exec(day_text, which)
        if exec_price <= 0:
            return None
        portfolio_value = cash + shares * exec_price
        buy_amount = min(cash, portfolio_value * (float(params.buy_position_pct) / 100.0))
        if buy_amount <= 0:
            return None
        buy_qty = min(_floor_share_count(buy_amount / exec_price), _floor_share_count(cash / exec_price))
        if buy_qty < 1:
            return None
        actual = buy_qty * exec_price
        total_cost = shares * avg_cost + actual
        shares += buy_qty
        avg_cost = total_cost / shares if shares > 0 else 0.0
        cash -= actual
        position_symbol = which
        greed_peak_price = None
        take_profit_cycle_sell_count = 0
        cooldown_remaining = int(params.cooldown_days)
        return {"date": day_text, "action": "BUY", "symbol": symbol, "quantity": buy_qty, "price": exec_price, "amount": actual}

    for index, day_text in enumerate(main_dates):
        if index == 0 and use_next_open:
            # 首日无前一日信号，只记账不平仓
            close_price = float(main["by_date"][day_text]["close"])
            equity_value = cash + (shares * current_close(day_text) if position_symbol else 0.0)
            benchmark_value = benchmark_cash + benchmark_shares_qty * close_price
            equity_values[index] = equity_value
            benchmark_values[index] = benchmark_value
            if detailed:
                equity_curve.append({"date": day_text, "value": equity_value, "benchmark_value": benchmark_value})
                sub_info0 = sub["by_date"].get(day_text)
                sub2_info0 = sub2["by_date"].get(day_text) if sub2 is not None else None
                daily_data.append({
                    "date": day_text,
                    "open": float(main["by_date"][day_text]["open"]),
                    "high": float(main["by_date"][day_text]["high"]),
                    "low": float(main["by_date"][day_text]["low"]),
                    "close": close_price,
                    "execution_price": float(main["by_date"][day_text]["exec"]),
                    "execution_price_label": execution_price_label,
                    "volume": float(main["by_date"][day_text]["volume"]),
                    "fear_greed": float(base_df.iloc[index]["fear_greed"]) if index < len(base_df) else None,
                    "equity": equity_value, "cash": cash, "shares": shares, "avg_cost": avg_cost,
                    "benchmark_value": benchmark_value,
                    "sub_symbol": sub_symbol or None,
                    "sub_open": float(sub_info0["open"]) if sub_info0 else None,
                    "sub_high": float(sub_info0["high"]) if sub_info0 else None,
                    "sub_low": float(sub_info0["low"]) if sub_info0 else None,
                    "sub_close": float(sub_info0["close"]) if sub_info0 else None,
                    "sub_volume": float(sub_info0["volume"]) if sub_info0 else None,
                    "sub2_symbol": sub2_symbol or None,
                    "sub2_open": float(sub2_info0["open"]) if sub2 is not None and sub2_info0 else None,
                    "sub2_high": float(sub2_info0["high"]) if sub2 is not None and sub2_info0 else None,
                    "sub2_low": float(sub2_info0["low"]) if sub2 is not None and sub2_info0 else None,
                    "sub2_close": float(sub2_info0["close"]) if sub2 is not None and sub2_info0 else None,
                    "sub2_volume": float(sub2_info0["volume"]) if sub2 is not None and sub2_info0 else None,
                })
            continue

        main_info = main["by_date"][day_text]
        main_fear = main_info["fear"]
        main_vr = main_info["buy_vr"]
        main_signal = (
            np.isfinite(main_fear) and main_fear <= float(params.buy_threshold)
            and np.isfinite(main_vr) and main_vr >= float(params.volume_ratio_threshold)
        )
        main_greedy = np.isfinite(main_fear) and main_fear >= float(params.greed_threshold)
        sub_info = sub["by_date"].get(day_text)
        sub_fear = float(sub_info["fear"]) if sub_info else np.nan
        sub_vr = float(sub_info["buy_vr"]) if sub_info else np.nan
        sub_signal = (
            np.isfinite(sub_fear) and sub_fear <= float(params.sub_buy_threshold)
            and np.isfinite(sub_vr) and sub_vr >= float(params.sub_volume_ratio_threshold)
        )
        sub_greedy = np.isfinite(sub_fear) and sub_fear >= float(params.greed_threshold)

        can_trade = cooldown_remaining == 0
        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            can_trade = False

        action = None
        reason = None
        trade_signal_fear = None
        trade_signal_vr = None
        trade_signal_label = None

        use_swap = params.swap_threshold is not None
        swap_value = float(params.swap_threshold) if use_swap else None

        # 三标的信号汇总（sub2 可选）
        sig = {
            "main": {"fear": main_fear, "vr": main_vr, "symbol": main_symbol, "label": main_fear_label,
                     "signal": main_signal, "greedy": main_greedy},
            "sub": {"fear": sub_fear, "vr": sub_vr, "symbol": sub_symbol, "label": sub_fear_label,
                    "signal": sub_signal, "greedy": sub_greedy},
        }
        if sub2 is not None:
            sub2_info = sub2["by_date"].get(day_text)
            sub2_fear = float(sub2_info["fear"]) if sub2_info else np.nan
            sub2_vr = float(sub2_info["buy_vr"]) if sub2_info else np.nan
            sub2_signal = (
                np.isfinite(sub2_fear) and sub2_fear <= float(params.sub2_buy_threshold)
                and np.isfinite(sub2_vr) and sub2_vr >= float(params.sub2_volume_ratio_threshold)
            )
            sub2_greedy = np.isfinite(sub2_fear) and sub2_fear >= float(params.greed_threshold)
            sig["sub2"] = {
                "fear": sub2_fear, "vr": sub2_vr, "symbol": sub2_symbol, "label": sub2_fear_label,
                "signal": sub2_signal, "greedy": sub2_greedy,
            }

        def _sell_held():
            """持有标的贪恐卖出（trailing 支持）。"""
            nonlocal action, reason, trade_signal_fear, trade_signal_vr, trade_signal_label
            held_sig = sig[position_symbol]
            held_fear = float(held_sig["fear"])
            held_vr = float(held_sig["vr"])
            if float(params.trailing_stop_pct) <= 0:
                action = do_sell(day_text)
                reason = f"{held_sig['label']} {held_fear:.2f} 到达贪恐阈值即卖（移动止盈=0）"
            else:
                held_info = _holder(position_symbol)["by_date"][day_text]
                if held_sig["greedy"]:
                    greed_peak_price = max(float(greed_peak_price or held_info["high"]), held_info["high"])
                close_here = held_info["close"]
                drawdown = (float(greed_peak_price) - close_here) / float(greed_peak_price) * 100 if greed_peak_price else 0.0
                if drawdown >= float(params.trailing_stop_pct):
                    action = do_sell(day_text)
                    reason = f"{held_sig['label']} {held_fear:.2f} 回撤 {drawdown:.2f}% 触发移动止盈"
            trade_signal_fear, trade_signal_vr, trade_signal_label = held_fear, held_vr, held_sig["label"]

        def _swap_held_to(target_key: str):
            """换仓：卖当前持仓 → 买 target 标的。"""
            nonlocal action, reason, trade_signal_fear, trade_signal_vr, trade_signal_label
            held_sig = sig[position_symbol]
            target_sig = sig[target_key]
            sell_action = do_sell(day_text)
            if sell_action:
                trade_signal_fear, trade_signal_vr, trade_signal_label = (
                    float(target_sig["fear"]), float(target_sig["vr"]), target_sig["label"]
                )
                if use_swap:
                    reason = (
                        f"{held_sig['label']} {held_sig['fear']:.2f} 恐贪>{swap_value:g} 且 {target_sig['label']} 出买入信号，"
                        f"卖出 {held_sig['symbol']} 换到 {target_sig['symbol']}"
                    )
                else:
                    reason = (
                        f"主标的出信号（{main_fear_label} {main_fear:.2f} + 量比 {main_vr:.2f}），"
                        f"卖出候补 {held_sig['symbol']} 换回主标的"
                    )
            buy_action = do_buy(day_text, target_key)
            if sell_action:
                trades.append({
                    "date": sell_action["date"], "action": "SELL", "symbol": sell_action.get("symbol"),
                    "quantity": sell_action["quantity"], "price": sell_action["price"], "amount": sell_action["amount"],
                    "profit": sell_action.get("profit"), "signal_date": day_text,
                    "fear_score": trade_signal_fear, "volume_ratio": trade_signal_vr, "reason": reason,
                })
            if buy_action:
                trades.append({
                    "date": buy_action["date"], "action": "BUY", "symbol": buy_action.get("symbol"),
                    "quantity": buy_action["quantity"], "price": buy_action["price"], "amount": buy_action["amount"],
                    "signal_date": day_text,
                    "fear_score": trade_signal_fear, "volume_ratio": trade_signal_vr,
                    "reason": f"买入 {target_sig['symbol']}",
                })
            action = sell_action or buy_action

        if position_symbol is None and can_trade:
            if use_swap:
                cands = [k for k, s in sig.items() if s["signal"] and np.isfinite(s["fear"])]
                if cands:
                    target = min(cands, key=lambda k: sig[k]["fear"])
                    action = do_buy(day_text, target)
                    t = sig[target]
                    reason = f"多标的都触发，{t['label']} {t['fear']:.2f} 更恐慌，买入 {t['symbol']}"
                    trade_signal_fear, trade_signal_vr, trade_signal_label = float(t["fear"]), float(t["vr"]), t["label"]
            elif main_signal:
                action = do_buy(day_text, "main")
                reason = f"{main_fear_label} {main_fear:.2f} 极恐放量买入主标的"
                trade_signal_fear, trade_signal_vr, trade_signal_label = main_fear, main_vr, main_fear_label
            elif sub_signal:
                action = do_buy(day_text, "sub")
                reason = f"主标的空仓，{sub_fear_label} {sub_fear:.2f} 极恐放量买入候补 {sub_symbol}"
                trade_signal_fear, trade_signal_vr, trade_signal_label = sub_fear, sub_vr, sub_fear_label
        elif position_symbol and can_trade and shares > 0:
            held_sig = sig.get(position_symbol)
            if held_sig and held_sig["greedy"]:
                _sell_held()
            elif use_swap and held_sig and np.isfinite(held_sig["fear"]) and held_sig["fear"] > swap_value:
                others = [k for k, s in sig.items() if k != position_symbol and s["signal"] and np.isfinite(s["fear"])]
                if others:
                    target = min(others, key=lambda k: sig[k]["fear"])
                    _swap_held_to(target)
            elif not use_swap and position_symbol == "sub" and main_signal:
                _swap_held_to("main")

        if action is not None and action.get("action"):
            already_logged = any(
                item.get("date") == action["date"] and item.get("symbol") == action.get("symbol") and item.get("action") == action["action"]
                for item in trades
            )
            if not already_logged:
                trades.append({
                    "date": action["date"], "action": action["action"], "symbol": action.get("symbol"),
                    "quantity": action["quantity"], "price": action["price"], "amount": action["amount"],
                    "profit": action.get("profit"), "signal_date": day_text,
                    "fear_score": trade_signal_fear, "volume_ratio": trade_signal_vr,
                    "reason": reason or "",
                })

        close_price = float(main["by_date"][day_text]["close"])
        equity_value = cash + (shares * current_close(day_text) if position_symbol else 0.0)
        benchmark_value = benchmark_cash + benchmark_shares_qty * close_price
        equity_values[index] = equity_value
        benchmark_values[index] = benchmark_value
        if detailed:
            equity_curve.append({"date": day_text, "value": equity_value, "benchmark_value": benchmark_value})
            daily_data.append({
                "date": day_text,
                "open": float(main["by_date"][day_text]["open"]),
                "high": float(main["by_date"][day_text]["high"]),
                "low": float(main["by_date"][day_text]["low"]),
                "close": close_price,
                "execution_price": float(main["by_date"][day_text]["exec"]),
                "execution_price_label": execution_price_label,
                "volume": float(main["by_date"][day_text]["volume"]),
                "fear_greed": float(base_df.iloc[index]["fear_greed"]) if index < len(base_df) else None,
                "equity": equity_value,
                "cash": cash,
                "shares": shares,
                "avg_cost": avg_cost,
                "benchmark_value": benchmark_value,
                "sub_symbol": sub_symbol or None,
                "sub_open": float(sub_info["open"]) if sub_info else None,
                "sub_high": float(sub_info["high"]) if sub_info else None,
                "sub_low": float(sub_info["low"]) if sub_info else None,
                "sub_close": float(sub_info["close"]) if sub_info else None,
                "sub_volume": float(sub_info["volume"]) if sub_info else None,
                "sub2_symbol": sub2_symbol or None,
                "sub2_open": float(sub2_info["open"]) if sub2 is not None and sub2_info else None,
                "sub2_high": float(sub2_info["high"]) if sub2 is not None and sub2_info else None,
                "sub2_low": float(sub2_info["low"]) if sub2 is not None and sub2_info else None,
                "sub2_close": float(sub2_info["close"]) if sub2 is not None and sub2_info else None,
                "sub2_volume": float(sub2_info["volume"]) if sub2 is not None and sub2_info else None,
            })

    strategy_metrics, drawdowns = _compute_equity_metrics(main["dates"], equity_values)
    benchmark_metrics, benchmark_drawdowns = _compute_equity_metrics(main["dates"], benchmark_values)
    trade_win_rate = (winning_trade_count / closed_trade_count * 100) if closed_trade_count > 0 else 0.0
    closed_profits = [float(item.get("profit") or 0.0) for item in trades if item.get("action") == "SELL"]
    winning_profits = [item for item in closed_profits if item > 0]
    losing_profits = [item for item in closed_profits if item < 0]
    trade_profit_loss_ratio = None
    if winning_profits and losing_profits:
        avg_profit = sum(winning_profits) / len(winning_profits)
        avg_loss = abs(sum(losing_profits) / len(losing_profits))
        trade_profit_loss_ratio = float(avg_profit / avg_loss) if avg_loss > 0 else None
    yearly_returns = _merge_yearly_returns(
        _compute_yearly_returns_from_arrays(main["date_texts"], equity_values),
        _compute_yearly_returns_from_arrays(main["date_texts"], benchmark_values),
    )
    yearly_trade_stats = _compute_yearly_trade_stats(trades)
    for item in yearly_returns:
        stats = yearly_trade_stats.get(str(item["year"]), {})
        item["trade_count"] = int(stats.get("trade_count", 0))
        item["buy_count"] = int(stats.get("buy_count", 0))
        item["sell_count"] = int(stats.get("sell_count", 0))

    result = {
        "params": params.dict(),
        **strategy_metrics,
        "trade_win_rate": trade_win_rate,
        "trade_profit_loss_ratio": trade_profit_loss_ratio,
        "trade_count": len(trades),
        "buy_count": sum(1 for item in trades if item["action"] == "BUY"),
        "sell_count": sum(1 for item in trades if item["action"] == "SELL"),
        "ending_cash": cash,
        "ending_shares": shares,
        "execution_price_type": execution_price_type,
        "execution_price_label": execution_price_label,
        "benchmark_metrics": benchmark_metrics,
        "yearly_returns": yearly_returns,
    }
    if detailed:
        for index, item in enumerate(equity_curve):
            item["drawdown"] = float(drawdowns[index]) * 100 if len(drawdowns) > index else 0.0
            item["benchmark_drawdown"] = (
                float(benchmark_drawdowns[index]) * 100 if len(benchmark_drawdowns) > index else 0.0
            )
        result["trades"] = trades
        result["equity_curve"] = equity_curve
        result["daily_data"] = daily_data
    return result


def _count_search_params(payload: SOXLFearSearchParams) -> int:
    value_groups = [
        payload.fear_source_values,
        payload.buy_threshold_values,
        payload.greed_threshold_values,
        payload.volume_ratio_threshold_values,
        payload.volume_ratio_consecutive_days_values,
        payload.buy_position_pct_values,
        payload.cooldown_days_values,
        payload.trailing_stop_pct_values,
        payload.sell_position_pct_values,
        payload.sell_reduction_basis_values,
        payload.sell_price_above_avg_cost_values,
        payload.max_take_profit_sells_per_cycle_values,
        payload.min_position_pct_after_take_profit_values,
        payload.execute_next_open_values,
        payload.sub_buy_threshold_values,
        payload.sub_volume_ratio_threshold_values,
        payload.swap_threshold_values,
        payload.sub2_buy_threshold_values,
        payload.sub2_volume_ratio_threshold_values,
    ]
    total = 1
    for values in value_groups:
        if not values:
            return 0
        total *= len(values)
    return total


def _evaluate_search_candidates(
    payload: SOXLFearSearchParams,
    base_dfs: Dict[str, pd.DataFrame],
    progress_callback=None,
    sub_base_df: Optional[pd.DataFrame] = None,
    sub2_base_df: Optional[pd.DataFrame] = None,
) -> Tuple[List[Dict], Dict, int]:
    total_combinations = _count_search_params(payload)
    eval_workers = payload.eval_workers or SEARCH_EVAL_MAX_WORKERS
    eval_batch_size = max(16, eval_workers * 8)
    top_results_heap = []
    best_summary = None
    best_sort_key = None
    skipped_combinations = 0
    processed_combinations = 0

    def handle_completed_result(index: int, sort_key: Tuple[float, float, float, float], result: Dict):
        nonlocal best_summary
        nonlocal best_sort_key

        if best_summary is None or best_sort_key is None or sort_key > best_sort_key:
            best_summary = result
            best_sort_key = sort_key

        heap_entry = (sort_key, index, result)
        if len(top_results_heap) < payload.top_n:
            heappush(top_results_heap, heap_entry)
        elif sort_key > top_results_heap[0][0]:
            heapreplace(top_results_heap, heap_entry)

    def iter_value_batches() -> Iterator[Tuple[str, List[Tuple[int, Tuple]]]]:
        index = 0
        for fear_source in payload.fear_source_values:
            batch = []
            for values in product(
                payload.buy_threshold_values,
                payload.greed_threshold_values,
                payload.volume_ratio_threshold_values,
                payload.volume_ratio_consecutive_days_values,
                payload.buy_position_pct_values,
                payload.cooldown_days_values,
                payload.trailing_stop_pct_values,
                payload.sell_position_pct_values,
                payload.sell_reduction_basis_values,
                payload.sell_price_above_avg_cost_values,
                payload.max_take_profit_sells_per_cycle_values,
                payload.min_position_pct_after_take_profit_values,
                payload.execute_next_open_values,
                payload.sub_buy_threshold_values,
                payload.sub_volume_ratio_threshold_values,
                payload.swap_threshold_values,
                payload.sub2_buy_threshold_values,
                payload.sub2_volume_ratio_threshold_values,
            ):
                index += 1
                batch.append((index, values))
                if len(batch) >= eval_batch_size:
                    yield fear_source, batch
                    batch = []
            if batch:
                yield fear_source, batch

    def flush_futures(futures_map):
        nonlocal processed_combinations
        nonlocal skipped_combinations

        def consume_batch_result(batch_result):
            nonlocal processed_combinations
            nonlocal skipped_combinations

            for index, sort_key, result in batch_result["results"]:
                handle_completed_result(index, sort_key, result)
            skipped_combinations += batch_result["skipped_combinations"]
            for index, message in batch_result["skip_messages"]:
                if skipped_combinations <= 5 or skipped_combinations % 100 == 0:
                    logger.warning(
                        "Skipping invalid SOXL fear parameter combination %s/%s: %s",
                        index,
                        total_combinations,
                        message,
                    )
            processed_combinations += batch_result["processed_combinations"]

        for future in as_completed(list(futures_map.keys())):
            batch_meta = futures_map[future]
            try:
                batch_result = future.result()
                consume_batch_result(batch_result)
            except Exception as exc:
                logger.warning(
                    "SOXL fear parameter batch %s-%s failed in process pool, retrying inline: %s",
                    batch_meta["start_index"],
                    batch_meta["end_index"],
                    str(exc),
                )
                try:
                    batch_result = _evaluate_search_batch(
                        base_dfs[batch_meta["fear_source"]],
                        batch_meta["fear_source"],
                        FEAR_SOURCE_OPTIONS[batch_meta["fear_source"]]["label"],
                        payload.initial_capital,
                        payload.objective,
                        payload.rebalance_threshold_pct,
                        batch_meta["batch"],
                        sub_base_df=sub_base_df,
                        sub_symbol=payload.sub_symbol,
                        sub_fear_source=payload.sub_fear_source,
                        sub_volume_signal_symbol=payload.sub_volume_signal_symbol,
                        sub2_base_df=sub2_base_df,
                        sub2_symbol=payload.sub2_symbol,
                        sub2_fear_source=payload.sub2_fear_source,
                        sub2_volume_signal_symbol=payload.sub2_volume_signal_symbol,
                    )
                    consume_batch_result(batch_result)
                except Exception as fallback_exc:
                    skipped_combinations += batch_meta["batch_size"]
                    processed_combinations += batch_meta["batch_size"]
                    logger.warning(
                        "Skipping failed SOXL fear parameter batch %s-%s after inline retry: %s",
                        batch_meta["start_index"],
                        batch_meta["end_index"],
                        str(fallback_exc),
                    )
            if progress_callback:
                progress_callback(processed_combinations, total_combinations, skipped_combinations)

    with ProcessPoolExecutor(max_workers=eval_workers) as executor:
        futures_map = {}
        max_pending_batches = max(eval_workers * 2, 2)

        for fear_source, batch in iter_value_batches():
            future = executor.submit(
                _evaluate_search_batch,
                base_dfs[fear_source],
                fear_source,
                FEAR_SOURCE_OPTIONS[fear_source]["label"],
                payload.initial_capital,
                payload.objective,
                payload.rebalance_threshold_pct,
                batch,
                sub_base_df,
                payload.sub_symbol,
                payload.sub_fear_source,
                payload.sub_volume_signal_symbol,
                sub2_base_df,
                payload.sub2_symbol,
                payload.sub2_fear_source,
                payload.sub2_volume_signal_symbol,
            )
            futures_map[future] = {
                "start_index": batch[0][0],
                "end_index": batch[-1][0],
                "batch_size": len(batch),
                "fear_source": fear_source,
                "batch": batch,
            }

            if len(futures_map) >= max_pending_batches:
                flush_futures(futures_map)
                futures_map = {}

        if futures_map:
            flush_futures(futures_map)

    results = [item[2] for item in sorted(top_results_heap, key=lambda item: item[0], reverse=True)]
    if not results or best_summary is None:
        raise ValueError("没有可用的回测结果")

    return results, best_summary, skipped_combinations


def _result_sort_key(result: Dict, objective: str = "annualized_return") -> Tuple[float, float, float, float]:
    if objective == "sharpe_ratio":
        return (
            float(result["sharpe_ratio"]),
            float(result["annualized_return"]),
            float(result["calmar_ratio"]),
            -float(result["max_drawdown"]),
        )
    return (
        float(result["annualized_return"]),
        float(result["sharpe_ratio"]),
        float(result["calmar_ratio"]),
        -float(result["max_drawdown"]),
    )


def _evaluate_search_batch(
    base_df: pd.DataFrame,
    fear_source: str,
    fear_source_label: str,
    initial_capital: float,
    objective: str,
    rebalance_threshold_pct: float,
    batch_items: List[Tuple[int, Tuple]],
    sub_base_df: Optional[pd.DataFrame] = None,
    sub_symbol: Optional[str] = None,
    sub_fear_source: Optional[str] = None,
    sub_volume_signal_symbol: Optional[str] = None,
    sub2_base_df: Optional[pd.DataFrame] = None,
    sub2_symbol: Optional[str] = None,
    sub2_fear_source: Optional[str] = None,
    sub2_volume_signal_symbol: Optional[str] = None,
) -> Dict:
    results = []
    skipped_combinations = 0
    skip_messages = []

    for index, values in batch_items:
        try:
            (
                buy_threshold,
                greed_threshold,
                volume_ratio_threshold,
                volume_ratio_consecutive_days,
                buy_position_pct,
                cooldown_days,
                trailing_stop_pct,
                sell_position_pct,
                sell_reduction_basis,
                sell_price_above_avg_cost,
                max_take_profit_sells_per_cycle,
                min_position_pct_after_take_profit,
                execute_next_open,
                sub_buy_threshold,
                sub_volume_ratio_threshold,
                swap_threshold,
                sub2_buy_threshold,
                sub2_volume_ratio_threshold,
            ) = values
            params = SOXLFearStrategyParams(
                buy_threshold=float(buy_threshold),
                greed_threshold=float(greed_threshold),
                volume_ratio_threshold=float(volume_ratio_threshold),
                volume_ratio_consecutive_days=int(volume_ratio_consecutive_days),
                buy_position_pct=float(buy_position_pct),
                cooldown_days=int(cooldown_days),
                trailing_stop_pct=float(trailing_stop_pct),
                sell_position_pct=float(sell_position_pct),
                sell_reduction_basis=str(sell_reduction_basis),
                sell_price_above_avg_cost=bool(sell_price_above_avg_cost),
                max_take_profit_sells_per_cycle=int(max_take_profit_sells_per_cycle),
                min_position_pct_after_take_profit=float(min_position_pct_after_take_profit),
                execute_next_open=bool(execute_next_open),
                rebalance_threshold_pct=float(rebalance_threshold_pct),
                sub_symbol=sub_symbol,
                sub_fear_source=sub_fear_source or "cnn",
                sub_volume_signal_symbol=sub_volume_signal_symbol,
                sub_buy_threshold=float(sub_buy_threshold if sub_buy_threshold is not None else 25.0),
                sub_volume_ratio_threshold=float(sub_volume_ratio_threshold if sub_volume_ratio_threshold is not None else 1.6),
                swap_threshold=swap_threshold,
                sub2_symbol=sub2_symbol,
                sub2_fear_source=sub2_fear_source or "cnn",
                sub2_volume_signal_symbol=sub2_volume_signal_symbol,
                sub2_buy_threshold=float(sub2_buy_threshold if sub2_buy_threshold is not None else 20.0),
                sub2_volume_ratio_threshold=float(sub2_volume_ratio_threshold if sub2_volume_ratio_threshold is not None else 1.3),
            )
        except ValidationError as exc:
            skipped_combinations += 1
            if len(skip_messages) < 5:
                skip_messages.append((index, exc.errors()[0].get("msg", str(exc))))
            continue

        try:
            if sub_base_df is not None and params.sub_symbol:
                result = _run_seesaw_backtest(
                    base_df, sub_base_df, params, initial_capital, detailed=False,
                    sub2_base_df=sub2_base_df,
                )
            else:
                result = _run_backtest(base_df, params, initial_capital, detailed=False)
        except Exception as exc:
            skipped_combinations += 1
            if len(skip_messages) < 5:
                skip_messages.append((index, str(exc)))
            continue

        result["fear_source"] = fear_source
        result["fear_source_label"] = fear_source_label
        sort_key = _result_sort_key(result, objective)
        results.append((index, sort_key, result))

    return {
        "processed_combinations": len(batch_items),
        "skipped_combinations": skipped_combinations,
        "skip_messages": skip_messages,
        "results": results,
    }


def _serialize_summary(result: Dict) -> Dict:
    serialized = {
        "fear_source": result.get("fear_source") or "cnn",
        "fear_source_label": result.get("fear_source_label") or FEAR_SOURCE_OPTIONS["cnn"]["label"],
        "annualized_return": _round_or_none(result["annualized_return"], 4),
        "annualized_volatility": _round_or_none(result["annualized_volatility"], 4),
        "total_return": _round_or_none(result["total_return"], 4),
        "max_drawdown": _round_or_none(result["max_drawdown"], 4),
        "max_drawdown_duration_days": int(result["max_drawdown_duration_days"]),
        "sharpe_ratio": _round_or_none(result["sharpe_ratio"], 4),
        "sortino_ratio": _round_or_none(result["sortino_ratio"], 4),
        "calmar_ratio": _round_or_none(result["calmar_ratio"], 4),
        "win_rate": _round_or_none(result["win_rate"], 4),
        "profit_loss_ratio": _round_or_none(result["profit_loss_ratio"], 4),
        "trade_win_rate": _round_or_none(result["trade_win_rate"], 4),
        "trade_profit_loss_ratio": _round_or_none(result["trade_profit_loss_ratio"], 4),
        "trade_count": int(result["trade_count"]),
        "buy_count": int(result["buy_count"]),
        "sell_count": int(result["sell_count"]),
    }
    serialized.update(result["params"])
    return serialized


def _execution_mode_meta(values: List[bool]) -> Tuple[str, str]:
    """按搜索开关候选值计算成交口径的汇总文案。"""
    normalized = list(dict.fromkeys(values or []))
    has_on = True in normalized
    has_off = False in normalized
    if has_on and has_off:
        return "same_day_close_or_next_open", "信号日收盘价 / 信号日次日开盘价"
    if has_on:
        return "next_day_open", "信号日次日开盘价"
    return "same_day_close", "信号日收盘价"


def _build_search_response(payload: SOXLFearSearchParams) -> Dict:
    start_date = _parse_date(payload.start_date)
    end_date = _parse_date(payload.end_date, default=date.today())
    if start_date >= end_date:
        raise ValueError("开始日期必须早于结束日期")

    total_combinations = _count_search_params(payload)
    if total_combinations <= 0:
        raise ValueError("至少需要提供一组有效的超参数候选值")

    base_dfs, source_metas, meta, sub_base_df, sub_meta, sub2_base_df, sub2_meta = _prepare_search_dataframes(
        payload.symbol,
        start_date,
        end_date,
        payload.fear_source_values,
        payload.volume_signal_symbol,
        sub_symbol=payload.sub_symbol,
        sub_fear_source=payload.sub_fear_source,
        sub_volume_signal_symbol=payload.sub_volume_signal_symbol,
        sub2_symbol=payload.sub2_symbol,
        sub2_fear_source=payload.sub2_fear_source,
        sub2_volume_signal_symbol=payload.sub2_volume_signal_symbol,
    )
    logger.info(
        "Starting SOXL fear parameter search, symbol=%s, volume_signal_symbol=%s, fear_sources=%s, combinations=%s, top_n=%s, sub_symbol=%s, sub2_symbol=%s",
        payload.symbol,
        payload.volume_signal_symbol or payload.symbol,
        payload.fear_source_values,
        total_combinations,
        payload.top_n,
        payload.sub_symbol,
        payload.sub2_symbol,
    )

    def progress_callback(index: int, total: int, skipped: int):
        if index % 1000 == 0 or index == total:
            logger.info(
                "SOXL fear parameter search progress %s/%s (%.2f%%), skipped=%s",
                index,
                total,
                (index / total) * 100,
                skipped,
            )

    results, best_summary, skipped_combinations = _evaluate_search_candidates(
        payload,
        base_dfs,
        progress_callback=progress_callback,
        sub_base_df=sub_base_df,
        sub2_base_df=sub2_base_df,
    )

    best_fear_source = best_summary.get("fear_source") or payload.fear_source_values[0]
    best_meta = source_metas[best_fear_source]
    best_params = SOXLFearStrategyParams(**best_summary["params"])
    if sub_base_df is not None and best_params.sub_symbol:
        best_result = _run_seesaw_backtest(
            base_dfs[best_fear_source], sub_base_df, best_params, payload.initial_capital, detailed=True,
            sub2_base_df=sub2_base_df,
        )
    else:
        best_result = _run_backtest(
            base_dfs[best_fear_source],
            best_params,
            payload.initial_capital,
            detailed=True,
        )
    fear_series = _build_fear_series_payload(base_dfs)
    search_execution_type, search_execution_label = _execution_mode_meta(payload.execute_next_open_values)

    return {
        "meta": {
            **meta,
            "initial_capital": payload.initial_capital,
            "eval_workers": payload.eval_workers or SEARCH_EVAL_MAX_WORKERS,
            "searched_combinations": total_combinations,
            "skipped_combinations": skipped_combinations,
            "valid_combinations": total_combinations - skipped_combinations,
            "returned_results": min(payload.top_n, len(results)),
            "objective": payload.objective,
            "execution_price_type": search_execution_type,
            "execution_price_label": search_execution_label,
        },
        "results": [_serialize_summary(item) for item in results[:payload.top_n]],
        "best_result": {
            **best_result,
            "params": best_result["params"],
            "fear_series": fear_series,
            "meta": {
                **best_meta,
                "initial_capital": payload.initial_capital,
                "execution_price_type": best_result.get("execution_price_type"),
                "execution_price_label": best_result.get("execution_price_label"),
                "sub_meta": sub_meta,
                "sub2_meta": sub2_meta,
            },
        },
    }


def _cleanup_finished_jobs(max_age_hours: int = 12):
    threshold = datetime.now().timestamp() - max_age_hours * 3600
    with SEARCH_JOBS_LOCK:
        expired_ids = [
            task_id
            for task_id, job in SEARCH_JOBS.items()
            if job.get("updated_at", 0) < threshold and job.get("status") in {"completed", "failed"}
        ]
        for task_id in expired_ids:
            SEARCH_JOBS.pop(task_id, None)


def _publish_search_job(task_id: str):
    with SEARCH_JOBS_LOCK:
        job = SEARCH_JOBS.get(task_id)
        if not job:
            return
        account_id = job.get("account_id")
        payload = SOXLFearSearchJobStatus(**job).dict()
    publish_event(account_id, "soxl_fear_search", payload)


def _update_search_job(task_id: str, **updates):
    with SEARCH_JOBS_LOCK:
        job = SEARCH_JOBS.get(task_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = datetime.now().timestamp()
    _publish_search_job(task_id)


def _run_search_job(task_id: str, payload: SOXLFearSearchParams):
    try:
        start_date = _parse_date(payload.start_date)
        end_date = _parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")

        total_combinations = _count_search_params(payload)
        if total_combinations <= 0:
            raise ValueError("至少需要提供一组有效的超参数候选值")

        _update_search_job(
            task_id,
            status="running",
            progress=1,
            total_combinations=total_combinations,
            processed_combinations=0,
            skipped_combinations=0,
            message="正在准备回测数据",
        )

        logger.info(
            "Starting SOXL fear parameter search job, task_id=%s, symbol=%s, volume_signal_symbol=%s, fear_sources=%s, start_date=%s, end_date=%s, combinations=%s, top_n=%s",
            task_id,
            payload.symbol,
            payload.volume_signal_symbol or payload.symbol,
            payload.fear_source_values,
            start_date,
            end_date,
            total_combinations,
            payload.top_n,
        )

        base_dfs, source_metas, meta, sub_base_df, sub_meta, sub2_base_df, sub2_meta = _prepare_search_dataframes(
            payload.symbol,
            start_date,
            end_date,
            payload.fear_source_values,
            payload.volume_signal_symbol,
            sub_symbol=payload.sub_symbol,
            sub_fear_source=payload.sub_fear_source,
            sub_volume_signal_symbol=payload.sub_volume_signal_symbol,
            sub2_symbol=payload.sub2_symbol,
            sub2_fear_source=payload.sub2_fear_source,
            sub2_volume_signal_symbol=payload.sub2_volume_signal_symbol,
        )

        def progress_callback(index: int, total: int, skipped: int):
            if index == 1 or index % 100 == 0 or index == total:
                progress = min(99, max(1, int(index / total * 100)))
                _update_search_job(
                    task_id,
                    progress=progress,
                    processed_combinations=index,
                    total_combinations=total,
                    skipped_combinations=skipped,
                    message=f"正在评估参数组合 {index}/{total}",
                )

            if index % 1000 == 0 or index == total:
                logger.info(
                    "SOXL fear parameter search job=%s progress %s/%s (%.2f%%), skipped=%s",
                    task_id,
                    index,
                    total,
                    (index / total) * 100,
                    skipped,
                )

        results, best_summary, skipped_combinations = _evaluate_search_candidates(
            payload,
            base_dfs,
            progress_callback=progress_callback,
            sub_base_df=sub_base_df,
            sub2_base_df=sub2_base_df,
        )

        _update_search_job(
            task_id,
            progress=99,
            processed_combinations=total_combinations,
            total_combinations=total_combinations,
            skipped_combinations=skipped_combinations,
            message="正在生成最优参数的详细回测结果",
        )

        best_fear_source = best_summary.get("fear_source") or payload.fear_source_values[0]
        best_meta = source_metas[best_fear_source]
        best_params = SOXLFearStrategyParams(**best_summary["params"])
        if sub_base_df is not None and best_params.sub_symbol:
            best_result = _run_seesaw_backtest(
                base_dfs[best_fear_source], sub_base_df, best_params, payload.initial_capital, detailed=True,
                sub2_base_df=sub2_base_df,
            )
        else:
            best_result = _run_backtest(
                base_dfs[best_fear_source],
                best_params,
                payload.initial_capital,
                detailed=True,
            )
        fear_series = _build_fear_series_payload(base_dfs)
        search_execution_type, search_execution_label = _execution_mode_meta(payload.execute_next_open_values)

        result_payload = {
            "meta": {
                **meta,
                "initial_capital": payload.initial_capital,
                "eval_workers": payload.eval_workers or SEARCH_EVAL_MAX_WORKERS,
                "searched_combinations": total_combinations,
                "skipped_combinations": skipped_combinations,
                "valid_combinations": total_combinations - skipped_combinations,
                "returned_results": min(payload.top_n, len(results)),
                "objective": payload.objective,
                "execution_price_type": search_execution_type,
                "execution_price_label": search_execution_label,
            },
            "results": [_serialize_summary(item) for item in results[:payload.top_n]],
            "best_result": {
                **best_result,
                "params": best_result["params"],
                "fear_series": fear_series,
                "meta": {
                    **best_meta,
                    "initial_capital": payload.initial_capital,
                    "execution_price_type": best_result.get("execution_price_type"),
                    "execution_price_label": best_result.get("execution_price_label"),
                    "sub_meta": sub_meta,
                "sub2_meta": sub2_meta,
                },
            },
        }

        _update_search_job(
            task_id,
            status="completed",
            progress=100,
            processed_combinations=total_combinations,
            total_combinations=total_combinations,
            skipped_combinations=skipped_combinations,
            message="搜索完成",
            result=result_payload,
        )
    except Exception as exc:
        logger.exception("SOXL fear parameter search job failed, task_id=%s", task_id)
        _update_search_job(
            task_id,
            status="failed",
            message="搜索失败",
            error=str(exc),
        )


@router.get("/options")
def get_soxl_fear_backtest_options(
    account_id: str = Depends(valid_account),
):
    return {
        "symbol_options": TARGET_OPTIONS,
        "volume_signal_symbol_options": TARGET_OPTIONS,
        "fear_source_options": [
            {
                "label": config["label"],
                "value": key,
                "symbol": config.get("symbol"),
                "market": config.get("market") or "us",
            }
            for key, config in FEAR_SOURCE_OPTIONS.items()
        ],
        "a_stock_preset_pairs": A_STOCK_PRESET_PAIRS,
        "default_request": {
            "symbol": "SOXL.US",
            "volume_signal_symbol": None,
            "fear_source_values": ["cnn"],
            "a_stock_symbol": A_STOCK_PRESET_PAIRS[0]["target_symbol"] if A_STOCK_PRESET_PAIRS else None,
            "a_stock_fear_source": A_STOCK_PRESET_PAIRS[0]["fear_source"] if A_STOCK_PRESET_PAIRS else None,
        },
    }


@router.post("/search")
def search_soxl_fear_params(
    payload: SOXLFearSearchParams,
    account_id: str = Depends(valid_account),
):
    try:
        return _build_search_response(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/search/jobs", response_model=SOXLFearSearchJobCreated)
def create_soxl_fear_search_job(
    payload: SOXLFearSearchParams,
    account_id: str = Depends(valid_account),
):
    try:
        start_date = _parse_date(payload.start_date)
        end_date = _parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")

        total_combinations = _count_search_params(payload)
        if total_combinations <= 0:
            raise ValueError("至少需要提供一组有效的超参数候选值")

        _cleanup_finished_jobs()
        task_id = uuid.uuid4().hex
        with SEARCH_JOBS_LOCK:
            SEARCH_JOBS[task_id] = {
                "task_id": task_id,
                "account_id": account_id,
                "status": "pending",
                "progress": 0,
                "processed_combinations": 0,
                "total_combinations": total_combinations,
                "skipped_combinations": 0,
                "message": "任务已创建，等待执行",
                "result": None,
                "error": None,
                "updated_at": datetime.now().timestamp(),
            }

        _publish_search_job(task_id)
        SEARCH_JOB_EXECUTOR.submit(_run_search_job, task_id, payload)
        return SOXLFearSearchJobCreated(
            task_id=task_id,
            status="pending",
            total_combinations=total_combinations,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/search/jobs/{task_id}", response_model=SOXLFearSearchJobStatus)
def get_soxl_fear_search_job_status(
    task_id: str,
    account_id: str = Depends(valid_account),
):
    with SEARCH_JOBS_LOCK:
        job = SEARCH_JOBS.get(task_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        return SOXLFearSearchJobStatus(**job)


class SOXLFearRunParams(BaseModel):
    symbol: str = "SOXL.US"
    volume_signal_symbol: Optional[str] = None
    fear_source: str = "cnn"
    compare_fear_sources: Optional[List[str]] = None
    initial_capital: float = 100000.0
    start_date: str = "2021-01-01"
    end_date: Optional[str] = None
    params: SOXLFearStrategyParams

    @validator("symbol")
    def validate_symbol(cls, value):
        symbol = _normalize_symbol(value)
        if not symbol or not SYMBOL_PATTERN.match(symbol):
            raise ValueError("symbol 格式不正确")
        return symbol

    @validator("volume_signal_symbol")
    def validate_volume_signal_symbol(cls, value):
        if not value:
            return None
        symbol = _normalize_symbol(value)
        if not SYMBOL_PATTERN.match(symbol):
            raise ValueError("volume_signal_symbol 格式不正确")
        return symbol

    @validator("fear_source")
    def validate_fear_source(cls, value):
        if value not in FEAR_SOURCE_OPTIONS:
            raise ValueError("fear_source 包含不支持的来源")
        return value

    @validator("compare_fear_sources")
    def validate_compare_fear_sources(cls, value):
        if value is None:
            return value
        normalized = list(dict.fromkeys(value or []))
        invalid = [item for item in normalized if item not in FEAR_SOURCE_OPTIONS]
        if invalid:
            raise ValueError("compare_fear_sources 包含不支持的来源")
        return normalized


@router.post("/run")
def run_soxl_fear_backtest(
    payload: SOXLFearRunParams,
    account_id: str = Depends(valid_account),
):
    try:
        start_date = _parse_date(payload.start_date)
        end_date = _parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")

        compare_sources = list(dict.fromkeys(payload.compare_fear_sources or [payload.fear_source]))
        if payload.fear_source not in compare_sources:
            compare_sources.insert(0, payload.fear_source)

        base_dfs, source_metas, _, sub_base_df, sub_meta, sub2_base_df, sub2_meta = _prepare_search_dataframes(
            payload.symbol,
            start_date,
            end_date,
            compare_sources,
            payload.volume_signal_symbol,
            sub_symbol=payload.params.sub_symbol,
            sub_fear_source=payload.params.sub_fear_source,
            sub_volume_signal_symbol=payload.params.sub_volume_signal_symbol,
            sub2_symbol=payload.params.sub2_symbol,
            sub2_fear_source=payload.params.sub2_fear_source,
            sub2_volume_signal_symbol=payload.params.sub2_volume_signal_symbol,
        )
        base_df = base_dfs[payload.fear_source]
        meta = source_metas[payload.fear_source]
        if sub_base_df is not None and payload.params.sub_symbol:
            result = _run_seesaw_backtest(
                base_df, sub_base_df, payload.params, payload.initial_capital, detailed=True,
                sub2_base_df=sub2_base_df,
            )
        else:
            result = _run_backtest(base_df, payload.params, payload.initial_capital, detailed=True)
        result["meta"] = {
            **meta,
            "initial_capital": payload.initial_capital,
            "execution_price_type": result.get("execution_price_type") or meta.get("execution_price_type"),
            "execution_price_label": result.get("execution_price_label") or meta.get("execution_price_label"),
            "sub_meta": sub_meta,
            "sub2_meta": sub2_meta,
        }
        result["fear_series"] = _build_fear_series_payload(base_dfs)
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
