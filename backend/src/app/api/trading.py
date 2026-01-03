from fastapi import APIRouter, HTTPException, Depends, Query
import threading
import asyncio
import logging
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from ...core.database import get_db, AutomatedTradingConfig, AutomatedTradeLog
from .account import valid_account
from ...core.services.trading_strategy import execute_trading_strategy

router = APIRouter(prefix="/api/trading", tags=["Automated Trading"])

class TradingConfigSchema(BaseModel):
    enabled: bool
    etf_code: str
    short_window: int
    long_window: int
    ib_port: int
    target_ratio: float = 10.0

class TradeLogSchema(BaseModel):
    id: int
    timestamp: datetime
    symbol: str
    action: str
    price: Optional[float]
    quantity: Optional[float]
    status: str
    message: Optional[str]

    class Config:
        from_attributes = True

@router.get("/config", response_model=Optional[TradingConfigSchema])
async def get_trading_config(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    config = db.query(AutomatedTradingConfig).filter(
        AutomatedTradingConfig.account_id == account_id
    ).first()
    return config

@router.post("/config")
async def save_trading_config(
    config_data: TradingConfigSchema,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    if config_data.short_window >= config_data.long_window:
        raise HTTPException(status_code=400, detail="Short window must be less than long window")
    
    config = db.query(AutomatedTradingConfig).filter(
        AutomatedTradingConfig.account_id == account_id
    ).first()
    
    if config:
        config.enabled = config_data.enabled
        config.etf_code = config_data.etf_code
        config.short_window = config_data.short_window
        config.long_window = config_data.long_window
        config.ib_port = config_data.ib_port
        config.target_ratio = config_data.target_ratio
        config.updated_at = datetime.now()
    else:
        config = AutomatedTradingConfig(
            account_id=account_id,
            **config_data.dict()
        )
        db.add(config)
    
    db.commit()
    return {"message": "Configuration saved successfully"}

@router.get("/logs", response_model=List[TradeLogSchema])
async def get_trading_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    logs = db.query(AutomatedTradeLog).filter(
        AutomatedTradeLog.account_id == account_id
    ).order_by(AutomatedTradeLog.timestamp.desc())\
     .offset((page - 1) * page_size)\
     .limit(page_size)\
     .all()
    return logs

@router.post("/manual-check")
async def manual_strategy_check(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    # 手动触发策略检查，运行在独立线程和Loop中以避免冲突
    def task_runner(acc_id):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(execute_trading_strategy(acc_id, client_id=10))
        except Exception as e:
            logging.getLogger(__name__).error(f"Manual strategy execution failed: {e}")
        finally:
            loop.close()
    try:
        t = threading.Thread(target=task_runner, args=(account_id,), daemon=True)
        t.start()
        return {"message": "Strategy check triggered in background thread"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
