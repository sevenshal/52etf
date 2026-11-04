from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from ...core.services.a_stock_crowding import CrowdingDataError, fetch_crowding_dashboard
from .account import valid_market_viewer

router = APIRouter(prefix="/api/market/crowding", tags=["A Stock Crowding"])


@router.get("")
async def get_crowding_dashboard(
    days: int = Query(250, ge=20, le=1250),
    account_id: str = Depends(valid_market_viewer),
):
    try:
        return await run_in_threadpool(fetch_crowding_dashboard, days)
    except CrowdingDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
