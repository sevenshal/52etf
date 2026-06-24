from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBase,
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    ExternalTradingValuationSimPositionState,
)
from src.core.services.external_trading_ledger import STRATEGY_VALUATION_SIM
from src.core.services.valuation_simulator import ValuationSimulationService


class ValuationSimulationExternalTargetsTest(TestCase):
    def _db_session(self):
        engine = create_engine("sqlite:///:memory:")
        ExternalTradingBase.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    def _config(self):
        return SimpleNamespace(
            id=7,
            account_id="acct",
            external_trading_account_id=2,
            live_sub_account_id=9,
            max_positions=5,
            ema_window=120,
            volume_lookback_days=20,
            volume_consecutive_days=3,
            trailing_stop_atr_window=20,
            trailing_stop_atr_multiple=2.5,
            stale_high_days=5,
        )

    def _seed_account(self, db, *, cash_available=1000.0):
        db.add(
            ExternalTradingAccount(
                id=2,
                account_id="acct",
                name="US",
                identifier="us-paper",
                market_type="US_STOCK",
                enabled=True,
            )
        )
        db.add(
            ExternalTradingSubAccount(
                id=9,
                account_id="acct",
                external_trading_account_id=2,
                name="valuation",
                strategy_type=STRATEGY_VALUATION_SIM,
                strategy_config_id=7,
                cash_available=cash_available,
                enabled=True,
            )
        )
        db.commit()

    def _position(self, symbol, quantity, price):
        return ExternalTradingLedgerPosition(
            account_id="acct",
            external_trading_account_id=2,
            sub_account_id=9,
            symbol=symbol,
            quantity=quantity,
            available_quantity=quantity,
            avg_cost=price,
            market_price=price,
            market_value=quantity * price,
        )

    def _state(self, symbol, *, highest_price, days_without_high=0, last_trade_date=None):
        return ExternalTradingValuationSimPositionState(
            account_id="acct",
            external_trading_account_id=2,
            sub_account_id=9,
            config_id=7,
            symbol=symbol,
            highest_price=highest_price,
            highest_price_date=last_trade_date,
            days_without_high=days_without_high,
            last_trade_date=last_trade_date,
        )

    def test_keeps_existing_position_and_buys_only_with_cash(self):
        db = self._db_session()
        self._seed_account(db, cash_available=1000.0)
        db.add(self._position("AAPL.US", 10, 110.0))
        db.commit()

        payload = {
            "candidates": [
                {"symbol": "MSFT.US", "price": 100.0, "volume_ratio": 2.0},
            ],
            "latest_prices": {
                "AAPL.US": {"close": 110.0, "high": 110.0},
            },
        }

        with patch("src.core.services.valuation_simulator._build_candidate_payload", return_value=payload):
            result = ValuationSimulationService(SimpleNamespace())._sync_target_positions(
                db,
                self._config(),
                date(2026, 6, 5),
            )

        targets = {
            row.symbol: row.target_quantity
            for row in db.query(ExternalTradingTargetPosition).all()
        }
        self.assertEqual(10, targets["AAPL.US"])
        self.assertEqual(10, targets["MSFT.US"])
        state = db.query(ExternalTradingValuationSimPositionState).filter_by(symbol="AAPL.US").one()
        self.assertEqual(110.0, state.highest_price)
        self.assertEqual(0, state.days_without_high)
        self.assertEqual(1, result["buy_count"])
        self.assertEqual(0, result["sell_count"])

    def test_stop_and_stale_replacement_create_sell_and_replacement_targets(self):
        db = self._db_session()
        self._seed_account(db, cash_available=100.0)
        db.add_all([
            self._position("AAA.US", 10, 100.0),
            self._position("BBB.US", 10, 100.0),
            self._position("CCC.US", 10, 100.0),
            self._position("DDD.US", 10, 100.0),
            self._position("EEE.US", 10, 100.0),
            self._state("AAA.US", highest_price=120.0, last_trade_date=date(2026, 6, 4)),
            self._state("BBB.US", highest_price=105.0, days_without_high=5, last_trade_date=date(2026, 6, 4)),
        ])
        db.commit()

        payload = {
            "candidates": [
                {"symbol": "FFF.US", "price": 100.0, "volume_ratio": 2.5},
                {"symbol": "GGG.US", "price": 100.0, "volume_ratio": 2.0},
            ],
            "latest_prices": {
                "AAA.US": {"close": 113.0, "high": 113.0, "atr": 2.0},
                "BBB.US": {"close": 100.0, "high": 100.0},
                "CCC.US": {"close": 100.0, "high": 100.0},
                "DDD.US": {"close": 100.0, "high": 100.0},
                "EEE.US": {"close": 100.0, "high": 100.0},
            },
        }

        with patch("src.core.services.valuation_simulator._build_candidate_payload", return_value=payload):
            result = ValuationSimulationService(SimpleNamespace())._sync_target_positions(
                db,
                self._config(),
                date(2026, 6, 5),
            )

        targets = {
            row.symbol: row.target_quantity
            for row in db.query(ExternalTradingTargetPosition).all()
        }
        self.assertEqual(0, targets["AAA.US"])
        self.assertEqual(0, targets["BBB.US"])
        self.assertEqual(10, targets["CCC.US"])
        self.assertEqual(10, targets["DDD.US"])
        self.assertEqual(10, targets["EEE.US"])
        self.assertEqual(11, targets["FFF.US"])
        self.assertEqual(11, targets["GGG.US"])
        self.assertEqual(2, result["buy_count"])
        self.assertEqual(2, result["sell_count"])
