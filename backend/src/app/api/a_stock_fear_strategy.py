import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.database import (
    AStockFearStrategyConfig,
    AStockFearStrategyLog,
    AStockFearStrategyState,
    get_db,
)
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    get_external_trading_db,
)
from ...core.services.external_trading_ledger import STRATEGY_A_STOCK_FEAR
from ...core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_A_STOCK,
    normalize_external_trading_market_type,
)
from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_NAMES
from ...robot.a_stock_fear_strategy_trader import AStockFearStrategyTrader
from .account import valid_account
from .soxl_fear_backtest import (
    A_STOCK_FEAR_SOURCE_OPTIONS,
    A_STOCK_PRESET_PAIRS,
    A_STOCK_TARGET_OPTIONS,
)

router = APIRouter(prefix="/api/a-stock-fear-strategy", tags=["a-stock-fear-strategy"])
logger = logging.getLogger(__name__)

A_STOCK_SYMBOLS = {str(item["value"]).upper() for item in A_STOCK_TARGET_OPTIONS}
A_STOCK_FEAR_SOURCE_KEYS = {key for key in A_STOCK_FEAR_SOURCE_OPTIONS}


class AStockFearStrategyConfigPayload(BaseModel):
    enabled: bool = False
    symbol: str = "510880.SH"
    fear_source: str = "a_stock_000015_sh"
    volume_signal_symbol: Optional[str] = None
    # 跷跷板候补（可选）：主标的空仓时，候补极恐放量则买入候补；主标的出信号换回
    sub_symbol: Optional[str] = None
    sub_fear_source: str = "a_stock_000688_sh"
    sub_volume_signal_symbol: Optional[str] = None
    sub_buy_threshold: float = 25.0
    sub_volume_ratio_threshold: float = 1.6
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None
    run_time: str = "09:30"
    buy_threshold: float = 30.0
    greed_threshold: float = 70.0
    volume_ratio_threshold: float = 1.3
    buy_position_pct: float = 100.0
    cooldown_days: int = 0
    trailing_stop_pct: float = 0.0
    sell_position_pct: float = 100.0
    sell_reduction_basis: str = "holdings"
    sell_price_above_avg_cost: bool = False
    max_take_profit_sells_per_cycle: int = 2
    min_position_pct_after_take_profit: float = 0.0
    rebalance_threshold_pct: float = 0.0

    @validator("symbol")
    def validate_symbol(cls, value):
        value = (value or "").strip().upper()
        if value not in A_STOCK_SYMBOLS:
            raise ValueError("交易标的必须是可交易 A 股 ETF（或先在标的池中配置）")
        return value

    @validator("fear_source")
    def validate_fear_source(cls, value):
        value = (value or "").strip().lower()
        if value not in A_STOCK_FEAR_SOURCE_KEYS:
            raise ValueError("恐贪来源必须是 A 股指数恐贪（a_stock_*）")
        return value

    @validator("volume_signal_symbol")
    def validate_volume_signal_symbol(cls, value):
        if not value:
            return None
        value = (value or "").strip().upper()
        if value not in A_STOCK_SYMBOLS:
            raise ValueError("量比来源标的必须是可交易 A 股 ETF")
        return value

    @validator("sub_symbol")
    def validate_sub_symbol(cls, value):
        if not value:
            return None
        value = (value or "").strip().upper()
        if value not in A_STOCK_SYMBOLS:
            raise ValueError("候补标的必须是可交易 A 股 ETF")
        return value

    @validator("sub_fear_source")
    def validate_sub_fear_source(cls, value):
        value = (value or "").strip().lower()
        if value not in A_STOCK_FEAR_SOURCE_KEYS:
            raise ValueError("候补恐贪来源必须是 A 股指数恐贪（a_stock_*）")
        return value

    @validator("sub_volume_signal_symbol")
    def validate_sub_volume_signal_symbol(cls, value):
        if not value:
            return None
        value = (value or "").strip().upper()
        if value not in A_STOCK_SYMBOLS:
            raise ValueError("候补量比来源标的必须是可交易 A 股 ETF")
        return value

    @validator("sub_buy_threshold")
    def validate_sub_buy_threshold(cls, value):
        if value < 0 or value > 100:
            raise ValueError("候补恐慌阈值必须在 0 到 100 之间")
        return value

    @validator("sub_volume_ratio_threshold")
    def validate_sub_volume_ratio_threshold(cls, value):
        if value <= 0 or value > 20:
            raise ValueError("候补量比阈值必须大于 0 且不超过 20")
        return value

    @validator("run_time")
    def validate_run_time(cls, value):
        value = (value or "09:30").strip()
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError("run_time 必须为 HH:MM 格式（Asia/Shanghai）")
        return value

    @validator("sell_reduction_basis")
    def validate_sell_reduction_basis(cls, value):
        if value not in {"portfolio", "holdings"}:
            raise ValueError("sell_reduction_basis 仅支持 portfolio 或 holdings")
        return value

    @validator("buy_threshold", "greed_threshold")
    def validate_threshold(cls, value):
        if value < 0 or value > 100:
            raise ValueError("恐贪阈值必须在 0 到 100 之间")
        return value

    @validator("volume_ratio_threshold")
    def validate_volume_ratio_threshold(cls, value):
        if value <= 0 or value > 20:
            raise ValueError("量比阈值必须大于 0 且不超过 20")
        return value

    @validator("buy_position_pct")
    def validate_buy_position_pct(cls, value):
        if value <= 0 or value > 100:
            raise ValueError("买入仓位必须在 0 到 100 之间")
        return value

    @validator("sell_position_pct", "min_position_pct_after_take_profit", "rebalance_threshold_pct")
    def validate_sell_percent(cls, value):
        if value < 0 or value > 100:
            raise ValueError("百分比参数必须在 0 到 100 之间")
        return value

    @validator("trailing_stop_pct")
    def validate_trailing_stop_pct(cls, value):
        # 0 = 到达贪恐阈值（>= greed_threshold）即卖
        if value < 0 or value > 100:
            raise ValueError("移动止盈回撤必须在 0 到 100 之间（0=到达贪恐阈值即卖）")
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


