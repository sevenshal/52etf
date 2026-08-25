#!/usr/bin/env python3
"""Test normalized MA5 weight/price slope ratios on Xueqiu holdings snapshots."""
from __future__ import annotations
import argparse
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd
from scipy import stats


def slope5(values):
    y=np.asarray(values,dtype=float)
    if len(y)<5 or np.any(~np.isfinite(y)) or np.mean(y)==0: return np.nan
    return np.polyfit(np.arange(5),y,1)[0]/np.mean(y)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--db',type=Path,required=True)
    ap.add_argument('--panel',type=Path,required=True); ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    con=duckdb.connect(str(a.db),read_only=True)
    h=con.execute("""
      WITH base AS (
        SELECT snapshot_date,cube_symbol,stock_symbol,sum(weight_pct) weight_pct
        FROM xueqiu_cube_holdings_snapshots WHERE coalesce(is_active,false) AND weight_pct>0 AND stock_symbol<>'CASH'
        GROUP BY 1,2,3), cubes AS (
        SELECT snapshot_date,count(distinct cube_symbol) cube_count FROM base GROUP BY 1), agg AS (
        SELECT b.snapshot_date,b.stock_symbol,count(distinct b.cube_symbol) holding_cubes,
               sum(b.weight_pct)/max(c.cube_count) composite_weight
        FROM base b JOIN cubes c USING(snapshot_date) GROUP BY 1,2)
      SELECT * FROM agg WHERE snapshot_date BETWEEN date '2026-06-04' AND date '2026-08-24' ORDER BY stock_symbol,snapshot_date
    """).df()
    h['ts_code']=h.stock_symbol.str[3:]+'.'+h.stock_symbol.str[:2]
    px=con.execute("""SELECT ts_code,trade_date snapshot_date,close FROM a_stock_market_daily_qfq
      WHERE trade_date BETWEEN date '2026-06-04' AND date '2026-08-24'""").df()
    h=h.merge(px,on=['ts_code','snapshot_date'],how='left').sort_values(['ts_code','snapshot_date'])
    for col in ['composite_weight','close']:
      h[f'{col}_ma5']=h.groupby('ts_code')[col].transform(lambda s:s.rolling(5,min_periods=5).mean())
      h[f'{col}_slope5']=h.groupby('ts_code')[f'{col}_ma5'].transform(lambda s:s.rolling(5,min_periods=5).apply(slope5,raw=True))
    h['signed_slope_ratio']=h.composite_weight_slope5/h.close_slope5
    h['inverse_slope_score']=h.composite_weight_slope5/h.close_slope5.abs().clip(lower=.002)
    panel=pd.read_csv(a.panel,parse_dates=['snapshot_date']); h['snapshot_date']=pd.to_datetime(h.snapshot_date)
    out=panel.merge(h[['snapshot_date','ts_code','composite_weight_ma5','close_ma5','composite_weight_slope5','close_slope5','signed_slope_ratio','inverse_slope_score']],on=['snapshot_date','ts_code'],how='left')
    out=out[(out.holding_cubes>=5)&out.composite_weight_slope5.notna()&out.close_slope5.notna()].copy()
    out['slope_inverse_state']=(out.composite_weight_slope5>0)&(out.close_slope5<0)
    rows=[]
    for universe_name,u in [('all',out),('weight_up_price_down',out[out.slope_inverse_state])]:
      for metric in ['signed_slope_ratio','inverse_slope_score']:
        # In the inverse state, signed ratio is negative: more negative means stronger.
        ascending=(metric=='signed_slope_ratio' and universe_name=='weight_up_price_down')
        score=-u[metric] if ascending else u[metric]
        valid=u[np.isfinite(score)].copy(); valid['score']=score[np.isfinite(score)]
        cuts=valid.score.quantile([.2,.4,.6,.8]).tolist()
        valid['bucket']=pd.cut(valid.score,[-np.inf,*cuts,np.inf],labels=[1,2,3,4,5],include_lowest=True).astype(int)
        for horizon in [1,3,5,10,20]:
          col=f'excess_{horizon}d'; mature=valid.dropna(subset=[col])
          ic=mature.groupby('snapshot_date').apply(lambda g: stats.spearmanr(g.score,g[col]).statistic if len(g)>=5 else np.nan,include_groups=False).mean()
          for bucket,g in mature.groupby('bucket',observed=True):
            daily=g.groupby('snapshot_date')[col].mean(); test=stats.ttest_1samp(daily,0) if len(daily)>1 else None
            rows.append({'universe':universe_name,'metric':metric,'horizon':horizon,'bucket':int(bucket),
              'n':len(g),'dates':len(daily),'score_min':g.score.min(),'score_median':g.score.median(),'score_max':g.score.max(),
              'excess':daily.mean(),'win_rate':(daily>0).mean(),'p':float(test.pvalue) if test else np.nan,'daily_ic':ic})
    results=pd.DataFrame(rows); out.to_csv(a.output/'ma5_slope_panel.csv',index=False); results.to_csv(a.output/'ma5_slope_buckets.csv',index=False)
    print(results[(results.universe=='weight_up_price_down')&(results.metric=='inverse_slope_score')&results.horizon.isin([5,10,20])].to_string(index=False))

if __name__=='__main__': main()
