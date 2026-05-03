import logging
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy import and_

from ..core.database import ETFHolding as DBETFHolding
from ..core.database import Session
from ..core.models.etf import ETFHolding
from ..core.services.etf_fear_greed_clone_service import ETFFearGreedCloneCalculator
from ..core.services.longport import LongPortService
from ..core.services.quote import QuoteService
from .etf.ishares import ISharesETFFetcher
from .etf_manager import ETFManager
from .etf_nport_holdings_import import ETFNPortHoldingsImporter, TARGET_ETFS


DEFAULT_HISTORICAL_START_DATE = date(2025, 1, 1)


class ETFHoldingsDBWriter:
    MIN_EQUITY_HOLDINGS = {
        "SOXX.US": 20,
        "IWM.US": 1000,
        "ITB.US": 20,
        "ITA.US": 20,
        "SPY.US": 400,
        "QQQ.US": 80,
        "DIA.US": 25,
    }

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db = Session()

    def close(self):
        self.db.close()

    def _save_holdings(
        self,
        etf_symbol: str,
        holding_date: date,
        holdings: List[ETFHolding],
    ) -> int:
        valid_holdings = self._validate_holdings_for_overwrite(
            etf_symbol,
            holding_date,
            holdings,
        )

        self.db.query(DBETFHolding).filter(
            and_(
                DBETFHolding.etf_symbol == etf_symbol,
                DBETFHolding.date == holding_date,
            )
        ).delete(synchronize_session=False)

        saved = 0
        for holding in valid_holdings:
            symbol = str(holding.symbol or "").strip()
            self.db.merge(
                DBETFHolding(
                    etf_symbol=etf_symbol,
                    symbol=symbol,
                    date=holding_date,
                    name=holding.name,
                    asset_class=holding.asset_class,
                    shares=self._safe_int(holding.shares),
                    weight=float(holding.weight or 0.0),
                )
            )
            saved += 1
        return saved

    def _validate_holdings_for_overwrite(
        self,
        etf_symbol: str,
        holding_date: date,
        holdings: List[ETFHolding],
    ) -> List[ETFHolding]:
        valid_holdings = [
            holding for holding in holdings
            if str(holding.symbol or "").strip()
        ]
        if not valid_holdings:
            raise ValueError(f"{etf_symbol} {holding_date} holdings are empty; keep existing DB rows")

        equity_count = sum(
            1 for holding in valid_holdings
            if holding.asset_class == "Equity" and float(holding.weight or 0.0) > 0
        )
        min_equity_count = self.MIN_EQUITY_HOLDINGS.get(etf_symbol, 1)
        if equity_count < min_equity_count:
            raise ValueError(
                f"{etf_symbol} {holding_date} holdings look incomplete: "
                f"equity_count={equity_count}, expected>={min_equity_count}; keep existing DB rows"
            )

        total_weight = sum(float(holding.weight or 0.0) for holding in valid_holdings)
        if total_weight <= 0.5:
            raise ValueError(
                f"{etf_symbol} {holding_date} holdings weight looks invalid: "
                f"total_weight={total_weight:.4f}; keep existing DB rows"
            )
        return valid_holdings

    @staticmethod
    def _safe_int(value) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0


class ETFHoldingsLatestIngest(ETFHoldingsDBWriter):
    """Fetch the latest issuer holdings snapshot and store it by its own date."""

    def __init__(self, symbols: Optional[List[str]] = None):
        super().__init__()
        self.manager = ETFManager(LongPortService.get_instance())
        self.fetchers = self.manager.fetchers
        self.symbols = symbols or list(self.fetchers.keys())

    def close(self):
        try:
            self.manager.db_session.close()
        finally:
            super().close()

    def sync_latest(self) -> Dict[str, object]:
        saved = 0
        skipped = 0
        errors = []
        saved_dates: Dict[str, str] = {}

        try:
            for symbol in self.symbols:
                fetcher = self.fetchers.get(symbol)
                if not fetcher:
                    errors.append({"symbol": symbol, "error": "unsupported ETF"})
                    skipped += 1
                    continue

                try:
                    holdings_data = fetcher.get_holdings(symbol)
                    if not holdings_data.update_date:
                        raise ValueError(f"{symbol} latest holdings missing update_date")
                    saved_count = self._save_holdings(
                        symbol,
                        holdings_data.update_date,
                        holdings_data.holdings,
                    )
                    self.db.commit()
                    saved += saved_count
                    saved_dates[symbol] = holdings_data.update_date.isoformat()
                    self.logger.info(
                        "Saved %s latest holdings for %s on %s",
                        saved_count,
                        symbol,
                        holdings_data.update_date,
                    )
                except Exception as exc:
                    self.db.rollback()
                    errors.append({"symbol": symbol, "error": str(exc)})
                    skipped += 1
                    self.logger.error("Failed to sync latest holdings for %s: %s", symbol, exc)
        except Exception:
            self.db.rollback()
            raise

        return {
            "symbols": self.symbols,
            "saved": saved,
            "skipped": skipped,
            "errors": errors,
            "saved_dates": saved_dates,
        }


