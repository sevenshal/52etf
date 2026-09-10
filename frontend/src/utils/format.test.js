import { formatChineseAmount } from './format';

test('按量级折成 万亿/亿/万', () => {
  expect(formatChineseAmount(38871348331.23)).toBe('388.71亿');   // 长电科技 2025 营收
  expect(formatChineseAmount(2.5e12)).toBe('2.50万亿');
  expect(formatChineseAmount(48193622)).toBe('4819.36万');
  expect(formatChineseAmount(1234.5)).toBe('1234.50');
});

test('负数保留符号且按绝对值选单位', () => {
  // 投资活动现金流、自由现金流常是负的，单位得按绝对值挑，不能因为是负数就掉到"元"
  expect(formatChineseAmount(-9056197270.84)).toBe('-90.56亿');
  expect(formatChineseAmount(-30000)).toBe('-3.00万');
});

test('空值与非数值给占位符，不给 NaN', () => {
  expect(formatChineseAmount(null)).toBe('--');
  expect(formatChineseAmount(undefined)).toBe('--');
  expect(formatChineseAmount('')).toBe('--');
  expect(formatChineseAmount('abc')).toBe('--');
  expect(formatChineseAmount(null, '', '-')).toBe('-');
});

test('单位后缀照旧拼上（行情摘要里的成交量用"手"）', () => {
  expect(formatChineseAmount(481936, '手')).toBe('48.19万手');
});
