import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.database import (
    IBKRAccountConfig,
    LongPortAccount,
    SoxlFearStrategyConfig,
    SoxlFearStrategyLog,
    SoxlFearStrategyState,
    get_db,
)
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    get_external_trading_db,
)
from ...core.services.external_trading_ledger import STRATEGY_SOXL_FEAR
from ...core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_US_STOCK,
    normalize_external_trading_market_type,
)
from ...robot.soxl_fear_strategy_trader import SoxlFearStrategyTrader
from .account import valid_account

router = APIRouter(prefix="/api/soxl-fear-strategy", tags=["soxl-fear-strategy"])
logger = logging.getLogger(__name__)


class SoxlFearStrategyConfigPayload(BaseModel):
    enabled: bool = False
    symbol: str = "SOXL.US"
    account_type: str = "ib"
    ib_account_id: Optional[int] = None
    longport_account_id: Optional[str] = None
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None
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

    @validator("symbol")
    def validate_symbol(cls, value):
        value = (value or "").strip().upper()
        if not value:
            raise ValueError("交易标的不能为空")
        return value

    @validator("account_type")
    def validate_account_type(cls, value):
        if value not in {"ib", "longport", "external"}:
            raise ValueError("account_type 仅支持 ib、longport 或 external")
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


class SoxlFearStrategyConfigSchema(SoxlFearStrategyConfigPayload):
    id: Optional[int] = None
    account_id: Optional[str] = None
    trading_account_id: Optional[str] = None
    external_trading_account_name: Optional[str] = None
    live_sub_account_name: Optional[str] = None
    live_sub_account_enabled: Optional[bool] = None
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None
    last_run_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SoxlFearStrategyLogSchema(BaseModel):
    id: int
    config_id: Optional[int] = None
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


class SoxlFearStrategyStatePayload(BaseModel):
    last_processed_date: Optional[date] = None
    cooldown_remaining_days: int = 0
    greed_peak_price: Optional[float] = None
    take_profit_cycle_sell_count: int = 0

    @validator("cooldown_remaining_days")
    def validate_cooldown_remaining_days(cls, value):
        if value < 0 or value > 60:
            raise ValueError("cooldown_remaining_days 必须在 0 到 60 之间")
        return value

    @validator("greed_peak_price")
    def validate_greed_peak_price(cls, value):
        if value is not None and value < 0:
            raise ValueError("greed_peak_price 不能为负数")
        return value

    @validator("take_profit_cycle_sell_count")
    def validate_take_profit_cycle_sell_count(cls, value):
        if value < 0 or value > 20:
            raise ValueError("take_profit_cycle_sell_count 必须在 0 到 20 之间")
        return value


class SoxlFearStrategyStateSchema(SoxlFearStrategyStatePayload):
    config_id: int
    account_id: Optional[str] = None
    symbol: str = "SOXL.US"
    updated_at: Optional[datetime] = None
    has_state: bool = False

    class Config:
        from_attributes = True


CONFIG_FIELDS = [
    "enabled",
    "symbol",
    "account_type",
    "ib_account_id",
    "longport_account_id",
    "external_trading_account_id",
    "live_sub_account_id",
    "buy_threshold",
    "greed_threshold",
    "volume_ratio_threshold",
    "buy_position_pct",
    "cooldown_days",
    "trailing_stop_pct",
    "sell_position_pct",
    "sell_reduction_basis",
    "max_take_profit_sells_per_cycle",
    "min_position_pct_after_take_profit",
    "rebalance_threshold_pct",
]


def _get_config_or_404(db: Session, account_id: str, config_id: int) -> SoxlFearStrategyConfig:
    config = db.query(SoxlFearStrategyConfig).filter(
        SoxlFearStrategyConfig.id == config_id,
        SoxlFearStrategyConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="未找到 SOXL 情绪量能策略配置")
    return config


