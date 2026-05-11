import asyncio
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
    ExternalTradingAccount,
    W20MomentumLiveConfig,
    W20MomentumLiveEquity,
    W20MomentumLiveExecution,
    W20MomentumLiveHolding,
    W20MomentumLiveLog,
    W20MomentumLiveTrade,
    get_db,
    get_db_ctx,
)
from ...core.services.external_trading import ExternalTradingConnectionError, external_trading_hub
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
W20_PENDING_EXECUTION_STATUSES = {"PENDING", "PROCESSING"}


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
    live_trade_total_amount: Optional[float] = None
    live_trade_order_type: str = "LIMIT"
    live_trade_price_level: int = 1
    live_trade_market_type: int = 0

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

    @validator("live_trade_total_amount")
    def validate_live_trade_total_amount(cls, value):
        if value is not None and value <= 0:
            raise ValueError("实盘目标资金必须大于 0")
        return value

    @validator("live_trade_order_type")
    def validate_live_trade_order_type(cls, value):
        normalized = str(value or "LIMIT").upper()
        if normalized in {"MKT", "MARKET"}:
            return "MARKET"
        if normalized != "LIMIT":
            raise ValueError("实盘下单方式仅支持 LIMIT 或 MARKET")
        return normalized

    @validator("live_trade_price_level")
    def validate_live_trade_price_level(cls, value):
        if value not in {-1, 0, 1, 2, 3, 4, 5}:
            raise ValueError("实盘限价档位仅支持 -1、0、1、2、3、4、5")
        return value

    @validator("live_trade_market_type")
    def validate_live_trade_market_type(cls, value):
        if value not in {0, 1, 2, 3, 4, 5}:
            raise ValueError("市价委托类型仅支持 0、1、2、3、4、5")
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
    defer_until_market_open: bool = True


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
        "live_trade_total_amount": getattr(config, "live_trade_total_amount", None),
        "live_trade_order_type": getattr(config, "live_trade_order_type", None) or "LIMIT",
        "live_trade_price_level": getattr(config, "live_trade_price_level", 1),
        "live_trade_market_type": getattr(config, "live_trade_market_type", 0),
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


def _get_quote_last_price(quote: Dict) -> float:
    for key in ("price", "last_px", "last_price", "latest_price", "current_price"):
        price = _safe_float(quote.get(key))
        if price > 0:
            return price
    return 0.0


def _get_order_reference_price(quote: Dict, side: str, price_level: int) -> Tuple[float, str]:
    side = str(side or "").upper()
    bid = _safe_float(quote.get("bid"))
    ask = _safe_float(quote.get("ask"))
    last_price = _get_quote_last_price(quote)

    if price_level == -1:
        price = ask if side == "BUY" else bid
        return price or last_price, "ptrade_depth_fallback"
    if price_level == 0:
        return last_price, "last_price"

    levels = quote.get("ask_levels") if side == "BUY" else quote.get("bid_levels")
    price = _get_level_price(levels, price_level)
    return price, f"level_{price_level}"


def _order_estimated_price(order: Dict) -> float:
    return _safe_float(
        order.get("limit_price"),
        _safe_float(order.get("protection_limit_price"), _safe_float(order.get("estimated_price"))),
    )


def _normalize_live_trade_order_type(config: W20MomentumLiveConfig) -> str:
    order_type = str(getattr(config, "live_trade_order_type", None) or "LIMIT").upper()
    return "MARKET" if order_type in {"MARKET", "MKT"} else "LIMIT"


def _get_live_trade_order_type_label(order_type: str) -> str:
    return "市价单" if str(order_type or "").upper() == "MARKET" else "限价单"


def _get_market_type_label(value) -> str:
    return {
        0: "对手方最优价格",
        1: "最优五档即时成交剩余转限价",
        2: "本方最优价格",
        3: "即时成交剩余撤销",
        4: "最优五档即时成交剩余撤销",
        5: "全额成交或撤单",
    }.get(_safe_int(value), str(value if value is not None else "-"))


