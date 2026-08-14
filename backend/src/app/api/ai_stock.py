"""Administrator-only APIs for AI-selected A-share recommendations.

The endpoints expose stored, auditable data.  Network calls and model inference
live in the service layer and are never performed while a SQLite session is open.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc

from ...core.database import AIStockEvaluation, get_db_ctx
from ...core.services.ai_stock import (
    AIStockBenchmarkCollector,
    AIStockConfigurationError,
    AIStockError,
    AIStockModelError,
    AIStockPaperTradingService,
    AIStockRecommendationService,
    ai_stock_runtime_logs,
    evaluate_ai_stock_benchmark,
    get_ai_stock_service_settings,
    trigger_recommendation_async,
    update_ai_stock_service_settings,
)
from .account import valid_admin_account


router = APIRouter(prefix="/api/ai-stock", tags=["AI Stock"])


class AIStockServiceSettingsUpdate(BaseModel):
    # Empty clears the saved key; omitted retains the current stored value.
    deepseek_api_key: Optional[str] = Field(default=None, max_length=512)
    deepseek_model: Optional[str] = Field(default=None, max_length=100)
    max_candidates: Optional[int] = Field(default=None, ge=1, le=10000)
    max_events: Optional[int] = Field(default=None, ge=1, le=100)
    max_boards: Optional[int] = Field(default=None, ge=1, le=50)
    max_candidates_per_board: Optional[int] = Field(default=None, ge=1, le=10000)
    min_market_cap: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    min_avg_turnover: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    max_recommendations: Optional[int] = Field(default=None, ge=1, le=50)
    min_listing_days: Optional[int] = Field(default=None, ge=1, le=10000)
    target_return_pct_min: Optional[float] = Field(default=None, ge=0, le=100)
    target_return_pct_max: Optional[float] = Field(default=None, ge=0, le=100)
    news_signal_weight: Optional[float] = Field(default=None, ge=0, le=1)


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (AIStockConfigurationError, AIStockModelError)):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, (ValueError, AIStockError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="AI 荐股服务暂时不可用")


@router.get("/settings")
def get_ai_stock_settings(_: str = Depends(valid_admin_account)):
    return get_ai_stock_service_settings()


@router.put("/settings")
def save_ai_stock_settings(
    payload: AIStockServiceSettingsUpdate,
    account_id: str = Depends(valid_admin_account),
):
    try:
        return update_ai_stock_service_settings(
            deepseek_api_key=payload.deepseek_api_key,
            deepseek_model=payload.deepseek_model,
            updated_by=account_id,
            max_candidates=payload.max_candidates,
            max_events=payload.max_events,
            max_boards=payload.max_boards,
            max_candidates_per_board=payload.max_candidates_per_board,
            min_market_cap=payload.min_market_cap,
            min_avg_turnover=payload.min_avg_turnover,
            max_recommendations=payload.max_recommendations,
            min_listing_days=payload.min_listing_days,
            target_return_pct_min=payload.target_return_pct_min,
            target_return_pct_max=payload.target_return_pct_max,
            news_signal_weight=payload.news_signal_weight,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/recommendations/current")
def get_current_recommendations(
    limit: Optional[int] = Query(None, ge=1, le=50),
    _: str = Depends(valid_admin_account),
):
    return AIStockRecommendationService().current(limit=limit)


@router.post("/recommendations/run")
def create_recommendation_run(
    run_type: Optional[str] = Query(None, pattern="^(PREOPEN|OPENING|INTRADAY)$"),
    top_n: Optional[int] = Query(None, ge=1, le=50),
    _: str = Depends(valid_admin_account),
):
    try:
        # This administrator-only endpoint is an intentional manual override.
        # The batch runs in a background thread (it can take minutes); the
        # request returns immediately and completion is pushed to the frontend
        # over the shared backend event stream.
        return trigger_recommendation_async(run_type=run_type, top_n=top_n)
    except Exception as exc:
        raise _translate_error(exc)


@router.get("/recommendations/history")
def get_recommendation_history(
    trade_date: Optional[date] = None,
    run_type: Optional[str] = Query(None, pattern="^(PREOPEN|OPENING|INTRADAY)$"),
    limit: int = Query(60, ge=1, le=200),
    _: str = Depends(valid_admin_account),
):
    return AIStockRecommendationService().history(trade_date=trade_date, run_type=run_type, limit=limit)


@router.get("/recommendations/today")
def get_today_recommendations(_: str = Depends(valid_admin_account)):
    return AIStockRecommendationService().today()


@router.get("/recommendations/runs/{run_id}")
def get_recommendation_run(run_id: int, _: str = Depends(valid_admin_account)):
    try:
        return AIStockRecommendationService().get_run(run_id)
    except AIStockError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/recommendations/runs/{run_id}/evidence")
def get_recommendation_run_evidence(run_id: int, _: str = Depends(valid_admin_account)):
    try:
        return AIStockRecommendationService().get_run_evidence(run_id)
    except AIStockError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/recommendations/runs/{run_id}/transcript")
def get_recommendation_run_transcript(run_id: int, _: str = Depends(valid_admin_account)):
    try:
        return AIStockRecommendationService().get_run_transcript(run_id)
    except AIStockError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/recommendations/runs/{run_id}/performance")
def get_recommendation_run_performance(run_id: int, _: str = Depends(valid_admin_account)):
    try:
        return AIStockRecommendationService().run_performance(run_id)
    except AIStockError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise _translate_error(exc)


@router.get("/paper/overview")
def get_paper_overview(_: str = Depends(valid_admin_account)):
    return AIStockPaperTradingService().overview()


@router.get("/paper/positions")
def get_paper_positions(_: str = Depends(valid_admin_account)):
    try:
        return AIStockPaperTradingService().positions()
    except Exception as exc:
        raise _translate_error(exc)


@router.get("/paper/trades")
def get_paper_trades(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: str = Depends(valid_admin_account),
):
    return AIStockPaperTradingService().trades(page=page, page_size=page_size)


@router.get("/paper/equity-curve")
def get_paper_equity_curve(_: str = Depends(valid_admin_account)):
    return AIStockPaperTradingService().equity_curve()


@router.get("/paper/hold-evaluations")
def get_paper_hold_evaluations(limit: int = Query(100, ge=1, le=500), _: str = Depends(valid_admin_account)):
    return AIStockPaperTradingService().hold_evaluations(limit=limit)


@router.get("/paper/statistics")
def get_paper_statistics(_: str = Depends(valid_admin_account)):
    return AIStockPaperTradingService().paper_statistics()


@router.get("/paper/config")
def get_paper_strategy_config(_: str = Depends(valid_admin_account)):
    return AIStockPaperTradingService().strategy_config()


class PaperStrategyConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    max_positions: Optional[float] = Field(default=None, ge=1, le=1000)
    slot_count: Optional[float] = Field(default=None, ge=1, le=1000)
    single_stock_cap: Optional[float] = Field(default=None, ge=0, le=1.0)
    max_execution_target: Optional[float] = Field(default=None, ge=0, le=1.0)
    entry_price_cap_pct: Optional[float] = Field(default=None, ge=0, le=20)
    stop_loss_half_pct: Optional[float] = Field(default=None, ge=-100, le=0)
    stop_loss_full_pct: Optional[float] = Field(default=None, ge=-100, le=0)
    trading_start_minute: Optional[float] = Field(default=None, ge=570, le=690)
    hold_evaluation_enabled: Optional[bool] = None
    hold_sell_threshold: Optional[float] = Field(default=None, ge=0, le=100)
    max_buys_per_day: Optional[float] = Field(default=None, ge=1, le=1000)
    trailing_take_profit_pct: Optional[float] = Field(default=None, ge=0, le=100)
    rotation_confidence_gap: Optional[float] = Field(default=None, ge=0, le=100)


@router.put("/paper/config")
def update_paper_strategy_config(
    payload: PaperStrategyConfigUpdate,
    account_id: str = Depends(valid_admin_account),
):
    try:
        params = {name: value for name, value in payload.model_dump(exclude_none=True).items() if name != "enabled"}
        return AIStockPaperTradingService().update_strategy_config(
            updated_by=account_id,
            enabled=payload.enabled,
            **params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/benchmark/status")
def get_benchmark_status(_: str = Depends(valid_admin_account)):
    return AIStockBenchmarkCollector().status()


@router.post("/benchmark/collect")
def collect_benchmark(_: str = Depends(valid_admin_account)):
    return AIStockBenchmarkCollector().collect()


@router.post("/evaluation/run")
def run_benchmark_evaluation(
    window_days: int = Query(20, ge=5, le=120),
    _: str = Depends(valid_admin_account),
):
    return evaluate_ai_stock_benchmark(window_days=window_days)


@router.get("/evaluation/latest")
def get_latest_benchmark_evaluation(_: str = Depends(valid_admin_account)):
    with get_db_ctx() as db:
        row = db.query(AIStockEvaluation).order_by(desc(AIStockEvaluation.evaluated_at)).first()
        if not row:
            return None
        return {
            "id": row.id,
            "evaluated_at": row.evaluated_at,
            "window_start": row.window_start,
            "window_end": row.window_end,
            "theme_overlap_pct": row.theme_overlap_pct,
            "stock_overlap_pct": row.stock_overlap_pct,
            "system_return_pct": row.system_return_pct,
            "benchmark_return_pct": row.benchmark_return_pct,
            "system_max_drawdown_pct": row.system_max_drawdown_pct,
            "benchmark_max_drawdown_pct": row.benchmark_max_drawdown_pct,
            "passed": row.passed,
            "details": row.details or {},
        }


@router.get("/logs")
def get_ai_stock_runtime_logs(
    limit: int = Query(100, ge=1, le=300),
    _: str = Depends(valid_admin_account),
):
    return ai_stock_runtime_logs(limit=limit)
