import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from ...core.database import (
    W20MomentumLiveConfig,
    W20MomentumLiveEquity,
    W20MomentumLiveHolding,
    W20MomentumLiveLog,
    W20MomentumLiveTrade,
    get_db,
    get_db_ctx,
)
from .account import valid_account
from .w20_momentum_backtest import (
    DEFAULT_BENCHMARKS,
    DEFAULT_SYMBOLS,
    W20MomentumBacktestEngine,
    W20MomentumBacktestParams,
    _get_quote_service,
    _normalize_frequency,
    _parse_float_list,
    _parse_symbol_list,
)

router = APIRouter(prefix="/api/w20-momentum-live", tags=["W20 Momentum Live"])
logger = logging.getLogger(__name__)


class W20MomentumLiveConfigPayload(BaseModel):
    name: str = "W20 风险调整 ETF 动量"
    enabled: bool = True
    symbols: List[str] = Field(default_factory=lambda: DEFAULT_SYMBOLS.copy())
    benchmark_symbols: List[str] = Field(default_factory=lambda: DEFAULT_BENCHMARKS.copy())
    initial_capital: float = 1_000_000.0
    start_date: date = date(2018, 1, 2)
    window: int = 20
    top_weights: List[float] = Field(default_factory=lambda: [80.0, 20.0])
    rebalance_frequency: str = "weekly"
    drift_threshold_pct: float = 100.0
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    lot_size: int = 100

    @validator("name")
    def validate_name(cls, value):
        value = (value or "").strip()
        if not value:
            raise ValueError("策略名称不能为空")
        return value

    @validator("symbols", "benchmark_symbols", pre=True)
    def validate_symbols(cls, value):
        parsed = _parse_symbol_list(value)
        if not parsed:
            raise ValueError("至少需要一个标的")
        return parsed

    @validator("top_weights", pre=True)
    def validate_top_weights(cls, value):
        parsed = _parse_float_list(value)
        if not parsed:
            raise ValueError("目标权重不能为空")
        if sum(parsed) <= 0:
            raise ValueError("目标权重之和必须大于 0")
        return parsed

    @validator("rebalance_frequency")
    def validate_rebalance_frequency(cls, value):
        normalized = _normalize_frequency(value)
        if normalized not in {"daily", "weekly", "monthly"}:
            raise ValueError("排名/调仓频率仅支持 daily、weekly、monthly")
        return normalized

    @validator("window")
    def validate_window(cls, value):
        if value < 2:
            raise ValueError("回归窗口不能小于 2")
        return value

    @validator("initial_capital", "drift_threshold_pct", "commission_pct", "slippage_pct")
    def validate_non_negative(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("lot_size")
    def validate_lot_size(cls, value):
        if value < 1:
            raise ValueError("最小交易单位不能小于 1")
        return value

    @validator("top_weights")
    def validate_top_weights_length(cls, value, values):
        symbols = values.get("symbols") or []
        if symbols and len(value) > len(symbols):
            raise ValueError(f"目标权重表示 Top{len(value)}，但标的池只有 {len(symbols)} 个标的")
        return value


class W20MomentumLiveConfigSchema(W20MomentumLiveConfigPayload):
    id: Optional[int] = None
    account_id: Optional[str] = None
    top_n: Optional[int] = None
    last_sync_at: Optional[datetime] = None
    last_sync_status: Optional[str] = None
    last_sync_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


def _config_to_dict(config: W20MomentumLiveConfig) -> Dict:
    top_weights = config.top_weights or [80.0, 20.0]
    return {
        "id": config.id,
        "account_id": config.account_id,
        "name": config.name,
        "enabled": bool(config.enabled),
        "symbols": config.symbols or [],
        "benchmark_symbols": config.benchmark_symbols or [],
        "initial_capital": config.initial_capital,
        "start_date": config.start_date,
        "window": config.window,
        "top_n": len(top_weights),
        "top_weights": top_weights,
        "rebalance_frequency": config.rebalance_frequency,
        "drift_threshold_pct": config.drift_threshold_pct,
        "commission_pct": config.commission_pct,
        "slippage_pct": config.slippage_pct,
        "lot_size": config.lot_size,
        "last_sync_at": config.last_sync_at,
        "last_sync_status": config.last_sync_status,
        "last_sync_message": config.last_sync_message,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _get_config_or_404(db: Session, account_id: str, config_id: int) -> W20MomentumLiveConfig:
    config = db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.id == config_id,
        W20MomentumLiveConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="未找到 W20 虚拟盘配置")
    return config


def _apply_payload(config: W20MomentumLiveConfig, payload: W20MomentumLiveConfigPayload):
    for field, value in payload.dict().items():
        setattr(config, field, value)
    config.updated_at = datetime.now()


def _build_backtest_params(config: W20MomentumLiveConfig) -> W20MomentumBacktestParams:
    top_weights = config.top_weights or [80.0, 20.0]
    return W20MomentumBacktestParams(
        symbols=config.symbols or DEFAULT_SYMBOLS.copy(),
        benchmark_symbols=config.benchmark_symbols or DEFAULT_BENCHMARKS.copy(),
        initial_capital=float(config.initial_capital or 0),
        start_date=config.start_date.isoformat(),
        end_date=date.today().isoformat(),
        window=int(config.window or 20),
        top_n=len(top_weights),
        top_weights=top_weights,
        rebalance_frequency=config.rebalance_frequency or "weekly",
        drift_threshold_pct=float(config.drift_threshold_pct or 0),
        commission_pct=float(config.commission_pct or 0),
        slippage_pct=float(config.slippage_pct or 0),
        lot_size=int(config.lot_size or 100),
    )


def _replace_config_runtime_state(
    db: Session,
    config: W20MomentumLiveConfig,
    result: Dict,
    trigger_source: str = "manual",
):
    db.query(W20MomentumLiveTrade).filter(W20MomentumLiveTrade.config_id == config.id).delete()
    db.query(W20MomentumLiveHolding).filter(W20MomentumLiveHolding.config_id == config.id).delete()
    db.query(W20MomentumLiveEquity).filter(W20MomentumLiveEquity.config_id == config.id).delete()
    db.query(W20MomentumLiveLog).filter(W20MomentumLiveLog.config_id == config.id).delete()

    now = datetime.now()
    for item in result.get("equity_curve") or []:
        db.add(W20MomentumLiveEquity(
            config_id=config.id,
            account_id=config.account_id,
            date=date.fromisoformat(item["date"]),
            value=item.get("value"),
            benchmark_value=item.get("benchmark_value"),
            drawdown=item.get("drawdown"),
            benchmark_drawdown=item.get("benchmark_drawdown"),
            created_at=now,
            updated_at=now,
        ))

    for item in result.get("trades") or []:
        db.add(W20MomentumLiveTrade(
            config_id=config.id,
            account_id=config.account_id,
            date=date.fromisoformat(item["date"]),
            signal_date=date.fromisoformat(item["signal_date"]) if item.get("signal_date") else None,
            action=item.get("action"),
            symbol=item.get("symbol"),
            price=item.get("price"),
            open_price=item.get("open_price"),
            quantity=item.get("quantity"),
            amount=item.get("amount"),
            commission=item.get("commission"),
            reason=item.get("reason"),
            reason_detail=item.get("reason_detail"),
            cash_after=item.get("cash_after"),
            portfolio_value_after=item.get("portfolio_value_after"),
            symbol_market_value_after=item.get("symbol_market_value_after"),
            symbol_weight_pct_after=item.get("symbol_weight_pct_after"),
            target_symbols=item.get("target_symbols"),
            target_weights_pct=item.get("target_weights_pct"),
            created_at=now,
        ))

    for item in result.get("current_holdings") or []:
        db.add(W20MomentumLiveHolding(
            config_id=config.id,
            account_id=config.account_id,
            symbol=item.get("symbol"),
            shares=item.get("shares") or 0,
            price=item.get("price"),
            market_value=item.get("market_value"),
            actual_weight_pct=item.get("actual_weight_pct"),
            target_weight_pct=item.get("target_weight_pct"),
            updated_at=now,
        ))

    for item in result.get("signal_history") or []:
        db.add(W20MomentumLiveLog(
            config_id=config.id,
            account_id=config.account_id,
            timestamp=now,
            date=date.fromisoformat(item["date"]),
            level="INFO",
            action="SIGNAL",
            message=f"信号日 {item['date']} 目标: {', '.join(item.get('selected_symbols') or [])}",
            payload=item,
        ))

    db.add(W20MomentumLiveLog(
        config_id=config.id,
        account_id=config.account_id,
        timestamp=now,
        level="INFO",
        action="SYNC",
        message="虚拟盘已同步到最新 K 线",
        payload={
            "trigger_source": trigger_source,
            "metrics": result.get("metrics"),
            "meta": result.get("meta"),
            "latest_signal": result.get("latest_signal"),
            "benchmark_metrics": result.get("benchmark_metrics"),
            "annual_performance": result.get("annual_performance"),
            "symbol_trade_stats": result.get("symbol_trade_stats"),
        },
    ))

    config.last_sync_at = now
    config.last_sync_status = "success"
    config.last_sync_message = "同步完成"
    config.updated_at = now
    db.query(W20MomentumLiveConfig).filter(W20MomentumLiveConfig.id == config.id).update(
        {
            "last_sync_at": now,
            "last_sync_status": "success",
            "last_sync_message": "同步完成",
            "updated_at": now,
        },
        synchronize_session=False,
    )


def _sync_config_now(db: Session, config: W20MomentumLiveConfig, trigger_source: str = "manual") -> Dict:
    quote_service = _get_quote_service(config.account_id)
    params = _build_backtest_params(config)
    result = W20MomentumBacktestEngine(quote_service, params).run()
    _replace_config_runtime_state(db, config, result, trigger_source=trigger_source)
    return result


def _mark_sync_running(db: Session, config: W20MomentumLiveConfig, message: str = "同步中"):
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "running"
    config.last_sync_message = message
    config.updated_at = now


def _mark_sync_failed(db: Session, config_id: int, account_id: str, exc: Exception):
    config = db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.id == config_id,
        W20MomentumLiveConfig.account_id == account_id,
    ).first()
    if not config:
        return
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "failed"
    config.last_sync_message = str(exc)[:500]
    config.updated_at = now
    db.add(W20MomentumLiveLog(
        config_id=config.id,
        account_id=account_id,
        timestamp=now,
        level="ERROR",
        action="SYNC_FAILED",
        message=str(exc),
    ))


