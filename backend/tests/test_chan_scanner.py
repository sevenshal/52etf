from datetime import datetime

from src.core.services import chan_scanner as scanner_module
from src.core.services.chan_scanner import ChanScanManager, _load_scan_rows


def test_load_scan_rows_aggregates_history_then_merges_target_frequency_realtime_bar(monkeypatch):
    historical = [
        {
            "timestamp": datetime(2026, 8, 25, 14, 55),
            "open": 10,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 100,
            "turnover": 1000,
        },
        {
            "timestamp": datetime(2026, 8, 25, 15, 0),
            "open": 10.1,
            "high": 10.2,
            "low": 10,
            "close": 10.05,
            "volume": 90,
            "turnover": 900,
        },
    ]
    realtime = [
        {
            "timestamp": datetime(2026, 8, 25, 15, 0),
            "open": 10.1,
            "high": 10.3,
            "low": 10,
            "close": 10.25,
            "volume": 120,
            "turnover": 1200,
        }
    ]
    captured = {}
    monkeypatch.setattr(scanner_module, "load_minute_rows", lambda *_args, **_kwargs: historical)

    def fake_aggregate(_symbol, rows, _freq):
        captured["history"] = rows
        return rows

    monkeypatch.setattr(scanner_module, "aggregate_minute_rows", fake_aggregate)

    result = _load_scan_rows("000001.SZ", "5m", realtime)

    assert len(result) == 2
    assert captured["history"][-1]["close"] == 10.05
    assert result[-1]["close"] == 10.25


def test_realtime_scan_fetches_batch_data_and_passes_it_without_persisting(monkeypatch):
    live_row = {"timestamp": datetime(2026, 8, 25, 14, 59), "close": 10.25}
    load_calls = []
    realtime_calls = []
    write_calls = []
    monkeypatch.setattr(
        scanner_module,
        "filter_stock_pool",
        lambda _filters: [{"ts_code": "000001.SZ"}],
    )
    monkeypatch.setattr(
        scanner_module,
        "fetch_realtime_minute_rows",
        lambda symbols, freq: realtime_calls.append((symbols, freq)) or {"000001.SZ": [live_row]},
    )

    def fake_load(symbol, freq, realtime_rows=None):
        load_calls.append((symbol, freq, realtime_rows))
        return [{"timestamp": live_row["timestamp"]}]

    monkeypatch.setattr(scanner_module, "_load_scan_rows", fake_load)
    monkeypatch.setattr(
        scanner_module,
        "analyze_bars",
        lambda *_args, **_kwargs: {"signals": []},
    )
    monkeypatch.setattr(scanner_module, "_write_run", lambda run_id, **updates: write_calls.append((run_id, updates)))
    monkeypatch.setattr(ChanScanManager, "_active", {"scan-job"})
    monkeypatch.setattr(ChanScanManager, "_cancelled", set())

    ChanScanManager._run("scan-job", "5m", {}, "buy", True)

    assert realtime_calls == [(["000001.SZ"], "5MIN")]
    assert load_calls == [("000001.SZ", "5m", [live_row])]
    assert any(updates.get("status") == "SUCCESS" for _, updates in write_calls)
