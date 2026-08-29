from pathlib import Path
import duckdb, pandas as pd, numpy as np

root=Path(__file__).resolve().parents[1]/'lab/output/chan_signal_pair_backtest_20260829_120d'
x=pd.read_csv(root/'1m_buy_strength_trades_merged.csv',parse_dates=['signal_time']); x=x.dropna(subset=['score']); x['signal_date']=pd.to_datetime(x.signal_date).dt.date
trades=pd.read_csv(root/'t1_close_trades.csv',parse_dates=['signal_time','entry_time','t1_close_time'])
x=x.merge(trades[['symbol','signal_time','entry_time','t1_close_time','entry_price']],on=['symbol','signal_time'],how='left')
dates=sorted(x.signal_date.unique()); parts=[]
for i,d in enumerate(dates):
    h=x[x.signal_date.isin(dates[max(0,i-10):i])].score
    if len(h)>=20: parts.append(x[(x.signal_date==d)&(x.score>=h.quantile(.99))])
x=pd.concat(parts,ignore_index=True).sort_values('signal_time').drop_duplicates(['signal_date','symbol']).reset_index(drop=True); x['trade_id']=x.index
c=duckdb.connect('/home/quantd/quant_prod/quant_robot/analytics.duckdb',read_only=True); c.register('trades',x[['trade_id','symbol','entry_time','t1_close_time','entry_price']])
rows=[]
for target,stop in [(.003,-.01),(.005,-.01),(.005,-.02),(.01,-.02)]:
    q=c.execute(f'''WITH hits AS (SELECT t.trade_id,m.trade_time,(m.high>=t.entry_price*(1+{target})) hit_target,(m.low<=t.entry_price*(1+{stop})) hit_stop FROM trades t JOIN a_stock_minute_bar_qfq m ON m.ts_code=t.symbol AND m.trade_time>t.entry_time AND m.trade_time<=t.t1_close_time WHERE m.high>=t.entry_price*(1+{target}) OR m.low<=t.entry_price*(1+{stop})), firsts AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY trade_id ORDER BY trade_time) rn FROM hits) SELECT COUNT(*) FILTER(WHERE rn=1 AND hit_target AND NOT hit_stop),COUNT(*) FILTER(WHERE rn=1 AND hit_stop AND NOT hit_target),COUNT(*) FILTER(WHERE rn=1 AND hit_target AND hit_stop) FROM firsts''').fetchone()
    rows.append({'target':target,'stop':stop,'observations':len(x),'target_first':q[0],'stop_first':q[1],'same_bar':q[2]})
c.close(); out=pd.DataFrame(rows); out['target_first_rate']=out.target_first/out.observations; out['stop_first_rate']=out.stop_first/out.observations; out['same_bar_rate']=out.same_bar/out.observations; out.to_csv(root/'p99_first_hit_summary.csv',index=False); print(out.to_string(index=False))
