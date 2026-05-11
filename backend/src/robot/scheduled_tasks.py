import logging
import re
import threading
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from typing import Callable, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo

import schedule

from ..core.database import ScheduledTaskConfig, SnowballApiHeartbeat, get_db_ctx
from ..core.utils import send_alert_email

TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
LAST_RUN_MESSAGE_MAX_LENGTH = 4000
TASK_ERROR_PREVIEW_LIMIT = 20
TASK_ERROR_PREVIEW_MAX_LENGTH = 3600
SNOWBALL_PTRADE_HEARTBEAT_ENDPOINT = "snowball_opportunities"
SNOWBALL_PTRADE_MONITORED_URL = "http://api.52etf.vip/api/snowball/opportunities"
SNOWBALL_PTRADE_BASE_URL = "http://api.52etf.vip/api/snowball"
SNOWBALL_PTRADE_HEARTBEAT_WINDOW_MINUTES = 5


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

    logging.getLogger("ScheduledTaskManager").info(
        (
            "US stock base data synced: static_symbols=%s static_fetched=%s "
            "daily_symbols=%s us_static_info_fetched=%s daily_saved_rows=%s "
            "daily_adjustment_refreshes=%s daily_errors=%s tables=%s"
        ),
        static_result.get("symbols"),
        static_result.get("fetched"),
        us_result.get("daily_symbols"),
        us_result.get("static_info_fetched"),
        us_result.get("daily_saved_rows"),
        us_result.get("daily_adjustment_refresh_count"),
        len(us_result.get("daily_errors") or []),
        us_result.get("tables"),
    )

    return (
        "US stock base data sync "
        f"static_symbols={static_result.get('symbols')} "
        f"static_fetched={static_result.get('fetched')} "
        f"daily_symbols={us_result.get('daily_symbols')} "
        f"static_info_fetched={us_result.get('static_info_fetched')} "
        f"daily_fetched_symbols={us_result.get('daily_fetched_symbols')} "
        f"daily_saved_rows={us_result.get('daily_saved_rows')} "
        f"daily_adjustment_refreshes={us_result.get('daily_adjustment_refresh_count')} "
        f"daily_errors={len(us_result.get('daily_errors') or [])} "
        f"tables={us_result.get('tables')}"
    )


