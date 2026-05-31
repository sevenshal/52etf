from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.services.a_stock_fund_flow import (
    FundFlowDataError,
    fetch_fund_flow_dashboard,
    fetch_industry_rank,
    fetch_market_rank,
    fetch_northbound_realtime,
    fetch_stock_fund_flow,
)
from .account import valid_account


router = APIRouter(prefix="/api/a-stock-fund-flow", tags=["A Stock Fund Flow"])


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FundFlowDataError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/dashboard")
def get_dashboard(
    limit: int = Query(30, ge=5, le=100),
    stock_code: Optional[str] = Query(None),
    account_id: str = Depends(valid_account),
):
    return fetch_fund_flow_dashboard(limit=limit, stock_code=stock_code)


@router.get("/northbound")
def get_northbound(account_id: str = Depends(valid_account)):
    try:
        return fetch_northbound_realtime()
    except Exception as exc:
        raise _translate_error(exc)


@router.get("/market-rank")
def get_market_rank(
    limit: int = Query(30, ge=5, le=100),
    direction: str = Query("inflow", pattern="^(inflow|outflow)$"),
    account_id: str = Depends(valid_account),
):
    try:
        return fetch_market_rank(limit=limit, direction=direction)
    except Exception as exc:
        raise _translate_error(exc)


@router.get("/industry-rank")
def get_industry_rank(
    limit: int = Query(30, ge=5, le=100),
    direction: str = Query("inflow", pattern="^(inflow|outflow)$"),
    account_id: str = Depends(valid_account),
):
    try:
        return fetch_industry_rank(limit=limit, direction=direction)
    except Exception as exc:
        raise _translate_error(exc)


@router.get("/stocks/{code}")
def get_stock_fund_flow(
    code: str,
    daily_limit: int = Query(60, ge=5, le=120),
    account_id: str = Depends(valid_account),
):
    try:
        return fetch_stock_fund_flow(code, daily_limit=daily_limit)
    except Exception as exc:
        raise _translate_error(exc)
