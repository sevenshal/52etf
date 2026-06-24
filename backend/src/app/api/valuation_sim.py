import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from ...core.database import (
    ValuationSimConfig,
    ValuationSimLog,
    get_db,
)
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    ExternalTradingValuationSimPositionState,
    get_external_trading_db,
)
from ...core.services.external_trading_ledger import (
    STRATEGY_VALUATION_SIM,
    safe_float as external_safe_float,
    safe_int as external_safe_int,
)
from ...core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_US_STOCK,
    normalize_external_trading_market_type,
)
from ...core.services.valuation_simulator import (
    ValuationSimulationService,
    process_enabled_valuation_simulations,
)
from .account import valid_account

router = APIRouter(prefix="/api/valuation-sim", tags=["valuation-sim"])
logger = logging.getLogger(__name__)


class ValuationSimConfigBase(BaseModel):
    name: str = "纳指100估值成长模拟盘"
    enabled: bool = False
    universe_tag_ids: List[str] = Field(default_factory=list)
    min_market_cap_100m: Optional[float] = 100.0
    max_market_cap_100m: Optional[float] = None
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


class ValuationSimConfigPayload(ValuationSimConfigBase):
    external_trading_account_id: int
    live_sub_account_id: int

    @validator("external_trading_account_id", "live_sub_account_id")
    def validate_required_external_id(cls, value):
        if not value:
            raise ValueError("估值模拟盘必须绑定外部交易账户和子账户")
        return int(value)


class ValuationSimConfigSchema(ValuationSimConfigBase):
    id: int
    account_id: Optional[str] = None
    account_source: str = "external"
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None
    external_trading_account_name: Optional[str] = None
    external_trading_account_identifier: Optional[str] = None
    external_trading_account_label: Optional[str] = None
    live_sub_account_name: Optional[str] = None
    external_cash_allocated: Optional[float] = None
    external_cash_available: Optional[float] = None
    external_position_value: Optional[float] = None
    external_net_asset: Optional[float] = None
    last_run_at: Optional[datetime] = None
    last_run_date: Optional[date] = None
    last_run_status: Optional[str] = None
    last_run_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

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
    "max_positions",
    "external_trading_account_id",
    "live_sub_account_id",
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


def _get_bound_external_sub_account(
    external_db: Session,
    config: ValuationSimConfig,
) -> Optional[ExternalTradingSubAccount]:
    if not config.external_trading_account_id or not config.live_sub_account_id:
        return None
    return external_db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == config.live_sub_account_id,
        ExternalTradingSubAccount.account_id == config.account_id,
        ExternalTradingSubAccount.external_trading_account_id == config.external_trading_account_id,
    ).first()


def _position_market_value(position: ExternalTradingLedgerPosition) -> float:
    quantity = external_safe_int(position.quantity)
    if quantity <= 0:
        return 0.0
    market_value = external_safe_float(position.market_value)
    if market_value > 0:
        return market_value
    market_price = external_safe_float(position.market_price)
    if market_price <= 0:
        market_price = external_safe_float(position.avg_cost)
    return quantity * market_price if market_price > 0 else 0.0


def _external_config_cash_view(
    external_db: Session,
    sub_account: ExternalTradingSubAccount,
) -> Dict[str, float]:
    positions = (
        external_db.query(ExternalTradingLedgerPosition)
        .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
        .all()
    )
    position_value = round(sum(_position_market_value(position) for position in positions), 2)
    cash_allocated = round(external_safe_float(sub_account.cash_allocated), 2)
    cash_available = round(external_safe_float(sub_account.cash_available), 2)
    return {
        "cash_allocated": cash_allocated,
        "cash_available": cash_available,
        "position_value": position_value,
        "net_asset": round(cash_available + position_value, 2),
    }


