import React from 'react';
import { Space, Typography } from 'antd';
import CustomIndexResearch from './CustomIndexResearch';

const { Text } = Typography;

const formatMoney = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
);

export const MICRO400_CONFIG = {
  apiPrefix: '/api/a-stock-micro400',
  eventKey: 'a_stock_micro400_job',
  actionHostId: 'factor-lab-micro400-actions',
  indexLabel: 'A股微盘400',
  rebalanceNoun: '调样',
  rebalanceTypeMeta: {
    inception: { label: '初始建仓', color: 'purple' },
    monthly_reconstitution: { label: '月度调样', color: 'blue' },
  },
  constituentValueColumns: [
    { title: '总市值(万元)', dataIndex: 'total_mv', key: 'total_mv', width: 140, render: value => formatMoney(value, 0) },
    { title: '流通市值(万元)', dataIndex: 'circ_mv', key: 'circ_mv', width: 140, render: value => formatMoney(value, 0) },
  ],
  ruleItems: rule => [
    { label: '对标', value: rule.benchmark || '-' },
    { label: '样本', value: rule.universe || '-' },
    { label: '选样', value: rule.selection || '-' },
    { label: '加权', value: rule.weighting || '-' },
    { label: '调样', value: rule.reconstitution || '-' },
    {
      label: '与真实指数的差异',
      value: (
        <Space direction="vertical" size={2}>
          {(rule.known_differences || []).map(item => (
            <Text type="secondary" key={item}>· {item}</Text>
          ))}
        </Space>
      ),
    },
  ],
};

const AStockMicro400 = ({ embedded = false }) => (
  <CustomIndexResearch config={MICRO400_CONFIG} embedded={embedded} />
);

export default AStockMicro400;