class AStockFearStrategyConfigSchema(AStockFearStrategyConfigPayload):
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


class AStockFearStrategyLogSchema(BaseModel):
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


class AStockFearStrategyStatePayload(BaseModel):
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


class AStockFearStrategyStateSchema(AStockFearStrategyStatePayload):
    config_id: int
    account_id: Optional[str] = None
    symbol: str = "510880.SH"
    updated_at: Optional[datetime] = None
    has_state: bool = False

    class Config:
        from_attributes = True


CONFIG_FIELDS = [
    "enabled",
    "symbol",
    "fear_source",
    "volume_signal_symbol",
    "sub_symbol",
    "sub_fear_source",
    "sub_volume_signal_symbol",
    "sub_buy_threshold",
    "sub_volume_ratio_threshold",
    "external_trading_account_id",
    "live_sub_account_id",
    "run_time",
    "buy_threshold",
    "greed_threshold",
    "volume_ratio_threshold",
    "buy_position_pct",
    "cooldown_days",
    "trailing_stop_pct",
    "sell_position_pct",
    "sell_reduction_basis",
    "sell_price_above_avg_cost",
    "max_take_profit_sells_per_cycle",
    "min_position_pct_after_take_profit",
    "rebalance_threshold_pct",
]


def _get_config_or_404(db: Session, account_id: str, config_id: int) -> AStockFearStrategyConfig:
    config = db.query(AStockFearStrategyConfig).filter(
        AStockFearStrategyConfig.id == config_id,
        AStockFearStrategyConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="未找到 A股情绪量能策略配置")
    return config


