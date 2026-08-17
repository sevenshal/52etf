from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import and_, desc, func, text

from .fear_greed_clone_service import (
    CBOE_OPTIONS_DAILY_URL,
    ComponentSpec,
    FearGreedCloneCalculator,
    FearGreedCloneError,
)
from .barchat import BarchartService
from .evc import EVCService
from .longport import LongPortService
from .market import MarketService
from .quote import QuoteService
from .volume_metrics import calculate_volume_ratio
from .a_stock_fear_greed_clone_service import A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL
from ..analytics_database import AnalyticsSession
from ..database import ETFHolding as DBETFHolding
from ..database import (
    ETFFearGreedCloneHistory,
    ETFFearGreedCloneHolding,
    ETFOptionExpiration,
    ETFPutCallRatio,
    Session,
)
from ..models.etf import ETFHolding
from ..utils import normalize_us_equity_symbol
from ...robot.etf.ishares import ISharesETFFetcher
from ...robot.etf.qqq import QQQDataFetcher
from ...robot.etf.spdr import SPDRDataFetcher


NASDAQ_OPTION_CHAIN_URL = "https://api.nasdaq.com/api/quote/{symbol}/option-chain"
DEFAULT_ETF_FEAR_GREED_SYMBOLS = ["SOXX.US", "SPY.US", "QQQ.US", "DIA.US"]


def _a_stock_proxy_etf_map() -> Dict[str, str]:
    """A股指数自算贪恐 symbol → 场内代理 ETF 代码（无 proxy_etf 的指数不在映射里）。"""
    return {
        symbol: str(target["proxy_etf"]).strip().upper()
        for symbol, target in A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL.items()
        if target.get("proxy_etf")
    }


def _finite_or_none(value: Any) -> Optional[float]:
    """None / NaN / inf → None，否则保留 6 位小数。"""
    try:
        if value is None or pd.isna(value):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(number) else round(number, 6)


