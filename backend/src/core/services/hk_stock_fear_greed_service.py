from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text

from .fear_greed_clone_service import ComponentSpec, FearGreedCloneCalculator, FearGreedCloneError
from ..analytics_database import AnalyticsSession
from ..database import ETFFearGreedCloneHistory, Session
from ...robot.hk_stock_base_data_config import (
    HK_INDEX_FEAR_GREED_TARGET_BY_SYMBOL,
)


HK_MIN_COMPONENT_COUNT = 5
HK_COMPONENTS: Dict[str, ComponentSpec] = {
    "market_momentum": ComponentSpec(
        key="market_momentum",
        name="HK Index Momentum",
        raw_label="index close / 125-day moving average - 1",
        source="hk_index_daily",
        proxy_note="Momentum of the target Hang Seng benchmark.",
    ),
    "stock_price_strength": ComponentSpec(
        key="stock_price_strength",
        name="Constituent Price Strength",
        raw_label="drift-weighted constituent 52-week range position",
        source="official review weights + hk_stock_daily_qfq",
        proxy_note="Official quarterly weights drift with adjusted constituent returns until the next review.",
    ),
    "stock_price_breadth": ComponentSpec(
        key="stock_price_breadth",
        name="Constituent Price Breadth",
        raw_label="5-day drift-weighted advancing turnover ratio",
        source="official review weights + Tushare hk_daily",
        proxy_note="Uses turnover, so no volume adjustment is required for corporate actions.",
    ),
    "put_call_options": ComponentSpec(
        key="put_call_options",
        name="HK Index Put/Call Options",
        raw_label="-5-day index option put/call volume ratio",
        source="HKEX index-option daily data",
        proxy_note="Reserved until the HKEX option history synchronizer has sufficient coverage.",
    ),
    "market_volatility": ComponentSpec(
        key="market_volatility",
        name="HK Index Volatility",
        raw_label="-(20-day realized volatility / 50-day average - 1)",
        source="hk_index_daily",
        proxy_note="Realized-volatility proxy used consistently across HSI and HSTECH.",
    ),
    "safe_haven_demand": ComponentSpec(
        key="safe_haven_demand",
        name="USD Safe Haven Demand",
        raw_label="20-day HK index return - 20-day TLT return",
        source="hk_index_daily + us_stock_daily(TLT.US)",
        proxy_note="Uses US Treasury duration as the linked-HKD safe-haven proxy.",
    ),
    "junk_bond_demand": ComponentSpec(
        key="junk_bond_demand",
        name="USD Credit Spread Demand",
        raw_label="-(US high-yield OAS - investment-grade OAS)",
        source="FRED BAMLH0A0HYM2 and BAMLC0A0CM",
        proxy_note="Broad USD credit-risk proxy; it is not a local Hong Kong corporate spread.",
    ),
}


