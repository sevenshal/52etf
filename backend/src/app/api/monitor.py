from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
import logging
from ...robot.portfolio_copy_trader import PortfolioCopyTrader

router = APIRouter(prefix="/api/monitor", tags=["Android Monitor"])
logger = logging.getLogger(__name__)

class NotificationSchema(BaseModel):
    package: str
    title: Optional[str] = None
    text: Optional[str] = None
    post_time: int
    
    class Config:
        from_attributes = True

@router.post("/notification")
async def receive_notification(
    notification: NotificationSchema,
    background_tasks: BackgroundTasks
):
    try:
        pkg = notification.package
        title = notification.title or ""
        text = notification.text or ""
        
        logger.info(f"Received notification from {pkg}: [{title}] {text}")
        
        # Check if it is Futu Niuniu
        target_packages = ["cn.futu.trader", "com.futunn.Touch", "com.moomoo.trade"]
        
        if pkg in target_packages:
            logger.info("Notification is from Futu. Checking for portfolio triggers...")
            
            # Combine title and text for searching
            content = f"{title} {text}"
            
            trader = PortfolioCopyTrader()
            
            # Since we don't know the exact portfolio name, we rely on the trader to find
            # portfolios whose names appear in the content.
            # However, `trigger_rebalance_by_name_keyword` takes a keyword and finds portfolios matching that keyword.
            # Here we need the reverse: find portfolios whose names are IN the content.
            # The current implementation of `trigger_rebalance_by_name_keyword` does: `portfolio_name LIKE %keyword%`
            # If I pass the whole content as keyword, it won't match (unless content is a substring of portfolio name).
            # We need to iterate active portfolios and check if `portfolio_name` is in `content`.
            
            # Let's adjust the logic. 
            # We can invoke a method that does the reverse check, or just implement it here?
            # Better to encapsualte in trader.
            
            # Accessing DB here might be cleaner than adding complex logic to trader if we consider `trigger_rebalance_by_name_keyword` strictly for search.
            # But `PortfolioCopyTrader` owns the rebalancing logic.
            
            # Let's update `trigger_rebalance_by_name_keyword` or add `trigger_rebalance_if_match`
            # For now, I will use a custom logic here to extract potential keywords or just call a new method on trader.
            
            # Actually, the user requirement is: "If notification title or text contains ... portfolio name"
            # So: `portfolio_name` in `content`
            
            triggered = await trader.trigger_rebalance_if_name_in_content(content)
            
            if triggered:
                return {"status": "triggered", "accounts": triggered}
            else:
                return {"status": "ignored", "reason": "no_matching_portfolio"}
                
    except Exception as e:
        logger.error(f"Error processing notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
    return {"status": "received"}
