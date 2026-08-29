"""1m 一买强度代理分桶：严格 T+1 收盘收益。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from czsc import CZSC

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from src.core.services.chan_analysis import rows_to_raw_bars  # noqa: E402
from chan_signal_pair_backtest import (  # noqa: E402
    BacktestConfig, _load_bars,
)


def _is_down(b) -> bool:
    text = str(getattr(b, "direction", ""))
    return "down" in text.lower() or "向下" in text


def _build_segments(bis) -> list[dict]:
    """由已确认笔递归构造方向性线段。

    采用可审计的特征序列规则：同向笔必须持续创出该方向极值；
    下一根同向笔不再创新极值时，前一组线段确认结束，当前笔开启新线段。
    这避免把未完成线段或未来笔带入信号时点。
    """
    if not bis:
        return []
    segments: list[dict] = []
    current: list = [bis[0]]
    direction_down = _is_down(bis[0])
    extreme = float(getattr(bis[0], "low" if direction_down else "high"))
    for bi in bis[1:]:
        if _is_down(bi) != direction_down:
            current.append(bi)
            continue
        value = float(getattr(bi, "low" if direction_down else "high"))
        extends = value <= extreme if direction_down else value >= extreme
        if extends:
            current.append(bi)
            extreme = value
            continue
        if len(current) >= 2:
            segments.append({"down": direction_down, "bis": tuple(current),
                             "low": min(float(getattr(x, "low")) for x in current),
                             "high": max(float(getattr(x, "high")) for x in current),
                             "power_price": sum(abs(float(getattr(x, "power_price", 0) or 0)) for x in current),
                             "power_volume": sum(abs(float(getattr(x, "power_volume", 0) or 0)) for x in current)})
        current = [bi]
        direction_down = _is_down(bi)
        extreme = value
    # 最后一组仅在当前笔已经确认时纳入；它仍可能是延伸中的线段，但不使用未来信息。
    if len(current) >= 2:
        segments.append({"down": direction_down, "bis": tuple(current),
                         "low": min(float(getattr(x, "low")) for x in current),
                         "high": max(float(getattr(x, "high")) for x in current),
                         "power_price": sum(abs(float(getattr(x, "power_price", 0) or 0)) for x in current),
                         "power_volume": sum(abs(float(getattr(x, "power_volume", 0) or 0)) for x in current)})
    return segments


def _strength_score(c: CZSC, close: float) -> float:
    """真实笔/自建线段/中枢特征的一买背驰分数。"""
    down = [s for s in _build_segments(c.bi_list) if s["down"]]
    if len(down) < 2:
        return np.nan
    prev, recent = down[-2], down[-1]
    prev_power = max(prev["power_price"], 1e-9)
    recent_power = recent["power_price"]
    prev_vol = max(prev["power_volume"], 1e-9)
    recent_vol = recent["power_volume"]
    # 价格创新低但笔力度/量能力度下降，分数更高。
    price_new_low = recent["low"] <= prev["low"]
    divergence = (1 - recent_power / prev_power) * 0.5 + (1 - recent_vol / prev_vol) * 0.5
    if not price_new_low:
        divergence -= 0.5
    center_bonus = 0.0
    if c.zs_list:
        zs = c.zs_list[-1]
        zd, zg = float(getattr(zs, "zd", close)), float(getattr(zs, "zg", close))
        center_bonus = 0.2 if close < zd else (-0.2 if close > zg else 0.0)
    return float(np.clip(divergence, -3, 3) + center_bonus)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    ap.add_argument("--supplemental-weights", required=True)
    ap.add_argument("--start-date", default="2026-07-13")
    ap.add_argument("--end-date", default="2026-08-26")
    ap.add_argument("--output", default="lab/output/chan_signal_pair_backtest_20260826_marked/1m_buy_strength_buckets.csv")
    ap.add_argument("--trades", default="lab/output/chan_signal_pair_backtest_20260826_marked/t1_close_trades.csv")
    ap.add_argument("--min-avg-amount", type=float, default=100_000.0)
    ap.add_argument("--liquidity-days", type=int, default=20)
    ap.add_argument("--buy-cost-bps", type=float, default=15.0)
    ap.add_argument("--sell-cost-bps", type=float, default=25.0)
    ap.add_argument("--symbol-limit", type=int)
    ap.add_argument("--symbol-offset", type=int, default=0)
    ap.add_argument("--rolling-days", type=int, default=10,
                    help="仅用此前N个交易日的一买分数拟合五分位阈值")
    args = ap.parse_args()
    cfg = BacktestConfig(database=args.database, start_date=date.fromisoformat(args.start_date),
                         end_date=date.fromisoformat(args.end_date), min_avg_amount=args.min_avg_amount,
                         liquidity_days=args.liquidity_days, buy_cost_bps=args.buy_cost_bps,
                         sell_cost_bps=args.sell_cost_bps, supplemental_weights=args.supplemental_weights)
    con = duckdb.connect(cfg.database, read_only=True)
    records: list[dict] = []
    try:
        source = pd.read_csv(ROOT / args.trades, parse_dates=["signal_time"])
        source = source[(source.freq == "1m") & (source.buy_type == "一买")]
        source_rows = {(r.symbol, r.signal_time): float(r.net_return) for r in source.itertuples()}
        symbols = sorted(source.symbol.unique())
        symbols = symbols[args.symbol_offset:]
        if args.symbol_limit:
            symbols = symbols[:args.symbol_limit]
        for n, symbol in enumerate(symbols, 1):
            try:
                bars = _load_bars(con, symbol, "1m", cfg)
                if len(bars) < 60:
                    continue
                times = pd.to_datetime(bars.timestamp)
                idx = {pd.Timestamp(t): i for i, t in enumerate(times)}
                signal_times = set(source.loc[source.symbol == symbol, "signal_time"])
                raw = rows_to_raw_bars(symbol, bars.to_dict("records"), "1m")
                cz = CZSC(raw[:20])
                score_by_time = {}
                for bar in raw[20:]:
                    cz.update(bar)
                    if pd.Timestamp(bar.dt) in signal_times:
                        score_by_time[pd.Timestamp(bar.dt)] = _strength_score(cz, float(bar.close))
                for signal_time in signal_times:
                    i = idx.get(signal_time)
                    if i is None or i + 1 >= len(bars):
                        continue
                    records.append({"symbol": symbol, "signal_time": signal_time, "signal_date": signal_time.date(),
                                    "score": score_by_time.get(signal_time, np.nan),
                                    "net_return": source_rows[(symbol, signal_time)]})
            except Exception:
                continue
            if n % 200 == 0 or n == len(symbols):
                print(f"1m strength: {n}/{len(symbols)} symbols, {len(records)} observations", flush=True)
    finally:
        con.close()
    frame = pd.DataFrame(records).dropna(subset=["score"])
    # 每个信号日只用此前 rolling_days 个交易日拟合阈值，严格避免事后分桶。
    frame["bucket"] = pd.NA
    trading_dates = sorted(frame.signal_date.unique())
    labels = ["Q1最低", "Q2", "Q3", "Q4", "Q5最高"]
    for pos, d in enumerate(trading_dates):
        history_dates = trading_dates[max(0, pos - args.rolling_days):pos]
        hist = frame[frame.signal_date.isin(history_dates)].score.dropna()
        cur = frame.signal_date == d
        if len(hist) < 20:
            continue
        bins = np.quantile(hist.to_numpy(), np.linspace(0, 1, 6))
        # 重复边界时允许空桶；digitize 不会因边界重复报错。
        bucket_idx = np.clip(np.digitize(frame.loc[cur, "score"].to_numpy(float), bins[1:-1], right=True), 0, 4)
        frame.loc[cur, "bucket"] = [labels[i] for i in bucket_idx]
    frame = frame.dropna(subset=["bucket"])
    rows = []
    for bucket, x in frame.groupby("bucket", observed=False):
        r = x.net_return
        wins, losses = r[r > 0], r[r < 0]
        rows.append({"buy_type": "一买", "bucket": str(bucket), "observation_count": len(x),
                     "symbol_count": x.symbol.nunique(), "score_min": x.score.min(), "score_max": x.score.max(),
                     "avg_net_return": r.mean(), "median_net_return": r.median(), "win_rate": (r > 0).mean(),
                     "profit_factor": wins.sum() / abs(losses.sum()) if len(losses) else np.inf})
    out = ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    frame.to_csv(out.with_name("1m_buy_strength_trades.csv"), index=False)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
