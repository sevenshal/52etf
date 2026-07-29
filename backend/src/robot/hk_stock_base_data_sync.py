from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd
import requests

from ..core.analytics_database import ANALYTICS_DB_PATH, ensure_analytics_schema
from ..core.duckdb_utils import connect_duckdb
from ..core.services.tushare import TushareService
from .hk_stock_base_data_config import (
    HANG_SENG_REVIEW_RELEASE_DATES,
    HK_INDEX_FEAR_GREED_TARGETS,
    HK_STOCK_DEFAULT_START_DATE,
)


logger = logging.getLogger(__name__)
HK_DAILY_MIN_INTERVAL_SECONDS = float(os.getenv("HK_DAILY_MIN_INTERVAL_SECONDS", "61"))
HK_REVIEW_CACHE_DIR = os.getenv(
    "HK_REVIEW_CACHE_DIR",
    "/var/lib/quant_robot/cache/hk_index_reviews",
)


def _parse_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _date_column(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_datetime(frame[column].astype("string"), errors="coerce").dt.date


def _numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(pd.NA, index=frame.index, dtype="Float64")
    return pd.to_numeric(frame[column], errors="coerce").astype("Float64")


def _upsert_frame(table: str, columns: List[str], frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    payload = frame.loc[:, columns].copy()
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    temp_name = "hk_sync_payload"
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.register(temp_name, payload)
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            f'INSERT OR REPLACE INTO "{table}" ({quoted_columns}) '
            f'SELECT {quoted_columns} FROM "{temp_name}"'
        )
        connection.execute("COMMIT")
        return len(payload)
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()


class HKStockBaseDataSyncService:
    """Synchronize HK market data without consuming LongPort symbol quota."""

    def __init__(self, tushare_service: Optional[TushareService] = None):
        ensure_analytics_schema()
        self.tushare = tushare_service or TushareService.getInstance()
        self._last_hk_daily_request_at: Optional[float] = None

    def sync_basic(self) -> int:
        frame = self.tushare.pro.hk_basic(list_status="L")
        if frame is None or frame.empty:
            return 0
        result = pd.DataFrame(index=frame.index)
        result["ts_code"] = frame["ts_code"].astype("string").str.strip().str.upper()
        for column in ("name", "fullname", "enname", "market", "list_status", "isin", "curr_type"):
            result[column] = frame[column].astype("string").str.strip() if column in frame else None
        result["list_date"] = _date_column(frame, "list_date")
        result["delist_date"] = _date_column(frame, "delist_date")
        result["trade_unit"] = _numeric_column(frame, "trade_unit")
        result["updated_at"] = datetime.now()
        return _upsert_frame(
            "hk_stock_basic",
            [
                "ts_code", "name", "fullname", "enname", "market", "list_status",
                "list_date", "delist_date", "trade_unit", "isin", "curr_type", "updated_at",
            ],
            result.dropna(subset=["ts_code"]),
        )

    def trading_dates(self, start_date: date, end_date: date) -> List[date]:
        frame = self.tushare.pro.hk_tradecal(
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        if frame is None or frame.empty:
            return []
        frame = frame[pd.to_numeric(frame["is_open"], errors="coerce") == 1]
        return sorted(item for item in _date_column(frame, "cal_date").dropna().tolist())

    def _wait_for_hk_daily_quota(self):
        if self._last_hk_daily_request_at is None:
            return
        elapsed = time.monotonic() - self._last_hk_daily_request_at
        remaining = HK_DAILY_MIN_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def sync_market_day(self, trade_date: date) -> int:
        self._wait_for_hk_daily_quota()
        try:
            frame = self.tushare.pro.hk_daily(trade_date=trade_date.strftime("%Y%m%d"))
        finally:
            self._last_hk_daily_request_at = time.monotonic()
        if frame is None or frame.empty:
            return 0
        return self._save_market_frame(frame)

    def sync_market_symbol(self, symbol: str, start_date: date, end_date: date) -> int:
        self._wait_for_hk_daily_quota()
        try:
            frame = self.tushare.pro.hk_daily(
                ts_code=normalize_hk_symbol(symbol),
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
        finally:
            self._last_hk_daily_request_at = time.monotonic()
        if frame is None or frame.empty:
            return 0
        return self._save_market_frame(frame)

    def _save_market_frame(self, frame: pd.DataFrame) -> int:
        result = pd.DataFrame(index=frame.index)
        result["trade_date"] = _date_column(frame, "trade_date")
        result["ts_code"] = frame["ts_code"].astype("string").str.strip().str.upper()
        for column in ("open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"):
            result[column] = _numeric_column(frame, column)
        now = datetime.now()
        result["created_at"] = now
        result["updated_at"] = now
        return _upsert_frame(
            "hk_stock_daily",
            [
                "trade_date", "ts_code", "open", "high", "low", "close", "pre_close",
                "change", "pct_chg", "vol", "amount", "created_at", "updated_at",
            ],
            result.dropna(subset=["trade_date", "ts_code"]),
        )

    def sync_constituent_history(self, start_date: date, end_date: date) -> Dict:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT con_code
                FROM hk_index_weight_snapshot
                WHERE effective_date >= ?
                   OR effective_date = (
                        SELECT MAX(w2.effective_date)
                        FROM hk_index_weight_snapshot w2
                        WHERE w2.index_code = hk_index_weight_snapshot.index_code
                          AND w2.effective_date < ?
                   )
                ORDER BY con_code
                """,
                [start_date, start_date],
            ).fetchall()
        finally:
            connection.close()
        symbols = [row[0] for row in rows]
        saved = 0
        completed = 0
        for symbol in symbols:
            saved += self.sync_market_symbol(symbol, start_date, end_date)
            completed += 1
            logger.info(
                "HK constituent history %s/%s symbol=%s saved_total=%s",
                completed,
                len(symbols),
                symbol,
                saved,
            )
        return {"symbols": len(symbols), "completed": completed, "rows": saved}

    def sync_constituent_history_yahoo(
        self,
        start_date: date,
        end_date: date,
        workers: int = 8,
        extra_symbols: Optional[List[str]] = None,
    ) -> Dict:
        symbols = self._constituent_symbols(start_date)
        symbols = list(dict.fromkeys([*symbols, *(extra_symbols or [])]))
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            covered = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT ts_code
                    FROM hk_stock_daily
                    WHERE trade_date BETWEEN ? AND ?
                    GROUP BY ts_code
                    HAVING COUNT(*) >= 120
                    """,
                    [start_date, end_date],
                ).fetchall()
            }
        finally:
            connection.close()
        symbols = [symbol for symbol in symbols if symbol not in covered]
        frames = {}
        errors = []
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 16))) as executor:
            futures = {
                executor.submit(self._fetch_yahoo_history, symbol, start_date, end_date): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    frame = future.result()
                    if frame.empty:
                        errors.append({"symbol": symbol, "error": "empty"})
                    else:
                        frames[symbol] = frame
                except Exception as exc:
                    errors.append({"symbol": symbol, "error": str(exc)})
        saved = 0
        for symbol in symbols:
            frame = frames.get(symbol)
            if frame is not None:
                saved += self._save_market_frame(frame)
        return {
            "symbols": len(symbols),
            "completed": len(frames),
            "rows": saved,
            "errors": errors,
            "source": "yahoo_chart_bootstrap",
        }

    def _constituent_symbols(self, start_date: date) -> List[str]:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT con_code
                FROM hk_index_weight_snapshot
                WHERE effective_date >= ?
                   OR effective_date = (
                        SELECT MAX(w2.effective_date)
                        FROM hk_index_weight_snapshot w2
                        WHERE w2.index_code = hk_index_weight_snapshot.index_code
                          AND w2.effective_date < ?
                   )
                ORDER BY con_code
                """,
                [start_date, start_date],
            ).fetchall()
            return [row[0] for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _fetch_yahoo_history(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
        normalized = normalize_hk_symbol(symbol)
        code = normalized.split(".", 1)[0]
        yahoo_symbol = f"{int(code):04d}.HK"
        period1 = int(datetime.combine(start_date, datetime.min.time()).timestamp())
        period2 = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time()).timestamp())
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
            params={
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "div,splits",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json().get("chart", {}).get("result")
        if not result:
            return pd.DataFrame()
        payload = result[0]
        timestamps = payload.get("timestamp") or []
        quote = (payload.get("indicators", {}).get("quote") or [{}])[0]
        frame = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(timestamps, unit="s", utc=True)
                .tz_convert("Asia/Hong_Kong")
                .date,
                "ts_code": normalized,
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": quote.get("close"),
                "vol": quote.get("volume"),
            }
        )
        frame = frame.dropna(subset=["trade_date", "close"])
        frame["pre_close"] = pd.to_numeric(frame["close"], errors="coerce").shift(1)
        frame["change"] = pd.to_numeric(frame["close"], errors="coerce") - frame["pre_close"]
        frame["pct_chg"] = frame["change"] / frame["pre_close"].replace(0, pd.NA) * 100
        frame["amount"] = (
            pd.to_numeric(frame["vol"], errors="coerce")
            * pd.to_numeric(frame["close"], errors="coerce")
            / 1000.0
        )
        return frame

    def build_hscei_proxy_index(self, proxy_symbol: str = "02828.HK") -> int:
        """Use the HSCEI tracker only when Tushare index_global lacks HSCEI."""
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
        try:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(
                """
                INSERT OR REPLACE INTO hk_index_daily (
                    ts_code, trade_date, open, high, low, close, pre_close,
                    change, pct_chg, swing, vol, source, created_at, updated_at
                )
                SELECT
                    'HSCEI', trade_date, open, high, low, close, pre_close,
                    change, pct_chg, NULL, vol, 'proxy_02828.HK',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM hk_stock_daily
                WHERE ts_code = ?
                """,
                [proxy_symbol],
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM hk_index_daily WHERE ts_code = 'HSCEI'"
            ).fetchone()[0]
            connection.execute("COMMIT")
            return int(count)
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def sync_yahoo_index(
        self,
        yahoo_symbol: str,
        index_code: str,
        start_date: date,
        end_date: date,
    ) -> int:
        period1 = int(datetime.combine(start_date, datetime.min.time()).timestamp())
        period2 = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time()).timestamp())
        response = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
            params={"period1": period1, "period2": period2, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json().get("chart", {}).get("result")
        if not result:
            return 0
        payload = result[0]
        quote = (payload.get("indicators", {}).get("quote") or [{}])[0]
        frame = pd.DataFrame(
            {
                "ts_code": index_code,
                "trade_date": pd.to_datetime(payload.get("timestamp") or [], unit="s", utc=True)
                .tz_convert("Asia/Hong_Kong")
                .date,
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": quote.get("close"),
                "vol": quote.get("volume"),
            }
        ).dropna(subset=["trade_date", "close"])
        frame["pre_close"] = pd.to_numeric(frame["close"], errors="coerce").shift(1)
        frame["change"] = pd.to_numeric(frame["close"], errors="coerce") - frame["pre_close"]
        frame["pct_chg"] = frame["change"] / frame["pre_close"].replace(0, pd.NA) * 100
        frame["swing"] = pd.NA
        frame["source"] = f"yahoo_{yahoo_symbol}"
        now = datetime.now()
        frame["created_at"] = now
        frame["updated_at"] = now
        return _upsert_frame(
            "hk_index_daily",
            [
                "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                "change", "pct_chg", "swing", "vol", "source", "created_at", "updated_at",
            ],
            frame,
        )

    def sync_market_daily(
        self,
        start_date: date,
        end_date: date,
        max_days: Optional[int] = None,
    ) -> Dict:
        existing = self._existing_market_dates(start_date, end_date)
        missing = [day for day in self.trading_dates(start_date, end_date) if day not in existing]
        if max_days is not None:
            missing = missing[:max(0, int(max_days))]
        rows = 0
        completed = []
        for day in missing:
            saved = self.sync_market_day(day)
            rows += saved
            completed.append(day.isoformat())
        return {"dates": completed, "date_count": len(completed), "rows": rows}

    def sync_index_daily(self, start_date: date, end_date: date) -> Dict:
        saved_by_symbol = {}
        for target in HK_INDEX_FEAR_GREED_TARGETS:
            provider_code = target.get("tushare_index_code")
            if not provider_code:
                saved_by_symbol[target["symbol"]] = 0
                continue
            frames = []
            for year in range(start_date.year, end_date.year + 1):
                chunk_start = max(start_date, date(year, 1, 1))
                chunk_end = min(end_date, date(year, 12, 31))
                frame = self.tushare.pro.index_global(
                    ts_code=provider_code,
                    start_date=chunk_start.strftime("%Y%m%d"),
                    end_date=chunk_end.strftime("%Y%m%d"),
                )
                if frame is not None and not frame.empty:
                    frames.append(frame)
            if not frames:
                saved_by_symbol[target["symbol"]] = 0
                continue
            frame = pd.concat(frames, ignore_index=True).drop_duplicates("trade_date", keep="last")
            result = pd.DataFrame(index=frame.index)
            result["ts_code"] = target["index_code"]
            result["trade_date"] = _date_column(frame, "trade_date")
            for column in ("open", "high", "low", "close", "pre_close", "change", "pct_chg", "swing", "vol"):
                result[column] = _numeric_column(frame, column)
            result["source"] = "tushare_index_global"
            now = datetime.now()
            result["created_at"] = now
            result["updated_at"] = now
            saved_by_symbol[target["symbol"]] = _upsert_frame(
                "hk_index_daily",
                [
                    "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                    "change", "pct_chg", "swing", "vol", "source", "created_at", "updated_at",
                ],
                result.dropna(subset=["trade_date"]),
            )
        return saved_by_symbol

    def import_weight_snapshot_manifest(self, path: str) -> Dict:
        """Import reviewed official snapshots from a JSON manifest.

        Each snapshot contains index_code, effective_date, optional metadata and
        a constituents array. Keeping extraction separate from DB writes makes
        PDF parsing reviewable and prevents malformed official documents from
        silently becoming production weights.
        """
        manifest_path = Path(path)
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshots = data.get("snapshots") if isinstance(data, dict) else data
        if not isinstance(snapshots, list):
            raise ValueError("weight manifest must contain a snapshots array")
        rows = []
        for snapshot in snapshots:
            index_code = str(snapshot.get("index_code") or "").strip().upper()
            effective_date = _parse_date(snapshot.get("effective_date"))
            if not index_code or not effective_date:
                raise ValueError("each weight snapshot requires index_code and effective_date")
            constituents = snapshot.get("constituents") or []
            for item in constituents:
                code = normalize_hk_symbol(item.get("code") or item.get("con_code"))
                weight = float(item.get("weight") or 0)
                if not code or weight <= 0:
                    continue
                rows.append(
                    {
                        "index_code": index_code,
                        "effective_date": effective_date,
                        "con_code": code,
                        "con_name": item.get("name") or item.get("con_name"),
                        "weight": weight,
                        "free_float_factor": item.get("free_float_factor"),
                        "reference_date": _parse_date(snapshot.get("reference_date")),
                        "source_url": snapshot.get("source_url"),
                        "source_document": snapshot.get("source_document") or manifest_path.name,
                        "extraction_method": snapshot.get("extraction_method") or "reviewed_manifest",
                        "verified": 1.0 if snapshot.get("verified", True) else 0.0,
                        "created_at": datetime.now(),
                        "updated_at": datetime.now(),
                    }
                )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return {"snapshots": 0, "rows": 0}
        self._validate_weight_snapshots(frame)
        saved = _upsert_frame(
            "hk_index_weight_snapshot",
            [
                "index_code", "effective_date", "con_code", "con_name", "weight",
                "free_float_factor", "reference_date", "source_url", "source_document",
                "extraction_method", "verified", "created_at", "updated_at",
            ],
            frame,
        )
        return {
            "snapshots": int(frame[["index_code", "effective_date"]].drop_duplicates().shape[0]),
            "rows": saved,
        }

    def download_official_review_documents(
        self,
        cache_dir: Optional[str] = None,
    ) -> Dict:
        """Cache official Hang Seng quarterly review PDFs for audited extraction."""
        target_dir = Path(cache_dir or HK_REVIEW_CACHE_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        downloaded = []
        cached = []
        missing = []
        for release_date in HANG_SENG_REVIEW_RELEASE_DATES:
            output = target_dir / f"{release_date}.pdf"
            if output.exists() and output.stat().st_size > 10_000:
                cached.append(str(output))
                continue
            found = False
            for timestamp in ("T174500", "T000000"):
                url = (
                    "https://www.hsi.com.hk/static/uploads/contents/en/news/"
                    f"pressRelease/{release_date}{timestamp}.pdf"
                )
                response = requests.get(url, timeout=30)
                if response.status_code != 200 or not response.content.startswith(b"%PDF"):
                    continue
                temp = output.with_suffix(".tmp")
                temp.write_bytes(response.content)
                temp.replace(output)
                downloaded.append({"path": str(output), "source_url": url})
                found = True
                break
            if not found:
                missing.append(release_date)
        index_path = target_dir / "source_index.json"
        index_path.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now().isoformat(),
                    "downloaded": downloaded,
                    "cached": cached,
                    "missing": missing,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "downloaded": len(downloaded),
            "cached": len(cached),
            "missing": missing,
            "cache_dir": str(target_dir),
        }

    @staticmethod
    def _validate_weight_snapshots(frame: pd.DataFrame):
        for keys, group in frame.groupby(["index_code", "effective_date"]):
            total = pd.to_numeric(group["weight"], errors="coerce").sum()
            if not 99.0 <= total <= 101.0:
                raise ValueError(f"HK index weight snapshot {keys} sums to {total:.4f}, expected about 100")
            if group["con_code"].duplicated().any():
                raise ValueError(f"HK index weight snapshot {keys} contains duplicate constituents")

    @staticmethod
    def _existing_market_dates(start_date: date, end_date: date) -> set:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM hk_stock_daily
                WHERE trade_date BETWEEN ? AND ?
                """,
                [start_date, end_date],
            ).fetchall()
            return {row[0] for row in rows}
        finally:
            connection.close()

    def sync_incremental(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        max_market_days: int = 1,
        weight_manifest_path: Optional[str] = None,
        download_review_documents: bool = False,
        review_cache_dir: Optional[str] = None,
    ) -> Dict:
        end_value = end_date or date.today()
        market_latest = self._max_date("hk_stock_daily")
        index_latest = self._max_date("hk_index_daily")
        market_start = start_date or (
            market_latest + timedelta(days=1)
            if market_latest
            else end_value - timedelta(days=10)
        )
        index_start = start_date or (
            index_latest - timedelta(days=10) if index_latest else HK_STOCK_DEFAULT_START_DATE
        )
        result = {
            "basic_rows": self.sync_basic(),
            "market": self.sync_market_daily(market_start, end_value, max_days=max_market_days),
            "indexes": self.sync_index_daily(index_start, end_value),
            "weights": None,
            "review_documents": None,
        }
        if download_review_documents:
            result["review_documents"] = self.download_official_review_documents(review_cache_dir)
        if weight_manifest_path:
            result["weights"] = self.import_weight_snapshot_manifest(weight_manifest_path)
        return result

    @staticmethod
    def _max_date(table: str) -> Optional[date]:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            row = connection.execute(f'SELECT MAX(trade_date) FROM "{table}"').fetchone()
            return row[0] if row else None
        finally:
            connection.close()


def normalize_hk_symbol(value) -> Optional[str]:
    text = str(value or "").strip().upper().replace(".HK", "")
    if not text or not text.isdigit():
        return None
    return f"{int(text):05d}.HK"


def sync_hk_stock_base_data(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    max_market_days: int = 1,
    weight_manifest_path: Optional[str] = None,
    download_review_documents: bool = False,
    review_cache_dir: Optional[str] = None,
) -> Dict:
    service = HKStockBaseDataSyncService()
    return service.sync_incremental(
        start_date=start_date,
        end_date=end_date,
        max_market_days=max_market_days,
        weight_manifest_path=weight_manifest_path,
        download_review_documents=download_review_documents,
        review_cache_dir=review_cache_dir,
    )
