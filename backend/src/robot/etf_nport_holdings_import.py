import argparse
import csv
import io
import logging
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests
from sqlalchemy import func

from ..core.database import ETFHolding as DBETFHolding
from ..core.database import Session
from ..core.utils import normalize_us_equity_symbol
from .etf.qqq import QQQDataFetcher
from .etf.spdr import SPDRDataFetcher


SEC_NPORT_URL_TEMPLATE = (
    "https://www.sec.gov/files/dera/data/form-n-port-data-sets/{year}q{quarter}_nport.zip"
)
DEFAULT_SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "quant-research sevenshal@gmail.com",
)
DEFAULT_CACHE_DIR = Path(os.getenv("SEC_NPORT_CACHE_DIR", "/var/lib/quant_robot/sec_nport"))


TARGET_ETFS = {
    "SPY.US": {"cik": "0000884394", "name": "SPDR S&P 500 ETF TRUST"},
    "QQQ.US": {"cik": "0001067839", "name": "Invesco QQQ Trust, Series 1"},
    "DIA.US": {"cik": "0001041130", "name": "SPDR DOW JONES INDUSTRIAL AVERAGE ETF TRUST"},
    "XBI.US": {"cik": "0001064642", "series_id": "S000010018", "name": "SPDR S&P Biotech ETF"},
    "KRE.US": {"cik": "0001064642", "series_id": "S000012325", "name": "SPDR S&P Regional Banking ETF"},
    "XRT.US": {"cik": "0001064642", "series_id": "S000012322", "name": "SPDR S&P Retail ETF"},
    "XLF.US": {"cik": "0001064641", "series_id": "S000006411", "name": "Financial Select Sector SPDR ETF"},
    "XLV.US": {"cik": "0001064641", "series_id": "S000006412", "name": "Health Care Select Sector SPDR ETF"},
    "XLK.US": {"cik": "0001064641", "series_id": "S000006415", "name": "Technology Select Sector SPDR ETF"},
    "XLE.US": {"cik": "0001064641", "series_id": "S000006410", "name": "Energy Select Sector SPDR ETF"},
    "XLI.US": {"cik": "0001064641", "series_id": "S000006413", "name": "Industrial Select Sector SPDR ETF"},
    "XLU.US": {"cik": "0001064641", "series_id": "S000006416", "name": "Utilities Select Sector SPDR ETF"},
}

STATIC_CUSIP_SYMBOL_MAP = {
    "70432V102": "PAYC.US",
    "60855R100": "MOH.US",
    "57667L107": "MTCH.US",
    "436440101": "HOLX.US",
    "513272104": "LW.US",
    "15677J108": "DAY.US",
    "046353108": "AZN.US",
    "049468101": "TEAM.US",
}


@dataclass(frozen=True)
class NPortFiling:
    etf_symbol: str
    accession_number: str
    report_date: date
    filing_date: Optional[date]


@dataclass
class NPortHolding:
    symbol: str
    name: str
    asset_class: str
    shares: int
    weight: float


