import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  EditOutlined,
  FundProjectionScreenOutlined,
  HistoryOutlined,
  LineChartOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';

const { Title, Text } = Typography;

const formatNumber = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toFixed(digits)
);
const formatMoney = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
);
const formatPercent = (value, digits = 2) => (
  value === null || value === undefined ? '-' : `${Number(value || 0).toFixed(digits)}%`
);
const formatDateTime = (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-');
const formatDate = (value) => (value ? dayjs(value).format('YYYY-MM-DD') : '-');
const formatWeights = (weights) => `${(weights || []).join(' / ')}%`;
const parseNumberList = (value) => String(value || '')
  .split(/[,，]+/)
  .map(item => item.trim())
  .filter(Boolean)
  .map(item => Number(item));
const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(item => {
      if (typeof item === 'string') return item;
      const field = Array.isArray(item.loc) ? item.loc.filter(part => part !== 'body').join('.') : '';
      return field ? `${field}: ${item.msg}` : item.msg;
    }).filter(Boolean).join('；') || fallback;
  }
  if (typeof detail === 'object') return detail.msg || detail.message || JSON.stringify(detail);
  return String(detail);
};

const universeOptions = [
  { label: '纳指ETF 513100.SH', value: '513100.SH' },
  { label: 'A500ETF 563360.SH', value: '563360.SH' },
  { label: '沪深300ETF 510300.SH', value: '510300.SH' },
  { label: '创业板ETF 159915.SZ', value: '159915.SZ' },
  { label: '科创200ETF 588230.SH', value: '588230.SH' },
  { label: '中证500ETF 510500.SH', value: '510500.SH' },
  { label: '红利ETF 510880.SH', value: '510880.SH' },
  { label: '煤炭ETF 515220.SH', value: '515220.SH' },
  { label: '30年国债ETF 511090.SH', value: '511090.SH' },
  { label: '豆粕ETF 159985.SZ', value: '159985.SZ' },
  { label: '有色ETF 159980.SZ', value: '159980.SZ' },
  { label: '医疗ETF 512170.SH', value: '512170.SH' },
  { label: '军工ETF 512660.SH', value: '512660.SH' },
  { label: '酒ETF 512690.SH', value: '512690.SH' },
  { label: '银行ETF 512800.SH', value: '512800.SH' },
  { label: '证券ETF 512880.SH', value: '512880.SH' },
  { label: '日本ETF 513000.SH', value: '513000.SH' },
  { label: '德国ETF 513030.SH', value: '513030.SH' },
  { label: '香港创新药ETF 513120.SH', value: '513120.SH' },
  { label: '纳指生物科技ETF 513290.SH', value: '513290.SH' },
  { label: '标普500ETF 513500.SH', value: '513500.SH' },
  { label: '港股红利ETF 513630.SH', value: '513630.SH' },
  { label: '东南亚科技ETF 513730.SH', value: '513730.SH' },
  { label: '能源化工ETF 159981.SZ', value: '159981.SZ' },
  { label: '可转债ETF 511380.SH', value: '511380.SH' },
  { label: '恒生科技ETF 513130.SH', value: '513130.SH' },
  { label: '新能源车ETF 515030.SH', value: '515030.SH' },
  { label: '5G通信ETF 515050.SH', value: '515050.SH' },
  { label: '软件ETF 515230.SH', value: '515230.SH' },
  { label: '光伏ETF 515790.SH', value: '515790.SH' },
  { label: '黄金ETF 518880.SH', value: '518880.SH' },
  { label: '沙特ETF 520830.SH', value: '520830.SH' },
  { label: '巴西ETF 520870.SH', value: '520870.SH' },
  { label: '机器人ETF 562500.SH', value: '562500.SH' },
  { label: '科创板ETF 588000.SH', value: '588000.SH' },
  { label: '纳指科技ETF 159509.SZ', value: '159509.SZ' },
  { label: '标普消费ETF 159529.SZ', value: '159529.SZ' },
  { label: '亚太精选ETF 159687.SZ', value: '159687.SZ' },
  { label: '人工智能ETF 159819.SZ', value: '159819.SZ' },
  { label: '养殖ETF 159865.SZ', value: '159865.SZ' },
  { label: '游戏ETF 159869.SZ', value: '159869.SZ' },
  { label: '纳指100ETF 159941.SZ', value: '159941.SZ' },
  { label: '芯片ETF 159995.SZ', value: '159995.SZ' },
  { label: '印度ETF 164824.SZ', value: '164824.SZ' },
];

