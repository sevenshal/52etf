/**
 * 行情头部估值指标的两个纯计算。
 *
 * 快照(pe/pb/ps)是上一个收盘口径，盘中价格变了倍数要跟着变，所以统一按
 * 当前价/快照收盘价的比例折算——市盈率、市净率原本就是这么算的，市销率同理。
 */

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

/** 快照倍数按当前价折算；缺当前价或缺快照值时返回 null。 */
export const scaleToLivePrice = (snapshotValue, priceRatio) => {
  const value = toNumber(snapshotValue);
  const ratio = toNumber(priceRatio);
  return value === null || ratio === null ? null : value * ratio;
};
