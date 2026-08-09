from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .account import valid_admin_account
from ...robot.scheduled_tasks import scheduled_task_manager

router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled-tasks"])


class ScheduledTaskUpdateRequest(BaseModel):
    enabled: bool
    cron_rule: Optional[str] = None
    schedule_time: Optional[str] = None
    timezone: Optional[str] = None
    allow_queue: Optional[bool] = None
    parameters: Optional[Dict[str, Any]] = None


class ScheduledTaskRunRequest(BaseModel):
    start_date: Optional[str] = None
    symbols: Optional[List[str]] = None


class ScheduledTaskResponse(BaseModel):
    task_key: str
    name: str
    description: Optional[str] = None
    enabled: bool
    schedule_time: str
    cron_rule: Optional[str] = None
    timezone: str = "Asia/Shanghai"
    allow_queue: bool = True
    parameters: Dict[str, Any] = Field(default_factory=dict)
    parameter_schema: List[Dict[str, Any]] = Field(default_factory=list)
    first_daily_trigger_minutes: Optional[int] = None
    sort_order: int
    supports_start_date: bool = False
    is_running: bool
    next_run_at: Optional[str] = None
    last_trigger_source: Optional[str] = None
    last_run_started_at: Optional[str] = None
    last_run_finished_at: Optional[str] = None
    last_run_status: Optional[str] = None
    last_run_message: Optional[str] = None
    last_duration_seconds: Optional[float] = None
    is_queued: bool = False
    updated_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@router.get("", response_model=List[ScheduledTaskResponse])
def list_scheduled_tasks(_account_id: str = Depends(valid_admin_account)):
    return scheduled_task_manager.list_tasks()


@router.put("/{task_key}", response_model=ScheduledTaskResponse)
def update_scheduled_task(
    task_key: str,
    payload: ScheduledTaskUpdateRequest,
    account_id: str = Depends(valid_admin_account),
):
    try:
        return scheduled_task_manager.update_task(
            task_key=task_key,
            enabled=payload.enabled,
            cron_rule=payload.cron_rule,
            schedule_time=payload.schedule_time,
            timezone=payload.timezone,
            allow_queue=payload.allow_queue,
            parameters=payload.parameters,
            updated_by=account_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{task_key}/run", response_model=ScheduledTaskResponse)
def run_scheduled_task_now(
    task_key: str,
    payload: Optional[ScheduledTaskRunRequest] = None,
    account_id: str = Depends(valid_admin_account),
):
    try:
        runner_kwargs = {}
        if task_key in {
            "evc_static_info_sync",
            "cnn_fear_greed_fetch",
            "a_stock_base_data_sync",
            "etf_holdings_backfill",
            "soxx_fear_greed_backfill",
            "a_stock_etf_fear_greed_backfill",
            "hk_stock_base_data_sync",
            "hk_index_fear_greed_backfill",
        } and payload and payload.start_date:
            runner_kwargs["start_date"] = payload.start_date
        if (
            task_key in {
                "a_stock_etf_fear_greed_backfill",
                "a_stock_index_valuation_refresh",
            }
            and payload
            and payload.symbols
        ):
            runner_kwargs["symbols"] = payload.symbols
        scheduled_task_manager.trigger_task(
            task_key=task_key,
            trigger_source="manual",
            triggered_by=account_id,
            background=True,
            **runner_kwargs,
        )
        return scheduled_task_manager.get_task(task_key)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
