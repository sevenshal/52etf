// 估值点位闸门的选项与展示（情绪量能回测页、A股情绪量能实盘页共用）。估值点位越大越贵。
export const valuationWindowOptions = [
  { label: '近 252 日（约一年）', value: 252 },
  { label: '近 504 日（约两年）', value: 504 },
];

export const formatValuationGate = (params) => {
  if (!params) {
    return '-';
  }
  const parts = [];
  if (params.valuation_buy_max != null) {
    parts.push(`买≤${params.valuation_buy_max}`);
  }
  if (params.valuation_sell_min != null) {
    parts.push(`卖≥${params.valuation_sell_min}`);
    if (params.valuation_force_sell_greed != null) {
      parts.push(`贪恐≥${params.valuation_force_sell_greed}直接卖`);
    }
  }
  return parts.length ? `${params.valuation_window ?? 252}日点位 ${parts.join(' / ')}` : '关闭';
};
