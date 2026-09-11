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
const describeBasis = (value, record, band) => {
  if (!value) return null;
  if (value.basis === 'target_price') return `目标价本身（${value.quarter}）`;
  if (value.basis === 'pe_band') {
    return `前瞻PE ${formatFixed(band?.low_pe, 1)}~${formatFixed(band?.high_pe, 1)}倍`
      + ` × 未来12个月EPS ${formatFixed(record.ntm_eps)}`;
  }
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
            {record.method === 'pe_band' ? <Tag color="purple">PE通道</Tag> : null}
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
      render: (_, record) => describeBasis(record.value, record, detail.pe_band) || '--',
    },
    {
      title: '目标价',
      key: 'target_price',
      width: 160,
      render: (_, record) => {
        if (record.method === 'pe_band') return <Text type="secondary">未给目标价</Text>;
        const restated = formatRange(record.target_price_low, record.target_price_high);
        const adjustment = toNumber(record.price_adjustment);
        if (adjustment === null || Math.abs(adjustment - 1) < 0.005) return restated;
        const raw = formatRange(record.target_price_low_raw, record.target_price_high_raw);
        return (
          <Tooltip title={`研报原始目标价 ${raw}；研报发布后发生了除权（送转/分红），已按复权因子 ×${adjustment.toFixed(4)} 换算到当前股价口径`}>
            <span>
              {restated}
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>（原 {raw}）</Text>
            </span>
          </Tooltip>
        );
      },
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
      {detail.use_pe_band ? (
        <div style={{ color: '#666', marginBottom: 12 }}>
          前瞻PE通道（近3年20%/80%分位）：
          {detail.pe_band?.status === 'available'
            ? `${formatFixed(detail.pe_band.low_pe, 1)} ~ ${formatFixed(detail.pe_band.high_pe, 1)}倍，`
              + `中位 ${formatFixed(detail.pe_band.mid_pe, 1)}倍，当前 ${formatFixed(detail.pe_band.current_pe, 1)}倍`
            : '不可用，没给目标价的机构不参与估值'}
        </div>
      ) : null}
      {detail.pool === 'T-1' ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`T期披露后能给出估值的机构不足${detail.min_pool_organizations || 2}家，已退到T-1期披露日之后的研报，估值待更新。`}
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
        = 目标价 × 该研报对应财年 EPS ÷ 当前财年 EPS（缺 EPS 时用净利润之比）。研报发布后发生除权的，目标价按复权因子
        换算到当前股价口径；盈利预测指引展示研报原始 EPS。开启 PE 通道后，没给目标价的机构 = 通道 20% / 80% 分位
        × 该机构未来 12 个月 EPS（当年、次年 EPS 按年内已过时间加权）。上下限分别取各家机构的最低 / 最高值。
      </Text>
    </Modal>
  );
};

export default AStockConsensusValuationModal;