const defaultUniverseSymbols = [
  '513100.SH',
  '159915.SZ',
  '563360.SH',
  '588230.SH',
  '510500.SH',
  '510880.SH',
  '515220.SH',
  '518880.SH',
];

const benchmarkOptions = [
  { label: '沪深300ETF 510300.SH', value: '510300.SH' },
  { label: '中证500ETF 510500.SH', value: '510500.SH' },
  { label: '纳指ETF 513100.SH', value: '513100.SH' },
];

const symbolLabelMap = [...universeOptions, ...benchmarkOptions].reduce((acc, item) => {
  acc[item.value] = item.label;
  return acc;
}, {});
const formatSymbolLabel = (value) => symbolLabelMap[value] || value || '-';
const formatSymbolList = (symbols, maxCount = 4) => {
  const values = symbols || [];
  if (!values.length) return '-';
  const shown = values.slice(0, maxCount).map(formatSymbolLabel).join('、');
  return values.length > maxCount ? `${shown} 等${values.length}个` : shown;
};

const rebalanceFrequencyOptions = [
  { label: '每日', value: 'daily' },
  { label: '每周', value: 'weekly' },
  { label: '每月', value: 'monthly' },
];
const getRebalanceFrequencyLabel = (value) => (
  rebalanceFrequencyOptions.find(item => item.value === value)?.label || value || '-'
);

const tradeReasonMeta = {
  initial_entry: { label: '首次建仓', color: 'purple' },
  basket_symbols_changed: { label: '标的变化', color: 'orange' },
  rank_order_changed: { label: '排名变化', color: 'gold' },
  target_weights_changed: { label: '权重变化', color: 'cyan' },
  drift_threshold: { label: '阈值触发', color: 'volcano' },
  basket_change: { label: '篮子变化', color: 'orange' },
  drift: { label: '权重漂移', color: 'volcano' },
  target_refresh: { label: '目标刷新', color: 'default' },
};
const getTradeReasonMeta = (value) => tradeReasonMeta[value] || { label: value || '-', color: 'default' };

const defaultValues = {
  name: 'W20 风险调整 ETF 动量虚拟盘',
  enabled: true,
  symbols: defaultUniverseSymbols,
  benchmark_symbols: ['510300.SH', '510500.SH', '513100.SH'],
  initial_capital: 1000000,
  start_date: dayjs('2018-01-02'),
  window: 20,
  top_weights_text: '80,20',
  rebalance_frequency: 'weekly',
  drift_threshold_pct: 100,
  commission_pct: 0.03,
  slippage_pct: 0.02,
  lot_size: 100,
};

const normalizeConfigForForm = (config) => ({
  ...defaultValues,
  ...config,
  start_date: config?.start_date ? dayjs(config.start_date) : defaultValues.start_date,
  top_weights_text: (config?.top_weights || [80, 20]).join(','),
});