def _resolve_trading_account_id(
    payload: SoxlFearStrategyConfigPayload,
    account_id: str,
    db: Session,
    trading_db: Session,
    config_id: Optional[int] = None,
) -> str:
    if payload.account_type == "ib":
        if not payload.ib_account_id:
            raise HTTPException(status_code=400, detail="必须选择 IB 账户")
        ib_account = db.query(IBKRAccountConfig).filter(
            IBKRAccountConfig.id == payload.ib_account_id,
            IBKRAccountConfig.account_id == account_id,
        ).first()
        if not ib_account:
            raise HTTPException(status_code=400, detail="IB 账户不存在或不属于当前账户")
        return str(payload.ib_account_id)

    if payload.account_type == "longport":
        longport_account_id = (payload.longport_account_id or "").strip()
        if not longport_account_id:
            raise HTTPException(status_code=400, detail="必须选择长桥账户")
        longport_account = db.query(LongPortAccount).filter(
            LongPortAccount.lp_account_id == longport_account_id,
            LongPortAccount.account_id == account_id,
        ).first()
        if not longport_account:
            raise HTTPException(status_code=400, detail="长桥账户不存在或不属于当前账户")
        return longport_account_id

    account = _validate_soxl_external_account_selection(
        trading_db,
        account_id,
        payload.external_trading_account_id,
    )
    sub_account = _get_valid_soxl_live_sub_account_selection(
        trading_db,
        account_id,
        payload.external_trading_account_id,
        payload.live_sub_account_id,
        config_id=config_id,
        require_enabled=True,
    )
    if not account or not sub_account:
        raise HTTPException(status_code=400, detail="必须选择外部交易账户和虚拟子账户")
    return f"external:{account.id}:{sub_account.id}"


def _validate_soxl_external_account_selection(
    db: Session,
    account_id: str,
    external_account_id: Optional[int],
) -> Optional[ExternalTradingAccount]:
    if not external_account_id:
        return None
    account = db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == external_account_id,
        ExternalTradingAccount.account_id == account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="所选外部交易账户不存在")
    if not account.enabled:
        raise HTTPException(status_code=400, detail="所选外部交易账户未启用")
    if normalize_external_trading_market_type(account.market_type) != EXTERNAL_TRADING_MARKET_US_STOCK:
        raise HTTPException(status_code=400, detail="SOXL 策略只能选择美股外部交易账户")
    return account


def _get_valid_soxl_live_sub_account_selection(
    db: Session,
    account_id: str,
    external_account_id: Optional[int],
    sub_account_id: Optional[int],
    *,
    config_id: Optional[int] = None,
    require_enabled: bool = False,
) -> Optional[ExternalTradingSubAccount]:
    if not sub_account_id:
        return None
    if not external_account_id:
        raise HTTPException(status_code=400, detail="选择虚拟子账户前请先选择外部交易账户")
    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=400, detail="所选虚拟子账户不存在")
    if require_enabled and not sub_account.enabled:
        raise HTTPException(status_code=400, detail="所选虚拟子账户未启用")
    is_bound = bool(sub_account.strategy_type or sub_account.strategy_config_id)
    is_current_binding = (
        sub_account.strategy_type == STRATEGY_SOXL_FEAR
        and config_id
        and sub_account.strategy_config_id == config_id
    )
    if is_bound and not is_current_binding:
        raise HTTPException(status_code=400, detail="所选虚拟子账户已被其他策略绑定")
    return sub_account


def _deactivate_soxl_target_positions(
    db: Session,
    *,
    sub_account_id: Optional[int],
    config_id: Optional[int],
) -> None:
    if not sub_account_id:
        return
    query = db.query(ExternalTradingTargetPosition).filter(
        ExternalTradingTargetPosition.sub_account_id == sub_account_id,
        ExternalTradingTargetPosition.status == "ACTIVE",
    )
    if config_id:
        query = query.filter(
            ExternalTradingTargetPosition.strategy_type == STRATEGY_SOXL_FEAR,
            ExternalTradingTargetPosition.strategy_config_id == config_id,
        )
    now = datetime.now()
    for row in query.all():
        row.status = "PREVIEW"
        row.updated_at = now


