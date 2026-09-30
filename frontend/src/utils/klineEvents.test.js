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

test('同一根 K 线上多类事件都有时，按 财报/快报/预告/研报 自下而上排', () => {
  const { financial, express, forecast, research } = buildEventMarkers(
    DATES,
    [
      { date: '2026-09-03', reports: [{ org_name: 'A' }] },
      { date: '2026-09-08', reports: [{ org_name: 'B' }] },
    ],
    [{ date: '2026-09-03', period_label: '2026中报' }],
    [{ date: '2026-09-03', period_label: '2026中报' }],
    [{ date: '2026-09-03', period_label: '2026中报' }],
  );

  expect(financial[0].slot).toBe(0);
  expect(express[0].slot).toBe(1);
  expect(forecast[0].slot).toBe(2);
  expect(research.find(marker => marker.index === 1).slot).toBe(3);
  // 只有研报的那天仍然贴在最下面
  expect(research.find(marker => marker.index === 4).slot).toBe(0);
});

test('只有财报和研报时上下层位和以前一样', () => {
  const { financial, research } = buildEventMarkers(
    DATES,
    [{ date: '2026-09-03', reports: [{ org_name: 'A' }] }],
    [{ date: '2026-09-03', period_label: '2026中报' }],
  );

  expect(financial[0].slot).toBe(0);
  expect(research[0].slot).toBe(1);
});

test('快报和预告各自成标记，同一报告期的两条预告不合并', () => {
  const { express, forecast } = buildEventMarkers(
    DATES,
    [],
    [],
    [{ date: '2026-09-03', period_label: '2026中报', revenue: 1 }],
    [
      { date: '2026-09-03', period_label: '2025年报', is_correction: false },
      { date: '2026-09-07', period_label: '2025年报', is_correction: true },
    ],
  );

  expect(express).toHaveLength(1);
  expect(express[0].items.map(item => item.revenue)).toEqual([1]);
  expect(forecast).toHaveLength(2);
  expect(forecast.map(marker => marker.index)).toEqual([1, 3]);
  expect(forecast.map(marker => marker.count)).toEqual([1, 1]);
});

test('早于第一根 K 线的事件不堆到第一根上', () => {
  const { financial } = buildEventMarkers(DATES, [], [{ date: '2025-01-01', period_label: '2024三季报' }]);
  expect(financial).toEqual([]);
});

test('空输入不炸', () => {
  expect(buildEventMarkers(DATES)).toEqual({ financial: [], express: [], forecast: [], research: [] });
  expect(buildEventMarkers([], [{ date: '2026-09-03', reports: [{}] }])).toEqual({
    financial: [], express: [], forecast: [], research: [],
  });
});
