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

const DEFAULT_FORM_VALUES = {
  name: '美股风险调整混合动量虚拟盘',
  enabled: true,
  candidate_etfs: ['SPY.US', 'QQQ.US'],
  initial_capital: 100000,
  start_date: dayjs().subtract(3, 'year'),
  min_listing_days: 365,
  momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
  max_positions: 7,
  sell_rank_multiplier: 2,
  index_weight_blend: 0.4,
  slippage_pct: 0.02,
  commission_pct: 0.03,
  auto_sync_enabled: true,
  auto_sync_time: '16:15',
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
  ...DEFAULT_MOMENTUM_WEIGHTS,
  ...(weights || {}),
});

const normalizeConfigForForm = config => {
  const merged = {
    ...DEFAULT_FORM_VALUES,
    ...config,
    start_date: config?.start_date ? dayjs(config.start_date) : DEFAULT_FORM_VALUES.start_date,
  };
  merged.momentum_weights = normalizeMomentumWeights(config?.momentum_weights);
  return merged;
};

const buildPayload = values => ({
  ...values,
  momentum_weights: normalizeMomentumWeights(values.momentum_weights),
  start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : dayjs().format('YYYY-MM-DD'),
});

const USStockSignalLive = () => {
  const [form] = Form.useForm();
  const [configs, setConfigs] = useState([]);
  const [selectedConfig, setSelectedConfig] = useState(null);
  const [detail, setDetail] = useState(null);
  const [candidateEtfs, setCandidateEtfs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const loadCandidateEtfs = useCallback(async () => {
    try {
      const { data } = await request.get('/api/us-stock-signal-live/candidate-etfs');
      setCandidateEtfs(data || []);
    } catch (error) {
      message.error(getErrorMessage(error, '加载候选ETF失败'));
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
    loadCandidateEtfs();
    loadConfigs();
  }, [loadCandidateEtfs, loadConfigs]);

  useEffect(() => {
    if (selectedConfig?.id) {
      loadDetail(selectedConfig.id);
      form.setFieldsValue(normalizeConfigForForm(selectedConfig));
    }
  }, [selectedConfig, loadDetail, form]);

  const handleNew = () => {
    setSelectedConfig(null);
    setDetail(null);
    form.setFieldsValue(DEFAULT_FORM_VALUES);
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
      form.setFieldsValue(DEFAULT_FORM_VALUES);
      await loadConfigs();
    } catch (error) {
      message.error(getErrorMessage(error, '删除虚拟盘配置失败'));
    }
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
    { title: '股数', dataIndex: 'quantity', width: 90, align: 'right' },
    { title: '金额', dataIndex: 'amount', width: 120, align: 'right', render: formatMoney },
    { title: '佣金', dataIndex: 'commission', width: 90, align: 'right', render: value => formatNumber(value, 2) },
    { title: '收益', dataIndex: 'profit', width: 100, align: 'right', render: value => value === null || value === undefined ? '-' : formatMoney(value) },
    { title: '收益率', dataIndex: 'profit_pct', width: 90, align: 'right', render: formatPercent },
    { title: '成交来源', dataIndex: 'price_source', width: 100 },
    { title: '原因', dataIndex: 'reason_detail', width: 260, ellipsis: true },
  ];

  const eventColumns = [
    { title: '日期', dataIndex: 'date', width: 110 },
    {
      title: '类型',
      dataIndex: 'direction',
      width: 80,
      render: value => <Tag color={value === 'RANK' ? 'blue' : value === 'BUY' ? 'red' : 'green'}>{value}</Tag>,
    },
    { title: '股票', dataIndex: 'symbol', width: 100 },
    {
      title: '排名',
      dataIndex: ['payload', 'rank'],
      width: 80,
      align: 'right',
    },
    { title: '价格', dataIndex: 'signal_price', width: 90, align: 'right', render: value => formatNumber(value, 2) },
    { title: '成交额', dataIndex: 'turnover', width: 130, align: 'right', render: formatMoney },
    { title: '排名分数', dataIndex: ['payload', 'rank_score'], width: 100, align: 'right', render: formatScore },
    { title: '动量分数', dataIndex: 'threshold_pct', width: 100, align: 'right', render: formatScore },
    { title: '权重', dataIndex: ['payload', 'index_weight_pct'], width: 90, align: 'right', render: formatPercent },
    { title: '20日分数', dataIndex: ['payload', 'components', '20', 'risk_adjusted_score'], width: 100, align: 'right', render: formatScore },
    { title: '60日分数', dataIndex: ['payload', 'components', '60', 'risk_adjusted_score'], width: 100, align: 'right', render: formatScore },
    { title: '120日分数', dataIndex: ['payload', 'components', '120', 'risk_adjusted_score'], width: 110, align: 'right', render: formatScore },
    { title: '混合波动', dataIndex: 'annualized_volatility_pct', width: 110, align: 'right', render: formatPercent },
    {
      title: '混合涨跌',
      dataIndex: ['payload', 'window_return_pct'],
      width: 100,
      align: 'right',
      render: formatPercent,
    },
    {
      title: '混合R²',
      dataIndex: ['payload', 'r_squared'],
      width: 80,
      align: 'right',
      render: value => value === null || value === undefined ? '-' : Number(value).toFixed(3),
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

  return (
    <div style={{ padding: 24 }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={4} style={{ margin: 0 }}>美股风险调整混合动量虚拟盘</Title>
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
        <Col xs={24} lg={7}>
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
            <Form form={form} layout="vertical" initialValues={DEFAULT_FORM_VALUES}>
              <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="candidate_etfs" label="候选ETF" rules={[{ required: true }]}>
                <Select
                  mode="multiple"
                  options={(candidateEtfs.length ? candidateEtfs : [
                    { label: '标普500', value: 'SPY.US' },
                    { label: '纳指100', value: 'QQQ.US' },
                  ]).map(item => ({ label: `${item.label} (${item.value})`, value: item.value }))}
                />
              </Form.Item>
              <Descriptions size="small" column={1} style={{ marginBottom: 16 }}>
                <Descriptions.Item label="策略">风险调整混合动量 Top N</Descriptions.Item>
                <Descriptions.Item label="轮换规则">持仓跌出卖出排名才卖出</Descriptions.Item>
                <Descriptions.Item label="买入规则">现金等分补位新票</Descriptions.Item>
                <Descriptions.Item label="排名规则">混合动量 + 成分权重倾斜</Descriptions.Item>
                <Descriptions.Item label="检查频率">每周最后一个交易日</Descriptions.Item>
                <Descriptions.Item label="执行方式">收盘出信号，次日开盘成交</Descriptions.Item>
              </Descriptions>
              <Row gutter={12}>
                <Col span={8}>
                  <Form.Item name={['momentum_weights', '20']} label="20日权重" rules={[{ required: true }]}>
                    <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name={['momentum_weights', '60']} label="60日权重" rules={[{ required: true }]}>
                    <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name={['momentum_weights', '120']} label="120日权重" rules={[{ required: true }]}>
                    <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name="start_date" label="开始日期" rules={[{ required: true }]}>
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="initial_capital" label="初始资金" rules={[{ required: true }]}>
                    <InputNumber min={1} step={10000} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="max_positions" label="最大持仓数" rules={[{ required: true }]}>
                    <InputNumber min={1} max={50} step={1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="sell_rank_multiplier" label="卖出排名倍数" rules={[{ required: true }]}>
                    <InputNumber min={1} max={10} step={0.1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="index_weight_blend" label="成分权重倾斜" rules={[{ required: true }]}>
                    <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="min_listing_days" label="最少上市天数" rules={[{ required: true }]}>
                    <InputNumber min={0} max={3650} step={30} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="slippage_pct" label="滑点(%)" rules={[{ required: true }]}>
                    <InputNumber min={0} max={10} step={0.01} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item name="commission_pct" label="佣金(%)" rules={[{ required: true }]}>
                    <InputNumber min={0} max={10} step={0.01} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="auto_sync_time" label="自动同步时间(ET)" rules={[{ required: true }]}>
                    <Input />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name="auto_sync_enabled" label="自动同步" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Form>
          </Card>
        </Col>

        <Col xs={24} lg={17}>
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
                    <Descriptions.Item label="周度检查">{metrics.rebalance_count ?? '-'}</Descriptions.Item>
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
                        children: <Table rowKey="id" size="small" columns={tradeColumns} dataSource={detail?.trades || []} pagination={{ pageSize: 20 }} scroll={{ x: 1450 }} />,
                      },
                      {
                        key: 'events',
                        label: '排名',
                        children: <Table rowKey="id" size="small" columns={eventColumns} dataSource={detail?.events || []} pagination={{ pageSize: 20 }} scroll={{ x: 1450 }} />,
                      },
                      {
                        key: 'logs',
                        label: '日志',
                        children: <Table rowKey="id" size="small" columns={logColumns} dataSource={detail?.logs || []} pagination={{ pageSize: 20 }} scroll={{ x: 760 }} />,
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
