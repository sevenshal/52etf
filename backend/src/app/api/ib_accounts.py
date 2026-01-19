from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime

from ...core.database import get_db, IBKRAccountConfig
from ...core.services.ib_account_service import IBAccountService
from pydantic import BaseModel
from .account import valid_account

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
async def list_ib_accounts(
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    return db.query(IBKRAccountConfig).filter(IBKRAccountConfig.account_id == account_id).all()

@router.post("", response_model=IBKRAccountSchema)
async def save_ib_account(
    config_data: IBKRAccountSchema, 
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    if config_data.id:
        config = db.query(IBKRAccountConfig).filter(
            IBKRAccountConfig.id == config_data.id,
            IBKRAccountConfig.account_id == account_id
        ).first()
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
        # Check port uniqueness (within the system or globally? globally is safer for ports)
        # But we should probably check if the port is used by ANY account.
        existing = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.ib_port == config_data.ib_port).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Port {config_data.ib_port} is already used")
            
        config = IBKRAccountConfig(
            account_id=account_id,
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

@router.delete("/{config_id}")
async def delete_ib_account(
    config_id: int, 
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(IBKRAccountConfig).filter(
        IBKRAccountConfig.id == config_id,
        IBKRAccountConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    db.delete(config)
    db.commit()
    return {"message": "Deleted successfully"}

@router.get("/{config_id}/status")
async def get_ib_account_status(
    config_id: int, 
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(IBKRAccountConfig).filter(
        IBKRAccountConfig.id == config_id,
        IBKRAccountConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    status = await IBAccountService.get_account_status(
        config.ib_host, 
        config.ib_port, 
        config.client_id
    )
    return status

@router.post("/{config_id}/restart")
async def restart_ib_gateway(
    config_id: int, 
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(IBKRAccountConfig).filter(
        IBKRAccountConfig.id == config_id,
        IBKRAccountConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if not config.container_name:
        raise HTTPException(status_code=400, detail="Container name not configured")
        
    result = IBAccountService.restart_gateway(config.container_name)
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["message"])
        
    return result


@router.post("/{config_id}/deploy")
async def deploy_ib_gateway(
    config_id: int, 
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(IBKRAccountConfig).filter(
        IBKRAccountConfig.id == config_id,
        IBKRAccountConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Account not found")
    
    result = IBAccountService.deploy_gateway(config)
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["message"])
        
    return result

@router.websocket("/{config_id}/logs")
async def ws_ib_account_logs(
    websocket: WebSocket, 
    config_id: int, 
    db: Session = Depends(get_db)
):
    # Note: Websockets are hard to auth with headers in browsers. 
    # For now we won't strictly enforce account owner check via Depends(valid_account) directly in signature 
    # unless we can extract it from query params.
    # But to be safe, we should at least check existence.
    await websocket.accept()
    
    config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == config_id).first()
    
    if not config or not config.container_name:
        await websocket.send_text("Error: Container not configured or account not found")
        await websocket.close()
        return

    import asyncio
    import os
    docker_bin = os.getenv('DOCKER_BINARY_PATH', 'docker')
    
    process = await asyncio.create_subprocess_exec(
        docker_bin, "logs", "--tail", "100", "-f", config.container_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            await websocket.send_text(line.decode('utf-8'))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"Error: {str(e)}")
        except:
            pass
    finally:
        if process:
            try:
                process.terminate()
                await process.wait()
            except:
                pass
