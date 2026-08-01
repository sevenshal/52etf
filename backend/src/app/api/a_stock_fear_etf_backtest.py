import math
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from itertools import product
from typing import Any, Dict, List, Optional

import duckdb
import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, validator

from lab.a_stock_fear_etf_range_backtest import (
    DEFAULT_EXCLUDED,
    build_signal_rows,
    load_etf_bars,
    load_fear,
    max_drawdown,
    run_backtest,
    summarize,
    target_mapping,
)
from ...core.database import DB_PATH
from ...core.duckdb_utils import ANALYTICS_DB_PATH
from ...core.event_stream import publish_event
from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_NAMES, A_STOCK_INDEX_FEAR_GREED_TARGETS
from .account import valid_account


router = APIRouter(prefix="/api/a-stock-fear-etf-backtest", tags=["A-Stock Fear ETF Backtest"])
SEARCH_JOBS: Dict[str, Dict[str, Any]] = {}
SEARCH_LOCK = threading.Lock()
SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="a-fear-etf-search")
MAX_SEARCH_COMBINATIONS = 5000


class StrategyParams(BaseModel):
    fear_entry: float = 25.0
    volume_std_multiplier: float = 1.0
    no_new_high_days: int = 10
    fear_exit: float = 70.0

    @validator("fear_entry", "fear_exit")
    def validate_fear(cls, value):
        if value < 0 or value > 100:
            raise ValueError("恐贪阈值必须在 0 到 100 之间")
        return value

    @validator("volume_std_multiplier")
    def validate_std_multiplier(cls, value):
        if value < 0 or value > 10:
            raise ValueError("成交量标准差倍数必须在 0 到 10 之间")
        return value

    @validator("no_new_high_days")
    def validate_no_new_high_days(cls, value):
        if value < 1 or value > 500:
            raise ValueError("未创新高天数必须在 1 到 500 之间")
        return value


class RunRequest(BaseModel):
    start_date: str = "2020-01-02"
    end_date: Optional[str] = None
    initial_capital: float = 1_000_000.0
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    stamp_duty_pct: float = 0.05
    lot_size: int = 100
    excluded_indexes: List[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED))
    params: StrategyParams = Field(default_factory=StrategyParams)

    @validator("initial_capital")
    def validate_capital(cls, value):
        if value <= 0:
            raise ValueError("初始资金必须大于 0")
        return value

    @validator("commission_pct", "slippage_pct", "stamp_duty_pct")
    def validate_cost(cls, value):
        if value < 0 or value > 10:
            raise ValueError("交易成本必须在 0 到 10% 之间")
        return value

    @validator("lot_size")
    def validate_lot_size(cls, value):
        if value < 1:
            raise ValueError("交易手数必须大于 0")
        return value


class SearchRequest(RunRequest):
    top_n: int = 20
    objective: str = "sharpe_zero_rf"
    fear_entry_values: List[float] = Field(default_factory=lambda: [20.0, 25.0, 30.0])
    volume_std_multiplier_values: List[float] = Field(default_factory=lambda: [0.5, 1.0, 1.5])
    no_new_high_days_values: List[int] = Field(default_factory=lambda: [5, 10, 20, 60])
    fear_exit_values: List[float] = Field(default_factory=lambda: [65.0, 70.0, 75.0])

    @validator("top_n")
    def validate_top_n(cls, value):
        if value < 1 or value > 100:
            raise ValueError("返回结果数必须在 1 到 100 之间")
        return value

    @validator("objective")
    def validate_objective(cls, value):
        if value not in {"total_return_pct", "annualized_return_pct", "sharpe_zero_rf", "calmar_ratio"}:
            raise ValueError("不支持的搜索目标")
        return value

    @validator("fear_entry_values", "volume_std_multiplier_values", "no_new_high_days_values", "fear_exit_values")
    def validate_candidates(cls, value):
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


def _normalize_excluded(values: List[str]) -> set[str]:
    return {str(value).strip().upper() for value in values if str(value).strip()}


def _json_records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    clean = frame.replace({np.nan: None})
    return clean.to_dict(orient="records")


def _prepare_data(request: RunRequest):
    start = _parse_date(request.start_date)
    end = _parse_date(request.end_date, date.today())
    if start >= end:
        raise ValueError("开始日期必须早于结束日期")
    excluded = _normalize_excluded(request.excluded_indexes)
    mapping = target_mapping(excluded)
    fear = load_fear(DB_PATH, list(mapping), start.isoformat(), end.isoformat())
    mapping = {key: value for key, value in mapping.items() if key in set(fear["index_symbol"])}
    with duckdb.connect(ANALYTICS_DB_PATH, read_only=True) as connection:
        bars = load_etf_bars(connection, sorted(set(mapping.values())), start.isoformat(), end.isoformat())
        available = set(bars["etf_symbol"])
        mapping = {key: value for key, value in mapping.items() if value in available}
        fear = fear[fear["index_symbol"].isin(mapping)].copy()
        bars = bars[bars["etf_symbol"].isin(mapping.values())].copy()
        benchmark = connection.execute(
            "SELECT trade_date, close FROM a_stock_index_daily "
            "WHERE ts_code='000300.SH' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [start.isoformat(), end.isoformat()],
        ).fetch_df()
    if bars.empty or fear.empty:
        raise ValueError("所选区间没有可用的ETF行情或恐贪历史")
    return start, end, excluded, mapping, fear, bars, benchmark


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


