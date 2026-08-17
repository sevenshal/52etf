"""自算贪恐底/顶信号统一配置（fear_greed_signal_configs 表）测试。"""

import pytest

from src.core.database import Base, FearGreedSignalConfig, Session
from src.core.services.fear_greed_signal_config import (
    load_fear_greed_signal_config,
    update_fear_greed_signal_config,
)


@pytest.fixture(autouse=True)
def _clean_config_table():
    Base.metadata.create_all(Session().bind)
    with Session() as db:
        db.query(FearGreedSignalConfig).delete()
        db.commit()
    yield
    with Session() as db:
        db.query(FearGreedSignalConfig).delete()
        db.commit()


def test_defaults_when_no_row():
    config = load_fear_greed_signal_config()
    assert config == {
        "ma5_bottom_score": 25.0,
        "ma5_top_score": 75.0,
        "ma5_lookback_days": 5,
        "volume_bottom_score": 30.0,
        "volume_top_score": 75.0,
        "volume_expand_std": 1.25,
        "volume_shrink_std": 0.25,
        "cooldown_days": 5,
        "updated_at": None,
    }


def test_update_then_load():
    saved = update_fear_greed_signal_config({
        "volume_bottom_score": 33.0,
        "volume_expand_std": 1.5,
        "cooldown_days": 3,
    })
    assert saved["volume_bottom_score"] == 33.0
    assert saved["volume_expand_std"] == 1.5
    assert saved["cooldown_days"] == 3
    # 未更新的字段保持默认
    assert saved["ma5_bottom_score"] == 25.0
    assert saved["volume_top_score"] == 75.0

    # 再次读取仍是最新值
    config = load_fear_greed_signal_config()
    assert config["volume_bottom_score"] == 33.0
    assert config["cooldown_days"] == 3


def test_update_ignores_unknown_and_none():
    saved = update_fear_greed_signal_config({
        "volume_bottom_score": 40.0,
        "not_a_field": 999,
        "cooldown_days": None,
    })
    assert saved["volume_bottom_score"] == 40.0
    assert saved["cooldown_days"] == 5  # None 不更新，保持默认
    assert "not_a_field" not in saved


def test_update_overwrites_previous_row():
    update_fear_greed_signal_config({"volume_top_score": 70.0})
    update_fear_greed_signal_config({"volume_top_score": 80.0, "ma5_lookback_days": 4})
    config = load_fear_greed_signal_config()
    assert config["volume_top_score"] == 80.0
    assert config["ma5_lookback_days"] == 4
    with Session() as db:
        assert db.query(FearGreedSignalConfig).count() == 1
