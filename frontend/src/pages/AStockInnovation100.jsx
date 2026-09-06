import React from 'react';
import { Tag } from 'antd';
import CustomIndexResearch from './CustomIndexResearch';

const formatPercent = (value, digits = 2) => (
  value === null || value === undefined ? '-' : `${Number(value || 0).toFixed(digits)}%`
);
const formatMoney = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
);

export const INNOVATION100_CONFIG = {
  apiPrefix: '/api/a-stock-innovation100',
  eventKey: 'a_stock_innovation100_job',
  actionHostId: 'factor-lab-innovation100-actions',
  indexLabel: 'A股创新100',
  rebalanceNoun: '再平衡',
  rebalanceTypeMeta: {
    inception: { label: '初始建仓', color: 'purple' },
    annual_reconstitution: { label: '年度重构', color: 'blue' },
    quarterly_reweight: { label: '季度再平衡', color: 'cyan' },
  },
  constituentValueColumns: [
    { title: '原始权重', dataIndex: 'raw_weight_pct', key: 'raw_weight_pct', width: 110, render: value => formatPercent(value, 3) },
    { title: '流通市值(万元)', dataIndex: 'circ_mv', key: 'circ_mv', width: 140, render: value => formatMoney(value, 0) },
  ],
  ruleItems: rule => [
    { label: '样本', value: rule.universe || '-' },
    { label: '选样', value: rule.reconstitution || '-' },
    { label: '调权', value: rule.rebalance || '-' },
    {
      label: '权重上限',
      value: `单票 ${formatPercent(rule.max_single_weight_pct)} / 前五 ${formatPercent(rule.top5_weight_cap_pct)} / 大权重合计 ${formatPercent(rule.large_weight_cap_pct)}`,
    },
    {
      label: '流动性',
      value: `近${rule.liquidity_window || 60}日均成交额不低于 ${formatMoney(rule.min_avg_amount_60d, 0)} 千元`,
    },
  ],
};

const AStockInnovation100 = ({ embedded = false }) => (
  <CustomIndexResearch config={INNOVATION100_CONFIG} embedded={embedded} />
);

export default AStockInnovation100;
