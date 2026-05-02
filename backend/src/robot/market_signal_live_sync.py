import asyncio
import logging
import threading
import traceback

from ..core.services.market import MarketService
from ..core.utils import send_alert_email

logger = logging.getLogger(__name__)

_start_lock = threading.Lock()
_started = False


class MarketSignalLiveSync:
    def __init__(self):
        self.is_running = False

    async def scheduler_loop(self):
        self.is_running = True
        logger.info("Starting Market Signal module auto sync loop")
        logger.info("Current US/Eastern time: %s", MarketService.get_eastern_now())

        while True:
            try:
                now_et = MarketService.get_eastern_now()
                if now_et.weekday() >= 5 or MarketService.is_us_market_holiday(now_et.date()):
                    await asyncio.sleep(3600)
                    continue

                result = self._sync_due_configs(now_et)
                synced_count = len(result.get("synced") or [])
                error_count = len(result.get("errors") or [])
                if synced_count or error_count:
                    logger.info(
                        "Market signal module auto sync checked at %s US/Eastern: success=%s, errors=%s",
                        result.get("current_time"),
                        synced_count,
                        error_count,
                    )

                await asyncio.sleep(20)
            except Exception as exc:
                logger.error("Error in MarketSignalLiveSync loop: %s", exc, exc_info=True)
                send_alert_email(
                    "市场信号模块自动同步异常",
                    f"Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
                )
                await asyncio.sleep(60)

    def _sync_due_configs(self, now_et):
        from ..app.api.market_signal import sync_due_market_signal_configs_for_auto_sync

        return sync_due_market_signal_configs_for_auto_sync(now_et=now_et)


def start_market_signal_live_sync():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    worker = MarketSignalLiveSync()

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(worker.scheduler_loop())

    thread = threading.Thread(target=run, daemon=True, name="market-signal-live-sync")
    thread.start()
    logger.info("Market Signal module auto sync thread started")
