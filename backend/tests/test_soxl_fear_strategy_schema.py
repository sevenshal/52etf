from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

import src.core.database as database

LOG_TABLE_SQL = (
    "CREATE TABLE soxl_fear_strategy_logs (id INTEGER PRIMARY KEY, account_id VARCHAR, "
    "symbol VARCHAR, config_id INTEGER)"
)
CONFIG_TABLE_SQL = (
    "CREATE TABLE soxl_fear_strategy_configs (id INTEGER PRIMARY KEY, account_id VARCHAR, "
    "symbol VARCHAR, account_type VARCHAR, trading_account_id VARCHAR, external_trading_account_id INTEGER, "
    "live_sub_account_id INTEGER, created_at DATETIME)"
)


def _run_schema_upgrade(engine):
    with patch.object(database, "engine", engine):
        database.ensure_soxl_fear_strategy_multi_config_schema()
        database.ensure_soxl_fear_strategy_multi_config_schema()  # 幂等


@pytest.mark.parametrize(
    "state_table_sql",
    [
        # 多配置结构但缺 pending_sell_signal_date：只补列
        "CREATE TABLE soxl_fear_strategy_states (config_id INTEGER NOT NULL PRIMARY KEY, "
        "account_id VARCHAR, symbol VARCHAR, last_processed_date DATE, "
        "cooldown_remaining_days INTEGER NOT NULL DEFAULT 0, greed_peak_price FLOAT, "
        "take_profit_cycle_sell_count INTEGER NOT NULL DEFAULT 0, updated_at DATETIME)",
        # 单账户老结构：重建表，重建后也要带上挂起列
        "CREATE TABLE soxl_fear_strategy_states (account_id VARCHAR NOT NULL, symbol VARCHAR, "
        "last_processed_date DATE, cooldown_remaining_days INTEGER, greed_peak_price FLOAT, "
        "take_profit_cycle_sell_count INTEGER, updated_at DATETIME, PRIMARY KEY (account_id))",
    ],
    ids=["add_column", "rebuild"],
)
def test_schema_upgrade_adds_pending_sell_signal_date(tmp_path, state_table_sql):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text(CONFIG_TABLE_SQL))
        conn.execute(text(
            "INSERT INTO soxl_fear_strategy_configs (id, account_id, symbol, trading_account_id, created_at) "
            "VALUES (2, 'acc', 'SOXL.US', 'U1', CURRENT_TIMESTAMP)"
        ))
        conn.execute(text(LOG_TABLE_SQL))
        conn.execute(text(state_table_sql))
        if "config_id" in state_table_sql:
            conn.execute(text(
                "INSERT INTO soxl_fear_strategy_states (config_id, account_id, symbol, take_profit_cycle_sell_count) "
                "VALUES (2, 'acc', 'SOXL.US', 1)"
            ))
        else:
            conn.execute(text(
                "INSERT INTO soxl_fear_strategy_states (account_id, symbol, take_profit_cycle_sell_count) "
                "VALUES ('acc', 'SOXL.US', 1)"
            ))

    _run_schema_upgrade(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(soxl_fear_strategy_states)"))}
        row = conn.execute(text(
            "SELECT config_id, take_profit_cycle_sell_count, pending_sell_signal_date "
            "FROM soxl_fear_strategy_states"
        )).one()
    assert "pending_sell_signal_date" in columns
    # 存量状态保留，补列后没有挂起的卖出信号
    assert tuple(row) == (2, 1, None)

    # ORM 查询（实盘 run_config_once 的读法）在升级后的库上可用
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        state = session.query(database.SoxlFearStrategyState).filter(
            database.SoxlFearStrategyState.config_id == 2
        ).first()
        assert state is not None and state.pending_sell_signal_date is None
