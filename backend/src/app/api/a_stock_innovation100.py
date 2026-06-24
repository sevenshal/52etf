import logging
import threading
import uuid
from datetime import date, datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...core.database import (
    AStockInnovation100Constituent,
    AStockInnovation100Level,
    AStockInnovation100Rebalance,
    Session as ScopedSession,
    get_db,
    get_db_ctx,
)
from ...core.event_stream import publish_event
from ...robot.a_stock_innovation100 import (
    DEFAULT_START_DATE,
    INDEX_CODE,
    AStockInnovation100Builder,
    compute_yearly_returns,
    load_a_stock_innovation100_summary,
    load_benchmark_index_curves,
    rebuild_a_stock_innovation100,
)
from .account import valid_account


router = APIRouter(prefix="/api/a-stock-innovation100", tags=["A Stock Innovation 100"])
logger = logging.getLogger(__name__)

JOBS: Dict[str, Dict] = {}
JOBS_LOCK = threading.Lock()


class AStockInnovation100RebuildRequest(BaseModel):
    start_date: date = DEFAULT_START_DATE
    end_date: Optional[date] = None


def _update_job(task_id: str, **kwargs):
    event_payload = None
    account_id = None
    with JOBS_LOCK:
        job = JOBS.setdefault(task_id, {})
        job.update(kwargs)
        job["updated_at"] = datetime.now().isoformat()
        account_id = job.get("account_id")
        event_payload = {"task_id": task_id, **job}
    publish_event(account_id, "a_stock_innovation100_job", event_payload)


def _get_job(task_id: str) -> Dict:
    with JOBS_LOCK:
        return dict(JOBS.get(task_id, {}))


