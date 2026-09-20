from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from ...core.services.earnings_gap import EarningsGapDataError, get_earnings_gap
from ...core.services.market_alerts import fetch_alerts, run_alert_scan
from ...core.services.market_overview import (
    MarketOverviewDataError,
    fetch_breadth_distribution,
    fetch_daily_amount,
    fetch_index_overview,
)
from ...core.services.market_volume import MarketVolumeDataError, fetch_intraday_volume_compare
from .account import valid_admin_account


router = APIRouter(prefix="/api/market", tags=["Market"])


@router.get("/intraday-volume")
def get_intraday_volume(
    target_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    compare_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    account_id: str = Depends(valid_admin_account),
):
    try:
        return fetch_intraday_volume_compare(target_date=target_date, compare_date=compare_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except MarketVolumeDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/earnings-gap")
async def get_earnings_gap_signals(
    refresh: bool = Query(False),
    account_id: str = Depends(valid_admin_account),
):
    """净利润断层信号：默认读最近一次计算的快照，refresh=true 时立即重算。"""
    try:
        return await run_in_threadpool(get_earnings_gap, refresh)
    except (EarningsGapDataError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/index-overview")
async def get_index_overview(account_id: str = Depends(valid_admin_account)):
    """指数概览条：上证/深证成指/创业板指/科创50 的现价、涨跌幅与当日分时曲线。"""
    try:
        return await run_in_threadpool(fetch_index_overview)
    except MarketOverviewDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/breadth-distribution")
async def get_breadth_distribution(account_id: str = Depends(valid_admin_account)):
    """全A涨跌分布：分析库最新交易日按涨跌幅分档，涨停/跌停单列。"""
    try:
        return await run_in_threadpool(fetch_breadth_distribution)
    except (MarketOverviewDataError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/daily-amount")
async def get_daily_amount(
    days: int = Query(120, ge=20, le=250),
    account_id: str = Depends(valid_admin_account),
):
    """每日两市成交额（亿元）。"""
    try:
        return await run_in_threadpool(fetch_daily_amount, days)
    except MarketOverviewDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/alerts")
async def get_market_alerts(
    date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    label: Optional[str] = Query(None, max_length=8),
    account_id: str = Depends(valid_admin_account),
):
    """提示看板：某交易日的命中记录与命中后表现统计。"""
    from datetime import date as date_cls
    trade_date = date_cls.fromisoformat(date) if date else None
    return await run_in_threadpool(fetch_alerts, trade_date, label)


@router.post("/alerts/scan")
async def trigger_alert_scan(account_id: str = Depends(valid_admin_account)):
    """手动触发一轮扫描（定时任务每分钟自动跑，这里用于调试）。"""
    return await run_in_threadpool(run_alert_scan)
