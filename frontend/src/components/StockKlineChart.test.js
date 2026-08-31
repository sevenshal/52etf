import { appendNineTurnAtr } from '../utils/nineTurn';
import { calculateCloseMovingAverage } from '../utils/klines';

const kline = (close, index) => ({
  timestamp: `2026-01-${String(index + 1).padStart(2, '0')}`,
  open: close,
  high: close + 1,
  low: close - 1,
  close,
  volume: 1000,
});

test('computes ATR14, nine-turn counts, and drawdown from the latest red marker', () => {
  const closes = Array.from({ length: 14 }, (_, index) => 10 + index).concat([15, 14]);
  const result = appendNineTurnAtr(closes.map(kline));

  expect(result[5].highCount).toBe(2);
  expect(result[6].highCount).toBe(3);
  expect(result[12].highCount).toBe(9);
  expect(result[12].latestRisingClose).toBe(22);
  expect(result[13].highCount).toBe(10);
  expect(result[13].latestRisingClose).toBe(23);
  expect(result[13].latestRisingCount).toBe(10);
  expect(result[14].lowCount).toBe(1);
  expect(result[15].lowCount).toBe(2);
  expect(result[15].latestRisingClose).toBe(23);
  expect(result[15].latestRisingCount).toBe(10);
  expect(result[15].atr14).toBeGreaterThan(0);
  expect(result[15].risingDrawdownPct).toBeCloseTo((9 / 23) * 100);
  expect(result[15].risingDrawdownAtr).toBeGreaterThan(0);
});

test('uses the latest red marker even when the rising count only reaches two', () => {
  const closes = [10, 10, 10, 10, 11, 12, 9, 8];
  const result = appendNineTurnAtr(closes.map(kline), 2);

  expect(result[5].highCount).toBe(2);
  expect(result[7].lowCount).toBe(2);
  expect(result[7].latestRisingClose).toBe(12);
  expect(result[7].latestRisingCount).toBe(2);
  expect(result[7].risingDrawdownPct).toBeCloseTo((4 / 12) * 100);
  expect(result[7].risingDrawdownAtr).toBeGreaterThan(0);
});

test('calculates MA20 from closing prices', () => {
  const rows = Array.from({ length: 21 }, (_, index) => kline(index + 1, index));
  const ma20 = calculateCloseMovingAverage(rows, 20);

  expect(ma20.slice(0, 19).every(value => value === null)).toBe(true);
  expect(ma20[19]).toBe(10.5);
  expect(ma20[20]).toBe(11.5);
});
