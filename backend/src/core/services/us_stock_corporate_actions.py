"""Infer US stock splits/reverse splits from raw and forward-adjusted LongPort bars."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session as ORMSession

from ..database import USStockCorporateAction, get_db_ctx


logger = logging.getLogger(__name__)

# Deliberately conservative: ordinary dividends must not be classified as splits.
COMMON_SPLIT_RATIOS: Sequence[float] = (1.5, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 30.0, 50.0, 100.0)
SPLIT_RATIO_RELATIVE_TOLERANCE = 0.08
EVC_HARD_RATIO_MIN = 0.2
EVC_HARD_RATIO_MAX = 5.0


@dataclass(frozen=True)
class DetectedCorporateAction:
    effective_date: date
    action_ratio: float
    action_type: str
    confidence: float


@dataclass(frozen=True)
class EVCAdjustmentResult:
    values: Optional[Dict[str, float]]
    action_ratio: float
    detected_actions: Sequence[DetectedCorporateAction]
    reason: Optional[str] = None


def evc_values_need_review(price: Any, fair_value_lo: Any, fair_value_hi: Any) -> bool:
    current_price = _positive_float(price)
    fair_lo = _positive_float(fair_value_lo)
    fair_hi = _positive_float(fair_value_hi)
    if current_price is None or fair_lo is None or fair_hi is None:
        return False
    fair_ratio = ((fair_lo + fair_hi) / 2.0) / current_price
    return fair_ratio < EVC_HARD_RATIO_MIN or fair_ratio > EVC_HARD_RATIO_MAX


def _bar_date(row: Dict[str, Any]) -> Optional[date]:
    value = row.get("timestamp") or row.get("trade_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _canonical_action_ratio(observed_ratio: float) -> Optional[tuple[float, float]]:
    candidates = tuple(COMMON_SPLIT_RATIOS) + tuple(1.0 / ratio for ratio in COMMON_SPLIT_RATIOS)
    nearest = min(candidates, key=lambda candidate: abs(observed_ratio / candidate - 1.0))
    relative_error = abs(observed_ratio / nearest - 1.0)
    if relative_error > SPLIT_RATIO_RELATIVE_TOLERANCE:
        return None
    confidence = max(0.0, 1.0 - relative_error / SPLIT_RATIO_RELATIVE_TOLERANCE)
    return nearest, confidence


def detect_corporate_actions(
    forward_rows: Iterable[Dict[str, Any]],
    raw_rows: Iterable[Dict[str, Any]],
) -> List[DetectedCorporateAction]:
    """Detect adjustment-factor steps; action_ratio > 1 is a forward split."""
    forward = {
        row_date: close
        for row in forward_rows
        if (row_date := _bar_date(row)) is not None
        and (close := _positive_float(row.get("close"))) is not None
    }
    raw = {
        row_date: close
        for row in raw_rows
        if (row_date := _bar_date(row)) is not None
        and (close := _positive_float(row.get("close"))) is not None
    }
    factors = [
        (row_date, raw[row_date] / forward[row_date])
        for row_date in sorted(set(forward) & set(raw))
    ]
    actions: List[DetectedCorporateAction] = []
    for (previous_date, previous_factor), (current_date, current_factor) in zip(factors, factors[1:]):
        observed_ratio = previous_factor / current_factor
        canonical = _canonical_action_ratio(observed_ratio)
        if canonical is None:
            continue
        action_ratio, confidence = canonical
        actions.append(DetectedCorporateAction(
            effective_date=current_date,
            action_ratio=action_ratio,
            action_type="split" if action_ratio > 1 else "reverse_split",
            confidence=confidence,
        ))
    return actions


def save_corporate_actions(symbol: str, actions: Iterable[DetectedCorporateAction]) -> int:
    normalized_symbol = str(symbol or "").strip().upper()
    snapshots = list(actions)
    if not normalized_symbol or not snapshots:
        return 0
    with get_db_ctx() as db:
        for action in snapshots:
            db.merge(USStockCorporateAction(
                symbol=normalized_symbol,
                effective_date=action.effective_date,
                action_ratio=action.action_ratio,
                action_type=action.action_type,
                source="longport_raw_vs_forward",
                confidence=action.confidence,
                detected_at=datetime.now(),
            ))
    return len(snapshots)


def cumulative_action_ratio(
    db: ORMSession,
    symbol: str,
    source_date: date,
    basis_date: date,
) -> float:
    rows = (
        db.query(USStockCorporateAction.action_ratio)
        .filter(
            USStockCorporateAction.symbol == str(symbol or "").strip().upper(),
            USStockCorporateAction.effective_date > source_date,
            USStockCorporateAction.effective_date <= basis_date,
        )
        .order_by(USStockCorporateAction.effective_date.asc())
        .all()
    )
    ratio = 1.0
    for (value,) in rows:
        number = _positive_float(value)
        if number is not None:
            ratio *= number
    return ratio


def fetch_and_detect_corporate_actions(
    quote_service: Any,
    symbol: str,
    source_date: date,
    basis_date: date,
    *,
    save: bool = True,
) -> List[DetectedCorporateAction]:
    start_date = source_date - timedelta(days=10)
    forward_rows = quote_service.get_klines(
        symbol,
        start_date=start_date,
        end_date=basis_date,
        adjust_type="forward",
    )
    raw_rows = quote_service.get_klines(
        symbol,
        start_date=start_date,
        end_date=basis_date,
        adjust_type="none",
    )
    actions = detect_corporate_actions(forward_rows, raw_rows)
    if save:
        save_corporate_actions(symbol, actions)
    return actions


def adjust_or_reject_evc_values(
    db: ORMSession,
    quote_service: Any,
    *,
    symbol: str,
    price: Any,
    valuation_date: Optional[date],
    basis_date: date,
    values: Dict[str, Any],
) -> EVCAdjustmentResult:
    """Normalize stale pre-split per-share EVC values, or reject an unresolved hard outlier."""
    current_price = _positive_float(price)
    numeric_values = {key: _positive_float(value) for key, value in values.items()}
    fair_lo = numeric_values.get("fair_value_lo")
    fair_hi = numeric_values.get("fair_value_hi")
    if current_price is None or fair_lo is None or fair_hi is None:
        return EVCAdjustmentResult(values=None, action_ratio=1.0, detected_actions=(), reason="missing_price_or_fair_value")

    raw_fair_ratio = ((fair_lo + fair_hi) / 2.0) / current_price
    hard_outlier = raw_fair_ratio < EVC_HARD_RATIO_MIN or raw_fair_ratio > EVC_HARD_RATIO_MAX
    if valuation_date is None:
        return EVCAdjustmentResult(
            values=None if hard_outlier else {key: value for key, value in numeric_values.items() if value is not None},
            action_ratio=1.0,
            detected_actions=(),
            reason="hard_outlier_without_valuation_date" if hard_outlier else None,
        )

    ratio = cumulative_action_ratio(db, symbol, valuation_date, basis_date)
    detected_actions: Sequence[DetectedCorporateAction] = ()
    if ratio == 1.0 and hard_outlier:
        try:
            detected_actions = fetch_and_detect_corporate_actions(
                quote_service,
                symbol,
                valuation_date,
                basis_date,
                save=False,
            )
            for action in detected_actions:
                db.merge(USStockCorporateAction(
                    symbol=str(symbol or "").strip().upper(),
                    effective_date=action.effective_date,
                    action_ratio=action.action_ratio,
                    action_type=action.action_type,
                    source="longport_raw_vs_forward",
                    confidence=action.confidence,
                    detected_at=datetime.now(),
                ))
                if valuation_date < action.effective_date <= basis_date:
                    ratio *= action.action_ratio
        except Exception as exc:  # upstream confirmation failure must degrade safely
            logger.warning("Confirm US corporate action failed for %s: %s", symbol, exc)

    adjusted_values = {
        key: value / ratio
        for key, value in numeric_values.items()
        if value is not None
    }
    adjusted_fair_lo = adjusted_values.get("fair_value_lo")
    adjusted_fair_hi = adjusted_values.get("fair_value_hi")
    adjusted_fair_ratio = (
        ((adjusted_fair_lo + adjusted_fair_hi) / 2.0) / current_price
        if adjusted_fair_lo is not None and adjusted_fair_hi is not None
        else None
    )
    raw_ratio_is_valid = EVC_HARD_RATIO_MIN <= raw_fair_ratio <= EVC_HARD_RATIO_MAX
    adjusted_ratio_is_valid = (
        adjusted_fair_ratio is not None
        and EVC_HARD_RATIO_MIN <= adjusted_fair_ratio <= EVC_HARD_RATIO_MAX
    )
    # The upstream may already have normalized its fair values without changing fair_value_date.
    # In that case a cached split must not be applied a second time.
    if ratio != 1.0 and raw_ratio_is_valid and (
        not adjusted_ratio_is_valid
        or abs(math.log(raw_fair_ratio)) <= abs(math.log(adjusted_fair_ratio))
    ):
        return EVCAdjustmentResult(
            values={key: value for key, value in numeric_values.items() if value is not None},
            action_ratio=1.0,
            detected_actions=detected_actions,
        )
    if (
        not adjusted_ratio_is_valid
    ):
        return EVCAdjustmentResult(
            values=None,
            action_ratio=ratio,
            detected_actions=detected_actions,
            reason=f"unresolved_fair_value_ratio:{adjusted_fair_ratio}",
        )
    return EVCAdjustmentResult(
        values=adjusted_values,
        action_ratio=ratio,
        detected_actions=detected_actions,
    )
