#!/usr/bin/env python3
"""Correlation de-duplication and representative selection for SZDT A ETFs."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import replace
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search

CORRELATION_THRESHOLD = 0.80
MIN_OVERLAP = 120
UNIFORM = dict(when_buy=-90, when_sell=70, buy_factor=5.0, sell_factor=1.0, volume_ratio=1.2, max_position=100)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    metadata = {str(item["code"]).upper(): item for item in universe}
    prices = await search.fetch_prices(list(metadata))
    # The production K-line endpoint can occasionally throttle one request in a
    # batch.  Retry only the missing symbols so a partial response never changes
    # the correlation graph or its chosen representative.
    for _ in range(3):
        missing = [code for code in metadata if code not in prices]
        if not missing:
            break
        await asyncio.sleep(1)
        prices.update(await search.fetch_prices(missing))
    for code in [code for code in metadata if code not in prices]:
        # A final single-symbol request avoids transient per-batch throttling.
        prices.update(await search.fetch_prices([code]))
        await asyncio.sleep(0.2)
    # Correlation deliberately uses only the production daily price history.
    # Do not merge emotion observations here: doing so would incorrectly remove
    # price dates on which SZDT did not publish a score.
    price_frames = {code: frame for code, frame in prices.items() if len(frame) >= MIN_OVERLAP}
    frames: dict[str, pd.DataFrame] = {}
    for code, frame in price_frames.items():
        history = histories.get(code)
        if history is None:
            continue
        merged = frame.merge(history, on="date", how="inner").dropna().sort_values("date")
        if len(merged) >= 25:
            frames[code] = merged

    base = {code: configured.get(code, search.Config(code=code, name=str(metadata[code].get("name") or code))) for code in price_frames}
    per_asset = []
    for code, frame in price_frames.items():
        strategy_frame = frames.get(code)
        cfg = replace(base[code], **UNIFORM)
        result = search.asset_backtest(strategy_frame, cfg, search.START, search.END) if strategy_frame is not None else {"total_return": 0, "annualized_return": 0, "max_drawdown": 0, "sharpe": -99, "trades": 0, "final_nav": 100_000}
        nav = frame.close / frame.close.iloc[0]
        buy_hold_total = float(nav.iloc[-1] - 1)
        buy_hold_annual = (1 + buy_hold_total) ** (252 / max(1, len(frame) - 1)) - 1
        buy_hold_mdd = float((nav / nav.cummax() - 1).min())
        per_asset.append({
            "code": code, "name": cfg.name, "scale_billion": float(metadata[code].get("scale") or 0),
            "sessions": len(frame), "buy_hold_total_return": buy_hold_total,
            "buy_hold_annualized_return": buy_hold_annual, "buy_hold_max_drawdown": buy_hold_mdd, **result,
        })
    asset = pd.DataFrame(per_asset).set_index("code")
    closes = pd.concat({code: frame.set_index("date").close for code, frame in price_frames.items()}, axis=1)
    returns = closes.pct_change(fill_method=None)
    corr = returns.corr(min_periods=MIN_OVERLAP)
    overlap = returns.notna().astype(int).T.dot(returns.notna().astype(int))

    parent = {code: code for code in frames}
    def find(code: str) -> str:
        while parent[code] != code:
            parent[code] = parent[parent[code]]
            code = parent[code]
        return code
    def union(left: str, right: str) -> None:
        left, right = find(left), find(right)
        if left != right: parent[right] = left
    codes = list(price_frames)
    for index, left in enumerate(codes):
        for right in codes[index + 1:]:
            if overlap.loc[left, right] >= MIN_OVERLAP and corr.loc[left, right] >= CORRELATION_THRESHOLD:
                union(left, right)
    groups: dict[str, list[str]] = {}
    for code in codes: groups.setdefault(find(code), []).append(code)

    rows, candidates, cluster_member_rows = [], [], []
    for cluster_id, members in enumerate(groups.values(), start=1):
        members = sorted(members)
        common_close = pd.concat({code: price_frames[code].set_index("date").close for code in members}, axis=1).dropna()
        common_metrics = []
        for code in members:
            nav = common_close[code] / common_close[code].iloc[0]
            total = float(nav.iloc[-1] - 1)
            daily_returns = common_close[code].pct_change(fill_method=None).dropna()
            volatility = daily_returns.std()
            common_metrics.append({
                "code": code, "cluster_common_sessions": len(common_close),
                "cluster_common_annualized_return": (1 + total) ** (252 / max(1, len(common_close) - 1)) - 1,
                "cluster_common_max_drawdown": float((nav / nav.cummax() - 1).min()),
                "cluster_common_sharpe": float(daily_returns.mean() / volatility * math.sqrt(252)) if len(daily_returns) > 3 and volatility > 0 else None,
            })
        subset = asset.loc[members].join(pd.DataFrame(common_metrics).set_index("code"))
        subset["selection_score"] = subset.cluster_common_sharpe
        subset = subset.sort_values(["cluster_common_sharpe", "cluster_common_annualized_return", "scale_billion"], ascending=[False, False, False])
        for rank, (code, member) in enumerate(subset.iterrows(), start=1):
            cluster_member_rows.append({"cluster_id": cluster_id, "rank_by_common_price_sharpe": rank,
                                        "cluster_size": len(members), "code": code, **member.to_dict()})
        representative = subset.iloc[0]
        candidates.append({"code": representative.name, "name": representative["name"], "cluster_size": len(members), **representative.to_dict()})
        rows.append({
            "representative": representative.name, "representative_name": representative["name"], "members": members,
            "member_names": [asset.loc[member, "name"] for member in members], "cluster_size": len(members),
            "representative_buy_hold_sharpe": float(representative.cluster_common_sharpe),
            "representative_buy_hold_annualized_return": float(representative.cluster_common_annualized_return),
            "representative_buy_hold_max_drawdown": float(representative.cluster_common_max_drawdown),
            "cluster_common_sessions": int(representative.cluster_common_sessions),
        })
    candidates_df = pd.DataFrame(candidates).sort_values(["selection_score", "trades", "scale_billion"], ascending=False)
    candidates_df.to_csv(output / "representatives.csv", index=False)
    pd.DataFrame(cluster_member_rows).to_csv(output / "cluster_members_ranked.csv", index=False)
    asset.reset_index().to_csv(output / "all_assets_uniform_rule.csv", index=False)
    high_pairs = []
    for index, left in enumerate(codes):
        for right in codes[index + 1:]:
            value = corr.loc[left, right]
            if pd.notna(value) and value >= CORRELATION_THRESHOLD:
                high_pairs.append({"left": left, "left_name": asset.loc[left, "name"], "right": right, "right_name": asset.loc[right, "name"], "correlation": float(value), "overlap": int(overlap.loc[left, right])})
    pd.DataFrame(high_pairs).sort_values("correlation", ascending=False).to_csv(output / "high_correlation_pairs.csv", index=False)
    summary = {
        "method": {"period": [str(search.START), str(search.END)], "return": "daily close-to-close percent return", "correlation": "Pearson", "threshold": CORRELATION_THRESHOLD, "min_overlap_sessions": MIN_OVERLAP, "representative": "highest buy-and-hold Sharpe ratio on the cluster's common price dates"},
        "coverage": {"universe": len(universe), "price_available": len(prices), "correlation_eligible": len(price_frames), "strategy_history_available": len(frames), "excluded": sorted(set(metadata) - set(price_frames))},
        "clusters": sorted(rows, key=lambda row: (-row["cluster_size"], row["representative"])),
        "candidates": candidates_df.to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-a-etf-correlation"))
    asyncio.run(main(parser.parse_args().out))
