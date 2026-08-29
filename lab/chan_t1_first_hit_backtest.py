"""按逐根1m顺序回放一买的止盈止损先后。"""
from pathlib import Path
import duckdb, pandas as pd

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'lab/output/chan_signal_pair_backtest_20260829_120d'

def main():
    t=pd.read_csv(DATA/'t1_close_trades.csv',parse_dates=['entry_time','t1_close_time'])
    t=t[t.buy_type=='一买'].reset_index(drop=True); t['trade_id']=t.index
    c=duckdb.connect('/home/quantd/quant_prod/quant_robot/analytics.duckdb',read_only=True)
    c.register('trades',t[['trade_id','symbol','entry_time','t1_close_time','entry_price']])
    rows=[]
    for target,stop in [(0.002,-0.005),(0.003,-0.005),(0.005,-0.005),(0.002,-0.01),(0.003,-0.01),(0.005,-0.01),(0.005,-0.02),(0.01,-0.02),(0.01,-0.03)]:
        q=c.execute(f'''WITH hits AS (
          SELECT t.trade_id, m.trade_time,
                 (m.high >= t.entry_price*(1+{target})) AS hit_target,
                 (m.low <= t.entry_price*(1+{stop})) AS hit_stop
          FROM trades t JOIN a_stock_minute_bar_qfq m
            ON m.ts_code=t.symbol AND m.trade_time>t.entry_time AND m.trade_time<=t.t1_close_time
          WHERE m.high >= t.entry_price*(1+{target}) OR m.low <= t.entry_price*(1+{stop})
        ), firsts AS (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY trade_id ORDER BY trade_time) rn FROM hits
        )
        SELECT COUNT(*) FILTER (WHERE rn=1) AS triggered,
          COUNT(*) FILTER (WHERE rn=1 AND hit_target AND NOT hit_stop) AS target_first,
          COUNT(*) FILTER (WHERE rn=1 AND hit_stop AND NOT hit_target) AS stop_first,
          COUNT(*) FILTER (WHERE rn=1 AND hit_target AND hit_stop) AS same_bar,
          COUNT(*) FILTER (WHERE trade_id NOT IN (SELECT trade_id FROM firsts WHERE rn=1)) AS no_trigger
        FROM firsts''').fetchone()
        rows.append({'target':target,'stop':stop,'observations':len(t),**dict(zip(['triggered','target_first','stop_first','same_bar','no_trigger'],q))})
    c.close(); out=pd.DataFrame(rows); out['target_first_rate']=out.target_first/len(t); out['stop_first_rate']=out.stop_first/len(t); out['same_bar_rate']=out.same_bar/len(t); out['no_trigger_rate']=out.no_trigger/len(t)
    # 同一根同时触发按止损处理；近似按固定目标/止损计算净收益。
    buy_cost=0.0015; sell_cost=0.0025
    out['approx_avg_net']=(out.target_first*(1+out.target)*(1-sell_cost)/(1+buy_cost)+out.stop_first*(1+out.stop)*(1-sell_cost)/(1+buy_cost)+out.same_bar*(1+out.stop)*(1-sell_cost)/(1+buy_cost)-len(t))/len(t)
    out.to_csv(DATA/'t1_first_hit_summary.csv',index=False); print(out.to_string(index=False))

if __name__=='__main__': main()
