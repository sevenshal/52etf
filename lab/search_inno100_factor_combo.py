from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.database import SessionLocal
from src.core.services import factor_backtest_engine as engine
from src.core.services.factor_backtest_engine import (
    A_STOCK_INNO100_POOL,
    FactorBacktestConfig,
    FactorBacktestLeg,
    prepare_factor_backtest_base_data,
    resolve_factor_legs,
    run_factor_backtest,
)
from src.core.services.factor_engine import (
    DEFAULT_MOMENTUM_WEIGHTS,
    FACTOR_REGISTRY,
    MIXED_WINDOW_KEY,
)


DEFAULT_CANDIDATES = [
    "alpha167",
    "alpha189",
    "alpha175",
    "alpha161",
    "alpha187",
    "alpha169",
    "alpha174",
    "alpha042",
    "alpha059",
    "alpha088",
    "alpha093",
    "alpha095",
    "alpha106",
    "alpha135",
    "alpha160",
]


@dataclass(frozen=True)
class FactorSpec:
    key: str
    window: int | str


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value).date()


def factor_spec(key: str) -> FactorSpec:
    definition = FACTOR_REGISTRY[key]
    if definition.supports_mixed_windows:
        return FactorSpec(key=key, window=MIXED_WINDOW_KEY)
    return FactorSpec(key=key, window=int(definition.default_windows[0]))


def normalize_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def simplex_weights(parts: int, bucket_count: int, *, min_bucket: int = 0) -> list[tuple[float, ...]]:
    if parts <= 1:
        return [(1.0,)]
    result: list[tuple[float, ...]] = []

    def walk(remaining: int, slots: int, prefix: list[int]) -> None:
        if slots == 1:
            if remaining >= min_bucket:
                values = [*prefix, remaining]
                total = sum(values)
                if total > 0:
                    result.append(tuple(value / total for value in values))
            return
        lower = min_bucket
        upper = remaining - min_bucket * (slots - 1)
        for value in range(lower, upper + 1):
            walk(remaining - value, slots - 1, [*prefix, value])

    walk(bucket_count, parts, [])
    return result


def position_weight_grid(max_positions: int, *, equal_only: bool = False) -> list[tuple[float, ...]]:
    count = int(max_positions)
    if count <= 1:
        return [(1.0,)]
    equal = tuple(round(1.0 / count, 10) for _ in range(count))
    if equal_only:
        return [equal]
    grids = [equal]
    if count == 2:
        grids.extend([(0.65, 0.35), (0.75, 0.25)])
    elif count == 3:
        grids.extend([(0.5, 0.3, 0.2), (0.6, 0.25, 0.15), (0.7, 0.2, 0.1)])
    elif count == 4:
        grids.extend([(0.4, 0.3, 0.2, 0.1), (0.55, 0.25, 0.15, 0.05)])
    elif count == 5:
        grids.extend([(0.35, 0.25, 0.2, 0.12, 0.08), (0.5, 0.2, 0.15, 0.1, 0.05)])
    dedup: list[tuple[float, ...]] = []
    seen = set()
    for item in grids:
        normalized = tuple(round(float(weight), 10) for weight in item)
        if normalized not in seen:
            seen.add(normalized)
            dedup.append(normalized)
    return dedup


