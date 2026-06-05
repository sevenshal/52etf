import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import dayjs from 'dayjs';
import { Space, Table, Tooltip, Typography } from 'antd';
import request from '../utils/request';

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

const normalizeServerTableFilters = filters => Object.entries(filters || {}).reduce((result, [key, value]) => {
  const list = Array.isArray(value) ? value.filter(item => item !== null && item !== undefined && item !== '') : [];
  if (list.length) {
    result[key] = list.map(item => String(item));
  }
  return result;
}, {});
const normalizeSorter = sorter => {
  const activeSorter = Array.isArray(sorter) ? sorter.find(item => item?.order) : sorter;
  const sortField = activeSorter?.field || activeSorter?.columnKey;
  const sortOrder = activeSorter?.order;
  if (sortField !== 'realized_pnl' || !sortOrder) {
    return { sortField: null, sortOrder: null };
  }
  return { sortField, sortOrder };
};
const joinFilterValues = values => (Array.isArray(values) && values.length ? values.join(',') : undefined);

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
  pagination = false,
  showSubAccount = true,
  marketType,
  size = 'small',
  scroll,
  accountId,
  subAccountId,
  enabled = true,
  reloadToken = 0,
  onDataChange,
  onLoadingChange,
  defaultPageSize = 10,
}) => {
  const isRemoteEnabled = Boolean(accountId && enabled);
  const apiPath = `/api/external-trading-accounts/${accountId}/executor/status/ledger-positions`;
  const [remoteRows, setRemoteRows] = useState([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [remotePagination, setRemotePagination] = useState({ page: 1, page_size: defaultPageSize, total: 0 });
  const [remoteFilterOptions, setRemoteFilterOptions] = useState({});
  const [remotePriceDetails, setRemotePriceDetails] = useState({});
  const [remoteTableState, setRemoteTableState] = useState({
    page: 1,
    pageSize: defaultPageSize,
    filters: {},
    sortField: null,
    sortOrder: null,
  });
  const requestTokenRef = useRef(0);
  const remoteStateSignature = useMemo(() => JSON.stringify({
    page: remoteTableState.page || 1,
    pageSize: remoteTableState.pageSize || defaultPageSize,
    filters: {
      symbol: remoteTableState.filters?.symbol || [],
      sub_account: remoteTableState.filters?.sub_account || [],
      strategy: remoteTableState.filters?.strategy || [],
    },
    sortField: remoteTableState.sortField || null,
    sortOrder: remoteTableState.sortOrder || null,
  }), [
    defaultPageSize,
    remoteTableState.filters?.symbol,
    remoteTableState.filters?.sub_account,
    remoteTableState.filters?.strategy,
    remoteTableState.page,
    remoteTableState.pageSize,
    remoteTableState.sortField,
    remoteTableState.sortOrder,
  ]);

  useEffect(() => {
    if (!isRemoteEnabled) {
      return;
    }
    setRemoteRows([]);
    setRemotePagination({ page: 1, page_size: defaultPageSize, total: 0 });
    setRemoteFilterOptions({});
    setRemotePriceDetails({});
    setRemoteTableState({
      page: 1,
      pageSize: defaultPageSize,
      filters: {},
      sortField: null,
      sortOrder: null,
    });
  }, [accountId, subAccountId, defaultPageSize, isRemoteEnabled]);

  const setLoadingState = useCallback(
    value => {
      if (!isRemoteEnabled) return;
      setRemoteLoading(value);
      onLoadingChange?.(value);
    },
    [isRemoteEnabled, onLoadingChange],
  );

  const fetchRemoteData = useCallback(async stateOverride => {
    if (!isRemoteEnabled) return;
    const state = stateOverride || remoteTableState;
    const requestId = ++requestTokenRef.current;
    setLoadingState(true);
    try {
      const params = {
        page: state.page || 1,
        page_size: state.pageSize || defaultPageSize,
        symbol: joinFilterValues(state.filters?.symbol),
        sub_account: joinFilterValues(state.filters?.sub_account),
        strategy: joinFilterValues(state.filters?.strategy),
        sort_field: state.sortField || undefined,
        sort_order: state.sortOrder || undefined,
      };
      if (subAccountId) {
        params.sub_account_id = subAccountId;
      }
      const { data } = await request.get(apiPath, { params });
      if (requestId !== requestTokenRef.current) return;
      const nextRows = data?.rows || [];
      const nextPagination = data?.pagination || { page: state.page, page_size: state.pageSize, total: 0 };
      const nextPriceDetails = data?.price_details || {};
      const nextFilterOptions = data?.filter_options || {};
      setRemoteRows(nextRows);
      setRemotePagination(nextPagination);
      setRemotePriceDetails(nextPriceDetails);
      setRemoteFilterOptions(nextFilterOptions);
      onDataChange?.({
        rows: nextRows,
        pagination: nextPagination,
        priceDetails: nextPriceDetails,
        filterOptions: nextFilterOptions,
        sortField: state.sortField || null,
        sortOrder: state.sortOrder || null,
        filters: state.filters || {},
      });
    } catch (error) {
      if (requestId !== requestTokenRef.current) return;
      setRemoteRows([]);
      setRemotePagination({ page: 1, page_size: state.pageSize || defaultPageSize, total: 0 });
      setRemotePriceDetails({});
      setRemoteFilterOptions({});
      onDataChange?.({
        rows: [],
        pagination: { page: 1, page_size: state.pageSize || defaultPageSize, total: 0 },
        priceDetails: {},
        filterOptions: {},
        sortField: state.sortField || null,
        sortOrder: state.sortOrder || null,
        filters: state.filters || {},
      });
    } finally {
      if (requestId === requestTokenRef.current) {
        setLoadingState(false);
      }
    }
  }, [apiPath, isRemoteEnabled, defaultPageSize, onDataChange, requestTokenRef, remoteTableState, setLoadingState, subAccountId]);

  useEffect(() => {
    if (!isRemoteEnabled) {
      setRemoteRows([]);
      setRemotePagination({ page: 1, page_size: defaultPageSize, total: 0 });
      setRemotePriceDetails({});
      setRemoteFilterOptions({});
      setLoadingState(false);
      onDataChange?.({ rows: [], pagination: { page: 1, page_size: defaultPageSize, total: 0 }, priceDetails: {}, filterOptions: {} });
      return;
    }
    fetchRemoteData();
  }, [fetchRemoteData, isRemoteEnabled, reloadToken, accountId, subAccountId, remoteStateSignature, setLoadingState, onDataChange, defaultPageSize]);

  const handleRemoteTableChange = (tablePagination, tableFilters, sorter) => {
    setRemoteTableState(prevState => ({
      ...prevState,
      page: tablePagination?.current || prevState.page || 1,
      pageSize: tablePagination?.pageSize || prevState.pageSize || defaultPageSize,
      filters: normalizeServerTableFilters(tableFilters),
      ...normalizeSorter(sorter),
    }));
  };

  const filterProps = key => {
    const options = remoteFilterOptions?.[key] || [];
    return {
      key,
      filters: options,
      filterSearch: true,
      filterMultiple: true,
      filteredValue: remoteTableState.filters?.[key] || null,
    };
  };

  const showT1Columns = shouldShowT1Columns(marketType);
  const defaultScrollX = showSubAccount
    ? (showT1Columns ? 1680 : 1280)
    : (showT1Columns ? 1440 : 1040);
  const tableScroll = scroll
    ? { x: defaultScrollX, ...scroll }
    : { x: defaultScrollX, y: 320 };
  const tableLoading = remoteLoading;
  const tablePriceDetails = remotePriceDetails;
  const tablePagination = {
    current: remotePagination?.page || 1,
    pageSize: remotePagination?.page_size || remoteTableState.pageSize || defaultPageSize,
    total: remotePagination?.total || 0,
    showSizeChanger: true,
    pageSizeOptions: ['10', '20', '50', '100', '200'],
    showTotal: total => `共 ${total} 条`,
  };
  const finalTablePagination = pagination === false ? false : tablePagination;

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
    { title: '市价', key: 'market_price', width: 100, render: buildMarketPriceRenderer(tablePriceDetails) },
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
      sortOrder: remoteTableState.sortOrder,
      render: value => formatNumber(value, 2),
    },
    { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 170, render: formatTime },
  ].filter(Boolean);

  return (
    <Table
      rowKey={record => `${record?.sub_account_id || 'sub'}-${normalizeSymbolKey(record?.symbol)}-${record?.id || ''}`}
      columns={columns}
      dataSource={remoteRows}
      loading={tableLoading}
      pagination={finalTablePagination}
      onChange={handleRemoteTableChange}
      size={size}
      scroll={tableScroll}
    />
  );
};

export default ExternalLedgerPositionsTable;
