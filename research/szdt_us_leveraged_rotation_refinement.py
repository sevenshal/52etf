#!/usr/bin/env python3
"""Production-enabled US leveraged ETF rotation refinement (same-close)."""
import asyncio, json, sqlite3
from dataclasses import replace
from itertools import product
from pathlib import Path
import pandas as pd
import szdt_us_regular_etf_close_grid_search as base

BUY=(-95,-90,-85); SELL=(75,80,85); FACTOR=(3.,5.); BUY_AMT=(5e3,1e4,2e4); SELL_AMT=(4e3,6e3,8e3,1e4); VOL=(1.2,1.3,1.4); ROT=(101,-30,0,30,60)
PROD='/home/quantd/quant_prod/quant_robot/evc_stocks.db'; ACCOUNT='amNDUzNWU4OTA4NjcyZWYwMGYyZDMyYzQzNDFjYjAwOTUK'

def run(p):
 buy,sell,bf,sf,ba,sa,vr,rot=p; cash=base.INITIAL_CAPITAL; sh={c:0 for c in base.CODES}; nav=[]; trades=rots=0
 for d in base.MARKET_DAYS:
  rows={c:base.MARKETS[c].get(d) for c in base.CODES}
  for c,r in rows.items():
   if r and r[3]>=sell and sh[c]>0:
    q=min(sh[c],max(1,int(base.scaled_trade_amount(sa,r[3],sell,sf,is_buy=False)/r[1]))); g=q*r[1]; cash+=g-base.fee(g); sh[c]-=q; trades+=1
  for c,r in sorted(rows.items(),key=lambda x: x[1][3] if x[1] else 999):
   if not r or r[3]>buy or r[2]<vr: continue
   want=base.scaled_trade_amount(ba,r[3],buy,bf,is_buy=True)
   if rot<=100 and cash<want:
    for score,s,sp in sorted(((x[1][3],x[0],x[1][1]) for x in rows.items() if x[1] and x[0]!=c and sh[x[0]]>0 and x[1][3]>=rot),reverse=True):
     if cash>=want: break
     q=min(sh[s],max(1,int(__import__('math').ceil((want-cash)/sp)))); g=q*sp; cash+=g-base.fee(g); sh[s]-=q; trades+=1; rots+=1
   q=int(min(cash,want)/r[1]); g=q*r[1]
   while q and g+base.fee(g)>cash: q-=1; g=q*r[1]
   if q: cash-=g+base.fee(g); sh[c]+=q; trades+=1
  nav.append(cash+sum(sh[c]*r[1] for c,r in rows.items() if r))
 s=pd.Series(nav); daily=s.pct_change().dropna(); total=s.iloc[-1]/base.INITIAL_CAPITAL-1; ann=(1+total)**(252/max(1,len(daily)))-1; dd=(s/s.cummax()-1).min(); vol=daily.std(); sharpe=daily.mean()/vol*(252**.5) if vol else -99
 return dict(when_buy=buy,when_sell=sell,buy_factor=bf,sell_factor=sf,buy_amount=ba,sell_amount=sa,volume_ratio=vr,rotation_enabled=rot<=100,rotation_threshold=None if rot==101 else rot,total_return=total,annualized_return=ann,max_drawdown=dd,sharpe=sharpe,calmar=ann/abs(dd) if dd else 0,trades=trades,rotation_trades=rots)

async def main(out):
 conn=sqlite3.connect(f'file:{PROD}?mode=ro',uri=True); codes={r[0] for r in conn.execute("select code from szdt_trade_stocks where account_id=? and type=1 and enabled=1",(ACCOUNT,))}; conn.close()
 frames,names,_,_=await base.load_frames(1,codes); base.CODES=tuple(sorted(frames)); base.MARKETS={c:{x.date:(float(x.open),float(x.close),float(x.volume_ratio) if pd.notna(x.volume_ratio) else 0,float(x.score)) for x in f.itertuples(index=False)} for c,f in frames.items()}; base.MARKET_DAYS=sorted({d for m in base.MARKETS.values() for d in m})
 rows=[run(x) for x in product(BUY,SELL,FACTOR,FACTOR,BUY_AMT,SELL_AMT,VOL,ROT)]; df=pd.DataFrame(rows).sort_values(['sharpe','annualized_return'],ascending=False); Path(out).mkdir(parents=True,exist_ok=True); df.to_csv(Path(out)/'grid.csv',index=False); print(df.head(20).to_json(orient='records'))
if __name__=='__main__': asyncio.run(main('/tmp/szdt-us-rotation-refine'))
