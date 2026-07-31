from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, func

from ..database import (
    AStockIndexValuationSnapshot,
    ETFFearGreedCloneHistory,
    ETFFearGreedCloneHolding,
    Session,
    StockEVC,
)


def _positive_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def calculate_weighted_index_valuation(
    *,
    index_level: Any,
    holdings: Iterable[Any],
    valuations: Dict[str, Any],
) -> Dict[str, Any]:
    """Aggregate constituent fair-value multiples into index-level ranges."""
    level = _positive_number(index_level)
    total_weight = 0.0
    covered_weight = 0.0
    weighted_multiples = {
        "fair_value_lo": 0.0,
        "fair_value_hi": 0.0,
        "forward_next_fy_lo": 0.0,
        "forward_next_fy_hi": 0.0,
    }
    covered_count = 0
    valuation_dates: List[date] = []

    for holding in holdings:
        weight = _positive_number(getattr(holding, "weight", None))
        if weight is None:
            continue
        total_weight += weight
        valuation = valuations.get(str(getattr(holding, "holding_symbol", "")).upper())
        if valuation is None:
            continue
        price = _positive_number(getattr(valuation, "last_price", None))
        values = {
            key: _positive_number(getattr(valuation, key, None))
            for key in weighted_multiples
        }
        if price is None or any(value is None for value in values.values()):
            continue
        covered_weight += weight
        covered_count += 1
        valuation_dates.append(valuation.date)
        for key, value in values.items():
            weighted_multiples[key] += weight * value / price

    coverage_ratio = covered_weight / total_weight if total_weight > 0 else 0.0
    result = {
        "status": "available" if level is not None and covered_weight > 0 else "unavailable",
        "index_level": level,
        "constituent_count": sum(1 for _ in holdings) if isinstance(holdings, list) else None,
        "covered_count": covered_count,
        "total_weight": round(total_weight, 8),
        "covered_weight": round(covered_weight, 8),
        "coverage_ratio": round(coverage_ratio, 6),
        "valuation_date_min": min(valuation_dates).isoformat() if valuation_dates else None,
        "valuation_date_max": max(valuation_dates).isoformat() if valuation_dates else None,
        "method": "constituent_weighted_fair_value_multiple",
    }
    for key, weighted_multiple in weighted_multiples.items():
        result[key] = (
            round(level * weighted_multiple / covered_weight, 4)
            if level is not None and covered_weight > 0
            else None
        )
    if level is not None:
        current_mid = _midpoint(result["fair_value_lo"], result["fair_value_hi"])
        forward_mid = _midpoint(result["forward_next_fy_lo"], result["forward_next_fy_hi"])
        result["current_gap_pct"] = _gap_pct(current_mid, level)
        result["forward_gap_pct"] = _gap_pct(forward_mid, level)
        result["rating"] = _valuation_rating(level, result["fair_value_lo"], result["fair_value_hi"])
        result["forward_rating"] = _valuation_rating(
            level,
            result["forward_next_fy_lo"],
            result["forward_next_fy_hi"],
        )
    else:
        result.update(
            current_gap_pct=None,
            forward_gap_pct=None,
            rating="数据不足",
            forward_rating="数据不足",
        )
    return result


def calculate_a_stock_index_valuation(symbol: str) -> Dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    db = Session()
    try:
        latest = (
            db.query(ETFFearGreedCloneHistory)
            .filter(ETFFearGreedCloneHistory.symbol == normalized_symbol)
            .order_by(ETFFearGreedCloneHistory.date.desc())
            .first()
        )
        if latest is None:
            return {"status": "unavailable", "reason": "fear_greed_history_missing"}
        holdings = (
            db.query(ETFFearGreedCloneHolding)
            .filter(
                ETFFearGreedCloneHolding.symbol == normalized_symbol,
                ETFFearGreedCloneHolding.date == latest.date,
            )
            .all()
        )
        holding_symbols = {
            str(row.holding_symbol or "").strip().upper()
            for row in holdings
            if row.holding_symbol
        }
        if not holding_symbols:
            return {"status": "unavailable", "reason": "constituent_weights_missing"}

        latest_dates = (
            db.query(StockEVC.symbol.label("symbol"), func.max(StockEVC.date).label("date"))
            .filter(StockEVC.symbol.in_(holding_symbols))
            .group_by(StockEVC.symbol)
            .subquery()
        )
        valuation_rows = (
            db.query(StockEVC)
            .join(
                latest_dates,
                and_(
                    StockEVC.symbol == latest_dates.c.symbol,
                    StockEVC.date == latest_dates.c.date,
                ),
            )
            .all()
        )
        result = calculate_weighted_index_valuation(
            index_level=latest.etf_close,
            holdings=holdings,
            valuations={str(row.symbol).upper(): row for row in valuation_rows},
        )
        result["index_date"] = latest.date.isoformat()
        return result
    finally:
        Session.remove()


def refresh_a_stock_index_valuations(symbols: Iterable[str]) -> Dict[str, Any]:
    calculated = []
    errors = []
    for symbol in dict.fromkeys(str(item or "").strip().upper() for item in symbols):
        if not symbol:
            continue
        try:
            payload = calculate_a_stock_index_valuation(symbol)
            if payload.get("status") != "available" or not payload.get("index_date"):
                errors.append({"symbol": symbol, "error": payload.get("reason") or "valuation unavailable"})
                continue
            calculated.append((symbol, payload))
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})

    db = Session()
    try:
        for symbol, payload in calculated:
            snapshot_date = date.fromisoformat(payload["index_date"])
            db.merge(
                AStockIndexValuationSnapshot(
                    symbol=symbol,
                    date=snapshot_date,
                    payload=payload,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        Session.remove()
    return {
        "saved": len(calculated),
        "results": [
            {"symbol": symbol, "date": payload["index_date"]}
            for symbol, payload in calculated
        ],
        "errors": errors,
    }


def load_a_stock_index_valuation(symbol: str) -> Dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    db = Session()
    try:
        snapshot = (
            db.query(AStockIndexValuationSnapshot)
            .filter(AStockIndexValuationSnapshot.symbol == normalized_symbol)
            .order_by(AStockIndexValuationSnapshot.date.desc())
            .first()
        )
        if snapshot is None:
            return {"status": "unavailable", "reason": "valuation_snapshot_missing"}
        return dict(snapshot.payload or {})
    finally:
        Session.remove()


def _midpoint(low: Optional[float], high: Optional[float]) -> Optional[float]:
    if low is None or high is None:
        return None
    return (low + high) / 2.0


def _gap_pct(fair_value: Optional[float], level: float) -> Optional[float]:
    if fair_value is None or level <= 0:
        return None
    return round((fair_value / level - 1.0) * 100.0, 2)


def _valuation_rating(level: float, low: Optional[float], high: Optional[float]) -> str:
    if low is None or high is None:
        return "数据不足"
    if level < low:
        return "低估"
    if level > high:
        return "高估"
    return "合理"
