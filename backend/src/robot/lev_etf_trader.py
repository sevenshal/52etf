import asyncio
import threading
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from ..core.database import AutomatedTradingConfig, get_db_ctx
from ..core.services.trading_strategy import is_market_closing_soon, execute_trading_strategy
from ..core.services.market import MarketService
from ..core.utils import send_alert_email
import traceback

logger = logging.getLogger(__name__)

class LevETFTrader:
    def __init__(self):
        self.is_running = False

    async def scheduler_loop(self):
        """后台调度循环"""
        logger.info("Starting Leveraged ETF Trader Scheduler Loop")
        logger.info(f"Current Eastern Time: {MarketService.get_eastern_now()}")

        while True:
            try:
                now = MarketService.get_eastern_now()
                
                # Check if today is a holiday or weekend
                if now.weekday() >= 5 or MarketService.is_us_market_holiday(now.date()):
                    # Sleep for an hour and check again
                    await asyncio.sleep(3600)
                    continue

                close_time = MarketService.get_us_market_close_time(now.date())
                target_close = datetime.combine(now.date(), close_time, tzinfo=ZoneInfo('US/Eastern'))
                
                # Calculate time until market close (in seconds)
                delta = (target_close - now).total_seconds()
                
                # Close window: 10 seconds before close
                if 0 < delta <= 10:
                    logger.info(f"Market is closing in {delta:.2f}s, Triggering Lev ETF Strategies...")
                    
                    # 获取所有开启了自动化交易的账户
                    with get_db_ctx() as db:
                        configs = db.query(AutomatedTradingConfig).filter(
                            AutomatedTradingConfig.enabled == True
                        ).all()
                        
                        count = 0
                        for config in configs:
                            # 为每个账户执行策略
                            asyncio.create_task(execute_trading_strategy(config.account_id, client_id=2))
                            count += 1
                        
                        logger.info(f"Triggered strategies for {count} configs.")
                    
                    # Wait long enough to pass the close time to avoid double trigger
                    await asyncio.sleep(120)

                elif 10 < delta <= 60:
                    # Less than 1 minute to close, check frequently
                    await asyncio.sleep(1)
                elif 60 < delta <= 300:
                    # Less than 5 minutes, check every 10s
                    await asyncio.sleep(10)
                elif delta > 300:
                    # More than 5 minutes, sleep longer (up to 1 hour, but check at least once an hour)
                    # Sleep until 5 mins before close
                    sleep_time = min(delta - 60, 3600)
                    await asyncio.sleep(sleep_time)
                else:
                    # Market already closed (delta <= 0)
                    # Check if it was closed just now
                    if delta > -60:
                         logger.info("Market closed recently.")
                    
                    # Sleep a bit before checking for "tomorrow" or simply loop
                    await asyncio.sleep(300)
                    
            except Exception as e:
                logger.error(f"Error in LevETFTrader loop: {e}", exc_info=True)
                send_alert_email("自动化跟单策略报错: LevETFTrader 主循环异常", f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}")
                await asyncio.sleep(60)

def start_lev_etf_trader():
    """启动调度器线程"""
    trader = LevETFTrader()
    def run():
        # 为新线程创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trader.scheduler_loop())
    
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info("Leveraged ETF Trader Thread started")
