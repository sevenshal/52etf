import {
  REVISION_DOWN_COLOR,
  REVISION_UP_COLOR,
  describeRevision,
  revisionTooltip,
} from './epsRevision';

const revision = extra => ({
  prev_date: '2026-03-10', prev_eps: 0.9, prev_eps_raw: 0.9, prev_authors: '刘俊,邵梓洋',
  change_pct: 12.5, match: 'analyst', disclosed_between: [], ...extra,
});

test('上调标红、下调标绿（A股惯例）', () => {
  expect(describeRevision(revision())).toEqual({ text: '↑ +12.50%', color: REVISION_UP_COLOR });
  expect(describeRevision(revision({ change_pct: -10 }))).toEqual({ text: '↓ -10.00%', color: REVISION_DOWN_COLOR });
});

test('没有可比的上一篇显示"首次"，变化极小显示"持平"', () => {
  expect(describeRevision(null).text).toBe('首次');
  expect(describeRevision(revision({ change_pct: 0.001 })).text).toBe('持平');
  expect(describeRevision(revision({ change_pct: null })).text).toBe('--');
});

test('悬停说明区分"同分析师"和"换了人按同机构比"', () => {
  expect(revisionTooltip(revision())).toContain('同分析师');
  expect(revisionTooltip(revision({ match: 'org' }))).toContain('分析师已更换，按同机构上一篇比较');
});

test('中间隔着财报披露时写出来', () => {
  expect(revisionTooltip(revision({ disclosed_between: ['2025年报', '2026中报'] })))
    .toContain('期间披露 2025年报、2026中报');
});

test('上次的 EPS 经过除权换算时注明原始值', () => {
  const text = revisionTooltip(revision({ prev_eps: 1.0, prev_eps_raw: 1.5 }));
  expect(text).toContain('上次原始 EPS 1.500，已按除权换算到当前口径');
  expect(revisionTooltip(revision())).not.toContain('除权');
});

test('首次预测的说明', () => {
  expect(revisionTooltip(null)).toBe('往前两年内没有这家机构对这一年的更早预测');
});
