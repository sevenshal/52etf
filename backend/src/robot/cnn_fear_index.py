from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from sqlalchemy import func

from ..core.database import CNNFearGreedIndex, ETFFearGreedCloneHistory, Session


CNN_BASE_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_HISTORY_SYMBOL = "CNN*.US"
CNN_HISTORY_START_DATE = date(2021, 1, 22)
CNN_HISTORY_INCREMENTAL_LOOKBACK_DAYS = 7
CNN_HISTORY_METHOD = "CNN official Fear & Greed history"
CNN_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "origin": "https://www.cnn.com",
    "pragma": "no-cache",
    "referer": "https://www.cnn.com/",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
        "Mobile/15E148 Safari/604.1"
    ),
}

CNN_COMPONENT_KEYS = (
    "market_momentum_sp500",
    "market_momentum_sp125",
    "stock_price_strength",
    "stock_price_breadth",
    "put_call_options",
    "market_volatility_vix",
    "market_volatility_vix_50",
    "junk_bond_demand",
    "safe_haven_demand",
)


class CNNFearGreedIndexScraper:
    def __init__(self):
        today_utc = datetime.now(timezone.utc).date()
        self.base_url = CNN_BASE_URL
        self.url = self._build_url(today_utc)
        self.db_session = Session()

    def fetch_data(self, start_date: Optional[Any] = None):
        request_start_date = self._coerce_date(start_date) or datetime.now(timezone.utc).date()
        response = requests.get(
            self._build_url(request_start_date),
            headers=CNN_HEADERS,
            timeout=30,
        )
        response.raise_for_status()  # 如果请求失败则抛出异常
        return response.json()

    def __save_to_db(self, data):
        # 提取主要的恐慌贪婪指数
        fear_and_greed = data["fear_and_greed"]
        historical = data.get("fear_and_greed_historical") or {}
        index_timestamp = self._parse_cnn_datetime(
            fear_and_greed.get("timestamp") or historical.get("timestamp")
        )
        if index_timestamp is None:
            rows = historical.get("data") or []
            index_timestamp = (
                self._parse_cnn_datetime(rows[-1].get("x"))
                if rows
                else datetime.utcnow()
            )

        # 创建并保存记录
        new_record = CNNFearGreedIndex(
            date=index_timestamp.date(),
            index_value=float(fear_and_greed["score"]),
            index_timestamp=index_timestamp,
            previous_close=self._number_or_none(fear_and_greed.get("previous_close")),
            previous_1_week=self._number_or_none(fear_and_greed.get("previous_1_week")),
            previous_1_month=self._number_or_none(fear_and_greed.get("previous_1_month")),
            previous_1_year=self._number_or_none(fear_and_greed.get("previous_1_year")),
            market_momentum=self._component_score(data, "market_momentum_sp500"),
            market_momentum_125=self._component_score(data, "market_momentum_sp125"),
            stock_price_strength=self._component_score(data, "stock_price_strength"),
            stock_price_breadth=self._component_score(data, "stock_price_breadth"),
            put_call_options=self._component_score(data, "put_call_options"),
            market_volatility_vix=self._component_score(data, "market_volatility_vix"),
            market_volatility_vix_50=self._component_score(data, "market_volatility_vix_50"),
            junk_bond_demand=self._component_score(data, "junk_bond_demand"),
            safe_haven_demand=self._component_score(data, "safe_haven_demand"),
            created_at=datetime.now()
        )
        self.db_session.merge(new_record)
        self.db_session.commit()

    def sync_history_to_db(
        self,
        data: Dict[str, Any],
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
    ) -> Dict[str, Any]:
        start_day = self._coerce_date(start_date)
        end_day = self._coerce_date(end_date)
        components_by_date = self._build_component_history_by_date(data)
        rows = data.get("fear_and_greed_historical", {}).get("data") or []
        history_by_date: Dict[date, Dict[str, Any]] = {}
        saved = 0
        first_saved = None
        last_saved = None

        for item in rows:
            item_day = self._parse_cnn_date(item.get("x"))
            if item_day is None:
                continue
            if start_day and item_day < start_day:
                continue
            if end_day and item_day > end_day:
                continue
            score = self._number_or_none(item.get("y"))
            if score is None:
                continue
            history_by_date[item_day] = item

        for item_day in sorted(history_by_date):
            item = history_by_date[item_day]
            score = self._number_or_none(item.get("y"))
            if score is None:
                continue
            components = components_by_date.get(item_day, {})
            components["fear_and_greed"] = {
                "score": score,
                "rating": item.get("rating") or self._rating_from_score(score),
                "source": "CNN",
            }
            now = datetime.now()
            record = ETFFearGreedCloneHistory(
                symbol=CNN_HISTORY_SYMBOL,
                date=item_day,
                score=score,
                rating=item.get("rating") or self._rating_from_score(score),
                method=CNN_HISTORY_METHOD,
                use_historical_holdings=False,
                market_momentum_raw=self._component_raw(components, "market_momentum_sp500"),
                stock_price_strength_raw=self._component_raw(components, "stock_price_strength"),
                stock_price_breadth_raw=self._component_raw(components, "stock_price_breadth"),
                put_call_options_raw=self._component_raw(components, "put_call_options"),
                market_volatility_raw=self._component_raw(components, "market_volatility_vix"),
                safe_haven_demand_raw=self._component_raw(components, "safe_haven_demand"),
                junk_bond_demand_raw=self._component_raw(components, "junk_bond_demand"),
                components=components,
                warnings=[],
                updated_at=now,
            )
            self.db_session.merge(record)
            saved += 1
            first_saved = item_day if first_saved is None else min(first_saved, item_day)
            last_saved = item_day if last_saved is None else max(last_saved, item_day)
            if saved % 500 == 0:
                self.db_session.commit()

        self.db_session.commit()
        return {
            "symbol": CNN_HISTORY_SYMBOL,
            "saved": saved,
            "start_date": first_saved.isoformat() if first_saved else None,
            "end_date": last_saved.isoformat() if last_saved else None,
        }

    def fetch_data_and_save(self):
        data = self.fetch_data()
        self.__save_to_db(data)
        return data

    def fetch_data_and_save_history(self, start_date: Optional[Any] = None):
        history_start_date, mode = self._resolve_history_start_date(start_date)
        data = self.fetch_data(history_start_date)
        self.__save_to_db(data)
        history_result = self.sync_history_to_db(data, start_date=history_start_date)
        history_result["mode"] = mode
        history_result["fetch_start_date"] = history_start_date.isoformat()
        return {
            "data": data,
            "history": history_result,
        }

    def _resolve_history_start_date(self, start_date: Optional[Any]) -> Tuple[date, str]:
        explicit_start_date = self._coerce_date(start_date)
        if explicit_start_date:
            return explicit_start_date, "manual"

        latest_date = (
            self.db_session.query(func.max(ETFFearGreedCloneHistory.date))
            .filter(ETFFearGreedCloneHistory.symbol == CNN_HISTORY_SYMBOL)
            .scalar()
        )
        if latest_date:
            buffered_start_date = max(
                CNN_HISTORY_START_DATE,
                latest_date - timedelta(days=CNN_HISTORY_INCREMENTAL_LOOKBACK_DAYS),
            )
            return buffered_start_date, "incremental"
        return CNN_HISTORY_START_DATE, "full"

    def _build_component_history_by_date(self, data: Dict[str, Any]) -> Dict[date, Dict[str, Any]]:
        result: Dict[date, Dict[str, Any]] = {}
        for component_key in CNN_COMPONENT_KEYS:
            for item in (data.get(component_key, {}) or {}).get("data") or []:
                item_day = self._parse_cnn_date(item.get("x"))
                value = self._number_or_none(item.get("y"))
                if item_day is None or value is None:
                    continue
                result.setdefault(item_day, {})[component_key] = {
                    "raw_value": value,
                    "rating": item.get("rating"),
                    "source": f"CNN {component_key}",
                }
        return result

    def _build_url(self, start_date: date) -> str:
        return f"{self.base_url}/{start_date.isoformat()}"

    @staticmethod
    def _coerce_date(value: Optional[Any]) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            return datetime.strptime(value, "%Y-%m-%d").date()
        raise ValueError(f"Unsupported date value: {value!r}")

    @staticmethod
    def _parse_cnn_datetime(value: Optional[Any]) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            return None

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @classmethod
    def _parse_cnn_date(cls, value: Optional[Any]) -> Optional[date]:
        parsed = cls._parse_cnn_datetime(value)
        return parsed.date() if parsed else None

    @staticmethod
    def _number_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _component_score(cls, data: Dict[str, Any], key: str) -> Optional[float]:
        return cls._number_or_none((data.get(key) or {}).get("score"))

    @staticmethod
    def _component_raw(components: Dict[str, Any], key: str) -> Optional[float]:
        component = components.get(key) or {}
        return component.get("raw_value")

    @staticmethod
    def _rating_from_score(score: float) -> str:
        if score <= 25:
            return "extreme fear"
        if score <= 45:
            return "fear"
        if score < 55:
            return "neutral"
        if score < 75:
            return "greed"
        return "extreme greed"

if __name__ == "__main__":
    scraper = CNNFearGreedIndexScraper()
    result = scraper.fetch_data_and_save_history()
    print(result["history"])