def _validate_live_market_type_for_symbol(symbol: str, market_type: int):
    suffix = str(symbol or "").split(".")[-1].upper()
    allowed = {0, 1, 2, 4} if suffix in {"SH", "SS"} else {0, 2, 3, 4, 5}
    if market_type not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"{symbol} 不支持市价委托类型 {market_type}（{_get_market_type_label(market_type)}）",
        )


def _china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def _to_naive_china_datetime(value: datetime) -> datetime:
    return value.astimezone(CHINA_TZ).replace(tzinfo=None) if value.tzinfo else value


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


def _next_china_live_trade_time(now: Optional[datetime] = None) -> datetime:
    current = now or _china_now()
    if current.tzinfo:
        current = current.astimezone(CHINA_TZ)

    if _is_china_trading_day(current.date()):
        current_time = current.time()
        if current_time < LIVE_EXECUTION_START_TIME:
            return current.replace(
                hour=LIVE_EXECUTION_START_TIME.hour,
                minute=LIVE_EXECUTION_START_TIME.minute,
                second=0,
                microsecond=0,
            )
        if CHINA_MARKET_MORNING_CLOSE < current_time < CHINA_MARKET_AFTERNOON_OPEN:
            return current.replace(
                hour=CHINA_MARKET_AFTERNOON_OPEN.hour,
                minute=CHINA_MARKET_AFTERNOON_OPEN.minute,
                second=0,
                microsecond=0,
            )

    next_day = current.date() + timedelta(days=1)
    while not _is_china_trading_day(next_day):
        next_day += timedelta(days=1)
    return datetime.combine(next_day, LIVE_EXECUTION_START_TIME, tzinfo=CHINA_TZ)


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

    try:
        assets_resp, positions_resp = await asyncio.gather(
            external_trading_hub.get_assets(external_account.id, timeout=10.0),
            external_trading_hub.get_positions(external_account.id, timeout=10.0),
        )
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    assets = assets_resp.get("assets") or {}
    raw_positions = positions_resp.get("positions") or []
    current_positions = {}
    for item in raw_positions:
        symbol = _to_w20_symbol(item.get("symbol") or item.get("client_symbol"))
        if not symbol:
            continue
        current_positions[symbol] = {
            "quantity": _safe_int(item.get("quantity")),
            "available_quantity": _safe_int(item.get("available_quantity"), _safe_int(item.get("quantity"))),
            "cost_price": _safe_float(item.get("cost_price")),
        }

    managed_symbols = set(_to_w20_symbol(symbol) for symbol in (config.symbols or []))
    quote_symbols = sorted(managed_symbols | set(selected_symbols) | set(current_positions.keys()))
    try:
        quotes_resp = await external_trading_hub.get_quotes(external_account.id, quote_symbols, timeout=10.0)
    except ExternalTradingConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    quotes = {}
    for item in quotes_resp.get("quotes") or []:
        symbol = _to_w20_symbol(item.get("symbol") or item.get("client_symbol"))
        if symbol:
            quotes[symbol] = item

    lot_size = max(int(config.lot_size or 100), 1)
    account_portfolio_value = _safe_float(assets.get("portfolio_value"))
    available_cash = _safe_float(assets.get("available_cash"), _safe_float(assets.get("total_cash")))
    configured_trade_amount = _safe_float(getattr(config, "live_trade_total_amount", None))
    trade_base_value = configured_trade_amount if configured_trade_amount > 0 else account_portfolio_value
    if trade_base_value <= 0:
        raise HTTPException(status_code=400, detail="外部账户资产为空，无法生成实盘计划")

    price_level = int(getattr(config, "live_trade_price_level", 1) if getattr(config, "live_trade_price_level", None) is not None else 1)
    order_type = _normalize_live_trade_order_type(config)
    market_type = int(getattr(config, "live_trade_market_type", 0) or 0)
    rows = []
    sell_orders = []
    buy_candidates = []
    for symbol in sorted(managed_symbols | set(selected_symbols)):
        quote = quotes.get(symbol) or {}
        bid = _safe_float(quote.get("bid"))
        ask = _safe_float(quote.get("ask"))
        last_price = _get_quote_last_price(quote)
        buy_price, buy_price_source = _get_order_reference_price(quote, "BUY", price_level)
        sell_price, sell_price_source = _get_order_reference_price(quote, "SELL", price_level)
        reference_price = buy_price or sell_price or ask or bid or last_price
        if reference_price <= 0:
            rows.append({
                "symbol": symbol,
                "target_weight_pct": round(target_weight_by_symbol.get(symbol, 0.0) * 100, 4),
                "current_quantity": current_positions.get(symbol, {}).get("quantity", 0),
                "target_quantity": 0,
                "delta_quantity": 0,
                "price_level": price_level,
                "order_type": order_type,
                "market_type": market_type if order_type == "MARKET" else None,
                "action": "SKIP",
                "message": "无可用盘口价格",
            })
            continue

        current_quantity = current_positions.get(symbol, {}).get("quantity", 0)
        available_quantity = current_positions.get(symbol, {}).get("available_quantity", current_quantity)
        target_weight = target_weight_by_symbol.get(symbol, 0.0)
        target_value = trade_base_value * target_weight
        target_quantity = _round_to_lot(target_value / buy_price, lot_size) if target_weight > 0 and buy_price > 0 else 0
        delta = target_quantity - current_quantity

        row = {
            "symbol": symbol,
            "target_weight_pct": round(target_weight * 100, 4),
            "current_quantity": current_quantity,
            "available_quantity": available_quantity,
            "target_quantity": target_quantity,
            "delta_quantity": delta,
            "bid": bid or None,
            "ask": ask or None,
            "last_price": last_price or None,
            "price_level": price_level,
            "order_type": order_type,
            "market_type": market_type if order_type == "MARKET" else None,
            "buy_price_source": buy_price_source,
            "sell_price_source": sell_price_source,
            "estimated_price": buy_price if delta > 0 else sell_price,
            "action": "HOLD",
            "message": "",
        }
        if delta < 0:
            if sell_price <= 0:
                row["action"] = "SKIP"
                row["message"] = "无可用卖出价格"
                rows.append(row)
                continue
            sell_quantity = min(abs(delta), max(available_quantity, 0))
            if sell_quantity > 0:
                row["action"] = "SELL"
                row["order_quantity"] = sell_quantity
                row["estimated_amount"] = round(sell_quantity * sell_price, 2)
                order = {
                    "symbol": symbol,
                    "side": "SELL",
                    "quantity": sell_quantity,
                    "order_type": order_type,
                    "estimated_price": sell_price,
                    "price_level": price_level,
                    "price_source": sell_price_source,
                    "remark": f"W20 {config.id} rebalance",
                }
                if order_type == "MARKET":
                    _validate_live_market_type_for_symbol(symbol, market_type)
                    order["market_type"] = market_type
                sell_orders.append(order)
            else:
                row["action"] = "SKIP"
                row["message"] = "无可卖数量"
        elif delta > 0:
            if buy_price <= 0:
                row["action"] = "SKIP"
                row["message"] = "无可用买入价格"
                rows.append(row)
                continue
            buy_quantity = _round_to_lot(delta, lot_size)
            if buy_quantity >= lot_size:
                row["action"] = "BUY"
                row["order_quantity"] = buy_quantity
                row["estimated_amount"] = round(buy_quantity * buy_price, 2)
                order = {
                    "symbol": symbol,
                    "side": "BUY",
                    "quantity": buy_quantity,
                    "order_type": order_type,
                    "estimated_price": buy_price,
                    "price_level": price_level,
                    "price_source": buy_price_source,
                    "remark": f"W20 {config.id} rebalance",
                }
                if order_type == "MARKET":
                    _validate_live_market_type_for_symbol(symbol, market_type)
                    order["market_type"] = market_type
                buy_candidates.append({
                    "row": row,
                    "order": order,
                })
            else:
                row["action"] = "SKIP"
                row["message"] = "买入数量不足最小交易单位"
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
    return {
        "external_account": {
            "id": external_account.id,
            "name": external_account.name,
            "identifier": external_account.identifier,
        },
        "latest_signal": latest_signal,
        "assets": assets,
        "trade_base_value": trade_base_value,
        "available_cash": available_cash,
        "projected_cash": projected_cash,
        "order_type": order_type,
        "price_level": price_level,
        "market_type": market_type if order_type == "MARKET" else None,
        "rows": rows,
        "orders": orders,
    }


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
        if str(order.get("order_type") or "LIMIT").upper() == "MARKET":
            price_text = (
                f"市价单({_get_market_type_label(order.get('market_type'))})，"
                f"PTrade 下单时现场计算保护价，当前估算价 {_format_live_trade_price(price)}"
            )
        else:
            price_text = f"PTrade 下单时现场计算限价，当前估算价 {_format_live_trade_price(price)}"
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
    price_level = plan.get("price_level")
    order_type = plan.get("order_type") or getattr(config, "live_trade_order_type", None) or "LIMIT"
    price_level_text = {
        -1: "PTrade 深度兜底",
        0: "最新成交价",
        1: "一档",
        2: "二档",
        3: "三档",
        4: "四档",
        5: "五档",
    }.get(price_level, str(price_level or "-"))
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
        f"目标资金: {_format_live_trade_money(plan.get('trade_base_value'))}",
        f"可用现金: {_format_live_trade_money(plan.get('available_cash'))}",
        f"预计剩余现金: {_format_live_trade_money(plan.get('projected_cash'))}",
        f"下单方式: {_get_live_trade_order_type_label(order_type)}",
        f"执行价格规则: {price_level_text}（PTrade 下单时现场取快照计算）",
        f"市价委托类型: {_get_market_type_label(plan.get('market_type')) if str(order_type).upper() == 'MARKET' else '-'}",
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

    external_account = _get_external_account_for_live_trade(db, config)
    latest_signal = sync_result.get("latest_signal") or {}
    plan = asyncio.run(_build_live_trade_plan(db, config, external_account, latest_signal))
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
    return await _build_live_trade_plan(db, config, external_account, latest_signal)


