import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session

from ...core.database import SoxlFearStrategyConfig, SoxlFearStrategyLog, get_db
from ...robot.soxl_fear_strategy_trader import SoxlFearStrategyTrader
from .account import valid_account

router = APIRouter(prefix="/api/soxl-fear-strategy", tags=["soxl-fear-strategy"])
logger = logging.getLogger(__name__)


class SoxlFearStrategyConfigSchema(BaseModel):
    enabled: bool = False
    symbol: str = "SOXL.US"
    account_type: str = "ib"
    ib_account_id: Optional[int] = None
    longport_account_id: Optional[str] = None
    buy_threshold: float = 40.0
    greed_threshold: float = 41.0
    volume_ratio_threshold: float = 1.38
    buy_position_pct: float = 60.0
    cooldown_days: int = 10
    trailing_stop_pct: float = 5.0
    sell_position_pct: float = 50.0
    sell_reduction_basis: str = "portfolio"
    max_take_profit_sells_per_cycle: int = 2
    min_position_pct_after_take_profit: float = 5.0
    rebalance_threshold_pct: float = 5.0
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None
    last_run_message: Optional[str] = None

    @validator("account_type")
    def validate_account_type(cls, value):
        if value not in {"ib", "longport"}:
            raise ValueError("account_type 仅支持 ib 或 longport")
        return value

    @validator("sell_reduction_basis")
    def validate_sell_reduction_basis(cls, value):
        if value not in {"portfolio", "holdings"}:
            raise ValueError("sell_reduction_basis 仅支持 portfolio 或 holdings")
        return value

    @validator(
        "buy_threshold",
        "greed_threshold",
        "volume_ratio_threshold",
        "buy_position_pct",
        "trailing_stop_pct",
        "sell_position_pct",
        "min_position_pct_after_take_profit",
        "rebalance_threshold_pct",
    )
    def validate_percent_like_values(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("cooldown_days")
    def validate_cooldown_days(cls, value):
        if value < 0 or value > 60:
            raise ValueError("cooldown_days 必须在 0 到 60 之间")
        return value

    @validator("max_take_profit_sells_per_cycle")
    def validate_max_take_profit_sells_per_cycle(cls, value):
        if value < 1 or value > 20:
            raise ValueError("max_take_profit_sells_per_cycle 必须在 1 到 20 之间")
        return value

    class Config:
        from_attributes = True


class SoxlFearStrategyLogSchema(BaseModel):
    id: int
    timestamp: datetime
    symbol: str
    trigger_source: str
    action: str
    status: str
    price: Optional[float] = None
    quantity: Optional[int] = None
    fear_score: Optional[float] = None
    volume_ratio: Optional[float] = None
    position_ratio_before: Optional[float] = None
    position_ratio_after: Optional[float] = None
    message: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("/config", response_model=SoxlFearStrategyConfigSchema)
def get_soxl_fear_strategy_config(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = db.query(SoxlFearStrategyConfig).filter(SoxlFearStrategyConfig.account_id == account_id).first()
    if config:
        return config
    return SoxlFearStrategyConfigSchema()


@router.post("/config")
def save_soxl_fear_strategy_config(
    payload: SoxlFearStrategyConfigSchema,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    if payload.enabled:
        if payload.account_type == "ib" and not payload.ib_account_id:
            raise HTTPException(status_code=400, detail="开启策略时必须选择 IB 账户")
        if payload.account_type == "longport" and not payload.longport_account_id:
            raise HTTPException(status_code=400, detail="开启策略时必须选择长桥账户")

    config = db.query(SoxlFearStrategyConfig).filter(SoxlFearStrategyConfig.account_id == account_id).first()
    if not config:
        config = SoxlFearStrategyConfig(account_id=account_id)
        db.add(config)

    for field, value in payload.dict(exclude={"last_run_at", "last_run_status", "last_run_message"}).items():
        setattr(config, field, value)

    config.updated_at = datetime.now()
    db.commit()
    return {"message": "配置已保存"}


@router.get("/logs", response_model=List[SoxlFearStrategyLogSchema])
def get_soxl_fear_strategy_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    return (
        db.query(SoxlFearStrategyLog)
        .filter(SoxlFearStrategyLog.account_id == account_id)
        .order_by(SoxlFearStrategyLog.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )


@router.post("/manual-check")
def manual_check_soxl_fear_strategy(
    account_id: str = Depends(valid_account),
):
    try:
        SoxlFearStrategyTrader().trigger_manual_run(account_id)
        return {"message": "已在后台触发一次 SOXL 情绪量能策略检查"}
    except Exception as exc:
        logger.error("Failed to trigger SOXL fear strategy manually: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
