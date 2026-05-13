import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
import pandas as pd
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
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingOrder,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    get_external_trading_db,
    get_external_trading_db_ctx,
)
from ...core.services.external_trading_executor import trigger_external_trading_executor
from ...core.services.external_trading_execution_policy import resolve_execution_policy
from ...core.services.external_trading_ledger import (
    STRATEGY_W20,
    get_ledger_positions,
    get_open_order_quantities,
    serialize_ledger_position,
    serialize_order,
    serialize_sub_account,
    sync_target_positions,
)
from ...core.services.external_trading_valuation import (
    ExternalTradingValuationError,
    calculate_sub_account_net_asset,
    get_realtime_reference_prices,
)
from ...core.utils import send_alert_email
from .account import valid_account
from .w20_momentum_backtest import (
    DEFAULT_BENCHMARKS,
    DEFAULT_SYMBOLS,
    W20MomentumBacktestEngine,
    W20MomentumBacktestParams,
    _build_price_frame,
    _get_quote_service,
    _is_valid_price,
    _normalize_frequency,
    _parse_float_list,
    _parse_symbol_list,
)

router = APIRouter(prefix="/api/w20-momentum-live", tags=["W20 Momentum Live"])
logger = logging.getLogger(__name__)
CHINA_TZ = ZoneInfo("Asia/Shanghai")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
LIVE_EXECUTION_START_TIME = dtime(9, 30)
CHINA_MARKET_MORNING_CLOSE = dtime(11, 30)
CHINA_MARKET_AFTERNOON_OPEN = dtime(13, 0)
CHINA_MARKET_CLOSE = dtime(15, 0)
LIVE_PRICE_SOURCE = "realtime_quote"


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
    auto_signal_enabled: bool = True
    auto_signal_time: str = "18:35"
    auto_virtual_trade_enabled: bool = True
    auto_virtual_trade_time: str = "09:31"
    live_trade_enabled: bool = False
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None

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

    @validator("auto_signal_time", "auto_virtual_trade_time")
    def validate_auto_time(cls, value):
        value = (value or "").strip()
        if not TIME_PATTERN.match(value):
            raise ValueError("时间格式必须为 HH:mm")
        return value

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


class W20LiveTradeRequest(BaseModel):
    dry_run: bool = True
    sync_before: bool = True


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
        "auto_signal_enabled": bool(getattr(config, "auto_signal_enabled", True)),
        "auto_signal_time": getattr(config, "auto_signal_time", None) or "18:35",
        "auto_virtual_trade_enabled": bool(getattr(config, "auto_virtual_trade_enabled", True)),
        "auto_virtual_trade_time": getattr(config, "auto_virtual_trade_time", None) or "09:31",
        "last_auto_signal_at": getattr(config, "last_auto_signal_at", None),
        "last_auto_virtual_trade_at": getattr(config, "last_auto_virtual_trade_at", None),
        "live_trade_enabled": bool(getattr(config, "live_trade_enabled", False)),
        "external_trading_account_id": getattr(config, "external_trading_account_id", None),
        "live_sub_account_id": getattr(config, "live_sub_account_id", None),
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


def _validate_external_account_selection(db: Session, account_id: str, external_account_id: Optional[int]):
    if not external_account_id:
        return
    account = db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == external_account_id,
        ExternalTradingAccount.account_id == account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="所选外部交易账户不存在")
    if not account.enabled:
        raise HTTPException(status_code=400, detail="所选外部交易账户未启用")


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
        raise HTTPException(status_code=400, detail="选择实盘虚拟子账户前请先选择外部交易账户")
    query = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account_id,
    )
    sub_account = query.first()
    if not sub_account:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户不存在")
    if require_enabled and not sub_account.enabled:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户未启用")
    is_bound = bool(sub_account.strategy_type or sub_account.strategy_config_id)
    is_current_binding = (
        sub_account.strategy_type == STRATEGY_W20
        and sub_account.strategy_config_id == config_id
    )
    if is_bound and not is_current_binding:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户已被其他策略绑定")
    return sub_account


def _deactivate_w20_target_positions(
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
            ExternalTradingTargetPosition.strategy_type == STRATEGY_W20,
            ExternalTradingTargetPosition.strategy_config_id == config_id,
        )
    now = datetime.now()
    for row in query.all():
        row.status = "PREVIEW"
        row.updated_at = now


def _sync_w20_live_sub_account_binding(
    db: Session,
    config: W20MomentumLiveConfig,
    *,
    previous_sub_account_id: Optional[int],
) -> None:
    if getattr(config, "live_trade_enabled", False):
        if not config.external_trading_account_id:
            raise HTTPException(status_code=400, detail="开启实盘交易时必须选择外部交易账户")
        if not config.live_sub_account_id:
            raise HTTPException(status_code=400, detail="开启实盘交易时必须选择实盘虚拟子账户")

    selected_sub_account = _get_valid_live_sub_account_selection(
        db,
        config.account_id,
        config.external_trading_account_id,
        config.live_sub_account_id,
        config_id=config.id,
        require_enabled=bool(config.live_sub_account_id),
    )

    if previous_sub_account_id and previous_sub_account_id != config.live_sub_account_id:
        _deactivate_w20_target_positions(
            db,
            sub_account_id=previous_sub_account_id,
            config_id=config.id,
        )
        previous = db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == previous_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_W20,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if previous:
            previous.strategy_type = None
            previous.strategy_config_id = None
            previous.updated_at = datetime.now()

    if not getattr(config, "enabled", True) or not getattr(config, "live_trade_enabled", False):
        _deactivate_w20_target_positions(
            db,
            sub_account_id=config.live_sub_account_id or previous_sub_account_id,
            config_id=config.id,
        )

    if selected_sub_account:
        selected_sub_account.strategy_type = STRATEGY_W20
        selected_sub_account.strategy_config_id = config.id
        selected_sub_account.updated_at = datetime.now()


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


def _parse_param_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _quote_local_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            timestamp = datetime.fromisoformat(text)
        except ValueError:
            return None
    if timestamp.tzinfo:
        return timestamp.astimezone(CHINA_TZ)
    return timestamp.replace(tzinfo=CHINA_TZ)


