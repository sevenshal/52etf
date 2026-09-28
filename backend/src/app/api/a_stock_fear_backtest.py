"""A 股情绪量能回测的独立 API 边界。

计算迁移期间保留既有结果口径；A 股路由、异步任务和后续 A 股专属执行器均从
本模块进入，不能再由 SOXL router 反向注册。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from .account import valid_admin_account
from . import soxl_fear_backtest as legacy
from .a_stock_fear_volume_config import (
    A_STOCK_FEAR_SOURCE_OPTIONS,
    A_STOCK_PRESET_PAIRS,
    A_STOCK_TARGET_OPTIONS,
    FEAR_SOURCE_OPTIONS,
)


router = APIRouter(prefix="/api/a-stock-fear-backtest", tags=["A-stock Fear Volume Backtest"])


def _validate_a_stock_symbol(symbol: str) -> None:
    valid_symbols = {item["value"] for item in A_STOCK_TARGET_OPTIONS}
    if str(symbol or "").strip().upper() not in valid_symbols:
        raise ValueError("A 股情绪量能回测仅支持 A 股 ETF 标的")


@router.get("/options")
def get_a_stock_fear_backtest_options(account_id: str = Depends(valid_admin_account)):
    return {
        "symbol_options": A_STOCK_TARGET_OPTIONS,
        "volume_signal_symbol_options": A_STOCK_TARGET_OPTIONS,
        "fear_source_options": [
            {"label": config["label"], "value": key, "symbol": config.get("symbol"), "market": config.get("market")}
            for key, config in FEAR_SOURCE_OPTIONS.items()
        ],
        "a_stock_preset_pairs": A_STOCK_PRESET_PAIRS,
        "default_request": {
            "symbol": A_STOCK_PRESET_PAIRS[0]["target_symbol"] if A_STOCK_PRESET_PAIRS else None,
            "volume_signal_symbol": None,
            "fear_source_values": [next(iter(A_STOCK_FEAR_SOURCE_OPTIONS), "")],
        },
    }


@router.post("/search")
def search_a_stock_fear_params(
    payload: legacy.SOXLFearSearchParams,
    account_id: str = Depends(valid_admin_account),
):
    _validate_a_stock_symbol(payload.symbol)
    try:
        return legacy._build_search_response(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/search/jobs", response_model=legacy.SOXLFearSearchJobCreated)
def create_a_stock_fear_search_job(
    payload: legacy.SOXLFearSearchParams,
    account_id: str = Depends(valid_admin_account),
):
    _validate_a_stock_symbol(payload.symbol)
    try:
        start_date = legacy._parse_date(payload.start_date)
        end_date = legacy._parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")
        total_combinations = legacy._count_search_params(payload)
        if total_combinations <= 0:
            raise ValueError("至少需要提供一组有效的超参数候选值")

        legacy._cleanup_finished_jobs()
        task_id = legacy.uuid.uuid4().hex
        with legacy.SEARCH_JOBS_LOCK:
            legacy.SEARCH_JOBS[task_id] = {
                "task_id": task_id, "account_id": account_id, "status": "pending",
                "progress": 0, "processed_combinations": 0,
                "total_combinations": total_combinations, "skipped_combinations": 0,
                "message": "任务已创建，等待执行", "result": None, "error": None,
                "updated_at": legacy.datetime.now().timestamp(),
            }
        legacy._publish_search_job(task_id)
        legacy.SEARCH_JOB_EXECUTOR.submit(legacy._run_search_job, task_id, payload)
        return legacy.SOXLFearSearchJobCreated(
            task_id=task_id, status="pending", total_combinations=total_combinations,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/search/jobs/{task_id}", response_model=legacy.SOXLFearSearchJobStatus)
def get_a_stock_fear_search_job_status(
    task_id: str,
    account_id: str = Depends(valid_admin_account),
):
    return legacy.get_soxl_fear_search_job_status(task_id=task_id, account_id=account_id)


@router.post("/run")
def run_a_stock_fear_backtest(
    payload: legacy.SOXLFearRunParams,
    account_id: str = Depends(valid_admin_account),
):
    _validate_a_stock_symbol(payload.symbol)
    try:
        start_date = legacy._parse_date(payload.start_date)
        end_date = legacy._parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")
        compare_sources = list(dict.fromkeys(payload.compare_fear_sources or [payload.fear_source]))
        if payload.fear_source not in compare_sources:
            compare_sources.insert(0, payload.fear_source)
        (
            base_dfs, source_metas, _, sub_base_df, sub_meta,
            sub2_base_df, sub2_meta, sub3_base_df, sub3_meta,
        ) = legacy._prepare_search_dataframes(
            payload.symbol, start_date, end_date, compare_sources, payload.volume_signal_symbol,
            sub_symbol=payload.params.sub_symbol, sub_fear_source=payload.params.sub_fear_source,
            sub_volume_signal_symbol=payload.params.sub_volume_signal_symbol,
            sub2_symbol=payload.params.sub2_symbol, sub2_fear_source=payload.params.sub2_fear_source,
            sub2_volume_signal_symbol=payload.params.sub2_volume_signal_symbol,
            sub3_symbol=payload.params.sub3_symbol, sub3_fear_source=payload.params.sub3_fear_source,
            sub3_volume_signal_symbol=payload.params.sub3_volume_signal_symbol,
            sell_ma5_signal_symbol=payload.params.sell_ma5_signal_symbol,
        )
        base_df = base_dfs[payload.fear_source]
        result = (
            legacy._run_seesaw_backtest(
                base_df, sub_base_df, payload.params, payload.initial_capital, detailed=True,
                sub2_base_df=sub2_base_df, sub3_base_df=sub3_base_df,
            )
            if sub_base_df is not None and payload.params.sub_symbol
            else legacy._run_backtest(base_df, payload.params, payload.initial_capital, detailed=True)
        )
        result["meta"] = {
            **source_metas[payload.fear_source], "initial_capital": payload.initial_capital,
            "execution_price_type": result.get("execution_price_type"),
            "execution_price_label": result.get("execution_price_label"),
            "sub_meta": sub_meta, "sub2_meta": sub2_meta, "sub3_meta": sub3_meta,
        }
        result["fear_series"] = legacy._build_fear_series_payload(base_dfs)
        return legacy._json_safe(result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
