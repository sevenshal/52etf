#!/usr/bin/env python3
"""Evaluate the Xueqiu 5-day weight/price ratio without look-ahead.

Reads the production DuckDB in read-only mode and writes reproducible CSV/JSON/MD
artifacts. A signal observed after snapshot-date close enters at next trading-day
open; forward returns are measured to subsequent closes.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--start", default="2026-07-01")
    p.add_argument("--end", default="2026-08-24")
    p.add_argument("--output", type=Path, default=Path("lab/output/xueqiu_weight_price_ratio_20260701"))
    p.add_argument("--cost-bps", type=float, default=20.0, help="Round-trip cost")
    return p.parse_args()


def t_test(values):
    x = pd.Series(values).dropna().astype(float)
    if len(x) < 2:
        return {"n": len(x), "mean": None, "t": None, "p": None}
    result = stats.ttest_1samp(x, 0.0)
    return {"n": len(x), "mean": x.mean(), "t": float(result.statistic), "p": float(result.pvalue)}


def pct(x):
    return "—" if x is None or pd.isna(x) else f"{100*x:.2f}%"


def main():
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(args.db), read_only=True)
    # Match production aggregation and its fifth-prior-snapshot comparison.
    query = """
    WITH base AS (
      SELECT snapshot_date, cube_symbol, stock_symbol, any_value(stock_name) stock_name,
             sum(weight_pct) weight_pct
      FROM xueqiu_cube_holdings_snapshots
      WHERE coalesce(is_active,false) AND weight_pct>0 AND stock_symbol<>'CASH'
      GROUP BY 1,2,3
    ), agg AS (
      SELECT snapshot_date,stock_symbol,any_value(stock_name) stock_name,
             count(distinct cube_symbol) holding_cubes,sum(weight_pct) total_weight
      FROM base GROUP BY 1,2
    ), ranked AS (
      SELECT *,row_number() over(partition by snapshot_date order by total_weight desc,holding_cubes desc,stock_symbol desc) composite_rank
      FROM agg
    ), dates AS (
      SELECT snapshot_date,lag(snapshot_date,5) over(order by snapshot_date) compare_date FROM (select distinct snapshot_date from ranked)
    )
    SELECT r.*,d.compare_date,p.holding_cubes prior_cubes,p.total_weight prior_weight,p.composite_rank prior_rank
    FROM ranked r JOIN dates d using(snapshot_date)
    LEFT JOIN ranked p ON p.snapshot_date=d.compare_date AND p.stock_symbol=r.stock_symbol
    WHERE r.snapshot_date BETWEEN ? AND ?
    """
    signals = con.execute(query, [args.start, args.end]).df()
    dates = {pd.Timestamp(x).date() for x in con.execute("select distinct trade_date from a_stock_market_daily_qfq where trade_date between ? and ? order by 1", [args.start, args.end]).df()["trade_date"]}
    snapshot_dates = {pd.Timestamp(x).date() for x in signals.snapshot_date.unique()}
    missing_weekdays = [str(d.date()) for d in pd.date_range(args.start, args.end, freq="B") if d.date() not in snapshot_dates and d.date() in dates]
    symbols = signals.stock_symbol.str[3:] + "." + signals.stock_symbol.str[:2]
    signals["ts_code"] = symbols
    price_start = str(pd.Timestamp(args.start) - pd.Timedelta(days=15))[:10]
    prices = con.execute("""
      SELECT ts_code,trade_date,open,close FROM a_stock_market_daily_qfq
      WHERE trade_date BETWEEN ? AND ? AND open>0 AND close>0
    """, [price_start, args.end]).df().sort_values(["ts_code","trade_date"])
    price_map = {(r.ts_code, r.trade_date): (r.open, r.close) for r in prices.itertuples()}
    calendar = sorted(prices.trade_date.unique().tolist())
    date_pos = {d:i for i,d in enumerate(calendar)}
    rows=[]
    for r in signals.itertuples():
        if r.holding_cubes < 5 or pd.isna(r.compare_date): continue
        now = price_map.get((r.ts_code,r.snapshot_date)); old = price_map.get((r.ts_code,r.compare_date))
        if not now or not old: continue
        pm=now[1]/old[1]
        if pm<=0: continue
        is_new = pd.isna(r.prior_weight) or r.prior_weight <= 0
        wm = np.nan if is_new else r.total_weight/r.prior_weight
        ratio = np.nan if is_new else wm/pm
        if is_new: direction="新进"
        elif wm > 1.05 and ratio > 1.05: direction="顺势加仓" if pm >= 1 else "逆势吸筹"
        elif wm < 0.95 and ratio < 0.95: direction="借涨减仓" if pm >= 1 else "减仓"
        else: direction="持平"
        item=r._asdict(); item.update(weight_multiple=wm,price_multiple=pm,ratio=ratio,direction=direction,
          cube_change=(r.holding_cubes-r.prior_cubes) if not is_new else np.nan,
          weight_change=(r.total_weight-r.prior_weight) if not is_new else np.nan,
          momentum_5d=pm-1)
        pos=date_pos.get(r.snapshot_date)
        if pos is None or pos+1>=len(calendar): continue
        entry_date=calendar[pos+1]; entry=price_map.get((r.ts_code,entry_date))
        if not entry: continue
        item["entry_date"]=entry_date; item["entry_open"]=entry[0]
        for h in [1,3,5,10,20]:
            exit_pos=pos+h
            out=price_map.get((r.ts_code,calendar[exit_pos])) if exit_pos < len(calendar) else None
            item[f"ret_{h}d"]=(out[1]/entry[0]-1) if out else np.nan
        rows.append(item)
    panel=pd.DataFrame(rows)
    # Winsorize ratio only for statistical sorting/regression; preserve raw values.
    panel["ratio_w"] = panel["ratio"].clip(panel.ratio.quantile(.01), panel.ratio.quantile(.99))
    panel["eligible"]=(panel.composite_rank<=100)&(panel.holding_cubes>=8)&(panel.cube_change>=2)&(panel.weight_change>0)&(panel.ratio>=1.15)
    for h in [1,3,5,10,20]:
        col=f"ret_{h}d"
        panel[f"excess_{h}d"] = panel[col] - panel.groupby("snapshot_date")[col].transform("mean")
    summaries=[]; daily=[]
    for h in [1,3,5,10,20]:
        col=f"ret_{h}d"; mature=panel.dropna(subset=[col,"ratio_w"]).copy()
        for d,g in mature.groupby("snapshot_date"):
            if len(g)<20: continue
            g=g.copy(); g["decile"]=pd.qcut(g.ratio_w.rank(method="first"),10,labels=False)
            rho=stats.spearmanr(g.ratio_w,g[col]).statistic
            top=g[g.decile==9][col].mean(); bottom=g[g.decile==0][col].mean()
            daily.append({"horizon":h,"snapshot_date":d,"n":len(g),"ic":rho,"top":top,"bottom":bottom,"spread":top-bottom})
        dg=pd.DataFrame([x for x in daily if x["horizon"]==h])
        elig=mature[mature.eligible]
        summaries.append({"horizon":h,"stock_days":len(mature),"dates":mature.snapshot_date.nunique(),
          "ic_mean":dg.ic.mean(),"ic_positive":(dg.ic>0).mean(),"top_return":dg.top.mean(),
          "bottom_return":dg.bottom.mean(),"spread":dg.spread.mean(),"spread_p":t_test(dg.spread)["p"],
          "eligible_n":len(elig),"eligible_return":elig[col].mean(),"eligible_excess":elig[f"excess_{h}d"].mean(),
          "eligible_win":(elig[f"excess_{h}d"]>0).mean()})
    summary=pd.DataFrame(summaries)
    # Same-universe top-10 event portfolios: ratio and key ablations.
    methods={"ratio":"ratio_w","weight_multiple":"weight_multiple","cube_change":"cube_change","rank":"composite_rank","price_momentum":"momentum_5d"}
    portfolio=[]
    base=panel[(panel.composite_rank<=100)&(panel.holding_cubes>=8)&(panel.cube_change>=2)&(panel.weight_change>0)]
    for h in [5,10]:
      for name,col in methods.items():
        vals=[]
        for d,g in base.dropna(subset=[f"ret_{h}d"]).groupby("snapshot_date"):
          ascending=name=="rank"; pick=g.sort_values(col,ascending=ascending).head(10)
          if len(pick): vals.append(pick[f"ret_{h}d"].mean()-g[f"ret_{h}d"].mean()-args.cost_bps/10000)
        test=t_test(vals); portfolio.append({"horizon":h,"method":name,"events":len(vals),"mean_net":test["mean"],"win_rate":float((pd.Series(vals)>0).mean()),"p":test["p"]})
    portfolio=pd.DataFrame(portfolio)
    behavior=[]
    direction_order=["新进","持平","顺势加仓","逆势吸筹","借涨减仓","减仓"]
    for h in [1,3,5,10,20]:
      col=f"ret_{h}d"; ecol=f"excess_{h}d"
      mature=panel.dropna(subset=[col])
      for direction in direction_order:
        group=mature[mature.direction==direction]
        daily_group=group.groupby("snapshot_date").agg(abs_return=(col,"mean"),excess=(ecol,"mean"),stocks=("ts_code","size")).reset_index()
        test=t_test(daily_group.excess)
        behavior.append({"horizon":h,"direction":direction,"stock_days":len(group),"dates":len(daily_group),
          "avg_stocks_per_date":daily_group.stocks.mean(),"absolute_return":daily_group.abs_return.mean(),
          "excess_return":daily_group.excess.mean(),"excess_win":(daily_group.excess>0).mean(),"excess_p":test["p"]})
    behavior=pd.DataFrame(behavior)
    contrarian=panel[panel.direction=="逆势吸筹"].copy()
    contrarian["ratio_bucket"]=(contrarian.groupby("snapshot_date")["ratio"].rank(method="first",pct=True)*10).apply(np.ceil).clip(1,10).astype(int)
    bucket_rows=[]
    for h in [1,3,5,10,20]:
      col=f"ret_{h}d"; ecol=f"excess_{h}d"
      mature=contrarian.dropna(subset=[col])
      for bucket in range(1,11):
        group=mature[mature.ratio_bucket==bucket]
        daily_group=group.groupby("snapshot_date").agg(abs_return=(col,"mean"),excess=(ecol,"mean"),stocks=("ts_code","size")).reset_index()
        test=t_test(daily_group.excess)
        bucket_rows.append({"horizon":h,"bucket":bucket,"stock_days":len(group),"dates":len(daily_group),
          "ratio_min":group.ratio.min(),"ratio_median":group.ratio.median(),"ratio_max":group.ratio.max(),
          "absolute_return":daily_group.abs_return.mean(),"excess_return":daily_group.excess.mean(),
          "excess_win":(daily_group.excess>0).mean(),"excess_p":test["p"]})
    buckets=pd.DataFrame(bucket_rows)
    behavior_bucket_rows=[]
    bucket_directions=["持平","顺势加仓","逆势吸筹","借涨减仓","减仓"]
    fixed_cutpoints=[]
    for direction in bucket_directions:
      subset=panel[panel.direction==direction].copy()
      metric="ratio"
      subset=subset.dropna(subset=[metric])
      boundaries=subset[metric].quantile([.2,.4,.6,.8]).tolist()
      fixed_cutpoints.extend({"direction":direction,"boundary_after_bucket":i,"cutpoint":value}
        for i,value in enumerate(boundaries,start=1))
      subset["strength_bucket"]=pd.cut(subset[metric],[-np.inf,*boundaries,np.inf],labels=[1,2,3,4,5],include_lowest=True).astype(int)
      for h in [1,3,5,10,20]:
        col=f"ret_{h}d"; ecol=f"excess_{h}d"; mature=subset.dropna(subset=[col])
        for bucket in range(1,6):
          group=mature[mature.strength_bucket==bucket]
          daily_group=group.groupby("snapshot_date").agg(abs_return=(col,"mean"),excess=(ecol,"mean"),stocks=("ts_code","size")).reset_index()
          test=t_test(daily_group.excess)
          behavior_bucket_rows.append({"direction":direction,"bucket_metric":metric,"horizon":h,"bucket":bucket,
            "stock_days":len(group),"dates":len(daily_group),"metric_min":group[metric].min(),
            "metric_median":group[metric].median(),"metric_max":group[metric].max(),
            "absolute_return":daily_group.abs_return.mean(),"excess_return":daily_group.excess.mean(),
            "excess_win":(daily_group.excess>0).mean(),"excess_p":test["p"]})
    behavior_buckets=pd.DataFrame(behavior_bucket_rows)
    cutpoints=pd.DataFrame(fixed_cutpoints)
    panel.to_csv(args.output/"factor_panel.csv",index=False)
    pd.DataFrame(daily).to_csv(args.output/"daily_factor_results.csv",index=False)
    summary.to_csv(args.output/"summary.csv",index=False); portfolio.to_csv(args.output/"portfolio_ablation.csv",index=False)
    behavior.to_csv(args.output/"behavior_groups.csv",index=False)
    buckets.to_csv(args.output/"contrarian_ratio_buckets.csv",index=False)
    behavior_buckets.to_csv(args.output/"behavior_five_buckets.csv",index=False)
    cutpoints.to_csv(args.output/"behavior_global_quintile_cutpoints.csv",index=False)
    quality={"snapshot_min":str(signals.snapshot_date.min()),"snapshot_max":str(signals.snapshot_date.max()),
      "snapshot_days":int(signals.snapshot_date.nunique()),"panel_rows":len(panel),"symbols":int(panel.ts_code.nunique()),"minimum_holding_cubes":5,
      "missing_snapshot_trading_days":missing_weekdays,"price_max":str(prices.trade_date.max()),"round_trip_cost_bps":args.cost_bps}
    (args.output/"metrics.json").write_text(json.dumps(quality,ensure_ascii=False,indent=2,default=str))
    lines=["# 雪球5日权价比有效性实验", "", f"区间：{args.start}—{args.end}；信号后下一交易日开盘成交；组合消融扣双边合计 {args.cost_bps:.0f} bps。", "",
      "## 数据质量", "", f"- 仅保留当前至少被5个活跃组合持仓的股票：{quality['snapshot_days']} 个快照日、{quality['panel_rows']} 个股票-日、{quality['symbols']} 只股票。", f"- 缺失交易日快照：{', '.join(missing_weekdays) or '无'}。", f"- 行情截至 {quality['price_max']}；靠近结束日的远期收益自动不纳入。", "", "## 六类行为结果（每日等权，避免某天股票多而过度加权）", "",
      "|持有期|行为|股票日|截面数|每日平均股票数|绝对收益|相对同日股票池|超额胜率|p值|", "|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in behavior.itertuples(): lines.append(f"|{r.horizon}日|{r.direction}|{r.stock_days}|{r.dates}|{r.avg_stocks_per_date:.1f}|{pct(r.absolute_return)}|{pct(r.excess_return)}|{pct(r.excess_win)}|{r.excess_p:.3f}|")
    lines += ["", "## 逆势吸筹：每日权价比十桶（1最低，10最高）", "",
      "|持有期|桶|股票日|截面数|权价比中位数|绝对收益|相对同日股票池|超额胜率|p值|", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in buckets.itertuples(): lines.append(f"|{r.horizon}日|{r.bucket}|{r.stock_days}|{r.dates}|{r.ratio_median:.2f}|{pct(r.absolute_return)}|{pct(r.excess_return)}|{pct(r.excess_win)}|{r.excess_p:.3f}|")
    lines += ["", "## 五类行为各自五桶", "",
      "新进没有权价比，不参与分桶。其余行为先在全区间分类内等频求20/40/60/80%切点，再以固定切点应用到每天；1最低、5最高。", "",
      "|行为|持有期|桶|指标中位数|股票日|截面数|绝对收益|相对同日股票池|超额胜率|p值|", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in behavior_buckets.itertuples(): lines.append(f"|{r.direction}|{r.horizon}日|{r.bucket}|{r.metric_median:.2f}|{r.stock_days}|{r.dates}|{pct(r.absolute_return)}|{pct(r.excess_return)}|{pct(r.excess_win)}|{r.excess_p:.3f}|")
    lines += ["", "## 连续权价比因子结果（新进无权价比，不参与）", "",
      "|持有期|股票日|日截面数|平均Rank IC|IC为正比例|Top-Decile|Bottom-Decile|多空差|差异p值|生产资格绝对收益|相对同日股票池|超额胜率|", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in summary.itertuples(): lines.append(f"|{r.horizon}日|{r.stock_days}|{r.dates}|{r.ic_mean:.3f}|{pct(r.ic_positive)}|{pct(r.top_return)}|{pct(r.bottom_return)}|{pct(r.spread)}|{r.spread_p:.3f}|{pct(r.eligible_return)}|{pct(r.eligible_excess)}|{pct(r.eligible_win)}|")
    lines += ["", "## 同股票池Top10消融（相对基础候选池的事件平均净超额）", "", "|持有期|指标|事件数|平均净超额|超额胜率|p值|", "|---:|---|---:|---:|---:|---:|"]
    for r in portfolio.itertuples(): lines.append(f"|{r.horizon}日|{r.method}|{r.events}|{pct(r.mean_net)}|{pct(r.win_rate)}|{r.p:.3f}|")
    (args.output/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(args.output/"report.md")


if __name__ == "__main__": main()