def _load_existing_realtime_execution_overrides(
    db: Session,
    config: W20MomentumLiveConfig,
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    price_overrides: Dict[str, Dict[str, float]] = {}
    source_overrides: Dict[str, Dict[str, str]] = {}
    timestamp_overrides: Dict[str, Dict[str, str]] = {}
    rows = (
        db.query(W20MomentumLiveTrade)
        .filter(
            W20MomentumLiveTrade.config_id == config.id,
            W20MomentumLiveTrade.price_source == LIVE_PRICE_SOURCE,
        )
        .all()
    )
    for row in rows:
        if not row.date or not row.symbol or not _is_valid_price(row.open_price):
            continue
        date_key = row.date.isoformat()
        price_overrides.setdefault(date_key, {})[row.symbol] = float(row.open_price)
        source_overrides.setdefault(date_key, {})[row.symbol] = LIVE_PRICE_SOURCE
        if row.quote_timestamp:
            timestamp_overrides.setdefault(date_key, {})[row.symbol] = row.quote_timestamp.isoformat()
    return price_overrides, source_overrides, timestamp_overrides


def _merge_execution_override(
    price_overrides: Dict[str, Dict[str, float]],
    source_overrides: Dict[str, Dict[str, str]],
    timestamp_overrides: Dict[str, Dict[str, str]],
    date_key: str,
    symbol: str,
    price: float,
    source: str,
    quote_timestamp: Optional[datetime],
    overwrite: bool = False,
):
    if not _is_valid_price(price):
        return
    if not overwrite and symbol in price_overrides.get(date_key, {}):
        return
    price_overrides.setdefault(date_key, {})[symbol] = float(price)
    source_overrides.setdefault(date_key, {})[symbol] = source
    if quote_timestamp:
        timestamp_overrides.setdefault(date_key, {})[symbol] = quote_timestamp.isoformat()


def _fetch_frames_for_params(quote_service, params: W20MomentumBacktestParams):
    start_dt = _parse_param_date(params.start_date)
    end_dt = _parse_param_date(params.end_date) if params.end_date else date.today()
    fetch_start = start_dt - timedelta(days=max(60, params.window * 3))
    universe_frames = {
        symbol: _build_price_frame(quote_service, symbol, fetch_start, end_dt)
        for symbol in params.symbols
    }
    benchmark_frames = {
        symbol: _build_price_frame(quote_service, symbol, fetch_start, end_dt)
        for symbol in params.benchmark_symbols
    }
    return universe_frames, benchmark_frames


def _latest_frame_date_before(frames: Dict[str, pd.DataFrame], cutoff: date) -> Optional[date]:
    candidates = []
    for frame in frames.values():
        if frame is None or frame.empty:
            continue
        candidates.extend([item for item in frame.index if item < cutoff])
    return max(candidates) if candidates else None


def _append_live_quote_row(frame: pd.DataFrame, live_date: date, quote: Dict) -> pd.DataFrame:
    next_frame = frame.copy() if frame is not None else pd.DataFrame()
    if not next_frame.empty:
        next_frame = next_frame[next_frame.index < live_date].copy()
    price = float(quote.get("price"))
    next_frame.loc[live_date, ["open", "high", "low", "close", "volume", "turnover"]] = [
        price,
        price,
        price,
        price,
        float(quote.get("volume") or 0),
        float(quote.get("turnover") or 0),
    ]
    return next_frame.sort_index()


def _prepare_live_execution_context(
    quote_service,
    params: W20MomentumBacktestParams,
    price_overrides: Dict[str, Dict[str, float]],
    source_overrides: Dict[str, Dict[str, str]],
    timestamp_overrides: Dict[str, Dict[str, str]],
):
    all_symbols = list(dict.fromkeys((params.symbols or []) + (params.benchmark_symbols or [])))
    quotes = quote_service.get_quote_batch(all_symbols) or []
    usable_quotes: Dict[str, Dict] = {}
    quote_dates = []
    today_cn = datetime.now(CHINA_TZ).date()

    for quote in quotes:
        symbol = quote.get("symbol")
        price = quote.get("price")
        quote_dt = _quote_local_datetime(quote.get("timestamp"))
        if not symbol or not quote_dt or not _is_valid_price(price):
            continue
        if quote_dt.date() != today_cn or quote_dt.time() < LIVE_EXECUTION_START_TIME:
            continue
        usable_quotes[symbol] = quote
        quote_dates.append(quote_dt.date())

    if not usable_quotes or not quote_dates:
        return None, None, None

    live_date = max(quote_dates)
    universe_frames, benchmark_frames = _fetch_frames_for_params(quote_service, params)
    signal_cutoff_date = _latest_frame_date_before(universe_frames, live_date)
    if not signal_cutoff_date:
        return None, None, None

    live_date_key = live_date.isoformat()
    for symbol, quote in usable_quotes.items():
        quote_dt = _quote_local_datetime(quote.get("timestamp"))
        _merge_execution_override(
            price_overrides,
            source_overrides,
            timestamp_overrides,
            live_date_key,
            symbol,
            float(quote.get("price")),
            LIVE_PRICE_SOURCE,
            quote_dt,
            overwrite=False,
        )

    for symbol in params.symbols:
        quote = usable_quotes.get(symbol)
        if quote:
            universe_frames[symbol] = _append_live_quote_row(universe_frames.get(symbol), live_date, quote)
    for symbol in params.benchmark_symbols:
        quote = usable_quotes.get(symbol)
        if quote:
            benchmark_frames[symbol] = _append_live_quote_row(benchmark_frames.get(symbol), live_date, quote)

    return universe_frames, benchmark_frames, signal_cutoff_date


def _replace_config_runtime_state(
    db: Session,
    config: W20MomentumLiveConfig,
    result: Dict,
    trigger_source: str = "manual",
):
    db.query(W20MomentumLiveTrade).filter(W20MomentumLiveTrade.config_id == config.id).delete()
    db.query(W20MomentumLiveHolding).filter(W20MomentumLiveHolding.config_id == config.id).delete()
    db.query(W20MomentumLiveEquity).filter(W20MomentumLiveEquity.config_id == config.id).delete()
    db.query(W20MomentumLiveLog).filter(
        W20MomentumLiveLog.config_id == config.id,
        W20MomentumLiveLog.action.in_(["SIGNAL", "SYNC", "SYNC_FAILED"]),
    ).delete(synchronize_session=False)

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
            price_source=item.get("price_source"),
            quote_timestamp=datetime.fromisoformat(item["quote_timestamp"]) if item.get("quote_timestamp") else None,
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


def _sync_config_now(
    db: Session,
    config: W20MomentumLiveConfig,
    trigger_source: str = "manual",
    use_realtime_execution: bool = True,
) -> Dict:
    quote_service = _get_quote_service(config.account_id)
    params = _build_backtest_params(config)
    price_overrides, source_overrides, timestamp_overrides = _load_existing_realtime_execution_overrides(db, config)
    universe_frames = None
    benchmark_frames = None
    signal_cutoff_date = None
    if use_realtime_execution:
        universe_frames, benchmark_frames, signal_cutoff_date = _prepare_live_execution_context(
            quote_service,
            params,
            price_overrides,
            source_overrides,
            timestamp_overrides,
        )
    result = W20MomentumBacktestEngine(
        quote_service,
        params,
        universe_frames=universe_frames,
        benchmark_frames=benchmark_frames,
        execution_price_overrides=price_overrides,
        execution_price_source_overrides=source_overrides,
        execution_quote_timestamp_overrides=timestamp_overrides,
        signal_cutoff_date=signal_cutoff_date,
    ).run()
    result["live_execution_context"] = {
        "enabled": use_realtime_execution,
        "used": bool(signal_cutoff_date),
        "signal_cutoff_date": signal_cutoff_date.isoformat() if signal_cutoff_date else None,
    }
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
        db.query(W20MomentumLiveLog.payload)
        .filter(
            W20MomentumLiveLog.config_id == config.id,
            W20MomentumLiveLog.action == "SYNC",
        )
        .order_by(W20MomentumLiveLog.timestamp.desc(), W20MomentumLiveLog.id.desc())
        .first()
    )
    return latest_sync_log[0] if latest_sync_log and latest_sync_log[0] else {}


