import React from 'react';
import dayjs from 'dayjs';
import { Space, Table, Tooltip, Typography } from 'antd';

const { Text } = Typography;

const formatNumber = (value, digits = 0) => {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return '-';
  return num.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
};

const formatTime = value => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-');
const normalizeSymbolKey = value => String(value || '').trim().toUpperCase();
const normalizeMarketType = value => String(value || '').trim().toUpperCase();
const shouldShowT1Columns = marketType => {
  const normalized = normalizeMarketType(marketType);
  if (!normalized) return true;
  return normalized === 'A_STOCK';
};
const normalizeText = value => {
  if (value === undefined || value === null || value === '') return '-';
  return String(value);
};

const renderSymbol = (_, record) => {
  const symbol = normalizeText(record?.symbol);
  const name = record?.symbol_name;
  if (!name) return symbol;
  return (
    <Space direction="vertical" size={0}>
      <Text strong>{name}</Text>
      <Text type="secondary">{symbol}</Text>
    </Space>
  );
};

const renderSubAccountStrategy = (_, record) => (
  <Space direction="vertical" size={0}>
    <Text>{record?.sub_account_name || record?.name || '-'}</Text>
    <Text type="secondary">策略: {record?.strategy_name || record?.strategy_type || record?.binding_label || '-'}</Text>
  </Space>
);

const renderSellability = (value, record) => {
  const quantity = formatNumber(value);
  const details = [
    `原始可用 ${formatNumber(record?.raw_available_quantity)}`,
    `T+1锁定 ${formatNumber(record?.t1_locked_quantity)}`,
    `当日买入 ${formatNumber(record?.today_buy_quantity)}`,
  ];
  if (record?.sellable_rule) details.push(`规则 ${record.sellable_rule}`);
  return <Tooltip title={details.join(' / ')}>{quantity}</Tooltip>;
};

const buildMarketPriceRenderer = priceDetails => (_, record) => {
  const detail = priceDetails?.[normalizeSymbolKey(record?.symbol)];
  const price = Number(detail?.price ?? record?.market_price);
  if (!Number.isFinite(price) || price <= 0) return '-';
  const text = formatNumber(price, 3);
  return detail?.source ? <Tooltip title={`来源: ${detail.source}`}>{text}</Tooltip> : text;
};

const ExternalLedgerPositionsTable = ({
  rows = [],
  loading = false,
  pagination = false,
  onChange,
  priceDetails = {},
  getColumnFilterProps,
  showSubAccount = true,
  marketType,
  realizedPnlSortOrder = null,
  size = 'small',
  scroll,
}) => {
  const filterProps = key => (typeof getColumnFilterProps === 'function' ? getColumnFilterProps(key) : {});
  const showT1Columns = shouldShowT1Columns(marketType);
  const defaultScrollX = showSubAccount
    ? (showT1Columns ? 1680 : 1280)
    : (showT1Columns ? 1440 : 1040);
  const tableScroll = scroll
    ? { x: defaultScrollX, ...scroll }
    : { x: defaultScrollX, y: 320 };
  const columns = [
    showSubAccount
      ? {
        title: '子账户',
        dataIndex: 'sub_account_name',
        key: 'sub_account',
        width: 240,
        render: renderSubAccountStrategy,
        ...filterProps('sub_account'),
      }
      : null,
    {
      title: '标的',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 150,
      render: renderSymbol,
      ...filterProps('symbol'),
    },
    { title: '市价', key: 'market_price', width: 100, render: buildMarketPriceRenderer(priceDetails) },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    showT1Columns ? { title: '原始可用', dataIndex: 'raw_available_quantity', key: 'raw_available_quantity', width: 110, render: value => formatNumber(value) } : null,
    showT1Columns ? { title: '可卖', dataIndex: 'available_quantity', key: 'available_quantity', width: 150, render: renderSellability } : null,
    showT1Columns ? { title: 'T+1锁定', dataIndex: 't1_locked_quantity', key: 't1_locked_quantity', width: 110, render: value => formatNumber(value) } : null,
    showT1Columns ? { title: '当日买入', dataIndex: 'today_buy_quantity', key: 'today_buy_quantity', width: 100, render: value => formatNumber(value) } : null,
    { title: '成本价', dataIndex: 'avg_cost', key: 'avg_cost', width: 100, render: value => formatNumber(value, 4) },
    { title: '市值', dataIndex: 'market_value', key: 'market_value', width: 120, render: value => formatNumber(value, 2) },
    {
      title: '已实现盈亏',
      dataIndex: 'realized_pnl',
      key: 'realized_pnl',
      width: 130,
      sorter: true,
      sortOrder: realizedPnlSortOrder,
      render: value => formatNumber(value, 2),
    },
    { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 170, render: formatTime },
  ].filter(Boolean);

  return (
    <Table
      rowKey={record => `${record?.sub_account_id || 'sub'}-${normalizeSymbolKey(record?.symbol)}-${record?.id || ''}`}
      columns={columns}
      dataSource={rows}
      loading={loading}
      pagination={pagination}
      onChange={onChange}
      size={size}
      scroll={tableScroll}
    />
  );
};

export default ExternalLedgerPositionsTable;
