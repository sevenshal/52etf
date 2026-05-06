import logging
import re
from datetime import date, datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session as ORMSession

from ...core.database import (
    AStockInnovationMomentumConfig,
    AStockInnovationMomentumEquity,
    AStockInnovationMomentumEvent,
    AStockInnovationMomentumHolding,
    AStockInnovationMomentumLog,
    AStockInnovationMomentumTrade,
    get_db,
    get_db_ctx,
)
from ...robot.a_stock_innovation_momentum_virtual import (
    BENCHMARK_SYMBOL,
    DAILY_PRICE_SOURCE,
    DEFAULT_COMMISSION_PCT,
    DEFAULT_INDEX_WEIGHT_BLEND,
    DEFAULT_INITIAL_CAPITAL,
    DEFAULT_LOT_SIZE,
    DEFAULT_MAX_POSITIONS,
    DEFAULT_MIN_LISTING_DAYS,
    DEFAULT_MOMENTUM_WEIGHTS,
    DEFAULT_NAME,
    DEFAULT_REBALANCE_FREQUENCY,
    DEFAULT_SELL_RANK_MULTIPLIER,
    DEFAULT_SLIPPAGE_PCT,
    DEFAULT_START_DATE,
    SUPPORTED_MOMENTUM_WINDOWS,
    SUPPORTED_REBALANCE_FREQUENCIES,
    AStockInnovationMomentumVirtualEngine,
)
from .account import valid_account