def _get_latest_runtime_summary(db: Session, config: W20MomentumLiveConfig) -> Dict:
    latest_equity = (
        db.query(W20MomentumLiveEquity.date, W20MomentumLiveEquity.value)
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


def _to_w20_symbol(symbol: str) -> str:
    if not symbol:
        return symbol
    parts = str(symbol).split(".")
    if len(parts) != 2:
        return str(symbol)
    first, second = parts[0].upper(), parts[1].upper()
    if first in {"SH", "SS", "SZ", "BJ"}:
        suffix = "SH" if first in {"SH", "SS"} else first
        return f"{parts[1]}.{suffix}"
    suffix = "SH" if second == "SS" else second
    return f"{parts[0]}.{suffix}"


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _round_to_lot(quantity: float, lot_size: int) -> int:
    lot_size = max(int(lot_size or 1), 1)
    return int(quantity // lot_size) * lot_size


def _get_level_price(levels, level: int) -> float:
    for item in levels or []:
        if _safe_int(item.get("level")) == level:
            return _safe_float(item.get("price"))
    return 0.0


def _order_estimated_price(order: Dict) -> float:
    return _safe_float(
        order.get("limit_price"),
        _safe_float(order.get("protection_limit_price"), _safe_float(order.get("estimated_price"))),
    )


def _china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def _parse_hhmm(value: str, default: dtime) -> dtime:
    text = str(value or "").strip()
    if not TIME_PATTERN.match(text):
        return default
    hour, minute = [int(part) for part in text.split(":")]
    return dtime(hour, minute)


def _has_run_today(timestamp: Optional[datetime], today: date) -> bool:
    if not timestamp:
        return False
    local_timestamp = timestamp.astimezone(CHINA_TZ) if timestamp.tzinfo else timestamp.replace(tzinfo=CHINA_TZ)
    return local_timestamp.date() == today


def _parse_optional_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _get_result_effective_date(result: Dict) -> Optional[date]:
    meta = result.get("meta") or {}
    return _parse_optional_date(meta.get("effective_end_date"))


def _get_result_latest_signal_date(result: Dict) -> Optional[date]:
    latest_signal = result.get("latest_signal") or {}
    return _parse_optional_date(latest_signal.get("date"))


def _mark_auto_waiting(
    db: Session,
    config_id: int,
    account_id: str,
    message: str,
):
    config = db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.id == config_id,
        W20MomentumLiveConfig.account_id == account_id,
    ).first()
    if not config:
        return
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "waiting"
    config.last_sync_message = message[:500]
    config.updated_at = now


def _is_china_trading_day(check_date: date) -> bool:
    if check_date.weekday() >= 5:
        return False
    try:
        from ...core.services.tushare import TushareService

        calendar = TushareService.get_instance().get_trade_calendar_frame(check_date, check_date)
        if not calendar.empty:
            row = calendar.iloc[0]
            return int(row.get("is_open") or 0) == 1
    except Exception as exc:
        logger.warning("A-share trading calendar check failed for %s: %s", check_date, exc)
    return True


def _is_china_live_trade_window(now: Optional[datetime] = None) -> bool:
    current = now or _china_now()
    if current.tzinfo:
        current = current.astimezone(CHINA_TZ)
    if not _is_china_trading_day(current.date()):
        return False
    current_time = current.time()
    return (
        LIVE_EXECUTION_START_TIME <= current_time <= CHINA_MARKET_MORNING_CLOSE
        or CHINA_MARKET_AFTERNOON_OPEN <= current_time <= CHINA_MARKET_CLOSE
    )


def _get_external_account_for_live_trade(
    db: Session,
    config: W20MomentumLiveConfig,
) -> ExternalTradingAccount:
    if not getattr(config, "live_trade_enabled", False):
        raise HTTPException(status_code=400, detail="该配置未开启实盘交易")
    if not config.external_trading_account_id:
        raise HTTPException(status_code=400, detail="请先选择外部交易账户")
    account = db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == config.external_trading_account_id,
        ExternalTradingAccount.account_id == config.account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="所选外部交易账户不存在")
    if not account.enabled:
        raise HTTPException(status_code=400, detail="所选外部交易账户未启用")
    return account


def _get_w20_live_sub_account(
    db: Session,
    config: W20MomentumLiveConfig,
    external_account: ExternalTradingAccount,
) -> ExternalTradingSubAccount:
    if not getattr(config, "live_sub_account_id", None):
        raise HTTPException(status_code=400, detail="请先在 W20 实盘设置中选择虚拟子账户")
    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == config.live_sub_account_id,
        ExternalTradingSubAccount.account_id == config.account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account.id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户不存在")
    if not sub_account.enabled:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户未启用")
    if sub_account.strategy_type != STRATEGY_W20 or sub_account.strategy_config_id != config.id:
        raise HTTPException(status_code=400, detail="所选实盘虚拟子账户尚未绑定当前 W20 配置，请先保存配置")
    return sub_account


