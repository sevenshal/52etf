import { scaleToLivePrice } from './valuationMetrics';

test('快照倍数按当前价折算', () => {
  // 快照收盘 10 元、现价 11 元时，倍数同步放大 10%
  expect(scaleToLivePrice(20, 1.1)).toBeCloseTo(22);
  expect(scaleToLivePrice('3.5', 1)).toBeCloseTo(3.5);
});

test('缺当前价或缺快照值时不瞎算', () => {
  expect(scaleToLivePrice(20, null)).toBeNull();
  expect(scaleToLivePrice(null, 1.1)).toBeNull();
  expect(scaleToLivePrice(undefined, undefined)).toBeNull();
  expect(scaleToLivePrice('', 1.1)).toBeNull();
  expect(scaleToLivePrice('abc', 1.1)).toBeNull();
});
