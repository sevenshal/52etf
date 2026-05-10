from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests


CHINABOND_YIELD_CURVE_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc"


class ChinaBondYieldCurveService:
    """Small client for ChinaBond yield curve points."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": "https://yield.chinabond.com.cn",
                "Referer": "https://yield.chinabond.com.cn/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def get_yield_curve_frame(
        self,
        trade_date: date,
        curve_ids: Iterable[str],
    ) -> pd.DataFrame:
        return self.get_yield_curve_dates_frame([trade_date], curve_ids)

    def get_yield_curve_dates_frame(
        self,
        trade_dates: Iterable[date],
        curve_ids: Iterable[str],
    ) -> pd.DataFrame:
        dates = sorted({item for item in (self._to_date(value) for value in trade_dates) if item})
        ids = [str(item or "").strip() for item in curve_ids if str(item or "").strip()]
        if not dates or not ids:
            return pd.DataFrame()

        if len(dates) > 1 and len(ids) > 1:
            payload: List[Dict[str, Any]] = []
            for curve_id in ids:
                payload.extend(self._fetch_yield_curves(dates, [curve_id]))
        else:
            payload = self._fetch_yield_curves(dates, ids)
        records: List[Dict[str, Any]] = []
        for item in payload:
            curve_id = str(item.get("ycDefId") or "").strip()
            curve_name = str(item.get("ycDefName") or "").strip()
            worktime = self._to_date(item.get("worktime")) or dates[0]
            for point in item.get("seriesData") or []:
                if not isinstance(point, list) or len(point) < 2:
                    continue
                try:
                    term = float(point[0])
                    yield_rate = float(point[1])
                except (TypeError, ValueError):
                    continue
                records.append(
                    {
                        "trade_date": worktime,
                        "curve_id": curve_id,
                        "curve_name": curve_name,
                        "term": term,
                        "yield_rate": yield_rate,
                    }
                )

        if not records:
            return pd.DataFrame()
        return pd.DataFrame(records).drop_duplicates(
            subset=["trade_date", "curve_id", "term"],
            keep="last",
        )

    def _fetch_yield_curves(self, trade_dates: List[date], curve_ids: List[str]) -> List[Dict[str, Any]]:
        params = {
            "xyzSelect": "txy",
            "workTimes": ",".join(item.strftime("%Y-%m-%d") for item in trade_dates),
            "dxbj": "0",
            "qxll": "0,",
            "yqqxN": "N",
            "yqqxK": "K",
            "ycDefIds": ",".join(curve_ids) + ",",
            "wrjxCBFlag": "0",
            "locale": "zh_cN",
        }
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self.session.post(
                    CHINABOND_YIELD_CURVE_URL,
                    params=params,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, list):
                    return payload
                raise ValueError("ChinaBond returned non-list payload")
            except Exception as exc:
                last_error = exc
                if attempt >= 2:
                    break
                time.sleep(0.5 + attempt)
        raise RuntimeError(f"ChinaBond yield curve fetch failed for {trade_dates[0]}~{trade_dates[-1]}: {last_error}")

    @staticmethod
    def _to_date(value) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        parsed = pd.to_datetime(str(value), errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date()
