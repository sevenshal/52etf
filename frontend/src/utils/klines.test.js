import { appendRollingPocSupportResistance } from './klines';

const bar = (price, volume, turnoverRate) => ({
  high: price + 0.2,
  low: price - 0.2,
  close: price,
  volume,
  turnover_rate: turnoverRate,
});

// 前 20 天在 10 元放巨量形成筹码峰，之后 40 天在 12 元缩量横盘；取第 61 根看过去 60 根的分布
const buildSeries = turnoverRate => [
  ...Array.from({ length: 20 }, () => bar(10, 5000000, turnoverRate)),
  ...Array.from({ length: 41 }, () => bar(12, 1000000, turnoverRate)),
];

const lastSupportResistance = (klines) => {
  const rows = appendRollingPocSupportResistance(klines, { window: 60, enableTurnoverDecay: true });
  return rows[rows.length - 1].support_resistance;
};

test('A 股典型 ~2% 日换手（小数口径）下，两个月前的筹码峰仍留在成交分布里', () => {
  const supportResistance = lastSupportResistance(buildSeries(0.02));
  const oldPeak = supportResistance.supports.find(level => level.price >= 9.8 && level.price <= 10.2);

  expect(oldPeak).toBeDefined();
  // 单根 5M 的 K 线落到收盘价位不到 1M；超过 10M 说明是 20 天累积、只按每天 2% 衰减
  expect(oldPeak.volume).toBeGreaterThan(10000000);
});

test('turnover_rate 若按百分数喂入（2 表示 2%），衰减会每天清空分布，只剩最后一根', () => {
  const supportResistance = lastSupportResistance(buildSeries(2));

  // 这就是 A 股接口曾直接透传 tushare 百分数时的表现：10 元的筹码峰整个消失
  expect(supportResistance.supports).toEqual([]);
  expect(supportResistance.poc_level.volume).toBeLessThanOrEqual(1000000);
});
