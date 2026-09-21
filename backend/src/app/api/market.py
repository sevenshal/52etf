from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from pydantic import BaseModel

from ...core.services.earnings_gap import (
    DEFAULT_CONFIG,
    EarningsGapDataError,
    get_earnings_gap,
    load_config,
    refresh_earnings_gap,
    save_config,
)
from ...core.services.industry_relation import (
    IndustryRelationDataError,
    fetch_industry_history,
    fetch_industry_relation,
)
from ...core.services.intraday_minutes import fetch_intraday_minutes
from ...core.services.market_alerts import fetch_alerts, run_alert_scan
from ...core.services.market_overview import (
    MarketOverviewDataError,
    fetch_breadth_distribution,
    fetch_daily_amount,
    fetch_index_overview,
)
from ...core.services.market_volume import MarketVolumeDataError, fetch_intraday_volume_compare
from .account import ADMIN_ACCOUNT_ID, valid_admin_account, valid_market_viewer


router = APIRouter(prefix="/api/market", tags=["Market"])


@router.get("/intraday-volume")
def get_intraday_volume(
    target_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    compare_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    account_id: str = Depends(valid_market_viewer),
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
    account_id: str = Depends(valid_market_viewer),
):
    """净利润断层信号：默认读最近一次计算的快照，refresh=true 时立即重算（全市场扫描，仅管理员）。"""
    if refresh and account_id != ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="仅管理员可重新计算")
    try:
        return await run_in_threadpool(get_earnings_gap, refresh)
    except (EarningsGapDataError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/index-overview")
async def get_index_overview(account_id: str = Depends(valid_market_viewer)):
    """指数概览条：上证/深证成指/创业板指/科创50 的现价、涨跌幅与当日分时曲线。"""
    try:
        return await run_in_threadpool(fetch_index_overview)
    except MarketOverviewDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/breadth-distribution")
async def get_breadth_distribution(account_id: str = Depends(valid_market_viewer)):
    """全A涨跌分布：分析库最新交易日按涨跌幅分档，涨停/跌停单列。"""
    try:
        return await run_in_threadpool(fetch_breadth_distribution)
    except (MarketOverviewDataError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/daily-amount")
async def get_daily_amount(
    days: int = Query(120, ge=20, le=250),
    account_id: str = Depends(valid_market_viewer),
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
    level: str = Query("l1", pattern="^(l1|l2|l3)$"),
    l1_label: Optional[str] = Query(None, max_length=8),
    l2_label: Optional[str] = Query(None, max_length=8),
    account_id: str = Depends(valid_market_viewer),
):
    """提示看板：某交易日的命中记录与命中后表现统计；行业分布按申万 level 级分组（默认一级）。

    l1_label / l2_label 按个股所属申万一级/二级行业的当日标签过滤（none=该行业无信号）。
    """
    from datetime import date as date_cls
    trade_date = date_cls.fromisoformat(date) if date else None
    return await run_in_threadpool(fetch_alerts, trade_date, label, level, l1_label, l2_label)


@router.post("/alerts/scan")
async def trigger_alert_scan(account_id: str = Depends(valid_admin_account)):
    """手动触发一轮扫描（定时任务每分钟自动跑，这里用于调试）。"""
    return await run_in_threadpool(run_alert_scan)


@router.get("/industry-relation")
async def get_industry_relation(
    universe: str = Query("all", max_length=16),
    focus: str = Query("", max_length=4),
    l1_label: str = Query("", max_length=8),
    l2_label: str = Query("", max_length=8),
    account_id: str = Depends(valid_market_viewer),
):
    """行业关联：申万一/二/三级行业的盘中聚合与成分股明细。

    focus 按个股自身标签/涨停状态过滤；l1_label / l2_label 按所属申万一级/二级行业的当日标签过滤
    （none=该行业无信号），三者可以组合。
    """
    try:
        return await run_in_threadpool(fetch_industry_relation, universe, focus, l1_label, l2_label)
    except (IndustryRelationDataError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/industry-relation/history")
async def get_industry_relation_history(
    level: str = Query("l1", pattern="^(?i)(l1|l2|l3)$"),
    name: str = Query(..., min_length=1, max_length=64),
    days: int = Query(60, ge=5, le=250),
    account_id: str = Depends(valid_market_viewer),
):
    """某个行业的申万行业指数走势（关联结构面板）。"""
    try:
        return await run_in_threadpool(fetch_industry_history, level, name, days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (IndustryRelationDataError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class EarningsGapConfigPayload(BaseModel):
    sources: Optional[List[str]] = None
    min_profit_yoy: Optional[float] = None
    max_profit_yoy: Optional[float] = None
    min_gap_pct: Optional[float] = None
    min_amount_yuan: Optional[float] = None
    min_listed_trade_days: Optional[int] = None
    require_bullish_close: Optional[bool] = None
    require_unsealed: Optional[bool] = None
    require_true_gap: Optional[bool] = None
    min_amount_ratio: Optional[float] = None
    amount_ratio_days: Optional[int] = None


@router.get("/earnings-gap/config")
def get_earnings_gap_config(account_id: str = Depends(valid_market_viewer)):
    return {"config": load_config(), "defaults": DEFAULT_CONFIG}


@router.put("/earnings-gap/config")
async def update_earnings_gap_config(
    payload: EarningsGapConfigPayload,
    recompute: bool = Query(True),
    account_id: str = Depends(valid_admin_account),
):
    """保存阈值；默认保存后立即按新阈值重算一次（全市场扫描，秒级到几十秒）。"""
    try:
        config = save_config(payload.model_dump(exclude_none=True), updated_by=account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not recompute:
        return {"config": config}
    try:
        return {"config": config, "signals": await run_in_threadpool(refresh_earnings_gap)}
    except (EarningsGapDataError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/intraday-minutes")
async def get_intraday_minutes(
    ts_code: str = Query(..., min_length=6, max_length=16),
    days: int = Query(5, ge=1, le=10),
    account_id: str = Depends(valid_market_viewer),
):
    """个股分时小图：库里分钟历史 + 当日实时补齐，点开个股时按需调用。"""
    try:
        return await run_in_threadpool(fetch_intraday_minutes, ts_code, days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
