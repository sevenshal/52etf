import logging
import os
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Set

from sqlalchemy import distinct, or_
from sqlalchemy.orm import Session as ORMSession

from ..core.database import ETFHolding, Session, USStockIndustrySnapshot
from ..core.services.fmp_service import FMPService
from ..core.utils import normalize_us_equity_symbol


DEFAULT_CANDIDATE_ETFS = ["SPY.US", "QQQ.US"]
DEFAULT_PROVIDER = "fmp"
DEFAULT_DAILY_SINGLE_REQUEST_LIMIT = 900
US_COMMON_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]*\.US$")
FMP_INDUSTRY_SKIP_SYMBOLS_ENV = "FMP_INDUSTRY_SKIP_SYMBOLS"

# Historical/invalid SPY/QQQ holding tickers that FMP stable/profile no longer resolves.
DEFAULT_FMP_PROFILE_SKIP_SYMBOLS = {
    "BHGE.US",
    "CBS.US",
    "CELG.US",
    "CTL.US",
    "CTRP.US",
    "CXO.US",
    "EUR.US",
    "ETFC.US",
    "FLIR.US",
    "JEC.US",
    "MYL.US",
    "NBL.US",
    "NQM6.US",
    "NQU6.US",
    "RTN.US",
    "SYMC.US",
    "TIF.US",
    "UNHN.US",
    "UTX.US",
    "VAR.US",
    "VIAB.US",
    "WCG.US",
    "XEC.US",
}


def _clean_text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol_to_fmp(symbol: str) -> Optional[str]:
    normalized = normalize_us_equity_symbol(symbol)
    if not normalized or not normalized.endswith(".US"):
        return None
    return normalized[:-3].replace(".", "-")


def _normalize_symbol_set(symbols: Optional[Sequence[str]]) -> Set[str]:
    normalized_symbols: Set[str] = set()
    for symbol in symbols or []:
        normalized = normalize_us_equity_symbol(symbol)
        if normalized:
            normalized_symbols.add(normalized)
    return normalized_symbols


def _profile_skip_symbols(extra_symbols: Optional[Sequence[str]] = None) -> Set[str]:
    env_symbols = re.split(r"[\s,;]+", os.getenv(FMP_INDUSTRY_SKIP_SYMBOLS_ENV, "").strip())
    skip_symbols = set(DEFAULT_FMP_PROFILE_SKIP_SYMBOLS)
    skip_symbols.update(_normalize_symbol_set(env_symbols))
    skip_symbols.update(_normalize_symbol_set(extra_symbols))
    return skip_symbols


def _candidate_etf_component_symbols(
    db: ORMSession,
    candidate_etfs: Optional[Sequence[str]] = None,
) -> List[str]:
    etf_symbols = [
        normalize_us_equity_symbol(item)
        for item in (candidate_etfs or DEFAULT_CANDIDATE_ETFS)
    ]
    etf_symbols = [item for item in dict.fromkeys(etf_symbols) if item]
    if not etf_symbols:
        return []

    rows = (
        db.query(distinct(ETFHolding.symbol))
        .filter(
            ETFHolding.etf_symbol.in_(etf_symbols),
            or_(ETFHolding.asset_class == "Equity", ETFHolding.asset_class == "EQUITY"),
        )
        .all()
    )
    symbols = []
    for row in rows:
        symbol = normalize_us_equity_symbol(row[0])
        if symbol and US_COMMON_SYMBOL_PATTERN.fullmatch(symbol):
            symbols.append(symbol)
    return sorted(dict.fromkeys(symbols))


