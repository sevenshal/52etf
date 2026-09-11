import { alignToTradingDay } from './klineEvents';

/**
 * 雪球持仓在 K 线上的展示：综合权重曲线 + 5 日权价比方向标记。
 *
 * 方向**不在这里计算**：后端 `_xueqiu_5d_ratio_fields` → `_xueqiu_direction` 是唯一口径，
 * 历史接口已按全局快照序列"往前第 5 个持仓日"为锚点给每行算好 `direction_5d`，
 * 和「雪球持仓」模块的「权重和排名历史 · 方向」列同源。这里只负责读、对齐、上色。
 */
export const XUEQIU_DIRECTIONS = ['新进', '持平', '顺势加仓', '逆势吸筹', '借涨减仓', '减仓'];

// 颜色与「雪球持仓」模块的方向 Tag 一致（blue/default/green/cyan/orange/red）
export const XUEQIU_DIRECTION_META = {
  '新进': { glyph: '新', color: '#1677ff' },
  '持平': { glyph: '平', color: '#8c8c8c' },
  '顺势加仓': { glyph: '加', color: '#52c41a' },
  '逆势吸筹': { glyph: '吸', color: '#13c2c2' },
  '借涨减仓': { glyph: '抛', color: '#fa8c16' },
  '减仓': { glyph: '减', color: '#f5222d' },
};

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

/**
 * 把历史快照对齐到 K 线下标，返回与 dates 等长的数组（没有快照的那天是 null）。
 *
 * - 快照若不在交易日（极少见），落到下一个交易日；多个快照落到同一根 K 线时，
 *   取日期最晚的那个（最新的持仓状态）。
 * - 早于第一根 K 线的快照不画。
 * - `changed`：方向和上一个有方向的快照不同。图缩得很小时只在转折日写字。
 */
export const alignXueqiuHistory = (dates, historyRows = []) => {
  const points = new Array(dates.length).fill(null);
  if (!dates.length) return points;
  [...(historyRows || [])]
    .filter(row => row?.snapshot_date)
    .sort((a, b) => String(a.snapshot_date).localeCompare(String(b.snapshot_date)))
    .forEach(row => {
      const snapshotDate = String(row.snapshot_date).slice(0, 10);
      if (snapshotDate < dates[0]) return;
      const index = alignToTradingDay(dates, snapshotDate);
      if (index < 0) return;
      points[index] = {
        snapshotDate,
        weight: toNumber(row.composite_weight_pct),
        direction: XUEQIU_DIRECTION_META[row.direction_5d] ? row.direction_5d : null,
        ratio: toNumber(row.weight_price_ratio_5d),
        weightMultiple: toNumber(row.weight_multiple_5d),
        momentumMultiple: toNumber(row.momentum_multiple_5d),
        changed: false,
      };
    });

  let previous = null;
  points.forEach(point => {
    if (!point || !point.direction) return;
    point.changed = point.direction !== previous;
    previous = point.direction;
  });
  return points;
};

/**
 * 当前的雪球持仓排行。
 *
 * 历史接口只返回这只股票**上榜那些天**的行，最后一行是它"最后一次上榜"，不一定是今天——
 * 跌出榜单之后那一行里的排名是旧的。所以必须和全局最新快照日比对：
 *
 * - onLatest === true：最新一期快照仍在榜上，rank 就是当前排名；
 * - onLatest === false：已不在最新一期榜上（lastDate 为空表示从没上过榜）；
 * - onLatest === null：拿不到全局最新快照日，无法确认，只能说"截至 lastDate"。
 *
 * 历史为空且也没有全局日期（接口不可用）时返回 null，调用方应不显示这一项。
 */
export const resolveXueqiuRank = (historyRows = [], latestSnapshotDate = null) => {
  const latestDate = latestSnapshotDate ? String(latestSnapshotDate).slice(0, 10) : null;
  const rows = [...(historyRows || [])]
    .filter(row => row?.snapshot_date)
    .sort((a, b) => String(a.snapshot_date).localeCompare(String(b.snapshot_date)));
  if (!rows.length) {
    return latestDate ? { rank: null, onLatest: false, lastDate: null, latestSnapshotDate: latestDate } : null;
  }
  const last = rows[rows.length - 1];
  const lastDate = String(last.snapshot_date).slice(0, 10);
  return {
    rank: toNumber(last.composite_rank),
    onLatest: latestDate ? lastDate === latestDate : null,
    lastDate,
    latestSnapshotDate: latestDate,
  };
};
