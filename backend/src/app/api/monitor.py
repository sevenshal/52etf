from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
import logging
from ...robot.portfolio_copy_trader import PortfolioCopyTrader
from ...core.database import Session, SnowballCopyConfig

router = APIRouter(prefix="/api/monitor", tags=["Android Monitor"])
logger = logging.getLogger(__name__)

# --- Schemas ---
class NotificationSchema(BaseModel):
    package: str
    title: Optional[str] = None
    text: Optional[str] = None
    post_time: int
    
    class Config:
        from_attributes = True

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
                             logger.info(
                                 "Matched Snowball portfolio: %s (ID: %s)",
                                 config.combination_name,
                                 config.combination_id,
                             )
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