def _load_proxy_etf_bars(
    etf_symbols: List[str],
    start_date: date,
    end_date: date,
    use_qfq: bool = True,
) -> Dict[str, pd.DataFrame]:
    """从 DuckDB 读取 A股场内 ETF 日线（价格前复权可选）。

    use_qfq=True 查 a_stock_fund_daily_qfq（价格前复权，成交量/成交额不变）；
    False 查原始 a_stock_fund_daily。
    返回 {ts_code: DataFrame(index=trade_date, columns=[open, high, low, close, volume, turnover])}。
    """
    symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (etf_symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not symbols:
        return {}
    table = "a_stock_fund_daily_qfq" if use_qfq else "a_stock_fund_daily"
    placeholders = ", ".join(f":etf_{idx}" for idx in range(len(symbols)))
    params: Dict[str, Any] = {
        f"etf_{idx}": symbol for idx, symbol in enumerate(symbols)
    }
    params["start_date"] = start_date.isoformat()
    params["end_date"] = end_date.isoformat()
    analytics_db = AnalyticsSession()
    try:
        rows = analytics_db.execute(
            text(
                f"""
                SELECT ts_code, trade_date, open, high, low, close, vol, amount
                FROM {table}
                WHERE ts_code IN ({placeholders})
                  AND trade_date >= :start_date
                  AND trade_date <= :end_date
                ORDER BY ts_code, trade_date
                """
            ),
            params,
        ).fetchall()
    finally:
        AnalyticsSession.remove()
    by_symbol: Dict[str, List[tuple]] = {}
    for row in rows:
        by_symbol.setdefault(str(row[0]).upper(), []).append(tuple(row))
    frames: Dict[str, pd.DataFrame] = {}
    for symbol, symbol_rows in by_symbol.items():
        frame = pd.DataFrame(
            symbol_rows,
            columns=[
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "turnover",
            ],
        )
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        for column in ("open", "high", "low", "close", "volume", "turnover"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frames[symbol] = frame.set_index("trade_date").sort_index()
    return frames


ETF_COMPONENTS: Dict[str, ComponentSpec] = {
    "market_momentum": ComponentSpec(
        key="market_momentum",
        name="ETF Momentum",
        raw_label="ETF close / 125-day moving average - 1",
        source="LongPort daily prices via QuoteService",
        proxy_note="Same idea as CNN market momentum, applied to the ETF itself.",
    ),
    "stock_price_strength": ComponentSpec(
        key="stock_price_strength",
        name="Holdings Price Strength",
        raw_label="Current-holdings weighted 52-week range position",
        source="DB ETF holdings + LongPort stock history",
        proxy_note=(
            "ETF-specific proxy for new highs/lows. It uses the stored ETF "
            "holding snapshot for each trading day."
        ),
    ),
    "stock_price_breadth": ComponentSpec(
        key="stock_price_breadth",
        name="Holdings Price Breadth",
        raw_label="5-day average weighted advancing dollar-volume ratio",
        source="DB ETF holdings + LongPort stock history",
        proxy_note=(
            "ETF-specific proxy for breadth. Advancing/declining dollar volume "
            "is computed across the stored ETF constituents for each trading day."
        ),
    ),
    "put_call_options": ComponentSpec(
        key="put_call_options",
        name="ETF Put/Call Options",
        raw_label="-5-day average symbol-specific put/call volume ratio",
        source="Local Barchart ETF put/call history table",
        proxy_note=(
            "Uses symbol-specific Barchart putCallVolumeRatio rows saved in "
            "etf_put_call_ratios. If the table has no usable rows for the "
            "requested range, the calculator falls back to Cboe's "
            "exchange-traded products put/call ratio."
        ),
    ),
    "market_volatility": ComponentSpec(
        key="market_volatility",
        name="ETF Volatility",
        raw_label="-(20-day realized volatility / 50-day average - 1)",
        source="LongPort daily prices via QuoteService",
        proxy_note="ETF-specific replacement for CNN's VIX component.",
    ),
    "safe_haven_demand": ComponentSpec(
        key="safe_haven_demand",
        name="Safe Haven Demand",
        raw_label="20-day ETF return - 20-day TLT return",
        source="LongPort ETF history via QuoteService",
        proxy_note=(
            "Same risk-on/risk-off idea as CNN, comparing the ETF to long-duration "
            "Treasury ETF TLT."
        ),
    ),
    "junk_bond_demand": ComponentSpec(
        key="junk_bond_demand",
        name="Junk Bond Demand",
        raw_label="-(high-yield OAS - investment-grade OAS)",
        source="FRED BAMLH0A0HYM2 and BAMLC0A0CM",
        proxy_note="Credit-risk appetite is kept as a broad market component.",
    ),
}


class ETFFearGreedCloneCalculator(FearGreedCloneCalculator):
    """ETF-specific Fear & Greed clone built from free data sources.

    CNN discloses seven concepts but not its normalization. This calculator
    keeps the same seven-slot structure, swaps market-internal components for
    current ETF holdings where possible, and scores each raw signal with the
    same rolling z-score/CDF method used by FearGreedCloneCalculator.
    """

    REALTIME_CACHE_TTL_SECONDS = 300
    _realtime_cache: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
    _realtime_cache_lock = threading.Lock()

    def __init__(self, cache_dir: Optional[str] = None, timeout: float = 30.0):
        default_cache_dir = "/var/lib/quant_robot/cache/fear_greed_clone"
        super().__init__(
            cache_dir=cache_dir
            or os.getenv("ETF_FNG_CLONE_CACHE_DIR", default_cache_dir),
            timeout=timeout,
        )
        self.ishares_fetcher = ISharesETFFetcher()
        self.spdr_fetcher = SPDRDataFetcher()
        self.qqq_fetcher = QQQDataFetcher()
        self.holdings_fetchers = self._build_holdings_fetchers()
        self.quote_service = QuoteService(LongPortService.get_instance())
        self.price_cache: Dict[Tuple[str, date, date], pd.DataFrame] = {}

    def _build_holdings_fetchers(self) -> Dict[str, Any]:
        fetchers: Dict[str, Any] = {}
        for symbol in self.ishares_fetcher.ETF_CONFIGS:
            fetchers[symbol] = self.ishares_fetcher
        for symbol in self.spdr_fetcher.ETF_CONFIGS:
            fetchers[symbol] = self.spdr_fetcher
        fetchers["QQQ.US"] = self.qqq_fetcher
        return fetchers

    def calculate(
        self,
        symbol: str = "SOXX.US",
        as_of: Optional[str] = None,
        history_days: int = 550,
        score_window: int = 252,
        min_periods: int = 120,
        include_history: bool = False,
        history_points: int = 180,
        max_holdings: int = 0,
        use_historical_holdings: bool = True,
    ) -> Dict[str, Any]:
        etf_symbol = self._normalize_etf_symbol(symbol)
        end_date = self._parse_as_of(as_of)
        if history_days < 180:
            raise FearGreedCloneError("history_days should be at least 180")
        if min_periods > score_window:
            raise FearGreedCloneError("min_periods cannot exceed score_window")

        start_date = end_date - timedelta(days=history_days)
        (
            holdings_by_date,
            holdings_as_of_by_date,
            holdings_as_of,
            latest_holdings,
        ) = self._build_holdings_by_date(
            etf_symbol,
            start_date,
            end_date,
            max_holdings=max_holdings,
            use_historical_holdings=use_historical_holdings,
        )
        raw = self._build_raw_signals(etf_symbol, holdings_by_date, start_date, end_date)
        score_df = self._score_etf_raw_signals(raw, score_window, min_periods)

        score_columns = [f"{key}_score" for key in ETF_COMPONENTS]
        component_scores = score_df[score_columns].rename(
            columns={f"{key}_score": key for key in ETF_COMPONENTS}
        )
        score_df["fear_greed_clone"] = component_scores.mean(axis=1)
        valid = score_df.dropna(subset=score_columns + ["fear_greed_clone"])
        if valid.empty:
            raise FearGreedCloneError("not enough data to calculate the ETF clone index")

        latest_date = valid.index.max()
        latest_row = valid.loc[latest_date]
        latest_score = float(latest_row["fear_greed_clone"])

        warnings = [
            "This is an independent ETF-specific clone, not CNN's undisclosed calculation.",
            "Scores use rolling z-score/CDF and equal-weighted components.",
            "Holdings-based components read the latest etf_holdings snapshot on or before each trading day; missing prior snapshots will raise an error.",
            (
                "The option component uses local Barchart ETF put/call history "
                "from etf_put_call_ratios, with Cboe ETP put/call as an outage fallback."
            ),
            (
                "FRED ICE BofA credit-spread series currently provides only the "
                "latest three years of observations, which limits long backfills "
                "for the junk-bond-demand component."
            ),
        ]

        response: Dict[str, Any] = {
            "fear_and_greed_clone": {
                "symbol": etf_symbol,
                "score": round(latest_score, 2),
                "rating": self.rating(latest_score),
                "date": latest_date.date().isoformat(),
                "method": "equal-weighted rolling z-score normal CDF",
                "history_days": history_days,
                "score_window": score_window,
                "min_periods": min_periods,
                "holdings_as_of": holdings_as_of.isoformat(),
                "holdings_count": len(latest_holdings),
                "holdings_weight_used": round(sum(item.weight for item in latest_holdings), 6),
                "use_historical_holdings": use_historical_holdings,
            },
            "components": self._component_payload(raw, score_df, latest_date),
            "holdings": [
                {
                    "symbol": holding.symbol,
                    "name": holding.name,
                    "weight": round(holding.weight, 6),
                }
                for holding in latest_holdings
            ],
            "option_chain_snapshot": self._safe_option_chain_snapshot(etf_symbol),
            "warnings": warnings,
        }

        if include_history:
            response["history"] = self._history_payload(valid, history_points)

        return response

    def calculate_history(
        self,
        symbol: str = "SOXX.US",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        output_start_date: Optional[date] = None,
        history_days: int = 1200,
        score_window: int = 252,
        min_periods: int = 120,
        max_holdings: int = 0,
        use_historical_holdings: bool = True,
    ) -> Dict[str, Any]:
        etf_symbol = self._normalize_etf_symbol(symbol)
        end_date = end_date or datetime.utcnow().date()
        calc_start_date = start_date or (end_date - timedelta(days=history_days))
        if min_periods > score_window:
            raise FearGreedCloneError("min_periods cannot exceed score_window")

        (
            holdings_by_date,
            holdings_as_of_by_date,
            latest_holdings_as_of,
            latest_holdings,
        ) = self._build_holdings_by_date(
            etf_symbol,
            calc_start_date,
            end_date,
            max_holdings=max_holdings,
            use_historical_holdings=use_historical_holdings,
        )
        raw = self._build_raw_signals(etf_symbol, holdings_by_date, calc_start_date, end_date)
        score_df = self._score_etf_raw_signals(raw, score_window, min_periods)
        score_columns = [f"{key}_score" for key in ETF_COMPONENTS]
        component_scores = score_df[score_columns].rename(
            columns={f"{key}_score": key for key in ETF_COMPONENTS}
        )
        score_df["fear_greed_clone"] = component_scores.mean(axis=1)
        valid = score_df.dropna(subset=score_columns + ["fear_greed_clone"])

        warnings = self._warnings(etf_symbol, use_historical_holdings)
        records: List[Dict[str, Any]] = []
        for timestamp, row in valid.iterrows():
            if output_start_date and timestamp.date() < output_start_date:
                continue
            holdings = holdings_by_date.get(timestamp, [])
            holdings_as_of = holdings_as_of_by_date.get(timestamp)
            score = float(row["fear_greed_clone"])
            records.append(
                {
                    "symbol": etf_symbol,
                    "date": timestamp.date().isoformat(),
                    "score": round(score, 4),
                    "rating": self.rating(score),
                    "method": "equal-weighted rolling z-score normal CDF",
                    "history_days": history_days,
                    "score_window": score_window,
                    "min_periods": min_periods,
                    "use_historical_holdings": use_historical_holdings,
                    "etf_price": self._price_payload(raw.loc[timestamp]),
                    "holdings_as_of": holdings_as_of.isoformat() if holdings_as_of else None,
                    "holdings_count": len(holdings),
                    "holdings_weight_used": round(sum(item.weight for item in holdings), 6),
                    "components": self._component_payload(raw, score_df, timestamp),
                    "holdings": self._holdings_payload(holdings),
                    "warnings": warnings,
                }
            )

        return {
            "symbol": etf_symbol,
            "start_date": records[0]["date"] if records else None,
            "end_date": records[-1]["date"] if records else None,
            "count": len(records),
            "latest_holdings_as_of": latest_holdings_as_of.isoformat(),
            "latest_holdings_count": len(latest_holdings),
            "records": records,
            "warnings": warnings,
        }

    def calculate_realtime_cached(
        self,
        symbol: str = "SOXX.US",
        history_days: int = 550,
        score_window: int = 252,
        min_periods: int = 120,
        max_holdings: int = 0,
        include_extended: bool = True,
        include_holdings_quotes: bool = True,
        fetch_holdings_quotes: bool = True,
        cache_ttl_seconds: int = REALTIME_CACHE_TTL_SECONDS,
    ) -> Dict[str, Any]:
        etf_symbol = self._normalize_etf_symbol(symbol)
        cache_key = (
            etf_symbol,
            int(history_days),
            int(score_window),
            int(min_periods),
            int(max_holdings),
            bool(include_extended),
            bool(include_holdings_quotes),
            bool(fetch_holdings_quotes),
        )
        now = time.monotonic()
        with self._realtime_cache_lock:
            cached = self._realtime_cache.get(cache_key)
            if cached:
                cached_at, payload = cached
                age_seconds = now - cached_at
                if age_seconds < cache_ttl_seconds:
                    response = copy.deepcopy(payload)
                    response["cache"] = self._cache_payload(
                        hit=True,
                        age_seconds=age_seconds,
                        ttl_seconds=cache_ttl_seconds,
                    )
                    return response

            response = self.calculate_realtime(
                symbol=etf_symbol,
                history_days=history_days,
                score_window=score_window,
                min_periods=min_periods,
                max_holdings=max_holdings,
                include_extended=include_extended,
                include_holdings_quotes=include_holdings_quotes,
                fetch_holdings_quotes=fetch_holdings_quotes,
            )
            self._realtime_cache[cache_key] = (time.monotonic(), copy.deepcopy(response))
            response["cache"] = self._cache_payload(
                hit=False,
                age_seconds=0.0,
                ttl_seconds=cache_ttl_seconds,
            )
            return response

    def calculate_realtime(
        self,
        symbol: str = "SOXX.US",
        history_days: int = 550,
        score_window: int = 252,
        min_periods: int = 120,
        max_holdings: int = 0,
        include_extended: bool = True,
        include_holdings_quotes: bool = True,
        fetch_holdings_quotes: bool = True,
    ) -> Dict[str, Any]:
        """Calculate a price-driven intraday ETF Fear & Greed clone.

        The scoring baseline comes from the SQLite daily backfill. The current
        row is rebuilt from LongPort realtime quotes for the ETF, TLT and the
        latest DB holdings snapshot. Daily-only components are carried forward
        from the latest stored row and are marked in the response.

        fetch_holdings_quotes=False 时只取 ETF + TLT 两个实时价（轻量模式），
        依赖成分股行情的强度/宽度分量沿用最近入库日线值，用于摘要卡片这类
        需要快速出值的场景，避免为几百只成分股批量拉实时行情。
        """
        etf_symbol = self._normalize_etf_symbol(symbol)
        if min_periods > score_window:
            raise FearGreedCloneError("min_periods cannot exceed score_window")

        quote_map = self._fetch_realtime_quote_map([etf_symbol, "TLT.US"])
        etf_quote = quote_map.get(etf_symbol)
        if not etf_quote or not self._quote_price(etf_quote):
            raise FearGreedCloneError(f"{etf_symbol} 实时行情为空")

        realtime_date = self._quote_market_date(etf_quote)
        holdings, holdings_as_of = self._load_latest_db_holdings_on_or_before(
            etf_symbol, realtime_date, max_holdings=max_holdings
        )
        holdings_weight_used = sum(item.weight for item in holdings)
        if fetch_holdings_quotes:
            holding_quote_map = self._fetch_realtime_quote_map([holding.symbol for holding in holdings])
            quote_map.update(holding_quote_map)
        quote_symbols = [etf_symbol, "TLT.US"]
        if fetch_holdings_quotes:
            quote_symbols += [holding.symbol for holding in holdings]
        previous_trading_day = MarketService.get_previous_us_trading_day(realtime_date)
        history_start = realtime_date - timedelta(days=history_days)

        raw_history = self._load_component_raw_history_from_db(
            etf_symbol,
            start_date=history_start,
            end_date=previous_trading_day,
        )
        if raw_history.empty or len(raw_history.dropna()) < min_periods:
            raise FearGreedCloneError(
                f"{etf_symbol} SQLite 历史组件不足，请先执行 ETF 恐贪回跑"
            )

        current_timestamp = pd.Timestamp(realtime_date)
        current_raw, etf_price, raw_freshness = self._build_realtime_raw_row(
            etf_symbol=etf_symbol,
            holdings=holdings,
            quote_map=quote_map,
            current_date=realtime_date,
            previous_trading_day=previous_trading_day,
            price_history_count=max(history_days, 320),
            fetch_holdings_quotes=fetch_holdings_quotes,
        )

        latest_daily = raw_history.iloc[-1]
        stale_components = {
            "put_call_options": "latest stored option put/call component",
            "junk_bond_demand": "latest stored FRED credit-spread component",
        }
        if not fetch_holdings_quotes:
            stale_components["stock_price_strength"] = "latest stored constituent strength component"
            stale_components["stock_price_breadth"] = "latest stored constituent breadth component"
        for key in stale_components:
            if pd.isna(current_raw.get(key)):
                latest_value = latest_daily.get(key)
                if latest_value is not None and pd.notna(latest_value):
                    current_raw[key] = float(latest_value)
                    raw_freshness[key] = stale_components[key]

        raw_for_score = raw_history.copy()
        raw_for_score = raw_for_score[raw_for_score.index < current_timestamp]
        raw_for_score.loc[current_timestamp, list(ETF_COMPONENTS.keys())] = [
            current_raw[key] for key in ETF_COMPONENTS
        ]
        score_df = self._score_etf_raw_signals(
            raw_for_score,
            score_window=score_window,
            min_periods=min_periods,
        )
        score_columns = [f"{key}_score" for key in ETF_COMPONENTS]
        score_df["fear_greed_clone"] = score_df[score_columns].mean(axis=1)
        latest_row = score_df.loc[current_timestamp]
        if pd.isna(latest_row["fear_greed_clone"]):
            raise FearGreedCloneError("not enough realtime data to calculate the ETF clone index")

        score = float(latest_row["fear_greed_clone"])
        components = self._component_payload(raw_for_score, score_df, current_timestamp)
        for key, payload in components.items():
            freshness = raw_freshness.get(key) or "latest stored daily component"
            is_realtime = freshness not in stale_components.values()
            payload["is_realtime"] = is_realtime
            payload["freshness"] = freshness

        requested_quotes = list(dict.fromkeys(quote_symbols))
        missing_quotes = [item for item in requested_quotes if item not in quote_map]
        quote_timestamp = self._quote_timestamp(etf_quote)
        warnings = self._warnings(etf_symbol, use_historical_holdings=True) + [
            (
                "Realtime mode is price-driven: the ETF and TLT price components "
                "use LongPort realtime quotes"
                + (
                    "; holding price components use LongPort realtime quotes too"
                    if fetch_holdings_quotes
                    else "; constituent strength/breadth components carry forward the "
                    "latest stored daily values (quote-light mode)"
                )
                + "."
            ),
            (
                "Put/call uses Barchart's live expiration snapshot first, then "
                "today's local Barchart expiration snapshot if the live request "
                "fails; junk-bond-demand is daily data and is carried forward "
                "from the latest SQLite backfill row."
            ),
            (
                "Current-row holdings use the latest DB holdings snapshot on or before "
                "the quote date; "
                "the historical scoring baseline comes from stored daily backfills."
            ),
        ]

        response: Dict[str, Any] = {
            "fear_and_greed_clone": {
                "symbol": etf_symbol,
                "score": round(score, 2),
                "rating": self.rating(score),
                "date": realtime_date.isoformat(),
                "timestamp": quote_timestamp.isoformat() if quote_timestamp else None,
                "method": "equal-weighted rolling z-score normal CDF",
                "mode": "intraday price-driven",
                "history_days": history_days,
                "score_window": score_window,
                "min_periods": min_periods,
                "history_rows_used": int(len(raw_for_score)),
                "market_open": MarketService.is_us_market_open(
                    include_extended=include_extended
                ),
                "include_extended": include_extended,
                "holdings_as_of": holdings_as_of.isoformat(),
                "holdings_count": len(holdings),
                "holdings_weight_used": round(holdings_weight_used, 6),
            },
            "components": components,
            "etf_price": etf_price,
            "quote_coverage": {
                "requested": len(requested_quotes),
                "received": len(quote_map),
                "missing": missing_quotes,
            },
            "holdings": (
                self._realtime_holdings_payload(holdings, quote_map)
                if include_holdings_quotes
                else self._holdings_payload(holdings)
            ),
            "option_chain_snapshot": self._safe_option_chain_snapshot(etf_symbol),
            "warnings": warnings,
        }
        return response

    @staticmethod
    def _cache_payload(
        hit: bool,
        age_seconds: float,
        ttl_seconds: int,
    ) -> Dict[str, Any]:
        return {
            "hit": hit,
            "ttl_seconds": ttl_seconds,
            "age_seconds": round(max(age_seconds, 0.0), 3),
            "expires_in_seconds": round(max(ttl_seconds - age_seconds, 0.0), 3),
        }

    def backfill_to_db(
        self,
        symbol: str = "SOXX.US",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        output_start_date: Optional[date] = None,
        history_days: int = 1200,
        score_window: int = 252,
        min_periods: int = 120,
        max_holdings: int = 0,
        use_historical_holdings: bool = True,
    ) -> Dict[str, Any]:
        result = self.calculate_history(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            output_start_date=output_start_date,
            history_days=history_days,
            score_window=score_window,
            min_periods=min_periods,
            max_holdings=max_holdings,
            use_historical_holdings=use_historical_holdings,
        )

        db = Session()
        saved = 0
        try:
            for record in result["records"]:
                day = datetime.strptime(record["date"], "%Y-%m-%d").date()
                holdings_as_of = (
                    datetime.strptime(record["holdings_as_of"], "%Y-%m-%d").date()
                    if record.get("holdings_as_of")
                    else None
                )
                components = record["components"]
                price = record["etf_price"]
                db.merge(
                    ETFFearGreedCloneHistory(
                        symbol=record["symbol"],
                        date=day,
                        score=record["score"],
                        rating=record["rating"],
                        method=record["method"],
                        history_days=record["history_days"],
                        score_window=record["score_window"],
                        min_periods=record["min_periods"],
                        use_historical_holdings=record["use_historical_holdings"],
                        etf_open=price.get("open"),
                        etf_high=price.get("high"),
                        etf_low=price.get("low"),
                        etf_close=price.get("close"),
                        etf_volume=price.get("volume"),
                        etf_turnover=price.get("turnover"),
                        holdings_as_of=holdings_as_of,
                        holdings_count=record["holdings_count"],
                        holdings_weight_used=record["holdings_weight_used"],
                        market_momentum_score=components["market_momentum"]["score"],
                        market_momentum_raw=components["market_momentum"]["raw_value"],
                        stock_price_strength_score=components["stock_price_strength"]["score"],
                        stock_price_strength_raw=components["stock_price_strength"]["raw_value"],
                        stock_price_breadth_score=components["stock_price_breadth"]["score"],
                        stock_price_breadth_raw=components["stock_price_breadth"]["raw_value"],
                        put_call_options_score=components["put_call_options"]["score"],
                        put_call_options_raw=components["put_call_options"]["raw_value"],
                        market_volatility_score=components["market_volatility"]["score"],
                        market_volatility_raw=components["market_volatility"]["raw_value"],
                        safe_haven_demand_score=components["safe_haven_demand"]["score"],
                        safe_haven_demand_raw=components["safe_haven_demand"]["raw_value"],
                        junk_bond_demand_score=components["junk_bond_demand"]["score"],
                        junk_bond_demand_raw=components["junk_bond_demand"]["raw_value"],
                        components=components,
                        warnings=record["warnings"],
                        updated_at=datetime.now(),
                    )
                )
                db.query(ETFFearGreedCloneHolding).filter(
                    and_(
                        ETFFearGreedCloneHolding.symbol == record["symbol"],
                        ETFFearGreedCloneHolding.date == day,
                    )
                ).delete(synchronize_session=False)
                for holding in record["holdings"]:
                    db.merge(
                        ETFFearGreedCloneHolding(
                            symbol=record["symbol"],
                            date=day,
                            holding_symbol=holding["symbol"],
                            holdings_as_of=holdings_as_of,
                            name=holding["name"],
                            asset_class=holding["asset_class"],
                            shares=holding["shares"],
                            market_value=holding["market_value"],
                            weight=holding["weight"],
                            price=holding["price"],
                            updated_at=datetime.now(),
                        )
                    )
                saved += 1
                if saved % 50 == 0:
                    db.commit()
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            Session.remove()

        return {
            "symbol": result["symbol"],
            "start_date": result["start_date"],
            "end_date": result["end_date"],
            "count": result["count"],
            "saved": saved,
            "latest_holdings_as_of": result["latest_holdings_as_of"],
            "latest_holdings_count": result["latest_holdings_count"],
        }

    def load_history_from_db(
        self,
        symbol: str = "SOXX.US",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        include_components: bool = True,
        include_latest_holdings: bool = True,
    ) -> Dict[str, Any]:
        etf_symbol = self._normalize_etf_symbol(symbol)
        db = Session()
        try:
            query = db.query(ETFFearGreedCloneHistory).filter(
                ETFFearGreedCloneHistory.symbol == etf_symbol
            )
            if start_date:
                query = query.filter(ETFFearGreedCloneHistory.date >= start_date)
            if end_date:
                query = query.filter(ETFFearGreedCloneHistory.date <= end_date)
            rows = query.order_by(ETFFearGreedCloneHistory.date.asc()).all()
            data = [self._db_history_row_payload(row, include_components) for row in rows]
            # A股指数且有 proxy_etf：历史详情价格曲线/成交量用场内 ETF 日线替换指数点位/指数量
            proxy_etf = _a_stock_proxy_etf_map().get(etf_symbol)
            proxy_etf_used: Optional[str] = None
            if proxy_etf and data:
                first_day = datetime.strptime(data[0]["date"], "%Y-%m-%d").date()
                last_day = datetime.strptime(data[-1]["date"], "%Y-%m-%d").date()
                bars = _load_proxy_etf_bars(
                    [proxy_etf],
                    first_day - timedelta(days=30),
                    last_day,
                    use_qfq=True,
                ).get(proxy_etf)
                # 仅当 ETF 日线完整覆盖历史区间时才替换，避免新上市 ETF 早期缺数据导致
                # 指数点位与 ETF 价格混在一张图里（比例尺断裂）。
                if (
                    bars is not None
                    and not bars.empty
                    and bars.index.min().date() <= first_day
                    and bars.index.max().date() >= last_day
                ):
                    history_dates = pd.DatetimeIndex(
                        datetime.strptime(item["date"], "%Y-%m-%d")
                        for item in data
                    )
                    etf_reindexed = bars[
                        ["open", "high", "low", "close", "volume", "turnover"]
                    ].reindex(history_dates).ffill()
                    bar_dates = set(bars.index.date)
                    proxy_etf_used = proxy_etf
                    for item, ts in zip(data, history_dates):
                        row = etf_reindexed.loc[ts]
                        had_bar = ts.date() in bar_dates
                        item["etf_price"] = {
                            "open": _finite_or_none(row["open"]),
                            "high": _finite_or_none(row["high"]),
                            "low": _finite_or_none(row["low"]),
                            "close": _finite_or_none(row["close"]),
                            # 停牌/无成交日不显示成交量柱
                            "volume": (
                                _finite_or_none(row["volume"]) if had_bar else None
                            ),
                            "turnover": (
                                _finite_or_none(row["turnover"]) if had_bar else None
                            ),
                        }
            latest = data[-1] if data else None
            latest_holdings: List[Dict[str, Any]] = []
            if latest and include_latest_holdings:
                latest_date = datetime.strptime(latest["date"], "%Y-%m-%d").date()
                latest_holdings = [
                    self._db_holding_payload(row)
                    for row in db.query(ETFFearGreedCloneHolding)
                    .filter(
                        ETFFearGreedCloneHolding.symbol == etf_symbol,
                        ETFFearGreedCloneHolding.date == latest_date,
                    )
                    .order_by(desc(ETFFearGreedCloneHolding.weight))
                    .all()
                ]

            return {
                "symbol": etf_symbol,
                "start_date": data[0]["date"] if data else None,
                "end_date": data[-1]["date"] if data else None,
                "count": len(data),
                "latest": latest,
                "latest_holdings": latest_holdings,
                "proxy_etf": proxy_etf_used,
                "data": data,
            }
        finally:
            Session.remove()

    def load_summaries_from_db(self, symbols: List[str]) -> Dict[str, Any]:
        normalized_symbols = [
            self._normalize_etf_symbol(symbol)
            for symbol in dict.fromkeys(symbols or [])
            if str(symbol or "").strip()
        ]
        proxy_map = _a_stock_proxy_etf_map()
        proxy_symbols = [
            symbol for symbol in normalized_symbols if symbol in proxy_map
        ]
        db = Session()
        try:
            # A股指数：量比改用对应 proxy_etf 的成交量（以各指数最新恐贪日为锚点）
            proxy_volume_by_symbol: Dict[
                str, List[Tuple[date, Optional[float]]]
            ] = {}
            if proxy_symbols:
                anchors = {
                    symbol: anchor_date
                    for symbol, anchor_date in (
                        db.query(
                            ETFFearGreedCloneHistory.symbol,
                            func.max(ETFFearGreedCloneHistory.date),
                        )
                        .filter(ETFFearGreedCloneHistory.symbol.in_(proxy_symbols))
                        .group_by(ETFFearGreedCloneHistory.symbol)
                        .all()
                    )
                    if anchor_date
                }
                if anchors:
                    min_date = min(anchors.values()) - timedelta(days=120)
                    max_date = max(anchors.values())
                    bars_by_etf = _load_proxy_etf_bars(
                        sorted({proxy_map[symbol] for symbol in anchors}),
                        min_date,
                        max_date,
                        use_qfq=False,
                    )
                    for symbol, anchor in anchors.items():
                        frame = bars_by_etf.get(proxy_map[symbol])
                        if frame is None or frame.empty:
                            continue
                        upto = frame[frame.index <= pd.Timestamp(anchor)]
                        if upto.empty:
                            continue
                        # 恐贪最新日及之前最近 21 个交易日的 ETF 成交量（最新在前）
                        proxy_volume_by_symbol[symbol] = [
                            (ts.date(), float(row.volume))
                            for ts, row in upto.tail(21).iloc[::-1].iterrows()
                            if pd.notna(row.volume)
                        ]
            summaries = [
                self._db_summary_payload(
                    db,
                    symbol,
                    proxy_volume_history=proxy_volume_by_symbol.get(symbol),
                )
                for symbol in normalized_symbols
            ]
            return {
                "symbols": normalized_symbols,
                "count": len(summaries),
                "data": summaries,
            }
        finally:
            Session.remove()

    def _build_raw_signals(
        self,
        etf_symbol: str,
        holdings_by_date: Dict[pd.Timestamp, List[ETFHolding]],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        etf_prices = self._fetch_price_history(etf_symbol, start_date, end_date)
        index = pd.DatetimeIndex(etf_prices.index, name="date")
        etf_prices = etf_prices.reindex(index).ffill()
        tlt_prices = self._fetch_price_history(
            "TLT.US", start_date, end_date
        ).reindex(index).ffill()
        fred = self._fetch_fred(["BAMLH0A0HYM2", "BAMLC0A0CM"]).reindex(index).ffill()
        put_call = self._fetch_symbol_put_call_ratio(
            etf_symbol, start_date, end_date
        ).reindex(index).ffill(limit=3)

        unique_holdings = self._unique_holdings(holdings_by_date)
        holding_frames: Dict[str, pd.DataFrame] = {}
        for holding in unique_holdings:
            try:
                frame = self._fetch_price_history(
                    holding.symbol, start_date, end_date
                ).reindex(index).ffill()
            except Exception:
                continue
            if frame["close"].notna().sum() >= 120:
                holding_frames[holding.symbol] = frame

        if not holding_frames:
            raise FearGreedCloneError(f"LongPort returned no usable holding history for {etf_symbol}")

        etf_close = etf_prices["close"]
        etf_returns = etf_close.pct_change()
        realized_vol = etf_returns.rolling(20).std() * np.sqrt(252)

        df = pd.DataFrame(index=index)
        df["market_momentum"] = etf_close / etf_close.rolling(125).mean() - 1.0
        df["stock_price_strength"] = self._weighted_range_position(
            holding_frames, holdings_by_date, index
        )
        df["stock_price_breadth"] = self._weighted_advancing_volume_ratio(
            holding_frames, holdings_by_date, index
        )
        df["put_call_options"] = -put_call.rolling(5, min_periods=3).mean()
        df["market_volatility"] = -(realized_vol / realized_vol.rolling(50).mean() - 1.0)
        df["safe_haven_demand"] = etf_close.pct_change(20) - tlt_prices["close"].pct_change(20)
        df["junk_bond_demand"] = -(fred["BAMLH0A0HYM2"] - fred["BAMLC0A0CM"])
        for column in ("open", "high", "low", "close", "volume", "turnover"):
            df[f"etf_{column}"] = etf_prices[column]

        return df.replace([np.inf, -np.inf], np.nan)

    def _build_holdings_by_date(
        self,
        etf_symbol: str,
        start_date: date,
        end_date: date,
        max_holdings: int,
        use_historical_holdings: bool,
    ) -> Tuple[Dict[pd.Timestamp, List[ETFHolding]], Dict[pd.Timestamp, date], date, List[ETFHolding]]:
        index = pd.DatetimeIndex(
            self._fetch_price_history(etf_symbol, start_date, end_date).index,
            name="date",
        )
        holdings_by_date: Dict[pd.Timestamp, List[ETFHolding]] = {}
        holdings_as_of_by_date: Dict[pd.Timestamp, date] = {}
        latest_holdings: List[ETFHolding] = []
        holdings_as_of: Optional[date] = None
        for timestamp in index:
            day = timestamp.date()
            holdings, holdings_as_of = self._load_latest_db_holdings_on_or_before(
                etf_symbol, day, max_holdings=max_holdings
            )
            latest_holdings = holdings
            holdings_by_date[timestamp] = holdings
            holdings_as_of_by_date[timestamp] = holdings_as_of

        if not holdings_by_date or not latest_holdings or holdings_as_of is None:
            raise FearGreedCloneError(f"no DB holdings found for {etf_symbol}")

        return holdings_by_date, holdings_as_of_by_date, holdings_as_of, latest_holdings

    def _load_db_holdings_for_day(
        self,
        etf_symbol: str,
        day: date,
        max_holdings: int,
    ) -> List[ETFHolding]:
        db = Session()
        try:
            rows = (
                db.query(DBETFHolding)
                .filter(
                    DBETFHolding.etf_symbol == etf_symbol,
                    DBETFHolding.date == day,
                )
                .order_by(DBETFHolding.weight.desc())
                .all()
            )
        finally:
            Session.remove()

        if max_holdings > 0:
            rows = rows[:max_holdings]
        holdings = []
        for row in rows:
            if not row.symbol or row.asset_class != "Equity" or (row.weight or 0) <= 0:
                continue
            symbol = normalize_us_equity_symbol(row.symbol)
            if not symbol:
                logging.warning(
                    "跳过无法规范化的 DB ETF 持仓股票代码: %s %s %s",
                    etf_symbol,
                    day.isoformat(),
                    row.symbol,
                )
                continue
            holdings.append(
                ETFHolding(
                    symbol=symbol,
                    name=row.name,
                    asset_class=row.asset_class,
                    shares=int(row.shares or 0),
                    weight=float(row.weight or 0.0),
                    market_value=0.0,
                    price=None,
                )
            )
        if not holdings:
            raise FearGreedCloneError(
                f"{etf_symbol} {day.isoformat()} holdings missing in etf_holdings; "
                "please run ETF历史持仓回跑 first"
            )
        return holdings

    def _load_latest_db_holdings_on_or_before(
        self,
        etf_symbol: str,
        day: date,
        max_holdings: int,
    ) -> Tuple[List[ETFHolding], date]:
        db = Session()
        try:
            latest_date = (
                db.query(func.max(DBETFHolding.date))
                .filter(
                    DBETFHolding.etf_symbol == etf_symbol,
                    DBETFHolding.date <= day,
                )
                .scalar()
            )
            if not latest_date:
                raise FearGreedCloneError(
                    f"{etf_symbol} has no holdings in etf_holdings on or before {day.isoformat()}; "
                    "please run ETF历史持仓回跑 first"
                )
            rows = (
                db.query(DBETFHolding)
                .filter(
                    DBETFHolding.etf_symbol == etf_symbol,
                    DBETFHolding.date == latest_date,
                )
                .order_by(DBETFHolding.weight.desc())
                .all()
            )
        finally:
            Session.remove()

        if max_holdings > 0:
            rows = rows[:max_holdings]
        holdings = []
        for row in rows:
            if not row.symbol or row.asset_class != "Equity" or (row.weight or 0) <= 0:
                continue
            symbol = normalize_us_equity_symbol(row.symbol)
            if not symbol:
                logging.warning(
                    "跳过无法规范化的 DB ETF 持仓股票代码: %s %s %s",
                    etf_symbol,
                    latest_date.isoformat(),
                    row.symbol,
                )
                continue
            holdings.append(
                ETFHolding(
                    symbol=symbol,
                    name=row.name,
                    asset_class=row.asset_class,
                    shares=int(row.shares or 0),
                    weight=float(row.weight or 0.0),
                    market_value=0.0,
                    price=None,
                )
            )
        if not holdings:
            raise FearGreedCloneError(
                f"{etf_symbol} {latest_date.isoformat()} holdings in etf_holdings have no usable equity rows"
            )
        return holdings, latest_date

    def _get_current_holdings(
        self, etf_symbol: str, max_holdings: int
    ) -> Tuple[List[ETFHolding], date, float]:
        fetcher = self._get_holdings_fetcher(etf_symbol)

        holdings_data = fetcher.get_holdings(etf_symbol)
        holdings = [
            holding
            for holding in holdings_data.holdings
            if holding.asset_class == "Equity"
            and holding.weight > 0
            and self._nasdaq_symbol(holding.symbol)
        ]
        holdings.sort(key=lambda item: item.weight, reverse=True)
        if max_holdings > 0:
            holdings = holdings[:max_holdings]
        if not holdings:
            raise FearGreedCloneError(f"no equity holdings found for {etf_symbol}")

        total_weight = sum(holding.weight for holding in holdings)
        return holdings, holdings_data.update_date, total_weight

    def _get_holdings_fetcher(self, etf_symbol: str):
        fetcher = self.holdings_fetchers.get(etf_symbol)
        if not fetcher:
            raise FearGreedCloneError(f"unsupported ETF for holdings: {etf_symbol}")
        return fetcher

    def _supports_historical_holdings(self, etf_symbol: str) -> bool:
        return etf_symbol in self.ishares_fetcher.ETF_CONFIGS

    def _get_historical_holdings_for_day(
        self,
        etf_symbol: str,
        day: date,
        max_holdings: int,
    ) -> List[ETFHolding]:
        if etf_symbol not in self.ishares_fetcher.ETF_CONFIGS:
            raise FearGreedCloneError(f"unsupported ETF for iShares holdings: {etf_symbol}")

        payload = self._fetch_ishares_holdings_json(etf_symbol, day)
        holdings = [
            holding
            for holding in self._parse_ishares_holdings_json(payload)
            if holding.asset_class == "Equity"
            and holding.weight > 0
            and self._nasdaq_symbol(holding.symbol)
        ]
        holdings.sort(key=lambda item: item.weight, reverse=True)
        if max_holdings > 0:
            holdings = holdings[:max_holdings]
        return holdings

    def _fetch_ishares_holdings_json(self, etf_symbol: str, day: date) -> Dict[str, Any]:
        cache_file = (
            self.cache_dir
            / "ishares_holdings"
            / self._cache_key(etf_symbol)
            / f"{day.strftime('%Y%m%d')}.json"
        )
        payload: Optional[Dict[str, Any]] = None
        if cache_file.exists():
            try:
                with cache_file.open("r", encoding="utf-8") as file:
                    payload = json.load(file)
                if isinstance(payload, dict) and payload.get("aaData") == []:
                    cache_file.unlink(missing_ok=True)
                    payload = None
            except json.JSONDecodeError:
                cache_file.unlink(missing_ok=True)

        if payload is None:
            response = self._get(
                self.ishares_fetcher.ETF_CONFIGS[etf_symbol]["url"],
                params={
                    "fileType": "json",
                    "tab": "all",
                    "asOfDate": day.strftime("%Y%m%d"),
                },
                headers={
                    "User-Agent": self.ishares_fetcher.headers.get("User-Agent", ""),
                    "Accept": "application/json, text/plain, */*",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("aaData"):
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file = cache_file.with_suffix(".tmp")
                with temp_file.open("w", encoding="utf-8") as file:
                    json.dump(payload, file)
                temp_file.replace(cache_file)
            time.sleep(0.03)

        return payload

    def _parse_ishares_holdings_json(self, payload: Dict[str, Any]) -> List[ETFHolding]:
        holdings: List[ETFHolding] = []
        for row in payload.get("aaData", []):
            try:
                symbol = str(row[0]).strip()
                name = str(row[1]).strip()
                asset_class = str(row[3]).strip()
                market_value = self._ishares_raw_number(row[4])
                weight = self._ishares_raw_number(row[5]) / 100.0
                shares = int(self._ishares_raw_number(row[7], default=0.0))
                price = self._ishares_raw_number(row[11], default=np.nan)
                exchange = str(row[13]).strip()
                market_suffix = self.ishares_fetcher.exchange_map.get(exchange, "")

                is_equity = asset_class == "Equity" and not any(
                    char.isdigit() or char == "-" for char in symbol
                )
                normalized_symbol = symbol + market_suffix if is_equity else symbol
                holdings.append(
                    ETFHolding(
                        symbol=normalized_symbol,
                        name=name,
                        asset_class=(
                            "Other"
                            if not is_equity and asset_class == "Equity"
                            else asset_class
                        ),
                        shares=shares,
                        market_value=market_value,
                        weight=weight,
                        price=None if pd.isna(price) else price,
                    )
                )
            except (IndexError, TypeError, ValueError):
                continue
        return holdings

    def _score_etf_raw_signals(
        self,
        raw: pd.DataFrame,
        score_window: int,
        min_periods: int,
    ) -> pd.DataFrame:
        result = raw.copy()
        for key in ETF_COMPONENTS:
            series = raw[key]
            mean = series.rolling(score_window, min_periods=min_periods).mean()
            std = series.rolling(score_window, min_periods=min_periods).std(ddof=0)
            z_score = (series - mean) / std.replace(0, np.nan)
            result[f"{key}_score"] = (100.0 * self._normal_cdf(z_score)).clip(0, 100)
        return result

    def _weighted_range_position(
        self,
        holding_frames: Dict[str, pd.DataFrame],
        holdings_by_date: Dict[pd.Timestamp, List[ETFHolding]],
        index: pd.DatetimeIndex,
    ) -> pd.Series:
        position_by_symbol: Dict[str, pd.Series] = {}
        for symbol, frame in holding_frames.items():
            high_52w = frame["high"].rolling(252, min_periods=120).max()
            low_52w = frame["low"].rolling(252, min_periods=120).min()
            position = (frame["close"] - low_52w) / (high_52w - low_52w)
            position_by_symbol[symbol] = position.clip(0.0, 1.0)

        values = []
        for timestamp in index:
            weighted_sum = 0.0
            valid_weight = 0.0
            for holding in holdings_by_date.get(timestamp, []):
                series = position_by_symbol.get(holding.symbol)
                if series is None or timestamp not in series.index:
                    continue
                value = series.loc[timestamp]
                if pd.isna(value):
                    continue
                weighted_sum += float(value) * holding.weight
                valid_weight += holding.weight
            values.append(weighted_sum / valid_weight if valid_weight else np.nan)
        return pd.Series(values, index=index)

    def _weighted_advancing_volume_ratio(
        self,
        holding_frames: Dict[str, pd.DataFrame],
        holdings_by_date: Dict[pd.Timestamp, List[ETFHolding]],
        index: pd.DatetimeIndex,
    ) -> pd.Series:
        change_by_symbol = {
            symbol: frame["close"].diff() for symbol, frame in holding_frames.items()
        }
        dollar_volume_by_symbol = {
            symbol: (frame["close"] * frame["volume"]).fillna(0.0)
            for symbol, frame in holding_frames.items()
        }

        values = []
        for timestamp in index:
            up_volume = 0.0
            down_volume = 0.0
            for holding in holdings_by_date.get(timestamp, []):
                change = change_by_symbol.get(holding.symbol)
                dollar_volume = dollar_volume_by_symbol.get(holding.symbol)
                if (
                    change is None
                    or dollar_volume is None
                    or timestamp not in change.index
                    or timestamp not in dollar_volume.index
                ):
                    continue
                daily_change = change.loc[timestamp]
                weighted_volume = dollar_volume.loc[timestamp] * holding.weight
                if pd.isna(daily_change) or pd.isna(weighted_volume):
                    continue
                if daily_change > 0:
                    up_volume += float(weighted_volume)
                elif daily_change < 0:
                    down_volume += float(weighted_volume)

            total_volume = up_volume + down_volume
            values.append(up_volume / total_volume if total_volume else np.nan)

        ratio = pd.Series(values, index=index)
        return ratio.rolling(5, min_periods=3).mean()

    @staticmethod
    def _unique_holdings(
        holdings_by_date: Dict[pd.Timestamp, List[ETFHolding]]
    ) -> List[ETFHolding]:
        by_symbol: Dict[str, ETFHolding] = {}
        for holdings in holdings_by_date.values():
            for holding in holdings:
                if holding.symbol not in by_symbol or holding.weight > by_symbol[holding.symbol].weight:
                    by_symbol[holding.symbol] = holding
        return list(by_symbol.values())

    def _fetch_price_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        cache_key = (symbol, start_date, end_date)
        if cache_key in self.price_cache:
            return self.price_cache[cache_key]

        klines = self.quote_service.get_klines(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )
        if not klines:
            raise FearGreedCloneError(f"{symbol} 在指定区间内没有 K 线数据")

        frame = pd.DataFrame(
            [
                {
                    "date": (
                        item["timestamp"].date()
                        if hasattr(item["timestamp"], "date")
                        else item["timestamp"]
                    ),
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"]),
                    "volume": float(item["volume"]),
                    "turnover": float(item.get("turnover", 0.0)),
                }
                for item in klines
            ]
        ).sort_values("date")
        if frame.empty:
            raise FearGreedCloneError(f"{symbol} 在指定区间内没有可用 K 线")

        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.drop_duplicates(subset=["date"], keep="last").set_index("date")
        self.price_cache[cache_key] = frame.sort_index()
        return self.price_cache[cache_key]

    def _fetch_recent_price_history(
        self,
        symbol: str,
        count: int,
        end_date: date,
    ) -> pd.DataFrame:
        start_date = end_date - timedelta(days=max(60, count * 3))
        klines = self.quote_service.get_klines(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )[-count:]
        if not klines:
            raise FearGreedCloneError(f"{symbol} 在指定区间内没有 K 线数据")

        frame = pd.DataFrame(
            [
                {
                    "date": (
                        item["timestamp"].date()
                        if hasattr(item["timestamp"], "date")
                        else item["timestamp"]
                    ),
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"]),
                    "volume": float(item["volume"]),
                    "turnover": float(item.get("turnover", 0.0)),
                }
                for item in klines
            ]
        ).sort_values("date")
        if frame.empty:
            raise FearGreedCloneError(f"{symbol} 在指定区间内没有可用 K 线")

        frame["date"] = pd.to_datetime(frame["date"])
        return frame.drop_duplicates(subset=["date"], keep="last").set_index("date").sort_index()

    def _load_component_raw_history_from_db(
        self,
        etf_symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        db = Session()
        try:
            rows = (
                db.query(ETFFearGreedCloneHistory)
                .filter(
                    ETFFearGreedCloneHistory.symbol == etf_symbol,
                    ETFFearGreedCloneHistory.date >= start_date,
                    ETFFearGreedCloneHistory.date <= end_date,
                )
                .order_by(ETFFearGreedCloneHistory.date.asc())
                .all()
            )
            records = [
                {
                    "date": row.date,
                    "market_momentum": row.market_momentum_raw,
                    "stock_price_strength": row.stock_price_strength_raw,
                    "stock_price_breadth": row.stock_price_breadth_raw,
                    "put_call_options": row.put_call_options_raw,
                    "market_volatility": row.market_volatility_raw,
                    "safe_haven_demand": row.safe_haven_demand_raw,
                    "junk_bond_demand": row.junk_bond_demand_raw,
                }
                for row in rows
            ]
        finally:
            Session.remove()

        if not records:
            return pd.DataFrame(columns=list(ETF_COMPONENTS.keys()))

        frame = pd.DataFrame(records)
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.set_index("date").sort_index().replace([np.inf, -np.inf], np.nan)

    def _build_realtime_raw_row(
        self,
        etf_symbol: str,
        holdings: List[ETFHolding],
        quote_map: Dict[str, Dict[str, Any]],
        current_date: date,
        previous_trading_day: date,
        price_history_count: int,
        fetch_holdings_quotes: bool = True,
    ) -> Tuple[Dict[str, float], Dict[str, Optional[float]], Dict[str, str]]:
        index = pd.bdate_range(
            previous_trading_day - timedelta(days=max(price_history_count * 2, 420)),
            current_date,
            name="date",
        )
        current_timestamp = pd.Timestamp(current_date)

        etf_prices = self._append_realtime_quote(
            self._fetch_recent_price_history(
                etf_symbol,
                count=price_history_count,
                end_date=previous_trading_day,
            ),
            current_date,
            quote_map.get(etf_symbol),
        ).reindex(index).ffill()
        tlt_prices = self._append_realtime_quote(
            self._fetch_recent_price_history(
                "TLT.US",
                count=price_history_count,
                end_date=previous_trading_day,
            ),
            current_date,
            quote_map.get("TLT.US"),
        ).reindex(index).ffill()

        holding_frames: Dict[str, pd.DataFrame] = {}
        if fetch_holdings_quotes:
            for holding in holdings:
                try:
                    frame = self._fetch_recent_price_history(
                        holding.symbol,
                        count=price_history_count,
                        end_date=previous_trading_day,
                    )
                    frame = self._append_realtime_quote(
                        frame,
                        current_date,
                        quote_map.get(holding.symbol),
                    ).reindex(index).ffill()
                except Exception:
                    continue
                if frame["close"].notna().sum() >= 120:
                    holding_frames[holding.symbol] = frame

            if not holding_frames:
                raise FearGreedCloneError(f"LongPort returned no usable holding history for {etf_symbol}")

        holdings_by_date = {timestamp: holdings for timestamp in index}
        etf_close = etf_prices["close"]
        etf_returns = etf_close.pct_change()
        realized_vol = etf_returns.rolling(20).std() * np.sqrt(252)

        raw = pd.DataFrame(index=index)
        raw["market_momentum"] = etf_close / etf_close.rolling(125).mean() - 1.0
        if holding_frames:
            raw["stock_price_strength"] = self._weighted_range_position(
                holding_frames, holdings_by_date, index
            )
            raw["stock_price_breadth"] = self._weighted_advancing_volume_ratio(
                holding_frames, holdings_by_date, index
            )
        else:
            # 轻量模式：不取成分股行情，强度/宽度在调用方沿用最近入库日线值
            raw["stock_price_strength"] = np.nan
            raw["stock_price_breadth"] = np.nan
        raw["market_volatility"] = -(realized_vol / realized_vol.rolling(50).mean() - 1.0)
        raw["safe_haven_demand"] = etf_close.pct_change(20) - tlt_prices["close"].pct_change(20)

        latest_daily = self._latest_stored_component_raw(etf_symbol)
        raw["put_call_options"] = np.nan
        raw["junk_bond_demand"] = np.nan
        raw_freshness = {
            "market_momentum": "LongPort realtime quote",
            "stock_price_strength": (
                "LongPort realtime holding quotes"
                if fetch_holdings_quotes
                else "latest stored constituent strength component"
            ),
            "stock_price_breadth": (
                "LongPort realtime holding quotes"
                if fetch_holdings_quotes
                else "latest stored constituent breadth component"
            ),
            "market_volatility": "LongPort realtime quote",
            "safe_haven_demand": "LongPort realtime quote",
        }
        (
            realtime_put_call,
            put_call_snapshot_date,
            put_call_source,
        ) = self._fetch_realtime_barchart_put_call_raw(
            etf_symbol=etf_symbol,
            current_date=current_date,
            previous_trading_day=previous_trading_day,
        )
        if pd.notna(realtime_put_call):
            raw.loc[current_timestamp, "put_call_options"] = realtime_put_call
            snapshot_detail = (
                f" ({put_call_snapshot_date.isoformat()})"
                if put_call_snapshot_date
                else ""
            )
            raw_freshness["put_call_options"] = (
                f"{put_call_source or 'Barchart expiration snapshot'}{snapshot_detail}"
            )
        if latest_daily:
            raw.loc[current_timestamp, "junk_bond_demand"] = latest_daily.get("junk_bond_demand")
            raw_freshness["junk_bond_demand"] = "latest stored FRED credit-spread component"

        current = raw.loc[current_timestamp].replace([np.inf, -np.inf], np.nan)
        raw_values = {
            key: float(current[key]) if pd.notna(current[key]) else np.nan
            for key in ETF_COMPONENTS
        }
        price_payload = self._realtime_price_payload(etf_prices.loc[current_timestamp], quote_map.get(etf_symbol))
        return raw_values, price_payload, raw_freshness

    def _fetch_realtime_barchart_put_call_raw(
        self,
        etf_symbol: str,
        current_date: date,
        previous_trading_day: date,
    ) -> Tuple[float, Optional[date], Optional[str]]:
        try:
            snapshot = self._fetch_live_barchart_expiration_put_call_snapshot(etf_symbol)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to fetch live Barchart option expiration snapshot for %s: %s",
                etf_symbol,
                exc,
            )
            snapshot = self._today_db_barchart_expiration_put_call_snapshot(etf_symbol)

        try:
            if not snapshot:
                return np.nan, None, None

            current_ratio = snapshot.get("put_call_volume_ratio")
            snapshot_date = snapshot.get("snapshot_date")
            source = snapshot.get("source")
            if current_ratio is None or not np.isfinite(float(current_ratio)):
                return np.nan, snapshot_date, source

            history = self._fetch_db_put_call_ratio(
                etf_symbol,
                start_date=current_date - timedelta(days=14),
                end_date=previous_trading_day,
            )
            recent_values = [
                float(value)
                for value in history.sort_index().tail(4).tolist()
                if np.isfinite(float(value))
            ]
            recent_values.append(float(current_ratio))
            if len(recent_values) < 3:
                return np.nan, snapshot_date, source
            return -float(np.mean(recent_values)), snapshot_date, source
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to build realtime Barchart option put/call raw for %s: %s",
                etf_symbol,
                exc,
            )
            return np.nan, None, None

    def _fetch_live_barchart_expiration_put_call_snapshot(
        self,
        etf_symbol: str,
    ) -> Optional[Dict[str, Any]]:
        ticker = self._nasdaq_symbol(etf_symbol)
        barchart = BarchartService(timeout=self.timeout)
        try:
            rows = barchart.get_options_expirations(
                ticker,
                page_limit=1000,
                sleep_seconds=0,
            )
            return self._build_barchart_expiration_put_call_snapshot(
                etf_symbol=etf_symbol,
                rows=rows,
                snapshot_date=date.today(),
                source="Barchart live expiration snapshot",
            )
        finally:
            barchart.close()

    def _today_db_barchart_expiration_put_call_snapshot(
        self,
        etf_symbol: str,
    ) -> Optional[Dict[str, Any]]:
        ticker = self._nasdaq_symbol(etf_symbol)
        today = date.today()
        db = Session()
        try:
            rows = (
                db.query(ETFOptionExpiration)
                .filter(
                    ETFOptionExpiration.symbol == ticker,
                    ETFOptionExpiration.snapshot_date == today,
                )
                .all()
            )
            return self._build_barchart_expiration_put_call_snapshot(
                etf_symbol=etf_symbol,
                rows=rows,
                snapshot_date=today,
                source="today's local Barchart expiration snapshot",
            )
        finally:
            Session.remove()

    def _build_barchart_expiration_put_call_snapshot(
        self,
        etf_symbol: str,
        rows: List[Any],
        snapshot_date: date,
        source: str,
    ) -> Optional[Dict[str, Any]]:
        put_volume = 0.0
        call_volume = 0.0
        total_volume = 0.0
        row_count = 0

        for row in rows or []:
            getter = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
            put_volume += self._number(getter("putVolume", getter("put_volume")), default=0.0)
            call_volume += self._number(getter("callVolume", getter("call_volume")), default=0.0)
            total_volume += self._number(getter("totalVolume", getter("total_volume")), default=0.0)
            row_count += 1

        if row_count == 0 or call_volume <= 0:
            return None

        return {
            "symbol": etf_symbol,
            "source": source,
            "snapshot_date": snapshot_date,
            "expiration_count": row_count,
            "put_volume": put_volume,
            "call_volume": call_volume,
            "total_volume": total_volume,
            "put_call_volume_ratio": put_volume / call_volume,
        }

    def _latest_stored_component_raw(self, etf_symbol: str) -> Dict[str, Optional[float]]:
        db = Session()
        try:
            row = (
                db.query(ETFFearGreedCloneHistory)
                .filter(ETFFearGreedCloneHistory.symbol == etf_symbol)
                .order_by(ETFFearGreedCloneHistory.date.desc())
                .first()
            )
            if not row:
                return {}
            return {
                "put_call_options": row.put_call_options_raw,
                "junk_bond_demand": row.junk_bond_demand_raw,
            }
        finally:
            Session.remove()

    def _append_realtime_quote(
        self,
        frame: pd.DataFrame,
        current_date: date,
        quote: Optional[Dict[str, Any]],
    ) -> pd.DataFrame:
        if quote is None or not self._quote_price(quote):
            return frame

        price = float(self._quote_price(quote))
        open_price = self._quote_float(quote, "open", price)
        high = self._quote_float(quote, "high", price)
        low = self._quote_float(quote, "low", price)
        volume = self._quote_float(quote, "volume", 0.0)
        turnover = self._quote_float(quote, "turnover", 0.0)
        current_ts = pd.Timestamp(current_date)
        next_frame = frame.copy()
        next_frame.loc[current_ts, ["open", "high", "low", "close", "volume", "turnover"]] = [
            open_price,
            max(high, price),
            min(low, price),
            price,
            volume,
            turnover,
        ]
        return next_frame.sort_index()

    def _fetch_realtime_quote_map(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        quote_map: Dict[str, Dict[str, Any]] = {}
        unique_symbols = list(dict.fromkeys(symbols))
        batch_size = 100
        for start in range(0, len(unique_symbols), batch_size):
            batch = unique_symbols[start : start + batch_size]
            quotes = self.quote_service.get_quote_batch(batch)
            for quote in quotes or []:
                symbol = quote.get("symbol")
                if symbol:
                    quote_map[symbol] = quote
        return quote_map

    def _fetch_symbol_put_call_ratio(
        self,
        etf_symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.Series:
        try:
            return self._fetch_db_put_call_ratio(etf_symbol, start_date, end_date)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to fetch DB option put/call for %s, fallback to Cboe ETP ratio: %s",
                etf_symbol,
                exc,
            )
            return self._fetch_cboe_ratio(
                start_date,
                end_date,
                ratio_name="EXCHANGE TRADED PRODUCTS PUT/CALL RATIO",
            )

    def _fetch_db_put_call_ratio(
        self,
        etf_symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.Series:
        ticker = self._nasdaq_symbol(etf_symbol)
        db = Session()
        try:
            rows = (
                db.query(ETFPutCallRatio)
                .filter(
                    ETFPutCallRatio.symbol == ticker,
                    ETFPutCallRatio.date >= start_date,
                    ETFPutCallRatio.date <= end_date,
                    ETFPutCallRatio.put_call_volume_ratio.isnot(None),
                )
                .order_by(ETFPutCallRatio.date.asc())
                .all()
            )
            values: List[Dict[str, Any]] = []
            for row in rows:
                ratio = float(row.put_call_volume_ratio)
                if not np.isfinite(ratio):
                    continue
                values.append({"date": pd.Timestamp(row.date), "put_call": ratio})

            if not values:
                raise FearGreedCloneError(f"DB returned no option put/call data for {etf_symbol}")

            frame = (
                pd.DataFrame(values)
                .drop_duplicates(subset=["date"], keep="last")
                .set_index("date")
                .sort_index()
            )
            return frame["put_call"]
        finally:
            Session.remove()

    def _fetch_evc_put_call_ratio(
        self,
        etf_symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.Series:
        rows = EVCService().option_put_call_history(self._nasdaq_symbol(etf_symbol))
        values: List[Dict[str, Any]] = []
        for item in rows:
            if not item.date or item.put_call_vol is None:
                continue
            if item.date < start_date or item.date > end_date:
                continue
            ratio = float(item.put_call_vol)
            if not np.isfinite(ratio):
                continue
            values.append({"date": pd.Timestamp(item.date), "put_call": ratio})

        if not values:
            raise FearGreedCloneError(f"EVC returned no option put/call data for {etf_symbol}")

        frame = (
            pd.DataFrame(values)
            .drop_duplicates(subset=["date"], keep="last")
            .set_index("date")
            .sort_index()
        )
        return frame["put_call"]

    def _fetch_cboe_ratio(
        self,
        start_date: date,
        end_date: date,
        ratio_name: str,
    ) -> pd.Series:
        values: List[Dict[str, Any]] = []
        for timestamp in pd.bdate_range(start_date, end_date):
            day = timestamp.date()
            ratio = self._fetch_cboe_ratio_for_day(day, ratio_name)
            if ratio is not None:
                values.append({"date": timestamp, "put_call": ratio})

        if not values:
            raise FearGreedCloneError(f"Cboe returned no {ratio_name} data")

        df = pd.DataFrame(values).set_index("date").sort_index()
        return df["put_call"]

    def _fetch_cboe_ratio_for_day(self, day: date, ratio_name: str) -> Optional[float]:
        cache_file = self.cache_dir / "cboe_put_call" / f"{day.isoformat()}.json"
        payload: Optional[Dict[str, Any]] = None

        if cache_file.exists():
            try:
                with cache_file.open("r", encoding="utf-8") as file:
                    payload = json.load(file)
            except json.JSONDecodeError:
                cache_file.unlink(missing_ok=True)

        if payload is None:
            url = CBOE_OPTIONS_DAILY_URL.format(date=day.isoformat())
            response = self._get(url)
            if response.status_code in (403, 404):
                return None
            response.raise_for_status()
            payload = response.json()
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = cache_file.with_suffix(".tmp")
            with temp_file.open("w", encoding="utf-8") as file:
                json.dump(payload, file)
            temp_file.replace(cache_file)

        for item in payload.get("ratios", []):
            if item.get("name") == ratio_name:
                return float(item["value"])
        return None

    def _safe_option_chain_snapshot(self, etf_symbol: str) -> Optional[Dict[str, Any]]:
        try:
            return self._fetch_option_chain_snapshot(etf_symbol)
        except Exception:
            return None

    def _fetch_option_chain_snapshot(self, etf_symbol: str) -> Dict[str, Any]:
        ticker = self._nasdaq_symbol(etf_symbol)
        response = self._get(
            NASDAQ_OPTION_CHAIN_URL.format(symbol=ticker),
            params={"assetclass": "etf", "limit": "9999"},
            headers={
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/",
            },
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        rows = data.get("table", {}).get("rows", [])

        call_volume = 0.0
        put_volume = 0.0
        call_open_interest = 0.0
        put_open_interest = 0.0
        for row in rows:
            call_volume += self._number(row.get("c_Volume"), default=0.0)
            put_volume += self._number(row.get("p_Volume"), default=0.0)
            call_open_interest += self._number(row.get("c_Openinterest"), default=0.0)
            put_open_interest += self._number(row.get("p_Openinterest"), default=0.0)

        snapshot = {
            "symbol": etf_symbol,
            "source": "Nasdaq option-chain current snapshot",
            "last_trade": data.get("lastTrade"),
            "total_contract_rows": data.get("totalRecord"),
            "call_volume": int(call_volume),
            "put_volume": int(put_volume),
            "put_call_volume_ratio": (
                round(put_volume / call_volume, 6) if call_volume > 0 else None
            ),
            "call_open_interest": int(call_open_interest),
            "put_open_interest": int(put_open_interest),
            "put_call_open_interest_ratio": (
                round(put_open_interest / call_open_interest, 6)
                if call_open_interest > 0
                else None
            ),
        }
        self._cache_option_chain_snapshot(snapshot)
        return snapshot

    def _cache_option_chain_snapshot(self, snapshot: Dict[str, Any]) -> None:
        last_trade = snapshot.get("last_trade") or ""
        match = re.search(r"AS OF ([A-Z]{3} \d{1,2}, \d{4})", last_trade)
        if not match:
            return
        snapshot_date = datetime.strptime(match.group(1).title(), "%b %d, %Y").date()
        cache_file = (
            self.cache_dir
            / "nasdaq_option_chain"
            / f"{self._cache_key(snapshot['symbol'])}_{snapshot_date.isoformat()}.json"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = cache_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(snapshot, file)
        temp_file.replace(cache_file)

    def _component_payload(
        self,
        raw: pd.DataFrame,
        scores: pd.DataFrame,
        latest_date: pd.Timestamp,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for key, spec in ETF_COMPONENTS.items():
            component_score = float(scores.loc[latest_date, f"{key}_score"])
            raw_value = float(raw.loc[latest_date, key])
            payload[key] = {
                "name": spec.name,
                "score": round(component_score, 2),
                "rating": self.rating(component_score),
                "raw_value": round(raw_value, 6),
                "raw_label": spec.raw_label,
                "source": spec.source,
                "proxy_note": spec.proxy_note,
            }
        return payload

    def _warnings(self, etf_symbol: str, use_historical_holdings: bool) -> List[str]:
        return [
            "This is an independent ETF-specific clone, not CNN's undisclosed calculation.",
            "Scores use rolling z-score/CDF and equal-weighted components.",
            (
                "Holdings-based components read the latest etf_holdings snapshot "
                "on or before each trading day; missing prior snapshots will raise an error."
            ),
            (
                "The option component uses local Barchart ETF put/call history "
                "from etf_put_call_ratios, with Cboe ETP put/call as an outage fallback."
            ),
            (
                "FRED ICE BofA credit-spread series currently provides only the "
                "latest three years of observations, which limits long backfills "
                "for the junk-bond-demand component."
            ),
        ]

    @staticmethod
    def _price_payload(row: pd.Series) -> Dict[str, Optional[float]]:
        payload: Dict[str, Optional[float]] = {}
        for key in ("open", "high", "low", "close", "volume", "turnover"):
            value = row.get(f"etf_{key}")
            payload[key] = None if pd.isna(value) else round(float(value), 6)
        return payload

    @staticmethod
    def _holdings_payload(holdings: List[ETFHolding]) -> List[Dict[str, Any]]:
        return [
            {
                "symbol": holding.symbol,
                "name": holding.name,
                "asset_class": holding.asset_class,
                "shares": holding.shares,
                "market_value": None
                if holding.market_value is None or pd.isna(holding.market_value)
                else round(float(holding.market_value), 4),
                "weight": round(float(holding.weight), 8),
                "price": None
                if holding.price is None or pd.isna(holding.price)
                else round(float(holding.price), 6),
            }
            for holding in holdings
        ]

    @staticmethod
    def _db_history_row_payload(
        row: ETFFearGreedCloneHistory,
        include_components: bool,
    ) -> Dict[str, Any]:
        payload = {
            "symbol": row.symbol,
            "date": row.date.isoformat(),
            "score": row.score,
            "rating": row.rating,
            "method": row.method,
            "history_days": row.history_days,
            "score_window": row.score_window,
            "min_periods": row.min_periods,
            "use_historical_holdings": row.use_historical_holdings,
            "etf_price": {
                "open": row.etf_open,
                "high": row.etf_high,
                "low": row.etf_low,
                "close": row.etf_close,
                "volume": row.etf_volume,
                "turnover": row.etf_turnover,
            },
            "holdings_as_of": row.holdings_as_of.isoformat() if row.holdings_as_of else None,
            "holdings_count": row.holdings_count,
            "holdings_weight_used": row.holdings_weight_used,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        if include_components:
            payload["components"] = row.components
        else:
            payload["component_scores"] = {
                "market_momentum": row.market_momentum_score,
                "stock_price_strength": row.stock_price_strength_score,
                "stock_price_breadth": row.stock_price_breadth_score,
                "put_call_options": row.put_call_options_score,
                "market_volatility": row.market_volatility_score,
                "safe_haven_demand": row.safe_haven_demand_score,
                "junk_bond_demand": row.junk_bond_demand_score,
            }
        return payload

    @classmethod
    def _db_summary_payload(
        cls,
        db,
        symbol: str,
        proxy_volume_history: Optional[List[Tuple[date, Optional[float]]]] = None,
    ) -> Dict[str, Any]:
        latest_row = (
            db.query(ETFFearGreedCloneHistory)
            .filter(ETFFearGreedCloneHistory.symbol == symbol)
            .order_by(ETFFearGreedCloneHistory.date.desc())
            .first()
        )
        if not latest_row:
            return {
                "symbol": symbol,
                "latest": None,
                "seven_day_ago": None,
                "one_month_ago": None,
                "score_change_7d": None,
                "score_change_1m": None,
                "price_change_7d_pct": None,
                "price_change_1m_pct": None,
                "volume_ratio_20d": None,
                "volume_ma20": None,
                "history_points": 0,
                "is_stale": True,
                "stale_days": None,
            }

        previous_volume_rows = (
            db.query(ETFFearGreedCloneHistory)
            .filter(
                ETFFearGreedCloneHistory.symbol == symbol,
                ETFFearGreedCloneHistory.date < latest_row.date,
                ETFFearGreedCloneHistory.etf_volume.isnot(None),
            )
            .order_by(ETFFearGreedCloneHistory.date.desc())
            .limit(20)
            .all()
        )
        if proxy_volume_history:
            # 有 proxy_etf 的 A股指数：量比用 ETF 成交量（恐贪日 ÷ 前 20 个 ETF 交易日）
            volume_ratio_20d, volume_ma20 = calculate_volume_ratio(
                proxy_volume_history[0][1],
                [volume for _, volume in proxy_volume_history[1:]],
            )
            if volume_ratio_20d is None:
                # ETF 前值不足 20 个交易日时回退到指数量比
                volume_ratio_20d, volume_ma20 = calculate_volume_ratio(
                    latest_row.etf_volume,
                    [row.etf_volume for row in previous_volume_rows],
                )
        else:
            volume_ratio_20d, volume_ma20 = calculate_volume_ratio(
                latest_row.etf_volume,
                [row.etf_volume for row in previous_volume_rows],
            )
        seven_day_row = cls._db_history_row_on_or_before(db, symbol, latest_row.date - timedelta(days=7))
        one_month_row = cls._db_history_row_on_or_before(db, symbol, latest_row.date - timedelta(days=30))
        history_points = (
            db.query(ETFFearGreedCloneHistory)
            .filter(ETFFearGreedCloneHistory.symbol == symbol)
            .count()
        )
        stale_days = max((datetime.now().date() - latest_row.date).days, 0)

        return {
            "symbol": symbol,
            "latest": cls._db_history_row_payload(latest_row, include_components=False),
            "seven_day_ago": cls._db_history_row_payload(seven_day_row, include_components=False) if seven_day_row else None,
            "one_month_ago": cls._db_history_row_payload(one_month_row, include_components=False) if one_month_row else None,
            "score_change_7d": cls._score_change(latest_row, seven_day_row),
            "score_change_1m": cls._score_change(latest_row, one_month_row),
            "price_change_7d_pct": cls._price_change_pct(latest_row, seven_day_row),
            "price_change_1m_pct": cls._price_change_pct(latest_row, one_month_row),
            "volume_ratio_20d": (
                round(volume_ratio_20d, 6)
                if volume_ratio_20d is not None
                else None
            ),
            "volume_ma20": (
                round(volume_ma20, 4)
                if volume_ma20 is not None
                else None
            ),
            "history_points": history_points,
            "is_stale": stale_days > 5,
            "stale_days": stale_days,
        }

    @staticmethod
    def _db_history_row_on_or_before(db, symbol: str, target_date: date):
        return (
            db.query(ETFFearGreedCloneHistory)
            .filter(
                ETFFearGreedCloneHistory.symbol == symbol,
                ETFFearGreedCloneHistory.date <= target_date,
            )
            .order_by(ETFFearGreedCloneHistory.date.desc())
            .first()
        )

    @staticmethod
    def _score_change(latest_row, previous_row) -> Optional[float]:
        if not latest_row or not previous_row or latest_row.score is None or previous_row.score is None:
            return None
        return round(float(latest_row.score) - float(previous_row.score), 4)

    @staticmethod
    def _price_change_pct(latest_row, previous_row) -> Optional[float]:
        if not latest_row or not previous_row or latest_row.etf_close is None or previous_row.etf_close in (None, 0):
            return None
        return round((float(latest_row.etf_close) / float(previous_row.etf_close) - 1.0) * 100.0, 4)

    @staticmethod
    def _db_holding_payload(row: ETFFearGreedCloneHolding) -> Dict[str, Any]:
        return {
            "symbol": row.holding_symbol,
            "name": row.name,
            "asset_class": row.asset_class,
            "shares": row.shares,
            "market_value": row.market_value,
            "weight": row.weight,
            "price": row.price,
            "holdings_as_of": row.holdings_as_of.isoformat() if row.holdings_as_of else None,
        }

    def _realtime_holdings_payload(
        self,
        holdings: List[ETFHolding],
        quote_map: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        payload = []
        for holding in holdings:
            item = self._holdings_payload([holding])[0]
            quote = quote_map.get(holding.symbol)
            item["realtime_quote"] = self._quote_payload(quote) if quote else None
            payload.append(item)
        return payload

    def _realtime_price_payload(
        self,
        row: pd.Series,
        quote: Optional[Dict[str, Any]],
    ) -> Dict[str, Optional[float]]:
        payload = self._price_payload(
            pd.Series({f"etf_{key}": row.get(key) for key in ("open", "high", "low", "close", "volume", "turnover")})
        )
        payload["quote"] = self._quote_payload(quote) if quote else None
        return payload

    def _quote_payload(self, quote: Dict[str, Any]) -> Dict[str, Any]:
        timestamp = self._quote_timestamp(quote)
        return {
            "symbol": quote.get("symbol"),
            "price": self._quote_float(quote, "price"),
            "open": self._quote_float(quote, "open"),
            "high": self._quote_float(quote, "high"),
            "low": self._quote_float(quote, "low"),
            "prev_close": self._quote_float(quote, "prev_close"),
            "change": self._quote_float(quote, "change"),
            "percent_change": self._quote_float(quote, "percent_change"),
            "volume": self._quote_float(quote, "volume"),
            "turnover": self._quote_float(quote, "turnover"),
            "timestamp": timestamp.isoformat() if timestamp else None,
        }

    @staticmethod
    def _quote_price(quote: Dict[str, Any]) -> Optional[float]:
        value = quote.get("price")
        if value is None:
            return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    @staticmethod
    def _quote_float(
        quote: Dict[str, Any],
        key: str,
        default: Optional[float] = None,
    ) -> Optional[float]:
        value = quote.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _quote_market_date(self, quote: Dict[str, Any]) -> date:
        timestamp = self._quote_timestamp(quote)
        if timestamp:
            return self._latest_trading_day_on_or_before(timestamp.date())

        day = MarketService.get_eastern_now().date()
        return self._latest_trading_day_on_or_before(day)

    @staticmethod
    def _latest_trading_day_on_or_before(day: date) -> date:
        while day.weekday() >= 5 or MarketService.is_us_market_holiday(day):
            day -= timedelta(days=1)
        return day

    @staticmethod
    def _quote_timestamp(quote: Dict[str, Any]) -> Optional[datetime]:
        timestamp = quote.get("timestamp")
        if isinstance(timestamp, pd.Timestamp):
            timestamp = timestamp.to_pydatetime()
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is not None:
                return timestamp.astimezone(ZoneInfo("US/Eastern"))
            return timestamp
        return None

    @staticmethod
    def _normalize_etf_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if "." not in normalized:
            normalized = f"{normalized}.US"
        return normalized

    @staticmethod
    def _nasdaq_symbol(symbol: str) -> str:
        return symbol.strip().upper().replace(".US", "")

    @staticmethod
    def _cache_key(symbol: str) -> str:
        return re.sub(r"[^A-Z0-9._-]+", "_", symbol.upper())

    @staticmethod
    def _number(value: Any, default: float = np.nan) -> float:
        if value is None:
            return default
        text = str(value).strip().replace("$", "").replace(",", "")
        if text in ("", "--", "N/A", "NA"):
            return default
        return float(pd.to_numeric(text, errors="coerce"))

    @staticmethod
    def _ishares_raw_number(value: Any, default: float = np.nan) -> float:
        if isinstance(value, dict):
            return float(value.get("raw", default))
        return ETFFearGreedCloneCalculator._number(value, default=default)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate an ETF-specific Fear & Greed clone."
    )
    parser.add_argument("--symbol", default="SOXX.US", help="ETF symbol, default SOXX.US")
    parser.add_argument("--as-of", default=None, help="End date in YYYY-MM-DD format")
    parser.add_argument("--start-date", default=None, help="Start date for DB backfill in YYYY-MM-DD format")
    parser.add_argument("--history-days", type=int, default=550)
    parser.add_argument("--score-window", type=int, default=252)
    parser.add_argument("--min-periods", type=int, default=120)
    parser.add_argument("--include-history", action="store_true")
    parser.add_argument("--max-holdings", type=int, default=0)
    parser.add_argument("--backfill-db", action="store_true", help="Calculate all valid history and store it in SQLite.")
    parser.add_argument(
        "--use-current-holdings",
        action="store_true",
        help="Use the latest holdings for the full backfill instead of iShares historical holdings.",
    )
    args = parser.parse_args()

    calculator = ETFFearGreedCloneCalculator()
    if args.backfill_db:
        result = calculator.backfill_to_db(
            symbol=args.symbol,
            start_date=datetime.strptime(args.start_date, "%Y-%m-%d").date()
            if args.start_date
            else None,
            end_date=datetime.strptime(args.as_of, "%Y-%m-%d").date()
            if args.as_of
            else None,
            history_days=args.history_days,
            score_window=args.score_window,
            min_periods=args.min_periods,
            max_holdings=args.max_holdings,
            use_historical_holdings=not args.use_current_holdings,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    result = calculator.calculate(
        symbol=args.symbol,
        as_of=args.as_of,
        history_days=args.history_days,
        score_window=args.score_window,
        min_periods=args.min_periods,
        include_history=args.include_history,
        max_holdings=args.max_holdings,
        use_historical_holdings=not args.use_current_holdings,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
