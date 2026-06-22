import logging
import re
import threading
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from ..core.database import ScheduledTaskConfig, get_db_ctx
from ..core.event_stream import publish_event
from ..core.utils import send_alert_email

TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
CRON_RULE_SPLIT_PATTERN = re.compile(r"[;\n]+")
DEFAULT_TASK_TIMEZONE = "Asia/Shanghai"
SUPPORTED_TASK_TIMEZONES = {"Asia/Shanghai", "America/New_York"}
SERVER_TZ = ZoneInfo("Asia/Shanghai")
LAST_RUN_MESSAGE_MAX_LENGTH = 4000
TASK_ERROR_PREVIEW_LIMIT = 20
TASK_ERROR_PREVIEW_MAX_LENGTH = 3600
NO_STARTUP_CATCH_UP_TASK_KEYS = {"xueqiu_top_holdings_rebalance"}
DEPRECATED_TASK_KEYS = [
    "a_stock_income_sync",
    "etf_historical_holdings_backfill",
    "etf_nport_holdings_import",
    "external_trading_fee_reconcile_retry",
    "a_stock_fund_flow_sync",
    "snowball_ptrade_heartbeat_check",
]


def _truncate_task_message(message: Optional[str], max_length: int = LAST_RUN_MESSAGE_MAX_LENGTH) -> Optional[str]:
    if not message:
        return None
    text = str(message)
    if len(text) <= max_length:
        return text
    suffix = "...[已截断]"
    return f"{text[:max_length - len(suffix)]}{suffix}"


def _format_error_preview(
    errors: List[dict],
    formatter: Callable[[dict], str],
    limit: int = TASK_ERROR_PREVIEW_LIMIT,
    max_length: int = TASK_ERROR_PREVIEW_MAX_LENGTH,
) -> str:
    preview_items = [formatter(item) for item in errors[:limit]]
    preview = "; ".join(preview_items)
    remaining = len(errors) - len(preview_items)
    if remaining > 0:
        preview = f"{preview}; ... and {remaining} more"
    return _truncate_task_message(preview, max_length) or ""


def _run_evc_stock_fetch():
    from .evc_manager import EVCManager

    EVCManager().fetch_and_stocks()


def _run_us_stock_base_data_sync(start_date: Optional[str] = None):
    from .us_stock_base_data_sync import sync_us_stock_base_data

    parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    us_result = sync_us_stock_base_data(start_date=parsed_start_date)
    static_result = us_result.get("static_snapshot") or {}
    daily_errors = us_result.get("daily_errors") or []

    logging.getLogger("ScheduledTaskManager").info(
        (
            "US stock base data synced: status=%s static_symbols=%s static_fetched=%s "
            "daily_symbols=%s us_static_info_fetched=%s daily_saved_rows=%s "
            "daily_adjustment_refreshes=%s daily_errors=%s tables=%s"
        ),
        us_result.get("status"),
        static_result.get("symbols"),
        static_result.get("fetched"),
        us_result.get("daily_symbols"),
        us_result.get("static_info_fetched"),
        us_result.get("daily_saved_rows"),
        us_result.get("daily_adjustment_refresh_count"),
        len(daily_errors),
        us_result.get("tables"),
    )

    result_message = (
        "US stock base data sync "
        f"status={us_result.get('status')} "
        f"static_symbols={static_result.get('symbols')} "
        f"static_fetched={static_result.get('fetched')} "
        f"daily_symbols={us_result.get('daily_symbols')} "
        f"static_info_fetched={us_result.get('static_info_fetched')} "
        f"daily_fetched_symbols={us_result.get('daily_fetched_symbols')} "
        f"daily_saved_rows={us_result.get('daily_saved_rows')} "
        f"daily_adjustment_refreshes={us_result.get('daily_adjustment_refresh_count')} "
        f"daily_errors={len(daily_errors)} "
        f"tables={us_result.get('tables')}"
    )
    if daily_errors:
        preview = _format_error_preview(
            daily_errors,
            lambda item: f"{item.get('symbol')}: {item.get('error')}",
        )
        raise RuntimeError(
            f"{result_message} finished with {len(daily_errors)} daily errors: {preview}"
        )
    return result_message


def _run_us_stock_industry_sync():
    from .us_stock_industry_sync import sync_us_stock_industry_snapshots

    result = sync_us_stock_industry_snapshots()
    logging.getLogger("ScheduledTaskManager").info(
        (
            "US stock industry synced: symbols=%s target=%s saved=%s skipped=%s "
            "profile_skipped=%s remaining=%s api_calls=%s errors=%s"
        ),
        result.get("symbols"),
        result.get("target_symbols"),
        result.get("saved"),
        result.get("skipped_existing"),
        result.get("skipped_profile_unavailable"),
        result.get("remaining"),
        result.get("api_calls"),
        len(result.get("errors") or []),
    )
    errors = result.get("errors") or []
    if errors and not result.get("saved"):
        preview = _format_error_preview(
            errors,
            lambda item: f"{item.get('symbol')}: {item.get('error')}",
        )
        raise RuntimeError(f"US stock industry sync failed: {preview}")
    return (
        "US stock industry sync "
        f"symbols={result.get('symbols')} "
        f"target={result.get('target_symbols')} "
        f"saved={result.get('saved')} "
        f"skipped={result.get('skipped_existing')} "
        f"profile_skipped={result.get('skipped_profile_unavailable')} "
        f"remaining={result.get('remaining')} "
        f"api_calls={result.get('api_calls')} "
        f"errors={len(errors)}"
    )


def _run_etf_fair_value_analysis():
    from ..core.services.longport import LongPortService
    from .etf_manager import ETFManager

    manager = ETFManager(LongPortService.get_instance())
    try:
        manager.analyze_all_fair_value()
    finally:
        manager.db_session.close()


def _run_etf_holdings_ingest():
    from .etf_holdings_backfill import ETFHoldingsLatestIngest

    ingest = ETFHoldingsLatestIngest()
    try:
        result = ingest.sync_latest()
    finally:
        ingest.close()

    logging.getLogger("ScheduledTaskManager").info(
        "ETF latest holdings ingest saved=%s skipped=%s errors=%s dates=%s",
        result.get("saved"),
        result.get("skipped"),
        len(result.get("errors") or []),
        result.get("saved_dates"),
    )
    errors = result.get("errors") or []
    if errors:
        preview = _format_error_preview(
            errors,
            lambda item: f"{item.get('symbol')}: {item.get('error')}",
        )
        raise RuntimeError(
            f"ETF latest holdings ingest finished with {len(errors)} errors: {preview}"
        )


