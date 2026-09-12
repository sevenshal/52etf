"""选股系统回测任务管理：主进程负责建任务、起子进程、查进度；子进程负责跑。

回测一次要逐月回放第一层（每个调仓日约一分钟）再逐日回放三层，是长时间的 CPU 密集计算，不能放进
定时任务队列（会堵住当天的数据同步）也不能放在 uvicorn 进程里（抢 GIL 拖慢网页）。沿用因子实验室
搜参的做法：用全新解释器起一个子进程，任务状态和结果都写 SQLite（多进程读写安全），页面轮询。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import traceback
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional

from ...database import (
    SessionLocal,
    StockSystemBacktestNav,
    StockSystemBacktestRun,
    StockSystemBacktestTrade,
)
from ...duckdb_utils import ANALYTICS_DB_PATH
from .backtest import VARIANT_KEYS, VARIANTS
from .config import load_stock_system_config, normalize_stock_system_config

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
FREQUENCIES = ("monthly", "weekly")
DEFAULT_START = date(2022, 7, 1)
SOURCE_DB_ENV = "STOCK_SYSTEM_BACKTEST_SOURCE_DB"
_PROCESSES: Dict[int, subprocess.Popen] = {}


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    process = _PROCESSES.get(pid)
    if process is not None:
        return process.poll() is None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _run_dict(run: StockSystemBacktestRun, *, include_summary: bool = True) -> Dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "progress": run.progress,
        "message": run.message,
        "params": run.params,
        "config": run.config if include_summary else None,
        "summary": run.summary if include_summary else None,
        "cancel_requested": run.cancel_requested,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _recover_stale_runs(db) -> None:
    """子进程已经不在了、状态还停在排队/运行中的任务，标成失败。"""
    for run in db.query(StockSystemBacktestRun).filter(StockSystemBacktestRun.status.in_(ACTIVE_STATUSES)).all():
        if not _pid_alive(run.pid):
            run.status = "failed"
            run.message = (run.message or "") + "（回测进程已退出）"
            run.finished_at = datetime.now()


def normalize_params(params: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    def as_date(value: Any, fallback: date) -> date:
        if not value:
            return fallback
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])

    start = as_date(params.get("start_date"), DEFAULT_START)
    end = as_date(params.get("end_date"), date.today())
    if start >= end:
        raise ValueError("开始日期必须早于结束日期")
    frequency = str(params.get("frequency") or "monthly")
    if frequency not in FREQUENCIES:
        raise ValueError("调仓频率只支持 monthly / weekly")
    variants = [variant for variant in VARIANT_KEYS if variant in set(params.get("variants") or VARIANT_KEYS)]
    if not variants:
        raise ValueError("至少选一个回测方案")
    capital = float(params.get("initial_capital") or config["paper"]["initial_capital"])
    if capital <= 0:
        raise ValueError("初始资金必须大于 0")
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "frequency": frequency,
        "variants": variants,
        "initial_capital": capital,
    }


def _spawn(run_id: int) -> subprocess.Popen:
    from .backtest_workspace import workspace_path

    src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    src_parent = os.path.dirname(src_root)
    bootstrap = (
        "import sys; "
        f"sys.path.insert(0, {src_parent!r}); "
        "from src.scripts.stock_system_backtest_worker import main; "
        f"sys.exit(main({int(run_id)}))"
    )
    env = {
        **os.environ,
        "ANALYTICS_DB_PATH": workspace_path(ANALYTICS_DB_PATH),
        SOURCE_DB_ENV: ANALYTICS_DB_PATH,
    }
    return subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
        env=env,
    )


def start_backtest(params: Mapping[str, Any], *, created_by: Optional[str] = None) -> Dict[str, Any]:
    config = normalize_stock_system_config(load_stock_system_config())
    normalized = normalize_params(params, config)
    with SessionLocal() as db:
        _recover_stale_runs(db)
        if db.query(StockSystemBacktestRun).filter(StockSystemBacktestRun.status.in_(ACTIVE_STATUSES)).count():
            db.commit()
            raise RuntimeError("已有回测在运行，请等它结束或先取消")
        run = StockSystemBacktestRun(
            status="queued", progress=0.0, message="等待回测进程启动", params=normalized, config=config,
            created_by=created_by, created_at=datetime.now(),
        )
        db.add(run)
        db.commit()
        run_id = run.id
    process = _spawn(run_id)
    _PROCESSES[process.pid] = process
    with SessionLocal() as db:
        run = db.get(StockSystemBacktestRun, run_id)
        if run.status == "queued":
            run.pid = process.pid
        db.commit()
    return get_run(run_id)


def cancel_backtest(run_id: int) -> Dict[str, Any]:
    with SessionLocal() as db:
        run = db.get(StockSystemBacktestRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in ACTIVE_STATUSES:
            run.cancel_requested = True
            run.message = "已请求取消，等回测进程响应"
        db.commit()
    return get_run(run_id)


def delete_backtest(run_id: int) -> None:
    with SessionLocal() as db:
        run = db.get(StockSystemBacktestRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in ACTIVE_STATUSES and _pid_alive(run.pid):
            raise RuntimeError("回测还在运行，先取消")
        db.query(StockSystemBacktestNav).filter(StockSystemBacktestNav.run_id == run_id).delete()
        db.query(StockSystemBacktestTrade).filter(StockSystemBacktestTrade.run_id == run_id).delete()
        db.delete(run)
        db.commit()


def list_runs(limit: int = 30) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        _recover_stale_runs(db)
        db.commit()
        rows = db.query(StockSystemBacktestRun).order_by(StockSystemBacktestRun.id.desc()).limit(int(limit)).all()
        return [_run_dict(row, include_summary=False) for row in rows]


def get_run(run_id: int) -> Dict[str, Any]:
    with SessionLocal() as db:
        run = db.get(StockSystemBacktestRun, run_id)
        if run is None:
            raise KeyError(run_id)
        return _run_dict(run)


def load_run_detail(run_id: int, trade_limit: int = 300) -> Dict[str, Any]:
    """结果页：任务信息、各方案净值曲线、每个方案最近的平仓记录。"""
    run = get_run(run_id)
    with SessionLocal() as db:
        navs: Dict[str, List[Dict[str, Any]]] = {}
        for row in (
            db.query(StockSystemBacktestNav)
            .filter(StockSystemBacktestNav.run_id == run_id)
            .order_by(StockSystemBacktestNav.variant, StockSystemBacktestNav.trade_date)
            .all()
        ):
            navs.setdefault(row.variant, []).append({
                "trade_date": row.trade_date.isoformat(), "nav": row.nav,
                "exposure_pct": row.exposure_pct, "positions": row.positions,
            })
        trades: Dict[str, List[Dict[str, Any]]] = {}
        for variant in [*VARIANT_KEYS]:
            rows = (
                db.query(StockSystemBacktestTrade)
                .filter(StockSystemBacktestTrade.run_id == run_id, StockSystemBacktestTrade.variant == variant)
                .order_by(StockSystemBacktestTrade.exit_date.desc(), StockSystemBacktestTrade.id.desc())
                .limit(int(trade_limit))
                .all()
            )
            if rows:
                trades[variant] = [
                    {
                        "ts_code": row.ts_code, "name": row.name,
                        "entry_date": row.entry_date.isoformat() if row.entry_date else None,
                        "exit_date": row.exit_date.isoformat() if row.exit_date else None,
                        "entry_price": row.entry_price, "exit_price": row.exit_price, "quantity": row.quantity,
                        "pnl": row.pnl, "return_pct": row.return_pct, "holding_days": row.holding_days,
                        "reason": row.reason,
                    }
                    for row in rows
                ]
    return {**run, "navs": navs, "trades": trades, "variants": VARIANTS}


# ---------------------------------------------------------------------------
# 子进程里执行
# ---------------------------------------------------------------------------

def _update(run_id: int, **fields: Any) -> None:
    with SessionLocal() as db:
        run = db.get(StockSystemBacktestRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()


def _cancel_requested(run_id: int) -> bool:
    with SessionLocal() as db:
        run = db.get(StockSystemBacktestRun, run_id)
        return bool(run and run.cancel_requested)


def save_results(run_id: int, result: Mapping[str, Any]) -> None:
    with SessionLocal() as db:
        db.query(StockSystemBacktestNav).filter(StockSystemBacktestNav.run_id == run_id).delete()
        db.query(StockSystemBacktestTrade).filter(StockSystemBacktestTrade.run_id == run_id).delete()
        db.bulk_save_objects([
            StockSystemBacktestNav(
                run_id=run_id, variant=variant, trade_date=point["trade_date"], nav=float(point["nav"]),
                exposure_pct=point.get("exposure_pct"), positions=point.get("positions"),
            )
            for variant, points in result["navs"].items()
            for point in points
        ])
        db.bulk_save_objects([
            StockSystemBacktestTrade(run_id=run_id, variant=variant, **trade)
            for variant, items in result["trades"].items()
            for trade in items
        ])
        db.commit()


def execute_run(run_id: int, source_db: str) -> int:
    """回测子进程的主体：复制数据工作区 → 回放 → 写结果。返回进程退出码。"""
    from .backtest import BacktestCancelled, run_backtest
    from .backtest_workspace import build_workspace

    with SessionLocal() as db:
        run = db.get(StockSystemBacktestRun, run_id)
        if run is None:
            return 1
        params, config = dict(run.params or {}), dict(run.config or {})
        run.status = "running"
        run.pid = os.getpid()
        run.started_at = datetime.now()
        run.message = "复制回测数据到工作区"
        db.commit()

    last_progress = {"pct": -1.0}

    def progress(pct: float, message: str) -> None:
        if pct - last_progress["pct"] < 0.5 and pct < 100:
            return
        last_progress["pct"] = pct
        _update(run_id, progress=round(pct, 1), message=message)

    try:
        start = date.fromisoformat(params["start_date"])
        end = date.fromisoformat(params["end_date"])
        build_workspace(source_db, start, end, log=lambda message: progress(max(last_progress["pct"], 1.0) + 0.1, message))
        result = run_backtest(
            start=start,
            end=end,
            config=config,
            variants=params.get("variants") or VARIANT_KEYS,
            frequency=params.get("frequency") or "monthly",
            initial_capital=params.get("initial_capital"),
            progress=progress,
            cancelled=lambda: _cancel_requested(run_id),
        )
        progress(98, "保存结果")
        save_results(run_id, result)
        _update(run_id, status="completed", progress=100.0, message="回测完成", summary=result["summary"],
                finished_at=datetime.now())
        return 0
    except BacktestCancelled:
        _update(run_id, status="cancelled", message="已取消", finished_at=datetime.now())
        return 0
    except Exception as exc:  # noqa: BLE001 失败原因写回任务，页面上能看到
        logger.exception("stock system backtest %s failed", run_id)
        _update(run_id, status="failed", message=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                finished_at=datetime.now())
        return 1
