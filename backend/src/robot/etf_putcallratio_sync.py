import ast
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.database import ETFOptionExpiration, ETFPutCallRatio, Session
from ..core.services.barchat import BarchartService


def normalize_symbol(symbol: str) -> str:
    symbol = str(symbol or "").strip().upper()
    if symbol.startswith("US."):
        symbol = symbol[3:]
    if symbol.endswith(".US"):
        symbol = symbol[:-3]
    return symbol


def parse_etf_manager_symbols(etf_manager_path: Optional[Path] = None) -> List[str]:
    """Parse regular and leveraged ETF symbols from etf_manager.py."""
    if etf_manager_path is None:
        etf_manager_path = Path(__file__).resolve().parent / "etf_manager.py"

    tree = ast.parse(etf_manager_path.read_text(encoding="utf-8"), filename=str(etf_manager_path))
    symbols = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue

        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "LEVERAGED_ETF_MAP"
                and isinstance(node.value, ast.Dict)
            ):
                for key, value in zip(node.value.keys, node.value.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        symbols.add(key.value)
                    if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
                        first = value.elts[0]
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            symbols.add(first.value)

            if (
                isinstance(target, ast.Attribute)
                and target.attr == "fetchers"
                and isinstance(node.value, ast.Dict)
            ):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        symbols.add(key.value)

    return sorted(symbol for symbol in {normalize_symbol(item) for item in symbols} if symbol)


class BarchartETFPutCallRatioSync:
    """Sync Barchart ETF option history and current expiration snapshots."""

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        timeout: float = 30.0,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.symbols = parse_etf_manager_symbols() if symbols is None else [
            normalize_symbol(symbol) for symbol in symbols
        ]
        self.db = Session()
        self.barchart = BarchartService(timeout=timeout)

    def close(self):
        self.barchart.close()
        self.db.close()
        Session.remove()

    def sync_all(
        self,
        full: bool = False,
        page_limit: int = 1000,
        recent_limit: int = 10,
        expirations_limit: int = 100,
        sleep_seconds: float = 0.2,
        snapshot_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        if page_limit < 1:
            raise ValueError("page_limit must be positive")
        if recent_limit < 1:
            raise ValueError("recent_limit must be positive")
        if expirations_limit < 1:
            raise ValueError("expirations_limit must be positive")

        snapshot_date = snapshot_date or date.today()
        saved_history = 0
        saved_expirations = 0
        errors = []
        details = {}

        for symbol in self.symbols:
            symbol = normalize_symbol(symbol)
            if not symbol:
                continue

            try:
                history_rows = self.barchart.get_options_history(
                    symbol=symbol,
                    full=full,
                    page_limit=page_limit,
                    recent_limit=recent_limit,
                    sleep_seconds=sleep_seconds,
                )
                expiration_rows = self.barchart.get_options_expirations(
                    symbol=symbol,
                    page_limit=expirations_limit,
                    sleep_seconds=sleep_seconds,
                )

                history_saved = self.save_history_rows(symbol, history_rows)
                expirations_saved = self.save_expiration_rows(symbol, snapshot_date, expiration_rows)
                self.db.commit()
                self.db.expunge_all()

                saved_history += history_saved
                saved_expirations += expirations_saved
                details[symbol] = {
                    "history_fetched": len(history_rows),
                    "history_saved": history_saved,
                    "history_latest": history_rows[0].get("date") if history_rows else None,
                    "history_oldest": history_rows[-1].get("date") if history_rows else None,
                    "expirations_fetched": len(expiration_rows),
                    "expirations_saved": expirations_saved,
                }
                self.logger.info(
                    "Saved Barchart option rows for %s, history=%s expirations=%s full=%s",
                    symbol,
                    history_saved,
                    expirations_saved,
                    full,
                )
            except Exception as exc:
                self.db.rollback()
                self.db.expunge_all()
                errors.append({"symbol": symbol, "error": str(exc)})
                self.logger.error("Failed to sync Barchart option rows for %s: %s", symbol, exc)

        return {
            "mode": "full" if full else "recent",
            "symbols": len(self.symbols),
            "saved_history": saved_history,
            "saved_expirations": saved_expirations,
            "errors": errors,
            "details": details,
        }

    def save_history_rows(self, symbol: str, rows: List[Dict[str, Any]]) -> int:
        now = datetime.now()
        saved = 0
        parsed_rows = []

        for item in rows:
            record_date = self._parse_date(item.get("date"))
            if not record_date:
                continue
            parsed_rows.append((record_date, item))

        if not parsed_rows:
            return 0

        existing_records = {
            record.date: record
            for record in self.db.query(ETFPutCallRatio).filter(
                ETFPutCallRatio.symbol == symbol,
                ETFPutCallRatio.date.in_([record_date for record_date, _ in parsed_rows]),
            ).all()
        }

        for record_date, item in parsed_rows:
            values = {
                "put_volume": self._to_int_or_none(item.get("putVolume")),
                "call_volume": self._to_int_or_none(item.get("callVolume")),
                "total_volume": self._to_int_or_none(item.get("totalVolume")),
                "put_call_volume_ratio": self._to_float(item.get("putCallVolumeRatio")),
                "put_open_interest": self._to_int_or_none(item.get("putOpenInterest")),
                "call_open_interest": self._to_int_or_none(item.get("callOpenInterest")),
                "total_open_interest": self._to_int_or_none(item.get("totalOpenInterest")),
                "put_call_open_interest_ratio": self._to_float(item.get("putCallOpenInterestRatio")),
            }

            record = existing_records.get(record_date)
            if record:
                for key, value in values.items():
                    setattr(record, key, value)
                record.updated_at = now
            else:
                self.db.add(
                    ETFPutCallRatio(
                        symbol=symbol,
                        date=record_date,
                        created_at=now,
                        updated_at=now,
                        **values,
                    )
                )
            saved += 1

        return saved

    def save_expiration_rows(
        self,
        symbol: str,
        snapshot_date: date,
        rows: List[Dict[str, Any]],
    ) -> int:
        now = datetime.now()
        saved = 0
        parsed_rows = []

        for item in rows:
            expiration_date = self._parse_date(item.get("expirationDate"))
            if not expiration_date:
                continue
            parsed_rows.append((expiration_date, item))

        if not parsed_rows:
            return 0

        returned_expiration_dates = {expiration_date for expiration_date, _ in parsed_rows}
        existing_records = {
            record.expiration_date: record
            for record in self.db.query(ETFOptionExpiration).filter(
                ETFOptionExpiration.symbol == symbol,
                ETFOptionExpiration.snapshot_date == snapshot_date,
                ETFOptionExpiration.expiration_date.in_(returned_expiration_dates),
            ).all()
        }

        for expiration_date, item in parsed_rows:
            values = {
                "expiration_type": item.get("expirationType"),
                "days_to_expiration": self._to_int_or_none(item.get("daysToExpiration")),
                "put_volume": self._to_int_or_none(item.get("putVolume")),
                "call_volume": self._to_int_or_none(item.get("callVolume")),
                "total_volume": self._to_int_or_none(item.get("totalVolume")),
                "put_call_volume_ratio": self._to_float(item.get("putCallVolumeRatio")),
                "put_open_interest": self._to_int_or_none(item.get("putOpenInterest")),
                "call_open_interest": self._to_int_or_none(item.get("callOpenInterest")),
                "total_open_interest": self._to_int_or_none(item.get("totalOpenInterest")),
                "put_call_open_interest_ratio": self._to_float(item.get("putCallOpenInterestRatio")),
                "average_volatility": self._to_float(item.get("averageVolatility")),
                "symbol_type": item.get("symbolType"),
                "last_price": self._to_float(item.get("lastPrice")),
            }

            record = existing_records.get(expiration_date)
            if record:
                for key, value in values.items():
                    setattr(record, key, value)
                record.updated_at = now
            else:
                self.db.add(
                    ETFOptionExpiration(
                        symbol=symbol,
                        snapshot_date=snapshot_date,
                        expiration_date=expiration_date,
                        created_at=now,
                        updated_at=now,
                        **values,
                    )
            )
            saved += 1

        self.db.query(ETFOptionExpiration).filter(
            ETFOptionExpiration.symbol == symbol,
            ETFOptionExpiration.snapshot_date == snapshot_date,
            ~ETFOptionExpiration.expiration_date.in_(returned_expiration_dates),
        ).delete(synchronize_session=False)

        return saved

    @staticmethod
    def _parse_date(value: Any):
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        text = str(value).strip().replace(",", "").replace("%", "")
        if not text or text in {"-", "N/A", "null"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _to_int_or_none(value: Any) -> Optional[int]:
        if value is None:
            return None
        text = str(value).strip().replace(",", "")
        if not text or text in {"-", "N/A", "null"}:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
