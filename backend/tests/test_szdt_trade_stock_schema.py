from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core import database


def test_existing_szdt_trade_stocks_default_to_enabled_after_schema_upgrade():
    """旧表自动补 enabled，且存量配置保持启用。"""
    engine = create_engine("sqlite:///:memory:")
    database.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        session.add(database.SzdtTradeStock(
            account_id="test-account",
            code="SH.510300",
            name="沪深300",
            type=3,
            when_buy=-60,
            when_sell=60,
            max_position=5,
            buy_amount=2000,
            sell_amount=2000,
            buy_factor=1,
            sell_factor=1,
            lever=1,
            emo_area="a",
        ))
        session.commit()

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE szdt_trade_stocks DROP COLUMN enabled"))

    with patch.object(database, "engine", engine):
        database.ensure_table_columns()
        database.ensure_table_columns()

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(szdt_trade_stocks)"))
        }
        enabled = connection.execute(
            text("SELECT enabled FROM szdt_trade_stocks WHERE code = 'SH.510300'")
        ).scalar_one()

    assert "enabled" in columns
    assert enabled is True or enabled == 1


def test_existing_szdt_config_auto_adds_external_account_binding_columns():
    """存量策略配置表启动时自动补外部账户和虚拟子账户绑定字段。"""
    engine = create_engine("sqlite:///:memory:")
    database.Base.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE szdt_trading_configs DROP COLUMN external_trading_account_id"
        ))
        connection.execute(text(
            "ALTER TABLE szdt_trading_configs DROP COLUMN live_sub_account_id"
        ))

    with patch.object(database, "engine", engine):
        database.ensure_table_columns()
        database.ensure_table_columns()

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(szdt_trading_configs)"))
        }

    assert {"external_trading_account_id", "live_sub_account_id"}.issubset(columns)
