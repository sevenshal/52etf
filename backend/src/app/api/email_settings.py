from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .account import valid_account
from ...core.services.email_settings import get_email_settings, update_email_settings


router = APIRouter(prefix="/api/email-settings", tags=["email-settings"])


class EmailScenarioSetting(BaseModel):
    key: str
    name: str
    category: str
    description: str
    recipient_email: Optional[str] = None
    effective_email: Optional[str] = None
    uses_default: bool = False
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class EmailSettingsResponse(BaseModel):
    default_email: Optional[str] = None
    scenarios: List[EmailScenarioSetting]


class EmailSettingsUpdateRequest(BaseModel):
    default_email: Optional[str] = None
    scenarios: Dict[str, Optional[str]] = Field(default_factory=dict)


@router.get("", response_model=EmailSettingsResponse)
def read_email_settings(_account_id: str = Depends(valid_account)):
    return get_email_settings()


@router.put("", response_model=EmailSettingsResponse)
def save_email_settings(
    payload: EmailSettingsUpdateRequest,
    account_id: str = Depends(valid_account),
):
    try:
        return update_email_settings(
            default_email=payload.default_email,
            scenario_emails=payload.scenarios,
            updated_by=account_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