async def _build_live_trade_plan(
    db: Session,
    config: W20MomentumLiveConfig,
    external_account: ExternalTradingAccount,
    latest_signal: Dict,
) -> Dict:
    selected_symbols = [_to_w20_symbol(symbol) for symbol in latest_signal.get("selected_symbols") or []]
    target_weights_pct = [float(item or 0) for item in (latest_signal.get("target_weights_pct") or [])]
    if not selected_symbols or not target_weights_pct:
        raise HTTPException(status_code=400, detail="当前策略没有可执行的目标信号，请先同步虚拟盘")

    target_weight_by_symbol = {
        symbol: target_weights_pct[index] / 100.0
        for index, symbol in enumerate(selected_symbols)
        if index < len(target_weights_pct)
    }

    sub_account = _get_w20_live_sub_account(db, config, external_account)
    sub_account_allocated_cash = _safe_float(sub_account.cash_allocated)
    sub_account_available_cash = _safe_float(sub_account.cash_available, sub_account_allocated_cash)

    ledger_positions = get_ledger_positions(db, sub_account.id)
    current_positions = {
        symbol: {
            "quantity": _safe_int(pos.quantity),
            "available_quantity": _safe_int(pos.available_quantity),
            "cost_price": _safe_float(pos.avg_cost),
        }
        for symbol, pos in ledger_positions.items()
    }
    open_quantities = get_open_order_quantities(db, sub_account.id)

    plan_symbols = set(selected_symbols) | set(current_positions.keys()) | set(open_quantities.keys())
    quote_symbols = sorted(plan_symbols)
    try:
        reference_prices = await get_realtime_reference_prices(
            external_account.id,
            quote_symbols,
            timeout=10.0,
        )
    except ExternalTradingValuationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    reference_prices = {
        _to_w20_symbol(symbol): _safe_float(price)
        for symbol, price in reference_prices.items()
        if _to_w20_symbol(symbol) and _safe_float(price) > 0
    }

    try:
        valuation = await calculate_sub_account_net_asset(
            db,
            sub_account,
            positions=list(ledger_positions.values()),
            prefetched_prices=reference_prices,
            timeout=10.0,
        )
    except ExternalTradingValuationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    position_market_value = valuation["position_market_value"]
    sub_account_net_asset = valuation["net_asset"]

    lot_size = max(int(config.lot_size or 100), 1)
    available_cash = sub_account_available_cash
    trade_base_value = sub_account_net_asset
    if trade_base_value <= 0:
        raise HTTPException(status_code=400, detail="虚拟子账户净资产为空，无法生成实盘计划")

    rows = []
    sell_orders = []
    buy_candidates = []
    target_rows = []
    for symbol in sorted(plan_symbols):
        reference_price = _safe_float(reference_prices.get(symbol))
        price_source = "realtime_reference_price"
        if reference_price <= 0:
            rows.append({
                "symbol": symbol,
                "target_weight_pct": round(target_weight_by_symbol.get(symbol, 0.0) * 100, 4),
                "current_quantity": current_positions.get(symbol, {}).get("quantity", 0),
                "target_quantity": 0,
                "delta_quantity": 0,
                "action": "SKIP",
                "message": "无可用实时参考价",
            })
            continue

        current_quantity = current_positions.get(symbol, {}).get("quantity", 0)
        pending_buy_quantity = (open_quantities.get(symbol) or {}).get("BUY", 0)
        pending_sell_quantity = (open_quantities.get(symbol) or {}).get("SELL", 0)
        effective_quantity = current_quantity + pending_buy_quantity - pending_sell_quantity
        ledger_available_quantity = current_positions.get(symbol, {}).get("available_quantity", current_quantity)
        available_quantity = max(ledger_available_quantity - pending_sell_quantity, 0)
        target_weight = target_weight_by_symbol.get(symbol, 0.0)
        target_value = trade_base_value * target_weight
        target_quantity = _round_to_lot(target_value / reference_price, lot_size) if target_weight > 0 else 0
        delta = target_quantity - effective_quantity
        target_rows.append({
            "symbol": symbol,
            "target_quantity": target_quantity,
            "target_weight_pct": round(target_weight * 100, 4),
            "target_value": target_value,
        })

        row = {
            "symbol": symbol,
            "target_weight_pct": round(target_weight * 100, 4),
            "current_quantity": current_quantity,
            "pending_buy_quantity": pending_buy_quantity,
            "pending_sell_quantity": pending_sell_quantity,
            "effective_quantity": effective_quantity,
            "available_quantity": available_quantity,
            "target_quantity": target_quantity,
            "delta_quantity": delta,
            "reference_price": reference_price,
            "last_price": reference_price,
            "price_source": price_source,
            "estimated_price": reference_price,
            "action": "HOLD",
            "message": "",
        }
        if delta < 0:
            sell_quantity = min(abs(delta), max(available_quantity, 0))
            if sell_quantity > 0:
                row["action"] = "SELL"
                row["order_quantity"] = sell_quantity
                row["estimated_amount"] = round(sell_quantity * reference_price, 2)
                order = {
                    "symbol": symbol,
                    "side": "SELL",
                    "quantity": sell_quantity,
                    "estimated_price": reference_price,
                    "price_source": price_source,
                    "remark": f"W20 {config.id} rebalance",
                }
                sell_orders.append(order)
            else:
                row["action"] = "SKIP"
                row["message"] = "无可卖数量"
        elif delta > 0:
            buy_quantity = _round_to_lot(delta, lot_size)
            if buy_quantity >= lot_size:
                row["action"] = "BUY"
                row["order_quantity"] = buy_quantity
                row["estimated_amount"] = round(buy_quantity * reference_price, 2)
                order = {
                    "symbol": symbol,
                    "side": "BUY",
                    "quantity": buy_quantity,
                    "estimated_price": reference_price,
                    "price_source": price_source,
                    "remark": f"W20 {config.id} rebalance",
                }
                buy_candidates.append({
                    "row": row,
                    "order": order,
                })
            else:
                row["action"] = "SKIP"
                row["message"] = "买入数量不足最小交易单位"
        is_relevant_row = any([
            row["action"] != "HOLD",
            _safe_float(row.get("target_weight_pct")) > 0,
            _safe_int(row.get("current_quantity")) != 0,
            _safe_int(row.get("pending_buy_quantity")) != 0,
            _safe_int(row.get("pending_sell_quantity")) != 0,
            _safe_int(row.get("target_quantity")) != 0,
            _safe_int(row.get("delta_quantity")) != 0,
        ])
        if is_relevant_row:
            rows.append(row)

    projected_cash = available_cash + sum(_order_estimated_price(item) * _safe_int(item.get("quantity")) for item in sell_orders)
    buy_orders = []
    for candidate in buy_candidates:
        order = candidate["order"]
        cost = _order_estimated_price(order) * _safe_int(order.get("quantity"))
        if cost <= projected_cash:
            buy_orders.append(order)
            projected_cash -= cost
            continue

        price = _order_estimated_price(order)
        partial_quantity = _round_to_lot(projected_cash / price, lot_size) if price > 0 else 0
        row = candidate["row"]
        if partial_quantity >= lot_size:
            order["quantity"] = partial_quantity
            row["order_quantity"] = partial_quantity
            row["estimated_amount"] = round(partial_quantity * price, 2)
            row["message"] = "现金不足，按可用资金部分买入"
            buy_orders.append(order)
            projected_cash -= partial_quantity * price
        else:
            row["action"] = "SKIP"
            row["order_quantity"] = 0
            row["message"] = "现金不足，跳过买入"

    orders = sell_orders + buy_orders
    sub_account_data = serialize_sub_account(sub_account)
    sub_account_data["effective_executor_policy"] = resolve_execution_policy(external_account, sub_account)
    return {
        "external_account": {
            "id": external_account.id,
            "name": external_account.name,
            "identifier": external_account.identifier,
        },
        "strategy_type": STRATEGY_W20,
        "strategy_config_id": config.id,
        "sub_account": sub_account_data,
        "latest_signal": latest_signal,
        "open_order_quantities": open_quantities,
        "target_positions": target_rows,
        "trade_base_value": trade_base_value,
        "trade_base_source": "sub_account_net_asset",
        "sub_account_position_market_value": round(position_market_value, 2),
        "sub_account_net_asset": sub_account_net_asset,
        "sub_account_valuation": valuation,
        "available_cash": available_cash,
        "projected_cash": projected_cash,
        "pricing_note": "W20 仅使用行情估算目标数量；真实下单价格由通用执行器策略决定",
        "rows": rows,
        "orders": orders,
    }


