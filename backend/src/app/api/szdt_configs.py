from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ...core.database import get_db, SZDTTradingConfig
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingSubAccount,
    get_external_trading_db_ctx,
)
from ...core.services.external_trading_ledger import STRATEGY_SZDT_A_STOCK
from ...core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_A_STOCK,
    normalize_external_trading_market_type,
)
from .account import valid_admin_account

router = APIRouter(
    prefix="/api/szdt-configs",
    tags=["SZDT Configs"]
)

class SZDTConfigBase(BaseModel):
    enabled: bool = False
    enabled_a: bool = False
    ib_account_id: Optional[int] = None
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None

class SZDTConfigCreate(SZDTConfigBase):
    pass

class SZDTConfigResponse(SZDTConfigBase):
    id: int
    account_id: str

    class Config:
        from_attributes = True


def _sync_a_stock_external_binding(
    config: SZDTTradingConfig,
    *,
    previous_sub_account_id: Optional[int],
) -> None:
    """Validate and reserve the dedicated external virtual sub-account for SZDT A shares."""
    if config.enabled_a and (not config.external_trading_account_id or not config.live_sub_account_id):
        raise HTTPException(status_code=400, detail="启用A股自动交易前请配置外部交易账户和虚拟子账户")
    if not config.external_trading_account_id and not config.live_sub_account_id:
        if previous_sub_account_id:
            with get_external_trading_db_ctx() as trading_db:
                previous = trading_db.query(ExternalTradingSubAccount).filter(
                    ExternalTradingSubAccount.id == previous_sub_account_id,
                    ExternalTradingSubAccount.account_id == config.account_id,
                    ExternalTradingSubAccount.strategy_type == STRATEGY_SZDT_A_STOCK,
                    ExternalTradingSubAccount.strategy_config_id == config.id,
                ).first()
                if previous:
                    previous.strategy_type = None
                    previous.strategy_config_id = None
        return
    if not config.external_trading_account_id or not config.live_sub_account_id:
        raise HTTPException(status_code=400, detail="外部交易账户和虚拟子账户必须同时选择")

    with get_external_trading_db_ctx() as trading_db:
        account = trading_db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.id == config.external_trading_account_id,
            ExternalTradingAccount.account_id == config.account_id,
        ).first()
        if not account or not account.enabled:
            raise HTTPException(status_code=400, detail="所选外部交易账户不存在或未启用")
        if normalize_external_trading_market_type(account.market_type) != EXTERNAL_TRADING_MARKET_A_STOCK:
            raise HTTPException(status_code=400, detail="守猪逮兔A股策略只能绑定A股外部交易账户")
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
            ExternalTradingSubAccount.external_trading_account_id == config.external_trading_account_id,
        ).first()
        if not sub_account or not sub_account.enabled:
            raise HTTPException(status_code=400, detail="所选虚拟子账户不存在或未启用")
        if (
            (sub_account.strategy_type or sub_account.strategy_config_id)
            and not (
                sub_account.strategy_type == STRATEGY_SZDT_A_STOCK
                and sub_account.strategy_config_id == config.id
            )
        ):
            raise HTTPException(status_code=400, detail="所选虚拟子账户已被其他策略绑定")

        if previous_sub_account_id and previous_sub_account_id != sub_account.id:
            previous = trading_db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == previous_sub_account_id,
                ExternalTradingSubAccount.account_id == config.account_id,
                ExternalTradingSubAccount.strategy_type == STRATEGY_SZDT_A_STOCK,
                ExternalTradingSubAccount.strategy_config_id == config.id,
            ).first()
            if previous:
                previous.strategy_type = None
                previous.strategy_config_id = None

        sub_account.strategy_type = STRATEGY_SZDT_A_STOCK
        sub_account.strategy_config_id = config.id

@router.get("/", response_model=SZDTConfigResponse)
def get_config(
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_admin_account)
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
    account_id: str = Depends(valid_admin_account)
):
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    if not config:
        config = SZDTTradingConfig(account_id=account_id)
        db.add(config)
    
    previous_sub_account_id = config.live_sub_account_id
    config.enabled = config_in.enabled
    config.enabled_a = config_in.enabled_a
    config.ib_account_id = config_in.ib_account_id
    config.external_trading_account_id = config_in.external_trading_account_id
    config.live_sub_account_id = config_in.live_sub_account_id
    
    try:
        db.flush()
        _sync_a_stock_external_binding(config, previous_sub_account_id=previous_sub_account_id)
        db.commit()
        db.refresh(config)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save config: {str(e)}")
        
    return config
