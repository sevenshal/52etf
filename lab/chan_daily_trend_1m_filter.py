"""日线CZSC上涨趋势过滤1m一买，严格使用信号日前已完成日线。"""
from pathlib import Path
import sys, duckdb, pandas as pd, numpy as np
from datetime import date
from bisect import bisect_left
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'backend'))
from czsc import CZSC
from src.core.services.chan_analysis import rows_to_raw_bars
from chan_signal_pair_backtest import BacktestConfig, _load_bars

DATA=ROOT/'lab/output/chan_signal_pair_backtest_20260829_120d'; SRC=DATA/'t1_close_trades.csv'
def uptrend(c, close):
    """Return broad and strict daily CZSC uptrend states.

    Only completed daily bars are fed to ``c``.  The broad rule accepts either
    higher-high/higher-low upward strokes or a close above the latest ZS upper
    boundary.  The strict rule requires the latest completed stroke to be up,
    two consecutive upward strokes to improve, and (when available) price
    above the latest ZS upper boundary.
    """
    bis=list(c.bi_list)
    ups=[b for b in bis if 'up' in str(getattr(b,'direction','')).lower() or '向上' in str(getattr(b,'direction',''))]
    if len(ups)<2: return False, False
    a,b=ups[-2],ups[-1]
    higher=float(b.high)>float(a.high) and float(b.low)>float(a.low)
    center=bool(c.zs_list) and close>float(getattr(c.zs_list[-1],'zg',close))
    last_up = bis and ups[-1] is bis[-1]
    return higher or center, bool(higher and last_up and (not c.zs_list or center))
def stat(name,x):
    r=x.net_return; w=r[r>0]; l=r[r<0]
    return {'variant':name,'observations':len(x),'symbols':x.symbol.nunique(),'avg_net_return':r.mean(),'median':r.median(),'win_rate':(r>0).mean(),'pf':w.sum()/abs(l.sum()) if len(l) else np.inf}
def main():
    x=pd.read_csv(SRC,parse_dates=['signal_time']); x=x[x.buy_type=='一买'].copy(); x['signal_date']=pd.to_datetime(x.signal_time).dt.date
    con=duckdb.connect('/home/quantd/quant_prod/quant_robot/analytics.duckdb',read_only=True); states={}
    cfg=BacktestConfig(str(con),date(2020,1,1),date(2026,8,28),100000,20,15,25,'/tmp/chan_bt_000852_weights.csv')
    for n,symbol in enumerate(sorted(x.symbol.unique()),1):
        try:
            bars=_load_bars(con,symbol,'d',cfg); raw=rows_to_raw_bars(symbol,bars.to_dict('records'),'d')
            if len(raw)<30: continue
            c=CZSC(raw[:30])
            for bar in raw[30:]:
                c.update(bar); states[(symbol,bar.dt.date())]=uptrend(c,float(bar.close))
        except Exception: continue
        if n%200==0: print(f'daily CZSC: {n}/{x.symbol.nunique()} symbols',flush=True)
    con.close()
    # 对盘中信号使用前一交易日状态；同日第一根日线不能参与。
    dates_by_symbol={symbol: sorted(d for (s,d) in states if s==symbol)
                     for symbol in x.symbol.unique()}
    broad=[]; strict=[]
    for r in x.itertuples():
        ds=dates_by_symbol.get(r.symbol,[]); i=bisect_left(ds,r.signal_date)-1
        state=states.get((r.symbol,ds[i]),(False,False)) if i>=0 else (False,False)
        broad.append(state[0]); strict.append(state[1])
    x['daily_chan_uptrend']=broad; x['daily_chan_uptrend_strict']=strict
    rows=[stat('全部1m一买',x),
          stat('前一交易日日线CZSC上涨趋势-宽松',x[x.daily_chan_uptrend]),
          stat('前一交易日日线CZSC上涨趋势-严格',x[x.daily_chan_uptrend_strict])]
    out=pd.DataFrame(rows); out.to_csv(DATA/'daily_chan_1m_filter_summary.csv',index=False); print(out.to_string(index=False))
if __name__=='__main__': main()
