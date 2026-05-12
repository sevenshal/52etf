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
            
            try:
                from ...core.services.market import MarketService
                from ...core.utils import send_alert_email
                is_cn_open = MarketService.is_china_market_open()
                
                if is_cn_open:
                    markets = []
                    if is_cn_open: markets.append("A股")
                    open_markets = "、".join(markets)
                    
                    send_alert_email(
                        f"WebSocket 长连接断开告警: {cli_id}",
                        f"Android/PTrade 客户端 {cli_id} 的 WebSocket 长连接已断开！\n\n当前处于 {open_markets} 开盘时间段，自动化跟单可能受影响，请及时检查客户端或手机 App 是否正常运行。"
                    )
            except Exception as e:
                logger.error(f"Failed to send disconnect email: {e}")

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
            triggered = await trader.trigger_rebalance_if_name_in_content(content, platform="futu")
            if triggered:
                logger.info(f"Triggered rebalance for accounts: {triggered}")

        # 1.5 Handle Star Wealth Notifications
        if pkg == "com.fosunhani.stock":
             logger.info("Checking StarWealth portfolio triggers...")
             trader = PortfolioCopyTrader()
             triggered = await trader.trigger_rebalance_if_name_in_content(content, platform="star_wealth")
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
                 matched_external_config_ids = []
                 
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
                             if (
                                 getattr(config, "live_trade_enabled", False)
                                 and getattr(config, "external_trading_account_id", None)
                                 and getattr(config, "live_sub_account_id", None)
                             ):
                                 matched_external_config_ids.append(config.id)

                 if matched_external_config_ids:
                     from .snowball import sync_snowball_external_trading_config_ids

                     background_tasks.add_task(
                         sync_snowball_external_trading_config_ids,
                         matched_external_config_ids,
                         trigger_source="notification",
                         trigger_executor=True,
                     )
                                 
             except Exception as e:
                 logger.error(f"Error processing Snowball trigger: {e}")
             finally:
                 db.close()

    except Exception as e:
        logger.error(f"Error processing notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
    return {"status": "received"}
