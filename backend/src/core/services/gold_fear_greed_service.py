import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ..database import ETFFearGreedCloneHistory, ETFPutCallRatio, GoldFearGreedInput, Session
from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb


GOLD_COMPONENTS = {
    "gold_price_momentum": ("黄金价格趋势", "GLD收盘价相对125日均线"),
    "gold_options": ("黄金期权情绪", "GLD五日平均Put/Call成交量比（反向）"),
    "cot_positioning": ("COMEX投机持仓", "Managed Money净多头占未平仓量"),
    "gold_etf_demand": ("黄金ETF持仓需求", "实物黄金ETF持仓吨数/份额四个披露期变化"),
    "real_yield_dollar": ("实际利率与美元", "10年实际利率与广义美元20日变化（反向）"),
}


class GoldFearGreedCalculator:
    """GLD专用五因子贪恐，所有低频数据只从其实际可用日起向后填充。"""

    def calculate_history(
        self,
        start_date: date,
        end_date: date,
        output_start_date: Optional[date] = None,
        score_window: int = 252,
        min_periods: int = 120,
    ) -> Dict[str, Any]:
        prices = self._prices(start_date, end_date)
        if prices.empty:
            raise RuntimeError("GLD.US 没有日线数据，请先运行美股基础数据同步")
        index = prices.index
        option_history = self._options(start_date, end_date)
        options = (
            option_history.reindex(option_history.index.union(index))
            .sort_index().ffill(limit=3).reindex(index)
        )
        input_history = self._inputs(start_date, end_date)
        inputs = (
            input_history.reindex(input_history.index.union(index))
            .sort_index().ffill().reindex(index)
        )

        raw = pd.DataFrame(index=index)
        raw["gold_price_momentum"] = prices["close"] / prices["close"].rolling(125).mean() - 1.0
        raw["gold_options"] = -options.rolling(5, min_periods=3).mean()
        raw["cot_positioning"] = (
            inputs["cot_managed_money_long"] - inputs["cot_managed_money_short"]
        ) / inputs["cot_open_interest"].replace(0, np.nan)
        holdings = input_history["gold_etf_holdings_tonnes"].copy()
        shares = input_history["gold_etf_shares"]
        if shares.notna().any():
            holdings = holdings.fillna(shares)
        observed_holdings = holdings.dropna()
        demand = observed_holdings.pct_change(4, fill_method=None).replace([np.inf, -np.inf], np.nan)
        raw["gold_etf_demand"] = (
            demand.reindex(demand.index.union(index)).sort_index().ffill().reindex(index)
        )
        raw["real_yield_dollar"] = -inputs["real_yield_10y"].diff(20) - 10.0 * inputs["broad_dollar_index"].pct_change(20, fill_method=None)
        raw = raw.replace([np.inf, -np.inf], np.nan)

        scored = pd.DataFrame(index=index)
        for key in GOLD_COMPONENTS:
            series = raw[key]
            mean = series.rolling(score_window, min_periods=min_periods).mean()
            std = series.rolling(score_window, min_periods=min_periods).std(ddof=0).replace(0, np.nan)
            z = (series - mean) / std
            scored[key] = pd.Series(
                [50.0 * (1.0 + math.erf(value / math.sqrt(2.0))) if pd.notna(value) else np.nan for value in z],
                index=index,
            ).clip(0, 100)
        scored["score"] = scored[list(GOLD_COMPONENTS)].mean(axis=1, skipna=False)
        valid = scored.dropna(subset=["score"])
        if output_start_date:
            valid = valid[valid.index.date >= output_start_date]
        records = []
        for timestamp, score_row in valid.iterrows():
            components = {}
            for key, (name, label) in GOLD_COMPONENTS.items():
                components[key] = {
                    "name": name,
                    "score": round(float(score_row[key]), 2),
                    "rating": self.rating(float(score_row[key])),
                    "raw_value": round(float(raw.loc[timestamp, key]), 8),
                    "raw_label": label,
                }
            price = prices.loc[timestamp]
            records.append({
                "symbol": "GLD.US",
                "date": timestamp.date().isoformat(),
                "score": round(float(score_row["score"]), 2),
                "rating": self.rating(float(score_row["score"])),
                "method": "gold five-factor equal-weight rolling z-score CDF",
                "components": components,
                "etf_price": {key: self._finite(price.get(key)) for key in ("open", "high", "low", "close", "volume", "turnover")},
            })
        return {"symbol": "GLD.US", "component_count": 5, "records": records}

    def backfill_to_db(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        output_start_date: Optional[date] = None,
        history_days: int = 1200,
        score_window: int = 252,
        min_periods: int = 120,
        **_kwargs,
    ) -> Dict[str, Any]:
        end_value = end_date or date.today()
        start_value = start_date or (end_value - timedelta(days=history_days))
        result = self.calculate_history(start_value, end_value, output_start_date, score_window, min_periods)
        db = Session()
        saved = 0
        try:
            for item in result["records"]:
                day = date.fromisoformat(item["date"])
                components = item["components"]
                price = item["etf_price"]
                db.merge(ETFFearGreedCloneHistory(
                    symbol="GLD.US", date=day, score=item["score"], rating=item["rating"],
                    method=item["method"], history_days=history_days, score_window=score_window,
                    min_periods=min_periods, use_historical_holdings=False,
                    etf_open=price["open"], etf_high=price["high"], etf_low=price["low"],
                    etf_close=price["close"], etf_volume=price["volume"], etf_turnover=price["turnover"],
                    holdings_count=0, holdings_weight_used=0,
                    market_momentum_score=components["gold_price_momentum"]["score"],
                    market_momentum_raw=components["gold_price_momentum"]["raw_value"],
                    put_call_options_score=components["gold_options"]["score"],
                    put_call_options_raw=components["gold_options"]["raw_value"],
                    stock_price_strength_score=components["cot_positioning"]["score"],
                    stock_price_strength_raw=components["cot_positioning"]["raw_value"],
                    stock_price_breadth_score=components["gold_etf_demand"]["score"],
                    stock_price_breadth_raw=components["gold_etf_demand"]["raw_value"],
                    safe_haven_demand_score=components["real_yield_dollar"]["score"],
                    safe_haven_demand_raw=components["real_yield_dollar"]["raw_value"],
                    components=components,
                    warnings=[
                        "GLD专用五因子等权模型，不使用股票ETF七因子。",
                        "COT按周五公布日生效；黄金ETF持仓按来源发布日期向后填充。",
                    ], updated_at=datetime.now(),
                ))
                saved += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            Session.remove()
        return {"symbol": "GLD.US", "saved": saved, "start_date": start_value.isoformat(), "end_date": end_value.isoformat(), "component_count": 5}

    @staticmethod
    def rating(score: float) -> str:
        if score < 25: return "extreme fear"
        if score < 45: return "fear"
        if score <= 55: return "neutral"
        if score <= 75: return "greed"
        return "extreme greed"

    @staticmethod
    def _finite(value):
        return None if value is None or pd.isna(value) else round(float(value), 6)

    @staticmethod
    def _prices(start_date: date, end_date: date) -> pd.DataFrame:
        con = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            frame = con.execute("""
                SELECT trade_date, open, high, low, close, volume, turnover
                FROM us_stock_daily WHERE symbol='GLD.US' AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
            """, [start_date, end_date]).df()
        finally:
            con.close()
        if frame.empty: return frame
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        return frame.set_index("trade_date")

    @staticmethod
    def _options(start_date: date, end_date: date) -> pd.Series:
        db = Session()
        try:
            rows = db.query(ETFPutCallRatio).filter(
                ETFPutCallRatio.symbol == "GLD", ETFPutCallRatio.date >= start_date,
                ETFPutCallRatio.date <= end_date, ETFPutCallRatio.put_call_volume_ratio.isnot(None),
            ).order_by(ETFPutCallRatio.date).all()
            return pd.Series({pd.Timestamp(row.date): float(row.put_call_volume_ratio) for row in rows}, dtype=float)
        finally:
            db.close(); Session.remove()

    @staticmethod
    def _inputs(start_date: date, end_date: date) -> pd.DataFrame:
        columns = ["real_yield_10y", "broad_dollar_index", "cot_managed_money_long", "cot_managed_money_short", "cot_open_interest", "gold_etf_holdings_tonnes", "gold_etf_shares"]
        db = Session()
        try:
            rows = db.query(GoldFearGreedInput).filter(GoldFearGreedInput.date >= start_date, GoldFearGreedInput.date <= end_date).order_by(GoldFearGreedInput.date).all()
            data = [{"date": pd.Timestamp(row.date), **{key: getattr(row, key) for key in columns}} for row in rows]
        finally:
            db.close(); Session.remove()
        if not data: return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], name="date"))
        return pd.DataFrame(data).set_index("date")
