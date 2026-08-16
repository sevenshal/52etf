import math
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from itertools import product
from typing import Any, Dict, List, Optional

import duckdb
import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, validator

from ...core.database import DB_PATH
from ...core.duckdb_utils import ANALYTICS_DB_PATH
from ...core.event_stream import publish_event
from ...core.services.a_stock_fear_etf_backtest_engine import (
    build_bars_by_date,
    build_fear_by_date,
    build_signal_rows,
    build_top_signals,
    build_trend_index_by_date,
    build_trend_ma_by_index,
    load_etf_bars,
    load_fear,
    load_trend_index_close,
    max_drawdown,
    prepare_fear_features,
    prepare_market_features,
    run_backtest,
    summarize,
    target_mapping,
)
from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_NAMES, A_STOCK_INDEX_FEAR_GREED_TARGETS
from .account import valid_account


router = APIRouter(prefix="/api/a-stock-fear-etf-backtest", tags=["A-Stock Fear ETF Backtest"])
SEARCH_JOBS: Dict[str, Dict[str, Any]] = {}
SEARCH_LOCK = threading.Lock()
SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="a-fear-etf-search")
MAX_SEARCH_COMBINATIONS = 5000

# 推荐标的池：默认参数（跷跷板轮动+见顶卖出）针对该组合优化
# 红利与科创50/100/200恐贪相关性仅 0.03（跷跷板），叠加沪深300/中证500/创业板
RECOMMENDED_INDEXES = [
    "000015.SH", "000688.SH", "000698.SH", "000699.SH",
    "000300.SH", "000905.SH", "399006.SZ",
]
DEFAULT_BENCHMARK_SYMBOL = "000300.SH"

# 趋势补位默认候选池（指数 -> ETF）：科创50/科创100/科创200/半导体/纳指科技/
# 军工/新能源车/创新药/医疗/有色/证券/煤炭/银行/消费/红利低波
DEFAULT_TREND_SLOTS: list[tuple[str, str]] = [
    ("000688.SH", "588000.SH"),
    ("000698.SH", "588220.SH"),
    ("000699.SH", "588230.SH"),
    ("H30184.CSI", "512480.SH"),
    ("QQQ.US", "159509.SZ"),
    ("399967.SZ", "512660.SH"),
    ("930997.CSI", "515030.SH"),
    ("931152.CSI", "515120.SH"),
    ("399989.SZ", "512170.SH"),
]

TREND_SLOT_LABELS: dict[str, str] = {
    "000688.SH": "科创50", "000698.SH": "科创100", "000699.SH": "科创200",
    "H30184.CSI": "半导体", "QQQ.US": "纳指科技", "399967.SZ": "军工",
    "930997.CSI": "新能源车", "931152.CSI": "创新药", "399989.SZ": "医疗",

}


def _parse_trend_slots(raw: Optional[List[str]]) -> list[tuple[str, str]]:
    """解析前端传入的趋势槽位（"INDEX:ETF" 格式），None/空 → 默认15池。"""
    if not raw:
        return list(DEFAULT_TREND_SLOTS)
    slots: list[tuple[str, str]] = []
    for item in raw:
        parts = str(item).split(":")
        if len(parts) != 2:
            continue
        index_symbol = parts[0].strip().upper()
        etf_symbol = parts[1].strip().upper()
        if index_symbol and etf_symbol:
            slots.append((index_symbol, etf_symbol))
    return slots or list(DEFAULT_TREND_SLOTS)


def _target_options() -> List[Dict[str, str]]:
    targets = []
    seen: set[tuple[str, str]] = set()
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        if not item.get("proxy_etf"):
            continue
        index_symbol = str(item["symbol"]).upper()
        etf = str(item["proxy_etf"]).upper()
        if (index_symbol, etf) in seen:
            continue
        seen.add((index_symbol, etf))
        targets.append({
            "index_symbol": index_symbol,
            "index_label": item.get("ticker") or item.get("label") or index_symbol,
            "etf_symbol": etf,
            "etf_label": A_STOCK_ETF_DAILY_NAMES.get(etf, etf),
        })
    return targets


