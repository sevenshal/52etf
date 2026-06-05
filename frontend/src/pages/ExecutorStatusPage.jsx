import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import dayjs from 'dayjs';
import {
  Alert,
  Button,
  Empty,
  Form,
  InputNumber,
  Modal,
  Select,
  Segmented,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import request from '../utils/request';
import ExternalLedgerPositionsTable from '../components/ExternalLedgerPositionsTable';
import { PageSection, PageShell } from '../components/PageScaffold';
import './ExecutorStatusPage.css';

const { Text } = Typography;
const LAST_EXECUTOR_ACCOUNT_KEY = 'executorStatusAccountId';
const MARKET_TYPE_US_STOCK = 'US_STOCK';
const marketTypeLabel = value => (value === MARKET_TYPE_US_STOCK ? '美股' : 'A股');
const marketTypeColor = value => (value === MARKET_TYPE_US_STOCK ? 'blue' : 'red');
const formatTime = value => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-');
const formatNumber = (value, digits = 0) => {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return '-';
  return num.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
};
const formatOptionalNumber = (value, digits = 0) => (
  value === null || value === undefined || value === '' ? '-' : formatNumber(value, digits)
);
const sumNumberField = (rows, field) => (rows || []).reduce((total, row) => {
  const value = Number(row?.[field] || 0);
  return Number.isFinite(value) ? total + value : total;
}, 0);
const normalizeText = value => {
  if (value === undefined || value === null || value === '') return '-';
  return String(value);
};
const normalizeSymbolKey = value => String(value || '').trim().toUpperCase();
const priceLevelLabel = value => {
  if (value === -1) return '本方最优价';
  if (value === 0) return '参考价限价';
  if (value === null || value === undefined) return '-';
  return `${value}档`;
};
const sequenceToText = value => (Array.isArray(value) && value.length ? value : [1, 2, 3, 5, -1]).join(',');
const timeoutSequenceToText = value => (Array.isArray(value) && value.length ? value : [120, 120, 120, 120, 120]).join(',');
const formatPolicy = policy => {
  if (!policy) return '-';
  const maxReplace = policy.max_replace_count ?? policy.executor_max_replace_count;
  const maxSlippage = policy.max_slippage_pct ?? policy.executor_max_slippage_pct;
  const sequence = sequenceToText(policy.price_level_sequence ?? policy.executor_price_level_sequence);
  const timeoutSequence = timeoutSequenceToText(
    policy.order_timeout_seconds_sequence
      ?? policy.executor_order_timeout_seconds_sequence
      ?? (policy.order_timeout_seconds || policy.executor_order_timeout_seconds
        ? [policy.order_timeout_seconds ?? policy.executor_order_timeout_seconds]
        : undefined)
  );
  return `档位序列${sequence} / 超时序列${timeoutSequence}s / 重定价${maxReplace ?? '-'}次 / 滑点${maxSlippage ?? '-'}%`;
};
const roleLabel = value => {
  if (value === 'PARENT') return '父单';
  if (value === 'CHILD') return '子单';
  if (value === 'BLOCK') return '阻断';
  return value || '-';
};
const roleColor = value => {
  if (value === 'PARENT') return 'blue';
  if (value === 'CHILD') return 'green';
  if (value === 'BLOCK') return 'orange';
  return 'default';
};
const renderRoleTag = value => (
  <Tag className="executor-role-tag" color={roleColor(value)}>
    {roleLabel(value)}
  </Tag>
);
const successfulOrderStatuses = new Set(['FILLED', 'SUCCESS', 'SUCCEEDED', 'DONE', 'COMPLETED']);
const isOrderSuccessful = status => successfulOrderStatuses.has(String(status || '').trim().toUpperCase());
const getOrderMessage = record => String(record?.message || '').trim();
const shouldShowOrderMessage = record => !isOrderSuccessful(record?.status) && Boolean(getOrderMessage(record));
const orderStatusColor = status => {
  if (isOrderSuccessful(status)) return 'success';
  if (['REJECTED', 'FAILED', 'EXPIRED'].includes(status)) return 'error';
  if (['CANCELED', 'PARTIALLY_CANCELED'].includes(status)) return 'default';
  if (['PARTIALLY_FILLED', 'CANCEL_PENDING', 'BLOCKED_INSUFFICIENT_SELLABLE', 'BLOCKED_INSUFFICIENT_POSITION', 'BLOCKED_NON_RETRYABLE_REJECTION'].includes(status)) return 'warning';
  return 'processing';
};
const pTradeStatusLabels = {
  '0': '未报',
  '1': '待报',
  '2': '已报',
  '3': '已报待撤',
  '4': '部成待撤',
  '5': '部撤',
  '6': '已撤',
  '7': '部成',
  '8': '已成',
  '9': '废单',
  '+': '已报',
  '-': '废单',
  V: '已确认',
};
const pTradeStatusColor = value => {
  const key = String(value || '').trim().toUpperCase();
  if (key === '8') return 'success';
  if (['5', '6'].includes(key)) return 'default';
  if (key === '7' || key === '4') return 'warning';
  if (key === '9' || key === '-') return 'error';
  if (['2', '+', 'V'].includes(key)) return 'processing';
  return 'default';
};
const renderPTradeStatus = value => {
  const key = String(value || '').trim().toUpperCase();
  if (!key) return '-';
  const label = pTradeStatusLabels[key] || '未知';
  return (
    <Tooltip title={`PTrade原始状态: ${key}`}>
      <Tag color={pTradeStatusColor(key)}>{label}</Tag>
    </Tooltip>
  );
};
const eventTypeLabel = value => {
  if (value === 'order_event') return '订单回报';
  if (value === 'trade_event') return '成交回报';
  return value || '-';
};
const eventTypeColor = value => {
  if (value === 'trade_event') return 'blue';
  if (value === 'order_event') return 'purple';
  return 'default';
};
const processStatusColor = value => {
  if (value === 'PROCESSED') return 'success';
  if (value === 'FAILED') return 'error';
  if (value === 'UNMATCHED') return 'warning';
  if (value === 'RECEIVED') return 'processing';
  return 'default';
};
const deliverStatusColor = value => {
  if (value === 'MATCHED') return 'success';
  if (value === 'POSITION_ADJUSTED') return 'blue';
  if (value === 'UNMATCHED') return 'warning';
  if (value === 'IGNORED') return 'default';
  return 'default';
};
const diffTextColor = value => {
  const num = Number(value || 0);
  if (!Number.isFinite(num) || num === 0) return undefined;
  return num > 0 ? '#cf1322' : '#389e0d';
};
const getBlockLabel = record => {
  if (record?.blocked_status === 'BLOCKED_NON_RETRYABLE_REJECTION') return '规则阻断';
  if (record?.status === 'BLOCKED_NON_RETRYABLE_REJECTION') return '规则阻断';
  if (record?.blocked_status === 'BLOCKED_INSUFFICIENT_POSITION') return '持仓不足';
  if (record?.status === 'BLOCKED_INSUFFICIENT_POSITION') return '持仓不足';
  return '可卖阻断';
};
const joinFilterValues = values => (Array.isArray(values) && values.length ? values.join(',') : undefined);
const normalizeServerTableFilters = filters => Object.entries(filters || {}).reduce((result, [key, value]) => {
  const list = Array.isArray(value) ? value.filter(item => item !== null && item !== undefined && item !== '') : [];
  if (list.length) {
    result[key] = list.map(item => String(item));
  }
  return result;
}, {});
const renderSymbolText = record => {
  const symbol = normalizeText(record?.symbol);
  const name = record?.symbol_name;
  return name ? `${name} ${symbol}` : symbol;
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
const tableEndpoints = {
  target_positions: 'target-positions',
  ledger_positions: 'ledger-positions',
  orders: 'orders',
  fills: 'fills',
  deliver_records: 'deliver-records',
  events: 'events',
};
const tableLabels = {
  target_positions: '目标仓位',
  ledger_positions: '账本持仓',
  orders: '订单生命周期',
  fills: '成交回报',
  deliver_records: '交割单',
  events: '事件流水',
};
const createEmptyTable = () => ({ rows: [], pagination: { page: 1, page_size: 10, total: 0 }, price_details: {}, filter_options: {} });
const createEmptyTables = () => Object.keys(tableEndpoints).reduce((result, key) => ({ ...result, [key]: createEmptyTable() }), {});
const createDefaultTableState = () => Object.keys(tableEndpoints).reduce((result, key) => ({
  ...result,
  [key]: { page: 1, pageSize: 10, filters: {}, unfilledOnly: false, activeOnly: false, nonEmptyOnly: false, deltaOnly: false },
}), {});
const tableRowKey = (tableKey, row, index) => {
  if (row?.id !== undefined && row?.id !== null) return `${tableKey}:${row.id}`;
  if (['target_positions', 'ledger_positions'].includes(tableKey)) {
    return `${tableKey}:${row?.sub_account_id || ''}:${normalizeSymbolKey(row?.symbol)}`;
  }
  return `${tableKey}:${row?.sub_account_id || ''}:${normalizeSymbolKey(row?.symbol)}:${row?.created_at || row?.updated_at || index}`;
};
const mergeTableRows = (tableKey, currentRows = [], nextRows = []) => {
  const seen = new Set(currentRows.map((row, index) => tableRowKey(tableKey, row, index)));
  const merged = [...currentRows];
  nextRows.forEach((row, index) => {
    const key = tableRowKey(tableKey, row, currentRows.length + index);
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(row);
    }
  });
  return merged;
};
const EXECUTOR_TABS = [
  { key: 'sub_accounts', label: '子账户' },
  { key: 'targets', label: '目标仓位' },
  { key: 'ledger', label: '账本持仓' },
  { key: 'orders', label: '订单' },
  { key: 'fills', label: '成交' },
  { key: 'deliver_records', label: '交割单' },
  { key: 'events', label: '事件' },
  { key: 'plan', label: '净额预览' },
];

const ExecutorStatusPage = ({ embedded = false }) => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [accounts, setAccounts] = useState([]);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [selectedAccountId, setSelectedAccountId] = useState(null);
  const [status, setStatus] = useState(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [subAccountStatus, setSubAccountStatus] = useState({ rows: [], price_details: {} });
  const [subAccountLoading, setSubAccountLoading] = useState(false);
  const [tables, setTables] = useState(createEmptyTables);
  const [tableState, setTableState] = useState(createDefaultTableState);
  const [tableLoading, setTableLoading] = useState({});
  const [tableLoaded, setTableLoaded] = useState({});
  const [plan, setPlan] = useState(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [planLoaded, setPlanLoaded] = useState(false);
  const [activeTab, setActiveTab] = useState('sub_accounts');
  const [executeLoading, setExecuteLoading] = useState(false);
  const [markBlockSuccessForm] = Form.useForm();
  const [repairParentFillForm] = Form.useForm();
  const [markBlockRecord, setMarkBlockRecord] = useState(null);
  const [repairParentRecord, setRepairParentRecord] = useState(null);
  const [orderActionId, setOrderActionId] = useState(null);
  const [ledgerReloadToken, setLedgerReloadToken] = useState(0);
  const infiniteLoadingRef = useRef({});
  const targetToolbarRef = useRef(null);
  const orderToolbarRef = useRef(null);

  const selectedAccount = useMemo(() => (
    accounts.find(item => String(item.id) === String(selectedAccountId)) || null
  ), [accounts, selectedAccountId]);
  const subRows = subAccountStatus?.rows || [];
  const targetRows = tables.target_positions?.rows || [];
  const ledgerRows = tables.ledger_positions?.rows || [];
  const orderRows = tables.orders?.rows || [];
  const fillRows = tables.fills?.rows || [];
  const deliverRows = tables.deliver_records?.rows || [];
  const eventRows = tables.events?.rows || [];
  const demandRows = plan?.plan?.demands || [];
  const internalCrossRows = plan?.plan?.internal_crosses || [];
  const externalOrderRows = plan?.plan?.external_orders || [];
  const totals = {
    cashAllocated: sumNumberField(subRows, 'cash_allocated'),
    netAsset: sumNumberField(subRows, 'net_asset'),
    cashAvailable: sumNumberField(subRows, 'cash_available'),
  };
  const priceDetails = {
    ...(status?.price_details || {}),
    ...(subAccountStatus?.price_details || {}),
    ...(plan?.price_details || {}),
    ...(tables.target_positions?.price_details || {}),
    ...(tables.ledger_positions?.price_details || {}),
    ...(tables.orders?.price_details || {}),
    ...(tables.fills?.price_details || {}),
    ...(tables.deliver_records?.price_details || {}),
    ...(tables.events?.price_details || {}),
  };

  const loadAccounts = useCallback(async () => {
    setAccountsLoading(true);
    try {
      const { data } = await request.get('/api/external-trading-accounts');
      const rows = data || [];
      setAccounts(rows);
      const accountIdFromUrl = searchParams.get('account_id');
      const savedAccountId = localStorage.getItem(LAST_EXECUTOR_ACCOUNT_KEY);
      const preferredId = accountIdFromUrl || savedAccountId;
      const preferred = rows.find(item => String(item.id) === String(preferredId));
      const fallback = rows.find(item => item.enabled !== false) || rows[0];
      const nextAccountId = preferred?.id || fallback?.id || null;
      if (nextAccountId) {
        setSelectedAccountId(nextAccountId);
        localStorage.setItem(LAST_EXECUTOR_ACCOUNT_KEY, String(nextAccountId));
      }
    } catch (error) {
      message.error(error.response?.data?.detail || '获取外部交易账号失败');
    } finally {
      setAccountsLoading(false);
    }
  }, [searchParams]);

  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  const buildTableParams = (state, tableKey) => ({
    page: state?.page || 1,
    page_size: state?.pageSize || 10,
    symbol: joinFilterValues(state?.filters?.symbol),
    trade_date: joinFilterValues(state?.filters?.trade_date),
    sub_account: joinFilterValues(state?.filters?.sub_account),
    strategy: joinFilterValues(state?.filters?.strategy),
    side: joinFilterValues(state?.filters?.side),
    status: joinFilterValues(state?.filters?.status),
    role: joinFilterValues(state?.filters?.role),
    event_type: joinFilterValues(state?.filters?.event_type),
    process_status: joinFilterValues(state?.filters?.process_status),
    active_only: tableKey === 'orders' && state?.activeOnly ? 'true' : undefined,
    unfilled_only: tableKey === 'orders' && state?.unfilledOnly ? 'true' : undefined,
    non_empty_only: tableKey === 'target_positions' && state?.nonEmptyOnly ? 'true' : undefined,
    delta_only: tableKey === 'target_positions' && state?.deltaOnly ? 'true' : undefined,
  });

  const fetchBaseStatus = useCallback(async account => {
    if (!account?.id) return;
    setStatusLoading(true);
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/executor/status`);
      setStatus(data || null);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取执行器状态失败');
      setStatus(null);
    } finally {
      setStatusLoading(false);
    }
  }, []);

  const fetchSubAccounts = useCallback(async account => {
    if (!account?.id) return;
    setSubAccountLoading(true);
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/executor/status/sub-accounts`);
      setSubAccountStatus({ rows: data?.rows || [], price_details: data?.price_details || {} });
    } catch (error) {
      message.error(error.response?.data?.detail || '获取子账户状态失败');
      setSubAccountStatus({ rows: [], price_details: {} });
    } finally {
      setSubAccountLoading(false);
    }
  }, []);

  const fetchTable = useCallback(async (account, tableKey, nextState, options = {}) => {
    if (!account?.id || !tableEndpoints[tableKey]) return;
    const append = options.append === true;
    setTableLoading(prev => ({ ...prev, [tableKey]: true }));
    try {
      const { data } = await request.get(
        `/api/external-trading-accounts/${account.id}/executor/status/${tableEndpoints[tableKey]}`,
        { params: buildTableParams(nextState, tableKey) }
      );
      const nextRows = data?.rows || [];
      setTables(prev => ({
        ...prev,
        [tableKey]: {
          ...createEmptyTable(),
          ...(data || {}),
          rows: append ? mergeTableRows(tableKey, prev[tableKey]?.rows || [], nextRows) : nextRows,
          price_details: data?.price_details || {},
          filter_options: data?.filter_options || {},
        },
      }));
      setTableLoaded(prev => ({ ...prev, [tableKey]: true }));
    } catch (error) {
      message.error(error.response?.data?.detail || `获取${tableLabels[tableKey]}失败`);
      if (!append) {
        setTables(prev => ({ ...prev, [tableKey]: createEmptyTable() }));
        setTableLoaded(prev => ({ ...prev, [tableKey]: false }));
      }
    } finally {
      setTableLoading(prev => ({ ...prev, [tableKey]: false }));
    }
  }, []);

  const fetchPlan = useCallback(async account => {
    if (!account?.id) return;
    setPlanLoading(true);
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/executor/status/plan`);
      setPlan(data || null);
      setPlanLoaded(true);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取净额预览失败');
      setPlan(null);
      setPlanLoaded(false);
    } finally {
      setPlanLoading(false);
    }
  }, []);

  const tableKeyFromTab = tabKey => {
    if (tabKey === 'targets') return 'target_positions';
    if (tabKey === 'ledger') return 'ledger_positions';
    if (tabKey === 'orders') return 'orders';
    if (tabKey === 'fills') return 'fills';
    if (tabKey === 'deliver_records') return 'deliver_records';
    if (tabKey === 'events') return 'events';
    return null;
  };

  const fetchActiveTab = useCallback((account, tabKey, force = false) => {
    if (!account?.id) return;
    if (tabKey === 'sub_accounts') {
      fetchSubAccounts(account);
      return;
    }
    if (tabKey === 'plan') {
      if (force || !planLoaded) fetchPlan(account);
      return;
    }
    const tableKey = tableKeyFromTab(tabKey);
    if (tableKey === 'ledger_positions') {
      return;
    }
    if (tableKey && (force || !tableLoaded[tableKey])) {
      const nextState = force
        ? { ...(tableState[tableKey] || { pageSize: 10, filters: {} }), page: 1 }
        : tableState[tableKey];
      if (force) {
        setTableState(prev => ({ ...prev, [tableKey]: nextState }));
      }
      fetchTable(account, tableKey, nextState);
    }
  }, [fetchPlan, fetchSubAccounts, fetchTable, planLoaded, tableLoaded, tableState]);

  const resetExecutorData = useCallback(() => {
    const nextTableState = createDefaultTableState();
    setStatus(null);
    setSubAccountStatus({ rows: [], price_details: {} });
    setTables(createEmptyTables());
    setTableState(nextTableState);
    setTableLoading({});
    setTableLoaded({});
    setPlan(null);
    setPlanLoaded(false);
    return nextTableState;
  }, []);

  useEffect(() => {
    if (!selectedAccount) return;
    resetExecutorData();
    fetchBaseStatus(selectedAccount);
    fetchSubAccounts(selectedAccount);
  }, [fetchBaseStatus, fetchSubAccounts, resetExecutorData, selectedAccount]);

  const handleAccountChange = value => {
    setSelectedAccountId(value);
    localStorage.setItem(LAST_EXECUTOR_ACCOUNT_KEY, String(value));
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('account_id', String(value));
    setSearchParams(nextParams, { replace: true });
  };

  const refreshAll = () => {
    if (!selectedAccount) return;
    setLedgerReloadToken(prev => prev + 1);
    fetchBaseStatus(selectedAccount);
    fetchActiveTab(selectedAccount, activeTab, true);
  };

  const executeNetted = async ({ force = false } = {}) => {
    if (!selectedAccount?.id) return;
    setExecuteLoading(true);
    try {
      const { data } = await request.post(`/api/external-trading-accounts/${selectedAccount.id}/executor/execute`, { force });
      const accountResult = data?.accounts?.[0] || {};
      const marketClosedResult = accountResult?.reason === 'market_closed' ? accountResult : data;
      if (marketClosedResult?.status === 'SKIPPED' && marketClosedResult?.reason === 'market_closed') {
        const marketLabel = marketClosedResult.market_label || marketTypeLabel(selectedAccount?.market_type);
        message.warning(`当前不在 ${marketLabel}交易时段，执行器将在 ${formatTime(marketClosedResult.next_run_at)} 后继续处理`);
      } else if (accountResult?.status === 'CANCEL_REQUESTED') {
        message.success('已提交撤单，等待回报后执行器会继续撮合');
      } else {
        message.success(accountResult?.result?.message || (force ? '已强制触发净额撮合执行器' : '已触发净额撮合执行器'));
      }
      refreshAll();
    } catch (error) {
      message.error(error.response?.data?.detail || '执行净额撮合失败');
    } finally {
      setExecuteLoading(false);
    }
  };

  const handleTabChange = tabKey => {
    setActiveTab(tabKey);
    fetchActiveTab(selectedAccount, tabKey);
  };

  const handleTableChange = tableKey => (pagination, filters, sorter) => {
    if (tableKey === 'ledger_positions') return;
    const previousState = tableState[tableKey] || { page: 1, pageSize: 10, filters: {} };
    const next = {
      page: pagination?.current || 1,
      pageSize: pagination?.pageSize || previousState.pageSize || 10,
      filters: normalizeServerTableFilters(filters),
      sortField: previousState.sortField || null,
      sortOrder: previousState.sortOrder || null,
      unfilledOnly: previousState.unfilledOnly || false,
      activeOnly: previousState.activeOnly || false,
      nonEmptyOnly: previousState.nonEmptyOnly || false,
      deltaOnly: previousState.deltaOnly || false,
    };
    const nextState = { ...tableState, [tableKey]: next };
    setTableState(nextState);
    fetchTable(selectedAccount, tableKey, next);
  };

  const keepToolbarInPlace = (toolbarRef, beforeTop) => {
    const scroller = document.querySelector('.app-shell__scroll');
    if (!scroller || beforeTop === null || beforeTop === undefined) return;
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const afterTop = toolbarRef.current?.getBoundingClientRect().top;
        if (afterTop === null || afterTop === undefined) return;
        scroller.scrollTop += afterTop - beforeTop;
      });
    });
  };

  const keepOrderToolbarInPlace = beforeTop => {
    keepToolbarInPlace(orderToolbarRef, beforeTop);
  };

  const keepTargetToolbarInPlace = beforeTop => {
    keepToolbarInPlace(targetToolbarRef, beforeTop);
  };

  const handleTargetPositionFilterChange = value => {
    const nonEmptyOnly = value === 'non_empty';
    const deltaOnly = value === 'delta';
    const previousState = tableState.target_positions || { pageSize: 10, filters: {} };
    if (
      (previousState.nonEmptyOnly || false) === nonEmptyOnly
      && (previousState.deltaOnly || false) === deltaOnly
    ) return;
    const toolbarTop = targetToolbarRef.current?.getBoundingClientRect().top;
    const nextStateForTargets = {
      ...previousState,
      page: 1,
      nonEmptyOnly,
      deltaOnly,
    };
    setTableState(prev => ({ ...prev, target_positions: nextStateForTargets }));
    setTableLoaded(prev => ({ ...prev, target_positions: false }));
    if (selectedAccount?.id) {
      fetchTable(selectedAccount, 'target_positions', nextStateForTargets).finally(() => {
        keepTargetToolbarInPlace(toolbarTop);
      });
    } else {
      keepTargetToolbarInPlace(toolbarTop);
    }
  };

  const handleOrderLifecycleFilterChange = value => {
    const activeOnly = value === 'active';
    const unfilledOnly = value === 'unfilled';
    const previousState = tableState.orders || { pageSize: 10, filters: {} };
    if (
      (previousState.activeOnly || false) === activeOnly
      && (previousState.unfilledOnly || false) === unfilledOnly
    ) return;
    const toolbarTop = orderToolbarRef.current?.getBoundingClientRect().top;
    const nextStateForOrders = {
      ...previousState,
      page: 1,
      activeOnly,
      unfilledOnly,
    };
    setTableState(prev => ({ ...prev, orders: nextStateForOrders }));
    setTableLoaded(prev => ({ ...prev, orders: false }));
    if (selectedAccount?.id) {
      fetchTable(selectedAccount, 'orders', nextStateForOrders).finally(() => {
        keepOrderToolbarInPlace(toolbarTop);
      });
    } else {
      keepOrderToolbarInPlace(toolbarTop);
    }
  };

  const paginationFor = tableKey => {
    const currentState = tableState[tableKey] || { page: 1, pageSize: 10 };
    const pagination = tables[tableKey]?.pagination || {};
    return {
      current: pagination.page || currentState.page,
      pageSize: pagination.page_size || currentState.pageSize,
      total: pagination.total || 0,
      showSizeChanger: true,
      pageSizeOptions: ['10', '20', '50', '100', '200'],
      showTotal: total => `共 ${total} 条`,
    };
  };

  const hasMoreTableRows = useCallback(tableKey => {
    const table = tables[tableKey] || createEmptyTable();
    const total = Number(table.pagination?.total || 0);
    const loaded = (table.rows || []).length;
    return total > loaded;
  }, [tables]);

  const loadMoreTable = useCallback(tableKey => {
    if (tableKey === 'ledger_positions') return;
    if (!selectedAccount?.id || !tableEndpoints[tableKey]) return;
    if (tableLoading[tableKey] || infiniteLoadingRef.current[tableKey] || !hasMoreTableRows(tableKey)) return;
    const currentState = tableState[tableKey] || { page: 1, pageSize: 10, filters: {} };
    const currentPagination = tables[tableKey]?.pagination || {};
    const nextState = {
      ...currentState,
      page: Number(currentPagination.page || currentState.page || 1) + 1,
      pageSize: Number(currentPagination.page_size || currentState.pageSize || 10),
    };
    infiniteLoadingRef.current[tableKey] = true;
    setTableState(prev => ({ ...prev, [tableKey]: nextState }));
    fetchTable(selectedAccount, tableKey, nextState, { append: true }).finally(() => {
      infiniteLoadingRef.current[tableKey] = false;
    });
  }, [fetchTable, hasMoreTableRows, selectedAccount, tableLoading, tables, tableState]);

  const handleLedgerPositionsDataChange = useCallback(({
    rows = [],
    pagination: ledgerPagination,
    priceDetails: nextPriceDetails = {},
    filterOptions = {},
    sortField,
    sortOrder,
    filters = {},
  }) => {
    setTables(prev => ({
      ...prev,
      ledger_positions: {
        ...createEmptyTable(),
        rows,
        pagination: ledgerPagination || { page: 1, page_size: 10, total: 0 },
        price_details: nextPriceDetails,
        filter_options: filterOptions,
      },
    }));
    setTableState(prev => {
      const nextPagination = ledgerPagination || {};
      const current = prev.ledger_positions || { page: 1, pageSize: 10, filters: {} };
      return {
        ...prev,
        ledger_positions: {
          ...current,
          page: nextPagination.page || current.page || 1,
          pageSize: nextPagination.page_size || current.pageSize || 10,
          sortField: sortField || null,
          sortOrder: sortOrder || null,
          filters,
        },
      };
    });
    setTableLoaded(prev => ({ ...prev, ledger_positions: true }));
  }, []);

  const handleLedgerPositionsLoadingChange = useCallback(loading => {
    setTableLoading(prev => ({ ...prev, ledger_positions: loading }));
  }, []);

  useEffect(() => {
    const tableKey = tableKeyFromTab(activeTab);
    if (!tableKey || !selectedAccount?.id) return undefined;
    const mediaQuery = window.matchMedia('(max-width: 760px)');
    if (!mediaQuery.matches) return undefined;
    const scroller = document.querySelector('.app-shell__scroll');
    if (!scroller) return undefined;
    let ticking = false;
    const checkScrollPosition = () => {
      ticking = false;
      if (!mediaQuery.matches) return;
      const distanceToBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      if (distanceToBottom < 320) {
        loadMoreTable(tableKey);
      }
    };
    const handleScroll = () => {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(checkScrollPosition);
    };
    scroller.addEventListener('scroll', handleScroll, { passive: true });
    checkScrollPosition();
    return () => scroller.removeEventListener('scroll', handleScroll);
  }, [activeTab, loadMoreTable, selectedAccount?.id]);

  const serverFilterProps = (tableKey, filterKey) => {
    const filteredValue = tableState[tableKey]?.filters?.[filterKey] || [];
    return {
      key: filterKey,
      filters: tables[tableKey]?.filter_options?.[filterKey] || [],
      filterSearch: true,
      filteredValue: filteredValue.length ? filteredValue : null,
    };
  };

  const renderMarketPrice = (_, record) => {
    const detail = priceDetails[normalizeSymbolKey(record?.symbol)];
    const price = Number(detail?.price);
    if (!Number.isFinite(price) || price <= 0) return '-';
    const text = formatNumber(price, 3);
    return detail?.source ? <Tooltip title={`来源: ${detail.source}`}>{text}</Tooltip> : text;
  };

  const renderLifecyclePrice = (value, source, digits = 4) => {
    const text = formatOptionalNumber(value, digits);
    if (text === '-' || !source) return text;
    return <Tooltip title={`来源: ${source}`}>{text}</Tooltip>;
  };

  const openMarkBlockSuccess = record => {
    const defaultPrice = Number(record?.submitted_price || record?.avg_fill_price || 0);
    setMarkBlockRecord(record);
    markBlockSuccessForm.setFieldsValue({
      price: Number.isFinite(defaultPrice) && defaultPrice > 0 ? defaultPrice : undefined,
    });
  };

  const openRepairParentFill = record => {
    const defaultPrice = Number(record?.avg_fill_price || record?.submitted_price || 0);
    setRepairParentRecord(record);
    repairParentFillForm.setFieldsValue({
      price: Number.isFinite(defaultPrice) && defaultPrice > 0 ? defaultPrice : undefined,
    });
  };

  const handleMarkBlockSuccess = async () => {
    if (!selectedAccount?.id || !markBlockRecord?.id) return;
    setOrderActionId(markBlockRecord.id);
    try {
      const values = await markBlockSuccessForm.validateFields();
      await request.post(
        `/api/external-trading-accounts/${selectedAccount.id}/orders/${markBlockRecord.id}/mark-success`,
        { price: values.price }
      );
      message.success('阻断单已标记成功');
      setMarkBlockRecord(null);
      markBlockSuccessForm.resetFields();
      refreshAll();
    } catch (error) {
      if (!error?.errorFields) {
        message.error(error.response?.data?.detail || '阻断单标记成功失败');
      }
    } finally {
      setOrderActionId(null);
    }
  };

  const handleRepairParentFill = async () => {
    if (!selectedAccount?.id || !repairParentRecord?.id) return;
    setOrderActionId(repairParentRecord.id);
    try {
      const values = await repairParentFillForm.validateFields();
      await request.post(
        `/api/external-trading-accounts/${selectedAccount.id}/orders/${repairParentRecord.id}/repair-parent-fill`,
        { price: values.price }
      );
      message.success('父单补成交完成');
      setRepairParentRecord(null);
      repairParentFillForm.resetFields();
      refreshAll();
    } catch (error) {
      if (!error?.errorFields) {
        message.error(error.response?.data?.detail || '父单补成交失败');
      }
    } finally {
      setOrderActionId(null);
    }
  };

  const renderOrderActions = record => {
    const actions = [];
    if (record?.allocation_role === 'BLOCK' && record?.status === 'BLOCKED_INSUFFICIENT_POSITION') {
      actions.push(
        <Button key="mark-success" size="small" type="link" loading={orderActionId === record.id} onClick={() => openMarkBlockSuccess(record)}>
          标记成功
        </Button>
      );
    }
    if (record?.allocation_role === 'PARENT' && record?.needs_fill_repair) {
      actions.push(
        <Button key="repair-parent-fill" size="small" type="link" loading={orderActionId === record.id} onClick={() => openRepairParentFill(record)}>
          补成交
        </Button>
      );
    }
    return actions.length ? <Space size={4}>{actions}</Space> : '-';
  };

  const renderSubStrategy = (_, record) => (
    <Space direction="vertical" size={0}>
      <Text>{record.sub_account_name || record.name || '-'}</Text>
      <Text type="secondary">策略: {record.strategy_name || record.strategy_type || record.binding_label || '-'}</Text>
    </Space>
  );

  const eventRelatedSubAccounts = record => (
    Array.isArray(record?.related_sub_accounts)
      ? record.related_sub_accounts.filter(item => item && (item.name || item.sub_account_name))
      : []
  );

  const eventRelatedNames = record => (
    eventRelatedSubAccounts(record)
      .map(item => item.name || item.sub_account_name)
      .filter(Boolean)
      .join(' / ')
  );

  const eventSubAccountSummary = record => {
    const relatedNames = eventRelatedNames(record);
    if (relatedNames) return relatedNames;
    return record.sub_account_name || record.name || '-';
  };

  const renderEventSubStrategy = (_, record) => {
    const relatedRows = eventRelatedSubAccounts(record);
    if (!relatedRows.length) return <Text>{record.sub_account_name || record.name || '-'}</Text>;

    const relatedNames = eventRelatedNames(record);
    return (
      <Tooltip title={relatedNames}>
        <Text className="executor-event-sub-account-line">{relatedNames}</Text>
      </Tooltip>
    );
  };

  const renderTradeFee = (_, record) => {
    const summary = record?.trade_fee_summary || {};
    const total = record?.cumulative_trade_fee_total ?? summary.effective_fee_total;
    return (
      <Space direction="vertical" size={0}>
        <Text>{formatNumber(total, 2)}</Text>
        <Text type="secondary">真实 {formatNumber(summary.actual_fee_total, 2)} / 估算 {formatNumber(summary.estimated_fee_total, 2)}</Text>
      </Space>
    );
  };

  const subColumns = [
    { title: '子账户', dataIndex: 'name', width: 240, render: renderSubStrategy },
    { title: '分配资金', dataIndex: 'cash_allocated', width: 120, render: value => formatNumber(value, 2) },
    { title: '净资产', dataIndex: 'net_asset', width: 120, render: value => formatNumber(value, 2) },
    { title: '可用资金', dataIndex: 'cash_available', width: 120, render: value => formatNumber(value, 2) },
    { title: '累计交易费', dataIndex: 'cumulative_trade_fee_total', width: 170, render: renderTradeFee },
    { title: '成交数', dataIndex: ['trade_fee_summary', 'fill_count'], width: 90, render: value => formatNumber(value) },
    { title: '执行策略', dataIndex: 'effective_executor_policy', width: 320, render: formatPolicy },
    { title: '启用', dataIndex: 'enabled', width: 90, render: value => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
  ];
  const targetColumns = [
    { title: '子账户', dataIndex: 'sub_account_name', width: 220, render: renderSubStrategy, ...serverFilterProps('target_positions', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('target_positions', 'symbol') },
    { title: '市价', width: 100, render: renderMarketPrice },
    { title: '参考价', dataIndex: 'reference_price', width: 100, render: (value, record) => renderLifecyclePrice(value, record.reference_price_source, 3) },
    { title: '目标', dataIndex: 'target_quantity', width: 100, render: value => formatNumber(value) },
    { title: '账本', dataIndex: 'current_quantity', width: 100, render: value => formatNumber(value) },
    { title: '可卖', dataIndex: 'available_quantity', width: 110, render: value => formatNumber(value) },
    { title: '有效', dataIndex: 'effective_quantity', width: 100, render: value => formatNumber(value) },
    { title: '差额', dataIndex: 'delta_quantity', width: 100, render: value => <Text style={{ color: diffTextColor(value) }}>{formatNumber(value)}</Text> },
    { title: '动作', dataIndex: 'side', width: 80, render: value => value ? <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> : <Tag>HOLD</Tag> },
    { title: '更新时间', dataIndex: 'updated_at', width: 170, render: formatTime },
  ];
  const orderColumns = [
    { title: '创建时间', dataIndex: 'created_at', width: 170, render: formatTime },
    { title: '角色', dataIndex: 'allocation_role', width: 90, ...serverFilterProps('orders', 'role'), render: renderRoleTag },
    { title: '子账户', dataIndex: 'sub_account_name', width: 220, render: renderSubStrategy, ...serverFilterProps('orders', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('orders', 'symbol') },
    { title: '方向', dataIndex: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value || '-'}</Tag> },
    { title: '状态', dataIndex: 'status', width: 140, render: value => <Tag color={orderStatusColor(value)}>{value || '-'}</Tag> },
    { title: '数量', dataIndex: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '已成', dataIndex: 'filled_quantity', width: 100, render: value => formatNumber(value) },
    { title: '未成', dataIndex: 'remaining_quantity', width: 100, render: value => formatNumber(value) },
    { title: '提交价', dataIndex: 'submitted_price', width: 100, render: value => value ? formatNumber(value, 4) : '-' },
    { title: '均价', dataIndex: 'avg_fill_price', width: 100, render: value => value ? formatNumber(value, 4) : '-' },
    { title: '参考价', dataIndex: 'reference_price', width: 100, render: (value, record) => renderLifecyclePrice(value, record.reference_price_source) },
    { title: '保护价', dataIndex: 'protection_limit_price', width: 100, render: (value, record) => renderLifecyclePrice(value, record.protection_limit_source) },
    { title: '档位', dataIndex: 'price_level', width: 100, render: priceLevelLabel },
    { title: '超时点', dataIndex: 'deadline_at', width: 170, render: formatTime },
    {
      title: '消息',
      dataIndex: 'message',
      width: 260,
      render: (_, record) => (
        shouldShowOrderMessage(record)
          ? <span className="executor-order-message">{getOrderMessage(record)}</span>
          : '-'
      ),
    },
    { title: '操作', width: 120, fixed: 'right', render: (_, record) => renderOrderActions(record) },
  ];
  const fillColumns = [
    { title: '成交时间', dataIndex: 'traded_at', width: 170, render: formatTime },
    { title: '角色', dataIndex: 'allocation_role', width: 90, ...serverFilterProps('fills', 'role'), render: renderRoleTag },
    { title: '子账户', dataIndex: 'sub_account_name', width: 220, render: renderSubStrategy, ...serverFilterProps('fills', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('fills', 'symbol') },
    { title: '方向', dataIndex: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value || '-'}</Tag> },
    { title: '数量', dataIndex: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '价格', dataIndex: 'price', width: 100, render: value => formatNumber(value, 4) },
    { title: '金额', dataIndex: 'amount', width: 120, render: value => formatNumber(value, 2) },
    { title: '真实费用', dataIndex: 'actual_fee_total', width: 110, render: value => value === null || value === undefined ? '-' : formatNumber(value, 2) },
  ];
  const deliverColumns = [
    { title: '交割日期', dataIndex: 'trade_date', width: 120, ...serverFilterProps('deliver_records', 'trade_date') },
    { title: '业务', dataIndex: 'business_name', width: 130, render: value => value || '-' },
    { title: '状态', dataIndex: 'status', width: 150, ...serverFilterProps('deliver_records', 'status'), render: value => <Tag color={deliverStatusColor(value)}>{value || '-'}</Tag> },
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('deliver_records', 'symbol') },
    { title: '方向', dataIndex: 'side', width: 80, ...serverFilterProps('deliver_records', 'side'), render: value => value ? <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> : '-' },
    { title: '数量', dataIndex: 'quantity', width: 100, render: value => formatOptionalNumber(value) },
    { title: '余额', dataIndex: 'post_amount', width: 100, render: value => formatOptionalNumber(value) },
    { title: '价格', dataIndex: 'price', width: 100, render: value => formatOptionalNumber(value, 4) },
    { title: '金额', dataIndex: 'amount', width: 120, render: value => formatOptionalNumber(value, 2) },
    { title: '费用', dataIndex: 'total_fee', width: 100, render: value => formatOptionalNumber(value, 2) },
    { title: '业务代码', dataIndex: 'business_flag', width: 100, render: value => value || '-' },
    { title: '业务编号', dataIndex: 'business_no', width: 120, render: value => value || '-' },
    { title: '流水号', dataIndex: 'serial_no', width: 120, render: value => value || '-' },
    { title: '匹配订单', dataIndex: 'matched_order_id', width: 100, render: value => value || '-' },
    { title: '处理时间', dataIndex: 'reconciled_at', width: 170, render: formatTime },
    { title: '消息', dataIndex: 'message', width: 260, render: value => value || '-' },
  ];
  const eventColumns = [
    { title: '事件时间', dataIndex: 'event_time', width: 170, render: formatTime },
    { title: '事件类型', dataIndex: 'event_type', width: 120, ...serverFilterProps('events', 'event_type'), render: value => <Tag color={eventTypeColor(value)}>{eventTypeLabel(value)}</Tag> },
    { title: '处理状态', dataIndex: 'process_status', width: 110, ...serverFilterProps('events', 'process_status'), render: value => <Tag color={processStatusColor(value)}>{value || '-'}</Tag> },
    { title: '子账户', dataIndex: 'sub_account_name', width: 260, render: renderEventSubStrategy, ...serverFilterProps('events', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('events', 'symbol') },
    { title: '方向', dataIndex: 'side', width: 80, render: value => value ? <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> : '-' },
    { title: '数量', dataIndex: 'quantity', width: 100, render: value => formatOptionalNumber(value) },
    { title: '已成', dataIndex: 'filled_quantity', width: 100, render: value => formatOptionalNumber(value) },
    { title: '价格', dataIndex: 'price', width: 100, render: value => formatOptionalNumber(value, 4) },
    { title: '金额', dataIndex: 'amount', width: 120, render: value => formatOptionalNumber(value, 2) },
    { title: 'PTrade状态', dataIndex: 'ptrade_status', width: 120, render: renderPTradeStatus },
    { title: '消息', dataIndex: 'process_message', width: 220, render: value => value || '-' },
  ];
  const demandColumns = [
    { title: '子账户', dataIndex: 'sub_account_name', width: 220, render: renderSubStrategy },
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol },
    { title: '市价', width: 100, render: renderMarketPrice },
    { title: '方向', dataIndex: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '数量', dataIndex: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '状态', dataIndex: 'blocked', width: 110, render: (_, record) => record.blocked ? <Tag color="orange">{getBlockLabel(record)}</Tag> : <Tag color="green">可执行</Tag> },
    { title: '执行策略', dataIndex: 'execution_policy', width: 300, render: formatPolicy },
  ];
  const internalCrossColumns = [
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol },
    { title: '市价', width: 100, render: renderMarketPrice },
    { title: '撮合数量', dataIndex: 'quantity', width: 110, render: value => formatNumber(value) },
    { title: '参考价', dataIndex: 'price', width: 100, render: value => value ? formatNumber(value, 4) : '-' },
    { title: '买方分配', dataIndex: 'buy_allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' },
    { title: '卖方分配', dataIndex: 'sell_allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' },
  ];
  const externalOrderColumns = [
    { title: '标的', dataIndex: 'symbol', width: 150, render: renderSymbol },
    { title: '市价', width: 100, render: renderMarketPrice },
    { title: '方向', dataIndex: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '数量', dataIndex: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '限价规则', dataIndex: 'price_level', width: 110, render: priceLevelLabel },
    { title: '执行策略', dataIndex: 'execution_policy', width: 310, render: formatPolicy },
    { title: '分配', dataIndex: 'allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' },
  ];

  const renderMetricCards = () => {
    const summary = status?.summary || {};
    const metrics = [
      { label: '子账户', value: status?.summary?.sub_account_count ?? subRows.length },
      { label: '目标仓位', value: status?.summary?.target_position_count ?? 0 },
      { label: '待执行差额', value: status?.summary?.pending_delta_count ?? 0, tone: 'warning' },
      { label: '活跃订单', value: status?.summary?.active_order_count ?? 0, tone: 'danger' },
      { label: '成交回报', value: status?.summary?.fill_count ?? 0 },
      { label: '交割单', value: status?.summary?.deliver_record_count ?? 0 },
      { label: '总净资产', value: formatNumber(totals.netAsset, 2) },
      { label: '可用资金', value: formatNumber(totals.cashAvailable, 2) },
      { label: '交易费', value: formatNumber(summary.trade_fee_total, 2) },
      { label: '归因', value: formatNumber(summary.attributed_trade_fee_total, 2) },
      { label: '非交易费', value: formatNumber(summary.non_trade_fee_total, 2) },
      { label: '总费用', value: formatNumber(summary.total_fee, 2) },
    ];
    return (
      <div className="executor-metrics">
        {metrics.map(item => (
          <div className={`executor-metric executor-metric--${item.tone || 'normal'}`} key={item.label}>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </div>
        ))}
      </div>
    );
  };

  const renderSimpleCard = (record, kind) => {
    const title = kind === 'sub'
      ? record.name
      : kind === 'deliver'
        ? (record.business_name || renderSymbolText(record))
      : renderSymbolText(record);
    const subtitle = kind === 'sub'
      ? (record.strategy_name || record.binding_label || '虚拟子账户')
      : kind === 'deliver'
        ? `${record.trade_date || '-'} / ${renderSymbolText(record)}`
      : (record.sub_account_name || record.name || '-');
    return (
      <div className="executor-row-card" key={record.id || `${record.sub_account_id}-${record.symbol}-${record.created_at || record.updated_at}`}>
        <div className="executor-row-card__header">
          <div>
            <Text strong>{title}</Text>
            <Text type="secondary">{subtitle}</Text>
          </div>
          {kind === 'order' ? <Tag color={orderStatusColor(record.status)}>{record.status || '-'}</Tag> : null}
          {kind === 'sub' ? <Tag color={record.enabled ? 'green' : 'default'}>{record.enabled ? '启用' : '停用'}</Tag> : null}
          {kind === 'target' && record.side ? <Tag color={record.side === 'BUY' ? 'red' : 'green'}>{record.side}</Tag> : null}
          {kind === 'deliver' ? <Tag color={deliverStatusColor(record.status)}>{record.status || '-'}</Tag> : null}
        </div>
        <div className="executor-row-card__metrics">
          {kind === 'sub' && (
            <>
              <div><span>净资产</span><strong>{formatNumber(record.net_asset, 2)}</strong></div>
              <div><span>可用资金</span><strong>{formatNumber(record.cash_available, 2)}</strong></div>
              <div><span>分配资金</span><strong>{formatNumber(record.cash_allocated, 2)}</strong></div>
              <div><span>交易费</span><strong>{formatNumber(record.cumulative_trade_fee_total ?? record.trade_fee_summary?.effective_fee_total, 2)}</strong></div>
            </>
          )}
          {kind === 'target' && (
            <>
              <div><span>目标</span><strong>{formatNumber(record.target_quantity)}</strong></div>
              <div><span>账本</span><strong>{formatNumber(record.current_quantity)}</strong></div>
              <div><span>差额</span><strong style={{ color: diffTextColor(record.delta_quantity) }}>{formatNumber(record.delta_quantity)}</strong></div>
              <div><span>可卖</span><strong>{formatNumber(record.available_quantity)}</strong></div>
            </>
          )}
          {kind === 'ledger' && (
            <>
              <div><span>数量</span><strong>{formatNumber(record.quantity)}</strong></div>
              <div><span>可卖</span><strong>{formatNumber(record.available_quantity)}</strong></div>
              <div><span>成本</span><strong>{formatNumber(record.avg_cost, 4)}</strong></div>
              <div><span>市值</span><strong>{formatNumber(record.market_value, 2)}</strong></div>
            </>
          )}
          {kind === 'order' && (
            <>
              <div><span>方向</span><strong>{record.side || '-'}</strong></div>
              <div><span>数量</span><strong>{formatNumber(record.quantity)}</strong></div>
              <div><span>已成</span><strong>{formatNumber(record.filled_quantity)}</strong></div>
              <div><span>未成</span><strong>{formatNumber(record.remaining_quantity)}</strong></div>
            </>
          )}
          {kind === 'fill' && (
            <>
              <div><span>方向</span><strong>{record.side || '-'}</strong></div>
              <div><span>数量</span><strong>{formatNumber(record.quantity)}</strong></div>
              <div><span>价格</span><strong>{formatNumber(record.price, 4)}</strong></div>
              <div><span>金额</span><strong>{formatNumber(record.amount, 2)}</strong></div>
            </>
          )}
          {kind === 'deliver' && (
            <>
              <div><span>业务</span><strong>{record.business_name || '-'}</strong></div>
              <div><span>数量</span><strong>{formatOptionalNumber(record.quantity)}</strong></div>
              <div><span>余额</span><strong>{formatOptionalNumber(record.post_amount)}</strong></div>
              <div><span>金额</span><strong>{formatOptionalNumber(record.amount, 2)}</strong></div>
            </>
          )}
        </div>
        {kind === 'order' ? (
          <div className="executor-row-card__details">
            {renderRoleTag(record.allocation_role)}
            <span>档位 {priceLevelLabel(record.price_level)}</span>
            <span>提交价 {record.submitted_price ? formatNumber(record.submitted_price, 4) : '-'}</span>
            <span>超时 {formatTime(record.deadline_at)}</span>
          </div>
        ) : null}
        {kind === 'order' && shouldShowOrderMessage(record) ? (
          <p className="executor-order-message">{getOrderMessage(record)}</p>
        ) : null}
        {kind === 'deliver' && record.message ? (
          <p className="executor-order-message">{record.message}</p>
        ) : null}
        {kind === 'order' && renderOrderActions(record) !== '-' ? (
          <div className="executor-row-card__actions">{renderOrderActions(record)}</div>
        ) : null}
      </div>
    );
  };

  const renderMobileLoadState = tableKey => {
    if (!tableKey) return null;
    const table = tables[tableKey] || createEmptyTable();
    const loaded = (table.rows || []).length;
    const total = Number(table.pagination?.total || 0);
    if (tableLoading[tableKey]) {
      return (
        <div className="executor-mobile-load-state">
          <Spin size="small" />
          <span>{loaded ? '加载下一页' : '加载中'}</span>
        </div>
      );
    }
    if (loaded && total && loaded >= total) {
      return <div className="executor-mobile-load-state">已加载全部 {loaded} 条</div>;
    }
    if (loaded && total && hasMoreTableRows(tableKey)) {
      return <div className="executor-mobile-load-state">已加载 {loaded}/{total}</div>;
    }
    return null;
  };

  const renderEventCards = tableKey => (
    <div className="executor-mobile-list">
      {eventRows.length ? eventRows.map(row => (
        <div className="executor-row-card" key={row.id}>
          <div className="executor-row-card__header">
            <div>
              <Text strong>{eventTypeLabel(row.event_type)}</Text>
              <Text type="secondary">{formatTime(row.event_time)}</Text>
            </div>
            <Tag color={processStatusColor(row.process_status)}>{row.process_status || '-'}</Tag>
          </div>
          <div className="executor-row-card__details">
            <span>{eventSubAccountSummary(row)}</span>
            <span>{renderSymbolText(row)}</span>
            <span>数量 {formatOptionalNumber(row.quantity)}</span>
            <span>已成 {formatOptionalNumber(row.filled_quantity)}</span>
            <span>{renderPTradeStatus(row.ptrade_status)}</span>
          </div>
          {row.process_message ? <p>{row.process_message}</p> : null}
        </div>
      )) : (
        tableLoading[tableKey]
          ? <div className="executor-mobile-load-state"><Spin size="small" /><span>加载中</span></div>
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
      )}
      {eventRows.length ? renderMobileLoadState(tableKey) : null}
    </div>
  );

  const renderMobileCards = (rows, kind, tableKey) => (
    <div className="executor-mobile-list">
      {rows.length ? rows.map(row => renderSimpleCard(row, kind)) : (
        tableKey && tableLoading[tableKey]
          ? <div className="executor-mobile-load-state"><Spin size="small" /><span>加载中</span></div>
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
      )}
      {rows.length ? renderMobileLoadState(tableKey) : null}
    </div>
  );

  const renderTargetToolbar = () => {
    const nonEmptyOnly = tableState.target_positions?.nonEmptyOnly || false;
    const deltaOnly = tableState.target_positions?.deltaOnly || false;
    let filterValue = 'all';
    if (nonEmptyOnly) filterValue = 'non_empty';
    if (deltaOnly) filterValue = 'delta';
    const pagination = tables.target_positions?.pagination || {};
    const loaded = targetRows.length;
    const total = Number(pagination.total || 0);
    return (
      <div className="executor-list-toolbar" ref={targetToolbarRef}>
        <Segmented
          size="small"
          value={filterValue}
          options={[
            { label: '全部', value: 'all' },
            { label: '只看非空', value: 'non_empty' },
            { label: '只看差额', value: 'delta' },
          ]}
          onChange={handleTargetPositionFilterChange}
        />
        <span>{tableLoading.target_positions ? '更新中' : (total ? `已显示 ${loaded}/${total}` : `已显示 ${loaded}`)}</span>
      </div>
    );
  };

  const renderOrderToolbar = () => {
    const activeOnly = tableState.orders?.activeOnly || false;
    const unfilledOnly = tableState.orders?.unfilledOnly || false;
    let filterValue = 'all';
    if (activeOnly) filterValue = 'active';
    if (unfilledOnly) filterValue = 'unfilled';
    const pagination = tables.orders?.pagination || {};
    const loaded = orderRows.length;
    const total = Number(pagination.total || 0);
    return (
      <div className="executor-list-toolbar" ref={orderToolbarRef}>
        <Segmented
          size="small"
          value={filterValue}
          options={[
            { label: '全部', value: 'all' },
            { label: '只看活跃', value: 'active' },
            { label: '只看未成交', value: 'unfilled' },
          ]}
          onChange={handleOrderLifecycleFilterChange}
        />
        <span>{tableLoading.orders ? '更新中' : (total ? `已显示 ${loaded}/${total}` : `已显示 ${loaded}`)}</span>
      </div>
    );
  };

  const renderCurrentTab = () => {
    if (activeTab === 'sub_accounts') {
      return (
        <>
          {renderMobileCards(subRows, 'sub')}
          <div className="executor-desktop-table">
            <Table rowKey="id" columns={subColumns} dataSource={subRows} loading={subAccountLoading} pagination={false} size="small" scroll={{ x: 1500 }} />
          </div>
        </>
      );
    }
    if (activeTab === 'targets') {
      return (
        <>
          {renderTargetToolbar()}
          {renderMobileCards(targetRows, 'target', 'target_positions')}
          <div className="executor-desktop-table">
            <Table rowKey={record => `${record.sub_account_id}-${record.symbol}`} columns={targetColumns} dataSource={targetRows} loading={tableLoading.target_positions} pagination={paginationFor('target_positions')} onChange={handleTableChange('target_positions')} size="small" scroll={{ x: 1280 }} />
          </div>
        </>
      );
    }
    if (activeTab === 'ledger') {
      return (
        <>
          {renderMobileCards(ledgerRows, 'ledger', 'ledger_positions')}
          <div className="executor-desktop-table">
            <ExternalLedgerPositionsTable
              pagination={paginationFor('ledger_positions')}
              accountId={selectedAccount?.id}
              subAccountId={null}
              enabled={Boolean(selectedAccount?.id)}
              reloadToken={ledgerReloadToken}
              onDataChange={handleLedgerPositionsDataChange}
              onLoadingChange={handleLedgerPositionsLoadingChange}
              showSubAccount
              marketType={selectedAccount?.market_type}
              scroll={{}}
            />
          </div>
        </>
      );
    }
    if (activeTab === 'orders') {
      return (
        <>
          {renderOrderToolbar()}
          {renderMobileCards(orderRows, 'order', 'orders')}
          <div className="executor-desktop-table">
            <Table rowKey="id" columns={orderColumns} dataSource={orderRows} loading={tableLoading.orders} pagination={paginationFor('orders')} onChange={handleTableChange('orders')} size="small" scroll={{ x: 2100 }} />
          </div>
        </>
      );
    }
    if (activeTab === 'fills') {
      return (
        <>
          {renderMobileCards(fillRows, 'fill', 'fills')}
          <div className="executor-desktop-table">
            <Table rowKey="id" columns={fillColumns} dataSource={fillRows} loading={tableLoading.fills} pagination={paginationFor('fills')} onChange={handleTableChange('fills')} size="small" scroll={{ x: 1200 }} />
          </div>
        </>
      );
    }
    if (activeTab === 'deliver_records') {
      return (
        <>
          {renderMobileCards(deliverRows, 'deliver', 'deliver_records')}
          <div className="executor-desktop-table">
            <Table
              rowKey="id"
              columns={deliverColumns}
              dataSource={deliverRows}
              loading={tableLoading.deliver_records}
              pagination={paginationFor('deliver_records')}
              onChange={handleTableChange('deliver_records')}
              expandable={{
                expandedRowRender: record => (
                  <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {JSON.stringify(record.raw_record || {}, null, 2)}
                  </pre>
                ),
                rowExpandable: record => !!record.raw_record,
              }}
              size="small"
              scroll={{ x: 2300 }}
            />
          </div>
        </>
      );
    }
    if (activeTab === 'events') {
      return (
        <>
          {renderEventCards('events')}
          <div className="executor-desktop-table">
            <Table rowKey="id" columns={eventColumns} dataSource={eventRows} loading={tableLoading.events} pagination={paginationFor('events')} onChange={handleTableChange('events')} size="small" scroll={{ x: 1600 }} />
          </div>
        </>
      );
    }
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <Table title={() => '子账户目标差额'} rowKey={(record, index) => `${record.sub_account_id}-${record.symbol}-${record.side}-${index}`} columns={demandColumns} dataSource={demandRows} loading={planLoading} pagination={false} size="small" scroll={{ x: 1100 }} />
        <Table title={() => '内部撮合'} rowKey={(record, index) => `${record.symbol}-${index}`} columns={internalCrossColumns} dataSource={internalCrossRows} loading={planLoading} pagination={false} size="small" scroll={{ x: 980 }} />
        <Table title={() => '提交到 PTrade 的净额限价单'} rowKey={(record, index) => `${record.symbol}-${record.side}-${index}`} columns={externalOrderColumns} dataSource={externalOrderRows} loading={planLoading} pagination={false} size="small" scroll={{ x: 1080 }} />
      </Space>
    );
  };

  const accountOptions = accounts.map(account => ({
    label: `${account.name || account.identifier || account.id}${account.enabled === false ? '（停用）' : ''}`,
    value: account.id,
  }));

  return (
    <PageShell
      className="executor-page"
      title={embedded ? null : '订单执行器'}
      subtitle="净额撮合、订单生命周期、成交回报和事件流水"
    >
      <PageSection className="executor-control-section">
        <div className="executor-account-banner">
          <div className="executor-account-main">
            <div className="executor-account-heading">
              <Select
                className="executor-account-title-select"
                value={selectedAccountId}
                options={accountOptions}
                loading={accountsLoading}
                placeholder="选择外部交易账户"
                onChange={handleAccountChange}
              />
              <Space className="executor-account-tags" wrap size={6}>
                <Tag color={selectedAccount?.connected ? 'green' : 'default'}>{selectedAccount?.connected ? '在线' : '离线'}</Tag>
                {selectedAccount ? <Tag color={marketTypeColor(selectedAccount.market_type)}>{marketTypeLabel(selectedAccount.market_type)}</Tag> : null}
                {selectedAccount?.enabled === false ? <Tag>停用</Tag> : <Tag color="green">启用</Tag>}
              </Space>
            </div>
          </div>
          <div className="executor-actions">
            <Button icon={<ReloadOutlined />} onClick={refreshAll} loading={statusLoading || subAccountLoading || planLoading} disabled={!selectedAccount}>
              刷新
            </Button>
            <Button type="primary" danger icon={<ThunderboltOutlined />} onClick={() => executeNetted()} loading={executeLoading} disabled={!selectedAccount || statusLoading}>
              执行净额限价单
            </Button>
          </div>
        </div>
        {status?.plan_error || plan?.plan_error ? (
          <Alert className="executor-alert" type="warning" showIcon message="净额预览生成失败" description={plan?.plan_error || status?.plan_error} />
        ) : null}
        {renderMetricCards()}
      </PageSection>

      <PageSection className="executor-status-section">
        <div className="executor-tab-grid">
          {EXECUTOR_TABS.map(item => (
            <button
              type="button"
              key={item.key}
              className={`executor-tab${activeTab === item.key ? ' is-active' : ''}`}
              onClick={() => handleTabChange(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <Spin spinning={statusLoading && !status}>
          {selectedAccount ? renderCurrentTab() : <Empty description="请选择外部交易账户" />}
        </Spin>
      </PageSection>

      <Modal
        className="executor-action-modal"
        title="标记阻断单成功"
        open={!!markBlockRecord}
        onCancel={() => setMarkBlockRecord(null)}
        onOk={handleMarkBlockSuccess}
        okText="标记成功"
        cancelText="取消"
        confirmLoading={orderActionId === markBlockRecord?.id}
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Text>系统会按你输入的成交价写入一笔人工成交，并按成功成交回写账本；此操作当前不支持自动撤销。</Text>
          <Space wrap>
            <Tag>{markBlockRecord?.sub_account_name || '-'}</Tag>
            <Tag>{markBlockRecord?.symbol_name || markBlockRecord?.symbol || '-'}</Tag>
            <Tag color={markBlockRecord?.side === 'SELL' ? 'green' : 'red'}>{markBlockRecord?.side || '-'}</Tag>
            <Tag>数量 {formatNumber(markBlockRecord?.remaining_quantity || markBlockRecord?.quantity)}</Tag>
          </Space>
          <Form form={markBlockSuccessForm} layout="vertical">
            <Form.Item name="price" label="成交价" rules={[{ required: true, message: '请输入成交价' }, { type: 'number', min: 0.0001, message: '成交价必须大于 0' }]}>
              <InputNumber min={0.0001} precision={4} step={0.01} style={{ width: '100%' }} placeholder="请输入人工成交价" />
            </Form.Item>
          </Form>
        </Space>
      </Modal>

      <Modal
        className="executor-action-modal"
        title="补父单成交"
        open={!!repairParentRecord}
        onCancel={() => setRepairParentRecord(null)}
        onOk={handleRepairParentFill}
        okText="补成交"
        cancelText="取消"
        confirmLoading={orderActionId === repairParentRecord?.id}
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Text>系统会按你输入的成交价补写父单成交，并把成交数量分配到子单和子账户账本；此操作当前不支持自动撤销。</Text>
          <Space wrap>
            <Tag>{repairParentRecord?.symbol_name || repairParentRecord?.symbol || '-'}</Tag>
            <Tag color={repairParentRecord?.side === 'SELL' ? 'green' : 'red'}>{repairParentRecord?.side || '-'}</Tag>
            <Tag>父单数量 {formatNumber(repairParentRecord?.quantity)}</Tag>
            <Tag>待分配 {formatNumber(repairParentRecord?.child_remaining_quantity)}</Tag>
          </Space>
          <Form form={repairParentFillForm} layout="vertical">
            <Form.Item name="price" label="成交价" rules={[{ required: true, message: '请输入成交价' }, { type: 'number', min: 0.0001, message: '成交价必须大于 0' }]}>
              <InputNumber min={0.0001} precision={4} step={0.01} style={{ width: '100%' }} placeholder="请输入实际成交价" />
            </Form.Item>
          </Form>
        </Space>
      </Modal>
    </PageShell>
  );
};

export default ExecutorStatusPage;
