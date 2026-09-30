/**
 * K 线事件标记的纯函数：把研报/财报/快报/预告事件对齐到交易日、合并同一天的事件、分配上下层位。
 *
 * 接口返回的是事件的原始日期。研报常在周末发布、业绩公告常在盘后或非交易日披露，
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

/**
 * 同一根 K 线挤了好几种事件时，从下往上紧挨着排的顺序。
 * 贴 K 线最近的是最"硬"的定期报告，越往上越是预期性的信息（快报 → 预告 → 卖方观点）。
 */
export const EVENT_KINDS = ['financial', 'express', 'forecast', 'research'];

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
 * @returns {{ financial: Array, express: Array, forecast: Array, research: Array }}
 *   每个标记 { index, count, items, kind, slot }。
 *   同一根 K 线上多类并存时按 EVENT_KINDS 的顺序自下而上占 slot 0/1/2/3；
 *   只有一类时 slot 恒为 0，所以只有财报 + 研报的那种老情形，画面和以前完全一样。
 */
export const buildEventMarkers = (
  dates,
  researchDays = [],
  financialReports = [],
  expressReports = [],
  forecastReports = [],
) => {
  const byKind = {
    // 研报：一个事件日里可能有多篇；周六、周日的会和周一合到同一根 K 线上
    research: groupByTradingDay(
      dates,
      researchDays,
      day => (day.reports || []).map(report => ({ ...report, report_date: day.date })),
    ),
    // 财报：近半数公司的年报和次年一季报同一天披露，合成一个标记、计数为 2
    financial: groupByTradingDay(dates, financialReports, report => [report]),
    // 快报/预告：一份公告一个事件（同一报告期的修正预告是当天的独立信息，不合并）
    express: groupByTradingDay(dates, expressReports, report => [report]),
    forecast: groupByTradingDay(dates, forecastReports, report => [report]),
  };

  const kindsByIndex = new Map();
  EVENT_KINDS.forEach(kind => {
    byKind[kind].forEach(marker => {
      const kinds = kindsByIndex.get(marker.index);
      if (kinds) kinds.push(kind);
      else kindsByIndex.set(marker.index, [kind]);
    });
  });

  return Object.fromEntries(EVENT_KINDS.map(kind => [
    kind,
    byKind[kind].map(marker => ({
      ...marker,
      kind,
      slot: kindsByIndex.get(marker.index).indexOf(kind),
    })),
  ]));
};