def _run_us_stock_industry_sync():
    from .us_stock_industry_sync import sync_us_stock_industry_snapshots

    result = sync_us_stock_industry_snapshots()
    logging.getLogger("ScheduledTaskManager").info(
        (
            "US stock industry synced: symbols=%s target=%s saved=%s skipped=%s "
            "remaining=%s api_calls=%s errors=%s"
        ),
        result.get("symbols"),
        result.get("target_symbols"),
        result.get("saved"),
        result.get("skipped_existing"),
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
            "option_basic_saved=%s option_daily_saved=%s option_refresh_dates=%s option_chunks=%s option_errors=%s "
            "repo_daily_saved=%s repo_refresh_dates=%s repo_chunks=%s repo_errors=%s "
            "chinabond_defs_saved=%s chinabond_daily_saved=%s chinabond_refresh_dates=%s chinabond_chunks=%s chinabond_errors=%s "
            "income_fetched_rows=%s income_saved_rows=%s income_fetch_seconds=%s "
            "income_insert_seconds=%s income_total_seconds=%s income_insert_batches=%s "
            "income_skipped_symbols=%s income_backfill_symbols=%s income_incremental_symbols=%s income_full_symbols=%s"
        ),
        result.get("status"),
        result.get("mode"),
        result.get("end_date"),
        result.get("tables"),
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
    )
    return (
        "A stock base data sync "
        f"start={result.get('start_date')} "
        f"market_start={result.get('market_start_date')} "
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


def _run_a_stock_innovation_momentum_live_sync():
    from ..app.api.a_stock_innovation_momentum_live import sync_all_enabled_a_stock_innovation_momentum_configs_for_scheduler

    result = sync_all_enabled_a_stock_innovation_momentum_configs_for_scheduler()
    logging.getLogger("ScheduledTaskManager").info(
        "A stock innovation momentum virtual strategies synced: success=%s, errors=%s",
        len(result.get("synced") or []),
        len(result.get("errors") or []),
    )


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


def _run_snowball_ptrade_heartbeat_check():
    now_shanghai = datetime.now(ZoneInfo("Asia/Shanghai"))
    check_time = now_shanghai.time()
    if not (dtime(9, 35) <= check_time <= dtime(9, 45)):
        return f"跳过检查: 当前上海时间 {now_shanghai.strftime('%H:%M:%S')} 不在 09:35-09:45 窗口"

    if not _is_china_trading_day(now_shanghai.date()):
        return f"跳过检查: {now_shanghai.date().isoformat()} 非A股交易日"

    now = now_shanghai.replace(tzinfo=None)
    cutoff = now - timedelta(minutes=SNOWBALL_PTRADE_HEARTBEAT_WINDOW_MINUTES)
    with get_db_ctx() as db:
        heartbeat = db.query(SnowballApiHeartbeat).filter(
            SnowballApiHeartbeat.endpoint == SNOWBALL_PTRADE_HEARTBEAT_ENDPOINT
        ).first()
        heartbeat_snapshot = {
            "last_called_at": heartbeat.last_called_at,
            "last_cli_id": heartbeat.last_cli_id,
            "call_count": heartbeat.call_count,
        } if heartbeat else None

    last_called_at = heartbeat_snapshot["last_called_at"] if heartbeat_snapshot else None
    if last_called_at and last_called_at >= cutoff:
        return (
            "雪球 PTrade 心跳正常 "
            f"last_called_at={last_called_at.strftime('%Y-%m-%d %H:%M:%S')} "
            f"cli_id={heartbeat_snapshot['last_cli_id']} call_count={heartbeat_snapshot['call_count']}"
        )

    last_called_text = last_called_at.strftime("%Y-%m-%d %H:%M:%S") if last_called_at else "无记录"
    subject = "雪球PTrade接口心跳告警: A股开盘后5分钟未调用"
    body = (
        "A股开盘后心跳检查发现最近5分钟没有收到 PTrade 对雪球交易机会接口的调用。\n\n"
        f"监控接口: {SNOWBALL_PTRADE_MONITORED_URL}\n"
        f"接口前缀: {SNOWBALL_PTRADE_BASE_URL}\n"
        f"检查时间(上海): {now_shanghai.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"检查窗口: 最近 {SNOWBALL_PTRADE_HEARTBEAT_WINDOW_MINUTES} 分钟\n"
        f"最近调用时间: {last_called_text}\n"
        f"最近 cli_id: {heartbeat_snapshot['last_cli_id'] if heartbeat_snapshot else '无'}\n\n"
        "请检查 PTrade 策略是否已启动、券商服务器能否访问 HTTP 接口，以及 nginx 的 api.52etf.vip:80 反代是否正常。"
    )
    send_alert_email(subject, body)
    return f"已发送告警: 最近调用时间={last_called_text}"


@dataclass(frozen=True)
class TaskDefinition:
    task_key: str
    name: str
    description: str
    default_time: str
    default_enabled: bool
    sort_order: int
    runner: Callable[..., None]


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
        self.scheduler = schedule.Scheduler()
        self._lock = threading.RLock()
        self._bootstrapped = False
        self._jobs: Dict[str, schedule.Job] = {}
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
                name="美股ETF持仓抓取入库",
                description="抓取全部 ETF 最新持仓，并按发行商返回的持仓日期覆盖入库。",
                default_time="05:30",
                default_enabled=True,
                sort_order=15,
                runner=_run_etf_holdings_ingest,
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
            "etf_historical_holdings_backfill": TaskDefinition(
                task_key="etf_historical_holdings_backfill",
                name="美股ETF历史持仓回刷",
                description="手动回刷 iShares 历史 asOfDate 持仓和非 iShares 的 SEC N-PORT 历史持仓。",
                default_time="05:00",
                default_enabled=False,
                sort_order=12,
                runner=_run_etf_historical_holdings_backfill,
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
                description="同步A股基础信息、名称变更、全市场日行情、基准/贪恐目标指数日行情、A股ETF日行情、贪恐/因子指数成分权重、期权/回购行情、中债信用曲线和利润表到DuckDB分析库。",
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
                description="增量或回跑计算 A创100、中证A500、中证500、科创200、创业板指、中证煤炭、上证红利的指数贪恐复刻值，并保存到 etf_fear_greed_clone_history。",
                default_time="18:40",
                default_enabled=True,
                sort_order=76,
                runner=_run_a_stock_etf_fear_greed_backfill,
            ),
            "a_stock_innovation_momentum_live_sync": TaskDefinition(
                task_key="a_stock_innovation_momentum_live_sync",
                name="A股创新100动量虚拟盘同步",
                description="同步所有启用的A股创新100风险调整混合动量虚拟盘，生成排名信号、模拟成交、刷新净值和持仓。",
                default_time="18:45",
                default_enabled=True,
                sort_order=77,
                runner=_run_a_stock_innovation_momentum_live_sync,
            ),
            "snowball_ptrade_heartbeat_check": TaskDefinition(
                task_key="snowball_ptrade_heartbeat_check",
                name="PTrade接口心跳检查",
                description="A股开盘后检查PTrade交易机会接口最近5分钟是否被调用，未调用则发送告警邮件。",
                default_time="09:35",
                default_enabled=True,
                sort_order=22,
                runner=_run_snowball_ptrade_heartbeat_check,
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
                ScheduledTaskConfig.task_key.in_([
                    "a_stock_income_sync",
                    "etf_nport_holdings_import",
                    "w20_momentum_live_sync",
                ])
            ).delete(synchronize_session=False)
            for task in self.task_definitions.values():
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
                            sort_order=task.sort_order,
                        )
                    )
                    continue

                config.name = task.name
                config.description = task.description
                config.sort_order = task.sort_order
                if not self.is_valid_time(config.schedule_time):
                    config.schedule_time = task.default_time

    def is_valid_time(self, value: str) -> bool:
        return bool(value and TIME_PATTERN.match(value))

    @staticmethod
    def _schedule_sort_minutes(value: Optional[str]) -> int:
        if not value or not TIME_PATTERN.match(value):
            return 24 * 60 + 1
        hour, minute = [int(part) for part in value.split(":")]
        return hour * 60 + minute

    def _task_time_sort_key(self, task: TaskDefinition) -> tuple:
        try:
            snapshot = self._get_task_snapshot(task.task_key)
            schedule_time = snapshot.get("schedule_time")
        except KeyError:
            schedule_time = task.default_time
        return (
            self._schedule_sort_minutes(schedule_time),
            task.sort_order,
            task.task_key,
        )

    def _config_time_sort_key(self, config: dict) -> tuple:
        return (
            self._schedule_sort_minutes(config.get("schedule_time")),
            config.get("sort_order") or 0,
            config.get("task_key") or "",
        )

    def reload_jobs(self):
        self.ensure_task_configs()
        configs = self._list_task_snapshots()

        with self._lock:
            self.scheduler.clear("managed-task")
            self._jobs = {}
            for config in sorted(configs, key=self._config_time_sort_key):
                if not config["enabled"] or not self.is_valid_time(config["schedule_time"]):
                    continue
                task = self.task_definitions.get(config["task_key"])
                if not task:
                    continue
                job = self.scheduler.every().day.at(config["schedule_time"]).do(
                    self.trigger_task,
                    config["task_key"],
                    trigger_source="schedule",
                    background=True,
                    raise_if_running=False,
                )
                job.tag("managed-task", config["task_key"])
                self._jobs[config["task_key"]] = job
                self.logger.info(
                    "Registered scheduled task %s at %s",
                    config["task_key"],
                    config["schedule_time"],
                )

    def run_pending(self):
        self.bootstrap()
        with self._lock:
            self.scheduler.run_pending()

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
        schedule_time = task_snapshot.get("schedule_time")
        if not self.is_valid_time(schedule_time):
            return False

        now = now or datetime.now()
        hour, minute = [int(part) for part in schedule_time.split(":")]
        scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now >= scheduled_at

    def should_run_on_startup(self, task_key: str, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        return (
            self.is_task_enabled(task_key)
            and not self.has_run_today(task_key, target_date=now.date())
            and self.has_missed_schedule_today(task_key, now=now)
        )

    def update_task(self, task_key: str, enabled: bool, schedule_time: str, updated_by: Optional[str] = None) -> dict:
        self.bootstrap()
        self._require_task(task_key)
        if not self.is_valid_time(schedule_time):
            raise ValueError("时间格式必须为 HH:MM")

        with get_db_ctx() as db:
            config = db.query(ScheduledTaskConfig).filter(
                ScheduledTaskConfig.task_key == task_key
            ).first()
            if not config:
                raise KeyError(f"Task config not found: {task_key}")
            config.enabled = enabled
            config.schedule_time = schedule_time
            config.updated_by = updated_by
            config.updated_at = datetime.now()

        self.reload_jobs()
        return self.get_task(task_key)

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
        done_event = None if background else threading.Event()

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
            self._task_queue.append(queued_run)
            self._queued_tasks.add(task_key)
            queue_size = len(self._task_queue)
            self._ensure_worker_locked()

        self.logger.info(
            "Queued scheduled task %s, source=%s, queue_size=%s",
            task_key,
            trigger_source,
            queue_size,
        )

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

    def _serialize_task(
        self,
        config: dict,
        job: Optional[schedule.Job],
        is_running: bool,
        is_queued: bool = False,
    ) -> dict:
        return {
            "task_key": config["task_key"],
            "name": config["name"],
            "description": config["description"],
            "enabled": config["enabled"],
            "schedule_time": config["schedule_time"],
            "sort_order": config["sort_order"],
            "supports_start_date": config["task_key"] in {
                "evc_static_info_sync",
                "a_stock_base_data_sync",
                "etf_historical_holdings_backfill",
                "soxx_fear_greed_backfill",
                "a_stock_etf_fear_greed_backfill",
            },
            "is_running": is_running,
            "is_queued": is_queued,
            "next_run_at": job.next_run.isoformat() if job and job.next_run else None,
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
        return {
            "task_key": config.task_key,
            "name": config.name,
            "description": config.description,
            "enabled": config.enabled,
            "schedule_time": config.schedule_time,
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
