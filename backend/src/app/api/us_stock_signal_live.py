import logging
import re
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional, Tuple, Union
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
from ...core.services.market import MarketService
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from ...core.services.factor_backtest_engine import (
    FACTOR_REGISTRY,
    MIXED_WINDOW_KEY,
    NEUTRALIZATION_OPTIONS,
    SUPPORTED_WINDOWS,
    STANDARDIZATION_OPTIONS,
    default_virtual_factor_leg_payloads,
)
from ...robot.us_stock_signal_virtual import (
    CANDIDATE_ETF_OPTIONS,
    DAILY_PRICE_SOURCE,
    DEFAULT_CANDIDATE_ETFS,
    DEFAULT_MOMENTUM_WEIGHTS,
    DEFAULT_REBALANCE_FREQUENCY,
    DEFAULT_SELL_RANK_MULTIPLIER,
    SUPPORTED_REBALANCE_FREQUENCIES,
    SUPPORTED_MOMENTUM_WINDOWS,
    USStockSignalVirtualEngine,
)
from .account import valid_account

router = APIRouter(prefix="/api/us-stock-signal-live", tags=["US Stock Momentum Live"])
logger = logging.getLogger(__name__)
EASTERN_TZ = ZoneInfo("US/Eastern")
DEFAULT_AUTO_SYNC_TIME = "16:15"
DEFAULT_AUTO_TRADE_TIME = "09:31"
LIVE_QUOTE_PRICE_SOURCE = "quote_realtime"
LIVE_DEPTH_PRICE_SOURCE = "depth_orderbook"
AUTO_SYNC_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class LiveQuoteUnavailable(Exception):
    pass


def _default_virtual_legs() -> List[Dict]:
    return default_virtual_factor_leg_payloads()


class VirtualFactorLegPayload(BaseModel):
    factor: str
    window: Union[int, str] = 20
    weight: float = 1.0
    neutralization: str = "none"
    standardization: str = "rank_percentile"
    momentum_weights: Dict[str, float] = Field(default_factory=lambda: DEFAULT_MOMENTUM_WEIGHTS.copy())

    @validator("factor")
    def validate_factor(cls, value):
        if value not in FACTOR_REGISTRY:
            raise ValueError(f"不支持的因子: {value}")
        return value

    @validator("window", pre=True)
    def validate_window(cls, value):
        if isinstance(value, str) and value.strip().lower() == MIXED_WINDOW_KEY:
            return MIXED_WINDOW_KEY
        try:
            window = int(value)
        except (TypeError, ValueError):
            raise ValueError("窗口必须是数字或 mixed")
        if window not in SUPPORTED_MOMENTUM_WINDOWS:
            raise ValueError(f"窗口只支持 {', '.join(str(item) for item in SUPPORTED_MOMENTUM_WINDOWS)} 或 mixed")
        return window

    @validator("neutralization")
    def validate_neutralization(cls, value):
        if value not in NEUTRALIZATION_OPTIONS:
            raise ValueError("不支持的中性化方式")
        return value

    @validator("standardization")
    def validate_standardization(cls, value):
        if value not in STANDARDIZATION_OPTIONS:
            raise ValueError("不支持的标准化方式")
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


