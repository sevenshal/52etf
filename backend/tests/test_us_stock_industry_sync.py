from datetime import date
from tempfile import TemporaryDirectory
from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base, ETFHolding, USStockIndustrySnapshot
from src.robot.us_stock_industry_sync import USStockIndustrySync


class _FakeFMPService:
    def __init__(self, profiles=None):
        self.profiles = profiles or {}
        self.calls = []

    def get_company_profile(self, symbol):
        self.calls.append(symbol)
        return self.profiles.get(symbol)


class USStockIndustrySyncTest(TestCase):
    def _session_factory(self, tmpdir):
        engine = create_engine(f"sqlite:///{tmpdir}/industry.db")
        Base.metadata.create_all(
            engine,
            tables=[ETFHolding.__table__, USStockIndustrySnapshot.__table__],
        )
        return sessionmaker(bind=engine)

    def test_known_empty_profile_symbols_are_skipped_without_fmp_calls(self):
        with TemporaryDirectory() as tmpdir:
            session_factory = self._session_factory(tmpdir)
            db = session_factory()
            try:
                db.add_all(
                    [
                        ETFHolding(
                            etf_symbol="SPY.US",
                            symbol="EUR.US",
                            date=date(2026, 6, 20),
                            asset_class="Equity",
                            weight=0.1,
                        ),
                        ETFHolding(
                            etf_symbol="SPY.US",
                            symbol="NQU6.US",
                            date=date(2026, 6, 20),
                            asset_class="Equity",
                            weight=0.1,
                        ),
                    ]
                )
                db.commit()

                fmp = _FakeFMPService()
                result = USStockIndustrySync(db=db, fmp_service=fmp).sync_spy_qqq(
                    candidate_etfs=["SPY.US"],
                    force=True,
                    snapshot_date=date(2026, 6, 20),
                )

                self.assertEqual([], fmp.calls)
                self.assertEqual(2, result["symbols"])
                self.assertEqual(0, result["target_symbols"])
                self.assertEqual(2, result["skipped_profile_unavailable"])
                self.assertEqual(["EUR.US", "NQU6.US"], result["skipped_profile_unavailable_symbols"])
                self.assertEqual(0, result["remaining"])
                self.assertEqual([], result["errors"])
            finally:
                db.close()

    def test_empty_fmp_profile_response_is_skipped_not_error(self):
        with TemporaryDirectory() as tmpdir:
            session_factory = self._session_factory(tmpdir)
            db = session_factory()
            try:
                db.add(
                    ETFHolding(
                        etf_symbol="SPY.US",
                        symbol="NQU7.US",
                        date=date(2026, 6, 20),
                        asset_class="Equity",
                        weight=0.1,
                    )
                )
                db.commit()

                fmp = _FakeFMPService(profiles={"NQU7": None})
                result = USStockIndustrySync(db=db, fmp_service=fmp).sync_spy_qqq(
                    candidate_etfs=["SPY.US"],
                    force=True,
                    snapshot_date=date(2026, 6, 20),
                )

                self.assertEqual(["NQU7"], fmp.calls)
                self.assertEqual(1, result["target_symbols"])
                self.assertEqual(0, result["saved"])
                self.assertEqual(1, result["skipped_profile_unavailable"])
                self.assertEqual(["NQU7.US"], result["skipped_profile_unavailable_symbols"])
                self.assertEqual(0, result["remaining"])
                self.assertEqual([], result["errors"])
            finally:
                db.close()
