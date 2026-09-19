/**
 * 研报盈利预测修正（EPS 较上次）的展示规则。
 *
 * 比对口径在后端算好（a_stock_chart_events）：同机构、同预测年度，优先取有至少一位相同
 * 分析师的上一篇，没有就退到同机构上一篇；EPS 都已按写研报时的复权因子换算到前复权口径，
 * 送转不会被误判成下调。这里只负责把结果翻译成文字和颜色。
 */

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

// A股惯例：上调标红、下调标绿
export const REVISION_UP_COLOR = '#cf1322';
export const REVISION_DOWN_COLOR = '#389e0d';
export const REVISION_FLAT_COLOR = '#8c8c8c';

/** 修正幅度 → { text, color }；没有可比的上一篇时显示"首次"。 */
export const describeRevision = revision => {
  if (!revision) return { text: '首次', color: REVISION_FLAT_COLOR };
  const change = toNumber(revision.change_pct);
  if (change === null) return { text: '--', color: REVISION_FLAT_COLOR };
  if (Math.abs(change) < 0.005) return { text: '持平', color: REVISION_FLAT_COLOR };
  return change > 0
    ? { text: `↑ +${change.toFixed(2)}%`, color: REVISION_UP_COLOR }
    : { text: `↓ ${change.toFixed(2)}%`, color: REVISION_DOWN_COLOR };
};

/** 悬停说明：和谁比、是不是同一批分析师、中间隔了哪些财报、是否做过除权换算。 */
export const revisionTooltip = revision => {
  if (!revision) return '往前两年内没有这家机构对这一年的更早预测';
  const eps = toNumber(revision.prev_eps);
  const parts = [
    `上次 ${revision.prev_date} EPS ${eps === null ? '--' : eps.toFixed(3)}${revision.prev_authors ? `（${revision.prev_authors}）` : ''}`,
    revision.match === 'analyst' ? '同分析师' : '分析师已更换，按同机构上一篇比较',
  ];
  if (revision.disclosed_between?.length) parts.push(`期间披露 ${revision.disclosed_between.join('、')}`);
  const raw = toNumber(revision.prev_eps_raw);
  if (raw !== null && eps !== null && Math.abs(raw - eps) > 1e-6) {
    parts.push(`上次原始 EPS ${raw.toFixed(3)}，已按除权换算到当前口径`);
  }
  return parts.join('；');
};
