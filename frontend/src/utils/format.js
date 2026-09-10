import dayjs from 'dayjs';

/**
 * Format number with specified decimal places and thousands separator
 */
export const formatNumber = (value, decimals = 0) => {
  if (value === null || value === undefined) return '-';
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
};

/**
 * 把金额按中文习惯折成 万亿/亿/万，例如 38871348331.23 → "388.71亿"。
 *
 * 原先是 AStockQuoteSummary 里的私有函数；财务报表卡片也要用同一套折算，
 * 提到这里共用，免得两处各写一份、以后单位阈值改了只改一边。
 */
export const formatChineseAmount = (value, unit = '', empty = '--') => {
  if (value === null || value === undefined || value === '') return empty;
  const number = Number(value);
  if (!Number.isFinite(number)) return empty;
  const absolute = Math.abs(number);
  if (absolute >= 1e12) return `${(number / 1e12).toFixed(2)}万亿${unit}`;
  if (absolute >= 1e8) return `${(number / 1e8).toFixed(2)}亿${unit}`;
  if (absolute >= 1e4) return `${(number / 1e4).toFixed(2)}万${unit}`;
  return `${number.toFixed(2)}${unit}`;
};

/**
 * Format date to YYYY-MM-DD
 */
export const formatDate = (date) => {
  if (!date) return '-';
  return dayjs(date).format('YYYY-MM-DD');
};