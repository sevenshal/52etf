import {
  chinaTodayString,
  fearColor,
  fearStatus,
  fearTextColor,
  resolveFearSummaryScore,
} from './utils';
import { CN_ETF_OPTIONS } from './components/fearMarketTaxonomy';

const TODAY = '2026-09-12';

test('A股自算贪恐目录包含守猪逮兔新增的八条实际跟踪指数', () => {
  const symbols = new Set(CN_ETF_OPTIONS.map(item => item.symbol));
  [
    '000001.SH', '000913.CSI', '399987.SZ', '931079.CSI',
    '000906.SH', 'H30590.CSI', '930713.CSI', '980017.SZ',
  ].forEach(symbol => expect(symbols.has(symbol)).toBe(true));
});
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

test('颜色与状态：按 20 分一档分五档，缺值有兜底', () => {
  expect(fearColor(80)).toBe('#cf1322');
  expect(fearStatus(80)).toBe('极端贪婪');
  expect(fearStatus(50)).toBe('中性');
  expect(fearColor(null)).toBe('#8c8c8c');
  expect(fearStatus(undefined)).toBe('未入库');
  // 中性色做文字太浅，换成深灰；其余颜色不变
  expect(fearTextColor(50)).toBe('#595959');
  expect(fearTextColor(80)).toBe('#cf1322');
});

test('档位边界：0-20 极端恐慌 / 20-40 恐慌 / 40-60 中性 / 60-80 贪婪 / 80-100 极端贪婪', () => {
  expect(fearStatus(0)).toBe('极端恐慌');
  expect(fearStatus(19.9)).toBe('极端恐慌');
  expect(fearStatus(20)).toBe('恐慌');
  expect(fearStatus(39.9)).toBe('恐慌');
  expect(fearStatus(40)).toBe('中性');
  expect(fearStatus(59.9)).toBe('中性');
  expect(fearStatus(60)).toBe('贪婪');
  expect(fearStatus(79.9)).toBe('贪婪');
  expect(fearStatus(100)).toBe('极端贪婪');
  // 越界分数先夹到 0-100 再分档
  expect(fearStatus(-5)).toBe('极端恐慌');
  expect(fearStatus(130)).toBe('极端贪婪');
  expect(fearColor(20)).toBe('#52c41a');
  expect(fearColor(60)).toBe('#fa8c16');
});

test('今天按上海时区给出 YYYY-MM-DD', () => {
  expect(chinaTodayString()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
});
