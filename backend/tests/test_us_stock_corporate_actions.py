from datetime import date

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from src.core.database import Base, Session, USStockCorporateAction
from src.core.services.longport import LongPortService
from src.core.services.us_stock_corporate_actions import (
    adjust_or_reject_evc_values,
    cumulative_action_ratio,
    detect_corporate_actions,
)


def _bar(day, close):
    return {"timestamp": date.fromisoformat(day), "close": close}


def test_detects_forward_split_from_raw_and_forward_adjusted_bars():
    raw = [_bar("2026-04-27", 5100), _bar("2026-04-28", 5200), _bar("2026-04-29", 174)]
    forward = [_bar("2026-04-27", 170), _bar("2026-04-28", 5200 / 30), _bar("2026-04-29", 174)]

    actions = detect_corporate_actions(forward, raw)

    assert len(actions) == 1
    assert actions[0].effective_date == date(2026, 4, 29)
    assert actions[0].action_ratio == 30.0
    assert actions[0].action_type == "split"


def test_detects_reverse_split():
    raw = [_bar("2026-06-01", 2), _bar("2026-06-02", 21)]
    forward = [_bar("2026-06-01", 20), _bar("2026-06-02", 21)]

    actions = detect_corporate_actions(forward, raw)

    assert len(actions) == 1
    assert actions[0].action_ratio == pytest.approx(0.1)
    assert actions[0].action_type == "reverse_split"


def test_dividend_adjustment_is_not_classified_as_split():
    raw = [_bar("2026-06-01", 100), _bar("2026-06-02", 99)]
    forward = [_bar("2026-06-01", 99), _bar("2026-06-02", 99)]
    assert detect_corporate_actions(forward, raw) == []


def test_cached_split_normalizes_stale_evc_values():
    symbol = "TESTSPLIT.US"
    db = Session()
    try:
        db.query(USStockCorporateAction).filter(USStockCorporateAction.symbol == symbol).delete()
        db.add(USStockCorporateAction(
            symbol=symbol,
            effective_date=date(2026, 4, 29),
            action_ratio=30.0,
            action_type="split",
            source="test",
            confidence=1.0,
        ))
        db.commit()

        result = adjust_or_reject_evc_values(
            db,
            object(),
            symbol=symbol,
            price=174.0,
            valuation_date=date(2026, 2, 19),
            basis_date=date(2026, 4, 30),
            values={
                "fair_value_lo": 4766.6496,
                "fair_value_hi": 5631.8727,
                "forward_next_fy_lo": 6000.0,
                "forward_next_fy_hi": 6600.0,
            },
        )

        assert result.action_ratio == 30.0
        assert result.values is not None
        assert result.values["fair_value_lo"] == pytest.approx(158.88832)
        assert result.values["fair_value_hi"] == pytest.approx(187.72909)
        assert cumulative_action_ratio(
            db, symbol, date(2026, 2, 19), date(2026, 4, 30)
        ) == 30.0
    finally:
        db.query(USStockCorporateAction).filter(USStockCorporateAction.symbol == symbol).delete()
        db.commit()
        Session.remove()


def test_cached_split_does_not_double_adjust_already_normalized_evc_values():
    symbol = "TESTALREADYADJUSTED.US"
    db = Session()
    try:
        db.query(USStockCorporateAction).filter(USStockCorporateAction.symbol == symbol).delete()
        db.add(USStockCorporateAction(
            symbol=symbol,
            effective_date=date(2026, 4, 29),
            action_ratio=30.0,
            action_type="split",
            source="test",
            confidence=1.0,
        ))
        db.commit()
        result = adjust_or_reject_evc_values(
            db,
            object(),
            symbol=symbol,
            price=174.0,
            valuation_date=date(2026, 2, 19),
            basis_date=date(2026, 4, 30),
            values={"fair_value_lo": 160.0, "fair_value_hi": 190.0},
        )
        assert result.action_ratio == 1.0
        assert result.values == {"fair_value_lo": 160.0, "fair_value_hi": 190.0}
    finally:
        db.query(USStockCorporateAction).filter(USStockCorporateAction.symbol == symbol).delete()
        db.commit()
        Session.remove()


def test_hard_outlier_is_isolated_when_split_cannot_be_confirmed():
    class EmptyQuotes:
        def get_klines(self, *args, **kwargs):
            return []

    symbol = "TESTUNRESOLVED.US"
    db = Session()
    try:
        db.query(USStockCorporateAction).filter(USStockCorporateAction.symbol == symbol).delete()
        db.commit()
        result = adjust_or_reject_evc_values(
            db,
            EmptyQuotes(),
            symbol=symbol,
            price=174.0,
            valuation_date=date(2026, 2, 19),
            basis_date=date(2026, 4, 30),
            values={"fair_value_lo": 4766.0, "fair_value_hi": 5632.0},
        )
        assert result.values is None
        assert result.reason.startswith("unresolved_fair_value_ratio:")
    finally:
        Session.remove()


def test_longport_accepts_raw_and_forward_adjustment_names():
    from longport.openapi import AdjustType

    assert LongPortService._resolve_adjust_type("forward") == AdjustType.ForwardAdjust
    assert LongPortService._resolve_adjust_type("none") == AdjustType.NoAdjust
    with pytest.raises(ValueError):
        LongPortService._resolve_adjust_type("mystery")


def test_existing_sqlite_database_automatically_gets_corporate_action_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE stock_evc (symbol VARCHAR, date DATE)"))

    Base.metadata.create_all(engine, tables=[USStockCorporateAction.__table__])
    Base.metadata.create_all(engine, tables=[USStockCorporateAction.__table__])

    assert inspect(engine).has_table("us_stock_corporate_actions")
    LocalSession = sessionmaker(bind=engine)
    db = LocalSession()
    try:
        db.add(USStockCorporateAction(
            symbol="BKNG.US",
            effective_date=date(2026, 4, 29),
            action_ratio=30.0,
            action_type="split",
            source="test",
            confidence=1.0,
        ))
        db.commit()
        assert db.query(USStockCorporateAction).count() == 1
    finally:
        db.close()
