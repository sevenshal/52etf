import logging
import re
from datetime import date, datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session as ORMSession

from ...core.database import (
    LongPortAccount,
    USStockSignalVirtualConfig,
    USStockSignalVirtualEquity,
    USStockSignalVirtualEvent,
    USStockSignalVirtualHolding,
    USStockSignalVirtualLog,
    USStockSignalVirtualTrade,
    get_db,
    get_db_ctx,
)
from ...core.services.longport import LongPortService
from ...core.services.market import MarketService
from ...core.services.quote import QuoteService
from ...robot.us_stock_signal_virtual import (
    CANDIDATE_ETF_OPTIONS,
    DAILY_PRICE_SOURCE,
    DEFAULT_CANDIDATE_ETFS,
    USStockSignalVirtualEngine,
)
from .account import valid_account

router = APIRouter(prefix="/api/us-stock-signal-live", tags=["US Stock Signal Live"])
logger = logging.getLogger(__name__)
EASTERN_TZ = ZoneInfo("US/Eastern")
DEFAULT_AUTO_SYNC_TIME = "16:15"
AUTO_SYNC_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class USStockSignalConfigPayload(BaseModel):
    name: str = "美股成分股买卖点虚拟盘"
    enabled: bool = True
    candidate_etfs: List[str] = Field(default_factory=lambda: DEFAULT_CANDIDATE_ETFS.copy())
    initial_capital: float = 100_000.0
    start_date: date = date(2020, 1, 2)
    window: int = 125
    stabilization_period: int = 10
    volatility_floor_pct: float = 15.0
    volatility_cap_pct: float = 45.0
    min_listing_days: int = 365
    volume_std_multiplier: float = 1.0
    max_positions: int = 10
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    auto_sync_enabled: bool = True
    auto_sync_time: str = DEFAULT_AUTO_SYNC_TIME

    @validator("name")
    def validate_name(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("虚拟盘名称不能为空")
        return text

    @validator("candidate_etfs", pre=True)
    def validate_candidate_etfs(cls, value):
        if value is None:
            return DEFAULT_CANDIDATE_ETFS.copy()
        items = value.replace(",", " ").split() if isinstance(value, str) else list(value)
        allowed = {item["value"] for item in CANDIDATE_ETF_OPTIONS}
        normalized = []
        for item in items:
            text = str(item or "").strip().upper()
            if text in {"SP500", "S&P500", "SPY"}:
                text = "SPY.US"
            if text in {"NASDAQ100", "NDX", "QQQ"}:
                text = "QQQ.US"
            if text not in allowed:
                raise ValueError(f"不支持的候选ETF: {item}")
            if text not in normalized:
                normalized.append(text)
        if not normalized:
            raise ValueError("至少选择一个候选ETF")
        return normalized

    @validator("window")
    def validate_window(cls, value):
        if value < 20:
            raise ValueError("窗口大小不能小于20")
        return value

    @validator("stabilization_period")
    def validate_stabilization_period(cls, value):
        if value < 1:
            raise ValueError("企稳天数不能小于1")
        return value

    @validator("max_positions")
    def validate_max_positions(cls, value):
        if value < 1:
            raise ValueError("最大持仓数不能小于1")
        return value

    @validator("min_listing_days")
    def validate_min_listing_days(cls, value):
        if value < 0:
            raise ValueError("最少上市天数不能为负数")
        return value

    @validator("initial_capital")
    def validate_initial_capital(cls, value):
        if value <= 0:
            raise ValueError("初始资金必须大于0")
        return value

    @validator("volatility_floor_pct", "volatility_cap_pct", "volume_std_multiplier", "commission_pct", "slippage_pct")
    def validate_non_negative(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("volatility_cap_pct")
    def validate_volatility_cap(cls, value, values):
        lower = values.get("volatility_floor_pct")
        if lower is not None and value < lower:
            raise ValueError("波动阈值上限不能小于下限")
        return value

    @validator("auto_sync_time")
    def validate_auto_sync_time(cls, value):
        text = str(value or DEFAULT_AUTO_SYNC_TIME).strip()
        if not AUTO_SYNC_TIME_PATTERN.match(text):
            raise ValueError("自动同步时间格式应为 HH:mm")
        return text


def _config_to_dict(config: USStockSignalVirtualConfig) -> Dict:
    return {
        "id": config.id,
        "account_id": config.account_id,
        "name": config.name,
        "enabled": bool(config.enabled),
        "candidate_etfs": config.candidate_etfs or DEFAULT_CANDIDATE_ETFS.copy(),
        "initial_capital": config.initial_capital,
        "start_date": config.start_date.isoformat() if config.start_date else None,
        "window": config.window,
        "stabilization_period": config.stabilization_period,
        "volatility_floor_pct": config.volatility_floor_pct,
        "volatility_cap_pct": config.volatility_cap_pct,
        "min_listing_days": getattr(config, "min_listing_days", 365),
        "volume_std_multiplier": config.volume_std_multiplier,
        "max_positions": config.max_positions,
        "commission_pct": config.commission_pct,
        "slippage_pct": config.slippage_pct,
        "auto_sync_enabled": bool(config.auto_sync_enabled),
        "auto_sync_time": config.auto_sync_time or DEFAULT_AUTO_SYNC_TIME,
        "last_auto_sync_at": config.last_auto_sync_at,
        "last_sync_at": config.last_sync_at,
        "last_sync_status": config.last_sync_status,
        "last_sync_message": config.last_sync_message,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _get_config_or_404(db: ORMSession, account_id: str, config_id: int) -> USStockSignalVirtualConfig:
    config = db.query(USStockSignalVirtualConfig).filter(
        USStockSignalVirtualConfig.id == config_id,
        USStockSignalVirtualConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="未找到美股买卖点虚拟盘配置")
    return config


def _apply_payload(config: USStockSignalVirtualConfig, payload: USStockSignalConfigPayload):
    for field, value in payload.dict().items():
        setattr(config, field, value)
    config.lot_size = 1
    config.updated_at = datetime.now()


def _get_longport_account_id(db: ORMSession, account_id: str) -> str:
    account = db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).first()
    if account and account.lp_account_id:
        return account.lp_account_id
    return "LBPT10001248"


def _get_quote_service(db: ORMSession, account_id: str) -> QuoteService:
    return QuoteService(LongPortService.get_instance(_get_longport_account_id(db, account_id)))


def _replace_config_runtime_state(
    db: ORMSession,
    config: USStockSignalVirtualConfig,
    result: Dict,
    trigger_source: str = "manual",
):
    db.query(USStockSignalVirtualEvent).filter(USStockSignalVirtualEvent.config_id == config.id).delete()
    db.query(USStockSignalVirtualTrade).filter(USStockSignalVirtualTrade.config_id == config.id).delete()
    db.query(USStockSignalVirtualHolding).filter(USStockSignalVirtualHolding.config_id == config.id).delete()
    db.query(USStockSignalVirtualEquity).filter(USStockSignalVirtualEquity.config_id == config.id).delete()
    db.query(USStockSignalVirtualLog).filter(USStockSignalVirtualLog.config_id == config.id).delete()

    now = datetime.now()
    for item in result.get("equity_curve") or []:
        db.add(USStockSignalVirtualEquity(
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
        db.add(USStockSignalVirtualEvent(
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
        db.add(USStockSignalVirtualTrade(
            config_id=config.id,
            account_id=config.account_id,
            date=date.fromisoformat(item["date"]),
            signal_date=date.fromisoformat(item["signal_date"]) if item.get("signal_date") else None,
            action=item.get("action"),
            symbol=item.get("symbol"),
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
        db.add(USStockSignalVirtualHolding(
            config_id=config.id,
            account_id=config.account_id,
            symbol=item.get("symbol"),
            shares=item.get("shares") or 0,
            price=item.get("price"),
            avg_cost=item.get("avg_cost"),
            entry_date=date.fromisoformat(item["entry_date"]) if item.get("entry_date") else None,
            market_value=item.get("market_value"),
            actual_weight_pct=item.get("actual_weight_pct"),
            updated_at=now,
        ))

    db.add(USStockSignalVirtualLog(
        config_id=config.id,
        account_id=config.account_id,
        timestamp=now,
        level="INFO",
        action="SYNC",
        message="美股买卖点虚拟盘已同步到最新状态",
        payload={
            "trigger_source": trigger_source,
            "metrics": result.get("metrics"),
            "meta": result.get("meta"),
            "errors": result.get("errors"),
            "benchmark_curve": result.get("benchmark_curve"),
        },
    ))
    config.last_sync_at = now
    config.last_sync_status = "success"
    config.last_sync_message = "同步完成"
    config.updated_at = now


def _mark_sync_running(db: ORMSession, config: USStockSignalVirtualConfig, message: str = "同步中"):
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "running"
    config.last_sync_message = message
    config.updated_at = now


def _mark_sync_failed(db: ORMSession, config_id: int, account_id: str, exc: Exception):
    config = db.query(USStockSignalVirtualConfig).filter(
        USStockSignalVirtualConfig.id == config_id,
        USStockSignalVirtualConfig.account_id == account_id,
    ).first()
    if not config:
        return
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "failed"
    config.last_sync_message = str(exc)[:500]
    config.updated_at = now
    db.add(USStockSignalVirtualLog(
        config_id=config.id,
        account_id=account_id,
        timestamp=now,
        level="ERROR",
        action="SYNC_FAILED",
        message=str(exc),
    ))


def _sync_config_now(
    db: ORMSession,
    config: USStockSignalVirtualConfig,
    trigger_source: str = "manual",
    end_date: Optional[date] = None,
) -> Dict:
    quote_service = _get_quote_service(db, config.account_id)
    result = USStockSignalVirtualEngine(
        db,
        quote_service,
        config,
        end_date=end_date,
    ).run()
    _replace_config_runtime_state(db, config, result, trigger_source=trigger_source)
    return result


def _get_latest_sync_payload(db: ORMSession, config: USStockSignalVirtualConfig) -> Dict:
    latest_sync_log = (
        db.query(USStockSignalVirtualLog.payload)
        .filter(
            USStockSignalVirtualLog.config_id == config.id,
            USStockSignalVirtualLog.action == "SYNC",
        )
        .order_by(USStockSignalVirtualLog.timestamp.desc(), USStockSignalVirtualLog.id.desc())
        .first()
    )
    return latest_sync_log[0] if latest_sync_log and latest_sync_log[0] else {}


def _runtime_summary(db: ORMSession, config: USStockSignalVirtualConfig) -> Dict:
    latest_equity = (
        db.query(USStockSignalVirtualEquity.date, USStockSignalVirtualEquity.value)
        .filter(USStockSignalVirtualEquity.config_id == config.id)
        .order_by(USStockSignalVirtualEquity.date.desc())
        .first()
    )
    metrics = (_get_latest_sync_payload(db, config).get("metrics") or {})
    return {
        "latest_date": latest_equity.date.isoformat() if latest_equity else None,
        "portfolio_value": latest_equity.value if latest_equity else None,
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "trade_count": metrics.get("trade_count") or db.query(USStockSignalVirtualTrade).filter(USStockSignalVirtualTrade.config_id == config.id).count(),
        "signal_count": metrics.get("signal_count") or db.query(USStockSignalVirtualEvent).filter(USStockSignalVirtualEvent.config_id == config.id).count(),
        "holding_count": db.query(USStockSignalVirtualHolding).filter(USStockSignalVirtualHolding.config_id == config.id).count(),
    }


@router.get("/candidate-etfs")
def get_candidate_etfs():
    return CANDIDATE_ETF_OPTIONS


@router.get("/configs")
def list_configs(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    configs = (
        db.query(USStockSignalVirtualConfig)
        .filter(USStockSignalVirtualConfig.account_id == account_id)
        .order_by(USStockSignalVirtualConfig.updated_at.desc(), USStockSignalVirtualConfig.id.desc())
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
    payload: USStockSignalConfigPayload,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = USStockSignalVirtualConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload(config, payload)
    db.add(config)
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.put("/configs/{config_id}")
def update_config(
    config_id: int,
    payload: USStockSignalConfigPayload,
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
    db.query(USStockSignalVirtualEvent).filter(USStockSignalVirtualEvent.config_id == config.id).delete()
    db.query(USStockSignalVirtualTrade).filter(USStockSignalVirtualTrade.config_id == config.id).delete()
    db.query(USStockSignalVirtualHolding).filter(USStockSignalVirtualHolding.config_id == config.id).delete()
    db.query(USStockSignalVirtualEquity).filter(USStockSignalVirtualEquity.config_id == config.id).delete()
    db.query(USStockSignalVirtualLog).filter(USStockSignalVirtualLog.config_id == config.id).delete()
    db.delete(config)
    db.commit()
    return {"message": "已删除美股买卖点虚拟盘配置"}


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
        logger.exception("US stock signal virtual sync failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/configs/sync-enabled")
def sync_enabled_configs(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    configs = db.query(USStockSignalVirtualConfig).filter(
        USStockSignalVirtualConfig.account_id == account_id,
        USStockSignalVirtualConfig.enabled == True,  # noqa: E712
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


def _auto_sync_already_attempted(config: USStockSignalVirtualConfig, now_et: datetime) -> bool:
    if not config.last_auto_sync_at:
        return False
    last_at = config.last_auto_sync_at
    if last_at.tzinfo:
        return last_at.astimezone(EASTERN_TZ).date() == now_et.date()
    return last_at.date() == now_et.date()


def _is_auto_sync_due(config: USStockSignalVirtualConfig, now_et: datetime) -> bool:
    auto_sync_time = config.auto_sync_time or DEFAULT_AUTO_SYNC_TIME
    if not AUTO_SYNC_TIME_PATTERN.match(auto_sync_time):
        auto_sync_time = DEFAULT_AUTO_SYNC_TIME
    return now_et.strftime("%H:%M") >= auto_sync_time


def sync_due_us_stock_signal_configs_for_auto_sync(now_et: Optional[datetime] = None) -> Dict:
    now_et = now_et or MarketService.get_eastern_now()
    result = {
        "synced": [],
        "errors": [],
        "skipped": [],
        "current_time": now_et.strftime("%H:%M"),
        "timezone": "US/Eastern",
    }
    if now_et.weekday() >= 5 or MarketService.is_us_market_holiday(now_et.date()):
        result["skipped"].append({"reason": "美股休市日"})
        return result

    with get_db_ctx() as db:
        configs = (
            db.query(USStockSignalVirtualConfig)
            .filter(
                USStockSignalVirtualConfig.enabled == True,  # noqa: E712
                USStockSignalVirtualConfig.auto_sync_enabled == True,  # noqa: E712
            )
            .order_by(USStockSignalVirtualConfig.account_id.asc(), USStockSignalVirtualConfig.id.asc())
            .all()
        )
        for config in configs:
            config_id = config.id
            account_id = config.account_id
            config_name = config.name
            if not _is_auto_sync_due(config, now_et):
                result["skipped"].append({"id": config_id, "account_id": account_id, "name": config_name, "reason": "未到自动同步时间"})
                continue
            if _auto_sync_already_attempted(config, now_et):
                result["skipped"].append({"id": config_id, "account_id": account_id, "name": config_name, "reason": "今日已自动触发"})
                continue

            attempt_at = now_et.replace(tzinfo=None)
            _mark_sync_running(db, config, message=f"自动同步中（美东 {now_et.strftime('%H:%M')}）")
            config.last_auto_sync_at = attempt_at
            db.commit()
            try:
                sync_result = _sync_config_now(db, config, trigger_source="module_auto")
                config.last_auto_sync_at = attempt_at
                db.commit()
                result["synced"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "summary": sync_result.get("metrics"),
                })
            except Exception as exc:
                db.rollback()
                failed_config = db.query(USStockSignalVirtualConfig).filter(
                    USStockSignalVirtualConfig.id == config_id,
                    USStockSignalVirtualConfig.account_id == account_id,
                ).first()
                if failed_config:
                    failed_config.last_auto_sync_at = attempt_at
                _mark_sync_failed(db, config_id, account_id, exc)
                db.commit()
                logger.exception("US stock signal module auto sync failed")
                result["errors"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "error": str(exc),
                })
    return result


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
        db.query(
            USStockSignalVirtualEquity.date.label("date"),
            USStockSignalVirtualEquity.value.label("value"),
            USStockSignalVirtualEquity.cash.label("cash"),
            USStockSignalVirtualEquity.position_value.label("position_value"),
            USStockSignalVirtualEquity.drawdown.label("drawdown"),
        )
        .filter(USStockSignalVirtualEquity.config_id == config.id)
        .order_by(USStockSignalVirtualEquity.date.asc())
        .all()
    )
    holdings = (
        db.query(
            USStockSignalVirtualHolding.symbol.label("symbol"),
            USStockSignalVirtualHolding.shares.label("shares"),
            USStockSignalVirtualHolding.price.label("price"),
            USStockSignalVirtualHolding.avg_cost.label("avg_cost"),
            USStockSignalVirtualHolding.entry_date.label("entry_date"),
            USStockSignalVirtualHolding.market_value.label("market_value"),
            USStockSignalVirtualHolding.actual_weight_pct.label("actual_weight_pct"),
        )
        .filter(USStockSignalVirtualHolding.config_id == config.id)
        .order_by(USStockSignalVirtualHolding.market_value.desc())
        .all()
    )
    trades = (
        db.query(
            USStockSignalVirtualTrade.id.label("id"),
            USStockSignalVirtualTrade.date.label("date"),
            USStockSignalVirtualTrade.signal_date.label("signal_date"),
            USStockSignalVirtualTrade.action.label("action"),
            USStockSignalVirtualTrade.symbol.label("symbol"),
            USStockSignalVirtualTrade.price.label("price"),
            USStockSignalVirtualTrade.quantity.label("quantity"),
            USStockSignalVirtualTrade.amount.label("amount"),
            USStockSignalVirtualTrade.commission.label("commission"),
            USStockSignalVirtualTrade.profit.label("profit"),
            USStockSignalVirtualTrade.profit_pct.label("profit_pct"),
            USStockSignalVirtualTrade.reason.label("reason"),
            USStockSignalVirtualTrade.reason_detail.label("reason_detail"),
            USStockSignalVirtualTrade.cash_after.label("cash_after"),
            USStockSignalVirtualTrade.portfolio_value_after.label("portfolio_value_after"),
            USStockSignalVirtualTrade.symbol_market_value_after.label("symbol_market_value_after"),
            USStockSignalVirtualTrade.symbol_weight_pct_after.label("symbol_weight_pct_after"),
            USStockSignalVirtualTrade.price_source.label("price_source"),
        )
        .filter(USStockSignalVirtualTrade.config_id == config.id)
        .order_by(USStockSignalVirtualTrade.date.desc(), USStockSignalVirtualTrade.id.desc())
        .limit(trade_limit)
        .all()
    )
    events = (
        db.query(
            USStockSignalVirtualEvent.id.label("id"),
            USStockSignalVirtualEvent.date.label("date"),
            USStockSignalVirtualEvent.symbol.label("symbol"),
            USStockSignalVirtualEvent.direction.label("direction"),
            USStockSignalVirtualEvent.signal_price.label("signal_price"),
            USStockSignalVirtualEvent.turnover.label("turnover"),
            USStockSignalVirtualEvent.annualized_volatility_pct.label("annualized_volatility_pct"),
            USStockSignalVirtualEvent.threshold_pct.label("threshold_pct"),
            USStockSignalVirtualEvent.payload.label("payload"),
            USStockSignalVirtualEvent.price_source.label("price_source"),
        )
        .filter(USStockSignalVirtualEvent.config_id == config.id)
        .order_by(USStockSignalVirtualEvent.date.desc(), USStockSignalVirtualEvent.id.desc())
        .limit(event_limit)
        .all()
    )
    logs = (
        db.query(
            USStockSignalVirtualLog.id.label("id"),
            USStockSignalVirtualLog.timestamp.label("timestamp"),
            USStockSignalVirtualLog.date.label("date"),
            USStockSignalVirtualLog.level.label("level"),
            USStockSignalVirtualLog.action.label("action"),
            USStockSignalVirtualLog.message.label("message"),
            USStockSignalVirtualLog.payload.label("payload"),
        )
        .filter(USStockSignalVirtualLog.config_id == config.id)
        .order_by(USStockSignalVirtualLog.timestamp.desc(), USStockSignalVirtualLog.id.desc())
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
            "trade_count": db.query(USStockSignalVirtualTrade).filter(USStockSignalVirtualTrade.config_id == config.id).count(),
            "signal_count": db.query(USStockSignalVirtualEvent).filter(USStockSignalVirtualEvent.config_id == config.id).count(),
            "holding_count": len(holdings),
        },
        "benchmark_curve": sync_payload.get("benchmark_curve") or [],
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