class ETFHistoricalHoldingsBackfill(ETFHoldingsDBWriter):
    """Manual historical holdings backfill.

    iShares ETFs use the iShares historical asOfDate endpoint. Non-iShares ETFs
    use official SEC N-PORT quarterly/monthly filings when their N-PORT target
    mapping is known.
    """

    def __init__(self, symbols: Optional[List[str]] = None):
        super().__init__()
        self.manager = ETFManager(LongPortService.get_instance())
        self.fetchers = self.manager.fetchers
        self.symbols = symbols or list(self.fetchers.keys())
        self.quote_service = QuoteService(LongPortService.get_instance())
        self.calculator = ETFFearGreedCloneCalculator()

    def close(self):
        try:
            self.manager.db_session.close()
        finally:
            super().close()

    def backfill(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> Dict[str, object]:
        start_date = start_date or DEFAULT_HISTORICAL_START_DATE
        end_date = end_date or date.today()
        ishares_symbols = self._ishares_symbols()
        nport_symbols = self._nport_symbols()

        ishares_result = self._backfill_ishares(ishares_symbols, start_date, end_date)
        nport_results = self._backfill_nport(nport_symbols, start_date, end_date)

        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "ishares": ishares_result,
            "nport": nport_results,
            "saved": ishares_result.get("saved", 0)
            + sum(int(item.get("saved_rows") or 0) for item in nport_results),
            "skipped": ishares_result.get("skipped", 0)
            + sum(int(item.get("skipped_invalid_dates") or 0) for item in nport_results),
            "errors": ishares_result.get("errors", []),
        }

    def _ishares_symbols(self) -> List[str]:
        return [
            symbol for symbol in self.symbols
            if isinstance(self.fetchers.get(symbol), ISharesETFFetcher)
            and symbol in self.calculator.ishares_fetcher.ETF_CONFIGS
        ]

    def _nport_symbols(self) -> List[str]:
        return [
            symbol for symbol in self.symbols
            if not isinstance(self.fetchers.get(symbol), ISharesETFFetcher)
            and symbol in TARGET_ETFS
        ]

    def _backfill_ishares(
        self,
        symbols: List[str],
        start_date: date,
        end_date: date,
    ) -> Dict[str, object]:
        if not symbols:
            return {"symbols": [], "saved": 0, "skipped": 0, "errors": []}

        trading_dates = self._get_spy_trading_dates(start_date, end_date)
        saved = 0
        skipped = 0
        errors = []

        for symbol in symbols:
            for trading_day in trading_dates:
                try:
                    payload = self.calculator._fetch_ishares_holdings_json(symbol, trading_day)
                    holdings = self.calculator._parse_ishares_holdings_json(payload)
                    saved += self._save_holdings(symbol, trading_day, holdings)
                    self.db.commit()
                except Exception as exc:
                    self.db.rollback()
                    errors.append({
                        "symbol": symbol,
                        "date": trading_day.isoformat(),
                        "error": str(exc),
                    })
                    skipped += 1
                    self.logger.error(
                        "Failed to backfill iShares holdings for %s on %s: %s",
                        symbol,
                        trading_day,
                        exc,
                    )

        return {
            "symbols": symbols,
            "start_date": trading_dates[0].isoformat() if trading_dates else None,
            "end_date": trading_dates[-1].isoformat() if trading_dates else None,
            "saved": saved,
            "skipped": skipped,
            "errors": errors,
        }

    def _backfill_nport(
        self,
        symbols: List[str],
        start_date: date,
        end_date: date,
    ) -> List[Dict[str, object]]:
        if not symbols:
            return []

        start_year, start_quarter = self._quarter_for_date(start_date)
        end_year, end_quarter = self._quarter_for_date(end_date)
        importer = ETFNPortHoldingsImporter(symbols=symbols)
        try:
            return importer.import_range(
                start_year,
                start_quarter,
                end_year,
                end_quarter,
                allow_missing=True,
            )
        finally:
            importer.close()

    def _get_spy_trading_dates(
        self,
        start_date: date,
        end_date: date,
    ) -> List[date]:
        klines = self.quote_service.get_klines(
            "SPY.US",
            start_date=start_date,
            end_date=end_date,
        )
        dates = []
        for item in klines or []:
            timestamp = item.get("timestamp")
            trading_day = timestamp.date() if hasattr(timestamp, "date") else timestamp
            if isinstance(trading_day, datetime):
                trading_day = trading_day.date()
            if isinstance(trading_day, pd.Timestamp):
                trading_day = trading_day.date()
            if isinstance(trading_day, date):
                dates.append(trading_day)
        return sorted(set(dates))

    @staticmethod
    def _quarter_for_date(value: date):
        return value.year, (value.month - 1) // 3 + 1
