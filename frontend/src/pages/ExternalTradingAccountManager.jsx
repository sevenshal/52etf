import React, { useCallback, useEffect, useRef, useState } from 'react';
import dayjs from 'dayjs';
import {
  Alert,
  Badge,
  Button,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message
} from 'antd';
import {
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  InfoCircleOutlined,
  LineChartOutlined,
  PlusOutlined,
  RightOutlined,
  SyncOutlined
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import { useAccount } from '../contexts/AccountContext';
import { PageSection, PageShell } from '../components/PageScaffold';
import './ExternalTradingAccountManager.css';

const { Text } = Typography;
const MARKET_TYPE_A_STOCK = 'A_STOCK';
const MARKET_TYPE_US_STOCK = 'US_STOCK';
const MARKET_TYPE_OPTIONS = [
  { label: 'A股', value: MARKET_TYPE_A_STOCK },
  { label: '美股', value: MARKET_TYPE_US_STOCK }
];
const marketTypeLabel = value => (value === MARKET_TYPE_US_STOCK ? '美股' : 'A股');
const marketTypeColor = value => (value === MARKET_TYPE_US_STOCK ? 'blue' : 'red');
const marketDefaultFields = value => (
  value === MARKET_TYPE_US_STOCK
    ? { executor_lot_size: 1, stamp_tax_rate_pct: 0 }
    : { executor_lot_size: 100, stamp_tax_rate_pct: 0.05 }
);
const buildExternalTradingStatusWsUrl = accountId => {
  const apiUrl = process.env.REACT_APP_API_URL || '';
  const wsHost = apiUrl
    ? apiUrl.replace(/^http/, 'ws')
    : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;
  return `${wsHost}/api/external-trading-accounts/status/ws?account_id=${encodeURIComponent(accountId)}`;
};

const formatTime = value => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-');
const formatNumber = (value, digits = 0) => {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return '-';
  return num.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
};
const sumNumberField = (rows, field) => (rows || []).reduce((total, row) => {
  const value = Number(row?.[field] || 0);
  return Number.isFinite(value) ? total + value : total;
}, 0);
const roleLabel = value => {
  if (value === 'PARENT') return '父单';
  if (value === 'CHILD') return '子单';
  if (value === 'BLOCK') return '阻断';
  return value || '-';
};
const roleFilterOptions = [
  { text: '父单', value: 'PARENT' },
  { text: '子单', value: 'CHILD' },
  { text: '阻断', value: 'BLOCK' }
];
const executorServerTableEndpoints = {
  target_positions: 'target-positions',
  ledger_positions: 'ledger-positions',
  orders: 'orders',
  fills: 'fills',
  events: 'events'
};
const executorServerTableLabels = {
  target_positions: '目标仓位',
  ledger_positions: '账本持仓',
  orders: '订单生命周期',
  fills: '成交回报',
  events: '事件流水'
};
const executorServerTableKeys = Object.keys(executorServerTableEndpoints);
const createDefaultExecutorTableState = () => ({
  target_positions: { page: 1, pageSize: 10, filters: {} },
  ledger_positions: { page: 1, pageSize: 10, filters: {} },
  orders: { page: 1, pageSize: 10, filters: {} },
  fills: { page: 1, pageSize: 10, filters: {} },
  events: { page: 1, pageSize: 10, filters: {} }
});
const createEmptyExecutorTableData = () => ({
  rows: [],
  pagination: { page: 1, page_size: 10, total: 0 },
  price_details: {},
  filter_options: {}
});
const createEmptyExecutorTables = () => executorServerTableKeys.reduce(
  (result, key) => ({ ...result, [key]: createEmptyExecutorTableData() }),
  {}
);
const DEFAULT_EXECUTOR_SEQUENCE = [1, 2, 3, 5, -1];
const DEFAULT_TIMEOUT_SEQUENCE = [120, 120, 120, 120, 120];
const MAX_TIMEOUT_SECONDS = 86400;
const priceLevelTooltip = (
  <Space direction="vertical" size={4} style={{ color: '#fff', maxWidth: 420 }}>
    <span>0：参考价保护，按 reference_price 作为保护限价，不额外放大滑点。</span>
    <span>1：最多用到一档，买看卖一，卖看买一。</span>
    <span>2/3/4/5：最多用到二/三/四/五档；累计量够时按实际覆盖档位提交。</span>
    <span>除 0 外，所有档位实际下单都会受 reference_price 加最大滑点形成的保护价约束。</span>
    <span>-1：本方最优价，买单按买一档，卖单按卖一档；盘口缺失时尝试涨跌停价。</span>
    <span style={{ color: 'rgba(255,255,255,0.72)' }}>序列第一个档位用于首次提交，后续重定价按序列向后推进。最终仍可能触发 PTrade 的涨跌停价兜底。</span>
  </Space>
);
const priceLevelLabel = value => {
  if (value === -1) return '本方最优价';
  if (value === 0) return '参考价限价';
  return `${value}档`;
};
const sequenceToText = value => (Array.isArray(value) && value.length ? value : DEFAULT_EXECUTOR_SEQUENCE).join(',');
const timeoutSequenceToText = value => (Array.isArray(value) && value.length ? value : DEFAULT_TIMEOUT_SEQUENCE).join(',');
const parseSequence = (value, fallback = DEFAULT_EXECUTOR_SEQUENCE) => {
  if (Array.isArray(value)) return value;
  const text = String(value || '').trim();
  if (!text) return fallback;
  const parsed = text.split(',').map(item => Number(item.trim())).filter(item => [-1, 0, 1, 2, 3, 4, 5].includes(item));
  return parsed.length ? parsed : fallback;
};
const parseTimeoutSequence = (value, fallback = DEFAULT_TIMEOUT_SEQUENCE) => {
  if (Array.isArray(value)) return value.map(item => Number(item)).filter(item => Number.isFinite(item) && item >= 10 && item <= MAX_TIMEOUT_SECONDS);
  const text = String(value || '').trim();
  if (!text) return fallback;
  const parsed = text
    .split(',')
    .map(item => Number(item.trim()))
    .filter(item => Number.isFinite(item) && item >= 10 && item <= MAX_TIMEOUT_SECONDS)
    .map(item => Math.round(item));
  return parsed.length ? parsed : fallback;
};
const validateSequenceLengths = (priceSequence, timeoutSequence, maxReplaceCount) => {
  const maxReplace = Number(maxReplaceCount ?? 0);
  if (!Number.isFinite(maxReplace) || maxReplace < 0) return '最大重定价次数无效';
  const requiredLength = Math.floor(maxReplace) + 1;
  if ((priceSequence || []).length < requiredLength) {
    return `重定价档位序列至少需要 ${requiredLength} 项，才能覆盖首次提交和 ${Math.floor(maxReplace)} 次重定价`;
  }
  if ((timeoutSequence || []).length < requiredLength) {
    return `订单超时秒数序列至少需要 ${requiredLength} 项，才能覆盖首次提交和 ${Math.floor(maxReplace)} 次重定价`;
  }
  return null;
};
const getBlockLabel = record => {
  if (record?.blocked_status === 'BLOCKED_NON_RETRYABLE_REJECTION') return '规则阻断';
  if (record?.status === 'BLOCKED_NON_RETRYABLE_REJECTION') return '规则阻断';
  if (record?.blocked_status === 'BLOCKED_INSUFFICIENT_POSITION') return '持仓不足';
  if (record?.status === 'BLOCKED_INSUFFICIENT_POSITION') return '持仓不足';
  return '可卖阻断';
};
const normalizeFilterText = value => {
  if (value === undefined || value === null || value === '') return '-';
  return String(value);
};
const filterOptionsFromRows = (rows, getter) => {
  const values = Array.from(new Set((rows || []).map(getter).map(normalizeFilterText).filter(value => value !== '-')));
  return values.sort((a, b) => a.localeCompare(b, 'zh-CN')).map(value => ({ text: value, value }));
};
const textColumnFilter = (rows, getter) => ({
  filters: filterOptionsFromRows(rows, getter),
  filterSearch: true,
  onFilter: (value, record) => normalizeFilterText(getter(record)) === String(value)
});
const normalizeServerTableFilters = filters => Object.entries(filters || {}).reduce((result, [key, value]) => {
  const list = Array.isArray(value) ? value.filter(item => item !== null && item !== undefined && item !== '') : [];
  if (list.length) {
    result[key] = list.map(item => String(item));
  }
  return result;
}, {});
const joinFilterValues = values => (Array.isArray(values) && values.length ? values.join(',') : undefined);
const symbolText = record => {
  const symbol = normalizeFilterText(record?.symbol);
  const name = record?.symbol_name ? String(record.symbol_name) : '';
  return name ? `${name} ${symbol}` : symbol;
};
const normalizeSymbolKey = value => String(value || '').trim().toUpperCase();
const renderSymbol = (_, record) => {
  const symbol = normalizeFilterText(record?.symbol);
  const name = record?.symbol_name;
  if (!name) return symbol;
  return (
    <Space direction="vertical" size={0}>
      <Text strong>{name}</Text>
      <Text type="secondary">{symbol}</Text>
    </Space>
  );
};
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
        : DEFAULT_TIMEOUT_SEQUENCE)
  );
  return `档位序列${sequence} / 超时序列${timeoutSequence}s / 重定价${maxReplace ?? '-'}次 / 滑点${maxSlippage ?? '-'}%`;
};
const renderTradeFeeSummary = (_, record) => {
  const summary = record?.trade_fee_summary || {};
  const total = record?.cumulative_trade_fee_total ?? summary.effective_fee_total;
  return (
    <Space direction="vertical" size={0}>
      <Text>{formatNumber(total, 2)}</Text>
      <Text type="secondary">
        真实 {formatNumber(summary.actual_fee_total, 2)} / 估算 {formatNumber(summary.estimated_fee_total, 2)}
      </Text>
    </Space>
  );
};
const diffTextColor = value => {
  const num = Number(value || 0);
  if (!Number.isFinite(num) || num === 0) return undefined;
  return num > 0 ? '#cf1322' : '#389e0d';
};
const renderDiffValue = value => (
  <Text style={{ color: diffTextColor(value) }}>
    {formatNumber(value)}
  </Text>
);
const renderSellability = (value, record) => (
  <Space direction="vertical" size={0}>
    <Text>{formatNumber(value)}</Text>
    {record?.raw_available_quantity !== undefined ? (
      <Text type="secondary">原始 {formatNumber(record.raw_available_quantity)}</Text>
    ) : null}
  </Space>
);
const renderTargetSellability = (_, record) => (
  <Space direction="vertical" size={0}>
    <Text>{formatNumber(record?.available_quantity)}</Text>
    <Text type="secondary">
      原始 {formatNumber(record?.raw_available_quantity)} / T+1锁定 {formatNumber(record?.t1_locked_quantity)} / 当日买入 {formatNumber(record?.today_buy_quantity)}
    </Text>
  </Space>
);
const getNetAssetHistoryOption = rows => {
  const dates = (rows || []).map(item => item.trading_date);
  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: value => Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
    },
    legend: {
      top: 0,
      data: ['净资产', '持仓市值', '可用资金']
    },
    grid: {
      left: 56,
      right: 24,
      top: 48,
      bottom: 56
    },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: {
        formatter: value => Number(value || 0).toLocaleString()
      }
    },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 22, bottom: 16 }
    ],
    series: [
      {
        name: '净资产',
        type: 'line',
        smooth: true,
        showSymbol: false,
        connectNulls: false,
        data: (rows || []).map(item => item.status === 'SUCCESS' ? Number(item.net_asset || 0) : null)
      },
      {
        name: '持仓市值',
        type: 'line',
        smooth: true,
        showSymbol: false,
        connectNulls: false,
        data: (rows || []).map(item => item.status === 'SUCCESS' ? Number(item.position_market_value || 0) : null)
      },
      {
        name: '可用资金',
        type: 'line',
        smooth: true,
        showSymbol: false,
        connectNulls: false,
        data: (rows || []).map(item => item.status === 'SUCCESS' ? Number(item.cash_available || 0) : null)
      }
    ]
  };
};

