"""无泄漏 P99 一买 + 日线过滤对照。"""
from pathlib import Path
import argparse
import duckdb, pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'lab/output/chan_signal_pair_backtest_20260826_marked/1m_buy_strength_trades.csv'
OUT = ROOT / 'lab/output/chan_signal_pair_backtest_20260826_marked/1m_p99_daily_filter_summary.csv'

def stat(name, x):
    r=x.net_return; w=r[r>0]; l=r[r<0]
    return {'variant':name,'observations':len(x),'days':x.signal_date.nunique(),'avg_signals_per_day':len(x)/x.signal_date.nunique() if len(x) else 0,
            'avg_net_return':r.mean(),'median_net_return':r.median(),'win_rate':(r>0).mean(),'profit_factor':w.sum()/abs(l.sum()) if len(l) else float('inf')}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source',default=str(SRC)); ap.add_argument('--output',default=str(OUT)); args=ap.parse_args()
    x=pd.read_csv(args.source,parse_dates=['signal_time']); x=x.dropna(subset=['score']); x['signal_date']=pd.to_datetime(x.signal_date).dt.date
    dates=sorted(x.signal_date.unique()); selected=[]
    for i,d in enumerate(dates):
        h=x[x.signal_date.isin(dates[max(0,i-10):i])].score
        if len(h)>=20: selected.append(x[(x.signal_date==d)&(x.score>=h.quantile(.99))])
    p99=pd.concat(selected,ignore_index=True).sort_values('signal_time').drop_duplicates(['signal_date','symbol'])
    con=duckdb.connect('/home/quantd/quant_prod/quant_robot/analytics.duckdb',read_only=True)
    symbols=sorted(p99.symbol.unique()); con.register('syms',pd.DataFrame({'ts_code':symbols}))
    d=con.execute("""select d.ts_code,d.trade_date,d.close from a_stock_market_daily_qfq d join syms s using(ts_code)
                     where d.trade_date between DATE '2026-01-01' and DATE '2026-08-27' order by d.ts_code,d.trade_date""").fetchdf(); con.close()
    d['trade_date']=pd.to_datetime(d.trade_date).dt.date
    d['ema20']=d.groupby('ts_code').close.transform(lambda s:s.ewm(span=20,adjust=False).mean())
    d['ema20_prev']=d.groupby('ts_code').ema20.shift(1); d['ret20']=d.groupby('ts_code').close.pct_change(20)
    p99=p99.merge(d,left_on=['symbol','signal_date'],right_on=['ts_code','trade_date'],how='left')
    rows=[stat('P99（无日线过滤）',p99)]
    for name,mask in [('日线收盘>EMA20',p99.close>p99.ema20),('日线EMA20向上',p99.ema20>p99.ema20_prev),('日线收盘>EMA20且EMA向上',(p99.close>p99.ema20)&(p99.ema20>p99.ema20_prev)),('日线20日收益>-5%',p99.ret20>-0.05)]:
        rows.append(stat('P99+'+name,p99[mask.fillna(False)]))
    result=pd.DataFrame(rows); out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True); result.to_csv(out,index=False); print(result.to_string(index=False))

if __name__=='__main__': main()
