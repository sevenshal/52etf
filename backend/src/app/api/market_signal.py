import logging
import math
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy import func
from sqlalchemy.orm import Session as ORMSession

from ...core.database import (
    ETFHolding,
    LongPortAccount,
    MarketSignalEvent,
    MarketSignalStrategyConfig,
    MarketSignalVirtualEquity,
    MarketSignalVirtualHolding,
    MarketSignalVirtualLog,
    MarketSignalVirtualTrade,
    StockEVC,
    get_db,
    get_db_ctx,
    Session as DbSession,
)
from ...core.services.longport import LongPortService
from ...core.services.market import MarketService
from ...core.services.quote import QuoteService
from ...robot.market_signal import (
    MARKET_SIGNAL_MAX_LOOKBACK,
    MARKET_SIGNAL_STRATEGIES,
    MARKET_SIGNAL_STRATEGY_MAP,
    MarketSignalStrategyEvaluator,
)
from .account import valid_account

router = APIRouter(tags=["market-signal"])
logger = logging.getLogger(__name__)

EASTERN_TZ = ZoneInfo("US/Eastern")
DEFAULT_SIGNAL_SYMBOLS = [
    "AAPL.US",
    "MSFT.US",
    "NVDA.US",
    "AMZN.US",
    "META.US",
    "GOOGL.US",
    "AVGO.US",
    "TSLA.US",
    "LLY.US",
    "JPM.US",
]
DAILY_PRICE_SOURCE = "daily_close"
REALTIME_PRICE_SOURCE = "realtime_quote"
DEFAULT_SIGNAL_SYMBOL_LIMIT = 700
DEFAULT_AUTO_SYNC_TIME = "15:58"
AUTO_SYNC_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
BACKTEST_JOBS: Dict[str, Dict] = {}
BACKTEST_JOBS_LOCK = threading.Lock()


def _round_or_none(value, digits: int = 2):
    if value is None:
        return None
    try:
        if not np.isfinite(float(value)):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _parse_date(value: Optional[str], default: Optional[date] = None) -> date:
    if not value:
        if default is None:
            raise ValueError("日期不能为空")
        return default
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def _normalize_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return raw
    if raw.startswith("US.") and len(raw) > 3:
        return f"{raw[3:]}.US"
    if "." not in raw and raw.isalpha():
        return f"{raw}.US"
    return raw


def _parse_symbol_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("，", ",").replace(";", ",").replace("；", ",").replace("\n", ",").split(",")
    else:
        items = list(value)
    symbols = [_normalize_symbol(item) for item in items]
    return list(dict.fromkeys([item for item in symbols if item]))


def _strategy_param_defs(strategy_id: str) -> List[Dict]:
    return MARKET_SIGNAL_STRATEGY_MAP.get(strategy_id, {}).get("params") or []


def _strategy_param_defaults(strategy_id: str) -> Dict[str, float]:
    return {
        item["key"]: item.get("default")
        for item in _strategy_param_defs(strategy_id)
        if item.get("key")
    }


def _normalize_strategy_params(strategy_id: str, value) -> Dict[str, float]:
    params = _strategy_param_defaults(strategy_id)
    if not isinstance(value, dict):
        return params

    defs_by_key = {
        item["key"]: item
        for item in _strategy_param_defs(strategy_id)
        if item.get("key")
    }
    for key, raw_value in value.items():
        if key not in defs_by_key or raw_value in (None, ""):
            continue
        definition = defs_by_key[key]
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            continue
        minimum = definition.get("min")
        maximum = definition.get("max")
        if minimum is not None:
            numeric = max(float(minimum), numeric)
        if maximum is not None:
            numeric = min(float(maximum), numeric)
        if int(definition.get("precision", 2)) == 0:
            params[key] = int(round(numeric))
        else:
            params[key] = numeric
    return params


def _normalize_auto_sync_time(value: Optional[str]) -> str:
    text = str(value or DEFAULT_AUTO_SYNC_TIME).strip()
    if not AUTO_SYNC_TIME_PATTERN.match(text):
        return DEFAULT_AUTO_SYNC_TIME
    return text


def _kline_date(kline: Dict) -> date:
    timestamp = kline["timestamp"]
    return timestamp.date() if hasattr(timestamp, "date") else timestamp


def _quote_eastern_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            timestamp = datetime.fromisoformat(text)
        except ValueError:
            return None
    if timestamp.tzinfo:
        return timestamp.astimezone(EASTERN_TZ)
    return timestamp.replace(tzinfo=EASTERN_TZ)


def _get_longport_account_id(account_id: str) -> str:
    db = DbSession()
    try:
        account = db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).first()
        if account and account.lp_account_id:
            return account.lp_account_id
    finally:
        DbSession.remove()
    return "LBPT10001248"


def _get_quote_service(account_id: str) -> QuoteService:
    return QuoteService(LongPortService.get_instance(_get_longport_account_id(account_id)))


def _get_stock_evc_default_symbols(db: ORMSession, limit: int = 50) -> List[str]:
    latest_date = db.query(func.max(StockEVC.date)).scalar()
    if not latest_date:
        return []
    rows = (
        db.query(StockEVC.symbol)
        .filter(StockEVC.date == latest_date)
        .order_by(StockEVC.symbol.asc())
        .limit(limit)
        .all()
    )
    return [row[0] for row in rows if row[0]]


def _get_default_symbols(db: ORMSession, limit: int = 50) -> List[str]:
    symbols = _get_spy_qqq_union_symbols(db)
    if symbols:
        return symbols[:limit]

    symbols = _get_stock_evc_default_symbols(db, limit)
    if symbols:
        return symbols
    return DEFAULT_SIGNAL_SYMBOLS[:limit]


def _get_latest_etf_equity_symbols(db: ORMSession, etf_symbol: str) -> Tuple[Optional[date], List[str]]:
    latest_date = (
        db.query(func.max(ETFHolding.date))
        .filter(ETFHolding.etf_symbol == etf_symbol)
        .scalar()
    )
    if not latest_date:
        return None, []

    rows = (
        db.query(ETFHolding.symbol, ETFHolding.weight)
        .filter(
            ETFHolding.etf_symbol == etf_symbol,
            ETFHolding.date == latest_date,
            ETFHolding.asset_class == "Equity",
        )
        .order_by(ETFHolding.weight.desc())
        .all()
    )
    symbols = [_normalize_symbol(row[0]) for row in rows if row[0]]
    return latest_date, list(dict.fromkeys([item for item in symbols if item and item.endswith(".US")]))