def _serialize_config(
    config: ValuationSimConfig,
    external_db: Optional[Session] = None,
) -> Dict[str, Any]:
    data = {field: getattr(config, field, None) for field in CONFIG_FIELDS}
    data.update({
        "id": config.id,
        "account_id": config.account_id,
        "account_source": "external",
        "last_run_at": config.last_run_at,
        "last_run_date": config.last_run_date,
        "last_run_status": config.last_run_status,
        "last_run_message": config.last_run_message,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    })
    if external_db is None or not config.external_trading_account_id:
        return data

    external_account = external_db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == config.external_trading_account_id,
        ExternalTradingAccount.account_id == config.account_id,
    ).first()
    sub_account = _get_bound_external_sub_account(external_db, config)
    if external_account:
        data["external_trading_account_name"] = external_account.name
        data["external_trading_account_identifier"] = external_account.identifier
        data["external_trading_account_label"] = (
            f"{external_account.name}（{external_account.identifier}）"
            if external_account.name and external_account.identifier
            else external_account.name or external_account.identifier
        )
    if sub_account:
        cash_view = _external_config_cash_view(external_db, sub_account)
        data["live_sub_account_name"] = sub_account.name
        data["external_cash_allocated"] = cash_view["cash_allocated"]
        data["external_cash_available"] = cash_view["cash_available"]
        data["external_position_value"] = cash_view["position_value"]
        data["external_net_asset"] = cash_view["net_asset"]
    return data


def _clear_target_positions(
    external_db: Session,
    config: ValuationSimConfig,
    *,
    sub_account_id: Optional[int] = None,
) -> None:
    target_sub_account_id = sub_account_id or config.live_sub_account_id
    query = external_db.query(ExternalTradingTargetPosition).filter(
        ExternalTradingTargetPosition.account_id == config.account_id,
        ExternalTradingTargetPosition.strategy_type == STRATEGY_VALUATION_SIM,
        ExternalTradingTargetPosition.strategy_config_id == config.id,
    )
    if target_sub_account_id:
        query = query.filter(ExternalTradingTargetPosition.sub_account_id == target_sub_account_id)
    query.delete(synchronize_session=False)

    state_query = external_db.query(ExternalTradingValuationSimPositionState).filter(
        ExternalTradingValuationSimPositionState.account_id == config.account_id,
        ExternalTradingValuationSimPositionState.config_id == config.id,
    )
    if target_sub_account_id:
        state_query = state_query.filter(ExternalTradingValuationSimPositionState.sub_account_id == target_sub_account_id)
    state_query.delete(synchronize_session=False)


def _bind_valuation_sim_sub_account(
    external_db: Session,
    config: ValuationSimConfig,
    *,
    previous_sub_account_id: Optional[int] = None,
) -> None:
    if previous_sub_account_id and previous_sub_account_id != config.live_sub_account_id:
        previous = external_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == previous_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
        ).first()
        if (
            previous
            and previous.strategy_type == STRATEGY_VALUATION_SIM
            and previous.strategy_config_id == config.id
        ):
            _clear_target_positions(external_db, config, sub_account_id=previous_sub_account_id)
            previous.strategy_type = None
            previous.strategy_config_id = None
            previous.updated_at = datetime.now()

    if not config.external_trading_account_id or not config.live_sub_account_id:
        raise HTTPException(status_code=400, detail="估值模拟盘必须选择外部交易账户和子账户")

    account = external_db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == config.external_trading_account_id,
        ExternalTradingAccount.account_id == config.account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="所选外部交易账户不存在")
    if not account.enabled:
        raise HTTPException(status_code=400, detail="所选外部交易账户未启用")
    market_type = normalize_external_trading_market_type(getattr(account, "market_type", None))
    if market_type != EXTERNAL_TRADING_MARKET_US_STOCK:
        raise HTTPException(status_code=400, detail="估值模拟盘只能绑定美股外部交易账户")

    sub_account = _get_bound_external_sub_account(external_db, config)
    if not sub_account:
        raise HTTPException(status_code=400, detail="所选外部交易子账户不存在")
    if not sub_account.enabled:
        raise HTTPException(status_code=400, detail="所选外部交易子账户未启用")
    if (
        sub_account.strategy_type
        and not (
            sub_account.strategy_type == STRATEGY_VALUATION_SIM
            and sub_account.strategy_config_id == config.id
        )
    ):
        raise HTTPException(status_code=400, detail="该外部交易子账户已绑定其他策略")

    sub_account.strategy_type = STRATEGY_VALUATION_SIM
    sub_account.strategy_config_id = config.id
    sub_account.updated_at = datetime.now()


