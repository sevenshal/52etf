import logging
import os
import re
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from itertools import product
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, root_validator, validator

from ...core.database import LongPortAccount, Session
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from .account import valid_account

router = APIRouter(prefix="/api/w20-momentum-backtest", tags=["W20 Momentum Backtest"])
logger = logging.getLogger(__name__)

JOBS: Dict[str, Dict] = {}
JOBS_LOCK = threading.Lock()

DEFAULT_SYMBOLS = [
    "513100.SH",
    "159934.SZ",
    "563360.SH",
    "159915.SZ",
    "588230.SH",
    "510500.SH",
    "510880.SH",
    "515220.SH",
]

DEFAULT_BENCHMARKS = [
    "510300.SH",
    "510500.SH",
    "513100.SH",
]


def _normalize_symbol(symbol: str) -> str:
    raw = (symbol or "").strip().upper()
    if not raw:
        return raw
    if "." in raw:
        left, right = raw.split(".", 1)
        if left in {"SH", "SZ", "BJ"} and right:
            return f"{right}.{left}"
        return raw
    if len(raw) > 2 and raw[:2] in {"SH", "SZ", "BJ"} and raw[2:]:
        return f"{raw[2:]}.{raw[:2]}"
    return raw


def _parse_date(value: Optional[str], default: Optional[date] = None) -> date:
    if not value:
        if default is None:
            raise ValueError("日期不能为空")
        return default
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_float_list(value) -> List[float]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,，]", value)
    else:
        items = list(value)
    parsed: List[float] = []
    for item in items:
        if item is None or item == "":
            continue
        parsed.append(float(item))
    return parsed


