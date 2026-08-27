import logging
import math
import os
import hashlib
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import tushare as ts

from .quote import QuoteObserver, QuoteProvider
from .tushare_account import get_tushare_token_for_runtime


TUSHARE_INCOME_MAX_REQUESTS_PER_MINUTE = max(
    0,
    int(os.getenv("TUSHARE_INCOME_MAX_REQUESTS_PER_MINUTE", "450")),
)
TUSHARE_OPTION_DAILY_MAX_REQUESTS_PER_MINUTE = max(
    0,
    int(os.getenv("TUSHARE_OPTION_DAILY_MAX_REQUESTS_PER_MINUTE", "420")),
)
TUSHARE_REPO_DAILY_MAX_REQUESTS_PER_MINUTE = max(
    0,
    int(os.getenv("TUSHARE_REPO_DAILY_MAX_REQUESTS_PER_MINUTE", "420")),
)
TUSHARE_INDEX_WEIGHT_MAX_REQUESTS_PER_MINUTE = 420
TUSHARE_FUND_DAILY_MAX_REQUESTS_PER_MINUTE = 420
TUSHARE_REPORT_RC_MAX_REQUESTS_PER_MINUTE = max(
    0,
    int(os.getenv("TUSHARE_REPORT_RC_MAX_REQUESTS_PER_MINUTE", "120")),
)
TUSHARE_MAJOR_NEWS_MAX_REQUESTS_PER_HOUR = max(
    1,
    int(os.getenv("TUSHARE_MAJOR_NEWS_MAX_REQUESTS_PER_HOUR", "27")),
)
TUSHARE_RT_K_MAX_REQUESTS_PER_MINUTE = max(
    0,
    int(os.getenv("TUSHARE_RT_K_MAX_REQUESTS_PER_MINUTE", "450")),
)
TUSHARE_THS_MEMBER_MAX_REQUESTS_PER_MINUTE = max(
    0,
    int(os.getenv("TUSHARE_THS_MEMBER_MAX_REQUESTS_PER_MINUTE", "420")),
)
TUSHARE_MINUTE_MAX_REQUESTS_PER_MINUTE = max(
    0,
    int(os.getenv("TUSHARE_MINUTE_MAX_REQUESTS_PER_MINUTE", "450")),
)

# 沪深 ETF 代码前缀（rt_k 对沪市 ETF 返回空，需走 rt_etf_k 接口）
TUSHARE_A_SHARE_ETF_PREFIXES = ("15", "50", "51", "52", "56", "58")

# Tushare publishes CSI-owned indexes with the .CSI suffix even when the
# application uses the familiar exchange-style symbol as its canonical key.
TUSHARE_INDEX_DAILY_CODE_ALIASES = {
    "000985.SH": "000985.CSI",
}


class TushareUnsupportedError(NotImplementedError):
    pass


class TushareRateLimitError(ValueError):
    """A provider quota was exhausted and retrying now cannot succeed."""
    pass


class _SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max(0, int(max_calls or 0))
        self.period_seconds = float(period_seconds)
        self._lock = threading.Lock()
        self._calls = deque()

    def wait(self):
        if self.max_calls <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period_seconds:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait_seconds = self.period_seconds - (now - self._calls[0])
            time.sleep(max(wait_seconds, 0.01))

    def try_acquire(self) -> tuple[bool, float]:
        """Reserve one quota slot without ever blocking the caller."""
        if self.max_calls <= 0:
            return True, 0.0
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.period_seconds:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True, 0.0
            return False, max(0.0, self.period_seconds - (now - self._calls[0]))