def _unbind_valuation_sim_sub_account(
    external_db: Session,
    config: ValuationSimConfig,
) -> None:
    sub_account = _get_bound_external_sub_account(external_db, config)
    if (
        sub_account
        and sub_account.strategy_type == STRATEGY_VALUATION_SIM
        and sub_account.strategy_config_id == config.id
    ):
        sub_account.strategy_type = None
        sub_account.strategy_config_id = None
        sub_account.updated_at = datetime.now()


def _apply_payload(config: ValuationSimConfig, payload: ValuationSimConfigPayload):
    payload_data = payload.dict()
    min_market_cap = payload_data.get("min_market_cap_100m")
    max_market_cap = payload_data.get("max_market_cap_100m")
    if min_market_cap is not None and max_market_cap is not None and min_market_cap > max_market_cap:
        raise HTTPException(status_code=400, detail="市值下限不能大于上限")
    for field in CONFIG_FIELDS:
        setattr(config, field, payload_data[field])
    config.updated_at = datetime.now()


@router.get("/configs", response_model=List[ValuationSimConfigSchema])
def list_valuation_sim_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    external_db: Session = Depends(get_external_trading_db),
):
    rows = (
        db.query(ValuationSimConfig)
        .filter(ValuationSimConfig.account_id == account_id)
        .order_by(ValuationSimConfig.updated_at.desc(), ValuationSimConfig.id.desc())
        .all()
    )
    return [_serialize_config(row, external_db) for row in rows]


@router.post("/configs", response_model=ValuationSimConfigSchema)
def create_valuation_sim_config(
    payload: ValuationSimConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    external_db: Session = Depends(get_external_trading_db),
):
    config = ValuationSimConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload(config, payload)
    db.add(config)
    db.flush()
    _bind_valuation_sim_sub_account(external_db, config)
    external_db.commit()
    db.commit()
    db.refresh(config)
    return _serialize_config(config, external_db)


@router.get("/configs/{config_id}", response_model=ValuationSimConfigSchema)
def get_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    external_db: Session = Depends(get_external_trading_db),
):
    return _serialize_config(_get_config_or_404(db, account_id, config_id), external_db)


@router.put("/configs/{config_id}", response_model=ValuationSimConfigSchema)
def update_valuation_sim_config(
    config_id: int,
    payload: ValuationSimConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    external_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    previous_sub_account_id = config.live_sub_account_id
    _apply_payload(config, payload)
    _bind_valuation_sim_sub_account(
        external_db,
        config,
        previous_sub_account_id=previous_sub_account_id,
    )
    external_db.commit()
    db.commit()
    db.refresh(config)
    return _serialize_config(config, external_db)


@router.delete("/configs/{config_id}")
def delete_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    external_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _clear_target_positions(external_db, config)
    _unbind_valuation_sim_sub_account(external_db, config)
    db.query(ValuationSimLog).filter(ValuationSimLog.config_id == config.id).delete()
    db.delete(config)
    external_db.commit()
    db.commit()
    return {"message": "配置已删除"}


@router.post("/configs/{config_id}/reset", response_model=ValuationSimConfigSchema)
def reset_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    external_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _clear_target_positions(external_db, config)
    db.query(ValuationSimLog).filter(ValuationSimLog.config_id == config.id).delete()
    config.last_run_at = None
    config.last_run_date = None
    config.last_run_status = None
    config.last_run_message = None
    config.updated_at = datetime.now()
    external_db.commit()
    db.commit()
    db.refresh(config)
    return _serialize_config(config, external_db)


@router.post("/configs/{config_id}/run")
def run_valuation_sim_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    external_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    try:
        result = ValuationSimulationService(db, external_db=external_db).run_config(config, trigger_source="manual")
        external_db.commit()
        db.commit()
        return result
    except Exception as exc:
        external_db.rollback()
        db.rollback()
        logger.exception("Manual valuation simulation run failed, config_id=%s", config_id)
        raise HTTPException(status_code=500, detail=str(exc))


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