class USStockIndustrySync:
    """Sync slow-moving US stock sector/industry metadata into SQLite."""

    def __init__(
        self,
        db: Optional[ORMSession] = None,
        fmp_service: Optional[FMPService] = None,
    ):
        self.db = db or Session()
        self._owns_db = db is None
        self.fmp = fmp_service or FMPService()
        self.logger = logging.getLogger(self.__class__.__name__)

    def close(self):
        if self._owns_db:
            self.db.close()

    def sync_spy_qqq(
        self,
        candidate_etfs: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        force: bool = False,
        snapshot_date: Optional[date] = None,
        profile_skip_symbols: Optional[Sequence[str]] = None,
    ) -> Dict:
        snapshot_date = snapshot_date or date.today()
        symbols = _candidate_etf_component_symbols(self.db, candidate_etfs)
        if not symbols:
            return {
                "status": "empty",
                "symbols": 0,
                "saved": 0,
                "skipped_existing": 0,
                "skipped_profile_unavailable": 0,
                "skipped_profile_unavailable_symbols": [],
                "errors": [],
            }

        profile_skip_set = _profile_skip_symbols(profile_skip_symbols)
        skipped_profile_unavailable_symbols = [symbol for symbol in symbols if symbol in profile_skip_set]
        eligible_symbols = [symbol for symbol in symbols if symbol not in profile_skip_set]
        target_symbols = eligible_symbols if force else self._filter_missing_symbols(eligible_symbols)
        skipped_existing = len(eligible_symbols) - len(target_symbols)
        saved_symbols: Set[str] = set()
        runtime_profile_unavailable_symbols: Set[str] = set()
        errors: List[Dict] = []
        api_calls = 0

        single_limit = self._single_request_limit(limit)
        for symbol in target_symbols[:single_limit]:
            fmp_symbol = _symbol_to_fmp(symbol)
            if not fmp_symbol:
                errors.append({"symbol": symbol, "source": "single", "error": "无法转换为FMP symbol"})
                continue
            profile = self.fmp.get_company_profile(fmp_symbol)
            api_calls += 1
            if not profile:
                runtime_profile_unavailable_symbols.add(symbol)
                continue
            try:
                self._upsert_profile(profile, symbol, snapshot_date)
                saved_symbols.add(symbol)
            except Exception as exc:
                errors.append({"symbol": symbol, "source": "single", "error": str(exc)})

        self.db.commit()
        remaining_after_limit = max(0, len(target_symbols) - single_limit)
        skipped_profile_unavailable_symbols = sorted(
            dict.fromkeys(
                [
                    *skipped_profile_unavailable_symbols,
                    *runtime_profile_unavailable_symbols,
                ]
            )
        )
        resolved_symbols = saved_symbols | runtime_profile_unavailable_symbols
        return {
            "status": "ok",
            "provider": DEFAULT_PROVIDER,
            "date": snapshot_date.isoformat(),
            "symbols": len(symbols),
            "target_symbols": len(target_symbols),
            "saved": len(saved_symbols),
            "skipped_existing": skipped_existing,
            "skipped_profile_unavailable": len(skipped_profile_unavailable_symbols),
            "skipped_profile_unavailable_symbols": skipped_profile_unavailable_symbols,
            "remaining": max(0, len(target_symbols) - len(resolved_symbols)),
            "remaining_after_limit": remaining_after_limit,
            "api_calls": api_calls,
            "single_request_limit": single_limit,
            "errors": errors,
        }

    def _filter_missing_symbols(self, symbols: List[str]) -> List[str]:
        existing_rows = (
            self.db.query(USStockIndustrySnapshot.symbol)
            .filter(
                USStockIndustrySnapshot.provider == DEFAULT_PROVIDER,
                USStockIndustrySnapshot.symbol.in_(symbols),
                or_(
                    USStockIndustrySnapshot.sector.isnot(None),
                    USStockIndustrySnapshot.industry.isnot(None),
                    USStockIndustrySnapshot.sic_code.isnot(None),
                ),
            )
            .all()
        )
        existing = {row[0] for row in existing_rows}
        return [symbol for symbol in symbols if symbol not in existing]

    def _upsert_profile(self, profile: Dict, symbol: str, snapshot_date: date):
        record = (
            self.db.query(USStockIndustrySnapshot)
            .filter(
                USStockIndustrySnapshot.symbol == symbol,
                USStockIndustrySnapshot.date == snapshot_date,
                USStockIndustrySnapshot.provider == DEFAULT_PROVIDER,
            )
            .first()
        )
        if not record:
            record = USStockIndustrySnapshot(
                symbol=symbol,
                date=snapshot_date,
                provider=DEFAULT_PROVIDER,
            )
            self.db.add(record)

        record.name = _clean_text(
            profile.get("companyName")
            or profile.get("company")
            or profile.get("name")
        )
        record.exchange = _clean_text(profile.get("exchangeShortName") or profile.get("exchange"))
        record.sector = _clean_text(profile.get("sector"))
        record.industry_group = _clean_text(profile.get("industryGroup"))
        record.industry = _clean_text(profile.get("industry"))
        record.sub_industry = _clean_text(profile.get("subIndustry"))
        record.sic_code = _clean_text(profile.get("sicCode") or profile.get("sic_code"))
        record.sic_description = _clean_text(
            profile.get("sicDescription")
            or profile.get("sic_description")
            or profile.get("sic")
        )
        record.market_cap = _safe_float(
            profile.get("marketCap")
            or profile.get("mktCap")
            or profile.get("market_cap")
        )
        record.raw_data = profile
        record.updated_at = datetime.now()

    def _single_request_limit(self, limit: Optional[int]) -> int:
        env_value = os.getenv("FMP_INDUSTRY_DAILY_LIMIT")
        try:
            default_limit = int(env_value) if env_value else DEFAULT_DAILY_SINGLE_REQUEST_LIMIT
        except ValueError:
            default_limit = DEFAULT_DAILY_SINGLE_REQUEST_LIMIT
        if limit is None:
            return max(0, default_limit)
        return max(0, min(int(limit), default_limit))


def sync_us_stock_industry_snapshots(
    candidate_etfs: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    force: bool = False,
    profile_skip_symbols: Optional[Sequence[str]] = None,
) -> Dict:
    syncer = USStockIndustrySync()
    try:
        return syncer.sync_spy_qqq(
            candidate_etfs=candidate_etfs,
            limit=limit,
            force=force,
            profile_skip_symbols=profile_skip_symbols,
        )
    finally:
        syncer.close()