def _get_latest_sync_payload(db: Session, config: W20MomentumLiveConfig) -> Dict:
    latest_sync_log = (
        db.query(W20MomentumLiveLog)
        .filter(
            W20MomentumLiveLog.config_id == config.id,
            W20MomentumLiveLog.action == "SYNC",
        )
        .order_by(W20MomentumLiveLog.timestamp.desc(), W20MomentumLiveLog.id.desc())
        .first()
    )
    return latest_sync_log.payload if latest_sync_log and latest_sync_log.payload else {}


def _get_latest_runtime_summary(db: Session, config: W20MomentumLiveConfig) -> Dict:
    latest_equity = (
        db.query(W20MomentumLiveEquity)
        .filter(W20MomentumLiveEquity.config_id == config.id)
        .order_by(W20MomentumLiveEquity.date.desc())
        .first()
    )
    sync_payload = _get_latest_sync_payload(db, config)
    metrics = sync_payload.get("metrics") or {}
    trade_count = db.query(W20MomentumLiveTrade).filter(W20MomentumLiveTrade.config_id == config.id).count()
    holding_count = db.query(W20MomentumLiveHolding).filter(W20MomentumLiveHolding.config_id == config.id).count()
    return {
        "latest_date": latest_equity.date.isoformat() if latest_equity else None,
        "portfolio_value": latest_equity.value if latest_equity else None,
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "trade_count": trade_count,
        "holding_count": holding_count,
    }