router = APIRouter(prefix="/api/a-stock-innovation-momentum-live", tags=["A Stock Innovation Momentum Live"])
logger = logging.getLogger(__name__)
CN_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_AUTO_SYNC_TIME = "15:30"
AUTO_SYNC_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AStockInnovationMomentumConfigPayload(BaseModel):
    name: str = DEFAULT_NAME
    enabled: bool = True
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    start_date: date = DEFAULT_START_DATE
    min_listing_days: int = DEFAULT_MIN_LISTING_DAYS
    momentum_weights: Dict[str, float] = Field(default_factory=lambda: DEFAULT_MOMENTUM_WEIGHTS.copy())
    max_positions: int = DEFAULT_MAX_POSITIONS
    sell_rank_multiplier: float = DEFAULT_SELL_RANK_MULTIPLIER
    index_weight_blend: float = DEFAULT_INDEX_WEIGHT_BLEND
    rebalance_frequency: str = DEFAULT_REBALANCE_FREQUENCY
    commission_pct: float = DEFAULT_COMMISSION_PCT
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    lot_size: int = DEFAULT_LOT_SIZE
    auto_sync_enabled: bool = True
    auto_sync_time: str = DEFAULT_AUTO_SYNC_TIME

    @validator("name")
    def validate_name(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("虚拟盘名称不能为空")
        return text

    @validator("initial_capital")
    def validate_initial_capital(cls, value):
        if value <= 0:
            raise ValueError("初始资金必须大于0")
        return value

    @validator("min_listing_days")
    def validate_min_listing_days(cls, value):
        if value < 0:
            raise ValueError("最少上市天数不能为负数")
        return value

    @validator("momentum_weights", pre=True)
    def validate_momentum_weights(cls, value):
        raw_weights = value if isinstance(value, dict) else DEFAULT_MOMENTUM_WEIGHTS
        normalized = {}
        for window in SUPPORTED_MOMENTUM_WINDOWS:
            raw_value = raw_weights.get(str(window), raw_weights.get(window, 0))
            try:
                weight = float(raw_value or 0)
            except (TypeError, ValueError):
                raise ValueError(f"{window}日动量权重必须是数字")
            if weight < 0:
                raise ValueError(f"{window}日动量权重不能为负数")
            normalized[str(window)] = weight
        if sum(normalized.values()) <= 0:
            raise ValueError("至少设置一个大于0的动量权重")
        return normalized

    @validator("max_positions")
    def validate_max_positions(cls, value):
        if value < 1:
            raise ValueError("最大持仓数不能小于1")
        if value > 100:
            raise ValueError("最大持仓数不能大于100")
        return value

    @validator("sell_rank_multiplier")
    def validate_sell_rank_multiplier(cls, value):
        if value < 1:
            raise ValueError("卖出排名倍数不能小于1")
        if value > 20:
            raise ValueError("卖出排名倍数不能大于20")
        return value

    @validator("index_weight_blend")
    def validate_index_weight_blend(cls, value):
        if value < 0 or value > 1:
            raise ValueError("成分权重倾斜必须在0到1之间")
        return value

    @validator("rebalance_frequency")
    def validate_rebalance_frequency(cls, value):
        text = str(value or DEFAULT_REBALANCE_FREQUENCY).strip().lower()
        if text not in SUPPORTED_REBALANCE_FREQUENCIES:
            raise ValueError("调仓周期必须是 daily、weekly 或 monthly")
        return text

    @validator("commission_pct", "slippage_pct")
    def validate_non_negative(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("lot_size")
    def validate_lot_size(cls, value):
        if value < 1:
            raise ValueError("交易单位不能小于1")
        return value

    @validator("auto_sync_time")
    def validate_auto_sync_time(cls, value):
        text = str(value or DEFAULT_AUTO_SYNC_TIME).strip()
        if not AUTO_SYNC_TIME_PATTERN.match(text):
            raise ValueError("自动同步时间格式应为 HH:mm")
        return text


def _config_to_dict(config: AStockInnovationMomentumConfig) -> Dict:
    rebalance_frequency = getattr(config, "rebalance_frequency", DEFAULT_REBALANCE_FREQUENCY)
    if rebalance_frequency not in SUPPORTED_REBALANCE_FREQUENCIES:
        rebalance_frequency = DEFAULT_REBALANCE_FREQUENCY
    return {
        "id": config.id,
        "account_id": config.account_id,
        "name": config.name,
        "enabled": bool(config.enabled),
        "initial_capital": config.initial_capital,
        "start_date": config.start_date.isoformat() if config.start_date else None,
        "min_listing_days": config.min_listing_days,
        "momentum_weights": config.momentum_weights or DEFAULT_MOMENTUM_WEIGHTS.copy(),
        "max_positions": config.max_positions,
        "sell_rank_multiplier": config.sell_rank_multiplier,
        "index_weight_blend": config.index_weight_blend,
        "rebalance_frequency": rebalance_frequency,
        "commission_pct": config.commission_pct,
        "slippage_pct": config.slippage_pct,
        "lot_size": config.lot_size,
        "auto_sync_enabled": bool(config.auto_sync_enabled),
        "auto_sync_time": config.auto_sync_time or DEFAULT_AUTO_SYNC_TIME,
        "last_auto_sync_at": config.last_auto_sync_at,
        "last_sync_at": config.last_sync_at,
        "last_sync_status": config.last_sync_status,
        "last_sync_message": config.last_sync_message,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _get_config_or_404(db: ORMSession, account_id: str, config_id: int) -> AStockInnovationMomentumConfig:
    config = db.query(AStockInnovationMomentumConfig).filter(
        AStockInnovationMomentumConfig.id == config_id,
        AStockInnovationMomentumConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="未找到A股创新100动量虚拟盘配置")
    return config


def _apply_payload(config: AStockInnovationMomentumConfig, payload: AStockInnovationMomentumConfigPayload):
    for field, value in payload.dict().items():
        setattr(config, field, value)
    config.updated_at = datetime.now()


def _replace_config_runtime_state(
    db: ORMSession,
    config: AStockInnovationMomentumConfig,
    result: Dict,
    trigger_source: str = "manual",
):
    db.query(AStockInnovationMomentumEvent).filter(AStockInnovationMomentumEvent.config_id == config.id).delete()
    db.query(AStockInnovationMomentumTrade).filter(AStockInnovationMomentumTrade.config_id == config.id).delete()
    db.query(AStockInnovationMomentumHolding).filter(AStockInnovationMomentumHolding.config_id == config.id).delete()
    db.query(AStockInnovationMomentumEquity).filter(AStockInnovationMomentumEquity.config_id == config.id).delete()
    db.query(AStockInnovationMomentumLog).filter(AStockInnovationMomentumLog.config_id == config.id).delete()

    now = datetime.now()
    for item in result.get("equity_curve") or []:
        db.add(AStockInnovationMomentumEquity(
            config_id=config.id,
            account_id=config.account_id,
            date=date.fromisoformat(item["date"]),
            value=item.get("value"),
            cash=item.get("cash"),
            position_value=item.get("position_value"),
            drawdown=item.get("drawdown"),
            created_at=now,
            updated_at=now,
        ))

    for item in result.get("events") or []:
        db.add(AStockInnovationMomentumEvent(
            config_id=config.id,
            account_id=config.account_id,
            symbol=item.get("symbol"),
            date=date.fromisoformat(item["date"]),
            direction=item.get("direction"),
            signal_price=item.get("signal_price"),
            turnover=item.get("turnover"),
            annualized_volatility_pct=item.get("annualized_volatility_pct"),
            threshold_pct=item.get("threshold_pct"),
            payload=item.get("payload"),
            price_source=item.get("price_source") or DAILY_PRICE_SOURCE,
            created_at=now,
        ))

    for item in result.get("trades") or []:
        db.add(AStockInnovationMomentumTrade(
            config_id=config.id,
            account_id=config.account_id,
            date=date.fromisoformat(item["date"]),
            signal_date=date.fromisoformat(item["signal_date"]) if item.get("signal_date") else None,
            action=item.get("action"),
            symbol=item.get("symbol"),
            name=item.get("name"),
            price=item.get("price"),
            quantity=item.get("quantity"),
            amount=item.get("amount"),
            commission=item.get("commission"),
            profit=item.get("profit"),
            profit_pct=item.get("profit_pct"),
            reason=item.get("reason"),
            reason_detail=item.get("reason_detail"),
            cash_after=item.get("cash_after"),
            portfolio_value_after=item.get("portfolio_value_after"),
            symbol_market_value_after=item.get("symbol_market_value_after"),
            symbol_weight_pct_after=item.get("symbol_weight_pct_after"),
            price_source=item.get("price_source") or DAILY_PRICE_SOURCE,
            created_at=now,
        ))

    for item in result.get("current_holdings") or []:
        db.add(AStockInnovationMomentumHolding(
            config_id=config.id,
            account_id=config.account_id,
            symbol=item.get("symbol"),
            name=item.get("name"),
            shares=item.get("shares") or 0,
            price=item.get("price"),
            avg_cost=item.get("avg_cost"),
            entry_date=date.fromisoformat(item["entry_date"]) if item.get("entry_date") else None,
            market_value=item.get("market_value"),
            actual_weight_pct=item.get("actual_weight_pct"),
            updated_at=now,
        ))

    db.add(AStockInnovationMomentumLog(
        config_id=config.id,
        account_id=config.account_id,
        timestamp=now,
        level="INFO",
        action="SYNC",
        message="A股创新100动量虚拟盘已同步到最新状态",
        payload={
            "trigger_source": trigger_source,
            "metrics": result.get("metrics"),
            "meta": result.get("meta"),
            "errors": result.get("errors"),
            "benchmark_curve": result.get("benchmark_curve"),
            "yearly_stats": result.get("yearly_stats"),
        },
    ))
    config.last_sync_at = now
    config.last_sync_status = "success"
    config.last_sync_message = "同步完成"
    config.updated_at = now


def _mark_sync_running(db: ORMSession, config: AStockInnovationMomentumConfig, message: str = "同步中"):
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "running"
    config.last_sync_message = message
    config.updated_at = now


def _mark_sync_failed(db: ORMSession, config_id: int, account_id: str, exc: Exception):
    config = db.query(AStockInnovationMomentumConfig).filter(
        AStockInnovationMomentumConfig.id == config_id,
        AStockInnovationMomentumConfig.account_id == account_id,
    ).first()
    if not config:
        return
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "failed"
    config.last_sync_message = str(exc)[:500]
    config.updated_at = now
    db.add(AStockInnovationMomentumLog(
        config_id=config.id,
        account_id=account_id,
        timestamp=now,
        level="ERROR",
        action="SYNC_FAILED",
        message=str(exc),
    ))


def _sync_config_now(
    db: ORMSession,
    config: AStockInnovationMomentumConfig,
    trigger_source: str = "manual",
    end_date: Optional[date] = None,
) -> Dict:
    result = AStockInnovationMomentumVirtualEngine(db, config, end_date=end_date).run()
    _replace_config_runtime_state(db, config, result, trigger_source=trigger_source)
    return result


def _get_latest_sync_payload(db: ORMSession, config: AStockInnovationMomentumConfig) -> Dict:
    latest_sync_log = (
        db.query(AStockInnovationMomentumLog.payload)
        .filter(
            AStockInnovationMomentumLog.config_id == config.id,
            AStockInnovationMomentumLog.action == "SYNC",
        )
        .order_by(AStockInnovationMomentumLog.timestamp.desc(), AStockInnovationMomentumLog.id.desc())
        .first()
    )
    return latest_sync_log[0] if latest_sync_log and latest_sync_log[0] else {}


def _runtime_summary(db: ORMSession, config: AStockInnovationMomentumConfig) -> Dict:
    latest_equity = (
        db.query(AStockInnovationMomentumEquity.date, AStockInnovationMomentumEquity.value)
        .filter(AStockInnovationMomentumEquity.config_id == config.id)
        .order_by(AStockInnovationMomentumEquity.date.desc())
        .first()
    )
    metrics = (_get_latest_sync_payload(db, config).get("metrics") or {})
    return {
        "latest_date": latest_equity.date.isoformat() if latest_equity else None,
        "portfolio_value": latest_equity.value if latest_equity else None,
        "total_return": metrics.get("total_return"),
        "benchmark_total_return": metrics.get("benchmark_total_return"),
        "excess_return": metrics.get("excess_return"),
        "annualized_return": metrics.get("annualized_return"),
        "trade_count": metrics.get("trade_count") or db.query(AStockInnovationMomentumTrade).filter(AStockInnovationMomentumTrade.config_id == config.id).count(),
        "signal_count": metrics.get("signal_count") or db.query(AStockInnovationMomentumEvent).filter(AStockInnovationMomentumEvent.config_id == config.id).count(),
        "holding_count": db.query(AStockInnovationMomentumHolding).filter(AStockInnovationMomentumHolding.config_id == config.id).count(),
    }


@router.get("/defaults")
def get_defaults():
    return {
        "name": DEFAULT_NAME,
        "initial_capital": DEFAULT_INITIAL_CAPITAL,
        "start_date": DEFAULT_START_DATE.isoformat(),
        "min_listing_days": DEFAULT_MIN_LISTING_DAYS,
        "momentum_weights": DEFAULT_MOMENTUM_WEIGHTS.copy(),
        "max_positions": DEFAULT_MAX_POSITIONS,
        "sell_rank_multiplier": DEFAULT_SELL_RANK_MULTIPLIER,
        "index_weight_blend": DEFAULT_INDEX_WEIGHT_BLEND,
        "rebalance_frequency": DEFAULT_REBALANCE_FREQUENCY,
        "commission_pct": DEFAULT_COMMISSION_PCT,
        "slippage_pct": DEFAULT_SLIPPAGE_PCT,
        "lot_size": DEFAULT_LOT_SIZE,
        "auto_sync_time": DEFAULT_AUTO_SYNC_TIME,
        "benchmark_symbol": BENCHMARK_SYMBOL,
    }


@router.get("/configs")
def list_configs(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    configs = (
        db.query(AStockInnovationMomentumConfig)
        .filter(AStockInnovationMomentumConfig.account_id == account_id)
        .order_by(AStockInnovationMomentumConfig.updated_at.desc(), AStockInnovationMomentumConfig.id.desc())
        .all()
    )
    return [
        {
            **_config_to_dict(config),
            "runtime": _runtime_summary(db, config),
        }
        for config in configs
    ]


@router.post("/configs")
def create_config(
    payload: AStockInnovationMomentumConfigPayload,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = AStockInnovationMomentumConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload(config, payload)
    db.add(config)
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.put("/configs/{config_id}")
def update_config(
    config_id: int,
    payload: AStockInnovationMomentumConfigPayload,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _apply_payload(config, payload)
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.delete("/configs/{config_id}")
def delete_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    db.query(AStockInnovationMomentumEvent).filter(AStockInnovationMomentumEvent.config_id == config.id).delete()
    db.query(AStockInnovationMomentumTrade).filter(AStockInnovationMomentumTrade.config_id == config.id).delete()
    db.query(AStockInnovationMomentumHolding).filter(AStockInnovationMomentumHolding.config_id == config.id).delete()
    db.query(AStockInnovationMomentumEquity).filter(AStockInnovationMomentumEquity.config_id == config.id).delete()
    db.query(AStockInnovationMomentumLog).filter(AStockInnovationMomentumLog.config_id == config.id).delete()
    db.delete(config)
    db.commit()
    return {"message": "已删除A股创新100动量虚拟盘配置"}


@router.post("/configs/{config_id}/sync")
def sync_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _mark_sync_running(db, config)
    db.commit()
    try:
        result = _sync_config_now(db, config, trigger_source="manual")
        db.commit()
        return {"message": "同步完成", "config": _config_to_dict(config), "summary": result.get("metrics")}
    except Exception as exc:
        db.rollback()
        _mark_sync_failed(db, config_id, account_id, exc)
        db.commit()
        logger.exception("A stock innovation momentum sync failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/configs/sync-enabled")
def sync_enabled_configs(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    configs = db.query(AStockInnovationMomentumConfig).filter(
        AStockInnovationMomentumConfig.account_id == account_id,
        AStockInnovationMomentumConfig.enabled == True,  # noqa: E712
    ).all()
    synced = []
    errors = []
    for config in configs:
        config_id = config.id
        config_name = config.name
        _mark_sync_running(db, config)
        db.commit()
        try:
            result = _sync_config_now(db, config, trigger_source="manual_all")
            db.commit()
            synced.append({"id": config_id, "name": config_name, "summary": result.get("metrics")})
        except Exception as exc:
            db.rollback()
            _mark_sync_failed(db, config_id, account_id, exc)
            db.commit()
            errors.append({"id": config_id, "name": config_name, "error": str(exc)})
    return {"synced": synced, "errors": errors}


def _auto_sync_already_attempted(config: AStockInnovationMomentumConfig, now_cn: datetime) -> bool:
    if not config.last_auto_sync_at:
        return False
    last_at = config.last_auto_sync_at
    if last_at.tzinfo:
        return last_at.astimezone(CN_TZ).date() == now_cn.date()
    return last_at.date() == now_cn.date()


def _is_auto_sync_due(config: AStockInnovationMomentumConfig, now_cn: datetime) -> bool:
    auto_sync_time = config.auto_sync_time or DEFAULT_AUTO_SYNC_TIME
    if not AUTO_SYNC_TIME_PATTERN.match(auto_sync_time):
        auto_sync_time = DEFAULT_AUTO_SYNC_TIME
    return now_cn.strftime("%H:%M") >= auto_sync_time


def sync_due_a_stock_innovation_momentum_configs_for_auto_sync(now_cn: Optional[datetime] = None) -> Dict:
    now_cn = now_cn or datetime.now(CN_TZ)
    result = {
        "synced": [],
        "errors": [],
        "skipped": [],
        "current_time": now_cn.strftime("%H:%M"),
        "timezone": "Asia/Shanghai",
    }
    if now_cn.weekday() >= 5:
        result["skipped"].append({"reason": "A股周末休市"})
        return result

    with get_db_ctx() as db:
        configs = (
            db.query(AStockInnovationMomentumConfig)
            .filter(
                AStockInnovationMomentumConfig.enabled == True,  # noqa: E712
                AStockInnovationMomentumConfig.auto_sync_enabled == True,  # noqa: E712
            )
            .order_by(AStockInnovationMomentumConfig.account_id.asc(), AStockInnovationMomentumConfig.id.asc())
            .all()
        )
        for config in configs:
            config_id = config.id
            account_id = config.account_id
            config_name = config.name
            if not _is_auto_sync_due(config, now_cn):
                result["skipped"].append({"id": config_id, "account_id": account_id, "name": config_name, "reason": "未到自动同步时间"})
                continue
            if _auto_sync_already_attempted(config, now_cn):
                result["skipped"].append({"id": config_id, "account_id": account_id, "name": config_name, "reason": "今日已自动触发"})
                continue

            attempt_at = now_cn.replace(tzinfo=None)
            _mark_sync_running(db, config, message=f"自动同步中（北京时间 {now_cn.strftime('%H:%M')}）")
            config.last_auto_sync_at = attempt_at
            db.commit()
            try:
                sync_result = _sync_config_now(db, config, trigger_source="module_auto")
                config.last_auto_sync_at = attempt_at
                db.commit()
                result["synced"].append({"id": config_id, "account_id": account_id, "name": config_name, "summary": sync_result.get("metrics")})
            except Exception as exc:
                db.rollback()
                failed_config = db.query(AStockInnovationMomentumConfig).filter(
                    AStockInnovationMomentumConfig.id == config_id,
                    AStockInnovationMomentumConfig.account_id == account_id,
                ).first()
                if failed_config:
                    failed_config.last_auto_sync_at = attempt_at
                _mark_sync_failed(db, config_id, account_id, exc)
                db.commit()
                logger.exception("A stock innovation momentum auto sync failed")
                result["errors"].append({"id": config_id, "account_id": account_id, "name": config_name, "error": str(exc)})
    return result


def sync_all_enabled_a_stock_innovation_momentum_configs_for_scheduler() -> Dict:
    return sync_due_a_stock_innovation_momentum_configs_for_auto_sync(datetime.now(CN_TZ).replace(hour=23, minute=59))


@router.get("/configs/{config_id}/detail")
def get_detail(
    config_id: int,
    event_limit: int = Query(500, ge=1, le=5000),
    trade_limit: int = Query(500, ge=1, le=5000),
    log_limit: int = Query(200, ge=1, le=1000),
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    equity = (
        db.query(AStockInnovationMomentumEquity)
        .filter(AStockInnovationMomentumEquity.config_id == config.id)
        .order_by(AStockInnovationMomentumEquity.date.asc())
        .all()
    )
    holdings = (
        db.query(AStockInnovationMomentumHolding)
        .filter(AStockInnovationMomentumHolding.config_id == config.id)
        .order_by(AStockInnovationMomentumHolding.market_value.desc())
        .all()
    )
    trades = (
        db.query(AStockInnovationMomentumTrade)
        .filter(AStockInnovationMomentumTrade.config_id == config.id)
        .order_by(AStockInnovationMomentumTrade.date.desc(), AStockInnovationMomentumTrade.id.desc())
        .limit(trade_limit)
        .all()
    )
    events = (
        db.query(AStockInnovationMomentumEvent)
        .filter(AStockInnovationMomentumEvent.config_id == config.id)
        .order_by(AStockInnovationMomentumEvent.date.desc(), AStockInnovationMomentumEvent.id.desc())
        .limit(event_limit)
        .all()
    )
    logs = (
        db.query(AStockInnovationMomentumLog)
        .filter(AStockInnovationMomentumLog.config_id == config.id)
        .order_by(AStockInnovationMomentumLog.timestamp.desc(), AStockInnovationMomentumLog.id.desc())
        .limit(log_limit)
        .all()
    )
    sync_payload = _get_latest_sync_payload(db, config)
    metrics = sync_payload.get("metrics") or {}
    latest_equity = equity[-1] if equity else None
    initial_value = equity[0].value if equity else None
    total_return = (
        (latest_equity.value / initial_value - 1) * 100
        if latest_equity and initial_value and initial_value > 0
        else None
    )

    return {
        "config": _config_to_dict(config),
        "summary": {
            "latest_date": latest_equity.date.isoformat() if latest_equity else None,
            "portfolio_value": latest_equity.value if latest_equity else None,
            "total_return": total_return,
            "metrics": metrics,
            "meta": sync_payload.get("meta") or {},
            "errors": sync_payload.get("errors") or [],
            "yearly_stats": sync_payload.get("yearly_stats") or [],
            "trade_count": db.query(AStockInnovationMomentumTrade).filter(AStockInnovationMomentumTrade.config_id == config.id).count(),
            "signal_count": db.query(AStockInnovationMomentumEvent).filter(AStockInnovationMomentumEvent.config_id == config.id).count(),
            "holding_count": len(holdings),
        },
        "benchmark_curve": sync_payload.get("benchmark_curve") or [],
        "yearly_stats": sync_payload.get("yearly_stats") or [],
        "equity_curve": [
            {
                "date": item.date.isoformat(),
                "value": item.value,
                "cash": item.cash,
                "position_value": item.position_value,
                "drawdown": item.drawdown,
            }
            for item in equity
        ],
        "holdings": [
            {
                "symbol": item.symbol,
                "name": item.name,
                "shares": item.shares,
                "price": item.price,
                "avg_cost": item.avg_cost,
                "entry_date": item.entry_date.isoformat() if item.entry_date else None,
                "market_value": item.market_value,
                "actual_weight_pct": item.actual_weight_pct,
            }
            for item in holdings
        ],
        "trades": [
            {
                "id": item.id,
                "date": item.date.isoformat() if item.date else None,
                "signal_date": item.signal_date.isoformat() if item.signal_date else None,
                "action": item.action,
                "symbol": item.symbol,
                "name": item.name,
                "price": item.price,
                "quantity": item.quantity,
                "amount": item.amount,
                "commission": item.commission,
                "profit": item.profit,
                "profit_pct": item.profit_pct,
                "reason": item.reason,
                "reason_detail": item.reason_detail,
                "cash_after": item.cash_after,
                "portfolio_value_after": item.portfolio_value_after,
                "symbol_market_value_after": item.symbol_market_value_after,
                "symbol_weight_pct_after": item.symbol_weight_pct_after,
                "price_source": item.price_source,
            }
            for item in trades
        ],
        "events": [
            {
                "id": item.id,
                "date": item.date.isoformat() if item.date else None,
                "symbol": item.symbol,
                "direction": item.direction,
                "signal_price": item.signal_price,
                "turnover": item.turnover,
                "annualized_volatility_pct": item.annualized_volatility_pct,
                "threshold_pct": item.threshold_pct,
                "payload": item.payload,
                "price_source": item.price_source,
            }
            for item in events
        ],
        "logs": [
            {
                "id": item.id,
                "timestamp": item.timestamp.isoformat() if item.timestamp else None,
                "date": item.date.isoformat() if item.date else None,
                "level": item.level,
                "action": item.action,
                "message": item.message,
                "payload": item.payload,
            }
            for item in logs
        ],
    }