def _build_live_target_signal_version(plan: Dict, target_rows: List[Dict]) -> str:
    latest_signal = plan.get("latest_signal") or {}
    payload = {
        "signal_date": latest_signal.get("signal_date") or latest_signal.get("date"),
        "targets": [
            {
                "symbol": item.get("symbol"),
                "target_quantity": _safe_int(item.get("target_quantity")),
                "target_weight_pct": _safe_float(item.get("target_weight_pct")),
            }
            for item in sorted(target_rows or [], key=lambda row: str(row.get("symbol") or ""))
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    signal_date = str(payload.get("signal_date") or "unknown").replace(":", "").replace(" ", "_")
    return f"w20:{signal_date}:{digest[:16]}"[:64]


def _activate_live_trade_targets(db: Session, plan: Dict) -> Dict:
    sub_account_data = plan.get("sub_account") or {}
    sub_account_id = sub_account_data.get("id")
    if not sub_account_id:
        raise HTTPException(status_code=400, detail="实盘计划缺少虚拟子账户，无法确认目标仓位")

    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.external_trading_account_id == plan.get("external_account", {}).get("id"),
    ).first()
    if not sub_account:
        raise HTTPException(status_code=400, detail="实盘计划中的虚拟子账户不存在")

    latest_signal = plan.get("latest_signal") or {}
    target_rows = plan.get("target_positions") or []
    if not target_rows:
        target_rows = [
            {
                "symbol": item.get("symbol"),
                "target_quantity": item.get("target_quantity"),
                "target_weight_pct": item.get("target_weight_pct"),
                "target_value": item.get("target_value"),
            }
            for item in (plan.get("rows") or [])
            if item.get("symbol")
        ]

    signal_version = _build_live_target_signal_version(plan, target_rows)
    sync_target_positions(
        db,
        sub_account=sub_account,
        targets=target_rows,
        signal_id=str(latest_signal.get("signal_date") or latest_signal.get("date") or ""),
        signal_version=signal_version,
        source_execution_id=None,
    )
    activation = {
        "sub_account_id": sub_account.id,
        "target_count": len(target_rows),
        "signal_version": signal_version,
        "activated_at": datetime.now().isoformat(),
    }
    plan["targets_activated_at"] = activation["activated_at"]
    plan["target_signal_version"] = signal_version
    return activation


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def _get_live_trade_confirm_url(config_id: int) -> str:
    base_url = (
        os.getenv("QUANT_WEB_BASE_URL")
        or os.getenv("FRONTEND_BASE_URL")
        or os.getenv("WEB_BASE_URL")
        or "https://52etf.vip"
    ).rstrip("/")
    query = urlencode({"config_id": config_id, "tab": "live", "plan": "latest"})
    return f"{base_url}/w20-momentum-live?{query}"


def _format_live_trade_money(value, digits: int = 2) -> str:
    return f"{_safe_float(value):,.{digits}f}"


def _format_live_trade_price(value) -> str:
    price = _safe_float(value)
    return "-" if price <= 0 else f"{price:.4f}"


def _format_live_trade_targets(latest_signal: Dict) -> str:
    symbols = latest_signal.get("selected_symbols") or []
    weights = latest_signal.get("target_weights_pct") or []
    items = []
    for index, symbol in enumerate(symbols):
        weight = _safe_float(weights[index]) if index < len(weights) else 0.0
        items.append(f"{symbol} {weight:.2f}%")
    return "、".join(items) if items else "-"


def _format_live_trade_order_lines(orders: List[Dict], limit: int = 20) -> str:
    lines = []
    for order in (orders or [])[:limit]:
        side = str(order.get("side") or "").upper()
        side_label = "买入" if side == "BUY" else "卖出" if side == "SELL" else side
        quantity = _safe_int(order.get("quantity"))
        price = _order_estimated_price(order)
        amount = quantity * price
        price_text = f"计划估算价 {_format_live_trade_price(price)}，真实下单价格由通用执行器决定"
        lines.append(
            f"- {side_label} {order.get('symbol')}: 数量 {quantity:,}，{price_text}，预估金额 {amount:,.2f}"
        )
    if len(orders or []) > limit:
        lines.append(f"- ... 还有 {len(orders) - limit} 笔订单，请到确认页查看")
    return "\n".join(lines) if lines else "- 无待提交订单"


def _build_live_trade_plan_email_body(
    config: W20MomentumLiveConfig,
    external_account: ExternalTradingAccount,
    plan: Dict,
    confirm_url: str,
) -> str:
    latest_signal = plan.get("latest_signal") or {}
    orders = plan.get("orders") or []
    generated_at = datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join([
        "A股ETF W20 风险调整动量虚拟盘已自动生成实盘调仓计划。",
        "",
        "定时任务只生成计划，不会自动提交订单。请打开确认页核对后确认；若确认时不在 A 股交易时段，系统会等开盘后自动执行。",
        "",
        f"策略: {config.name} (ID: {config.id})",
        f"生成时间: {generated_at}",
        f"信号日期: {latest_signal.get('date') or '-'}",
        f"目标持仓: {_format_live_trade_targets(latest_signal)}",
        f"外部交易账户: {external_account.name} ({external_account.identifier})",
        f"目标净资产: {_format_live_trade_money(plan.get('trade_base_value'))}",
        f"可用现金: {_format_live_trade_money(plan.get('available_cash'))}",
        f"预计剩余现金: {_format_live_trade_money(plan.get('projected_cash'))}",
        "执行策略: 真实下单价格、超时和重定价由外部交易账号/虚拟子账户的通用执行器配置决定",
        f"待提交订单数: {len(orders)}",
        "",
        "订单明细:",
        _format_live_trade_order_lines(orders),
        "",
        f"确认下单页: {confirm_url}",
    ])


def _send_live_trade_plan_email(
    config: W20MomentumLiveConfig,
    external_account: ExternalTradingAccount,
    plan: Dict,
    confirm_url: str,
):
    subject = f"W20 实盘调仓计划待确认: {config.name}"
    body = _build_live_trade_plan_email_body(config, external_account, plan, confirm_url)
    send_alert_email(subject, body)


def _send_live_trade_plan_failure_email(
    config: W20MomentumLiveConfig,
    error_message: str,
):
    confirm_url = _get_live_trade_confirm_url(config.id)
    body = "\n".join([
        "A股ETF W20 风险调整动量虚拟盘定时同步已完成，但实盘调仓计划生成失败。",
        "",
        f"策略: {config.name} (ID: {config.id})",
        f"失败时间: {datetime.now(CHINA_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"错误: {error_message}",
        "",
        "请检查外部交易账户是否在线、是否已启用，以及 PTrade 长连接是否正常。",
        f"确认页: {confirm_url}",
    ])
    send_alert_email(f"W20 实盘计划生成失败: {config.name}", body)


def _add_live_trade_plan_log(
    db: Session,
    config: W20MomentumLiveConfig,
    plan: Dict,
    *,
    trigger_source: str,
    sync_before: bool,
):
    db.add(W20MomentumLiveLog(
        config_id=config.id,
        account_id=config.account_id,
        timestamp=datetime.now(),
        level="INFO",
        action="LIVE_TRADE_PLAN",
        message="已生成实盘调仓计划",
        payload={
            "dry_run": True,
            "sync_before": sync_before,
            "trigger_source": trigger_source,
            "plan": plan,
            "execution": None,
        },
    ))


def _record_live_trade_plan_failure(
    db: Session,
    config_id: int,
    account_id: str,
    error_message: str,
    *,
    trigger_source: str,
):
    config = db.query(W20MomentumLiveConfig).filter(
        W20MomentumLiveConfig.id == config_id,
        W20MomentumLiveConfig.account_id == account_id,
    ).first()
    db.add(W20MomentumLiveLog(
        config_id=config_id,
        account_id=account_id,
        timestamp=datetime.now(),
        level="ERROR",
        action="LIVE_TRADE_PLAN_FAILED",
        message=error_message[:1000],
        payload={"trigger_source": trigger_source, "error": error_message},
    ))
    if config:
        _send_live_trade_plan_failure_email(config, error_message)


def _generate_scheduled_live_trade_plan(
    db: Session,
    config: W20MomentumLiveConfig,
    sync_result: Dict,
    trigger_source: str = "schedule",
) -> Optional[Dict]:
    if not getattr(config, "live_trade_enabled", False):
        return None

    latest_signal = sync_result.get("latest_signal") or {}
    with get_external_trading_db_ctx() as trading_db:
        external_account = _get_external_account_for_live_trade(trading_db, config)
        plan = asyncio.run(_build_live_trade_plan(trading_db, config, external_account, latest_signal))
    _add_live_trade_plan_log(
        db,
        config,
        plan,
        trigger_source=trigger_source,
        sync_before=True,
    )

    orders = plan.get("orders") or []
    confirm_url = _get_live_trade_confirm_url(config.id)
    emailed = False
    if orders:
        _send_live_trade_plan_email(config, external_account, plan, confirm_url)
        emailed = True

    return {
        "status": "generated",
        "order_count": len(orders),
        "row_count": len(plan.get("rows") or []),
        "emailed": emailed,
        "confirm_url": confirm_url,
    }


def _get_latest_live_trade_plan_log(
    db: Session,
    config: W20MomentumLiveConfig,
) -> Optional[W20MomentumLiveLog]:
    return (
        db.query(W20MomentumLiveLog)
        .filter(
            W20MomentumLiveLog.config_id == config.id,
            W20MomentumLiveLog.action == "LIVE_TRADE_PLAN",
        )
        .order_by(W20MomentumLiveLog.timestamp.desc(), W20MomentumLiveLog.id.desc())
        .first()
    )


async def _prepare_live_trade_plan(
    db: Session,
    trading_db: Session,
    config: W20MomentumLiveConfig,
    external_account: ExternalTradingAccount,
    *,
    sync_before: bool,
    trigger_source: str,
) -> Dict:
    if sync_before:
        _mark_sync_running(db, config, message="实盘执行前同步中")
        db.commit()
        sync_result = _sync_config_now(db, config, trigger_source=trigger_source)
        db.commit()
        latest_signal = sync_result.get("latest_signal") or {}
    else:
        latest_signal = (_get_latest_sync_payload(db, config).get("latest_signal") or {})
    return await _build_live_trade_plan(trading_db, config, external_account, latest_signal)


def _get_latest_live_trade_approval_log(
    db: Session,
    config: W20MomentumLiveConfig,
) -> Optional[W20MomentumLiveLog]:
    return (
        db.query(W20MomentumLiveLog)
        .filter(
            W20MomentumLiveLog.config_id == config.id,
            W20MomentumLiveLog.action == "LIVE_TRADE_APPROVED",
        )
        .order_by(W20MomentumLiveLog.timestamp.desc(), W20MomentumLiveLog.id.desc())
        .first()
    )


def _approval_log_to_dict(log: Optional[W20MomentumLiveLog]) -> Optional[Dict]:
    if not log:
        return None
    payload = log.payload or {}
    return {
        "id": log.id,
        "timestamp": log.timestamp.isoformat() if log.timestamp else None,
        "message": log.message,
        "trigger_source": payload.get("trigger_source"),
        "market_open": payload.get("market_open"),
        "executor_triggered": payload.get("executor_triggered"),
        "activation": payload.get("activation"),
        "executor_result": payload.get("executor_result"),
    }


def _add_live_trade_approval_log(
    db: Session,
    config: W20MomentumLiveConfig,
    plan: Dict,
    *,
    activation: Dict,
    market_open: bool,
    trigger_source: str,
    executor_result: Optional[Dict] = None,
) -> Dict:
    now = datetime.now()
    message = (
        "已确认目标仓位并触发通用执行器"
        if market_open and executor_result is not None
        else "已确认目标仓位，通用执行器将在下个 A 股交易时段自动执行"
    )
    db.add(W20MomentumLiveLog(
        config_id=config.id,
        account_id=config.account_id,
        timestamp=now,
        level="INFO",
        action="LIVE_TRADE_APPROVED",
        message=message,
        payload={
            "trigger_source": trigger_source,
            "market_open": market_open,
            "executor_triggered": bool(market_open and executor_result is not None),
            "activation": activation,
            "plan": plan,
            "executor_result": executor_result,
        },
    ))
    return activation


def _approve_live_trade_plan(
    db: Session,
    trading_db: Session,
    config: W20MomentumLiveConfig,
    plan: Dict,
    *,
    market_open: bool,
    trigger_source: str,
    executor_result: Optional[Dict] = None,
) -> Dict:
    activation = _activate_live_trade_targets(trading_db, plan)
    return _add_live_trade_approval_log(
        db,
        config,
        plan,
        activation=activation,
        market_open=market_open,
        trigger_source=trigger_source,
        executor_result=executor_result,
    )


def _validate_live_trade_plan_binding(
    config: W20MomentumLiveConfig,
    external_account: ExternalTradingAccount,
    plan: Dict,
) -> None:
    plan_external_account_id = _safe_int((plan.get("external_account") or {}).get("id"))
    plan_sub_account_id = _safe_int((plan.get("sub_account") or {}).get("id"))
    if plan_external_account_id != external_account.id or plan_sub_account_id != config.live_sub_account_id:
        raise HTTPException(status_code=400, detail="最近实盘计划与当前外部账户/虚拟子账户不一致，请重新生成实盘计划")


def process_w20_momentum_live_strategy_automation_for_robot() -> Dict:
    now_cn = _china_now()
    today = now_cn.date()
    if not _is_china_trading_day(today):
        return {"signals": 0, "virtual_trades": 0, "plan_emails": 0, "errors": [], "skipped": "not_trading_day"}

    result = {
        "signals": 0,
        "signal_waiting": 0,
        "virtual_trades": 0,
        "virtual_trade_waiting": 0,
        "plan_emails": 0,
        "plan_skipped": 0,
        "errors": [],
    }
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
            current_time = now_cn.time()

            should_run_signal = (
                bool(getattr(config, "auto_signal_enabled", True))
                and current_time >= _parse_hhmm(getattr(config, "auto_signal_time", None), dtime(18, 35))
                and not _has_run_today(getattr(config, "last_auto_signal_at", None), today)
            )
            should_run_virtual_trade = (
                bool(getattr(config, "auto_virtual_trade_enabled", True))
                and _is_china_live_trade_window(now_cn)
                and current_time >= _parse_hhmm(getattr(config, "auto_virtual_trade_time", None), dtime(9, 31))
                and not _has_run_today(getattr(config, "last_auto_virtual_trade_at", None), today)
            )

            if should_run_virtual_trade:
                _mark_sync_running(db, config, message="自动开盘虚拟交易更新中")
                db.commit()
                try:
                    sync_result = _sync_config_now(
                        db,
                        config,
                        trigger_source="auto_virtual_trade",
                        use_realtime_execution=True,
                    )
                    live_context = sync_result.get("live_execution_context") or {}
                    if not live_context.get("used"):
                        db.rollback()
                        _mark_auto_waiting(
                            db,
                            config_id,
                            account_id,
                            "等待 09:30 后当日实时盘口，稍后重试开盘虚拟交易更新",
                        )
                        db.commit()
                        result["virtual_trade_waiting"] += 1
                        logger.info("W20 auto virtual trade waiting for realtime quotes: config=%s", config_id)
                        continue

                    now = datetime.now()
                    config.last_auto_virtual_trade_at = now
                    config.updated_at = now
                    db.commit()
                    result["virtual_trades"] += 1
                    logger.info(
                        "W20 auto virtual trade updated: config=%s live_context=%s",
                        config_id,
                        live_context.get("used"),
                    )
                except Exception as exc:
                    db.rollback()
                    _mark_sync_failed(db, config_id, account_id, exc)
                    db.commit()
                    error_message = _exception_message(exc)
                    logger.exception("W20 auto virtual trade update failed")
                    result["errors"].append({
                        "id": config_id,
                        "account_id": account_id,
                        "name": config_name,
                        "stage": "auto_virtual_trade",
                        "error": error_message,
                    })

            if should_run_signal:
                _mark_sync_running(db, config, message="自动盘后信号生成中")
                db.commit()
                try:
                    sync_result = _sync_config_now(
                        db,
                        config,
                        trigger_source="auto_signal",
                        use_realtime_execution=False,
                    )
                    effective_date = _get_result_effective_date(sync_result)
                    if effective_date != today:
                        db.rollback()
                        _mark_auto_waiting(
                            db,
                            config_id,
                            account_id,
                            f"等待当日 A 股基础数据更新，当前有效日期 {effective_date.isoformat() if effective_date else '-'}",
                        )
                        db.commit()
                        result["signal_waiting"] += 1
                        logger.info(
                            "W20 auto signal waiting for current daily data: config=%s effective_date=%s today=%s",
                            config_id,
                            effective_date,
                            today,
                        )
                        continue

                    now = datetime.now()
                    config.last_auto_signal_at = now
                    config.updated_at = now
                    db.commit()
                    result["signals"] += 1

                    if getattr(config, "live_trade_enabled", False):
                        signal_date = _get_result_latest_signal_date(sync_result)
                        if signal_date != today:
                            db.add(W20MomentumLiveLog(
                                config_id=config_id,
                                account_id=account_id,
                                timestamp=datetime.now(),
                                level="INFO",
                                action="LIVE_TRADE_PLAN_SKIPPED",
                                message="今日没有新的调仓信号，跳过实盘计划邮件",
                                payload={
                                    "trigger_source": "auto_signal",
                                    "latest_signal_date": signal_date.isoformat() if signal_date else None,
                                    "effective_date": effective_date.isoformat() if effective_date else None,
                                    "expected_date": today.isoformat(),
                                },
                            ))
                            db.commit()
                            result["plan_skipped"] += 1
                            continue

                        try:
                            plan_result = _generate_scheduled_live_trade_plan(
                                db,
                                config,
                                sync_result,
                                trigger_source="auto_signal",
                            )
                            db.commit()
                            if plan_result and plan_result.get("emailed"):
                                result["plan_emails"] += 1
                        except Exception as plan_exc:
                            db.rollback()
                            error_message = _exception_message(plan_exc)
                            _record_live_trade_plan_failure(
                                db,
                                config_id,
                                account_id,
                                error_message,
                                trigger_source="auto_signal",
                            )
                            db.commit()
                            logger.exception("W20 auto live trade plan generation failed")
                            result["errors"].append({
                                "id": config_id,
                                "account_id": account_id,
                                "name": config_name,
                                "stage": "auto_live_trade_plan",
                                "error": error_message,
                            })
                except Exception as exc:
                    db.rollback()
                    _mark_sync_failed(db, config_id, account_id, exc)
                    db.commit()
                    error_message = _exception_message(exc)
                    logger.exception("W20 auto signal generation failed")
                    result["errors"].append({
                        "id": config_id,
                        "account_id": account_id,
                        "name": config_name,
                        "stage": "auto_signal",
                        "error": error_message,
                    })

    return result


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
    trading_db: Session = Depends(get_external_trading_db),
):
    _validate_external_account_selection(trading_db, account_id, payload.external_trading_account_id)
    config = W20MomentumLiveConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload(config, payload)
    db.add(config)
    db.flush()
    _sync_w20_live_sub_account_binding(trading_db, config, previous_sub_account_id=None)
    trading_db.commit()
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
    trading_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    previous_sub_account_id = getattr(config, "live_sub_account_id", None)
    _validate_external_account_selection(trading_db, account_id, payload.external_trading_account_id)
    _apply_payload(config, payload)
    _sync_w20_live_sub_account_binding(trading_db, config, previous_sub_account_id=previous_sub_account_id)
    trading_db.commit()
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.delete("/configs/{config_id}")
def delete_w20_momentum_live_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    if getattr(config, "live_sub_account_id", None):
        _deactivate_w20_target_positions(
            trading_db,
            sub_account_id=config.live_sub_account_id,
            config_id=config.id,
        )
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_W20,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if sub_account:
            sub_account.strategy_type = None
            sub_account.strategy_config_id = None
            sub_account.updated_at = datetime.now()
    db.query(W20MomentumLiveTrade).filter(W20MomentumLiveTrade.config_id == config.id).delete()
    db.query(W20MomentumLiveHolding).filter(W20MomentumLiveHolding.config_id == config.id).delete()
    db.query(W20MomentumLiveEquity).filter(W20MomentumLiveEquity.config_id == config.id).delete()
    db.query(W20MomentumLiveLog).filter(W20MomentumLiveLog.config_id == config.id).delete()
    db.delete(config)
    trading_db.commit()
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