def _validate_external_account_selection(
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
    if normalize_external_trading_market_type(account.market_type) != EXTERNAL_TRADING_MARKET_A_STOCK:
        raise HTTPException(status_code=400, detail="A股情绪量能策略只能选择 A 股外部交易账户")
    return account


def _get_valid_live_sub_account_selection(
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
        sub_account.strategy_type == STRATEGY_A_STOCK_FEAR
        and config_id
        and sub_account.strategy_config_id == config_id
    )
    if is_bound and not is_current_binding:
        raise HTTPException(status_code=400, detail="所选虚拟子账户已被其他策略绑定")
    return sub_account


def _deactivate_target_positions(
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
            ExternalTradingTargetPosition.strategy_type == STRATEGY_A_STOCK_FEAR,
            ExternalTradingTargetPosition.strategy_config_id == config_id,
        )
    now = datetime.now()
    for row in query.all():
        row.status = "PREVIEW"
        row.updated_at = now


def _sync_live_sub_account_binding(
    db: Session,
    config: AStockFearStrategyConfig,
    *,
    previous_sub_account_id: Optional[int],
) -> None:
    if not config.external_trading_account_id:
        raise HTTPException(status_code=400, detail="使用外部交易账户时必须选择外部交易账户")
    if not config.live_sub_account_id:
        raise HTTPException(status_code=400, detail="使用外部交易账户时必须选择虚拟子账户")

    _validate_external_account_selection(db, config.account_id, config.external_trading_account_id)
    selected_sub_account = _get_valid_live_sub_account_selection(
        db,
        config.account_id,
        config.external_trading_account_id,
        config.live_sub_account_id,
        config_id=config.id,
        require_enabled=bool(config.live_sub_account_id),
    )

    if previous_sub_account_id and previous_sub_account_id != config.live_sub_account_id:
        _deactivate_target_positions(
            db,
            sub_account_id=previous_sub_account_id,
            config_id=config.id,
        )
        previous = db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == previous_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_A_STOCK_FEAR,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if previous:
            previous.strategy_type = None
            previous.strategy_config_id = None
            previous.updated_at = datetime.now()

    if not getattr(config, "enabled", True):
        _deactivate_target_positions(
            db,
            sub_account_id=config.live_sub_account_id or previous_sub_account_id,
            config_id=config.id,
        )

    if selected_sub_account:
        selected_sub_account.strategy_type = STRATEGY_A_STOCK_FEAR
        selected_sub_account.strategy_config_id = config.id
        selected_sub_account.updated_at = datetime.now()


def _config_response(
    config: AStockFearStrategyConfig,
    trading_db: Session,
) -> AStockFearStrategyConfigSchema:
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
    return AStockFearStrategyConfigSchema(**data)


def _assert_unique_target_account(
    db: Session,
    symbol: str,
    trading_account_id: str,
    exclude_config_id: Optional[int] = None,
):
    query = db.query(AStockFearStrategyConfig).filter(
        AStockFearStrategyConfig.symbol == symbol,
        AStockFearStrategyConfig.trading_account_id == trading_account_id,
    )
    if exclude_config_id:
        query = query.filter(AStockFearStrategyConfig.id != exclude_config_id)
    if query.first():
        raise HTTPException(status_code=400, detail="交易标的、账户ID 已存在相同配置")


def _apply_payload_to_config(
    config: AStockFearStrategyConfig,
    payload: AStockFearStrategyConfigPayload,
    trading_account_id: str,
):
    for field in CONFIG_FIELDS:
        setattr(config, field, payload.dict()[field])
    config.trading_account_id = trading_account_id
    config.updated_at = datetime.now()


def _state_response(
    config: AStockFearStrategyConfig,
    state: Optional[AStockFearStrategyState],
) -> AStockFearStrategyStateSchema:
    return AStockFearStrategyStateSchema(
        config_id=config.id,
        account_id=config.account_id,
        symbol=getattr(state, "symbol", None) or config.symbol or "510880.SH",
        last_processed_date=getattr(state, "last_processed_date", None),
        cooldown_remaining_days=int(getattr(state, "cooldown_remaining_days", 0) or 0),
        greed_peak_price=getattr(state, "greed_peak_price", None),
        take_profit_cycle_sell_count=int(getattr(state, "take_profit_cycle_sell_count", 0) or 0),
        updated_at=getattr(state, "updated_at", None),
        has_state=bool(state),
    )


def _resolve_trading_account_id(
    payload: AStockFearStrategyConfigPayload,
    account_id: str,
    db: Session,
    trading_db: Session,
    config_id: Optional[int] = None,
) -> str:
    account = _validate_external_account_selection(trading_db, account_id, payload.external_trading_account_id)
    sub_account = _get_valid_live_sub_account_selection(
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


@router.get("/options")
def get_a_stock_fear_strategy_options(account_id: str = Depends(valid_account)):
    return {
        "target_options": A_STOCK_TARGET_OPTIONS,
        "fear_source_options": [
            {
                "label": config["label"],
                "value": key,
                "symbol": config.get("symbol"),
            }
            for key, config in A_STOCK_FEAR_SOURCE_OPTIONS.items()
        ],
        "preset_pairs": A_STOCK_PRESET_PAIRS,
        "default_request": {
            "symbol": "510880.SH",
            "fear_source": "a_stock_000015_sh",
            "volume_signal_symbol": None,
            "run_time": "09:30",
            "buy_threshold": 30.0,
            "greed_threshold": 70.0,
            "volume_ratio_threshold": 1.3,
            "buy_position_pct": 100.0,
            "cooldown_days": 0,
            "trailing_stop_pct": 0.0,
            "sell_position_pct": 100.0,
            "sell_reduction_basis": "holdings",
            "sell_price_above_avg_cost": False,
            "max_take_profit_sells_per_cycle": 2,
            "min_position_pct_after_take_profit": 0.0,
            "rebalance_threshold_pct": 0.0,
        },
    }


@router.get("/configs", response_model=List[AStockFearStrategyConfigSchema])
def list_a_stock_fear_strategy_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    configs = (
        db.query(AStockFearStrategyConfig)
        .filter(AStockFearStrategyConfig.account_id == account_id)
        .order_by(AStockFearStrategyConfig.updated_at.desc(), AStockFearStrategyConfig.id.desc())
        .all()
    )
    return [_config_response(config, trading_db) for config in configs]


@router.post("/configs", response_model=AStockFearStrategyConfigSchema)
def create_a_stock_fear_strategy_config(
    payload: AStockFearStrategyConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    trading_account_id = _resolve_trading_account_id(payload, account_id, db, trading_db)
    _assert_unique_target_account(db, payload.symbol, trading_account_id)

    config = AStockFearStrategyConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload_to_config(config, payload, trading_account_id)
    db.add(config)

    try:
        db.flush()
        _sync_live_sub_account_binding(trading_db, config, previous_sub_account_id=None)
        trading_db.commit()
        db.commit()
    except IntegrityError:
        db.rollback()
        trading_db.rollback()
        raise HTTPException(status_code=400, detail="交易标的、账户ID 已存在相同配置")
    except HTTPException:
        db.rollback()
        trading_db.rollback()
        raise
    except Exception:
        db.rollback()
        trading_db.rollback()
        raise

    db.refresh(config)
    return _config_response(config, trading_db)


@router.get("/configs/{config_id}", response_model=AStockFearStrategyConfigSchema)
def get_a_stock_fear_strategy_config_by_id(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    return _config_response(_get_config_or_404(db, account_id, config_id), trading_db)


@router.put("/configs/{config_id}", response_model=AStockFearStrategyConfigSchema)
def update_a_stock_fear_strategy_config(
    config_id: int,
    payload: AStockFearStrategyConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    previous_sub_account_id = getattr(config, "live_sub_account_id", None)
    trading_account_id = _resolve_trading_account_id(payload, account_id, db, trading_db, config_id=config.id)
    _assert_unique_target_account(db, payload.symbol, trading_account_id, exclude_config_id=config.id)
    _apply_payload_to_config(config, payload, trading_account_id)

    try:
        db.flush()
        _sync_live_sub_account_binding(trading_db, config, previous_sub_account_id=previous_sub_account_id)
        trading_db.commit()
        db.commit()
    except IntegrityError:
        db.rollback()
        trading_db.rollback()
        raise HTTPException(status_code=400, detail="交易标的、账户ID 已存在相同配置")
    except HTTPException:
        db.rollback()
        trading_db.rollback()
        raise
    except Exception:
        db.rollback()
        trading_db.rollback()
        raise

    db.refresh(config)
    return _config_response(config, trading_db)


@router.delete("/configs/{config_id}")
def delete_a_stock_fear_strategy_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    if getattr(config, "live_sub_account_id", None):
        _deactivate_target_positions(
            trading_db,
            sub_account_id=config.live_sub_account_id,
            config_id=config.id,
        )
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_A_STOCK_FEAR,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if sub_account:
            sub_account.strategy_type = None
            sub_account.strategy_config_id = None
            sub_account.updated_at = datetime.now()
    db.query(AStockFearStrategyState).filter(AStockFearStrategyState.config_id == config.id).delete()
    db.query(AStockFearStrategyLog).filter(AStockFearStrategyLog.config_id == config.id).delete()
    db.delete(config)
    trading_db.commit()
    db.commit()
    return {"message": "配置已删除"}


@router.get("/configs/{config_id}/logs", response_model=List[AStockFearStrategyLogSchema])
def get_a_stock_fear_strategy_logs_by_config(
    config_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    return (
        db.query(AStockFearStrategyLog)
        .filter(AStockFearStrategyLog.config_id == config.id)
        .order_by(AStockFearStrategyLog.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )


@router.get("/configs/{config_id}/state", response_model=AStockFearStrategyStateSchema)
def get_a_stock_fear_strategy_state_by_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    state = db.query(AStockFearStrategyState).filter(AStockFearStrategyState.config_id == config.id).first()
    return _state_response(config, state)


@router.put("/configs/{config_id}/state", response_model=AStockFearStrategyStateSchema)
def update_a_stock_fear_strategy_state_by_config(
    config_id: int,
    payload: AStockFearStrategyStatePayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    state = db.query(AStockFearStrategyState).filter(AStockFearStrategyState.config_id == config.id).first()
    if not state:
        state = AStockFearStrategyState(
            config_id=config.id,
            account_id=config.account_id,
            symbol=config.symbol or "510880.SH",
        )
        db.add(state)
    state.account_id = config.account_id
    state.symbol = config.symbol or "510880.SH"
    state.last_processed_date = payload.last_processed_date
    state.cooldown_remaining_days = int(payload.cooldown_remaining_days or 0)
    state.greed_peak_price = payload.greed_peak_price
    state.take_profit_cycle_sell_count = int(payload.take_profit_cycle_sell_count or 0)
    state.updated_at = datetime.now()
    db.commit()
    db.refresh(state)
    return _state_response(config, state)


@router.post("/configs/{config_id}/manual-check")
def manual_check_a_stock_fear_strategy_by_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    try:
        AStockFearStrategyTrader().trigger_manual_run(config.id, account_id)
        return {"message": "已在后台触发一次 A股情绪量能策略检查"}
    except Exception as exc:
        logger.error("Failed to trigger A股 fear strategy manually: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
