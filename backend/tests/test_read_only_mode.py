"""``QUANT_DB_READ_ONLY`` 只读模式：开关解析、跳过初始化写库、DuckDB 只读连接。

关键断言都走「子进程 import 一遍库」——因为三个库的初始化写操作都发生在 import 阶段，
只有在新进程里 import 才能真实复现"一 import 就往库里写"的场景。
"""
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
import pytest

from src.core import duckdb_utils
from src.core.analytics_database import AnalyticsBase, ChanScanRun
from src.core.read_only_mode import (
    READ_ONLY_ENV_VAR,
    ReadOnlyModeError,
    is_read_only,
    reset_skip_log,
    skip_init_writes,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def workdir():
    """独立临时目录；不用 pytest 的 tmp_path，避免和同目录其它测试抢基目录。"""
    path = Path(tempfile.mkdtemp(prefix="quant_readonly_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 开关解析
# ---------------------------------------------------------------------------


def test_is_read_only_false_by_default(monkeypatch):
    monkeypatch.delenv(READ_ONLY_ENV_VAR, raising=False)
    assert is_read_only() is False
    for value in ("", " ", "false", "False", "0", "no", "off", "2", "random"):
        monkeypatch.setenv(READ_ONLY_ENV_VAR, value)
        assert is_read_only() is False, value


def test_is_read_only_truthy_values(monkeypatch):
    for value in ("1", "true", "TRUE", " True ", "yes", "y", "on", "t"):
        monkeypatch.setenv(READ_ONLY_ENV_VAR, value)
        assert is_read_only() is True, value


def test_skip_init_writes_returns_false_when_disabled(monkeypatch):
    monkeypatch.delenv(READ_ONLY_ENV_VAR, raising=False)
    reset_skip_log()
    assert skip_init_writes("any step") is False


def test_skip_init_writes_logs_each_step_once(monkeypatch, caplog):
    monkeypatch.setenv(READ_ONLY_ENV_VAR, "true")
    reset_skip_log()
    with caplog.at_level(logging.WARNING, logger="src.core.read_only_mode"):
        assert skip_init_writes("主库补列") is True
        assert skip_init_writes("主库补列") is True
        assert skip_init_writes("主库建索引") is True
    messages = [record.getMessage() for record in caplog.records]
    assert sum("主库补列" in message for message in messages) == 1
    assert sum("主库建索引" in message for message in messages) == 1
    assert all(READ_ONLY_ENV_VAR in message for message in messages)


# ---------------------------------------------------------------------------
# DuckDB 连接行为
# ---------------------------------------------------------------------------


def test_connect_duckdb_for_write_raises_in_read_only_mode(monkeypatch, workdir):
    monkeypatch.setenv(READ_ONLY_ENV_VAR, "true")
    with pytest.raises(ReadOnlyModeError) as excinfo:
        duckdb_utils.connect_duckdb_for_write(str(workdir / "analytics.duckdb"), attempts=1, sleep_seconds=0)
    assert READ_ONLY_ENV_VAR in str(excinfo.value)


def test_connect_duckdb_is_strictly_read_only_in_read_only_mode(monkeypatch, workdir):
    path = str(workdir / "analytics.duckdb")
    seed = duckdb.connect(path)
    seed.execute("CREATE TABLE t (i INTEGER)")
    seed.execute("INSERT INTO t VALUES (1)")
    seed.close()

    monkeypatch.setenv(READ_ONLY_ENV_VAR, "true")
    connection = duckdb_utils.connect_duckdb(path)
    try:
        assert connection.execute("SELECT i FROM t").fetchall() == [(1,)]
        with pytest.raises(Exception):
            connection.execute("CREATE TABLE t2 (i INTEGER)")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 子进程 import：只读模式下不能改库
# ---------------------------------------------------------------------------


def _run_import(module: str, env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        env=env,
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(f"import {module} 失败：\n{result.stderr}")
    return result


def _sqlite_tables(path: str) -> set:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def _duckdb_objects(path: str) -> set:
    connection = duckdb.connect(path, read_only=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
    finally:
        connection.close()


def _columns(path: str, table: str) -> set:
    connection = duckdb.connect(path, read_only=True)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        connection.close()


def _break_analytics_schema(db_path: str) -> None:
    """在已建好的完整 schema 上制造两处"待修复"：缺一个视图、缺一张表。

    不删列是因为 DuckDB 不允许改仍被索引依赖的表（``DependencyException``），
    而删表/删视图都能被正常 import 的 create_all / view_sqls 补回来，同样可观测。
    """
    connection = duckdb.connect(db_path)
    try:
        connection.execute("DROP VIEW a_stock_market_daily_qfq")
        connection.execute("DROP TABLE a_stock_announcement")
    finally:
        connection.close()


def test_import_main_database_in_read_only_mode_creates_nothing(workdir):
    """只读模式：import 主库模块后连 sqlite 文件都不该出现。"""
    db_path = str(workdir / "evc_stocks.db")
    result = _run_import(
        "src.core.database",
        {
            "QUANT_DB_READ_ONLY": "true",
            "QUANT_SQLITE_PATH": db_path,
        },
    )
    assert "只读模式" in (result.stderr or "")
    assert not Path(db_path).exists()


def test_import_main_database_without_flag_creates_tables(workdir):
    """对照组：不开只读模式时行为与改动前一致，建表照常发生。"""
    db_path = str(workdir / "evc_stocks.db")
    _run_import("src.core.database", {"QUANT_SQLITE_PATH": db_path})
    assert len(_sqlite_tables(db_path)) > 50


def test_import_external_trading_database_in_read_only_mode_creates_nothing(workdir):
    db_path = str(workdir / "external_trading.db")
    _run_import(
        "src.core.external_trading_database",
        {
            "QUANT_DB_READ_ONLY": "true",
            "EXTERNAL_TRADING_DB_PATH": db_path,
        },
    )
    assert not Path(db_path).exists()


def test_import_analytics_database_in_read_only_mode_keeps_schema_untouched(workdir):
    """只读模式：库里已有的"待修复"状态原样保留——不建表、不重建视图。"""
    db_path = str(workdir / "analytics.duckdb")
    # 先按正常路径建出完整 schema，再制造两处待修复，确保"没修复"是可观测的
    _run_import("src.core.analytics_database", {"ANALYTICS_DB_PATH": db_path})
    assert "a_stock_market_daily_qfq" in _duckdb_objects(db_path)
    assert "a_stock_announcement" in _duckdb_objects(db_path)
    _break_analytics_schema(db_path)

    result = _run_import(
        "src.core.analytics_database",
        {"QUANT_DB_READ_ONLY": "true", "ANALYTICS_DB_PATH": db_path},
    )

    assert "只读模式" in result.stderr
    assert "a_stock_market_daily_qfq" not in _duckdb_objects(db_path)
    assert "a_stock_announcement" not in _duckdb_objects(db_path)


def test_import_analytics_database_without_flag_repairs_schema(workdir):
    """对照组：同样两处待修复，不开只读模式时会被补回来，说明上面的"没修复"确实来自开关。"""
    db_path = str(workdir / "analytics.duckdb")
    _run_import("src.core.analytics_database", {"ANALYTICS_DB_PATH": db_path})
    _break_analytics_schema(db_path)

    _run_import("src.core.analytics_database", {"ANALYTICS_DB_PATH": db_path})

    assert "a_stock_market_daily_qfq" in _duckdb_objects(db_path)
    assert "a_stock_announcement" in _duckdb_objects(db_path)
    assert "title" in _columns(db_path, "a_stock_announcement")


def test_import_analytics_database_in_read_only_mode_creates_nothing_on_empty_db(workdir):
    """空库 + 只读：连库文件都不该被写入（对比：不开开关时 create_all 会建出上百张表）。"""
    db_path = str(workdir / "analytics.duckdb")
    duckdb.connect(db_path).close()

    _run_import(
        "src.core.analytics_database",
        {"QUANT_DB_READ_ONLY": "true", "ANALYTICS_DB_PATH": db_path},
    )

    assert _duckdb_objects(db_path) == set()

    _run_import("src.core.analytics_database", {"ANALYTICS_DB_PATH": db_path})
    assert "a_stock_basic" in _duckdb_objects(db_path)


# ---------------------------------------------------------------------------
# 运行期建表入口
# ---------------------------------------------------------------------------


def test_snapshot_ensure_tables_skipped_in_read_only_mode(monkeypatch):
    from src.core.services.stock_system import storage

    calls = []
    monkeypatch.setattr(
        AnalyticsBase.metadata, "create_all", lambda *args, **kwargs: calls.append(1)
    )

    monkeypatch.delenv(READ_ONLY_ENV_VAR, raising=False)
    storage.ensure_tables(ChanScanRun)
    assert calls == [1]

    monkeypatch.setenv(READ_ONLY_ENV_VAR, "true")
    storage.ensure_tables(ChanScanRun)
    assert calls == [1], "只读模式下不应再调用 create_all"