@router.post("/configs/{config_id}/live-trade")
async def execute_w20_momentum_live_trade(
    config_id: int,
    payload: W20LiveTradeRequest,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    external_account = _get_external_account_for_live_trade(trading_db, config)

    try:
        market_open = _is_china_live_trade_window()
        if payload.dry_run:
            plan = await _prepare_live_trade_plan(
                db,
                trading_db,
                config,
                external_account,
                sync_before=payload.sync_before,
                trigger_source="live_trade",
            )
            now = datetime.now()
            db.add(W20MomentumLiveLog(
                config_id=config.id,
                account_id=account_id,
                timestamp=now,
                level="INFO",
                action="LIVE_TRADE_PLAN",
                message="已生成实盘调仓计划",
                payload={
                    "dry_run": True,
                    "sync_before": payload.sync_before,
                    "trigger_source": "manual",
                    "plan": plan,
                    "execution": None,
                },
            ))
            db.commit()
            return {
                "message": "实盘调仓计划已生成",
                "dry_run": True,
                "approved": False,
                "deferred": False,
                "plan": plan,
                "execution": None,
                "approval": _approval_log_to_dict(_get_latest_live_trade_approval_log(db, config)),
                "timestamp": now.isoformat(),
                "trigger_source": "manual",
            }

        if not market_open:
            if not getattr(external_account, "executor_enabled", True):
                raise HTTPException(status_code=400, detail="非交易时段确认需要先开启外部交易账户的通用执行器定时执行")
            latest_plan_log = _get_latest_live_trade_plan_log(db, config)
            plan = (latest_plan_log.payload or {}).get("plan") if latest_plan_log and latest_plan_log.payload else None
            if not plan:
                raise HTTPException(status_code=400, detail="暂无可确认的实盘计划，请先生成实盘计划")
            _validate_live_trade_plan_binding(config, external_account, plan)
            activation = _approve_live_trade_plan(
                db,
                trading_db,
                config,
                plan,
                market_open=False,
                trigger_source="manual_confirm",
            )
            now = datetime.now()
            trading_db.commit()
            db.commit()
            return {
                "message": "目标仓位已确认，通用执行器将在下个 A 股交易时段自动执行",
                "dry_run": False,
                "approved": True,
                "deferred": True,
                "plan": plan,
                "execution": None,
                "approval": {
                    "activation": activation,
                    "timestamp": now.isoformat(),
                    "market_open": False,
                    "executor_triggered": False,
                },
                "timestamp": now.isoformat(),
                "trigger_source": "manual_confirm",
            }

        plan = await _prepare_live_trade_plan(
            db,
            trading_db,
            config,
            external_account,
            sync_before=payload.sync_before,
            trigger_source="live_trade",
        )
        activation = _activate_live_trade_targets(trading_db, plan)
        trading_db.commit()
        execution = await trigger_external_trading_executor(
            account_id=account_id,
            external_account_id=external_account.id,
            trigger_source="w20_manual_confirm",
        )
        with get_db_ctx() as approval_db:
            approval_config = approval_db.query(W20MomentumLiveConfig).filter(
                W20MomentumLiveConfig.id == config.id,
                W20MomentumLiveConfig.account_id == account_id,
            ).first()
            if approval_config:
                _add_live_trade_approval_log(
                    approval_db,
                    approval_config,
                    plan,
                    activation=activation,
                    market_open=True,
                    trigger_source="manual_confirm",
                    executor_result=execution,
                )
        now = datetime.now()
        return {
            "message": "目标仓位已确认，并已触发通用执行器",
            "dry_run": False,
            "approved": True,
            "deferred": False,
            "plan": plan,
            "execution": execution,
            "approval": {
                "activation": activation,
                "timestamp": now.isoformat(),
                "market_open": True,
                "executor_triggered": True,
                "executor_result": execution,
            },
            "timestamp": now.isoformat(),
            "trigger_source": "manual_confirm",
        }
    except HTTPException:
        trading_db.rollback()
        db.rollback()
        raise
    except Exception as exc:
        trading_db.rollback()
        db.rollback()
        logger.exception("W20 live trade execution failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/configs/{config_id}/live-trade/latest-plan")
def get_latest_w20_live_trade_plan(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    latest_approval = _get_latest_live_trade_approval_log(db, config)
    latest_log = _get_latest_live_trade_plan_log(db, config)
    if not latest_log:
        return {
            "message": "暂无实盘调仓计划",
            "log_id": None,
            "dry_run": True,
            "plan": None,
            "execution": None,
            "timestamp": None,
            "trigger_source": None,
            "approval": _approval_log_to_dict(latest_approval),
        }

    payload = latest_log.payload or {}
    return {
        "message": latest_log.message or "实盘调仓计划",
        "log_id": latest_log.id,
        "dry_run": payload.get("dry_run", True),
        "plan": payload.get("plan"),
        "execution": payload.get("execution"),
        "timestamp": latest_log.timestamp.isoformat() if latest_log.timestamp else None,
        "trigger_source": payload.get("trigger_source"),
        "approval": _approval_log_to_dict(latest_approval),
    }


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
                synced_item = {
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "summary": sync_result.get("metrics"),
                }
                result["synced"].append(synced_item)
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
    trading_db: Session = Depends(get_external_trading_db),
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
    live_sub_account = None
    live_ledger_positions = []
    live_orders = []
    if getattr(config, "live_sub_account_id", None):
        live_sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == account_id,
        ).first()
    if live_sub_account:
        live_external_account = trading_db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.id == live_sub_account.external_trading_account_id,
            ExternalTradingAccount.account_id == account_id,
        ).first()
        live_sub_account_data = serialize_sub_account(live_sub_account)
        live_sub_account_data["effective_executor_policy"] = (
            resolve_execution_policy(live_external_account, live_sub_account)
            if live_external_account else None
        )
        live_ledger_positions = (
            trading_db.query(ExternalTradingLedgerPosition)
            .filter(ExternalTradingLedgerPosition.sub_account_id == live_sub_account.id)
            .order_by(ExternalTradingLedgerPosition.market_value.desc(), ExternalTradingLedgerPosition.symbol.asc())
            .all()
        )
        live_orders = (
            trading_db.query(ExternalTradingOrder)
            .filter(ExternalTradingOrder.sub_account_id == live_sub_account.id)
            .order_by(ExternalTradingOrder.created_at.desc(), ExternalTradingOrder.id.desc())
            .limit(100)
            .all()
        )

    return {
        "config": _config_to_dict(config),
        "live_sub_account": live_sub_account_data if live_sub_account else None,
        "live_ledger_positions": [serialize_ledger_position(item) for item in live_ledger_positions],
        "live_orders": [serialize_order(item) for item in live_orders],
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
                "price_source": item.price_source,
                "quote_timestamp": item.quote_timestamp.isoformat() if item.quote_timestamp else None,
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
