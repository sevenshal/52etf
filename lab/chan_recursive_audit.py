"""Audit native structural recursion on daily and 30m data."""
from __future__ import annotations
import argparse, sys
from datetime import date
from pathlib import Path
import duckdb, pandas as pd
ROOT=Path(__file__).resolve().parents[1]; sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'lab')]
from chan_native import Kline, recursive_levels
from chan_signal_pair_backtest import BacktestConfig, _load_bars

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--database',required=True); ap.add_argument('--symbol-limit',type=int,default=100); ap.add_argument('--output',default='lab/output/chan_signal_pair_backtest_20260829_120d/native_recursive_audit.csv'); a=ap.parse_args()
    cfg=BacktestConfig(a.database,date(2020,1,1),date(2026,8,28),100000,20,15,25,None); con=duckdb.connect(a.database,read_only=True)
    src=ROOT/'lab/output/chan_signal_pair_backtest_20260829_120d/native_chan_1m_t1_trades.csv'; symbols=sorted(pd.read_csv(src,usecols=['symbol']).symbol.astype(str).unique())[:a.symbol_limit]; rows=[]
    for sym in symbols:
        rec={'symbol':sym}
        for freq in ('30m','d'):
            b=_load_bars(con,sym,freq,cfg)
            raw=[Kline(i=i,high=float(r.high),low=float(r.low),dt=pd.Timestamp(r.timestamp)) for i,r in enumerate(b.itertuples())]
            levels=recursive_levels(raw,levels=3,min_gap=4)
            rec[f'{freq}_levels']=len(levels)
            rec[f'{freq}_l0_segments']=len(levels[0]['segments']) if levels else 0
            rec[f'{freq}_l1_segments']=len(levels[1]['segments']) if len(levels)>1 else 0
            causal=True
            for lo, hi in zip(levels, levels[1:]):
                for higher in hi['segments']:
                    contributing=lo['segments'][higher.start_stroke:higher.end_stroke+1]
                    if contributing and higher.confirm_i < max(x.confirm_i for x in contributing):
                        causal=False
            rec[f'{freq}_causal']=causal
        rows.append(rec)
    con.close(); out=pd.DataFrame(rows); p=ROOT/a.output; p.parent.mkdir(parents=True,exist_ok=True); out.to_csv(p,index=False); print(out.describe(include='all').to_string())
if __name__=='__main__': main()
