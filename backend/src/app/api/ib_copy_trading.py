from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

from ...core.database import get_db, PortfolioCopyConfig, PortfolioCopyLog
from .account import valid_account

router = APIRouter(prefix="/api/ib-copy-trading", tags=["ib-copy-trading"])

class PortfolioCopyConfigSchema(BaseModel):
    id: Optional[int] = None
    account_id: Optional[str] = None
    enabled: bool = False
    portfolio_id: str
    portfolio_name: Optional[str] = None
    cron_rule: str = "0 8 * * *"
    timezone: str = "America/New_York"
    ib_port: Optional[int] = None # Now optional
    ib_account_id: Optional[int] = None # Added
    total_position_ratio: Optional[float] = 100.0
    total_amount: Optional[float] = None
    tracking_error_pct: Optional[float] = 5.0
    api_headers: Optional[dict] = None
    account_type: Optional[str] = "ib" 
    longport_account_id: Optional[str] = None
    platform: Optional[str] = "futu"

    class Config:
        from_attributes = True

class PortfolioCopyLogSchema(BaseModel):
    id: int
    config_id: Optional[int] = None
    account_id: str
    timestamp: datetime
    portfolio_id: Optional[str] = None
    action: str
    symbol: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    status: str
    message: Optional[str] = None

    class Config:
        from_attributes = True

@router.get("/configs", response_model=List[PortfolioCopyConfigSchema])
async def list_configs(
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    return db.query(PortfolioCopyConfig).filter(PortfolioCopyConfig.account_id == account_id).all()

@router.post("/configs", response_model=PortfolioCopyConfigSchema)
async def save_config(
    config_data: PortfolioCopyConfigSchema,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    if config_data.id:
        config = db.query(PortfolioCopyConfig).filter(
            PortfolioCopyConfig.id == config_data.id,
            PortfolioCopyConfig.account_id == account_id
        ).first()
        if not config:
            raise HTTPException(status_code=404, detail="Config not found")
        
        config.enabled = config_data.enabled
        config.portfolio_id = config_data.portfolio_id
        config.portfolio_name = config_data.portfolio_name
        config.cron_rule = config_data.cron_rule
        config.timezone = config_data.timezone
        config.ib_port = config_data.ib_port or 0
        config.ib_account_id = config_data.ib_account_id
        config.total_position_ratio = config_data.total_position_ratio
        config.total_amount = config_data.total_amount
        config.tracking_error_pct = config_data.tracking_error_pct
        config.api_headers = config_data.api_headers
        config.account_type = config_data.account_type
        config.longport_account_id = config_data.longport_account_id
        config.platform = config_data.platform
    else:
        config = PortfolioCopyConfig(
            account_id=account_id,
            enabled=config_data.enabled,
            portfolio_id=config_data.portfolio_id,
            portfolio_name=config_data.portfolio_name,
            cron_rule=config_data.cron_rule,
            timezone=config_data.timezone,
            ib_port=config_data.ib_port or 0,
            ib_account_id=config_data.ib_account_id,
            total_position_ratio=config_data.total_position_ratio,
            total_amount=config_data.total_amount,
            tracking_error_pct=config_data.tracking_error_pct,
            api_headers=config_data.api_headers,
            account_type=config_data.account_type,
            longport_account_id=config_data.longport_account_id,
            platform=config_data.platform
        )
        db.add(config)
    
    db.commit()
    db.refresh(config)
    return config

@router.delete("/configs/{config_id}")
async def delete_config(
    config_id: int,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(PortfolioCopyConfig).filter(
        PortfolioCopyConfig.id == config_id,
        PortfolioCopyConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    
    db.delete(config)
    db.commit()
    return {"message": "Deleted successfully"}

@router.get("/logs", response_model=List[PortfolioCopyLogSchema])
async def list_logs(
    portfolio_id: Optional[str] = None,
    config_id: Optional[int] = None,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    query = db.query(PortfolioCopyLog)
    # Always filter by authenticated account_id
    query = query.filter(PortfolioCopyLog.account_id == account_id)
    if config_id:
        query = query.filter(PortfolioCopyLog.config_id == config_id)
    return query.order_by(PortfolioCopyLog.timestamp.desc()).limit(100).all()

@router.get("/portfolio-info/{portfolio_id}")
async def get_portfolio_info_proxy(portfolio_id: str, platform: str = "futu"):
    from ...robot.portfolio_copy_trader import PortfolioCopyTrader
    trader = PortfolioCopyTrader()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
        }
        info = await trader.get_portfolio_info(portfolio_id, headers, platform=platform)
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/configs/{config_id}/preview")
async def preview_rebalance(
    config_id: int,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    from ...robot.portfolio_copy_trader import PortfolioCopyTrader
    config = db.query(PortfolioCopyConfig).filter(
        PortfolioCopyConfig.id == config_id,
        PortfolioCopyConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    
    trader = PortfolioCopyTrader()
    try:
        # 使用独特的 client_id (100 + config_id) 避免与后台机器人冲突
        # Offload to worker thread via task queue
        plan = await trader.submit_rebalance_task(config, client_id=100 + config_id)
        return plan
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
