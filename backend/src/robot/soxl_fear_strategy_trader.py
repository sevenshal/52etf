import asyncio
import logging
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time as dtime
from math import ceil, floor
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from ..core.database import (
    CNNFearGreedIndex,
    IBKRAccountConfig,
    LongPortAccount,
    Session,
    SoxlFearStrategyConfig,
    SoxlFearStrategyLog,
    SoxlFearStrategyState,
    get_db_ctx,
)
from ..core.services.ib_service import IBKRService
from ..core.services.longport import LongPortService
from ..core.services.market import MarketService
from ..core.services.quote import QuoteService
from ..core.services.trade import OrderSide, OrderType, OutsideRTH, TimeInForceType
from ..core.utils import mask_account_id, send_alert_email
from .cnn_fear_index import CNNFearGreedIndexScraper

logger = logging.getLogger(__name__)
CNN_HISTORY_BASE_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "origin": "https://www.cnn.com",
    "pragma": "no-cache",
    "referer": "https://www.cnn.com/",
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
}


@dataclass
class BrokerSnapshot:
    shares: int
    available_shares: int
    avg_cost: float
    current_price: float
    available_cash: float
    portfolio_value: float
    has_today_order: bool
    order_service: object


class SoxlFearStrategyTrader:
    _instance = None
    _lock = threading.Lock()
    MARKET_OPEN_TIME = dtime(9, 30)
    MANUAL_GREED_STATE_UPDATE_WINDOW_MINUTES = 10
    VOLUME_PROJECTION_WINDOW_MINUTES = 30

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SoxlFearStrategyTrader, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._thread_started = False
        self._thread_lock = threading.Lock()
        self._last_auto_trigger_date: Optional[date] = None
        self.ib_services: Dict[str, IBKRService] = {}

    async def _ensure_ib_connected(self, port: int, client_id: int) -> IBKRService:
        key = f"{port}_{client_id}"
        if key not in self.ib_services:
            self.ib_services[key] = IBKRService(port=port, client_id=client_id)
        service = self.ib_services[key]
        await service.connect()
        return service

    def _get_market_data_service(self, account_id: str, preferred_longport_account_id: Optional[str] = None) -> LongPortService:
        with get_db_ctx() as db:
            lp_account_id = preferred_longport_account_id
            if not lp_account_id:
                account = db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).first()
                if account:
                    lp_account_id = account.lp_account_id
        return LongPortService.get_instance(lp_account_id or "LBPT10001248")

    def _append_log(
        self,
        config_id: Optional[int],
        account_id: str,
        symbol: str,
        trigger_source: str,
        action: str,
        status: str,
        message: str,
        price: Optional[float] = None,
        quantity: Optional[int] = None,
        fear_score: Optional[float] = None,
        volume_ratio: Optional[float] = None,
        position_ratio_before: Optional[float] = None,
        position_ratio_after: Optional[float] = None,
    ):
        with get_db_ctx() as db:
            db.add(
                SoxlFearStrategyLog(
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

    def _update_run_status(self, config_id: Optional[int], status: str, message: str):
        if not config_id:
            return
        with get_db_ctx() as db:
            config = db.query(SoxlFearStrategyConfig).filter(SoxlFearStrategyConfig.id == config_id).first()
            if config:
                config.last_run_at = datetime.now()
                config.last_run_status = status
                config.last_run_message = message[:500]

    def _fetch_latest_cnn_score(self) -> Tuple[float, datetime]:
        scraper = CNNFearGreedIndexScraper()
        try:
            data = scraper.fetch_data_and_save()
            fear_and_greed = data["fear_and_greed"]
            index_timestamp = datetime.fromtimestamp(data["fear_and_greed_historical"]["timestamp"] / 1000)
            return float(fear_and_greed["score"]), index_timestamp
        except Exception as exc:
            logger.warning("Failed to fetch latest CNN fear&greed, fallback to DB: %s", exc)
            db = Session()
            try:
                latest = db.query(CNNFearGreedIndex).order_by(CNNFearGreedIndex.date.desc()).first()
                if not latest:
                    raise
                return float(latest.index_value), latest.index_timestamp or datetime.combine(latest.date, datetime.min.time())
            finally:
                db.close()
        finally:
            scraper.db_session.close()

    def _to_eastern_datetime(self, value) -> Optional[datetime]:
        if not isinstance(value, datetime):
            return None
        eastern = ZoneInfo("US/Eastern")
        if value.tzinfo is not None:
            return value.astimezone(eastern)
        return value.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(eastern)

    def _fallback_volume_completion_ratio(self, now_et: datetime, market_date: date) -> Tuple[float, str]:
        close_time = MarketService.get_us_market_close_time(market_date)
        open_at = datetime.combine(market_date, self.MARKET_OPEN_TIME, tzinfo=now_et.tzinfo)
        close_at = datetime.combine(market_date, close_time, tzinfo=now_et.tzinfo)
        if now_et >= close_at:
            return 1.0, "complete"
        if now_et <= open_at:
            return 1.0, "raw_pre_open"

        minutes_remaining = max(0.0, (close_at - now_et).total_seconds() / 60.0)
        if minutes_remaining > self.VOLUME_PROJECTION_WINDOW_MINUTES:
            return 1.0, "raw_not_near_close"

        # Median SOXL.US 09:30-16:00 ET minute-volume completion over the
        # latest 20 complete sessions sampled on 2026-04-30 ET:
        # 2026-04-01..2026-04-29, skipping 2026-04-03 (no minute data).
        points = [
            (0.0, 1.0),
            (1.0, 0.9910),
            (2.0, 0.9858),
            (3.0, 0.9821),
            (4.0, 0.9781),
            (5.0, 0.9726),
            (10.0, 0.9495),
            (30.0, 0.9043),
        ]
        for index in range(1, len(points)):
            prev_minutes, prev_completion = points[index - 1]
            next_minutes, next_completion = points[index]
            if minutes_remaining <= next_minutes:
                span = next_minutes - prev_minutes
                weight = (minutes_remaining - prev_minutes) / span if span > 0 else 0.0
                completion = prev_completion + (next_completion - prev_completion) * weight
                return completion, "fallback_close_curve"
        return points[-1][1], "fallback_close_curve"

    def _should_update_greed_state(self, trigger_source: str, now_et: datetime, market_date: date) -> bool:
        if str(trigger_source or "").lower() == "auto":
            return True

        close_time = MarketService.get_us_market_close_time(market_date)
        close_at = datetime.combine(market_date, close_time, tzinfo=now_et.tzinfo)
        seconds_from_close = abs((now_et - close_at).total_seconds())
        return seconds_from_close <= self.MANUAL_GREED_STATE_UPDATE_WINDOW_MINUTES * 60

    def _fetch_cnn_score_map(self, db, start_date: date, end_date: date) -> Dict[date, float]:
        if start_date > end_date:
            return {}

        scores: Dict[date, float] = {}
        rows = (
            db.query(CNNFearGreedIndex)
            .filter(
                CNNFearGreedIndex.date >= start_date,
                CNNFearGreedIndex.date <= end_date,
            )
            .order_by(CNNFearGreedIndex.date.asc())
            .all()
        )
        for row in rows:
            if row.index_value is not None:
                scores[row.date] = float(row.index_value)

        try:
            response = requests.get(
                f"{CNN_HISTORY_BASE_URL}/{start_date.isoformat()}",
                headers=CNN_HEADERS,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("fear_and_greed_historical", {}).get("data", []):
                if item.get("x") is None or item.get("y") is None:
                    continue
                item_date = datetime.utcfromtimestamp(item["x"] / 1000).date()
                if start_date <= item_date <= end_date:
                    scores[item_date] = float(item["y"])
        except Exception as exc:
            logger.info("Failed to fetch CNN history for SOXL greed state backfill: %s", exc)

        return scores

    def _fetch_daily_close_rows(
        self,
        account_id: str,
        symbol: str,
        start_date: date,
        end_date: date,
        preferred_longport_account_id: Optional[str] = None,
    ) -> List[Tuple[date, float]]:
        if start_date > end_date:
            return []

        market_data_service = self._get_market_data_service(account_id, preferred_longport_account_id)
        quote_service = QuoteService(market_data_service)
        klines = quote_service.get_klines(symbol, start_date=start_date, end_date=end_date)
        rows: List[Tuple[date, float]] = []
        for item in klines or []:
            item_date = item.get("timestamp")
            if hasattr(item_date, "date"):
                item_date = item_date.date()
            if not isinstance(item_date, date) or item_date < start_date or item_date > end_date:
                continue
            close_price = float(item.get("close") or 0)
            if close_price > 0:
                rows.append((item_date, close_price))
        return sorted(rows, key=lambda value: value[0])

    def _backfill_missing_greed_state(
        self,
        db,
        state: SoxlFearStrategyState,
        config: SoxlFearStrategyConfig,
        symbol: str,
        market_date: date,
        shares: int,
    ) -> Optional[date]:
        if shares <= 0 or not state.last_processed_date:
            return None

        backfill_end = MarketService.get_previous_us_trading_day(market_date)
        if state.last_processed_date > backfill_end:
            return None

        # Re-read the last processed day as well. This lets a prior intraday manual
        # check be corrected by the final daily close without making the strategy
        # more aggressive than the close-based backtest.
        price_start = state.last_processed_date
        fear_start = max(date(2020, 1, 1), price_start - timedelta(days=14))
        preferred_longport_account_id = config.longport_account_id if config.account_type == "longport" else None
        try:
            close_rows = self._fetch_daily_close_rows(
                config.account_id,
                symbol,
                price_start,
                backfill_end,
                preferred_longport_account_id=preferred_longport_account_id,
            )
        except Exception as exc:
            logger.info("Failed to fetch SOXL daily close for greed state backfill: %s", exc)
            return None
        if not close_rows:
            return None

        score_map = self._fetch_cnn_score_map(db, fear_start, backfill_end)
        if not score_map:
            return None

        original_processed_date = state.last_processed_date
        sorted_scores = sorted(score_map.items(), key=lambda value: value[0])
        score_index = 0
        last_score: Optional[float] = None
        processed_dates = []

        for price_date, close_price in close_rows:
            while score_index < len(sorted_scores) and sorted_scores[score_index][0] <= price_date:
                last_score = sorted_scores[score_index][1]
                score_index += 1
            if last_score is None:
                continue

            if last_score >= float(config.greed_threshold):
                state.greed_peak_price = max(float(state.greed_peak_price or close_price), close_price)
            else:
                state.greed_peak_price = None
                state.take_profit_cycle_sell_count = 0
            processed_dates.append(price_date)

        if not processed_dates:
            return None

        cooldown_days_to_decrement = sum(1 for item_date in processed_dates if item_date > original_processed_date)
        if cooldown_days_to_decrement > 0 and state.cooldown_remaining_days > 0:
            state.cooldown_remaining_days = max(0, state.cooldown_remaining_days - cooldown_days_to_decrement)

        state.last_processed_date = processed_dates[-1]
        logger.info(
            "Backfilled SOXL greed state for %s from %s to %s using daily close",
            mask_account_id(config.account_id),
            processed_dates[0],
            processed_dates[-1],
        )
        return processed_dates[-1]

    def _project_current_volume(
        self,
        current_volume: float,
        now_et: datetime,
        market_date: date,
    ) -> Dict[str, object]:
        quote_volume = max(0.0, float(current_volume or 0.0))
        completion_ratio, projection_source = self._fallback_volume_completion_ratio(now_et, market_date)

        if quote_volume <= 0:
            return {
                "current_volume": 0.0,
                "current_volume_source": "quote",
                "current_volume_timestamp": now_et,
                "quote_volume": quote_volume,
                "projected_volume": 0.0,
                "completion_ratio": 1.0,
                "projection_factor": 1.0,
                "projection_source": "empty_volume",
            }

        completion_ratio = max(0.50, min(1.0, float(completion_ratio or 1.0)))
        projection_factor = 1.0 / completion_ratio if completion_ratio > 0 else 1.0
        projected_volume = max(quote_volume, quote_volume * projection_factor)
        return {
            "current_volume": quote_volume,
            "current_volume_source": "quote",
            "current_volume_timestamp": now_et,
            "quote_volume": quote_volume,
            "projected_volume": projected_volume,
            "completion_ratio": completion_ratio,
            "projection_factor": projection_factor,
            "projection_source": projection_source,
        }

    def _format_volume_projection_message(self, market_snapshot: dict) -> str:
        volume_ratio = float(market_snapshot.get("volume_ratio") or 0)
        raw_volume_ratio = float(market_snapshot.get("raw_volume_ratio") or 0)
        detail_parts = []
        if abs(volume_ratio - raw_volume_ratio) >= 0.0005:
            detail_parts.append(f"原始 {raw_volume_ratio:.2f}")
        detail_parts.extend([
            f"预计全天量 {float(market_snapshot.get('projected_volume') or 0):.0f}",
            f"完成率 {float(market_snapshot.get('volume_completion_ratio') or 1) * 100:.1f}%",
            str(market_snapshot.get("volume_projection_source") or "raw"),
        ])
        return (
            f"量比 {volume_ratio:.2f}"
            f"（{'，'.join(detail_parts)}）"
        )

    def _build_realtime_dataframe(
        self,
        account_id: str,
        symbol: str,
        current_market_date: date,
        preferred_longport_account_id: Optional[str] = None,
    ) -> Tuple[pd.DataFrame, dict]:
        market_data_service = self._get_market_data_service(account_id, preferred_longport_account_id)
        history = [
            item for item in (market_data_service.get_candlesticks(symbol, 25, period="d") or [])
            if (item["timestamp"].date() if hasattr(item["timestamp"], "date") else item["timestamp"])
            < current_market_date
        ]
        quote = market_data_service.get_quote(symbol)

        if not history:
            raise ValueError(f"{symbol} 历史日线为空")
        if not quote or not quote.get("price"):
            raise ValueError(f"{symbol} 实时行情为空")

        quote_time_et = self._to_eastern_datetime(quote.get("timestamp"))
        now_et = quote_time_et if quote_time_et and quote_time_et.date() == current_market_date else MarketService.get_eastern_now()

        rows = [
            {
                "date": item["timestamp"].date() if hasattr(item["timestamp"], "date") else item["timestamp"],
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item["volume"]),
            }
            for item in history
        ]
        quote_volume = float(quote.get("volume") or 0)
        projection = self._project_current_volume(
            quote_volume,
            now_et,
            current_market_date,
        )
        current_volume = float(projection.get("current_volume") or quote_volume)
        rows.append(
            {
                "date": current_market_date,
                "open": float(quote.get("open") or quote["price"]),
                "high": float(quote.get("high") or quote["price"]),
                "low": float(quote.get("low") or quote["price"]),
                "close": float(quote["price"]),
                "volume": float(projection["projected_volume"]),
            }
        )
        df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        df["ma20"] = df["close"].rolling(20).mean()
        df["volume_ma20"] = df["volume"].shift(1).rolling(20).mean()
        df["volume_ratio"] = df["volume"] / df["volume_ma20"]
        latest = df.iloc[-1]

        if pd.isna(latest["ma20"]) or pd.isna(latest["volume_ma20"]):
            raise ValueError(f"{symbol} 可用历史数据不足 20 天")

        raw_volume_ratio = current_volume / float(latest["volume_ma20"]) if float(latest["volume_ma20"]) > 0 else 0.0
        return df, {
            "current_price": float(latest["close"]),
            "current_volume": current_volume,
            "current_volume_source": projection.get("current_volume_source") or "quote",
            "current_volume_timestamp": projection.get("current_volume_timestamp") or now_et,
            "quote_volume": quote_volume,
            "projected_volume": float(projection["projected_volume"]),
            "ma20": float(latest["ma20"]),
            "volume_ma20": float(latest["volume_ma20"]),
            "raw_volume_ratio": raw_volume_ratio,
            "volume_ratio": float(latest["volume_ratio"]) if pd.notna(latest["volume_ratio"]) else 0.0,
            "volume_completion_ratio": float(projection["completion_ratio"]),
            "volume_projection_factor": float(projection["projection_factor"]),
            "volume_projection_source": projection["projection_source"],
            "quote_timestamp": now_et,
        }

    async def _build_ib_snapshot(self, config: SoxlFearStrategyConfig, current_price: float) -> BrokerSnapshot:
        with get_db_ctx() as db:
            ib_config = db.query(IBKRAccountConfig).filter(
                IBKRAccountConfig.id == config.ib_account_id,
                IBKRAccountConfig.account_id == config.account_id,
            ).first()
            if not ib_config:
                raise ValueError("未找到对应的 IB 账户配置")
            ib_port = ib_config.ib_port

        service = await self._ensure_ib_connected(ib_port, 3000 + (config.id or 0))
        position = service.get_position(config.symbol)
        shares = int(max(0, round(position.get("qty") or 0)))
        available_cash = float(service.get_available_cash() or 0)
        portfolio_value = float(service.get_net_liquidation() or 0)
        if portfolio_value <= 0:
            portfolio_value = available_cash + shares * current_price

        return BrokerSnapshot(
            shares=shares,
            available_shares=shares,
            avg_cost=float(position.get("avg_cost") or 0),
            current_price=current_price,
            available_cash=available_cash,
            portfolio_value=max(portfolio_value, available_cash + shares * current_price, 1.0),
            has_today_order=await service.has_today_orders(config.symbol),
            order_service=service,
        )

    def _build_longport_snapshot(self, config: SoxlFearStrategyConfig, current_price: float) -> BrokerSnapshot:
        if not config.longport_account_id:
            raise ValueError("未选择长桥账户")
        service = LongPortService.get_instance(config.longport_account_id)
        position = service.get_position_info(config.symbol) or {}
        shares = int(position.get("quantity") or 0)
        available_shares = int(position.get("available_quantity") or shares)
        balance = service.account_balance() or {}
        available_cash = float(balance.get("available_balance") or 0)
        frozen_cash = float(balance.get("frozen_balance") or 0)
        positions = service.stock_positions() or []
        position_symbols = [item["symbol"] for item in positions]
        price_map = {}
        if position_symbols:
            for item in service.get_quote_batch(position_symbols):
                price_map[item["symbol"]] = float(item.get("price") or 0)
        holdings_value = 0.0
        for item in positions:
            symbol = item["symbol"]
            holdings_value += float(item.get("quantity") or 0) * float(price_map.get(symbol) or 0)

        return BrokerSnapshot(
            shares=shares,
            available_shares=available_shares,
            avg_cost=float(position.get("cost_price") or 0),
            current_price=current_price,
            available_cash=available_cash,
            portfolio_value=max(available_cash + frozen_cash + holdings_value, 1.0),
            has_today_order=bool(service.today_orders(symbol=config.symbol)),
            order_service=service,
        )

    async def _build_broker_snapshot(self, config: SoxlFearStrategyConfig, current_price: float) -> BrokerSnapshot:
        if config.account_type == "longport":
            return self._build_longport_snapshot(config, current_price)
        return await self._build_ib_snapshot(config, current_price)

    async def _place_order(self, config: SoxlFearStrategyConfig, snapshot: BrokerSnapshot, action: str, quantity: int, price: float) -> str:
        if quantity < 1:
            raise ValueError("下单数量必须大于 0")

        if config.account_type == "longport":
            side = OrderSide.Buy if action == "BUY" else OrderSide.Sell
            return snapshot.order_service.submit_order(
                side=side,
                symbol=config.symbol,
                order_type=OrderType.MO,
                submitted_price=price,
                submitted_quantity=int(quantity),
                time_in_force=TimeInForceType.Day,
                outside_rth=OutsideRTH.AnyTime,
                remark="SOXL fear strategy",
            )

        trade = await snapshot.order_service.place_market_order(config.symbol, action, int(quantity))
        return str(getattr(getattr(trade, "order", None), "orderId", ""))

    async def run_config_once(self, config: SoxlFearStrategyConfig, trigger_source: str = "auto", ignore_enabled: bool = False):
        config_id = config.id
        masked_account_id = mask_account_id(config.account_id)
        if not config.enabled and not ignore_enabled:
            return

        logger.info("Running SOXL fear strategy for %s config=%s source=%s", masked_account_id, config_id, trigger_source)
        symbol = config.symbol or "SOXL.US"
        now_et = MarketService.get_eastern_now()
        market_date = now_et.date()

        try:
            if not config_id:
                raise ValueError("策略配置缺少配置ID")

            cnn_score, cnn_timestamp = self._fetch_latest_cnn_score()

            _, market_snapshot = self._build_realtime_dataframe(
                config.account_id,
                symbol,
                market_date,
                preferred_longport_account_id=config.longport_account_id if config.account_type == "longport" else None,
            )
            current_price = float(market_snapshot["current_price"])
            volume_ratio = float(market_snapshot["volume_ratio"])
            raw_volume_ratio = float(market_snapshot.get("raw_volume_ratio") or 0.0)
            quote_timestamp = market_snapshot.get("quote_timestamp") or now_et
            volume_detail = self._format_volume_projection_message(market_snapshot)
            broker_snapshot = await self._build_broker_snapshot(config, current_price)

            with get_db_ctx() as db:
                persisted_config = db.query(SoxlFearStrategyConfig).filter(SoxlFearStrategyConfig.id == config_id).first()
                if persisted_config:
                    config.buy_threshold = float(persisted_config.buy_threshold)
                    config.greed_threshold = float(persisted_config.greed_threshold)

                state = db.query(SoxlFearStrategyState).filter(SoxlFearStrategyState.config_id == config_id).first()
                if not state:
                    state = SoxlFearStrategyState(config_id=config_id, account_id=config.account_id, symbol=symbol)
                    db.add(state)
                    db.flush()

                shares = int(broker_snapshot.shares)
                available_shares = int(broker_snapshot.available_shares)
                avg_cost = float(broker_snapshot.avg_cost or 0)
                portfolio_value = float(broker_snapshot.portfolio_value or 0)
                available_cash = float(broker_snapshot.available_cash or 0)
                position_value = shares * current_price
                position_ratio_before = (position_value / portfolio_value * 100) if portfolio_value > 0 else 0.0

                allow_greed_state_update = self._should_update_greed_state(trigger_source, quote_timestamp, market_date)
                if shares > 0:
                    self._backfill_missing_greed_state(db, state, config, symbol, market_date, shares)

                if allow_greed_state_update and state.last_processed_date != market_date:
                    if state.last_processed_date and state.cooldown_remaining_days > 0:
                        state.cooldown_remaining_days = max(0, state.cooldown_remaining_days - 1)
                    state.last_processed_date = market_date

                is_fear = float(cnn_score) <= float(config.buy_threshold)
                is_greedy = float(cnn_score) >= float(config.greed_threshold)
                can_trade = state.cooldown_remaining_days <= 0

                if shares <= 0:
                    state.greed_peak_price = None
                    state.take_profit_cycle_sell_count = 0
                elif allow_greed_state_update:
                    if not is_greedy:
                        state.greed_peak_price = None
                        state.take_profit_cycle_sell_count = 0
                    else:
                        state.greed_peak_price = max(float(state.greed_peak_price or current_price), current_price)

                if broker_snapshot.has_today_order:
                    message = "今日已存在订单，跳过重复执行"
                    self._append_log(
                        config_id,
                        config.account_id,
                        symbol,
                        trigger_source,
                        "SKIP",
                        "SKIPPED",
                        message,
                        price=current_price,
                        fear_score=cnn_score,
                        volume_ratio=volume_ratio,
                        position_ratio_before=position_ratio_before,
                        position_ratio_after=position_ratio_before,
                    )
                    self._update_run_status(config_id, "SKIPPED", message)
                    return

                trade_action = None
                trade_quantity = 0
                trade_message = ""
                position_ratio_after = position_ratio_before

                if (
                    shares > 0
                    and can_trade
                    and is_greedy
                    and state.greed_peak_price
                    and state.take_profit_cycle_sell_count < int(config.max_take_profit_sells_per_cycle)
                ):
                    drawdown_from_peak = ((float(state.greed_peak_price) - current_price) / float(state.greed_peak_price) * 100) if float(state.greed_peak_price) > 0 else 0.0
                    current_position_ratio = position_ratio_before
                    min_hold_shares = ceil(portfolio_value * (float(config.min_position_pct_after_take_profit) / 100.0) / current_price) if portfolio_value > 0 and current_price > 0 else 0

                    if float(config.sell_reduction_basis or "portfolio") == "portfolio":
                        trade_quantity = floor(portfolio_value * (float(config.sell_position_pct) / 100.0) / current_price)
                    else:
                        trade_quantity = floor(available_shares * (float(config.sell_position_pct) / 100.0))
                    trade_quantity = min(int(trade_quantity), int(available_shares))

                    if available_shares - trade_quantity < min_hold_shares:
                        trade_quantity = max(0, available_shares - min_hold_shares)

                    sell_amount = trade_quantity * current_price
                    trade_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0

                    if drawdown_from_peak >= float(config.trailing_stop_pct) and avg_cost > 0 and current_price > avg_cost and current_position_ratio > float(config.min_position_pct_after_take_profit):
                        if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                            order_id = await self._place_order(config, broker_snapshot, "SELL", trade_quantity, current_price)
                            trade_action = "SELL"
                            state.cooldown_remaining_days = int(config.cooldown_days)
                            state.take_profit_cycle_sell_count += 1
                            if shares - trade_quantity <= 0 or state.take_profit_cycle_sell_count >= int(config.max_take_profit_sells_per_cycle):
                                state.greed_peak_price = None
                            else:
                                state.greed_peak_price = current_price
                            position_ratio_after = max(0.0, ((shares - trade_quantity) * current_price / portfolio_value * 100) if portfolio_value > 0 else 0.0)
                            trade_message = f"CNN={cnn_score:.2f} 进入止盈区，价格较峰值回撤 {drawdown_from_peak:.2f}% 触发移动止盈，订单ID={order_id}"
                        else:
                            trade_message = "止盈信号成立，但可卖数量过小或未达到调仓阈值"
                    else:
                        trade_message = f"处于止盈区，等待进一步回撤。当前回撤 {drawdown_from_peak:.2f}%"

                if not trade_action and is_fear and volume_ratio >= float(config.volume_ratio_threshold) and can_trade:
                    buy_amount = portfolio_value * (float(config.buy_position_pct) / 100.0)
                    trade_quantity = min(floor(buy_amount / current_price), floor(available_cash / current_price))
                    actual_buy_amount = trade_quantity * current_price
                    trade_pct = (actual_buy_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0

                    if trade_quantity >= 1 and trade_pct > float(config.rebalance_threshold_pct):
                        order_id = await self._place_order(config, broker_snapshot, "BUY", trade_quantity, current_price)
                        trade_action = "BUY"
                        state.cooldown_remaining_days = int(config.cooldown_days)
                        state.greed_peak_price = None
                        state.take_profit_cycle_sell_count = 0
                        position_ratio_after = ((shares + trade_quantity) * current_price / portfolio_value * 100) if portfolio_value > 0 else position_ratio_before
                        trade_message = f"CNN={cnn_score:.2f} 进入买入区，{volume_detail} 放大，订单ID={order_id}"
                    else:
                        trade_message = "买入信号成立，但可买数量过小或未达到调仓阈值"

                if not trade_action and not trade_message:
                    if not can_trade:
                        trade_message = f"处于冷却期，剩余 {state.cooldown_remaining_days} 个交易日"
                    elif is_fear and volume_ratio < float(config.volume_ratio_threshold):
                        trade_message = (
                            f"CNN 进入买入区，但{volume_detail}低于阈值 "
                            f"{float(config.volume_ratio_threshold):.2f}"
                        )
                    elif is_greedy:
                        if shares <= 0:
                            trade_message = "处于止盈区，但未持有标的，跳过止盈"
                        elif not allow_greed_state_update and not state.greed_peak_price:
                            trade_message = "处于止盈区，但非收盘附近手动运行，未初始化移动止盈峰值"
                        elif state.take_profit_cycle_sell_count >= int(config.max_take_profit_sells_per_cycle):
                            trade_message = "处于止盈区，但本轮止盈次数已达上限"
                        else:
                            trade_message = "处于止盈区，但尚未触发移动止盈"
                    else:
                        trade_message = "当前无买卖信号"

                status = "SUCCESS" if trade_action else "INFO"
                volume_ratio_detail = f" | volume_ratio={volume_ratio:.4f}"
                if abs(volume_ratio - raw_volume_ratio) >= 0.00005:
                    volume_ratio_detail += f" | raw_volume_ratio={raw_volume_ratio:.4f}"
                self._append_log(
                    config_id,
                    config.account_id,
                    symbol,
                    trigger_source,
                    trade_action or "CHECK",
                    status,
                    (
                        f"{trade_message} | cnn_score={cnn_score:.2f} | cnn_timestamp={cnn_timestamp}"
                        f"{volume_ratio_detail}"
                        f" | volume_projection_source={market_snapshot.get('volume_projection_source')}"
                    ),
                    price=current_price,
                    quantity=trade_quantity if trade_action else None,
                    fear_score=cnn_score,
                    volume_ratio=volume_ratio,
                    position_ratio_before=position_ratio_before,
                    position_ratio_after=position_ratio_after,
                )
                self._update_run_status(config_id, status, trade_message)
                logger.info("SOXL fear strategy %s config=%s result=%s msg=%s", masked_account_id, config_id, trade_action or "CHECK", trade_message)
        except Exception as exc:
            logger.error("SOXL fear strategy failed for %s config=%s: %s", masked_account_id, config_id, exc, exc_info=True)
            error_message = f"执行失败: {exc}"
            self._append_log(
                config_id,
                config.account_id,
                symbol,
                trigger_source,
                "ERROR",
                "ERROR",
                error_message,
            )
            self._update_run_status(config_id, "ERROR", error_message)
            send_alert_email(
                f"SOXL情绪量能自动交易报错: {masked_account_id}#{config_id}",
                f"Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
            )

    async def run_config_id_once(
        self,
        config_id: int,
        account_id: Optional[str] = None,
        trigger_source: str = "manual",
        ignore_enabled: bool = True,
    ):
        with get_db_ctx() as db:
            query = db.query(SoxlFearStrategyConfig).filter(SoxlFearStrategyConfig.id == config_id)
            if account_id:
                query = query.filter(SoxlFearStrategyConfig.account_id == account_id)
            config = query.first()
            if not config:
                raise ValueError("未找到 SOXL 情绪量能策略配置")
            db.expunge(config)
        await self.run_config_once(config, trigger_source=trigger_source, ignore_enabled=ignore_enabled)

    async def run_account_once(self, account_id: str, trigger_source: str = "manual", ignore_enabled: bool = True):
        with get_db_ctx() as db:
            config = (
                db.query(SoxlFearStrategyConfig)
                .filter(SoxlFearStrategyConfig.account_id == account_id)
                .order_by(SoxlFearStrategyConfig.id.asc())
                .first()
            )
            if not config:
                raise ValueError("未找到 SOXL 情绪量能策略配置")
            config_id = config.id
        await self.run_config_id_once(config_id, account_id, trigger_source=trigger_source, ignore_enabled=ignore_enabled)

    async def run_all_enabled_once(self, trigger_source: str = "auto"):
        with get_db_ctx() as db:
            configs = db.query(SoxlFearStrategyConfig).filter(SoxlFearStrategyConfig.enabled == True).all()
            for config in configs:
                db.expunge(config)

        for config in configs:
            await self.run_config_once(config, trigger_source=trigger_source, ignore_enabled=False)

    async def worker_loop(self):
        logger.info("SOXL fear strategy trader loop started")
        while True:
            try:
                now = MarketService.get_eastern_now()
                if now.weekday() >= 5 or MarketService.is_us_market_holiday(now.date()):
                    await asyncio.sleep(3600)
                    continue

                close_time = MarketService.get_us_market_close_time(now.date())
                target_time = datetime.combine(now.date(), close_time, tzinfo=now.tzinfo) - timedelta(minutes=2)
                seconds_to_trigger = (target_time - now).total_seconds()

                if 0 <= seconds_to_trigger <= 30 and self._last_auto_trigger_date != now.date():
                    self._last_auto_trigger_date = now.date()
                    await self.run_all_enabled_once(trigger_source="auto")
                    await asyncio.sleep(120)
                    continue

                if seconds_to_trigger > 600:
                    await asyncio.sleep(min(seconds_to_trigger - 300, 3600))
                elif seconds_to_trigger > 60:
                    await asyncio.sleep(30)
                elif seconds_to_trigger > 0:
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(300)
            except Exception as exc:
                logger.error("SOXL fear strategy worker loop error: %s", exc, exc_info=True)
                send_alert_email(
                    "SOXL情绪量能自动交易主循环异常",
                    f"Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
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
        thread = threading.Thread(target=runner, daemon=True, name=f"SOXLFearManual-{thread_label}")
        thread.start()


def start_soxl_fear_strategy_trader():
    trader = SoxlFearStrategyTrader()
    with trader._thread_lock:
        if trader._thread_started:
            logger.info("SOXL fear strategy trader already started, skipping.")
            return
        trader._thread_started = True

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trader.worker_loop())

    thread = threading.Thread(target=runner, daemon=True, name="SOXLFearStrategyTrader")
    thread.start()
    logger.info("SOXL fear strategy trader thread started")
