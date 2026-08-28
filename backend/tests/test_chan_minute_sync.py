import threading
from datetime import date

import pandas as pd

from src.core.services import chan_minute_sync as sync_module
from src.core.services.chan_minute_sync import ChanMinuteSyncManager


def _running_state(job_id):
    return {
        "status": "RUNNING",
        "job_id": job_id,
        "cancel_requested": False,
        "processed": 0,
        "total": 0,
        "completed_batches": 0,
        "total_batches": 0,
        "fetched_rows": 0,
        "saved_rows": 0,
        "request_count": 0,
        "errors": [],
    }


def test_full_sync_fetches_requested_days_concurrently_and_writes_on_manager_thread(monkeypatch):
    symbols = ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH"]
    manager_thread = threading.current_thread().name
    fetch_threads = []
    write_threads = []
    prune_calls = []
    barrier = threading.Barrier(len(symbols))

    monkeypatch.setattr(
        sync_module,
        "recent_market_universe",
        lambda trading_days: (symbols, date(2026, 7, 10), date(2026, 8, 24)),
    )
    monkeypatch.setattr(sync_module, "CHAN_MINUTE_FETCH_WORKERS", len(symbols))
    monkeypatch.setattr(sync_module.TushareService, "get_instance", classmethod(lambda cls: object()))

    def fake_fetch(_service, batch, _start_date, _end_date):
        fetch_threads.append(threading.current_thread().name)
        barrier.wait(timeout=2)
        return {"frame": pd.DataFrame([{"ts_code": batch[0]}]), "errors": []}

    def fake_upsert(frame):
        write_threads.append(threading.current_thread().name)
        return len(frame)

    def fake_prune(keep_from):
        prune_calls.append((keep_from, threading.current_thread().name))
        return 7

    monkeypatch.setattr(sync_module, "fetch_historical_minute_batch", fake_fetch)
    monkeypatch.setattr(sync_module, "upsert_minute_frame", fake_upsert)
    monkeypatch.setattr(sync_module, "prune_minute_history", fake_prune)
    monkeypatch.setattr(ChanMinuteSyncManager, "_state", _running_state("full-job"))

    ChanMinuteSyncManager._run("full-job", trading_days=32, full=True)
    state = ChanMinuteSyncManager.snapshot()

    assert state["status"] == "SUCCESS"
    assert state["trading_days"] == 32
    assert state["batch_size"] == 1
    assert state["processed"] == len(symbols)
    assert state["total_batches"] == len(symbols)
    assert state["completed_batches"] == len(symbols)
    assert len(set(fetch_threads)) > 1
    assert all(name.startswith("chan-minute-fetch") for name in fetch_threads)
    assert 1 <= len(write_threads) <= len(symbols)
    assert all(name == manager_thread for name in write_threads)
    assert prune_calls == [(date(2026, 7, 10), manager_thread)]
    assert state["pruned_rows"] == 7


def test_full_sync_caps_manual_backfill_at_128_days(monkeypatch):
    monkeypatch.setattr(sync_module, "recent_market_universe", lambda trading_days: ([], date(2026, 1, 1), date(2026, 8, 24)))
    monkeypatch.setattr(sync_module, "prune_minute_history", lambda _keep_from: 0)
    monkeypatch.setattr(ChanMinuteSyncManager, "_state", _running_state("cap-job"))
    ChanMinuteSyncManager._run("cap-job", trading_days=999, full=True)
    state = ChanMinuteSyncManager.snapshot()
    assert state["status"] == "SUCCESS"
    assert state["trading_days"] == 128


def test_incremental_sync_adds_one_day_and_uses_sixteen_symbol_batches(monkeypatch):
    symbols = [f"{index:06d}.SZ" for index in range(1, 21)]
    fetched_batches = []
    prune_calls = []

    def fake_fetch(_service, batch, start_date, end_date):
        fetched_batches.append((batch, start_date, end_date))
        return {"frame": pd.DataFrame([{"ts_code": symbol} for symbol in batch]), "errors": []}

    monkeypatch.setattr(
        sync_module,
        "incremental_minute_sync_groups",
        lambda trading_days: (
            [
                {
                    "symbols": symbols,
                    "start_date": date(2026, 8, 21),
                    "end_date": date(2026, 8, 24),
                    "run_days": 2,
                    "missing_days": 1,
                }
            ],
            date(2026, 7, 10),
            date(2026, 8, 24),
        ),
    )
    monkeypatch.setattr(sync_module, "CHAN_MINUTE_FETCH_WORKERS", 2)
    monkeypatch.setattr(sync_module.TushareService, "get_instance", classmethod(lambda cls: object()))
    monkeypatch.setattr(sync_module, "fetch_historical_minute_batch", fake_fetch)
    monkeypatch.setattr(sync_module, "upsert_minute_frame", len)
    monkeypatch.setattr(sync_module, "prune_minute_history", lambda keep_from: prune_calls.append(keep_from) or 0)
    monkeypatch.setattr(ChanMinuteSyncManager, "_state", _running_state("daily-job"))

    ChanMinuteSyncManager._run("daily-job", trading_days=32, full=False)
    state = ChanMinuteSyncManager.snapshot()

    assert state["status"] == "SUCCESS"
    assert state["mode"] == "INCREMENTAL"
    assert state["trading_days"] is None
    assert state["max_run_days"] == 2
    assert state["batch_size"] == 16
    assert state["batch_sizes"] == {"2": 16}
    assert state["total_batches"] == 2
    assert sorted(len(batch) for batch, _, _ in fetched_batches) == [4, 16]
    assert all(start == date(2026, 8, 21) for _, start, _ in fetched_batches)
    assert all(end == date(2026, 8, 24) for _, _, end in fetched_batches)
    assert state["processed"] == 20
    assert prune_calls == []


def test_incremental_sync_skips_network_when_no_dates_are_missing(monkeypatch):
    monkeypatch.setattr(
        sync_module,
        "incremental_minute_sync_groups",
        lambda trading_days: ([], date(2026, 7, 10), date(2026, 8, 24)),
    )
    monkeypatch.setattr(
        sync_module.TushareService,
        "get_instance",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("network should not run"))),
    )
    monkeypatch.setattr(ChanMinuteSyncManager, "_state", _running_state("no-work-job"))

    ChanMinuteSyncManager._run("no-work-job", trading_days=32, full=False)
    state = ChanMinuteSyncManager.snapshot()

    assert state["status"] == "SUCCESS"
    assert state["total"] == 0
    assert state["total_batches"] == 0
    assert state["fetch_workers"] == 0
