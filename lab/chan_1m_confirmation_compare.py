from pathlib import Path
import duckdb, pandas as pd

root = Path(__file__).resolve().parents[1] / 'lab/output/chan_signal_pair_backtest_20260829_120d'
t = pd.read_csv(root / 't1_close_trades.csv', parse_dates=['signal_time', 'entry_time'])
t = t[t.buy_type == '一买'].reset_index(drop=True)
t['id'] = t.index
c = duckdb.connect('/home/quantd/quant_prod/quant_robot/analytics.duckdb', read_only=True)
c.register('t', t[['id', 'symbol', 'signal_time', 'entry_time', 'entry_price', 'net_return']])
q = c.execute('''SELECT t.*, s.high signal_high, s.close signal_close,
 e.close entry_close, e.high entry_high, e.low entry_low
 FROM t JOIN a_stock_minute_bar_qfq s ON s.ts_code=t.symbol AND s.trade_time=t.signal_time
 JOIN a_stock_minute_bar_qfq e ON e.ts_code=t.symbol AND e.trade_time=t.entry_time''').fetchdf()
c.close()
rows = []
for name, m in [('全部', q.index == q.index),
                ('下一根收盘突破信号高', q.entry_close > q.signal_high),
                ('下一根最高突破信号高', q.entry_high > q.signal_high),
                ('下一根不跌破信号收盘', q.entry_low >= q.signal_close)]:
    x = q[m]
    r = x.net_return
    w, l = r[r > 0], r[r < 0]
    rows.append({'variant': name, 'observations': len(x),
                 'avg_net_return': r.mean(), 'median': r.median(),
                 'win_rate': (r > 0).mean(),
                 'pf': w.sum() / abs(l.sum()) if len(l) else float('inf')})
out = pd.DataFrame(rows)
out.to_csv(root / '1m_confirmation_summary.csv', index=False)
print(out.to_string(index=False))
