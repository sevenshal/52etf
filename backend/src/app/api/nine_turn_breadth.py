from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.services.nine_turn_breadth import (
    DEFAULT_PERCENTILE,
    get_nine_turn_breadth_detail,
    get_nine_turn_breadth_overview,
)
from .account import valid_admin_account


router = APIRouter(prefix="/api/research/nine-turn-breadth", tags=["Nine Turn Breadth Research"])


@router.get("/overview")
def overview(
    percentile: float = Query(DEFAULT_PERCENTILE, ge=50, le=99),
    refresh: bool = Query(False),
    _: str = Depends(valid_admin_account),
):
    try:
        return get_nine_turn_breadth_overview(percentile=percentile, force_refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/boards/{index_code}/detail")
def detail(
    index_code: str,
    percentile: float = Query(DEFAULT_PERCENTILE, ge=50, le=99),
    _: str = Depends(valid_admin_account),
):
    try:
        return get_nine_turn_breadth_detail(index_code=index_code, percentile=percentile)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="未找到该指数或板块") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
