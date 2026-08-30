"""Self-contained Chan structural backtest (no CZSC signal engine)."""
from __future__ import annotations
import argparse, sys
from datetime import date
from pathlib import Path
import duckdb, numpy as np, pandas as pd
ROOT=Path(__file__).resolve().parents[1]; sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'lab')]
from chan_native import Kline, calculate, build_segments, detect_buy_sell, detect_buy_sell_classic, recursive_levels
from chan_signal_pair_backtest import (BacktestConfig, INDEX_CODES, _in_intervals,
                                       _load_bars, _load_daily_filters,
                                       _membership_intervals)

def native_up_states(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=['timestamp', 'uptrend'])
    raw=[Kline(i=i,high=float(r.high),low=float(r.low),dt=pd.Timestamp(r.timestamp)) for i,r in enumerate(frame.itertuples())]
    _,_,st,_=calculate(raw,min_gap=4); seg=build_segments(st)
    out=[]; latest=False; j=0
    for i,r in enumerate(frame.itertuples()):
        while j < len(seg) and seg[j].confirm_i <= i:
            latest = seg[j].direction == 'up'; j += 1
        out.append({'timestamp':pd.Timestamp(r.timestamp),'uptrend':latest})
    return pd.DataFrame(out)

def recursive_up_states(frame: pd.DataFrame) -> pd.DataFrame:
    """Return only the direction of completed recursive level-1 segments.

    No level-1 segment means ``uptrend`` is False and ``available`` is False;
    callers must not interpret missing higher-level structure as a downtrend.
    """
    if frame.empty:
        return pd.DataFrame(columns=['timestamp', 'uptrend', 'available'])
    raw=[Kline(i=i,high=float(r.high),low=float(r.low),dt=pd.Timestamp(r.timestamp)) for i,r in enumerate(frame.itertuples())]
    levels=recursive_levels(raw, levels=2, min_gap=4)
    high=levels[1]['segments'] if len(levels)>1 else []
    out=[]; j=0; latest=False
    for i,r in enumerate(frame.itertuples()):
        while j < len(high) and high[j].confirm_i <= i:
            latest=high[j].direction == 'up'; j += 1
        out.append({'timestamp':pd.Timestamp(r.timestamp),'uptrend':latest,'available':j>0})
    return pd.DataFrame(out)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--database',required=True); ap.add_argument('--start-date',default='2026-02-01'); ap.add_argument('--end-date',default='2026-08-28'); ap.add_argument('--output',default='lab/output/chan_signal_pair_backtest_20260829_120d/native_chan_1m_t1_summary.csv'); ap.add_argument('--symbol-limit',type=int); ap.add_argument('--classic-macd',action='store_true'); ap.add_argument('--supplemental-weights', help='补充指数权重CSV；必须含 index_code,con_code,trade_date[,weight]')
    a=ap.parse_args(); cfg=BacktestConfig(a.database,date.fromisoformat(a.start_date),date.fromisoformat(a.end_date),100000,20,15,25,a.supplemental_weights)
    con=duckdb.connect(a.database,read_only=True)
    # Build the pool from historical membership intervals, never from the
    # previous signal sample.  932000.CSI is the only accepted CSI 2000 code;
    # 000932.SH is an unrelated index and must not be used as a substitute.
    pool_codes=list(INDEX_CODES)+['932000.CSI']
    weights=con.execute(
        f"select index_code,con_code,trade_date,weight from a_stock_index_weight where index_code in ({','.join('?' for _ in pool_codes)}) and trade_date<=?",
        [*pool_codes,cfg.end_date]).fetchdf()
    if cfg.supplemental_weights:
        extra=pd.read_csv(cfg.supplemental_weights)
        required={'index_code','con_code','trade_date'}
        if not required.issubset(extra.columns):
            raise ValueError(f'补充权重文件缺少列: {sorted(required-set(extra.columns))}')
        if 'weight' not in extra.columns: extra['weight']=np.nan
        weights=pd.concat([weights,extra[['index_code','con_code','trade_date','weight']]],ignore_index=True)
    weights['index_code']=weights['index_code'].astype(str).str.upper()
    weights['con_code']=weights['con_code'].astype(str).str.upper()
    weights['trade_date']=pd.to_datetime(weights['trade_date']).dt.date
    weights=weights[weights.index_code.isin(pool_codes)].drop_duplicates(['index_code','con_code','trade_date'],keep='last')
    available=sorted(set(weights.index_code))
    missing=sorted(set(pool_codes)-set(available))
    if missing: print(f'WARNING: 缺少历史成分数据，未纳入: {missing}',flush=True)
    intervals=_membership_intervals(weights)
    symbols=sorted(intervals)
    symbols=symbols[:a.symbol_limit] if a.symbol_limit else symbols
    filt=_load_daily_filters(con,symbols,cfg)
    filt['eligible']=filt['not_st'].fillna(False) & (filt['liquidity_observations']>=20) & (filt['avg_amount']>=100000)
    eligible={s:dict(zip(g.trade_date,g.eligible,strict=False)) for s,g in filt.groupby('ts_code')}
    rows=[]; daily_states={}
    for n,sym in enumerate(symbols,1):
        try:
            b=_load_bars(con,sym,'1m',cfg)
            if len(b)<100: continue
            raw=[Kline(i=i,high=float(r.high),low=float(r.low),close=float(r.close),dt=pd.Timestamp(r.timestamp)) for i,r in enumerate(b.itertuples())]
            norm,fx,st,zs=calculate(raw,min_gap=4); seg=build_segments(st); events=(detect_buy_sell_classic(st,seg,zs,raw) if a.classic_macd else detect_buy_sell(st,seg,zs))
            events=[e for e in events if e.kind in ('一买','二买','三买') and e.confirm_i+1<len(b)]
            if not events: continue
            # Higher-level recursion needs warm-up history.  The evaluation
            # window remains cfg.start_date..cfg.end_date; these earlier bars
            # are used only to establish a causal daily structure state.
            daily_cfg=BacktestConfig(cfg.database,date(2020,1,1),cfg.end_date,cfg.min_avg_amount,cfg.liquidity_days,cfg.buy_cost_bps,cfg.sell_cost_bps,cfg.supplemental_weights)
            db=_load_bars(con,sym,'d',daily_cfg)
            draw=[Kline(i=i,high=float(r.high),low=float(r.low),dt=pd.Timestamp(r.timestamp)) for i,r in enumerate(db.itertuples())]
            dl=calculate(draw,min_gap=4); dseg=build_segments(dl[2])
            # A daily state may only use segments already confirmed by that
            # completed daily bar; never use the final segment's later shape.
            daily_states[sym]={}; daily_recursive_states={}
            d_recursive=recursive_up_states(db)
            for rr in d_recursive.itertuples():
                if rr.available:
                    daily_recursive_states[pd.Timestamp(rr.timestamp).date()] = bool(rr.uptrend)
            for i,r in enumerate(db.itertuples()):
                confirmed=[s for s in dseg if s.confirm_i <= i]
                daily_states[sym][pd.Timestamp(r.timestamp).date()] = bool(confirmed and confirmed[-1].direction == 'up')
            m30_cfg=BacktestConfig(cfg.database,date(2025,1,1),cfg.end_date,cfg.min_avg_amount,cfg.liquidity_days,cfg.buy_cost_bps,cfg.sell_cost_bps,cfg.supplemental_weights)
            m30_frame=_load_bars(con,sym,'30m',m30_cfg)
            m30_states=native_up_states(m30_frame)
            m30_recursive=recursive_up_states(m30_frame)
            times=pd.to_datetime(b.timestamp); dates=times.dt.date.to_numpy()
            for e in events:
                if e.kind not in ('一买','二买','三买') or e.confirm_i+1>=len(b): continue
                i=e.confirm_i; d=dates[i]
                if not _in_intervals(d, intervals.get(sym, [])): continue
                if not eligible.get(sym, {}).get(d, False): continue
                entry_i=i+1; entry_date=dates[entry_i]; future=np.flatnonzero(dates>entry_date)
                if len(future)==0: continue
                exit_i=int(future[-1] if False else np.flatnonzero(dates==dates[future[0]])[-1])
                entry=float(b.iloc[entry_i].open); close=float(b.iloc[exit_i].close)
                net=close*(1-25/10000)/(entry*(1+15/10000))-1
                sig_date=daily_states.get(sym,{})
                signal_date=times.iloc[i].date(); prev=[x for x in sig_date if x < signal_date]
                k=m30_states.timestamp.searchsorted(times.iloc[i],side='left')-1
                rk=m30_recursive.timestamp.searchsorted(times.iloc[i],side='left')-1
                rprev=[x for x in daily_recursive_states if x < signal_date]
                rows.append({'buy_type':e.kind,'symbol':sym,'signal_time':times.iloc[i],'entry_time':times.iloc[entry_i],'t1_close_time':times.iloc[exit_i],'net_return':net,'detail':e.detail,'center_start_stroke':e.center_start_stroke,'center_end_stroke':e.center_end_stroke,'break_stroke':e.break_stroke,'segment_start_stroke':e.segment_start_stroke,'segment_end_stroke':e.segment_end_stroke,'daily_native_uptrend':bool(prev and sig_date[prev[-1]]),'m30_native_uptrend':bool(k>=0 and m30_states.iloc[k].uptrend),'daily_recursive_uptrend':bool(rprev and daily_recursive_states[rprev[-1]]),'m30_recursive_uptrend':bool(rk>=0 and m30_recursive.iloc[rk].available and m30_recursive.iloc[rk].uptrend)})
        except Exception as exc: continue
        if n%100==0 or n==len(symbols): print(f'native 1m: {n}/{len(symbols)} symbols, {len(rows)} events',flush=True)
    con.close(); frame=pd.DataFrame(rows)
    if not frame.empty:
        frame['signal_date']=pd.to_datetime(frame['signal_time']).dt.date
        frame=frame.sort_values('signal_time').drop_duplicates(['symbol','signal_date','buy_type'],keep='first').reset_index(drop=True)
    out=[]
    for typ in ('一买','二买','三买'):
        for label, x in [('all',frame[frame.buy_type==typ]),('daily_native_uptrend',frame[(frame.buy_type==typ)&frame.daily_native_uptrend.astype(bool)]),('m30_and_daily_native_uptrend',frame[(frame.buy_type==typ)&frame.daily_native_uptrend.astype(bool)&frame.m30_native_uptrend.astype(bool)]),('recursive_m30_and_daily_uptrend',frame[(frame.buy_type==typ)&frame.daily_recursive_uptrend.astype(bool)&frame.m30_recursive_uptrend.astype(bool)])]:
            r=x.net_return.astype(float) if len(x) else pd.Series(dtype=float); w=r[r>0]; l=r[r<0]
            out.append({'filter':label,'buy_type':typ,'observations':len(x),'symbols':x.symbol.nunique(),'avg_net_return':r.mean(),'median_net_return':r.median(),'win_rate':(r>0).mean(),'pf':w.sum()/abs(l.sum()) if len(l) else np.nan})
    target=ROOT/a.output; target.parent.mkdir(parents=True,exist_ok=True); pd.DataFrame(out).to_csv(target,index=False); frame.to_csv(target.with_name('native_chan_1m_t1_trades.csv'),index=False); print(pd.DataFrame(out).to_string(index=False))
if __name__=='__main__': main()
