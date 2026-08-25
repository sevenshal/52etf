"""Single-process orchestration for the rolling all-market minute backfill."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any

from .chan_minute_data import (
    ROLLING_TRADING_DAYS,
    fetch_historical_minute_batch,
    historical_minute_batch_size,
    incremental_minute_sync_groups,
    prune_minute_history,
    recent_market_universe,
    upsert_minute_frame,
)
from .tushare import TushareService


CHAN_MINUTE_FETCH_WORKERS = max(1, min(32, int(os.getenv("CHAN_MINUTE_FETCH_WORKERS", "16"))))


class ChanMinuteSyncManager:
    _lock = threading.Lock()
    _state: dict[str, Any] = {
        "status": "IDLE",
        "job_id": None,
        "cancel_requested": False,
        "processed": 0,
        "total": 0,
        "completed_batches": 0,
        "total_batches": 0,
        "fetched_rows": 0,
        "saved_rows": 0,
        "errors": [],
    }

    @classmethod
    def snapshot(cls) -> dict[str, Any]:
        with cls._lock:
            return dict(cls._state, errors=list(cls._state.get("errors") or []))

    @classmethod
    def start(cls, trading_days: int = ROLLING_TRADING_DAYS, full: bool = False) -> dict[str, Any]:
        with cls._lock:
            if cls._state.get("status") == "RUNNING":
                return dict(cls._state, errors=list(cls._state.get("errors") or []))
            job_id = uuid.uuid4().hex
            cls._state = {
                "status": "RUNNING",
                "job_id": job_id,
                "cancel_requested": False,
                "processed": 0,
                "total": 0,
                "completed_batches": 0,
                "total_batches": 0,
                "fetched_rows": 0,
                "saved_rows": 0,
                "errors": [],
                "started_at": datetime.now().isoformat(),
                "finished_at": None,
                "start_date": None,
                "end_date": None,
            }
        threading.Thread(
            target=cls._run,
            args=(job_id, trading_days, full),
            daemon=True,
            name="chan-minute-sync",
        ).start()
        return cls.snapshot()

    @classmethod
    def cancel(cls) -> dict[str, Any]:
        with cls._lock:
            if cls._state.get("status") == "RUNNING":
                cls._state["cancel_requested"] = True
        return cls.snapshot()

    @classmethod
    def _run(cls, job_id: str, trading_days: int, full: bool) -> None:
        try:
            requested_days = min(ROLLING_TRADING_DAYS, max(1, int(trading_days)))
            if full:
                symbols, calendar_start, calendar_end = recent_market_universe(requested_days)
                groups = [
                    {
                        "symbols": symbols,
                        "start_date": calendar_start,
                        "end_date": calendar_end,
                        "run_days": requested_days,
                        "missing_days": requested_days,
                    }
                ]
            else:
                groups, calendar_start, calendar_end = incremental_minute_sync_groups(ROLLING_TRADING_DAYS)

            requests = []
            batch_sizes = {}
            for group in groups:
                run_days = int(group["run_days"])
                batch_size = historical_minute_batch_size(run_days)
                batch_sizes[run_days] = batch_size
                group_symbols = group["symbols"]
                for offset in range(0, len(group_symbols), batch_size):
                    requests.append(
                        {
                            **group,
                            "symbols": group_symbols[offset : offset + batch_size],
                            "batch_size": batch_size,
                        }
                    )

            work_total = sum(len(item["symbols"]) for item in requests)
            unique_symbols = len({symbol for item in requests for symbol in item["symbols"]})
            workers = min(CHAN_MINUTE_FETCH_WORKERS, len(requests)) if requests else 0
            start_date = min((item["start_date"] for item in requests), default=calendar_end)
            end_date = max((item["end_date"] for item in requests), default=calendar_end)
            with cls._lock:
                if cls._state.get("job_id") != job_id:
                    return
                cls._state.update(
                    mode="FULL" if full else "INCREMENTAL",
                    total=work_total,
                    unique_symbols=unique_symbols,
                    total_batches=len(requests),
                    trading_days=requested_days if full else None,
                    max_run_days=max(batch_sizes, default=0),
                    batch_size=next(iter(batch_sizes.values())) if len(batch_sizes) == 1 else None,
                    batch_sizes={str(days): size for days, size in sorted(batch_sizes.items())},
                    fetch_workers=workers,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                )

            cancelled = False
            if requests:
                service = TushareService.get_instance()
                executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chan-minute-fetch")
                request_iter = iter(requests)
                pending = {}

                def submit_until_full() -> None:
                    while len(pending) < workers:
                        try:
                            request = next(request_iter)
                        except StopIteration:
                            return
                        future = executor.submit(
                            fetch_historical_minute_batch,
                            service,
                            request["symbols"],
                            request["start_date"],
                            request["end_date"],
                        )
                        pending[future] = request

                try:
                    submit_until_full()
                    while pending:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            request = pending.pop(future)
                            batch = request["symbols"]
                            with cls._lock:
                                if cls._state.get("cancel_requested"):
                                    cls._state["status"] = "CANCELLED"
                                    cancelled = True
                            if cancelled:
                                break

                            errors = []
                            try:
                                result = future.result()
                                frame = result.get("frame")
                                errors.extend(result.get("errors") or [])
                                fetched = len(frame) if frame is not None else 0
                            except Exception as exc:
                                frame = None
                                fetched = 0
                                errors.append(f"{batch[0]}~{batch[-1]}: {exc}")

                            saved = 0
                            if frame is not None and not frame.empty:
                                try:
                                    # Network workers never access DuckDB; this manager thread is the only writer.
                                    saved = upsert_minute_frame(frame)
                                except Exception as exc:
                                    errors.append(f"{batch[0]}~{batch[-1]} 写入失败: {exc}")

                            with cls._lock:
                                cls._state["processed"] += len(batch)
                                cls._state["completed_batches"] += 1
                                cls._state["fetched_rows"] += fetched
                                cls._state["saved_rows"] += saved
                                remaining_error_slots = max(0, 200 - len(cls._state["errors"]))
                                cls._state["errors"].extend(errors[:remaining_error_slots])
                        if cancelled:
                            break
                        submit_until_full()
                finally:
                    if cancelled:
                        for future in pending:
                            future.cancel()
                    executor.shutdown(wait=True, cancel_futures=cancelled)

            if not cancelled:
                pruned_rows = 0
                if full:
                    try:
                        pruned_rows = prune_minute_history(start_date)
                    except Exception as exc:
                        with cls._lock:
                            if len(cls._state["errors"]) < 200:
                                cls._state["errors"].append(f"清理过期分钟行情失败: {exc}")
                with cls._lock:
                    cls._state["pruned_rows"] = pruned_rows
                    cls._state["status"] = "SUCCESS" if not cls._state["errors"] else "PARTIAL_SUCCESS"
        except Exception as exc:
            with cls._lock:
                cls._state["status"] = "FAILED"
                cls._state["errors"] = [str(exc)]
        finally:
            with cls._lock:
                if cls._state.get("job_id") == job_id:
                    cls._state["finished_at"] = datetime.now().isoformat()
