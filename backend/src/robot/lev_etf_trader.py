import asyncio
import threading
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from ..core.database import get_db, Session, AutomatedTradingConfig
from ..core.services.trading_strategy import is_market_closing_soon, execute_trading_strategy

logger = logging.getLogger(__name__)

class LevETFTrader:
    def __init__(self):
        self.is_running = False

    async def scheduler_loop(self):
        """后台调度循环"""
        logger.info("Starting Leveraged ETF Trader Scheduler Loop")
        while True:
            try:
                if is_market_closing_soon():
                    logger.info("Market is closing soon, checking Lev ETF strategies...")
                    
                    # 获取所有开启了自动化交易的账户
                    db = get_db()
                    try:
                        configs = db.query(AutomatedTradingConfig).filter(
                            AutomatedTradingConfig.enabled == True
                        ).all()
                        
                        for config in configs:
                            # 为每个账户执行策略
                            asyncio.create_task(execute_trading_strategy(config.account_id, client_id=2))
                    finally:
                        db.close()
                    
                    # 执行完后休息 60 秒，避免在 10s 窗口内重复触发
                    await asyncio.sleep(60)
                else:
                    # 每 5 秒检查一次时间
                    await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error in LevETFTrader loop: {e}")
                await asyncio.sleep(10)

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