LIVE_EXECUTION_PRICE_FIELDS = {
    "price",
    "limit_price",
    "protection_limit_price",
    "market_limit_price",
    "estimated_price",
    "price_source",
}


def _build_ptrade_execution_orders(plan: Dict) -> List[Dict]:
    execution_orders = []
    plan_order_type = str(plan.get("order_type") or "LIMIT").upper()
    plan_price_level = plan.get("price_level")
    plan_market_type = plan.get("market_type")

    for order in plan.get("orders") or []:
        execution_order = {
            key: value
            for key, value in dict(order).items()
            if key not in LIVE_EXECUTION_PRICE_FIELDS
        }
        order_type = str(execution_order.get("order_type") or plan_order_type or "LIMIT").upper()
        execution_order["order_type"] = "MARKET" if order_type in {"MARKET", "MKT"} else "LIMIT"
        if execution_order.get("price_level") is None:
            execution_order["price_level"] = plan_price_level if plan_price_level is not None else 1
        if execution_order["order_type"] == "MARKET" and execution_order.get("market_type") is None:
            execution_order["market_type"] = plan_market_type if plan_market_type is not None else 0
        execution_order["execution_pricing"] = "PTRATE_SNAPSHOT_AT_ORDER_TIME"
        execution_orders.append(execution_order)

    return execution_orders