@router.get("/configs")
def list_w20_momentum_live_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    configs = (
        db.query(W20MomentumLiveConfig)
        .filter(W20MomentumLiveConfig.account_id == account_id)
        .order_by(W20MomentumLiveConfig.updated_at.desc(), W20MomentumLiveConfig.id.desc())
        .all()
    )
    return [
        {
            **_config_to_dict(config),
            "runtime": _get_latest_runtime_summary(db, config),
        }
        for config in configs
    ]


@router.post("/configs")
def create_w20_momentum_live_config(
    payload: W20MomentumLiveConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = W20MomentumLiveConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload(config, payload)
    db.add(config)
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.get("/configs/{config_id}")
def get_w20_momentum_live_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    return _config_to_dict(config)


@router.put("/configs/{config_id}")
def update_w20_momentum_live_config(
    config_id: int,
    payload: W20MomentumLiveConfigPayload,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _apply_payload(config, payload)
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.delete("/configs/{config_id}")
def delete_w20_momentum_live_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    db.query(W20MomentumLiveTrade).filter(W20MomentumLiveTrade.config_id == config.id).delete()
    db.query(W20MomentumLiveHolding).filter(W20MomentumLiveHolding.config_id == config.id).delete()
    db.query(W20MomentumLiveEquity).filter(W20MomentumLiveEquity.config_id == config.id).delete()
    db.query(W20MomentumLiveLog).filter(W20MomentumLiveLog.config_id == config.id).delete()
    db.delete(config)
    db.commit()
    return {"message": "已删除 W20 虚拟盘配置"}


@router.post("/configs/{config_id}/sync")
def sync_w20_momentum_live_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
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
        logger.exception("W20 virtual strategy sync failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/configs/sync-enabled")
def sync_enabled_w20_momentum_live_configs(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    configs = db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.account_id == account_id,
        W20MomentumLiveConfig.enabled == True,  # noqa: E712
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


def sync_all_enabled_w20_momentum_live_configs_for_scheduler() -> Dict:
    result = {"synced": [], "errors": []}
    with get_db_ctx() as db:
        configs = (
            db.query(W20MomentumLiveConfig)
            .filter(W20MomentumLiveConfig.enabled == True)  # noqa: E712
            .order_by(W20MomentumLiveConfig.account_id.asc(), W20MomentumLiveConfig.id.asc())
            .all()
        )
        for config in configs:
            config_id = config.id
            account_id = config.account_id
            config_name = config.name
            _mark_sync_running(db, config, message="定时同步中")
            db.commit()
            try:
                sync_result = _sync_config_now(db, config, trigger_source="schedule")
                db.commit()
                result["synced"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "summary": sync_result.get("metrics"),
                })
            except Exception as exc:
                db.rollback()
                _mark_sync_failed(db, config_id, account_id, exc)
                db.commit()
                logger.exception("Scheduled W20 virtual strategy sync failed")
                result["errors"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "error": str(exc),
                })
    return result


@router.get("/configs/{config_id}/detail")
def get_w20_momentum_live_detail(
    config_id: int,
    log_limit: int = Query(200, ge=1, le=1000),
    trade_limit: int = Query(500, ge=1, le=5000),
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    equity = (
        db.query(W20MomentumLiveEquity)
        .filter(W20MomentumLiveEquity.config_id == config.id)
        .order_by(W20MomentumLiveEquity.date.asc())
        .all()
    )
    holdings = (
        db.query(W20MomentumLiveHolding)
        .filter(W20MomentumLiveHolding.config_id == config.id)
        .order_by(W20MomentumLiveHolding.market_value.desc())
        .all()
    )
    trades = (
        db.query(W20MomentumLiveTrade)
        .filter(W20MomentumLiveTrade.config_id == config.id)
        .order_by(W20MomentumLiveTrade.date.desc(), W20MomentumLiveTrade.id.desc())
        .limit(trade_limit)
        .all()
    )
    logs = (
        db.query(W20MomentumLiveLog)
        .filter(W20MomentumLiveLog.config_id == config.id)
        .order_by(W20MomentumLiveLog.timestamp.desc(), W20MomentumLiveLog.id.desc())
        .limit(log_limit)
        .all()
    )
    latest_signal = next((log for log in logs if log.action == "SIGNAL"), None)
    sync_payload = _get_latest_sync_payload(db, config)
    metrics = sync_payload.get("metrics") or {}
    meta = sync_payload.get("meta") or {}
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
            "meta": meta,
            "benchmark_metrics": sync_payload.get("benchmark_metrics") or [],
            "annual_performance": sync_payload.get("annual_performance") or [],
            "symbol_trade_stats": sync_payload.get("symbol_trade_stats") or [],
            "trade_count": db.query(W20MomentumLiveTrade).filter(W20MomentumLiveTrade.config_id == config.id).count(),
            "holding_count": len(holdings),
            "latest_signal": sync_payload.get("latest_signal") or (latest_signal.payload if latest_signal else None),
        },
        "equity_curve": [
            {
                "date": item.date.isoformat(),
                "value": item.value,
                "benchmark_value": item.benchmark_value,
                "drawdown": item.drawdown,
                "benchmark_drawdown": item.benchmark_drawdown,
            }
            for item in equity
        ],
        "holdings": [
            {
                "symbol": item.symbol,
                "shares": item.shares,
                "price": item.price,
                "market_value": item.market_value,
                "actual_weight_pct": item.actual_weight_pct,
                "target_weight_pct": item.target_weight_pct,
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
                "open_price": item.open_price,
                "quantity": item.quantity,
                "amount": item.amount,
                "commission": item.commission,
                "reason": item.reason,
                "reason_detail": item.reason_detail,
                "cash_after": item.cash_after,
                "portfolio_value_after": item.portfolio_value_after,
                "symbol_market_value_after": item.symbol_market_value_after,
                "symbol_weight_pct_after": item.symbol_weight_pct_after,
                "target_symbols": item.target_symbols,
                "target_weights_pct": item.target_weights_pct,
            }
            for item in trades
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
