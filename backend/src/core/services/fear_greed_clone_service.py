from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests


FRED_GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CBOE_OPTIONS_DAILY_URL = (
    "https://cdn.cboe.com/data/us/options/market_statistics/daily/"
    "{date}_daily_options"
)
NASDAQ_HISTORY_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    name: str
    raw_label: str
    source: str
    proxy_note: str


COMPONENTS: Dict[str, ComponentSpec] = {
    "market_momentum": ComponentSpec(
        key="market_momentum",
        name="Market Momentum",
        raw_label="S&P 500 close / 125-day moving average - 1",
        source="FRED SP500",
        proxy_note="Close match to CNN's published component definition.",
    ),
    "stock_price_strength": ComponentSpec(
        key="stock_price_strength",
        name="Stock Price Strength",
        raw_label="RSP 52-week range position",
        source="Nasdaq RSP daily close",
        proxy_note=(
            "Proxy for NYSE net new 52-week highs/lows. Free NYSE new-high/"
            "new-low history is not reliably available without a paid feed."
        ),
    ),
    "stock_price_breadth": ComponentSpec(
        key="stock_price_breadth",
        name="Stock Price Breadth",
        raw_label="20-day RSP return - 20-day SPY return",
        source="Nasdaq RSP and SPY daily close",
        proxy_note=(
            "Proxy for McClellan Volume Summation. Equal-weight outperformance "
            "is used as a broad-participation signal."
        ),
    ),
    "put_call_options": ComponentSpec(
        key="put_call_options",
        name="Put/Call Options",
        raw_label="-5-day average total put/call ratio",
        source="Cboe daily options market statistics",
        proxy_note="Close match to CNN's published 5-day put/call component.",
    ),
    "market_volatility": ComponentSpec(
        key="market_volatility",
        name="Market Volatility",
        raw_label="-(VIX / 50-day moving average - 1)",
        source="FRED VIXCLS",
        proxy_note="Close match to CNN's published VIX/50-day component.",
    ),
    "safe_haven_demand": ComponentSpec(
        key="safe_haven_demand",
        name="Safe Haven Demand",
        raw_label="20-day SPY return - 20-day TLT return",
        source="Nasdaq SPY and TLT daily close",
        proxy_note=(
            "Proxy for stock returns minus Treasury-bond returns. Uses ETF "
            "price returns, so distributions are not adjusted."
        ),
    ),
    "junk_bond_demand": ComponentSpec(
        key="junk_bond_demand",
        name="Junk Bond Demand",
        raw_label="-(high-yield OAS - investment-grade OAS)",
        source="FRED BAMLH0A0HYM2 and BAMLC0A0CM",
        proxy_note="Close spread is greed; wide spread is fear.",
    ),
}


class FearGreedCloneError(Exception):
    pass


