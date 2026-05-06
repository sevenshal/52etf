import logging
import re
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Callable, Dict, List, Optional

import schedule

from ..core.database import ScheduledTaskConfig, get_db_ctx
from ..core.utils import send_alert_email

TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
LAST_RUN_MESSAGE_MAX_LENGTH = 4000
TASK_ERROR_PREVIEW_LIMIT = 20
TASK_ERROR_PREVIEW_MAX_LENGTH = 3600


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


def _run_evc_static_info_sync():
    from .evc_manager import EVCManager

    manager = EVCManager()
    try:
        result = manager.sync_static_info_snapshots()
    finally:
        manager.db_session.close()

    logging.getLogger("ScheduledTaskManager").info(
        "EVC static info sync symbols=%s fetched=%s created=%s changed=%s refreshed=%s history=%s missing=%s",
        result.get("symbols"),
        result.get("fetched"),
        result.get("created"),
        result.get("changed"),
        result.get("refreshed"),
        result.get("history"),
        result.get("missing"),
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
        scraper.fetch_data_and_save()
    finally:
        scraper.db_session.close()


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


def _run_w20_momentum_live_sync():
    from ..app.api.w20_momentum_live import sync_all_enabled_w20_momentum_live_configs_for_scheduler

    result = sync_all_enabled_w20_momentum_live_configs_for_scheduler()
    logging.getLogger("ScheduledTaskManager").info(
        "W20 momentum virtual strategies synced: success=%s, errors=%s",
        len(result.get("synced") or []),
        len(result.get("errors") or []),
    )

def _run_a_stock_innovation100_rebuild():
    from ..app.api.a_stock_innovation100 import rebuild_a_stock_innovation100_for_scheduler

    result = rebuild_a_stock_innovation100_for_scheduler()
    logging.getLogger("ScheduledTaskManager").info(
        "A stock innovation100 rebuilt: latest_date=%s, latest_level=%s",
        result.get("latest_date"),
        result.get("latest_level"),
    )

def _run_a_stock_innovation_momentum_live_sync():
    from ..app.api.a_stock_innovation_momentum_live import sync_all_enabled_a_stock_innovation_momentum_configs_for_scheduler

    result = sync_all_enabled_a_stock_innovation_momentum_configs_for_scheduler()
    logging.getLogger("ScheduledTaskManager").info(
        "A stock innovation momentum virtual strategies synced: success=%s, errors=%s",
        len(result.get("synced") or []),
        len(result.get("errors") or []),
    )


@dataclass(frozen=True)
class TaskDefinition:
    task_key: str
    name: str
    description: str
    default_time: str
    default_enabled: bool
    sort_order: int
    runner: Callable[..., None]


class ScheduledTaskManager:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.scheduler = schedule.Scheduler()
        self._lock = threading.RLock()
        self._bootstrapped = False
        self._jobs: Dict[str, schedule.Job] = {}
        self._running_tasks = set()
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
                name="股票静态信息同步",
                description="批量获取 EVC 股票池的 LongPort static_info，维护快照与历史记录。",
                default_time="07:15",
                default_enabled=True,
                sort_order=11,
                runner=_run_evc_static_info_sync,
            ),
            "etf_fair_value_analysis": TaskDefinition(
                task_key="etf_fair_value_analysis",
                name="ETF 估值分析",
                description="分析全部 ETF 的持仓与公允价值。",
                default_time="09:00",
                default_enabled=True,
                sort_order=20,
                runner=_run_etf_fair_value_analysis,
            ),
            "etf_holdings_backfill": TaskDefinition(
                task_key="etf_holdings_backfill",
                name="ETF持仓抓取入库",
                description="抓取全部 ETF 最新持仓，并按发行商返回的持仓日期覆盖入库。",
                default_time="05:30",
                default_enabled=True,
                sort_order=15,
                runner=_run_etf_holdings_ingest,
            ),
            "etf_put_call_ratio_sync": TaskDefinition(
                task_key="etf_put_call_ratio_sync",
                name="ETF期权数据刷新",
                description="手动触发时全量抓取 Barchart ETF Put/Call Ratio；每天自动刷新最近 10 条并记录当前期权到期未平仓快照。",
                default_time="06:00",
                default_enabled=True,
                sort_order=55,
                runner=_run_etf_put_call_ratio_sync,
            ),
            "etf_historical_holdings_backfill": TaskDefinition(
                task_key="etf_historical_holdings_backfill",
                name="ETF历史持仓回刷",
                description="手动回刷 iShares 历史 asOfDate 持仓和非 iShares 的 SEC N-PORT 历史持仓。",
                default_time="05:00",
                default_enabled=False,
                sort_order=12,
                runner=_run_etf_historical_holdings_backfill,
            ),
            "cnn_fear_greed_fetch": TaskDefinition(
                task_key="cnn_fear_greed_fetch",
                name="CNN Fear & Greed 抓取",
                description="抓取并保存 CNN Fear & Greed Index。",
                default_time="10:00",
                default_enabled=True,
                sort_order=50,
                runner=_run_cnn_fear_greed_fetch,
            ),
            "soxx_fear_greed_backfill": TaskDefinition(
                task_key="soxx_fear_greed_backfill",
                name="ETF贪恐回跑入库",
                description="计算 SOXX/SPY/QQQ/DIA 贪恐复刻指数并保存历史、价格和持仓明细。",
                default_time="06:00",
                default_enabled=True,
                sort_order=60,
                runner=_run_etf_fear_greed_backfill,
            ),
            "w20_momentum_live_sync": TaskDefinition(
                task_key="w20_momentum_live_sync",
                name="W20动量虚拟盘同步",
                description="同步所有启用的 W20 风险调整 ETF 动量虚拟盘，生成信号、模拟成交、刷新净值和持仓。",
                default_time="09:35",
                default_enabled=True,
                sort_order=70,
                runner=_run_w20_momentum_live_sync,
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
            "a_stock_innovation_momentum_live_sync": TaskDefinition(
                task_key="a_stock_innovation_momentum_live_sync",
                name="A股创新100动量虚拟盘同步",
                description="同步所有启用的A股创新100风险调整混合动量虚拟盘，生成排名信号、模拟成交、刷新净值和持仓。",
                default_time="18:45",
                default_enabled=True,
                sort_order=76,
                runner=_run_a_stock_innovation_momentum_live_sync,
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
                    "etf_nport_holdings_import",
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

    def reload_jobs(self):
        self.ensure_task_configs()
        configs = self._list_task_snapshots()

        with self._lock:
            self.scheduler.clear("managed-task")
            self._jobs = {}
            for config in configs:
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
            jobs = dict(self._jobs)

        return [
            self._serialize_task(
                config,
                jobs.get(config["task_key"]),
                config["task_key"] in running_tasks,
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
        return self._serialize_task(config, job, is_running)

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

        with self._lock:
            if task_key in self._running_tasks:
                if raise_if_running:
                    raise RuntimeError(f"任务 {task.name} 正在执行中，请稍后再试")
                self.logger.info("Skip triggering %s because it is already running", task_key)
                return False
            self._running_tasks.add(task_key)

        if background:
            thread = threading.Thread(
                target=self._execute_task,
                args=(task, trigger_source, triggered_by, runner_kwargs),
                daemon=True,
                name=f"task-{task_key}",
            )
            thread.start()
            return True

        self._execute_task(task, trigger_source, triggered_by, runner_kwargs)
        return True

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

    def _serialize_task(self, config: dict, job: Optional[schedule.Job], is_running: bool) -> dict:
        return {
            "task_key": config["task_key"],
            "name": config["name"],
            "description": config["description"],
            "enabled": config["enabled"],
            "schedule_time": config["schedule_time"],
            "sort_order": config["sort_order"],
            "supports_start_date": config["task_key"] in {
                "etf_historical_holdings_backfill",
                "soxx_fear_greed_backfill",
            },
            "is_running": is_running,
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
    tasks = sorted(
        scheduled_task_manager.task_definitions.values(),
        key=lambda task: task.sort_order,
    )
    for task in tasks:
        if not scheduled_task_manager.should_run_on_startup(task.task_key):
            continue
        scheduled_task_manager.trigger_task(
            task.task_key,
            trigger_source="startup",
            triggered_by="system",
            background=True,
            raise_if_running=False,
        )
