import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from ...core.database import (
    ValuationSimConfig,
    ValuationSimEquity,
    ValuationSimLog,
    ValuationSimPendingOrder,
    ValuationSimPosition,
    ValuationSimTrade,
    get_db,
)
from ...core.services.valuation_simulator import (
    ValuationSimulationService,
    process_enabled_valuation_simulations,
)
from .account import valid_account

router = APIRouter(prefix="/api/valuation-sim", tags=["valuation-sim"])
logger = logging.getLogger(__name__)


class ValuationSimConfigPayload(BaseModel):
    name: str = "纳指100估值成长模拟盘"
    enabled: bool = False
    universe_tag_ids: List[str] = Field(default_factory=list)
    min_market_cap_100m: Optional[float] = 100.0
    max_market_cap_100m: Optional[float] = None
    initial_cash: float = 100000.0
    max_positions: int = 5
    trigger_time: str = "18:00"
    trigger_timezone: str = "America/New_York"
    undervalue_threshold: float = 0.9
    next_fy_growth_threshold: float = 1.1
    ema_window: int = 120
    price_below_ema_pct: float = 10.0
    volume_lookback_days: int = 20
    volume_consecutive_days: int = 3
    volume_ratio_threshold: float = 1.4
    trailing_stop_pct: float = 5.0
    trailing_stop_atr_window: int = 20
    trailing_stop_atr_multiple: float = 2.5
    stale_high_days: int = 5

    @validator("name")
    def validate_name(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("名称不能为空")
        return text[:120]

    @validator("trigger_time")
    def validate_trigger_time(cls, value):
        text = str(value or "").strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError("触发时间格式必须为 HH:MM")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("触发时间格式必须为 HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @validator("trigger_timezone")
    def validate_timezone(cls, value):
        text = str(value or "").strip()
        return text or "America/New_York"

    @validator("universe_tag_ids", pre=True, always=True)
    def validate_universe_tag_ids(cls, value):
        if not value:
            return []
        if not isinstance(value, list):
            raise ValueError("候选池标签必须是列表")
        result = []
        for item in value:
            tag_id = str(item or "").strip()
            if tag_id and tag_id not in result:
                result.append(tag_id)
        return result

    @validator("min_market_cap_100m", "max_market_cap_100m", pre=True)
    def validate_market_cap_filter(cls, value):
        if value is None or value == "":
            return None
        number = float(value)
        if number < 0:
            raise ValueError("市值范围不能小于 0")
        return number

    @validator("initial_cash")
    def validate_initial_cash(cls, value):
        if value <= 0:
            raise ValueError("初始资金必须大于 0")
        return value

    @validator("max_positions")
    def validate_max_positions(cls, value):
        if value < 1 or value > 50:
            raise ValueError("持仓数必须在 1 到 50 之间")
        return value

    @validator("ema_window", "volume_lookback_days", "volume_consecutive_days", "trailing_stop_atr_window", "stale_high_days")
    def validate_positive_int(cls, value):
        if value < 1 or value > 1000:
            raise ValueError("窗口参数必须在 1 到 1000 之间")
        return value

    @validator(
        "undervalue_threshold",
        "next_fy_growth_threshold",
        "price_below_ema_pct",
        "volume_ratio_threshold",
        "trailing_stop_pct",
        "trailing_stop_atr_multiple",
    )
    def validate_positive_float(cls, value):
        if value <= 0:
            raise ValueError("阈值参数必须大于 0")
        return value


class ValuationSimConfigSchema(ValuationSimConfigPayload):
    id: int
    account_id: Optional[str] = None
    current_cash: float = 0.0
    last_run_at: Optional[datetime] = None
    last_run_date: Optional[date] = None
    last_run_status: Optional[str] = None
    last_run_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ValuationSimPositionSchema(BaseModel):
    id: int
    symbol: str
    quantity: float
    avg_cost: float
    cost_basis: float
    highest_price: Optional[float] = None
    highest_price_date: Optional[date] = None
    days_without_high: int
    opened_trade_date: Optional[date] = None
    last_price: Optional[float] = None
    last_market_value: Optional[float] = None
    last_trade_date: Optional[date] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ValuationSimTradeSchema(BaseModel):
    id: int
    timestamp: datetime
    trade_date: Optional[date] = None
    symbol: str
    action: str
    price: Optional[float] = None
    quantity: Optional[float] = None
    amount: Optional[float] = None
    cash_after: Optional[float] = None
    realized_pnl: Optional[float] = None
    reason: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    message: Optional[str] = None

    class Config:
        from_attributes = True


class ValuationSimPendingOrderSchema(BaseModel):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    signal_date: Optional[date] = None
    execution_date: Optional[date] = None
    symbol: str
    action: str
    status: str
    reason: Optional[str] = None
    signal_price: Optional[float] = None
    execution_price: Optional[float] = None
    quantity: Optional[float] = None
    amount: Optional[float] = None
    realized_pnl: Optional[float] = None
    priority: Optional[int] = None
    metrics: Optional[Dict[str, Any]] = None
    message: Optional[str] = None

    class Config:
        from_attributes = True


class ValuationSimEquitySchema(BaseModel):
    id: int
    trade_date: Optional[date] = None
    cash: Optional[float] = None
    position_value: Optional[float] = None
    total_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    position_count: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ValuationSimLogSchema(BaseModel):
    id: int
    timestamp: datetime
    trigger_source: str
    status: str
    action: str
    trade_date: Optional[date] = None
    candidate_count: Optional[int] = None
    buy_count: Optional[int] = None
    sell_count: Optional[int] = None
    total_equity: Optional[float] = None
    message: Optional[str] = None

    class Config:
        from_attributes = True


CONFIG_FIELDS = [
    "name",
    "enabled",
    "universe_tag_ids",
    "min_market_cap_100m",
    "max_market_cap_100m",
    "initial_cash",
    "max_positions",
    "trigger_time",
    "trigger_timezone",
    "undervalue_threshold",
    "next_fy_growth_threshold",
    "ema_window",
    "price_below_ema_pct",
    "volume_lookback_days",
    "volume_consecutive_days",
    "volume_ratio_threshold",
    "trailing_stop_pct",
    "trailing_stop_atr_window",
    "trailing_stop_atr_multiple",
    "stale_high_days",
]


def _get_config_or_404(db: Session, account_id: str, config_id: int) -> ValuationSimConfig:
    config = (
        db.query(ValuationSimConfig)
        .filter(ValuationSimConfig.id == config_id, ValuationSimConfig.account_id == account_id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="未找到估值模拟盘配置")
    return config


def _apply_payload(config: ValuationSimConfig, payload: ValuationSimConfigPayload, reset_cash_if_new: bool = False):
    payload_data = payload.dict()
    min_market_cap = payload_data.get("min_market_cap_100m")
    max_market_cap = payload_data.get("max_market_cap_100m")
    if min_market_cap is not None and max_market_cap is not None and min_market_cap > max_market_cap:
        raise HTTPException(status_code=400, detail="市值下限不能大于上限")
    for field in CONFIG_FIELDS:
        setattr(config, field, payload_data[field])
    if reset_cash_if_new:
        config.current_cash = payload.initial_cash
    config.updated_at = datetime.now()


@router.get("/configs", response_model=List[ValuationSimConfigSchema])
def list_valuation_sim_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    return (
        db.query(ValuationSimConfig)
        .filter(ValuationSimConfig.account_id == account_id)
        .order_by(ValuationSimConfig.updated_at.desc(), ValuationSimConfig.id.desc())
        .all()
    )


@router.post("/configs", response_model=ValuationSimConfigSchema)
def create_valuation_sim_config(
    payload: ValuationSimConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = ValuationSimConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload(config, payload, reset_cash_if_new=True)
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.get("/configs/{config_id}", response_model=ValuationSimConfigSchema)
def get_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    return _get_config_or_404(db, account_id, config_id)


@router.put("/configs/{config_id}", response_model=ValuationSimConfigSchema)
def update_valuation_sim_config(
    config_id: int,
    payload: ValuationSimConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _apply_payload(config, payload)
    db.commit()
    db.refresh(config)
    return config


@router.delete("/configs/{config_id}")
def delete_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    db.query(ValuationSimPosition).filter(ValuationSimPosition.config_id == config.id).delete()
    db.query(ValuationSimPendingOrder).filter(ValuationSimPendingOrder.config_id == config.id).delete()
    db.query(ValuationSimTrade).filter(ValuationSimTrade.config_id == config.id).delete()
    db.query(ValuationSimEquity).filter(ValuationSimEquity.config_id == config.id).delete()
    db.query(ValuationSimLog).filter(ValuationSimLog.config_id == config.id).delete()
    db.delete(config)
    db.commit()
    return {"message": "配置已删除"}


@router.post("/configs/{config_id}/reset", response_model=ValuationSimConfigSchema)
def reset_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    db.query(ValuationSimPosition).filter(ValuationSimPosition.config_id == config.id).delete()
    db.query(ValuationSimPendingOrder).filter(ValuationSimPendingOrder.config_id == config.id).delete()
    db.query(ValuationSimTrade).filter(ValuationSimTrade.config_id == config.id).delete()
    db.query(ValuationSimEquity).filter(ValuationSimEquity.config_id == config.id).delete()
    db.query(ValuationSimLog).filter(ValuationSimLog.config_id == config.id).delete()
    config.current_cash = config.initial_cash
    config.last_run_at = None
    config.last_run_date = None
    config.last_run_status = None
    config.last_run_message = None
    config.updated_at = datetime.now()
    db.commit()
    db.refresh(config)
    return config


@router.post("/configs/{config_id}/run")
def run_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    try:
        result = ValuationSimulationService(db).run_config(config, trigger_source="manual")
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        logger.exception("Manual valuation simulation run failed, config_id=%s", config_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/configs/{config_id}/positions", response_model=List[ValuationSimPositionSchema])
def list_valuation_sim_positions(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    return (
        db.query(ValuationSimPosition)
        .filter(ValuationSimPosition.config_id == config.id)
        .order_by(ValuationSimPosition.last_market_value.desc(), ValuationSimPosition.id.asc())
        .all()
    )


@router.get("/configs/{config_id}/pending-orders", response_model=List[ValuationSimPendingOrderSchema])
def list_valuation_sim_pending_orders(
    config_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=300),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    return (
        db.query(ValuationSimPendingOrder)
        .filter(ValuationSimPendingOrder.config_id == config.id)
        .order_by(
            ValuationSimPendingOrder.status.asc(),
            ValuationSimPendingOrder.signal_date.desc(),
            ValuationSimPendingOrder.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )


@router.get("/configs/{config_id}/trades", response_model=List[ValuationSimTradeSchema])
def list_valuation_sim_trades(
    config_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    return (
        db.query(ValuationSimTrade)
        .filter(ValuationSimTrade.config_id == config.id)
        .order_by(ValuationSimTrade.timestamp.desc(), ValuationSimTrade.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )


@router.get("/configs/{config_id}/logs", response_model=List[ValuationSimLogSchema])
def list_valuation_sim_logs(
    config_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    return (
        db.query(ValuationSimLog)
        .filter(ValuationSimLog.config_id == config.id)
        .order_by(ValuationSimLog.timestamp.desc(), ValuationSimLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )


@router.get("/configs/{config_id}/equity", response_model=List[ValuationSimEquitySchema])
def list_valuation_sim_equity(
    config_id: int,
    limit: int = Query(120, ge=1, le=1000),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    rows = (
        db.query(ValuationSimEquity)
        .filter(ValuationSimEquity.config_id == config.id)
        .order_by(ValuationSimEquity.trade_date.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


@router.get("/configs/{config_id}/candidates")
def preview_valuation_sim_candidates(
    config_id: int,
    limit: int = Query(50, ge=1, le=200),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    try:
        return ValuationSimulationService(db).preview_candidates(config, limit=limit)
    except Exception as exc:
        logger.exception("Valuation simulation candidate preview failed, config_id=%s", config_id)
        raise HTTPException(status_code=500, detail=str(exc))


def process_valuation_sim_automation_for_robot() -> Dict[str, Any]:
    return process_enabled_valuation_simulations()