def _run_etf_historical_holdings_backfill(start_date: Optional[str] = None):
    from .etf_holdings_backfill import ETFHistoricalHoldingsBackfill

    parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    backfill = ETFHistoricalHoldingsBackfill()
    try:
        result = backfill.backfill(start_date=parsed_start_date)
    finally:
        backfill.close()

    logging.getLogger("ScheduledTaskManager").info(
        "ETF historical holdings backfill saved=%s skipped=%s range=%s~%s errors=%s",
        result.get("saved"),
        result.get("skipped"),
        result.get("start_date"),
        result.get("end_date"),
        len(result.get("errors") or []),
    )
    errors = result.get("errors") or []
    if errors:
        preview = _format_error_preview(
            errors,
            lambda item: f"{item.get('symbol')} {item.get('date')}: {item.get('error')}",
        )
        raise RuntimeError(
            "ETF historical holdings backfill "
            f"range={result.get('start_date')}~{result.get('end_date')} "
            f"saved={result.get('saved')} skipped={result.get('skipped')} "
            f"finished with {len(errors)} errors: {preview}"
        )
    return (
        "ETF historical holdings backfill "
        f"range={result.get('start_date')}~{result.get('end_date')} "
        f"saved={result.get('saved')} skipped={result.get('skipped')}"
    )


def _run_etf_holdings_sync(start_date: Optional[str] = None):
    if start_date:
        return _run_etf_historical_holdings_backfill(start_date=start_date)
    return _run_etf_holdings_ingest()


def _run_etf_put_call_ratio_sync(full: bool = False):
    from .etf_putcallratio_sync import BarchartETFPutCallRatioSync

    syncer = BarchartETFPutCallRatioSync()
    try:
        result = syncer.sync_all(full=full)
    finally:
        syncer.close()

    logging.getLogger("ScheduledTaskManager").info(
        "ETF option data sync mode=%s symbols=%s history_saved=%s expirations_saved=%s errors=%s",
        result.get("mode"),
        result.get("symbols"),
        result.get("saved_history"),
        result.get("saved_expirations"),
        len(result.get("errors") or []),
    )
    errors = result.get("errors") or []
    if errors:
        preview = _format_error_preview(
            errors,
            lambda item: f"{item.get('symbol')}: {item.get('error')}",
        )
        raise RuntimeError(
            f"ETF put/call ratio sync finished with {len(errors)} errors: {preview}"
        )


def _run_cnn_fear_greed_fetch():
    from .cnn_fear_index import CNNFearGreedIndexScraper

    scraper = CNNFearGreedIndexScraper()
    try:
        result = scraper.fetch_data_and_save_history()
    finally:
        scraper.db_session.close()

    history = result.get("history", {})
    return (
        "CNN Fear & Greed sync "
        f"mode={history.get('mode')} "
        f"symbol={history.get('symbol')} "
        f"fetch_start={history.get('fetch_start_date')} "
        f"range={history.get('start_date')}~{history.get('end_date')} "
        f"saved={history.get('saved')}"
    )


def _run_etf_fear_greed_backfill(start_date: Optional[str] = None):
    from ..core.services.etf_fear_greed_clone_service import (
        DEFAULT_ETF_FEAR_GREED_SYMBOLS,
        ETFFearGreedCloneCalculator,
    )

    end_date = date.today()
    if start_date:
        output_start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    else:
        # Daily runs only need to refresh the recent tail, but still need a
        # long calculation window for rolling z-score and 52-week components.
        output_start_date = end_date - timedelta(days=3)

    # The slowest component needs about 244 trading days to warm up
    # (125-day momentum raw value + 120-point rolling score). 390 calendar
    # days leaves a small buffer while still fitting the 2025+ holdings backfill.
    calculation_start_date = output_start_date - timedelta(days=390)
    calculator = ETFFearGreedCloneCalculator()
    logger = logging.getLogger("ScheduledTaskManager")
    for symbol in DEFAULT_ETF_FEAR_GREED_SYMBOLS:
        result = calculator.backfill_to_db(
            symbol=symbol,
            start_date=calculation_start_date,
            end_date=end_date,
            output_start_date=output_start_date,
            history_days=390,
            score_window=252,
            min_periods=120,
            max_holdings=40,
            use_historical_holdings=True,
        )
        logger.info(
            "%s fear greed backfill saved %s rows, range=%s~%s",
            symbol,
            result.get("saved"),
            result.get("start_date"),
            result.get("end_date"),
        )


