"""管理员专用：选股交易系统接口（基本面股票池、情绪择时与仓位控制、技术信号与模拟盘）。"""

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.services.stock_system.allocation import load_allocation
from ...core.services.stock_system.backtest import VARIANTS as BACKTEST_VARIANTS
from ...core.services.stock_system.backtest_runner import (
    DEFAULT_START as BACKTEST_DEFAULT_START,
    cancel_backtest,
    delete_backtest,
    list_runs,
    load_run_detail,
    start_backtest,
)
from ...core.services.stock_system.config import (
    config_definitions,
    default_stock_system_config,
    load_stock_system_config,
    reset_stock_system_config,
    save_stock_system_config,
)
from ...core.services.stock_system.fundamental_pool import list_pool_runs, load_pool_snapshot
from ...core.services.stock_system.paper import load_paper_overview, reset_paper
from ...core.services.stock_system.trading import load_signals
from ...robot.scheduled_tasks import STOCK_SYSTEM_POOL_TASK_KEY, scheduled_task_manager
from .account import valid_admin_account

router = APIRouter(prefix="/api/stock-system", tags=["Stock System"])


class PoolRunRequest(BaseModel):
    as_of: Optional[date] = None
    # 只重算情绪择时、仓位和技术信号（沿用已有的股票池快照），改了后两层参数后用它
    only_allocation: bool = False


class PaperResetRequest(BaseModel):
    # 留空用配置里的初始资金
    initial_capital: Optional[float] = Field(default=None, gt=0)


def _config_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "config": config,
        "defaults": default_stock_system_config(),
        "definitions": config_definitions(),
    }


def _read(loader, *args, **kwargs):
    try:
        return loader(*args, **kwargs)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/config")
def get_stock_system_config(_: str = Depends(valid_admin_account)):
    """当前超参数、默认值和表单定义（闸门/因子/触发条件的名称、单位、说明）。"""
    return _config_payload(load_stock_system_config())


@router.put("/config")
def update_stock_system_config(
    payload: Dict[str, Any] = Body(...),
    account_id: str = Depends(valid_admin_account),
):
    """整份保存；越界值会被夹到合法区间，未知字段丢弃。下一次计算时生效。"""
    return _config_payload(save_stock_system_config(payload, updated_by=account_id))


@router.post("/config/reset")
def reset_config(account_id: str = Depends(valid_admin_account)):
    return _config_payload(reset_stock_system_config(updated_by=account_id))


@router.get("/pool")
def get_pool(
    trade_date: Optional[date] = Query(default=None, description="交易日，留空取最新一次快照"),
    view: str = Query(default="pool", pattern="^(pool|passed|excluded|all)$"),
    _: str = Depends(valid_admin_account),
):
    """股票池快照：pool=入池，passed=通过闸门，excluded=未通过闸门，all=股票池范围内全部。"""
    return _read(load_pool_snapshot, trade_date, view=view)


@router.get("/runs")
def get_runs(limit: int = Query(default=30, ge=1, le=365), _: str = Depends(valid_admin_account)):
    return _read(list_pool_runs, limit)


@router.get("/allocation")
def get_allocation(
    trade_date: Optional[date] = Query(default=None, description="交易日，留空取最新一次"),
    _: str = Depends(valid_admin_account),
):
    """情绪择时与仓位：市场/各板块状态、入池股票的目标仓位或没分到仓位的原因。"""
    return _read(load_allocation, trade_date)


@router.get("/signals")
def get_signals(
    trade_date: Optional[date] = Query(default=None, description="交易日，留空取最新一次"),
    _: str = Depends(valid_admin_account),
):
    """技术信号：候选的入场触发/过滤/计划仓位，模拟盘持仓的出场理由。"""
    return _read(load_signals, trade_date)


@router.get("/paper")
def get_paper(_: str = Depends(valid_admin_account)):
    """模拟盘：账户、持仓、净值曲线、最近订单。"""
    return load_paper_overview()


@router.post("/paper/reset")
def reset_paper_account(payload: Optional[PaperResetRequest] = None, _: str = Depends(valid_admin_account)):
    """清空模拟盘并用新的初始资金重建，下一次计算从当天开始。"""
    capital = payload.initial_capital if payload and payload.initial_capital else None
    if capital is None:
        capital = load_stock_system_config()["paper"]["initial_capital"]
    reset_paper(capital)
    return load_paper_overview()


class BacktestRequest(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    frequency: str = Field(default="monthly", pattern="^(monthly|weekly)$")
    variants: Optional[List[str]] = None
    initial_capital: Optional[float] = Field(default=None, gt=0)


@router.get("/backtests")
def get_backtests(limit: int = Query(default=30, ge=1, le=200), _: str = Depends(valid_admin_account)):
    """回测任务列表 + 可选方案 + 表单默认值。"""
    return {
        "runs": list_runs(limit),
        "variants": BACKTEST_VARIANTS,
        "defaults": {
            "start_date": BACKTEST_DEFAULT_START.isoformat(),
            "end_date": date.today().isoformat(),
            "frequency": "monthly",
            "initial_capital": load_stock_system_config()["paper"]["initial_capital"],
        },
    }


@router.post("/backtests")
def create_backtest(payload: BacktestRequest, account_id: str = Depends(valid_admin_account)):
    """按当前的选股系统配置起一次回测（独立子进程，同一时间只能有一个在跑）。"""
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


@router.get("/pool/task")
def get_pool_task(_: str = Depends(valid_admin_account)):
    """选股系统定时任务的状态（页面轮询"正在计算"用）。"""
    return scheduled_task_manager.get_task(STOCK_SYSTEM_POOL_TASK_KEY)


@router.post("/pool/run")
def run_pool(payload: Optional[PoolRunRequest] = None, account_id: str = Depends(valid_admin_account)):
    """后台计算一次（走定时任务队列，与定时执行互斥）：股票池 → 情绪择时与仓位 → 技术信号与模拟盘。"""
    runner_kwargs: Dict[str, Any] = {}
    if payload and payload.as_of:
        runner_kwargs["as_of"] = payload.as_of.isoformat()
    if payload and payload.only_allocation:
        runner_kwargs["only_allocation"] = True
    try:
        scheduled_task_manager.trigger_task(
            task_key=STOCK_SYSTEM_POOL_TASK_KEY,
            trigger_source="manual",
            triggered_by=account_id,
            background=True,
            **runner_kwargs,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return scheduled_task_manager.get_task(STOCK_SYSTEM_POOL_TASK_KEY)
