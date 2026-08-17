from datetime import date, timedelta
from core.services.longport import LongPortService

SYMBOL = 'SPY.US'
END_ANCHOR = date(2026, 5, 13)
TARGET_DAYS = 5
svc = LongPortService.get_instance('LBPT10001248')

results = []
checked = []
for offset in range(1, 25):
    d = END_ANCHOR - timedelta(days=offset)
    rows = svc.get_candlesticks_by_date(SYMBOL, d, d, '1m')
    rows = [r for r in rows if r.get('volume') is not None]
    checked.append((d, len(rows)))
    if len(rows) < 300:
        continue
    # Prefer full regular US session. Keep rows in returned chronological order.
    first = rows[0]
    last = rows[-1]
    first_vol = float(first['volume'])
    last_vol = float(last['volume'])
    middle = rows[1:-1]
    baseline = sum(float(r['volume']) for r in middle) / len(middle) if middle else None
    total = sum(float(r['volume']) for r in rows)
    results.append({
        'date': d,
        'rows': len(rows),
        'first_time': first['timestamp'],
        'last_time': last['timestamp'],
        'first_volume': first_vol,
        'last_volume': last_vol,
        'baseline': baseline,
        'first_multiple': first_vol / baseline if baseline else None,
        'last_multiple': last_vol / baseline if baseline else None,
        'total_volume': total,
        'first_share': first_vol / total * 100 if total else None,
        'last_share': last_vol / total * 100 if total else None,
    })
    if len(results) >= TARGET_DAYS:
        break

print(f"symbol={SYMBOL}")
print('checked=' + ', '.join(f"{d}:{n}" for d, n in checked))
print('| 日期 | 分钟数 | 开盘第1分钟量 | 收盘最后1分钟量 | 其他388分钟均量 | 开盘倍数 | 收盘倍数 | 开盘占全天 | 收盘占全天 |')
print('|---|---:|---:|---:|---:|---:|---:|---:|---:|')
for r in results:
    print(
        f"| {r['date'].isoformat()} | {r['rows']} | {r['first_volume']:.0f} | {r['last_volume']:.0f} | {r['baseline']:.2f} | {r['first_multiple']:.2f}x | {r['last_multiple']:.2f}x | {r['first_share']:.2f}% | {r['last_share']:.2f}% |"
    )

if results:
    avg_first_multiple = sum(r['first_multiple'] for r in results) / len(results)
    avg_last_multiple = sum(r['last_multiple'] for r in results) / len(results)
    avg_first_volume = sum(r['first_volume'] for r in results) / len(results)
    avg_last_volume = sum(r['last_volume'] for r in results) / len(results)
    avg_baseline = sum(r['baseline'] for r in results) / len(results)
    pooled_first_volume = sum(r['first_volume'] for r in results)
    pooled_last_volume = sum(r['last_volume'] for r in results)
    pooled_baseline_volume = sum(r['baseline'] * (r['rows'] - 2) for r in results)
    pooled_baseline = pooled_baseline_volume / sum(r['rows'] - 2 for r in results)
    pooled_first_multiple = (pooled_first_volume / len(results)) / pooled_baseline
    pooled_last_multiple = (pooled_last_volume / len(results)) / pooled_baseline
    print('\nsummary')
    print(f"days={len(results)} from={results[-1]['date'].isoformat()} to={results[0]['date'].isoformat()}")
    print(f"avg_first_volume={avg_first_volume:.2f}")
    print(f"avg_last_volume={avg_last_volume:.2f}")
    print(f"avg_baseline={avg_baseline:.2f}")
    print(f"simple_avg_first_multiple={avg_first_multiple:.4f}")
    print(f"simple_avg_last_multiple={avg_last_multiple:.4f}")
    print(f"pooled_baseline={pooled_baseline:.2f}")
    print(f"pooled_first_multiple={pooled_first_multiple:.4f}")
    print(f"pooled_last_multiple={pooled_last_multiple:.4f}")