def build_legs(specs: list[FactorSpec], weights: Iterable[float], standardization: str) -> list[FactorBacktestLeg]:
    return [
        FactorBacktestLeg(
            factor=spec.key,
            window=spec.window,
            weight=float(weight),
            neutralization="none",
            standardization=standardization,
            momentum_weights=DEFAULT_MOMENTUM_WEIGHTS.copy(),
        )
        for spec, weight in zip(specs, weights)
        if abs(float(weight)) > 1e-12
    ]


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def equity_segment_metrics(equity_curve: list[dict[str, Any]], start: date | None, end: date | None = None) -> dict[str, float | None]:
    rows: list[tuple[date, float]] = []
    for item in equity_curve or []:
        try:
            row_date = date.fromisoformat(str(item.get("date")))
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        if start is not None and row_date < start:
            continue
        if end is not None and row_date > end:
            continue
        rows.append((row_date, value))
    if len(rows) < 2:
        return {
            "total_return": None,
            "annualized_return": None,
            "annualized_volatility": None,
            "sharpe": None,
            "max_drawdown": None,
        }
    start_date, start_value = rows[0]
    end_date, end_value = rows[-1]
    total_return = (end_value / start_value - 1) * 100
    elapsed_days = max(1, (end_date - start_date).days)
    annualized_return = ((1 + total_return / 100) ** (365 / elapsed_days) - 1) * 100 if total_return > -100 else None
    peak = start_value
    previous = start_value
    max_drawdown = 0.0
    returns: list[float] = []
    for _, value in rows[1:]:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, (value / peak - 1) * 100)
        returns.append(value / previous - 1)
        previous = value
    annualized_volatility = None
    sharpe = None
    if len(returns) > 1:
        mean_return = sum(returns) / len(returns)
        std_return = math.sqrt(sum((item - mean_return) ** 2 for item in returns) / (len(returns) - 1))
        annualized_volatility = std_return * math.sqrt(engine.TRADING_DAYS_PER_YEAR) * 100
        if std_return > 0:
            sharpe = mean_return / std_return * math.sqrt(engine.TRADING_DAYS_PER_YEAR)
    return {
        "total_return": round(total_return, 4),
        "annualized_return": round(annualized_return, 4) if annualized_return is not None else None,
        "annualized_volatility": round(annualized_volatility, 4) if annualized_volatility is not None else None,
        "sharpe": round(sharpe, 6) if sharpe is not None else None,
        "max_drawdown": round(max_drawdown, 4),
    }


def summarize_result(result: dict[str, Any], oos_start: date | None) -> dict[str, Any]:
    metrics = result.get("metrics") or {}
    yearly = result.get("yearly_stats") or []
    valid_years = [row for row in yearly if row.get("primary_outperformed") is not None]
    outperformed = [row for row in valid_years if row.get("primary_outperformed")]
    excess_values = [
        safe_float(row.get("primary_excess_return_pct"))
        for row in valid_years
        if safe_float(row.get("primary_excess_return_pct")) is not None
    ]
    oos_metrics = equity_segment_metrics(result.get("equity_curve") or [], oos_start) if oos_start else {}
    oos_years = [
        row
        for row in valid_years
        if oos_start is not None and int(row.get("year")) >= oos_start.year
    ]
    failed_years = [
        str(row.get("year"))
        for row in valid_years
        if not row.get("primary_outperformed")
    ]
    sharpe = safe_float(metrics.get("sharpe"))
    ann_return = safe_float(metrics.get("annualized_return"))
    max_drawdown = safe_float(metrics.get("max_drawdown"))
    yearly_count = len(valid_years)
    outperform_count = len(outperformed)
    min_excess = min(excess_values) if excess_values else None
    stable_hit = bool(yearly_count and outperform_count == yearly_count and sharpe is not None and sharpe >= 1.5)
    oos_outperform_count = sum(1 for row in oos_years if row.get("primary_outperformed"))
    oos_sharpe = safe_float(oos_metrics.get("sharpe"))
    score = (
        (outperform_count / yearly_count * 100 if yearly_count else 0)
        + (sharpe or -10) * 20
        + (ann_return or -100)
        + (min_excess or -100) * 0.5
        + (max_drawdown or -100) * 0.1
        + (50 if stable_hit else 0)
        + (10 if oos_years and oos_outperform_count == len(oos_years) else 0)
        + (oos_sharpe or 0) * 5
    )
    return {
        "stable_hit": stable_hit,
        "score": round(score, 6),
        "annualized_return": ann_return,
        "total_return": safe_float(metrics.get("total_return")),
        "sharpe": sharpe,
        "calmar": safe_float(metrics.get("calmar")),
        "max_drawdown": max_drawdown,
        "annualized_volatility": safe_float(metrics.get("annualized_volatility")),
        "trade_count": metrics.get("trade_count"),
        "win_rate": safe_float(metrics.get("win_rate")),
        "rebalance_count": metrics.get("rebalance_count"),
        "yearly_count": yearly_count,
        "yearly_outperform_count": outperform_count,
        "yearly_outperform_rate": round(outperform_count / yearly_count, 6) if yearly_count else None,
        "min_yearly_excess": round(min_excess, 6) if min_excess is not None else None,
        "failed_years": ",".join(failed_years),
        "oos_annualized_return": safe_float(oos_metrics.get("annualized_return")),
        "oos_sharpe": oos_sharpe,
        "oos_max_drawdown": safe_float(oos_metrics.get("max_drawdown")),
        "oos_yearly_count": len(oos_years),
        "oos_yearly_outperform_count": oos_outperform_count,
    }


