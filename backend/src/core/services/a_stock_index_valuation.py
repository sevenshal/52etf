from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

from ..analytics_database import AnalyticsSession
from ..database import AStockIndexValuationSnapshot, ETFFearGreedCloneHistory, Session
from .a_stock_fear_greed_clone_service import AStockInnovation100FearGreedCloneCalculator
from .a_stock_consensus import load_a_stock_consensus_valuation_history_map


VALUATION_POSITION_MAX_WINDOW = 504
VALUATION_POSITION_SHORT_WINDOW = 252
VALUATION_POSITION_MIN_SAMPLES = 120
VALUATION_FULL_CONFIDENCE_REPORT_COUNT = 3


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
    current_weighted_multiples = {
        "fair_value_lo": 0.0,
        "fair_value_mid": 0.0,
        "fair_value_hi": 0.0,
    }
    forward_weighted_multiples = {
        "forward_next_fy_lo": 0.0,
        "forward_next_fy_mid": 0.0,
        "forward_next_fy_hi": 0.0,
    }
    covered_count = 0
    forward_covered_weight = 0.0
    effective_covered_weight = 0.0
    forward_effective_covered_weight = 0.0
    forward_covered_count = 0
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
        current_values = {
            key: _positive_number(getattr(valuation, key, None))
            for key in current_weighted_multiples
        }
        if price is None or any(value is None for value in current_values.values()):
            continue
        report_count = getattr(valuation, "target_report_count", None)
        confidence = (
            min(max(float(report_count), 0.0) / VALUATION_FULL_CONFIDENCE_REPORT_COUNT, 1.0)
            if report_count is not None
            else 1.0
        )
        effective_weight = weight * confidence
        if effective_weight <= 0:
            continue
        covered_weight += weight
        effective_covered_weight += effective_weight
        covered_count += 1
        valuation_dates.append(valuation.date)
        for key, value in current_values.items():
            current_weighted_multiples[key] += effective_weight * value / price
        forward_values = {
            key: _positive_number(getattr(valuation, key, None))
            for key in forward_weighted_multiples
        }
        if all(value is not None for value in forward_values.values()):
            forward_covered_weight += weight
            forward_effective_covered_weight += effective_weight
            forward_covered_count += 1
            for key, value in forward_values.items():
                forward_weighted_multiples[key] += effective_weight * value / price

    coverage_ratio = covered_weight / total_weight if total_weight > 0 else 0.0
    result = {
        "status": "available" if level is not None and covered_weight > 0 else "unavailable",
        "index_level": level,
        "constituent_count": sum(1 for _ in holdings) if isinstance(holdings, list) else None,
        "covered_count": covered_count,
        "forward_covered_count": forward_covered_count,
        "total_weight": round(total_weight, 8),
        "covered_weight": round(covered_weight, 8),
        "effective_covered_weight": round(effective_covered_weight, 8),
        "forward_covered_weight": round(forward_covered_weight, 8),
        "forward_effective_covered_weight": round(forward_effective_covered_weight, 8),
        "coverage_ratio": round(coverage_ratio, 6),
        "effective_coverage_ratio": round(
            effective_covered_weight / total_weight if total_weight > 0 else 0.0,
            6,
        ),
        "forward_coverage_ratio": round(
            forward_covered_weight / total_weight if total_weight > 0 else 0.0,
            6,
        ),
        "valuation_date_min": min(valuation_dates).isoformat() if valuation_dates else None,
        "valuation_date_max": max(valuation_dates).isoformat() if valuation_dates else None,
        "method": "constituent_weighted_fair_value_multiple",
    }
    for key, weighted_multiple in current_weighted_multiples.items():
        result[key] = (
            round(level * weighted_multiple / effective_covered_weight, 4)
            if level is not None and effective_covered_weight > 0
            else None
        )
    for key, weighted_multiple in forward_weighted_multiples.items():
        result[key] = (
            round(level * weighted_multiple / forward_effective_covered_weight, 4)
            if level is not None and forward_effective_covered_weight > 0
            else None
        )
    if level is not None:
        current_mid = result["fair_value_mid"]
        forward_mid = result["forward_next_fy_mid"]
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
        history_rows = (
            db.query(ETFFearGreedCloneHistory)
            .filter(ETFFearGreedCloneHistory.symbol == normalized_symbol)
            .order_by(ETFFearGreedCloneHistory.date.desc())
            .limit(VALUATION_POSITION_MAX_WINDOW)
            .all()
        )
        if not history_rows:
            return {"status": "unavailable", "reason": "fear_greed_history_missing"}
        history_rows.reverse()
        index_levels = {row.date: row.etf_close for row in history_rows}
        latest = SimpleNamespace(date=history_rows[-1].date, etf_close=history_rows[-1].etf_close)
    finally:
        Session.remove()

    calculator = AStockInnovation100FearGreedCloneCalculator(normalized_symbol)
    holdings_history, holdings_as_of_history = calculator.load_holdings_history(index_levels)
    holding_rows = holdings_history.get(latest.date, [])
    holdings_as_of = holdings_as_of_history.get(latest.date)
    holdings = [SimpleNamespace(holding_symbol=row["symbol"], **row) for row in holding_rows]
    holding_symbols = {
        str(row.get("symbol") or "").strip().upper()
        for rows in holdings_history.values()
        for row in rows
        if row.get("symbol")
    }
    if not holding_symbols:
        return {"status": "unavailable", "reason": "constituent_weights_missing"}

    analytics_db = AnalyticsSession()
    try:
        valuation_history = load_a_stock_consensus_valuation_history_map(
            analytics_db,
            holding_symbols,
            index_levels,
        )
        daily_results = []
        for trade_day, index_level in index_levels.items():
            day_holdings = [
                SimpleNamespace(holding_symbol=row["symbol"], **row)
                for row in holdings_history.get(trade_day, [])
            ]
            day_valuations = {
                valuation_symbol: SimpleNamespace(**payload)
                for valuation_symbol, payload in valuation_history.get(trade_day, {}).items()
            }
            day_result = calculate_weighted_index_valuation(
                index_level=index_level,
                holdings=day_holdings,
                valuations=day_valuations,
            )
            if day_result.get("status") != "available" or day_result.get("current_gap_pct") is None:
                continue
            day_result["index_date"] = trade_day.isoformat()
            holdings_day = holdings_as_of_history.get(trade_day)
            day_result["holdings_as_of"] = holdings_day.isoformat() if holdings_day else None
            daily_results.append(day_result)
        if not daily_results:
            return {"status": "unavailable", "reason": "valuation_history_missing"}
        result = daily_results[-1]
        gaps = [item["current_gap_pct"] for item in daily_results]
        result.update(
            **_build_valuation_position_fields(gaps, result["current_gap_pct"]),
            valuation_gap_min=round(min(gaps), 2),
            valuation_gap_median=round(sorted(gaps)[len(gaps) // 2], 2),
            valuation_gap_max=round(max(gaps), 2),
            method="adaptive_up_to_504d_percentile_of_robust_consensus_undervaluation",
            _history=daily_results,
        )
        result["index_date"] = latest.date.isoformat()
        result["holdings_as_of"] = holdings_as_of.isoformat() if holdings_as_of else None
        return result
    finally:
        AnalyticsSession.remove()


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
            history = payload.pop("_history", [])
            for history_payload in history:
                snapshot_date = date.fromisoformat(history_payload["index_date"])
                db.merge(AStockIndexValuationSnapshot(
                    symbol=symbol,
                    date=snapshot_date,
                    payload=history_payload,
                ))
            latest_date = date.fromisoformat(payload["index_date"])
            db.merge(AStockIndexValuationSnapshot(symbol=symbol, date=latest_date, payload=payload))
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


def _percentile_rank(values: Iterable[float], current: float) -> float:
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return 0.0
    less = sum(value < current for value in clean)
    equal = sum(value == current for value in clean)
    return round((less + 0.5 * equal) / len(clean) * 100.0, 2)


def _build_valuation_position_fields(values: Iterable[float], current: float) -> Dict[str, Any]:
    primary_values = [
        float(value)
        for value in values
        if value is not None
    ][-VALUATION_POSITION_MAX_WINDOW:]
    short_values = primary_values[-VALUATION_POSITION_SHORT_WINDOW:]

    def build_position(sample: List[float]) -> tuple[Optional[float], str]:
        if len(sample) < VALUATION_POSITION_MIN_SAMPLES:
            return None, "样本不足"
        position_pct = _percentile_rank(sample, current)
        return position_pct, _valuation_position_label(position_pct)

    position_pct, position_label = build_position(primary_values)
    short_position_pct, short_position_label = build_position(short_values)
    return {
        "valuation_position_pct": position_pct,
        "valuation_position_label": position_label,
        "valuation_history_days": len(primary_values),
        "valuation_position_max_days": VALUATION_POSITION_MAX_WINDOW,
        "valuation_position_min_days": VALUATION_POSITION_MIN_SAMPLES,
        "valuation_position_is_full_window": len(primary_values) >= VALUATION_POSITION_MAX_WINDOW,
        "valuation_position_252_pct": short_position_pct,
        "valuation_position_252_label": short_position_label,
        "valuation_history_252_days": len(short_values),
    }


def _valuation_position_label(position_pct: float) -> str:
    if position_pct >= 80:
        return "极度低估"
    if position_pct >= 60:
        return "低估"
    if position_pct >= 40:
        return "合理"
    if position_pct >= 20:
        return "高估"
    return "极度高估"
