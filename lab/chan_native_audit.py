"""Audit invariants of the native Chan structure on a symbol sample."""
from __future__ import annotations
import argparse, sys
from pathlib import Path
import duckdb, pandas as pd
ROOT=Path(__file__).resolve().parents[1]; sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'lab')]
from chan_native import Kline, build_segments, calculate, detect_buy_sell
from chan_signal_pair_backtest import BacktestConfig, _load_bars

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--database',required=True); ap.add_argument('--symbol-limit',type=int,default=100); ap.add_argument('--output',default='lab/output/chan_signal_pair_backtest_20260829_120d/native_structure_audit.csv'); a=ap.parse_args()
    con=duckdb.connect(a.database,read_only=True); src=ROOT/'lab/output/chan_signal_pair_backtest_20260829_120d/native_chan_1m_t1_trades.csv'; symbols=sorted(pd.read_csv(src,usecols=['symbol']).symbol.astype(str).unique())[:a.symbol_limit]; cfg=BacktestConfig(a.database,__import__('datetime').date(2026,2,1),__import__('datetime').date(2026,8,28),100000,20,15,25,None); rows=[]
    for sym in symbols:
        b=_load_bars(con,sym,'1m',cfg); raw=[Kline(i=i,high=float(r.high),low=float(r.low),dt=r.timestamp) for i,r in enumerate(b.itertuples())]; norm,fx,st,zs=calculate(raw); seg=build_segments(st); ev=detect_buy_sell(st,seg,zs)
        rows.append({'symbol':sym,'bars':len(raw),'strokes':len(st),'segments':len(seg),'centers':len(zs),'events':len(ev),'alternating_strokes':all(st[i].direction!=st[i-1].direction for i in range(1,len(st))),'segments_ge3':all(s.end_stroke-s.start_stroke+1>=3 for s in seg),'centers_valid':all(z.zd<=z.zg for z in zs),'confirm_monotonic':all(st[i].confirm_i>=st[i-1].confirm_i for i in range(1,len(st))),'event_confirm_in_range':all(0<=e.confirm_i<len(raw) for e in ev)})
    con.close(); out=pd.DataFrame(rows); out['all_invariants']=out[['alternating_strokes','segments_ge3','centers_valid','confirm_monotonic','event_confirm_in_range']].all(axis=1); p=ROOT/a.output; p.parent.mkdir(parents=True,exist_ok=True); out.to_csv(p,index=False); print(out[['symbol','bars','strokes','segments','centers','events','all_invariants']].to_string(index=False)); print('PASS',int(out.all_invariants.sum()),'/',len(out))
if __name__=='__main__': main()