def _run_rebuild_job(task_id: str, start_date: date, end_date: Optional[date]):
    db = ScopedSession()

    def on_progress(payload: Dict):
        _update_job(task_id, **payload)

    _update_job(
        task_id,
        status="running",
        progress=0,
        message="任务已开始",
        result=None,
        error=None,
        started_at=datetime.now().isoformat(),
    )
    try:
        result = rebuild_a_stock_innovation100(
            db,
            start_date=start_date,
            end_date=end_date or date.today(),
            progress_callback=on_progress,
        )
        _update_job(
            task_id,
            status="completed",
            progress=100,
            message="回跑完成",
            result=result,
            finished_at=datetime.now().isoformat(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("A stock innovation100 rebuild failed")
        _update_job(
            task_id,
            status="failed",
            message=str(exc),
            error=str(exc),
            finished_at=datetime.now().isoformat(),
        )
    finally:
        ScopedSession.remove()


def _serialize_rebalance(row: AStockInnovation100Rebalance) -> Dict:
    return {
        "id": row.id,
        "index_code": row.index_code,
        "rebalance_date": row.rebalance_date.isoformat() if row.rebalance_date else None,
        "effective_date": row.effective_date.isoformat() if row.effective_date else None,
        "rebalance_type": row.rebalance_type,
        "constituent_count": row.constituent_count,
        "turnover_pct": row.turnover_pct,
        "total_circ_mv": row.total_circ_mv,
        "additions": row.additions or [],
        "removals": row.removals or [],
        "rule_snapshot": row.rule_snapshot or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _serialize_constituent(row: AStockInnovation100Constituent) -> Dict:
    return {
        "index_code": row.index_code,
        "rebalance_id": row.rebalance_id,
        "ts_code": row.ts_code,
        "rebalance_date": row.rebalance_date.isoformat() if row.rebalance_date else None,
        "effective_date": row.effective_date.isoformat() if row.effective_date else None,
        "name": row.name,
        "industry": row.industry,
        "rank": row.rank,
        "raw_weight_pct": row.raw_weight_pct,
        "weight_pct": row.weight_pct,
        "total_mv": row.total_mv,
        "circ_mv": row.circ_mv,
        "avg_amount_60d": row.avg_amount_60d,
        "action": row.action,
    }


@router.post("/rebuild")
def start_rebuild(
    payload: AStockInnovation100RebuildRequest,
    account_id: str = Depends(valid_account),
):
    task_id = uuid.uuid4().hex
    _update_job(
        task_id,
        status="queued",
        progress=0,
        message="任务已创建，等待执行",
        account_id=account_id,
        started_at=datetime.now().isoformat(),
    )
    thread = threading.Thread(
        target=_run_rebuild_job,
        args=(task_id, payload.start_date, payload.end_date),
        daemon=True,
        name=f"a-stock-innovation100-{task_id[:8]}",
    )
    thread.start()
    return {"task_id": task_id, "status": "queued"}


@router.get("/jobs/{task_id}")
def get_rebuild_job(
    task_id: str,
    account_id: str = Depends(valid_account),
):
    job = _get_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.get("account_id") and job.get("account_id") != account_id:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job


@router.get("/detail")
def get_detail(
    rebalance_id: Optional[int] = Query(None),
    rebalance_limit: int = Query(80, ge=1, le=500),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    summary = load_a_stock_innovation100_summary(db)
    levels = (
        db.query(AStockInnovation100Level)
        .filter(AStockInnovation100Level.index_code == INDEX_CODE)
        .order_by(AStockInnovation100Level.date.asc())
        .all()
    )
    rebalances = (
        db.query(AStockInnovation100Rebalance)
        .filter(AStockInnovation100Rebalance.index_code == INDEX_CODE)
        .order_by(AStockInnovation100Rebalance.rebalance_date.desc(), AStockInnovation100Rebalance.id.desc())
        .limit(rebalance_limit)
        .all()
    )

    selected_rebalance = None
    if rebalance_id:
        selected_rebalance = db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE,
            AStockInnovation100Rebalance.id == rebalance_id,
        ).first()
    if not selected_rebalance and rebalances:
        selected_rebalance = rebalances[0]

    selected_constituents = []
    if selected_rebalance:
        selected_constituents = (
            db.query(AStockInnovation100Constituent)
            .filter(
                AStockInnovation100Constituent.index_code == INDEX_CODE,
                AStockInnovation100Constituent.rebalance_id == selected_rebalance.id,
            )
            .order_by(AStockInnovation100Constituent.weight_pct.desc())
            .all()
        )

    benchmark_levels = (
        load_benchmark_index_curves(db, levels[0].date, levels[-1].date)
        if levels
        else []
    )

    return {
        "summary": summary,
        "levels": [
            {
                "date": row.date.isoformat(),
                "level": row.level,
                "daily_return_pct": row.daily_return_pct,
                "drawdown_pct": row.drawdown_pct,
                "constituent_count": row.constituent_count,
                "total_circ_mv": row.total_circ_mv,
            }
            for row in levels
        ],
        "benchmark_levels": benchmark_levels,
        "yearly_returns": compute_yearly_returns(levels),
        "rebalances": [_serialize_rebalance(row) for row in rebalances],
        "selected_rebalance": _serialize_rebalance(selected_rebalance) if selected_rebalance else None,
        "selected_constituents": [_serialize_constituent(row) for row in selected_constituents],
    }


@router.get("/rebalances/{rebalance_id}")
def get_rebalance_detail(
    rebalance_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    rebalance = db.query(AStockInnovation100Rebalance).filter(
        AStockInnovation100Rebalance.index_code == INDEX_CODE,
        AStockInnovation100Rebalance.id == rebalance_id,
    ).first()
    if not rebalance:
        raise HTTPException(status_code=404, detail="未找到该再平衡记录")
    constituents = (
        db.query(AStockInnovation100Constituent)
        .filter(
            AStockInnovation100Constituent.index_code == INDEX_CODE,
            AStockInnovation100Constituent.rebalance_id == rebalance.id,
        )
        .order_by(AStockInnovation100Constituent.weight_pct.desc())
        .all()
    )
    return {
        "rebalance": _serialize_rebalance(rebalance),
        "constituents": [_serialize_constituent(row) for row in constituents],
    }


def rebuild_a_stock_innovation100_for_scheduler(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    full_rebuild: bool = False,
) -> Dict:
    with get_db_ctx() as db:
        builder = AStockInnovation100Builder(db)
        try:
            end_value = end_date or date.today()
            if full_rebuild or start_date:
                return builder.rebuild(
                    start_date=start_date or DEFAULT_START_DATE,
                    end_date=end_value,
                    force_rebuild_outputs=True,
                )
            return builder.refresh_incremental(end_date=end_value)
        finally:
            builder.close()