def _run_a_stock_base_data_sync(start_date: Optional[str] = None):
    from .a_stock_base_data_sync import sync_a_stock_base_data

    parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    result = sync_a_stock_base_data(start_date=parsed_start_date, incremental=parsed_start_date is None)
    logging.getLogger("ScheduledTaskManager").info(
        (
            "A stock base data synced: status=%s mode=%s end_date=%s tables=%s "
            "index_daily_saved=%s index_daily_jobs=%s index_daily_errors=%s "
            "fund_daily_saved=%s fund_daily_jobs=%s fund_daily_date_batches=%s fund_daily_symbol_jobs=%s fund_daily_errors=%s "
            "option_basic_saved=%s option_daily_saved=%s option_refresh_dates=%s option_chunks=%s option_errors=%s "
            "repo_daily_saved=%s repo_refresh_dates=%s repo_chunks=%s repo_errors=%s "
            "chinabond_defs_saved=%s chinabond_daily_saved=%s chinabond_refresh_dates=%s chinabond_chunks=%s chinabond_errors=%s "
            "income_fetched_rows=%s income_saved_rows=%s income_fetch_seconds=%s "
            "income_insert_seconds=%s income_total_seconds=%s income_insert_batches=%s "
            "income_skipped_symbols=%s income_backfill_symbols=%s income_incremental_symbols=%s income_full_symbols=%s "
            "fund_flow_source=%s fund_flow_saved_rows=%s fund_flow_trade_dates=%s fund_flow_errors=%s"
        ),
        result.get("status"),
        result.get("mode"),
        result.get("end_date"),
        result.get("tables"),
        result.get("index_daily_saved_rows"),
        result.get("index_daily_jobs"),
        result.get("index_daily_errors"),
        result.get("fund_daily_saved_rows"),
        result.get("fund_daily_jobs"),
        result.get("fund_daily_date_batches"),
        result.get("fund_daily_symbol_jobs"),
        result.get("fund_daily_errors"),
        result.get("option_basic_rows_saved"),
        result.get("option_daily_saved_rows"),
        result.get("option_daily_refresh_dates"),
        result.get("option_daily_chunks"),
        result.get("option_daily_errors"),
        result.get("repo_daily_saved_rows"),
        result.get("repo_daily_refresh_dates"),
        result.get("repo_daily_chunks"),
        result.get("repo_daily_errors"),
        result.get("chinabond_curve_defs_saved"),
        result.get("chinabond_curve_daily_saved_rows"),
        result.get("chinabond_curve_refresh_dates"),
        result.get("chinabond_curve_chunks"),
        result.get("chinabond_curve_errors"),
        result.get("income_fetched_rows"),
        result.get("income_saved_rows"),
        result.get("income_fetch_seconds"),
        result.get("income_insert_seconds"),
        result.get("income_total_seconds"),
        result.get("income_insert_batches"),
        result.get("income_skipped_symbols"),
        result.get("income_backfill_symbols"),
        result.get("income_incremental_symbols"),
        result.get("income_full_symbols"),
        result.get("fund_flow_source"),
        result.get("fund_flow_saved_rows"),
        result.get("fund_flow_trade_dates"),
        result.get("fund_flow_errors"),
    )
    return (
        "A stock base data sync "
        f"start={result.get('start_date')} "
        f"market_start={result.get('market_start_date')} "
        f"index_start={result.get('index_start_date')} "
        f"index_daily_saved={result.get('index_daily_saved_rows')} "
        f"index_daily_jobs={result.get('index_daily_jobs')} "
        f"index_daily_errors={result.get('index_daily_errors')} "
        f"fund_daily_saved={result.get('fund_daily_saved_rows')} "
        f"fund_daily_jobs={result.get('fund_daily_jobs')} "
        f"fund_daily_date_batches={result.get('fund_daily_date_batches')} "
        f"fund_daily_symbol_jobs={result.get('fund_daily_symbol_jobs')} "
        f"fund_daily_errors={result.get('fund_daily_errors')} "
        f"option_start={result.get('option_start_date')} "
        f"repo_start={result.get('repo_start_date')} "
        f"option_basic_saved={result.get('option_basic_rows_saved')} "
        f"option_daily_saved={result.get('option_daily_saved_rows')} "
        f"option_refresh_dates={result.get('option_daily_refresh_dates')} "
        f"option_chunks={result.get('option_daily_chunks')} "
        f"option_errors={result.get('option_daily_errors')} "
        f"repo_daily_saved={result.get('repo_daily_saved_rows')} "
        f"repo_refresh_dates={result.get('repo_daily_refresh_dates')} "
        f"repo_chunks={result.get('repo_daily_chunks')} "
        f"repo_errors={result.get('repo_daily_errors')} "
        f"chinabond_start={result.get('chinabond_start_date')} "
        f"chinabond_daily_saved={result.get('chinabond_curve_daily_saved_rows')} "
        f"chinabond_refresh_dates={result.get('chinabond_curve_refresh_dates')} "
        f"chinabond_chunks={result.get('chinabond_curve_chunks')} "
        f"chinabond_errors={result.get('chinabond_curve_errors')} "
        f"income_mode={result.get('income_sync_mode')} "
        f"income_scope={result.get('income_symbol_scope')} "
        f"income_symbols={result.get('income_symbols')} "
        f"income_start={result.get('income_start_date')} "
        f"income_end={result.get('income_end_date')} "
        f"end_date={result.get('end_date')} "
        f"income_saved_rows={result.get('income_saved_rows')} "
        f"income_fetch_seconds={result.get('income_fetch_seconds')} "
        f"income_insert_seconds={result.get('income_insert_seconds')} "
        f"income_total_seconds={result.get('income_total_seconds')} "
        f"income_insert_batches={result.get('income_insert_batches')} "
        f"income_skipped_symbols={result.get('income_skipped_symbols')} "
        f"income_backfill_symbols={result.get('income_backfill_symbols')} "
        f"income_incremental_symbols={result.get('income_incremental_symbols')} "
        f"income_full_symbols={result.get('income_full_symbols')} "
        f"fund_flow_source={result.get('fund_flow_source')} "
        f"fund_flow_saved_rows={result.get('fund_flow_saved_rows')} "
        f"fund_flow_trade_dates={result.get('fund_flow_trade_dates')} "
        f"fund_flow_errors={result.get('fund_flow_errors')} "
        f"tables={result.get('tables')}"
    )


def _run_a_stock_innovation100_rebuild():
    from ..app.api.a_stock_innovation100 import rebuild_a_stock_innovation100_for_scheduler

    result = rebuild_a_stock_innovation100_for_scheduler()
    logging.getLogger("ScheduledTaskManager").info(
        "A stock innovation100 refreshed: mode=%s, status=%s, latest_date=%s, latest_level=%s, levels_saved=%s, rebalances_saved=%s",
        result.get("mode"),
        result.get("status"),
        result.get("latest_date"),
        result.get("latest_level"),
        result.get("levels_saved"),
        result.get("rebalances_saved"),
    )


def _run_a_stock_etf_fear_greed_backfill(start_date: Optional[str] = None):
    from ..core.services.a_stock_fear_greed_clone_service import (
        A_STOCK_FEAR_GREED_TARGETS,
        AStockInnovation100FearGreedCloneCalculator,
    )

    end_date = date.today()
    if start_date:
        output_start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    else:
        output_start_date = end_date - timedelta(days=3)

    calculation_start_date = output_start_date - timedelta(days=550)
    logger = logging.getLogger("ScheduledTaskManager")
    results = []
    errors = []
    for target in A_STOCK_FEAR_GREED_TARGETS:
        symbol = target["symbol"]
        try:
            calculator = AStockInnovation100FearGreedCloneCalculator(symbol)
            result = calculator.backfill_to_db(
                start_date=calculation_start_date,
                end_date=end_date,
                output_start_date=output_start_date,
                history_days=550,
                score_window=252,
                min_periods=120,
            )
            results.append(result)
            logger.info(
                "%s fear greed backfill saved %s rows, range=%s~%s",
                symbol,
                result.get("saved"),
                result.get("start_date"),
                result.get("end_date"),
            )
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            logger.warning("%s fear greed backfill failed: %s", symbol, exc)
    saved = sum(int(item.get("saved") or 0) for item in results)
    symbols = ",".join(str(item.get("symbol")) for item in results)
    return (
        "A stock index fear greed backfill "
        f"symbols={symbols} saved={saved} errors={len(errors)}"
    )


def _format_external_trading_fee_reconcile_result(result: Dict) -> str:
    if result.get("status") == "SKIPPED":
        return (
            "外部交易费用对账检查跳过 "
            f"reason={result.get('reason')} "
            f"today={result.get('today')}"
        )
    return (
        "外部交易费用对账检查 "
        f"status={result.get('status')} "
        f"trade_date={result.get('trade_date')} "
        f"checked={result.get('checked')} "
        f"reconciled={result.get('reconciled')} "
        f"missing={result.get('missing')} "
        f"skipped_non_a_stock={result.get('skipped_non_a_stock', 0)}"
    )


def _run_external_trading_fee_reconcile():
    if _external_trading_fee_reconcile_succeeded_today():
        return "跳过费用对账检查: 今日早前检查已成功"

    from ..core.services.external_trading_fee_reconcile import (
        check_and_alert_missing_deliver_records,
    )

    logger = logging.getLogger("ScheduledTaskManager")
    now_shanghai = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now_shanghai.date()

    result = check_and_alert_missing_deliver_records(today)

    logger.info("External trading fee reconcile check result: %s", result)
    return _format_external_trading_fee_reconcile_result(result)


def _external_trading_fee_reconcile_succeeded_today() -> bool:
    with get_db_ctx() as db:
        config = db.query(ScheduledTaskConfig).filter(
            ScheduledTaskConfig.task_key == "external_trading_fee_reconcile"
        ).first()
        if not config or not config.last_run_started_at:
            return False
        today_shanghai = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if config.last_run_started_at.date() != today_shanghai:
            return False
        message = config.last_run_message or ""
        return config.last_run_status == "SUCCESS" and "status=OK" in message


