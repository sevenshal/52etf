from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

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
