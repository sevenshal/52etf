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
    """恐贪来源 key（如 a_stock_000015_sh）→ 指数标的（000015.SH）。"""
    key = (fear_source or "").strip().lower()
    if not key.startswith("a_stock_"):
        return None
    return key.removeprefix("a_stock_").replace("_", ".").upper()


def _fear_source_label(fear_source: str) -> str:
    key = (fear_source or "").strip().lower()
    symbol = _fear_source_index_symbol(key)
    if not symbol:
        return "贪恐"
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
        """ETF 日线（前复权，a_stock_fund_daily_qfq）。"""
        normalized = normalize_external_symbol(symbol) or symbol
        try:
            connection = connect_duckdb(prefer_read_only=True)
        except Exception as exc:
            logger.warning("A股情绪量能策略无法连接分析库: %s", exc)
            return pd.DataFrame()
        try:
            frame = connection.execute(
                """
                SELECT trade_date, open, high, low, close, volume
                FROM a_stock_fund_daily_qfq
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

    async def _build_snapshot(self, config: SimpleNamespace, current_price: float) -> SimpleNamespace:
        """外部交易子账户快照（仅 A 股外部账户）。"""
        if not config.external_trading_account_id or not config.live_sub_account_id:
            raise ValueError("未选择外部交易账户或虚拟子账户")
        symbol = normalize_external_symbol(config.symbol)
        if not symbol:
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
            position = positions.get(symbol)
            shares = external_safe_int(getattr(position, "quantity", 0))
            available_shares = external_safe_int(getattr(position, "available_quantity", shares), shares)
            avg_cost = external_safe_float(getattr(position, "avg_cost", 0.0))

            try:
                valuation = await calculate_sub_account_net_asset(db, sub_account)
                available_cash = external_safe_float(valuation.get("cash_available"))
                portfolio_value = external_safe_float(valuation.get("net_asset"))
                if portfolio_value <= 0:
                    portfolio_value = available_cash + shares * current_price
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
                    if row_symbol == symbol and row_price <= 0:
                        row_price = current_price
                    holdings_value += quantity * max(row_price, 0.0)
                portfolio_value = available_cash + holdings_value

            has_open_order = db.query(ExternalTradingOrder).filter(
                ExternalTradingOrder.account_id == config.account_id,
                ExternalTradingOrder.external_trading_account_id == account.id,
                ExternalTradingOrder.sub_account_id == sub_account.id,
                ExternalTradingOrder.symbol == symbol,
                ExternalTradingOrder.status.in_(list(ACTIVE_ORDER_STATUSES)),
            ).first() is not None

            return SimpleNamespace(
                config_id=config.config_id,
                account_id=config.account_id,
                symbol=symbol,
                shares=max(0, shares),
                available_shares=max(0, available_shares),
                avg_cost=avg_cost,
                current_price=max(current_price, 0.0),
                available_cash=available_cash,
                portfolio_value=max(portfolio_value, available_cash + max(0, shares) * current_price, 1.0),
                has_today_order=has_open_order,
                external_trading_account_id=account.id,
                live_sub_account_id=sub_account.id,
            )

    async def _sync_target_order(
        self,
        config: SimpleNamespace,
        snapshot: SimpleNamespace,
        action: str,
        quantity: int,
        price: float,
        trigger_source: str,
    ) -> str:
        symbol = normalize_external_symbol(config.symbol)
        if not symbol:
            raise ValueError("交易标的格式不正确")
        if action == "BUY":
            target_quantity = int(snapshot.shares) + int(quantity)
        elif action == "SELL":
            target_quantity = max(0, int(snapshot.shares) - int(quantity))
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
            volume_ratio = self._volume_ratio_at(bars, signal_date)
            if volume_ratio is None:
                log_message = f"信号日 {signal_date} 缺少 {volume_symbol} 量比数据（历史不足 {VOLUME_WINDOW} 日），跳过。"
                self._persist_run_result(
                    config_id=config_id, account_id=config.account_id, symbol=symbol,
                    trigger_source=trigger_source, action="CHECK", status="SKIPPED",
                    message=log_message, state_values=state, run_message="量比数据未就绪",
                )
                return

            # 实时价格（用于下单数量），hub quote + LongPort 兜底
            current_price = None
            try:
                price_details = await get_realtime_price_details(
                    config.external_trading_account_id,
                    [symbol],
                )
                detail = price_details.get(normalize_external_symbol(symbol))
                candidate = external_safe_float(detail.get("price")) if detail else 0.0
                if candidate > 0:
                    current_price = candidate
            except Exception as exc:
                logger.warning("A股情绪量能策略 %s 获取实时价失败: %s", masked_account_id, exc)

            snapshot = await self._build_snapshot(config, current_price or 0.0)
            current_price = snapshot.current_price
            if current_price <= 0:
                log_message = "无法获取标的实时价格，跳过本次检查。"
                self._persist_run_result(
                    config_id=config_id, account_id=config.account_id, symbol=symbol,
                    trigger_source=trigger_source, action="CHECK", status="SKIPPED",
                    message=log_message, state_values=state, run_message=log_message,
                )
                return

            shares = int(snapshot.shares)
            available_shares = int(snapshot.available_shares)
            avg_cost = float(snapshot.avg_cost or 0)
            portfolio_value = float(snapshot.portfolio_value or 0)
            available_cash = float(snapshot.available_cash or 0)
            position_value = shares * current_price
            position_ratio_before = (position_value / portfolio_value * 100) if portfolio_value > 0 else 0.0

            # 跨日状态推进：冷却递减 + 止盈峰值回填（用贪恐区间的日线最高价）
            if state.last_processed_date and state.last_processed_date < signal_date:
                if state.cooldown_remaining_days > 0:
                    if bars is not None and not bars.empty:
                        gap_days = int(bars[bars["trade_date"] > state.last_processed_date]["trade_date"].nunique())
                        state.cooldown_remaining_days = max(0, state.cooldown_remaining_days - max(1, gap_days))
                    else:
                        state.cooldown_remaining_days = max(0, state.cooldown_remaining_days - 1)
                if shares > 0 and bars is not None and not bars.empty:
                    for _, row in bars[bars["trade_date"] > state.last_processed_date].iterrows():
                        day_fear = fear_map.get(row["trade_date"])
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
            signal_day_bars = bars[bars["trade_date"] == signal_date] if bars is not None and not bars.empty else None
            signal_day_high = None
            if signal_day_bars is not None and not signal_day_bars.empty:
                signal_day_high = float(signal_day_bars.iloc[0]["high"]) if np.isfinite(signal_day_bars.iloc[0]["high"]) else None
            if shares > 0:
                if fear_score >= float(config.greed_threshold):
                    state.greed_peak_price = max(float(state.greed_peak_price or signal_day_high or current_price), signal_day_high or current_price)
                else:
                    state.greed_peak_price = None
                    state.take_profit_cycle_sell_count = 0
            state.last_processed_date = signal_date

            is_fear = fear_score <= float(config.buy_threshold)
            is_greedy = fear_score >= float(config.greed_threshold)
            can_trade = state.cooldown_remaining_days <= 0
            position_ratio_after = position_ratio_before
            order_action = None
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

            # ---- 卖出 ----
            if (
                log_action != "SKIP"
                and shares > 0
                and can_trade
                and is_greedy
                and state.greed_peak_price
                and state.take_profit_cycle_sell_count < int(config.max_take_profit_sells_per_cycle)
            ):
                if float(config.trailing_stop_pct) <= 0:
                    drawdown_from_peak = 0.0
                    drawdown_reached = True
                    trailing_reason = "到达贪恐阈值即卖（移动止盈=0）"
                else:
                    drawdown_from_peak = (
                        (float(state.greed_peak_price) - current_price) / float(state.greed_peak_price) * 100
                        if float(state.greed_peak_price) > 0 else 0.0
                    )
                    drawdown_reached = drawdown_from_peak >= float(config.trailing_stop_pct)
                    trailing_reason = f"较止盈峰值回撤 {drawdown_from_peak:.2f}% 触发移动止盈"
                sell_price_guard_passed = (not config.sell_price_above_avg_cost) or current_price > avg_cost
                current_position_ratio = position_ratio_before
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
                if drawdown_reached and sell_price_guard_passed and current_position_ratio > float(config.min_position_pct_after_take_profit):
                    if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                        order_action = "SELL"
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

            # ---- 买入 ----
            if log_action != "SKIP" and not order_action and is_fear and volume_ratio >= float(config.volume_ratio_threshold) and can_trade:
                buy_amount = portfolio_value * (float(config.buy_position_pct) / 100.0)
                trade_quantity = min(floor(buy_amount / current_price), floor(available_cash / current_price))
                actual_buy_amount = trade_quantity * current_price
                trade_pct = (actual_buy_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                    order_action = "BUY"
                    order_quantity = trade_quantity
                    order_message_template = (
                        f"{fear_label} {fear_score:.2f}（{signal_date}）进入买入区，"
                        f"量比 {volume_ratio:.2f} >= {config.volume_ratio_threshold:.2f}，订单ID={{order_id}}"
                    )
                    position_ratio_after = ((shares + trade_quantity) * current_price / portfolio_value * 100) if portfolio_value > 0 else position_ratio_before
                else:
                    trade_message = "买入信号成立，但可买数量过小或未达到调仓阈值"

            if log_action != "SKIP" and not order_action and not trade_message:
                if not can_trade:
                    trade_message = f"处于冷却期，剩余 {state.cooldown_remaining_days} 个交易日"
                elif is_fear and volume_ratio < float(config.volume_ratio_threshold):
                    trade_message = f"{fear_label} 进入买入区，但量比 {volume_ratio:.2f} 低于阈值 {config.volume_ratio_threshold:.2f}"
                elif is_greedy:
                    if shares <= 0:
                        trade_message = "处于止盈区，但未持有标的，跳过止盈"
                    elif state.take_profit_cycle_sell_count >= int(config.max_take_profit_sells_per_cycle):
                        trade_message = "处于止盈区，但本轮止盈次数已达上限"
                    else:
                        trade_message = "处于止盈区，但尚未触发移动止盈"
                else:
                    trade_message = "当前无买卖信号"

            if order_action:
                order_id = await self._sync_target_order(
                    config,
                    snapshot,
                    order_action,
                    order_quantity,
                    current_price,
                    trigger_source=trigger_source,
                )
                trade_action = order_action
                trade_quantity = order_quantity
                trade_message = order_message_template.format(order_id=order_id)
                status = "SUCCESS"
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

            log_message = (
                f"{trade_message} | fear_score={fear_score:.2f} | fear_date={signal_date}"
                f" | volume_ratio={volume_ratio:.4f} | price={current_price:.4f}"
            )
            self._persist_run_result(
                config_id=config_id,
                account_id=config.account_id,
                symbol=symbol,
                trigger_source=trigger_source,
                action=trade_action or log_action,
                status=status,
                message=log_message,
                state_values=state,
                run_message=trade_message,
                price=current_price,
                quantity=trade_quantity if trade_action else None,
                fear_score=fear_score,
                volume_ratio=volume_ratio,
                position_ratio_before=position_ratio_before,
                position_ratio_after=position_ratio_after,
            )
            logger.info("A股 fear strategy %s config=%s result=%s msg=%s", masked_account_id, config_id, trade_action or "CHECK", trade_message)
            if trade_action:
                self._send_rebalance_notification(
                    config_id=config_id,
                    masked_account_id=masked_account_id,
                    symbol=symbol,
                    trigger_source=trigger_source,
                    action=trade_action,
                    quantity=trade_quantity,
                    price=current_price,
                    position_ratio_before=position_ratio_before,
                    position_ratio_after=position_ratio_after,
                    fear_score=fear_score,
                    fear_label=fear_label,
                    signal_date=signal_date,
                    volume_ratio=volume_ratio,
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