def _is_china_trading_day(check_date: date) -> bool:
    if check_date.weekday() >= 5:
        return False

    logger = logging.getLogger("ScheduledTaskManager")
    try:
        from ..core.services.tushare import TushareService

        calendar = TushareService.get_instance().get_trade_calendar_frame(check_date, check_date)
        if not calendar.empty:
            row = calendar.iloc[0]
            return int(row.get("is_open") or 0) == 1
    except Exception as exc:
        logger.warning("A-share trading calendar check failed for %s: %s", check_date, exc)

    return True


def _run_external_trading_sub_account_net_asset_snapshot():
    today_shanghai = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if not _is_china_trading_day(today_shanghai):
        return f"跳过外部交易子账户净资产快照: {today_shanghai} 不是A股交易日"

    from ..core.services.external_trading_net_asset_history import (
        process_external_trading_sub_account_net_asset_snapshot_for_robot,
    )

    result = process_external_trading_sub_account_net_asset_snapshot_for_robot()
    logging.getLogger("ScheduledTaskManager").info(
        "External trading sub-account net asset snapshot result: %s",
        result,
    )
    return (
        "外部交易子账户净资产快照 "
        f"status={result.get('status')} "
        f"trading_date={result.get('trading_date')} "
        f"checked={result.get('checked')} "
        f"recorded={result.get('recorded')} "
        f"failed={result.get('failed')}"
    )


def _run_xueqiu_top_holdings_rebalance():
    today_shanghai = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if not _is_china_trading_day(today_shanghai):
        return f"跳过雪球Top1000主理人活跃360天综合持仓自动调仓: {today_shanghai} 不是A股交易日"

    from .xueqiu_top_holdings_report import process_xueqiu_top_holdings_rebalance_for_robot

    result = process_xueqiu_top_holdings_rebalance_for_robot()
    logging.getLogger("ScheduledTaskManager").info(
        "Xueqiu top holdings rebalance result: %s",
        result,
    )
    return result


def _run_xueqiu_top_holdings_cache_refresh():
    today_shanghai = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if not _is_china_trading_day(today_shanghai):
        return f"跳过雪球Top1000榜单和主理人调仓缓存刷新: {today_shanghai} 不是A股交易日"

    from .xueqiu_top_holdings_report import process_xueqiu_top_holdings_cache_refresh_for_robot

    result = process_xueqiu_top_holdings_cache_refresh_for_robot()
    logging.getLogger("ScheduledTaskManager").info(
        "Xueqiu top holdings cache refresh result: %s",
        result,
    )
    return result


def _run_xueqiu_token_freshness_check():
    from ..core.services.xueqiu_token_monitor import process_xueqiu_token_freshness_check_for_robot

    return process_xueqiu_token_freshness_check_for_robot()


@dataclass(frozen=True)
class TaskDefinition:
    task_key: str
    name: str
    description: str
    default_time: str
    default_enabled: bool
    sort_order: int
    runner: Callable[..., None]
    default_cron_rule: Optional[str] = None
    default_allow_queue: bool = True
    default_timezone: str = DEFAULT_TASK_TIMEZONE


@dataclass(frozen=True)
class QueuedTaskRun:
    task: TaskDefinition
    trigger_source: str
    triggered_by: Optional[str]
    runner_kwargs: dict
    queued_at: datetime
    done_event: Optional[threading.Event] = None


