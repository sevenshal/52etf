"""对已计算的一买线段强度结果做降频与全市场 Top-K 对比。"""
from pathlib import Path
import argparse
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "lab/output/chan_signal_pair_backtest_20260826_marked/1m_buy_strength_trades.csv"
OUT = ROOT / "lab/output/chan_signal_pair_backtest_20260826_marked/1m_signal_thinning_summary.csv"

def stat(name, x):
    r = x.net_return
    w, l = r[r > 0], r[r < 0]
    return {"variant": name, "observations": len(x), "days": x.signal_date.nunique(),
            "symbols": x.symbol.nunique(), "avg_net_return": r.mean(), "median_net_return": r.median(),
            "win_rate": (r > 0).mean(), "profit_factor": w.sum() / abs(l.sum()) if len(l) else float("inf"),
            "avg_signals_per_day": len(x) / x.signal_date.nunique() if len(x) else 0}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--source', default=str(SRC)); ap.add_argument('--output', default=str(OUT)); args = ap.parse_args()
    x = pd.read_csv(args.source, parse_dates=["signal_time"])
    x["signal_date"] = pd.to_datetime(x["signal_date"]).dt.date
    x = x.dropna(subset=["score"]).sort_values(["signal_time", "symbol"])
    rows = [stat("原始（滚动分桶样本）", x)]
    first = x.sort_values("signal_time").drop_duplicates(["signal_date", "symbol"], keep="first")
    rows.append(stat("每股每日首个一买", first))
    # 30根1m冷却：同股前一个信号后至少30根K，近似用分钟时间差过滤交易时段内重复触发。
    cool = first.sort_values(["symbol", "signal_time"]).copy()
    cool["prev_time"] = cool.groupby("symbol").signal_time.shift()
    cool = cool[cool.prev_time.isna() | ((cool.signal_time - cool.prev_time).dt.total_seconds() >= 30 * 60)]
    rows.append(stat("每股每日首个+30分钟冷却", cool))
    for k in (3, 5, 10):
        top = x.sort_values(["signal_date", "score"], ascending=[True, False]).groupby("signal_date", group_keys=False).head(k)
        rows.append(stat(f"全市场每日Top{k}", top))
    # 严格无泄漏：当天只使用此前 rolling_days 的分数阈值，不使用当天其他信号排序。
    dates = sorted(x.signal_date.unique())
    for q in (0.8, 0.9, 0.95, 0.98, 0.99):
        selected = []
        for i, d in enumerate(dates):
            hist_dates = dates[max(0, i - 10):i]
            hist = x[x.signal_date.isin(hist_dates)].score.dropna()
            if len(hist) < 20:
                continue
            selected.append(x[(x.signal_date == d) & (x.score >= hist.quantile(q))])
        thresholded = pd.concat(selected, ignore_index=True) if selected else x.iloc[0:0]
        rows.append(stat(f"前10日阈值P{int(q*100)}（无泄漏）", thresholded))
    result = pd.DataFrame(rows)
    out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    print(result.to_string(index=False))

if __name__ == "__main__":
    main()
