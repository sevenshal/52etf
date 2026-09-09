import { calculateMacd, histogramGrowing } from './macd';

const kline = (close, index) => ({
  timestamp: `2026-01-${String(index + 1).padStart(2, '0')}`,
  open: close,
  high: close + 1,
  low: close - 1,
  close,
  volume: 1000,
});

const closes = length => Array.from({ length }, (_, index) => kline(10 + index, index));

// EMA 的独立参考实现：递推播种，用来验证 calculateMacd 没有自己另算一套
const ema = (values, period) => {
  const alpha = 2 / (period + 1);
  let previous = null;
  return values.map(value => {
    previous = previous === null ? value : previous + alpha * (value - previous);
    return previous;
  });
};

test('DIF/DEA 与独立实现的递推 EMA 一致，柱子是差值的两倍', () => {
  const rows = closes(60);
  const prices = rows.map(row => row.close);
  const expectedDif = prices.map((_, index) => ema(prices, 12)[index] - ema(prices, 26)[index]);
  const expectedDea = ema(expectedDif, 9);

  const { dif, dea, histogram } = calculateMacd(rows);

  expect(dif[59]).toBeCloseTo(expectedDif[59], 10);
  expect(dea[59]).toBeCloseTo(expectedDea[59], 10);
  expect(histogram[59]).toBeCloseTo((expectedDif[59] - expectedDea[59]) * 2, 10);
});

test('前 25 根（慢线周期 − 1）不出值，避免展示 EMA 播种偏差', () => {
  const { dif, dea, histogram } = calculateMacd(closes(40));

  expect(dif.slice(0, 25).every(value => value === null)).toBe(true);
  expect(dea.slice(0, 25).every(value => value === null)).toBe(true);
  expect(histogram.slice(0, 25).every(value => value === null)).toBe(true);
  expect(dif[25]).not.toBeNull();
});

test('横盘时 DIF/DEA/柱都收敛到 0，上涨时 DIF 为正', () => {
  const flat = calculateMacd(Array.from({ length: 60 }, (_, index) => kline(10, index)));
  expect(flat.dif[59]).toBeCloseTo(0, 10);
  expect(flat.histogram[59]).toBeCloseTo(0, 10);

  const rising = calculateMacd(closes(60));
  expect(rising.dif[59]).toBeGreaterThan(0);

  const falling = calculateMacd(
    Array.from({ length: 60 }, (_, index) => kline(100 - index, index))
  );
  expect(falling.dif[59]).toBeLessThan(0);
});

test('周期可配置，warm-up 跟着最长的那个周期走', () => {
  const { dif, params } = calculateMacd(closes(60), { fast: 5, slow: 10, signal: 20 });

  expect(params).toEqual({ fast: 5, slow: 10, signal: 20 });
  expect(dif.slice(0, 19).every(value => value === null)).toBe(true);
  expect(dif[19]).not.toBeNull();
});

test('周期非法时回落到默认的 12/26/9', () => {
  expect(calculateMacd(closes(40), { fast: null, slow: 0, signal: 'x' }).params)
    .toEqual({ fast: 12, slow: 26, signal: 9 });
});

test('柱子变号的那一根算动能放大，不按绝对值误判成收缩', () => {
  expect(histogramGrowing([null, -0.5, 0.1, 0.3, 0.2, -0.05, -0.9]))
    .toEqual([null, true, true, true, false, true, true]);
});

test('空输入不炸', () => {
  expect(calculateMacd([]).dif).toEqual([]);
  expect(calculateMacd(null).histogram).toEqual([]);
  expect(histogramGrowing(null)).toEqual([]);
});