const W20MomentumLive = () => {
  const [form] = Form.useForm();
  const [viewMode, setViewMode] = useState('list');
  const [activeTab, setActiveTab] = useState('overview');
  const [configs, setConfigs] = useState([]);
  const [selectedConfig, setSelectedConfig] = useState(null);
  const [detail, setDetail] = useState(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [configLoading, setConfigLoading] = useState(false);
  const [syncLoadingId, setSyncLoadingId] = useState(null);
  const [formError, setFormError] = useState(null);

  useEffect(() => {
    fetchConfigs();
  }, []);

  const fetchConfigs = async () => {
    setListLoading(true);
    try {
      const { data } = await request.get('/api/w20-momentum-live/configs');
      setConfigs(data || []);
    } catch (error) {
      message.error(formatErrorMessage(error, '加载 W20 虚拟盘列表失败'));
    } finally {
      setListLoading(false);
    }
  };

  const fetchDetail = async (configId) => {
    if (!configId) {
      setDetail(null);
      return null;
    }
    setDetailLoading(true);
    try {
      const { data } = await request.get(`/api/w20-momentum-live/configs/${configId}/detail`);
      setDetail(data);
      setSelectedConfig(data.config);
      form.setFieldsValue(normalizeConfigForForm(data.config));
      return data;
    } catch (error) {
      message.error(formatErrorMessage(error, '加载 W20 虚拟盘详情失败'));
      return null;
    } finally {
      setDetailLoading(false);
    }
  };

  const openCreate = () => {
    setSelectedConfig(null);
    setDetail(null);
    setFormError(null);
    form.resetFields();
    form.setFieldsValue(defaultValues);
    setActiveTab('config');
    setViewMode('detail');
  };

  const openConfig = async (record, tabKey = 'overview') => {
    setViewMode('detail');
    setActiveTab(tabKey);
    setFormError(null);
    await fetchDetail(record.id);
  };

  const returnToList = async () => {
    setViewMode('list');
    setSelectedConfig(null);
    setDetail(null);
    setFormError(null);
    form.resetFields();
    await fetchConfigs();
  };

  const buildPayload = (values) => {
    const weights = parseNumberList(values.top_weights_text);
    if (!weights.length || weights.some(item => !Number.isFinite(item) || item < 0)) {
      throw new Error('目标权重需要填写为逗号分隔的非负数字，例如 80,20');
    }
    if (weights.reduce((sum, item) => sum + item, 0) <= 0) {
      throw new Error('目标权重之和必须大于 0');
    }
    if ((values.symbols || []).length < weights.length) {
      throw new Error(`目标权重代表 Top${weights.length}，标的池至少需要 ${weights.length} 个标的`);
    }
    return {
      name: values.name,
      enabled: !!values.enabled,
      symbols: values.symbols || [],
      benchmark_symbols: values.benchmark_symbols || [],
      initial_capital: values.initial_capital,
      start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : defaultValues.start_date.format('YYYY-MM-DD'),
      window: values.window,
      top_weights: weights,
      rebalance_frequency: values.rebalance_frequency,
      drift_threshold_pct: values.drift_threshold_pct,
      commission_pct: values.commission_pct,
      slippage_pct: values.slippage_pct,
      lot_size: values.lot_size,
    };
  };

  const handleSave = async (values) => {
    setConfigLoading(true);
    setFormError(null);
    try {
      const payload = buildPayload(values);
      const requestAction = selectedConfig?.id
        ? request.put(`/api/w20-momentum-live/configs/${selectedConfig.id}`, payload)
        : request.post('/api/w20-momentum-live/configs', payload);
      const { data } = await requestAction;
      setSelectedConfig(data);
      form.setFieldsValue(normalizeConfigForForm(data));
      message.success('W20 虚拟盘配置已保存');
      await fetchConfigs();
      await fetchDetail(data.id);
      setActiveTab('overview');
    } catch (error) {
      const errorMessage = formatErrorMessage(error, '保存 W20 虚拟盘配置失败');
      setFormError(errorMessage);
      message.error(errorMessage);
    } finally {
      setConfigLoading(false);
    }
  };

  const handleDelete = async (record) => {
    try {
      await request.delete(`/api/w20-momentum-live/configs/${record.id}`);
      message.success('配置已删除');
      if (selectedConfig?.id === record.id) {
        setViewMode('list');
        setSelectedConfig(null);
        setDetail(null);
      }
      await fetchConfigs();
    } catch (error) {
      message.error(formatErrorMessage(error, '删除配置失败'));
    }
  };

  const handleSync = async (record = selectedConfig) => {
    if (!record?.id) {
      message.warning('请先保存配置');
      return;
    }
    setSyncLoadingId(record.id);
    try {
      await request.post(`/api/w20-momentum-live/configs/${record.id}/sync`);
      message.success('虚拟盘同步完成');
      await fetchConfigs();
      if (viewMode === 'detail') {
        await fetchDetail(record.id);
      }
    } catch (error) {
      message.error(formatErrorMessage(error, '同步 W20 虚拟盘失败'));
      await fetchConfigs();
    } finally {
      setSyncLoadingId(null);
    }
  };

  const renderActionButton = (eventHandler) => (event) => {
    event.stopPropagation();
    eventHandler();
  };

  const equityOption = useMemo(() => {
    const curve = detail?.equity_curve || [];
    if (!curve.length) return null;
    const firstValue = curve.find(item => item.value)?.value || curve[0]?.value || 1;
    const firstBenchmark = curve.find(item => item.benchmark_value)?.benchmark_value || curve[0]?.benchmark_value || null;
    return {
      tooltip: { trigger: 'axis', valueFormatter: value => `${Number(value).toFixed(2)}%` },
      legend: { top: 0 },
      grid: { top: 48, left: 54, right: 24, bottom: 44 },
      xAxis: { type: 'category', data: curve.map(item => item.date), boundaryGap: false },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
      series: [
        {
          name: '策略',
          type: 'line',
          showSymbol: false,
          smooth: true,
          data: curve.map(item => (item.value / firstValue - 1) * 100),
        },
        {
          name: '动态等权基准',
          type: 'line',
          showSymbol: false,
          smooth: true,
          data: curve.map(item => (
            firstBenchmark && item.benchmark_value ? (item.benchmark_value / firstBenchmark - 1) * 100 : null
          )),
        },
      ],
    };
  }, [detail]);

  const drawdownOption = useMemo(() => {
    const curve = detail?.equity_curve || [];
    if (!curve.length) return null;
    return {
      tooltip: { trigger: 'axis', valueFormatter: value => `${Number(value).toFixed(2)}%` },
      legend: { top: 0 },
      grid: { top: 48, left: 54, right: 24, bottom: 44 },
      xAxis: { type: 'category', data: curve.map(item => item.date), boundaryGap: false },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
      series: [
        {
          name: '策略回撤',
          type: 'line',
          showSymbol: false,
          areaStyle: {},
          data: curve.map(item => item.drawdown),
        },
        {
          name: '基准回撤',
          type: 'line',
          showSymbol: false,
          data: curve.map(item => item.benchmark_drawdown),
        },
      ],
    };
  }, [detail]);

  const configColumns = [
    {
      title: '策略',
      dataIndex: 'name',
      key: 'name',
      width: 220,
      render: (value, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{value}</Text>
          <Text type="secondary">{formatSymbolList(record.symbols, 2)}</Text>
        </Space>
      ),
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 80,
      render: (value) => <Tag color={value ? 'success' : 'default'}>{value ? '开启' : '关闭'}</Tag>,
    },
    {
      title: '参数',
      key: 'params',
      width: 260,
      render: (_, record) => (
        <Space wrap size={4}>
          <Tag>W{record.window}</Tag>
          <Tag>Top{record.top_n}</Tag>
          <Tag>{formatWeights(record.top_weights)}</Tag>
          <Tag>{getRebalanceFrequencyLabel(record.rebalance_frequency)}</Tag>
          <Tag>阈值 {formatPercent(record.drift_threshold_pct)}</Tag>
        </Space>
      ),
    },
    {
      title: '最新日期',
      dataIndex: ['runtime', 'latest_date'],
      key: 'latest_date',
      width: 110,
      render: formatDate,
    },
    {
      title: '总资产',
      dataIndex: ['runtime', 'portfolio_value'],
      key: 'portfolio_value',
      width: 130,
      render: value => formatMoney(value, 0),
    },
    {
      title: '累计收益',
      dataIndex: ['runtime', 'total_return'],
      key: 'total_return',
      width: 110,
      render: value => formatPercent(value),
    },
    {
      title: '交易笔数',
      dataIndex: ['runtime', 'trade_count'],
      key: 'trade_count',
      width: 90,
      render: value => value ?? 0,
    },
    {
      title: '同步状态',
      key: 'sync_status',
      width: 180,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Tag color={record.last_sync_status === 'success' ? 'success' : record.last_sync_status === 'failed' ? 'error' : 'blue'}>
            {record.last_sync_status || '-'}
          </Tag>
          <Text type="secondary">{formatDateTime(record.last_sync_at)}</Text>
        </Space>
      ),
    },
    {
      title: '操作',
      key: 'action',
      fixed: 'right',
      width: 150,
      render: (_, record) => (
        <Space size={8}>
          <Button icon={<EditOutlined />} size="small" onClick={renderActionButton(() => openConfig(record, 'config'))} />
          <Button icon={<LineChartOutlined />} size="small" onClick={renderActionButton(() => openConfig(record, 'overview'))} />
          <Button
            icon={<PlayCircleOutlined />}
            size="small"
            loading={syncLoadingId === record.id}
            onClick={renderActionButton(() => handleSync(record))}
          />
          <Popconfirm
            title="删除配置"
            description="确认删除这条 W20 虚拟盘配置和对应记录吗？"
            okText="删除"
            cancelText="取消"
            onConfirm={(event) => {
              event?.stopPropagation?.();
              handleDelete(record);
            }}
          >
            <Button icon={<DeleteOutlined />} size="small" danger onClick={(event) => event.stopPropagation()} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const holdingColumns = [
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 180, render: formatSymbolLabel },
    { title: '股数', dataIndex: 'shares', key: 'shares', width: 100, render: value => formatMoney(value, 0) },
    { title: '价格', dataIndex: 'price', key: 'price', width: 100, render: value => formatNumber(value, 4) },
    { title: '市值', dataIndex: 'market_value', key: 'market_value', width: 130, render: value => formatMoney(value, 2) },
    { title: '实际仓位', dataIndex: 'actual_weight_pct', key: 'actual_weight_pct', width: 110, render: value => formatPercent(value) },
    { title: '目标仓位', dataIndex: 'target_weight_pct', key: 'target_weight_pct', width: 110, render: value => formatPercent(value) },
  ];

  const tradeColumns = [
    { title: '日期', dataIndex: 'date', key: 'date', width: 110, render: formatDate },
    { title: '信号日', dataIndex: 'signal_date', key: 'signal_date', width: 110, render: formatDate },
    {
      title: '方向',
      dataIndex: 'action',
      key: 'action',
      width: 80,
      render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag>,
    },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 180, render: formatSymbolLabel },
    { title: '价格', dataIndex: 'price', key: 'price', width: 100, render: value => formatNumber(value, 4) },
    {
      title: '价格来源',
      dataIndex: 'price_source',
      key: 'price_source',
      width: 120,
      render: value => <Tag color={value === 'realtime_quote' ? 'blue' : 'default'}>{value === 'realtime_quote' ? '实时价' : '日K'}</Tag>,
    },
    { title: '行情时间', dataIndex: 'quote_timestamp', key: 'quote_timestamp', width: 170, render: formatDateTime },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatMoney(value, 0) },
    { title: '金额', dataIndex: 'amount', key: 'amount', width: 120, render: value => formatMoney(value, 2) },
    { title: '佣金', dataIndex: 'commission', key: 'commission', width: 90, render: value => formatMoney(value, 2) },
    { title: '交易后总资产', dataIndex: 'portfolio_value_after', key: 'portfolio_value_after', width: 130, render: value => formatMoney(value, 2) },
    { title: '标的市值', dataIndex: 'symbol_market_value_after', key: 'symbol_market_value_after', width: 120, render: value => formatMoney(value, 2) },
    { title: '标的仓位', dataIndex: 'symbol_weight_pct_after', key: 'symbol_weight_pct_after', width: 110, render: value => formatPercent(value) },
    {
      title: '触发原因',
      dataIndex: 'reason',
      key: 'reason',
      width: 120,
      render: value => {
        const meta = getTradeReasonMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    { title: '原因详情', dataIndex: 'reason_detail', key: 'reason_detail', width: 420 },
  ];

  const logColumns = [
    { title: '时间', dataIndex: 'timestamp', key: 'timestamp', width: 170, render: formatDateTime },
    { title: '日期', dataIndex: 'date', key: 'date', width: 110, render: formatDate },
    {
      title: '级别',
      dataIndex: 'level',
      key: 'level',
      width: 80,
      render: value => <Tag color={value === 'ERROR' ? 'error' : 'blue'}>{value}</Tag>,
    },
    { title: '动作', dataIndex: 'action', key: 'action', width: 100, render: value => <Tag>{value}</Tag> },
    { title: '消息', dataIndex: 'message', key: 'message' },
  ];

  const annualColumns = [
    { title: '年份', dataIndex: 'year', key: 'year', width: 90 },
    { title: '策略', dataIndex: 'strategy_return', key: 'strategy_return', width: 110, render: value => formatPercent(value) },
    { title: '动态等权基准', dataIndex: 'equal_weight_return', key: 'equal_weight_return', width: 130, render: value => formatPercent(value) },
    { title: '超额等权', dataIndex: 'excess_equal_weight_return', key: 'excess_equal_weight_return', width: 110, render: value => formatPercent(value) },
    ...((selectedConfig?.benchmark_symbols || defaultValues.benchmark_symbols).map(symbol => ({
      title: formatSymbolLabel(symbol),
      key: symbol,
      width: 140,
      render: (_, record) => formatPercent(record.benchmark_returns?.[symbol]),
    }))),
  ];

  const benchmarkColumns = [
    { title: '基准', dataIndex: 'symbol', key: 'symbol', width: 180, render: formatSymbolLabel },
    {
      title: '有效区间',
      key: 'range',
      width: 220,
      render: (_, record) => `${formatDate(record.effective_start_date)} ~ ${formatDate(record.effective_end_date)}`,
    },
    { title: '累计收益', dataIndex: 'total_return', key: 'total_return', width: 110, render: value => formatPercent(value) },
    { title: '年化收益', dataIndex: 'annualized_return', key: 'annualized_return', width: 110, render: value => formatPercent(value) },
    { title: '最大回撤', dataIndex: 'max_drawdown', key: 'max_drawdown', width: 110, render: value => formatPercent(value) },
    { title: 'Sharpe', dataIndex: 'sharpe_ratio', key: 'sharpe_ratio', width: 90, render: value => formatNumber(value, 3) },
    { title: 'Calmar', dataIndex: 'calmar_ratio', key: 'calmar_ratio', width: 90, render: value => formatNumber(value, 3) },
  ];

  const symbolTradeColumns = [
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 180, render: formatSymbolLabel },
    {
      title: '有效时间范围',
      key: 'range',
      width: 220,
      render: (_, record) => `${formatDate(record.effective_start_date)} ~ ${formatDate(record.effective_end_date)}`,
    },
    {
      title: '盈利金额',
      dataIndex: 'profit_amount',
      key: 'profit_amount',
      width: 120,
      render: value => <span style={{ color: Number(value || 0) >= 0 ? '#3f8600' : '#cf1322' }}>{formatMoney(value, 2)}</span>,
      sorter: (a, b) => Number(a.profit_amount || 0) - Number(b.profit_amount || 0),
    },
    { title: '买入次数', dataIndex: 'buy_count', key: 'buy_count', width: 100 },
    { title: '卖出次数', dataIndex: 'sell_count', key: 'sell_count', width: 100 },
    { title: '合计次数', dataIndex: 'trade_count', key: 'trade_count', width: 100 },
  ];

  const renderConfigForm = () => (
    <Form form={form} layout="vertical" initialValues={defaultValues} onFinish={handleSave}>
      {formError && <Alert type="error" showIcon message={formError} style={{ marginBottom: 16 }} />}
      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Form.Item name="name" label="策略名称" rules={[{ required: true, message: '请输入策略名称' }]}>
            <Input />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="enabled" label="启用策略" valuePropName="checked">
            <Switch checkedChildren="开启" unCheckedChildren="关闭" />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="start_date" label="开始日期" rules={[{ required: true, message: '请选择开始日期' }]}>
            <DatePicker style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Form.Item name="symbols" label="标的池" rules={[{ required: true, message: '请选择标的池' }]}>
            <Select mode="multiple" options={universeOptions} optionFilterProp="label" />
          </Form.Item>
        </Col>
        <Col xs={24} md={12}>
          <Form.Item name="benchmark_symbols" label="展示基准" rules={[{ required: true, message: '请选择基准' }]}>
            <Select mode="multiple" options={benchmarkOptions} optionFilterProp="label" />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} md={6}>
          <Form.Item name="initial_capital" label="初始资金" rules={[{ required: true }]}>
            <InputNumber min={0} step={10000} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="window" label="回归窗口" rules={[{ required: true }]}>
            <InputNumber min={2} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="top_weights_text" label="Top N 权重%" rules={[{ required: true, message: '请输入目标权重' }]}>
            <Input placeholder="80,20" />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="rebalance_frequency" label="排名/调仓频率" rules={[{ required: true }]}>
            <Select options={rebalanceFrequencyOptions} />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} md={6}>
          <Form.Item name="drift_threshold_pct" label="绝对漂移阈值%" rules={[{ required: true }]}>
            <InputNumber min={0} max={1000} step={1} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="commission_pct" label="佣金%" rules={[{ required: true }]}>
            <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="slippage_pct" label="滑点%" rules={[{ required: true }]}>
            <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={6}>
          <Form.Item name="lot_size" label="最小交易单位" rules={[{ required: true }]}>
            <InputNumber min={1} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>

      <Form.Item>
        <Space wrap>
          <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={configLoading}>
            保存配置
          </Button>
          <Button
            icon={<PlayCircleOutlined />}
            onClick={() => handleSync(selectedConfig)}
            loading={syncLoadingId === selectedConfig?.id}
            disabled={!selectedConfig?.id}
          >
            同步虚拟盘
          </Button>
        </Space>
      </Form.Item>
    </Form>
  );

  const renderOverview = () => {
    if (!selectedConfig?.id) return <Empty description="保存配置后查看虚拟盘" />;
    const metrics = detail?.summary?.metrics || {};
    const meta = detail?.summary?.meta || {};
    const latestSignal = detail?.summary?.latest_signal;
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={16}>
        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}>
            <Statistic title="总资产" value={detail?.summary?.portfolio_value || 0} precision={2} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="累计收益" value={metrics.total_return ?? detail?.summary?.total_return ?? 0} precision={2} suffix="%" />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="年化收益" value={metrics.annualized_return || 0} precision={2} suffix="%" />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="最大回撤" value={metrics.max_drawdown || 0} precision={2} suffix="%" />
          </Col>
        </Row>
        <Descriptions bordered size="small" column={{ xs: 1, md: 3 }}>
          <Descriptions.Item label="有效区间">
            {formatDate(meta.effective_start_date)} ~ {formatDate(meta.effective_end_date)}
          </Descriptions.Item>
          <Descriptions.Item label="交易日数">{meta.trading_days ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="信号日数">{meta.signal_days ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="Sharpe">{formatNumber(metrics.sharpe_ratio, 3)}</Descriptions.Item>
          <Descriptions.Item label="Calmar">{formatNumber(metrics.calmar_ratio, 3)}</Descriptions.Item>
          <Descriptions.Item label="交易笔数">{metrics.trade_count ?? detail?.summary?.trade_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="最新信号日">{formatDate(latestSignal?.date)}</Descriptions.Item>
          <Descriptions.Item label="当前信号" span={2}>
            {(latestSignal?.selected_symbols || []).map((symbol, index) => (
              <Tag key={symbol} color="blue">
                {formatSymbolLabel(symbol)} {formatPercent((latestSignal?.target_weights_pct || [])[index])}
              </Tag>
            ))}
          </Descriptions.Item>
        </Descriptions>
        {latestSignal?.ranking?.length ? (
          <Table
            size="small"
            rowKey="symbol"
            columns={[
              { title: '排名', dataIndex: 'rank', key: 'rank', width: 80 },
              { title: '标的', dataIndex: 'symbol', key: 'symbol', render: formatSymbolLabel },
              { title: '风险调整分数', dataIndex: 'risk_adjusted_score', key: 'risk_adjusted_score', width: 130, render: value => formatNumber(value, 2) },
              { title: '20日收益', dataIndex: 'window_return_pct', key: 'window_return_pct', width: 110, render: value => formatPercent(value) },
              { title: '年化波动', dataIndex: 'annualized_volatility_pct', key: 'annualized_volatility_pct', width: 110, render: value => formatPercent(value) },
            ]}
            dataSource={latestSignal.ranking.slice(0, 12)}
            pagination={false}
          />
        ) : null}
      </Space>
    );
  };

  const renderCharts = () => {
    if (!detail?.equity_curve?.length) return <Empty description="同步后查看净值曲线" />;
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={16}>
        {equityOption && <ReactECharts option={equityOption} style={{ height: 360 }} />}
        {drawdownOption && <ReactECharts option={drawdownOption} style={{ height: 300 }} />}
      </Space>
    );
  };

  const renderRecords = () => (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Table
        title={() => '最新持仓'}
        rowKey="symbol"
        columns={holdingColumns}
        dataSource={detail?.holdings || []}
        pagination={false}
        scroll={{ x: 760 }}
      />
      <Table
        title={() => '模拟成交'}
        rowKey="id"
        columns={tradeColumns}
        dataSource={detail?.trades || []}
        pagination={{ pageSize: 10 }}
        scroll={{ x: 2200 }}
      />
    </Space>
  );

  const renderAnalysis = () => (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Table
        title={() => '分年表现'}
        rowKey="year"
        columns={annualColumns}
        dataSource={detail?.summary?.annual_performance || []}
        pagination={false}
        scroll={{ x: 900 }}
      />
      <Table
        title={() => '基准表现'}
        rowKey="symbol"
        columns={benchmarkColumns}
        dataSource={detail?.summary?.benchmark_metrics || []}
        pagination={false}
        scroll={{ x: 900 }}
      />
      <Table
        title={() => '标的表现'}
        rowKey="symbol"
        columns={symbolTradeColumns}
        dataSource={detail?.summary?.symbol_trade_stats || []}
        pagination={false}
        scroll={{ x: 860 }}
      />
    </Space>
  );

  const renderLogs = () => (
    <Table
      rowKey="id"
      columns={logColumns}
      dataSource={detail?.logs || []}
      pagination={{ pageSize: 12 }}
      scroll={{ x: 900 }}
    />
  );

  const renderList = () => (
    <Card>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <Title level={4} style={{ margin: 0 }}>W20 风险调整动量虚拟盘</Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchConfigs} loading={listLoading}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增配置
          </Button>
        </Space>
      </Space>
      <Table
        columns={configColumns}
        dataSource={configs}
        rowKey="id"
        loading={listLoading}
        pagination={{ pageSize: 10 }}
        scroll={{ x: 1500 }}
        onRow={(record) => ({
          onClick: () => openConfig(record),
          style: { cursor: 'pointer' },
        })}
      />
    </Card>
  );

  const renderDetail = () => (
    <>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={returnToList}>
            返回列表
          </Button>
          <Title level={4} style={{ margin: 0 }}>
            {selectedConfig?.id ? selectedConfig.name : '新增 W20 虚拟盘'}
          </Title>
        </Space>
        <Space wrap>
          {selectedConfig?.id && (
            <Text type="secondary">
              {formatDateTime(selectedConfig.last_sync_at)} / {selectedConfig.last_sync_status || '-'}
            </Text>
          )}
          {selectedConfig?.id && (
            <Button
              icon={<PlayCircleOutlined />}
              onClick={() => handleSync(selectedConfig)}
              loading={syncLoadingId === selectedConfig.id}
            >
              同步虚拟盘
            </Button>
          )}
        </Space>
      </Space>
      <Card loading={detailLoading && activeTab !== 'config'}>
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: 'overview',
              label: <span><FundProjectionScreenOutlined />概览</span>,
              disabled: !selectedConfig?.id,
              children: renderOverview(),
            },
            {
              key: 'config',
              label: <span><SettingOutlined />策略配置</span>,
              children: renderConfigForm(),
            },
            {
              key: 'charts',
              label: <span><LineChartOutlined />净值曲线</span>,
              disabled: !selectedConfig?.id,
              children: renderCharts(),
            },
            {
              key: 'records',
              label: <span><UnorderedListOutlined />交易与持仓</span>,
              disabled: !selectedConfig?.id,
              children: renderRecords(),
            },
            {
              key: 'analysis',
              label: <span><FundProjectionScreenOutlined />年度与基准</span>,
              disabled: !selectedConfig?.id,
              children: renderAnalysis(),
            },
            {
              key: 'logs',
              label: <span><HistoryOutlined />运行日志</span>,
              disabled: !selectedConfig?.id,
              children: renderLogs(),
            },
          ]}
        />
      </Card>
    </>
  );

  return (
    <div style={{ padding: 24 }}>
      {viewMode === 'list' ? renderList() : renderDetail()}
    </div>
  );
};

export default W20MomentumLive;