class TushareService(QuoteProvider):
    _instances = {}
    _income_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_INCOME_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _option_daily_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_OPTION_DAILY_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _repo_daily_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_REPO_DAILY_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _index_weight_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_INDEX_WEIGHT_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _fund_daily_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_FUND_DAILY_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _report_rc_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_REPORT_RC_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _major_news_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_MAJOR_NEWS_MAX_REQUESTS_PER_HOUR,
        3600.0,
    )
    _rt_k_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_RT_K_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _ths_member_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_THS_MEMBER_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )
    _minute_rate_limiter = _SlidingWindowRateLimiter(
        TUSHARE_MINUTE_MAX_REQUESTS_PER_MINUTE,
        60.0,
    )

    def __init__(self, token: Optional[str] = None):
        self.token = (token or get_tushare_token_for_runtime() or "").strip()
        if not self.token:
            raise ValueError("Tushare token is required")
        self.logger = logging.getLogger("TushareService")
        ts.set_token(self.token)
        self.pro = ts.pro_api(self.token)
        self._stock_basic_frame: Optional[pd.DataFrame] = None
        self._fund_basic_frame: Optional[pd.DataFrame] = None
        self._etf_basic_frames: Dict[str, pd.DataFrame] = {}
        self._daily_basic_frame: Optional[pd.DataFrame] = None
        self._fund_share_frame: Optional[pd.DataFrame] = None
        self._fina_indicator_cache: Dict[str, Dict] = {}
        self._income_frame_cache: Dict[str, pd.DataFrame] = {}

    @classmethod
    def get_instance(cls, token: Optional[str] = None):
        effective_token = (token or get_tushare_token_for_runtime() or "").strip()
        cache_key = hashlib.sha256(effective_token.encode("utf-8")).hexdigest() if effective_token else ""
        if cache_key not in cls._instances:
            cls._instances[cache_key] = cls(effective_token)
        return cls._instances[cache_key]

    @classmethod
    def clear_cached_instances(cls) -> None:
        cls._instances.clear()

    @classmethod
    def getInstance(cls, token: Optional[str] = None):
        return cls.get_instance(token=token)

    @staticmethod
    def _unsupported(operation: str):
        raise TushareUnsupportedError(f"TushareService does not support {operation}")

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        raw = (symbol or "").strip().upper()
        if not raw:
            return raw
        if "." in raw:
            left, right = raw.split(".", 1)
            if left in {"SH", "SZ", "BJ"} and right:
                return f"{right}.{left}"
            return raw
        if len(raw) > 2 and raw[:2] in {"SH", "SZ", "BJ"} and raw[2:]:
            return f"{raw[2:]}.{raw[:2]}"
        return raw

    @classmethod
    def _strip_exchange(cls, symbol: str) -> str:
        normalized = cls.normalize_symbol(symbol)
        return normalized.split(".", 1)[0] if "." in normalized else normalized

    @staticmethod
    def _infer_symbol_from_code(code: str) -> str:
        code = (code or "").strip().upper()
        if not code:
            return code
        if code.startswith(("50", "51", "52", "53", "56", "58", "59", "60", "68", "90")):
            return f"{code}.SH"
        if code.startswith(("00", "01", "02", "03", "15", "16", "18", "30", "39")):
            return f"{code}.SZ"
        if code.startswith(("4", "8", "920")):
            return f"{code}.BJ"
        return code

    @staticmethod
    def is_cn_equity_symbol(symbol: str) -> bool:
        normalized = (symbol or "").strip().upper()
        if not normalized:
            return False
        if "." not in normalized:
            return False
        left, right = normalized.split(".", 1)
        return left in {"SH", "SZ", "BJ"} or right in {"SH", "SZ", "BJ"}

    @staticmethod
    def is_daily_period(period: Optional[str]) -> bool:
        normalized = str(period or "d").strip().lower()
        return normalized in {"d", "day", "daily"}

    @staticmethod
    def _to_date(value) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _to_float(value, default: Optional[float] = None) -> Optional[float]:
        if value is None or pd.isna(value):
            return default
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return default
        return numeric if math.isfinite(numeric) else default

    @staticmethod
    def _to_timestamp(date_value, time_value) -> Optional[datetime]:
        parsed_date = TushareService._to_date(date_value)
        if not parsed_date:
            return None

        text = str(time_value or "").strip()
        if not text:
            return datetime.combine(parsed_date, datetime.min.time())
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(text, fmt).time()
                return datetime.combine(parsed_date, parsed_time)
            except ValueError:
                continue
        return datetime.combine(parsed_date, datetime.min.time())

    @staticmethod
    def _row_to_dict(row) -> Dict:
        if row is None:
            return {}
        if hasattr(row, "to_dict"):
            row = row.to_dict()
        return {str(key).lower(): value for key, value in dict(row).items()}

    def _quote_from_realtime_row(self, symbol: str, row, volume_scale: float) -> Optional[Dict]:
        data = self._row_to_dict(row)
        price = self._to_float(data.get("price"))
        prev_close = self._to_float(data.get("pre_close"))
        if price is None or price <= 0:
            return None

        normalized_symbol = self.normalize_symbol(symbol or data.get("ts_code") or "")
        code = self._strip_exchange(normalized_symbol)
        change = price - prev_close if prev_close and prev_close > 0 else 0.0
        percent_change = (change / prev_close * 100) if prev_close and prev_close > 0 else 0.0
        volume = self._to_float(data.get("volume"), 0.0) or 0.0
        turnover = self._to_float(data.get("amount"), 0.0) or 0.0

        return {
            "symbol": normalized_symbol,
            "code": code,
            "name": data.get("name"),
            "name_cn": data.get("name"),
            "price": price,
            "change": change,
            "percent_change": percent_change,
            "high": self._to_float(data.get("high"), price),
            "low": self._to_float(data.get("low"), price),
            "open": self._to_float(data.get("open"), price),
            "prev_close": prev_close,
            "volume": int(round(volume * volume_scale)),
            "turnover": turnover,
            "timestamp": self._to_timestamp(data.get("date"), data.get("time")),
        }

    def _infer_asset(self, symbol: str) -> str:
        code = self.normalize_symbol(symbol).split(".", 1)[0]
        fund_prefixes = {"15", "16", "18", "50", "51", "52", "53", "56", "58", "59"}
        if any(code.startswith(prefix) for prefix in fund_prefixes):
            return "FD"
        return "E"

    def _load_stock_basic_frame(self) -> pd.DataFrame:
        if self._stock_basic_frame is None:
            try:
                self._stock_basic_frame = self.pro.stock_basic(
                    fields="ts_code,symbol,name,area,industry,market,exchange,list_date,delist_date,list_status"
                )
            except Exception as exc:
                self.logger.warning("Tushare stock_basic fetch failed: %s", exc)
                self._stock_basic_frame = pd.DataFrame()
        return self._stock_basic_frame

    def get_trade_calendar_frame(self, start_date: date, end_date: date, exchange: str = "SSE") -> pd.DataFrame:
        """获取交易日历。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        frames = []
        chunk_start = start_value
        while chunk_start <= end_value:
            chunk_end = min(chunk_start + timedelta(days=370), end_value)
            try:
                frame = self.pro.trade_cal(
                    exchange=exchange,
                    start_date=chunk_start.strftime("%Y%m%d"),
                    end_date=chunk_end.strftime("%Y%m%d"),
                    fields="cal_date,is_open,pretrade_date",
                )
            except Exception as exc:
                self.logger.warning("Tushare trade_cal fetch failed for %s~%s: %s", chunk_start, chunk_end, exc)
                chunk_start = chunk_end + timedelta(days=1)
                continue
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                frames.append(frame)
            chunk_start = chunk_end + timedelta(days=1)

        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["cal_date"], keep="last")
        result["cal_date"] = pd.to_datetime(result["cal_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["cal_date"]).sort_values("cal_date")

    def get_a_stock_basic_frame(self, list_statuses: Optional[List[str]] = None) -> pd.DataFrame:
        """获取A股公司基础信息，默认包含上市与退市股票。"""
        statuses = list_statuses or ["L", "D"]
        frames = []
        for status in statuses:
            try:
                frame = self.pro.stock_basic(
                    exchange="",
                    list_status=status,
                    fields="ts_code,symbol,name,area,industry,market,exchange,list_date,delist_date,list_status",
                )
            except Exception as exc:
                self.logger.warning("Tushare stock_basic fetch failed for status %s: %s", status, exc)
                continue
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        for column in ("list_date", "delist_date"):
            if column in result.columns:
                result[column] = pd.to_datetime(result[column], format="%Y%m%d", errors="coerce").dt.date
        return result.drop_duplicates(subset=["ts_code"], keep="first")

    def get_a_stock_name_changes_frame(self, start_date: date, end_date: date) -> pd.DataFrame:
        """获取A股名称/ST变更记录。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()
        try:
            frame = self.pro.namechange(
                start_date=start_value.strftime("%Y%m%d"),
                end_date=end_value.strftime("%Y%m%d"),
                fields="ts_code,name,start_date,end_date,change_reason",
                limit=10000,
                offset=0,
            )
        except Exception as exc:
            self.logger.warning("Tushare namechange fetch failed: %s", exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        frame = frame.copy()
        for column in ("start_date", "end_date"):
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column], format="%Y%m%d", errors="coerce").dt.date
        return frame.drop_duplicates()

    def get_a_stock_news_frame(
        self,
        start_at: datetime,
        end_at: datetime,
        sources: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Fetch every available short-news title in a time range.

        ``news`` documents a 1,500-row response cap but not offset pagination.
        Split saturated windows instead of relying on undocumented parameters.
        """
        if not start_at or not end_at or start_at >= end_at:
            return pd.DataFrame()
        frames = []
        for source in sources or ["sina", "wallstreetcn", "10jqka", "eastmoney", "yuncaijing", "fenghuang", "jinrongjie", "cls", "yicai"]:
            source_frames = []

            def fetch_window(window_start: datetime, window_end: datetime):
                try:
                    frame = self.pro.news(
                        src=source,
                        start_date=window_start.strftime("%Y-%m-%d %H:%M:%S"),
                        end_date=window_end.strftime("%Y-%m-%d %H:%M:%S"),
                        fields="datetime,title,channels",
                    )
                except Exception as exc:
                    self.logger.warning("Tushare news fetch failed for %s: %s", source, exc)
                    return
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    return
                if len(frame) >= 1500 and (window_end - window_start).total_seconds() > 1:
                    midpoint = window_start + (window_end - window_start) / 2
                    fetch_window(window_start, midpoint)
                    fetch_window(midpoint, window_end)
                    return
                snapshot = frame.copy()
                snapshot["source"] = source
                snapshot["news_kind"] = "news"
                source_frames.append(snapshot)

            fetch_window(start_at, end_at)
            frames.extend(source_frames)
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        if "datetime" in result.columns:
            result["datetime"] = pd.to_datetime(result["datetime"], errors="coerce")
        return result.drop_duplicates(subset=["source", "datetime", "title"], keep="last")

    def get_a_stock_major_news_frame(
        self,
        start_at: datetime,
        end_at: datetime,
        sources: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Fetch every available long-news *title* without sending article bodies.

        ``src`` is intentionally omitted so that Tushare returns all sources in one
        call (capped at 800 items).  The time window is split only when the cap is
        hit; per-source iteration is replaced by an optional post-fetch filter.
        """
        if not start_at or not end_at or start_at >= end_at:
            return pd.DataFrame()
        frames: List[pd.DataFrame] = []

        def fetch_window(window_start: datetime, window_end: datetime):
            acquired, retry_after = self._major_news_rate_limiter.try_acquire()
            if not acquired:
                retry_minutes = max(1, math.ceil(retry_after / 60.0))
                raise TushareRateLimitError(
                    "Tushare major_news 本地小时级限流已达到上限；"
                    f"请约 {retry_minutes} 分钟后再试"
                )
            try:
                frame = self.pro.major_news(
                    start_date=window_start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    fields="title,pub_time,src",
                )
            except Exception as exc:
                if "频率超限" in str(exc):
                    raise TushareRateLimitError(
                        f"Tushare major_news 频率超限：{exc}"
                    ) from exc
                self.logger.warning("Tushare major_news fetch failed: %s", exc)
                return
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                return
            # Tushare caps major_news at 800 items; split when saturated.
            if len(frame) >= 800 and (window_end - window_start).total_seconds() > 1:
                midpoint = window_start + (window_end - window_start) / 2
                fetch_window(window_start, midpoint)
                fetch_window(midpoint, window_end)
                return
            snapshot = frame.copy()
            snapshot["datetime"] = pd.to_datetime(snapshot.get("pub_time"), errors="coerce")
            snapshot["source"] = snapshot.get("src", "")
            snapshot["news_kind"] = "major_news"
            frames.append(snapshot)

        fetch_window(start_at, end_at)
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        if sources:
            result = result[result["source"].isin(sources)]
            if result.empty:
                return pd.DataFrame()
        return result.drop_duplicates(subset=["source", "datetime", "title"], keep="last")

    def get_ths_index_frame(self, index_type: str) -> pd.DataFrame:
        normalized_type = str(index_type or "").strip().upper()
        if normalized_type not in {"N", "TH", "I"}:
            raise ValueError("THS 板块类型必须是 N、TH 或 I")
        try:
            frame = self.pro.ths_index(
                exchange="A",
                type=normalized_type,
                fields="ts_code,name,count,exchange,list_date,type",
            )
        except Exception as exc:
            self.logger.warning("Tushare ths_index fetch failed for %s: %s", normalized_type, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["list_date"] = pd.to_datetime(result.get("list_date"), format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "name"])

    def get_ths_member_frame(self, ths_code: str) -> pd.DataFrame:
        try:
            self._ths_member_rate_limiter.wait()
            frame = self.pro.ths_member(
                ts_code=str(ths_code or "").upper(),
                fields="ts_code,con_code,con_name,weight,in_date,out_date,is_new",
            )
        except Exception as exc:
            self.logger.warning("Tushare ths_member fetch failed for %s: %s", ths_code, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        return frame.dropna(subset=["ts_code", "con_code", "con_name"])

    def get_ths_daily_frame(self, trade_date: date) -> pd.DataFrame:
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()
        try:
            frame = self.pro.ths_daily(
                trade_date=trade_value.strftime("%Y%m%d"),
                fields="ts_code,trade_date,open,close,high,low,pre_close,avg_price,change,pct_change,vol,turnover_rate,total_mv,float_mv",
            )
        except Exception as exc:
            self.logger.warning("Tushare ths_daily fetch failed for %s: %s", trade_value, exc)
            return pd.DataFrame()
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()

    def get_ths_moneyflow_frame(self, trade_date: date) -> pd.DataFrame:
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()
        try:
            frame = self.pro.moneyflow_cnt_ths(
                trade_date=trade_value.strftime("%Y%m%d"),
                fields="trade_date,ts_code,name,lead_stock,close_price,pct_change,industry_index,company_num,pct_change_stock,net_buy_amount,net_sell_amount,net_amount",
            )
        except Exception as exc:
            self.logger.warning("Tushare moneyflow_cnt_ths fetch failed for %s: %s", trade_value, exc)
            return pd.DataFrame()
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()

    def get_a_stock_realtime_minute_frame(self, ts_code: str, freq: str = "1MIN") -> pd.DataFrame:
        """Return Tushare intraday accumulated minute bars for one A-share."""
        symbol = self.normalize_symbol(ts_code)
        if not self.is_cn_equity_symbol(symbol):
            return pd.DataFrame()
        normalized_freq = str(freq or "1MIN").upper()
        if normalized_freq not in {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}:
            raise ValueError("分钟频率必须为 1MIN、5MIN、15MIN、30MIN 或 60MIN")
        self._minute_rate_limiter.wait()
        try:
            frame = self.pro.rt_min_daily(
                ts_code=symbol,
                freq=normalized_freq,
                fields="ts_code,freq,time,open,close,high,low,vol,amount",
            )
        except Exception as exc:
            self.logger.warning("Tushare rt_min_daily fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["time"] = pd.to_datetime(result["time"], errors="coerce")
        for column in ("open", "close", "high", "low", "vol", "amount"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce")
        return result.dropna(subset=["time", "close"]).sort_values("time")

    def get_a_stock_historical_minute_frame(
        self,
        ts_code: str,
        start_time: datetime,
        end_time: datetime,
        freq: str = "1min",
    ) -> pd.DataFrame:
        """Return historical A-share minute bars from the privileged stk_mins API."""
        symbol = self.normalize_symbol(ts_code)
        if not self.is_cn_equity_symbol(symbol):
            return pd.DataFrame()
        try:
            return self.get_a_stock_historical_minute_batch_frame(
                [symbol],
                start_time,
                end_time,
                freq=freq,
                raise_on_error=True,
            )
        except Exception as exc:
            self.logger.warning("Tushare stk_mins fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame()

    def get_a_stock_historical_minute_batch_frame(
        self,
        ts_codes: List[str],
        start_time: datetime,
        end_time: datetime,
        freq: str = "1min",
        *,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """Return historical minute bars for comma-separated A-share symbols."""
        symbols = [self.normalize_symbol(item) for item in ts_codes]
        symbols = list(dict.fromkeys(item for item in symbols if self.is_cn_equity_symbol(item)))
        if not symbols:
            return pd.DataFrame()
        normalized_freq = str(freq or "1min").lower()
        if normalized_freq not in {"1min", "5min", "15min", "30min", "60min"}:
            raise ValueError("分钟频率必须为 1min、5min、15min、30min 或 60min")
        self._minute_rate_limiter.wait()
        try:
            frame = self.pro.stk_mins(
                ts_code=",".join(symbols),
                freq=normalized_freq,
                start_date=start_time.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end_time.strftime("%Y-%m-%d %H:%M:%S"),
                fields="ts_code,trade_time,open,close,high,low,vol,amount",
            )
        except Exception as exc:
            if raise_on_error:
                raise
            self.logger.warning("Tushare stk_mins batch fetch failed for %s: %s", symbols, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["ts_code"] = result["ts_code"].astype(str).str.strip().str.upper()
        result["trade_time"] = pd.to_datetime(result["trade_time"], errors="coerce")
        for column in ("open", "close", "high", "low", "vol", "amount"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result = result[result["ts_code"].isin(symbols)]
        return result.dropna(subset=["trade_time", "open", "close", "high", "low"]).sort_values(
            ["ts_code", "trade_time"]
        )

    def get_a_stock_realtime_minute_batch_frame(self, ts_codes: List[str], freq: str = "1MIN") -> pd.DataFrame:
        """Fetch the latest realtime minute bar for up to 300 A-share symbols."""
        symbols = [self.normalize_symbol(item) for item in ts_codes]
        symbols = list(dict.fromkeys(item for item in symbols if self.is_cn_equity_symbol(item)))
        if not symbols:
            return pd.DataFrame()
        if len(symbols) > 300:
            raise ValueError("实时分钟单批最多300只股票")
        normalized_freq = str(freq or "1MIN").upper()
        if normalized_freq not in {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}:
            raise ValueError("分钟频率必须为 1MIN、5MIN、15MIN、30MIN 或 60MIN")
        self._minute_rate_limiter.wait()
        try:
            frame = self.pro.rt_min(
                ts_code=",".join(symbols),
                freq=normalized_freq,
                fields="ts_code,time,open,close,high,low,vol,amount",
            )
        except Exception as exc:
            self.logger.warning("Tushare rt_min batch fetch failed: %s", exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.rename(columns={"code": "ts_code", "time": "trade_time"}).copy()
        result["trade_time"] = pd.to_datetime(result["trade_time"], errors="coerce")
        for column in ("open", "close", "high", "low", "vol", "amount"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
        return result.dropna(subset=["ts_code", "trade_time", "open", "close", "high", "low"])

    def get_a_stock_realtime_index_frame(
        self, ts_codes, fields: Optional[str] = None
    ) -> pd.DataFrame:
        """Return Tushare rt_idx_k realtime daily bars for one or more exchange indexes.

        rt_idx_k (指数实时日线) is the economical choice: up to 50 calls/minute,
        and a single call can fetch the whole market (wildcard) or many
        comma-separated codes. Output includes close (现价), pre_close (昨收),
        high/open/low, vol and amount.
        """
        if isinstance(ts_codes, str):
            codes = [item for item in ts_codes.split(",") if item.strip()]
        else:
            codes = [item for item in (ts_codes or []) if item]
        codes = [self.normalize_symbol(str(code).strip()) for code in codes]
        if not codes:
            return pd.DataFrame()
        field_text = (
            fields
            or "ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount"
        )
        try:
            frame = self.pro.rt_idx_k(ts_code=",".join(codes), fields=field_text)
        except Exception as exc:
            self.logger.warning("Tushare rt_idx_k failed for %s: %s", codes, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["ts_code"] = result["ts_code"].astype(str).str.strip().str.upper()
        result["trade_time"] = pd.to_datetime(result["trade_time"], errors="coerce")
        for column in ("close", "pre_close", "high", "open", "low", "vol", "amount"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce")
        return result.dropna(subset=["close"]).sort_values("ts_code")

    @staticmethod
    def _is_a_share_etf_ts_code(ts_code: str) -> bool:
        normalized = str(ts_code or "").strip().upper()
        if "." not in normalized:
            return False
        code, market = normalized.split(".", 1)
        return (
            market in {"SH", "SZ"}
            and code.isdigit()
            and len(code) == 6
            and code.startswith(TUSHARE_A_SHARE_ETF_PREFIXES)
        )

    def get_a_stock_realtime_rt_k_frame(self, ts_codes) -> pd.DataFrame:
        """A股实时日线（rt_k），用于非 ETF 股票。

        休市/未开盘时返回的是上一交易日数据，调用方必须用 trade_time 过滤
        （只接受 trade_time 日期为当日的行）才能当作实时价。
        """
        if isinstance(ts_codes, str):
            codes = [item for item in ts_codes.split(",") if item.strip()]
        else:
            codes = [item for item in (ts_codes or []) if item]
        normalized = [self.normalize_symbol(str(code).strip()) for code in codes]
        normalized = [
            code for code in normalized
            if code and code.endswith((".SH", ".SZ")) and not self._is_a_share_etf_ts_code(code)
        ]
        if not normalized:
            return pd.DataFrame()
        try:
            self._rt_k_rate_limiter.wait()
            frame = self.pro.rt_k(
                ts_code=",".join(normalized),
                fields="ts_code,close,pre_close,open,high,low,vol,amount,num,trade_time,"
                "ask_price1,ask_volume1,bid_price1,bid_volume1",
            )
        except Exception as exc:
            self.logger.warning("Tushare rt_k failed for %s: %s", normalized, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["ts_code"] = result["ts_code"].astype(str).str.strip().str.upper()
        if "trade_time" in result.columns:
            result["trade_time"] = pd.to_datetime(result["trade_time"], errors="coerce")
        for column in ("close", "pre_close", "open", "high", "low", "vol", "amount", "num"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce")
        return result.dropna(subset=["close"]).sort_values("ts_code")

    def get_a_stock_realtime_etf_rt_k_frame(self, ts_codes) -> pd.DataFrame:
        """ETF实时日线（rt_etf_k），覆盖 rt_k 查不到的沪市 ETF。

        沪市代码必须带 topic="HQ_FND_TICK"，深市代码不能带（带 topic 时深市被过滤），
        因此沪/深分组两次调用后合并。休市/未开盘时同样返回上一交易日数据，
        调用方必须用 trade_time 过滤。
        """
        if isinstance(ts_codes, str):
            codes = [item for item in ts_codes.split(",") if item.strip()]
        else:
            codes = [item for item in (ts_codes or []) if item]
        normalized = [self.normalize_symbol(str(code).strip()) for code in codes]
        normalized = [code for code in normalized if self._is_a_share_etf_ts_code(code)]
        if not normalized:
            return pd.DataFrame()
        sh_codes = [code for code in normalized if code.endswith(".SH")]
        sz_codes = [code for code in normalized if code.endswith(".SZ")]
        fields = (
            "ts_code,close,pre_close,open,high,low,vol,amount,num,trade_time,"
            "ask_price1,ask_volume1,bid_price1,bid_volume1"
        )
        frames = []
        try:
            if sh_codes:
                self._rt_k_rate_limiter.wait()
                frame = self.pro.rt_etf_k(ts_code=",".join(sh_codes), topic="HQ_FND_TICK", fields=fields)
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    frames.append(frame)
            if sz_codes:
                self._rt_k_rate_limiter.wait()
                frame = self.pro.rt_etf_k(ts_code=",".join(sz_codes), fields=fields)
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    frames.append(frame)
        except Exception as exc:
            self.logger.warning("Tushare rt_etf_k failed for %s: %s", normalized, exc)
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        result["ts_code"] = result["ts_code"].astype(str).str.strip().str.upper()
        if "trade_time" in result.columns:
            result["trade_time"] = pd.to_datetime(result["trade_time"], errors="coerce")
        for column in ("close", "pre_close", "open", "high", "low", "vol", "amount", "num"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce")
        return result.dropna(subset=["close"]).sort_values("ts_code")

    def get_a_stock_limit_concepts_frame(self, trade_date: date) -> pd.DataFrame:
        """Fetch the daily strongest limit-up concepts for AI candidate discovery."""
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()
        try:
            frame = self.pro.limit_cpt_list(
                trade_date=trade_value.strftime("%Y%m%d"),
                fields="ts_code,name,trade_date,days,up_stat,cons_nums,up_nums,pct_chg,rank",
            )
        except Exception as exc:
            self.logger.warning("Tushare limit_cpt_list fetch failed for %s: %s", trade_value, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "name"])

    def get_a_stock_concept_components_frame(
        self,
        trade_date: date,
        theme_code: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetch KPL concept constituents and their inclusion reason."""
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()
        kwargs = {
            "trade_date": trade_value.strftime("%Y%m%d"),
            "fields": "ts_code,trade_date,name,theme_code,industry_code,industry,reason,hot_num",
        }
        if theme_code:
            kwargs["theme_code"] = str(theme_code).strip().upper()
        try:
            frame = self.pro.dc_concept_cons(**kwargs)
        except Exception as exc:
            self.logger.warning("Tushare dc_concept_cons fetch failed for %s: %s", theme_code or "all", exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "name"])

    def get_a_stock_report_rc_range_frame(
        self,
        start_date: date,
        end_date: date,
        ts_code: Optional[str] = None,
        limit: int = 3000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """分页获取A股券商卖方研报盈利预测、评级和目标价明细。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        symbol = self.normalize_symbol(ts_code) if ts_code else None
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        fields = (
            "ts_code,name,report_date,report_title,report_type,classify,org_name,"
            "author_name,quarter,op_rt,op_pr,tp,np,eps,pe,rd,roe,ev_ebitda,"
            "rating,max_price,min_price,imp_dg,create_time"
        )
        frames = []
        offset = 0
        limit = max(1, int(limit or 3000))
        first_error = None
        while True:
            try:
                self._report_rc_rate_limiter.wait()
                kwargs = {
                    "start_date": start_value.strftime("%Y%m%d"),
                    "end_date": end_value.strftime("%Y%m%d"),
                    "fields": fields,
                    "limit": limit,
                    "offset": offset,
                }
                if symbol:
                    kwargs["ts_code"] = symbol
                frame = self.pro.report_rc(**kwargs)
            except Exception as exc:
                first_error = exc
                self.logger.warning(
                    "Tushare report_rc fetch failed for %s %s~%s offset=%s: %s",
                    symbol or "ALL",
                    start_value,
                    end_value,
                    offset,
                    exc,
                )
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            if raise_on_error and first_error is not None:
                raise first_error
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=[
                "ts_code",
                "report_date",
                "org_name",
                "author_name",
                "report_title",
                "quarter",
                "report_type",
                "classify",
            ],
            keep="last",
        )
        if "report_date" in result.columns:
            result["report_date"] = pd.to_datetime(result["report_date"], format="%Y%m%d", errors="coerce").dt.date
        if "create_time" in result.columns:
            result["create_time"] = pd.to_datetime(result["create_time"], errors="coerce")
        for column in ("op_rt", "op_pr", "tp", "np", "eps", "pe", "rd", "roe", "ev_ebitda", "max_price", "min_price"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce")
        return result.dropna(subset=["ts_code", "report_date"]).sort_values(["report_date", "ts_code"])

    def get_a_stock_daily_frame(self, trade_date: date) -> pd.DataFrame:
        """获取某交易日A股全市场价格截面。"""
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()
        try:
            frame = self.pro.daily(
                trade_date=trade_value.strftime("%Y%m%d"),
                fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
            )
        except Exception as exc:
            self.logger.warning("Tushare daily fetch failed for %s: %s", trade_value, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        frame = frame.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return frame.dropna(subset=["ts_code", "trade_date"])

    def get_a_stock_daily_range_frame(self, start_date: date, end_date: date, limit: int = 6000) -> pd.DataFrame:
        """分页获取一段时间内A股全市场价格截面。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()
        frames = []
        offset = 0
        while True:
            try:
                frame = self.pro.daily(
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning("Tushare daily range fetch failed for %s~%s offset=%s: %s", start_value, end_value, offset, exc)
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "trade_date"])

    def get_a_stock_daily_basic_frame(self, trade_date: date) -> pd.DataFrame:
        """获取某交易日A股全市场估值/股本截面。"""
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()
        try:
            frame = self.pro.daily_basic(
                trade_date=trade_value.strftime("%Y%m%d"),
                fields="ts_code,trade_date,total_mv,circ_mv,float_share,total_share,turnover_rate",
            )
        except Exception as exc:
            self.logger.warning("Tushare daily_basic fetch failed for %s: %s", trade_value, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        frame = frame.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return frame.dropna(subset=["ts_code", "trade_date"])

    def get_a_stock_daily_basic_range_frame(self, start_date: date, end_date: date, limit: int = 6000) -> pd.DataFrame:
        """分页获取一段时间内A股全市场估值/股本截面。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()
        frames = []
        offset = 0
        while True:
            try:
                frame = self.pro.daily_basic(
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields="ts_code,trade_date,total_mv,circ_mv,float_share,total_share,turnover_rate",
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning("Tushare daily_basic range fetch failed for %s~%s offset=%s: %s", start_value, end_value, offset, exc)
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "trade_date"])

    @staticmethod
    def _merge_daily_and_basic_frame(daily: pd.DataFrame, daily_basic: pd.DataFrame) -> pd.DataFrame:
        if daily is None or daily.empty:
            return pd.DataFrame()
        if daily_basic is None or daily_basic.empty:
            return daily
        return daily.merge(
            daily_basic.drop(columns=["trade_date"], errors="ignore"),
            on="ts_code",
            how="left",
        )

    def _fetch_daily_and_basic_parallel(self, trade_date: date) -> tuple:
        with ThreadPoolExecutor(max_workers=2) as executor:
            daily_future = executor.submit(self.get_a_stock_daily_frame, trade_date)
            basic_future = executor.submit(self.get_a_stock_daily_basic_frame, trade_date)
            daily = daily_future.result()
            daily_basic = basic_future.result()
        return daily, daily_basic

    def _fetch_daily_and_basic_range_parallel(self, start_date: date, end_date: date) -> tuple:
        with ThreadPoolExecutor(max_workers=2) as executor:
            daily_future = executor.submit(self.get_a_stock_daily_range_frame, start_date, end_date)
            basic_future = executor.submit(self.get_a_stock_daily_basic_range_frame, start_date, end_date)
            daily = daily_future.result()
            daily_basic = basic_future.result()
        return daily, daily_basic

    def get_a_stock_bak_daily_range_frame(self, start_date: date, end_date: date, limit: int = 7000) -> pd.DataFrame:
        """分页获取bak_daily截面；仅用于日线主接口失败时兜底。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()
        frames = []
        offset = 0
        fields = (
            "ts_code,trade_date,open,high,low,close,pre_close,change,pct_change,"
            "vol,amount,total_share,float_share,total_mv,float_mv"
        )
        while True:
            try:
                kwargs = {
                    "start_date": start_value.strftime("%Y%m%d"),
                    "end_date": end_value.strftime("%Y%m%d"),
                    "fields": fields,
                    "offset": offset,
                }
                # bak_daily显式传limit时服务端会压到6000；不传limit时当前可返回7000条。
                if limit < 7000:
                    kwargs["limit"] = limit
                frame = self.pro.bak_daily(**kwargs)
            except Exception as exc:
                self.logger.warning("Tushare bak_daily range fetch failed for %s~%s offset=%s: %s", start_value, end_value, offset, exc)
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit
        if not frames:
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        result = result.dropna(subset=["ts_code", "trade_date"])
        if result.empty:
            return result

        result = result.rename(columns={"pct_change": "pct_chg", "float_mv": "circ_mv"})
        # bak_daily: amount单位为万元，市值单位为亿元；项目缓存沿用daily/daily_basic的千元/万元。
        if "amount" in result.columns:
            result["amount"] = pd.to_numeric(result["amount"], errors="coerce") * 10.0
        for column in ("total_mv", "circ_mv"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce") * 10000.0
        return result

    def get_a_stock_market_daily_frame(self, trade_date: date) -> pd.DataFrame:
        """合并价格与估值截面，优先使用 daily + daily_basic 并行结果。"""
        daily, daily_basic = self._fetch_daily_and_basic_parallel(trade_date)
        merged = self._merge_daily_and_basic_frame(daily, daily_basic)
        if not merged.empty:
            return merged

        bak_daily = self.get_a_stock_bak_daily_range_frame(trade_date, trade_date)
        if not bak_daily.empty:
            self.logger.warning("Falling back to bak_daily for %s because daily/daily_basic were unavailable", trade_date)
        return bak_daily

    def get_a_stock_market_daily_range_frame(self, start_date: date, end_date: date) -> pd.DataFrame:
        """合并一段时间内的价格与估值截面，优先使用 daily + daily_basic 并行结果。"""
        daily, daily_basic = self._fetch_daily_and_basic_range_parallel(start_date, end_date)
        if not daily.empty:
            if daily_basic.empty:
                return daily
            merged = daily.merge(
                daily_basic,
                on=["ts_code", "trade_date"],
                how="left",
            )
            if not merged.empty:
                return merged

        bak_daily = self.get_a_stock_bak_daily_range_frame(start_date, end_date)
        if not bak_daily.empty:
            self.logger.warning(
                "Falling back to bak_daily for %s~%s because daily/daily_basic were unavailable",
                start_date,
                end_date,
            )
        return bak_daily

    def get_a_stock_adj_factor_range_frame(
        self,
        start_date: date,
        end_date: date,
        ts_code: Optional[str] = None,
        limit: int = 6000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """分页获取A股股票复权因子。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        symbol = self.normalize_symbol(ts_code) if ts_code else None
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        frames = []
        offset = 0
        limit = max(1, int(limit or 6000))
        first_error = None
        fields = "ts_code,trade_date,adj_factor"
        while True:
            try:
                kwargs = {
                    "start_date": start_value.strftime("%Y%m%d"),
                    "end_date": end_value.strftime("%Y%m%d"),
                    "fields": fields,
                    "limit": limit,
                    "offset": offset,
                }
                if symbol:
                    kwargs["ts_code"] = symbol
                frame = self.pro.adj_factor(**kwargs)
            except Exception as exc:
                first_error = exc
                self.logger.warning(
                    "Tushare adj_factor fetch failed for %s %s~%s offset=%s: %s",
                    symbol or "ALL",
                    start_value,
                    end_value,
                    offset,
                    exc,
                )
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            if raise_on_error and first_error is not None:
                raise first_error
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        if "adj_factor" in result.columns:
            result["adj_factor"] = pd.to_numeric(result["adj_factor"], errors="coerce")
        return result.dropna(subset=["ts_code", "trade_date", "adj_factor"]).sort_values(["ts_code", "trade_date"])

    def get_index_daily_range_frame(self, ts_code: str, start_date: date, end_date: date, limit: int = 5000) -> pd.DataFrame:
        """分页获取A股指数日行情。"""
        index_code = self.normalize_symbol(ts_code)
        provider_code = TUSHARE_INDEX_DAILY_CODE_ALIASES.get(index_code, index_code)
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not index_code or not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        frames = []
        offset = 0
        while True:
            try:
                frame = self.pro.index_daily(
                    ts_code=provider_code,
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning("Tushare index_daily fetch failed for %s %s~%s offset=%s: %s", index_code, start_value, end_value, offset, exc)
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        if provider_code != index_code:
            result["ts_code"] = index_code
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "trade_date"]).sort_values("trade_date")

    def get_a_stock_fund_adj_factor_range_frame(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        limit: int = 5000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """分页获取A股ETF/场内基金复权因子。"""
        fund_code = self.normalize_symbol(ts_code)
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not fund_code or not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        frames = []
        offset = 0
        limit = max(1, int(limit or 5000))
        first_error = None
        fields = "ts_code,trade_date,adj_factor"
        while True:
            try:
                self._fund_daily_rate_limiter.wait()
                frame = self.pro.fund_adj(
                    ts_code=fund_code,
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                first_error = exc
                self.logger.warning(
                    "Tushare fund_adj fetch failed for %s %s~%s offset=%s: %s",
                    fund_code,
                    start_value,
                    end_value,
                    offset,
                    exc,
                )
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            if raise_on_error and first_error is not None:
                raise first_error
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        if "adj_factor" in result.columns:
            result["adj_factor"] = pd.to_numeric(result["adj_factor"], errors="coerce")
        return result.dropna(subset=["ts_code", "trade_date", "adj_factor"]).sort_values(["ts_code", "trade_date"])

    def get_a_stock_fund_adj_factor_trade_date_frame(
        self,
        trade_date: date,
        limit: int = 2000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """按交易日批量获取A股ETF/场内基金复权因子。"""
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()

        frames = []
        offset = 0
        limit = max(1, int(limit or 2000))
        first_error = None
        fields = "ts_code,trade_date,adj_factor"
        while True:
            try:
                self._fund_daily_rate_limiter.wait()
                frame = self.pro.fund_adj(
                    trade_date=trade_value.strftime("%Y%m%d"),
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                first_error = exc
                self.logger.warning(
                    "Tushare fund_adj fetch failed for trade_date=%s offset=%s: %s",
                    trade_value,
                    offset,
                    exc,
                )
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            if raise_on_error:
                if first_error is not None:
                    raise first_error
                raise RuntimeError(f"Tushare fund_adj returned no rows for trade_date={trade_value}")
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        if "adj_factor" in result.columns:
            result["adj_factor"] = pd.to_numeric(result["adj_factor"], errors="coerce")
        return result.dropna(subset=["ts_code", "trade_date", "adj_factor"]).sort_values(["ts_code", "trade_date"])

    def get_a_stock_fund_daily_trade_date_frame(
        self,
        trade_date: date,
        limit: int = 5000,
        raise_on_error: bool = False,
        raise_on_empty: bool = False,
    ) -> pd.DataFrame:
        """按交易日批量获取A股ETF/场内基金日行情。"""
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()

        frames = []
        offset = 0
        limit = max(1, int(limit or 5000))
        fields = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
        first_error = None
        while True:
            try:
                self._fund_daily_rate_limiter.wait()
                frame = self.pro.fund_daily(
                    trade_date=trade_value.strftime("%Y%m%d"),
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                first_error = exc
                self.logger.warning(
                    "Tushare fund_daily fetch failed for trade_date=%s offset=%s: %s",
                    trade_value,
                    offset,
                    exc,
                )
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            if first_error is not None:
                if raise_on_error:
                    raise first_error
                return pd.DataFrame()
            if raise_on_empty:
                raise RuntimeError(f"Tushare fund_daily returned no rows for trade_date={trade_value}")
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "trade_date"]).sort_values(["ts_code", "trade_date"])

    def get_a_stock_fund_daily_range_frame(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
        limit: int = 5000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """分页获取A股ETF/场内基金日行情。"""
        fund_code = self.normalize_symbol(ts_code)
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not fund_code or not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        frames = []
        offset = 0
        limit = max(1, int(limit or 5000))
        fields = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
        first_error = None
        while True:
            try:
                self._fund_daily_rate_limiter.wait()
                frame = self.pro.fund_daily(
                    ts_code=fund_code,
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                first_error = exc
                self.logger.warning("Tushare fund_daily fetch failed for %s %s~%s offset=%s: %s", fund_code, start_value, end_value, offset, exc)
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            try:
                self._fund_daily_rate_limiter.wait()
                frame = ts.pro_bar(
                    api=self.pro,
                    ts_code=fund_code,
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    freq="D",
                    asset="FD",
                    fields="ts_code,trade_date,open,high,low,close,vol,amount",
                )
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    frames.append(frame)
            except Exception as exc:
                self.logger.warning("Tushare pro_bar fund fetch failed for %s %s~%s: %s", fund_code, start_value, end_value, exc)
                if raise_on_error and first_error is None:
                    first_error = exc

        if not frames:
            if raise_on_error and first_error is not None:
                raise first_error
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "trade_date"]).sort_values("trade_date")

    def get_option_basic_frame(self, exchange: str) -> pd.DataFrame:
        """获取交易所 ETF 期权合约基础信息。"""
        exchange_value = str(exchange or "").strip().upper()
        if exchange_value not in {"SSE", "SZSE"}:
            return pd.DataFrame()
        fields = (
            "ts_code,exchange,name,per_unit,opt_code,opt_type,call_put,exercise_type,"
            "exercise_price,s_month,maturity_date,list_price,list_date,delist_date,"
            "last_edate,last_ddate,quote_unit,min_price_chg"
        )
        try:
            frame = self.pro.opt_basic(exchange=exchange_value, fields=fields)
        except Exception as exc:
            self.logger.warning("Tushare opt_basic fetch failed for %s: %s", exchange_value, exc)
            return pd.DataFrame()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        return frame.copy().drop_duplicates(subset=["ts_code"], keep="last")

    def get_option_daily_frame(self, trade_date: date, exchange: str) -> pd.DataFrame:
        """获取某交易所某交易日 ETF 期权行情。"""
        trade_value = self._to_date(trade_date)
        exchange_value = str(exchange or "").strip().upper()
        if not trade_value or exchange_value not in {"SSE", "SZSE"}:
            return pd.DataFrame()
        return self.get_option_daily_range_frame(trade_value, trade_value, exchange_value)

    def get_option_daily_range_frame(
        self,
        start_date: date,
        end_date: date,
        exchange: str,
        limit: int = 15000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """批量获取某交易所 ETF 期权日线行情。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        exchange_value = str(exchange or "").strip().upper()
        if not start_value or not end_value or start_value > end_value or exchange_value not in {"SSE", "SZSE"}:
            return pd.DataFrame()
        fields = (
            "ts_code,trade_date,exchange,pre_settle,pre_close,open,high,low,"
            "close,settle,vol,amount,oi"
        )
        frames = []
        offset = 0
        limit = max(1, int(limit or 15000))
        while True:
            try:
                self._option_daily_rate_limiter.wait()
                frame = self.pro.opt_daily(
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    exchange=exchange_value,
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning(
                    "Tushare opt_daily range fetch failed for %s %s~%s offset=%s: %s",
                    exchange_value,
                    start_value,
                    end_value,
                    offset,
                    exc,
                )
                if raise_on_error:
                    raise
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["ts_code", "trade_date"],
            keep="last",
        )
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "trade_date"]).sort_values("trade_date")

    def get_repo_daily_frame(self, trade_date: date) -> pd.DataFrame:
        """获取某交易日交易所债券回购行情。"""
        trade_value = self._to_date(trade_date)
        if not trade_value:
            return pd.DataFrame()
        return self.get_repo_daily_range_frame(trade_value, trade_value)

    def get_repo_daily_range_frame(
        self,
        start_date: date,
        end_date: date,
        limit: int = 2000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """批量获取交易所债券回购日行情。"""
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()
        fields = (
            "ts_code,trade_date,repo_maturity,pre_close,open,high,low,close,"
            "weight,weight_r,amount,num"
        )
        frames = []
        offset = 0
        limit = max(1, int(limit or 2000))
        while True:
            try:
                self._repo_daily_rate_limiter.wait()
                frame = self.pro.repo_daily(
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning(
                    "Tushare repo_daily range fetch failed for %s~%s offset=%s: %s",
                    start_value,
                    end_value,
                    offset,
                    exc,
                )
                if raise_on_error:
                    raise
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["ts_code", "trade_date"],
            keep="last",
        )
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["ts_code", "trade_date"]).sort_values("trade_date")

    def get_index_weight_range_frame(
        self,
        index_code: str,
        start_date: date,
        end_date: date,
        limit: int = 6000,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """分页获取指数历史成分权重。"""
        normalized_index_code = self.normalize_symbol(index_code)
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not normalized_index_code or not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        fields = "index_code,con_code,trade_date,weight"
        frames = []
        offset = 0
        limit = max(1, int(limit or 6000))
        while True:
            try:
                self._index_weight_rate_limiter.wait()
                frame = self.pro.index_weight(
                    index_code=normalized_index_code,
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning(
                    "Tushare index_weight fetch failed for %s %s~%s offset=%s: %s",
                    normalized_index_code,
                    start_value,
                    end_value,
                    offset,
                    exc,
                )
                if raise_on_error:
                    raise
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["index_code", "trade_date", "con_code"],
            keep="last",
        )
        result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d", errors="coerce").dt.date
        return result.dropna(subset=["index_code", "trade_date", "con_code"]).sort_values(["index_code", "trade_date", "con_code"])

    def _load_fund_basic_frame(self) -> pd.DataFrame:
        if self._fund_basic_frame is None:
            try:
                self._fund_basic_frame = self.pro.fund_basic(fields="ts_code,name,market,list_date")
            except Exception as exc:
                self.logger.warning("Tushare fund_basic fetch failed: %s", exc)
                self._fund_basic_frame = pd.DataFrame()
        return self._fund_basic_frame

    def get_a_stock_fund_basic_frame(self, symbols: Optional[List[str]] = None) -> pd.DataFrame:
        """获取A股ETF/场内基金基础信息。"""
        requested_symbols = list(
            dict.fromkeys(
                self.normalize_symbol(symbol)
                for symbol in (symbols or [])
                if symbol
            )
        )
        base_frame = self._load_fund_basic_frame()
        frames = [base_frame] if isinstance(base_frame, pd.DataFrame) and not base_frame.empty else []

        if requested_symbols:
            available_symbols = set()
            if frames and "ts_code" in base_frame.columns:
                available_symbols = {
                    str(item or "").strip().upper()
                    for item in base_frame["ts_code"].dropna().tolist()
                }
            missing_symbols = [symbol for symbol in requested_symbols if symbol not in available_symbols]
            for symbol in missing_symbols:
                try:
                    frame = self.pro.fund_basic(ts_code=symbol, fields="ts_code,name,market,list_date")
                except Exception as exc:
                    self.logger.warning("Tushare fund_basic fetch failed for %s: %s", symbol, exc)
                    continue
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    frames.append(frame)

        if not frames:
            return pd.DataFrame(columns=["ts_code", "name", "market", "list_date"])

        result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code"], keep="last")
        if requested_symbols and "ts_code" in result.columns:
            result = result[result["ts_code"].astype("string").str.upper().isin(requested_symbols)]
        return result

    def get_a_stock_etf_basic_frame(self, list_status: str = "L") -> pd.DataFrame:
        """获取A股ETF基础信息。"""
        status = (list_status or "L").strip().upper()
        if status not in self._etf_basic_frames:
            try:
                frame = self.pro.etf_basic(
                    list_status=status,
                    fields=(
                        "ts_code,csname,extname,index_code,index_name,"
                        "exchange,etf_type,list_date,list_status"
                    ),
                )
            except Exception as exc:
                self.logger.warning("Tushare etf_basic fetch failed for list_status=%s: %s", status, exc)
                frame = pd.DataFrame()
            self._etf_basic_frames[status] = frame
        return self._etf_basic_frames[status]

    def _load_daily_basic_frame(self) -> pd.DataFrame:
        if self._daily_basic_frame is None:
            try:
                self._daily_basic_frame = self.pro.daily_basic(fields="ts_code,trade_date,total_share,float_share")
            except Exception as exc:
                self.logger.warning("Tushare daily_basic fetch failed: %s", exc)
                self._daily_basic_frame = pd.DataFrame()
        return self._daily_basic_frame

    def _load_fund_share_frame(self) -> pd.DataFrame:
        if self._fund_share_frame is None:
            try:
                self._fund_share_frame = self.pro.fund_share(fields="ts_code,trade_date,fd_share,fund_type,market")
            except Exception as exc:
                self.logger.warning("Tushare fund_share fetch failed: %s", exc)
                self._fund_share_frame = pd.DataFrame()
        return self._fund_share_frame

    def _lookup_symbol_row(self, frame: pd.DataFrame, ts_code: str, *, sort_columns: List[str]) -> Dict:
        if frame is None or frame.empty or "ts_code" not in frame.columns:
            return {}
        matches = frame[frame["ts_code"] == ts_code]
        if matches.empty:
            return {}
        for column in sort_columns:
            if column in matches.columns:
                matches = matches.sort_values(column, ascending=False)
                break
        return self._row_to_dict(matches.iloc[0])

    def _get_latest_fina_indicator_row(self, ts_code: str) -> Dict:
        if ts_code in self._fina_indicator_cache:
            return self._fina_indicator_cache[ts_code]
        try:
            frame = self.pro.fina_indicator(ts_code=ts_code, fields="ts_code,end_date,eps,bps")
            if isinstance(frame, pd.DataFrame) and not frame.empty and "ts_code" in frame.columns:
                matches = frame[frame["ts_code"] == ts_code]
                if not matches.empty:
                    if "end_date" in matches.columns:
                        matches = matches.sort_values("end_date", ascending=False)
                    row = self._row_to_dict(matches.iloc[0])
                    self._fina_indicator_cache[ts_code] = row
                    return row
        except Exception as exc:
            self.logger.warning("Tushare fina_indicator fetch failed for %s: %s", ts_code, exc)
        self._fina_indicator_cache[ts_code] = {}
        return {}

    def get_a_stock_income_frame(self, ts_code: str, limit: int = 2000) -> pd.DataFrame:
        """获取某只A股的历史利润表/收入表数据。"""
        ts_code = (ts_code or "").strip().upper()
        if not ts_code:
            return pd.DataFrame()
        if ts_code in self._income_frame_cache:
            return self._income_frame_cache[ts_code].copy()

        frames = []
        offset = 0
        while True:
            try:
                self._income_rate_limiter.wait()
                frame = self.pro.income(
                    ts_code=ts_code,
                    fields="ts_code,end_date,ann_date,operate_income,rd_exp,report_type",
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning("Tushare income fetch failed for %s offset=%s: %s", ts_code, offset, exc)
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            self._income_frame_cache[ts_code] = pd.DataFrame()
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["ts_code", "end_date", "ann_date", "report_type"],
            keep="last",
        )
        for column in ("end_date", "ann_date"):
            if column in result.columns:
                result[column] = pd.to_datetime(result[column], format="%Y%m%d", errors="coerce").dt.date
        for column in ("operate_income", "rd_exp"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce")
        if "report_type" in result.columns:
            result["report_type"] = result["report_type"].astype(str)
        result = result.dropna(subset=["ts_code", "end_date"])
        result = result.sort_values(["ann_date", "end_date"], na_position="first").reset_index(drop=True)
        self._income_frame_cache[ts_code] = result
        return result.copy()

    def get_a_stock_income_range_frame(
        self,
        start_date: date,
        end_date: date,
        ts_code: Optional[str] = None,
        limit: int = 5000,
    ) -> pd.DataFrame:
        """按股票代码和公告日期分页获取一段时间内的A股利润表/收入表数据。"""
        ts_code = (ts_code or "").strip().upper()
        if not ts_code:
            self.logger.warning("Skip Tushare income range fetch for %s~%s: ts_code is required", start_date, end_date)
            return pd.DataFrame()
        start_value = self._to_date(start_date)
        end_value = self._to_date(end_date)
        if not start_value or not end_value or start_value > end_value:
            return pd.DataFrame()

        frames = []
        offset = 0
        while True:
            try:
                self._income_rate_limiter.wait()
                frame = self.pro.income(
                    ts_code=ts_code,
                    start_date=start_value.strftime("%Y%m%d"),
                    end_date=end_value.strftime("%Y%m%d"),
                    fields="ts_code,end_date,ann_date,operate_income,rd_exp,report_type",
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:
                self.logger.warning(
                    "Tushare income range fetch failed for %s %s~%s offset=%s: %s",
                    ts_code,
                    start_value,
                    end_value,
                    offset,
                    exc,
                )
                break
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                break
            frames.append(frame)
            if len(frame) < limit:
                break
            offset += limit

        if not frames:
            return pd.DataFrame()

        result = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["ts_code", "end_date", "ann_date", "report_type"],
            keep="last",
        )
        for column in ("end_date", "ann_date"):
            if column in result.columns:
                result[column] = pd.to_datetime(result[column], format="%Y%m%d", errors="coerce").dt.date
        for column in ("operate_income", "rd_exp"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce")
        if "report_type" in result.columns:
            result["report_type"] = result["report_type"].astype("string")
        return result.dropna(subset=["ts_code", "end_date"])

    def _fetch_frame(self, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
        ts_code = self.normalize_symbol(symbol)
        asset = self._infer_asset(ts_code)
        start_value = start_date.strftime("%Y%m%d")
        end_value = end_date.strftime("%Y%m%d")

        pro_bar_kwargs = {
            "api": self.pro,
            "ts_code": ts_code,
            "start_date": start_value,
            "end_date": end_value,
            "freq": "D",
            "asset": asset,
            "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        }
        if asset == "E":
            pro_bar_kwargs["adj"] = "qfq"

        try:
            frame = ts.pro_bar(**pro_bar_kwargs)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        except Exception as exc:
            self.logger.warning("Tushare pro_bar fetch failed for %s: %s", ts_code, exc)

        fallback_methods = ["fund_daily", "daily"] if asset == "FD" else ["daily", "fund_daily"]
        for method_name in fallback_methods:
            fetcher = getattr(self.pro, method_name, None)
            if not callable(fetcher):
                continue
            try:
                frame = fetcher(
                    ts_code=ts_code,
                    start_date=start_value,
                    end_date=end_value,
                    fields="ts_code,trade_date,open,high,low,close,vol,amount",
                )
            except Exception as exc:
                self.logger.warning("Tushare %s fetch failed for %s: %s", method_name, ts_code, exc)
                continue
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        return pd.DataFrame()

    def _frame_to_klines(self, frame: pd.DataFrame) -> List[Dict]:
        if frame is None or frame.empty:
            return []
        working = frame.copy()
        if "trade_date" not in working.columns:
            return []
        working = working.dropna(subset=["trade_date", "open", "high", "low", "close"])
        working = working.sort_values("trade_date")

        klines: List[Dict] = []
        for _, row in working.iterrows():
            trade_date = self._to_date(row.get("trade_date"))
            if not trade_date:
                continue
            try:
                open_price = float(row.get("open"))
                high_price = float(row.get("high"))
                low_price = float(row.get("low"))
                close_price = float(row.get("close"))
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(price) and price > 0 for price in (open_price, high_price, low_price, close_price)):
                continue

            vol_value = row.get("vol")
            amount_value = row.get("amount")
            volume = int(round(float(vol_value) * 100.0)) if vol_value is not None and not pd.isna(vol_value) else 0
            turnover = float(amount_value) * 1000.0 if amount_value is not None and not pd.isna(amount_value) else 0.0

            klines.append(
                {
                    "timestamp": trade_date,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                    "turnover": turnover,
                }
            )
        return klines

    def get_klines_by_date(self, symbol: str, start_date, end_date, period: str = "d") -> List[Dict]:
        if not self.is_daily_period(period):
            self._unsupported(f"K-line period {period}")

        start = self._to_date(start_date)
        end = self._to_date(end_date)
        if not start or not end or start > end:
            return []

        ts_code = self.normalize_symbol(symbol)
        if not self.is_cn_equity_symbol(ts_code):
            self._unsupported(f"K-line data for non-CN symbol {symbol}")

        frame = self._fetch_frame(ts_code, start, end)
        if frame is None or frame.empty:
            return []
        return self._frame_to_klines(frame)

    def get_klines(self, symbol: str, count: int, period: str = "d") -> List[Dict]:
        if not self.is_daily_period(period):
            self._unsupported(f"K-line period {period}")

        fetch_count = count if isinstance(count, int) and count > 0 else 1000
        end_date = date.today()
        start_date = end_date - timedelta(days=max(60, fetch_count * 3))
        klines = self.get_klines_by_date(symbol, start_date, end_date, period)
        if not klines:
            return []
        if isinstance(count, int) and count > 0:
            return klines[-count:]
        return klines

    def get_static_info(self, symbols: List[str]) -> List[Dict]:
        if not symbols:
            return []

        results: List[Dict] = []
        for symbol in symbols:
            normalized = self.normalize_symbol(symbol)
            if not self.is_cn_equity_symbol(normalized):
                self._unsupported(f"static info for non-CN symbol {symbol}")

            ts_code = normalized
            code = self._strip_exchange(normalized)
            asset = self._infer_asset(normalized)

            if asset == "FD":
                basic_row = self._lookup_symbol_row(self._load_fund_basic_frame(), ts_code, sort_columns=["list_date"])
                share_row = self._lookup_symbol_row(self._load_fund_share_frame(), ts_code, sort_columns=["trade_date"])
                if not basic_row:
                    try:
                        frame = self.pro.fund_basic(ts_code=ts_code, fields="ts_code,name,market,list_date")
                        basic_row = self._lookup_symbol_row(frame, ts_code, sort_columns=["list_date"])
                    except Exception as exc:
                        self.logger.warning("Tushare fund_basic fetch failed for %s: %s", ts_code, exc)
                if not share_row:
                    try:
                        frame = self.pro.fund_share(ts_code=ts_code, fields="ts_code,trade_date,fd_share,fund_type,market")
                        share_row = self._lookup_symbol_row(frame, ts_code, sort_columns=["trade_date"])
                    except Exception as exc:
                        self.logger.warning("Tushare fund_share fetch failed for %s: %s", ts_code, exc)
                fd_share = self._to_float(share_row.get("fd_share"))
                total_shares = fd_share * 10000 if fd_share is not None else None
                name = basic_row.get("name") or share_row.get("name") or code
                market = basic_row.get("market") or share_row.get("market")
                results.append(
                    {
                        "symbol": normalized,
                        "code": code,
                        "name": name,
                        "name_cn": name,
                        "market": market,
                        "total_shares": total_shares,
                        "circulating_shares": total_shares,
                        "lot_size": 100,
                        "currency": "CNY",
                        "eps": None,
                        "eps_ttm": None,
                        "bps": None,
                    }
                )
                continue

            basic_row = self._lookup_symbol_row(self._load_stock_basic_frame(), ts_code, sort_columns=["list_date"])
            share_row = self._lookup_symbol_row(self._load_daily_basic_frame(), ts_code, sort_columns=["trade_date"])
            if not basic_row:
                try:
                    frame = self.pro.stock_basic(ts_code=ts_code, fields="ts_code,name,market,exchange,list_date")
                    basic_row = self._lookup_symbol_row(frame, ts_code, sort_columns=["list_date"])
                except Exception as exc:
                    self.logger.warning("Tushare stock_basic fetch failed for %s: %s", ts_code, exc)
            if not share_row:
                try:
                    frame = self.pro.daily_basic(ts_code=ts_code, fields="ts_code,trade_date,total_share,float_share")
                    share_row = self._lookup_symbol_row(frame, ts_code, sort_columns=["trade_date"])
                except Exception as exc:
                    self.logger.warning("Tushare daily_basic fetch failed for %s: %s", ts_code, exc)
            indicator_row = self._get_latest_fina_indicator_row(ts_code)

            total_share = self._to_float(share_row.get("total_share"))
            float_share = self._to_float(share_row.get("float_share"))
            eps = self._to_float(indicator_row.get("eps"))
            bps = self._to_float(indicator_row.get("bps"))
            name = basic_row.get("name") or code
            market = basic_row.get("exchange") or basic_row.get("market")
            results.append(
                {
                    "symbol": normalized,
                    "code": code,
                    "name": name,
                    "name_cn": name,
                    "market": market,
                    "total_shares": total_share * 10000 if total_share is not None else None,
                    "circulating_shares": float_share * 10000 if float_share is not None else None,
                    "lot_size": 100,
                    "currency": "CNY",
                    "eps": eps,
                    "eps_ttm": eps,
                    "bps": bps,
                }
            )

        return results

    def get_quote_batch(self, symbols: List[str]) -> List[Dict]:
        if not symbols:
            return []

        normalized_symbols = [self.normalize_symbol(symbol) for symbol in symbols if symbol]
        if not normalized_symbols:
            return []
        for symbol in normalized_symbols:
            if not self.is_cn_equity_symbol(symbol):
                self._unsupported(f"realtime quote for non-CN symbol {symbol}")

        # Primary source: Tushare Pro rt_min.  Send the complete candidate set
        # first (the service currently returns the full A-share universe in one
        # call), then diff response symbols and retry only missing names in
        # official-limit chunks of 1000.  This remains safe if Tushare restores
        # its documented 1000-row cap while keeping the common path one call.
        # rt_min returns the latest 1-minute bar per symbol; its `close` is the
        # live price during trading and the session close after hours.  Legacy
        # Sina quotes remain as a fallback when rt_min is unavailable.
        unique_symbols = list(dict.fromkeys(normalized_symbols))
        quotes_by_symbol: Dict[str, Dict] = {}

        def _merge_rt_min(batch: List[str], phase: str) -> None:
            try:
                frame = self.pro.rt_min(
                    ts_code=",".join(batch),
                    freq="1MIN",
                    fields="ts_code,time,open,close,high,low,vol,amount",
                )
            except Exception as exc:
                self.logger.error("Tushare rt_min %s failed for %d symbols: %s", phase, len(batch), exc)
                return
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                return
            for _, row in frame.iterrows():
                data = self._row_to_dict(row)
                symbol = self.normalize_symbol(data.get("ts_code") or "")
                price = self._to_float(data.get("close"))
                if not symbol or price is None or price <= 0:
                    continue
                timestamp = None
                time_text = str(data.get("time") or "").strip()
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        timestamp = datetime.strptime(time_text, fmt)
                        break
                    except ValueError:
                        continue
                quotes_by_symbol[symbol] = {
                    "symbol": symbol,
                    "code": self._strip_exchange(symbol),
                    "name": None,
                    "name_cn": None,
                    "price": price,
                    "change": None,
                    "percent_change": None,
                    "high": self._to_float(data.get("high"), price),
                    "low": self._to_float(data.get("low"), price),
                    "open": self._to_float(data.get("open"), price),
                    "prev_close": None,
                    "volume": int(round(self._to_float(data.get("vol"), 0.0) or 0.0)),
                    "turnover": self._to_float(data.get("amount"), 0.0) or 0.0,
                    "timestamp": timestamp,
                    "source": "tushare_rt_min",
                }

        _merge_rt_min(unique_symbols, "all-symbol request")
        missing = [symbol for symbol in unique_symbols if symbol not in quotes_by_symbol]
        for offset in range(0, len(missing), 1000):
            _merge_rt_min(missing[offset:offset + 1000], "missing-symbol retry")

        # Final fallback for names still absent after the bounded Tushare retry.
        missing = [symbol for symbol in unique_symbols if symbol not in quotes_by_symbol]
        for offset in range(0, len(missing), 800):
            batch = missing[offset:offset + 800]
            try:
                codes = [self._strip_exchange(s) for s in batch]
                code_to_symbol = {self._strip_exchange(s): s for s in batch}
                sina_frame = ts.get_realtime_quotes(codes)
                if isinstance(sina_frame, pd.DataFrame) and not sina_frame.empty:
                    for _, row in sina_frame.iterrows():
                        data = self._row_to_dict(row)
                        code = (data.get("code") or "").strip().upper()
                        if not code:
                            continue
                        quote = self._quote_from_realtime_row(code_to_symbol.get(code, self._infer_symbol_from_code(code)), data, volume_scale=0.01)
                        if quote:
                            quote["source"] = "sina_realtime"
                            quotes_by_symbol[quote["symbol"]] = quote
            except Exception as exc:
                self.logger.error("Sina realtime quote fallback failed for %d symbols: %s", len(batch), exc)

        return [quotes_by_symbol[s] for s in normalized_symbols if s in quotes_by_symbol]

    def get_quote(self, symbol: str) -> Dict:
        quotes = self.get_quote_batch([symbol])
        return quotes[0] if quotes else {}

    def get_candlesticks(self, symbol: str, count: int, period: str = "d") -> List[Dict]:
        return self.get_klines(symbol, count, period)

    def get_candlesticks_by_date(self, symbol: str, start, end, period: str = "d") -> List[Dict]:
        return self.get_klines_by_date(symbol, start, end, period)

    def subscribe(self, symbols: List[str], sub_types: List[str], is_first_push: bool = False) -> None:
        self._unsupported("market data subscription")

    def set_on_quote(self, observer: QuoteObserver):
        self._unsupported("quote callbacks")

    def unsubscribe(self, symbols: List[str], sub_types: List[str]):
        self._unsupported("market data unsubscription")

    def get_option_quote_batch(self, symbols: List[str]) -> List[Dict]:
        self._unsupported("option realtime quotes")