def _get_spy_qqq_intersection_symbols(db: ORMSession) -> List[str]:
    spy_date, spy_symbols = _get_latest_etf_equity_symbols(db, "SPY.US")
    qqq_date, qqq_symbols = _get_latest_etf_equity_symbols(db, "QQQ.US")
    if not spy_date or not qqq_date:
        return []

    spy_set = set(spy_symbols)
    return [symbol for symbol in qqq_symbols if symbol in spy_set]


def _get_spy_qqq_union_symbols(db: ORMSession) -> List[str]:
    spy_date, spy_symbols = _get_latest_etf_equity_symbols(db, "SPY.US")
    qqq_date, qqq_symbols = _get_latest_etf_equity_symbols(db, "QQQ.US")
    if not spy_date and not qqq_date:
        return []

    return list(dict.fromkeys(spy_symbols + qqq_symbols))


def _config_to_dict(config: MarketSignalStrategyConfig) -> Dict:
    strategy = MARKET_SIGNAL_STRATEGY_MAP.get(config.strategy_id, {})
    return {
        "id": config.id,
        "account_id": config.account_id,
        "strategy_id": config.strategy_id,
        "strategy_name": strategy.get("name", config.strategy_id),
        "strategy_summary": strategy.get("summary"),
        "name": config.name,
        "enabled": bool(config.enabled),
        "symbols": config.symbols or [],
        "initial_capital": config.initial_capital,
        "start_date": config.start_date.isoformat() if config.start_date else None,
        "holding_days": config.holding_days,
        "position_pct": config.position_pct,
        "max_positions": config.max_positions,
        "min_cash_pct": config.min_cash_pct,
        "commission_pct": config.commission_pct,
        "slippage_pct": config.slippage_pct,
        "lot_size": config.lot_size,
        "min_market_cap": config.min_market_cap,
        "strategy_params": _normalize_strategy_params(config.strategy_id, config.strategy_params),
        "auto_sync_enabled": bool(getattr(config, "auto_sync_enabled", True)),
        "auto_sync_time": _normalize_auto_sync_time(getattr(config, "auto_sync_time", DEFAULT_AUTO_SYNC_TIME)),
        "last_auto_sync_at": config.last_auto_sync_at,
        "last_sync_at": config.last_sync_at,
        "last_sync_status": config.last_sync_status,
        "last_sync_message": config.last_sync_message,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _get_config_or_404(db: ORMSession, account_id: str, config_id: int) -> MarketSignalStrategyConfig:
    config = db.query(MarketSignalStrategyConfig).filter(
        MarketSignalStrategyConfig.id == config_id,
        MarketSignalStrategyConfig.account_id == account_id,
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="未找到市场信号虚拟盘配置")
    return config


def _same_symbol_pool(left, right) -> bool:
    return _parse_symbol_list(left) == _parse_symbol_list(right)


def _ensure_default_configs(db: ORMSession, account_id: str) -> None:
    symbols = _get_default_symbols(db, limit=DEFAULT_SIGNAL_SYMBOL_LIMIT)
    legacy_default_pools = [
        DEFAULT_SIGNAL_SYMBOLS,
        _get_stock_evc_default_symbols(db, limit=120),
        _get_spy_qqq_intersection_symbols(db),
    ]
    existing = {
        row[0]
        for row in db.query(MarketSignalStrategyConfig.strategy_id)
        .filter(MarketSignalStrategyConfig.account_id == account_id)
        .all()
    }
    existing_configs = (
        db.query(MarketSignalStrategyConfig)
        .filter(MarketSignalStrategyConfig.account_id == account_id)
        .all()
    )
    touched = False
    for config in existing_configs:
        should_migrate_symbols = (
            not config.symbols
            or any(pool and _same_symbol_pool(config.symbols, pool) for pool in legacy_default_pools)
        )
        if should_migrate_symbols and not _same_symbol_pool(config.symbols, symbols):
            config.symbols = symbols
            config.updated_at = datetime.now()
            touched = True
        normalized_params = _normalize_strategy_params(config.strategy_id, config.strategy_params)
        if config.strategy_params != normalized_params:
            config.strategy_params = normalized_params
            config.updated_at = datetime.now()
            touched = True
        normalized_auto_sync_time = _normalize_auto_sync_time(config.auto_sync_time)
        if config.auto_sync_time != normalized_auto_sync_time:
            config.auto_sync_time = normalized_auto_sync_time
            config.updated_at = datetime.now()
            touched = True
        if config.auto_sync_enabled is None:
            config.auto_sync_enabled = True
            config.updated_at = datetime.now()
            touched = True
    if len(existing) >= len(MARKET_SIGNAL_STRATEGIES):
        if touched:
            db.commit()
        return

    now = datetime.now()
    for strategy in MARKET_SIGNAL_STRATEGIES:
        if strategy["id"] in existing:
            continue
        db.add(MarketSignalStrategyConfig(
            account_id=account_id,
            strategy_id=strategy["id"],
            name=f"{strategy['name']}虚拟盘",
            enabled=True,
            symbols=symbols,
            initial_capital=100_000.0,
            start_date=date(2023, 1, 1),
            holding_days=20,
            position_pct=10.0,
            max_positions=10,
            min_cash_pct=0.0,
            commission_pct=0.03,
            slippage_pct=0.02,
            lot_size=1,
            min_market_cap=20_000_000_000.0,
            strategy_params=_strategy_param_defaults(strategy["id"]),
            auto_sync_enabled=True,
            auto_sync_time=DEFAULT_AUTO_SYNC_TIME,
            created_at=now,
            updated_at=now,
        ))
    db.commit()


class MarketSignalConfigPayload(BaseModel):
    strategy_id: str = "v1"
    name: Optional[str] = None
    enabled: bool = True
    symbols: List[str] = Field(default_factory=lambda: DEFAULT_SIGNAL_SYMBOLS.copy())
    initial_capital: float = 100_000.0
    start_date: date = date(2023, 1, 1)
    holding_days: int = 20
    position_pct: float = 10.0
    max_positions: int = 10
    min_cash_pct: float = 0.0
    commission_pct: float = 0.03
    slippage_pct: float = 0.02
    lot_size: int = 1
    min_market_cap: float = 20_000_000_000.0
    strategy_params: Dict = Field(default_factory=dict)
    auto_sync_enabled: bool = True
    auto_sync_time: str = DEFAULT_AUTO_SYNC_TIME

    @validator("strategy_id")
    def validate_strategy_id(cls, value):
        if value not in MARKET_SIGNAL_STRATEGY_MAP:
            raise ValueError("不支持的策略")
        return value

    @validator("name", always=True)
    def validate_name(cls, value, values):
        text = (value or "").strip()
        if text:
            return text
        strategy = MARKET_SIGNAL_STRATEGY_MAP.get(values.get("strategy_id") or "v1", {})
        return f"{strategy.get('name', '市场信号策略')}虚拟盘"

    @validator("symbols", pre=True)
    def validate_symbols(cls, value):
        parsed = _parse_symbol_list(value)
        if not parsed:
            raise ValueError("至少需要一个标的")
        return parsed

    @validator("initial_capital", "position_pct", "commission_pct", "slippage_pct", "min_market_cap")
    def validate_non_negative(cls, value):
        if value < 0:
            raise ValueError("参数不能为负数")
        return value

    @validator("holding_days", "max_positions", "lot_size")
    def validate_positive_int(cls, value):
        if value < 1:
            raise ValueError("参数必须大于 0")
        return value

    @validator("position_pct", "min_cash_pct")
    def validate_percent(cls, value):
        if value > 100:
            raise ValueError("百分比参数不能超过 100")
        return value

    @validator("strategy_params", pre=True, always=True)
    def validate_strategy_params(cls, value):
        return value if isinstance(value, dict) else {}

    @validator("auto_sync_time")
    def validate_auto_sync_time(cls, value):
        text = str(value or "").strip()
        if not AUTO_SYNC_TIME_PATTERN.match(text):
            raise ValueError("自动同步时间必须为 HH:MM，美东时间")
        return text


class MarketSignalBacktestRequest(BaseModel):
    strategy_id: str = "v1"
    symbols: Optional[List[str]] = None
    start_date: str = "2023-01-01"
    end_date: Optional[str] = None
    holding_days: int = Field(20, ge=1, le=120)
    position_pct: float = Field(10.0, ge=0.1, le=100)
    max_positions: int = Field(10, ge=1, le=100)
    initial_capital: float = Field(100_000.0, gt=0)
    max_symbols: int = Field(DEFAULT_SIGNAL_SYMBOL_LIMIT, ge=1, le=DEFAULT_SIGNAL_SYMBOL_LIMIT)
    strategy_params: Dict = Field(default_factory=dict)

    @validator("strategy_id")
    def validate_strategy_id(cls, value):
        if value not in MARKET_SIGNAL_STRATEGY_MAP:
            raise ValueError("不支持的策略")
        return value

    @validator("symbols", pre=True)
    def parse_symbols(cls, value):
        parsed = _parse_symbol_list(value)
        return parsed or None

    @validator("strategy_params", pre=True, always=True)
    def parse_strategy_params(cls, value):
        return value if isinstance(value, dict) else {}


class MarketSignalBacktestJobStatus(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    message: Optional[str] = None
    result: Optional[Dict] = None
    error: Optional[str] = None


def _apply_payload(config: MarketSignalStrategyConfig, payload: MarketSignalConfigPayload):
    values = payload.dict()
    values["strategy_params"] = _normalize_strategy_params(values.get("strategy_id"), values.get("strategy_params"))
    for field, value in values.items():
        setattr(config, field, value)
    config.updated_at = datetime.now()


def _portfolio_value(cash: float, positions: Dict[str, Dict], price_map: Dict[str, float]) -> float:
    return float(cash + sum(position["shares"] * price_map.get(symbol, position.get("last_price") or 0.0) for symbol, position in positions.items()))


def _floor_lot(quantity: float, lot_size: int) -> int:
    lot = max(1, int(lot_size or 1))
    return int(quantity // lot) * lot


def _build_trade(
    config: MarketSignalStrategyConfig,
    trade_date: date,
    action: str,
    symbol: str,
    price: float,
    quantity: int,
    commission: float,
    reason: str,
    reason_detail: str,
    cash_after: float,
    portfolio_value_after: float,
    price_source: str,
    quote_timestamp: Optional[datetime],
    signal_date: Optional[date] = None,
    profit: Optional[float] = None,
    profit_pct: Optional[float] = None,
) -> Dict:
    amount = float(price * quantity)
    symbol_market_value_after = 0.0 if action == "SELL" else amount
    symbol_weight_pct_after = (
        symbol_market_value_after / portfolio_value_after * 100
        if portfolio_value_after > 0
        else 0.0
    )
    return {
        "config_id": config.id,
        "account_id": config.account_id,
        "date": trade_date.isoformat(),
        "signal_date": signal_date.isoformat() if signal_date else None,
        "strategy_id": config.strategy_id,
        "action": action,
        "symbol": symbol,
        "price": _round_or_none(price, 4),
        "quantity": quantity,
        "amount": _round_or_none(amount, 2),
        "commission": _round_or_none(commission, 2),
        "profit": _round_or_none(profit, 2),
        "profit_pct": _round_or_none(profit_pct, 2),
        "reason": reason,
        "reason_detail": reason_detail,
        "cash_after": _round_or_none(cash_after, 2),
        "portfolio_value_after": _round_or_none(portfolio_value_after, 2),
        "symbol_market_value_after": _round_or_none(symbol_market_value_after, 2),
        "symbol_weight_pct_after": _round_or_none(symbol_weight_pct_after, 2),
        "price_source": price_source,
        "quote_timestamp": quote_timestamp.isoformat() if quote_timestamp else None,
    }


def _append_live_quote_row(klines: List[Dict], quote: Dict) -> Tuple[List[Dict], Optional[date]]:
    quote_dt = _quote_eastern_datetime(quote.get("timestamp"))
    price = float(quote.get("price") or 0)
    if not quote_dt or price <= 0:
        return klines, None
    live_date = quote_dt.date()
    filtered = [item for item in klines if _kline_date(item) != live_date]
    open_price = float(quote.get("open") or price)
    high_price = max(float(quote.get("high") or price), price)
    low_price = min(float(quote.get("low") or price), price)
    filtered.append({
        "timestamp": datetime.combine(live_date, datetime.min.time()),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": price,
        "volume": float(quote.get("volume") or 0),
        "turnover": float(quote.get("turnover") or 0),
        "price_source": REALTIME_PRICE_SOURCE,
        "quote_timestamp": quote_dt,
    })
    return sorted(filtered, key=_kline_date), live_date


def _filter_symbols_by_market_cap(quote_service: QuoteService, symbols: List[str], min_market_cap: float) -> List[str]:
    if min_market_cap <= 0:
        return symbols
    try:
        static_infos = quote_service.get_static_info(symbols)
        shares_map = {}
        for info in static_infos:
            sym = info.get("symbol") or info.get("code")
            shares = info.get("total_shares")
            if sym and isinstance(shares, (int, float)) and shares > 0:
                shares_map[sym] = float(shares)
        quotes = quote_service.get_quote_batch(symbols)
        price_map = {item.get("symbol") or item.get("code"): item.get("price") for item in quotes}
        filtered = []
        for symbol in symbols:
            price = price_map.get(symbol)
            shares = shares_map.get(symbol)
            market_cap = price * shares if isinstance(price, (int, float)) and isinstance(shares, (int, float)) else 0
            if market_cap >= min_market_cap:
                filtered.append(symbol)
        return filtered or symbols
    except Exception:
        logger.exception("Market cap filter failed, falling back to unfiltered symbols")
        return symbols


def _run_virtual_engine(
    config: MarketSignalStrategyConfig,
    quote_service: QuoteService,
    end_dt: Optional[date] = None,
    include_live_quote: bool = False,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict:
    strategy = MARKET_SIGNAL_STRATEGY_MAP[config.strategy_id]
    start_dt = config.start_date
    end_dt = end_dt or date.today()
    fetch_start = start_dt - timedelta(days=430)
    symbols = _filter_symbols_by_market_cap(quote_service, config.symbols or [], float(config.min_market_cap or 0))
    if not symbols:
        raise ValueError("没有可用于同步的标的")

    def report(progress: int, message: str):
        if progress_callback:
            progress_callback(int(max(0, min(100, progress))), message)

    report(1, f"准备回测 {len(symbols)} 个标的")
    klines_by_symbol: Dict[str, List[Dict]] = {}
    errors = []
    for index, symbol in enumerate(symbols, start=1):
        try:
            report(2 + int(33 * (index - 1) / max(1, len(symbols))), f"获取K线 {index}/{len(symbols)}: {symbol}")
            klines = quote_service.get_klines(symbol, start_date=fetch_start, end_date=end_dt, period="d")
            if klines:
                klines_by_symbol[symbol] = klines
            else:
                errors.append({"symbol": symbol, "message": "没有可用K线"})
        except Exception as exc:
            errors.append({"symbol": symbol, "message": str(exc)})
    report(35, f"K线获取完成，可用标的 {len(klines_by_symbol)} 个")

    live_date = None
    live_quote_timestamps: Dict[Tuple[str, date], datetime] = {}
    if include_live_quote and klines_by_symbol:
        report(36, "获取实时行情")
        quotes = quote_service.get_quote_batch(list(klines_by_symbol.keys())) or []
        quote_map = {item.get("symbol"): item for item in quotes}
        for symbol, quote in quote_map.items():
            if symbol not in klines_by_symbol:
                continue
            next_klines, quote_date = _append_live_quote_row(klines_by_symbol[symbol], quote)
            if quote_date:
                klines_by_symbol[symbol] = next_klines
                live_date = max(live_date or quote_date, quote_date)
                quote_dt = _quote_eastern_datetime(quote.get("timestamp"))
                if quote_dt:
                    live_quote_timestamps[(symbol, quote_date)] = quote_dt

    date_index_by_symbol = {
        symbol: {_kline_date(item): idx for idx, item in enumerate(klines)}
        for symbol, klines in klines_by_symbol.items()
    }
    row_by_symbol_date = {
        symbol: {_kline_date(item): item for item in klines}
        for symbol, klines in klines_by_symbol.items()
    }
    dates = sorted({
        item_date
        for rows in row_by_symbol_date.values()
        for item_date in rows
        if start_dt <= item_date <= end_dt or (live_date and item_date == live_date)
    })
    if not dates:
        raise ValueError("回测区间内没有可用K线")

    strategy_params = _normalize_strategy_params(config.strategy_id, config.strategy_params)
    evaluator = MarketSignalStrategyEvaluator(**strategy_params)
    has_sell_signal = "SELL" in (strategy.get("directions") or [])
    cash = float(config.initial_capital or 0)
    positions: Dict[str, Dict] = {}
    last_prices: Dict[str, float] = {}
    equity_curve = []
    events = []
    trades = []
    closed_profits = []
    peak_value = cash

    for global_index, current_date in enumerate(dates):
        if global_index % max(1, len(dates) // 100) == 0:
            report(40 + int(55 * global_index / max(1, len(dates))), f"模拟交易日 {global_index + 1}/{len(dates)}")
        price_map = {}
        source_map = {}
        timestamp_map = {}
        for symbol, rows in row_by_symbol_date.items():
            row = rows.get(current_date)
            if not row:
                continue
            price = float(row.get("close") or 0)
            if price <= 0:
                continue
            price_map[symbol] = price
            last_prices[symbol] = price
            source_map[symbol] = row.get("price_source") or DAILY_PRICE_SOURCE
            timestamp_map[symbol] = row.get("quote_timestamp") or live_quote_timestamps.get((symbol, current_date))

        if not price_map:
            continue

        # 没有卖出信号的买入型策略，用固定持有日退出。
        if not has_sell_signal:
            for symbol, position in list(positions.items()):
                price = price_map.get(symbol)
                if price is None:
                    continue
                if global_index - position["entry_index"] < int(config.holding_days or 1):
                    continue
                sell_price = price * (1 - float(config.slippage_pct or 0) / 100)
                quantity = int(position["shares"])
                amount = sell_price * quantity
                commission = amount * float(config.commission_pct or 0) / 100
                cash += amount - commission
                cost_basis = float(position.get("cost_basis") or 0)
                profit = amount - commission - cost_basis
                profit_pct = profit / cost_basis * 100 if cost_basis > 0 else None
                closed_profits.append(profit)
                del positions[symbol]
                portfolio_after = _portfolio_value(cash, positions, last_prices)
                trades.append(_build_trade(
                    config, current_date, "SELL", symbol, sell_price, quantity, commission,
                    "holding_days_exit", f"持有满 {config.holding_days} 个交易日退出",
                    cash, portfolio_after, source_map.get(symbol, DAILY_PRICE_SOURCE), timestamp_map.get(symbol),
                    profit=profit, profit_pct=profit_pct,
                ))

        signal_events = []
        for symbol, klines in klines_by_symbol.items():
            symbol_date_index = date_index_by_symbol.get(symbol, {}).get(current_date)
            if symbol_date_index is None:
                continue
            window_start = max(0, symbol_date_index + 1 - MARKET_SIGNAL_MAX_LOOKBACK)
            result = evaluator.analyze_strategy(config.strategy_id, klines[window_start:symbol_date_index + 1])
            event = evaluator.build_signal_event(config.strategy_id, symbol, result)
            if not event or event["date"] != current_date:
                continue
            payload = _json_safe(result)
            payload.update(strategy_params)
            signal_record = {
                "config_id": config.id,
                "account_id": config.account_id,
                "strategy_id": config.strategy_id,
                "symbol": symbol,
                "date": current_date.isoformat(),
                "direction": event["direction"],
                "signal_price": event["signal_price"],
                "payload": payload,
                "price_source": source_map.get(symbol, DAILY_PRICE_SOURCE),
                "quote_timestamp": timestamp_map.get(symbol).isoformat() if timestamp_map.get(symbol) else None,
            }
            events.append(signal_record)
            signal_events.append(signal_record)

        for event in signal_events:
            symbol = event["symbol"]
            price = price_map.get(symbol)
            if price is None or price <= 0:
                continue

            if event["direction"] == "SELL" and symbol in positions:
                position = positions[symbol]
                sell_price = price * (1 - float(config.slippage_pct or 0) / 100)
                quantity = int(position["shares"])
                amount = sell_price * quantity
                commission = amount * float(config.commission_pct or 0) / 100
                cash += amount - commission
                cost_basis = float(position.get("cost_basis") or 0)
                profit = amount - commission - cost_basis
                profit_pct = profit / cost_basis * 100 if cost_basis > 0 else None
                closed_profits.append(profit)
                del positions[symbol]
                portfolio_after = _portfolio_value(cash, positions, last_prices)
                trades.append(_build_trade(
                    config, current_date, "SELL", symbol, sell_price, quantity, commission,
                    "sell_signal", f"{strategy['name']} 产生 SELL 信号",
                    cash, portfolio_after, event["price_source"], timestamp_map.get(symbol),
                    signal_date=current_date, profit=profit, profit_pct=profit_pct,
                ))
                continue

            if event["direction"] != "BUY" or symbol in positions:
                continue
            if len(positions) >= int(config.max_positions or 1):
                continue

            portfolio_before = _portfolio_value(cash, positions, last_prices)
            min_cash = portfolio_before * float(config.min_cash_pct or 0) / 100
            available_cash = max(0.0, cash - min_cash)
            target_amount = portfolio_before * float(config.position_pct or 0) / 100
            buy_budget = min(available_cash, target_amount)
            buy_price = price * (1 + float(config.slippage_pct or 0) / 100)
            commission_rate = float(config.commission_pct or 0) / 100
            quantity = _floor_lot(buy_budget / (buy_price * (1 + commission_rate)), int(config.lot_size or 1))
            if quantity <= 0:
                continue
            amount = buy_price * quantity
            commission = amount * commission_rate
            if amount + commission > cash + 1e-9:
                continue
            cash -= amount + commission
            positions[symbol] = {
                "shares": quantity,
                "avg_cost": (amount + commission) / quantity,
                "cost_basis": amount + commission,
                "entry_date": current_date,
                "entry_index": global_index,
                "last_price": price,
            }
            portfolio_after = _portfolio_value(cash, positions, last_prices)
            trades.append(_build_trade(
                config, current_date, "BUY", symbol, buy_price, quantity, commission,
                "buy_signal", f"{strategy['name']} 产生 BUY 信号",
                cash, portfolio_after, event["price_source"], timestamp_map.get(symbol),
                signal_date=current_date,
            ))

        value = _portfolio_value(cash, positions, last_prices)
        peak_value = max(peak_value, value)
        drawdown = (value / peak_value - 1) * 100 if peak_value > 0 else 0.0
        position_value = value - cash
        equity_curve.append({
            "date": current_date.isoformat(),
            "value": _round_or_none(value, 2),
            "cash": _round_or_none(cash, 2),
            "position_value": _round_or_none(position_value, 2),
            "drawdown": _round_or_none(drawdown, 2),
        })

    current_value = equity_curve[-1]["value"] if equity_curve else float(config.initial_capital or 0)
    initial_value = float(config.initial_capital or 0)
    total_return = (current_value / initial_value - 1) * 100 if initial_value > 0 else 0.0
    days = (date.fromisoformat(equity_curve[-1]["date"]) - date.fromisoformat(equity_curve[0]["date"])).days if len(equity_curve) > 1 else 0
    annualized_return = ((1 + total_return / 100) ** (365 / days) - 1) * 100 if days > 0 and total_return > -100 else 0.0
    win_count = sum(1 for item in closed_profits if item > 0)

    holdings = []
    for symbol, position in positions.items():
        price = last_prices.get(symbol, position.get("last_price") or 0.0)
        market_value = int(position["shares"]) * price
        holdings.append({
            "symbol": symbol,
            "shares": int(position["shares"]),
            "price": _round_or_none(price, 4),
            "avg_cost": _round_or_none(position.get("avg_cost"), 4),
            "entry_date": position["entry_date"].isoformat() if position.get("entry_date") else None,
            "market_value": _round_or_none(market_value, 2),
            "actual_weight_pct": _round_or_none(market_value / current_value * 100 if current_value > 0 else 0.0, 2),
        })
    holdings.sort(key=lambda item: item.get("market_value") or 0, reverse=True)

    metrics = {
        "total_return": _round_or_none(total_return, 2),
        "annualized_return": _round_or_none(annualized_return, 2),
        "max_drawdown": _round_or_none(min([item["drawdown"] for item in equity_curve] or [0]), 2),
        "signal_count": len(events),
        "buy_signal_count": sum(1 for item in events if item["direction"] == "BUY"),
        "sell_signal_count": sum(1 for item in events if item["direction"] == "SELL"),
        "trade_count": len(trades),
        "closed_trade_count": len(closed_profits),
        "win_count": win_count,
        "win_rate": _round_or_none(win_count / len(closed_profits) * 100 if closed_profits else 0.0, 2),
        "ending_value": _round_or_none(current_value, 2),
        "cash": equity_curve[-1]["cash"] if equity_curve else _round_or_none(cash, 2),
    }

    return {
        "strategy": strategy,
        "params": _config_to_dict(config),
        "metrics": metrics,
        "equity_curve": equity_curve,
        "events": events,
        "trades": trades,
        "current_holdings": holdings,
        "errors": errors,
        "meta": {
            "symbols_requested": config.symbols or [],
            "symbols_used": list(klines_by_symbol.keys()),
            "include_live_quote": include_live_quote,
            "live_date": live_date.isoformat() if live_date else None,
            "strategy_params": strategy_params,
        },
    }


def _replace_config_runtime_state(
    db: ORMSession,
    config: MarketSignalStrategyConfig,
    result: Dict,
    trigger_source: str = "manual",
):
    db.query(MarketSignalEvent).filter(MarketSignalEvent.config_id == config.id).delete()
    db.query(MarketSignalVirtualTrade).filter(MarketSignalVirtualTrade.config_id == config.id).delete()
    db.query(MarketSignalVirtualHolding).filter(MarketSignalVirtualHolding.config_id == config.id).delete()
    db.query(MarketSignalVirtualEquity).filter(MarketSignalVirtualEquity.config_id == config.id).delete()
    db.query(MarketSignalVirtualLog).filter(MarketSignalVirtualLog.config_id == config.id).delete()

    now = datetime.now()
    for item in result.get("events") or []:
        db.add(MarketSignalEvent(
            config_id=config.id,
            account_id=config.account_id,
            strategy_id=item.get("strategy_id"),
            symbol=item.get("symbol"),
            date=date.fromisoformat(item["date"]),
            direction=item.get("direction"),
            signal_price=item.get("signal_price"),
            payload=item.get("payload"),
            price_source=item.get("price_source"),
            quote_timestamp=datetime.fromisoformat(item["quote_timestamp"]) if item.get("quote_timestamp") else None,
            created_at=now,
        ))

    for item in result.get("equity_curve") or []:
        db.add(MarketSignalVirtualEquity(
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

    for item in result.get("trades") or []:
        db.add(MarketSignalVirtualTrade(
            config_id=config.id,
            account_id=config.account_id,
            date=date.fromisoformat(item["date"]),
            signal_date=date.fromisoformat(item["signal_date"]) if item.get("signal_date") else None,
            strategy_id=item.get("strategy_id"),
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
            price_source=item.get("price_source"),
            quote_timestamp=datetime.fromisoformat(item["quote_timestamp"]) if item.get("quote_timestamp") else None,
            created_at=now,
        ))

    for item in result.get("current_holdings") or []:
        db.add(MarketSignalVirtualHolding(
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

    db.add(MarketSignalVirtualLog(
        config_id=config.id,
        account_id=config.account_id,
        timestamp=now,
        level="INFO",
        action="SYNC",
        message="市场信号虚拟盘已同步",
        payload={
            "trigger_source": trigger_source,
            "metrics": result.get("metrics"),
            "meta": result.get("meta"),
            "errors": result.get("errors"),
        },
    ))

    config.last_sync_at = now
    config.last_sync_status = "success"
    config.last_sync_message = "同步完成"
    config.updated_at = now


def _mark_sync_running(db: ORMSession, config: MarketSignalStrategyConfig, message: str = "同步中"):
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "running"
    config.last_sync_message = message
    config.updated_at = now


def _mark_sync_failed(db: ORMSession, config_id: int, account_id: str, exc: Exception):
    config = db.query(MarketSignalStrategyConfig).filter(
        MarketSignalStrategyConfig.id == config_id,
        MarketSignalStrategyConfig.account_id == account_id,
    ).first()
    if not config:
        return
    now = datetime.now()
    config.last_sync_at = now
    config.last_sync_status = "failed"
    config.last_sync_message = str(exc)[:500]
    config.updated_at = now
    db.add(MarketSignalVirtualLog(
        config_id=config.id,
        account_id=account_id,
        timestamp=now,
        level="ERROR",
        action="SYNC_FAILED",
        message=str(exc),
    ))


def _sync_config_now(
    db: ORMSession,
    config: MarketSignalStrategyConfig,
    trigger_source: str = "manual",
    include_live_quote: bool = False,
) -> Dict:
    quote_service = _get_quote_service(config.account_id)
    result = _run_virtual_engine(config, quote_service, include_live_quote=include_live_quote)
    _replace_config_runtime_state(db, config, result, trigger_source=trigger_source)
    return result


def _latest_sync_payload(db: ORMSession, config: MarketSignalStrategyConfig) -> Dict:
    log = (
        db.query(MarketSignalVirtualLog)
        .filter(MarketSignalVirtualLog.config_id == config.id, MarketSignalVirtualLog.action == "SYNC")
        .order_by(MarketSignalVirtualLog.timestamp.desc(), MarketSignalVirtualLog.id.desc())
        .first()
    )
    return log.payload if log and log.payload else {}


def _runtime_summary(db: ORMSession, config: MarketSignalStrategyConfig) -> Dict:
    latest_equity = (
        db.query(MarketSignalVirtualEquity)
        .filter(MarketSignalVirtualEquity.config_id == config.id)
        .order_by(MarketSignalVirtualEquity.date.desc())
        .first()
    )
    payload = _latest_sync_payload(db, config)
    metrics = payload.get("metrics") or {}
    return {
        "latest_date": latest_equity.date.isoformat() if latest_equity else None,
        "portfolio_value": latest_equity.value if latest_equity else None,
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "signal_count": metrics.get("signal_count") or db.query(MarketSignalEvent).filter(MarketSignalEvent.config_id == config.id).count(),
        "trade_count": metrics.get("trade_count") or db.query(MarketSignalVirtualTrade).filter(MarketSignalVirtualTrade.config_id == config.id).count(),
        "holding_count": db.query(MarketSignalVirtualHolding).filter(MarketSignalVirtualHolding.config_id == config.id).count(),
    }


def _serialize_event(item: MarketSignalEvent) -> Dict:
    strategy = MARKET_SIGNAL_STRATEGY_MAP.get(item.strategy_id, {})
    return {
        "id": item.id,
        "config_id": item.config_id,
        "symbol": item.symbol,
        "strategy_id": item.strategy_id,
        "strategy_name": strategy.get("name", item.strategy_id),
        "strategy_summary": strategy.get("summary"),
        "close_price": item.signal_price,
        "signal_price": item.signal_price,
        "date": item.date.isoformat(),
        "direction": item.direction,
        "payload": item.payload,
        "price_source": item.price_source,
        "quote_timestamp": item.quote_timestamp.isoformat() if item.quote_timestamp else None,
        "below_200ma_ratio": (item.payload or {}).get("below_200ma_ratio"),
        "vol_5_std": (item.payload or {}).get("vol_5_std"),
        "today_vol_std": (item.payload or {}).get("today_vol_std"),
        "low_50": (item.payload or {}).get("low_50"),
        "close_vs_low_50": (item.payload or {}).get("close_vs_low_50"),
        "v2_price_change_ratio": (item.payload or {}).get("price_change_ratio"),
        "v2_stabilization_period": (item.payload or {}).get("stabilization_period"),
    }


def _update_backtest_job(task_id: str, **kwargs) -> None:
    with BACKTEST_JOBS_LOCK:
        job = BACKTEST_JOBS.setdefault(task_id, {})
        job.update(kwargs)


def _get_backtest_job(task_id: str) -> Dict:
    with BACKTEST_JOBS_LOCK:
        return dict(BACKTEST_JOBS.get(task_id, {}))


def _run_market_signal_backtest_job(task_id: str, payload: Dict, account_id: str) -> None:
    try:
        _update_backtest_job(task_id, status="running", progress=0, message="初始化")
        with get_db_ctx() as db:
            request = MarketSignalBacktestRequest(**payload)
            try:
                start_dt = _parse_date(request.start_date)
                end_dt = _parse_date(request.end_date, date.today())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            if start_dt >= end_dt:
                raise HTTPException(status_code=400, detail="开始日期必须早于结束日期")

            symbols = (request.symbols or _get_default_symbols(db, request.max_symbols))[:request.max_symbols]
            temp_config = MarketSignalStrategyConfig(
                id=0,
                account_id=account_id,
                strategy_id=request.strategy_id,
                name=f"{MARKET_SIGNAL_STRATEGY_MAP[request.strategy_id]['name']}回测",
                enabled=False,
                symbols=symbols,
                initial_capital=request.initial_capital,
                start_date=start_dt,
                holding_days=request.holding_days,
                position_pct=request.position_pct,
                max_positions=request.max_positions,
                min_cash_pct=0.0,
                commission_pct=0.03,
                slippage_pct=0.02,
                lot_size=1,
                min_market_cap=0.0,
                strategy_params=_normalize_strategy_params(request.strategy_id, request.strategy_params),
            )

            def progress_callback(progress: int, message: str):
                _update_backtest_job(task_id, progress=progress, message=message)

            result = _run_virtual_engine(
                temp_config,
                _get_quote_service(account_id),
                end_dt=end_dt,
                include_live_quote=False,
                progress_callback=progress_callback,
            )
        _update_backtest_job(task_id, status="completed", progress=100, message="完成", result=result)
    except Exception as exc:
        logger.exception("Market signal backtest failed")
        _update_backtest_job(task_id, status="failed", error=str(exc), message="失败")


@router.get("/api/market_signal/strategies")
def list_market_signal_strategies():
    return {"items": MARKET_SIGNAL_STRATEGIES}


@router.get("/api/market_signal/default-symbols")
def get_market_signal_default_symbols(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    spy_date, spy_symbols = _get_latest_etf_equity_symbols(db, "SPY.US")
    qqq_date, qqq_symbols = _get_latest_etf_equity_symbols(db, "QQQ.US")
    symbols = list(dict.fromkeys(spy_symbols + qqq_symbols))
    if symbols:
        return {
            "symbols": symbols,
            "source": "SPY.US ∪ QQQ.US",
            "spy_holding_date": spy_date.isoformat() if spy_date else None,
            "qqq_holding_date": qqq_date.isoformat() if qqq_date else None,
            "count": len(symbols),
        }

    fallback = _get_default_symbols(db, limit=DEFAULT_SIGNAL_SYMBOL_LIMIT)
    return {
        "symbols": fallback,
        "source": "fallback",
        "spy_holding_date": spy_date.isoformat() if spy_date else None,
        "qqq_holding_date": qqq_date.isoformat() if qqq_date else None,
        "count": len(fallback),
    }


@router.get("/api/market_signal/configs")
def list_market_signal_configs(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    _ensure_default_configs(db, account_id)
    configs = (
        db.query(MarketSignalStrategyConfig)
        .filter(MarketSignalStrategyConfig.account_id == account_id)
        .order_by(MarketSignalStrategyConfig.strategy_id.asc(), MarketSignalStrategyConfig.id.asc())
        .all()
    )
    return [
        {
            **_config_to_dict(config),
            "runtime": _runtime_summary(db, config),
        }
        for config in configs
    ]


@router.post("/api/market_signal/configs")
def create_market_signal_config(
    payload: MarketSignalConfigPayload,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = MarketSignalStrategyConfig(account_id=account_id, created_at=datetime.now())
    _apply_payload(config, payload)
    fields_set = getattr(payload, "__fields_set__", getattr(payload, "model_fields_set", set()))
    if "symbols" not in fields_set:
        config.symbols = _get_default_symbols(db, limit=DEFAULT_SIGNAL_SYMBOL_LIMIT)
    db.add(config)
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.put("/api/market_signal/configs/{config_id}")
def update_market_signal_config(
    config_id: int,
    payload: MarketSignalConfigPayload,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _apply_payload(config, payload)
    db.commit()
    db.refresh(config)
    return _config_to_dict(config)


@router.delete("/api/market_signal/configs/{config_id}")
def delete_market_signal_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    db.query(MarketSignalEvent).filter(MarketSignalEvent.config_id == config.id).delete()
    db.query(MarketSignalVirtualTrade).filter(MarketSignalVirtualTrade.config_id == config.id).delete()
    db.query(MarketSignalVirtualHolding).filter(MarketSignalVirtualHolding.config_id == config.id).delete()
    db.query(MarketSignalVirtualEquity).filter(MarketSignalVirtualEquity.config_id == config.id).delete()
    db.query(MarketSignalVirtualLog).filter(MarketSignalVirtualLog.config_id == config.id).delete()
    db.delete(config)
    db.commit()
    return {"message": "已删除市场信号虚拟盘配置"}


@router.post("/api/market_signal/configs/{config_id}/sync")
def sync_market_signal_config(
    config_id: int,
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    _mark_sync_running(db, config)
    db.commit()
    try:
        result = _sync_config_now(db, config, trigger_source="manual", include_live_quote=True)
        db.commit()
        return {"message": "同步完成", "config": _config_to_dict(config), "summary": result.get("metrics")}
    except Exception as exc:
        db.rollback()
        _mark_sync_failed(db, config_id, account_id, exc)
        db.commit()
        logger.exception("Market signal virtual sync failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/market_signal/configs/sync-enabled")
def sync_enabled_market_signal_configs(
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    _ensure_default_configs(db, account_id)
    configs = db.query(MarketSignalStrategyConfig).filter(
        MarketSignalStrategyConfig.account_id == account_id,
        MarketSignalStrategyConfig.enabled == True,  # noqa: E712
    ).all()
    synced = []
    errors = []
    for config in configs:
        config_id = config.id
        config_name = config.name
        _mark_sync_running(db, config)
        db.commit()
        try:
            result = _sync_config_now(db, config, trigger_source="manual_all", include_live_quote=True)
            db.commit()
            synced.append({"id": config_id, "name": config_name, "summary": result.get("metrics")})
        except Exception as exc:
            db.rollback()
            _mark_sync_failed(db, config_id, account_id, exc)
            db.commit()
            errors.append({"id": config_id, "name": config_name, "error": str(exc)})
    return {"synced": synced, "errors": errors}


def _auto_sync_already_attempted(config: MarketSignalStrategyConfig, now_et: datetime) -> bool:
    if not config.last_auto_sync_at:
        return False
    last_at = config.last_auto_sync_at
    if last_at.tzinfo:
        return last_at.astimezone(EASTERN_TZ).date() == now_et.date()
    return last_at.date() == now_et.date()


def sync_due_market_signal_configs_for_auto_sync(now_et: Optional[datetime] = None) -> Dict:
    now_et = now_et or MarketService.get_eastern_now()
    current_time = now_et.strftime("%H:%M")
    result = {
        "synced": [],
        "errors": [],
        "skipped": [],
        "current_time": current_time,
        "timezone": "US/Eastern",
    }
    if now_et.weekday() >= 5 or MarketService.is_us_market_holiday(now_et.date()):
        result["skipped"].append({"reason": "美股休市日"})
        return result

    with get_db_ctx() as db:
        configs = (
            db.query(MarketSignalStrategyConfig)
            .filter(
                MarketSignalStrategyConfig.enabled == True,  # noqa: E712
                MarketSignalStrategyConfig.auto_sync_enabled == True,  # noqa: E712
                MarketSignalStrategyConfig.auto_sync_time == current_time,
            )
            .order_by(MarketSignalStrategyConfig.account_id.asc(), MarketSignalStrategyConfig.id.asc())
            .all()
        )
        for config in configs:
            config_id = config.id
            account_id = config.account_id
            config_name = config.name
            if _auto_sync_already_attempted(config, now_et):
                result["skipped"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "reason": "今日已自动触发",
                })
                continue

            attempt_at = now_et.replace(tzinfo=None)
            _mark_sync_running(db, config, message=f"自动同步中（美东 {current_time}）")
            config.last_auto_sync_at = attempt_at
            db.commit()
            try:
                sync_result = _sync_config_now(db, config, trigger_source="module_auto", include_live_quote=True)
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
                failed_config = db.query(MarketSignalStrategyConfig).filter(
                    MarketSignalStrategyConfig.id == config_id,
                    MarketSignalStrategyConfig.account_id == account_id,
                ).first()
                if failed_config:
                    failed_config.last_auto_sync_at = attempt_at
                _mark_sync_failed(db, config_id, account_id, exc)
                db.commit()
                logger.exception("Market signal module auto sync failed")
                result["errors"].append({
                    "id": config_id,
                    "account_id": account_id,
                    "name": config_name,
                    "error": str(exc),
                })
    return result


@router.get("/api/market_signal/configs/{config_id}/detail")
def get_market_signal_detail(
    config_id: int,
    log_limit: int = Query(200, ge=1, le=1000),
    trade_limit: int = Query(500, ge=1, le=5000),
    event_limit: int = Query(500, ge=1, le=5000),
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    config = _get_config_or_404(db, account_id, config_id)
    equity = (
        db.query(MarketSignalVirtualEquity)
        .filter(MarketSignalVirtualEquity.config_id == config.id)
        .order_by(MarketSignalVirtualEquity.date.asc())
        .all()
    )
    holdings = (
        db.query(MarketSignalVirtualHolding)
        .filter(MarketSignalVirtualHolding.config_id == config.id)
        .order_by(MarketSignalVirtualHolding.market_value.desc())
        .all()
    )
    trades = (
        db.query(MarketSignalVirtualTrade)
        .filter(MarketSignalVirtualTrade.config_id == config.id)
        .order_by(MarketSignalVirtualTrade.date.desc(), MarketSignalVirtualTrade.id.desc())
        .limit(trade_limit)
        .all()
    )
    events = (
        db.query(MarketSignalEvent)
        .filter(MarketSignalEvent.config_id == config.id)
        .order_by(MarketSignalEvent.date.desc(), MarketSignalEvent.id.desc())
        .limit(event_limit)
        .all()
    )
    logs = (
        db.query(MarketSignalVirtualLog)
        .filter(MarketSignalVirtualLog.config_id == config.id)
        .order_by(MarketSignalVirtualLog.timestamp.desc(), MarketSignalVirtualLog.id.desc())
        .limit(log_limit)
        .all()
    )
    payload = _latest_sync_payload(db, config)
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
            "metrics": payload.get("metrics") or {},
            "meta": payload.get("meta") or {},
            "errors": payload.get("errors") or [],
            "trade_count": db.query(MarketSignalVirtualTrade).filter(MarketSignalVirtualTrade.config_id == config.id).count(),
            "signal_count": db.query(MarketSignalEvent).filter(MarketSignalEvent.config_id == config.id).count(),
            "holding_count": len(holdings),
        },
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
                "strategy_id": item.strategy_id,
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
                "quote_timestamp": item.quote_timestamp.isoformat() if item.quote_timestamp else None,
            }
            for item in trades
        ],
        "events": [_serialize_event(item) for item in events],
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


@router.get("/api/market_signal")
def get_market_signal(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    strategy_id: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    config_id: Optional[int] = Query(None),
    account_id: str = Depends(valid_account),
    db: ORMSession = Depends(get_db),
):
    if strategy_id and strategy_id not in MARKET_SIGNAL_STRATEGY_MAP:
        raise HTTPException(status_code=400, detail="不支持的策略")
    normalized_direction = direction.upper() if direction else None
    if normalized_direction and normalized_direction not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="direction 仅支持 BUY 或 SELL")

    query = db.query(MarketSignalEvent).filter(MarketSignalEvent.account_id == account_id)
    if strategy_id:
        query = query.filter(MarketSignalEvent.strategy_id == strategy_id)
    if normalized_direction:
        query = query.filter(MarketSignalEvent.direction == normalized_direction)
    if config_id:
        query = query.filter(MarketSignalEvent.config_id == config_id)
    query = query.order_by(MarketSignalEvent.date.desc(), MarketSignalEvent.id.desc())
    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize_event(item) for item in items],
    }


@router.post("/api/market_signal/backtest")
def start_market_signal_backtest(
    payload: MarketSignalBacktestRequest,
    background_tasks: BackgroundTasks,
    account_id: str = Depends(valid_account),
):
    task_id = uuid.uuid4().hex
    _update_backtest_job(
        task_id,
        status="pending",
        progress=0,
        message="等待启动",
        result=None,
        error=None,
        created_at=datetime.now().isoformat(),
    )
    background_tasks.add_task(_run_market_signal_backtest_job, task_id, payload.dict(), account_id)
    return {"task_id": task_id, "status": "pending"}


@router.get("/api/market_signal/backtest/jobs/{task_id}", response_model=MarketSignalBacktestJobStatus)
def get_market_signal_backtest_job(task_id: str):
    job = _get_backtest_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": task_id,
        "status": job.get("status", "pending"),
        "progress": int(job.get("progress", 0) or 0),
        "message": job.get("message"),
        "result": job.get("result"),
        "error": job.get("error"),
    }
