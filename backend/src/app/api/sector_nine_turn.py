"""管理员专用：板块九转策略接口（板块状态与个股信号、模拟盘、回测、参数）。"""

from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.services.sector_nine_turn.backtest import VARIANTS as BACKTEST_VARIANTS
from ...core.services.sector_nine_turn.backtest_runner import (
    cancel_backtest,
    delete_backtest,
    list_runs,
    load_run_detail,
    start_backtest,
)
from ...core.services.sector_nine_turn.config import (
    DEFAULT_BACKTEST_START,
    PICK_ORDERS,
    SELL_MODES,
    default_sector_nine_turn_config,
    index_catalog,
    load_sector_nine_turn_config,
    save_sector_nine_turn_config,
)
from ...core.services.sector_nine_turn.paper import load_paper_overview, reset_paper
from ...core.services.sector_nine_turn.views import load_daily_view, load_signal_history
from ...robot.scheduled_tasks import SECTOR_NINE_TURN_TASK_KEY, scheduled_task_manager
from .account import valid_admin_account

router = APIRouter(prefix="/api/sector-nine-turn", tags=["Sector Nine Turn"])


class DailyRunRequest(BaseModel):
    as_of: Optional[date] = None


class PaperResetRequest(BaseModel):
    initial_capital: Optional[float] = Field(default=None, gt=0)


class BacktestRequest(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    random_trials: int = Field(default=25, ge=0, le=200)


def _config_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "config": config,
        "defaults": default_sector_nine_turn_config(),
        "indexes": index_catalog(),
        "sell_modes": SELL_MODES,
        "pick_orders": PICK_ORDERS,
    }


def _read(loader, *args, **kwargs):
    try:
        return loader(*args, **kwargs)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/config")
def get_config(_: str = Depends(valid_admin_account)):
    """当前超参数、默认值、可选板块清单和下拉项。"""
    return _config_payload(load_sector_nine_turn_config())


@router.put("/config")
def update_config(payload: Dict[str, Any] = Body(...), account_id: str = Depends(valid_admin_account)):
    """整份保存；越界值会被夹到合法区间，未知字段丢弃。下一次计算时生效。"""
    return _config_payload(save_sector_nine_turn_config(payload, updated_by=account_id))


@router.post("/config/reset")
def reset_config(account_id: str = Depends(valid_admin_account)):
    return _config_payload(save_sector_nine_turn_config(default_sector_nine_turn_config(), updated_by=account_id))


@router.get("/daily")
def get_daily(
    trade_date: Optional[date] = Query(default=None, description="交易日，留空取最新一次快照"),
    _: str = Depends(valid_admin_account),
):
    """某个交易日的板块状态（九转计数、贪恐、是否布防）和个股买卖信号。"""
    return _read(load_daily_view, trade_date)


@router.get("/signal-history")
def get_signal_history(limit: int = Query(default=200, ge=1, le=1000), _: str = Depends(valid_admin_account)):
    """最近的买卖信号流水（跨交易日）。"""
    return {"rows": _read(load_signal_history, limit)}


@router.get("/paper")
def get_paper(_: str = Depends(valid_admin_account)):
    """模拟盘：账户、持仓、净值曲线、最近订单。"""
    return load_paper_overview()


@router.post("/paper/reset")
def reset_paper_account(payload: Optional[PaperResetRequest] = None, _: str = Depends(valid_admin_account)):
    """清空模拟盘并用新的初始资金重建，下一次计算从当天开始。"""
    capital = payload.initial_capital if payload and payload.initial_capital else None
    if capital is None:
        capital = load_sector_nine_turn_config()["paper"]["initial_capital"]
    reset_paper(capital)
    return load_paper_overview()


@router.get("/backtests")
def get_backtests(limit: int = Query(default=30, ge=1, le=200), _: str = Depends(valid_admin_account)):
    return {
        "runs": list_runs(limit),
        "variants": BACKTEST_VARIANTS,
        "defaults": {
            "start_date": DEFAULT_BACKTEST_START.isoformat(),
            "end_date": date.today().isoformat(),
            "random_trials": 25,
        },
    }


@router.post("/backtests")
def create_backtest(payload: BacktestRequest, account_id: str = Depends(valid_admin_account)):
    """按当前配置起一次回测（独立子进程，同一时间只能有一个在跑）。"""
    try:
        return start_backtest(payload.model_dump(), created_by=account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/backtests/{run_id}")
def get_backtest(run_id: int, _: str = Depends(valid_admin_account)):
    try:
        return load_run_detail(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="回测不存在")


@router.post("/backtests/{run_id}/cancel")
def cancel_backtest_run(run_id: int, _: str = Depends(valid_admin_account)):
    try:
        return cancel_backtest(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="回测不存在")


@router.delete("/backtests/{run_id}")
def delete_backtest_run(run_id: int, _: str = Depends(valid_admin_account)):
    try:
        delete_backtest(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="回测不存在")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"deleted": run_id}


@router.get("/task")
def get_task(_: str = Depends(valid_admin_account)):
    """每日计算定时任务的状态（页面轮询"正在计算"用）。"""
    return scheduled_task_manager.get_task(SECTOR_NINE_TURN_TASK_KEY)


@router.post("/run")
def run_daily(payload: Optional[DailyRunRequest] = None, account_id: str = Depends(valid_admin_account)):
    """后台跑一次每日计算（走定时任务队列，与定时执行互斥）。"""
    runner_kwargs: Dict[str, Any] = {}
    if payload and payload.as_of:
        runner_kwargs["as_of"] = payload.as_of.isoformat()
    try:
        scheduled_task_manager.trigger_task(
            task_key=SECTOR_NINE_TURN_TASK_KEY,
            trigger_source="manual",
            triggered_by=account_id,
            background=True,
            **runner_kwargs,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return scheduled_task_manager.get_task(SECTOR_NINE_TURN_TASK_KEY)
