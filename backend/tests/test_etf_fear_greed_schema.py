"""etf_fear_greed_clone_history 新增直接分量列的幂等迁移测试。"""
from unittest.mock import patch

from sqlalchemy import create_engine, text

from src.core import database


NEW_COLUMNS = (
    "cnn_fear_greed_score",
    "cnn_fear_greed_raw",
    "szdt_fear_greed_score",
    "szdt_fear_greed_raw",
    "implied_volatility_score",
    "implied_volatility_raw",
)


def test_existing_history_table_auto_adds_direct_component_columns():
    """旧表结构 → 自动补 4 个直接分量列 → 重复执行无副作用。"""
    engine = create_engine("sqlite:///:memory:")
    database.Base.metadata.create_all(engine)

    with engine.begin() as connection:
        for column in NEW_COLUMNS:
            connection.execute(
                text(f"ALTER TABLE etf_fear_greed_clone_history DROP COLUMN {column}")
            )

    with patch.object(database, "engine", engine):
        database.ensure_table_columns()
        database.ensure_table_columns()

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(
                text("PRAGMA table_info(etf_fear_greed_clone_history)")
            )
        }

    for column in NEW_COLUMNS:
        assert column in columns
