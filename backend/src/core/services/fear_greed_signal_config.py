"""自算贪恐底/顶信号统一配置（全局单份）。

星澜壹贰叁号（雪球组合）与自算贪恐历史曲线共用同一套信号参数，存在
``fear_greed_signal_configs`` 表（单行）。读取为独立短事务，返回普通 dict 快照；
调用方不要在长事务内调用。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..database import FearGreedSignalConfig, SessionLocal

# ---- 默认值（表里没有行时兜底） ----

# 均线型
MA5_BOTTOM_SCORE_DEFAULT = 25.0
MA5_TOP_SCORE_DEFAULT = 75.0
MA5_LOOKBACK_DAYS_DEFAULT = 5
# 量能型
VOLUME_BOTTOM_SCORE_DEFAULT = 30.0
VOLUME_TOP_SCORE_DEFAULT = 75.0
VOLUME_EXPAND_STD_DEFAULT = 1.25  # 放量：log 量比 z 高于该标准差
VOLUME_SHRINK_STD_DEFAULT = 0.25  # 缩量：log 量比 z 低于 -该标准差
# 冷却：同类信号（各类型顶/底分别独立）出后 N 个交易日不重复
COOLDOWN_DAYS_DEFAULT = 5


def fear_greed_signal_config_defaults() -> Dict[str, Any]:
    """统一信号配置默认值（无表行时兜底）。"""
    return {
        "ma5_bottom_score": MA5_BOTTOM_SCORE_DEFAULT,
        "ma5_top_score": MA5_TOP_SCORE_DEFAULT,
        "ma5_lookback_days": MA5_LOOKBACK_DAYS_DEFAULT,
        "volume_bottom_score": VOLUME_BOTTOM_SCORE_DEFAULT,
        "volume_top_score": VOLUME_TOP_SCORE_DEFAULT,
        "volume_expand_std": VOLUME_EXPAND_STD_DEFAULT,
        "volume_shrink_std": VOLUME_SHRINK_STD_DEFAULT,
        "cooldown_days": COOLDOWN_DAYS_DEFAULT,
        "updated_at": None,
    }


def load_fear_greed_signal_config() -> Dict[str, Any]:
    """读取统一信号配置（短事务）。表无行/字段缺失时回退默认值。"""
    defaults = fear_greed_signal_config_defaults()
    with SessionLocal() as db:
        row = db.query(FearGreedSignalConfig).first()
        if row is None:
            return defaults
        return {
            **defaults,
            "ma5_bottom_score": float(row.ma5_bottom_score),
            "ma5_top_score": float(row.ma5_top_score),
            "ma5_lookback_days": int(row.ma5_lookback_days),
            "volume_bottom_score": float(row.volume_bottom_score),
            "volume_top_score": float(row.volume_top_score),
            "volume_expand_std": float(row.volume_expand_std),
            "volume_shrink_std": float(row.volume_shrink_std),
            "cooldown_days": int(row.cooldown_days),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


def update_fear_greed_signal_config(
    payload: Dict[str, Any],
    *,
    updated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """更新统一信号配置（短事务，upsert 单行）。返回保存后的完整配置快照。"""
    updates = {
        key: value
        for key, value in payload.items()
        if value is not None and key in fear_greed_signal_config_defaults()
    }
    with SessionLocal() as db:
        row = db.query(FearGreedSignalConfig).first()
        if row is None:
            row = FearGreedSignalConfig()
            db.add(row)
        for key, value in updates.items():
            setattr(row, key, value)
        db.commit()
    # 提交后在新短事务里返回完整快照（避免跨 session 访问 ORM 属性）
    return load_fear_greed_signal_config()
