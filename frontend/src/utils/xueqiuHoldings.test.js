import {
  XUEQIU_DIRECTION_META,
  XUEQIU_DIRECTIONS,
  alignXueqiuHistory,
  resolveXueqiuRank,
} from './xueqiuHoldings';

// 2026-09-04 周五、09-07 周一
const DATES = ['2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07', '2026-09-08'];
const row = (snapshotDate, weight, direction, extra = {}) => ({
  snapshot_date: snapshotDate, composite_weight_pct: weight, direction_5d: direction, ...extra,
});

test('六个方向对应的单字：新、平、加、吸、抛、减', () => {
  expect(XUEQIU_DIRECTIONS.map(direction => XUEQIU_DIRECTION_META[direction].glyph))
    .toEqual(['新', '平', '加', '吸', '抛', '减']);
});

test('方向直接读后端的 direction_5d，不在前端重算', () => {
  // 权重倍数 1.2、权价比 1.2 按规则本该是加仓方向，但后端说持平就显示持平——口径以后端为准
  const [point] = alignXueqiuHistory(DATES, [
    row('2026-09-02', 1.5, '持平', { weight_multiple_5d: 1.2, weight_price_ratio_5d: 1.2, momentum_multiple_5d: 1.0 }),
  ]);

  expect(point.direction).toBe('持平');
  expect(point.ratio).toBe(1.2);
  expect(point.weightMultiple).toBe(1.2);
});

test('还没有 5 日锚点的快照没有方向，但权重照样画', () => {
  const points = alignXueqiuHistory(DATES, [row('2026-09-02', 0.8, null)]);

  expect(points[0].weight).toBe(0.8);
  expect(points[0].direction).toBeNull();
});

test('快照按交易日对齐；周末快照落到周一，同一根 K 线取最新那个', () => {
  const points = alignXueqiuHistory(DATES, [
    row('2026-09-07', 1.1, '持平'),
    row('2026-09-05', 1.0, '逆势吸筹'),
    row('2026-09-03', 0.9, '新进'),
  ]);

  expect(points.map(point => point && point.weight)).toEqual([null, 0.9, null, 1.1, null]);
  expect(points[3].snapshotDate).toBe('2026-09-07');
});

test('早于第一根 K 线或晚于最后一根的快照不画', () => {
  const points = alignXueqiuHistory(DATES, [row('2026-08-01', 1, '新进'), row('2026-09-30', 1, '减仓')]);
  expect(points.every(point => point === null)).toBe(true);
});

test('转折日标记：只在方向变化的那天为 true', () => {
  const points = alignXueqiuHistory(DATES, [
    row('2026-09-02', 1.0, '逆势吸筹'),
    row('2026-09-03', 1.1, '逆势吸筹'),
    row('2026-09-04', 1.1, null),
    row('2026-09-07', 1.1, '逆势吸筹'),
    row('2026-09-08', 1.0, '持平'),
  ]);

  expect(points.map(point => point.changed)).toEqual([true, false, false, false, true]);
});

test('未知方向文案按无方向处理；空输入不炸', () => {
  expect(alignXueqiuHistory(DATES, [row('2026-09-02', 1, '乱写的')])[0].direction).toBeNull();
  expect(alignXueqiuHistory([], [row('2026-09-02', 1, '新进')])).toEqual([]);
  expect(alignXueqiuHistory(DATES, null)).toEqual([null, null, null, null, null]);
});

// ---- 雪球持仓排行 ----
const ranked = (snapshotDate, rank) => ({ snapshot_date: snapshotDate, composite_rank: rank });

test('最新一期快照仍在榜上：显示当前排名', () => {
  expect(resolveXueqiuRank([ranked('2026-09-10', 3), ranked('2026-09-11', 1)], '2026-09-11'))
    .toEqual({ rank: 1, onLatest: true, lastDate: '2026-09-11', latestSnapshotDate: '2026-09-11' });
});

test('已跌出榜单：最后一次上榜的排名是旧的，onLatest 为 false', () => {
  // 8 月底还排第 1，之后不再持有——不能把这个 #1 当成今天的排名
  const rank = resolveXueqiuRank([ranked('2026-08-28', 1)], '2026-09-11');
  expect(rank.onLatest).toBe(false);
  expect(rank.rank).toBe(1);
  expect(rank.lastDate).toBe('2026-08-28');
});

test('从没上过榜：rank 为空，onLatest 为 false', () => {
  expect(resolveXueqiuRank([], '2026-09-11'))
    .toEqual({ rank: null, onLatest: false, lastDate: null, latestSnapshotDate: '2026-09-11' });
});

test('拿不到全局最新快照日：无法确认是否还在榜上', () => {
  expect(resolveXueqiuRank([ranked('2026-09-11', 5)], null).onLatest).toBeNull();
});

test('接口没有任何数据：返回 null，调用方不显示这一项', () => {
  expect(resolveXueqiuRank([], null)).toBeNull();
  expect(resolveXueqiuRank(undefined)).toBeNull();
});

test('历史行乱序时按日期取最后一次上榜', () => {
  expect(resolveXueqiuRank([ranked('2026-09-11', 2), ranked('2026-09-01', 9)], '2026-09-11').rank).toBe(2);
});
