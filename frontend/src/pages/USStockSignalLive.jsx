import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  message,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import { DeleteOutlined, PlusOutlined, SaveOutlined, SyncOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';
import { formatNumber } from '../utils/format';

const { Text, Title } = Typography;
const DEFAULT_MOMENTUM_WEIGHTS = { 20: 0.05, 60: 0.2, 120: 0.75 };
const MOMENTUM_WEIGHT_WINDOWS = [20, 60, 120];
const MIXED_WINDOW_KEY = 'mixed';
const DEFAULT_VIRTUAL_LEGS = [
  {
    factor: 'risk_adjusted_momentum',
    window: MIXED_WINDOW_KEY,
    weight: 0.6,
    neutralization: 'none',
    standardization: 'rank_percentile',
    momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
  },
  {
    factor: 'index_weight',
    window: 20,
    weight: 0.4,
    neutralization: 'none',
    standardization: 'rank_percentile',
    momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
  },
];
const POOL_OPTIONS = [
  { label: 'SPY+QQQ', value: 'SPY_QQQ', etfs: ['SPY.US', 'QQQ.US'] },
  { label: 'QQQ', value: 'QQQ', etfs: ['QQQ.US'] },
  { label: 'SPY', value: 'SPY', etfs: ['SPY.US'] },
];
const FALLBACK_FACTOR_OPTIONS = [
  {
    key: 'risk_adjusted_momentum',
    label: '动量：风险调整动量',
    group: '动量',
    default_windows: [20, 60, 120],
    supports_windows: true,
    supports_mixed_windows: true,
  },
  {
    key: 'raw_momentum',
    label: '动量：原始动量',
    group: '动量',
    default_windows: [20, 60, 120],
    supports_windows: true,
    supports_mixed_windows: true,
  },
  {
    key: 'index_weight',
    label: '指数：成分权重',
    group: '指数',
    default_windows: [20],
    supports_windows: false,
    supports_mixed_windows: false,
  },
];

const DEFAULT_FORM_VALUES = {
  name: '美股多因子策略虚拟盘',
  enabled: true,
  pool: 'SPY_QQQ',
  initial_capital: 100000,
  start_date: dayjs('2020-01-02'),
  min_listing_days: 365,
  max_positions: 7,
  sell_rank_multiplier: 2,
  rebalance_frequency: 'weekly',
  slippage_pct: 0.02,
  commission_pct: 0.03,
  lot_size: 1,
  legs: DEFAULT_VIRTUAL_LEGS,
  auto_sync_enabled: true,
  auto_sync_time: '16:15',
  auto_trade_time: '09:31',
};

const formatPercent = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Number(value).toFixed(2)}%`;
};

const formatMoney = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

const formatScore = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toFixed(2);
};

const getErrorMessage = (error, fallback) => (
  error?.response?.data?.detail
  || error?.response?.data?.message
  || error?.message
  || fallback
);

const normalizeMomentumWeights = weights => ({
  ...MOMENTUM_WEIGHT_WINDOWS.reduce((acc, window) => {
    const rawValue = weights?.[String(window)] ?? weights?.[window] ?? DEFAULT_MOMENTUM_WEIGHTS[window] ?? 0;
    const numberValue = Number(rawValue);
    acc[String(window)] = Number.isFinite(numberValue) ? Math.max(0, numberValue) : 0;
    return acc;
  }, {}),
});

const cloneLeg = leg => ({
  ...leg,
  neutralization: leg.neutralization || 'none',
  standardization: leg.standardization || 'rank_percentile',
  momentum_weights: normalizeMomentumWeights(leg.momentum_weights),
});

const cloneDefaultFormValues = () => ({
  ...DEFAULT_FORM_VALUES,
  legs: DEFAULT_VIRTUAL_LEGS.map(cloneLeg),
});

const isMixedWindow = value => String(value).toLowerCase() === MIXED_WINDOW_KEY;

const getEtfsForPool = pool => (
  POOL_OPTIONS.find(item => item.value === pool)?.etfs || POOL_OPTIONS[0].etfs
);

const getPoolFromCandidateEtfs = candidateEtfs => {
  const normalized = [...(candidateEtfs || [])].sort().join(',');
  const matched = POOL_OPTIONS.find(item => [...item.etfs].sort().join(',') === normalized);
  return matched?.value || 'SPY_QQQ';
};

const getFactorByKey = (factors, key) => (
  (factors || FALLBACK_FACTOR_OPTIONS).find(item => item.key === key)
);

const getDefaultWindowForFactor = factor => {
  if (!factor?.supports_windows) return factor?.default_windows?.[0] || 20;
  if (factor.supports_mixed_windows) return MIXED_WINDOW_KEY;
  return factor.default_windows?.[0] || 20;
};

const getWindowOptionsForFactor = (factor, baseWindows = [20, 60, 120]) => {
  const numericWindows = factor?.supports_windows
    ? (factor.default_windows?.length ? factor.default_windows : baseWindows)
    : (factor?.default_windows || [20]);
  const options = numericWindows.map(item => ({ label: `${item}日`, value: item }));
  if (factor?.supports_mixed_windows) {
    options.push({ label: '多窗口合成', value: MIXED_WINDOW_KEY });
  }
  return options;
};

const buildFactorSelectOptions = factors => {
  const groups = {};
  (factors?.length ? factors : FALLBACK_FACTOR_OPTIONS).forEach(factor => {
    const group = factor.group || '因子';
    if (!groups[group]) groups[group] = [];
    groups[group].push({ label: factor.label, value: factor.key });
  });
  return Object.entries(groups).map(([label, options]) => ({ label, options }));
};

const buildDefaultLeg = (factorKey = 'raw_momentum', factor = null) => ({
  factor: factorKey,
  window: factor ? getDefaultWindowForFactor(factor) : 120,
  weight: 1,
  neutralization: 'none',
  standardization: 'rank_percentile',
  momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
});

const normalizeLegs = legs => (
  (legs?.length ? legs : DEFAULT_VIRTUAL_LEGS)
    .filter(leg => leg?.factor)
    .map(leg => ({
      factor: leg.factor,
      window: isMixedWindow(leg.window) ? MIXED_WINDOW_KEY : Number(leg.window || 20),
      weight: Number(leg.weight || 0),
      neutralization: leg.neutralization || 'none',
      standardization: leg.standardization || 'rank_percentile',
      momentum_weights: normalizeMomentumWeights(leg.momentum_weights),
    }))
);

const normalizeConfigForForm = config => {
  const defaults = cloneDefaultFormValues();
  const merged = {
    ...defaults,
    ...config,
    pool: getPoolFromCandidateEtfs(config?.candidate_etfs || getEtfsForPool(defaults.pool)),
    start_date: config?.start_date ? dayjs(config.start_date) : defaults.start_date,
  };
  merged.legs = (config?.legs?.length ? config.legs : defaults.legs).map(cloneLeg);
  return merged;
};

const buildPayload = values => {
  const legs = normalizeLegs(values.legs);
  const { pool, ...rest } = values;
  return {
    ...rest,
    candidate_etfs: getEtfsForPool(pool),
    start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : DEFAULT_FORM_VALUES.start_date.format('YYYY-MM-DD'),
    lot_size: Number(values.lot_size || DEFAULT_FORM_VALUES.lot_size),
    legs,
  };
};

const USStockSignalLive = () => {
  const [form] = Form.useForm();
  const virtualLegs = Form.useWatch('legs', form);
  const [configs, setConfigs] = useState([]);
  const [selectedConfig, setSelectedConfig] = useState(null);
  const [detail, setDetail] = useState(null);
  const [factorOptions, setFactorOptions] = useState(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const loadFactorOptions = useCallback(async () => {
    try {
      const { data } = await request.get('/api/us-stock-signal-live/factor-options');
      setFactorOptions(data || null);
    } catch (error) {
      setFactorOptions({ factors: FALLBACK_FACTOR_OPTIONS });
      message.error(getErrorMessage(error, '加载因子选项失败'));
    }
  }, []);

  const loadConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await request.get('/api/us-stock-signal-live/configs');
      setConfigs(data || []);
      if (!selectedConfig && data?.length) {
        setSelectedConfig(data[0]);
        form.setFieldsValue(normalizeConfigForForm(data[0]));
      }
    } catch (error) {
      message.error(getErrorMessage(error, '加载虚拟盘配置失败'));
    } finally {
      setLoading(false);
    }
  }, [form, selectedConfig]);

  const loadDetail = useCallback(async (configId) => {
    if (!configId) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    try {
      const { data } = await request.get(`/api/us-stock-signal-live/configs/${configId}/detail`);
      setDetail(data);
    } catch (error) {
      message.error(getErrorMessage(error, '加载虚拟盘详情失败'));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    loadFactorOptions();
    loadConfigs();
  }, [loadFactorOptions, loadConfigs]);

  useEffect(() => {
    if (selectedConfig?.id) {
      loadDetail(selectedConfig.id);
      form.setFieldsValue(normalizeConfigForForm(selectedConfig));
    }
  }, [selectedConfig, loadDetail, form]);

  const handleNew = () => {
    setSelectedConfig(null);
    setDetail(null);
    form.setFieldsValue(cloneDefaultFormValues());
  };

  const handleSelectConfig = (config) => {
    setSelectedConfig(config);
    form.setFieldsValue(normalizeConfigForForm(config));
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      const payload = buildPayload(values);
      setSaving(true);
      const { data } = selectedConfig?.id
        ? await request.put(`/api/us-stock-signal-live/configs/${selectedConfig.id}`, payload)
        : await request.post('/api/us-stock-signal-live/configs', payload);
      message.success('虚拟盘配置已保存');
      setSelectedConfig(data);
      await loadConfigs();
    } catch (error) {
      if (!error?.errorFields) {
        message.error(getErrorMessage(error, '保存虚拟盘配置失败'));
      }
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedConfig?.id) return;
    try {
      await request.delete(`/api/us-stock-signal-live/configs/${selectedConfig.id}`);
      message.success('虚拟盘配置已删除');
      setSelectedConfig(null);
      setDetail(null);
      form.setFieldsValue(cloneDefaultFormValues());
      await loadConfigs();
    } catch (error) {
      message.error(getErrorMessage(error, '删除虚拟盘配置失败'));
    }
  };

  const factorSelectOptions = useMemo(() => buildFactorSelectOptions(factorOptions?.factors), [factorOptions]);
  const neutralizationOptions = useMemo(() => (
    (factorOptions?.neutralization_options || [
      { key: 'none', label: '不做中性化' },
      { key: 'sector', label: '行业大类中性化（Sector）' },
      { key: 'sector_market_cap', label: '行业大类+市值中性化' },
      { key: 'fine_industry', label: '细行业中性化（Industry，小样本回退Sector）' },
      { key: 'fine_industry_market_cap', label: '细行业+市值中性化（小样本回退Sector）' },
    ]).map(item => ({ label: item.label, value: item.key }))
  ), [factorOptions]);
  const standardizationOptions = useMemo(() => (
    (factorOptions?.standardization_options || [
      { key: 'none', label: '不标准化' },
      { key: 'zscore', label: '截面 Z-Score' },
      { key: 'rank_percentile', label: '截面排名分位' },
    ]).map(item => ({ label: item.label, value: item.key }))
  ), [factorOptions]);
  const rebalanceFrequencyOptions = useMemo(() => ([
    { label: '每日', value: 'daily' },
    { label: '每周', value: 'weekly' },
    { label: '每月', value: 'monthly' },
    { label: '季度', value: 'quarterly' },
    { label: '半年', value: 'semiannual' },
  ]), []);

  const handleLegFactorChange = (index, value) => {
    const factor = getFactorByKey(factorOptions?.factors, value);
    const legs = [...(form.getFieldValue('legs') || [])];
    legs[index] = {
      ...(legs[index] || {}),
      factor: value,
      window: getDefaultWindowForFactor(factor),
      momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
    };
    form.setFieldsValue({ legs });
  };

  const handleSync = async () => {
    if (!selectedConfig?.id) {
      message.warning('请先保存配置');
      return;
    }
    setSyncing(true);
    try {
      await request.post(`/api/us-stock-signal-live/configs/${selectedConfig.id}/sync`, null, { timeout: 600000 });
      message.success('虚拟盘同步完成');
      await loadConfigs();
      await loadDetail(selectedConfig.id);
    } catch (error) {
      message.error(getErrorMessage(error, '同步虚拟盘失败'));
    } finally {
      setSyncing(false);
    }
  };

  const chartOption = useMemo(() => {
    const equity = detail?.equity_curve || [];
    const benchmarkCurve = detail?.benchmark_curve || [];
    const benchmarkByDate = {};
    benchmarkCurve.forEach(item => {
      benchmarkByDate[item.date] = item.values || {};
    });
    const benchmarkSymbols = selectedConfig?.candidate_etfs || [];
    const dates = equity.map(item => item.date);
    const benchmarkSeries = benchmarkSymbols.map(symbol => ({
      name: `${symbol}基准`,
      type: 'line',
      data: dates.map(date => benchmarkByDate[date]?.[symbol] ?? null),
      showSymbol: false,
    }));
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['净值', ...benchmarkSeries.map(item => item.name), '回撤'] },
      grid: { left: 56, right: 56, top: 48, bottom: 56 },
      xAxis: { type: 'category', data: dates },
      yAxis: [
        { type: 'value', scale: true },
        { type: 'value', scale: true, axisLabel: { formatter: '{value}%' } },
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 24 }],
      series: [
        { name: '净值', type: 'line', data: equity.map(item => item.value), showSymbol: false },
        ...benchmarkSeries,
        { name: '回撤', type: 'line', yAxisIndex: 1, data: equity.map(item => item.drawdown), showSymbol: false },
      ],
    };
  }, [detail, selectedConfig]);

  const holdingColumns = [
    { title: '股票', dataIndex: 'symbol', width: 110 },
    { title: '股数', dataIndex: 'shares', width: 90, align: 'right' },
    { title: '现价', dataIndex: 'price', width: 100, align: 'right', render: value => formatNumber(value, 2) },
    { title: '成本', dataIndex: 'avg_cost', width: 100, align: 'right', render: value => formatNumber(value, 2) },
    { title: '市值', dataIndex: 'market_value', width: 120, align: 'right', render: formatMoney },
    { title: '权重', dataIndex: 'actual_weight_pct', width: 90, align: 'right', render: formatPercent },
    { title: '买入日', dataIndex: 'entry_date', width: 120 },
  ];

  const tradeColumns = [
    { title: '成交日', dataIndex: 'date', width: 110 },
    { title: '信号日', dataIndex: 'signal_date', width: 110 },
    {
      title: '方向',
      dataIndex: 'action',
      width: 80,
      render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag>,
    },
    { title: '股票', dataIndex: 'symbol', width: 100 },
    { title: '价格', dataIndex: 'price', width: 90, align: 'right', render: value => formatNumber(value, 2) },
    { title: '原始成交价', dataIndex: 'execution_price', width: 110, align: 'right', render: value => formatNumber(value, 2) },
    { title: '股数', dataIndex: 'quantity', width: 90, align: 'right' },
    { title: '金额', dataIndex: 'amount', width: 120, align: 'right', render: formatMoney },
    { title: '佣金', dataIndex: 'commission', width: 90, align: 'right', render: value => formatNumber(value, 2) },
    { title: '收益', dataIndex: 'profit', width: 100, align: 'right', render: value => value === null || value === undefined ? '-' : formatMoney(value) },
    { title: '收益率', dataIndex: 'profit_pct', width: 90, align: 'right', render: formatPercent },
    { title: '成交来源', dataIndex: 'price_source', width: 100 },
    { title: '报价时间', dataIndex: 'quote_timestamp', width: 170, render: value => value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-' },
    { title: '原因', dataIndex: 'reason_detail', width: 260, ellipsis: true },
  ];

  const eventColumns = [
    { title: '日期', dataIndex: 'date', width: 80 },
    { title: '股票', dataIndex: 'symbol', width: 80 },
    {
      title: '状态',
      dataIndex: 'payload',
      width: 150,
      render: value => (
        <Space size={[4, 4]} wrap>
          {value?.is_holding && <Tag color="gold">持仓</Tag>}
          {value?.is_selected && <Tag color="green">TopN</Tag>}
          {value?.in_sell_rank_threshold && <Tag color="blue">线内持有</Tag>}
          {value?.is_holding && !value?.in_sell_rank_threshold && <Tag color="red">卖出</Tag>}
        </Space>
      ),
    },
    {
      title: '排名',
      dataIndex: ['payload', 'rank'],
      width: 50,
      align: 'right',
      render: value => value ?? '-',
    },
    { title: '价格', dataIndex: 'signal_price', width: 80, align: 'right', render: value => formatNumber(value, 2) },
    { title: '成交额', dataIndex: 'turnover', width: 100, align: 'right', render: formatMoney },
    { title: '排名分数', dataIndex: ['payload', 'rank_score'], width: 50, align: 'right', render: formatScore },
    {
      title: '因子明细',
      dataIndex: ['payload', 'component_scores'],
      width: 360,
      render: value => {
        const items = Object.values(value || {});
        if (!items.length) return '-';
        return (
          <Space size={[4, 4]} wrap>
            {items.map(item => (
              <Tag key={item.component_key || `${item.factor}-${item.window}`}>
                {item.factor_label || item.factor}: {formatScore(item.score)}
              </Tag>
            ))}
          </Space>
        );
      },
    },
  ];

  const logColumns = [
    { title: '时间', dataIndex: 'timestamp', width: 180, render: value => value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-' },
    { title: '级别', dataIndex: 'level', width: 80 },
    { title: '动作', dataIndex: 'action', width: 110 },
    { title: '消息', dataIndex: 'message', ellipsis: true },
  ];

  const summary = detail?.summary || {};
  const metrics = summary.metrics || {};
  const yearlyStats = detail?.yearly_stats || summary.yearly_stats || [];
  const eventRows = useMemo(() => (
    [...(detail?.events || [])].sort((left, right) => {
      const dateCompare = String(right.date || '').localeCompare(String(left.date || ''));
      if (dateCompare !== 0) return dateCompare;
      const leftRank = left.payload?.rank ?? Number.MAX_SAFE_INTEGER;
      const rightRank = right.payload?.rank ?? Number.MAX_SAFE_INTEGER;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return String(left.symbol || '').localeCompare(String(right.symbol || ''));
    })
  ), [detail]);
  const yearlyColumns = useMemo(() => {
    const benchmarkSymbols = selectedConfig?.candidate_etfs || [];
    const primarySymbol = benchmarkSymbols[0];
    return [
      { title: '年份', dataIndex: 'year', width: 90 },
      { title: '区间', width: 210, render: (_, row) => `${row.start_date || '-'} ~ ${row.end_date || '-'}` },
      { title: '策略收益', dataIndex: 'strategy_return_pct', width: 110, align: 'right', render: formatPercent },
      ...benchmarkSymbols.map(symbol => ({
        title: `${symbol}基准`,
        dataIndex: ['benchmark_returns_pct', symbol],
        width: 120,
        align: 'right',
        render: formatPercent,
      })),
      {
        title: primarySymbol ? `超额${primarySymbol}` : '超额',
        dataIndex: ['excess_returns_pct', primarySymbol],
        width: 120,
        align: 'right',
        render: formatPercent,
      },
      {
        title: '跑赢全部',
        dataIndex: 'outperformed_all',
        width: 100,
        render: value => (
          value === null || value === undefined
            ? '-'
            : <Tag color={value ? 'green' : 'red'}>{value ? '是' : '否'}</Tag>
        ),
      },
    ];
  }, [selectedConfig]);

  return (
    <div style={{ padding: 24 }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={4} style={{ margin: 0 }}>美股多因子策略虚拟盘</Title>
        </Col>
        <Col>
          <Space>
            <Button icon={<PlusOutlined />} onClick={handleNew}>新建</Button>
            <Button icon={<SaveOutlined />} type="primary" loading={saving} onClick={handleSave}>保存配置</Button>
            <Button icon={<SyncOutlined />} loading={syncing} onClick={handleSync}>同步虚拟盘</Button>
            {selectedConfig?.id && (
              <Popconfirm title="确认删除这条虚拟盘配置和对应记录吗？" onConfirm={handleDelete}>
                <Button icon={<DeleteOutlined />} danger />
              </Popconfirm>
            )}
          </Space>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={9}>
          <Card title="配置" style={{ marginBottom: 16 }}>
            <Spin spinning={loading}>
              <List
                dataSource={configs}
                locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无配置" /> }}
                renderItem={item => (
                  <List.Item
                    onClick={() => handleSelectConfig(item)}
                    style={{
                      cursor: 'pointer',
                      background: selectedConfig?.id === item.id ? '#f0f5ff' : undefined,
                      padding: '12px 8px',
                    }}
                  >
                    <List.Item.Meta
                      title={<Space><Text strong>{item.name}</Text>{item.enabled ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>}</Space>}
                      description={
                        <Space direction="vertical" size={2}>
                          <Text type="secondary">最新：{item.runtime?.latest_date || '-'}</Text>
                          <Text type="secondary">净值：{formatMoney(item.runtime?.portfolio_value)}</Text>
                          <Text type="secondary">收益：{formatPercent(item.runtime?.total_return)}</Text>
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
            </Spin>
          </Card>

          <Card title={selectedConfig?.id ? '参数' : '新增参数'}>
            <Form form={form} layout="vertical" initialValues={cloneDefaultFormValues()}>
              <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="pool" label="股票池" rules={[{ required: true }]}>
                <Select
                  options={POOL_OPTIONS.map(item => ({ label: item.label, value: item.value }))}
                />
              </Form.Item>
              <Row gutter={12}>
                <Col span={24}>
                  <Form.Item name="start_date" label="开始日期" rules={[{ required: true }]}>
                    <DatePicker style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="initial_capital" label="初始资金" rules={[{ required: true }]}>
                    <InputNumber min={1} step={10000} precision={2} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="max_positions" label="持仓数" rules={[{ required: true }]}>
                    <InputNumber min={1} max={100} step={1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="sell_rank_multiplier" label="卖出倍数" rules={[{ required: true }]}>
                    <InputNumber min={1} max={10} step={0.1} precision={2} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="rebalance_frequency" label="调仓频率" rules={[{ required: true }]}>
                    <Select options={rebalanceFrequencyOptions} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="commission_pct" label="手续费%" rules={[{ required: true }]}>
                    <InputNumber min={0} max={10} step={0.01} precision={4} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="slippage_pct" label="滑点%" rules={[{ required: true }]}>
                    <InputNumber min={0} max={10} step={0.01} precision={4} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="lot_size" label="交易单位" rules={[{ required: true }]}>
                    <InputNumber min={1} step={1} precision={0} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="min_listing_days" label="上市天数" rules={[{ required: true }]}>
                    <InputNumber min={0} max={3650} step={30} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.List name="legs">
                {(fields, { add, remove }) => (
                  <div style={{ marginBottom: 16 }}>
                    <Row justify="space-between" align="middle" style={{ marginBottom: 8 }}>
                      <Col><Text strong>回测因子</Text></Col>
                      <Col>
                        <Button
                          size="small"
                          icon={<PlusOutlined />}
                          onClick={() => add(buildDefaultLeg('raw_momentum', getFactorByKey(factorOptions?.factors, 'raw_momentum')))}
                        >
                          添加因子
                        </Button>
                      </Col>
                    </Row>
                    {fields.map(field => {
                      const leg = (virtualLegs || [])[field.name] || {};
                      const factor = getFactorByKey(factorOptions?.factors, leg.factor);
                      return (
                        <div
                          key={field.key}
                          style={{
                            border: '1px solid #f0f0f0',
                            borderRadius: 6,
                            padding: 12,
                            marginBottom: 10,
                            background: '#fafafa',
                          }}
                        >
                          <Row gutter={12}>
                            <Col span={24}>
                              <Form.Item
                                {...field}
                                name={[field.name, 'factor']}
                                label="因子"
                                rules={[{ required: true, message: '请选择因子' }]}
                              >
                                <Select
                                  options={factorSelectOptions}
                                  onChange={value => handleLegFactorChange(field.name, value)}
                                />
                              </Form.Item>
                            </Col>
                            <Col span={12}>
                              <Form.Item
                                {...field}
                                name={[field.name, 'window']}
                                label="窗口"
                                rules={[{ required: true, message: '请选择窗口' }]}
                              >
                                <Select
                                  options={getWindowOptionsForFactor(factor, factorOptions?.windows)}
                                  disabled={factor && !factor.supports_windows}
                                />
                              </Form.Item>
                            </Col>
                            <Col span={12}>
                              <Form.Item
                                {...field}
                                name={[field.name, 'weight']}
                                label="权重"
                                rules={[{ required: true, message: '请输入权重' }]}
                              >
                                <InputNumber min={-100} max={100} step={0.1} precision={4} style={{ width: '100%' }} />
                              </Form.Item>
                            </Col>
                            <Col span={12}>
                              <Form.Item
                                {...field}
                                name={[field.name, 'neutralization']}
                                label="中性化"
                                rules={[{ required: true, message: '请选择中性化' }]}
                              >
                                <Select options={neutralizationOptions} />
                              </Form.Item>
                            </Col>
                            <Col span={12}>
                              <Form.Item
                                {...field}
                                name={[field.name, 'standardization']}
                                label="标准化"
                                rules={[{ required: true, message: '请选择标准化' }]}
                              >
                                <Select options={standardizationOptions} />
                              </Form.Item>
                            </Col>
                          </Row>
                          {isMixedWindow(leg.window) && (
                            <Row gutter={12}>
                              {MOMENTUM_WEIGHT_WINDOWS.map(window => (
                                <Col span={8} key={window}>
                                  <Form.Item name={[field.name, 'momentum_weights', String(window)]} label={`${window}日权重`}>
                                    <InputNumber min={0} step={0.05} precision={4} style={{ width: '100%' }} />
                                  </Form.Item>
                                </Col>
                              ))}
                            </Row>
                          )}
                          <Button
                            danger
                            block
                            icon={<DeleteOutlined />}
                            disabled={fields.length <= 1}
                            onClick={() => remove(field.name)}
                          >
                            删除因子
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Form.List>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="auto_sync_time" label="自动同步时间(ET)" rules={[{ required: true }]}>
                    <Input />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="auto_trade_time" label="自动交易时间(ET)" rules={[{ required: true }]}>
                    <Input />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name="auto_sync_enabled" label="开关" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Form>
          </Card>
        </Col>

        <Col xs={24} lg={15}>
          <Spin spinning={detailLoading || syncing}>
            {!selectedConfig?.id ? (
              <Card>
                <Empty description="保存配置后查看虚拟盘" />
              </Card>
            ) : (
              <>
                <Card style={{ marginBottom: 16 }}>
                  <Descriptions column={{ xs: 1, sm: 2, xl: 4 }} size="small">
                    <Descriptions.Item label="最新日期">{summary.latest_date || '-'}</Descriptions.Item>
                    <Descriptions.Item label="当前净值">{formatMoney(summary.portfolio_value)}</Descriptions.Item>
                    <Descriptions.Item label="总收益">{formatPercent(metrics.total_return ?? summary.total_return)}</Descriptions.Item>
                    <Descriptions.Item label="年化收益">{formatPercent(metrics.annualized_return)}</Descriptions.Item>
                    <Descriptions.Item label="最大回撤">{formatPercent(metrics.max_drawdown)}</Descriptions.Item>
                    <Descriptions.Item label="排名记录">{metrics.rank_signal_count ?? metrics.signal_count ?? summary.signal_count ?? '-'}</Descriptions.Item>
                    <Descriptions.Item label="调仓检查">{metrics.rebalance_count ?? '-'}</Descriptions.Item>
                    <Descriptions.Item label="交易数">{metrics.trade_count ?? summary.trade_count ?? '-'}</Descriptions.Item>
                    <Descriptions.Item label="持仓数">{summary.holding_count ?? '-'}</Descriptions.Item>
                  </Descriptions>
                </Card>

                <Card style={{ marginBottom: 16 }}>
                  {(detail?.equity_curve || []).length ? (
                    <ReactECharts option={chartOption} style={{ height: 360 }} />
                  ) : (
                    <Empty description="暂无净值数据" />
                  )}
                </Card>

                <Card>
                  <Tabs
                    items={[
                      {
                        key: 'holdings',
                        label: '持仓',
                        children: <Table rowKey="symbol" size="small" columns={holdingColumns} dataSource={detail?.holdings || []} pagination={false} scroll={{ x: 820 }} />,
                      },
                      {
                        key: 'trades',
                        label: '交易',
                        children: <Table rowKey="id" size="small" columns={tradeColumns} dataSource={detail?.trades || []} pagination={{ defaultPageSize: 20 }} scroll={{ x: 1730 }} />,
                      },
                      {
                        key: 'yearly',
                        label: '分年',
                        children: <Table rowKey="year" size="small" columns={yearlyColumns} dataSource={yearlyStats} pagination={false} scroll={{ x: 960 }} />,
                      },
                      {
                        key: 'events',
                        label: '排名',
                        children: <Table rowKey="id" size="small" columns={eventColumns} dataSource={eventRows} pagination={{ defaultPageSize: 20 }} scroll={{ x: 1580 }} />,
                      },
                      {
                        key: 'logs',
                        label: '日志',
                        children: <Table rowKey="id" size="small" columns={logColumns} dataSource={detail?.logs || []} pagination={{ defaultPageSize: 20 }} scroll={{ x: 760 }} />,
                      },
                    ]}
                  />
                </Card>
              </>
            )}
          </Spin>
        </Col>
      </Row>
    </div>
  );
};

export default USStockSignalLive;