class FearGreedCloneCalculator:
    """Independent, free-data approximation of CNN's Fear & Greed Index.

    CNN publishes the seven component concepts, but not its exact
    normalization. This calculator keeps the component structure and maps each
    raw signal to 0-100 with a rolling z-score and normal CDF.
    """

    def __init__(self, cache_dir: Optional[str] = None, timeout: float = 30.0):
        default_cache_dir = "/var/lib/quant_robot/cache/fear_greed_clone"
        self.cache_dir = Path(
            cache_dir or os.getenv("FNG_CLONE_CACHE_DIR", default_cache_dir)
        )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
            }
        )

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self.session.get(url, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(1.0 + attempt)
        raise last_error or FearGreedCloneError(f"request failed: {url}")

    def calculate(
        self,
        as_of: Optional[str] = None,
        history_days: int = 550,
        score_window: int = 252,
        min_periods: int = 120,
        include_history: bool = False,
        history_points: int = 180,
    ) -> Dict[str, Any]:
        end_date = self._parse_as_of(as_of)
        if history_days < 180:
            raise FearGreedCloneError("history_days should be at least 180")
        if min_periods > score_window:
            raise FearGreedCloneError("min_periods cannot exceed score_window")

        start_date = end_date - timedelta(days=history_days)
        raw = self._build_raw_signals(start_date, end_date)
        score_df = self._score_raw_signals(raw, score_window, min_periods)

        score_columns = [f"{key}_score" for key in COMPONENTS]
        component_scores = score_df[score_columns].rename(
            columns={f"{key}_score": key for key in COMPONENTS}
        )
        score_df["fear_greed_clone"] = component_scores.mean(axis=1)
        valid = score_df.dropna(subset=score_columns + ["fear_greed_clone"])
        if valid.empty:
            raise FearGreedCloneError("not enough data to calculate the clone index")

        latest_date = valid.index.max()
        latest_row = valid.loc[latest_date]
        latest_score = float(latest_row["fear_greed_clone"])

        response: Dict[str, Any] = {
            "fear_and_greed_clone": {
                "score": round(latest_score, 2),
                "rating": self.rating(latest_score),
                "date": latest_date.date().isoformat(),
                "method": "equal-weighted rolling z-score normal CDF",
                "history_days": history_days,
                "score_window": score_window,
                "min_periods": min_periods,
            },
            "components": self._component_payload(raw, score_df, latest_date),
            "warnings": [
                "This is an independent clone, not CNN's undisclosed normalization.",
                "Scores use rolling z-score/CDF; CNN only discloses equal "
                "weighting and deviation-from-average logic.",
                "Stock strength and breadth are ETF-based proxies because "
                "free NYSE internals history is unreliable.",
            ],
        }

        if include_history:
            response["history"] = self._history_payload(valid, history_points)

        return response

    @staticmethod
    def rating(score: float) -> str:
        if score < 25:
            return "extreme fear"
        if score < 45:
            return "fear"
        if score <= 55:
            return "neutral"
        if score <= 75:
            return "greed"
        return "extreme greed"

    def _build_raw_signals(self, start_date: date, end_date: date) -> pd.DataFrame:
        index = pd.bdate_range(start_date, end_date, name="date")
        fred = self._fetch_fred(["SP500", "VIXCLS", "BAMLH0A0HYM2", "BAMLC0A0CM"])
        fred = fred.reindex(index).ffill()

        etf_prices = {
            symbol: self._fetch_nasdaq_history(symbol, start_date, end_date)
            for symbol in ("SPY", "RSP", "TLT")
        }
        prices = pd.concat(
            [series.rename(symbol) for symbol, series in etf_prices.items()], axis=1
        ).reindex(index).ffill()

        put_call = self._fetch_cboe_total_put_call(start_date, end_date)
        put_call = put_call.reindex(index).ffill(limit=3)

        df = pd.DataFrame(index=index)
        df["market_momentum"] = fred["SP500"] / fred["SP500"].rolling(125).mean() - 1.0

        rsp_high = prices["RSP"].rolling(252, min_periods=120).max()
        rsp_low = prices["RSP"].rolling(252, min_periods=120).min()
        df["stock_price_strength"] = (prices["RSP"] - rsp_low) / (rsp_high - rsp_low)

        df["stock_price_breadth"] = (
            prices["RSP"].pct_change(20) - prices["SPY"].pct_change(20)
        )
        df["put_call_options"] = -put_call.rolling(5, min_periods=3).mean()
        df["market_volatility"] = -(fred["VIXCLS"] / fred["VIXCLS"].rolling(50).mean() - 1.0)
        df["safe_haven_demand"] = prices["SPY"].pct_change(20) - prices["TLT"].pct_change(20)
        df["junk_bond_demand"] = -(fred["BAMLH0A0HYM2"] - fred["BAMLC0A0CM"])

        return df.replace([np.inf, -np.inf], np.nan)

    def _score_raw_signals(
        self,
        raw: pd.DataFrame,
        score_window: int,
        min_periods: int,
    ) -> pd.DataFrame:
        result = raw.copy()
        for key in COMPONENTS:
            series = raw[key]
            mean = series.rolling(score_window, min_periods=min_periods).mean()
            std = series.rolling(score_window, min_periods=min_periods).std(ddof=0)
            z_score = (series - mean) / std.replace(0, np.nan)
            result[f"{key}_score"] = (100.0 * self._normal_cdf(z_score)).clip(0, 100)
        return result

    @staticmethod
    def _normal_cdf(series: pd.Series) -> pd.Series:
        values = series.to_numpy(dtype=float)
        cdf = [
            0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
            if not np.isnan(value)
            else np.nan
            for value in values
        ]
        return pd.Series(cdf, index=series.index)

    def _fetch_fred(self, series_ids: List[str]) -> pd.DataFrame:
        from io import StringIO

        frames = []
        for series_id in series_ids:
            # FRED's CSV endpoint can stall with browser/API Accept headers in
            # this environment, so use a plain requests call for this source.
            response = self._plain_get(FRED_GRAPH_URL, params={"id": series_id})
            response.raise_for_status()
            frame = pd.read_csv(StringIO(response.text), na_values=["."])
            frame["date"] = pd.to_datetime(frame["observation_date"])
            frame = frame.drop(columns=["observation_date"]).set_index("date").sort_index()
            frame[series_id] = pd.to_numeric(frame[series_id], errors="coerce")
            frames.append(frame[[series_id]])

        return pd.concat(frames, axis=1).sort_index()

    def _plain_get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return requests.get(url, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(1.0 + attempt)
        raise last_error or FearGreedCloneError(f"request failed: {url}")

    def _fetch_nasdaq_history(self, symbol: str, start_date: date, end_date: date) -> pd.Series:
        response = self._get(
            NASDAQ_HISTORY_URL.format(symbol=symbol),
            params={
                "assetclass": "etf",
                "fromdate": start_date.isoformat(),
                "todate": end_date.isoformat(),
                "limit": "9999",
            },
            headers={
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/",
            },
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", {}).get("tradesTable", {}).get("rows", [])
        if not rows:
            raise FearGreedCloneError(f"Nasdaq returned no history for {symbol}")

        records = []
        for row in rows:
            close_text = str(row.get("close", "")).replace("$", "").replace(",", "")
            close = pd.to_numeric(close_text, errors="coerce")
            if pd.isna(close):
                continue
            records.append(
                {
                    "date": pd.to_datetime(row["date"], format="%m/%d/%Y"),
                    "close": float(close),
                }
            )

        if not records:
            raise FearGreedCloneError(f"Nasdaq history for {symbol} had no prices")

        df = pd.DataFrame(records).set_index("date").sort_index()
        return df["close"]

    def _fetch_cboe_total_put_call(self, start_date: date, end_date: date) -> pd.Series:
        values: List[Dict[str, Any]] = []
        for timestamp in pd.bdate_range(start_date, end_date):
            day = timestamp.date()
            ratio = self._fetch_cboe_total_put_call_for_day(day)
            if ratio is not None:
                values.append({"date": timestamp, "put_call": ratio})

        if not values:
            raise FearGreedCloneError("Cboe returned no total put/call data")

        df = pd.DataFrame(values).set_index("date").sort_index()
        return df["put_call"]

    def _fetch_cboe_total_put_call_for_day(self, day: date) -> Optional[float]:
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
            if item.get("name") == "TOTAL PUT/CALL RATIO":
                return float(item["value"])
        return None

    def _component_payload(
        self,
        raw: pd.DataFrame,
        scores: pd.DataFrame,
        latest_date: pd.Timestamp,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for key, spec in COMPONENTS.items():
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

    def _history_payload(self, valid: pd.DataFrame, history_points: int) -> List[Dict[str, Any]]:
        history = []
        for day, row in valid.tail(history_points).iterrows():
            score = float(row["fear_greed_clone"])
            history.append(
                {
                    "date": day.date().isoformat(),
                    "score": round(score, 2),
                    "rating": self.rating(score),
                }
            )
        return history

    @staticmethod
    def _parse_as_of(as_of: Optional[str]) -> date:
        if not as_of:
            return datetime.utcnow().date()
        return datetime.strptime(as_of, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate an independent Fear & Greed clone."
    )
    parser.add_argument("--as-of", default=None, help="End date in YYYY-MM-DD format")
    parser.add_argument("--history-days", type=int, default=550)
    parser.add_argument("--score-window", type=int, default=252)
    parser.add_argument("--min-periods", type=int, default=120)
    parser.add_argument("--include-history", action="store_true")
    args = parser.parse_args()

    calculator = FearGreedCloneCalculator()
    result = calculator.calculate(
        as_of=args.as_of,
        history_days=args.history_days,
        score_window=args.score_window,
        min_periods=args.min_periods,
        include_history=args.include_history,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
