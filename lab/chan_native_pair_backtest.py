"""Pair native Chan buy/sell events under A-share T+1 constraints."""
from __future__ import annotations
import argparse, sys
from datetime import date
from pathlib import Path
import duckdb, numpy as np, pandas as pd
ROOT=Path(__file__).resolve().parents[1]; sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'lab')]
from chan_native import Kline, calculate, build_segments, detect_buy_sell
from chan_signal_pair_backtest import BacktestConfig, INDEX_CODES, _load_bars, _load_daily_filters, _membership_intervals, _in_intervals

BUY=('一买','二买','三买'); SELL=('一卖','二卖','三卖')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--database',required=True); ap.add_argument('--start-date',default='2026-02-01'); ap.add_argument('--end-date',default='2026-08-28'); ap.add_argument('--supplemental-weights'); ap.add_argument('--output',default='lab/output/chan_signal_pair_backtest_20260829_120d/native_chan_pair_summary.csv'); ap.add_argument('--symbol-limit',type=int)
    a=ap.parse_args(); cfg=BacktestConfig(a.database,date.fromisoformat(a.start_date),date.fromisoformat(a.end_date),100000,20,15,25,a.supplemental_weights)
    con=duckdb.connect(a.database,read_only=True); codes=list(INDEX_CODES)+['932000.CSI']
    w=con.execute(f"select index_code,con_code,trade_date,weight from a_stock_index_weight where index_code in ({','.join('?' for _ in codes)}) and trade_date<=?",[*codes,cfg.end_date]).fetchdf()
    if a.supplemental_weights:
        x=pd.read_csv(a.supplemental_weights); w=pd.concat([w,x[['index_code','con_code','trade_date','weight']]],ignore_index=True)
    w['index_code']=w.index_code.astype(str).str.upper(); w['con_code']=w.con_code.astype(str).str.upper(); w['trade_date']=pd.to_datetime(w.trade_date).dt.date; w=w[w.index_code.isin(codes)].drop_duplicates(['index_code','con_code','trade_date'],keep='last')
    membership=_membership_intervals(w); symbols=sorted(membership)[:a.symbol_limit] if a.symbol_limit else sorted(membership)
    filt=_load_daily_filters(con,symbols,cfg); filt['eligible']=filt.not_st.fillna(False)&(filt.liquidity_observations>=20)&(filt.avg_amount>=100000); eligible={s:dict(zip(g.trade_date,g.eligible,strict=False)) for s,g in filt.groupby('ts_code')}
    rows=[]
    for n,sym in enumerate(symbols,1):
        try:
            bars=_load_bars(con,sym,'1m',cfg)
            if len(bars)<100: continue
            raw=[Kline(i=i,high=float(r.high),low=float(r.low),dt=pd.Timestamp(r.timestamp)) for i,r in enumerate(bars.itertuples())]; _,_,st,z=calculate(raw); seg=build_segments(st); events=sorted(detect_buy_sell(st,seg,z),key=lambda e:(e.confirm_i,e.kind))
            times=pd.to_datetime(bars.timestamp); dates=np.asarray(times.dt.date); day_order={d:i for i,d in enumerate(dict.fromkeys(dates))}
            by_kind={k:[e for e in events if e.kind==k] for k in BUY+SELL}
            for bt in BUY:
                for stype in SELL:
                    entry=None
                    for e in events:
                        if e.confirm_i+1>=len(bars): continue
                        sig_date=dates[e.confirm_i]
                        if entry is None:
                            if e.kind==bt and _in_intervals(sig_date,membership.get(sym,[])) and eligible.get(sym,{}).get(sig_date,False):
                                ix=e.confirm_i+1; entry=(ix,times.iloc[ix],float(bars.iloc[ix].open))
                            continue
                        if e.kind!=stype: continue
                        ix=e.confirm_i+1; exit_date=dates[ix]
                        if day_order[exit_date]-day_order[entry[1].date()]<1: continue
                        ep=entry[2]; xp=float(bars.iloc[ix].open); net=xp*(1-25/10000)/(ep*(1+15/10000))-1
                        rows.append({'symbol':sym,'buy_type':bt,'sell_type':stype,'entry_time':entry[1],'exit_time':times.iloc[ix],'net_return':net,'exit_reason':'signal'}); entry=None
                    if entry is not None:
                        ix=len(bars)-1; ep=entry[2]; xp=float(bars.iloc[ix].close); net=xp*(1-25/10000)/(ep*(1+15/10000))-1
                        rows.append({'symbol':sym,'buy_type':bt,'sell_type':stype,'entry_time':entry[1],'exit_time':times.iloc[ix],'net_return':net,'exit_reason':'end_of_data'})
        except Exception: continue
        if n%100==0 or n==len(symbols): print(f'pair native: {n}/{len(symbols)} symbols, {len(rows)} trades',flush=True)
    con.close(); frame=pd.DataFrame(rows); out=[]
    for bt in BUY:
        for stype in SELL:
            x=frame[(frame.buy_type==bt)&(frame.sell_type==stype)] if not frame.empty else frame
            r=x.net_return.astype(float) if len(x) else pd.Series(dtype=float); win=r[r>0]; loss=r[r<0]
            closed=x[x.exit_reason=='signal'] if len(x) else x
            cr=closed.net_return.astype(float) if len(closed) else pd.Series(dtype=float)
            out.append({'buy_type':bt,'sell_type':stype,'observations':len(x),'signal_closed':len(closed),'end_of_data':int((x.exit_reason=='end_of_data').sum()) if len(x) else 0,'symbols':x.symbol.nunique() if len(x) else 0,'avg_net_return':r.mean(),'median_net_return':r.median(),'win_rate':(r>0).mean() if len(r) else np.nan,'pf':win.sum()/abs(loss.sum()) if len(loss) else np.nan,'signal_avg_net_return':cr.mean() if len(cr) else np.nan,'signal_win_rate':(cr>0).mean() if len(cr) else np.nan})
    p=ROOT/a.output; p.parent.mkdir(parents=True,exist_ok=True); pd.DataFrame(out).to_csv(p,index=False); frame.to_csv(p.with_name(p.stem+'_trades.csv'),index=False); print(pd.DataFrame(out).to_string(index=False))
if __name__=='__main__': main()
