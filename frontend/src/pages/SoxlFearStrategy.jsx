import React, { useEffect, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import { HistoryOutlined, PlayCircleOutlined, SaveOutlined, SettingOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';

const { Title, Text } = Typography;

const sellReductionBasisOptions = [
  { label: '按总资产', value: 'portfolio' },
  { label: '按持仓股票', value: 'holdings' },
];

const accountTypeOptions = [
  { label: 'Interactive Brokers (IB)', value: 'ib' },
  { label: '长桥证券 (Longport)', value: 'longport' },
];

const defaultValues = {
  enabled: false,
  symbol: 'SOXL.US',
  account_type: 'ib',
  buy_threshold: 40,
  greed_threshold: 41,
  volume_ratio_threshold: 1.38,
  buy_position_pct: 60,
  cooldown_days: 10,
  trailing_stop_pct: 5,
  sell_position_pct: 50,
  sell_reduction_basis: 'portfolio',
  max_take_profit_sells_per_cycle: 2,
  min_position_pct_after_take_profit: 5,
  rebalance_threshold_pct: 5,
};

const SoxlFearStrategy = () => {
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const [configLoading, setConfigLoading] = useState(false);
  const [logLoading, setLogLoading] = useState(false);
  const [manualLoading, setManualLoading] = useState(false);
  const [ibAccounts, setIbAccounts] = useState([]);
  const [longportAccounts, setLongportAccounts] = useState([]);
  const [logs, setLogs] = useState([]);
  const [config, setConfig] = useState(defaultValues);

  useEffect(() => {
    fetchInitialData();
  }, []);

  const fetchInitialData = async () => {
    await Promise.all([fetchConfig(), fetchLogs(), fetchIbAccounts(), fetchLongportAccounts()]);
  };

  const fetchConfig = async () => {
    setConfigLoading(true);
    try {
      const { data } = await request.get('/api/soxl-fear-strategy/config');
      const merged = { ...defaultValues, ...data };
      setConfig(merged);
      form.setFieldsValue(merged);
    } catch (error) {
      message.error(error.response?.data?.detail || '加载策略配置失败');
    } finally {
      setConfigLoading(false);
    }
  };

  const fetchLogs = async () => {
    setLogLoading(true);
    try {
      const { data } = await request.get('/api/soxl-fear-strategy/logs');
      setLogs(data);
    } catch (error) {
      message.error(error.response?.data?.detail || '加载运行日志失败');
    } finally {
      setLogLoading(false);
    }
  };

  const fetchIbAccounts = async () => {
    try {
      const { data } = await request.get('/api/ib-accounts');
      setIbAccounts(data);
    } catch (error) {
      message.error('获取 IB 账户失败');
    }
  };

  const fetchLongportAccounts = async () => {
    try {
      const { data } = await request.get('/api/longport-accounts');
      setLongportAccounts(data);
    } catch (error) {
      message.error('获取长桥账户失败');
    }
  };

  const handleSave = async (values) => {
    setConfigLoading(true);
    try {
      await request.post('/api/soxl-fear-strategy/config', values);
      message.success('策略配置已保存');
      fetchConfig();
    } catch (error) {
      message.error(error.response?.data?.detail || '保存策略配置失败');
    } finally {
      setConfigLoading(false);
    }
  };

  const handleManualRun = async () => {
    setManualLoading(true);
    try {
      await request.post('/api/soxl-fear-strategy/manual-check');
      message.success('已触发一次后台检查，请稍后刷新日志');
      setTimeout(fetchLogs, 3000);
      setTimeout(fetchConfig, 3000);
    } catch (error) {
      message.error(error.response?.data?.detail || '手动执行失败');
    } finally {
      setManualLoading(false);
    }
  };

  const handleBacktest = () => {
    const values = {
      ...defaultValues,
      ...form.getFieldsValue(),
    };
    navigate('/soxl-fear-backtest', {
      state: {
        autoRunBacktest: true,
        presetValues: {
          symbol: values.symbol || 'SOXL.US',
          initial_capital: 100000,
          top_n: 1,
          objective: 'annualized_return',
          eval_workers: 1,
          fit_rebalance_threshold_pct: values.rebalance_threshold_pct,
          buy_threshold_values: String(values.buy_threshold ?? defaultValues.buy_threshold),
          greed_threshold_values: String(values.greed_threshold ?? defaultValues.greed_threshold),
          volume_ratio_threshold_values: String(values.volume_ratio_threshold ?? defaultValues.volume_ratio_threshold),
          buy_position_pct_values: String(values.buy_position_pct ?? defaultValues.buy_position_pct),
          cooldown_days_values: String(values.cooldown_days ?? defaultValues.cooldown_days),
          trailing_stop_pct_values: String(values.trailing_stop_pct ?? defaultValues.trailing_stop_pct),
          sell_position_pct_values: String(values.sell_position_pct ?? defaultValues.sell_position_pct),
          sell_reduction_basis_values: [values.sell_reduction_basis || defaultValues.sell_reduction_basis],
          max_take_profit_sells_per_cycle_values: String(
            values.max_take_profit_sells_per_cycle ?? defaultValues.max_take_profit_sells_per_cycle
          ),
          min_position_pct_after_take_profit_values: String(
            values.min_position_pct_after_take_profit ?? defaultValues.min_position_pct_after_take_profit
          ),
        },
      },
    });
  };

  const logColumns = [
    {
      title: '时间',
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 160,
      render: (value) => dayjs(value).format('YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '来源',
      dataIndex: 'trigger_source',
      key: 'trigger_source',
      width: 90,
      render: (value) => <Tag color={value === 'manual' ? 'gold' : 'blue'}>{value}</Tag>,
    },
    {
      title: '动作',
      dataIndex: 'action',
      key: 'action',
      width: 90,
      render: (value) => {
        let color = 'default';
        if (value === 'BUY') color = 'red';
        if (value === 'SELL') color = 'green';
        if (value === 'ERROR') color = 'error';
        if (value === 'CHECK') color = 'blue';
        return <Tag color={color}>{value}</Tag>;
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value) => <Tag color={value === 'SUCCESS' ? 'success' : value === 'ERROR' ? 'error' : 'default'}>{value}</Tag>,
    },
    {
      title: '价格',
      dataIndex: 'price',
      key: 'price',
      width: 90,
      render: (value) => (value ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '数量',
      dataIndex: 'quantity',
      key: 'quantity',
      width: 80,
      render: (value) => value || '-',
    },
    {
      title: 'CNN',
      dataIndex: 'cnn_index_value',
      key: 'cnn_index_value',
      width: 80,
      render: (value) => (value !== null && value !== undefined ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '信号分(CNN)',
      dataIndex: 'fear_score',
      key: 'fear_score',
      width: 90,
      render: (value) => (value !== null && value !== undefined ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '投影量比',
      dataIndex: 'volume_ratio',
      key: 'volume_ratio',
      width: 80,
      render: (value) => (value !== null && value !== undefined ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '详情',
      dataIndex: 'message',
      key: 'message',
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Title level={4} style={{ marginTop: 0 }}>SOXL 情绪量能自动交易</Title>
      <Text type="secondary" style={{ display: 'block', marginBottom: 16 }}>
        每个美股交易日收盘前自动执行一次。系统会抓取最新 CNN 恐贪指数，结合 SOXL 最近日线与当日实时行情判断是否买卖；临近收盘时量比会按预计全天成交量校正。
      </Text>

      <Tabs
        defaultActiveKey="config"
        items={[
          {
            key: 'config',
            label: <span><SettingOutlined />策略配置</span>,
            children: (
              <Card loading={configLoading}>
                <Form
                  form={form}
                  layout="vertical"
                  initialValues={defaultValues}
                  onFinish={handleSave}
                >
                  <Form.Item name="enabled" label="启用策略" valuePropName="checked">
                    <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                  </Form.Item>

                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item name="symbol" label="交易标的">
                        <Input disabled />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="account_type" label="账户类型" rules={[{ required: true, message: '请选择账户类型' }]}>
                        <Select
                          options={accountTypeOptions}
                          onChange={() => form.setFieldsValue({ ib_account_id: undefined, longport_account_id: undefined })}
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item shouldUpdate={(prev, curr) => prev.account_type !== curr.account_type || prev.enabled !== curr.enabled}>
                        {() => {
                          const accountType = form.getFieldValue('account_type');
                          const enabled = form.getFieldValue('enabled');
                          if (accountType === 'longport') {
                            return (
                              <Form.Item name="longport_account_id" label="长桥账户" rules={enabled ? [{ required: true, message: '请选择长桥账户' }] : []}>
                                <Select placeholder="选择长桥账户">
                                  {longportAccounts.map((account) => (
                                    <Select.Option key={account.lp_account_id} value={account.lp_account_id}>
                                      {account.name} (ID: {account.lp_account_id})
                                    </Select.Option>
                                  ))}
                                </Select>
                              </Form.Item>
                            );
                          }
                          return (
                            <Form.Item name="ib_account_id" label="IB 账户" rules={enabled ? [{ required: true, message: '请选择 IB 账户' }] : []}>
                              <Select placeholder="选择 IB 账户">
                                {ibAccounts.map((account) => (
                                  <Select.Option key={account.id} value={account.id}>
                                    {account.name} (Port: {account.ib_port})
                                  </Select.Option>
                                ))}
                              </Select>
                            </Form.Item>
                          );
                        }}
                      </Form.Item>
                    </Col>
                  </Row>

                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item name="buy_threshold" label="买入触发阈值(<=)" rules={[{ required: true }]}>
                        <InputNumber min={0} max={100} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="greed_threshold" label="进入止盈区阈值(>=)" rules={[{ required: true }]}>
                        <InputNumber min={0} max={100} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="volume_ratio_threshold" label="投影量比阈值" rules={[{ required: true }]}>
                        <InputNumber min={0} step={0.1} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>

                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item name="buy_position_pct" label="每次买入仓位%" rules={[{ required: true }]}>
                        <InputNumber min={0} max={100} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="cooldown_days" label="冷却天数" rules={[{ required: true }]}>
                        <InputNumber min={0} max={60} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="trailing_stop_pct" label="移动止盈回撤%" rules={[{ required: true }]}>
                        <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>

                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item name="sell_position_pct" label="止盈减仓%" rules={[{ required: true }]}>
                        <InputNumber min={0} max={100} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="sell_reduction_basis" label="止盈减仓口径" rules={[{ required: true }]}>
                        <Select options={sellReductionBasisOptions} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="max_take_profit_sells_per_cycle" label="同轮止盈最多卖出次数" rules={[{ required: true }]}>
                        <InputNumber min={1} max={20} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>

                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item name="min_position_pct_after_take_profit" label="止盈后最低保留仓位%" rules={[{ required: true }]}>
                        <InputNumber min={0} max={100} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item name="rebalance_threshold_pct" label="调仓阈值%" rules={[{ required: true }]}>
                        <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>

                  <Card size="small" style={{ marginBottom: 16 }}>
                    <Space direction="vertical" size={4}>
                      <Text>最近执行时间: {config.last_run_at ? dayjs(config.last_run_at).format('YYYY-MM-DD HH:mm:ss') : '-'}</Text>
                      <Text>最近执行状态: {config.last_run_status || '-'}</Text>
                      <Text type="secondary">最近执行说明: {config.last_run_message || '-'}</Text>
                    </Space>
                  </Card>

                  <Form.Item>
                    <Space>
                      <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={configLoading}>
                        保存配置
                      </Button>
                      <Button onClick={handleBacktest}>
                        回测
                      </Button>
                      <Button icon={<PlayCircleOutlined />} onClick={handleManualRun} loading={manualLoading}>
                        立即执行一次
                      </Button>
                    </Space>
                  </Form.Item>
                </Form>
              </Card>
            ),
          },
          {
            key: 'logs',
            label: <span><HistoryOutlined />运行日志</span>,
            children: (
              <Card>
                <Table
                  columns={logColumns}
                  dataSource={logs}
                  rowKey="id"
                  loading={logLoading}
                  pagination={{ pageSize: 10 }}
                  scroll={{ x: 1400 }}
                />
                <Button style={{ marginTop: 16 }} onClick={fetchLogs} loading={logLoading}>
                  刷新日志
                </Button>
              </Card>
            ),
          },
        ]}
      />
    </div>
  );
};

export default SoxlFearStrategy;