def row_from_case(
    case_index: int,
    specs: list[FactorSpec],
    factor_weights: tuple[float, ...],
    max_positions: int,
    position_weights: tuple[float, ...],
    sell_rank_multiplier: float,
    rebalance_frequency: str,
    rotation_mode: str,
    standardization: str,
    result: dict[str, Any],
    oos_start: date | None,
    elapsed_ms: float,
) -> dict[str, Any]:
    summary = summarize_result(result, oos_start)
    factor_part = ",".join(f"{spec.key}:{weight:.4f}" for spec, weight in zip(specs, factor_weights))
    row = {
        "case_index": case_index,
        "factors": ",".join(spec.key for spec in specs),
        "factor_weights": factor_part,
        "max_positions": max_positions,
        "position_weights": ":".join(f"{weight:.4f}" for weight in position_weights),
        "sell_rank_multiplier": sell_rank_multiplier,
        "rebalance_frequency": rebalance_frequency,
        "rotation_mode": rotation_mode,
        "standardization": standardization,
        "elapsed_ms": round(elapsed_ms, 2),
    }
    row.update(summary)
    return row


def iter_cases(
    candidates: list[str],
    combo_sizes: list[int],
    factor_bucket_count: int,
    max_positions_list: list[int],
    sell_rank_multipliers: list[float],
    rebalance_frequencies: list[str],
    rotation_modes: list[str],
    standardizations: list[str],
    min_weight_bucket: int,
    must_include: set[str],
    equal_position_weights_only: bool,
) -> Iterable[tuple[list[FactorSpec], tuple[float, ...], int, tuple[float, ...], float, str, str, str]]:
    for combo_size in combo_sizes:
        for keys in itertools.combinations(candidates, combo_size):
            if must_include and not must_include.issubset(set(keys)):
                continue
            specs = [factor_spec(key) for key in keys]
            for factor_weights in simplex_weights(combo_size, factor_bucket_count, min_bucket=min_weight_bucket):
                for standardization in standardizations:
                    for max_positions in max_positions_list:
                        for position_weights in position_weight_grid(max_positions, equal_only=equal_position_weights_only):
                            for sell_rank_multiplier in sell_rank_multipliers:
                                for rebalance_frequency in rebalance_frequencies:
                                    for rotation_mode in rotation_modes:
                                        yield (
                                            specs,
                                            factor_weights,
                                            max_positions,
                                            position_weights,
                                            sell_rank_multiplier,
                                            rebalance_frequency,
                                            rotation_mode,
                                            standardization,
                                        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Search stable A股创新100 factor-combo backtest parameters.")
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--combo-sizes", default="1,2,3")
    parser.add_argument("--factor-bucket-count", type=int, default=10)
    parser.add_argument("--min-weight-bucket", type=int, default=1)
    parser.add_argument("--max-positions", default="1,2,3")
    parser.add_argument("--sell-rank-multipliers", default="1.0,1.5,2.0")
    parser.add_argument("--rebalance-frequencies", default="weekly,monthly")
    parser.add_argument("--rotation-modes", default="rank_exit_rebalance,cash_fill_rebalance")
    parser.add_argument("--standardizations", default="zscore")
    parser.add_argument("--must-include", default="")
    parser.add_argument("--equal-position-weights-only", action="store_true")
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date")
    parser.add_argument("--oos-start", default="2025-01-01")
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--stop-on-hit", action="store_true")
    parser.add_argument("--output-csv", default="/private/tmp/inno100_factor_combo_search.csv")
    parser.add_argument("--top-json", default="/private/tmp/inno100_factor_combo_search_top.json")
    args = parser.parse_args()

    candidates = normalize_csv(args.candidates, DEFAULT_CANDIDATES)
    missing = [key for key in candidates if key not in FACTOR_REGISTRY]
    if missing:
        raise SystemExit(f"Unknown factors: {', '.join(missing)}")
    combo_sizes = [int(item) for item in normalize_csv(args.combo_sizes, ["1", "2", "3"])]
    max_positions_list = [int(item) for item in normalize_csv(args.max_positions, ["1", "2", "3"])]
    sell_rank_multipliers = [float(item) for item in normalize_csv(args.sell_rank_multipliers, ["1.0", "1.5", "2.0"])]
    rebalance_frequencies = normalize_csv(args.rebalance_frequencies, ["weekly", "monthly"])
    rotation_modes = normalize_csv(args.rotation_modes, ["rank_exit_rebalance", "cash_fill_rebalance"])
    standardizations = normalize_csv(args.standardizations, ["zscore"])
    must_include = set(normalize_csv(args.must_include, []))
    missing_required = sorted(key for key in must_include if key not in candidates)
    if missing_required:
        raise SystemExit(f"--must-include factors are not in --candidates: {', '.join(missing_required)}")
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    oos_start = parse_date(args.oos_start)

    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    # Larger caches matter a lot when many cases reuse the same component factor frames.
    engine.BACKTEST_SEARCH_COMPONENT_FACTOR_CACHE_LIMIT = 128
    engine.BACKTEST_SEARCH_FACTOR_VALUES_CACHE_LIMIT = 4096

    superset_specs = [factor_spec(key) for key in candidates]
    superset_config = FactorBacktestConfig(
        pool=A_STOCK_INNO100_POOL,
        start_date=start_date,
        end_date=end_date,
        max_positions=max(max_positions_list),
        position_weights=[1.0 / max(max_positions_list)] * max(max_positions_list),
        sell_rank_multiplier=max(sell_rank_multipliers),
        rebalance_frequency=rebalance_frequencies[0],
        rotation_mode=rotation_modes[0],
        commission_pct=0.03,
        slippage_pct=0.02,
        lot_size=args.lot_size,
        min_listing_days=365,
        legs=build_legs(superset_specs, [1.0] * len(superset_specs), "zscore"),
    )

    db = SessionLocal()
    try:
        resolved_superset = resolve_factor_legs(superset_config.legs)
        prepared_data = prepare_factor_backtest_base_data(superset_config, db, resolved_superset)
        print(
            "prepared "
            f"symbols={prepared_data.get('symbol_count')} "
            f"days={len(prepared_data.get('dates') or [])} "
            f"end={prepared_data.get('end_date')} "
            f"candidates={len(candidates)}"
        )

        all_rows: list[dict[str, Any]] = []
        top_rows: list[dict[str, Any]] = []
        fieldnames: list[str] | None = None
        start = time.perf_counter()
        for case_index, case in enumerate(
            iter_cases(
                candidates,
                combo_sizes,
                args.factor_bucket_count,
                max_positions_list,
                sell_rank_multipliers,
                rebalance_frequencies,
                rotation_modes,
                standardizations,
                args.min_weight_bucket,
                must_include,
                args.equal_position_weights_only,
            ),
            start=1,
        ):
            if args.limit and case_index > args.limit:
                break
            (
                specs,
                factor_weights,
                max_positions,
                position_weights,
                sell_rank_multiplier,
                rebalance_frequency,
                rotation_mode,
                standardization,
            ) = case
            cfg = FactorBacktestConfig(
                pool=A_STOCK_INNO100_POOL,
                start_date=start_date,
                end_date=end_date,
                max_positions=max_positions,
                position_weights=list(position_weights),
                sell_rank_multiplier=sell_rank_multiplier,
                rebalance_frequency=rebalance_frequency,
                rotation_mode=rotation_mode,
                commission_pct=0.03,
                slippage_pct=0.02,
                lot_size=args.lot_size,
                min_listing_days=365,
                legs=build_legs(specs, factor_weights, standardization),
            )
            case_start = time.perf_counter()
            try:
                result = run_factor_backtest(cfg, db, prepared_data=prepared_data)
                row = row_from_case(
                    case_index,
                    specs,
                    factor_weights,
                    max_positions,
                    position_weights,
                    sell_rank_multiplier,
                    rebalance_frequency,
                    rotation_mode,
                    standardization,
                    result,
                    oos_start,
                    (time.perf_counter() - case_start) * 1000,
                )
            except Exception as exc:
                row = {
                    "case_index": case_index,
                    "factors": ",".join(spec.key for spec in specs),
                    "factor_weights": ",".join(f"{weight:.4f}" for weight in factor_weights),
                    "max_positions": max_positions,
                    "position_weights": ":".join(f"{weight:.4f}" for weight in position_weights),
                    "sell_rank_multiplier": sell_rank_multiplier,
                    "rebalance_frequency": rebalance_frequency,
                    "rotation_mode": rotation_mode,
                    "standardization": standardization,
                    "error": str(exc),
                    "elapsed_ms": round((time.perf_counter() - case_start) * 1000, 2),
                }
            if fieldnames is None:
                fieldnames = list(row)
                with output_csv.open("w", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerow(row)
            else:
                with output_csv.open("a", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writerow(row)
            all_rows.append(row)
            if not row.get("error"):
                top_rows.append(row)
                top_rows.sort(key=lambda item: float(item.get("score") or -1e18), reverse=True)
                del top_rows[args.top_n :]

            if case_index == 1 or case_index % 50 == 0 or row.get("stable_hit"):
                best = top_rows[0] if top_rows else row
                elapsed = time.perf_counter() - start
                print(
                    f"case={case_index} elapsed={elapsed:.1f}s "
                    f"best_score={best.get('score')} "
                    f"best_sharpe={best.get('sharpe')} "
                    f"best_ann={best.get('annualized_return')} "
                    f"best_years={best.get('yearly_outperform_count')}/{best.get('yearly_count')} "
                    f"best_factors={best.get('factor_weights')} "
                    f"stable_hit={best.get('stable_hit')}"
                )
            if row.get("stable_hit") and args.stop_on_hit:
                break

        with Path(args.top_json).open("w") as fh:
            json.dump(top_rows, fh, ensure_ascii=False, indent=2)

        print(f"wrote {output_csv} rows={len(all_rows)}")
        print(f"wrote {args.top_json} rows={len(top_rows)}")
        if top_rows:
            print("top")
            for item in top_rows[:10]:
                print(
                    f"score={item.get('score')} sharpe={item.get('sharpe')} "
                    f"ann={item.get('annualized_return')} maxdd={item.get('max_drawdown')} "
                    f"years={item.get('yearly_outperform_count')}/{item.get('yearly_count')} "
                    f"min_excess={item.get('min_yearly_excess')} "
                    f"oos_sharpe={item.get('oos_sharpe')} "
                    f"pos={item.get('position_weights')} "
                    f"sell={item.get('sell_rank_multiplier')} "
                    f"freq={item.get('rebalance_frequency')} mode={item.get('rotation_mode')} "
                    f"factors={item.get('factor_weights')}"
                )
    finally:
        db.close()


if __name__ == "__main__":
    main()
