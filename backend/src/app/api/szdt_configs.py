from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ...core.database import get_db, SZDTTradingConfig, IBKRAccountConfig
from .account import valid_account

router = APIRouter(
    prefix="/api/szdt-configs",
    tags=["SZDT Configs"]
)

class SZDTConfigBase(BaseModel):
    enabled: bool = False
    enabled_a: bool = False
    ib_account_id: Optional[int] = None

class SZDTConfigCreate(SZDTConfigBase):
    pass

class SZDTConfigResponse(SZDTConfigBase):
    id: int
    account_id: str

    class Config:
        from_attributes = True

@router.get("/", response_model=SZDTConfigResponse)
def get_config(
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    if not config:
        # Create default config for user if not exists
        config = SZDTTradingConfig(account_id=account_id)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config

@router.post("/", response_model=SZDTConfigResponse)
def update_config(
    config_in: SZDTConfigCreate,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    if not config:
        config = SZDTTradingConfig(account_id=account_id)
        db.add(config)
    
    config.enabled = config_in.enabled
    config.enabled_a = config_in.enabled_a
    config.ib_account_id = config_in.ib_account_id
    
    try:
        db.commit()
        db.refresh(config)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save config: {str(e)}")
        
    return config