async def _execute_live_trade_plan_now(
    external_account: ExternalTradingAccount,
    plan: Dict,
) -> Dict:
    if not plan.get("orders"):
        return {"orders": [], "message": "无需要执行的订单"}

    if not external_trading_hub.get_status(external_account.id).get("connected"):
        raise ExternalTradingConnectionError("外部交易账号未连接")

    execution_orders = _build_ptrade_execution_orders(plan)
    result = await external_trading_hub.place_orders(
        external_account.id,
        execution_orders,
        timeout=60.0,
    )
    result["execution_orders"] = execution_orders
    result["pricing_mode"] = "PTRATE_SNAPSHOT_AT_ORDER_TIME"
    return result


def _queue_live_trade_execution(
    db: Session,
    config: W20MomentumLiveConfig,
    external_account: ExternalTradingAccount,
    plan: Dict,
) -> W20MomentumLiveExecution:
    if not plan.get("orders"):
        raise HTTPException(status_code=400, detail="当前计划没有待提交订单")

    now = datetime.now()
    execute_after = _to_naive_china_datetime(_next_china_live_trade_time())
    existing = (
        db.query(W20MomentumLiveExecution)
        .filter(
            W20MomentumLiveExecution.config_id == config.id,
            W20MomentumLiveExecution.account_id == config.account_id,
            W20MomentumLiveExecution.status.in_(list(W20_PENDING_EXECUTION_STATUSES)),
        )
        .order_by(W20MomentumLiveExecution.approved_at.desc(), W20MomentumLiveExecution.id.desc())
        .first()
    )

    if existing:
        if existing.status == "PENDING":
            existing.external_trading_account_id = external_account.id
            existing.approved_at = now
            existing.execute_after = execute_after
            existing.approved_plan = plan
            existing.error_message = None
            existing.updated_at = now
        return existing

    queued = W20MomentumLiveExecution(
        config_id=config.id,
        account_id=config.account_id,
        external_trading_account_id=external_account.id,
        status="PENDING",
        approved_at=now,
        execute_after=execute_after,
        approved_plan=plan,
        created_at=now,
        updated_at=now,
    )
    db.add(queued)
    db.flush()
    db.add(W20MomentumLiveLog(
        config_id=config.id,
        account_id=config.account_id,
        timestamp=now,
        level="INFO",
        action="LIVE_TRADE_APPROVED",
        message="已确认实盘交易，等待 A 股交易时段自动执行",
        payload={
            "queued_execution_id": queued.id,
            "execute_after": execute_after.isoformat() if execute_after else None,
            "plan": plan,
        },
    ))
    return queued


