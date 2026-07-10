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
const symbolText = record => {
  const symbol = normalizeFilterText(record?.symbol);
  const name = record?.symbol_name ? String(record.symbol_name) : '';
  return name ? `${name} ${symbol}` : symbol;
};
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
  const minOrderAmount = Number(policy.min_order_amount ?? policy.executor_min_order_amount ?? 0);
  const sequence = sequenceToText(policy.price_level_sequence ?? policy.executor_price_level_sequence);
  const timeoutSequence = timeoutSequenceToText(
    policy.order_timeout_seconds_sequence
      ?? policy.executor_order_timeout_seconds_sequence
      ?? (policy.order_timeout_seconds || policy.executor_order_timeout_seconds
        ? [policy.order_timeout_seconds ?? policy.executor_order_timeout_seconds]
        : DEFAULT_TIMEOUT_SEQUENCE)
  );
  return `档位序列${sequence} / 超时序列${timeoutSequence}s / 重定价${maxReplace ?? '-'}次 / 滑点${maxSlippage ?? '-'}% / 最低金额${minOrderAmount > 0 ? formatNumber(minOrderAmount, 2) : '关闭'}`;
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

const ExternalTradingAccountManager = ({ embedded = false }) => {
  const { accountId } = useAccount();
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
  const [brokerPositionsVisible, setBrokerPositionsVisible] = useState(false);
  const [brokerPositionsAccount, setBrokerPositionsAccount] = useState(null);
  const [brokerPositions, setBrokerPositions] = useState(null);
  const [brokerPositionsLoading, setBrokerPositionsLoading] = useState(false);
  const [netAssetHistoryVisible, setNetAssetHistoryVisible] = useState(false);
  const [netAssetHistoryLoading, setNetAssetHistoryLoading] = useState(false);
  const [netAssetHistoryAccount, setNetAssetHistoryAccount] = useState(null);
  const [netAssetHistorySubAccount, setNetAssetHistorySubAccount] = useState(null);
  const [netAssetHistory, setNetAssetHistory] = useState(null);
  const [form] = Form.useForm();
  const [subForm] = Form.useForm();
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
        message.error('获取交易账户失败');
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
          console.warn('解析交易账户状态推送失败', error);
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
      executor_min_order_amount: 0,
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
      executor_min_order_amount: record.executor_min_order_amount ?? 0,
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
      executor_min_order_amount: null,
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
      executor_min_order_amount: subAccount.executor_min_order_amount,
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
        executor_min_order_amount: values.executor_min_order_amount ?? null,
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

  const openBrokerPositions = account => {
    setBrokerPositionsAccount(account);
    setBrokerPositionsVisible(true);
    fetchBrokerPositions(account);
  };

  const brokerPositionRows = brokerPositions?.positions || [];
  const brokerPositionSummary = brokerPositions?.summary || {};
  const brokerSnapshot = brokerPositions?.snapshot || null;
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
      width: 120,
      render: (_, record) => (
        <Space>
          <Tooltip title="刷新">
            <Button
              icon={<SyncOutlined />}
              size="small"
              loading={loading}
              disabled={loading}
              onClick={() => fetchAccounts()}
            />
          </Tooltip>
          <Tooltip title="编辑">
            <Button icon={<EditOutlined />} size="small" disabled={loading} onClick={() => openEditModal(record)} />
          </Tooltip>
          <Popconfirm title="确定删除这个交易账户吗？" onConfirm={() => handleDelete(record.id)}>
            <Button icon={<DeleteOutlined />} size="small" danger disabled={loading} />
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
            <Button
              size="small"
              icon={<LineChartOutlined />}
              disabled={loading}
              onClick={() => openNetAssetHistory(account, subAccount)}
            >
              曲线
            </Button>
            <Button
              size="small"
              icon={<EditOutlined />}
              disabled={loading}
              onClick={() => openSubEditModal(account, subAccount)}
            >
              编辑
            </Button>
            <Popconfirm title="确定删除这个虚拟子账户吗？" onConfirm={() => handleDeleteSubAccount(account, subAccount)}>
              <Button size="small" icon={<DeleteOutlined />} danger disabled={loading}>
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
          <Button size="small" type="primary" icon={<PlusOutlined />} disabled={loading} onClick={() => openSubCreateModal(account)}>
            子账户
          </Button>
          <Button size="small" disabled={loading} onClick={() => openBrokerPositions(account)}>
            券商持仓
          </Button>
          <Tooltip title="刷新">
            <Button
              icon={<SyncOutlined />}
              size="small"
              loading={loading}
              disabled={loading}
              onClick={() => fetchAccounts()}
            />
          </Tooltip>
          <Tooltip title="编辑">
            <Button icon={<EditOutlined />} size="small" disabled={loading} onClick={() => openEditModal(account)} />
          </Tooltip>
          <Popconfirm title="确定删除这个交易账户吗？" onConfirm={() => handleDelete(account.id)}>
            <Button icon={<DeleteOutlined />} size="small" danger disabled={loading} />
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
        title={embedded ? null : '交易账户'}
        subtitle="PTrade 与券商侧长连接、外部子账户管理"
      >
      <PageSection
        className="external-account-section"
        title="账号列表"
        extra={(
          <Space size={8}>
            <Tooltip title="添加账户">
              <Button aria-label="添加账户" icon={<PlusOutlined />} size="small" type="primary" onClick={openCreateModal} />
            </Tooltip>
            <Text type="secondary">共 {accounts.length} 个</Text>
          </Space>
        )}
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
              <Text type="secondary">正在加载交易账户...</Text>
            </div>
          ) : accounts.length ? (
            accounts.map(renderMobileAccountCard)
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无交易账户" />
          )}
        </div>
      </PageSection>

      <Modal
        className="external-trading-form-modal"
        title={editingAccount ? '编辑交易账户' : '添加交易账户'}
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
          <Form.Item name="executor_min_order_amount" label="单笔最低金额" rules={[{ required: true, message: '请输入单笔最低金额' }]}>
            <InputNumber min={0} step={100} precision={2} style={{ width: '100%' }} />
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
          <Text type="secondary">留空则继承交易账户默认策略；绑定策略保存时会同步它的档位序列、订单超时序列和最小交易单位。</Text>
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
          <Form.Item name="executor_min_order_amount" label="单笔最低金额">
            <InputNumber min={0} step={100} precision={2} style={{ width: '100%' }} placeholder="继承账户默认，0 表示不限制" />
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

    </PageShell>
  );
};

export default ExternalTradingAccountManager;
