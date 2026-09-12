"""DuckDB 写连接撞上其它进程的写锁时要等待重试，其它错误照常抛出。"""

import pytest

from src.core import duckdb_utils


def _fake_connect(failures, error_message):
    calls = {"count": 0}

    def connect(database, prefer_read_only=True):
        assert prefer_read_only is False
        calls["count"] += 1
        if calls["count"] <= failures:
            raise IOError(error_message)
        return f"connection:{database}"

    return connect, calls


def test_write_connection_waits_for_lock(monkeypatch):
    connect, calls = _fake_connect(2, 'IO Error: Could not set lock on file "x.duckdb": Conflicting lock')
    monkeypatch.setattr(duckdb_utils, "connect_duckdb", connect)
    sleeps = []

    connection = duckdb_utils.connect_duckdb_for_write("x.duckdb", attempts=5, sleep_seconds=0.1, sleep=sleeps.append)

    assert connection == "connection:x.duckdb"
    assert calls["count"] == 3
    assert sleeps == [0.1, 0.1]


def test_write_connection_gives_up_after_attempts(monkeypatch):
    connect, calls = _fake_connect(10, "Could not set lock on file")
    monkeypatch.setattr(duckdb_utils, "connect_duckdb", connect)

    with pytest.raises(IOError, match="Could not set lock"):
        duckdb_utils.connect_duckdb_for_write("x.duckdb", attempts=3, sleep_seconds=0, sleep=lambda _: None)
    assert calls["count"] == 3


def test_write_connection_does_not_retry_other_errors(monkeypatch):
    connect, calls = _fake_connect(1, "Catalog Error: table missing")
    monkeypatch.setattr(duckdb_utils, "connect_duckdb", connect)

    with pytest.raises(IOError, match="Catalog Error"):
        duckdb_utils.connect_duckdb_for_write("x.duckdb", attempts=5, sleep_seconds=0, sleep=lambda _: None)
    assert calls["count"] == 1