class USStockSignalConfigPayload(BaseModel):
    name: str = "美股多因子策略虚拟盘"
    enabled: bool = True
    candidate_etfs: List[str] = Field(default_factory=lambda: DEFAULT_CANDIDATE_ETFS.copy())
    initial_capital: float = 100_000.0
    start_date: date = date(2020, 1, 2)
    min_listing_days: int = 365
    max_positions: int = 7
    sell_rank_multiplier: float = DEFAULT_SELL_RANK_MULTIPLIER
    rebalance_frequency: str = DEFAULT_REBALANCE_FREQUENCY
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    lot_size: int = 1
    legs: List[VirtualFactorLegPayload] = Field(default_factory=lambda: [VirtualFactorLegPayload(**item) for item in _default_virtual_legs()])
    auto_sync_enabled: bool = True
    auto_sync_time: str = DEFAULT_AUTO_SYNC_TIME
    auto_trade_time: str = DEFAULT_AUTO_TRADE_TIME

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

    @validator("max_positions")
    def validate_max_positions(cls, value):
        if value < 1:
            raise ValueError("最大持仓数不能小于1")
        return value

    @validator("lot_size")
    def validate_lot_size(cls, value):
        if value < 1:
            raise ValueError("交易单位不能小于1")
        return value

    @validator("sell_rank_multiplier")
    def validate_sell_rank_multiplier(cls, value):
        if value < 1:
            raise ValueError("卖出排名倍数不能小于1")
        if value > 10:
            raise ValueError("卖出排名倍数不能大于10")
        return value

    @validator("rebalance_frequency")
    def validate_rebalance_frequency(cls, value):
        text = str(value or DEFAULT_REBALANCE_FREQUENCY).strip().lower()
        if text not in SUPPORTED_REBALANCE_FREQUENCIES:
            raise ValueError(f"调仓周期必须是: {', '.join(SUPPORTED_REBALANCE_FREQUENCIES)}")
        return text

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

    @validator("commission_pct", "slippage_pct")
    def validate_non_negative(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("auto_sync_time")
    def validate_auto_sync_time(cls, value):
        text = str(value or DEFAULT_AUTO_SYNC_TIME).strip()
        if not AUTO_SYNC_TIME_PATTERN.match(text):
            raise ValueError("自动同步时间格式应为 HH:mm")
        return text

    @validator("auto_trade_time")
    def validate_auto_trade_time(cls, value):
        text = str(value or DEFAULT_AUTO_TRADE_TIME).strip()
        if not AUTO_SYNC_TIME_PATTERN.match(text):
            raise ValueError("自动交易时间格式应为 HH:mm")
        return text

    @validator("legs")
    def validate_legs(cls, value):
        if len(value) < 1:
            raise ValueError("至少配置一个因子")
        if len(value) > 8:
            raise ValueError("最多支持8个因子")
        if sum(abs(float(item.weight)) for item in value) <= 0:
            raise ValueError("至少设置一个非0因子权重")
        return value


def _config_to_dict(config: USStockSignalVirtualConfig) -> Dict:
    rebalance_frequency = getattr(config, "rebalance_frequency", DEFAULT_REBALANCE_FREQUENCY)
    if rebalance_frequency not in SUPPORTED_REBALANCE_FREQUENCIES:
        rebalance_frequency = DEFAULT_REBALANCE_FREQUENCY
    return {
        "id": config.id,
        "account_id": config.account_id,
        "name": config.name,
        "enabled": bool(config.enabled),
        "candidate_etfs": config.candidate_etfs or DEFAULT_CANDIDATE_ETFS.copy(),
        "initial_capital": config.initial_capital,
        "start_date": config.start_date.isoformat() if config.start_date else None,
        "min_listing_days": getattr(config, "min_listing_days", 365),
        "max_positions": config.max_positions,
        "sell_rank_multiplier": getattr(config, "sell_rank_multiplier", DEFAULT_SELL_RANK_MULTIPLIER),
        "rebalance_frequency": rebalance_frequency,
        "commission_pct": config.commission_pct,
        "slippage_pct": config.slippage_pct,
        "lot_size": getattr(config, "lot_size", 1),
        "legs": getattr(config, "legs", None) or _default_virtual_legs(),
        "auto_sync_enabled": bool(config.auto_sync_enabled),
        "auto_sync_time": config.auto_sync_time or DEFAULT_AUTO_SYNC_TIME,
        "auto_trade_time": getattr(config, "auto_trade_time", None) or DEFAULT_AUTO_TRADE_TIME,
        "last_auto_sync_at": config.last_auto_sync_at,
        "last_auto_trade_at": getattr(config, "last_auto_trade_at", None),
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
        raise HTTPException(status_code=404, detail="未找到美股多因子策略虚拟盘配置")
    return config


def _apply_payload(config: USStockSignalVirtualConfig, payload: USStockSignalConfigPayload):
    payload_data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    for field, value in payload_data.items():
        setattr(config, field, value)
    config.updated_at = datetime.now()


def _is_valid_price(value) -> bool:
    try:
        number = float(value)
        return number > 0
    except (TypeError, ValueError):
        return False


def _get_longport_account_id(db: ORMSession, account_id: str) -> str:
    account = db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).first()
    if account and account.lp_account_id:
        return account.lp_account_id
    return "LBPT10001248"


def _get_quote_service(db: ORMSession, account_id: str) -> QuoteService:
    return QuoteService(LongPortService.get_instance(_get_longport_account_id(db, account_id)))


def _quote_eastern_datetime(value) -> Optional[datetime]:
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
        return timestamp.astimezone(EASTERN_TZ)
    return timestamp.replace(tzinfo=EASTERN_TZ)


def _load_existing_realtime_execution_overrides(
    db: ORMSession,
    config: USStockSignalVirtualConfig,
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    price_overrides: Dict[str, Dict[str, float]] = {}
    source_overrides: Dict[str, Dict[str, str]] = {}
    timestamp_overrides: Dict[str, Dict[str, str]] = {}
    live_price_sources = [LIVE_QUOTE_PRICE_SOURCE, LIVE_DEPTH_PRICE_SOURCE]
    rows = (
        db.query(USStockSignalVirtualTrade)
        .filter(
            USStockSignalVirtualTrade.config_id == config.id,
            USStockSignalVirtualTrade.price_source.in_(live_price_sources),
        )
        .all()
    )
    slippage_rate = max(0.0, float(getattr(config, "slippage_pct", 0) or 0)) / 100
    for row in rows:
        if not row.date or not row.symbol:
            continue
        execution_price = getattr(row, "execution_price", None)
        if not _is_valid_price(execution_price) and _is_valid_price(row.price):
            if row.action == "BUY":
                execution_price = float(row.price) / (1 + slippage_rate)
            elif row.action == "SELL" and slippage_rate < 1:
                execution_price = float(row.price) / (1 - slippage_rate)
        if not _is_valid_price(execution_price):
            continue
        date_key = row.date.isoformat()
        price_overrides.setdefault(date_key, {})[row.symbol] = float(execution_price)
        source_overrides.setdefault(date_key, {})[row.symbol] = row.price_source or LIVE_QUOTE_PRICE_SOURCE
        if getattr(row, "quote_timestamp", None):
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
    if not symbol or not _is_valid_price(price):
        return
    if not overwrite and symbol in price_overrides.get(date_key, {}):
        return
    price_overrides.setdefault(date_key, {})[symbol] = float(price)
    source_overrides.setdefault(date_key, {})[symbol] = source
    if quote_timestamp:
        timestamp_overrides.setdefault(date_key, {})[symbol] = quote_timestamp.isoformat()


def _valid_depth_levels(levels: List[Dict]) -> List[Dict]:
    result = []
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        price = level.get("price")
        volume = level.get("volume")
        if not _is_valid_price(price):
            continue
        try:
            volume_value = int(float(volume or 0))
        except (TypeError, ValueError):
            volume_value = 0
        if volume_value <= 0:
            continue
        result.append({
            "position": level.get("position"),
            "price": float(price),
            "volume": volume_value,
            "order_num": level.get("order_num"),
        })
    return result


def _normalize_depth_payload(depth: Dict, quote_timestamp: datetime) -> Optional[Dict]:
    if not depth:
        return None
    ask = _valid_depth_levels(depth.get("ask") or [])
    bid = _valid_depth_levels(depth.get("bid") or [])
    if not ask and not bid:
        return None
    return {
        "symbol": depth.get("symbol"),
        "ask": ask,
        "bid": bid,
        "price_source": LIVE_DEPTH_PRICE_SOURCE,
        "timestamp": quote_timestamp.isoformat(),
    }


def _latest_rank_signal_symbols(db: ORMSession, config: USStockSignalVirtualConfig) -> List[str]:
    latest_date = (
        db.query(USStockSignalVirtualEvent.date)
        .filter(
            USStockSignalVirtualEvent.config_id == config.id,
            USStockSignalVirtualEvent.direction == "RANK",
        )
        .order_by(USStockSignalVirtualEvent.date.desc())
        .first()
    )
    symbols: List[str] = []
    if latest_date and latest_date[0]:
        rows = (
            db.query(USStockSignalVirtualEvent.symbol, USStockSignalVirtualEvent.payload)
            .filter(
                USStockSignalVirtualEvent.config_id == config.id,
                USStockSignalVirtualEvent.date == latest_date[0],
                USStockSignalVirtualEvent.direction == "RANK",
            )
            .all()
        )
        for symbol, payload in rows:
            if symbol:
                symbols.append(symbol)
            payload = payload or {}
            for key in ("selected_symbols", "sell_rank_symbols", "event_symbols"):
                symbols.extend([item for item in (payload.get(key) or []) if item])
    holding_symbols = [
        item[0]
        for item in db.query(USStockSignalVirtualHolding.symbol)
        .filter(USStockSignalVirtualHolding.config_id == config.id)
        .all()
        if item[0]
    ]
    symbols.extend(holding_symbols)
    return list(dict.fromkeys(symbols))


def _time_from_hhmm(value: str, default_value: str) -> dtime:
    text = value if AUTO_SYNC_TIME_PATTERN.match(str(value or "")) else default_value
    hour, minute = [int(item) for item in text.split(":")]
    return dtime(hour=hour, minute=minute)


def _fetch_live_execution_overrides(
    db: ORMSession,
    config: USStockSignalVirtualConfig,
    now_et: datetime,
    price_overrides: Dict[str, Dict[str, float]],
    source_overrides: Dict[str, Dict[str, str]],
    timestamp_overrides: Dict[str, Dict[str, str]],
    depth_overrides: Dict[str, Dict[str, Dict]],
) -> Dict:
    symbols = _latest_rank_signal_symbols(db, config)
    if not symbols:
        return {"symbols": [], "quotes": [], "reason": "没有可执行的最新排名信号"}

    quote_service = _get_quote_service(db, config.account_id)
    trade_date_key = now_et.date().isoformat()
    depth_items = []
    min_quote_time = _time_from_hhmm(getattr(config, "auto_trade_time", DEFAULT_AUTO_TRADE_TIME), DEFAULT_AUTO_TRADE_TIME)
    for symbol in symbols:
        depth = quote_service.get_depth(symbol)
        normalized_depth = _normalize_depth_payload(depth, now_et)
        if not normalized_depth:
            continue
        depth_overrides.setdefault(trade_date_key, {})[symbol] = normalized_depth
        depth_items.append({
            "symbol": symbol,
            "bid1": normalized_depth["bid"][0]["price"] if normalized_depth["bid"] else None,
            "ask1": normalized_depth["ask"][0]["price"] if normalized_depth["ask"] else None,
            "timestamp": now_et.isoformat(),
        })

    quotes = quote_service.get_quote_batch(symbols) or []
    accepted = []

    for quote in quotes:
        symbol = quote.get("symbol")
        price = quote.get("price")
        quote_dt = _quote_eastern_datetime(quote.get("timestamp"))
        if not symbol or not _is_valid_price(price) or not quote_dt:
            continue
        if quote_dt.date() != now_et.date() or quote_dt.time() < min_quote_time:
            continue
        _merge_execution_override(
            price_overrides,
            source_overrides,
            timestamp_overrides,
            trade_date_key,
            symbol,
            float(price),
            LIVE_QUOTE_PRICE_SOURCE,
            quote_dt,
            overwrite=False,
        )
        accepted.append({
            "symbol": symbol,
            "price": float(price),
            "timestamp": quote_dt.isoformat(),
        })
    return {"symbols": symbols, "depths": depth_items, "quotes": accepted, "trade_date": trade_date_key}


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
            execution_price=item.get("execution_price"),
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
            quote_timestamp=datetime.fromisoformat(item["quote_timestamp"]) if item.get("quote_timestamp") else None,
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
        message="美股多因子策略虚拟盘已同步到最新状态",
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
    use_live_quotes: bool = False,
    now_et: Optional[datetime] = None,
) -> Dict:
    price_overrides, source_overrides, timestamp_overrides = _load_existing_realtime_execution_overrides(db, config)
    depth_overrides: Dict[str, Dict[str, Dict]] = {}
    live_quote_payload = None
    live_execution_date = None
    if use_live_quotes:
        now_et = now_et or MarketService.get_eastern_now()
        live_quote_payload = _fetch_live_execution_overrides(
            db,
            config,
            now_et,
            price_overrides,
            source_overrides,
            timestamp_overrides,
            depth_overrides,
        )
        if not live_quote_payload.get("quotes") and not live_quote_payload.get("depths"):
            raise LiveQuoteUnavailable(live_quote_payload.get("reason") or "没有拿到可用实时成交报价或盘口")
        live_execution_date = now_et.date()
    return _sync_config_with_execution_context(
        db,
        config,
        trigger_source,
        price_overrides,
        source_overrides,
        timestamp_overrides,
        depth_overrides,
        live_execution_date,
        live_quote_payload,
    )


def _sync_config_with_execution_context(
    db: ORMSession,
    config: USStockSignalVirtualConfig,
    trigger_source: str,
    price_overrides: Dict[str, Dict[str, float]],
    source_overrides: Dict[str, Dict[str, str]],
    timestamp_overrides: Dict[str, Dict[str, str]],
    depth_overrides: Optional[Dict[str, Dict[str, Dict]]] = None,
    live_execution_date: Optional[date] = None,
    live_quote_payload: Optional[Dict] = None,
) -> Dict:
    result = USStockSignalVirtualEngine(
        db,
        config,
        execution_price_overrides=price_overrides,
        execution_price_source_overrides=source_overrides,
        execution_quote_timestamp_overrides=timestamp_overrides,
        execution_depth_overrides=depth_overrides or {},
        live_execution_date=live_execution_date,
    ).run()
    if live_quote_payload is not None:
        result.setdefault("meta", {}).setdefault("live_execution", live_quote_payload)
        result.setdefault("metadata", {}).setdefault("live_execution", live_quote_payload)
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


@router.get("/factor-options")
def get_factor_options():
    return {
        "factors": [definition.to_option() for definition in FACTOR_REGISTRY.values()],
        "windows": SUPPORTED_WINDOWS,
        "neutralization_options": [
            {"key": key, **value}
            for key, value in NEUTRALIZATION_OPTIONS.items()
        ],
        "standardization_options": [
            {"key": key, **value}
            for key, value in STANDARDIZATION_OPTIONS.items()
        ],
    }


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
    return {"message": "已删除美股多因子策略虚拟盘配置"}


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


def _auto_trade_already_attempted(config: USStockSignalVirtualConfig, now_et: datetime) -> bool:
    last_auto_trade_at = getattr(config, "last_auto_trade_at", None)
    if not last_auto_trade_at:
        return False
    if last_auto_trade_at.tzinfo:
        return last_auto_trade_at.astimezone(EASTERN_TZ).date() == now_et.date()
    return last_auto_trade_at.date() == now_et.date()


def _is_auto_trade_due(config: USStockSignalVirtualConfig, now_et: datetime) -> bool:
    auto_trade_time = getattr(config, "auto_trade_time", None) or DEFAULT_AUTO_TRADE_TIME
    if not AUTO_SYNC_TIME_PATTERN.match(auto_trade_time):
        auto_trade_time = DEFAULT_AUTO_TRADE_TIME
    return now_et.strftime("%H:%M") >= auto_trade_time


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


def execute_due_us_stock_signal_configs_for_auto_trade(now_et: Optional[datetime] = None) -> Dict:
    now_et = now_et or MarketService.get_eastern_now()
    result = {
        "traded": [],
        "errors": [],
        "skipped": [],
        "current_time": now_et.strftime("%H:%M"),
        "timezone": "US/Eastern",
    }
    if now_et.weekday() >= 5 or MarketService.is_us_market_holiday(now_et.date()):
        result["skipped"].append({"reason": "美股休市日"})
        return result
    if not MarketService.is_us_market_open(include_extended=False):
        result["skipped"].append({"reason": "美股未处于常规交易时段"})
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
            if not _is_auto_trade_due(config, now_et):
                result["skipped"].append({"id": config_id, "account_id": account_id, "name": config_name, "reason": "未到自动交易时间"})
                continue
            if _auto_trade_already_attempted(config, now_et):
                result["skipped"].append({"id": config_id, "account_id": account_id, "name": config_name, "reason": "今日已自动交易"})
                continue

            attempt_at = now_et.replace(tzinfo=None)
            _mark_sync_running(db, config, message=f"自动交易执行中（美东 {now_et.strftime('%H:%M')}）")
            db.commit()
            try:
                trade_result = _sync_config_now(db, config, trigger_source="module_auto_trade", use_live_quotes=True, now_et=now_et)
                config.last_auto_trade_at = attempt_at
                db.commit()
                live_execution = (trade_result.get("meta") or {}).get("live_execution") or {}
                result["traded"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "quote_count": len(live_execution.get("quotes") or []),
                    "depth_count": len(live_execution.get("depths") or []),
                    "summary": trade_result.get("metrics"),
                })
            except LiveQuoteUnavailable as exc:
                db.rollback()
                skipped_config = db.query(USStockSignalVirtualConfig).filter(
                    USStockSignalVirtualConfig.id == config_id,
                    USStockSignalVirtualConfig.account_id == account_id,
                ).first()
                if skipped_config:
                    now = datetime.now()
                    skipped_config.last_sync_at = now
                    skipped_config.last_sync_status = "skipped"
                    skipped_config.last_sync_message = str(exc)[:500]
                    skipped_config.updated_at = now
                    db.commit()
                result["skipped"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "reason": str(exc),
                })
            except Exception as exc:
                db.rollback()
                failed_config = db.query(USStockSignalVirtualConfig).filter(
                    USStockSignalVirtualConfig.id == config_id,
                    USStockSignalVirtualConfig.account_id == account_id,
                ).first()
                if failed_config:
                    _mark_sync_failed(db, config_id, account_id, exc)
                    db.commit()
                logger.exception("US stock signal module auto trade failed")
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
            USStockSignalVirtualTrade.execution_price.label("execution_price"),
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
            USStockSignalVirtualTrade.quote_timestamp.label("quote_timestamp"),
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
        .order_by(USStockSignalVirtualEvent.date.desc(), USStockSignalVirtualEvent.id.asc())
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
            "yearly_stats": sync_payload.get("yearly_stats") or [],
            "trade_count": db.query(USStockSignalVirtualTrade).filter(USStockSignalVirtualTrade.config_id == config.id).count(),
            "signal_count": db.query(USStockSignalVirtualEvent).filter(USStockSignalVirtualEvent.config_id == config.id).count(),
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
                "execution_price": item.execution_price,
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
                "quote_timestamp": item.quote_timestamp.isoformat() if item.quote_timestamp else None,
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
