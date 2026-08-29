from pathlib import Path
import duckdb, pandas as pd

root = Path(__file__).resolve().parents[1] / 'lab/output/chan_signal_pair_backtest_20260829_120d'
t = pd.read_csv(root / 't1_close_trades.csv', parse_dates=['entry_time', 't1_close_time'])
t = t[t.buy_type == '一买'].reset_index(drop=True)
c = duckdb.connect('/home/quantd/quant_prod/quant_robot/analytics.duckdb', read_only=True)
c.register('trades', t[['symbol', 'entry_time', 't1_close_time', 'entry_price', 'net_return']])
q = c.execute('''WITH highs AS (
 SELECT t.entry_time, t.symbol, t.entry_price, t.net_return, MAX(m.high) AS t1_max_high,
        MIN(m.low) AS t1_min_low
 FROM trades t JOIN a_stock_minute_bar_qfq m
 ON m.ts_code=t.symbol AND m.trade_time>t.entry_time AND m.trade_time<=t.t1_close_time
 GROUP BY ALL)
 SELECT t1_max_high/entry_price-1 AS max_return,
        t1_min_low/entry_price-1 AS min_return, net_return FROM highs''').fetchdf()
c.close()
q['max_return'] = q.max_return.astype(float)
rows=[]
for target in [0.001,0.002,0.003,0.004,0.005,0.0075,0.01,0.015,0.02,0.03]:
    rows.append({'target':target,'observations':len(q),'close_win_rate':(q.net_return>0).mean(),
                 'intraday_hit_rate':(q.max_return>=target).mean(),'avg_max_return':q.max_return.mean()})
print(pd.DataFrame(rows).to_string(index=False))
pd.DataFrame(rows).to_csv(root/'t1_intraday_target_curve.csv',index=False)
for target, stop in [(0.003,-0.01),(0.005,-0.01),(0.005,-0.02),(0.01,-0.02)]:
    hit=(q.max_return>=target); stopped=(q.min_return<=stop)
    print({'target':target,'stop':stop,'target_hit':hit.mean(),'stop_hit':stopped.mean(),
           'target_only':(hit&~stopped).mean(),'stop_only':(stopped&~hit).mean(),'both_ambiguous':(hit&stopped).mean()})
