"""Administrator-only Chan theory analysis endpoints."""

from datetime import date, datetime, time, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from ...core.analytics_database import AnalyticsSession
from ...core.database import AIStockTHSIndexCache, get_db_ctx
from ...core.services.a_stock_consensus import load_a_stock_klines, normalize_a_stock_symbol
from ...core.services.chan_analysis import analyze_bars
from ...core.services.chan_minute_data import (
    aggregate_minute_rows,
    backfill_symbol_minutes,
    fetch_realtime_minute_rows,
    is_complete_a_share_minute_day,
    load_minute_rows,
    merge_minute_rows,
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
    limit: int = Field(default=500, ge=1, le=5000)


class ScanRequest(BaseModel):
    freq: str = Field(default="d", pattern="^(1m|5m|30m|d)$")
    signal_side: str = Field(default="buy", pattern="^(buy|sell|all)$")
    realtime: bool = False
    filters: StockPoolFilters = Field(default_factory=StockPoolFilters)


@router.get("/symbols")
def search_stock_symbols(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=20, ge=1, le=50),
    _: str = Depends(valid_admin_account),
):
    """Search active A-shares by code or Chinese stock name."""
    search_text = str(q or "").strip().upper()
    like_pattern = f"%{search_text}%"
    prefix_pattern = f"{search_text}%"
    db = AnalyticsSession()
    try:
        rows = db.execute(
            text(
                """
                SELECT ts_code, symbol, name, industry, market
                FROM a_stock_basic
                WHERE list_status = 'L'
                  AND (
                        :query = ''
                     OR UPPER(ts_code) LIKE :pattern
                     OR UPPER(symbol) LIKE :pattern
                     OR UPPER(COALESCE(name, '')) LIKE :pattern
                  )
                ORDER BY
                    CASE
                        WHEN UPPER(ts_code) = :query OR UPPER(symbol) = :query THEN 0
                        WHEN UPPER(ts_code) LIKE :prefix OR UPPER(symbol) LIKE :prefix THEN 1
                        WHEN UPPER(COALESCE(name, '')) LIKE :prefix THEN 2
                        ELSE 3
                    END,
                    ts_code
                LIMIT :limit
                """
            ),
            {
                "query": search_text,
                "pattern": like_pattern,
                "prefix": prefix_pattern,
                "limit": limit,
            },
        ).mappings().all()
        return [
            {
                "value": row["ts_code"],
                "label": f"{row['name']} · {row['ts_code']}" if row["name"] else row["ts_code"],
                "name": row["name"],
                "industry": row["industry"],
                "market": row["market"],
            }
            for row in rows
        ]
    finally:
        db.close()
        AnalyticsSession.remove()


@router.get("/chart/{symbol}")
def get_chan_chart(
    symbol: str,
    freq: str = Query(default="d", pattern="^(1m|5m|30m|d)$"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    _: str = Depends(valid_admin_account),
):
    """Return chart bars plus strict native Chan fractals, strokes, centers and signals."""
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="无效的A股代码")
    range_end = end_date or date.today()
    # Minute charts need roughly 120 trading days of context; 180 calendar
    # days leaves room for weekends and exchange holidays.
    range_start = start_date or (range_end - timedelta(days=730 if freq == "d" else 180))
    if range_start > range_end:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")

    realtime_merged = False
    historical_today_complete = None
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
        today = date.today()
        today_in_range = range_start <= today <= range_end
        historical_today_complete = (
            is_complete_a_share_minute_day(minute_rows, today) if today_in_range else None
        )
        bars = aggregate_minute_rows(normalized_symbol, minute_rows, freq)
        now = datetime.now()
        should_fetch_realtime = (
            today_in_range
            and today.weekday() < 5
            and now.time() >= time(9, 30)
            and not historical_today_complete
        )
        if should_fetch_realtime:
            realtime_freq = {"1m": "1MIN", "5m": "5MIN", "30m": "30MIN"}[freq]
            realtime_rows = fetch_realtime_minute_rows([normalized_symbol], realtime_freq).get(
                normalized_symbol, []
            )
            realtime_rows = [
                row
                for row in realtime_rows
                if pd.notna(timestamp := pd.to_datetime(row.get("timestamp"), errors="coerce"))
                and timestamp.date() == today
            ]
            if realtime_rows:
                bars = merge_minute_rows(bars, realtime_rows)
                realtime_merged = True
    if len(bars) < 20:
        raise HTTPException(status_code=404, detail="可用K线不足20根")

    try:
        analysis = analyze_bars(normalized_symbol, bars, freq=freq, confirmed=not realtime_merged)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "bars": bars,
        "analysis": analysis,
        "realtime_merged": realtime_merged,
        "historical_today_complete": historical_today_complete,
    }


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