class ETFNPortHoldingsImporter:
    """Import official SEC N-PORT ETF holdings into etf_holdings.

    N-PORT data usually has CUSIP but no ticker for large ETFs, so this importer
    builds a CUSIP -> ticker map from the current issuer holdings files. Unknown
    historical CUSIPs are skipped because downstream price logic requires tickers.
    """

    MIN_PARSE_COVERAGE = 0.75

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        sec_user_agent: str = DEFAULT_SEC_USER_AGENT,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.symbols = symbols or list(TARGET_ETFS.keys())
        self.cache_dir = Path(cache_dir)
        self.sec_user_agent = sec_user_agent
        self.db = Session()
        self._cusip_symbol_map: Optional[Dict[str, str]] = None

    def close(self):
        self.db.close()

    def import_quarter(
        self,
        year: int,
        quarter: int,
        download: bool = True,
        allow_missing: bool = False,
    ) -> Dict[str, object]:
        try:
            zip_path = self._ensure_quarter_zip(year, quarter) if download else self._quarter_zip_path(year, quarter)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if allow_missing and status_code == 404:
                return {
                    "year": year,
                    "quarter": quarter,
                    "zip_path": str(self._quarter_zip_path(year, quarter)),
                    "target_filings": 0,
                    "saved_dates": 0,
                    "saved_rows": 0,
                    "inserted_dates": 0,
                    "updated_existing_dates": 0,
                    "skipped_existing_dates": 0,
                    "skipped_unresolved": 0,
                    "skipped_invalid_dates": 0,
                    "invalid_filings": [],
                    "filings_without_holdings": [],
                    "missing": True,
                }
            raise
        return self.import_zip(zip_path, year=year, quarter=quarter)

    def import_zip(
        self,
        zip_path: Path,
        year: Optional[int] = None,
        quarter: Optional[int] = None,
    ) -> Dict[str, object]:
        zip_path = Path(zip_path)
        if not zip_path.exists():
            raise FileNotFoundError(f"N-PORT zip not found: {zip_path}")

        saved_dates = 0
        saved_rows = 0
        inserted_dates = 0
        updated_existing_dates = 0
        skipped_unresolved = 0
        skipped_invalid_dates = 0
        filings_without_holdings = []
        invalid_filings = []

        with zipfile.ZipFile(zip_path) as archive:
            filings = self._target_filings(archive)
            filings_to_import = {
                filing.accession_number: filing
                for filing in filings.values()
            }

            if not filings_to_import:
                return {
                    "year": year,
                    "quarter": quarter,
                    "zip_path": str(zip_path),
                    "target_filings": len(filings),
                    "saved_dates": 0,
                    "saved_rows": 0,
                    "inserted_dates": 0,
                    "updated_existing_dates": 0,
                    "skipped_existing_dates": 0,
                    "skipped_unresolved": 0,
                    "skipped_invalid_dates": 0,
                    "invalid_filings": [],
                    "filings_without_holdings": [],
                }

            identifier_symbol_map = self._load_identifier_symbol_map(archive)
            target_rows_by_accession, sec_cusip_symbol_map, raw_equity_counts = self._collect_target_holding_rows(
                archive,
                filings_to_import,
                identifier_symbol_map,
            )
            cusip_symbol_map = self._load_cusip_symbol_map()
            cusip_symbol_map.update(sec_cusip_symbol_map)

            holdings_by_accession: Dict[str, List[NPortHolding]] = {}
            for accession, rows in target_rows_by_accession.items():
                holdings_by_accession[accession] = []
                for row in rows:
                    holding = self._parse_holding(row, identifier_symbol_map, cusip_symbol_map)
                    if holding is None:
                        if self._is_long_equity_share_row(row):
                            skipped_unresolved += 1
                        continue
                    holdings_by_accession[accession].append(holding)

            try:
                for accession, filing in filings_to_import.items():
                    holdings = holdings_by_accession.get(accession) or []
                    if not holdings:
                        filings_without_holdings.append(accession)
                        continue

                    invalid_reason = self._invalid_holdings_reason(
                        filing.etf_symbol,
                        filing.report_date,
                        holdings,
                        raw_equity_counts.get(accession, 0),
                    )
                    if invalid_reason:
                        skipped_invalid_dates += 1
                        invalid_filings.append({
                            "accession_number": accession,
                            "symbol": filing.etf_symbol,
                            "date": filing.report_date.isoformat(),
                            "reason": invalid_reason,
                        })
                        continue

                    had_existing = self._has_holdings(filing.etf_symbol, filing.report_date)
                    self._replace_holdings(filing, holdings)
                    saved_dates += 1
                    saved_rows += len(holdings)
                    if had_existing:
                        updated_existing_dates += 1
                    else:
                        inserted_dates += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

        return {
            "year": year,
            "quarter": quarter,
            "zip_path": str(zip_path),
            "target_filings": len(filings),
            "saved_dates": saved_dates,
            "saved_rows": saved_rows,
            "inserted_dates": inserted_dates,
            "updated_existing_dates": updated_existing_dates,
            "skipped_existing_dates": 0,
            "skipped_unresolved": skipped_unresolved,
            "skipped_invalid_dates": skipped_invalid_dates,
            "invalid_filings": invalid_filings,
            "filings_without_holdings": filings_without_holdings,
        }

    def import_range(
        self,
        start_year: int,
        start_quarter: int,
        end_year: int,
        end_quarter: int,
        allow_missing: bool = False,
    ) -> List[Dict[str, object]]:
        results = []
        for year, quarter in self._iter_quarters(start_year, start_quarter, end_year, end_quarter):
            results.append(self.import_quarter(year, quarter, allow_missing=allow_missing))
        return results

    def import_recent(
        self,
        lookback_quarters: int = 4,
        end_date: Optional[date] = None,
        allow_missing: bool = True,
    ) -> List[Dict[str, object]]:
        end_year, end_quarter = self._quarter_for_date(end_date or date.today())
        quarters = list(self._iter_recent_quarters(end_year, end_quarter, lookback_quarters))
        if not quarters:
            return []
        start_year, start_quarter = quarters[0]
        return self.import_range(
            start_year,
            start_quarter,
            end_year,
            end_quarter,
            allow_missing=allow_missing,
        )

    def _target_filings(self, archive: zipfile.ZipFile) -> Dict[Tuple[str, date], NPortFiling]:
        target_configs = {
            symbol: config
            for symbol, config in TARGET_ETFS.items()
            if symbol in self.symbols
        }
        submissions = {}
        for row in self._iter_tsv(archive, "SUBMISSION.tsv"):
            accession = row.get("ACCESSION_NUMBER")
            report_date = self._parse_sec_date(row.get("REPORT_DATE"))
            if not accession or not report_date:
                continue
            submissions[accession] = {
                "report_date": report_date,
                "filing_date": self._parse_sec_date(row.get("FILING_DATE")),
            }

        fund_info_by_accession = {}
        for row in self._iter_tsv(archive, "FUND_REPORTED_INFO.tsv"):
            accession = row.get("ACCESSION_NUMBER")
            if accession:
                fund_info_by_accession[accession] = row

        filings: Dict[Tuple[str, date], NPortFiling] = {}
        for row in self._iter_tsv(archive, "REGISTRANT.tsv"):
            accession = row.get("ACCESSION_NUMBER")
            submission = submissions.get(accession)
            if not accession or not submission:
                continue

            symbol = self._match_target_symbol(
                row.get("CIK"),
                fund_info_by_accession.get(accession) or {},
                target_configs,
            )
            if not symbol:
                continue

            filing = NPortFiling(
                etf_symbol=symbol,
                accession_number=accession,
                report_date=submission["report_date"],
                filing_date=submission.get("filing_date"),
            )
            key = (symbol, filing.report_date)
            existing = filings.get(key)
            if not existing or self._date_sort_value(filing.filing_date) > self._date_sort_value(existing.filing_date):
                filings[key] = filing
        return filings

    def _match_target_symbol(
        self,
        cik: Optional[str],
        fund_info: Dict[str, str],
        target_configs: Dict[str, Dict[str, str]],
    ) -> Optional[str]:
        for symbol, config in target_configs.items():
            if config.get("cik") != cik:
                continue
            series_id = config.get("series_id")
            if series_id and fund_info.get("SERIES_ID") != series_id:
                continue
            return symbol
        return None

    def _parse_holding(
        self,
        row: Dict[str, str],
        identifier_symbol_map: Dict[str, str],
        cusip_symbol_map: Dict[str, str],
    ) -> Optional[NPortHolding]:
        if not self._is_long_equity_share_row(row):
            return None

        cusip = self._normalize_cusip(row.get("ISSUER_CUSIP"))
        symbol = cusip_symbol_map.get(cusip) or identifier_symbol_map.get(row.get("HOLDING_ID"))
        if not symbol:
            return None

        weight = self._safe_float(row.get("PERCENTAGE"))
        if weight is None or weight <= 0:
            return None
        shares = self._safe_int(row.get("BALANCE"))
        if shares <= 0:
            return None

        return NPortHolding(
            symbol=symbol,
            name=(row.get("ISSUER_TITLE") or row.get("ISSUER_NAME") or symbol).strip(),
            asset_class="Equity",
            shares=shares,
            weight=weight / 100.0,
        )

    def _replace_holdings(self, filing: NPortFiling, holdings: List[NPortHolding]) -> None:
        self.db.query(DBETFHolding).filter(
            DBETFHolding.etf_symbol == filing.etf_symbol,
            DBETFHolding.date == filing.report_date,
        ).delete(synchronize_session=False)

        for holding in holdings:
            self.db.add(
                DBETFHolding(
                    etf_symbol=filing.etf_symbol,
                    symbol=holding.symbol,
                    date=filing.report_date,
                    name=holding.name,
                    asset_class=holding.asset_class,
                    shares=holding.shares,
                    weight=holding.weight,
                )
            )

    def _invalid_holdings_reason(
        self,
        etf_symbol: str,
        holding_date: date,
        holdings: List[NPortHolding],
        raw_equity_count: int,
    ) -> Optional[str]:
        if not holdings:
            return f"{etf_symbol} {holding_date} holdings are empty"

        parsed_count = sum(
            1 for holding in holdings
            if holding.asset_class == "Equity" and float(holding.weight or 0.0) > 0
        )
        if raw_equity_count > 0 and parsed_count / raw_equity_count < self.MIN_PARSE_COVERAGE:
            return (
                f"parsed_equity_count={parsed_count}, raw_equity_count={raw_equity_count}; "
                "keep existing DB rows"
            )

        total_weight = sum(float(holding.weight or 0.0) for holding in holdings)
        if total_weight <= 0.5:
            return f"total_weight={total_weight:.4f}; keep existing DB rows"
        return None

    @staticmethod
    def _is_long_equity_share_row(row: Dict[str, str]) -> bool:
        if row.get("ASSET_CAT") != "EC" or row.get("UNIT") != "NS":
            return False
        payoff_profile = row.get("PAYOFF_PROFILE")
        return not payoff_profile or payoff_profile == "Long"

    def _load_identifier_symbol_map(self, archive: zipfile.ZipFile) -> Dict[str, str]:
        mapping = {}
        for row in self._iter_tsv(archive, "IDENTIFIERS.tsv"):
            holding_id = row.get("HOLDING_ID")
            ticker = self._normalize_ticker(row.get("IDENTIFIER_TICKER"))
            if holding_id and ticker:
                mapping[holding_id] = ticker
        return mapping

    def _collect_target_holding_rows(
        self,
        archive: zipfile.ZipFile,
        filings_to_import: Dict[str, NPortFiling],
        identifier_symbol_map: Dict[str, str],
    ) -> Tuple[Dict[str, List[Dict[str, str]]], Dict[str, str], Dict[str, int]]:
        target_rows_by_accession: Dict[str, List[Dict[str, str]]] = {
            accession: [] for accession in filings_to_import
        }
        sec_cusip_symbol_map: Dict[str, str] = {}
        raw_equity_counts: Dict[str, int] = {
            accession: 0 for accession in filings_to_import
        }
        for row in self._iter_tsv(archive, "FUND_REPORTED_HOLDING.tsv"):
            holding_id = row.get("HOLDING_ID")
            symbol = identifier_symbol_map.get(holding_id)
            cusip = self._normalize_cusip(row.get("ISSUER_CUSIP"))
            if symbol and cusip:
                sec_cusip_symbol_map.setdefault(cusip, symbol)

            accession = row.get("ACCESSION_NUMBER")
            if accession in target_rows_by_accession:
                target_rows_by_accession[accession].append(row)
                if self._is_long_equity_share_row(row):
                    raw_equity_counts[accession] += 1
        return target_rows_by_accession, sec_cusip_symbol_map, raw_equity_counts

    def _load_cusip_symbol_map(self) -> Dict[str, str]:
        if self._cusip_symbol_map is not None:
            return self._cusip_symbol_map

        mapping: Dict[str, str] = {}
        spdr_fetcher = SPDRDataFetcher()
        for symbol in SPDRDataFetcher.ETF_CONFIGS:
            if symbol in self.symbols:
                mapping.update(self._load_spdr_cusip_symbol_map(spdr_fetcher, symbol))
        if "QQQ.US" in self.symbols:
            mapping.update(self._load_qqq_cusip_symbol_map())
        mapping.update(STATIC_CUSIP_SYMBOL_MAP)
        self._cusip_symbol_map = mapping
        return mapping

    def _load_spdr_cusip_symbol_map(self, fetcher: SPDRDataFetcher, etf_symbol: str) -> Dict[str, str]:
        config = fetcher.ETF_CONFIGS.get(etf_symbol)
        if not config:
            return {}
        response = requests.get(config["url"], headers=fetcher.headers, timeout=30)
        response.raise_for_status()
        df = pd.read_excel(io.BytesIO(response.content))

        header_row = None
        for idx, row in df.iterrows():
            if "Ticker" in row.values and "Identifier" in row.values:
                header_row = idx
                break
        if header_row is None:
            raise ValueError(f"Cannot find ticker/identifier header in SPDR holdings for {etf_symbol}")

        data_df = df.iloc[header_row + 1:].copy()
        data_df.columns = df.iloc[header_row]
        mapping = {}
        for _, row in data_df.iterrows():
            ticker = self._normalize_ticker(row.get("Ticker"))
            cusip = self._normalize_cusip(row.get("Identifier"))
            if ticker and cusip:
                mapping[cusip] = ticker
        return mapping

    def _load_qqq_cusip_symbol_map(self) -> Dict[str, str]:
        fetcher = QQQDataFetcher()
        data = fetcher._get_holdings_response().json()
        mapping = {}
        for item in data.get("holdings", []):
            ticker = self._normalize_ticker(item.get("ticker"))
            cusip = self._normalize_cusip(item.get("cusip"))
            if ticker and cusip:
                mapping[cusip] = ticker
        return mapping

    def _ensure_quarter_zip(self, year: int, quarter: int) -> Path:
        zip_path = self._quarter_zip_path(year, quarter)
        if zip_path.exists() and zip_path.stat().st_size > 0:
            return zip_path

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        url = SEC_NPORT_URL_TEMPLATE.format(year=year, quarter=quarter)
        temp_path = zip_path.with_suffix(".tmp")
        headers = {
            "User-Agent": self.sec_user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        with requests.get(url, headers=headers, stream=True, timeout=60) as response:
            response.raise_for_status()
            with temp_path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)
        temp_path.replace(zip_path)
        return zip_path

    def _quarter_zip_path(self, year: int, quarter: int) -> Path:
        return self.cache_dir / f"{year}q{quarter}_nport.zip"

    def _has_holdings(self, etf_symbol: str, holding_date: date) -> bool:
        return (
            self.db.query(func.count(DBETFHolding.symbol))
            .filter(
                DBETFHolding.etf_symbol == etf_symbol,
                DBETFHolding.date == holding_date,
            )
            .scalar()
            > 0
        )

    @staticmethod
    def _iter_tsv(archive: zipfile.ZipFile, filename: str) -> Iterable[Dict[str, str]]:
        with archive.open(filename) as binary_file:
            text_file = io.TextIOWrapper(binary_file, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text_file, delimiter="\t")
            for row in reader:
                yield row

    @staticmethod
    def _parse_sec_date(value: Optional[str]) -> Optional[date]:
        if not value:
            return None
        return datetime.strptime(value.strip(), "%d-%b-%Y").date()

    @staticmethod
    def _date_sort_value(value: Optional[date]) -> date:
        return value or date.min

    @staticmethod
    def _normalize_cusip(value) -> Optional[str]:
        text = str(value or "").strip().upper()
        if not text or text in {"N/A", "NONE", "000000000"}:
            return None
        return text

    @staticmethod
    def _normalize_ticker(value) -> Optional[str]:
        return normalize_us_equity_symbol(value)

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _iter_quarters(
        start_year: int,
        start_quarter: int,
        end_year: int,
        end_quarter: int,
    ) -> Iterable[Tuple[int, int]]:
        year = start_year
        quarter = start_quarter
        while (year, quarter) <= (end_year, end_quarter):
            yield year, quarter
            quarter += 1
            if quarter > 4:
                year += 1
                quarter = 1

    @staticmethod
    def _quarter_for_date(value: date) -> Tuple[int, int]:
        return value.year, (value.month - 1) // 3 + 1

    @staticmethod
    def _iter_recent_quarters(
        end_year: int,
        end_quarter: int,
        lookback_quarters: int,
    ) -> Iterable[Tuple[int, int]]:
        year = end_year
        quarter = end_quarter
        items: List[Tuple[int, int]] = []
        for _ in range(max(0, lookback_quarters)):
            items.append((year, quarter))
            quarter -= 1
            if quarter < 1:
                year -= 1
                quarter = 4
        return reversed(items)


