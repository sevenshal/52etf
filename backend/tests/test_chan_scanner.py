import threading
from datetime import datetime

import pandas as pd

from src.core.services import chan_scanner as scanner_module
from src.core.services.chan_scanner import ChanScanManager, _rows_by_symbol


class _FakeConn:
    """Stand-in for a DuckDB connection when the batch loaders are mocked out."""

    def execute(self, *_args, **_kwargs):
        return self

    def fetchdf(self):
        return pd.DataFrame()

    def fetchall(self):
        return []

    def register(self, *_args, **_kwargs):
        pass

    def close(self):
        pass


def test_rows_by_symbol_groups_frame_and_fills_missing_symbols():
    frame = pd.DataFrame(
        [
            {"ts_code": "A", "close": 1},
            {"ts_code": "A", "close": 2},
            {"ts_code": "C", "close": 3},
        ]
    )
    out = _rows_by_symbol(frame, ["A", "B", "C"])
    assert [row["close"] for row in out["A"]] == [1, 2]
    assert out["B"] == []
    assert [row["close"] for row in out["C"]] == [3]
    assert "ts_code" not in out["A"][0]


def _one_buy_signal(*_args, **_kwargs):
    return {
        "signals": [
            {
                "type": "一买", "detail": "d", "key": "k", "value": "v",
                "confirmed": True, "bar_time": "2026-08-25T09:30:00", "name": "native_chan",
            }
        ]
    }


def test_run_loads_in_chunks_and_finishes_success(monkeypatch):
    monkeypatch.setattr(scanner_module, "SCAN_CHUNK", 2)
    monkeypatch.setattr(scanner_module, "connect_duckdb", lambda *_a, **_k: _FakeConn())
    candidates = [{"ts_code": f"{i:06d}.SZ", "name": f"n{i}"} for i in range(5)]
    monkeypatch.setattr(scanner_module, "filter_stock_pool", lambda _filters, connection=None: candidates)

    loaded_chunks: list[list[str]] = []

    def fake_batch(_conn, symbols):
        loaded_chunks.append(list(symbols))
        bar = {"timestamp": datetime(2026, 8, 25, 9, 30), "open": 1, "high": 1, "low": 1, "close": 1}
        return {symbol: [bar] * 30 for symbol in symbols}

    monkeypatch.setattr(scanner_module, "_batch_load_minute", fake_batch)
    monkeypatch.setattr(scanner_module, "analyze_bars", _one_buy_signal)

    writes: list[dict] = []
    monkeypatch.setattr(scanner_module, "_write_run", lambda _run_id, **updates: writes.append(updates))
    monkeypatch.setattr(ChanScanManager, "_active", {"job"})
    monkeypatch.setattr(ChanScanManager, "_cancelled", set())
    monkeypatch.setattr(ChanScanManager, "_progress", {})

    ChanScanManager._run("job", "1m", {}, "buy", False)

    assert loaded_chunks == [
        ["000000.SZ", "000001.SZ"], ["000002.SZ", "000003.SZ"], ["000004.SZ"],
    ]
    assert writes[-1]["status"] == "SUCCESS"
    assert writes[-1]["processed_count"] == 5
    assert writes[-1]["signal_count"] == 5


def test_run_skips_symbols_without_enough_bars(monkeypatch):
    monkeypatch.setattr(scanner_module, "connect_duckdb", lambda *_a, **_k: _FakeConn())
    monkeypatch.setattr(
        scanner_module, "filter_stock_pool",
        lambda _filters, connection=None: [{"ts_code": "000001.SZ", "name": "n"}],
    )
    monkeypatch.setattr(scanner_module, "_batch_load_minute", lambda _c, symbols: {symbols[0]: [{"close": 1}] * 3})
    called = {"n": 0}
    monkeypatch.setattr(scanner_module, "analyze_bars", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"signals": []})
    writes: list[dict] = []
    monkeypatch.setattr(scanner_module, "_write_run", lambda _run_id, **updates: writes.append(updates))
    monkeypatch.setattr(ChanScanManager, "_active", {"job"})
    monkeypatch.setattr(ChanScanManager, "_cancelled", set())
    monkeypatch.setattr(ChanScanManager, "_progress", {})

    ChanScanManager._run("job", "1m", {}, "buy", False)

    assert called["n"] == 0  # never analyzed: only 3 bars < MIN_BARS_FOR_ANALYSIS
    assert writes[-1]["status"] == "SUCCESS"
    assert writes[-1]["processed_count"] == 1
    assert writes[-1]["error_count"] == 0