const ExternalTradingAccountManager = () => {
  const { accountId } = useAccount();
  const navigate = useNavigate();
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [editingAccount, setEditingAccount] = useState(null);
  const [subAccounts, setSubAccounts] = useState({});
  const [expandedMobileAccountIds, setExpandedMobileAccountIds] = useState([]);
  const [subModalVisible, setSubModalVisible] = useState(false);
  const [editingSubAccount, setEditingSubAccount] = useState(null);
  const [activeAccountForSub, setActiveAccountForSub] = useState(null);
  const [executorStatusVisible, setExecutorStatusVisible] = useState(false);
  const [executorStatusAccount, setExecutorStatusAccount] = useState(null);
  const [executorStatus, setExecutorStatus] = useState(null);
  const [executorStatusLoading, setExecutorStatusLoading] = useState(false);
  const [executorStatusTables, setExecutorStatusTables] = useState(createEmptyExecutorTables);
  const [executorStatusTableState, setExecutorStatusTableState] = useState(createDefaultExecutorTableState);
  const [executorStatusTableLoading, setExecutorStatusTableLoading] = useState({});
  const [executorStatusTableLoaded, setExecutorStatusTableLoaded] = useState({});
  const [executorStatusActiveTab, setExecutorStatusActiveTab] = useState('sub_accounts');
  const [executorSubAccountStatus, setExecutorSubAccountStatus] = useState({ rows: [] });
  const [executorSubAccountStatusLoading, setExecutorSubAccountStatusLoading] = useState(false);
  const [executorSubAccountStatusLoaded, setExecutorSubAccountStatusLoaded] = useState(false);
  const [executorPlan, setExecutorPlan] = useState(null);
  const [executorPlanLoading, setExecutorPlanLoading] = useState(false);
  const [executorPlanLoaded, setExecutorPlanLoaded] = useState(false);
  const [executorExecuteLoading, setExecutorExecuteLoading] = useState(false);
  const [brokerPositionsVisible, setBrokerPositionsVisible] = useState(false);
  const [brokerPositionsAccount, setBrokerPositionsAccount] = useState(null);
  const [brokerPositions, setBrokerPositions] = useState(null);
  const [brokerPositionsLoading, setBrokerPositionsLoading] = useState(false);
  const [markBlockSuccessOrderId, setMarkBlockSuccessOrderId] = useState(null);
  const [markBlockSuccessModalVisible, setMarkBlockSuccessModalVisible] = useState(false);
  const [markBlockSuccessRecord, setMarkBlockSuccessRecord] = useState(null);
  const [repairParentFillOrderId, setRepairParentFillOrderId] = useState(null);
  const [repairParentFillModalVisible, setRepairParentFillModalVisible] = useState(false);
  const [repairParentFillRecord, setRepairParentFillRecord] = useState(null);
  const [netAssetHistoryVisible, setNetAssetHistoryVisible] = useState(false);
  const [netAssetHistoryLoading, setNetAssetHistoryLoading] = useState(false);
  const [netAssetHistoryAccount, setNetAssetHistoryAccount] = useState(null);
  const [netAssetHistorySubAccount, setNetAssetHistorySubAccount] = useState(null);
  const [netAssetHistory, setNetAssetHistory] = useState(null);
  const [form] = Form.useForm();
  const [subForm] = Form.useForm();
  const [markBlockSuccessForm] = Form.useForm();
  const [repairParentFillForm] = Form.useForm();
  const statusWsRef = useRef(null);
  const statusWsReconnectTimerRef = useRef(null);

  const fetchAccounts = useCallback(async (silent = false) => {
    if (!accountId) {
      if (!silent) {
        setLoading(false);
      }
      return;
    }
    if (!silent) {
      setLoading(true);
    }
    try {
      const { data } = await request.get('/api/external-trading-accounts');
      setAccounts(data || []);
    } catch (error) {
      if (!silent) {
        message.error('获取外部交易账号失败');
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [accountId]);

  useEffect(() => {
    if (!accountId) {
      return undefined;
    }
    fetchAccounts();
  }, [accountId, fetchAccounts]);

  useEffect(() => {
    if (!accountId) {
      return undefined;
    }

    let stopped = false;
    const connectStatusWs = () => {
      const ws = new WebSocket(buildExternalTradingStatusWsUrl(accountId));
      statusWsRef.current = ws;

      ws.onmessage = event => {
        try {
          const payload = JSON.parse(event.data);
          if (Array.isArray(payload?.accounts)) {
            setAccounts(payload.accounts);
          }
        } catch (error) {
          console.warn('解析外部交易账号状态推送失败', error);
        }
      };

      ws.onclose = () => {
        if (statusWsRef.current === ws) {
          statusWsRef.current = null;
        }
        if (!stopped) {
          statusWsReconnectTimerRef.current = window.setTimeout(connectStatusWs, 3000);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connectStatusWs();

    return () => {
      stopped = true;
      if (statusWsReconnectTimerRef.current) {
        window.clearTimeout(statusWsReconnectTimerRef.current);
        statusWsReconnectTimerRef.current = null;
      }
      if (statusWsRef.current) {
        statusWsRef.current.close();
        statusWsRef.current = null;
      }
    };
  }, [accountId]);

  const openCreateModal = () => {
    setEditingAccount(null);
    form.resetFields();
    form.setFieldsValue({
      market_type: MARKET_TYPE_A_STOCK,
      enabled: true,
      executor_lot_size: 100,
      executor_order_timeout_seconds_sequence: timeoutSequenceToText(DEFAULT_TIMEOUT_SEQUENCE),
      executor_max_replace_count: 3,
      executor_max_slippage_pct: 0.5,
      executor_price_level_sequence: sequenceToText(DEFAULT_EXECUTOR_SEQUENCE),
      commission_rate_pct: 0.025,
      min_commission: 5,
      stamp_tax_rate_pct: 0.05
    });
    setModalVisible(true);
  };

  const openEditModal = record => {
    setEditingAccount(record);
    form.setFieldsValue({
      name: record.name,
      identifier: record.identifier,
      market_type: record.market_type || MARKET_TYPE_A_STOCK,
      enabled: record.enabled,
      executor_lot_size: record.executor_lot_size ?? 100,
      executor_order_timeout_seconds_sequence: timeoutSequenceToText(
        record.executor_order_timeout_seconds_sequence
          || (record.executor_order_timeout_seconds ? [record.executor_order_timeout_seconds] : DEFAULT_TIMEOUT_SEQUENCE)
      ),
      executor_max_replace_count: record.executor_max_replace_count ?? 3,
      executor_max_slippage_pct: record.executor_max_slippage_pct ?? 0.5,
      executor_price_level_sequence: sequenceToText(record.executor_price_level_sequence),
      commission_rate_pct: record.commission_rate_pct ?? 0.025,
      min_commission: record.min_commission ?? 5,
      stamp_tax_rate_pct: record.stamp_tax_rate_pct ?? 0.05
    });
    setModalVisible(true);
  };

  const handleSave = async values => {
    setSaving(true);
    try {
      const priceSequence = parseSequence(values.executor_price_level_sequence);
      const timeoutSequence = parseTimeoutSequence(values.executor_order_timeout_seconds_sequence);
      const sequenceError = validateSequenceLengths(priceSequence, timeoutSequence, values.executor_max_replace_count);
      if (sequenceError) {
        message.error(sequenceError);
        return;
      }
      const payload = {
        ...values,
        market_type: values.market_type || MARKET_TYPE_A_STOCK,
        enabled: values.enabled !== false,
        executor_price_level_sequence: priceSequence,
        executor_order_timeout_seconds_sequence: timeoutSequence,
        executor_order_timeout_seconds: timeoutSequence[0],
      };
      if (editingAccount) {
        await request.put(`/api/external-trading-accounts/${editingAccount.id}`, payload);
        message.success('更新成功');
      } else {
        await request.post('/api/external-trading-accounts', payload);
        message.success('添加成功');
      }
      setModalVisible(false);
      fetchAccounts();
    } catch (error) {
      message.error(error.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleAccountMarketTypeChange = value => {
    form.setFieldsValue(marketDefaultFields(value));
  };

  const handleDelete = async id => {
    try {
      await request.delete(`/api/external-trading-accounts/${id}`);
      message.success('删除成功');
      fetchAccounts();
    } catch (error) {
      message.error(error.response?.data?.detail || '删除失败');
    }
  };

  const fetchSubAccounts = async accountId => {
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${accountId}/sub-accounts`);
      setSubAccounts(prev => ({ ...prev, [accountId]: data || [] }));
    } catch (error) {
      message.error(error.response?.data?.detail || '获取虚拟子账户失败');
    }
  };

  const toggleMobileAccount = account => {
    const willExpand = !expandedMobileAccountIds.includes(account.id);
    if (willExpand && !subAccounts[account.id]) {
      fetchSubAccounts(account.id);
    }
    setExpandedMobileAccountIds(prev => (
      prev.includes(account.id)
        ? prev.filter(id => id !== account.id)
        : [...prev, account.id]
    ));
  };

  const fetchNetAssetHistory = async (account, subAccount) => {
    if (!account?.id || !subAccount?.id) return;
    setNetAssetHistoryLoading(true);
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/sub-accounts/${subAccount.id}/net-asset-history`);
      setNetAssetHistory(data || null);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取净资产历史失败');
      setNetAssetHistory(null);
    } finally {
      setNetAssetHistoryLoading(false);
    }
  };

  const openNetAssetHistory = (account, subAccount) => {
    setNetAssetHistoryAccount(account);
    setNetAssetHistorySubAccount(subAccount);
    setNetAssetHistoryVisible(true);
    fetchNetAssetHistory(account, subAccount);
  };

  const openSubCreateModal = record => {
    setActiveAccountForSub(record);
    setEditingSubAccount(null);
    subForm.resetFields();
    subForm.setFieldsValue({
      enabled: true,
      cash_allocated: 0,
      executor_lot_size: null,
      executor_order_timeout_seconds_sequence: '',
      executor_max_replace_count: null,
      executor_max_slippage_pct: null,
      executor_price_level_sequence: ''
    });
    setSubModalVisible(true);
  };

  const openSubEditModal = (account, subAccount) => {
    setActiveAccountForSub(account);
    setEditingSubAccount(subAccount);
    subForm.setFieldsValue({
      name: subAccount.name,
      cash_allocated: subAccount.cash_allocated,
      remark: subAccount.remark,
      enabled: subAccount.enabled,
      executor_lot_size: subAccount.executor_lot_size,
      executor_order_timeout_seconds_sequence: Array.isArray(subAccount.executor_order_timeout_seconds_sequence)
        ? timeoutSequenceToText(subAccount.executor_order_timeout_seconds_sequence)
        : '',
      executor_max_replace_count: subAccount.executor_max_replace_count,
      executor_max_slippage_pct: subAccount.executor_max_slippage_pct,
      executor_price_level_sequence: Array.isArray(subAccount.executor_price_level_sequence)
        ? sequenceToText(subAccount.executor_price_level_sequence)
        : ''
    });
    setSubModalVisible(true);
  };

  const handleSaveSubAccount = async values => {
    if (!activeAccountForSub?.id) return;
    setSaving(true);
    try {
      const inheritedPriceSequence = Array.isArray(activeAccountForSub.executor_price_level_sequence)
        ? activeAccountForSub.executor_price_level_sequence
        : DEFAULT_EXECUTOR_SEQUENCE;
      const inheritedTimeoutSequence = Array.isArray(activeAccountForSub.executor_order_timeout_seconds_sequence)
        ? activeAccountForSub.executor_order_timeout_seconds_sequence
        : DEFAULT_TIMEOUT_SEQUENCE;
      const priceSequence = values.executor_price_level_sequence
        ? parseSequence(values.executor_price_level_sequence, [])
        : null;
      const timeoutSequence = values.executor_order_timeout_seconds_sequence
        ? parseTimeoutSequence(values.executor_order_timeout_seconds_sequence, [])
        : null;
      const effectiveMaxReplaceCount = values.executor_max_replace_count ?? activeAccountForSub.executor_max_replace_count ?? 3;
      const sequenceError = validateSequenceLengths(
        priceSequence || inheritedPriceSequence,
        timeoutSequence || inheritedTimeoutSequence,
        effectiveMaxReplaceCount
      );
      if (sequenceError) {
        message.error(sequenceError);
        return;
      }
      const payload = {
        ...values,
        cash_allocated: Number(values.cash_allocated || 0),
        remark: values.remark || null,
        enabled: values.enabled !== false,
        executor_lot_size: values.executor_lot_size ?? null,
        executor_order_timeout_seconds: timeoutSequence ? timeoutSequence[0] : null,
        executor_order_timeout_seconds_sequence: timeoutSequence,
        executor_max_replace_count: values.executor_max_replace_count ?? null,
        executor_max_slippage_pct: values.executor_max_slippage_pct ?? null,
        executor_price_level_sequence: priceSequence
      };
      if (editingSubAccount) {
        await request.put(`/api/external-trading-accounts/${activeAccountForSub.id}/sub-accounts/${editingSubAccount.id}`, payload);
      } else {
        await request.post(`/api/external-trading-accounts/${activeAccountForSub.id}/sub-accounts`, payload);
      }
      message.success('虚拟子账户已保存');
      setSubModalVisible(false);
      fetchSubAccounts(activeAccountForSub.id);
    } catch (error) {
      message.error(error.response?.data?.detail || '保存虚拟子账户失败');
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteSubAccount = async (account, subAccount) => {
    try {
      await request.delete(`/api/external-trading-accounts/${account.id}/sub-accounts/${subAccount.id}`);
      message.success('虚拟子账户已删除');
      fetchSubAccounts(account.id);
    } catch (error) {
      message.error(error.response?.data?.detail || '删除虚拟子账户失败');
    }
  };

  const executeNettedExecutor = async ({ force = false } = {}) => {
    if (!executorStatusAccount?.id) return;
    setExecutorExecuteLoading(true);
    try {
      const { data } = await request.post(`/api/external-trading-accounts/${executorStatusAccount.id}/executor/execute`, { force });
      const accountResult = data?.accounts?.[0] || {};
      const marketClosedResult = accountResult?.reason === 'market_closed' ? accountResult : data;
      if (marketClosedResult?.status === 'SKIPPED' && marketClosedResult?.reason === 'market_closed') {
        const marketLabel = marketClosedResult.market_label || marketTypeLabel(executorStatusAccount?.market_type);
        message.warning(`当前不在 ${marketLabel}交易时段，执行器将在 ${formatTime(marketClosedResult.next_run_at)} 后继续处理`);
      } else if (accountResult?.status === 'CANCEL_REQUESTED') {
        message.success('已提交撤单，等待回报后执行器会继续撮合');
      } else {
        message.success(accountResult?.result?.message || (force ? '已强制触发净额撮合执行器' : '已触发净额撮合执行器'));
      }
      fetchSubAccounts(executorStatusAccount.id);
      refreshExecutorStatus(executorStatusAccount);
    } catch (error) {
      message.error(error.response?.data?.detail || '执行净额撮合失败');
    } finally {
      setExecutorExecuteLoading(false);
    }
  };



  const buildExecutorTableParams = tableState => {
    const filters = tableState?.filters || {};
    return {
      page: tableState?.page || 1,
      page_size: tableState?.pageSize || 10,
      symbol: joinFilterValues(filters.symbol),
      sub_account: joinFilterValues(filters.sub_account),
      strategy: joinFilterValues(filters.strategy),
      role: joinFilterValues(filters.role),
      event_type: joinFilterValues(filters.event_type),
      process_status: joinFilterValues(filters.process_status)
    };
  };

  const fetchExecutorStatusTable = async (account, tableKey, tableState) => {
    if (!account?.id || !executorServerTableEndpoints[tableKey]) return;
    setExecutorStatusTableLoading(prev => ({ ...prev, [tableKey]: true }));
    try {
      const { data } = await request.get(
        `/api/external-trading-accounts/${account.id}/executor/status/${executorServerTableEndpoints[tableKey]}`,
        { params: buildExecutorTableParams(tableState) }
      );
      setExecutorStatusTables(prev => ({
        ...prev,
        [tableKey]: {
          ...createEmptyExecutorTableData(),
          ...(data || {}),
          rows: data?.rows || [],
          price_details: data?.price_details || {},
          filter_options: data?.filter_options || {}
        }
      }));
      setExecutorStatusTableLoaded(prev => ({ ...prev, [tableKey]: true }));
    } catch (error) {
      message.error(error.response?.data?.detail || `获取${executorServerTableLabels[tableKey]}失败`);
      setExecutorStatusTables(prev => ({
        ...prev,
        [tableKey]: createEmptyExecutorTableData()
      }));
      setExecutorStatusTableLoaded(prev => ({ ...prev, [tableKey]: false }));
    } finally {
      setExecutorStatusTableLoading(prev => ({ ...prev, [tableKey]: false }));
    }
  };

  const fetchExecutorStatusTables = async (account, tableState = executorStatusTableState) => {
    if (!account?.id) return;
    await Promise.all(executorServerTableKeys.map(key => fetchExecutorStatusTable(account, key, tableState[key])));
  };

  const fetchExecutorSubAccountStatus = async account => {
    if (!account?.id) return;
    setExecutorSubAccountStatusLoading(true);
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/executor/status/sub-accounts`);
      setExecutorSubAccountStatus({
        rows: data?.rows || [],
        price_details: data?.price_details || {}
      });
      setExecutorSubAccountStatusLoaded(true);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取子账户状态失败');
      setExecutorSubAccountStatus({ rows: [] });
      setExecutorSubAccountStatusLoaded(false);
    } finally {
      setExecutorSubAccountStatusLoading(false);
    }
  };

  const fetchExecutorPlan = async account => {
    if (!account?.id) return;
    setExecutorPlanLoading(true);
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/executor/status/plan`);
      setExecutorPlan(data || null);
      setExecutorPlanLoaded(true);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取净额预览失败');
      setExecutorPlan(null);
      setExecutorPlanLoaded(false);
    } finally {
      setExecutorPlanLoading(false);
    }
  };

  const fetchExecutorStatus = async (account, { tableState = executorStatusTableState, includeTables = false } = {}) => {
    if (!account?.id) return;
    setExecutorStatusLoading(true);
    if (includeTables) {
      fetchExecutorStatusTables(account, tableState);
    }
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/executor/status`);
      setExecutorStatus(data || null);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取执行器状态失败');
      setExecutorStatus(null);
    } finally {
      setExecutorStatusLoading(false);
    }
  };

  const executorTabTableKey = tabKey => {
    if (tabKey === 'targets') return 'target_positions';
    if (tabKey === 'ledger') return 'ledger_positions';
    if (tabKey === 'orders') return 'orders';
    if (tabKey === 'fills') return 'fills';
    if (tabKey === 'events') return 'events';
    return null;
  };

  const refreshExecutorStatusTab = (account, tabKey, { force = false } = {}) => {
    if (!account?.id) return;
    if (tabKey === 'sub_accounts') {
      if (force || !executorSubAccountStatusLoaded) {
        fetchExecutorSubAccountStatus(account);
      }
      return;
    }
    const tableKey = executorTabTableKey(tabKey);
    if (tableKey) {
      if (force || !executorStatusTableLoaded[tableKey]) {
        fetchExecutorStatusTable(account, tableKey, executorStatusTableState[tableKey]);
      }
      return;
    }
    if (tabKey === 'plan' && (force || !executorPlanLoaded)) {
      fetchExecutorPlan(account);
    }
  };

  const handleExecutorStatusTabChange = tabKey => {
    setExecutorStatusActiveTab(tabKey);
    refreshExecutorStatusTab(executorStatusAccount, tabKey);
  };

  const refreshExecutorStatus = account => {
    fetchExecutorStatus(account);
    refreshExecutorStatusTab(account, executorStatusActiveTab, { force: true });
  };

  const fetchBrokerPositions = async account => {
    if (!account?.id) return;
    setBrokerPositionsLoading(true);
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${account.id}/broker-positions`);
      setBrokerPositions(data || null);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取券商持仓失败');
      setBrokerPositions(null);
    } finally {
      setBrokerPositionsLoading(false);
    }
  };

  const handleMarkBlockSuccess = async record => {
    if (!executorStatusAccount?.id || !record?.id) return;
    setMarkBlockSuccessOrderId(record.id);
    try {
      const values = await markBlockSuccessForm.validateFields();
      const { data } = await request.post(
        `/api/external-trading-accounts/${executorStatusAccount.id}/orders/${record.id}/mark-success`,
        { price: values.price }
      );
      message.success(data?.message || '阻断单已标记成功');
      setMarkBlockSuccessModalVisible(false);
      setMarkBlockSuccessRecord(null);
      markBlockSuccessForm.resetFields();
      refreshExecutorStatus(executorStatusAccount);
    } catch (error) {
      if (!error?.errorFields) {
        message.error(error.response?.data?.detail || '阻断单标记成功失败');
      }
    } finally {
      setMarkBlockSuccessOrderId(null);
    }
  };

  const openMarkBlockSuccessModal = record => {
    const defaultPrice = Number(record?.submitted_price || record?.avg_fill_price || 0);
    setMarkBlockSuccessRecord(record);
    setMarkBlockSuccessModalVisible(true);
    markBlockSuccessForm.setFieldsValue({
      price: Number.isFinite(defaultPrice) && defaultPrice > 0 ? defaultPrice : undefined
    });
  };

  const closeMarkBlockSuccessModal = () => {
    if (markBlockSuccessOrderId) return;
    setMarkBlockSuccessModalVisible(false);
    setMarkBlockSuccessRecord(null);
    markBlockSuccessForm.resetFields();
  };

  const handleRepairParentFill = async record => {
    if (!executorStatusAccount?.id || !record?.id) return;
    setRepairParentFillOrderId(record.id);
    try {
      const values = await repairParentFillForm.validateFields();
      const { data } = await request.post(
        `/api/external-trading-accounts/${executorStatusAccount.id}/orders/${record.id}/repair-parent-fill`,
        { price: values.price }
      );
      message.success(data?.message || '父单补成交完成');
      setRepairParentFillModalVisible(false);
      setRepairParentFillRecord(null);
      repairParentFillForm.resetFields();
      refreshExecutorStatus(executorStatusAccount);
    } catch (error) {
      if (!error?.errorFields) {
        message.error(error.response?.data?.detail || '父单补成交失败');
      }
    } finally {
      setRepairParentFillOrderId(null);
    }
  };

  const openRepairParentFillModal = record => {
    const defaultPrice = Number(record?.avg_fill_price || record?.submitted_price || 0);
    setRepairParentFillRecord(record);
    setRepairParentFillModalVisible(true);
    repairParentFillForm.setFieldsValue({
      price: Number.isFinite(defaultPrice) && defaultPrice > 0 ? defaultPrice : undefined
    });
  };

  const closeRepairParentFillModal = () => {
    if (repairParentFillOrderId) return;
    setRepairParentFillModalVisible(false);
    setRepairParentFillRecord(null);
    repairParentFillForm.resetFields();
  };

  const openExecutorStatus = account => {
    if (!account?.id) return;
    navigate(`/executor-status?account_id=${account.id}`);
  };

  const openBrokerPositions = account => {
    setBrokerPositionsAccount(account);
    setBrokerPositionsVisible(true);
    fetchBrokerPositions(account);
  };

  const orderStatusColor = status => {
    if (status === 'FILLED') return 'success';
    if (['REJECTED', 'FAILED', 'EXPIRED'].includes(status)) return 'error';
    if (['CANCELED', 'PARTIALLY_CANCELED'].includes(status)) return 'default';
    if (['PARTIALLY_FILLED', 'CANCEL_PENDING', 'BLOCKED_INSUFFICIENT_SELLABLE', 'BLOCKED_INSUFFICIENT_POSITION', 'BLOCKED_NON_RETRYABLE_REJECTION'].includes(status)) return 'warning';
    return 'processing';
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

  const eventProcessStatusColor = value => {
    if (value === 'PROCESSED') return 'success';
    if (value === 'FAILED') return 'error';
    if (value === 'UNMATCHED') return 'warning';
    if (value === 'RECEIVED') return 'processing';
    return 'default';
  };

  const renderEventSubAccounts = (_, record) => {
    const related = record?.related_sub_accounts || [];
    if (!related.length) {
      return (
        <Space direction="vertical" size={0}>
          <Text>{record?.sub_account_name || '-'}</Text>
          {record?.strategy_name ? <Text type="secondary">策略: {record.strategy_name}</Text> : null}
        </Space>
      );
    }
    return (
      <Space direction="vertical" size={0}>
        <Text>{record?.sub_account_name || '-'}</Text>
        <Text type="secondary">
          {related.map(item => item.strategy_name ? `${item.name}(${item.strategy_name})` : item.name).join(' / ')}
        </Text>
      </Space>
    );
  };

  const handleExecutorServerTableChange = tableKey => (pagination, filters) => {
    const previousState = executorStatusTableState[tableKey] || { page: 1, pageSize: 10, filters: {} };
    const nextTableState = {
      page: pagination?.current || 1,
      pageSize: pagination?.pageSize || previousState.pageSize || 10,
      filters: normalizeServerTableFilters(filters)
    };
    const nextState = {
      ...executorStatusTableState,
      [tableKey]: nextTableState
    };
    setExecutorStatusTableState(nextState);
    fetchExecutorStatusTable(executorStatusAccount, tableKey, nextTableState);
  };

  const executorServerPagination = tableKey => {
    const tableState = executorStatusTableState[tableKey] || { page: 1, pageSize: 10 };
    const pagination = executorStatusTables[tableKey]?.pagination || {};
    return {
      current: pagination.page || tableState.page,
      pageSize: pagination.page_size || tableState.pageSize,
      total: pagination.total || 0,
      showSizeChanger: true,
      pageSizeOptions: ['10', '20', '50', '100', '200'],
      showTotal: total => `共 ${total} 条`
    };
  };

  const serverFilterProps = (tableKey, filterKey, fallbackOptions = []) => {
    const filteredValue = executorStatusTableState[tableKey]?.filters?.[filterKey] || [];
    return {
      filters: executorStatusTables[tableKey]?.filter_options?.[filterKey] || fallbackOptions,
      filterSearch: true,
      filteredValue: filteredValue.length ? filteredValue : null
    };
  };

  const demandRows = executorPlan?.plan?.demands || [];
  const internalCrossRows = executorPlan?.plan?.internal_crosses || [];
  const externalOrderRows = executorPlan?.plan?.external_orders || [];
  const targetRows = executorStatusTables.target_positions?.rows || [];
  const ledgerRows = executorStatusTables.ledger_positions?.rows || [];
  const lifecycleRows = executorStatusTables.orders?.rows || [];
  const fillRows = executorStatusTables.fills?.rows || [];
  const eventRows = executorStatusTables.events?.rows || [];
  const executorSubAccountRows = executorSubAccountStatus?.rows || [];
  const executorSubAccountTotals = {
    cashAllocated: sumNumberField(executorSubAccountRows, 'cash_allocated'),
    netAsset: sumNumberField(executorSubAccountRows, 'net_asset'),
    cashAvailable: sumNumberField(executorSubAccountRows, 'cash_available')
  };
  const brokerPositionRows = brokerPositions?.positions || [];
  const brokerPositionSummary = brokerPositions?.summary || {};
  const brokerSnapshot = brokerPositions?.snapshot || null;
  const executorPriceDetails = {
    ...(executorStatus?.price_details || {}),
    ...(executorSubAccountStatus?.price_details || {}),
    ...(executorPlan?.price_details || {}),
    ...(executorStatusTables.target_positions?.price_details || {}),
    ...(executorStatusTables.ledger_positions?.price_details || {}),
    ...(executorStatusTables.orders?.price_details || {}),
    ...(executorStatusTables.fills?.price_details || {}),
    ...(executorStatusTables.events?.price_details || {})
  };
  const renderMarketPrice = (_, record) => {
    const detail = executorPriceDetails[normalizeSymbolKey(record?.symbol)];
    const price = Number(detail?.price);
    if (!Number.isFinite(price) || price <= 0) return '-';
    const text = formatNumber(price, 3);
    return detail?.source ? <Tooltip title={`来源: ${detail.source}`}>{text}</Tooltip> : text;
  };
  const renderLifecyclePrice = (value, source) => {
    const price = Number(value || 0);
    if (!Number.isFinite(price) || price <= 0) return '-';
    const text = formatNumber(price, 4);
    return source ? <Tooltip title={`来源: ${source}`}>{text}</Tooltip> : text;
  };
  const marketPriceColumn = { title: '市价', key: 'market_price', width: 100, render: renderMarketPrice };
  const renderSubAccountStrategy = (_, record) => (
    <Space direction="vertical" size={0}>
      <Text>{record.sub_account_name || record.name || '-'}</Text>
      <Text type="secondary">策略: {record.strategy_name || record.strategy_type || record.binding_label || '-'}</Text>
    </Space>
  );

  const demandColumns = [
    { title: '子账户', dataIndex: 'sub_account_name', key: 'sub_account_name', width: 240, render: renderSubAccountStrategy, ...textColumnFilter(demandRows, record => record.sub_account_name) },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...textColumnFilter(demandRows, symbolText) },
    marketPriceColumn,
    { title: '方向', dataIndex: 'side', key: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    {
      title: '状态',
      dataIndex: 'blocked',
      key: 'blocked',
      width: 110,
      render: (_, record) => record.blocked ? <Tag color="orange">{getBlockLabel(record)}</Tag> : <Tag color="green">可执行</Tag>
    },
    { title: '阻断到', dataIndex: 'blocked_until', key: 'blocked_until', width: 170, render: formatTime },
    { title: '当前', dataIndex: 'current_quantity', key: 'current_quantity', width: 100, render: value => formatNumber(value) },
    { title: '目标', dataIndex: 'target_quantity', key: 'target_quantity', width: 100, render: value => formatNumber(value) },
    { title: '执行策略', dataIndex: 'execution_policy', key: 'execution_policy', width: 300, render: formatPolicy }
  ];

  const internalCrossColumns = [
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...textColumnFilter(internalCrossRows, symbolText) },
    marketPriceColumn,
    { title: '撮合数量', dataIndex: 'quantity', key: 'quantity', width: 110, render: value => formatNumber(value) },
    { title: '参考价', dataIndex: 'price', key: 'price', width: 100, render: value => value ? formatNumber(value, 4) : '-' },
    { title: '买方分配', dataIndex: 'buy_allocations', key: 'buy_allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' },
    { title: '卖方分配', dataIndex: 'sell_allocations', key: 'sell_allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' },
    { title: '状态', dataIndex: 'status', key: 'status', width: 120, render: value => <Tag color={value === 'READY' ? 'green' : 'orange'}>{value}</Tag> }
  ];

  const externalOrderColumns = [
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...textColumnFilter(externalOrderRows, symbolText) },
    marketPriceColumn,
    { title: '方向', dataIndex: 'side', key: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '类型', dataIndex: 'order_type', key: 'order_type', width: 90 },
    { title: '限价规则', dataIndex: 'price_level', key: 'price_level', width: 100, render: priceLevelLabel },
    { title: '执行策略', dataIndex: 'execution_policy', key: 'execution_policy', width: 310, render: formatPolicy },
    { title: '分配', dataIndex: 'allocations', key: 'allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' }
  ];

  const targetPositionColumns = [
    { title: '子账户', dataIndex: 'sub_account_name', key: 'sub_account', width: 240, render: renderSubAccountStrategy, ...serverFilterProps('target_positions', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('target_positions', 'symbol') },
    marketPriceColumn,
    { title: '参考价', dataIndex: 'reference_price', key: 'reference_price', width: 100, render: value => value ? formatNumber(value, 3) : '-' },
    { title: '目标', dataIndex: 'target_quantity', key: 'target_quantity', width: 100, render: value => formatNumber(value) },
    { title: '账本', dataIndex: 'current_quantity', key: 'current_quantity', width: 100, render: value => formatNumber(value) },
    { title: '可卖', dataIndex: 'available_quantity', key: 'available_quantity', width: 150, render: renderTargetSellability },
    { title: '未成买', dataIndex: 'pending_buy_quantity', key: 'pending_buy_quantity', width: 90, render: value => formatNumber(value) },
    { title: '未成卖', dataIndex: 'pending_sell_quantity', key: 'pending_sell_quantity', width: 90, render: value => formatNumber(value) },
    { title: '有效', dataIndex: 'effective_quantity', key: 'effective_quantity', width: 100, render: value => formatNumber(value) },
    {
      title: '差额',
      dataIndex: 'delta_quantity',
      key: 'delta_quantity',
      width: 100,
      render: value => <Text type={Number(value || 0) === 0 ? 'secondary' : Number(value || 0) > 0 ? 'danger' : 'success'}>{formatNumber(value)}</Text>
    },
    { title: '动作', dataIndex: 'side', key: 'side', width: 80, render: value => value ? <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> : <Tag>HOLD</Tag> },
    { title: '需执行', dataIndex: 'demand_quantity', key: 'demand_quantity', width: 100, render: value => formatNumber(value) },
    { title: '信号版本', dataIndex: 'signal_version', key: 'signal_version', width: 170, render: value => value || '-' },
    { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 170, render: formatTime }
  ];

  const ledgerPositionColumns = [
    { title: '子账户', dataIndex: 'sub_account_name', key: 'sub_account', width: 240, render: renderSubAccountStrategy, ...serverFilterProps('ledger_positions', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('ledger_positions', 'symbol') },
    marketPriceColumn,
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '原始可用', dataIndex: 'raw_available_quantity', key: 'raw_available_quantity', width: 110, render: value => formatNumber(value) },
    { title: '可卖', dataIndex: 'available_quantity', key: 'available_quantity', width: 150, render: renderSellability },
    { title: 'T+1锁定', dataIndex: 't1_locked_quantity', key: 't1_locked_quantity', width: 110, render: value => formatNumber(value) },
    { title: '当日买入', dataIndex: 'today_buy_quantity', key: 'today_buy_quantity', width: 100, render: value => formatNumber(value) },
    { title: '成本价', dataIndex: 'avg_cost', key: 'avg_cost', width: 100, render: value => formatNumber(value, 4) },
    { title: '市值', dataIndex: 'market_value', key: 'market_value', width: 120, render: value => formatNumber(value, 2) },
    { title: '已实现盈亏', dataIndex: 'realized_pnl', key: 'realized_pnl', width: 120, render: value => formatNumber(value, 2) },
    { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 170, render: formatTime }
  ];

  const brokerPositionColumns = [
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...textColumnFilter(brokerPositionRows, symbolText) },
    { title: '券商数量', dataIndex: 'broker_quantity', key: 'broker_quantity', width: 110, render: value => formatNumber(value) },
    { title: '账本数量', dataIndex: 'ledger_quantity', key: 'ledger_quantity', width: 110, render: value => formatNumber(value) },
    { title: '账本目标', dataIndex: 'ledger_target_quantity', key: 'ledger_target_quantity', width: 110, render: value => formatNumber(value) },
    { title: '数量差额', dataIndex: 'quantity_diff', key: 'quantity_diff', width: 110, render: renderDiffValue },
    { title: '券商可卖', dataIndex: 'broker_available_quantity', key: 'broker_available_quantity', width: 110, render: value => formatNumber(value) },
    { title: '账本可卖', dataIndex: 'ledger_computed_sellable_quantity', key: 'ledger_computed_sellable_quantity', width: 110, render: value => formatNumber(value) },
    { title: '可卖差额', dataIndex: 'sellable_quantity_diff', key: 'sellable_quantity_diff', width: 110, render: renderDiffValue },
    { title: '券商市值', dataIndex: 'broker_market_value', key: 'broker_market_value', width: 130, render: value => formatNumber(value, 2) },
    { title: '账本市值', dataIndex: 'ledger_market_value', key: 'ledger_market_value', width: 130, render: value => formatNumber(value, 2) },
    { title: '市值差额', dataIndex: 'market_value_diff', key: 'market_value_diff', width: 130, render: renderDiffValue },
    {
      title: '差异状态',
      dataIndex: 'diff_status',
      key: 'diff_status',
      width: 130,
      render: value => {
        if (value === 'MATCH') return <Tag color="green">一致</Tag>;
        if (value === 'BROKER_ONLY') return <Tag color="orange">券商独有</Tag>;
        if (value === 'LEDGER_ONLY') return <Tag color="blue">账本独有</Tag>;
        return <Tag color="red">不一致</Tag>;
      }
    }
  ];

  const renderOrderActions = record => {
    const actions = [];
    if (record?.allocation_role === 'BLOCK' && record?.status === 'BLOCKED_INSUFFICIENT_POSITION') {
      actions.push(
        <Button
          key="mark-success"
          size="small"
          type="link"
          loading={markBlockSuccessOrderId === record.id}
          onClick={() => openMarkBlockSuccessModal(record)}
        >
          标记成功
        </Button>
      );
    }
    if (record?.allocation_role === 'PARENT' && record?.needs_fill_repair) {
      actions.push(
        <Button
          key="repair-parent-fill"
          size="small"
          type="link"
          loading={repairParentFillOrderId === record.id}
          onClick={() => openRepairParentFillModal(record)}
        >
          补成交
        </Button>
      );
    }
    return actions.length ? <Space size={4}>{actions}</Space> : '-';
  };

  const orderLifecycleColumns = [
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 170, render: formatTime },
    {
      title: '角色',
      dataIndex: 'allocation_role',
      key: 'role',
      width: 90,
      ...serverFilterProps('orders', 'role', roleFilterOptions),
      render: value => {
        if (value === 'PARENT') return <Tag color="purple">父单</Tag>;
        if (value === 'CHILD') return <Tag color="blue">子单</Tag>;
        if (value === 'BLOCK') return <Tag color="orange">阻断</Tag>;
        return <Tag>{value || '-'}</Tag>;
      }
    },
    {
      title: '子账户',
      dataIndex: 'sub_account_name',
      key: 'sub_account',
      width: 240,
      render: renderSubAccountStrategy,
      ...serverFilterProps('orders', 'sub_account')
    },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('orders', 'symbol') },
    marketPriceColumn,
    { title: '方向', dataIndex: 'side', key: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '状态', dataIndex: 'status', key: 'status', width: 130, render: value => <Tag color={orderStatusColor(value)}>{value || '-'}</Tag> },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '已成', dataIndex: 'filled_quantity', key: 'filled_quantity', width: 100, render: value => formatNumber(value) },
    { title: '未成', dataIndex: 'remaining_quantity', key: 'remaining_quantity', width: 100, render: value => formatNumber(value) },
    { title: '提交价', dataIndex: 'submitted_price', key: 'submitted_price', width: 100, render: value => value ? formatNumber(value, 4) : '-' },
    { title: '均价', dataIndex: 'avg_fill_price', key: 'avg_fill_price', width: 100, render: value => value ? formatNumber(value, 4) : '-' },
    { title: '参考价', dataIndex: 'reference_price', key: 'reference_price', width: 100, render: (value, record) => renderLifecyclePrice(value, record.reference_price_source) },
    { title: '保护价', dataIndex: 'protection_limit_price', key: 'protection_limit_price', width: 100, render: (value, record) => renderLifecyclePrice(value, record.protection_limit_source) },
    { title: '估算费用', dataIndex: 'estimated_fee_total', key: 'estimated_fee_total', width: 110, render: value => formatNumber(value, 2) },
    { title: '真实费用', dataIndex: 'actual_fee_total', key: 'actual_fee_total', width: 110, render: value => value === null || value === undefined ? '-' : formatNumber(value, 2) },
    { title: '费用来源', dataIndex: 'fee_source', key: 'fee_source', width: 110, render: value => value || '-' },
    { title: 'PTrade状态', dataIndex: 'ptrade_status', key: 'ptrade_status', width: 100, render: value => value || '-' },
    { title: '券商订单号', dataIndex: 'broker_order_id', key: 'broker_order_id', width: 170, render: value => value || '-' },
    { title: '档位', dataIndex: 'price_level', key: 'price_level', width: 90, render: value => value === null || value === undefined ? '-' : priceLevelLabel(value) },
    { title: '重定价', dataIndex: 'replace_count', key: 'replace_count', width: 90, render: value => formatNumber(value) },
    { title: '超时点', dataIndex: 'deadline_at', key: 'deadline_at', width: 170, render: formatTime },
    { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 170, render: formatTime },
    { title: '消息', dataIndex: 'message', key: 'message', width: 220, render: value => value || '-' },
    {
      title: '操作',
      key: 'action',
      width: 120,
      fixed: 'right',
      render: (_, record) => renderOrderActions(record)
    }
  ];

  const fillColumns = [
    {
      title: '角色',
      dataIndex: 'allocation_role',
      key: 'role',
      width: 130,
      ...serverFilterProps('fills', 'role', roleFilterOptions.filter(item => item.value !== 'BLOCK')),
      render: (_, record) => (
        <Tag color={record.allocation_role === 'PARENT' ? 'purple' : record.allocation_role === 'BLOCK' ? 'orange' : 'blue'}>
          {roleLabel(record.allocation_role)}
        </Tag>
      )
    },
    { title: '子账户', dataIndex: 'sub_account_name', key: 'sub_account', width: 240, render: renderSubAccountStrategy, ...serverFilterProps('fills', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('fills', 'symbol') },
    marketPriceColumn,
    { title: '方向', dataIndex: 'side', key: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '价格', dataIndex: 'price', key: 'price', width: 100, render: value => formatNumber(value, 4) },
    { title: '金额', dataIndex: 'amount', key: 'amount', width: 120, render: value => formatNumber(value, 2) },
    { title: '估算费用', dataIndex: 'estimated_fee_total', key: 'estimated_fee_total', width: 110, render: value => formatNumber(value, 2) },
    { title: '真实费用', dataIndex: 'actual_fee_total', key: 'actual_fee_total', width: 110, render: value => value === null || value === undefined ? '-' : formatNumber(value, 2) },
    { title: '费用来源', dataIndex: 'fee_source', key: 'fee_source', width: 110, render: value => value || '-' },
    { title: '订单号', dataIndex: 'broker_order_id', key: 'broker_order_id', width: 170, render: value => value || '-' },
    { title: '成交时间', dataIndex: 'traded_at', key: 'traded_at', width: 170, render: formatTime }
  ];

  const eventColumns = [
    { title: '入库时间', dataIndex: 'created_at', key: 'created_at', width: 170, render: formatTime },
    { title: '事件时间', dataIndex: 'event_time', key: 'event_time', width: 170, render: formatTime },
    {
      title: '事件类型',
      dataIndex: 'event_type',
      key: 'event_type',
      width: 120,
      ...serverFilterProps('events', 'event_type'),
      render: value => <Tag color={eventTypeColor(value)}>{eventTypeLabel(value)}</Tag>
    },
    {
      title: '处理状态',
      dataIndex: 'process_status',
      key: 'process_status',
      width: 120,
      ...serverFilterProps('events', 'process_status'),
      render: value => <Tag color={eventProcessStatusColor(value)}>{value || '-'}</Tag>
    },
    { title: '子账户', dataIndex: 'sub_account_name', key: 'sub_account', width: 280, render: renderEventSubAccounts, ...serverFilterProps('events', 'sub_account') },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 150, render: renderSymbol, ...serverFilterProps('events', 'symbol') },
    { title: '方向', dataIndex: 'side', key: 'side', width: 80, render: value => value ? <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> : '-' },
    { title: 'PTrade状态', dataIndex: 'ptrade_status', key: 'ptrade_status', width: 100, render: value => value || '-' },
    { title: '匹配角色', dataIndex: 'matched_order_role', key: 'matched_order_role', width: 100, render: value => value ? roleLabel(value) : '-' },
    { title: '匹配订单状态', dataIndex: 'matched_order_status', key: 'matched_order_status', width: 130, render: value => value ? <Tag color={orderStatusColor(value)}>{value}</Tag> : '-' },
    { title: '券商订单号', dataIndex: 'broker_order_id', key: 'broker_order_id', width: 180, render: value => value || '-' },
    { title: '委托号', dataIndex: 'entrust_no', key: 'entrust_no', width: 120, render: value => value || '-' },
    { title: '客户端订单号', dataIndex: 'client_order_id', key: 'client_order_id', width: 180, render: value => value || '-' },
    { title: '匹配本地ID', dataIndex: 'matched_order_id', key: 'matched_order_id', width: 110, render: value => value || '-' },
    { title: '重放', dataIndex: 'replay_count', key: 'replay_count', width: 80, render: value => formatNumber(value) },
    { title: '处理时间', dataIndex: 'processed_at', key: 'processed_at', width: 170, render: formatTime },
    { title: '消息', dataIndex: 'process_message', key: 'process_message', width: 220, render: value => value || '-' },
    { title: '来源', dataIndex: 'source', key: 'source', width: 110, render: value => value || '-' }
  ];

  const executorSubAccountColumns = [
    { title: '子账户', dataIndex: 'name', key: 'name', width: 240, render: renderSubAccountStrategy, ...textColumnFilter(executorSubAccountRows, record => record.name) },
    { title: '分配资金', dataIndex: 'cash_allocated', key: 'cash_allocated', width: 120, render: value => formatNumber(value, 2) },
    { title: '净资产', dataIndex: 'net_asset', key: 'net_asset', width: 120, render: value => formatNumber(value, 2) },
    { title: '可用资金', dataIndex: 'cash_available', key: 'cash_available', width: 120, render: value => formatNumber(value, 2) },
    { title: '累计交易费', dataIndex: 'cumulative_trade_fee_total', key: 'cumulative_trade_fee_total', width: 170, render: renderTradeFeeSummary },
    { title: '成交数', dataIndex: ['trade_fee_summary', 'fill_count'], key: 'fee_fill_count', width: 90, render: value => formatNumber(value) },
    { title: '未对账成交', dataIndex: ['trade_fee_summary', 'unreconciled_fill_count'], key: 'unreconciled_fill_count', width: 110, render: value => formatNumber(value) },
    { title: '执行策略', dataIndex: 'effective_executor_policy', key: 'effective_executor_policy', width: 320, render: formatPolicy },
    { title: '启用', dataIndex: 'enabled', key: 'enabled', width: 90, render: value => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> }
  ];

  const netAssetHistoryRows = netAssetHistory?.history || [];
  const netAssetHistoryColumns = [
    { title: '日期', dataIndex: 'trading_date', key: 'trading_date', width: 120 },
    { title: '净资产', dataIndex: 'net_asset', key: 'net_asset', width: 130, render: value => formatNumber(value, 2) },
    { title: '持仓市值', dataIndex: 'position_market_value', key: 'position_market_value', width: 130, render: value => formatNumber(value, 2) },
    { title: '可用资金', dataIndex: 'cash_available', key: 'cash_available', width: 130, render: value => formatNumber(value, 2) },
    { title: '分配资金', dataIndex: 'cash_allocated', key: 'cash_allocated', width: 130, render: value => formatNumber(value, 2) },
    { title: '持仓数', dataIndex: 'position_count', key: 'position_count', width: 90, render: value => formatNumber(value) },
    { title: '状态', dataIndex: 'status', key: 'status', width: 100, render: value => <Tag color={value === 'SUCCESS' ? 'green' : 'red'}>{value || '-'}</Tag> },
    { title: '估值时间', dataIndex: 'valued_at', key: 'valued_at', width: 170, render: formatTime },
    { title: '消息', dataIndex: 'message', key: 'message', render: value => value || '-' }
  ];

  const expandedRowRender = account => {
    const rows = subAccounts[account.id] || [];
    const subColumns = [
      { title: '子账户', dataIndex: 'name', key: 'name' },
      {
        title: '绑定策略',
        dataIndex: 'binding_label',
        key: 'binding_label',
        render: (_, record) => record.binding_status === 'BOUND'
          ? <Tag color="blue">{record.strategy_name || record.binding_label}</Tag>
          : <Tag>空闲</Tag>
      },
      { title: '分配资金', dataIndex: 'cash_allocated', key: 'cash_allocated', render: value => Number(value || 0).toLocaleString() },
      { title: '净资产', dataIndex: 'net_asset', key: 'net_asset', render: value => Number(value || 0).toLocaleString() },
      { title: '持仓市值', dataIndex: 'position_market_value', key: 'position_market_value', render: value => Number(value || 0).toLocaleString() },
      { title: '可用资金', dataIndex: 'cash_available', key: 'cash_available', render: value => Number(value || 0).toLocaleString() },
      { title: '累计交易费', dataIndex: 'cumulative_trade_fee_total', key: 'cumulative_trade_fee_total', width: 170, render: renderTradeFeeSummary },
      { title: '执行策略', dataIndex: 'effective_executor_policy', key: 'effective_executor_policy', width: 320, render: formatPolicy },
      { title: '持仓数', dataIndex: 'position_count', key: 'position_count', render: value => formatNumber(value) },
      { title: '启用', dataIndex: 'enabled', key: 'enabled', render: value => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
      {
        title: '操作',
        key: 'action',
        render: (_, subAccount) => (
          <Space>
            <Button size="small" icon={<LineChartOutlined />} onClick={() => openNetAssetHistory(account, subAccount)}>
              曲线
            </Button>
            <Button size="small" icon={<EditOutlined />} onClick={() => openSubEditModal(account, subAccount)}>
              编辑
            </Button>
            <Popconfirm title="确定删除这个虚拟子账户吗？" onConfirm={() => handleDeleteSubAccount(account, subAccount)}>
              <Button size="small" icon={<DeleteOutlined />} danger>
                删除
              </Button>
            </Popconfirm>
          </Space>
        )
      }
    ];
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Space wrap>
          <Button size="small" icon={<PlusOutlined />} onClick={() => openSubCreateModal(account)}>
            添加虚拟子账户
          </Button>
          <Button size="small" onClick={() => openBrokerPositions(account)}>
            券商持仓
          </Button>
          <Button size="small" onClick={() => openExecutorStatus(account)}>
            执行器状态
          </Button>
        </Space>
        <Table rowKey="id" columns={subColumns} dataSource={rows} pagination={false} size="small" scroll={{ x: 1480 }} />
      </Space>
    );
  };

  const columns = [
    {
      title: '账户名',
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{text}</Text>
        </Space>
      )
    },
    {
      title: '唯一标识',
      dataIndex: 'identifier',
      key: 'identifier',
      render: value => <Tag>{value}</Tag>
    },
    {
      title: '市场',
      dataIndex: 'market_type',
      key: 'market_type',
      width: 90,
      render: value => <Tag color={marketTypeColor(value)}>{marketTypeLabel(value)}</Tag>
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 90,
      render: value => (value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>)
    },
    {
      title: '连接状态',
      key: 'connected',
      width: 140,
      render: (_, record) => {
        if (record.connected) {
          return <Badge status="success" text="在线" />;
        }
        return (
          <Tooltip title={record.last_disconnect_reason || ''}>
            <Badge status="default" text="离线" />
          </Tooltip>
        );
      }
    },
    {
      title: '最近心跳',
      key: 'last_seen_at',
      render: (_, record) => formatTime(record.runtime_last_seen_at || record.last_seen_at)
    },
    {
      title: '最近连接',
      dataIndex: 'last_connected_at',
      key: 'last_connected_at',
      render: formatTime
    },
    {
      title: '执行策略',
      key: 'executor_policy',
      width: 320,
      render: (_, record) => formatPolicy(record)
    },
    {
      title: '费用估算',
      key: 'fees',
      width: 220,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text>佣金 {formatNumber(record.commission_rate_pct, 5)}%</Text>
          <Text type="secondary">最低 {formatNumber(record.min_commission, 2)} / 印花税 {formatNumber(record.stamp_tax_rate_pct, 4)}%</Text>
        </Space>
      )
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      render: (_, record) => (
        <Space>
          <Tooltip title="刷新">
            <Button icon={<SyncOutlined />} size="small" onClick={() => fetchAccounts()} />
          </Tooltip>
          <Tooltip title="编辑">
            <Button icon={<EditOutlined />} size="small" onClick={() => openEditModal(record)} />
          </Tooltip>
          <Popconfirm title="确定删除这个外部交易账号吗？" onConfirm={() => handleDelete(record.id)}>
            <Button icon={<DeleteOutlined />} size="small" danger />
          </Popconfirm>
        </Space>
      )
    }
  ];

  const renderMobileSubAccountCards = account => {
    const rows = subAccounts[account.id];
    if (!rows) {
      return <Text type="secondary">正在加载虚拟子账户...</Text>;
    }
    if (!rows.length) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无虚拟子账户" />;
    }
    return rows.map(subAccount => {
      const feeSummary = subAccount?.trade_fee_summary || {};
      const feeTotal = subAccount?.cumulative_trade_fee_total ?? feeSummary.effective_fee_total;
      return (
        <div className="external-subaccount-card" key={subAccount.id}>
          <div className="external-subaccount-card__header">
            <div className="external-subaccount-card__title">
              <Text strong>{subAccount.name}</Text>
              <Text type="secondary">{subAccount.remark || subAccount.binding_label || '虚拟子账户'}</Text>
            </div>
            <Tag color={subAccount.enabled ? 'green' : 'default'}>
              {subAccount.enabled ? '启用' : '停用'}
            </Tag>
          </div>
          <div className="external-subaccount-card__binding">
            {subAccount.binding_status === 'BOUND'
              ? <Tag color="blue">{subAccount.strategy_name || subAccount.binding_label}</Tag>
              : <Tag>空闲</Tag>}
          </div>
          <div className="external-subaccount-card__metrics">
            <div>
              <span>分配资金</span>
              <strong>{formatNumber(subAccount.cash_allocated, 2)}</strong>
            </div>
            <div>
              <span>净资产</span>
              <strong>{formatNumber(subAccount.net_asset, 2)}</strong>
            </div>
            <div>
              <span>可用资金</span>
              <strong>{formatNumber(subAccount.cash_available, 2)}</strong>
            </div>
            <div>
              <span>持仓数</span>
              <strong>{formatNumber(subAccount.position_count)}</strong>
            </div>
          </div>
          <div className="external-subaccount-card__policy">
            <span>执行策略</span>
            <strong>{formatPolicy(subAccount.effective_executor_policy)}</strong>
          </div>
          <div className="external-subaccount-card__fee">
            累计交易费 {formatNumber(feeTotal, 2)}
          </div>
          <div className="external-subaccount-card__actions">
            <Button size="small" icon={<LineChartOutlined />} onClick={() => openNetAssetHistory(account, subAccount)}>
              曲线
            </Button>
            <Button size="small" icon={<EditOutlined />} onClick={() => openSubEditModal(account, subAccount)}>
              编辑
            </Button>
            <Popconfirm title="确定删除这个虚拟子账户吗？" onConfirm={() => handleDeleteSubAccount(account, subAccount)}>
              <Button size="small" icon={<DeleteOutlined />} danger>
                删除
              </Button>
            </Popconfirm>
          </div>
        </div>
      );
    });
  };

  const renderMobileAccountCard = account => {
    const expanded = expandedMobileAccountIds.includes(account.id);
    const rows = subAccounts[account.id] || [];
    return (
      <div className="external-account-card" key={account.id}>
        <div className="external-account-card__header">
          <div className="external-account-card__title">
            <Text strong>{account.name}</Text>
            <Tag color={marketTypeColor(account.market_type)}>{marketTypeLabel(account.market_type)}</Tag>
          </div>
          {account.connected ? (
            <Badge status="success" text="在线" />
          ) : (
            <Tooltip title={account.last_disconnect_reason || ''}>
              <Badge status="default" text="离线" />
            </Tooltip>
          )}
        </div>
        <div className="external-account-card__identifier">
          <Tag>{account.identifier}</Tag>
          <Tag color={account.enabled ? 'green' : 'default'}>{account.enabled ? '启用' : '停用'}</Tag>
        </div>
        <div className="external-account-card__metrics">
          <div>
            <span>最近心跳</span>
            <strong>{formatTime(account.runtime_last_seen_at || account.last_seen_at)}</strong>
          </div>
          <div>
            <span>最近连接</span>
            <strong>{formatTime(account.last_connected_at)}</strong>
          </div>
        </div>
        <div className="external-account-card__policy">
          <span>执行策略</span>
          <strong>{formatPolicy(account)}</strong>
        </div>
        <div className="external-account-card__fee">
          佣金 {formatNumber(account.commission_rate_pct, 5)}% / 最低 {formatNumber(account.min_commission, 2)} / 印花税 {formatNumber(account.stamp_tax_rate_pct, 4)}%
        </div>
        <div className="external-account-card__actions">
          <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => openSubCreateModal(account)}>
            子账户
          </Button>
          <Button size="small" onClick={() => openBrokerPositions(account)}>
            券商持仓
          </Button>
          <Button size="small" onClick={() => openExecutorStatus(account)}>
            执行器
          </Button>
          <Tooltip title="刷新">
            <Button icon={<SyncOutlined />} size="small" onClick={() => fetchAccounts()} />
          </Tooltip>
          <Tooltip title="编辑">
            <Button icon={<EditOutlined />} size="small" onClick={() => openEditModal(account)} />
          </Tooltip>
          <Popconfirm title="确定删除这个外部交易账号吗？" onConfirm={() => handleDelete(account.id)}>
            <Button icon={<DeleteOutlined />} size="small" danger />
          </Popconfirm>
        </div>
        <Button
          className="external-account-card__toggle"
          block
          size="small"
          icon={expanded ? <DownOutlined /> : <RightOutlined />}
          onClick={() => toggleMobileAccount(account)}
        >
          {expanded ? '收起虚拟子账户' : `查看虚拟子账户${rows.length ? ` (${rows.length})` : ''}`}
        </Button>
        {expanded ? (
          <div className="external-subaccount-list">
            {renderMobileSubAccountCards(account)}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <PageShell
      className="external-trading-page"
      title="外部交易账号"
      subtitle="PTrade 与券商侧长连接、子账户账本和执行器入口"
      actions={
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
          添加账号
        </Button>
      }
    >
      <PageSection
        className="external-account-section"
        title="账号列表"
        extra={<Text type="secondary">共 {accounts.length} 个</Text>}
      >
        <div className="external-account-desktop">
          <Table
            rowKey="id"
            columns={columns}
            dataSource={accounts}
            loading={loading}
            pagination={false}
            scroll={{ x: 1500 }}
            expandable={{
              expandedRowRender,
              onExpand: (expanded, record) => {
                if (expanded && !subAccounts[record.id]) fetchSubAccounts(record.id);
              }
            }}
          />
        </div>
        <div className="external-account-mobile-list">
          {loading ? (
            <div className="external-mobile-state">
              <Text type="secondary">正在加载外部交易账号...</Text>
            </div>
          ) : accounts.length ? (
            accounts.map(renderMobileAccountCard)
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无外部交易账号" />
          )}
        </div>
      </PageSection>

      <Modal
        className="external-trading-form-modal"
        title={editingAccount ? '编辑外部交易账号' : '添加外部交易账号'}
        visible={modalVisible}
        onCancel={() => setModalVisible(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width={720}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSave}
          initialValues={{ enabled: true }}
        >
          <Form.Item name="name" label="账户名" rules={[{ required: true, message: '请输入账户名' }]}>
            <Input placeholder="例如：PTrade-A股实盘" />
          </Form.Item>
          <Form.Item name="identifier" label="唯一标识" rules={[{ required: true, message: '请输入唯一标识' }]}>
            <Input placeholder="例如：GS66301027527" />
          </Form.Item>
          <Form.Item name="market_type" label="市场类型" rules={[{ required: true, message: '请选择市场类型' }]}>
            <Select options={MARKET_TYPE_OPTIONS} onChange={handleAccountMarketTypeChange} />
          </Form.Item>
          <Form.Item name="enabled" label="是否启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
          <Divider orientation="left">默认执行策略</Divider>
          <Form.Item
            name="executor_price_level_sequence"
            label={(
              <Space size={4}>
                <span>重定价档位序列</span>
                <Tooltip title={priceLevelTooltip} placement="right" overlayInnerStyle={{ backgroundColor: '#1f2937' }}>
                  <InfoCircleOutlined />
                </Tooltip>
              </Space>
            )}
          >
            <Input placeholder="例如：1,2,3,5,-1" />
          </Form.Item>
          <Form.Item
            name="executor_order_timeout_seconds_sequence"
            label="订单超时秒数序列"
            rules={[{ required: true, message: '请输入订单超时秒数序列' }]}
          >
            <Input placeholder="例如：120,120,180,240,300" />
          </Form.Item>
          <Form.Item name="executor_max_replace_count" label="最大重定价次数" rules={[{ required: true, message: '请输入最大重定价次数' }]}>
            <InputNumber min={0} max={20} step={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="executor_max_slippage_pct" label="最大滑点 (%)" rules={[{ required: true, message: '请输入最大滑点' }]}>
            <InputNumber min={0} max={20} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="executor_lot_size" label="默认最小交易单位" rules={[{ required: true, message: '请输入默认最小交易单位' }]}>
            <InputNumber min={1} step={100} style={{ width: '100%' }} />
          </Form.Item>
          <Divider orientation="left">交易费用估算</Divider>
          <Form.Item name="commission_rate_pct" label="佣金费率 (%)" rules={[{ required: true, message: '请输入佣金费率' }]}>
            <InputNumber min={0} step={0.00001} precision={5} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="min_commission" label="每笔最低佣金" rules={[{ required: true, message: '请输入每笔最低佣金' }]}>
            <InputNumber min={0} step={0.01} precision={2} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="stamp_tax_rate_pct" label="印花税费率 (%)" rules={[{ required: true, message: '请输入印花税费率' }]}>
            <InputNumber min={0} step={0.001} precision={4} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        className="external-trading-form-modal"
        title={editingSubAccount ? '编辑虚拟子账户' : '添加虚拟子账户'}
        visible={subModalVisible}
        onCancel={() => setSubModalVisible(false)}
        onOk={() => subForm.submit()}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width={640}
      >
        <Form
          form={subForm}
          layout="vertical"
          onFinish={handleSaveSubAccount}
          initialValues={{ enabled: true, cash_allocated: 0 }}
        >
          <Form.Item name="name" label="子账户名" rules={[{ required: true, message: '请输入子账户名' }]}>
            <Input placeholder="例如：因子线上交易账本" />
          </Form.Item>
          <Form.Item name="cash_allocated" label="分配资金">
            <InputNumber min={0} step={10000} style={{ width: '100%' }} />
          </Form.Item>
          {editingSubAccount ? (
            <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }} size={4}>
              <Text type="secondary">可用资金</Text>
              <Text>{formatNumber(editingSubAccount.cash_available, 2)}</Text>
              <Text type="secondary">绑定策略</Text>
              <Text>{editingSubAccount.strategy_name || editingSubAccount.binding_label || '空闲'}</Text>
              <Text type="secondary">当前生效执行策略</Text>
              <Text>{formatPolicy(editingSubAccount.effective_executor_policy)}</Text>
            </Space>
          ) : null}
          <Divider orientation="left">执行策略覆盖</Divider>
          <Text type="secondary">留空则继承外部交易账户默认策略；绑定策略保存时会同步它的档位序列、订单超时序列和最小交易单位。</Text>
          <Form.Item name="executor_price_level_sequence" label="重定价档位序列">
            <Input placeholder="留空继承，例如：1,2,3,5,-1" />
          </Form.Item>
          <Form.Item name="executor_order_timeout_seconds_sequence" label="订单超时秒数序列">
            <Input placeholder="留空继承，例如：120,120,180,240,300" />
          </Form.Item>
          <Form.Item name="executor_max_replace_count" label="最大重定价次数">
            <InputNumber min={0} max={20} step={1} style={{ width: '100%' }} placeholder="继承账户默认" />
          </Form.Item>
          <Form.Item name="executor_max_slippage_pct" label="最大滑点 (%)">
            <InputNumber min={0} max={20} step={0.1} style={{ width: '100%' }} placeholder="继承账户默认" />
          </Form.Item>
          <Form.Item name="executor_lot_size" label="最小交易单位">
            <InputNumber min={1} step={100} style={{ width: '100%' }} placeholder="继承账户默认" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={3} placeholder="可选" />
          </Form.Item>
          <Form.Item name="enabled" label="是否启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        className="external-trading-data-modal"
        title={`净资产曲线 - ${netAssetHistorySubAccount?.name || ''}`}
        visible={netAssetHistoryVisible}
        onCancel={() => setNetAssetHistoryVisible(false)}
        width={1080}
        footer={[
          <Button
            key="refresh"
            icon={<SyncOutlined />}
            loading={netAssetHistoryLoading}
            onClick={() => fetchNetAssetHistory(netAssetHistoryAccount, netAssetHistorySubAccount)}
          >
            刷新
          </Button>,
          <Button key="close" onClick={() => setNetAssetHistoryVisible(false)}>
            关闭
          </Button>
        ]}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          <Space wrap>
            <Tag>{netAssetHistorySubAccount?.binding_label || '空闲'}</Tag>
            <Text>记录 {netAssetHistory?.summary?.count || 0}</Text>
            <Text>成功 {netAssetHistory?.summary?.success_count || 0}</Text>
            <Text>失败 {netAssetHistory?.summary?.failed_count || 0}</Text>
          </Space>
          {netAssetHistoryRows.length ? (
            <ReactECharts option={getNetAssetHistoryOption(netAssetHistoryRows)} style={{ height: 380 }} />
          ) : (
            <Empty description={netAssetHistoryLoading ? '加载中' : '暂无净资产历史'} />
          )}
          <Table
            rowKey="id"
            columns={netAssetHistoryColumns}
            dataSource={netAssetHistoryRows}
            loading={netAssetHistoryLoading}
            pagination={{ pageSize: 8 }}
            size="small"
            scroll={{ x: 1200 }}
          />
        </Space>
      </Modal>

      <Modal
        className="external-trading-data-modal external-trading-wide-modal"
        title={`券商持仓 - ${brokerPositionsAccount?.name || ''}`}
        visible={brokerPositionsVisible}
        onCancel={() => setBrokerPositionsVisible(false)}
        width={1440}
        footer={[
          <Button
            key="refresh"
            icon={<SyncOutlined />}
            loading={brokerPositionsLoading}
            onClick={() => fetchBrokerPositions(brokerPositionsAccount)}
          >
            刷新
          </Button>,
          <Button key="close" onClick={() => setBrokerPositionsVisible(false)}>
            关闭
          </Button>
        ]}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          {brokerPositions?.refresh?.error ? (
            <Alert type="warning" showIcon message="快照刷新失败" description={brokerPositions.refresh.error} />
          ) : null}
          <Space wrap>
            <Tag color={brokerPositions?.refresh?.refreshed ? 'green' : 'default'}>
              {brokerPositions?.refresh?.refreshed ? '已刷新快照' : '仅快照'}
            </Tag>
            <Tag color={brokerPositions?.refresh?.market_window_open ? 'blue' : 'default'}>
              {brokerPositions?.refresh?.market_window_open ? '开盘刷新' : '非开盘读快照'}
            </Tag>
            <Text>快照 {brokerSnapshot?.snapshot_at ? formatTime(brokerSnapshot.snapshot_at) : '-'}</Text>
            <Text>来源 {brokerSnapshot?.snapshot_source || '-'}</Text>
            <Text>类型 {brokerSnapshot?.snapshot_kind || '-'}</Text>
            <Text>标的 {brokerPositionSummary?.symbol_count ?? 0}</Text>
            <Text>一致 {brokerPositionSummary?.matched_count ?? 0}</Text>
            <Text type="danger">不一致 {brokerPositionSummary?.mismatch_count ?? 0}</Text>
          </Space>
          <Space wrap>
            <Text>券商市值 {formatNumber(brokerPositionSummary?.broker_market_value_total, 2)}</Text>
            <Text>账本市值 {formatNumber(brokerPositionSummary?.ledger_market_value_total, 2)}</Text>
            <Text style={{ color: diffTextColor(brokerPositionSummary?.market_value_diff_total) }}>
              差额 {formatNumber(brokerPositionSummary?.market_value_diff_total, 2)}
            </Text>
            <Text style={{ color: diffTextColor(brokerPositionSummary?.quantity_diff_total) }}>
              数量差额 {formatNumber(brokerPositionSummary?.quantity_diff_total)}
            </Text>
          </Space>
          <Table
            rowKey={record => record.symbol}
            columns={brokerPositionColumns}
            dataSource={brokerPositionRows}
            loading={brokerPositionsLoading}
            pagination={{ pageSize: 10 }}
            size="small"
            scroll={{ x: 1860 }}
            onRow={record => ({
              style: record.diff_status === 'MATCH' ? undefined : { background: '#fffbe6' }
            })}
          />
        </Space>
      </Modal>

      <Modal
        className="external-trading-form-modal"
        title="标记阻断单成功"
        visible={markBlockSuccessModalVisible}
        onCancel={closeMarkBlockSuccessModal}
        onOk={() => handleMarkBlockSuccess(markBlockSuccessRecord)}
        confirmLoading={markBlockSuccessOrderId === markBlockSuccessRecord?.id}
        okText="标记成功"
        cancelText="取消"
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Text>
            系统会按你输入的成交价写入一笔人工成交，并按成功成交回写账本；此操作当前不支持自动撤销。
          </Text>
          <Space wrap>
            <Tag>{markBlockSuccessRecord?.sub_account_name || '-'}</Tag>
            <Tag>{markBlockSuccessRecord?.symbol_name || markBlockSuccessRecord?.symbol || '-'}</Tag>
            <Tag color={markBlockSuccessRecord?.side === 'SELL' ? 'green' : 'red'}>
              {markBlockSuccessRecord?.side || '-'}
            </Tag>
            <Tag>数量 {formatNumber(markBlockSuccessRecord?.remaining_quantity || markBlockSuccessRecord?.quantity)}</Tag>
          </Space>
          <Form form={markBlockSuccessForm} layout="vertical">
            <Form.Item
              name="price"
              label="成交价"
              rules={[
                { required: true, message: '请输入成交价' },
                { type: 'number', min: 0.0001, message: '成交价必须大于 0' }
              ]}
            >
              <InputNumber
                min={0.0001}
                precision={4}
                step={0.01}
                style={{ width: '100%' }}
                placeholder="请输入人工成交价"
              />
            </Form.Item>
          </Form>
        </Space>
      </Modal>

      <Modal
        className="external-trading-form-modal"
        title="补父单成交"
        visible={repairParentFillModalVisible}
        onCancel={closeRepairParentFillModal}
        onOk={() => handleRepairParentFill(repairParentFillRecord)}
        confirmLoading={repairParentFillOrderId === repairParentFillRecord?.id}
        okText="补成交"
        cancelText="取消"
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Text>
            系统会按你输入的成交价补写父单成交，并把成交数量分配到子单和子账户账本；此操作当前不支持自动撤销。
          </Text>
          <Space wrap>
            <Tag>{repairParentFillRecord?.symbol_name || repairParentFillRecord?.symbol || '-'}</Tag>
            <Tag color={repairParentFillRecord?.side === 'SELL' ? 'green' : 'red'}>
              {repairParentFillRecord?.side || '-'}
            </Tag>
            <Tag>父单数量 {formatNumber(repairParentFillRecord?.quantity)}</Tag>
            <Tag>待分配 {formatNumber(repairParentFillRecord?.child_remaining_quantity)}</Tag>
          </Space>
          <Form form={repairParentFillForm} layout="vertical">
            <Form.Item
              name="price"
              label="成交价"
              rules={[
                { required: true, message: '请输入成交价' },
                { type: 'number', min: 0.0001, message: '成交价必须大于 0' }
              ]}
            >
              <InputNumber
                min={0.0001}
                precision={4}
                step={0.01}
                style={{ width: '100%' }}
                placeholder="请输入实际成交价"
              />
            </Form.Item>
          </Form>
        </Space>
      </Modal>

      <Modal
        className="external-trading-data-modal external-trading-executor-modal"
        title={`执行器状态 - ${executorStatusAccount?.name || ''}`}
        visible={executorStatusVisible}
        onCancel={() => setExecutorStatusVisible(false)}
        width={1320}
        footer={[
          <Button
            key="execute"
            type="primary"
            danger
            loading={executorExecuteLoading}
            onClick={() => executeNettedExecutor()}
            disabled={!executorStatusAccount?.id || executorStatusLoading}
          >
            执行净额限价单
          </Button>,
          <Popconfirm
            key="force-execute"
            title="盘后强制执行会绕过 A 股交易时段限制"
            description="只建议连接 PTrade 模拟器时使用，确认继续？"
            okText="强制执行"
            cancelText="取消"
            onConfirm={() => executeNettedExecutor({ force: true })}
          >
            <Button
              danger
              loading={executorExecuteLoading}
              disabled={!executorStatusAccount?.id || executorStatusLoading}
            >
              盘后强制执行
            </Button>
          </Popconfirm>,
          <Button
            key="refresh"
            icon={<SyncOutlined />}
            onClick={() => refreshExecutorStatus(executorStatusAccount)}
            loading={executorStatusLoading || executorPlanLoading || executorSubAccountStatusLoading}
          >
            刷新
          </Button>,
          <Button key="close" onClick={() => setExecutorStatusVisible(false)}>
            关闭
          </Button>
        ]}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          {executorPlan?.plan_error || executorStatus?.plan_error ? (
            <Alert type="warning" showIcon message="净额预览生成失败" description={executorPlan?.plan_error || executorStatus?.plan_error} />
          ) : null}
          <Space wrap>
            <Tag color={executorStatus?.account?.connected ? 'green' : 'default'}>
              {executorStatus?.account?.connected ? '在线' : '离线'}
            </Tag>
            <Text>子账户 {executorStatus?.summary?.sub_account_count ?? 0}</Text>
            <Text>目标仓位 {executorStatus?.summary?.target_position_count ?? 0}</Text>
            <Text>待执行差额 {executorStatus?.summary?.pending_delta_count ?? 0}</Text>
            <Text>活跃订单 {executorStatus?.summary?.active_order_count ?? 0}</Text>
            <Text>成交回报 {executorStatus?.summary?.fill_count ?? 0}</Text>
            <Text>入站事件 {executorStatus?.summary?.event_log_count ?? 0}</Text>
            <Text>总分配资金 {formatNumber(executorSubAccountTotals.cashAllocated, 2)}</Text>
            <Text>总净资产 {formatNumber(executorSubAccountTotals.netAsset, 2)}</Text>
            <Text>总可用资金 {formatNumber(executorSubAccountTotals.cashAvailable, 2)}</Text>
            <Text>交易费 {formatNumber(executorStatus?.summary?.trade_fee_total, 2)}</Text>
            <Text>归因交易费 {formatNumber(executorStatus?.summary?.attributed_trade_fee_total, 2)}</Text>
            <Text>非交易费 {formatNumber(executorStatus?.summary?.non_trade_fee_total, 2)}</Text>
            <Text>非交易收益 {formatNumber(executorStatus?.summary?.non_trade_income_total, 2)}</Text>
            <Text>总费用 {formatNumber(executorStatus?.summary?.total_fee, 2)}</Text>
          </Space>
          <Tabs
            activeKey={executorStatusActiveTab}
            onChange={handleExecutorStatusTabChange}
            items={[
              {
                key: 'sub_accounts',
                label: '子账户',
                children: (
                  <Table
                    rowKey="id"
                    columns={executorSubAccountColumns}
                    dataSource={executorSubAccountRows}
                    loading={executorSubAccountStatusLoading}
                    pagination={{ pageSize: 10 }}
                    size="small"
                    scroll={{ x: 1500 }}
                  />
                )
              },
              {
                key: 'targets',
                label: '目标仓位',
                children: (
                  <Table
                    rowKey={record => `${record.sub_account_id}-${record.symbol}`}
                    columns={targetPositionColumns}
                    dataSource={targetRows}
                    loading={executorStatusTableLoading.target_positions}
                    pagination={executorServerPagination('target_positions')}
                    onChange={handleExecutorServerTableChange('target_positions')}
                    size="small"
                    scroll={{ x: 1980 }}
                  />
                )
              },
              {
                key: 'ledger',
                label: '账本持仓',
                children: (
                  <Table
                    rowKey={record => `${record.sub_account_id}-${record.symbol}`}
                    columns={ledgerPositionColumns}
                    dataSource={ledgerRows}
                    loading={executorStatusTableLoading.ledger_positions}
                    pagination={executorServerPagination('ledger_positions')}
                    onChange={handleExecutorServerTableChange('ledger_positions')}
                    size="small"
                    scroll={{ x: 1660 }}
                  />
                )
              },
              {
                key: 'orders',
                label: '订单生命周期',
                children: (
                  <Table
                    rowKey="id"
                    columns={orderLifecycleColumns}
                    dataSource={lifecycleRows}
                    loading={executorStatusTableLoading.orders}
                    pagination={executorServerPagination('orders')}
                    onChange={handleExecutorServerTableChange('orders')}
                    size="small"
                    scroll={{ x: 2600 }}
                  />
                )
              },
              {
                key: 'fills',
                label: '成交回报',
                children: (
                  <Table
                    rowKey="id"
                    columns={fillColumns}
                    dataSource={fillRows}
                    loading={executorStatusTableLoading.fills}
                    pagination={executorServerPagination('fills')}
                    onChange={handleExecutorServerTableChange('fills')}
                    size="small"
                    scroll={{ x: 1600 }}
                  />
                )
              },
              {
                key: 'events',
                label: '事件流水',
                children: (
                  <Table
                    rowKey="id"
                    columns={eventColumns}
                    dataSource={eventRows}
                    loading={executorStatusTableLoading.events}
                    pagination={executorServerPagination('events')}
                    onChange={handleExecutorServerTableChange('events')}
                    expandable={{
                      expandedRowRender: record => (
                        <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                          {JSON.stringify(record.raw_payload || {}, null, 2)}
                        </pre>
                      ),
                      rowExpandable: record => !!record.raw_payload
                    }}
                    size="small"
                    scroll={{ x: 2600 }}
                  />
                )
              },
              {
                key: 'plan',
                label: '净额预览',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }} size={16}>
                    <Table
                      title={() => '子账户目标差额'}
                      rowKey={(record, index) => `${record.sub_account_id}-${record.symbol}-${record.side}-${index}`}
                      columns={demandColumns}
                      dataSource={demandRows}
                      loading={executorPlanLoading}
                      pagination={false}
                      size="small"
                      scroll={{ x: 1500 }}
                    />
                    <Table
                      title={() => '内部撮合'}
                      rowKey={(record, index) => `${record.symbol}-${index}`}
                      columns={internalCrossColumns}
                      dataSource={internalCrossRows}
                      loading={executorPlanLoading}
                      pagination={false}
                      size="small"
                      scroll={{ x: 1100 }}
                    />
                    <Table
                      title={() => '提交到 PTrade 的净额限价单'}
                      rowKey={(record, index) => `${record.symbol}-${record.side}-${index}`}
                      columns={externalOrderColumns}
                      dataSource={externalOrderRows}
                      loading={executorPlanLoading}
                      pagination={false}
                      size="small"
                      scroll={{ x: 1280 }}
                    />
                  </Space>
                )
              }
            ]}
          />
        </Space>
      </Modal>
    </PageShell>
  );
};

export default ExternalTradingAccountManager;