def _benchmark_label(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    return next(
        (
            str(item["index_label"])
            for item in _target_options()
            if item["index_symbol"] == normalized
        ),
        normalized,
    )


def _benchmark_payload(symbol: str, price_source: str = "index") -> Dict[str, str]:
    normalized = str(symbol).strip().upper()
    target = next(
        (item for item in _target_options() if item["index_symbol"] == normalized),
        None,
    )
    proxy_etf = target["etf_symbol"] if target else normalized
    return {
        "symbol": normalized,
        "label": _benchmark_label(normalized),
        "price_source": price_source,
        "price_symbol": proxy_etf if price_source == "etf_proxy" else normalized,
    }


class StrategyParams(BaseModel):
    extreme_fear_threshold: float = 30.0
    volume_ratio_threshold: float = 1.0
    volume_window: int = 20
    bottom_fear_threshold: float = 20.0
    bottom_ma_window: int = 5
    extreme_buy_fraction: float = 1.0
    bottom_buy_fraction: float = 0.5
    greed_threshold: float = 70.0
    greed_sell_fraction: float = 1.0
    stop_loss_pct: float = 12.0
    stop_cooldown_days: int = 20
    volatility_window: int = 20
    volatility_baseline_window: int = 20
    volatility_std_multiplier: float = 0.5
    trailing_drawdown_pct: float = 5.0
    max_positions: int = 1
    commission_pct: float = 0.03
    min_commission: float = 5.0
    slippage_pct: float = 0.02
    stamp_duty_pct: float = 0.0
    lot_size: int = 100
    # 跷跷板轮动：空仓时才扫描全池买入最恐慌的指数（红利与科创等独立恐慌）
    sort_by_fear: bool = True
    buy_when_flat_only: bool = True
    # 恐贪见顶反转卖出（MA 转跌 + 近期触及极端贪婪）→ 清仓
    top_sell_threshold: Optional[float] = 70.0
    # 趋势补位：空仓且无恐慌信号时，选 gap（收盘/MA-1）最大的槽位全仓买入，跌破指数 MA 卖出
    trend_enabled: bool = False
    trend_ma_win: int = 20
    trend_max_fear: float = 50.0
    trend_slots: Optional[List[str]] = None  # ["INDEX:ETF", ...]，None/空=默认15池

    @validator("trend_ma_win")
    def validate_trend_ma_win(cls, value):
        if value < 2 or value > 500:
            raise ValueError("趋势均线窗口必须在2到500之间")
        return value

    @validator("trend_max_fear")
    def validate_trend_max_fear(cls, value):
        if value < 0 or value > 100:
            raise ValueError("趋势买入恐贪条件必须在0到100之间")
        return value

    @validator("trend_slots")
    def validate_trend_slots(cls, value):
        if value is None:
            return None
        normalized = [str(item).strip().upper() for item in value if str(item).strip()]
        return list(dict.fromkeys(normalized)) or None

    @validator("extreme_fear_threshold", "bottom_fear_threshold", "greed_threshold")
    def validate_fear(cls, value):
        if value < 0 or value > 100:
            raise ValueError("恐贪阈值必须在0到100之间")
        return value

    @validator("extreme_buy_fraction", "bottom_buy_fraction", "greed_sell_fraction")
    def validate_fraction(cls, value):
        # 0 允许：extreme/bottom 买入仓位设 0 = 关闭该买入信号
        if value < 0 or value > 1:
            raise ValueError("仓位比例必须在0到1之间（0=关闭该信号）")
        return value

    @validator("volume_ratio_threshold")
    def validate_volume_ratio(cls, value):
        if value <= 0 or value > 20:
            raise ValueError("量比阈值必须大于0且不超过20")
        return value

    @validator("volatility_std_multiplier")
    def validate_std_multiplier(cls, value):
        if value < 0 or value > 10:
            raise ValueError("波动率标准差倍数必须在0到10之间")
        return value

    @validator("trailing_drawdown_pct")
    def validate_drawdown(cls, value):
        if value <= 0 or value >= 100:
            raise ValueError("移动止盈回撤必须大于0且小于100%")
        return value

    @validator("top_sell_threshold", allow_reuse=True)
    def validate_top_sell(cls, value):
        if value is not None and (value <= 0 or value >= 100):
            raise ValueError("见顶卖出阈值必须在0到100之间，或留空关闭")
        return value

    @validator("stop_loss_pct")
    def validate_stop_loss(cls, value):
        if value <= 0 or value >= 100:
            raise ValueError("止损比例必须大于0且小于100%")
        return value

    @validator("stop_cooldown_days")
    def validate_stop_cooldown_days(cls, value):
        if value < 0 or value > 500:
            raise ValueError("止损冷静期必须在0到500个交易日之间")
        return value

    @validator(
        "volume_window", "bottom_ma_window", "volatility_window",
        "volatility_baseline_window",
    )
    def validate_window(cls, value):
        if value < 2 or value > 500:
            raise ValueError("计算窗口必须在2到500个交易日之间")
        return value

    @validator("max_positions")
    def validate_max_positions(cls, value):
        if value < 1 or value > 20:
            raise ValueError("最大持仓数必须在1到20之间")
        return value

    @validator("commission_pct", "slippage_pct", "stamp_duty_pct")
    def validate_cost(cls, value):
        if value < 0 or value > 10:
            raise ValueError("交易成本必须在0到10%之间")
        return value

    @validator("min_commission")
    def validate_min_commission(cls, value):
        if value < 0 or value > 1000:
            raise ValueError("最低佣金必须在0到1000元之间")
        return value

    @validator("lot_size")
    def validate_lot_size(cls, value):
        if value < 1 or value > 10000:
            raise ValueError("每手份数必须在1到10000之间")
        return value


class RunRequest(BaseModel):
    start_date: str = "2023-01-01"
    end_date: Optional[str] = None
    initial_capital: float = 1_000_000.0
    included_indexes: List[str] = Field(default_factory=list)
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL
    params: StrategyParams = Field(default_factory=StrategyParams)

    @validator("initial_capital")
    def validate_capital(cls, value):
        if value <= 0:
            raise ValueError("初始资金必须大于0")
        return value

    @validator("benchmark_symbol")
    def validate_benchmark_symbol(cls, value):
        symbol = str(value or "").strip().upper()
        available = {item["index_symbol"] for item in _target_options()}
        if symbol not in available:
            raise ValueError("基准必须从当前可交易指数标的池中选择")
        return symbol


SEARCH_FIELDS = (
    "extreme_fear_threshold", "volume_ratio_threshold", "volume_window",
    "bottom_fear_threshold", "bottom_ma_window", "extreme_buy_fraction",
    "bottom_buy_fraction", "greed_threshold", "greed_sell_fraction",
    "stop_loss_pct", "stop_cooldown_days",
    "volatility_window", "volatility_baseline_window", "volatility_std_multiplier",
    "trailing_drawdown_pct", "max_positions",
    "commission_pct", "min_commission", "slippage_pct", "stamp_duty_pct", "lot_size",
    "sort_by_fear", "buy_when_flat_only", "top_sell_threshold",
    "trend_enabled", "trend_ma_win", "trend_max_fear",
)


class SearchRequest(RunRequest):
    top_n: int = 20
    objective: str = "sharpe_zero_rf"
    extreme_fear_threshold_values: List[float] = Field(default_factory=lambda: [30.0, 20.0])
    volume_ratio_threshold_values: List[float] = Field(default_factory=lambda: [1.0, 1.3])
    volume_window_values: List[int] = Field(default_factory=lambda: [20])
    bottom_fear_threshold_values: List[float] = Field(default_factory=lambda: [20.0, 15.0])
    bottom_ma_window_values: List[int] = Field(default_factory=lambda: [5])
    extreme_buy_fraction_values: List[float] = Field(default_factory=lambda: [1.0])
    bottom_buy_fraction_values: List[float] = Field(default_factory=lambda: [0.5])
    greed_threshold_values: List[float] = Field(default_factory=lambda: [70, 80])
    greed_sell_fraction_values: List[float] = Field(default_factory=lambda: [1.0, 0.5])
    stop_loss_pct_values: List[float] = Field(default_factory=lambda: [12.0, 10.0])
    stop_cooldown_days_values: List[int] = Field(default_factory=lambda: [20])
    volatility_window_values: List[int] = Field(default_factory=lambda: [20])
    volatility_baseline_window_values: List[int] = Field(default_factory=lambda: [20])
    volatility_std_multiplier_values: List[float] = Field(default_factory=lambda: [0.5, 1.0])
    trailing_drawdown_pct_values: List[float] = Field(default_factory=lambda: [5, 7])
    max_positions_values: List[int] = Field(default_factory=lambda: [1])
    sort_by_fear_values: List[bool] = Field(default_factory=lambda: [True, False])
    buy_when_flat_only_values: List[bool] = Field(default_factory=lambda: [True, False])
    top_sell_threshold_values: List[Optional[float]] = Field(default_factory=lambda: [70.0, 80.0])
    trend_enabled_values: List[bool] = Field(default_factory=lambda: [False])
    trend_ma_win_values: List[int] = Field(default_factory=lambda: [20])
    trend_max_fear_values: List[float] = Field(default_factory=lambda: [50.0])
    commission_pct_values: List[float] = Field(default_factory=lambda: [0.03])
    min_commission_values: List[float] = Field(default_factory=lambda: [5])
    slippage_pct_values: List[float] = Field(default_factory=lambda: [0.02])
    stamp_duty_pct_values: List[float] = Field(default_factory=lambda: [0])
    lot_size_values: List[int] = Field(default_factory=lambda: [100])

    @validator("trend_max_fear_values")
    def validate_trend_max_fear_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("趋势恐贪条件候选至少一个值")
        for item in normalized:
            if item < 0 or item > 100:
                raise ValueError("趋势恐贪条件必须在0到100之间")
        return normalized

    @validator("trend_enabled_values")
    def validate_trend_enabled_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("趋势补位开关候选至少一个值")
        return normalized

    @validator("trend_ma_win_values")
    def validate_trend_ma_win_values(cls, value):
        normalized = list(dict.fromkeys(value or []))
        if not normalized:
            raise ValueError("趋势均线窗口候选至少一个值")
        for item in normalized:
            if item < 2 or item > 500:
                raise ValueError("趋势均线窗口必须在2到500之间")
        return normalized

    @validator("top_n")
    def validate_top_n(cls, value):
        if value < 1 or value > 100:
            raise ValueError("返回结果数必须在1到100之间")
        return value

    @validator("objective")
    def validate_objective(cls, value):
        if value not in {"total_return_pct", "annualized_return_pct", "sharpe_zero_rf", "calmar_ratio"}:
            raise ValueError("不支持的搜索目标")
        return value

    @validator(*(f"{field}_values" for field in SEARCH_FIELDS))
    def validate_candidate_lists(cls, value):
        if not value:
            raise ValueError("每个参数至少需要一个候选值")
        return list(dict.fromkeys(value))


class SearchJobCreated(BaseModel):
    task_id: str
    status: str
    total_combinations: int


class SearchJobStatus(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    processed_combinations: int = 0
    total_combinations: int = 0
    skipped_combinations: int = 0
    message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _parse_date(value: Optional[str], fallback: Optional[date] = None) -> date:
    if not value:
        if fallback is None:
            raise ValueError("日期不能为空")
        return fallback
    return datetime.strptime(value, "%Y-%m-%d").date()


def _normalize_symbols(values: List[str]) -> set[str]:
    return {str(value).strip().upper() for value in values if str(value).strip()}


def _json_records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return frame.replace({np.nan: None}).to_dict(orient="records")


def _max_lookback(request: RunRequest) -> int:
    if isinstance(request, SearchRequest):
        return max(
            request.volume_window_values + request.bottom_ma_window_values
            + request.volatility_window_values + request.volatility_baseline_window_values
        )
    params = request.params
    return max(
        params.volume_window, params.bottom_ma_window, params.volatility_window,
        params.volatility_baseline_window,
    )


def _prepare_data(request: RunRequest):
    start = _parse_date(request.start_date)
    end = _parse_date(request.end_date, date.today())
    if start >= end:
        raise ValueError("开始日期必须早于结束日期")
    included = _normalize_symbols(request.included_indexes)
    mapping = target_mapping(included=included)
    if included and not mapping:
        raise ValueError("所选标的池没有配置可交易ETF")
    # 趋势补位：仅当趋势可能启用时加载槽位数据（指数恐贪 + ETF行情 + 指数日线），
    # 关闭时完全走原逻辑（不加载多余数据、行为不变）
    def _trend_needed(req: RunRequest) -> bool:
        if isinstance(req, SearchRequest):
            return bool(getattr(req, "trend_enabled_values", [False]) and True in getattr(req, "trend_enabled_values", [False]))
        return bool(getattr(getattr(req, "params", None), "trend_enabled", False))
    trend_slots: list[tuple[str, str]] = []
    trend_indexes: list[str] = []
    trend_etfs: list[str] = []
    if _trend_needed(request):
        trend_slots = _parse_trend_slots(getattr(request.params, "trend_slots", None))
        trend_indexes = [idx for idx, _ in trend_slots]
        trend_etfs = [etf for _, etf in trend_slots]
    padding_days = max(180, _max_lookback(request) * 3, 120)
    feature_start = start - timedelta(days=padding_days)
    all_fear_indexes = list(dict.fromkeys([*mapping, *trend_indexes]))
    fear = load_fear(DB_PATH, all_fear_indexes, feature_start.isoformat(), end.isoformat())
    mapping = {key: value for key, value in mapping.items() if key in set(fear["index_symbol"])}
    if not mapping:
        raise ValueError("所选标的在本地数据库中没有恐贪历史，请先同步最新数据")
    with duckdb.connect(ANALYTICS_DB_PATH, read_only=True) as connection:
        all_etfs = sorted(set([*mapping.values(), *trend_etfs]))
        bars = load_etf_bars(
            connection, all_etfs, feature_start.isoformat(), end.isoformat()
        )
        available = set(bars["etf_symbol"])
        mapping = {key: value for key, value in mapping.items() if value in available}
        # 保留全部（主策略 mapping + 趋势槽位）的恐贪与行情，供 fear_by_date/趋势补位使用
        keep_indexes = set(mapping) | set(trend_indexes)
        keep_etfs = set(mapping.values()) | set(trend_etfs)
        fear = fear[fear["index_symbol"].isin(keep_indexes)].copy()
        bars = bars[bars["etf_symbol"].isin(keep_etfs)].copy()
        # 趋势指数日线（含 QQQ 等美股）
        trend_index = load_trend_index_close(
            connection, trend_indexes, feature_start.isoformat(), end.isoformat()
        )
        benchmark = connection.execute(
            "SELECT trade_date, close FROM a_stock_index_daily "
            "WHERE upper(ts_code)=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [request.benchmark_symbol, start.isoformat(), end.isoformat()],
        ).fetch_df()
        benchmark_source = "index"
        if benchmark.empty:
            benchmark_etf = target_mapping(included={request.benchmark_symbol}).get(
                request.benchmark_symbol
            )
            if benchmark_etf:
                benchmark = connection.execute(
                    "SELECT trade_date, close FROM a_stock_fund_daily_qfq "
                    "WHERE upper(symbol)=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    [benchmark_etf, start.isoformat(), end.isoformat()],
                ).fetch_df()
                benchmark_source = "etf_proxy"
        benchmark.attrs["price_source"] = benchmark_source
    if bars.empty or fear.empty:
        raise ValueError("所选区间没有可用的ETF行情或恐贪历史")
    if benchmark.empty:
        raise ValueError(f"所选区间没有可用的{_benchmark_label(request.benchmark_symbol)}基准行情")
    return start, end, included, mapping, fear, bars, benchmark, trend_index


def _attach_benchmark(curve: pd.DataFrame, benchmark: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    benchmark = benchmark.copy()
    benchmark["date"] = pd.to_datetime(benchmark["trade_date"]).dt.date.astype(str)
    benchmark["close"] = pd.to_numeric(benchmark["close"], errors="coerce")
    benchmark = benchmark.dropna(subset=["close"])
    if benchmark.empty:
        curve["benchmark_value"] = None
        return curve
    benchmark["benchmark_value"] = initial_capital * benchmark["close"] / benchmark.iloc[0]["close"]
    result = curve.merge(benchmark[["date", "benchmark_value"]], on="date", how="left")
    result["benchmark_value"] = result["benchmark_value"].ffill()
    return result


def _features(bars, fear, params: StrategyParams, cache: Optional[dict] = None):
    market_key = (
        params.volume_window, params.volatility_window,
        params.volatility_baseline_window, params.volatility_std_multiplier,
    )
    fear_key = params.bottom_ma_window
    cache = cache if cache is not None else {}
    market_cache = cache.setdefault("market", {})
    fear_cache = cache.setdefault("fear", {})
    if market_key not in market_cache:
        market_cache[market_key] = prepare_market_features(
            bars, volume_window=params.volume_window,
            volatility_window=params.volatility_window,
            volatility_baseline_window=params.volatility_baseline_window,
            volatility_std_multiplier=params.volatility_std_multiplier,
        )
    if fear_key not in fear_cache:
        fear_cache[fear_key] = prepare_fear_features(fear, params.bottom_ma_window)
    return market_cache[market_key], fear_cache[fear_key]


def _run_prepared(
    request: RunRequest, mapping, fear, bars, benchmark, params: StrategyParams,
    detailed: bool = True, feature_cache: Optional[dict] = None,
    trend_index: Optional[pd.DataFrame] = None,
):
    featured_bars, featured_fear = _features(bars, fear, params, feature_cache)
    signals = build_signal_rows(
        featured_bars, featured_fear, mapping,
        extreme_fear_threshold=params.extreme_fear_threshold,
        volume_ratio_threshold=params.volume_ratio_threshold,
        bottom_fear_threshold=params.bottom_fear_threshold,
        extreme_buy_fraction=params.extreme_buy_fraction,
        bottom_buy_fraction=params.bottom_buy_fraction,
        start_date=request.start_date,
        end_date=request.end_date or date.today().isoformat(),
        sort_by_fear=params.sort_by_fear,
    )
    top_signals = None
    if params.top_sell_threshold is not None:
        cache_key = ("top_signals", params.top_sell_threshold, params.bottom_ma_window)
        if feature_cache and cache_key in feature_cache:
            top_signals = feature_cache[cache_key]
        else:
            top_signals = build_top_signals(
                fear, mapping, top_threshold=params.top_sell_threshold,
                bottom_ma_window=params.bottom_ma_window,
                start_date=request.start_date,
                end_date=request.end_date or date.today().isoformat(),
            )
            if feature_cache is not None:
                feature_cache[cache_key] = top_signals
    # 日线/恐贪按日索引：基于特征行情构建（含 realized_volatility 等列），
    # 缓存键含特征参数（不同参数组合的特征列不同）
    market_key = (
        params.volume_window, params.volatility_window,
        params.volatility_baseline_window, params.volatility_std_multiplier,
    )
    bd_key = ("bars_by_date",) + market_key
    fd_key = ("fear_by_date", request.start_date, request.end_date or date.today().isoformat())
    bars_by_date = fear_by_date = None
    if feature_cache is not None:
        if bd_key not in feature_cache:
            _, bars_by_date = build_bars_by_date(
                featured_bars, request.start_date, request.end_date or date.today().isoformat()
            )
            feature_cache[bd_key] = bars_by_date
        else:
            bars_by_date = feature_cache[bd_key]
        if fd_key not in feature_cache:
            fear_by_date = build_fear_by_date(
                fear, request.start_date, request.end_date or date.today().isoformat()
            )
            feature_cache[fd_key] = fear_by_date
        else:
            fear_by_date = feature_cache[fd_key]
    # 趋势补位数据（按参数构建，搜索时缓存）
    trend_slots = _parse_trend_slots(params.trend_slots)
    trend_index_by_date = None
    trend_ma_by_index = None
    if params.trend_enabled and trend_index is not None and not trend_index.empty:
        td_key = ("trend_index_by_date", request.start_date, request.end_date or date.today().isoformat())
        tm_key = ("trend_ma_by_index", params.trend_ma_win)
        if feature_cache is not None:
            if td_key not in feature_cache:
                feature_cache[td_key] = build_trend_index_by_date(
                    trend_index, request.start_date, request.end_date or date.today().isoformat()
                )
            trend_index_by_date = feature_cache[td_key]
            if tm_key not in feature_cache:
                feature_cache[tm_key] = build_trend_ma_by_index(trend_index, params.trend_ma_win)
            trend_ma_by_index = feature_cache[tm_key]
        else:
            trend_index_by_date = build_trend_index_by_date(
                trend_index, request.start_date, request.end_date or date.today().isoformat()
            )
            trend_ma_by_index = build_trend_ma_by_index(trend_index, params.trend_ma_win)
    curve, trades = run_backtest(
        featured_bars, featured_fear, signals,
        start_date=request.start_date, end_date=request.end_date or date.today().isoformat(),
        initial_capital=request.initial_capital, greed_threshold=params.greed_threshold,
        greed_sell_fraction=params.greed_sell_fraction,
        stop_loss=params.stop_loss_pct / 100,
        stop_cooldown_days=params.stop_cooldown_days,
        trailing_drawdown=params.trailing_drawdown_pct / 100,
        commission_pct=params.commission_pct, min_commission=params.min_commission,
        slippage_pct=params.slippage_pct, stamp_duty_pct=params.stamp_duty_pct,
        lot_size=params.lot_size, max_positions=params.max_positions,
        buy_when_flat_only=params.buy_when_flat_only, top_signals=top_signals,
        bars_by_date=bars_by_date, fear_by_date=fear_by_date,
        trend_slots=trend_slots if params.trend_enabled else None,
        trend_ma_win=params.trend_ma_win,
        trend_max_fear=params.trend_max_fear,
        trend_index_by_date=trend_index_by_date if params.trend_enabled else None,
        trend_ma_by_index=trend_ma_by_index if params.trend_enabled else None,
    )
    if detailed:
        # 搜索/快速评估不需要基准曲线，跳过以加速
        curve = _attach_benchmark(curve, benchmark, request.initial_capital)
    summary = summarize(curve, trades, request.initial_capital)
    summary["calmar_ratio"] = (
        summary["annualized_return_pct"] / abs(summary["max_drawdown_pct"])
        if summary.get("max_drawdown_pct") else None
    )
    payload = {
        "summary": summary,
        "params": params.dict(),
        "signal_days": len(signals),
        "benchmark": _benchmark_payload(
            request.benchmark_symbol,
            benchmark.attrs.get("price_source", "index"),
        ),
    }
    if detailed:
        annual = curve.assign(year=pd.to_datetime(curve["date"]).dt.year).groupby("year").agg(
            start_value=("value", "first"), end_value=("value", "last")
        ).reset_index()
        annual["return_pct"] = (annual["end_value"] / annual["start_value"] - 1) * 100
        payload.update({
            "equity_curve": _json_records(curve), "trades": _json_records(trades),
            "yearly_returns": _json_records(annual),
        })
    return payload


def _run_request(request: RunRequest):
    start, end, included, mapping, fear, bars, benchmark, trend_index = _prepare_data(request)
    result = _run_prepared(request, mapping, fear, bars, benchmark, request.params, detailed=True, trend_index=trend_index)
    result["meta"] = {
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "included_indexes": sorted(included), "index_etf_mapping": mapping,
        "benchmark_symbol": request.benchmark_symbol,
        "trading_days": int(bars.loc[
            (bars["trade_date"] >= start) & (bars["trade_date"] <= end), "trade_date"
        ].nunique()),
        "fear_points": int(len(fear)),
    }
    return result


def _candidate_lists(request: SearchRequest) -> list[list[Any]]:
    return [getattr(request, f"{field}_values") for field in SEARCH_FIELDS]


def _combination_count(request: SearchRequest) -> int:
    return math.prod(len(values) for values in _candidate_lists(request))


def _score(item: Dict[str, Any], objective: str):
    summary = item["summary"]
    primary = summary.get(objective)
    return (
        float(primary) if primary is not None else -math.inf,
        float(summary.get("annualized_return_pct") or -math.inf),
        float(summary.get("sharpe_zero_rf") or -math.inf),
        -abs(float(summary.get("max_drawdown_pct") or math.inf)),
    )


def _update_job(task_id: str, **updates):
    with SEARCH_LOCK:
        job = SEARCH_JOBS.get(task_id)
        if not job:
            return
        job.update(updates)
        account_id = job.get("account_id")
        payload = SearchJobStatus(**job).dict()
    publish_event(account_id, "a_stock_fear_etf_search", payload)


def _search_job(task_id: str, request: SearchRequest):
    try:
        _update_job(task_id, status="running", message="正在加载ETF行情和恐贪历史")
        total = _combination_count(request)
        combinations = list(product(*_candidate_lists(request)))
        start, end, included, mapping, fear, bars, benchmark, trend_index = _prepare_data(request)
        results: list[dict[str, Any]] = []
        skipped = 0
        feature_cache: dict[str, dict] = {}
        for index, values in enumerate(combinations, start=1):
            try:
                params = StrategyParams(**dict(zip(SEARCH_FIELDS, values)))
                item = _run_prepared(
                    request, mapping, fear, bars, benchmark, params,
                    detailed=False, feature_cache=feature_cache, trend_index=trend_index,
                )
                results.append(item)
            except Exception as exc:
                import traceback
                print(f"[search] 组合 {values} 失败: {exc}\n{traceback.format_exc()}", flush=True)
                skipped += 1
            if index == total or index % max(1, total // 100) == 0:
                _update_job(
                    task_id, progress=int(index * 100 / total), processed_combinations=index,
                    skipped_combinations=skipped, message=f"已完成 {index}/{total} 组",
                )
        results.sort(key=lambda item: _score(item, request.objective), reverse=True)
        top_results = results[: request.top_n]
        best_detail = None
        if top_results:
            best_detail = _run_prepared(
                request, mapping, fear, bars, benchmark,
                StrategyParams(**top_results[0]["params"]), detailed=True,
                feature_cache=feature_cache, trend_index=trend_index,
            )
            best_detail["meta"] = {
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "included_indexes": sorted(included), "index_etf_mapping": mapping,
                "benchmark_symbol": request.benchmark_symbol,
            }
        _update_job(
            task_id, status="completed", progress=100, processed_combinations=total,
            skipped_combinations=skipped, message="参数搜索完成",
            result={
                "meta": {"total_combinations": total, "objective": request.objective},
                "results": top_results, "best_result": best_detail,
            },
        )
    except Exception as exc:
        _update_job(task_id, status="failed", error=str(exc), message="参数搜索失败")


@router.get("/options")
def options(account_id: str = Depends(valid_account)):
    targets = _target_options()
    default_request = RunRequest().dict()
    default_request["included_indexes"] = list(RECOMMENDED_INDEXES)
    return {
        "targets": targets, "max_search_combinations": MAX_SEARCH_COMBINATIONS,
        "default_request": default_request, "search_fields": list(SEARCH_FIELDS),
    }


@router.post("/run")
def run(payload: RunRequest, account_id: str = Depends(valid_account)):
    try:
        return _run_request(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/search/jobs", response_model=SearchJobCreated)
def create_search_job(payload: SearchRequest, account_id: str = Depends(valid_account)):
    total = _combination_count(payload)
    if total <= 0 or total > MAX_SEARCH_COMBINATIONS:
        raise HTTPException(status_code=400, detail=f"参数组合数必须在1到{MAX_SEARCH_COMBINATIONS}之间")
    task_id = uuid.uuid4().hex
    with SEARCH_LOCK:
        SEARCH_JOBS[task_id] = {
            "task_id": task_id, "account_id": account_id, "status": "pending",
            "progress": 0, "processed_combinations": 0, "total_combinations": total,
            "skipped_combinations": 0, "message": "任务已创建", "result": None, "error": None,
        }
    SEARCH_EXECUTOR.submit(_search_job, task_id, payload)
    return SearchJobCreated(task_id=task_id, status="pending", total_combinations=total)


@router.get("/search/jobs/{task_id}", response_model=SearchJobStatus)
def get_search_job(task_id: str, account_id: str = Depends(valid_account)):
    with SEARCH_LOCK:
        job = SEARCH_JOBS.get(task_id)
        if not job or job.get("account_id") != account_id:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        return SearchJobStatus(**job)
