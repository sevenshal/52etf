"""板块九转回测任务管理：主进程建任务、起子进程、查进度；子进程负责跑。

和选股系统回测同一套做法：DuckDB 是单写者，回测子进程先把要读的几张表复制到独立工作区文件
（复用 ``stock_system.backtest_workspace``，只是表清单换成本策略要读的那几张），之后整段回测
只读工作区，不和生产库抢锁。任务状态写 SQLite，页面轮询。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from ...database import (
    SectorNineTurnBacktestNav,
    SectorNineTurnBacktestRun,
    SectorNineTurnBacktestTrade,
    SessionLocal,
)
from ...duckdb_utils import ANALYTICS_DB_PATH
from .backtest import BENCHMARK_INDEX, VARIANTS, VARIANT_KEYS
from .config import DEFAULT_BACKTEST_START, load_sector_nine_turn_config, normalize_sector_nine_turn_config

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
SOURCE_DB_ENV = "SECTOR_NINE_TURN_BACKTEST_SOURCE_DB"
WORKSPACE_FILENAME = "sector_nine_turn_backtest_workspace.duckdb"
_PROCESSES: Dict[int, subprocess.Popen] = {}


def workspace_table_plan(start: date, end: date):
    """本策略只读这几张表：板块日线、成分权重、个股日线（前复权视图要的原始表 + 复权因子）、股票基本信息。"""
    market_from = start - timedelta(days=500)
    return [
        ("a_stock_basic", "TRUE", [], False),
        ("a_stock_market_daily", "trade_date BETWEEN ? AND ?", [market_from, end], False),
        # 复权因子不截尾：前复权视图以每只股票最新一个因子为锚，保持与生产一致
        ("a_stock_adj_factor", "trade_date >= ?", [market_from], False),
        ("a_stock_index_daily", "TRUE", [], False),
        ("a_stock_index_weight", "trade_date <= ?", [end], False),
    ]


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


def _run_dict(run: SectorNineTurnBacktestRun, *, include_summary: bool = True) -> Dict[str, Any]:
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
    for run in db.query(SectorNineTurnBacktestRun).filter(
        SectorNineTurnBacktestRun.status.in_(ACTIVE_STATUSES)
    ).all():
        if not _pid_alive(run.pid):
            run.status = "failed"
            run.message = (run.message or "") + "（回测进程已退出）"
            run.finished_at = datetime.now()


def normalize_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    def as_date(value: Any, fallback: date) -> date:
        if not value:
            return fallback
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])

    start = as_date(params.get("start_date"), DEFAULT_BACKTEST_START)
    end = as_date(params.get("end_date"), date.today())
    if start >= end:
        raise ValueError("开始日期必须早于结束日期")
    trials = int(params.get("random_trials") or 25)
    if trials < 0 or trials > 200:
        raise ValueError("随机挑股试验次数只能在 0~200 之间")
    return {"start_date": start.isoformat(), "end_date": end.isoformat(), "random_trials": trials}


def _spawn(run_id: int) -> subprocess.Popen:
    from ..stock_system.backtest_workspace import workspace_path

    src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    src_parent = os.path.dirname(src_root)
    bootstrap = (
        "import sys; "
        f"sys.path.insert(0, {src_parent!r}); "
        "from src.scripts.sector_nine_turn_backtest_worker import main; "
        f"sys.exit(main({int(run_id)}))"
    )
    env = {
        **os.environ,
        "ANALYTICS_DB_PATH": workspace_path(ANALYTICS_DB_PATH, WORKSPACE_FILENAME),
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
    config = normalize_sector_nine_turn_config(load_sector_nine_turn_config())
    normalized = normalize_params(params)
    with SessionLocal() as db:
        _recover_stale_runs(db)
        if db.query(SectorNineTurnBacktestRun).filter(
            SectorNineTurnBacktestRun.status.in_(ACTIVE_STATUSES)
        ).count():
            db.commit()
            raise RuntimeError("已有回测在运行，请等它结束或先取消")
        run = SectorNineTurnBacktestRun(
            status="queued", progress=0.0, message="等待回测进程启动", params=normalized, config=config,
            created_by=created_by, created_at=datetime.now(),
        )
        db.add(run)
        db.commit()
        run_id = run.id
    process = _spawn(run_id)
    _PROCESSES[process.pid] = process
    with SessionLocal() as db:
        run = db.get(SectorNineTurnBacktestRun, run_id)
        if run.status == "queued":
            run.pid = process.pid
        db.commit()
    return get_run(run_id)


def cancel_backtest(run_id: int) -> Dict[str, Any]:
    with SessionLocal() as db:
        run = db.get(SectorNineTurnBacktestRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in ACTIVE_STATUSES:
            run.cancel_requested = True
            run.message = "已请求取消，等回测进程响应"
        db.commit()
    return get_run(run_id)


def delete_backtest(run_id: int) -> None:
    with SessionLocal() as db:
        run = db.get(SectorNineTurnBacktestRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in ACTIVE_STATUSES and _pid_alive(run.pid):
            raise RuntimeError("回测还在运行，先取消")
        db.query(SectorNineTurnBacktestNav).filter(SectorNineTurnBacktestNav.run_id == run_id).delete()
        db.query(SectorNineTurnBacktestTrade).filter(SectorNineTurnBacktestTrade.run_id == run_id).delete()
        db.delete(run)
        db.commit()


def list_runs(limit: int = 30) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        _recover_stale_runs(db)
        db.commit()
        rows = db.query(SectorNineTurnBacktestRun).order_by(SectorNineTurnBacktestRun.id.desc()).limit(int(limit)).all()
        return [_run_dict(row, include_summary=False) for row in rows]


def get_run(run_id: int) -> Dict[str, Any]:
    with SessionLocal() as db:
        run = db.get(SectorNineTurnBacktestRun, run_id)
        if run is None:
            raise KeyError(run_id)
        return _run_dict(run)


def load_run_detail(run_id: int, trade_limit: int = 300) -> Dict[str, Any]:
    """结果页：任务信息、各方案净值曲线、每个方案最近的交易。"""
    run = get_run(run_id)
    with SessionLocal() as db:
        navs: Dict[str, List[Dict[str, Any]]] = {}
        for row in (
            db.query(SectorNineTurnBacktestNav)
            .filter(SectorNineTurnBacktestNav.run_id == run_id)
            .order_by(SectorNineTurnBacktestNav.variant, SectorNineTurnBacktestNav.trade_date)
            .all()
        ):
            navs.setdefault(row.variant, []).append({
                "trade_date": row.trade_date.isoformat(), "nav": row.nav, "positions": row.positions,
            })
        trades: Dict[str, List[Dict[str, Any]]] = {}
        for variant in [*VARIANT_KEYS]:
            rows = (
                db.query(SectorNineTurnBacktestTrade)
                .filter(
                    SectorNineTurnBacktestTrade.run_id == run_id,
                    SectorNineTurnBacktestTrade.variant == variant,
                    SectorNineTurnBacktestTrade.taken.is_(True),
                )
                .order_by(SectorNineTurnBacktestTrade.exit_date.desc(), SectorNineTurnBacktestTrade.id.desc())
                .limit(int(trade_limit))
                .all()
            )
            if rows:
                trades[variant] = [
                    {
                        "ts_code": row.ts_code, "name": row.name,
                        "sector_code": row.sector_code, "sector_name": row.sector_name,
                        "sector_fear_score": row.sector_fear_score,
                        "entry_date": row.entry_date.isoformat() if row.entry_date else None,
                        "exit_date": row.exit_date.isoformat() if row.exit_date else None,
                        "entry_price": row.entry_price, "exit_price": row.exit_price,
                        "closed": row.closed, "sell_drawdown_atr": row.sell_drawdown_atr,
                        "return_pct": row.return_pct, "holding_days": row.holding_days,
                    }
                    for row in rows
                ]
    return {**run, "navs": navs, "trades": trades, "variants": VARIANTS, "benchmark_index": BENCHMARK_INDEX}


# ---------------------------------------------------------------------------
# 子进程里执行
# ---------------------------------------------------------------------------

def _update(run_id: int, **fields: Any) -> None:
    with SessionLocal() as db:
        run = db.get(SectorNineTurnBacktestRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()


def _cancel_requested(run_id: int) -> bool:
    with SessionLocal() as db:
        run = db.get(SectorNineTurnBacktestRun, run_id)
        return bool(run and run.cancel_requested)


def save_results(run_id: int, result: Mapping[str, Any]) -> None:
    calendar = [date.fromisoformat(day) for day in result["calendar"]]
    with SessionLocal() as db:
        db.query(SectorNineTurnBacktestNav).filter(SectorNineTurnBacktestNav.run_id == run_id).delete()
        db.query(SectorNineTurnBacktestTrade).filter(SectorNineTurnBacktestTrade.run_id == run_id).delete()
        navs = [
            SectorNineTurnBacktestNav(
                run_id=run_id, variant=variant, trade_date=calendar[index],
                nav=float(value), positions=(payload.get("positions") or [None] * len(calendar))[index],
            )
            for variant, payload in result["variants"].items()
            for index, value in enumerate(payload["navs"])
        ]
        navs.extend(
            SectorNineTurnBacktestNav(run_id=run_id, variant="benchmark", trade_date=calendar[index],
                                      nav=float(value), positions=None)
            for index, value in enumerate(result["benchmark"]["navs"])
        )
        db.bulk_save_objects(navs)
        db.bulk_save_objects([
            SectorNineTurnBacktestTrade(
                run_id=run_id, variant=variant, ts_code=trade["ts_code"], name=trade.get("name"),
                sector_code=trade.get("sector_code"), sector_name=trade.get("sector_name"),
                sector_signal_date=trade.get("sector_signal_date"),
                sector_fear_score=trade.get("sector_fear_score"),
                entry_date=trade["entry_date"], exit_date=trade["exit_date"],
                entry_price=trade["entry_price"], exit_price=trade["exit_price"],
                closed=bool(trade["closed"]), sell_drawdown_atr=trade.get("sell_drawdown_atr"),
                return_pct=trade["return_pct"], holding_days=trade["holding_days"],
                taken=bool(trade.get("taken")),
            )
            for variant, items in result["trades"].items()
            for trade in items
        ])
        db.commit()


def summarize(result: Mapping[str, Any]) -> Dict[str, Any]:
    """存回任务行的摘要：不含逐日净值和逐笔交易。"""
    def strip(payload: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in payload.items() if key not in ("navs", "positions")}

    return {
        key: value for key, value in result.items() if key not in ("trades", "variants", "benchmark", "calendar", "triggers")
    } | {
        "variants": {key: strip(payload) for key, payload in result["variants"].items()},
        "benchmark": strip(result["benchmark"]),
    }


def execute_run(run_id: int, source_db: str) -> int:
    """回测子进程的主体：复制数据工作区 → 回放 → 写结果。返回进程退出码。"""
    from ..stock_system.backtest_workspace import build_workspace
    from .backtest import BacktestCancelled, run_backtest

    with SessionLocal() as db:
        run = db.get(SectorNineTurnBacktestRun, run_id)
        if run is None:
            return 1
        params, config = dict(run.params or {}), dict(run.config or {})
        run.status = "running"
        run.pid = os.getpid()
        run.started_at = datetime.now()
        run.message = "复制回测数据到工作区"
        db.commit()

    last = {"pct": -1.0}

    def progress(ratio: float, message: str) -> None:
        pct = round(ratio * 100.0, 1)
        if pct - last["pct"] < 0.5 and pct < 100:
            return
        last["pct"] = pct
        _update(run_id, progress=pct, message=message)

    try:
        start = date.fromisoformat(params["start_date"])
        end = date.fromisoformat(params["end_date"])
        build_workspace(source_db, start, end, plan=workspace_table_plan(start, end),
                        log=lambda message: _update(run_id, message=message))
        result = run_backtest(
            config, start, end,
            progress=progress,
            should_cancel=lambda: _cancel_requested(run_id),
            random_trials=int(params.get("random_trials") or 0),
        )
        _update(run_id, progress=98.0, message="保存结果")
        save_results(run_id, result)
        _update(run_id, status="completed", progress=100.0, message="回测完成",
                summary=summarize(result), finished_at=datetime.now())
        return 0
    except BacktestCancelled:
        _update(run_id, status="cancelled", message="已取消", finished_at=datetime.now())
        return 0
    except Exception as exc:  # noqa: BLE001 失败原因写回任务，页面上能看到
        logger.exception("sector nine turn backtest %s failed", run_id)
        _update(run_id, status="failed",
                message=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                finished_at=datetime.now())
        return 1