def _sync_soxl_live_sub_account_binding(
    db: Session,
    config: SoxlFearStrategyConfig,
    *,
    previous_sub_account_id: Optional[int],
) -> None:
    if config.account_type == "external":
        if not config.external_trading_account_id:
            raise HTTPException(status_code=400, detail="使用外部交易账户时必须选择外部交易账户")
        if not config.live_sub_account_id:
            raise HTTPException(status_code=400, detail="使用外部交易账户时必须选择虚拟子账户")
    else:
        config.external_trading_account_id = None
        config.live_sub_account_id = None

    _validate_soxl_external_account_selection(db, config.account_id, config.external_trading_account_id)
    selected_sub_account = _get_valid_soxl_live_sub_account_selection(
        db,
        config.account_id,
        config.external_trading_account_id,
        config.live_sub_account_id,
        config_id=config.id,
        require_enabled=bool(config.live_sub_account_id),
    )

    if previous_sub_account_id and previous_sub_account_id != config.live_sub_account_id:
        _deactivate_soxl_target_positions(
            db,
            sub_account_id=previous_sub_account_id,
            config_id=config.id,
        )
        previous = db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == previous_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_SOXL_FEAR,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if previous:
            previous.strategy_type = None
            previous.strategy_config_id = None
            previous.updated_at = datetime.now()

    if not getattr(config, "enabled", True) or config.account_type != "external":
        _deactivate_soxl_target_positions(
            db,
            sub_account_id=config.live_sub_account_id or previous_sub_account_id,
            config_id=config.id,
        )

    if selected_sub_account:
        selected_sub_account.strategy_type = STRATEGY_SOXL_FEAR
        selected_sub_account.strategy_config_id = config.id
        selected_sub_account.updated_at = datetime.now()


def _soxl_config_response(
    config: SoxlFearStrategyConfig,
    trading_db: Session,
) -> SoxlFearStrategyConfigSchema:
    data = {
        field: getattr(config, field, None)
        for field in [
            *CONFIG_FIELDS,
            "id",
            "account_id",
            "trading_account_id",
            "last_run_at",
            "last_run_status",
            "last_run_message",
            "created_at",
            "updated_at",
        ]
    }
    if getattr(config, "external_trading_account_id", None):
        account = trading_db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.id == config.external_trading_account_id,
            ExternalTradingAccount.account_id == config.account_id,
        ).first()
        if account:
            data["external_trading_account_name"] = f"{account.name} ({account.identifier})"
    if getattr(config, "live_sub_account_id", None):
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
        ).first()
        if sub_account:
            data["live_sub_account_name"] = sub_account.name
            data["live_sub_account_enabled"] = sub_account.enabled
    return SoxlFearStrategyConfigSchema(**data)


def _assert_unique_target_account(
    db: Session,
    symbol: str,
    account_type: str,
    trading_account_id: str,
    exclude_config_id: Optional[int] = None,
):
    query = db.query(SoxlFearStrategyConfig).filter(
        SoxlFearStrategyConfig.symbol == symbol,
        SoxlFearStrategyConfig.account_type == account_type,
        SoxlFearStrategyConfig.trading_account_id == trading_account_id,
    )
    if exclude_config_id:
        query = query.filter(SoxlFearStrategyConfig.id != exclude_config_id)
    if query.first():
        raise HTTPException(status_code=400, detail="交易标的、账户类型、账户ID 已存在相同配置")


def _apply_payload_to_config(
    config: SoxlFearStrategyConfig,
    payload: SoxlFearStrategyConfigPayload,
    trading_account_id: str,
):
    payload_data = payload.dict()
    if payload.account_type == "ib":
        payload_data["longport_account_id"] = None
        payload_data["external_trading_account_id"] = None
        payload_data["live_sub_account_id"] = None
    elif payload.account_type == "longport":
        payload_data["ib_account_id"] = None
        payload_data["external_trading_account_id"] = None
        payload_data["live_sub_account_id"] = None
    else:
        payload_data["ib_account_id"] = None
        payload_data["longport_account_id"] = None

    for field in CONFIG_FIELDS:
        setattr(config, field, payload_data[field])
    config.trading_account_id = trading_account_id
    config.updated_at = datetime.now()


