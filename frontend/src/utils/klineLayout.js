/**
 * K 线图多窗格布局（像素）：主图在上，副图(成交量、MACD、雪球持仓……)依次往下叠。
 *
 * 以前用百分比写死三个 grid；副图数量一旦可变，百分比就得每种组合各算一套。
 * 这里按像素累加：每个副图 = 缝隙(副图图例放在里面) + 固定高度。
 *
 * 主图高度由 baseHeight 在"默认两个副图"下反推——不带额外副图时和原先 600px 的
 * 百分比布局逐像素一致；多出来的副图往下加高整张图，而不是压缩主图。
 */
export const KLINE_LAYOUT = {
  top: 48,          // 主图上方给主图例
  gap: 30,          // 窗格之间的缝隙，副图图例画在这里
  subHeight: 72,    // 每个副图的高度
  axisLabel: 36,    // 最下面那个窗格的日期标签
  slider: 18,       // 缩放条
  bottom: 30,
  legendOffset: 9,  // 副图图例在缝隙里的纵向偏移
  minMainHeight: 160,
};

const DEFAULT_SUB_PANE_COUNT = 2;

export const buildKlinePaneLayout = (baseHeight = 600, subPaneKeys = ['volume', 'macd']) => {
  const {
    top, gap, subHeight, axisLabel, slider, bottom, legendOffset, minMainHeight,
  } = KLINE_LAYOUT;
  const fixed = top + DEFAULT_SUB_PANE_COUNT * (gap + subHeight) + axisLabel + slider + bottom;
  const mainHeight = Math.max(minMainHeight, Number(baseHeight) - fixed);

  const grids = [{ key: 'main', top, height: mainHeight }];
  const legendTops = {};
  let cursor = top + mainHeight;
  subPaneKeys.forEach(key => {
    legendTops[key] = cursor + legendOffset;
    cursor += gap;
    grids.push({ key, top: cursor, height: subHeight });
    cursor += subHeight;
  });
  const sliderTop = cursor + axisLabel;
  return {
    grids,
    legendTops,
    sliderTop,
    sliderHeight: slider,
    totalHeight: sliderTop + slider + bottom,
    mainHeight,
    indexOf: key => grids.findIndex(grid => grid.key === key),
  };
};
