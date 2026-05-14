from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text

from .fear_greed_clone_service import ComponentSpec, FearGreedCloneCalculator, FearGreedCloneError
from ..analytics_database import AnalyticsSession
from ..database import (
    AStockInnovation100Constituent,
    AStockInnovation100Level,
    AStockInnovation100Rebalance,
    ETFFearGreedCloneHistory,
    Session,
)
from ...robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS


A_STOCK_INNO100_FEAR_SYMBOL = "INNO100.CN"
A_STOCK_INNO100_INDEX_CODE = A_STOCK_INNO100_FEAR_SYMBOL
A_STOCK_INNO100_SAFE_HAVEN_INDEX = "H11006.CSI"
A_STOCK_INNO100_OPTION_UNDERLYINGS = (
    "OP588000.SH",
    "OP588080.SH",
    "OP159915.SZ",
    "OP510500.SH",
    "OP159922.SZ",
)
A_STOCK_INNO100_TARGET = {
    "symbol": A_STOCK_INNO100_FEAR_SYMBOL,
    "ticker": "A创100",
    "label": "A创100",
    "index_name": "A股创新100",
    "option_underlyings": list(A_STOCK_INNO100_OPTION_UNDERLYINGS),
    "custom_inno100": True,
}
A_STOCK_FEAR_GREED_TARGETS = [A_STOCK_INNO100_TARGET, *A_STOCK_INDEX_FEAR_GREED_TARGETS]
A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL = {
    str(item["symbol"]).upper(): item
    for item in A_STOCK_FEAR_GREED_TARGETS
}
A_STOCK_MIN_COMPONENT_COUNT = 6


A_STOCK_COMPONENTS: Dict[str, ComponentSpec] = {
    "market_momentum": ComponentSpec(
        key="market_momentum",
        name="A-share Index Momentum",
        raw_label="tracked index level / 125-day moving average - 1",
        source="A-share index level daily data",
        proxy_note="Same momentum idea as CNN, applied to the target A-share index.",
    ),
    "stock_price_strength": ComponentSpec(
        key="stock_price_strength",
        name="Constituent Price Strength",
        raw_label="Constituent-weighted 52-week range position",
        source="index constituents/weights + a_stock_market_daily_qfq",
        proxy_note="A-share constituent proxy for new-high/new-low strength.",
    ),
    "stock_price_breadth": ComponentSpec(
        key="stock_price_breadth",
        name="Constituent Price Breadth",
        raw_label="5-day average constituent-weighted advancing amount ratio",
        source="index constituents/weights + a_stock_market_daily_qfq",
        proxy_note="Advancing/declining turnover breadth across target index constituents.",
    ),
    "put_call_options": ComponentSpec(
        key="put_call_options",
        name="A-share ETF Put/Call Options",
        raw_label="-5-day average related ETF option put/call volume ratio",
        source="Tushare opt_basic + opt_daily in DuckDB",
        proxy_note="Uses related A-share ETF option volume PCR as target-index option-sentiment proxy.",
    ),
    "market_volatility": ComponentSpec(
        key="market_volatility",
        name="A-share Index Volatility",
        raw_label="-(20-day realized volatility / 50-day average - 1)",
        source="A-share index level daily data",
        proxy_note="Realized-volatility replacement for CNN's VIX component.",
    ),
    "safe_haven_demand": ComponentSpec(
        key="safe_haven_demand",
        name="Safe Haven Demand",
        raw_label="20-day target index return - 20-day 中证国债 return",
        source="A-share index level daily data + a_stock_index_daily",
        proxy_note="Compares target index with a China bond index as the risk-on/risk-off proxy.",
    ),
    "junk_bond_demand": ComponentSpec(
        key="junk_bond_demand",
        name="Credit Spread Demand",
        raw_label="-mean 3Y AA-AAA credit spread across medium-note, enterprise-bond and urban-investment curves",
        source="ChinaBond yield curves in DuckDB",
        proxy_note="ChinaBond AA/AAA credit spread proxy for risk appetite; narrower spread is greed.",
    ),
}


