import logging
import os
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any, Dict, Iterable, Optional

import pandas as pd
import requests
from ..core.database import GoldFearGreedInput, Session


FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_API_KEY = os.getenv("FRED_API_KEY", "f969b4eb2a07325467cffb3f100fa6ea")
CFTC_GOLD_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
SPDR_GLD_ARCHIVE_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; 52etf-gold-fear/1.0)"}


class GoldFearGreedInputSync:
    """同步黄金五因子的宏观、COT和实物黄金ETF持仓基础数据。"""

    def __init__(self, timeout: float = 45.0):
        self.timeout = timeout
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def close(self):
        self.session.close()

    def sync(self, start_date: Optional[date] = None, end_date: Optional[date] = None) -> Dict[str, Any]:
        end_value = end_date or date.today()
        start_value = start_date or date(2009, 9, 1)
        result = {"start_date": start_value.isoformat(), "end_date": end_value.isoformat(), "sources": {}, "errors": []}
        frames = []
        for name, loader in (
            ("fred", lambda: self._fetch_fred(start_value, end_value)),
            ("cftc_cot", lambda: self._fetch_cot(start_value, end_value)),
            ("spdr_gld_archive", self._fetch_gold_etf_holdings),
        ):
            try:
                frame = loader()
                if frame is not None and not frame.empty:
                    frames.append(frame)
                result["sources"][name] = {"rows": 0 if frame is None else len(frame)}
            except Exception as exc:
                self.logger.warning("Gold fear input %s sync failed: %s", name, exc)
                result["errors"].append({"source": name, "error": str(exc)})
        result["saved"] = self._save_frames(frames)
        result["status"] = "partial_failed" if result["errors"] else "success"
        return result

    def _get(self, url: str, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.get(url, **kwargs)
        response.raise_for_status()
        return response

    def _fetch_fred(self, start_date: date, end_date: date) -> pd.DataFrame:
        parts = []
        for series in ("DFII10", "DTWEXBGS"):
            payload = self._get(FRED_URL, params={
                "series_id": series,
                "file_type": "json",
                "observation_start": start_date.isoformat(),
                "observation_end": end_date.isoformat(),
                "api_key": FRED_API_KEY,
            }).json()
            frame = pd.DataFrame(payload.get("observations") or [])
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
            frame[series] = pd.to_numeric(frame["value"], errors="coerce")
            parts.append(frame[["date", series]])
        result = parts[0].merge(parts[1], on="date", how="outer")
        result = result[(result["date"] >= start_date) & (result["date"] <= end_date)]
        return result.rename(columns={"DFII10": "real_yield_10y", "DTWEXBGS": "broad_dollar_index"})

    def _fetch_cot(self, start_date: date, end_date: date) -> pd.DataFrame:
        params = {
            "$select": "report_date_as_yyyy_mm_dd,m_money_positions_long_all,m_money_positions_short_all,open_interest_all",
            "$where": (
                "cftc_contract_market_code='088691' AND "
                f"report_date_as_yyyy_mm_dd between '{start_date.isoformat()}T00:00:00.000' "
                f"and '{end_date.isoformat()}T23:59:59.999'"
            ),
            "$order": "report_date_as_yyyy_mm_dd asc",
            "$limit": "50000",
        }
        rows = self._get(CFTC_GOLD_URL, params=params).json()
        values = []
        for item in rows:
            report_date = pd.to_datetime(item.get("report_date_as_yyyy_mm_dd"), errors="coerce")
            if pd.isna(report_date):
                continue
            # 周二持仓通常周五发布；以周五作为可用日，防止历史回测偷看。
            values.append({
                "date": (report_date.date() + timedelta(days=3)),
                "cot_managed_money_long": self._number(item.get("m_money_positions_long_all")),
                "cot_managed_money_short": self._number(item.get("m_money_positions_short_all")),
                "cot_open_interest": self._number(item.get("open_interest_all")),
            })
        return pd.DataFrame(values)

    def _fetch_gold_etf_holdings(self) -> pd.DataFrame:
        workbook = self._get(SPDR_GLD_ARCHIVE_URL, params={
            "product": "gld",
            "exchange": "NYSE",
            "lang": "en",
        }).content
        frame = pd.read_excel(BytesIO(workbook), sheet_name="US GLD Historical Archive")
        date_col = "Date"
        tonnes_col = "Tonnes of Gold"
        ounces_col = "Total Ounces of Gold in the Trust"
        if date_col not in frame or tonnes_col not in frame:
            raise RuntimeError("SPDR GLD archive columns changed")
        result = pd.DataFrame({
            "date": pd.to_datetime(frame[date_col], errors="coerce").dt.date,
            "gold_etf_holdings_tonnes": pd.to_numeric(frame[tonnes_col], errors="coerce"),
            # 官方历史档案不直接给份额；总黄金盎司仍作为持仓口径的备用原始值。
            "gold_etf_shares": pd.to_numeric(frame.get(ounces_col), errors="coerce"),
        })
        return result.dropna(subset=["date", "gold_etf_holdings_tonnes"]).drop_duplicates("date", keep="last")

    def _extract_gld_holdings_rows(self, frame: pd.DataFrame) -> Iterable[Dict[str, Any]]:
        frame = frame.copy()
        frame.columns = [str(value).strip() for value in frame.columns]
        lower = {column: column.lower() for column in frame.columns}
        date_col = next((c for c, value in lower.items() if "date" in value), None)
        tonnes_col = next((c for c, value in lower.items() if "holding" in value and ("ton" in value or "tonne" in value)), None)
        shares_col = next((c for c, value in lower.items() if "share" in value and "outstanding" in value), None)
        ticker_col = next((c for c, value in lower.items() if "ticker" in value or value == "fund"), None)
        if not date_col or not (tonnes_col or shares_col):
            return []
        rows = []
        for _, item in frame.iterrows():
            if ticker_col and "GLD" not in str(item.get(ticker_col, "")).upper():
                continue
            day = pd.to_datetime(item.get(date_col), errors="coerce")
            if pd.isna(day):
                continue
            rows.append({
                "date": day.date(),
                "gold_etf_holdings_tonnes": self._number(item.get(tonnes_col)) if tonnes_col else None,
                "gold_etf_shares": self._number(item.get(shares_col)) if shares_col else None,
            })
        return rows

    def _save_frames(self, frames) -> int:
        if not frames:
            return 0
        combined = frames[0]
        for frame in frames[1:]:
            combined = combined.merge(frame, on="date", how="outer")
        combined = combined.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
        db = Session()
        saved = 0
        try:
            for item in combined.to_dict("records"):
                day = item.pop("date")
                existing = db.get(GoldFearGreedInput, day)
                row = existing or GoldFearGreedInput(date=day)
                for key, value in item.items():
                    if key in {"real_yield_10y", "broad_dollar_index", "cot_managed_money_long", "cot_managed_money_short", "cot_open_interest", "gold_etf_holdings_tonnes", "gold_etf_shares"} and pd.notna(value):
                        setattr(row, key, float(value))
                row.sources = {"fred": FRED_URL, "cftc": CFTC_GOLD_URL, "gold_etf": SPDR_GLD_ARCHIVE_URL}
                row.updated_at = datetime.now()
                if existing is None:
                    db.add(row)
                saved += 1
            db.commit()
            return saved
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            Session.remove()

    @staticmethod
    def _number(value):
        try:
            if value is None or pd.isna(value):
                return None
            return float(str(value).replace(",", "").replace("%", ""))
        except (TypeError, ValueError):
            return None


def sync_gold_fear_greed_inputs(start_date: Optional[date] = None, end_date: Optional[date] = None):
    syncer = GoldFearGreedInputSync()
    try:
        return syncer.sync(start_date=start_date, end_date=end_date)
    finally:
        syncer.close()