def _execution_to_dict(execution: W20MomentumLiveExecution) -> Dict:
    return {
        "id": execution.id,
        "status": execution.status,
        "approved_at": execution.approved_at.isoformat() if execution.approved_at else None,
        "execute_after": execution.execute_after.isoformat() if execution.execute_after else None,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "executed_at": execution.executed_at.isoformat() if execution.executed_at else None,
        "attempt_count": execution.attempt_count or 0,
        "error_message": execution.error_message,
    }


def _send_live_trade_execution_failure_email(
    config: W20MomentumLiveConfig,
    execution: W20MomentumLiveExecution,
    error_message: str,
):
    body = "\n".join([
        "A股ETF W20 风险调整动量虚拟盘已确认的实盘交易自动执行失败。",
        "",
        f"策略: {config.name} (ID: {config.id})",
        f"队列ID: {execution.id}",
        f"失败时间: {datetime.now(CHINA_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"错误: {error_message}",
        "",
        f"确认页: {_get_live_trade_confirm_url(config.id)}",
    ])
    send_alert_email(f"W20 实盘自动执行失败: {config.name}", body)


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


def process_pending_w20_live_trade_executions_for_robot() -> Dict:
    now_cn = _china_now()
    if not _is_china_live_trade_window(now_cn):
        return {"processed": 0, "failed": 0, "skipped": "market_closed"}

    now_naive = now_cn.replace(tzinfo=None)
    with get_db_ctx() as db:
        execution_ids = [
            item.id
            for item in (
                db.query(W20MomentumLiveExecution)
                .filter(
                    W20MomentumLiveExecution.status == "PENDING",
                    W20MomentumLiveExecution.execute_after <= now_naive,
                )
                .order_by(W20MomentumLiveExecution.approved_at.asc(), W20MomentumLiveExecution.id.asc())
                .limit(10)
                .all()
            )
        ]

    processed = 0
    failed = 0
    deferred = 0
    for execution_id in execution_ids:
        with get_db_ctx() as db:
            execution = db.query(W20MomentumLiveExecution).filter(
                W20MomentumLiveExecution.id == execution_id,
                W20MomentumLiveExecution.status == "PENDING",
            ).first()
            if not execution:
                continue

            now = datetime.now()
            execution.status = "PROCESSING"
            execution.started_at = now
            execution.last_attempt_at = now
            execution.attempt_count = int(execution.attempt_count or 0) + 1
            execution.updated_at = now
            db.commit()

            try:
                config = db.query(W20MomentumLiveConfig).filter(
                    W20MomentumLiveConfig.id == execution.config_id,
                    W20MomentumLiveConfig.account_id == execution.account_id,
                ).first()
                if not config:
                    raise RuntimeError("W20 配置不存在")

                external_account = db.query(ExternalTradingAccount).filter(
                    ExternalTradingAccount.id == execution.external_trading_account_id,
                    ExternalTradingAccount.account_id == execution.account_id,
                ).first()
                if not external_account or not external_account.enabled:
                    raise RuntimeError("外部交易账户不存在或未启用")
                if not getattr(config, "live_trade_enabled", False):
                    raise RuntimeError("该配置已关闭实盘交易")
                if not external_trading_hub.get_status(external_account.id).get("connected"):
                    next_attempt = datetime.now() + timedelta(minutes=1)
                    execution.status = "PENDING"
                    execution.execute_after = next_attempt
                    execution.error_message = "外部交易账号未连接，等待下一次交易时段重试"
                    execution.updated_at = datetime.now()
                    deferred += 1
                    db.commit()
                    continue

                plan = asyncio.run(_prepare_live_trade_plan(
                    db,
                    config,
                    external_account,
                    sync_before=True,
                    trigger_source="queued_live_trade",
                ))
                execution_result = asyncio.run(_execute_live_trade_plan_now(external_account, plan))
                done_at = datetime.now()
                execution.status = "EXECUTED"
                execution.executed_at = done_at
                execution.execution_plan = plan
                execution.execution_result = execution_result
                execution.error_message = None
                execution.updated_at = done_at
                db.add(W20MomentumLiveLog(
                    config_id=config.id,
                    account_id=config.account_id,
                    timestamp=done_at,
                    level="INFO",
                    action="LIVE_TRADE_EXECUTE",
                    message="已自动提交实盘调仓订单",
                    payload={
                        "dry_run": False,
                        "sync_before": True,
                        "trigger_source": "queued_auto_execute",
                        "queued_execution_id": execution.id,
                        "approved_plan": execution.approved_plan,
                        "plan": plan,
                        "execution": execution_result,
                    },
                ))
                processed += 1
                db.commit()
            except Exception as exc:
                db.rollback()
                error_message = _exception_message(exc)
                with get_db_ctx() as retry_db:
                    failed_execution = retry_db.query(W20MomentumLiveExecution).filter(
                        W20MomentumLiveExecution.id == execution_id,
                    ).first()
                    if not failed_execution:
                        continue
                    failed_execution.status = "FAILED"
                    failed_execution.error_message = error_message[:1000]
                    failed_execution.updated_at = datetime.now()
                    failed_config = retry_db.query(W20MomentumLiveConfig).filter(
                        W20MomentumLiveConfig.id == failed_execution.config_id,
                        W20MomentumLiveConfig.account_id == failed_execution.account_id,
                    ).first()
                    if failed_config:
                        retry_db.add(W20MomentumLiveLog(
                            config_id=failed_config.id,
                            account_id=failed_config.account_id,
                            timestamp=datetime.now(),
                            level="ERROR",
                            action="LIVE_TRADE_EXECUTE_FAILED",
                            message=error_message[:1000],
                            payload={
                                "queued_execution_id": failed_execution.id,
                                "error": error_message,
                            },
                        ))
                        _send_live_trade_execution_failure_email(failed_config, failed_execution, error_message)
                failed += 1
                logger.exception("Queued W20 live trade execution failed")

    return {"processed": processed, "failed": failed, "deferred": deferred}


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
    _validate_external_account_selection(db, account_id, payload.external_trading_account_id)
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
    _validate_external_account_selection(db, account_id, payload.external_trading_account_id)
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
    db.query(W20MomentumLiveExecution).filter(W20MomentumLiveExecution.config_id == config.id).delete()
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