class AStockInnovation100FearGreedCloneCalculator:
    """A-share index Fear & Greed clone using local A-share DuckDB data."""

    def __init__(self, target_symbol: str = A_STOCK_INNO100_FEAR_SYMBOL):
        symbol = str(target_symbol or A_STOCK_INNO100_FEAR_SYMBOL).strip().upper()
        self.target = A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL.get(symbol)
        if not self.target:
            supported = ", ".join(sorted(A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL))
            raise FearGreedCloneError(f"unsupported A-share index fear greed symbol {symbol}; supported: {supported}")
        self.target_symbol = str(self.target["symbol"]).upper()
        self.ticker = str(self.target.get("ticker") or self.target_symbol)
        self.label = str(self.target.get("label") or self.ticker)
        self.index_code = str(self.target.get("index_code") or self.target_symbol).upper()
        self.index_name = str(self.target.get("index_name") or self.index_code)
        self.option_underlyings = tuple(self.target.get("option_underlyings") or A_STOCK_INNO100_OPTION_UNDERLYINGS)
        self.custom_inno100 = bool(self.target.get("custom_inno100"))

    def calculate_history(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        output_start_date: Optional[date] = None,
        history_days: int = 550,
        score_window: int = 252,
        min_periods: int = 120,
    ) -> Dict[str, Any]:
        end_value = end_date or datetime.now().date()
        calc_start = start_date or (end_value - timedelta(days=history_days))
        if min_periods > score_window:
            raise FearGreedCloneError("min_periods cannot exceed score_window")
        if calc_start > end_value:
            raise FearGreedCloneError("start_date cannot be later than end_date")

        levels = self._load_levels(calc_start, end_value)
        if levels.empty:
            raise FearGreedCloneError(f"{self.label} index levels are empty; run A股基础数据同步 first")
        if len(levels) < min_periods:
            raise FearGreedCloneError(
                f"{self.label} 指数行情可用交易日不足: {len(levels)}/{min_periods}，请先执行 A股基础数据同步补齐指数日行情"
            )

        index = pd.DatetimeIndex(levels.index, name="date")
        holdings_by_date, holdings_as_of_by_date = self._build_holdings_by_date(index)
        raw = self._build_raw_signals(levels, holdings_by_date, calc_start, end_value)
        score_df = self._score_raw_signals(raw, score_window, min_periods)

        score_columns = [f"{key}_score" for key in A_STOCK_COMPONENTS]
        score_values = score_df[score_columns]
        score_df["component_count"] = score_values.notna().sum(axis=1)
        score_df["fear_greed_clone"] = score_values.mean(axis=1)
        valid = score_df[
            score_df["fear_greed_clone"].notna()
            & (score_df["component_count"] >= A_STOCK_MIN_COMPONENT_COUNT)
        ]
        if valid.empty:
            raise FearGreedCloneError(f"not enough data to calculate {self.label} Fear & Greed")

        warnings = self._warnings()
        records: List[Dict[str, Any]] = []
        for timestamp, row in valid.iterrows():
            day = timestamp.date()
            if output_start_date and day < output_start_date:
                continue
            score = float(row["fear_greed_clone"])
            holdings = holdings_by_date.get(timestamp, [])
            holdings_as_of = holdings_as_of_by_date.get(timestamp)
            components = self._component_payload(raw, score_df, timestamp)
            components_used = [
                key
                for key, component in components.items()
                if component.get("used_in_score")
            ]
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
                    "min_component_count": A_STOCK_MIN_COMPONENT_COUNT,
                    "component_count": int(row["component_count"]),
                    "components_used": components_used,
                    "use_historical_holdings": True,
                    "etf_price": self._price_payload(raw.loc[timestamp]),
                    "holdings_as_of": holdings_as_of.isoformat() if holdings_as_of else None,
                    "holdings_count": len(holdings),
                    "holdings_weight_used": round(sum(item["weight"] for item in holdings), 6),
                    "components": components,
                    "warnings": warnings,
                }
            )

        return {
            "symbol": self.target_symbol,
            "start_date": records[0]["date"] if records else None,
            "end_date": records[-1]["date"] if records else None,
            "count": len(records),
            "records": records,
            "warnings": warnings,
        }

    def backfill_to_db(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        output_start_date: Optional[date] = None,
        history_days: int = 550,
        score_window: int = 252,
        min_periods: int = 120,
    ) -> Dict[str, Any]:
        result = self.calculate_history(
            start_date=start_date,
            end_date=end_date,
            output_start_date=output_start_date,
            history_days=history_days,
            score_window=score_window,
            min_periods=min_periods,
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
                        use_historical_holdings=True,
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
        }

    def _load_levels(self, start_date: date, end_date: date) -> pd.DataFrame:
        if not self.custom_inno100:
            return self._load_duckdb_index_levels(start_date, end_date)

        db = Session()
        try:
            rows = (
                db.query(AStockInnovation100Level)
                .filter(
                    AStockInnovation100Level.index_code == A_STOCK_INNO100_INDEX_CODE,
                    AStockInnovation100Level.date >= start_date,
                    AStockInnovation100Level.date <= end_date,
                )
                .order_by(AStockInnovation100Level.date.asc())
                .all()
            )
        finally:
            Session.remove()

        records = [
            {
                "date": row.date,
                "level": float(row.level),
                "daily_return_pct": row.daily_return_pct,
            }
            for row in rows
            if row.date and row.level is not None and math.isfinite(float(row.level))
        ]
        if not records:
            return pd.DataFrame()
        frame = pd.DataFrame(records)
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.set_index("date").sort_index()

    def _load_duckdb_index_levels(self, start_date: date, end_date: date) -> pd.DataFrame:
        analytics_db = AnalyticsSession()
        try:
            rows = analytics_db.execute(
                text(
                    """
                    SELECT trade_date, open, high, low, close, pct_chg, vol, amount
                    FROM a_stock_index_daily
                    WHERE ts_code = :index_code
                      AND trade_date >= :start_date
                      AND trade_date <= :end_date
                    ORDER BY trade_date
                    """
                ),
                {
                    "index_code": self.index_code,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(
            [tuple(row) for row in rows],
            columns=["date", "open", "high", "low", "level", "daily_return_pct", "volume", "turnover"],
        )
        frame["date"] = pd.to_datetime(frame["date"])
        for column in ["open", "high", "low", "level", "daily_return_pct", "volume", "turnover"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["date", "level"])
        return frame.set_index("date").sort_index()

    def _build_holdings_by_date(
        self,
        index: pd.DatetimeIndex,
    ) -> Tuple[Dict[pd.Timestamp, List[Dict[str, Any]]], Dict[pd.Timestamp, date]]:
        if index.empty:
            return {}, {}

        if not self.custom_inno100:
            return self._build_duckdb_index_weight_holdings_by_date(index)

        db = Session()
        try:
            rebalances = (
                db.query(AStockInnovation100Rebalance)
                .filter(
                    AStockInnovation100Rebalance.index_code == A_STOCK_INNO100_INDEX_CODE,
                    AStockInnovation100Rebalance.rebalance_date <= index.max().date(),
                )
                .order_by(
                    AStockInnovation100Rebalance.effective_date.asc(),
                    AStockInnovation100Rebalance.rebalance_date.asc(),
                    AStockInnovation100Rebalance.id.asc(),
                )
                .all()
            )
            rebalance_ids = [row.id for row in rebalances]
            constituent_rows = (
                db.query(AStockInnovation100Constituent)
                .filter(
                    AStockInnovation100Constituent.index_code == A_STOCK_INNO100_INDEX_CODE,
                    AStockInnovation100Constituent.rebalance_id.in_(rebalance_ids),
                )
                .all()
                if rebalance_ids
                else []
            )
        finally:
            Session.remove()

        by_rebalance: Dict[int, List[Dict[str, Any]]] = {}
        for row in constituent_rows:
            weight = float(row.weight_pct or 0.0) / 100.0
            if not row.ts_code or weight <= 0:
                continue
            by_rebalance.setdefault(row.rebalance_id, []).append(
                {
                    "symbol": row.ts_code,
                    "name": row.name,
                    "weight": weight,
                }
            )

        effective_rows = []
        for row in rebalances:
            effective_date = row.effective_date or row.rebalance_date
            if not effective_date or row.id not in by_rebalance:
                continue
            effective_rows.append((effective_date, row.id, by_rebalance[row.id]))
        effective_rows.sort(key=lambda item: (item[0], item[1]))

        holdings_by_date: Dict[pd.Timestamp, List[Dict[str, Any]]] = {}
        holdings_as_of_by_date: Dict[pd.Timestamp, date] = {}
        cursor = -1
        current_holdings: List[Dict[str, Any]] = []
        current_as_of: Optional[date] = None
        for timestamp in index:
            day = timestamp.date()
            while cursor + 1 < len(effective_rows) and effective_rows[cursor + 1][0] <= day:
                cursor += 1
                current_as_of = effective_rows[cursor][0]
                current_holdings = effective_rows[cursor][2]
            if current_holdings and current_as_of:
                holdings_by_date[timestamp] = current_holdings
                holdings_as_of_by_date[timestamp] = current_as_of

        if not holdings_by_date:
            raise FearGreedCloneError(f"{self.label} constituent snapshots are empty; run A股创新100指数刷新 first")
        return holdings_by_date, holdings_as_of_by_date

    def _build_duckdb_index_weight_holdings_by_date(
        self,
        index: pd.DatetimeIndex,
    ) -> Tuple[Dict[pd.Timestamp, List[Dict[str, Any]]], Dict[pd.Timestamp, date]]:
        analytics_db = AnalyticsSession()
        try:
            rows = analytics_db.execute(
                text(
                    """
                    SELECT
                        w.trade_date,
                        w.con_code,
                        b.name,
                        w.weight
                    FROM a_stock_index_weight w
                    LEFT JOIN a_stock_basic b ON b.ts_code = w.con_code
                    WHERE w.index_code = :index_code
                      AND w.trade_date <= :end_date
                    ORDER BY w.trade_date ASC, w.weight DESC
                    """
                ),
                {
                    "index_code": self.index_code,
                    "end_date": index.max().date().isoformat(),
                },
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            raise FearGreedCloneError(f"{self.index_code} 指数成分权重缺失，请先执行 A股基础数据同步")

        by_date: Dict[pd.Timestamp, List[Dict[str, Any]]] = {}
        for row in rows:
            snapshot_date = pd.Timestamp(row[0])
            symbol = str(row[1] or "").strip().upper()
            weight = float(row[3] or 0.0) / 100.0
            if not symbol or weight <= 0:
                continue
            by_date.setdefault(snapshot_date, []).append(
                {
                    "symbol": symbol,
                    "name": row[2],
                    "weight": weight,
                }
            )

        effective_dates = sorted(by_date)
        holdings_by_date: Dict[pd.Timestamp, List[Dict[str, Any]]] = {}
        holdings_as_of_by_date: Dict[pd.Timestamp, date] = {}
        cursor = -1
        current_date: Optional[pd.Timestamp] = None
        current_holdings: List[Dict[str, Any]] = []
        for timestamp in index:
            while cursor + 1 < len(effective_dates) and effective_dates[cursor + 1] <= timestamp:
                cursor += 1
                current_date = effective_dates[cursor]
                current_holdings = by_date[current_date]
            if current_date is not None and current_holdings:
                holdings_by_date[timestamp] = current_holdings
                holdings_as_of_by_date[timestamp] = current_date.date()

        if not holdings_by_date:
            raise FearGreedCloneError(f"{self.index_code} 指数成分权重没有覆盖 {self.label} 的行情日期")
        return holdings_by_date, holdings_as_of_by_date

    def _build_raw_signals(
        self,
        levels: pd.DataFrame,
        holdings_by_date: Dict[pd.Timestamp, List[Dict[str, Any]]],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        index = pd.DatetimeIndex(levels.index, name="date")
        level = levels["level"].astype(float).reindex(index).ffill()
        level_returns = level.pct_change()
        realized_vol = level_returns.rolling(20).std() * np.sqrt(252)

        market_frames = self._load_constituent_market_frames(holdings_by_date, index, start_date, end_date)
        bond_close = self._load_index_close(A_STOCK_INNO100_SAFE_HAVEN_INDEX, start_date, end_date).reindex(index).ffill()
        option_pcr = self._load_option_volume_pcr(start_date, end_date).reindex(index).ffill(limit=3)
        credit_spread = self._load_credit_spread(start_date, end_date).reindex(index).ffill(limit=3)

        df = pd.DataFrame(index=index)
        df["market_momentum"] = level / level.rolling(125).mean() - 1.0
        df["stock_price_strength"] = self._weighted_range_position(market_frames, holdings_by_date, index)
        df["stock_price_breadth"] = self._weighted_advancing_amount_ratio(market_frames, holdings_by_date, index)
        df["put_call_options"] = -option_pcr.rolling(5, min_periods=3).mean()
        df["market_volatility"] = -(realized_vol / realized_vol.rolling(50).mean() - 1.0)
        df["safe_haven_demand"] = level.pct_change(20) - bond_close.pct_change(20)
        df["junk_bond_demand"] = -credit_spread
        df["etf_open"] = pd.to_numeric(levels["open"], errors="coerce").reindex(index).ffill() if "open" in levels.columns else level
        df["etf_high"] = pd.to_numeric(levels["high"], errors="coerce").reindex(index).ffill() if "high" in levels.columns else level
        df["etf_low"] = pd.to_numeric(levels["low"], errors="coerce").reindex(index).ffill() if "low" in levels.columns else level
        df["etf_close"] = level
        df["etf_volume"] = pd.to_numeric(levels["volume"], errors="coerce").reindex(index).ffill() if "volume" in levels.columns else np.nan
        df["etf_turnover"] = pd.to_numeric(levels["turnover"], errors="coerce").reindex(index).ffill() if "turnover" in levels.columns else np.nan
        return df.replace([np.inf, -np.inf], np.nan)

    def _load_constituent_market_frames(
        self,
        holdings_by_date: Dict[pd.Timestamp, List[Dict[str, Any]]],
        index: pd.DatetimeIndex,
        start_date: date,
        end_date: date,
    ) -> Dict[str, pd.DataFrame]:
        symbols = sorted({item["symbol"] for holdings in holdings_by_date.values() for item in holdings})
        if not symbols:
            return {}
        params: Dict[str, Any] = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        placeholders = []
        for idx, symbol in enumerate(symbols):
            key = f"symbol_{idx}"
            placeholders.append(f":{key}")
            params[key] = symbol
        sql = text(
            f"""
            SELECT trade_date, ts_code, high, low, close, pct_chg, amount
            FROM a_stock_market_daily_qfq
            WHERE trade_date >= :start_date
              AND trade_date <= :end_date
              AND ts_code IN ({", ".join(placeholders)})
            ORDER BY ts_code, trade_date
            """
        )
        analytics_db = AnalyticsSession()
        try:
            rows = analytics_db.execute(sql, params).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return {}

        frame = pd.DataFrame(
            [tuple(row) for row in rows],
            columns=["trade_date", "symbol", "high", "low", "close", "pct_chg", "amount"],
        )
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frames: Dict[str, pd.DataFrame] = {}
        for symbol, group in frame.groupby("symbol"):
            symbol_frame = group.sort_values("trade_date").drop_duplicates("trade_date", keep="last").set_index("trade_date")
            symbol_frame = symbol_frame.reindex(index)
            for column in ("high", "low", "close"):
                symbol_frame[column] = pd.to_numeric(symbol_frame[column], errors="coerce").ffill()
            symbol_frame["pct_chg"] = pd.to_numeric(symbol_frame["pct_chg"], errors="coerce")
            symbol_frame["amount"] = pd.to_numeric(symbol_frame["amount"], errors="coerce").fillna(0.0)
            if symbol_frame["close"].notna().sum() >= 120:
                frames[str(symbol)] = symbol_frame
        if not frames:
            return {}
        return frames

    def _load_index_close(self, ts_code: str, start_date: date, end_date: date) -> pd.Series:
        analytics_db = AnalyticsSession()
        try:
            rows = analytics_db.execute(
                text(
                    """
                    SELECT trade_date, close
                    FROM a_stock_index_daily
                    WHERE ts_code = :ts_code
                      AND trade_date >= :start_date
                      AND trade_date <= :end_date
                    ORDER BY trade_date
                    """
                ),
                {
                    "ts_code": ts_code,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return pd.Series(dtype=float)
        frame = pd.DataFrame([tuple(row) for row in rows], columns=["date", "close"])
        frame["date"] = pd.to_datetime(frame["date"])
        return pd.to_numeric(frame.set_index("date")["close"], errors="coerce").sort_index()

    def _load_option_volume_pcr(self, start_date: date, end_date: date) -> pd.Series:
        params: Dict[str, Any] = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        placeholders = []
        for idx, opt_code in enumerate(self.option_underlyings):
            key = f"opt_code_{idx}"
            placeholders.append(f":{key}")
            params[key] = opt_code
        analytics_db = AnalyticsSession()
        try:
            rows = analytics_db.execute(
                text(
                    f"""
                    SELECT
                        d.trade_date,
                        b.call_put,
                        SUM(COALESCE(d.vol, 0)) AS volume
                    FROM a_stock_option_daily d
                    JOIN a_stock_option_basic b ON b.ts_code = d.ts_code
                    WHERE d.trade_date >= :start_date
                      AND d.trade_date <= :end_date
                      AND b.opt_code IN ({", ".join(placeholders)})
                      AND b.call_put IN ('C', 'P')
                    GROUP BY d.trade_date, b.call_put
                    ORDER BY d.trade_date
                    """
                ),
                params,
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return pd.Series(dtype=float)

        frame = pd.DataFrame([tuple(row) for row in rows], columns=["date", "call_put", "volume"])
        frame["date"] = pd.to_datetime(frame["date"])
        pivot = frame.pivot_table(index="date", columns="call_put", values="volume", aggfunc="sum")
        call_volume = pd.to_numeric(
            pivot["C"] if "C" in pivot.columns else pd.Series(index=pivot.index, dtype=float),
            errors="coerce",
        )
        put_volume = pd.to_numeric(
            pivot["P"] if "P" in pivot.columns else pd.Series(index=pivot.index, dtype=float),
            errors="coerce",
        )
        ratio = put_volume / call_volume.replace(0, np.nan)
        return ratio.sort_index()

    def _load_credit_spread(self, start_date: date, end_date: date, term: float = 3.0) -> pd.Series:
        analytics_db = AnalyticsSession()
        try:
            rows = analytics_db.execute(
                text(
                    """
                    SELECT
                        d.trade_date,
                        defs.pair_key,
                        defs.rating,
                        d.yield_rate
                    FROM a_stock_chinabond_yield_curve_daily d
                    JOIN a_stock_chinabond_yield_curve_defs defs
                      ON defs.curve_id = d.curve_id
                    WHERE d.trade_date >= :start_date
                      AND d.trade_date <= :end_date
                      AND ABS(d.term - :term) < 0.000001
                      AND defs.pair_key IN ('medium_note', 'enterprise_bond', 'urban_investment_bond')
                      AND defs.rating IN ('AAA', 'AA')
                    ORDER BY d.trade_date
                    """
                ),
                {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "term": term,
                },
            ).fetchall()
        finally:
            AnalyticsSession.remove()
        if not rows:
            return pd.Series(dtype=float)
        frame = pd.DataFrame([tuple(row) for row in rows], columns=["date", "pair_key", "rating", "yield_rate"])
        frame["date"] = pd.to_datetime(frame["date"])
        frame["yield_rate"] = pd.to_numeric(frame["yield_rate"], errors="coerce")
        pivot = frame.pivot_table(
            index=["date", "pair_key"],
            columns="rating",
            values="yield_rate",
            aggfunc="last",
        )
        if "AA" not in pivot.columns or "AAA" not in pivot.columns:
            return pd.Series(dtype=float)
        spread_by_pair = (pivot["AA"] - pivot["AAA"]).dropna()
        if spread_by_pair.empty:
            return pd.Series(dtype=float)
        return spread_by_pair.groupby(level="date").mean().sort_index()

    def _weighted_range_position(
        self,
        market_frames: Dict[str, pd.DataFrame],
        holdings_by_date: Dict[pd.Timestamp, List[Dict[str, Any]]],
        index: pd.DatetimeIndex,
    ) -> pd.Series:
        position_by_symbol: Dict[str, pd.Series] = {}
        for symbol, frame in market_frames.items():
            high_52w = frame["high"].rolling(252, min_periods=120).max()
            low_52w = frame["low"].rolling(252, min_periods=120).min()
            position_by_symbol[symbol] = ((frame["close"] - low_52w) / (high_52w - low_52w)).clip(0, 1)

        values = []
        for timestamp in index:
            weighted_sum = 0.0
            valid_weight = 0.0
            for holding in holdings_by_date.get(timestamp, []):
                series = position_by_symbol.get(holding["symbol"])
                if series is None or timestamp not in series.index:
                    continue
                value = series.loc[timestamp]
                if pd.isna(value):
                    continue
                weight = float(holding["weight"])
                weighted_sum += float(value) * weight
                valid_weight += weight
            values.append(weighted_sum / valid_weight if valid_weight else np.nan)
        return pd.Series(values, index=index)

    def _weighted_advancing_amount_ratio(
        self,
        market_frames: Dict[str, pd.DataFrame],
        holdings_by_date: Dict[pd.Timestamp, List[Dict[str, Any]]],
        index: pd.DatetimeIndex,
    ) -> pd.Series:
        values = []
        for timestamp in index:
            up_amount = 0.0
            down_amount = 0.0
            for holding in holdings_by_date.get(timestamp, []):
                frame = market_frames.get(holding["symbol"])
                if frame is None or timestamp not in frame.index:
                    continue
                pct_chg = frame.loc[timestamp, "pct_chg"]
                amount = frame.loc[timestamp, "amount"]
                if pd.isna(pct_chg) or pd.isna(amount):
                    continue
                weighted_amount = float(amount) * float(holding["weight"])
                if pct_chg > 0:
                    up_amount += weighted_amount
                elif pct_chg < 0:
                    down_amount += weighted_amount
            total = up_amount + down_amount
            values.append(up_amount / total if total else np.nan)
        return pd.Series(values, index=index).rolling(5, min_periods=3).mean()

    def _score_raw_signals(
        self,
        raw: pd.DataFrame,
        score_window: int,
        min_periods: int,
    ) -> pd.DataFrame:
        result = raw.copy()
        for key in A_STOCK_COMPONENTS:
            series = raw[key]
            mean = series.rolling(score_window, min_periods=min_periods).mean()
            std = series.rolling(score_window, min_periods=min_periods).std(ddof=0)
            z_score = (series - mean) / std.replace(0, np.nan)
            result[f"{key}_score"] = (100.0 * FearGreedCloneCalculator._normal_cdf(z_score)).clip(0, 100)
        return result

    def _component_payload(
        self,
        raw: pd.DataFrame,
        scores: pd.DataFrame,
        latest_date: pd.Timestamp,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for key, spec in A_STOCK_COMPONENTS.items():
            component_score = self._finite_float_or_none(scores.loc[latest_date, f"{key}_score"])
            raw_value = self._finite_float_or_none(raw.loc[latest_date, key])
            used_in_score = component_score is not None
            payload[key] = {
                "name": spec.name,
                "score": round(component_score, 2) if component_score is not None else None,
                "rating": FearGreedCloneCalculator.rating(component_score) if component_score is not None else None,
                "raw_value": round(raw_value, 6) if raw_value is not None else None,
                "used_in_score": used_in_score,
                "raw_label": spec.raw_label,
                "source": spec.source,
                "proxy_note": spec.proxy_note,
            }
        return payload

    @staticmethod
    def _finite_float_or_none(value) -> Optional[float]:
        try:
            if pd.isna(value):
                return None
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _price_payload(row: pd.Series) -> Dict[str, Optional[float]]:
        payload: Dict[str, Optional[float]] = {}
        for source_key, payload_key in (
            ("etf_open", "open"),
            ("etf_high", "high"),
            ("etf_low", "low"),
            ("etf_close", "close"),
            ("etf_volume", "volume"),
            ("etf_turnover", "turnover"),
        ):
            value = row.get(source_key)
            payload[payload_key] = None if pd.isna(value) else round(float(value), 6)
        return payload

    def _warnings(self) -> List[str]:
        return [
            f"This is an independent {self.label}-specific clone, not CNN's undisclosed calculation.",
            f"Scores use rolling z-score/CDF and equal-weighted available components; at least {A_STOCK_MIN_COMPONENT_COUNT} components are required.",
            f"The option component uses related A-share ETF option volume put/call ratios from Tushare: {', '.join(self.option_underlyings)}.",
            "The credit-risk component uses ChinaBond 3Y AA-AAA spreads across medium-note, enterprise-bond and urban-investment curves.",
            "Safe-haven demand uses H11006.CSI 中证国债 as the bond proxy.",
        ]
