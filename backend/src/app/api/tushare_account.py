from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .account import valid_admin_account
from ...core.services.tushare_account import (
    get_tushare_account_settings,
    update_tushare_account_settings,
)


router = APIRouter(prefix="/api/tushare-account", tags=["tushare-account"])


class TushareAccountUpdate(BaseModel):
    # Omitted keeps the saved value; an empty string clears the page setting.
    api_token: Optional[str] = Field(default=None, max_length=512)


@router.get("")
def read_tushare_account(_: str = Depends(valid_admin_account)):
    return get_tushare_account_settings()


@router.put("")
def save_tushare_account(
    payload: TushareAccountUpdate,
    account_id: str = Depends(valid_admin_account),
):
    try:
        return update_tushare_account_settings(
            api_token=payload.api_token,
            updated_by=account_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
