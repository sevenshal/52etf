import { buildKlinePaneLayout } from './klineLayout';

test('默认两个副图时和原先 600px 百分比布局逐像素一致', () => {
  const layout = buildKlinePaneLayout(600);

  // 原先：主图 top 8%/高 44%、成交量 top 57%/高 12%、MACD top 74%/高 12%、缩放条 top 92%
  expect(layout.grids.map(grid => [grid.key, grid.top, grid.height])).toEqual([
    ['main', 48, 264], ['volume', 342, 72], ['macd', 444, 72],
  ]);
  // 原先副图图例 top 53.5% / 70.5%
  expect(layout.legendTops).toEqual({ volume: 321, macd: 423 });
  expect(layout.sliderTop).toBe(552);
  expect(layout.totalHeight).toBe(600);
});

test('多一个雪球副图时整张图加高，主图不被压缩', () => {
  const layout = buildKlinePaneLayout(600, ['volume', 'macd', 'xueqiu']);

  expect(layout.mainHeight).toBe(264);
  expect(layout.grids[3]).toEqual({ key: 'xueqiu', top: 546, height: 72 });
  expect(layout.legendTops.xueqiu).toBe(525);
  expect(layout.sliderTop).toBe(654);
  expect(layout.totalHeight).toBe(702);
  expect(layout.indexOf('xueqiu')).toBe(3);
  expect(layout.indexOf('nope')).toBe(-1);
});

test('副图图例都落在上一个窗格底边和本窗格顶边之间的缝隙里', () => {
  const layout = buildKlinePaneLayout(600, ['volume', 'macd', 'xueqiu']);
  layout.grids.slice(1).forEach((grid, index) => {
    const previous = layout.grids[index];
    const legendTop = layout.legendTops[grid.key];
    expect(legendTop).toBeGreaterThanOrEqual(previous.top + previous.height);
    expect(legendTop + 14).toBeLessThanOrEqual(grid.top);
  });
});

test('基准高度太小时主图不低于下限', () => {
  expect(buildKlinePaneLayout(200).mainHeight).toBe(160);
});
