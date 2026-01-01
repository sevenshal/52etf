from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime

from ...core.database import get_db, IBKRAccountConfig
from ...core.services.ib_account_service import IBAccountService
from pydantic import BaseModel

router = APIRouter(prefix="/api/ib-accounts", tags=["ib-accounts"])

class IBKRAccountSchema(BaseModel):
    id: int = None
    name: str
    ib_host: str = '127.0.0.1'
    ib_port: int
    client_id: int = 1
    tws_userid: str = ""
    tws_password: str = ""
    trading_mode: str = "paper"
    container_name: str = ""
    twofa_timeout_action: str = "restart"
    auto_restart_time: str = "08:59 PM"
    relogin_after_twofa_timeout: str = "yes"

    class Config:
        from_attributes = True

@router.get("", response_model=List[IBKRAccountSchema])
async def list_ib_accounts(db: Session = Depends(get_db)):
    return db.query(IBKRAccountConfig).all()

@router.post("", response_model=IBKRAccountSchema)
async def save_ib_account(config_data: IBKRAccountSchema, db: Session = Depends(get_db)):
    if config_data.id:
        config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == config_data.id).first()
        if not config:
            raise HTTPException(status_code=404, detail="Account not found")
        
        config.name = config_data.name
        config.ib_host = config_data.ib_host
        config.ib_port = config_data.ib_port
        config.client_id = config_data.client_id
        config.tws_userid = config_data.tws_userid
        config.tws_password = config_data.tws_password
        config.trading_mode = config_data.trading_mode
        config.container_name = config_data.container_name
        config.twofa_timeout_action = config_data.twofa_timeout_action
        config.auto_restart_time = config_data.auto_restart_time
        config.relogin_after_twofa_timeout = config_data.relogin_after_twofa_timeout
        config.updated_at = datetime.now()
    else:
        # 检查端口唯一性
        existing = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.ib_port == config_data.ib_port).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Port {config_data.ib_port} is already used by {existing.name}")
            
        config = IBKRAccountConfig(
            account_id="system", # 这里可以根据实际需求绑定用户
            name=config_data.name,
            ib_host=config_data.ib_host,
            ib_port=config_data.ib_port,
            client_id=config_data.client_id,
            tws_userid=config_data.tws_userid,
            tws_password=config_data.tws_password,
            trading_mode=config_data.trading_mode,
            container_name=config_data.container_name,
            twofa_timeout_action=config_data.twofa_timeout_action,
            auto_restart_time=config_data.auto_restart_time,
            relogin_after_twofa_timeout=config_data.relogin_after_twofa_timeout
        )
        db.add(config)
    
    db.commit()
    db.refresh(config)
    return config

@router.delete("/{account_id}")
async def delete_ib_account(account_id: int, db: Session = Depends(get_db)):
    config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == account_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    db.delete(config)
    db.commit()
    return {"message": "Deleted successfully"}

@router.get("/{account_id}/status")
async def get_ib_account_status(account_id: int, db: Session = Depends(get_db)):
    config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == account_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    status = await IBAccountService.get_account_status(
        config.ib_host, 
        config.ib_port, 
        config.client_id
    )
    return status

@router.post("/{account_id}/restart")
async def restart_ib_gateway(account_id: int, db: Session = Depends(get_db)):
    config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == account_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if not config.container_name:
        raise HTTPException(status_code=400, detail="Container name not configured")
        
    result = IBAccountService.restart_gateway(config.container_name)
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["message"])
        
    return result

@router.post("/{account_id}/deploy")
async def deploy_ib_gateway(account_id: int, db: Session = Depends(get_db)):
    config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == account_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    result = IBAccountService.deploy_gateway(config)
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["message"])
        
    return result
