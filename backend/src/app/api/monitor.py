from fastapi import APIRouter, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Optional, List, Dict
import logging
import asyncio
from ...robot.portfolio_copy_trader import PortfolioCopyTrader
from ...core.database import Session, SnowballCopyConfig

router = APIRouter(prefix="/api/monitor", tags=["Android Monitor"])
logger = logging.getLogger(__name__)

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self):
        # cli_id -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, cli_id: str):
        await websocket.accept()
        self.active_connections[cli_id] = websocket
        logger.info(f"PTrade Client {cli_id} connected via WebSocket")

    def disconnect(self, cli_id: str):
        if cli_id in self.active_connections:
            del self.active_connections[cli_id]
            logger.info(f"PTrade Client {cli_id} disconnected")

    async def send_message(self, cli_id: str, message: str):
        if cli_id in self.active_connections:
            try:
                await self.active_connections[cli_id].send_text(message)
                logger.info(f"Sent message to {cli_id}: {message}")
            except Exception as e:
                logger.error(f"Failed to send to {cli_id}: {e}")
                self.disconnect(cli_id)
        else:
            logger.warning(f"Client {cli_id} not connected, cannot send message")

manager = ConnectionManager()

# --- Schemas ---
class NotificationSchema(BaseModel):
    package: str
    title: Optional[str] = None
    text: Optional[str] = None
    post_time: int
    
    class Config:
        from_attributes = True

# --- WebSocket Endpoint ---
@router.websocket("/ws/{cli_id}")
async def websocket_endpoint(websocket: WebSocket, cli_id: str):
    await manager.connect(websocket, cli_id)
    try:
        while True:
            # Keep connection open, ignore incoming text for now
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(cli_id)
    except Exception as e:
        logger.error(f"WebSocket error for {cli_id}: {e}")
        manager.disconnect(cli_id)

# --- Notification Handler ---
@router.post("/notification")
async def receive_notification(
    notification: NotificationSchema,
    background_tasks: BackgroundTasks
):
    try:
        pkg = notification.package
        title = notification.title or ""
        text = notification.text or ""
        content = f"{title} {text}"
        
        logger.info(f"Received notification from {pkg}: [{title}] {text}")
        
        # 1. Handle Futu Notifications (Existing Logic)
        futu_packages = ["cn.futu.trader", "com.futunn.Touch", "com.moomoo.trade"]
        if pkg in futu_packages:
            logger.info("Checking Futu portfolio triggers...")
            trader = PortfolioCopyTrader()
            triggered = await trader.trigger_rebalance_if_name_in_content(content)
            if triggered:
                logger.info(f"Triggered rebalance for accounts: {triggered}")

        # 2. Handle Snowball Notifications (New Logic)
        snowball_packages = ["com.xueqiu.android"]
        if pkg in snowball_packages:
             logger.info("Checking Snowball portfolio triggers...")
             db = Session()
             try:
                 # Fetch all active Snowball configs
                 configs = db.query(SnowballCopyConfig).filter(SnowballCopyConfig.enabled == True).all()
                 
                 for config in configs:
                     # Check if combination_name exists and is in content
                     if config.combination_name and config.combination_name.strip():
                         # Fuzzy match: Snowball notifications usually contain the Name
                         if config.combination_name in content:
                             logger.info(f"Matched Snowball portfolio: {config.combination_name} (ID: {config.combination_id}) for Client: {config.cli_id}")
                             
                             # Identify Client ID and Push via WebSocket
                             if config.cli_id:
                                 # We send a specific command format
                                 cmd = f"TRIGGER:{config.combination_id}"
                                 await manager.send_message(config.cli_id, cmd)
                                 
             except Exception as e:
                 logger.error(f"Error processing Snowball trigger: {e}")
             finally:
                 db.close()

    except Exception as e:
        logger.error(f"Error processing notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
    return {"status": "received"}