def _soxl_state_response(
    config: SoxlFearStrategyConfig,
    state: Optional[SoxlFearStrategyState],
) -> SoxlFearStrategyStateSchema:
    return SoxlFearStrategyStateSchema(
        config_id=config.id,
        account_id=config.account_id,
        symbol=getattr(state, "symbol", None) or config.symbol or "SOXL.US",
        last_processed_date=getattr(state, "last_processed_date", None),
        cooldown_remaining_days=int(getattr(state, "cooldown_remaining_days", 0) or 0),
        greed_peak_price=getattr(state, "greed_peak_price", None),
        take_profit_cycle_sell_count=int(getattr(state, "take_profit_cycle_sell_count", 0) or 0),
        updated_at=getattr(state, "updated_at", None),
        has_state=bool(state),
    )


@router.get("/configs", response_model=List[SoxlFearStrategyConfigSchema])
def list_soxl_fear_strategy_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    configs = (
        db.query(SoxlFearStrategyConfig)
        .filter(SoxlFearStrategyConfig.account_id == account_id)
        .order_by(SoxlFearStrategyConfig.updated_at.desc(), SoxlFearStrategyConfig.id.desc())
        .all()
    )
    return [_soxl_config_response(config, trading_db) for config in configs]


@router.post("/configs", response_model=SoxlFearStrategyConfigSchema)
def create_soxl_fear_strategy_config(
    payload: SoxlFearStrategyConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    trading_account_id = _resolve_trading_account_id(payload, account_id, db, trading_db)
    _assert_unique_target_account(db, payload.symbol, payload.account_type, trading_account_id)

    config = SoxlFearStrategyConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload_to_config(config, payload, trading_account_id)
    db.add(config)

    try:
        db.flush()
        _sync_soxl_live_sub_account_binding(
            trading_db,
            config,
            previous_sub_account_id=None,
        )
        trading_db.commit()
        db.commit()
    except IntegrityError:
        db.rollback()
        trading_db.rollback()
        raise HTTPException(status_code=400, detail="交易标的、账户类型、账户ID 已存在相同配置")
    except HTTPException:
        db.rollback()
        trading_db.rollback()
        raise
    except Exception:
        db.rollback()
        trading_db.rollback()
        raise

    db.refresh(config)
    return _soxl_config_response(config, trading_db)


@router.get("/configs/{config_id}", response_model=SoxlFearStrategyConfigSchema)
def get_soxl_fear_strategy_config_by_id(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    return _soxl_config_response(_get_config_or_404(db, account_id, config_id), trading_db)


@router.put("/configs/{config_id}", response_model=SoxlFearStrategyConfigSchema)
def update_soxl_fear_strategy_config(
    config_id: int,
    payload: SoxlFearStrategyConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    previous_sub_account_id = getattr(config, "live_sub_account_id", None)
    trading_account_id = _resolve_trading_account_id(payload, account_id, db, trading_db, config_id=config.id)
    _assert_unique_target_account(db, payload.symbol, payload.account_type, trading_account_id, exclude_config_id=config.id)
    _apply_payload_to_config(config, payload, trading_account_id)

    try:
        db.flush()
        _sync_soxl_live_sub_account_binding(
            trading_db,
            config,
            previous_sub_account_id=previous_sub_account_id,
        )
        trading_db.commit()
        db.commit()
    except IntegrityError:
        db.rollback()
        trading_db.rollback()
        raise HTTPException(status_code=400, detail="交易标的、账户类型、账户ID 已存在相同配置")
    except HTTPException:
        db.rollback()
        trading_db.rollback()
        raise
    except Exception:
        db.rollback()
        trading_db.rollback()
        raise

    db.refresh(config)
    return _soxl_config_response(config, trading_db)


@router.delete("/configs/{config_id}")
def delete_soxl_fear_strategy_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    if getattr(config, "live_sub_account_id", None):
        _deactivate_soxl_target_positions(
            trading_db,
            sub_account_id=config.live_sub_account_id,
            config_id=config.id,
        )
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_SOXL_FEAR,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if sub_account:
            sub_account.strategy_type = None
            sub_account.strategy_config_id = None
            sub_account.updated_at = datetime.now()
    db.query(SoxlFearStrategyState).filter(SoxlFearStrategyState.config_id == config.id).delete()
    db.query(SoxlFearStrategyLog).filter(SoxlFearStrategyLog.config_id == config.id).delete()
    db.delete(config)
    trading_db.commit()
    db.commit()
    return {"message": "配置已删除"}


@router.get("/configs/{config_id}/logs", response_model=List[SoxlFearStrategyLogSchema])
def get_soxl_fear_strategy_logs_by_config(
    config_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    return (
        db.query(SoxlFearStrategyLog)
        .filter(SoxlFearStrategyLog.config_id == config.id)
        .order_by(SoxlFearStrategyLog.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )


@router.get("/configs/{config_id}/state", response_model=SoxlFearStrategyStateSchema)
def get_soxl_fear_strategy_state_by_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    state = db.query(SoxlFearStrategyState).filter(SoxlFearStrategyState.config_id == config.id).first()
    return _soxl_state_response(config, state)


@router.put("/configs/{config_id}/state", response_model=SoxlFearStrategyStateSchema)
def update_soxl_fear_strategy_state_by_config(
    config_id: int,
    payload: SoxlFearStrategyStatePayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    state = db.query(SoxlFearStrategyState).filter(SoxlFearStrategyState.config_id == config.id).first()
    if not state:
        state = SoxlFearStrategyState(
            config_id=config.id,
            account_id=config.account_id,
            symbol=config.symbol or "SOXL.US",
        )
        db.add(state)
    state.account_id = config.account_id
    state.symbol = config.symbol or "SOXL.US"
    state.last_processed_date = payload.last_processed_date
    state.cooldown_remaining_days = int(payload.cooldown_remaining_days or 0)
    state.greed_peak_price = payload.greed_peak_price
    state.take_profit_cycle_sell_count = int(payload.take_profit_cycle_sell_count or 0)
    state.updated_at = datetime.now()
    db.commit()
    db.refresh(state)
    return _soxl_state_response(config, state)


@router.post("/configs/{config_id}/manual-check")
def manual_check_soxl_fear_strategy_by_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    try:
        SoxlFearStrategyTrader().trigger_manual_run(config.id, account_id)
        return {"message": "已在后台触发一次 SOXL 情绪量能策略检查"}
    except Exception as exc:
        logger.error("Failed to trigger SOXL fear strategy manually: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/config", response_model=SoxlFearStrategyConfigSchema)
def get_soxl_fear_strategy_config(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = (
        db.query(SoxlFearStrategyConfig)
        .filter(SoxlFearStrategyConfig.account_id == account_id)
        .order_by(SoxlFearStrategyConfig.id.asc())
        .first()
    )
    if config:
        return _soxl_config_response(config, trading_db)
    return SoxlFearStrategyConfigSchema()


@router.post("/config", response_model=SoxlFearStrategyConfigSchema)
def save_soxl_fear_strategy_config(
    payload: SoxlFearStrategyConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = (
        db.query(SoxlFearStrategyConfig)
        .filter(SoxlFearStrategyConfig.account_id == account_id)
        .order_by(SoxlFearStrategyConfig.id.asc())
        .first()
    )
    if config:
        return update_soxl_fear_strategy_config(config.id, payload, account_id, db, trading_db)
    return create_soxl_fear_strategy_config(payload, account_id, db, trading_db)


@router.get("/logs", response_model=List[SoxlFearStrategyLogSchema])
def get_soxl_fear_strategy_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = (
        db.query(SoxlFearStrategyConfig)
        .filter(SoxlFearStrategyConfig.account_id == account_id)
        .order_by(SoxlFearStrategyConfig.id.asc())
        .first()
    )
    if not config:
        return []
    return (
        db.query(SoxlFearStrategyLog)
        .filter(SoxlFearStrategyLog.config_id == config.id)
        .order_by(SoxlFearStrategyLog.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )


@router.post("/manual-check")
def manual_check_soxl_fear_strategy(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = (
        db.query(SoxlFearStrategyConfig)
        .filter(SoxlFearStrategyConfig.account_id == account_id)
        .order_by(SoxlFearStrategyConfig.id.asc())
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="未找到 SOXL 情绪量能策略配置")
    try:
        SoxlFearStrategyTrader().trigger_manual_run(config.id, account_id)
        return {"message": "已在后台触发一次 SOXL 情绪量能策略检查"}
    except Exception as exc:
        logger.error("Failed to trigger SOXL fear strategy manually: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