class ScheduledTaskManager:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = threading.RLock()
        self._bootstrapped = False
        self._jobs: Dict[str, List[Dict[str, Any]]] = {}
        self._task_queue: Deque[QueuedTaskRun] = deque()
        self._queued_tasks = set()
        self._running_tasks = set()
        self._worker_thread: Optional[threading.Thread] = None
        self.task_definitions: Dict[str, TaskDefinition] = {
            "evc_stock_fetch": TaskDefinition(
                task_key="evc_stock_fetch",
                name="股票估值数据抓取",
                description="抓取 EVC 股票估值与标签数据。",
                default_time="08:00",
                default_enabled=True,
                sort_order=10,
                runner=_run_evc_stock_fetch,
            ),
            "evc_static_info_sync": TaskDefinition(
                task_key="evc_static_info_sync",
                name="美股基础数据同步",
                description="同步 EVC 全量股票池 LongPort static_info 快照/历史记录，并将标普500/纳指100成分股和 ETFManager 杠杆映射中的单倍/多倍 ETF 日K落到 DuckDB。",
                default_time="07:15",
                default_enabled=True,
                sort_order=11,
                runner=_run_us_stock_base_data_sync,
            ),
            "us_stock_industry_sync": TaskDefinition(
                task_key="us_stock_industry_sync",
                name="美股行业分类同步",
                description="使用 FMP Company Profile 补全 SPY/QQQ 成分股的 sector/industry 元数据，保存到 SQLite。",
                default_time="07:05",
                default_enabled=False,
                sort_order=13,
                runner=_run_us_stock_industry_sync,
            ),
            "etf_fair_value_analysis": TaskDefinition(
                task_key="etf_fair_value_analysis",
                name="美股ETF 估值分析",
                description="分析全部 美股ETF 的持仓与公允价值。",
                default_time="09:00",
                default_enabled=True,
                sort_order=20,
                runner=_run_etf_fair_value_analysis,
            ),
            "etf_holdings_backfill": TaskDefinition(
                task_key="etf_holdings_backfill",
                name="美股ETF持仓同步",
                description="不传开始日期时抓取全部 ETF 最新持仓增量入库；手动传入开始日期时从该日期起回刷历史持仓。",
                default_time="05:30",
                default_enabled=True,
                sort_order=15,
                runner=_run_etf_holdings_sync,
            ),
            "etf_put_call_ratio_sync": TaskDefinition(
                task_key="etf_put_call_ratio_sync",
                name="美股ETF期权数据刷新",
                description="手动触发时全量抓取 Barchart ETF Put/Call Ratio；每天自动刷新最近 10 条并记录当前期权到期未平仓快照。",
                default_time="06:00",
                default_enabled=True,
                sort_order=55,
                runner=_run_etf_put_call_ratio_sync,
            ),
            "cnn_fear_greed_fetch": TaskDefinition(
                task_key="cnn_fear_greed_fetch",
                name="CNN Fear & Greed 抓取",
                description="抓取并保存 CNN Fear & Greed Index 快照，并将 CNN 历史曲线增量写入 etf_fear_greed_clone_history。",
                default_time="10:00",
                default_enabled=True,
                sort_order=50,
                runner=_run_cnn_fear_greed_fetch,
            ),
            "soxx_fear_greed_backfill": TaskDefinition(
                task_key="soxx_fear_greed_backfill",
                name="美股ETF贪恐回跑入库",
                description="计算 SOXX/SPY/QQQ/DIA 贪恐复刻指数并保存历史、价格和持仓明细。",
                default_time="06:00",
                default_enabled=True,
                sort_order=60,
                runner=_run_etf_fear_greed_backfill,
            ),
            "a_stock_base_data_sync": TaskDefinition(
                task_key="a_stock_base_data_sync",
                name="A股基础数据同步",
                description="同步A股基础信息、名称变更、全市场日行情、基准/贪恐目标指数日行情、A股ETF日行情、贪恐/因子指数成分权重、期权/回购行情、中债信用曲线、利润表和主力资金流到DuckDB分析库。",
                default_time="18:20",
                default_enabled=True,
                sort_order=74,
                runner=_run_a_stock_base_data_sync,
            ),
            "a_stock_innovation100_rebuild": TaskDefinition(
                task_key="a_stock_innovation100_rebuild",
                name="A股创新100指数刷新",
                description="刷新A股创新100指数点位、成分股权重和再平衡追溯记录。",
                default_time="18:30",
                default_enabled=True,
                sort_order=75,
                runner=_run_a_stock_innovation100_rebuild,
            ),
            "a_stock_etf_fear_greed_backfill": TaskDefinition(
                task_key="a_stock_etf_fear_greed_backfill",
                name="A股指数贪恐回跑入库",
                description="增量或回跑计算 A创100、中证A500、中证500、中证全指、北证50、科创200、创业板指、中证煤炭、上证红利的指数贪恐复刻值，并保存到 etf_fear_greed_clone_history。",
                default_time="18:40",
                default_enabled=True,
                sort_order=76,
                runner=_run_a_stock_etf_fear_greed_backfill,
            ),
            "external_trading_fee_reconcile": TaskDefinition(
                task_key="external_trading_fee_reconcile",
                name="外部交易费用对账检查",
                description="A股开盘后检查PTrade是否已通过before_trading_start推送了前一交易日交割单(deliver_event)并完成对账；如有缺失发送告警邮件。",
                default_time="09:35",
                default_enabled=True,
                sort_order=23,
                runner=_run_external_trading_fee_reconcile,
                default_cron_rule="35 9 * * mon-fri",
            ),
            "xueqiu_token_freshness_check": TaskDefinition(
                task_key="xueqiu_token_freshness_check",
                name="雪球Token更新检查",
                description="每天上午9点检查雪球 xq_a_token 最近24小时是否更新；超过24小时未更新或未配置则发送告警邮件。",
                default_time="09:00",
                default_enabled=True,
                sort_order=22,
                runner=_run_xueqiu_token_freshness_check,
                default_cron_rule="0 9 * * *",
            ),
            "external_trading_sub_account_nav_snapshot": TaskDefinition(
                task_key="external_trading_sub_account_nav_snapshot",
                name="外部交易子账户净资产快照",
                description="A股收盘后5分钟计算每个虚拟子账户的净资产、持仓市值和可用资金，并写入每日历史曲线。",
                default_time="15:05",
                default_enabled=True,
                sort_order=24,
                runner=_run_external_trading_sub_account_net_asset_snapshot,
                default_cron_rule="5 15 * * mon-fri",
            ),
            "xueqiu_top_holdings_rebalance": TaskDefinition(
                task_key="xueqiu_top_holdings_rebalance",
                name="雪球Top1000主理人活跃360天综合持仓自动调仓",
                description="每日14:50执行；A股交易日拉取雪球年榜Top1000最新持仓，并使用已缓存的主理人调仓时间筛选最近360天活跃组合，再按Top10等权、跌出Top12才卖、从Top10补位的缓冲策略调仓目标雪球组合。",
                default_time="14:50",
                default_enabled=True,
                sort_order=25,
                runner=_run_xueqiu_top_holdings_rebalance,
                default_cron_rule="50 14 * * mon-fri",
            ),
            "xueqiu_top_holdings_cache_refresh": TaskDefinition(
                task_key="xueqiu_top_holdings_cache_refresh",
                name="雪球Top1000榜单和主理人调仓缓存刷新",
                description="每日18:00执行；A股交易日刷新雪球年榜Top1000缓存，并更新缺失或过期的主理人最新调仓时间缓存，供次日收盘前自动调仓直接使用。",
                default_time="18:00",
                default_enabled=True,
                sort_order=26,
                runner=_run_xueqiu_top_holdings_cache_refresh,
                default_cron_rule="0 18 * * mon-fri",
            ),
        }

    def bootstrap(self):
        with self._lock:
            if self._bootstrapped:
                return
            self._bootstrapped = True
        self.ensure_task_configs()
        self.reload_jobs()

    @staticmethod
    def _mask_triggered_by(triggered_by: Optional[str]) -> Optional[str]:
        if not triggered_by:
            return triggered_by
        if triggered_by == "system":
            return triggered_by
        if len(triggered_by) <= 8:
            return f"{triggered_by[:2]}***"
        return f"{triggered_by[:4]}***{triggered_by[-4:]}"

    def ensure_task_configs(self):
        with get_db_ctx() as db:
            db.query(ScheduledTaskConfig).filter(
                ScheduledTaskConfig.task_key == "etf_emotion_calculation"
            ).delete(synchronize_session=False)
            db.query(ScheduledTaskConfig).filter(
                ScheduledTaskConfig.task_key.in_(DEPRECATED_TASK_KEYS)
            ).delete(synchronize_session=False)
            for task in self.task_definitions.values():
                default_cron_rule = self._task_default_cron_rule(task)
                config = db.query(ScheduledTaskConfig).filter(
                    ScheduledTaskConfig.task_key == task.task_key
                ).first()
                if not config:
                    db.add(
                        ScheduledTaskConfig(
                            task_key=task.task_key,
                            name=task.name,
                            description=task.description,
                            enabled=task.default_enabled,
                            schedule_time=task.default_time,
                            cron_rule=default_cron_rule,
                            timezone=task.default_timezone,
                            allow_queue=task.default_allow_queue,
                            sort_order=task.sort_order,
                        )
                    )
                    continue

                config.name = task.name
                config.description = task.description
                config.sort_order = task.sort_order
                if (
                    task.task_key == "xueqiu_top_holdings_rebalance"
                    and str(config.cron_rule or "").strip() in {"40 14 * * mon-fri", "40 14 * * *"}
                ):
                    config.cron_rule = default_cron_rule
                if (
                    task.task_key == "xueqiu_top_holdings_rebalance"
                    and str(config.schedule_time or "").strip() == "14:40"
                ):
                    config.schedule_time = task.default_time
                if not self.is_valid_time(config.schedule_time):
                    config.schedule_time = task.default_time
                if not self.is_valid_cron_rule(config.cron_rule):
                    config.cron_rule = default_cron_rule
                if not self.is_valid_timezone(config.timezone):
                    config.timezone = task.default_timezone
                if config.allow_queue is None:
                    config.allow_queue = task.default_allow_queue

    def is_valid_time(self, value: str) -> bool:
        return bool(value and TIME_PATTERN.match(value))

    def is_valid_timezone(self, value: Optional[str]) -> bool:
        return bool(value in SUPPORTED_TASK_TIMEZONES)

    @staticmethod
    def _time_to_cron(value: str) -> str:
        if not value or not TIME_PATTERN.match(value):
            raise ValueError("时间格式必须为 HH:MM")
        hour, minute = value.split(":")
        return f"{int(minute)} {int(hour)} * * *"

    def _task_default_cron_rule(self, task: TaskDefinition) -> str:
        return task.default_cron_rule or self._time_to_cron(task.default_time)

    @staticmethod
    def _split_cron_rules(value: Optional[str]) -> List[str]:
        return [
            item.strip()
            for item in CRON_RULE_SPLIT_PATTERN.split(str(value or ""))
            if item.strip()
        ]

    def _build_cron_trigger(self, rule: str, timezone: Optional[str]) -> CronTrigger:
        fields = str(rule or "").split()
        if len(fields) != 5:
            raise ValueError("Cron 规则必须是 5 段格式")
        if fields[4] != "*" and re.search(r"\d", fields[4]):
            raise ValueError("Cron 周几字段请用 mon/tue/wed/thu/fri/sat/sun，避免数字周几语义差异")
        return CronTrigger.from_crontab(rule, timezone=self._task_timezone(timezone))

    def is_valid_cron_rule(self, value: Optional[str]) -> bool:
        rules = self._split_cron_rules(value)
        if not rules:
            return False
        for rule in rules:
            try:
                self._build_cron_trigger(rule, DEFAULT_TASK_TIMEZONE)
            except Exception:
                return False
        return True

    def _next_trigger_run(
        self,
        trigger: CronTrigger,
        now: Optional[datetime] = None,
        previous_fire_time: Optional[datetime] = None,
    ) -> Optional[datetime]:
        current = now or datetime.now(trigger.timezone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SERVER_TZ).astimezone(trigger.timezone)
        else:
            current = current.astimezone(trigger.timezone)
        try:
            return trigger.get_next_fire_time(previous_fire_time, current)
        except Exception as exc:
            self.logger.warning("Failed to calculate next cron run trigger=%s: %s", trigger, exc)
            return None

    def _cron_sort_minutes(self, value: Optional[str], timezone: Optional[str] = None) -> int:
        best = 24 * 60 + 1
        tz_name = self._task_timezone(timezone)
        base = datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(tz_name)) - timedelta(seconds=1)
        end = base + timedelta(days=366)
        for rule in self._split_cron_rules(value):
            try:
                trigger = self._build_cron_trigger(rule, tz_name)
                previous_fire_time = None
                cursor = base
                for _ in range(2000):
                    next_run = self._next_trigger_run(
                        trigger,
                        now=cursor,
                        previous_fire_time=previous_fire_time,
                    )
                    if not next_run or next_run > end:
                        break
                    best = min(best, next_run.hour * 60 + next_run.minute)
                    if best == 0:
                        return best
                    previous_fire_time = next_run
                    cursor = next_run + timedelta(seconds=1)
            except Exception:
                continue
        return best

    def _cron_sort_time_string(self, value: Optional[str], fallback: str, timezone: Optional[str] = None) -> str:
        minutes = self._cron_sort_minutes(value, timezone=timezone)
        if minutes > 24 * 60:
            return fallback
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def _task_timezone(self, value: Optional[str]) -> str:
        return value if self.is_valid_timezone(value) else DEFAULT_TASK_TIMEZONE

    def _latest_due_cron_time_today(
        self,
        cron_rule: Optional[str],
        timezone: Optional[str],
        now: Optional[datetime] = None,
    ) -> Optional[datetime]:
        if not self.is_valid_cron_rule(cron_rule):
            return None
        tz = ZoneInfo(self._task_timezone(timezone))
        current = now or datetime.now(tz)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SERVER_TZ)
        current = current.astimezone(tz)
        start_of_day = current.replace(hour=0, minute=0, second=0, microsecond=0)
        latest = None
        for rule in self._split_cron_rules(cron_rule):
            try:
                trigger = self._build_cron_trigger(rule, timezone)
                previous_fire_time = None
                cursor = start_of_day - timedelta(seconds=1)
                for _ in range(2000):
                    scheduled_at = self._next_trigger_run(
                        trigger,
                        now=cursor,
                        previous_fire_time=previous_fire_time,
                    )
                    if not scheduled_at or scheduled_at.date() != current.date() or scheduled_at > current:
                        break
                    if latest is None or scheduled_at > latest:
                        latest = scheduled_at
                    previous_fire_time = scheduled_at
                    cursor = scheduled_at + timedelta(seconds=1)
            except Exception:
                continue
        return latest

    def _task_time_sort_key(self, task: TaskDefinition) -> tuple:
        try:
            snapshot = self._get_task_snapshot(task.task_key)
            cron_rule = snapshot.get("cron_rule")
            timezone = snapshot.get("timezone")
        except KeyError:
            cron_rule = self._task_default_cron_rule(task)
            timezone = task.default_timezone
        return (
            0 if self._task_timezone(timezone) == "Asia/Shanghai" else 1,
            self._cron_sort_minutes(cron_rule, timezone=timezone),
            task.sort_order,
            task.task_key,
        )

    def _config_time_sort_key(self, config: dict) -> tuple:
        return (
            0 if self._task_timezone(config.get("timezone")) == "Asia/Shanghai" else 1,
            self._cron_sort_minutes(config.get("cron_rule"), timezone=config.get("timezone")),
            config.get("sort_order") or 0,
            config.get("task_key") or "",
        )

    def reload_jobs(self):
        self.ensure_task_configs()
        configs = self._list_task_snapshots()

        with self._lock:
            self._jobs = {}
            for config in sorted(configs, key=self._config_time_sort_key):
                if not config["enabled"] or not self.is_valid_cron_rule(config.get("cron_rule")):
                    continue
                task = self.task_definitions.get(config["task_key"])
                if not task:
                    continue
                jobs = []
                timezone = self._task_timezone(config.get("timezone"))
                for rule in self._split_cron_rules(config.get("cron_rule")):
                    trigger = self._build_cron_trigger(rule, timezone)
                    jobs.append({
                        "task_key": config["task_key"],
                        "rule": rule,
                        "timezone": timezone,
                        "trigger": trigger,
                        "previous_fire_time": None,
                        "next_run": self._next_trigger_run(trigger),
                    })
                self._jobs[config["task_key"]] = jobs
                self.logger.info(
                    "Registered scheduled task %s cron=%s timezone=%s",
                    config["task_key"],
                    config["cron_rule"],
                    timezone,
                )

    def run_pending(self):
        self.bootstrap()
        due_jobs = []
        due_task_keys = set()
        now_by_timezone: Dict[str, datetime] = {}
        with self._lock:
            for task_key, jobs in self._jobs.items():
                for job in jobs:
                    timezone = self._task_timezone(job.get("timezone"))
                    now = now_by_timezone.get(timezone)
                    if now is None:
                        now = datetime.now(ZoneInfo(timezone))
                        now_by_timezone[timezone] = now
                    trigger = job.get("trigger")
                    if not trigger:
                        continue
                    next_run = job.get("next_run")
                    if not next_run:
                        job["next_run"] = self._next_trigger_run(trigger, now=now)
                        continue
                    if now < next_run:
                        continue
                    if task_key not in due_task_keys:
                        due_jobs.append(task_key)
                        due_task_keys.add(task_key)
                    job["previous_fire_time"] = next_run
                    job["next_run"] = self._next_trigger_run(
                        trigger,
                        now=now,
                        previous_fire_time=next_run,
                    )
        for task_key in due_jobs:
            self.trigger_task(
                task_key,
                trigger_source="schedule",
                background=True,
                raise_if_running=False,
            )

    def list_tasks(self) -> List[dict]:
        self.bootstrap()
        configs = self._list_task_snapshots()

        with self._lock:
            running_tasks = set(self._running_tasks)
            queued_tasks = set(self._queued_tasks)
            jobs = dict(self._jobs)

        return [
            self._serialize_task(
                config,
                jobs.get(config["task_key"]),
                config["task_key"] in running_tasks,
                config["task_key"] in queued_tasks,
            )
            for config in configs
        ]

    def get_task(self, task_key: str) -> dict:
        self.bootstrap()
        self._require_task(task_key)
        config = self._get_task_snapshot(task_key)
        with self._lock:
            job = self._jobs.get(task_key)
            is_running = task_key in self._running_tasks
            is_queued = task_key in self._queued_tasks
        return self._serialize_task(config, job, is_running, is_queued)

    def _publish_tasks_event(self, task_key: Optional[str] = None):
        try:
            publish_event(None, "scheduled_tasks", {
                "task_key": task_key,
                "tasks": self.list_tasks(),
            })
        except Exception:
            self.logger.exception("Failed to publish scheduled task event")

    def is_task_enabled(self, task_key: str) -> bool:
        self.bootstrap()
        self._require_task(task_key)
        with get_db_ctx() as db:
            config = db.query(ScheduledTaskConfig).filter(
                ScheduledTaskConfig.task_key == task_key
            ).first()
            return bool(config and config.enabled)

    def has_run_today(self, task_key: str, target_date: Optional[date] = None) -> bool:
        self.bootstrap()
        self._require_task(task_key)
        task_snapshot = self._get_task_snapshot(task_key)
        last_run_started_at = task_snapshot.get("last_run_started_at")
        if not last_run_started_at:
            return False
        target_date = target_date or date.today()
        return last_run_started_at.date() == target_date

    def has_missed_schedule_today(self, task_key: str, now: Optional[datetime] = None) -> bool:
        self.bootstrap()
        self._require_task(task_key)
        task_snapshot = self._get_task_snapshot(task_key)
        latest_due = self._latest_due_cron_time_today(
            task_snapshot.get("cron_rule"),
            task_snapshot.get("timezone"),
            now=now,
        )
        if not latest_due:
            return False
        last_run_started_at = task_snapshot.get("last_run_started_at")
        if not last_run_started_at:
            return True
        latest_due_server = latest_due.astimezone(SERVER_TZ).replace(tzinfo=None)
        return last_run_started_at < latest_due_server

    def should_run_on_startup(self, task_key: str, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        if task_key in NO_STARTUP_CATCH_UP_TASK_KEYS:
            return False
        return (
            self.is_task_enabled(task_key)
            and self.has_missed_schedule_today(task_key, now=now)
        )

    def update_task(
        self,
        task_key: str,
        enabled: bool,
        cron_rule: Optional[str] = None,
        allow_queue: Optional[bool] = None,
        timezone: Optional[str] = None,
        schedule_time: Optional[str] = None,
        updated_by: Optional[str] = None,
    ) -> dict:
        self.bootstrap()
        task = self._require_task(task_key)
        if cron_rule is None:
            if schedule_time is not None:
                cron_rule = self._time_to_cron(schedule_time)
            else:
                cron_rule = self._task_default_cron_rule(task)
        cron_rule = "\n".join(self._split_cron_rules(cron_rule))
        if not self.is_valid_cron_rule(cron_rule):
            raise ValueError("Cron 规则必须是 5 段格式，多个规则可用分号或换行分隔；周几字段请用 mon-fri 这种英文写法")

        with get_db_ctx() as db:
            config = db.query(ScheduledTaskConfig).filter(
                ScheduledTaskConfig.task_key == task_key
            ).first()
            if not config:
                raise KeyError(f"Task config not found: {task_key}")
            timezone_value = timezone or config.timezone or task.default_timezone
            if not self.is_valid_timezone(timezone_value):
                raise ValueError("时区只支持 Asia/Shanghai 或 America/New_York")
            config.enabled = enabled
            config.cron_rule = cron_rule
            config.timezone = timezone_value
            config.schedule_time = self._cron_sort_time_string(
                cron_rule,
                task.default_time,
                timezone=timezone_value,
            )
            if allow_queue is not None:
                config.allow_queue = bool(allow_queue)
            config.updated_by = updated_by
            config.updated_at = datetime.now()

        self.reload_jobs()
        task_snapshot = self.get_task(task_key)
        self._publish_tasks_event(task_key)
        return task_snapshot

    def trigger_task(
        self,
        task_key: str,
        trigger_source: str = "manual",
        triggered_by: Optional[str] = None,
        background: bool = True,
        raise_if_running: bool = True,
        **runner_kwargs,
    ) -> bool:
        self.bootstrap()
        task = self._require_task(task_key)
        config = self._get_task_snapshot(task_key)
        allow_queue = bool(config.get("allow_queue"))
        done_event = None if background or not allow_queue else threading.Event()
        direct_run: Optional[QueuedTaskRun] = None

        with self._lock:
            if task_key in self._running_tasks or task_key in self._queued_tasks:
                if raise_if_running:
                    raise RuntimeError(f"任务 {task.name} 正在执行或排队中，请稍后再试")
                self.logger.info("Skip triggering %s because it is already running or queued", task_key)
                return False
            queued_run = QueuedTaskRun(
                task=task,
                trigger_source=trigger_source,
                triggered_by=triggered_by,
                runner_kwargs=dict(runner_kwargs),
                queued_at=datetime.now(),
                done_event=done_event,
            )
            if allow_queue:
                self._task_queue.append(queued_run)
                self._queued_tasks.add(task_key)
                queue_size = len(self._task_queue)
                self._ensure_worker_locked()
            else:
                self._running_tasks.add(task_key)
                queue_size = 0
                direct_run = queued_run

        if direct_run:
            self.logger.info("Starting non-queued scheduled task %s, source=%s", task_key, trigger_source)
            if background:
                threading.Thread(
                    target=self._run_direct_task,
                    args=(direct_run,),
                    daemon=True,
                    name=f"scheduled-task-direct-{task_key}",
                ).start()
            else:
                self._run_direct_task(direct_run)
        else:
            self.logger.info(
                "Queued scheduled task %s, source=%s, queue_size=%s",
                task_key,
                trigger_source,
                queue_size,
            )

        self._publish_tasks_event(task_key)

        if done_event:
            done_event.wait()

        return True

    def _ensure_worker_locked(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(
            target=self._run_background_queue,
            daemon=True,
            name="scheduled-task-worker",
        )
        self._worker_thread.start()

    def _run_direct_task(self, queued_run: QueuedTaskRun):
        try:
            self._execute_task(
                queued_run.task,
                queued_run.trigger_source,
                queued_run.triggered_by,
                queued_run.runner_kwargs,
            )
        finally:
            if queued_run.done_event:
                queued_run.done_event.set()

    def _run_background_queue(self):
        while True:
            queued_run = None
            with self._lock:
                if not self._task_queue:
                    self._worker_thread = None
                    return
                queued_run = self._task_queue.popleft()
                self._queued_tasks.discard(queued_run.task.task_key)
                self._running_tasks.add(queued_run.task.task_key)

            self._publish_tasks_event(queued_run.task.task_key)

            try:
                self._execute_task(
                    queued_run.task,
                    queued_run.trigger_source,
                    queued_run.triggered_by,
                    queued_run.runner_kwargs,
                )
            finally:
                if queued_run.done_event:
                    queued_run.done_event.set()

    def _execute_task(
        self,
        task: TaskDefinition,
        trigger_source: str,
        triggered_by: Optional[str],
        runner_kwargs: dict,
    ):
        started_at = datetime.now()
        status = "SUCCESS"
        message = "执行成功"
        masked_triggered_by = self._mask_triggered_by(triggered_by)

        self.logger.info(
            "Starting scheduled task %s, source=%s, triggered_by=%s, kwargs=%s",
            task.task_key,
            trigger_source,
            masked_triggered_by,
            runner_kwargs,
        )

        try:
            result_message = task.runner(**runner_kwargs)
            if result_message:
                message = str(result_message)
        except Exception as exc:
            status = "FAILED"
            message = str(exc)
            self.logger.error(
                "Scheduled task %s failed: %s\n%s",
                task.task_key,
                exc,
                traceback.format_exc(),
            )
            send_alert_email(
                f"定时任务执行失败: {task.name}",
                f"task_key={task.task_key}\nsource={trigger_source}\nerror={exc}\n\n{traceback.format_exc()}",
                scenario_key="scheduled_task_failure",
            )
        finally:
            finished_at = datetime.now()
            duration_seconds = round((finished_at - started_at).total_seconds(), 3)
            with get_db_ctx() as db:
                config = db.query(ScheduledTaskConfig).filter(
                    ScheduledTaskConfig.task_key == task.task_key
                ).first()
                if config:
                    config.last_trigger_source = trigger_source
                    config.last_run_started_at = started_at
                    config.last_run_finished_at = finished_at
                    config.last_run_status = status
                    config.last_run_message = _truncate_task_message(message)
                    config.last_duration_seconds = duration_seconds
                    if triggered_by:
                        config.updated_by = triggered_by

            with self._lock:
                self._running_tasks.discard(task.task_key)

            self.logger.info(
                "Finished scheduled task %s with status=%s in %.3fs",
                task.task_key,
                status,
                duration_seconds,
            )
            self._publish_tasks_event(task.task_key)

    def _serialize_task(
        self,
        config: dict,
        jobs: Optional[List[Dict[str, Any]]],
        is_running: bool,
        is_queued: bool = False,
    ) -> dict:
        scheduled_jobs = jobs or []
        next_runs = [job.get("next_run") for job in scheduled_jobs if job and job.get("next_run")]
        next_run = min(next_runs) if next_runs else None
        return {
            "task_key": config["task_key"],
            "name": config["name"],
            "description": config["description"],
            "enabled": config["enabled"],
            "schedule_time": config["schedule_time"],
            "cron_rule": config["cron_rule"],
            "timezone": config["timezone"],
            "allow_queue": config["allow_queue"],
            "first_daily_trigger_minutes": self._cron_sort_minutes(
                config.get("cron_rule"),
                timezone=config.get("timezone"),
            ),
            "sort_order": config["sort_order"],
            "supports_start_date": config["task_key"] in {
                "evc_static_info_sync",
                "a_stock_base_data_sync",
                "etf_holdings_backfill",
                "soxx_fear_greed_backfill",
                "a_stock_etf_fear_greed_backfill",
            },
            "is_running": is_running,
            "is_queued": is_queued,
            "next_run_at": next_run.isoformat() if next_run else None,
            "last_trigger_source": config["last_trigger_source"],
            "last_run_started_at": config["last_run_started_at"].isoformat() if config["last_run_started_at"] else None,
            "last_run_finished_at": config["last_run_finished_at"].isoformat() if config["last_run_finished_at"] else None,
            "last_run_status": config["last_run_status"],
            "last_run_message": config["last_run_message"],
            "last_duration_seconds": config["last_duration_seconds"],
            "updated_by": config["updated_by"],
            "created_at": config["created_at"].isoformat() if config["created_at"] else None,
            "updated_at": config["updated_at"].isoformat() if config["updated_at"] else None,
        }

    def _list_task_snapshots(self) -> List[dict]:
        with get_db_ctx() as db:
            configs = db.query(ScheduledTaskConfig).order_by(
                ScheduledTaskConfig.sort_order.asc()
            ).all()
            return [self._snapshot_config(config) for config in configs]

    def _get_task_snapshot(self, task_key: str) -> dict:
        with get_db_ctx() as db:
            config = db.query(ScheduledTaskConfig).filter(
                ScheduledTaskConfig.task_key == task_key
            ).first()
            if not config:
                raise KeyError(f"Task config not found: {task_key}")
            return self._snapshot_config(config)

    def _snapshot_config(self, config: ScheduledTaskConfig) -> dict:
        task = self.task_definitions.get(config.task_key)
        fallback_cron_rule = self._task_default_cron_rule(task) if task else None
        cron_rule = config.cron_rule or fallback_cron_rule or (
            self._time_to_cron(config.schedule_time) if self.is_valid_time(config.schedule_time) else None
        )
        timezone = self._task_timezone(config.timezone or (task.default_timezone if task else None))
        return {
            "task_key": config.task_key,
            "name": config.name,
            "description": config.description,
            "enabled": config.enabled,
            "schedule_time": config.schedule_time,
            "cron_rule": cron_rule,
            "timezone": timezone,
            "allow_queue": config.allow_queue is not False,
            "sort_order": config.sort_order,
            "last_trigger_source": config.last_trigger_source,
            "last_run_started_at": config.last_run_started_at,
            "last_run_finished_at": config.last_run_finished_at,
            "last_run_status": config.last_run_status,
            "last_run_message": config.last_run_message,
            "last_duration_seconds": config.last_duration_seconds,
            "updated_by": config.updated_by,
            "created_at": config.created_at,
            "updated_at": config.updated_at,
        }

    def _require_task(self, task_key: str) -> TaskDefinition:
        task = self.task_definitions.get(task_key)
        if not task:
            raise KeyError(f"Unknown task: {task_key}")
        return task


scheduled_task_manager = ScheduledTaskManager()


def run_startup_tasks():
    scheduled_task_manager.bootstrap()
    now = datetime.now()
    tasks = sorted(
        (
            task
            for task in scheduled_task_manager.task_definitions.values()
            if scheduled_task_manager.should_run_on_startup(task.task_key, now=now)
        ),
        key=scheduled_task_manager._task_time_sort_key,
    )
    for task in tasks:
        scheduled_task_manager.trigger_task(
            task.task_key,
            trigger_source="startup",
            triggered_by="system",
            background=True,
            raise_if_running=False,
        )
