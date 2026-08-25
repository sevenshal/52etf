"""Single-process orchestration for the rolling all-market minute backfill."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any

from .chan_minute_data import (
    ROLLING_TRADING_DAYS,
    backfill_symbol_minutes,
    prune_minute_history,
    recent_market_universe,
)


class ChanMinuteSyncManager:
    _lock = threading.Lock()
    _state: dict[str, Any] = {
        "status": "IDLE",
        "job_id": None,
        "cancel_requested": False,
        "processed": 0,
        "total": 0,
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
            symbols, start_date, end_date = recent_market_universe(trading_days if full else 1)
            with cls._lock:
                if cls._state.get("job_id") != job_id:
                    return
                cls._state.update(total=len(symbols), start_date=start_date.isoformat(), end_date=end_date.isoformat())
            for symbol in symbols:
                with cls._lock:
                    if cls._state.get("cancel_requested"):
                        cls._state["status"] = "CANCELLED"
                        break
                try:
                    result = backfill_symbol_minutes(symbol, start_date, end_date)
                    saved = int(result.get("saved_rows") or 0)
                    error = None
                except Exception as exc:  # continue the market job and expose a bounded error sample
                    saved = 0
                    error = f"{symbol}: {exc}"
                with cls._lock:
                    cls._state["processed"] += 1
                    cls._state["saved_rows"] += saved
                    if error and len(cls._state["errors"]) < 200:
                        cls._state["errors"].append(error)
            else:
                if full:
                    prune_minute_history(start_date)
                with cls._lock:
                    cls._state["status"] = "SUCCESS" if not cls._state["errors"] else "PARTIAL_SUCCESS"
        except Exception as exc:
            with cls._lock:
                cls._state["status"] = "FAILED"
                cls._state["errors"] = [str(exc)]
        finally:
            with cls._lock:
                if cls._state.get("job_id") == job_id:
                    cls._state["finished_at"] = datetime.now().isoformat()
