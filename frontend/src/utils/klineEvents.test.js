import { alignToTradingDay, buildEventMarkers } from './klineEvents';

// 2026-09-04 周五、09-07 周一
const DATES = ['2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07', '2026-09-08'];

test('交易日当天的事件落在当天', () => {
  expect(alignToTradingDay(DATES, '2026-09-03')).toBe(1);
});

test('周末发布的研报落到下一个交易日（周一）', () => {
  expect(alignToTradingDay(DATES, '2026-09-05')).toBe(3);
  expect(alignToTradingDay(DATES, '2026-09-06')).toBe(3);
});

test('晚于最后一根 K 线的事件不画', () => {
  expect(alignToTradingDay(DATES, '2026-09-09')).toBe(-1);
  expect(alignToTradingDay([], '2026-09-03')).toBe(-1);
});

test('周六和周日的研报合并到周一，计数按篇累加', () => {
  const { research } = buildEventMarkers(DATES, [
    { date: '2026-09-05', count: 2, reports: [{ org_name: 'A' }, { org_name: 'B' }] },
    { date: '2026-09-06', count: 1, reports: [{ org_name: 'C' }] },
  ]);

  expect(research).toHaveLength(1);
  expect(research[0].index).toBe(3);
  expect(research[0].count).toBe(3);
  // 每篇研报带着自己的原始发布日，弹窗里要能看出是周末发的
  expect(research[0].items.map(item => item.report_date)).toEqual(['2026-09-05', '2026-09-05', '2026-09-06']);
});

test('年报和次年一季报同一天披露，合成一个财报标记', () => {
  const { financial } = buildEventMarkers(DATES, [], [
    { date: '2026-09-03', period_label: '2025年报' },
    { date: '2026-09-03', period_label: '2026一季报' },
  ]);

  expect(financial).toHaveLength(1);
  expect(financial[0].count).toBe(2);
});

test('同一根 K 线上两类都有时，研报叠在财报上面', () => {
  const { financial, research } = buildEventMarkers(
    DATES,
    [
      { date: '2026-09-03', reports: [{ org_name: 'A' }] },
      { date: '2026-09-08', reports: [{ org_name: 'B' }] },
    ],
    [{ date: '2026-09-03', period_label: '2026中报' }],
  );

  expect(financial[0].slot).toBe(0);
  expect(research.find(marker => marker.index === 1).slot).toBe(1);
  expect(research.find(marker => marker.index === 4).slot).toBe(0);
});

test('早于第一根 K 线的事件不堆到第一根上', () => {
  const { financial } = buildEventMarkers(DATES, [], [{ date: '2025-01-01', period_label: '2024三季报' }]);
  expect(financial).toEqual([]);
});

test('空输入不炸', () => {
  expect(buildEventMarkers(DATES)).toEqual({ financial: [], research: [] });
  expect(buildEventMarkers([], [{ date: '2026-09-03', reports: [{}] }])).toEqual({ financial: [], research: [] });
});
