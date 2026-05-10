import asyncio
import logging
import threading
import traceback

from ..core.services.market import MarketService
from ..core.utils import send_alert_email

logger = logging.getLogger(__name__)

_start_lock = threading.Lock()
_started = False


class USStockSignalLiveSync:
    def __init__(self):
        self.is_running = False

    async def scheduler_loop(self):
        self.is_running = True
        logger.info("Starting US stock signal virtual auto sync loop")
        logger.info("Current US/Eastern time: %s", MarketService.get_eastern_now())

        while True:
            try:
                now_et = MarketService.get_eastern_now()
                if now_et.weekday() >= 5 or MarketService.is_us_market_holiday(now_et.date()):
                    await asyncio.sleep(3600)
                    continue

                sync_result = self._sync_due_configs(now_et)
                trade_result = self._trade_due_configs(now_et)
                synced_count = len(sync_result.get("synced") or [])
                traded_count = len(trade_result.get("traded") or [])
                error_count = len(sync_result.get("errors") or []) + len(trade_result.get("errors") or [])
                if synced_count or traded_count or error_count:
                    logger.info(
                        "US stock signal virtual scheduler checked at %s US/Eastern: synced=%s, traded=%s, errors=%s",
                        sync_result.get("current_time"),
                        synced_count,
                        traded_count,
                        error_count,
                    )

                await asyncio.sleep(60)
            except Exception as exc:
                logger.error("Error in USStockSignalLiveSync loop: %s", exc, exc_info=True)
                send_alert_email(
                    "美股多因子策略虚拟盘自动同步异常",
                    f"Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
                )
                await asyncio.sleep(60)

    def _sync_due_configs(self, now_et):
        from ..app.api.us_stock_signal_live import sync_due_us_stock_signal_configs_for_auto_sync

        return sync_due_us_stock_signal_configs_for_auto_sync(now_et=now_et)

    def _trade_due_configs(self, now_et):
        from ..app.api.us_stock_signal_live import execute_due_us_stock_signal_configs_for_auto_trade

        return execute_due_us_stock_signal_configs_for_auto_trade(now_et=now_et)


def start_us_stock_signal_live_sync():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    worker = USStockSignalLiveSync()

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(worker.scheduler_loop())

    thread = threading.Thread(target=run, daemon=True, name="us-stock-signal-live-sync")
    thread.start()
    logger.info("US stock signal virtual auto sync thread started")
