import {
  toXueqiuPortfolioUrl,
  toXueqiuStockUrl,
  toXueqiuSymbol,
} from './XueqiuStockLink';


describe('toXueqiuSymbol', () => {
  test.each([
    ['600256.SH', 'SH600256'],
    ['000001.SZ', 'SZ000001'],
    ['430047.BJ', 'BJ430047'],
    ['SH600256', 'SH600256'],
    ['SH.600256', 'SH600256'],
    ['SZ_000001', 'SZ000001'],
    ['600256', 'SH600256'],
    ['300750', 'SZ300750'],
    ['920002', 'BJ920002'],
    ['NVDA.US', 'NVDA'],
  ])('maps %s to %s', (input, expected) => {
    expect(toXueqiuSymbol(input)).toBe(expected);
  });

  test('rejects an unsupported value instead of placing it in a URL', () => {
    expect(toXueqiuSymbol('not/a/symbol')).toBeNull();
  });

  test('builds a stock page URL from a normalized symbol', () => {
    expect(toXueqiuStockUrl('600256.SH')).toBe('https://xueqiu.com/S/SH600256');
  });

  test('builds a normalized portfolio page URL', () => {
    expect(toXueqiuPortfolioUrl(' zh123456 ')).toBe('https://xueqiu.com/P/ZH123456');
  });

  test('does not build empty URLs', () => {
    expect(toXueqiuStockUrl('')).toBeUndefined();
    expect(toXueqiuPortfolioUrl('')).toBeUndefined();
  });
});
