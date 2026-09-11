import {
  chinaTodayString,
  fearColor,
  fearStatus,
  fearTextColor,
  resolveFearSummaryScore,
} from './utils';

const TODAY = '2026-09-12';
const summary = ({ latestDate, latestScore = 62.4, intradayScore } = {}) => ({
  latest: latestDate ? { date: latestDate, score: latestScore } : null,
  intraday: intradayScore === undefined ? undefined : { score: intradayScore },
});

test('盘中有快照、收盘还是昨天：显示盘中，副标“昨收”', () => {
  expect(resolveFearSummaryScore(summary({ latestDate: '2026-09-11', intradayScore: 48.1 }), TODAY))
    .toEqual({ score: 48.1, mode: 'intraday', closeLabel: '昨收', hasData: true });
});

test('今天已收盘入库：只显收盘，即使还留着盘中快照', () => {
  expect(resolveFearSummaryScore(summary({ latestDate: TODAY, intradayScore: 48.1 }), TODAY))
    .toEqual({ score: 62.4, mode: 'close', closeLabel: '收盘', hasData: true });
});

test('没有盘中快照：显示最近一次收盘，标“昨收”', () => {
  expect(resolveFearSummaryScore(summary({ latestDate: '2026-09-11' }), TODAY))
    .toEqual({ score: 62.4, mode: 'close', closeLabel: '昨收', hasData: true });
});

test('盘中快照没有有效分数时不采用', () => {
  expect(resolveFearSummaryScore(summary({ latestDate: '2026-09-11', intradayScore: null }), TODAY).mode)
    .toBe('close');
});

test('没有入库记录：hasData 为 false（和看板卡片的“未入库”一致）', () => {
  const resolved = resolveFearSummaryScore(summary(), TODAY);
  expect(resolved.hasData).toBe(false);
  expect(resolved.score).toBeUndefined();
  expect(resolveFearSummaryScore(undefined, TODAY).hasData).toBe(false);
});

test('颜色与状态：沿用原有阈值，缺值有兜底', () => {
  expect(fearColor(80)).toBe('#cf1322');
  expect(fearStatus(80)).toBe('极度贪婪');
  expect(fearStatus(50)).toBe('中性');
  expect(fearColor(null)).toBe('#8c8c8c');
  expect(fearStatus(undefined)).toBe('未入库');
  // 中性色做文字太浅，换成深灰；其余颜色不变
  expect(fearTextColor(50)).toBe('#595959');
  expect(fearTextColor(80)).toBe('#cf1322');
});

test('今天按上海时区给出 YYYY-MM-DD', () => {
  expect(chinaTodayString()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
});
