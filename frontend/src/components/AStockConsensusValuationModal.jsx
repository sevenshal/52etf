import React, { useMemo } from 'react';
import { Alert, Modal, Segmented, Space, Table, Tag, Tooltip, Typography } from 'antd';

const { Text } = Typography;

const POOL_LABELS = { T: 'T池', 'T-1': 'T-1池' };

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

const formatFixed = (value, digits = 2) => {
  const number = toNumber(value);
  return number === null ? '--' : number.toFixed(digits);
};

const formatRange = (low, high) => {
  const lo = toNumber(low);
  const hi = toNumber(high);
  if (lo === null && hi === null) return '--';
  if (lo !== null && hi !== null && Math.abs(lo - hi) < 1e-6) return formatFixed(lo);
  return `${formatFixed(lo)} ~ ${formatFixed(hi)}`;
};

const describePeriod = (label, disclosedAt) => (label ? `${label}（${disclosedAt || '-'} 披露）` : '--');

// 下财年 / 下下财年估值 = 目标价 × 该研报对应财年预测 ÷ 当前财年预测，把推算过程原样摆出来。
const describeBasis = (value, record) => {
  if (!value) return null;
  if (value.basis === 'target_price') return `目标价本身（${value.quarter}）`;
  const field = value.basis === 'np' ? 'np' : 'eps';
  const name = value.basis === 'np' ? '净利润' : 'EPS';
  const forecastOf = quarter => (record.forecasts || []).find(item => item.quarter === quarter);
  return `目标价 × ${value.quarter} ${name} ${formatFixed(forecastOf(value.quarter)?.[field])}`
    + ` ÷ ${value.base_quarter} ${name} ${formatFixed(forecastOf(value.base_quarter)?.[field])}`;
};

const AStockConsensusValuationModal = ({
  open,
  detail,
  horizonOffset = 0,
  bound = 'lo',
  onHorizonChange,
  onClose,
}) => {
  const horizons = detail?.horizons || [];
  const horizon = horizons.find(item => item.offset === horizonOffset) || horizons[0];
  const highlightedOrg = horizon ? (bound === 'hi' ? horizon.hi_org : horizon.lo_org) : null;

  const rows = useMemo(() => {
    if (!horizon) return [];
    return (detail?.organizations || [])
      .map(org => ({ ...org, key: org.org_name, value: org.values?.[horizon.offset] || null }))
      .sort((a, b) => {
        if (!a.value || !b.value) return (a.value ? 0 : 1) - (b.value ? 0 : 1);
        return a.value.mid - b.value.mid;
      });
  }, [detail, horizon]);

  if (!horizon) return null;

  const columns = [
    {
      title: '机构',
      dataIndex: 'org_name',
      key: 'org_name',
      width: 150,
      fixed: 'left',
      render: (name, record) => {
        const aliases = (record.raw_org_names || []).filter(item => item !== name);
        return (
          <Space size={4} wrap>
            <Tooltip title={aliases.length ? `数据源中的名称：${aliases.join('、')}` : null}>
              <span>{name}</span>
            </Tooltip>
            {horizon.lo_org === name ? <Tag color="blue">下限</Tag> : null}
            {horizon.hi_org === name ? <Tag color="red">上限</Tag> : null}
          </Space>
        );
      },
    },
    {
      title: `${horizon.fiscal_year}财年估值`,
      key: 'value',
      width: 130,
      render: (_, record) => (record.value
        ? <strong>{formatRange(record.value.lo, record.value.hi)}</strong>
        : <Text type="secondary">未给出 {horizon.fiscal_year}Q4 预测</Text>),
    },
    {
      title: '推算依据',
      key: 'basis',
      width: 280,
      render: (_, record) => describeBasis(record.value, record) || '--',
    },
    {
      title: '目标价',
      key: 'target_price',
      width: 110,
      render: (_, record) => formatRange(record.target_price_low, record.target_price_high),
    },
    {
      title: '盈利预测指引',
      key: 'forecasts',
      width: 250,
      render: (_, record) => (
        <Space size={[4, 4]} wrap>
          {(record.forecasts || []).map(item => (
            <Tag key={item.quarter}>{item.quarter} EPS {formatFixed(item.eps)}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '研报',
      key: 'report',
      width: 300,
      render: (_, record) => (
        <div>
          <div>{record.report_title || '--'}</div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.report_date || '--'} · {record.author_name || '--'}{record.rating ? ` · ${record.rating}` : ''}
          </Text>
        </div>
      ),
    },
  ];

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={1180}
      title={`卖方一致预期估值 · ${detail?.symbol || ''}`}
      destroyOnClose
    >
      <Segmented
        value={horizon.offset}
        onChange={onHorizonChange}
        options={horizons.map(item => ({ label: `${item.label}（${item.fiscal_year}）`, value: item.offset }))}
      />
      <Space size={[20, 8]} wrap style={{ margin: '12px 0 8px' }}>
        <span>
          估值区间：
          <Text strong style={{ color: '#0066FF' }}>{formatFixed(horizon.lo)}</Text>
          （{horizon.lo_org || '--'}） ~{' '}
          <Text strong style={{ color: '#FF0000' }}>{formatFixed(horizon.hi)}</Text>
          （{horizon.hi_org || '--'}）
        </span>
        <span>机构均值：{formatFixed(horizon.avg)}</span>
        <span>给出 {horizon.fiscal_year}Q4 预测的机构：{horizon.organization_count} 家</span>
      </Space>
      <Space size={[20, 4]} wrap style={{ color: '#666', marginBottom: 12 }}>
        <span>
          研报池：
          <Tag color={detail.pool === 'T' ? 'green' : 'orange'}>{POOL_LABELS[detail.pool] || detail.pool}</Tag>
          {detail.pool_start_date} 之后，{detail.organization_count} 家机构 / {detail.report_count} 篇研报
        </span>
        <span>T期：{describePeriod(detail.t_period_label, detail.t_disclosure_date)}</span>
        <span>T-1期：{describePeriod(detail.t1_period_label, detail.t1_disclosure_date)}</span>
      </Space>
      {detail.pool === 'T-1' ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`T期披露后给出目标价的机构不足${detail.min_pool_organizations || 2}家，已退到T-1期披露日之后的研报，估值待更新。`}
        />
      ) : null}
      <Table
        size="small"
        columns={columns}
        dataSource={rows}
        pagination={false}
        scroll={{ x: 1200 }}
        onRow={record => ({
          style: record.org_name === highlightedOrg ? { background: '#fffbe6' } : undefined,
        })}
      />
      <Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
        每家机构只取它在研报池里最新一篇带目标价的研报；当前财年估值 = 目标价，下财年 / 下下财年
        = 目标价 × 该研报对应财年 EPS ÷ 当前财年 EPS（缺 EPS 时用净利润之比）。上下限分别取各家机构的最低 / 最高值。
      </Text>
    </Modal>
  );
};

export default AStockConsensusValuationModal;
