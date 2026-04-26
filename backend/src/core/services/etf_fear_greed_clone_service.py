from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import and_, desc

from .fear_greed_clone_service import (
    CBOE_OPTIONS_DAILY_URL,
    ComponentSpec,
    FearGreedCloneCalculator,
    FearGreedCloneError,
)
from .longport import LongPortService
from .quote import QuoteService
from ..database import ETFFearGreedCloneHistory, ETFFearGreedCloneHolding, Session
from ..models.etf import ETFHolding
from ...robot.etf.ishares import ISharesETFFetcher


NASDAQ_OPTION_CHAIN_URL = "https://api.nasdaq.com/api/quote/{symbol}/option-chain"


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
        source="iShares holdings + LongPort stock history",
        proxy_note=(
            "ETF-specific proxy for new highs/lows. It uses the latest free "
            "iShares holdings, so historical scores include survivorship bias."
        ),
    ),
    "stock_price_breadth": ComponentSpec(
        key="stock_price_breadth",
        name="Holdings Price Breadth",
        raw_label="5-day average weighted advancing dollar-volume ratio",
        source="iShares holdings + LongPort stock history",
        proxy_note=(
            "ETF-specific proxy for breadth. Advancing/declining dollar volume "
            "is computed across current ETF constituents."
        ),
    ),
    "put_call_options": ComponentSpec(
        key="put_call_options",
        name="ETF Put/Call Options",
        raw_label="-5-day average Cboe ETP put/call ratio",
        source="Cboe daily options market statistics",
        proxy_note=(
            "Historical SOXX option volume is not freely available in a stable "
            "endpoint, so the scored history uses Cboe's exchange-traded "
            "products put/call ratio. The response also includes the latest "
            "Nasdaq SOXX option-chain snapshot when available."
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
            "Same risk-on/risk-off idea as CNN, comparing SOXX to long-duration "
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

    def __init__(self, cache_dir: Optional[str] = None, timeout: float = 30.0):
        default_cache_dir = "/var/lib/quant_robot/cache/fear_greed_clone"
        super().__init__(
            cache_dir=cache_dir
            or os.getenv("ETF_FNG_CLONE_CACHE_DIR", default_cache_dir),
            timeout=timeout,
        )
        self.ishares_fetcher = ISharesETFFetcher()
        self.quote_service = QuoteService(LongPortService.get_instance())
        self.price_cache: Dict[Tuple[str, date, date], pd.DataFrame] = {}

    def calculate(
        self,
        symbol: str = "SOXX.US",
        as_of: Optional[str] = None,
        history_days: int = 550,
        score_window: int = 252,
        min_periods: int = 120,
        include_history: bool = False,
        history_points: int = 180,
        max_holdings: int = 40,
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
            (
                "Holdings-based components use iShares historical holdings by "
                "asOfDate; missing holiday/weekend snapshots carry forward the "
                "latest valid holdings."
                if use_historical_holdings
                else (
                    "Holdings-based history uses the latest free iShares holdings, "
                    "so constituent history has survivorship bias."
                )
            ),
            (
                "SOXX-specific option history is not available from a stable free "
                "source; the scored option component uses Cboe ETP put/call data."
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
        history_days: int = 1200,
        score_window: int = 252,
        min_periods: int = 120,
        max_holdings: int = 40,
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

        warnings = self._warnings(use_historical_holdings)
        records: List[Dict[str, Any]] = []
        for timestamp, row in valid.iterrows():
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

    def backfill_to_db(
        self,
        symbol: str = "SOXX.US",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        history_days: int = 1200,
        score_window: int = 252,
        min_periods: int = 120,
        max_holdings: int = 40,
        use_historical_holdings: bool = True,
    ) -> Dict[str, Any]:
        result = self.calculate_history(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
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
                "data": data,
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
        index = pd.bdate_range(start_date, end_date, name="date")
        etf_prices = self._fetch_price_history(
            etf_symbol, start_date, end_date
        ).reindex(index).ffill()
        tlt_prices = self._fetch_price_history(
            "TLT.US", start_date, end_date
        ).reindex(index).ffill()
        fred = self._fetch_fred(["BAMLH0A0HYM2", "BAMLC0A0CM"]).reindex(index).ffill()
        put_call = self._fetch_cboe_ratio(
            start_date,
            end_date,
            ratio_name="EXCHANGE TRADED PRODUCTS PUT/CALL RATIO",
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
        index = pd.bdate_range(start_date, end_date, name="date")
        if not use_historical_holdings:
            holdings, holdings_as_of, _ = self._get_current_holdings(
                etf_symbol, max_holdings=max_holdings
            )
            return (
                {timestamp: holdings for timestamp in index},
                {timestamp: holdings_as_of for timestamp in index},
                holdings_as_of,
                holdings,
            )

        holdings_by_date: Dict[pd.Timestamp, List[ETFHolding]] = {}
        holdings_as_of_by_date: Dict[pd.Timestamp, date] = {}
        latest_holdings: List[ETFHolding] = []
        holdings_as_of: Optional[date] = None
        for timestamp in index:
            day = timestamp.date()
            holdings = self._get_historical_holdings_for_day(
                etf_symbol, day, max_holdings=max_holdings
            )
            if holdings:
                latest_holdings = holdings
                holdings_as_of = day
            if latest_holdings:
                holdings_by_date[timestamp] = latest_holdings
                holdings_as_of_by_date[timestamp] = holdings_as_of

        if not holdings_by_date or not latest_holdings or holdings_as_of is None:
            raise FearGreedCloneError(f"no historical holdings found for {etf_symbol}")

        return holdings_by_date, holdings_as_of_by_date, holdings_as_of, latest_holdings

    def _get_current_holdings(
        self, etf_symbol: str, max_holdings: int
    ) -> Tuple[List[ETFHolding], date, float]:
        if etf_symbol not in self.ishares_fetcher.ETF_CONFIGS:
            raise FearGreedCloneError(f"unsupported ETF for iShares holdings: {etf_symbol}")

        holdings_data = self.ishares_fetcher.get_holdings(etf_symbol)
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

    def _warnings(self, use_historical_holdings: bool) -> List[str]:
        return [
            "This is an independent ETF-specific clone, not CNN's undisclosed calculation.",
            "Scores use rolling z-score/CDF and equal-weighted components.",
            (
                "Holdings-based components use iShares historical holdings by "
                "asOfDate; missing holiday/weekend snapshots carry forward the "
                "latest valid holdings."
                if use_historical_holdings
                else (
                    "Holdings-based history uses the latest free iShares holdings, "
                    "so constituent history has survivorship bias."
                )
            ),
            (
                "SOXX-specific option history is not available from a stable free "
                "source; the scored option component uses Cboe ETP put/call data."
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
    parser.add_argument("--max-holdings", type=int, default=40)
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
