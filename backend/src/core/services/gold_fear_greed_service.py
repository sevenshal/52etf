import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ..database import ETFFearGreedCloneHistory, ETFPutCallRatio, GoldFearGreedInput, Session
from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from .fear_greed_clone_service import FearGreedCloneCalculator


GOLD_COMPONENTS = {
    "gold_price_momentum": ("黄金价格趋势", "GLD收盘价相对125日均线"),
    "gold_options": ("黄金期权情绪", "GLD五日平均Put/Call成交量比（反向）"),
    "cot_positioning": ("COMEX投机持仓", "Managed Money净多头占未平仓量"),
    "gold_etf_demand": ("黄金ETF持仓需求", "实物黄金ETF持仓吨数/份额四个披露期变化"),
    "real_yield_dollar": ("实际利率与美元", "10年实际利率与广义美元20日变化（反向）"),
}

# GVZ 保留为可观察的评分分量，但阶段顶/底和顶信号回测未显示其改善总分，
# 暂不纳入 GLD 总分；后续积累更多样本后再重新验证。
GOLD_DISPLAY_ONLY_COMPONENTS = {
    "gold_implied_volatility": ("黄金隐含波动率（GVZ）", "GVZ：GLD期权隐含30日波动率（正向，暂未参与总分）"),
}
GOLD_Z_SCORE_COMPONENTS = {**GOLD_COMPONENTS, **GOLD_DISPLAY_ONLY_COMPONENTS}

# 直接分量：外部已是 0-100 分数，不做滚动 z-score 归一化；
# 缺失时自动从等权平均中剔除，不阻塞历史。
GOLD_DIRECT_COMPONENTS = {
    "szdt_fear_greed": ("外部来源贪恐", "外部来源 GLD 情绪分（-100..100 线性映射为 0-100）"),
}

GOLD_ALL_COMPONENTS = {**GOLD_Z_SCORE_COMPONENTS, **GOLD_DIRECT_COMPONENTS}