class HKStockFearGreedCalculator:
    def __init__(self, target_symbol: str):
        symbol = str(target_symbol or "").strip().upper()
        target = HK_INDEX_FEAR_GREED_TARGET_BY_SYMBOL.get(symbol)
        if not target:
            raise FearGreedCloneError(f"unsupported HK fear greed symbol: {symbol}")
        self.target = target
        self.target_symbol = symbol
        self.index_code = str(target["index_code"]).upper()
        self.label = str(target["label"])
        self.shared = FearGreedCloneCalculator()

    def calculate_history(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        output_start_date: Optional[date] = None,
        history_days: int = 900,
        score_window: int = 252,
        min_periods: int = 120,
    ) -> Dict[str, Any]:
        end_value = end_date or date.today()
        calc_start = start_date or end_value - timedelta(days=history_days)
        levels = self._load_levels(calc_start, end_value)
        if levels.empty or len(levels) < min_periods:
            raise FearGreedCloneError(
                f"{self.label} index history is insufficient; run HK base data sync first"
            )
        index = pd.DatetimeIndex(levels.index, name="date")
        holdings, holdings_as_of = self._build_drifted_holdings(index, calc_start, end_value)
        raw = self._build_raw_signals(levels, holdings, calc_start, end_value)
        scores = self._score(raw, score_window, min_periods)
        score_columns = [f"{key}_score" for key in HK_COMPONENTS]
        scores["component_count"] = scores[score_columns].notna().sum(axis=1)
        scores["fear_greed_clone"] = scores[score_columns].mean(axis=1)
        valid = scores[
            scores["fear_greed_clone"].notna()
            & (scores["component_count"] >= HK_MIN_COMPONENT_COUNT)
        ]
        records = []
        for timestamp, row in valid.iterrows():
            day = timestamp.date()
            if output_start_date and day < output_start_date:
                continue
            components = self._component_payload(raw, scores, timestamp)
            active_holdings = holdings.get(timestamp, [])
            score = float(row["fear_greed_clone"])
            records.append(
                {
                    "symbol": self.target_symbol,
                    "date": day.isoformat(),
                    "score": round(score, 4),
                    "rating": FearGreedCloneCalculator.rating(score),
                    "method": "available-component equal-weighted rolling z-score normal CDF",
                    "history_days": history_days,
                    "score_window": score_window,
                    "min_periods": min_periods,
                    "min_component_count": HK_MIN_COMPONENT_COUNT,
                    "component_count": int(row["component_count"]),
                    "components_used": [
                        key for key, value in components.items() if value["used_in_score"]
                    ],
                    "use_historical_holdings": True,
                    "holdings_as_of": (
                        holdings_as_of[timestamp].isoformat()
                        if timestamp in holdings_as_of else None
                    ),
                    "holdings_count": len(active_holdings),
                    "holdings_weight_used": round(
                        sum(float(item["weight"]) for item in active_holdings), 8
                    ),
                    "etf_price": self._price_payload(levels.loc[timestamp]),
                    "components": components,
                    "warnings": self._warnings(),
                }
            )
        return {
            "symbol": self.target_symbol,
            "start_date": records[0]["date"] if records else None,
            "end_date": records[-1]["date"] if records else None,
            "count": len(records),
            "records": records,
            "warnings": self._warnings(),
        }

    def backfill_to_db(self, **kwargs) -> Dict[str, Any]:
        result = self.calculate_history(**kwargs)
        db = Session()
        saved = 0
        try:
            for record in result["records"]:
                components = record["components"]
                values = {
                    "symbol": record["symbol"],
                    "date": date.fromisoformat(record["date"]),
                    "score": record["score"],
                    "rating": record["rating"],
                    "method": record["method"],
                    "history_days": record["history_days"],
                    "score_window": record["score_window"],
                    "min_periods": record["min_periods"],
                    "use_historical_holdings": True,
                    "etf_open": record["etf_price"]["open"],
                    "etf_high": record["etf_price"]["high"],
                    "etf_low": record["etf_price"]["low"],
                    "etf_close": record["etf_price"]["close"],
                    "etf_volume": record["etf_price"]["volume"],
                    "etf_turnover": None,
                    "holdings_as_of": (
                        date.fromisoformat(record["holdings_as_of"])
                        if record["holdings_as_of"] else None
                    ),
                    "holdings_count": record["holdings_count"],
                    "holdings_weight_used": record["holdings_weight_used"],
                    "components": components,
                    "warnings": record["warnings"],
                    "updated_at": datetime.now(),
                }
                for key in HK_COMPONENTS:
                    values[f"{key}_score"] = components[key]["score"]
                    values[f"{key}_raw"] = components[key]["raw_value"]
                row = (
                    db.query(ETFFearGreedCloneHistory)
                    .filter(
                        ETFFearGreedCloneHistory.symbol == record["symbol"],
                        ETFFearGreedCloneHistory.date == values["date"],
                    )
                    .first()
                )
                if row:
                    for key, value in values.items():
                        setattr(row, key, value)
                else:
                    db.add(ETFFearGreedCloneHistory(**values))
                saved += 1
                if saved % 50 == 0:
                    db.commit()
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            Session.remove()
        return {**{key: result[key] for key in ("symbol", "start_date", "end_date", "count")}, "saved": saved}

    def _load_levels(self, start_date: date, end_date: date) -> pd.DataFrame:
        db = AnalyticsSession()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT trade_date, open, high, low, close, pct_chg, vol
                    FROM hk_index_daily
                    WHERE ts_code = :code
                      AND trade_date BETWEEN :start_date AND :end_date
                    ORDER BY trade_date
                    """
                ),
                {"code": self.index_code, "start_date": start_date, "end_date": end_date},
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(
            [tuple(row) for row in rows],
            columns=["date", "open", "high", "low", "close", "pct_chg", "volume"],
        )
        frame["date"] = pd.to_datetime(frame["date"])
        for column in frame.columns[1:]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna(subset=["date", "close"]).set_index("date").sort_index()

    def _build_drifted_holdings(
        self,
        index: pd.DatetimeIndex,
        start_date: date,
        end_date: date,
    ) -> Tuple[Dict[pd.Timestamp, List[Dict[str, Any]]], Dict[pd.Timestamp, date]]:
        db = AnalyticsSession()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT effective_date, con_code, con_name, weight
                    FROM hk_index_weight_snapshot
                    WHERE index_code = :code
                      AND effective_date <= :end_date
                      AND verified > 0
                    ORDER BY effective_date, con_code
                    """
                ),
                {"code": self.index_code, "end_date": end_date},
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            raise FearGreedCloneError(
                f"{self.label} has no verified official weight snapshots"
            )
        snapshots: Dict[pd.Timestamp, List[Dict[str, Any]]] = {}
        for effective_date, code, name, weight in rows:
            snapshots.setdefault(pd.Timestamp(effective_date), []).append(
                {"symbol": code, "name": name, "base_weight": float(weight) / 100.0}
            )
        symbols = sorted({item["symbol"] for items in snapshots.values() for item in items})
        price_start = min(pd.Timestamp(start_date), min(snapshots)).date()
        prices = self._load_constituent_closes(symbols, price_start, end_date)
        dates = sorted(snapshots)
        result: Dict[pd.Timestamp, List[Dict[str, Any]]] = {}
        as_of: Dict[pd.Timestamp, date] = {}
        cursor = -1
        active_date = None
        active = []
        base_prices: Dict[str, float] = {}
        for timestamp in index:
            while cursor + 1 < len(dates) and dates[cursor + 1] <= timestamp:
                cursor += 1
                active_date = dates[cursor]
                active = snapshots[active_date]
                base_prices = {
                    item["symbol"]: self._price_on_or_after(prices.get(item["symbol"]), active_date)
                    for item in active
                }
            if active_date is None:
                continue
            drifted = []
            total = 0.0
            for item in active:
                series = prices.get(item["symbol"])
                base = base_prices.get(item["symbol"])
                current = self._price_on_or_before(series, timestamp)
                if not base or not current:
                    continue
                value = item["base_weight"] * current / base
                if not math.isfinite(value) or value <= 0:
                    continue
                drifted.append({**item, "weight": value})
                total += value
            if total > 0:
                for item in drifted:
                    item["weight"] /= total
                result[timestamp] = drifted
                as_of[timestamp] = active_date.date()
        return result, as_of

    def _load_constituent_closes(
        self, symbols: List[str], start_date, end_date
    ) -> Dict[str, pd.Series]:
        if not symbols:
            return {}
        params = {"start_date": pd.Timestamp(start_date).date(), "end_date": end_date}
        placeholders = []
        for idx, symbol in enumerate(symbols):
            params[f"s{idx}"] = symbol
            placeholders.append(f":s{idx}")
        db = AnalyticsSession()
        try:
            rows = db.execute(
                text(
                    f"""
                    SELECT trade_date, ts_code, close
                    FROM hk_stock_daily_qfq
                    WHERE trade_date BETWEEN :start_date AND :end_date
                      AND ts_code IN ({", ".join(placeholders)})
                    ORDER BY ts_code, trade_date
                    """
                ),
                params,
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return {}
        frame = pd.DataFrame([tuple(row) for row in rows], columns=["date", "symbol", "close"])
        frame["date"] = pd.to_datetime(frame["date"])
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        return {
            str(symbol): group.dropna(subset=["close"]).set_index("date")["close"].sort_index()
            for symbol, group in frame.groupby("symbol")
        }

    def _load_market_frames(
        self, symbols: List[str], start_date: date, end_date: date, index: pd.DatetimeIndex
    ) -> Dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        params = {"start_date": start_date, "end_date": end_date}
        placeholders = []
        for idx, symbol in enumerate(symbols):
            params[f"s{idx}"] = symbol
            placeholders.append(f":s{idx}")
        db = AnalyticsSession()
        try:
            rows = db.execute(
                text(
                    f"""
                    SELECT trade_date, ts_code, high, low, close, pct_chg, amount
                    FROM hk_stock_daily_qfq
                    WHERE trade_date BETWEEN :start_date AND :end_date
                      AND ts_code IN ({", ".join(placeholders)})
                    ORDER BY ts_code, trade_date
                    """
                ),
                params,
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return {}
        frame = pd.DataFrame(
            [tuple(row) for row in rows],
            columns=["date", "symbol", "high", "low", "close", "pct_chg", "amount"],
        )
        frame["date"] = pd.to_datetime(frame["date"])
        result = {}
        for symbol, group in frame.groupby("symbol"):
            item = group.set_index("date").sort_index().reindex(index)
            for column in ("high", "low", "close"):
                item[column] = pd.to_numeric(item[column], errors="coerce").ffill()
            for column in ("pct_chg", "amount"):
                item[column] = pd.to_numeric(item[column], errors="coerce")
            result[str(symbol)] = item
        return result

    def _build_raw_signals(self, levels, holdings, start_date, end_date):
        index = pd.DatetimeIndex(levels.index, name="date")
        close = levels["close"].reindex(index).astype(float)
        returns = close.pct_change()
        realized = returns.rolling(20).std() * np.sqrt(252)
        symbols = sorted({item["symbol"] for values in holdings.values() for item in values})
        frames = self._load_market_frames(symbols, start_date, end_date, index)
        tlt = self._load_us_close("TLT.US", start_date, end_date).reindex(index).ffill()
        if tlt.notna().sum() < 120:
            try:
                tlt = self.shared._fetch_nasdaq_history(
                    "TLT", start_date, end_date
                ).reindex(index).ffill()
            except Exception:
                tlt = pd.Series(np.nan, index=index)
        try:
            fred = self.shared._fetch_fred(["BAMLH0A0HYM2", "BAMLC0A0CM"]).reindex(index).ffill()
            credit = -(fred["BAMLH0A0HYM2"] - fred["BAMLC0A0CM"])
        except Exception:
            credit = pd.Series(np.nan, index=index)
        raw = pd.DataFrame(index=index)
        raw["market_momentum"] = close / close.rolling(125).mean() - 1
        raw["stock_price_strength"] = self._strength(frames, holdings, index)
        raw["stock_price_breadth"] = self._breadth(frames, holdings, index)
        raw["put_call_options"] = np.nan
        raw["market_volatility"] = -(realized / realized.rolling(50).mean() - 1)
        raw["safe_haven_demand"] = close.pct_change(20) - tlt.pct_change(20)
        raw["junk_bond_demand"] = credit
        return raw.replace([np.inf, -np.inf], np.nan)

    def _load_us_close(self, symbol, start_date, end_date):
        db = AnalyticsSession()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT trade_date, close FROM us_stock_daily
                    WHERE symbol = :symbol AND trade_date BETWEEN :start_date AND :end_date
                    ORDER BY trade_date
                    """
                ),
                {"symbol": symbol, "start_date": start_date, "end_date": end_date},
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return pd.Series(dtype=float)
        frame = pd.DataFrame([tuple(row) for row in rows], columns=["date", "close"])
        frame["date"] = pd.to_datetime(frame["date"])
        return pd.to_numeric(frame.set_index("date")["close"], errors="coerce")

    @staticmethod
    def _strength(frames, holdings, index):
        positions = {}
        for symbol, frame in frames.items():
            high = frame["high"].rolling(252, min_periods=120).max()
            low = frame["low"].rolling(252, min_periods=120).min()
            positions[symbol] = ((frame["close"] - low) / (high - low).replace(0, np.nan)).clip(0, 1)
        values = []
        for timestamp in index:
            total = weight = 0.0
            for holding in holdings.get(timestamp, []):
                value = positions.get(holding["symbol"], pd.Series()).get(timestamp, np.nan)
                if pd.isna(value):
                    continue
                total += float(value) * holding["weight"]
                weight += holding["weight"]
            values.append(total / weight if weight else np.nan)
        return pd.Series(values, index=index)

    @staticmethod
    def _breadth(frames, holdings, index):
        values = []
        for timestamp in index:
            up = down = 0.0
            for holding in holdings.get(timestamp, []):
                frame = frames.get(holding["symbol"])
                if frame is None:
                    continue
                pct = frame.at[timestamp, "pct_chg"]
                amount = frame.at[timestamp, "amount"]
                if pd.isna(pct) or pd.isna(amount):
                    continue
                value = float(amount) * holding["weight"]
                if pct > 0:
                    up += value
                elif pct < 0:
                    down += value
            values.append(up / (up + down) if up + down else np.nan)
        return pd.Series(values, index=index).rolling(5, min_periods=3).mean()

    @staticmethod
    def _score(raw, score_window, min_periods):
        result = raw.copy()
        for key in HK_COMPONENTS:
            series = raw[key]
            mean = series.rolling(score_window, min_periods=min_periods).mean()
            std = series.rolling(score_window, min_periods=min_periods).std(ddof=0)
            z = (series - mean) / std.replace(0, np.nan)
            result[f"{key}_score"] = (
                100 * FearGreedCloneCalculator._normal_cdf(z)
            ).clip(0, 100)
        return result

    @staticmethod
    def _price_on_or_before(series: Optional[pd.Series], timestamp: pd.Timestamp):
        if series is None:
            return None
        values = series.loc[series.index <= timestamp]
        return float(values.iloc[-1]) if not values.empty else None

    @staticmethod
    def _price_on_or_after(series: Optional[pd.Series], timestamp: pd.Timestamp):
        if series is None:
            return None
        values = series.loc[series.index >= timestamp]
        return float(values.iloc[0]) if not values.empty else None

    @staticmethod
    def _price_payload(row):
        def value(key):
            item = row.get(key)
            return None if pd.isna(item) else round(float(item), 6)
        return {
            "open": value("open"), "high": value("high"), "low": value("low"),
            "close": value("close"), "volume": value("volume"), "turnover": None,
        }

    @staticmethod
    def _component_payload(raw, scores, timestamp):
        result = {}
        for key, spec in HK_COMPONENTS.items():
            score = scores.at[timestamp, f"{key}_score"]
            raw_value = raw.at[timestamp, key]
            score_value = None if pd.isna(score) else float(score)
            result[key] = {
                "name": spec.name,
                "score": None if score_value is None else round(score_value, 2),
                "rating": None if score_value is None else FearGreedCloneCalculator.rating(score_value),
                "raw_value": None if pd.isna(raw_value) else round(float(raw_value), 6),
                "used_in_score": score_value is not None,
                "raw_label": spec.raw_label,
                "source": spec.source,
                "proxy_note": spec.proxy_note,
            }
        return result

    @staticmethod
    def _warnings():
        return [
            "This is an independent Hong Kong benchmark sentiment index, not an official Hang Seng index.",
            "Constituent weights start from verified official review snapshots and drift with adjusted prices.",
            "Corporate-action adjustment is derived from Tushare pre_close versus prior raw close.",
            "HKEX put/call is omitted until sufficient local option history is available.",
            "USD Treasury and FRED credit spreads are global proxies, not Hong Kong local bond spreads.",
        ]
