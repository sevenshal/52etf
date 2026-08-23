from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .account import valid_admin_account
from ...core.services.fear_greed_signal_config import (
    load_fear_greed_signal_config,
    update_fear_greed_signal_config,
)


router = APIRouter(prefix="/api/fear-greed-signal-config", tags=["fear-greed-signal-config"])


class FearGreedSignalConfigUpdate(BaseModel):
    """自算贪恐底/顶信号统一配置（星澜壹贰叁号与历史曲线共用，全局单份）。"""
    ma5_bottom_score: Optional[float] = Field(None, gt=0, lt=100, description="均线底：MA5由降转升且最近N日任意恐贪≤该值")
    ma5_top_score: Optional[float] = Field(None, gt=0, lt=100, description="均线顶：MA5由升转降且最近N日任意恐贪≥该值")
    ma5_lookback_days: Optional[int] = Field(None, ge=1, le=30, description="MA5信号回看天数（最近N日任意一天触发分数条件）")
    volume_bottom_score: Optional[float] = Field(None, gt=0, lt=100, description="量能底恐贪阈值（恐贪≤该值且放量）")
    volume_top_score: Optional[float] = Field(None, gt=0, lt=100, description="量能顶恐贪阈值（恐贪≥该值且缩量）")
    volume_expand_std: Optional[float] = Field(None, ge=0, description="量能底放量确认：log量比z 需大于该标准差")
    volume_shrink_std: Optional[float] = Field(None, ge=0, description="量能顶缩量确认：log量比z 需小于 -该标准差")
    cooldown_days: Optional[int] = Field(None, ge=0, le=60, description="同类底/顶信号冷却天数（各类型顶/底分别独立）")


@router.get("")
def get_fear_greed_signal_config(_: str = Depends(valid_admin_account)):
    return load_fear_greed_signal_config()


@router.put("")
def save_fear_greed_signal_config(
    payload: FearGreedSignalConfigUpdate,
    account_id: str = Depends(valid_admin_account),
):
    return update_fear_greed_signal_config(payload.dict(exclude_none=True))