def main():
    parser = argparse.ArgumentParser(description="Import SEC N-PORT ETF holdings into etf_holdings")
    parser.add_argument("--year", type=int, help="N-PORT dataset year")
    parser.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], help="N-PORT dataset quarter")
    parser.add_argument("--start-year", type=int, help="Range start year")
    parser.add_argument("--start-quarter", type=int, choices=[1, 2, 3, 4], help="Range start quarter")
    parser.add_argument("--end-year", type=int, help="Range end year")
    parser.add_argument("--end-quarter", type=int, choices=[1, 2, 3, 4], help="Range end quarter")
    parser.add_argument("--zip", dest="zip_path", help="Import an existing N-PORT zip file")
    parser.add_argument("--recent", type=int, help="Import the latest N quarters, including the current quarter")
    parser.add_argument("--allow-missing", action="store_true", help="Skip unavailable SEC quarter zips")
    parser.add_argument(
        "--symbols",
        default=",".join(TARGET_ETFS.keys()),
        help="Comma-separated ETF symbols",
    )
    args = parser.parse_args()

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    importer = ETFNPortHoldingsImporter(symbols=symbols)
    try:
        if args.zip_path:
            result = importer.import_zip(Path(args.zip_path), year=args.year, quarter=args.quarter)
            print(result)
        elif args.year and args.quarter:
            result = importer.import_quarter(args.year, args.quarter, allow_missing=args.allow_missing)
            print(result)
        elif all([args.start_year, args.start_quarter, args.end_year, args.end_quarter]):
            results = importer.import_range(
                args.start_year,
                args.start_quarter,
                args.end_year,
                args.end_quarter,
                allow_missing=args.allow_missing,
            )
            for result in results:
                print(result)
        elif args.recent:
            results = importer.import_recent(
                lookback_quarters=args.recent,
                allow_missing=args.allow_missing,
            )
            for result in results:
                print(result)
        else:
            parser.error(
                "Provide --zip, --year/--quarter, --recent, or "
                "--start-year/--start-quarter/--end-year/--end-quarter"
            )
    finally:
        importer.close()


if __name__ == "__main__":
    main()
