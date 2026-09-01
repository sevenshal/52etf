import { toStockDetailSymbol } from './StockDetailLink';


test('normalizes holdings symbols for the internal stock detail route', () => {
  expect(toStockDetailSymbol('SH.600519')).toBe('600519.SH');
  expect(toStockDetailSymbol('SZ000001')).toBe('000001.SZ');
  expect(toStockDetailSymbol('430047.BJ')).toBe('430047.BJ');
  expect(toStockDetailSymbol('600519')).toBe('600519.SH');
});