def test_watchdog_marks_run_failed_when_progress_stalls(monkeypatch):
    monkeypatch.setattr(scanner_module, "STALL_SECONDS", 1)
    writes: list[dict] = []
    monkeypatch.setattr(scanner_module, "_write_run", lambda _run_id, **updates: writes.append(updates))
    monkeypatch.setattr(ChanScanManager, "_cancelled", set())
    monkeypatch.setattr(
        ChanScanManager, "_progress", {"job": (7, scanner_module._time.monotonic() - 999)}
    )
    stop = threading.Event()
    worker = threading.Thread(target=ChanScanManager._watchdog, args=("job", stop, 0.01))
    worker.start()
    worker.join(timeout=2)
    stop.set()

    assert not worker.is_alive()
    assert writes and writes[-1]["status"] == "FAILED"
    assert writes[-1]["processed_count"] == 7
    assert "job" in ChanScanManager._cancelled


def test_reap_active_drops_terminal_ids(monkeypatch):
    class _Conn(_FakeConn):
        def fetchall(self):
            return [("done", "SUCCESS"), ("stuck", "RUNNING")]

    monkeypatch.setattr(scanner_module, "connect_duckdb", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(ChanScanManager, "_active", {"done", "stuck"})
    monkeypatch.setattr(ChanScanManager, "_progress", {"done": (1, 0.0), "stuck": (1, 0.0)})

    ChanScanManager._reap_active()

    assert ChanScanManager._active == {"stuck"}
    assert "done" not in ChanScanManager._progress


def test_realtime_scan_passes_live_rows_without_persisting(monkeypatch):
    live_row = {"timestamp": datetime(2026, 8, 25, 14, 59), "open": 1, "high": 1, "low": 1, "close": 10.25}
    monkeypatch.setattr(scanner_module, "connect_duckdb", lambda *_a, **_k: _FakeConn())
    monkeypatch.setattr(
        scanner_module, "filter_stock_pool",
        lambda _filters, connection=None: [{"ts_code": "000001.SZ", "name": "n"}],
    )
    realtime_calls: list = []
    monkeypatch.setattr(
        scanner_module, "fetch_realtime_minute_rows",
        lambda symbols, freq: realtime_calls.append((symbols, freq)) or {"000001.SZ": [live_row]},
    )
    hist_bar = {"timestamp": datetime(2026, 8, 25, 14, 55), "open": 1, "high": 1, "low": 1, "close": 1}
    monkeypatch.setattr(scanner_module, "_batch_load_minute", lambda _c, symbols: {symbols[0]: [hist_bar] * 30})
    merged = {}
    monkeypatch.setattr(
        scanner_module, "merge_minute_rows",
        lambda hist, rt: merged.setdefault("args", (hist, rt)) or ([*hist, *rt]),
    )
    monkeypatch.setattr(scanner_module, "aggregate_minute_rows", lambda _s, rows, _f: rows)
    seen_rows = {}
    monkeypatch.setattr(
        scanner_module, "analyze_bars",
        lambda _symbol, rows, *_a, **_k: seen_rows.setdefault("rows", rows) or {"signals": []},
    )
    writes: list[dict] = []
    monkeypatch.setattr(scanner_module, "_write_run", lambda _run_id, **updates: writes.append(updates))
    monkeypatch.setattr(ChanScanManager, "_active", {"job"})
    monkeypatch.setattr(ChanScanManager, "_cancelled", set())
    monkeypatch.setattr(ChanScanManager, "_progress", {})

    ChanScanManager._run("job", "5m", {}, "buy", True)

    assert realtime_calls == [(["000001.SZ"], "5MIN")]
    assert merged["args"][1] == [live_row]
    assert any(updates.get("status") == "SUCCESS" for updates in writes)


def test_run_forwards_engine_choice_to_analyze_bars(monkeypatch):
    monkeypatch.setattr(scanner_module, "connect_duckdb", lambda *_a, **_k: _FakeConn())
    monkeypatch.setattr(
        scanner_module, "filter_stock_pool",
        lambda _filters, connection=None: [{"ts_code": "000001.SZ", "name": "n"}],
    )
    bar = {"timestamp": datetime(2026, 8, 25, 9, 30), "open": 1, "high": 1, "low": 1, "close": 1}
    monkeypatch.setattr(scanner_module, "_batch_load_minute", lambda _c, symbols: {symbols[0]: [bar] * 30})
    seen = {}

    def fake_analyze(symbol, rows, freq, **kwargs):
        seen["engine"] = kwargs.get("engine")
        return {"signals": []}

    monkeypatch.setattr(scanner_module, "analyze_bars", fake_analyze)
    monkeypatch.setattr(scanner_module, "_write_run", lambda *_a, **_k: None)
    monkeypatch.setattr(ChanScanManager, "_active", {"job"})
    monkeypatch.setattr(ChanScanManager, "_cancelled", set())
    monkeypatch.setattr(ChanScanManager, "_progress", {})

    ChanScanManager._run("job", "1m", {}, "buy", False, "czsc")
    assert seen["engine"] == "czsc"