@router.post("/configs/{config_id}/live-trade")
async def execute_w20_momentum_live_trade(
    config_id: int,
    payload: W20LiveTradeRequest,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    external_account = _get_external_account_for_live_trade(db, config)

    try:
        market_open = _is_china_live_trade_window()
        if not payload.dry_run and payload.defer_until_market_open and not market_open:
            latest_plan_log = _get_latest_live_trade_plan_log(db, config)
            plan = (latest_plan_log.payload or {}).get("plan") if latest_plan_log and latest_plan_log.payload else None
            if not plan:
                raise HTTPException(status_code=400, detail="暂无可确认的实盘计划，请先生成实盘计划")
            queued_execution = _queue_live_trade_execution(db, config, external_account, plan)
            db.commit()
            return {
                "message": "已确认实盘交易，将在 A 股交易时段自动执行",
                "dry_run": False,
                "deferred": True,
                "plan": plan,
                "execution": None,
                "queued_execution": _execution_to_dict(queued_execution),
                "timestamp": datetime.now().isoformat(),
                "trigger_source": "manual_confirm",
            }

        plan = await _prepare_live_trade_plan(
            db,
            config,
            external_account,
            sync_before=payload.sync_before,
            trigger_source="live_trade",
        )
        now = datetime.now()
        action = "LIVE_TRADE_PLAN" if payload.dry_run else "LIVE_TRADE_EXECUTE"
        execution = None

        if not payload.dry_run:
            try:
                execution = await _execute_live_trade_plan_now(external_account, plan)
            except ExternalTradingConnectionError as exc:
                raise HTTPException(status_code=409, detail=str(exc))

        db.add(W20MomentumLiveLog(
            config_id=config.id,
            account_id=account_id,
            timestamp=now,
            level="INFO",
            action=action,
            message="已生成实盘调仓计划" if payload.dry_run else "已提交实盘调仓订单",
            payload={
                "dry_run": payload.dry_run,
                "sync_before": payload.sync_before,
                "trigger_source": "manual",
                "plan": plan,
                "execution": execution,
            },
        ))
        db.commit()
        return {
            "message": "实盘调仓计划已生成" if payload.dry_run else "实盘调仓订单已提交",
            "dry_run": payload.dry_run,
            "deferred": False,
            "plan": plan,
            "execution": execution,
            "timestamp": now.isoformat(),
            "trigger_source": "manual",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
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
    pending_execution = (
        db.query(W20MomentumLiveExecution)
        .filter(
            W20MomentumLiveExecution.config_id == config.id,
            W20MomentumLiveExecution.account_id == account_id,
            W20MomentumLiveExecution.status.in_(list(W20_PENDING_EXECUTION_STATUSES)),
        )
        .order_by(W20MomentumLiveExecution.approved_at.desc(), W20MomentumLiveExecution.id.desc())
        .first()
    )
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
            "queued_execution": _execution_to_dict(pending_execution) if pending_execution else None,
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
        "queued_execution": _execution_to_dict(pending_execution) if pending_execution else None,
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
