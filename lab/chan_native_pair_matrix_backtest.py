"""v2 native Chan buy/sell pairing matrix under A-share T+1 simulated trading.

For each frequency in {1m, 5m, 30m} and every (buy_type × sell_type) pair in
{一买/二买/三买} × {一卖/二卖/三卖}:

* run the v2 structure engine once per symbol, take confirmed buy/sell events;
* enter at the bar **after** a buy signal confirms (next bar open);
* exit at the bar after the first later-day sell signal of the chosen type
  (T+1: the exit fill day must be strictly after the entry fill day);
* if no such sell arrives before data ends, close at the last close and mark
  the trade ``end_of_data`` (reported separately — never counted as a sell hit).

Costs 15 bp buy / 25 bp sell.  Pool = HS300 + CSI500 historical membership
(中证1000/2000 are not in this DB copy).  One position at a time per
(symbol, freq, pair).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "lab")]
from chan_native import Kline, build_segments, calculate, detect_buy_sell  # noqa: E402
from chan_signal_pair_backtest import (  # noqa: E402
    BacktestConfig,
    INDEX_CODES,
    _in_intervals,
    _load_bars,
    _load_daily_filters,
    _membership_intervals,
)

BUY = ("一买", "二买", "三买")
SELL = ("一卖", "二卖", "三卖")
FREQS = ("1m", "5m", "30m")
BUY_BP, SELL_BP = 15, 25


def simulate_pair(events, bars, times, day_order, day_first_bar, dates, membership, sym, eligible, bt, stype, max_hold=0):
    """One dict per trade for a single buy×sell pair, one position at a time.

    Enter at the bar after a ``bt`` signal confirms.  Exit at the bar after
    the first later-trading-day ``stype`` signal (A-share T+1).  ``max_hold``
    > 0 additionally force-exits at the open of the bar ``max_hold`` trading
    days after entry, so a pair whose sell signal never arrives still yields
    a bounded trade instead of an end-of-data mark.
    """
    n = len(bars)
    n_days = len(day_first_bar)

    def rec(entry, ix, net, reason):
        return {
            "symbol": sym, "buy_type": bt, "sell_type": stype, "freq": None,
            "entry_time": entry[1], "exit_time": times.iloc[ix],
            "hold_days": day_order[dates[ix]] - day_order[entry[1].date()],
            "net_return": net, "exit_reason": reason, "buy_detail": entry[3],
        }

    def netret(ep, xp):
        return xp * (1 - SELL_BP / 1e4) / (ep * (1 + BUY_BP / 1e4)) - 1

    out = []
    entry = None  # (fill_idx, fill_time, fill_price, detail)
    for e in events:
        fill = e.confirm_i + 1
        if fill >= n:
            continue
        if entry is not None:
            edo = day_order[entry[1].date()]
            # forced max-hold exit if it falls on or before this signal's fill
            if max_hold and edo + max_hold < n_days:
                cap_ix = day_first_bar[edo + max_hold]
                if cap_ix <= fill:
                    out.append(rec(entry, cap_ix, netret(entry[2], float(bars.iloc[cap_ix].open)), "max_hold"))
                    entry = None
        if entry is None:
            if e.kind == bt:
                sig_date = dates[e.confirm_i]
                if _in_intervals(sig_date, membership.get(sym, [])) and eligible.get(sym, {}).get(sig_date, False):
                    entry = (fill, times.iloc[fill], float(bars.iloc[fill].open), e.detail)
            continue
        if e.kind == stype and day_order[dates[fill]] - day_order[entry[1].date()] >= 1:
            out.append(rec(entry, fill, netret(entry[2], float(bars.iloc[fill].open)), "signal"))
            entry = None
    if entry is not None:
        edo = day_order[entry[1].date()]
        if max_hold and edo + max_hold < n_days:
            cap_ix = day_first_bar[edo + max_hold]
            out.append(rec(entry, cap_ix, netret(entry[2], float(bars.iloc[cap_ix].open)), "max_hold"))
        else:
            out.append(rec(entry, n - 1, netret(entry[2], float(bars.iloc[n - 1].close)), "end_of_data"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    ap.add_argument("--start-date", default="2026-02-25")
    ap.add_argument("--end-date", default="2026-08-28")
    ap.add_argument("--symbol-limit", type=int)
    ap.add_argument("--out", default="/tmp/claude-1000/-home-sevenshal-Dev-github-quant-52etf/272e534e-00c7-4daa-9192-855d3d76dae8/scratchpad")
    a = ap.parse_args()
    cfg = BacktestConfig(a.database, date.fromisoformat(a.start_date), date.fromisoformat(a.end_date), 100000, 20, 15, 25, None)

    con = duckdb.connect(a.database, read_only=True)
    codes = list(INDEX_CODES) + ["932000.CSI"]
    w = con.execute(
        f"select index_code,con_code,trade_date,weight from a_stock_index_weight "
        f"where index_code in ({','.join('?' for _ in codes)}) and trade_date<=?",
        [*codes, cfg.end_date],
    ).fetchdf()
    w["index_code"] = w.index_code.astype(str).str.upper()
    w["con_code"] = w.con_code.astype(str).str.upper()
    w["trade_date"] = pd.to_datetime(w.trade_date).dt.date
    w = w[w.index_code.isin(codes)].drop_duplicates(["index_code", "con_code", "trade_date"], keep="last")
    present = sorted(set(w.index_code))
    missing = sorted(set(codes) - set(present))
    if missing:
        print(f"WARNING: 缺少历史成分，未纳入: {missing}", flush=True)
    membership = _membership_intervals(w)
    symbols = sorted(membership)
    if a.symbol_limit:
        symbols = symbols[: a.symbol_limit]

    filt = _load_daily_filters(con, symbols, cfg)
    filt["eligible"] = filt.not_st.fillna(False) & (filt.liquidity_observations >= 20) & (filt.avg_amount >= 100000)
    eligible = {s: dict(zip(g.trade_date, g.eligible, strict=False)) for s, g in filt.groupby("ts_code")}

    MAX_HOLD = 10  # trading days; second table caps holding here
    all_trades: list[dict] = []
    for freq in FREQS:
        rows: list[dict] = []
        for n, sym in enumerate(symbols, 1):
            try:
                bars = _load_bars(con, sym, freq, cfg)
                if len(bars) < 100:
                    continue
                raw = [
                    Kline(i=i, high=float(r.high), low=float(r.low), close=float(r.close), dt=pd.Timestamp(r.timestamp))
                    for i, r in enumerate(bars.itertuples())
                ]
                _norm, _fx, st, zs = calculate(raw, min_gap=4)
                seg = build_segments(st)
                events = sorted(detect_buy_sell(st, seg, zs, bars=raw), key=lambda e: (e.confirm_i, e.kind))
                if not events:
                    continue
                times = pd.to_datetime(bars.timestamp)
                dates = np.asarray(times.dt.date)
                day_order = {d: i for i, d in enumerate(dict.fromkeys(dates))}
                day_first_bar = list(pd.Series(range(len(dates))).groupby(dates).min().sort_index().to_numpy())
                for bt in BUY:
                    for stype in SELL:
                        for mh, tag in ((0, "unbounded"), (MAX_HOLD, f"cap{MAX_HOLD}d")):
                            for tr in simulate_pair(events, bars, times, day_order, day_first_bar, dates,
                                                    membership, sym, eligible, bt, stype, max_hold=mh):
                                tr["freq"] = freq
                                tr["mode"] = tag
                                rows.append(tr)
            except Exception as exc:  # noqa: BLE001
                continue
            if n % 200 == 0 or n == len(symbols):
                print(f"[{freq}] {n}/{len(symbols)} symbols, {len(rows)} trades", flush=True)
        all_trades.extend(rows)
    con.close()

    frame = pd.DataFrame(all_trades)
    frame.to_csv(Path(a.out, "chan_native_pair_matrix_trades.csv"), index=False)

    def block(x: pd.DataFrame) -> dict:
        r = x.net_return.astype(float)
        loss = r[r < 0]
        return dict(
            trades=len(x),
            closed=int((x.exit_reason == "signal").sum()),
            capped=int((x.exit_reason == "max_hold").sum()),
            eod=int((x.exit_reason == "end_of_data").sum()),
            symbols=x.symbol.nunique(),
            avg=r.mean() if len(r) else np.nan,
            med=r.median() if len(r) else np.nan,
            win=(r > 0).mean() if len(r) else np.nan,
            pf=(r[r > 0].sum() / abs(loss.sum())) if len(loss) else np.nan,
            hold=x.hold_days.mean() if len(x) else np.nan,
        )

    lines: list[str] = []
    lines.append(f"# v2 native 买卖点配对矩阵  {a.start_date}..{a.end_date}  池=HS300+CSI500  A股T+1模拟  成本15/25bp")
    lines.append(f"# 每格：买入=买点信号次bar开盘，卖出=之后首个更晚交易日的卖点信号次bar开盘。")
    lines.append(f"# unbounded=一直等对应卖点（等不到则期末计价 eod）；cap{MAX_HOLD}d=最多持有{MAX_HOLD}个交易日强制平仓。\n")
    hdr = (f"{'买':<5}{'卖':<5}{'交易':>6}{'closed':>8}{'capped':>8}{'eod':>6}{'标的':>6}"
           f"{'均值':>9}{'中位':>9}{'胜率':>8}{'PF':>7}{'持有日':>8}")
    for mode in ("unbounded", f"cap{MAX_HOLD}d"):
        for freq in FREQS:
            lines.append(f"\n## {freq} · {mode}")
            lines.append(hdr)
            for bt in BUY:
                for stype in SELL:
                    x = frame[(frame["mode"] == mode) & (frame.freq == freq)
                              & (frame.buy_type == bt) & (frame.sell_type == stype)]
                    if x.empty:
                        lines.append(f"{bt:<5}{stype:<5}{'0':>6}")
                        continue
                    b = block(x)
                    lines.append(
                        f"{bt:<5}{stype:<5}{b['trades']:>6}{b['closed']:>8}{b['capped']:>8}{b['eod']:>6}{b['symbols']:>6}"
                        f"{b['avg']*100:>8.2f}%{b['med']*100:>8.2f}%{b['win']*100:>7.1f}%{b['pf']:>7.2f}{b['hold']:>8.1f}"
                    )
    report = "\n".join(lines)
    print("\n" + report)
    Path(a.out, "chan_native_pair_matrix_report.txt").write_text(report, encoding="utf-8")
    print(f"\nwritten: {a.out}/chan_native_pair_matrix_report.txt")


if __name__ == "__main__":
    main()