def _run_prepared(request: RunRequest, mapping, fear, bars, benchmark, params: StrategyParams, detailed=True):
    signals = build_signal_rows(
        bars, fear, mapping, params.fear_entry, params.volume_std_multiplier
    )
    curve, trades = run_backtest(
        bars,
        fear,
        signals,
        initial_capital=request.initial_capital,
        fear_greed_exit=params.fear_exit,
        no_new_high_days=params.no_new_high_days,
        commission_pct=request.commission_pct,
        slippage_pct=request.slippage_pct,
        stamp_duty_pct=request.stamp_duty_pct,
        lot_size=request.lot_size,
    )
    curve = _attach_benchmark(curve, benchmark, request.initial_capital)
    summary = summarize(curve, trades, request.initial_capital)
    summary["calmar_ratio"] = (
        summary["annualized_return_pct"] / abs(summary["max_drawdown_pct"])
        if summary.get("max_drawdown_pct") else None
    )
    payload = {"summary": summary, "params": params.dict(), "signal_days": len(signals)}
    if detailed:
        annual = curve.assign(year=pd.to_datetime(curve["date"]).dt.year).groupby("year").agg(
            start_value=("value", "first"), end_value=("value", "last")
        ).reset_index()
        annual["return_pct"] = (annual["end_value"] / annual["start_value"] - 1) * 100
        payload.update({
            "equity_curve": _json_records(curve),
            "trades": _json_records(trades),
            "yearly_returns": _json_records(annual),
        })
    return payload


def _run_request(request: RunRequest):
    start, end, excluded, mapping, fear, bars, benchmark = _prepare_data(request)
    result = _run_prepared(request, mapping, fear, bars, benchmark, request.params, detailed=True)
    result["meta"] = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "excluded_indexes": sorted(excluded),
        "index_etf_mapping": mapping,
        "trading_days": int(bars["trade_date"].nunique()),
        "fear_points": int(len(fear)),
    }
    return result


def _combination_count(request: SearchRequest) -> int:
    return math.prod([
        len(request.fear_entry_values),
        len(request.volume_std_multiplier_values),
        len(request.no_new_high_days_values),
        len(request.fear_exit_values),
    ])


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
        start, end, excluded, mapping, fear, bars, benchmark = _prepare_data(request)
        combinations = list(product(
            request.fear_entry_values,
            request.volume_std_multiplier_values,
            request.no_new_high_days_values,
            request.fear_exit_values,
        ))
        results = []
        skipped = 0
        for index, values in enumerate(combinations, start=1):
            try:
                params = StrategyParams(
                    fear_entry=values[0], volume_std_multiplier=values[1],
                    no_new_high_days=values[2], fear_exit=values[3],
                )
                item = _run_prepared(request, mapping, fear, bars, benchmark, params, detailed=False)
                results.append(item)
            except Exception:
                skipped += 1
            if index == len(combinations) or index % max(1, len(combinations) // 100) == 0:
                _update_job(
                    task_id,
                    progress=int(index * 100 / len(combinations)),
                    processed_combinations=index,
                    skipped_combinations=skipped,
                    message=f"已完成 {index}/{len(combinations)} 组",
                )
        results.sort(key=lambda item: _score(item, request.objective), reverse=True)
        top_results = results[: request.top_n]
        best = top_results[0] if top_results else None
        best_detail = None
        if best:
            best_detail = _run_prepared(
                request, mapping, fear, bars, benchmark, StrategyParams(**best["params"]), detailed=True
            )
            best_detail["meta"] = {
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "excluded_indexes": sorted(excluded), "index_etf_mapping": mapping,
            }
        _update_job(
            task_id,
            status="completed", progress=100, processed_combinations=len(combinations),
            skipped_combinations=skipped, message="参数搜索完成",
            result={
                "meta": {"total_combinations": len(combinations), "objective": request.objective},
                "results": top_results, "best_result": best_detail,
            },
        )
    except Exception as exc:
        _update_job(task_id, status="failed", error=str(exc), message="参数搜索失败")


@router.get("/options")
def options(account_id: str = Depends(valid_account)):
    targets = []
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        if not item.get("proxy_etf"):
            continue
        etf = str(item["proxy_etf"]).upper()
        targets.append({
            "index_symbol": str(item["symbol"]).upper(),
            "index_label": item.get("ticker") or item.get("label") or item["symbol"],
            "etf_symbol": etf,
            "etf_label": A_STOCK_ETF_DAILY_NAMES.get(etf, etf),
        })
    return {
        "targets": targets,
        "default_excluded_indexes": list(DEFAULT_EXCLUDED),
        "max_search_combinations": MAX_SEARCH_COMBINATIONS,
        "default_request": RunRequest().dict(),
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
        raise HTTPException(status_code=400, detail=f"参数组合数必须在 1 到 {MAX_SEARCH_COMBINATIONS} 之间")
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