class GoldFearGreedCalculator:
    """GLD专用五因子贪恐，GVZ作为展示观察分量。"""

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
        gvz_history = self._fetch_gvz_series(start_date, end_date)
        gvz = (
            gvz_history.reindex(gvz_history.index.union(index))
            .sort_index().ffill().reindex(index)
        )
        input_history = self._inputs(start_date, end_date)
        inputs = (
            input_history.reindex(input_history.index.union(index))
            .sort_index().ffill().reindex(index)
        )

        raw = pd.DataFrame(index=index)
        raw["gold_price_momentum"] = prices["close"] / prices["close"].rolling(125).mean() - 1.0
        raw["gold_options"] = -options.rolling(5, min_periods=3).mean()
        # GLD 阶段顶的 GVZ 显著高于阶段底，因此作为黄金过热/贪婪信号正向评分；
        # 不能套用股票“IV 越高越恐慌”的反向符号。
        raw["gold_implied_volatility"] = gvz
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
        # 外部 GLD 贪恐：直接分量，不参与 z-score 归一化
        szdt = self._fetch_szdt_emotion_series(start_date, end_date)
        raw["szdt_fear_greed"] = szdt.reindex(index).ffill(limit=3)
        raw = raw.replace([np.inf, -np.inf], np.nan)

        scored = pd.DataFrame(index=index)
        for key in GOLD_Z_SCORE_COMPONENTS:
            series = raw[key]
            mean = series.rolling(score_window, min_periods=min_periods).mean()
            std = series.rolling(score_window, min_periods=min_periods).std(ddof=0).replace(0, np.nan)
            z = (series - mean) / std
            scored[key] = pd.Series(
                [50.0 * (1.0 + math.erf(value / math.sqrt(2.0))) if pd.notna(value) else np.nan for value in z],
                index=index,
            ).clip(0, 100)
        scored["szdt_fear_greed"] = raw["szdt_fear_greed"].clip(0, 100)
        # 五个总分 z-score 分量必须齐全；GVZ 仅展示，不参与总分。
        # 外部来源有数据时按 1/6 等权，缺失时自动剔除。
        z_score_mean = scored[list(GOLD_COMPONENTS)].mean(axis=1, skipna=False)
        scored["score"] = (
            pd.concat([z_score_mean, scored["szdt_fear_greed"]], axis=1)
            .mean(axis=1)
            .where(z_score_mean.notna())
        )
        valid = scored.dropna(subset=["score"])
        if output_start_date:
            valid = valid[valid.index.date >= output_start_date]
        records = []
        for timestamp, score_row in valid.iterrows():
            components = {}
            for key, (name, label) in GOLD_ALL_COMPONENTS.items():
                component_score = score_row[key]
                raw_value = raw.loc[timestamp, key]
                included_in_total = key in GOLD_COMPONENTS or key in GOLD_DIRECT_COMPONENTS
                if pd.isna(component_score):
                    components[key] = {
                        "name": name,
                        "score": None,
                        "rating": None,
                        "raw_value": None,
                        "used_in_score": False,
                        "included_in_total": included_in_total,
                        "raw_label": label,
                    }
                    continue
                components[key] = {
                    "name": name,
                    "score": round(float(component_score), 2),
                    "rating": self.rating(float(component_score)),
                    "raw_value": round(float(raw_value), 8),
                    "used_in_score": included_in_total,
                    "included_in_total": included_in_total,
                    "raw_label": label,
                }
            price = prices.loc[timestamp]
            records.append({
                "symbol": "GLD.US",
                "date": timestamp.date().isoformat(),
                "score": round(float(score_row["score"]), 2),
                "rating": self.rating(float(score_row["score"])),
                "method": "gold 5 z-score factors + external direct component equal-weight; GVZ display-only",
                "components": components,
                "etf_price": {key: self._finite(price.get(key)) for key in ("open", "high", "low", "close", "volume", "turnover")},
            })
        return {"symbol": "GLD.US", "component_count": len(GOLD_ALL_COMPONENTS), "records": records}

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
                szdt_component = components.get("szdt_fear_greed") or {}
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
                    implied_volatility_score=components["gold_implied_volatility"]["score"],
                    implied_volatility_raw=components["gold_implied_volatility"]["raw_value"],
                    stock_price_strength_score=components["cot_positioning"]["score"],
                    stock_price_strength_raw=components["cot_positioning"]["raw_value"],
                    stock_price_breadth_score=components["gold_etf_demand"]["score"],
                    stock_price_breadth_raw=components["gold_etf_demand"]["raw_value"],
                    safe_haven_demand_score=components["real_yield_dollar"]["score"],
                    safe_haven_demand_raw=components["real_yield_dollar"]["raw_value"],
                    szdt_fear_greed_score=szdt_component.get("score"),
                    szdt_fear_greed_raw=szdt_component.get("raw_value"),
                    components=components,
                    warnings=[
                        "GLD专用模型：5 个 z-score 分量 + 外部来源贪恐直接分量等权；GVZ 黄金期权隐含波动率仅展示，不参与总分。",
                        "外部来源 GLD 贪恐（-100..100 映射为 0-100）为直接分量，缺失时自动从等权平均中剔除。",
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
        return {"symbol": "GLD.US", "saved": saved, "start_date": start_value.isoformat(), "end_date": end_value.isoformat(), "component_count": len(GOLD_ALL_COMPONENTS)}

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

    @staticmethod
    def _fetch_gvz_series(start_date: date, end_date: date) -> pd.Series:
        """Cboe GVZ：GLD 期权隐含的约 30 日预期波动率。"""
        try:
            frame = FearGreedCloneCalculator()._fetch_fred(["GVZCLS"])
            return frame["GVZCLS"].loc[
                (frame.index >= pd.Timestamp(start_date))
                & (frame.index <= pd.Timestamp(end_date))
            ].dropna()
        except Exception as exc:
            logging.getLogger(__name__).warning("Failed to fetch GVZ history: %s", exc)
            return pd.Series(dtype=float)

    @staticmethod
    def _fetch_szdt_emotion_series(start_date: date, end_date: date) -> pd.Series:
        """守猪逮兔 US.GLD 贪恐历史（-100..100 线性映射为 0-100）。

        取数失败或没有数据时返回空序列：该直接分量缺失不阻塞主计算。
        """
        logger = logging.getLogger(__name__)
        try:
            from .szdt import SZDTService

            response = SZDTService().get_etf_emotion_history_sync("US.GLD")
            data = (response or {}).get("data") or []
            values: Dict[pd.Timestamp, float] = {}
            for item in data:
                try:
                    day = pd.Timestamp(item.get("date"))
                    raw_score = float(item.get("score"))
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(raw_score):
                    continue
                values[day] = min(max((raw_score + 100.0) / 2.0, 0.0), 100.0)
            if not values:
                logger.warning("SZDT returned no GLD emotion history")
                return pd.Series(dtype=float)
            series = pd.Series(values).sort_index()
            series = series[~series.index.duplicated(keep="last")]
            return series.loc[
                (series.index >= pd.Timestamp(start_date))
                & (series.index <= pd.Timestamp(end_date))
            ]
        except Exception as exc:
            logger.warning("Failed to fetch SZDT GLD emotion history: %s", exc)
            return pd.Series(dtype=float)
