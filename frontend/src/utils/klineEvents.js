/**
 * K 线事件标记的纯函数：把研报/财报事件对齐到交易日、合并同一天的事件、分配上下层位。
 *
 * 接口返回的是事件的原始日期。研报常在周末发布、财报常在盘后或非交易日披露，
 * 这些都应该落在"市场第一次能对它做出反应"的那根 K 线上，也就是日期 ≥ 事件日的
 * 第一个交易日。
 */

/** 第一个日期 ≥ dateStr 的交易日下标；事件在最后一根 K 线之后则返回 -1。 */
export const alignToTradingDay = (dates, dateStr) => {
  if (!dates.length || !dateStr) return -1;
  let low = 0;
  let high = dates.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (dates[mid] < dateStr) low = mid + 1;
    else high = mid;
  }
  return low < dates.length ? low : -1;
};

const groupByTradingDay = (dates, events, toItems) => {
  const byIndex = new Map();
  (events || []).forEach(event => {
    // 早于图上第一根 K 线的事件不画，否则会全部堆到第一根上
    if (!event?.date || (dates.length && event.date < dates[0])) return;
    const index = alignToTradingDay(dates, event.date);
    if (index < 0) return;
    const items = toItems(event);
    const existing = byIndex.get(index);
    if (existing) existing.items.push(...items);
    else byIndex.set(index, { index, items: [...items] });
  });
  return [...byIndex.values()]
    .map(marker => ({ ...marker, count: marker.items.length }))
    .sort((a, b) => a.index - b.index);
};

/**
 * @returns {{ financial: Array, research: Array }} 每个标记 { index, count, items, slot }。
 * 同一根 K 线上两类都有时，财报贴着 K 线(slot 0)、研报叠在上面(slot 1)。
 */
export const buildEventMarkers = (dates, researchDays = [], financialReports = []) => {
  // 研报：一个事件日里可能有多篇；周六、周日的会和周一合到同一根 K 线上
  const research = groupByTradingDay(
    dates,
    researchDays,
    day => (day.reports || []).map(report => ({ ...report, report_date: day.date })),
  );
  // 财报：近半数公司的年报和次年一季报同一天披露，合成一个标记、计数为 2
  const financial = groupByTradingDay(dates, financialReports, report => [report]);

  const financialIndexes = new Set(financial.map(marker => marker.index));
  return {
    financial: financial.map(marker => ({ ...marker, slot: 0 })),
    research: research.map(marker => ({ ...marker, slot: financialIndexes.has(marker.index) ? 1 : 0 })),
  };
};