def _parse_semicolon_items(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[;；\n]+", value)
    else:
        items = list(value)
    parsed = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            parsed.append(text)
    return list(dict.fromkeys(parsed))


def _parse_numeric_candidate_items(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[;；,，\n]+", value)
    else:
        items = list(value)
    parsed = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            parsed.append(text)
    return list(dict.fromkeys(parsed))


def _parse_int_candidates(value) -> List[int]:
    parsed = []
    for item in _parse_numeric_candidate_items(value):
        parsed.append(int(item))
    return list(dict.fromkeys(parsed))


def _parse_float_candidates(value) -> List[float]:
    parsed = []
    for item in _parse_numeric_candidate_items(value):
        parsed.append(float(item))
    deduped: List[float] = []
    seen = set()
    for item in parsed:
        key = round(item, 10)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _parse_weight_candidates(value) -> List[List[float]]:
    candidates = []
    if isinstance(value, (list, tuple)) and any(isinstance(item, (list, tuple)) for item in value):
        for item in value:
            weights = _parse_float_list(item)
            if weights:
                candidates.append(weights)
    else:
        for item in _parse_semicolon_items(value):
            weights = _parse_float_list(item)
            if weights:
                candidates.append(weights)
    deduped: List[List[float]] = []
    seen = set()
    for weights in candidates:
        key = tuple(round(weight, 10) for weight in weights)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(weights)
    return deduped


def _normalize_frequency(value: str) -> str:
    normalized = (value or "").strip().lower()
    aliases = {
        "每日": "daily",
        "每天": "daily",
        "日": "daily",
        "daily": "daily",
        "day": "daily",
        "dayly": "daily",
        "daliy": "daily",
        "每周": "weekly",
        "周": "weekly",
        "weekly": "weekly",
        "week": "weekly",
        "每月": "monthly",
        "月": "monthly",
        "monthly": "monthly",
        "month": "monthly",
    }
    return aliases.get(normalized, normalized)


def _parse_symbol_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    normalized = []
    for item in items:
        if item is None:
            continue
        symbol = _normalize_symbol(str(item))
        if symbol:
            normalized.append(symbol)
    # preserve order but deduplicate
    return list(dict.fromkeys(normalized))


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


def _is_valid_price(value) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(numeric) and numeric > 0)


def _price_or_zero(value) -> float:
    return float(value) if _is_valid_price(value) else 0.0


def _repair_split_like_price_jumps(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Repair obvious ETF split jumps when the provider returns unadjusted prices.

    LongPort's date-range K-line API can return CN ETF history with split-like
    discontinuities even when ForwardAdjust is requested. A real ETF overnight
    move of 50%+ is extremely unlikely, so we only adjust when the open-to-prev
    close gap is extreme while the same-day open-to-close move is ordinary.
    """
    if frame.empty:
        return frame

    adjusted = frame.copy()
    price_columns = ["open", "high", "low", "close"]
    adjustments = []
    price_col_indexes = [adjusted.columns.get_loc(column) for column in price_columns]

    for index in range(1, len(adjusted)):
        prev_close = float(adjusted.iloc[index - 1]["close"])
        current_open = float(adjusted.iloc[index]["open"])
        current_close = float(adjusted.iloc[index]["close"])
        if prev_close <= 0 or current_open <= 0 or current_close <= 0:
            continue

        open_gap = current_open / prev_close
        close_gap = current_close / prev_close
        intraday_move = abs(current_close / current_open - 1)
        split_down = open_gap < 0.55 and close_gap < 0.70
        split_up = open_gap > 1.80 and close_gap > 1.50
        if intraday_move > 0.20 or not (split_down or split_up):
            continue

        factor = open_gap
        adjusted.iloc[:index, price_col_indexes] = adjusted.iloc[:index, price_col_indexes] * factor
        adjustments.append(
            {
                "symbol": symbol,
                "date": adjusted.index[index].isoformat(),
                "factor": _round_or_none(factor, 8),
                "prev_close_before_adjustment": _round_or_none(prev_close, 6),
                "current_open": _round_or_none(current_open, 6),
                "current_close": _round_or_none(current_close, 6),
            }
        )

    adjusted.attrs["split_like_adjustments"] = adjustments
    if adjustments:
        logger.info("Repaired split-like price jumps for %s: %s", symbol, adjustments)
    return adjusted


class W20MomentumBacktestParams(BaseModel):
    symbols: List[str] = Field(default_factory=lambda: DEFAULT_SYMBOLS.copy())
    benchmark_symbols: List[str] = Field(default_factory=lambda: DEFAULT_BENCHMARKS.copy())
    initial_capital: float = 1_000_000.0
    start_date: str = "2018-01-02"
    end_date: Optional[str] = None
    window: int = 20
    top_n: int = 2
    top_weights: List[float] = Field(default_factory=lambda: [70.0, 30.0])
    rebalance_frequency: str = "weekly"
    drift_threshold_pct: float = 100.0
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    lot_size: int = 100

    @validator("symbols", "benchmark_symbols", pre=True)
    def validate_symbol_list(cls, value):
        parsed = _parse_symbol_list(value)
        if not parsed:
            raise ValueError("至少需要一个标的")
        return parsed

    @validator("top_weights", pre=True)
    def validate_top_weights(cls, value):
        parsed = _parse_float_list(value)
        if not parsed:
            raise ValueError("top_weights 不能为空")
        return parsed

    @validator("rebalance_frequency")
    def validate_rebalance_frequency(cls, value):
        normalized = _normalize_frequency(value)
        if normalized not in {"daily", "weekly", "monthly"}:
            raise ValueError("rebalance_frequency 仅支持 daily、weekly、monthly")
        return normalized

    @validator("window")
    def validate_window(cls, value):
        if value < 2:
            raise ValueError("window 不能小于 2")
        return value

    @validator("top_n")
    def validate_top_n(cls, value):
        if value < 1:
            raise ValueError("top_n 不能小于 1")
        return value

    @validator("initial_capital", "drift_threshold_pct", "commission_pct", "slippage_pct")
    def validate_non_negative(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("lot_size")
    def validate_lot_size(cls, value):
        if value < 1:
            raise ValueError("lot_size 不能小于 1")
        return value

    @root_validator(skip_on_failure=True)
    def validate_weight_shape(cls, values):
        top_n = values.get("top_n") or 0
        top_weights = values.get("top_weights") or []
        if len(top_weights) != top_n:
            raise ValueError("top_weights 的长度必须与 top_n 一致")
        if sum(top_weights) <= 0:
            raise ValueError("top_weights 的和必须大于 0")
        if top_n > len(values.get("symbols") or []):
            raise ValueError("top_n 不能大于 symbols 数量")
        return values


class W20MomentumBatchBacktestParams(BaseModel):
    symbols: List[str] = Field(default_factory=lambda: DEFAULT_SYMBOLS.copy())
    benchmark_symbols: List[str] = Field(default_factory=lambda: DEFAULT_BENCHMARKS.copy())
    initial_capital: float = 1_000_000.0
    start_date: str = "2018-01-02"
    end_date: Optional[str] = None
    window_values: Any = Field(default_factory=lambda: [20])
    top_weights_values: Any = Field(default_factory=lambda: [[70.0, 30.0]])
    rebalance_frequency_values: Any = Field(default_factory=lambda: ["weekly"])
    drift_threshold_pct_values: Any = Field(default_factory=lambda: [100.0])
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    lot_size: int = 100
    eval_workers: Optional[int] = None
    max_results: int = 200
    max_combinations: int = 2000

    @validator("symbols", "benchmark_symbols", pre=True)
    def validate_symbol_list(cls, value):
        parsed = _parse_symbol_list(value)
        if not parsed:
            raise ValueError("至少需要一个标的")
        return parsed

    @validator("window_values", pre=True)
    def validate_window_values(cls, value):
        parsed = _parse_int_candidates(value)
        if not parsed:
            raise ValueError("回归窗口候选不能为空")
        if any(item < 2 for item in parsed):
            raise ValueError("回归窗口不能小于 2")
        return parsed

    @validator("top_weights_values", pre=True)
    def validate_top_weights_values(cls, value):
        parsed = _parse_weight_candidates(value)
        if not parsed:
            raise ValueError("前N权重候选不能为空")
        return parsed

    @validator("rebalance_frequency_values", pre=True)
    def validate_rebalance_frequency_values(cls, value):
        parsed = [_normalize_frequency(item) for item in _parse_semicolon_items(value)]
        if not parsed:
            raise ValueError("再平衡频率候选不能为空")
        invalid = [item for item in parsed if item not in {"daily", "weekly", "monthly"}]
        if invalid:
            raise ValueError(f"再平衡频率仅支持 daily、weekly、monthly: {', '.join(invalid)}")
        return list(dict.fromkeys(parsed))

    @validator("drift_threshold_pct_values", pre=True)
    def validate_drift_threshold_pct_values(cls, value):
        parsed = _parse_float_candidates(value)
        if not parsed:
            raise ValueError("漂移阈值候选不能为空")
        if any(item < 0 for item in parsed):
            raise ValueError("漂移阈值不能为负数")
        return parsed

    @validator("initial_capital", "commission_pct", "slippage_pct")
    def validate_non_negative(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("lot_size")
    def validate_lot_size(cls, value):
        if value < 1:
            raise ValueError("lot_size 不能小于 1")
        return value

    @validator("eval_workers")
    def validate_eval_workers(cls, value):
        if value is None:
            return value
        if value < 1:
            raise ValueError("并发进程数不能小于 1")
        return value

    @validator("max_results")
    def validate_max_results(cls, value):
        if value < 1:
            raise ValueError("max_results 不能小于 1")
        return min(value, 200)

    @validator("max_combinations")
    def validate_max_combinations(cls, value):
        if value < 1:
            raise ValueError("max_combinations 不能小于 1")
        return value

    @root_validator(skip_on_failure=True)
    def validate_batch_shape(cls, values):
        symbols = values.get("symbols") or []
        for weights in values.get("top_weights_values") or []:
            weights_text = ",".join(str(weight) for weight in weights)
            if len(weights) < 1:
                raise ValueError("每组 top_weights 至少需要一个权重")
            if len(weights) > len(symbols):
                raise ValueError(f"权重候选 {weights_text} 表示 Top{len(weights)}，但当前策略标的池只有 {len(symbols)} 个标的")
            if sum(weights) <= 0:
                raise ValueError(f"权重候选 {weights_text} 的和必须大于 0")
        total_combinations = (
            len(values.get("window_values") or [])
            * len(values.get("top_weights_values") or [])
            * len(values.get("rebalance_frequency_values") or [])
            * len(values.get("drift_threshold_pct_values") or [])
        )
        max_combinations = values.get("max_combinations") or 0
        if total_combinations > max_combinations:
            raise ValueError(f"候选组合数 {total_combinations} 超过上限 {max_combinations}")
        return values


class W20MomentumBacktestJobCreated(BaseModel):
    task_id: str
    status: str


class W20MomentumBatchJobCreated(BaseModel):
    task_id: str
    status: str
    total_combinations: int


class W20MomentumBacktestJobStatus(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    message: Optional[str] = None
    result: Optional[Dict] = None
    error: Optional[str] = None
    processed_combinations: int = 0
    total_combinations: int = 0
    eval_workers: int = 1


def _get_longport_account_id(account_id: str) -> str:
    db = Session()
    try:
        account = db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).first()
        if account and account.lp_account_id:
            return account.lp_account_id
    finally:
        Session.remove()
    return "LBPT10001248"


def _get_quote_service(account_id: str) -> QuoteService:
    lp_account_id = _get_longport_account_id(account_id)
    return QuoteService(LongPortService.get_instance(lp_account_id))


def _update_job(task_id: str, **kwargs) -> None:
    with JOBS_LOCK:
        job = JOBS.setdefault(task_id, {})
        job.update(kwargs)


def _get_job(task_id: str) -> Dict:
    with JOBS_LOCK:
        return dict(JOBS.get(task_id, {}))


def _build_price_frame(quote_service: QuoteService, symbol: str, start_dt: date, end_dt: date) -> pd.DataFrame:
    klines = quote_service.get_klines(symbol, start_date=start_dt, end_date=end_dt, period="d")
    if not klines:
        raise ValueError(f"{symbol} 在指定区间内没有可用K线数据")
    frame = pd.DataFrame([
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
    ])
    frame = frame.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    frame = frame.set_index("date")
    frame = _repair_split_like_price_jumps(frame, symbol)
    return frame


def _compute_momentum_snapshot(close_series: pd.Series, as_of: date, window: int) -> Optional[Dict]:
    history = close_series.loc[:as_of].dropna().astype(float)
    if len(history) < window:
        return None

    window_closes = history.tail(window).to_numpy(dtype=float)
    if np.any(window_closes <= 0):
        return None

    x = np.arange(window, dtype=float)
    y = np.log(window_closes)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 0.0 if ss_tot <= 0 else max(0.0, 1.0 - (ss_res / ss_tot))

    daily_returns = np.diff(window_closes) / window_closes[:-1]
    annualized_vol_pct = float(np.std(daily_returns, ddof=1) * np.sqrt(252) * 100) if len(daily_returns) > 1 else 0.0
    annualized_slope_pct = float(slope * 252 * 100)
    raw_score = float(annualized_slope_pct * r_squared)
    risk_adjusted_score = float(raw_score / annualized_vol_pct * 100) if annualized_vol_pct > 0 else None
    window_return_pct = float((window_closes[-1] / window_closes[0] - 1) * 100)

    return {
        "as_of": as_of.isoformat(),
        "window": window,
        "window_return_pct": _round_or_none(window_return_pct, 4),
        "annualized_slope_pct": _round_or_none(annualized_slope_pct, 4),
        "r_squared": _round_or_none(r_squared, 6),
        "raw_score": _round_or_none(raw_score, 4),
        "annualized_volatility_pct": _round_or_none(annualized_vol_pct, 4),
        "risk_adjusted_score": _round_or_none(risk_adjusted_score, 4),
    }


def _is_signal_day(dates: List[date], index: int) -> bool:
    if index >= len(dates) - 1:
        return True
    return (dates[index + 1] - dates[index]).days > 1


def _is_rebalance_day(dates: List[date], index: int, frequency: str) -> bool:
    if frequency == "daily":
        return True
    if index >= len(dates) - 1:
        return True
    if frequency == "weekly":
        return _is_signal_day(dates, index)
    if frequency == "monthly":
        return dates[index].month != dates[index + 1].month
    return False


def _compute_portfolio_value(positions: Dict[str, int], prices: Dict[str, float], cash: float) -> float:
    return float(cash + sum(positions.get(symbol, 0) * _price_or_zero(prices.get(symbol, 0.0)) for symbol in positions))


def _weights_from_shares(positions: Dict[str, int], prices: Dict[str, float], cash: float) -> Dict[str, float]:
    portfolio_value = _compute_portfolio_value(positions, prices, cash)
    if portfolio_value <= 0:
        return {symbol: 0.0 for symbol in positions}
    return {
        symbol: (positions.get(symbol, 0) * _price_or_zero(prices.get(symbol, 0.0))) / portfolio_value
        for symbol in positions
    }


def _build_target_shares(
    portfolio_value: float,
    current_positions: Dict[str, int],
    target_symbols: List[str],
    target_weights: List[float],
    close_prices: Dict[str, float],
    lot_size: int,
    commission_pct: float,
) -> Dict[str, int]:
    target_map: Dict[str, int] = {symbol: 0 for symbol in current_positions}
    for symbol in target_symbols:
        target_map.setdefault(symbol, 0)

    for symbol in current_positions:
        if symbol not in target_symbols:
            target_map[symbol] = 0

    total_weight = sum(target_weights)
    normalized_weights = [weight / total_weight for weight in target_weights]
    for symbol, weight in zip(target_symbols, normalized_weights):
        price = _price_or_zero(close_prices.get(symbol, 0.0))
        if price <= 0:
            continue
        target_value = portfolio_value * weight
        conservative_unit_cost = price * (1 + commission_pct / 100.0)
        if conservative_unit_cost <= 0:
            continue
        target_shares = int(target_value / conservative_unit_cost / lot_size) * lot_size
        target_map[symbol] = max(0, target_shares)
    return target_map


def _target_weights_pct(symbols: List[str], weights: Dict[str, float]) -> List[float]:
    return [_round_or_none(float(weights.get(symbol, 0.0)) * 100, 4) for symbol in symbols]


def _format_target_allocations(symbols: List[str], weights: Dict[str, float]) -> str:
    if not symbols:
        return "空仓"
    return " / ".join(f"{symbol} {float(weights.get(symbol, 0.0)) * 100:.2f}%" for symbol in symbols)


def _target_weights_changed(
    symbols: List[str],
    previous_weights: Dict[str, float],
    target_weights: Dict[str, float],
) -> bool:
    return any(abs(float(previous_weights.get(symbol, 0.0)) - float(target_weights.get(symbol, 0.0))) > 1e-9 for symbol in symbols)


def _build_target_change_metadata(
    previous_order: Tuple[str, ...],
    previous_weights: Dict[str, float],
    target_order: Tuple[str, ...],
    target_weights: Dict[str, float],
    signal_date: date,
) -> Dict:
    previous_symbols = list(previous_order or tuple())
    target_symbols = list(target_order or tuple())
    previous_text = _format_target_allocations(previous_symbols, previous_weights)
    target_text = _format_target_allocations(target_symbols, target_weights)
    metadata = {
        "trigger_type": "ranking",
        "signal_date": signal_date.isoformat(),
        "previous_target_symbols": previous_symbols,
        "previous_target_weights_pct": _target_weights_pct(previous_symbols, previous_weights),
        "target_symbols": target_symbols,
        "target_weights_pct": _target_weights_pct(target_symbols, target_weights),
    }

    if not previous_symbols:
        metadata.update(
            {
                "reason": "initial_entry",
                "reason_detail": f"首次建仓: 目标 {target_text}",
            }
        )
        return metadata

    previous_set = set(previous_symbols)
    target_set = set(target_symbols)
    if previous_set != target_set:
        added = [symbol for symbol in target_symbols if symbol not in previous_set]
        removed = [symbol for symbol in previous_symbols if symbol not in target_set]
        change_parts = []
        if added:
            change_parts.append(f"新增 {', '.join(added)}")
        if removed:
            change_parts.append(f"移出 {', '.join(removed)}")
        metadata.update(
            {
                "reason": "basket_symbols_changed",
                "reason_detail": f"Top{len(target_symbols)} 标的变化: {'; '.join(change_parts)}; 旧目标 {previous_text} -> 新目标 {target_text}",
            }
        )
        return metadata

    if tuple(previous_symbols) != tuple(target_symbols):
        metadata.update(
            {
                "reason": "rank_order_changed",
                "reason_detail": f"Top{len(target_symbols)} 排名顺序变化: 旧目标 {previous_text} -> 新目标 {target_text}",
            }
        )
        return metadata

    if _target_weights_changed(target_symbols, previous_weights, target_weights):
        metadata.update(
            {
                "reason": "target_weights_changed",
                "reason_detail": f"目标权重变化: 旧目标 {previous_text} -> 新目标 {target_text}",
            }
        )
        return metadata

    metadata.update(
        {
            "reason": "target_refresh",
            "reason_detail": f"排名日目标未变化: {target_text}",
        }
    )
    return metadata


def _build_drift_metadata(
    positions: Dict[str, int],
    prices: Dict[str, float],
    cash: float,
    target_order: Tuple[str, ...],
    target_weights: Dict[str, float],
    threshold_pct: float,
    signal_date: date,
) -> Tuple[bool, Dict]:
    actual_weights = _weights_from_shares(positions, prices, cash)
    drift_details = []
    for symbol in target_order:
        target_weight_pct = float(target_weights.get(symbol, 0.0)) * 100
        if target_weight_pct <= 0:
            continue
        actual_weight_pct = float(actual_weights.get(symbol, 0.0)) * 100
        drift_abs_pct = actual_weight_pct - target_weight_pct
        drift_pct = abs(drift_abs_pct)
        drift_relative_pct = abs(drift_abs_pct) / target_weight_pct * 100
        drift_details.append(
            {
                "symbol": symbol,
                "actual_weight_pct": _round_or_none(actual_weight_pct, 4),
                "target_weight_pct": _round_or_none(target_weight_pct, 4),
                "drift_abs_pct": _round_or_none(drift_abs_pct, 4),
                "drift_pct": _round_or_none(drift_pct, 4),
                "drift_relative_pct": _round_or_none(drift_relative_pct, 4),
            }
        )

    max_drift = max(drift_details, key=lambda item: float(item.get("drift_pct") or 0.0), default=None)
    triggered = bool(max_drift and float(max_drift.get("drift_pct") or 0.0) > threshold_pct)
    target_symbols = list(target_order or tuple())
    metadata = {
        "reason": "drift_threshold",
        "trigger_type": "drift",
        "signal_date": signal_date.isoformat(),
        "previous_target_symbols": target_symbols,
        "previous_target_weights_pct": _target_weights_pct(target_symbols, target_weights),
        "target_symbols": target_symbols,
        "target_weights_pct": _target_weights_pct(target_symbols, target_weights),
        "drift_threshold_pct": _round_or_none(threshold_pct, 4),
        "drift_details": drift_details,
    }
    if max_drift:
        comparator = ">" if triggered else "<="
        trigger_text = "漂移阈值触发" if triggered else "未达到漂移阈值"
        metadata.update(
            {
                "drift_symbol": max_drift.get("symbol"),
                "drift_pct": max_drift.get("drift_pct"),
                "drift_relative_pct": max_drift.get("drift_relative_pct"),
                "drift_abs_pct": max_drift.get("drift_abs_pct"),
                "actual_weight_pct": max_drift.get("actual_weight_pct"),
                "target_weight_pct": max_drift.get("target_weight_pct"),
                "reason_detail": (
                    f"{trigger_text}: {max_drift.get('symbol')} 实际 {float(max_drift.get('actual_weight_pct') or 0.0):.2f}% "
                    f"/ 目标 {float(max_drift.get('target_weight_pct') or 0.0):.2f}%, "
                    f"偏离 {float(max_drift.get('drift_abs_pct') or 0.0):+.2f}%, "
                    f"绝对漂移 {float(max_drift.get('drift_pct') or 0.0):.2f}% {comparator} 阈值 {threshold_pct:.2f}%"
                ),
            }
        )
    else:
        metadata["reason_detail"] = f"漂移阈值触发: 没有可比较的目标权重, 阈值 {threshold_pct:.2f}%"
    return triggered, metadata


def _merge_target_change_and_drift_metadata(target_metadata: Dict, drift_metadata: Dict) -> Dict:
    combined = dict(target_metadata)
    for key in (
        "drift_threshold_pct",
        "drift_symbol",
        "drift_pct",
        "drift_relative_pct",
        "drift_abs_pct",
        "actual_weight_pct",
        "target_weight_pct",
        "drift_details",
    ):
        if key in drift_metadata:
            combined[key] = drift_metadata.get(key)
    combined["trigger_type"] = "ranking_and_drift"
    if drift_metadata.get("reason_detail"):
        combined["reason_detail"] = f"{target_metadata.get('reason_detail', '')}; 已达到漂移阈值: {drift_metadata['reason_detail'].replace('漂移阈值触发: ', '')}"
    return combined


def _simulate_equal_weight_benchmark(
    close_matrix: pd.DataFrame,
    open_matrix: pd.DataFrame,
    dates: List[date],
    start_date: date,
    end_date: date,
    window: int,
    initial_capital: float,
    lot_size: int,
    commission_pct: float,
    slippage_pct: float,
) -> Tuple[List[Dict], Dict]:
    if close_matrix.empty:
        return [], {"trade_count": 0, "buy_trade_count": 0, "sell_trade_count": 0}

    symbols = list(close_matrix.columns)
    cash = initial_capital
    positions = {symbol: 0 for symbol in symbols}
    curve: List[Dict] = []
    last_close_prices: Dict[str, float] = {}
    current_target_symbols: Tuple[str, ...] = tuple()
    pending_target_symbols: Optional[Tuple[str, ...]] = None
    pending_orders: List[Dict] = []
    trade_count = 0
    buy_trade_count = 0
    sell_trade_count = 0

    for idx, current_date in enumerate(dates):
        if current_date > end_date:
            break
        open_prices = {symbol: _price_or_zero(open_matrix.iloc[idx][symbol]) for symbol in symbols}
        close_prices: Dict[str, float] = {}
        for symbol in symbols:
            close_price = _price_or_zero(close_matrix.iloc[idx][symbol])
            if close_price > 0:
                last_close_prices[symbol] = close_price
            close_prices[symbol] = close_price if close_price > 0 else float(last_close_prices.get(symbol, 0.0))

        if current_date < start_date:
            last_close_prices = close_prices
            continue

        if pending_orders:
            sells = [order for order in pending_orders if order["action"] == "SELL"]
            buys = [order for order in pending_orders if order["action"] == "BUY"]
            for order in sells + buys:
                symbol = order["symbol"]
                quantity = int(order.get("quantity") or 0)
                if quantity <= 0:
                    continue
                open_price = _price_or_zero(open_prices.get(symbol, 0.0))
                if open_price <= 0:
                    continue

                if order["action"] == "SELL":
                    fill_price = open_price * (1 - slippage_pct / 100.0)
                    notional = quantity * fill_price
                    commission = notional * commission_pct / 100.0
                    cash += notional - commission
                    positions[symbol] = max(0, positions.get(symbol, 0) - quantity)
                    if current_date >= start_date:
                        sell_trade_count += 1
                        trade_count += 1
                else:
                    fill_price = open_price * (1 + slippage_pct / 100.0)
                    notional = quantity * fill_price
                    commission = notional * commission_pct / 100.0
                    total_cost = notional + commission
                    per_share_cost = fill_price * (1 + commission_pct / 100.0)
                    affordable = int(cash / per_share_cost / lot_size) * lot_size if per_share_cost > 0 else 0
                    if affordable <= 0:
                        continue
                    if affordable < quantity:
                        quantity = affordable
                        notional = quantity * fill_price
                        commission = notional * commission_pct / 100.0
                        total_cost = notional + commission
                    cash -= total_cost
                    positions[symbol] = positions.get(symbol, 0) + quantity
                    if current_date >= start_date:
                        buy_trade_count += 1
                        trade_count += 1

            current_target_symbols = pending_target_symbols or current_target_symbols
            pending_target_symbols = None
            pending_orders = []

        portfolio_value = _compute_portfolio_value(positions, close_prices, cash)
        if current_date >= start_date:
            curve.append({"date": current_date.isoformat(), "value": _round_or_none(portfolio_value, 4)})

        if idx >= len(dates) - 1:
            continue

        eligible_symbols = []
        for symbol in symbols:
            if not _is_valid_price(close_matrix.iloc[idx][symbol]):
                continue
            if _compute_momentum_snapshot(close_matrix[symbol], current_date, window) is None:
                continue
            eligible_symbols.append(symbol)

        target_symbols_tuple = tuple(eligible_symbols)
        if target_symbols_tuple == current_target_symbols:
            continue

        target_weights = [1.0 / len(eligible_symbols) for _ in eligible_symbols] if eligible_symbols else []
        target_shares = _build_target_shares(
            portfolio_value=portfolio_value,
            current_positions=positions,
            target_symbols=eligible_symbols,
            target_weights=target_weights,
            close_prices=close_prices,
            lot_size=lot_size,
            commission_pct=commission_pct,
        )
        orders: List[Dict] = []
        for symbol in symbols:
            desired = int(target_shares.get(symbol, 0))
            current_shares = int(positions.get(symbol, 0))
            delta = desired - current_shares
            if delta > 0:
                orders.append({"action": "BUY", "symbol": symbol, "quantity": delta})
            elif delta < 0:
                orders.append({"action": "SELL", "symbol": symbol, "quantity": abs(delta)})
        pending_target_symbols = target_symbols_tuple
        pending_orders = orders
        if not pending_orders:
            current_target_symbols = target_symbols_tuple
            pending_target_symbols = None

    return curve, {
        "trade_count": trade_count,
        "buy_trade_count": buy_trade_count,
        "sell_trade_count": sell_trade_count,
    }


def _compute_benchmark_metrics_from_curve(curve: List[Dict]) -> Dict:
    if not curve:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "calmar_ratio": 0.0,
        }

    values = pd.Series([float(item["value"]) for item in curve])
    dates = [pd.to_datetime(item["date"]) for item in curve]
    returns = values.pct_change().dropna()
    total_return = float((values.iloc[-1] / values.iloc[0] - 1) * 100) if values.iloc[0] > 0 else 0.0
    days = max(1, (dates[-1] - dates[0]).days)
    annualized_return = float(((values.iloc[-1] / values.iloc[0]) ** (365 / days) - 1) * 100) if values.iloc[0] > 0 else 0.0
    annualized_volatility = float(returns.std(ddof=1) * np.sqrt(252) * 100) if len(returns) > 1 else 0.0
    drawdown = (values / values.cummax() - 1) * 100
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    sharpe_ratio = float((returns.mean() / returns.std(ddof=1)) * np.sqrt(252)) if len(returns) > 1 and returns.std(ddof=1) > 0 else 0.0
    calmar_ratio = float(annualized_return / abs(max_drawdown)) if max_drawdown < 0 else 0.0
    return {
        "total_return": _round_or_none(total_return, 4),
        "annualized_return": _round_or_none(annualized_return, 4),
        "annualized_volatility": _round_or_none(annualized_volatility, 4),
        "max_drawdown": _round_or_none(max_drawdown, 4),
        "sharpe_ratio": _round_or_none(sharpe_ratio, 4),
        "calmar_ratio": _round_or_none(calmar_ratio, 4),
    }


def _compute_annual_return_map(curve: List[Dict]) -> Dict[int, float]:
    if not curve:
        return {}
    frame = pd.DataFrame(curve)
    if frame.empty or "date" not in frame or "value" not in frame:
        return {}
    frame["date"] = pd.to_datetime(frame["date"])
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["date", "value"]).sort_values("date")
    if frame.empty:
        return {}

    annual_returns: Dict[int, float] = {}
    for year, group in frame.groupby(frame["date"].dt.year):
        if group.empty:
            continue
        start_value = float(group.iloc[0]["value"])
        end_value = float(group.iloc[-1]["value"])
        annual_returns[int(year)] = _round_or_none((end_value / start_value - 1) * 100 if start_value > 0 else 0.0, 4)
    return annual_returns


def _simulate_symbol_buy_hold(
    frame: pd.DataFrame,
    start_date: date,
    end_date: date,
    initial_capital: float,
    lot_size: int,
    commission_pct: float,
    slippage_pct: float,
) -> Tuple[List[Dict], Dict]:
    if frame.empty:
        return [], {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "annualized_volatility": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "calmar_ratio": 0.0,
            "effective_start_date": None,
            "effective_end_date": None,
            "trading_days": 0,
        }

    dates = list(frame.index)
    symbol = frame.columns[0]
    cash = initial_capital
    shares = 0
    curve: List[Dict] = []
    bought = False

    for idx, current_date in enumerate(dates):
        if current_date < start_date:
            continue
        if current_date > end_date:
            break
        open_price = _price_or_zero(frame.iloc[idx]["open"])
        close_price = _price_or_zero(frame.iloc[idx]["close"])
        if not bought and open_price > 0:
            fill_price = open_price * (1 + slippage_pct / 100.0)
            unit_cost = fill_price * (1 + commission_pct / 100.0)
            quantity = int(cash / unit_cost / lot_size) * lot_size
            if quantity > 0:
                notional = quantity * fill_price
                commission = notional * commission_pct / 100.0
                cash -= notional + commission
                shares = quantity
                bought = True
        value = cash + shares * close_price
        curve.append(
            {
                "date": current_date.isoformat(),
                "value": _round_or_none(value, 4),
            }
        )

    metrics = _compute_benchmark_metrics_from_curve(curve)
    metrics.update(
        {
            "effective_start_date": curve[0]["date"] if curve else None,
            "effective_end_date": curve[-1]["date"] if curve else None,
            "trading_days": len(curve),
        }
    )
    return curve, metrics


class W20MomentumBacktestEngine:
    def __init__(
        self,
        quote_service: QuoteService,
        params: W20MomentumBacktestParams,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        universe_frames: Optional[Dict[str, pd.DataFrame]] = None,
        benchmark_frames: Optional[Dict[str, pd.DataFrame]] = None,
    ):
        self.quote_service = quote_service
        self.params = params
        self.progress_callback = progress_callback
        self.universe_frames = universe_frames
        self.benchmark_frames = benchmark_frames

    def _report_progress(self, progress: int, message: str):
        if self.progress_callback:
            self.progress_callback(int(max(0, min(100, progress))), message)

    def _fetch_frames(self, symbols: List[str], start_dt: date, end_dt: date, stage_start: int, stage_span: int) -> Dict[str, pd.DataFrame]:
        frames: Dict[str, pd.DataFrame] = {}
        for index, symbol in enumerate(symbols):
            self._report_progress(stage_start + int(stage_span * index / max(1, len(symbols))), f"获取 {symbol} K线")
            frames[symbol] = _build_price_frame(self.quote_service, symbol, start_dt, end_dt)
        return frames

    def run(self) -> Dict:
        params = self.params
        start_dt = _parse_date(params.start_date)
        end_dt = _parse_date(params.end_date, date.today())
        warmup_days = max(60, params.window * 3)
        fetch_start = start_dt - timedelta(days=warmup_days)

        self._report_progress(1, "开始抓取标的K线")
        universe_frames = self.universe_frames or self._fetch_frames(params.symbols, fetch_start, end_dt, 2, 20)

        available_date_sets = [set(frame.index) for frame in universe_frames.values() if not frame.empty]
        all_dates = sorted(set.union(*available_date_sets)) if available_date_sets else []
        all_dates = [d for d in all_dates if fetch_start <= d <= end_dt]
        if not all_dates:
            raise ValueError("标的池在请求区间内没有可用交易日")
        if len([d for d in all_dates if d >= start_dt]) < 1:
            raise ValueError("回测区间内可用交易日不足")

        close_matrix = pd.DataFrame({symbol: universe_frames[symbol].reindex(all_dates)["close"].astype(float) for symbol in params.symbols}, index=all_dates)
        open_matrix = pd.DataFrame({symbol: universe_frames[symbol].reindex(all_dates)["open"].astype(float) for symbol in params.symbols}, index=all_dates)

        benchmark_frames = self.benchmark_frames or self._fetch_frames(params.benchmark_symbols, fetch_start, end_dt, 22, 10)
        price_adjustments = []
        for symbol, frame in {**universe_frames, **benchmark_frames}.items():
            price_adjustments.extend(frame.attrs.get("split_like_adjustments") or [])

        dates = list(close_matrix.index)
        normalized_weights = [weight / sum(params.top_weights) for weight in params.top_weights]

        cash = params.initial_capital
        positions = {symbol: 0 for symbol in params.symbols}
        current_target_order: Tuple[str, ...] = tuple()
        current_target_weights: Dict[str, float] = {}
        pending_target_order: Optional[Tuple[str, ...]] = None
        pending_target_weights: Dict[str, float] = {}
        pending_orders: List[Dict] = []
        pending_target_reason = None
        trade_log: List[Dict] = []
        signal_history: List[Dict] = []
        equity_curve_full: List[Dict] = []
        latest_ranking: List[Dict] = []
        latest_signal_date: Optional[date] = None
        last_close_prices: Dict[str, float] = {}
        warmup_recorded = False

        total_steps = len(dates)
        for idx, current_date in enumerate(dates):
            current_close_prices: Dict[str, float] = {}
            current_open_prices: Dict[str, float] = {}
            for symbol in params.symbols:
                close_price = _price_or_zero(close_matrix.iloc[idx][symbol])
                open_price = _price_or_zero(open_matrix.iloc[idx][symbol])
                if close_price > 0:
                    last_close_prices[symbol] = close_price
                current_close_prices[symbol] = close_price if close_price > 0 else float(last_close_prices.get(symbol, 0.0))
                current_open_prices[symbol] = open_price

            # execute orders at today's open
            if pending_orders:
                sells = [order for order in pending_orders if order["action"] == "SELL"]
                buys = [order for order in pending_orders if order["action"] == "BUY"]
                for order in sells + buys:
                    symbol = order["symbol"]
                    quantity = int(order["quantity"])
                    if quantity <= 0:
                        continue
                    open_price = _price_or_zero(current_open_prices.get(symbol, 0.0))
                    if open_price <= 0:
                        continue

                    if order["action"] == "SELL":
                        fill_price = open_price * (1 - params.slippage_pct / 100.0)
                        notional = quantity * fill_price
                        commission = notional * params.commission_pct / 100.0
                        cash += notional - commission
                        positions[symbol] = max(0, positions.get(symbol, 0) - quantity)
                    else:
                        fill_price = open_price * (1 + params.slippage_pct / 100.0)
                        notional = quantity * fill_price
                        commission = notional * params.commission_pct / 100.0
                        total_cost = notional + commission
                        per_share_cost = fill_price * (1 + params.commission_pct / 100.0)
                        affordable = int(cash / per_share_cost / params.lot_size) * params.lot_size if per_share_cost > 0 else 0
                        if affordable <= 0:
                            continue
                        if affordable < quantity:
                            quantity = affordable
                            notional = quantity * fill_price
                            commission = notional * params.commission_pct / 100.0
                            total_cost = notional + commission
                        cash -= total_cost
                        positions[symbol] = positions.get(symbol, 0) + quantity

                    total_market_value_after = sum(
                        int(positions.get(position_symbol, 0)) * (
                            _price_or_zero(current_open_prices.get(position_symbol, 0.0))
                            or _price_or_zero(current_close_prices.get(position_symbol, 0.0))
                        )
                        for position_symbol in positions
                    )
                    symbol_market_value_after = int(positions.get(symbol, 0)) * open_price
                    portfolio_value_after = float(cash + total_market_value_after)
                    symbol_weight_pct_after = (
                        symbol_market_value_after / portfolio_value_after * 100
                        if portfolio_value_after > 0
                        else 0.0
                    )
                    if current_date >= start_dt:
                        trade_record = {
                            "date": current_date.isoformat(),
                            "action": order["action"],
                            "symbol": symbol,
                            "price": _round_or_none(fill_price, 4),
                            "open_price": _round_or_none(open_price, 4),
                            "quantity": int(quantity),
                            "amount": _round_or_none(notional, 4),
                            "commission": _round_or_none(commission, 4),
                            "reason": order.get("reason"),
                            "cash_after": _round_or_none(cash, 4),
                            "total_market_value_after": _round_or_none(total_market_value_after, 4),
                            "symbol_market_value_after": _round_or_none(symbol_market_value_after, 4),
                            "symbol_weight_pct_after": _round_or_none(symbol_weight_pct_after, 4),
                            "portfolio_value_after": _round_or_none(portfolio_value_after, 4),
                        }
                        for metadata_key in (
                            "reason_detail",
                            "trigger_type",
                            "signal_date",
                            "previous_target_symbols",
                            "previous_target_weights_pct",
                            "target_symbols",
                            "target_weights_pct",
                            "drift_threshold_pct",
                            "drift_symbol",
                            "drift_pct",
                            "drift_relative_pct",
                            "drift_abs_pct",
                            "actual_weight_pct",
                            "target_weight_pct",
                            "drift_details",
                        ):
                            if metadata_key in order:
                                trade_record[metadata_key] = order.get(metadata_key)
                        trade_log.append(trade_record)

                current_target_order = pending_target_order or current_target_order
                current_target_weights = dict(pending_target_weights) if pending_target_weights else current_target_weights
                pending_orders = []
                pending_target_order = None
                pending_target_weights = {}
                pending_target_reason = None

            # mark to market at close
            portfolio_value_close = _compute_portfolio_value(positions, current_close_prices, cash)
            if current_date >= start_dt:
                if not warmup_recorded:
                    warmup_recorded = True
                equity_curve_full.append(
                    {
                        "date": current_date.isoformat(),
                        "value": _round_or_none(portfolio_value_close, 4),
                    }
                )

            should_rebalance_today = _is_rebalance_day(dates, idx, params.rebalance_frequency)
            if current_date < start_dt:
                self._report_progress(25 + int(65 * (idx + 1) / max(1, total_steps)), f"预热中 {idx + 1}/{total_steps}")
                last_close_prices = current_close_prices
                continue

            # Rank and select ETFs only on rebalance dates. Orders are still executed
            # at the next open, so today's close signal never trades on the same close.
            if should_rebalance_today:
                ranking: List[Dict] = []
                for symbol in params.symbols:
                    if not _is_valid_price(close_matrix.iloc[idx][symbol]):
                        continue
                    snapshot = _compute_momentum_snapshot(close_matrix[symbol], current_date, params.window)
                    if snapshot is None:
                        continue
                    ranking.append(
                        {
                            "symbol": symbol,
                            **snapshot,
                        }
                    )
                ranking = sorted(
                    ranking,
                    key=lambda item: (
                        -1e18 if item["risk_adjusted_score"] is None else float(item["risk_adjusted_score"])
                    ),
                    reverse=True,
                )
                latest_ranking = [
                    {
                        "rank": rank + 1,
                        **item,
                    }
                    for rank, item in enumerate(ranking)
                ]
                latest_signal_date = current_date
                if ranking:
                    selected = ranking[: min(params.top_n, len(ranking))]
                    selected_symbols = [item["symbol"] for item in selected]
                    selected_weights = normalized_weights[: len(selected_symbols)]
                    weight_total = sum(selected_weights)
                    if weight_total > 0:
                        selected_weights = [weight / weight_total for weight in selected_weights]
                    pending_target_order = tuple(selected_symbols)
                    pending_target_weights = {symbol: weight for symbol, weight in zip(selected_symbols, selected_weights)}
                    signal_history.append(
                        {
                            "date": current_date.isoformat(),
                            "selected_symbols": selected_symbols,
                            "target_weights_pct": [round(weight * 100, 4) for weight in selected_weights],
                            "ranking": latest_ranking[: len(params.symbols)],
                        }
                    )

            # rebalance evaluation
            if not should_rebalance_today:
                self._report_progress(25 + int(65 * (idx + 1) / max(1, total_steps)), f"模拟中 {idx + 1}/{total_steps}")
                last_close_prices = current_close_prices
                continue
            if idx >= len(dates) - 1:
                last_close_prices = current_close_prices
                continue
            active_target_order = pending_target_order or current_target_order
            active_target_weights = pending_target_weights or current_target_weights
            if not active_target_order or not active_target_weights:
                self._report_progress(25 + int(65 * (idx + 1) / max(1, total_steps)), f"模拟中 {idx + 1}/{total_steps}")
                last_close_prices = current_close_prices
                continue

            has_pending_target = bool(pending_target_order)
            target_change_metadata: Dict = {}
            force_target_rebalance = False
            same_basket_target_changed = False
            if has_pending_target:
                target_change_metadata = _build_target_change_metadata(
                    previous_order=current_target_order,
                    previous_weights=current_target_weights,
                    target_order=active_target_order,
                    target_weights=active_target_weights,
                    signal_date=current_date,
                )
                previous_symbols = set(current_target_order or tuple())
                target_symbols_set = set(active_target_order or tuple())
                target_weights_changed_by_symbol = _target_weights_changed(
                    list(active_target_order),
                    current_target_weights,
                    active_target_weights,
                )
                force_target_rebalance = not current_target_order or previous_symbols != target_symbols_set
                same_basket_target_changed = (
                    bool(current_target_order)
                    and previous_symbols == target_symbols_set
                    and target_weights_changed_by_symbol
                    and target_change_metadata.get("reason") in {"rank_order_changed", "target_weights_changed"}
                )
            portfolio_value = portfolio_value_close

            def target_needs_rebalance() -> Tuple[bool, Dict]:
                drift_triggered, drift_metadata = _build_drift_metadata(
                    positions=positions,
                    prices=current_close_prices,
                    cash=cash,
                    target_order=active_target_order,
                    target_weights=active_target_weights,
                    threshold_pct=params.drift_threshold_pct,
                    signal_date=current_date,
                )
                if force_target_rebalance:
                    return True, target_change_metadata
                if same_basket_target_changed:
                    if drift_triggered:
                        return True, _merge_target_change_and_drift_metadata(target_change_metadata, drift_metadata)
                    return False, target_change_metadata
                if drift_triggered:
                    return True, drift_metadata
                return False, drift_metadata

            needs_rebalance, rebalance_metadata = target_needs_rebalance()
            if needs_rebalance:
                target_symbols = list(active_target_order)
                target_weights = [active_target_weights[symbol] for symbol in target_symbols]
                target_shares = _build_target_shares(
                    portfolio_value=portfolio_value,
                    current_positions=positions,
                    target_symbols=target_symbols,
                    target_weights=target_weights,
                    close_prices=current_close_prices,
                    lot_size=params.lot_size,
                    commission_pct=params.commission_pct,
                )
                orders: List[Dict] = []
                for symbol in params.symbols:
                    desired = int(target_shares.get(symbol, 0))
                    current_shares = int(positions.get(symbol, 0))
                    delta = desired - current_shares
                    if delta > 0:
                        orders.append({"action": "BUY", "symbol": symbol, "quantity": delta, **rebalance_metadata})
                    elif delta < 0:
                        orders.append({"action": "SELL", "symbol": symbol, "quantity": abs(delta), **rebalance_metadata})
                pending_orders = orders
                pending_target_reason = rebalance_metadata.get("reason")
                if not pending_orders and has_pending_target:
                    current_target_order = pending_target_order or current_target_order
                    current_target_weights = dict(pending_target_weights) if pending_target_weights else current_target_weights
                    pending_target_order = None
                    pending_target_weights = {}
                    pending_target_reason = None
            elif has_pending_target:
                current_target_order = pending_target_order or current_target_order
                current_target_weights = dict(pending_target_weights) if pending_target_weights else current_target_weights
                pending_target_order = None
                pending_target_weights = {}
                pending_target_reason = None

            self._report_progress(25 + int(65 * (idx + 1) / max(1, total_steps)), f"模拟中 {idx + 1}/{total_steps}")
            last_close_prices = current_close_prices

        # curve filtering
        filtered_curve = [item for item in equity_curve_full if _parse_date(item["date"]) >= start_dt]
        filtered_trades = [item for item in trade_log if _parse_date(item["date"]) >= start_dt]
        if not filtered_curve:
            raise ValueError("回测区间内没有可用的净值曲线")
        strategy_start_dt = _parse_date(filtered_curve[0]["date"])
        strategy_end_dt = _parse_date(filtered_curve[-1]["date"])

        strategy_values = pd.Series([float(item["value"]) for item in filtered_curve])
        strategy_dates = [pd.to_datetime(item["date"]) for item in filtered_curve]
        strategy_returns = strategy_values.pct_change().dropna()
        total_return = float((strategy_values.iloc[-1] / strategy_values.iloc[0] - 1) * 100) if strategy_values.iloc[0] > 0 else 0.0
        days = max(1, (strategy_dates[-1] - strategy_dates[0]).days)
        annualized_return = float(((strategy_values.iloc[-1] / strategy_values.iloc[0]) ** (365 / days) - 1) * 100) if strategy_values.iloc[0] > 0 else 0.0
        annualized_volatility = float(strategy_returns.std(ddof=1) * np.sqrt(252) * 100) if len(strategy_returns) > 1 else 0.0
        strategy_cummax = strategy_values.cummax()
        drawdown = (strategy_values / strategy_cummax - 1) * 100
        max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
        sharpe_ratio = float((strategy_returns.mean() / strategy_returns.std(ddof=1)) * np.sqrt(252)) if len(strategy_returns) > 1 and strategy_returns.std(ddof=1) > 0 else 0.0
        calmar_ratio = float(annualized_return / abs(max_drawdown)) if max_drawdown < 0 else 0.0

        # equal weight benchmark
        equal_weight_curve, equal_weight_trade_stats = _simulate_equal_weight_benchmark(
            close_matrix=close_matrix,
            open_matrix=open_matrix,
            dates=dates,
            start_date=strategy_start_dt,
            end_date=strategy_end_dt,
            window=params.window,
            initial_capital=params.initial_capital,
            lot_size=params.lot_size,
            commission_pct=params.commission_pct,
            slippage_pct=params.slippage_pct,
        )
        equal_weight_metrics = _compute_benchmark_metrics_from_curve(equal_weight_curve)
        equal_weight_value_map = {item["date"]: item["value"] for item in equal_weight_curve}
        equal_weight_values = pd.Series([float(item["value"]) for item in equal_weight_curve]) if equal_weight_curve else pd.Series(dtype=float)
        equal_weight_drawdown = (equal_weight_values / equal_weight_values.cummax() - 1) * 100 if not equal_weight_values.empty else pd.Series(dtype=float)

        benchmark_metrics: List[Dict] = []
        for symbol, frame in benchmark_frames.items():
            curve, metrics = _simulate_symbol_buy_hold(
                frame=frame,
                start_date=strategy_start_dt,
                end_date=strategy_end_dt,
                initial_capital=params.initial_capital,
                lot_size=params.lot_size,
                commission_pct=params.commission_pct,
                slippage_pct=params.slippage_pct,
            )
            benchmark_metrics.append(
                {
                    "symbol": symbol,
                    "curve": curve,
                    **metrics,
                }
            )

        # current holdings snapshot
        current_holdings = []
        total_portfolio_value = _compute_portfolio_value(positions, last_close_prices, cash)
        for symbol in params.symbols:
            shares = int(positions.get(symbol, 0))
            if shares <= 0:
                continue
            price = float(last_close_prices.get(symbol, 0.0))
            market_value = shares * price
            current_holdings.append(
                {
                    "symbol": symbol,
                    "shares": shares,
                    "price": _round_or_none(price, 4),
                    "market_value": _round_or_none(market_value, 4),
                    "actual_weight_pct": _round_or_none(market_value / total_portfolio_value * 100 if total_portfolio_value > 0 else 0.0, 4),
                    "target_weight_pct": _round_or_none((current_target_weights.get(symbol, 0.0) or 0.0) * 100, 4),
                }
            )

        benchmark_total_return = equal_weight_metrics.get("total_return") or 0.0
        excess_equal_weight = float(total_return - benchmark_total_return)

        symbol_trade_stats = []
        for symbol in params.symbols:
            symbol_trades = [item for item in filtered_trades if item.get("symbol") == symbol]
            buy_trades = [item for item in symbol_trades if item.get("action") == "BUY"]
            sell_trades = [item for item in symbol_trades if item.get("action") == "SELL"]
            eligible_dates = [
                current_date
                for current_date in dates
                if strategy_start_dt <= current_date <= strategy_end_dt
                and _is_valid_price(close_matrix.loc[current_date, symbol])
                and _compute_momentum_snapshot(close_matrix[symbol], current_date, params.window) is not None
            ]
            symbol_trade_stats.append(
                {
                    "symbol": symbol,
                    "effective_start_date": eligible_dates[0].isoformat() if eligible_dates else None,
                    "effective_end_date": eligible_dates[-1].isoformat() if eligible_dates else None,
                    "trading_days": len(eligible_dates),
                    "buy_count": len(buy_trades),
                    "sell_count": len(sell_trades),
                    "trade_count": len(symbol_trades),
                }
            )

        equity_curve = []
        for idx, item in enumerate(filtered_curve):
            current_date = item["date"]
            benchmark_value = equal_weight_value_map.get(current_date)
            strategy_drawdown_value = float(drawdown.iloc[idx]) if idx < len(drawdown) else None
            benchmark_drawdown_value = float(equal_weight_drawdown.iloc[idx]) if idx < len(equal_weight_drawdown) else None
            equity_curve.append(
                {
                    "date": current_date,
                    "value": item["value"],
                    "benchmark_value": benchmark_value,
                    "drawdown": _round_or_none(strategy_drawdown_value, 4),
                    "benchmark_drawdown": _round_or_none(benchmark_drawdown_value, 4),
                }
            )

        strategy_annual_returns = _compute_annual_return_map(filtered_curve)
        equal_weight_annual_returns = _compute_annual_return_map(equal_weight_curve)
        benchmark_annual_returns = {
            item["symbol"]: _compute_annual_return_map(item.get("curve") or [])
            for item in benchmark_metrics
        }
        annual_year_set = set(strategy_annual_returns.keys()) | set(equal_weight_annual_returns.keys())
        for annual_returns in benchmark_annual_returns.values():
            annual_year_set |= set(annual_returns.keys())
        annual_years = sorted(annual_year_set)
        annual_performance = []
        for year in annual_years:
            strategy_year_return = strategy_annual_returns.get(year)
            equal_weight_year_return = equal_weight_annual_returns.get(year)
            benchmark_returns = {
                symbol: annual_returns.get(year)
                for symbol, annual_returns in benchmark_annual_returns.items()
            }
            annual_performance.append(
                {
                    "year": year,
                    "strategy_return": strategy_year_return,
                    "equal_weight_return": equal_weight_year_return,
                    "excess_equal_weight_return": _round_or_none(
                        float(strategy_year_return) - float(equal_weight_year_return),
                        4,
                    ) if strategy_year_return is not None and equal_weight_year_return is not None else None,
                    "benchmark_returns": benchmark_returns,
                }
            )

        latest_target_order = pending_target_order or current_target_order
        latest_target_weights = pending_target_weights or current_target_weights

        result = {
            "params": params.dict(),
            "meta": {
                "requested_start_date": start_dt.isoformat(),
                "requested_end_date": end_dt.isoformat(),
                "effective_start_date": strategy_start_dt.isoformat(),
                "effective_end_date": strategy_end_dt.isoformat(),
                "warmup_start_date": fetch_start.isoformat(),
                "trading_days": len(filtered_curve),
                "signal_days": len(signal_history),
                "universe_size": len(params.symbols),
                "top_n": params.top_n,
                "rebalance_frequency": params.rebalance_frequency,
                "drift_threshold_pct": params.drift_threshold_pct,
                "initial_capital": params.initial_capital,
                "window": params.window,
                "top_weights_pct": params.top_weights,
                "commission_pct": params.commission_pct,
                "slippage_pct": params.slippage_pct,
                "lot_size": params.lot_size,
                "benchmark_symbols": params.benchmark_symbols,
                "price_adjustments": price_adjustments,
            },
            "cash": _round_or_none(cash, 4),
            "portfolio_value": _round_or_none(total_portfolio_value, 4),
            "metrics": {
                "total_return": _round_or_none(total_return, 4),
                "annualized_return": _round_or_none(annualized_return, 4),
                "annualized_volatility": _round_or_none(annualized_volatility, 4),
                "max_drawdown": _round_or_none(max_drawdown, 4),
                "sharpe_ratio": _round_or_none(sharpe_ratio, 4),
                "calmar_ratio": _round_or_none(calmar_ratio, 4),
                "trade_count": len(filtered_trades),
                "buy_trade_count": sum(1 for item in filtered_trades if item["action"] == "BUY"),
                "sell_trade_count": sum(1 for item in filtered_trades if item["action"] == "SELL"),
                "equal_weight_total_return": equal_weight_metrics.get("total_return"),
                "equal_weight_annualized_return": equal_weight_metrics.get("annualized_return"),
                "equal_weight_annualized_volatility": equal_weight_metrics.get("annualized_volatility"),
                "equal_weight_max_drawdown": equal_weight_metrics.get("max_drawdown"),
                "equal_weight_sharpe_ratio": equal_weight_metrics.get("sharpe_ratio"),
                "equal_weight_calmar_ratio": equal_weight_metrics.get("calmar_ratio"),
                "equal_weight_trade_count": equal_weight_trade_stats.get("trade_count"),
                "equal_weight_buy_trade_count": equal_weight_trade_stats.get("buy_trade_count"),
                "equal_weight_sell_trade_count": equal_weight_trade_stats.get("sell_trade_count"),
                "excess_equal_weight_return": _round_or_none(excess_equal_weight, 4),
            },
            "equity_curve": [
                item
                for item in equity_curve
            ],
            "benchmark_curve": equal_weight_curve,
            "annual_performance": annual_performance,
            "benchmark_metrics": [
                {
                    "symbol": item["symbol"],
                    "effective_start_date": item.get("effective_start_date"),
                    "effective_end_date": item.get("effective_end_date"),
                    "trading_days": item.get("trading_days"),
                    "total_return": item.get("total_return"),
                    "annualized_return": item.get("annualized_return"),
                    "annualized_volatility": item.get("annualized_volatility"),
                    "max_drawdown": item.get("max_drawdown"),
                    "sharpe_ratio": item.get("sharpe_ratio"),
                    "calmar_ratio": item.get("calmar_ratio"),
                }
                for item in benchmark_metrics
            ],
            "latest_signal": {
                "date": latest_signal_date.isoformat() if latest_signal_date else None,
                "selected_symbols": list(latest_target_order) if latest_target_order else [],
                "target_weights_pct": [round(float(latest_target_weights.get(symbol, 0.0)) * 100, 4) for symbol in latest_target_order] if latest_target_order else [],
                "ranking": latest_ranking,
            },
            "current_holdings": current_holdings,
            "symbol_trade_stats": symbol_trade_stats,
            "trades": filtered_trades,
            "signal_history": signal_history,
        }
        return result


def _run_backtest_job(task_id: str, params: W20MomentumBacktestParams, account_id: str):
    try:
        _update_job(task_id, status="running", progress=0, message="初始化")
        quote_service = _get_quote_service(account_id)

        def progress_callback(progress: int, message: str):
            _update_job(task_id, progress=progress, message=message)

        engine = W20MomentumBacktestEngine(quote_service, params, progress_callback=progress_callback)
        result = engine.run()
        _update_job(task_id, status="completed", progress=100, message="完成", result=result)
    except Exception as exc:
        logger.exception("W20 momentum backtest failed")
        _update_job(task_id, status="failed", error=str(exc), message="失败")


def _get_batch_total_combinations(params: W20MomentumBatchBacktestParams) -> int:
    return (
        len(params.window_values)
        * len(params.top_weights_values)
        * len(params.rebalance_frequency_values)
        * len(params.drift_threshold_pct_values)
    )


def _build_single_params(
    batch_params: W20MomentumBatchBacktestParams,
    window: int,
    top_weights: List[float],
    rebalance_frequency: str,
    drift_threshold_pct: float,
) -> W20MomentumBacktestParams:
    return W20MomentumBacktestParams(
        symbols=batch_params.symbols,
        benchmark_symbols=batch_params.benchmark_symbols,
        initial_capital=batch_params.initial_capital,
        start_date=batch_params.start_date,
        end_date=batch_params.end_date,
        window=window,
        top_n=len(top_weights),
        top_weights=top_weights,
        rebalance_frequency=rebalance_frequency,
        drift_threshold_pct=drift_threshold_pct,
        commission_pct=batch_params.commission_pct,
        slippage_pct=batch_params.slippage_pct,
        lot_size=batch_params.lot_size,
    )


def _summarize_batch_result(result_id: str, result: Dict) -> Dict:
    metrics = result.get("metrics") or {}
    meta = result.get("meta") or {}
    latest_signal = result.get("latest_signal") or {}
    return {
        "result_id": result_id,
        "window": meta.get("window"),
        "top_n": meta.get("top_n"),
        "top_weights_pct": meta.get("top_weights_pct") or [],
        "rebalance_frequency": meta.get("rebalance_frequency"),
        "drift_threshold_pct": meta.get("drift_threshold_pct"),
        "effective_start_date": meta.get("effective_start_date"),
        "effective_end_date": meta.get("effective_end_date"),
        "trading_days": meta.get("trading_days"),
        "signal_days": meta.get("signal_days"),
        "selected_symbols": latest_signal.get("selected_symbols") or [],
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "annualized_volatility": metrics.get("annualized_volatility"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "calmar_ratio": metrics.get("calmar_ratio"),
        "trade_count": metrics.get("trade_count"),
        "excess_equal_weight_return": metrics.get("excess_equal_weight_return"),
    }


_BATCH_WORKER_STATE: Dict[str, Dict[str, pd.DataFrame]] = {}


def _init_batch_worker(universe_frames: Dict[str, pd.DataFrame], benchmark_frames: Dict[str, pd.DataFrame]) -> None:
    _BATCH_WORKER_STATE["universe_frames"] = universe_frames
    _BATCH_WORKER_STATE["benchmark_frames"] = benchmark_frames


def _run_batch_combo_worker(payload: Dict) -> Tuple[Dict, Optional[Dict], Optional[Dict]]:
    combo = payload["combo"]
    result_id = payload["result_id"]
    params_dict = payload["params"]
    try:
        single_params = W20MomentumBacktestParams(**params_dict)
        engine = W20MomentumBacktestEngine(
            quote_service=None,
            params=single_params,
            universe_frames=_BATCH_WORKER_STATE["universe_frames"],
            benchmark_frames=_BATCH_WORKER_STATE["benchmark_frames"],
        )
        detail = engine.run()
        summary = _summarize_batch_result(result_id, detail)
        return summary, detail, None
    except Exception as exc:
        logger.exception("W20 momentum batch combo failed in worker: %s", combo.get("label"))
        return (
            {},
            None,
            {
                "result_id": result_id,
                "window": combo.get("window"),
                "top_n": len(combo.get("top_weights_pct") or []),
                "top_weights_pct": combo.get("top_weights_pct"),
                "rebalance_frequency": combo.get("rebalance_frequency"),
                "drift_threshold_pct": combo.get("drift_threshold_pct"),
                "error": str(exc),
            },
        )


def _get_batch_eval_workers(params: W20MomentumBatchBacktestParams, total_combinations: int) -> int:
    if total_combinations <= 1:
        return 1
    cpu_count = os.cpu_count() or 1
    if params.eval_workers:
        return max(1, min(params.eval_workers, total_combinations))
    return max(1, min(4, cpu_count, total_combinations))


def _run_batch_backtest_job(task_id: str, params: W20MomentumBatchBacktestParams, account_id: str):
    try:
        quote_service = _get_quote_service(account_id)
        start_dt = _parse_date(params.start_date)
        end_dt = _parse_date(params.end_date, date.today())
        max_window = max(params.window_values)
        fetch_start = start_dt - timedelta(days=max(60, max_window * 3))
        total_combinations = _get_batch_total_combinations(params)

        _update_job(
            task_id,
            status="running",
            progress=1,
            message="开始抓取批量回测K线",
            processed_combinations=0,
            total_combinations=total_combinations,
        )

        universe_frames: Dict[str, pd.DataFrame] = {}
        for index, symbol in enumerate(params.symbols):
            _update_job(
                task_id,
                progress=2 + int(13 * index / max(1, len(params.symbols))),
                message=f"获取策略标的K线: {symbol}",
            )
            universe_frames[symbol] = _build_price_frame(quote_service, symbol, fetch_start, end_dt)

        benchmark_frames: Dict[str, pd.DataFrame] = {}
        for index, symbol in enumerate(params.benchmark_symbols):
            _update_job(
                task_id,
                progress=15 + int(10 * index / max(1, len(params.benchmark_symbols))),
                message=f"获取基准K线: {symbol}",
            )
            benchmark_frames[symbol] = _build_price_frame(quote_service, symbol, fetch_start, end_dt)
        price_adjustments = []
        for symbol, frame in {**universe_frames, **benchmark_frames}.items():
            price_adjustments.extend(frame.attrs.get("split_like_adjustments") or [])

        combinations = list(
            product(
                params.window_values,
                params.top_weights_values,
                params.rebalance_frequency_values,
                params.drift_threshold_pct_values,
            )
        )
        summaries_and_details: List[Tuple[Dict, Dict]] = []
        errors: List[Dict] = []
        worker_payloads: List[Dict] = []
        for index, (window, top_weights, rebalance_frequency, drift_threshold_pct) in enumerate(combinations, start=1):
            combo_label = (
                f"window={window}, weights={'/'.join(str(weight) for weight in top_weights)}, "
                f"frequency={rebalance_frequency}, threshold={drift_threshold_pct}%"
            )
            single_params = _build_single_params(
                params,
                window=window,
                top_weights=top_weights,
                rebalance_frequency=rebalance_frequency,
                drift_threshold_pct=drift_threshold_pct,
            )
            worker_payloads.append(
                {
                    "result_id": f"combo-{index}",
                    "combo": {
                        "index": index,
                        "label": combo_label,
                        "window": window,
                        "top_weights_pct": top_weights,
                        "rebalance_frequency": rebalance_frequency,
                        "drift_threshold_pct": drift_threshold_pct,
                    },
                    "params": single_params.dict(),
                }
            )

        eval_workers = _get_batch_eval_workers(params, total_combinations)
        _update_job(
            task_id,
            progress=25,
            message=f"开始并发回测 {total_combinations} 组参数，进程数 {eval_workers}",
            processed_combinations=0,
            total_combinations=total_combinations,
            eval_workers=eval_workers,
        )

        def collect_worker_result(summary: Dict, detail: Optional[Dict], error: Optional[Dict]) -> None:
            if error:
                errors.append(error)
            elif summary and detail:
                summaries_and_details.append((summary, detail))

        completed = 0
        if eval_workers == 1:
            _init_batch_worker(universe_frames, benchmark_frames)
            for payload in worker_payloads:
                summary, detail, error = _run_batch_combo_worker(payload)
                collect_worker_result(summary, detail, error)
                completed += 1
                _update_job(
                    task_id,
                    progress=25 + int(70 * completed / max(1, total_combinations)),
                    message=f"已完成 {completed}/{total_combinations} 组参数",
                    processed_combinations=completed,
                    total_combinations=total_combinations,
                    eval_workers=eval_workers,
                )
        else:
            with ProcessPoolExecutor(
                max_workers=eval_workers,
                initializer=_init_batch_worker,
                initargs=(universe_frames, benchmark_frames),
            ) as executor:
                futures = [executor.submit(_run_batch_combo_worker, payload) for payload in worker_payloads]
                for future in as_completed(futures):
                    summary, detail, error = future.result()
                    collect_worker_result(summary, detail, error)
                    completed += 1
                    _update_job(
                        task_id,
                        progress=25 + int(70 * completed / max(1, total_combinations)),
                        message=f"已完成 {completed}/{total_combinations} 组参数",
                        processed_combinations=completed,
                        total_combinations=total_combinations,
                        eval_workers=eval_workers,
                    )

        summaries_and_details = sorted(
            summaries_and_details,
            key=lambda item: -1e18 if item[0].get("total_return") is None else float(item[0].get("total_return")),
            reverse=True,
        )
        top_pairs = summaries_and_details[: params.max_results]
        results = []
        details_by_id = {}
        for rank, (summary, detail) in enumerate(top_pairs, start=1):
            ranked_summary = {**summary, "rank": rank}
            results.append(ranked_summary)
            details_by_id[summary["result_id"]] = detail

        result = {
            "meta": {
                "requested_start_date": start_dt.isoformat(),
                "requested_end_date": end_dt.isoformat(),
                "warmup_start_date": fetch_start.isoformat(),
                "symbols": params.symbols,
                "benchmark_symbols": params.benchmark_symbols,
                "window_values": params.window_values,
                "top_weights_values": params.top_weights_values,
                "rebalance_frequency_values": params.rebalance_frequency_values,
                "drift_threshold_pct_values": params.drift_threshold_pct_values,
                "total_combinations": total_combinations,
                "evaluated_combinations": len(summaries_and_details),
                "failed_combinations": len(errors),
                "returned_results": len(results),
                "max_results": params.max_results,
                "eval_workers": eval_workers,
                "initial_capital": params.initial_capital,
                "commission_pct": params.commission_pct,
                "slippage_pct": params.slippage_pct,
                "lot_size": params.lot_size,
                "price_adjustments": price_adjustments,
            },
            "results": results,
            "errors": errors[:50],
            "best_result": top_pairs[0][1] if top_pairs else None,
        }

        _update_job(
            task_id,
            status="completed",
            progress=100,
            message="完成",
            processed_combinations=total_combinations,
            total_combinations=total_combinations,
            result=result,
            details_by_id=details_by_id,
        )
    except Exception as exc:
        logger.exception("W20 momentum batch backtest failed")
        _update_job(task_id, status="failed", error=str(exc), message="失败")


@router.post("/start", response_model=W20MomentumBacktestJobCreated)
async def start_w20_momentum_backtest(
    params: W20MomentumBacktestParams,
    background_tasks: BackgroundTasks,
    account_id: str = Depends(valid_account),
):
    task_id = uuid.uuid4().hex
    _update_job(
        task_id,
        status="pending",
        progress=0,
        message="等待启动",
        result=None,
        error=None,
        created_at=datetime.now().isoformat(),
    )
    background_tasks.add_task(_run_backtest_job, task_id, params, account_id)
    return {"task_id": task_id, "status": "pending"}


@router.post("/batch/start", response_model=W20MomentumBatchJobCreated)
async def start_w20_momentum_batch_backtest(
    params: W20MomentumBatchBacktestParams,
    background_tasks: BackgroundTasks,
    account_id: str = Depends(valid_account),
):
    task_id = uuid.uuid4().hex
    total_combinations = _get_batch_total_combinations(params)
    _update_job(
        task_id,
        status="pending",
        progress=0,
        message="等待启动",
        result=None,
        error=None,
        details_by_id={},
        processed_combinations=0,
        total_combinations=total_combinations,
        eval_workers=_get_batch_eval_workers(params, total_combinations),
        created_at=datetime.now().isoformat(),
    )
    background_tasks.add_task(_run_batch_backtest_job, task_id, params, account_id)
    return {"task_id": task_id, "status": "pending", "total_combinations": total_combinations}


@router.get("/jobs/{task_id}/results/{result_id}")
async def get_w20_momentum_backtest_detail(task_id: str, result_id: str):
    job = _get_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")
    details_by_id = job.get("details_by_id") or {}
    detail = details_by_id.get(result_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Result detail not found")
    return detail


@router.get("/jobs/{task_id}", response_model=W20MomentumBacktestJobStatus)
async def get_w20_momentum_backtest_job(task_id: str):
    job = _get_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task_id,
        "status": job.get("status", "pending"),
        "progress": int(job.get("progress", 0) or 0),
        "message": job.get("message"),
        "result": job.get("result"),
        "error": job.get("error"),
        "processed_combinations": int(job.get("processed_combinations", 0) or 0),
        "total_combinations": int(job.get("total_combinations", 0) or 0),
        "eval_workers": int(job.get("eval_workers", 1) or 1),
    }
