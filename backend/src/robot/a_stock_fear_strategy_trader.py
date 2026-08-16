"""A股情绪量能自动交易策略（独立于 SOXL 美股情绪量能策略）。

信号与交易隔天：每个 A 股交易日在 run_time（默认 09:30 Asia/Shanghai 开盘）时，
用**前一交易日**收盘后形成的恐贪分数和 20 日量比判断信号，随后下市价单在开盘成交。
- 买入：前一交易日恐贪 <= buy_threshold 且量比 >= volume_ratio_threshold → 按 buy_position_pct 买入
- 卖出：前一交易日恐贪 >= greed_threshold（trailing_stop_pct=0 即卖）或移动止盈回撤触发 → 按 sell_position_pct 卖出

数据源全部是已完成数据（本地 sqlite 恐贪 + duckdb ETF 日线），不需要盘中量能投影；
实时价格只用于下单数量计算（外部 hub PTrade quote，LongPort 兜底）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import traceback
from datetime import date, datetime, time as dtime, timedelta
from math import ceil, floor
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy.exc import OperationalError

from ..core.database import (
    AStockFearStrategyConfig,
    AStockFearStrategyLog,
    AStockFearStrategyState,
    ETFFearGreedCloneHistory,
    get_db_ctx,
)
from ..core.duckdb_utils import connect_duckdb
from ..core.event_stream import publish_event
from ..core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingOrder,
    ExternalTradingSubAccount,
    get_external_trading_db_ctx,
)
from ..core.services.external_trading_executor import trigger_external_trading_executor
from ..core.services.external_trading_ledger import (
    ACTIVE_ORDER_STATUSES,
    STRATEGY_A_STOCK_FEAR,
    get_ledger_positions,
    normalize_symbol as normalize_external_symbol,
    safe_float as external_safe_float,
    safe_int as external_safe_int,
    sync_target_positions,
)
from ..core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_A_STOCK,
    _is_china_trading_day,
    normalize_external_trading_market_type,
)
from ..core.services.external_trading_valuation import (
    ExternalTradingValuationError,
    calculate_sub_account_net_asset,
    get_realtime_price_details,
)
from ..core.utils import mask_account_id, send_alert_email, send_configured_email
from .a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS

logger = logging.getLogger(__name__)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_RUN_TIME = "09:30"
VOLUME_WINDOW = 20
A_STOCK_INNO100_FEAR_SYMBOL = "INNO100.CN"
MAIN_DB_WRITE_RETRY_ATTEMPTS = 4
MAIN_DB_WRITE_RETRY_BASE_SECONDS = 1.0
MAIN_DB_WRITE_RETRY_MAX_SECONDS = 8.0


def _fear_source_index_symbol(fear_source: str) -> Optional[str]:
    """恐贪来源 key → 指数标的。a_stock_000015_sh → 000015.SH；qqq_clone → QQQ.US（美股自算贪恐）。"""
    key = (fear_source or "").strip().lower()
    if not key:
        return None
    if key.startswith("a_stock_"):
        return key.removeprefix("a_stock_").replace("_", ".").upper()
    us_map = {
        "qqq_clone": "QQQ.US",
        "soxx_clone": "SOXX.US",
        "spy_clone": "SPY.US",
        "dia_clone": "DIA.US",
    }
    return us_map.get(key)


def _fear_source_label(fear_source: str) -> str:
    key = (fear_source or "").strip().lower()
    symbol = _fear_source_index_symbol(key)
    if not symbol:
        return "贪恐"
    us_labels = {
        "qqq_clone": "QQQ 纳指100自算贪恐",
        "soxx_clone": "SOXX 半导体自算贪恐",
        "spy_clone": "SPY 标普500自算贪恐",
        "dia_clone": "DIA 道琼斯自算贪恐",
    }
    if key in us_labels:
        return us_labels[key]
    for target in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        if str(target["symbol"]).upper() == symbol:
            return f"{target.get('ticker') or target.get('label') or symbol} 指数贪恐"
    if symbol == A_STOCK_INNO100_FEAR_SYMBOL:
        return "A创100 指数贪恐"
    return f"{symbol} 贪恐"


class AStockFearStrategyTrader:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(AStockFearStrategyTrader, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._thread_started = False
        self._thread_lock = threading.Lock()
        self._last_auto_trigger_date: Dict[int, date] = {}

    @staticmethod
    def _china_now() -> datetime:
        return datetime.now(SHANGHAI_TZ)

    @staticmethod
    def _parse_run_time(value: str) -> dtime:
        text = str(value or DEFAULT_RUN_TIME).strip()
        try:
            return datetime.strptime(text, "%H:%M").time()
        except ValueError:
            return datetime.strptime(DEFAULT_RUN_TIME, "%H:%M").time()

    @staticmethod
    def _prev_china_trading_day(check_date: date) -> date:
        current = check_date - timedelta(days=1)
        while not _is_china_trading_day(current):
            current -= timedelta(days=1)
        return current

    @staticmethod
    def _is_sqlite_lock_error(exc: Exception) -> bool:
        if not isinstance(exc, OperationalError):
            return False
        message = str(exc).lower()
        return "database is locked" in message or "database table is locked" in message

    def _write_main_db_with_retry(self, operation_name: str, writer):
        for attempt in range(1, MAIN_DB_WRITE_RETRY_ATTEMPTS + 1):
            try:
                return writer()
            except Exception as exc:
                if not self._is_sqlite_lock_error(exc) or attempt >= MAIN_DB_WRITE_RETRY_ATTEMPTS:
                    raise
                sleep_seconds = min(
                    MAIN_DB_WRITE_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    MAIN_DB_WRITE_RETRY_MAX_SECONDS,
                )
                logger.warning(
                    "%s hit SQLite lock, retrying %s/%s in %.1fs: %s",
                    operation_name, attempt + 1, MAIN_DB_WRITE_RETRY_ATTEMPTS, sleep_seconds, exc,
                )
                time.sleep(sleep_seconds)
        return None

    def _fetch_fear_map(self, index_symbol: str, start: date, end: date) -> Dict[date, float]:
        """恐贪分数（final 日频，etf_fear_greed_clone_history）。"""
        if start > end:
            return {}
        with get_db_ctx() as db:
            rows = (
                db.query(ETFFearGreedCloneHistory)
                .filter(
                    ETFFearGreedCloneHistory.symbol == index_symbol,
                    ETFFearGreedCloneHistory.date >= start,
                    ETFFearGreedCloneHistory.date <= end,
                )
                .all()
            )
            result = {
                row.date: float(row.score)
                for row in rows
                if row.score is not None
            }
        return result

    def _fetch_etf_bars(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """日线（前复权）。A股查 a_stock_fund_daily_qfq；美股（.US，如 QQQ.US 量比来源）查 us_stock_daily。"""
        normalized = normalize_external_symbol(symbol) or symbol
        is_us = normalized.upper().endswith(".US")
        table = "us_stock_daily" if is_us else "a_stock_fund_daily_qfq"
        try:
            connection = connect_duckdb(prefer_read_only=True)
        except Exception as exc:
            logger.warning("A股情绪量能策略无法连接分析库: %s", exc)
            return pd.DataFrame()
        try:
            frame = connection.execute(
                f"""
                SELECT trade_date, open, high, low, close, volume
                FROM {table}
                WHERE upper(symbol) = ?
                  AND trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                ORDER BY trade_date
                """,
                [normalized, start.isoformat(), end.isoformat()],
            ).fetch_df()
        finally:
            connection.close()
        if frame is None or frame.empty:
            return pd.DataFrame()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna(subset=["close"]).sort_values("trade_date").reset_index(drop=True)

    @staticmethod
    def _volume_ratio_at(bars: pd.DataFrame, signal_date: date) -> Optional[float]:
        """信号日量比 = 当日成交量 / 前 20 日均量（shift(1) 语义，与回测一致）。"""
        if bars is None or bars.empty:
            return None
        prior_mean = bars["volume"].shift(1).rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).mean()
        bars = bars.assign(prior_volume_mean=prior_mean)
        row = bars[bars["trade_date"] == signal_date]
        if row.empty:
            return None
        volume = float(row.iloc[0]["volume"])
        prior = float(row.iloc[0]["prior_volume_mean"])
        if np.isnan(volume) or np.isnan(prior) or prior <= 0:
            return None
        return volume / prior

    @staticmethod
    def _log_z_at(bars: pd.DataFrame, signal_date: date) -> Optional[float]:
        """信号日对数放量 z 值：(log(vol) - 前20日log均值) / 前20日log标准差（与回测一致）。"""
        if bars is None or bars.empty:
            return None
        log_volume = np.log(pd.to_numeric(bars["volume"], errors="coerce").replace(0, np.nan))
        prior_mean = log_volume.shift(1).rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).mean()
        prior_std = log_volume.shift(1).rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).std(ddof=0)
        row = bars[bars["trade_date"] == signal_date]
        if row.empty:
            return None
        idx = row.index[0]
        log_vol = float(log_volume.iloc[idx])
        prior = float(prior_mean.iloc[idx])
        std = float(prior_std.iloc[idx])
        if np.isnan(log_vol) or np.isnan(prior) or np.isnan(std) or std <= 0:
            return None
        return (log_vol - prior) / std

    async def _build_snapshot(self, config: SimpleNamespace, price_map: Dict[str, float]) -> SimpleNamespace:
        """外部交易子账户快照（仅 A 股外部账户）。返回主标的与跷跷板候补的持仓。"""
        if not config.external_trading_account_id or not config.live_sub_account_id:
            raise ValueError("未选择外部交易账户或虚拟子账户")
        main_symbol = normalize_external_symbol(config.symbol)
        sub_symbol = normalize_external_symbol(config.sub_symbol) if getattr(config, "sub_symbol", None) else None
        sub2_symbol = normalize_external_symbol(config.sub2_symbol) if getattr(config, "sub2_symbol", None) else None
        if not main_symbol:
            raise ValueError("交易标的格式不正确")

        with get_external_trading_db_ctx() as db:
            account = db.query(ExternalTradingAccount).filter(
                ExternalTradingAccount.id == config.external_trading_account_id,
                ExternalTradingAccount.account_id == config.account_id,
                ExternalTradingAccount.enabled == True,  # noqa: E712
            ).first()
            if not account:
                raise ValueError("外部交易账户不存在或未启用")
            if normalize_external_trading_market_type(account.market_type) != EXTERNAL_TRADING_MARKET_A_STOCK:
                raise ValueError("A股情绪量能策略只能绑定 A 股外部交易账户")

            sub_account = db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == config.live_sub_account_id,
                ExternalTradingSubAccount.account_id == config.account_id,
                ExternalTradingSubAccount.external_trading_account_id == account.id,
                ExternalTradingSubAccount.enabled == True,  # noqa: E712
            ).first()
            if not sub_account:
                raise ValueError("外部交易虚拟子账户不存在或未启用")
            if sub_account.strategy_type != STRATEGY_A_STOCK_FEAR or sub_account.strategy_config_id != config.config_id:
                raise ValueError("外部交易虚拟子账户归属不匹配")

            positions = get_ledger_positions(db, sub_account.id)

            def _position_info(symbol_key: str) -> Dict:
                position = positions.get(symbol_key)
                return {
                    "shares": external_safe_int(getattr(position, "quantity", 0)),
                    "available_shares": external_safe_int(
                        getattr(position, "available_quantity", external_safe_int(getattr(position, "quantity", 0)))
                    ),
                    "avg_cost": external_safe_float(getattr(position, "avg_cost", 0.0)),
                }

            main_info = _position_info(main_symbol)
            sub_info = _position_info(sub_symbol) if sub_symbol else {"shares": 0, "available_shares": 0, "avg_cost": 0.0}
            sub2_info = _position_info(sub2_symbol) if sub2_symbol else {"shares": 0, "available_shares": 0, "avg_cost": 0.0}

            try:
                valuation = await calculate_sub_account_net_asset(db, sub_account)
                available_cash = external_safe_float(valuation.get("cash_available"))
                portfolio_value = external_safe_float(valuation.get("net_asset"))
                if portfolio_value <= 0:
                    portfolio_value = available_cash + main_info["shares"] * float(price_map.get(main_symbol) or 0) + sub_info["shares"] * float(price_map.get(sub_symbol) or 0)
            except ExternalTradingValuationError as exc:
                logger.warning("A股情绪量能策略估值失败，回退账本值: %s", exc)
                available_cash = external_safe_float(sub_account.cash_available)
                holdings_value = 0.0
                for row in positions.values():
                    quantity = external_safe_int(getattr(row, "quantity", 0))
                    if quantity <= 0:
                        continue
                    row_symbol = normalize_external_symbol(getattr(row, "symbol", None))
                    row_price = external_safe_float(getattr(row, "market_price", 0.0))
                    if row_symbol == main_symbol and row_price <= 0:
                        row_price = float(price_map.get(main_symbol) or 0)
                    if sub_symbol and row_symbol == sub_symbol and row_price <= 0:
                        row_price = float(price_map.get(sub_symbol) or 0)
                    holdings_value += quantity * max(row_price, 0.0)
                portfolio_value = available_cash + holdings_value

            has_open_order = db.query(ExternalTradingOrder).filter(
                ExternalTradingOrder.account_id == config.account_id,
                ExternalTradingOrder.external_trading_account_id == account.id,
                ExternalTradingOrder.sub_account_id == sub_account.id,
                ExternalTradingOrder.symbol.in_([main_symbol, sub_symbol, sub2_symbol] if sub2_symbol else ([main_symbol, sub_symbol] if sub_symbol else [main_symbol])),
                ExternalTradingOrder.status.in_(list(ACTIVE_ORDER_STATUSES)),
            ).first() is not None

            return SimpleNamespace(
                config_id=config.config_id,
                account_id=config.account_id,
                symbol=main_symbol,
                sub_symbol=sub_symbol,
                sub2_symbol=sub2_symbol,
                shares=max(0, main_info["shares"]),
                available_shares=max(0, main_info["available_shares"]),
                avg_cost=main_info["avg_cost"],
                sub_shares=max(0, sub_info["shares"]),
                sub_available_shares=max(0, sub_info["available_shares"]),
                sub_avg_cost=sub_info["avg_cost"],
                sub2_shares=max(0, sub2_info["shares"]),
                sub2_available_shares=max(0, sub2_info["available_shares"]),
                sub2_avg_cost=sub2_info["avg_cost"],
                available_cash=available_cash,
                portfolio_value=max(portfolio_value, 1.0),
                has_today_order=has_open_order,
                external_trading_account_id=account.id,
                live_sub_account_id=sub_account.id,
            )

    @staticmethod
    def _position_shares_for_symbol(snapshot: SimpleNamespace, symbol: str, config: SimpleNamespace) -> int:
        """按标的取快照持仓（区分主/sub/sub2，换仓目标可能是 sub2）。"""
        main_symbol = normalize_external_symbol(config.symbol)
        sub_symbol = normalize_external_symbol(config.sub_symbol) if getattr(config, "sub_symbol", None) else None
        sub2_symbol = normalize_external_symbol(config.sub2_symbol) if getattr(config, "sub2_symbol", None) else None
        if symbol == main_symbol:
            return int(snapshot.shares)
        if sub2_symbol and symbol == sub2_symbol:
            return int(snapshot.sub2_shares or 0)
        return int(snapshot.sub_shares or 0)

    async def _sync_target_order(
        self,
        config: SimpleNamespace,
        snapshot: SimpleNamespace,
        action: str,
        quantity: int,
        price: float,
        trigger_source: str,
        symbol: Optional[str] = None,
        trigger_executor: bool = True,
    ) -> str:
        symbol = normalize_external_symbol(symbol or config.symbol)
        if not symbol:
            raise ValueError("交易标的格式不正确")
        current_shares = self._position_shares_for_symbol(snapshot, symbol, config)
        if action == "BUY":
            target_quantity = current_shares + int(quantity)
        elif action == "SELL":
            target_quantity = max(0, current_shares - int(quantity))
        else:
            raise ValueError("外部交易仅支持 BUY 或 SELL")

        signal_version = datetime.now().strftime(f"a_stock_fear:{config.config_id}:%Y%m%d%H%M%S")
        target = {
            "symbol": symbol,
            "target_quantity": target_quantity,
            "target_weight_pct": (
                target_quantity * price / snapshot.portfolio_value * 100
                if snapshot.portfolio_value > 0 and price > 0
                else None
            ),
            "target_value": round(target_quantity * price, 2),
            "reference_price": round(price, 4),
            "reference_price_source": "a_stock_fear_strategy",
        }

        with get_external_trading_db_ctx() as db:
            sub_account = db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == snapshot.live_sub_account_id,
                ExternalTradingSubAccount.account_id == config.account_id,
                ExternalTradingSubAccount.external_trading_account_id == snapshot.external_trading_account_id,
                ExternalTradingSubAccount.strategy_type == STRATEGY_A_STOCK_FEAR,
                ExternalTradingSubAccount.strategy_config_id == config.config_id,
            ).first()
            if not sub_account:
                raise ValueError("外部交易虚拟子账户归属不匹配")
            sync_target_positions(
                db,
                sub_account=sub_account,
                targets=[target],
                signal_id=f"a_stock_fear:{config.config_id}:{action.lower()}",
                signal_version=signal_version,
                source_execution_id=None,
            )

        if trigger_executor:
            executor_result = await trigger_external_trading_executor(
                account_id=config.account_id,
                external_account_id=snapshot.external_trading_account_id,
                trigger_source=f"a_stock_fear_{trigger_source}",
            )
            external_order_count = sum(
                int(item.get("external_order_count") or 0)
                for item in (executor_result.get("accounts") or [])
                if isinstance(item, dict)
            )
            return (
                f"外部目标仓位已同步 target={target_quantity}, signal={signal_version}, "
                f"executor={executor_result.get('status')}, orders={external_order_count}"
            )
        return f"外部目标仓位已同步 target={target_quantity}, signal={signal_version}"

    def _persist_run_result(
        self,
        *,
        config_id: int,
        account_id: str,
        symbol: str,
        trigger_source: str,
        action: str,
        status: str,
        message: str,
        state_values: SimpleNamespace,
        run_message: Optional[str] = None,
        price: Optional[float] = None,
        quantity: Optional[int] = None,
        fear_score: Optional[float] = None,
        volume_ratio: Optional[float] = None,
        position_ratio_before: Optional[float] = None,
        position_ratio_after: Optional[float] = None,
    ):
        def write_result():
            with get_db_ctx() as db:
                state = db.query(AStockFearStrategyState).filter(
                    AStockFearStrategyState.config_id == config_id
                ).first()
                if not state:
                    state = AStockFearStrategyState(
                        config_id=config_id,
                        account_id=account_id,
                        symbol=symbol,
                    )
                    db.add(state)
                state.account_id = account_id
                state.symbol = symbol
                state.last_processed_date = state_values.last_processed_date
                state.cooldown_remaining_days = int(state_values.cooldown_remaining_days or 0)
                state.greed_peak_price = state_values.greed_peak_price
                state.take_profit_cycle_sell_count = int(state_values.take_profit_cycle_sell_count or 0)

                db.add(
                    AStockFearStrategyLog(
                        config_id=config_id,
                        account_id=account_id,
                        symbol=symbol,
                        trigger_source=trigger_source,
                        action=action,
                        status=status,
                        price=price,
                        quantity=quantity,
                        fear_score=fear_score,
                        volume_ratio=volume_ratio,
                        position_ratio_before=position_ratio_before,
                        position_ratio_after=position_ratio_after,
                        message=message[:1000],
                    )
                )
                config = db.query(AStockFearStrategyConfig).filter(AStockFearStrategyConfig.id == config_id).first()
                if config:
                    config.last_run_at = datetime.now()
                    config.last_run_status = status
                    config.last_run_message = (run_message if run_message is not None else message)[:500]

        self._write_main_db_with_retry("persist A股 fear strategy run result", write_result)

    def _send_rebalance_notification(
        self,
        *,
        config_id: int,
        masked_account_id: str,
        symbol: str,
        trigger_source: str,
        action: str,
        quantity: int,
        price: float,
        position_ratio_before: float,
        position_ratio_after: float,
        fear_score: float,
        fear_label: str,
        signal_date: date,
        volume_ratio: float,
        trade_message: str,
    ):
        action_label = "买入" if action == "BUY" else "卖出"
        amount = float(quantity or 0) * float(price or 0)
        body = "\n".join([
            "A股情绪量能策略已产生调仓动作。",
            "",
            f"配置ID: {config_id}",
            f"账号: {masked_account_id}",
            f"标的: {symbol}",
            f"动作: {action_label} ({action})",
            f"数量: {quantity}",
            f"参考价格: {price:.4f}",
            f"估算金额: {amount:.2f}",
            f"仓位变化: {position_ratio_before:.2f}% -> {position_ratio_after:.2f}%",
            f"恐贪分数（{signal_date}）: {fear_score:.2f}",
            f"恐贪来源: {fear_label}",
            f"量比（{signal_date}）: {volume_ratio:.4f}",
            f"触发来源: {trigger_source}",
            "",
            f"执行信息: {trade_message}",
            f"通知时间: {datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)",
        ])
        send_configured_email(
            "a_stock_fear_strategy_rebalance_signal",
            f"A股情绪量能策略调仓提醒: {action_label} {symbol} {masked_account_id}#{config_id}",
            body,
        )

    async def run_config_once(
        self,
        config: AStockFearStrategyConfig,
        trigger_source: str = "auto",
        ignore_enabled: bool = False,
    ):
        config_id = config.id
        masked_account_id = mask_account_id(config.account_id)
        if not config.enabled and not ignore_enabled:
            return

        logger.info("Running A股 fear strategy for %s config=%s source=%s", masked_account_id, config_id, trigger_source)
        symbol = config.symbol or "510880.SH"
        try:
            if not config_id:
                raise ValueError("策略配置缺少配置ID")
            fear_source_key = config.fear_source or "a_stock_000015_sh"
            index_symbol = _fear_source_index_symbol(fear_source_key)
            if not index_symbol:
                raise ValueError(f"不支持的恐贪来源: {fear_source_key}")
            fear_label = _fear_source_label(fear_source_key)

            # 信号日 = 前一交易日（隔天信号）
            now = self._china_now()
            today = now.date()
            if not _is_china_trading_day(today):
                logger.info("A股情绪量能策略 %s 非交易日跳过 config=%s", masked_account_id, config_id)
                return
            signal_date = self._prev_china_trading_day(today)

            with get_db_ctx() as db:
                persisted_config = db.query(AStockFearStrategyConfig).filter(
                    AStockFearStrategyConfig.id == config_id
                ).first()
                if persisted_config:
                    config.buy_threshold = float(persisted_config.buy_threshold)
                    config.greed_threshold = float(persisted_config.greed_threshold)
                    config.volume_ratio_threshold = float(persisted_config.volume_ratio_threshold)
                    config.volume_z_threshold = (
                        float(persisted_config.volume_z_threshold)
                        if getattr(persisted_config, "volume_z_threshold", None) is not None
                        else None
                    )
                    config.sell_shrink_z = float(getattr(persisted_config, "sell_shrink_z", -1.0) or -1.0)
                    config.trailing_stop_pct = float(persisted_config.trailing_stop_pct)
                    config.buy_position_pct = float(persisted_config.buy_position_pct)
                    config.sell_position_pct = float(persisted_config.sell_position_pct)
                    config.cooldown_days = int(persisted_config.cooldown_days or 0)
                    config.max_take_profit_sells_per_cycle = int(persisted_config.max_take_profit_sells_per_cycle or 1)
                    config.min_position_pct_after_take_profit = float(persisted_config.min_position_pct_after_take_profit or 0)
                    config.rebalance_threshold_pct = float(persisted_config.rebalance_threshold_pct or 0)
                    config.sell_reduction_basis = persisted_config.sell_reduction_basis or "holdings"
                    config.sell_price_above_avg_cost = bool(persisted_config.sell_price_above_avg_cost)

                state_row = db.query(AStockFearStrategyState).filter(
                    AStockFearStrategyState.config_id == config_id
                ).first()
                state = SimpleNamespace(
                    config_id=config_id,
                    account_id=config.account_id,
                    symbol=symbol,
                    last_processed_date=getattr(state_row, "last_processed_date", None),
                    cooldown_remaining_days=int(getattr(state_row, "cooldown_remaining_days", 0) or 0),
                    greed_peak_price=getattr(state_row, "greed_peak_price", None),
                    take_profit_cycle_sell_count=int(getattr(state_row, "take_profit_cycle_sell_count", 0) or 0),
                )

            # 已完成数据：恐贪 + ETF 日线（含量比）
            lookback_start = signal_date - timedelta(days=60)
            fear_map = self._fetch_fear_map(index_symbol, lookback_start, signal_date)
            if signal_date not in fear_map:
                log_message = (
                    f"信号日 {signal_date} 缺少恐贪数据（{fear_label}），跳过。"
                    f"请确认 A股基础数据同步/恐贪回跑已完成"
                )
                self._persist_run_result(
                    config_id=config_id, account_id=config.account_id, symbol=symbol,
                    trigger_source=trigger_source, action="CHECK", status="SKIPPED",
                    message=log_message, state_values=state, run_message="数据未就绪",
                )
                return

            fear_score = float(fear_map[signal_date])
            volume_symbol = normalize_external_symbol(config.volume_signal_symbol or symbol) or symbol
            bars = self._fetch_etf_bars(volume_symbol, lookback_start, signal_date)
            use_log_z = getattr(config, "volume_z_threshold", None) is not None
            volume_ratio = self._volume_ratio_at(bars, signal_date)
            log_z = self._log_z_at(bars, signal_date) if use_log_z else None
            if volume_ratio is None or (use_log_z and log_z is None):
                log_message = f"信号日 {signal_date} 缺少 {volume_symbol} 量能数据（历史不足 {VOLUME_WINDOW} 日），跳过。"
                self._persist_run_result(
                    config_id=config_id, account_id=config.account_id, symbol=symbol,
                    trigger_source=trigger_source, action="CHECK", status="SKIPPED",
                    message=log_message, state_values=state, run_message="量能数据未就绪",
                )
                return

            # 跷跷板候补数据（可选）：候补恐贪 + 候补量比（独立阈值）
            sub_symbol = normalize_external_symbol(config.sub_symbol) if getattr(config, "sub_symbol", None) else None
            sub_fear_score = None
            sub_volume_ratio = None
            sub_log_z = None
            sub_fear_label = ""
            sub_bars = None
            sub_fear_map: Dict[date, float] = {}
            if sub_symbol:
                sub_index_symbol = _fear_source_index_symbol(getattr(config, "sub_fear_source", None) or "a_stock_000688_sh")
                if sub_index_symbol:
                    sub_fear_map = self._fetch_fear_map(sub_index_symbol, lookback_start, signal_date)
                    if signal_date in sub_fear_map:
                        sub_fear_score = float(sub_fear_map[signal_date])
                    sub_volume_symbol = normalize_external_symbol(config.sub_volume_signal_symbol or sub_symbol) or sub_symbol
                    sub_bars = self._fetch_etf_bars(sub_volume_symbol, lookback_start, signal_date)
                    sub_volume_ratio = self._volume_ratio_at(sub_bars, signal_date)
                    if use_log_z:
                        sub_log_z = self._log_z_at(sub_bars, signal_date)
                    else:
                        sub_log_z = None
                    sub_fear_label = _fear_source_label(getattr(config, "sub_fear_source", None) or "a_stock_000688_sh")

            # 第二候补数据（可选，三标的轮动）：恐贪 + 量比（独立阈值）
            sub2_symbol = normalize_external_symbol(config.sub2_symbol) if getattr(config, "sub2_symbol", None) else None
            sub2_fear_score = None
            sub2_volume_ratio = None
            sub2_log_z = None
            sub2_fear_label = ""
            sub2_bars = None
            sub2_fear_map: Dict[date, float] = {}
            if sub2_symbol:
                sub2_index_symbol = _fear_source_index_symbol(getattr(config, "sub2_fear_source", None) or "qqq_clone")
                if sub2_index_symbol:
                    sub2_fear_map = self._fetch_fear_map(sub2_index_symbol, lookback_start, signal_date)
                    if signal_date in sub2_fear_map:
                        sub2_fear_score = float(sub2_fear_map[signal_date])
                    sub2_volume_symbol = normalize_external_symbol(config.sub2_volume_signal_symbol or sub2_symbol) or sub2_symbol
                    sub2_bars = self._fetch_etf_bars(sub2_volume_symbol, lookback_start, signal_date)
                    sub2_volume_ratio = self._volume_ratio_at(sub2_bars, signal_date)
                    if use_log_z:
                        sub2_log_z = self._log_z_at(sub2_bars, signal_date)
                    else:
                        sub2_log_z = None
                    sub2_fear_label = _fear_source_label(getattr(config, "sub2_fear_source", None) or "qqq_clone")

            # 实时价格（主+候补+第二候补），hub quote + LongPort 兜底
            price_symbols = [symbol]
            if sub_symbol:
                price_symbols.append(sub_symbol)
            if sub2_symbol:
                price_symbols.append(sub2_symbol)
            price_map: Dict[str, float] = {}
            try:
                price_details = await get_realtime_price_details(
                    config.external_trading_account_id,
                    price_symbols,
                )
                for price_symbol in price_symbols:
                    detail = price_details.get(normalize_external_symbol(price_symbol))
                    candidate = external_safe_float(detail.get("price")) if detail else 0.0
                    if candidate > 0:
                        price_map[normalize_external_symbol(price_symbol)] = candidate
            except Exception as exc:
                logger.warning("A股情绪量能策略 %s 获取实时价失败: %s", masked_account_id, exc)

            snapshot = await self._build_snapshot(config, price_map)
            main_price = price_map.get(normalize_external_symbol(symbol), 0.0)
            sub_price = price_map.get(normalize_external_symbol(sub_symbol), 0.0) if sub_symbol else 0.0
            sub2_price = price_map.get(normalize_external_symbol(sub2_symbol), 0.0) if sub2_symbol else 0.0
            if main_price <= 0 and (not sub_symbol or sub_price <= 0) and (not sub2_symbol or sub2_price <= 0):
                log_message = "无法获取标的实时价格，跳过本次检查。"
                self._persist_run_result(
                    config_id=config_id, account_id=config.account_id, symbol=symbol,
                    trigger_source=trigger_source, action="CHECK", status="SKIPPED",
                    message=log_message, state_values=state, run_message=log_message,
                )
                return

            portfolio_value = float(snapshot.portfolio_value or 0)
            available_cash = float(snapshot.available_cash or 0)

            # 持仓推断：sub2 > sub > main > 空仓（子账户同时只持有其中一只）
            main_shares = int(snapshot.shares)
            sub_shares = int(snapshot.sub_shares or 0) if sub_symbol else 0
            sub2_shares = int(snapshot.sub2_shares or 0) if sub2_symbol else 0
            if sub2_shares > 0:
                holding = "sub2"
                holding_symbol = sub2_symbol
                shares = sub2_shares
                available_shares = int(snapshot.sub2_available_shares or sub2_shares)
                avg_cost = float(snapshot.sub2_avg_cost or 0)
                current_price = sub2_price
            elif sub_shares > 0:
                holding = "sub"
                holding_symbol = sub_symbol
                shares = sub_shares
                available_shares = int(snapshot.sub_available_shares or sub_shares)
                avg_cost = float(snapshot.sub_avg_cost or 0)
                current_price = sub_price
            elif main_shares > 0:
                holding = "main"
                holding_symbol = symbol
                shares = main_shares
                available_shares = int(snapshot.available_shares or main_shares)
                avg_cost = float(snapshot.avg_cost or 0)
                current_price = main_price
            else:
                holding = None
                holding_symbol = None
                shares = 0
                available_shares = 0
                avg_cost = 0.0
                current_price = main_price or sub_price or sub2_price
            position_value = shares * current_price if holding else 0.0
            position_ratio_before = (position_value / portfolio_value * 100) if portfolio_value > 0 else 0.0

            # 状态回填/止盈峰值用持仓标的的日线与恐贪
            if holding == "main":
                holding_bars, holding_fear_map = bars, fear_map
            elif holding == "sub":
                holding_bars, holding_fear_map = sub_bars, sub_fear_map
            elif holding == "sub2":
                holding_bars, holding_fear_map = sub2_bars, sub2_fear_map
            else:
                holding_bars, holding_fear_map = bars, fear_map

            # 跨日状态推进：冷却递减 + 止盈峰值回填（用贪恐区间的日线最高价）
            if state.last_processed_date and state.last_processed_date < signal_date:
                if state.cooldown_remaining_days > 0:
                    if holding_bars is not None and not holding_bars.empty:
                        gap_days = int(holding_bars[holding_bars["trade_date"] > state.last_processed_date]["trade_date"].nunique())
                        state.cooldown_remaining_days = max(0, state.cooldown_remaining_days - max(1, gap_days))
                    else:
                        state.cooldown_remaining_days = max(0, state.cooldown_remaining_days - 1)
                if shares > 0 and holding_bars is not None and not holding_bars.empty:
                    for _, row in holding_bars[holding_bars["trade_date"] > state.last_processed_date].iterrows():
                        day_fear = holding_fear_map.get(row["trade_date"])
                        day_high = float(row["high"]) if np.isfinite(row["high"]) else None
                        if day_fear is None or day_high is None:
                            continue
                        if day_fear >= float(config.greed_threshold):
                            state.greed_peak_price = max(float(state.greed_peak_price or day_high), day_high)
                        else:
                            state.greed_peak_price = None
                            state.take_profit_cycle_sell_count = 0
            elif shares <= 0:
                state.greed_peak_price = None
                state.take_profit_cycle_sell_count = 0

            # 信号日当天（signal_date）纳入止盈峰值
            signal_day_bars = holding_bars[holding_bars["trade_date"] == signal_date] if holding_bars is not None and not holding_bars.empty else None
            signal_day_high = None
            if signal_day_bars is not None and not signal_day_bars.empty:
                signal_day_high = float(signal_day_bars.iloc[0]["high"]) if np.isfinite(signal_day_bars.iloc[0]["high"]) else None
            if shares > 0 and holding:
                holding_fear = float(holding_fear_map.get(signal_date) or 0.0) if holding_fear_map.get(signal_date) is not None else None
                if holding_fear is not None and holding_fear >= float(config.greed_threshold):
                    state.greed_peak_price = max(float(state.greed_peak_price or signal_day_high or current_price), signal_day_high or current_price)
                else:
                    state.greed_peak_price = None
                    state.take_profit_cycle_sell_count = 0
            state.last_processed_date = signal_date

            # 信号：主标的 + 候补（独立阈值）；放量统一用 log-z（volume_z_threshold）或旧量比
            if use_log_z:
                main_vol_ok = log_z is not None and log_z >= float(config.volume_z_threshold)
            else:
                main_vol_ok = volume_ratio is not None and volume_ratio >= float(config.volume_ratio_threshold)
            main_signal = (
                fear_score <= float(config.buy_threshold)
                and main_vol_ok
            )
            main_greedy = fear_score >= float(config.greed_threshold)
            sub_vol_ok = False
            if sub_symbol is not None and sub_volume_ratio is not None:
                if use_log_z:
                    sub_vol_ok = sub_log_z is not None and sub_log_z >= float(config.volume_z_threshold)
                else:
                    sub_vol_ok = sub_volume_ratio >= float(getattr(config, "sub_volume_ratio_threshold", 1.6) or 1.6)
            sub_signal = (
                sub_symbol is not None
                and sub_fear_score is not None
                and sub_fear_score <= float(getattr(config, "sub_buy_threshold", 25.0) or 25.0)
                and sub_vol_ok
            )
            sub_greedy = sub_symbol is not None and sub_fear_score is not None and sub_fear_score >= float(config.greed_threshold)
            # 第二候补信号（三标的轮动）
            sub2_vol_ok = False
            if sub2_symbol is not None and sub2_volume_ratio is not None:
                if use_log_z:
                    sub2_vol_ok = sub2_log_z is not None and sub2_log_z >= float(config.volume_z_threshold)
                else:
                    sub2_vol_ok = sub2_volume_ratio >= float(getattr(config, "sub2_volume_ratio_threshold", 1.3) or 1.3)
            sub2_signal = (
                sub2_symbol is not None
                and sub2_fear_score is not None
                and sub2_fear_score <= float(getattr(config, "sub2_buy_threshold", 20.0) or 20.0)
                and sub2_vol_ok
            )
            sub2_greedy = sub2_symbol is not None and sub2_fear_score is not None and sub2_fear_score >= float(config.greed_threshold)
            # 对称双轮动：换仓阈值非空时启用（恐贪超过阈值且另一标的有信号则换仓；空仓任一触发都买更恐慌的）
            use_swap = getattr(config, "swap_threshold", None) is not None
            swap_value = float(config.swap_threshold) if use_swap else None
            can_trade = state.cooldown_remaining_days <= 0
            position_ratio_after = position_ratio_before
            order_action = None
            order_symbol = None
            order_target_symbol = None
            order_quantity = 0
            order_message_template = None
            trade_action = None
            trade_quantity = 0
            trade_message = ""
            log_action = "CHECK"
            status = "INFO"

            if snapshot.has_today_order:
                trade_message = "今日已存在订单，跳过重复执行"
                log_action = "SKIP"
                status = "SKIPPED"

            # 三标的换仓目标选择：其他有信号标的中恐贪最低的
            def _pick_swap_target(held_key: str):
                candidates = []
                if held_key != "main" and main_signal and main_price > 0:
                    candidates.append(("main", symbol, main_price, fear_label, fear_score))
                if held_key != "sub" and sub_signal and sub_price > 0 and sub_fear_score is not None:
                    candidates.append(("sub", sub_symbol, sub_price, sub_fear_label, sub_fear_score))
                if held_key != "sub2" and sub2_signal and sub2_price > 0 and sub2_fear_score is not None:
                    candidates.append(("sub2", sub2_symbol, sub2_price, sub2_fear_label, sub2_fear_score))
                return min(candidates, key=lambda c: c[4]) if candidates else None

            # ---- 状态机：持有主/持有候补/空仓（use_swap=对称双轮动，否则主辅跷跷板） ----
            if log_action != "SKIP" and can_trade and shares > 0 and holding:
                # 缩量卖出确认（仅贪即卖 trailing=0 路径生效）：sell_shrink_z>0 时需持仓标的前一交易日缩量
                shrink_sell_ok = True
                if float(getattr(config, "sell_shrink_z", -1.0) or -1.0) > 0:
                    shrink_log_z = log_z if holding == "main" else (sub_log_z if holding == "sub" else sub2_log_z)
                    shrink_sell_ok = shrink_log_z is not None and shrink_log_z <= -float(getattr(config, "sell_shrink_z", -1.0))
                if holding == "main" and main_greedy:
                    drawdown_from_peak = 0.0
                    drawdown_reached = True
                    trailing_reason = "到达贪恐阈值即卖（移动止盈=0）"
                    if float(config.trailing_stop_pct) > 0:
                        drawdown_from_peak = (
                            (float(state.greed_peak_price) - current_price) / float(state.greed_peak_price) * 100
                            if float(state.greed_peak_price) > 0 else 0.0
                        )
                        drawdown_reached = drawdown_from_peak >= float(config.trailing_stop_pct)
                        trailing_reason = f"较止盈峰值回撤 {drawdown_from_peak:.2f}% 触发移动止盈"
                    elif not shrink_sell_ok:
                        drawdown_reached = False
                        trailing_reason = "到达贪恐阈值但未缩量，等待缩量确认"
                    sell_price_guard_passed = (not config.sell_price_above_avg_cost) or current_price > avg_cost
                    min_hold_shares = (
                        ceil(portfolio_value * (float(config.min_position_pct_after_take_profit) / 100.0) / current_price)
                        if portfolio_value > 0 and current_price > 0 else 0
                    )
                    if str(config.sell_reduction_basis or "portfolio") == "portfolio":
                        trade_quantity = floor(portfolio_value * (float(config.sell_position_pct) / 100.0) / current_price)
                    else:
                        trade_quantity = floor(available_shares * (float(config.sell_position_pct) / 100.0))
                    trade_quantity = min(int(trade_quantity), int(available_shares))
                    if available_shares - trade_quantity < min_hold_shares:
                        trade_quantity = max(0, available_shares - min_hold_shares)
                    sell_amount = trade_quantity * current_price
                    trade_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                    if drawdown_reached and sell_price_guard_passed and position_ratio_before > float(config.min_position_pct_after_take_profit):
                        if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                            order_action = "SELL"
                            order_symbol = holding_symbol
                            order_quantity = trade_quantity
                            order_message_template = (
                                f"{fear_label} {fear_score:.2f}（{signal_date}）进入止盈区，"
                                f"{trailing_reason}，订单ID={{order_id}}"
                            )
                            position_ratio_after = max(0.0, ((shares - trade_quantity) * current_price / portfolio_value * 100) if portfolio_value > 0 else 0.0)
                        else:
                            trade_message = "卖出信号成立，但可卖数量过小或未达到调仓阈值"
                    else:
                        trade_message = (
                            f"处于止盈区，等待触发。回撤 {drawdown_from_peak:.2f}%"
                            if drawdown_reached else f"处于止盈区，未触发（回撤 {drawdown_from_peak:.2f}% < {config.trailing_stop_pct:.2f}%）"
                        )
                elif holding == "main" and use_swap and not order_action:
                    # 对称双轮动：主标的恐贪>换仓阈值 且 任一其他标的有信号 → 换到恐贪最低的
                    if fear_score > swap_value:
                        target = _pick_swap_target("main")
                        if target:
                            t_key, t_symbol, t_price, t_label, t_fear = target
                            sell_quantity = int(available_shares)
                            sell_amount = sell_quantity * current_price
                            trade_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                            if sell_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                                order_action = "SELL_AND_BUY"
                                order_symbol = holding_symbol
                                order_target_symbol = t_symbol
                                order_quantity = sell_quantity
                                order_message_template = (
                                    f"{fear_label} {fear_score:.2f} 恐贪>{swap_value:g} 且 {t_label} 出买入信号，"
                                    f"卖出 {holding_symbol} 换到 {t_symbol}，订单ID={{order_id}}"
                                )
                            else:
                                trade_message = "换仓信号成立，但可卖数量过小或未达到调仓阈值"
                elif holding == "sub":
                    if use_swap:
                        # 对称双轮动：候补贪恐>=卖出阈值 → 卖；候补恐贪>换仓阈值 且 主有信号 → 换主
                        if sub_greedy and shrink_sell_ok:
                            order_action = "SELL"
                            order_symbol = holding_symbol
                            order_quantity = int(available_shares)
                            order_message_template = (
                                f"{sub_fear_label} {sub_fear_score:.2f}（{signal_date}）候补到达贪恐阈值即卖，"
                                f"保持空仓，订单ID={{order_id}}"
                            )
                            position_ratio_after = 0.0
                        elif (
                            sub_fear_score is not None
                            and sub_fear_score > swap_value
                        ):
                            # 三标的：换到恐贪最低的其他有信号标的
                            target = _pick_swap_target("sub")
                            if target:
                                t_key, t_symbol, t_price, t_label, t_fear = target
                                sell_quantity = int(available_shares)
                                sell_amount = sell_quantity * current_price
                                trade_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                                if sell_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                                    order_action = "SELL_AND_BUY"
                                    order_symbol = holding_symbol
                                    order_target_symbol = t_symbol
                                    order_quantity = sell_quantity
                                    order_message_template = (
                                        f"{sub_fear_label} {sub_fear_score:.2f} 恐贪>{swap_value:g} 且 {t_label} 出买入信号，"
                                        f"卖出候补 {holding_symbol} 换到 {t_symbol}，订单ID={{order_id}}"
                                    )
                                else:
                                    trade_message = "换仓信号成立，但可卖数量过小或未达到调仓阈值"
                    elif main_signal:
                        # 主标的出信号：卖出候补换回主标的（先卖后买，同日完成）
                        min_hold_shares = 0
                        sell_quantity = int(available_shares)
                        sell_amount = sell_quantity * current_price
                        trade_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                        if sell_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                            order_action = "SELL_AND_BUY"
                            order_symbol = holding_symbol
                            order_target_symbol = symbol
                            order_quantity = sell_quantity
                            order_message_template = (
                                f"主标的出信号（{fear_label} {fear_score:.2f} + 量比 {volume_ratio:.2f}），"
                                f"卖出候补 {holding_symbol} 换回主标的，订单ID={{order_id}}"
                            )
                        else:
                            trade_message = "换仓信号成立，但可卖数量过小或未达到调仓阈值"
                    elif sub_greedy:
                        drawdown_from_peak = 0.0
                        drawdown_reached = True
                        trailing_reason = "到达贪恐阈值即卖（移动止盈=0）"
                        if float(config.trailing_stop_pct) > 0:
                            drawdown_from_peak = (
                                (float(state.greed_peak_price) - current_price) / float(state.greed_peak_price) * 100
                                if float(state.greed_peak_price) > 0 else 0.0
                            )
                            drawdown_reached = drawdown_from_peak >= float(config.trailing_stop_pct)
                            trailing_reason = f"较止盈峰值回撤 {drawdown_from_peak:.2f}% 触发移动止盈"
                        sell_price_guard_passed = (not config.sell_price_above_avg_cost) or current_price > avg_cost
                        trade_quantity = int(available_shares)
                        sell_amount = trade_quantity * current_price
                        trade_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                        if drawdown_reached and sell_price_guard_passed and position_ratio_before > float(config.min_position_pct_after_take_profit):
                            if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                                order_action = "SELL"
                                order_symbol = holding_symbol
                                order_quantity = trade_quantity
                                order_message_template = (
                                    f"{sub_fear_label} {sub_fear_score:.2f}（{signal_date}）候补到达贪恐阈值即卖，"
                                    f"保持空仓，订单ID={{order_id}}"
                                )
                                position_ratio_after = 0.0
                            else:
                                trade_message = "候补卖出信号成立，但可卖数量过小或未达到调仓阈值"
                        else:
                            trade_message = "候补处于止盈区，等待触发"
                elif holding == "sub2":
                    if use_swap:
                        # 三标的对称：第二候补贪恐>=卖出阈值 → 卖；恐贪>换仓阈值 且 其他有信号 → 换仓
                        if sub2_greedy and shrink_sell_ok:
                            order_action = "SELL"
                            order_symbol = holding_symbol
                            order_quantity = int(available_shares)
                            order_message_template = (
                                f"{sub2_fear_label} {sub2_fear_score:.2f}（{signal_date}）第二候补到达贪恐阈值即卖，"
                                f"保持空仓，订单ID={{order_id}}"
                            )
                            position_ratio_after = 0.0
                        elif sub2_fear_score is not None and sub2_fear_score > swap_value:
                            target = _pick_swap_target("sub2")
                            if target:
                                t_key, t_symbol, t_price, t_label, t_fear = target
                                sell_quantity = int(available_shares)
                                sell_amount = sell_quantity * current_price
                                trade_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                                if sell_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                                    order_action = "SELL_AND_BUY"
                                    order_symbol = holding_symbol
                                    order_target_symbol = t_symbol
                                    order_quantity = sell_quantity
                                    order_message_template = (
                                        f"{sub2_fear_label} {sub2_fear_score:.2f} 恐贪>{swap_value:g} 且 {t_label} 出买入信号，"
                                        f"卖出 {holding_symbol} 换到 {t_symbol}，订单ID={{order_id}}"
                                    )
                                else:
                                    trade_message = "换仓信号成立，但可卖数量过小或未达到调仓阈值"
            elif log_action != "SKIP" and can_trade and not order_action:
                # 空仓：对称双轮动=谁触发买谁（都触发买恐贪最低的）；主辅跷跷板=主标的优先
                if use_swap:
                    cands = []
                    if main_signal and main_price > 0:
                        cands.append((symbol, main_price, fear_label, fear_score))
                    if sub_signal and sub_price > 0 and sub_fear_score is not None:
                        cands.append((sub_symbol, sub_price, sub_fear_label, sub_fear_score))
                    if sub2_signal and sub2_price > 0 and sub2_fear_score is not None:
                        cands.append((sub2_symbol, sub2_price, sub2_fear_label, sub2_fear_score))
                    if cands:
                        t_symbol, t_price, t_label, t_fear = min(cands, key=lambda c: c[3])
                        buy_amount = portfolio_value * (float(config.buy_position_pct) / 100.0)
                        trade_quantity = min(floor(buy_amount / t_price), floor(available_cash / t_price))
                        actual_buy_amount = trade_quantity * t_price
                        trade_pct = (actual_buy_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                        if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                            order_action = "BUY"
                            order_symbol = t_symbol
                            order_quantity = trade_quantity
                            order_message_template = (
                                f"多标的都触发，{t_label} 恐贪 {t_fear:.2f} 更恐慌，"
                                f"买入 {t_symbol}，订单ID={{order_id}}"
                            )
                            position_ratio_after = (trade_quantity * t_price / portfolio_value * 100) if portfolio_value > 0 else position_ratio_before
                        else:
                            trade_message = "买入信号成立，但可买数量过小或未达到调仓阈值"
                elif main_signal:
                    buy_amount = portfolio_value * (float(config.buy_position_pct) / 100.0)
                    trade_quantity = min(floor(buy_amount / main_price), floor(available_cash / main_price))
                    actual_buy_amount = trade_quantity * main_price
                    trade_pct = (actual_buy_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                    if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                        order_action = "BUY"
                        order_symbol = symbol
                        order_quantity = trade_quantity
                        order_message_template = (
                            f"{fear_label} {fear_score:.2f}（{signal_date}）进入买入区，"
                            f"量比 {volume_ratio:.2f} >= {config.volume_ratio_threshold:.2f}，订单ID={{order_id}}"
                        )
                        position_ratio_after = (trade_quantity * main_price / portfolio_value * 100) if portfolio_value > 0 else position_ratio_before
                    else:
                        trade_message = "主标的买入信号成立，但可买数量过小或未达到调仓阈值"
                elif sub_signal and sub_price > 0:
                    buy_amount = portfolio_value * (float(config.buy_position_pct) / 100.0)
                    trade_quantity = min(floor(buy_amount / sub_price), floor(available_cash / sub_price))
                    actual_buy_amount = trade_quantity * sub_price
                    trade_pct = (actual_buy_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                    if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                        order_action = "BUY"
                        order_symbol = sub_symbol
                        order_quantity = trade_quantity
                        order_message_template = (
                            f"主标的空仓，{sub_fear_label} {sub_fear_score:.2f}（{signal_date}）极恐放量，"
                            f"买入候补 {sub_symbol}（量比 {sub_volume_ratio:.2f}），订单ID={{order_id}}"
                        )
                        position_ratio_after = (trade_quantity * sub_price / portfolio_value * 100) if portfolio_value > 0 else position_ratio_before
                    else:
                        trade_message = "候补买入信号成立，但可买数量过小或未达到调仓阈值"

            if log_action != "SKIP" and not order_action and not trade_message:
                if not can_trade:
                    trade_message = f"处于冷却期，剩余 {state.cooldown_remaining_days} 个交易日"
                elif main_signal:
                    trade_message = f"{fear_label} 进入买入区（但持仓中或条件未满足）"
                elif main_greedy:
                    if shares <= 0:
                        trade_message = "处于止盈区，但未持有标的，跳过止盈"
                    elif state.take_profit_cycle_sell_count >= int(config.max_take_profit_sells_per_cycle):
                        trade_message = "处于止盈区，但本轮止盈次数已达上限"
                    else:
                        trade_message = "处于止盈区，但尚未触发移动止盈"
                elif sub_signal:
                    trade_message = f"候补 {sub_symbol} 进入买入区，但主标的信号优先/条件未满足"
                else:
                    trade_message = "当前无买卖信号"

            if order_action:
                if order_action == "SELL_AND_BUY":
                    # 换仓：先同步卖出当前持仓 + 买入目标（不触发），再统一触发一次 executor
                    # （避免卖出未成交时第二次 executor 因资金不足被拒；executor 按目标仓位先卖后买，T+0 资金衔接）
                    sell_id = await self._sync_target_order(
                        config, snapshot, "SELL", order_quantity, current_price,
                        trigger_source=trigger_source, symbol=order_symbol, trigger_executor=False,
                    )
                    target_symbol = order_target_symbol or symbol
                    if target_symbol == symbol:
                        target_price = main_price
                    elif sub2_symbol and target_symbol == sub2_symbol:
                        target_price = sub2_price
                    else:
                        target_price = sub_price
                    buy_cash = available_cash + order_quantity * current_price
                    buy_quantity = 0
                    buy_id = None
                    if target_price > 0:
                        buy_quantity = min(
                            floor(portfolio_value * (float(config.buy_position_pct) / 100.0) / target_price),
                            floor(buy_cash / target_price),
                        )
                        if buy_quantity >= 1:
                            buy_id = await self._sync_target_order(
                                config, snapshot, "BUY", buy_quantity, target_price,
                                trigger_source=trigger_source, symbol=target_symbol, trigger_executor=False,
                            )
                    executor_result = await trigger_external_trading_executor(
                        account_id=config.account_id,
                        external_account_id=snapshot.external_trading_account_id,
                        trigger_source=f"a_stock_fear_{trigger_source}_swap",
                    )
                    external_order_count = sum(
                        int(item.get("external_order_count") or 0)
                        for item in (executor_result.get("accounts") or [])
                        if isinstance(item, dict)
                    )
                    order_id = (
                        f"SELL={sell_id}; BUY={buy_id or '0'}; "
                        f"executor={executor_result.get('status')}, orders={external_order_count}"
                    )
                    trade_action = "SELL"
                    trade_quantity = order_quantity
                    state.cooldown_remaining_days = int(config.cooldown_days)
                    state.greed_peak_price = None
                    state.take_profit_cycle_sell_count = 0
                else:
                    order_id = await self._sync_target_order(
                        config,
                        snapshot,
                        order_action,
                        order_quantity,
                        current_price,
                        trigger_source=trigger_source,
                        symbol=order_symbol,
                    )
                    trade_action = order_action
                    trade_quantity = order_quantity
                    state.cooldown_remaining_days = int(config.cooldown_days)
                    if trade_action == "SELL":
                        state.take_profit_cycle_sell_count += 1
                        if shares - trade_quantity <= 0 or state.take_profit_cycle_sell_count >= int(config.max_take_profit_sells_per_cycle):
                            state.greed_peak_price = None
                        else:
                            state.greed_peak_price = current_price
                    else:
                        state.greed_peak_price = None
                        state.take_profit_cycle_sell_count = 0
                trade_message = order_message_template.format(order_id=order_id)
                status = "SUCCESS"

            # 实际交易标的与对应恐贪信息（主/候补）
            trade_symbol = order_symbol or holding_symbol or symbol
            if order_symbol == sub_symbol:
                trade_fear = sub_fear_score if sub_fear_score is not None else fear_score
                trade_vr = sub_volume_ratio if sub_volume_ratio is not None else volume_ratio
                trade_fear_label = sub_fear_label or fear_label
            elif sub2_symbol and order_symbol == sub2_symbol:
                trade_fear = sub2_fear_score if sub2_fear_score is not None else fear_score
                trade_vr = sub2_volume_ratio if sub2_volume_ratio is not None else volume_ratio
                trade_fear_label = sub2_fear_label or fear_label
            else:
                trade_fear = fear_score
                trade_vr = volume_ratio
                trade_fear_label = fear_label

            log_message = (
                f"{trade_message} | fear_score={trade_fear:.2f} | fear_date={signal_date}"
                f" | volume_ratio={trade_vr:.4f} | price={current_price:.4f} | symbol={trade_symbol}"
            )
            self._persist_run_result(
                config_id=config_id,
                account_id=config.account_id,
                symbol=trade_symbol,
                trigger_source=trigger_source,
                action=trade_action or log_action,
                status=status,
                message=log_message,
                state_values=state,
                run_message=trade_message,
                price=current_price,
                quantity=trade_quantity if trade_action else None,
                fear_score=trade_fear,
                volume_ratio=trade_vr,
                position_ratio_before=position_ratio_before,
                position_ratio_after=position_ratio_after,
            )
            logger.info("A股 fear strategy %s config=%s result=%s msg=%s", masked_account_id, config_id, trade_action or "CHECK", trade_message)
            if trade_action:
                self._send_rebalance_notification(
                    config_id=config_id,
                    masked_account_id=masked_account_id,
                    symbol=trade_symbol,
                    trigger_source=trigger_source,
                    action=trade_action,
                    quantity=trade_quantity,
                    price=current_price,
                    position_ratio_before=position_ratio_before,
                    position_ratio_after=position_ratio_after,
                    fear_score=trade_fear,
                    fear_label=trade_fear_label,
                    signal_date=signal_date,
                    volume_ratio=trade_vr,
                    trade_message=trade_message,
                )
        except Exception as exc:
            logger.error("A股 fear strategy failed for %s config=%s: %s", masked_account_id, config_id, exc, exc_info=True)
            error_message = f"执行失败: {exc}"
            self._append_error_log(config_id, config, symbol, trigger_source, error_message)
            self._update_run_status(config_id, "ERROR", error_message)
            send_alert_email(
                f"A股情绪量能自动交易报错: {masked_account_id}#{config_id}",
                f"Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
                scenario_key="a_stock_fear_strategy_error",
            )
        finally:
            publish_event(config.account_id, "a_stock_fear_strategy_run", {"config_id": config_id})

    def _append_error_log(self, config_id: int, config, symbol: str, trigger_source: str, error_message: str):
        def write_log():
            with get_db_ctx() as db:
                db.add(
                    AStockFearStrategyLog(
                        config_id=config_id,
                        account_id=config.account_id,
                        symbol=symbol,
                        trigger_source=trigger_source,
                        action="ERROR",
                        status="ERROR",
                        message=error_message[:1000],
                    )
                )
        self._write_main_db_with_retry("append A股 fear strategy error log", write_log)

    def _update_run_status(self, config_id: int, status: str, message: str):
        def write_status():
            with get_db_ctx() as db:
                config = db.query(AStockFearStrategyConfig).filter(AStockFearStrategyConfig.id == config_id).first()
                if config:
                    config.last_run_at = datetime.now()
                    config.last_run_status = status
                    config.last_run_message = message[:500]
        self._write_main_db_with_retry("update A股 fear strategy run status", write_status)

    async def run_config_id_once(
        self,
        config_id: int,
        account_id: Optional[str] = None,
        trigger_source: str = "manual",
        ignore_enabled: bool = True,
    ):
        with get_db_ctx() as db:
            query = db.query(AStockFearStrategyConfig).filter(AStockFearStrategyConfig.id == config_id)
            if account_id:
                query = query.filter(AStockFearStrategyConfig.account_id == account_id)
            config = query.first()
            if not config:
                raise ValueError("未找到 A股情绪量能策略配置")
            db.expunge(config)
        await self.run_config_once(config, trigger_source=trigger_source, ignore_enabled=ignore_enabled)

    async def run_all_enabled_once(self, trigger_source: str = "auto"):
        with get_db_ctx() as db:
            configs = db.query(AStockFearStrategyConfig).filter(
                AStockFearStrategyConfig.enabled == True  # noqa: E712
            ).all()
            for config in configs:
                db.expunge(config)
        for config in configs:
            await self.run_config_once(config, trigger_source=trigger_source, ignore_enabled=False)

    def _next_run_at(self, now: datetime, run_time: dtime) -> datetime:
        """下一个 A 股交易日的 run_time 时刻。"""
        candidate = datetime.combine(now.date(), run_time, tzinfo=SHANGHAI_TZ)
        if candidate > now:
            return candidate
        next_day = now.date() + timedelta(days=1)
        while not _is_china_trading_day(next_day):
            next_day += timedelta(days=1)
        return datetime.combine(next_day, run_time, tzinfo=SHANGHAI_TZ)

    async def worker_loop(self):
        logger.info("A股 fear strategy trader loop started")
        while True:
            try:
                now = self._china_now()
                triggered_any = False
                if _is_china_trading_day(now.date()):
                    with get_db_ctx() as db:
                        configs = db.query(AStockFearStrategyConfig).filter(
                            AStockFearStrategyConfig.enabled == True  # noqa: E712
                        ).all()
                        for config in configs:
                            db.expunge(config)
                    for config in configs:
                        run_time = self._parse_run_time(config.run_time)
                        window_start = datetime.combine(now.date(), run_time, tzinfo=SHANGHAI_TZ)
                        if (
                            now >= window_start
                            and now < window_start + timedelta(minutes=5)
                            and self._last_auto_trigger_date.get(config.id) != now.date()
                        ):
                            self._last_auto_trigger_date[config.id] = now.date()
                            await self.run_config_once(config, trigger_source="auto", ignore_enabled=False)
                            triggered_any = True

                if triggered_any:
                    await asyncio.sleep(60)
                    continue

                # 睡到最近的 run_time 事件（或最长 1 小时）
                sleep_seconds = 3600.0
                if _is_china_trading_day(now.date()):
                    with get_db_ctx() as db:
                        configs = db.query(AStockFearStrategyConfig).filter(
                            AStockFearStrategyConfig.enabled == True  # noqa: E712
                        ).all()
                        for config in configs:
                            db.expunge(config)
                    for config in configs:
                        run_time = self._parse_run_time(config.run_time)
                        next_run = self._next_run_at(now, run_time)
                        seconds = max(0.0, (next_run - now).total_seconds())
                        sleep_seconds = min(sleep_seconds, seconds)
                await asyncio.sleep(min(max(sleep_seconds, 5.0), 3600.0))
            except Exception as exc:
                logger.error("A股 fear strategy worker loop error: %s", exc, exc_info=True)
                send_alert_email(
                    "A股情绪量能自动交易主循环异常",
                    f"Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
                    scenario_key="a_stock_fear_strategy_error",
                )
                await asyncio.sleep(60)

    def trigger_manual_run(self, config_id: int, account_id: Optional[str] = None):
        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    self.run_config_id_once(config_id, account_id, trigger_source="manual", ignore_enabled=True)
                )
            finally:
                loop.close()

        thread_label = f"{mask_account_id(account_id)}#{config_id}" if account_id else f"config-{config_id}"
        thread = threading.Thread(target=runner, daemon=True, name=f"AStockFearManual-{thread_label}")
        thread.start()


def start_a_stock_fear_strategy_trader():
    trader = AStockFearStrategyTrader()
    with trader._thread_lock:
        if trader._thread_started:
            logger.info("A股 fear strategy trader already started, skipping.")
            return
        trader._thread_started = True

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trader.worker_loop())

    thread = threading.Thread(target=runner, daemon=True, name="AStockFearStrategyTrader")
    thread.start()
    logger.info("A股 fear strategy trader thread started")
