"""Administrator-only Chan theory analysis endpoints."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.analytics_database import AnalyticsSession
from ...core.database import AIStockTHSIndexCache, get_db_ctx
from ...core.services.a_stock_consensus import load_a_stock_klines, normalize_a_stock_symbol
from ...core.services.chan_analysis import analyze_bars
from ...core.services.chan_minute_data import (
    aggregate_minute_rows,
    backfill_symbol_minutes,
    load_minute_rows,
    minute_data_status,
)
from ...core.services.chan_minute_sync import ChanMinuteSyncManager
from ...core.services.chan_scanner import ChanScanManager, filter_stock_pool, get_scan, list_scans
from .account import valid_admin_account


router = APIRouter(prefix="/api/chan-analysis", tags=["Chan Analysis"])


class StockPoolFilters(BaseModel):
    min_total_mv: float | None = Field(default=None, ge=0)
    max_total_mv: float | None = Field(default=None, ge=0)
    min_circ_mv: float | None = Field(default=None, ge=0)
    max_circ_mv: float | None = Field(default=None, ge=0)
    min_avg_amount: float | None = Field(default=None, ge=0)
    liquidity_days: int = Field(default=20, ge=1, le=60)
    min_turnover_rate: float | None = Field(default=None, ge=0)
    max_turnover_rate: float | None = Field(default=None, ge=0)
    index_codes: list[str] = Field(default_factory=list)
    board_codes: list[str] = Field(default_factory=list)
    exclude_st: bool = True
    limit: int = Field(default=500, ge=1, le=2000)


class ScanRequest(BaseModel):
    freq: str = Field(default="d", pattern="^(1m|5m|30m|d)$")
    signal_side: str = Field(default="buy", pattern="^(buy|sell|all)$")
    realtime: bool = False
    filters: StockPoolFilters = Field(default_factory=StockPoolFilters)


@router.get("/chart/{symbol}")
def get_chan_chart(
    symbol: str,
    freq: str = Query(default="d", pattern="^(1m|5m|30m|d)$"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    _: str = Depends(valid_admin_account),
):
    """Return chart bars plus CZSC fractals, strokes, centers and signals."""
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="无效的A股代码")
    range_end = end_date or date.today()
    range_start = start_date or (range_end - timedelta(days=730 if freq == "d" else 100))
    if range_start > range_end:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")

    if freq == "d":
        db = AnalyticsSession()
        try:
            bars = load_a_stock_klines(db, normalized_symbol, start_date=range_start, end_date=range_end)
        finally:
            db.close()
            AnalyticsSession.remove()
    else:
        minute_rows = load_minute_rows(
            normalized_symbol,
            datetime.combine(range_start, datetime.min.time()),
            datetime.combine(range_end, datetime.max.time()),
        )
        bars = aggregate_minute_rows(normalized_symbol, minute_rows, freq)
    if len(bars) < 20:
        raise HTTPException(status_code=404, detail="可用K线不足20根")

    try:
        analysis = analyze_bars(normalized_symbol, bars, freq=freq)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"bars": bars, "analysis": analysis}


@router.get("/minute-data/status")
def get_minute_data_status(
    symbol: str | None = Query(default=None),
    _: str = Depends(valid_admin_account),
):
    return minute_data_status(symbol)


@router.post("/minute-data/backfill/{symbol}")
def backfill_minute_data(
    symbol: str,
    start_date: date = Query(...),
    end_date: date = Query(...),
    _: str = Depends(valid_admin_account),
):
    """Backfill one symbol; the all-market orchestration task calls the same service."""
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
    if (end_date - start_date).days > 120:
        raise HTTPException(status_code=400, detail="单次回补最多120个自然日")
    try:
        return backfill_symbol_minutes(symbol, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/minute-sync")
def get_minute_sync(_: str = Depends(valid_admin_account)):
    return ChanMinuteSyncManager.snapshot()


@router.post("/minute-sync")
def start_minute_sync(
    full: bool = Query(default=False),
    _: str = Depends(valid_admin_account),
):
    return ChanMinuteSyncManager.start(full=full)


@router.post("/minute-sync/cancel")
def cancel_minute_sync(_: str = Depends(valid_admin_account)):
    return ChanMinuteSyncManager.cancel()


@router.post("/pools/preview")
def preview_stock_pool(
    filters: StockPoolFilters,
    _: str = Depends(valid_admin_account),
):
    rows = filter_stock_pool(filters.model_dump())
    return {"count": len(rows), "rows": rows}


@router.post("/scans")
def start_scan(payload: ScanRequest, _: str = Depends(valid_admin_account)):
    try:
        run_id = ChanScanManager.start(payload.freq, payload.filters.model_dump(), payload.signal_side, payload.realtime)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": run_id, "status": "PENDING"}


@router.get("/scans")
def get_scans(limit: int = Query(default=20, ge=1, le=100), _: str = Depends(valid_admin_account)):
    return list_scans(limit)


@router.get("/scans/{run_id}")
def get_scan_detail(run_id: str, _: str = Depends(valid_admin_account)):
    result = get_scan(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="扫描批次不存在")
    return result


@router.post("/scans/{run_id}/cancel")
def cancel_scan(run_id: str, _: str = Depends(valid_admin_account)):
    ChanScanManager.cancel(run_id)
    return {"run_id": run_id, "cancel_requested": True}


@router.get("/boards")
def get_board_options(_: str = Depends(valid_admin_account)):
    with get_db_ctx() as db:
        rows = db.query(AIStockTHSIndexCache).order_by(AIStockTHSIndexCache.index_type, AIStockTHSIndexCache.name).all()
        return [
            {"code": row.ts_code, "name": row.name, "type": row.index_type, "count": row.constituent_count}
            for row in rows
        ]
