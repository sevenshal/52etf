import React, { useCallback, useEffect, useState } from 'react';
import { Card, Form, Select, InputNumber, Button, Switch, Table, Tabs, message, Tag, Space, Typography } from 'antd';
import { SettingOutlined, HistoryOutlined } from '@ant-design/icons';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';
import dayjs from 'dayjs';

const { Option } = Select;
const { TabPane } = Tabs;
const { Title, Text } = Typography;

const AutomatedTrading = () => {
  const [configLoading, setConfigLoading] = useState(false);
  const [logLoading, setLogLoading] = useState(false);
  const [logs, setLogs] = useState([]);
  const [form] = Form.useForm();
  const [isEnabled, setIsEnabled] = useState(false);

  const fetchConfig = useCallback(async () => {
    setConfigLoading(true);
    try {
      const response = await request.get('/api/trading/config');
            if (response.data) {
                form.setFieldsValue(response.data);
                setIsEnabled(response.data.enabled);
            }
        } catch (error) {
            message.error('Failed to load configuration');
    } finally {
      setConfigLoading(false);
    }
  }, [form]);

  const fetchLogs = useCallback(async () => {
    setLogLoading(true);
    try {
            const response = await request.get('/api/trading/logs');
            setLogs(response.data);
        } catch (error) {
            message.error('Failed to load trade logs');
    } finally {
      setLogLoading(false);
    }
  }, []);

  // Fetch config on mount
  useEffect(() => {
    fetchConfig();
    fetchLogs();
  }, [fetchConfig, fetchLogs]);

  useEffect(() => {
    return subscribeBackendEvent('automated_trading_logs', () => {
      fetchLogs();
    });
  }, [fetchLogs]);

  const onSaveConfig = async (values) => {
        setConfigLoading(true);
        try {
            await request.post('/api/trading/config', {
                ...values,
                enabled: isEnabled
            });
            message.success('Configuration saved successfully');
        } catch (error) {
            message.error(error.response?.data?.detail || 'Failed to save configuration');
        } finally {
            setConfigLoading(false);
        }
    };

    const onManualCheck = async () => {
        try {
            await request.post('/api/trading/manual-check');
            message.info('Manual check triggered. Logs will refresh when it finishes.');
        } catch (error) {
            message.error(error.response?.data?.detail || 'Manual check failed');
        }
    };

    const logColumns = [
        {
            title: '时间',
            dataIndex: 'timestamp',
            key: 'timestamp',
            render: (val) => dayjs(val).format('YYYY-MM-DD HH:mm:ss')
        },
        {
            title: '标的',
            dataIndex: 'symbol',
            key: 'symbol',
        },
        {
            title: '操作',
            dataIndex: 'action',
            key: 'action',
            render: (text) => {
                let color = 'blue';
                if (text === 'BUY') color = 'volcano';
                if (text === 'SELL') color = 'green';
                return <Tag color={color}>{text}</Tag>;
            }
        },
        {
            title: '价格',
            dataIndex: 'price',
            key: 'price',
            render: (val) => val ? val.toFixed(2) : '-'
        },
        {
            title: '数量',
            dataIndex: 'quantity',
            key: 'quantity',
            render: (val) => val || '-'
        },
        {
            title: '状态',
            dataIndex: 'status',
            key: 'status',
            render: (text) => (
                <Tag color={text === 'SUCCESS' ? 'success' : 'error'}>{text}</Tag>
            )
        },
        {
            title: '详情',
            dataIndex: 'message',
            key: 'message',
            ellipsis: true
        }
    ];

    return (
        <div style={{ padding: '24px' }}>
            <Title level={2}>杠杆ETF均线策略自动化交易</Title>
            <Tabs defaultActiveKey="1">
                <TabPane
                    tab={<span><SettingOutlined />策略配置</span>}
                    key="1"
                >
                    <Card>
                        <Form
                            form={form}
                            layout="vertical"
                            onFinish={onSaveConfig}
                            initialValues={{
                                etf_code: 'TQQQ',
                                short_window: 5,
                                long_window: 30,
                                ib_port: 4001,
                                target_ratio: 10
                            }}
                        >
                            <Form.Item label="启用策略">
                                <Switch
                                    checked={isEnabled}
                                    onChange={(val) => setIsEnabled(val)}
                                />
                                <Text type="secondary" style={{ marginLeft: 16 }}>
                                    开启后，系统将在每日美股收盘前10秒执行信号判断
                                </Text>
                            </Form.Item>

                            <Form.Item name="etf_code" label="交易标的" rules={[{ required: true }]}>
                                <Select>
                                    <Option value="TQQQ">TQQQ</Option>
                                    <Option value="SOXL">SOXL</Option>
                                    <Option value="CONL">CONL</Option>
                                    <Option value="SQQQ">SQQQ</Option>
                                    <Option value="QQQ">QQQ</Option>
                                    <Option value="SPMO">SPMO</Option>
                                    <Option value="NAIL">NAIL</Option>
                                    <Option value="LABU">LABU</Option>
                                    <Option value="UPRO">UPRO</Option>
                                    <Option value="TNA">TNA</Option>
                                    <Option value="YINN">YINN</Option>
                                </Select>
                            </Form.Item>

                            <Space size="large">
                                <Form.Item name="short_window" label="慢线 (短周期)" rules={[{ required: true }]}>
                                    <InputNumber min={1} />
                                </Form.Item>
                                <Form.Item name="long_window" label="快线 (长周期)" rules={[{ required: true }]}>
                                    <InputNumber min={2} />
                                </Form.Item>
                            </Space>
                            <Text type="secondary" style={{ display: 'block', marginBottom: 24 }}>
                                * 注：按照用户要求，此处配置为 慢线 &lt; 快线。系统会根据 MA 交叉产生买卖信号。
                            </Text>

                            <Form.Item name="ib_port" label="IB Gateway 端口" rules={[{ required: true }]}>
                                <InputNumber style={{ width: '100%' }} placeholder="默认 4001" />
                            </Form.Item>

                            <Form.Item name="target_ratio" label="目标仓位比例 (%)" rules={[{ required: true }]}>
                                <div style={{ display: 'flex', alignItems: 'center' }}>
                                    <Form.Item name="target_ratio" noStyle>
                                        <InputNumber
                                            min={1}
                                            max={100}
                                            style={{ margin: '0 16px' }}
                                        />
                                    </Form.Item>
                                    <Text type="secondary">账户总资产的比例</Text>
                                </div>
                            </Form.Item>

                            <Form.Item>
                                <Space>
                                    <Button type="primary" htmlType="submit" loading={configLoading}>
                                        保存配置
                                    </Button>
                                    <Button onClick={onManualCheck}>
                                        立即检查信号 (手动执行)
                                    </Button>
                                </Space>
                            </Form.Item>
                        </Form>
                    </Card>
                </TabPane>
                <TabPane
                    tab={<span><HistoryOutlined />交易日志</span>}
                    key="2"
                >
                    <Card>
                        <Table
                            columns={logColumns}
                            dataSource={logs}
                            rowKey="id"
                            loading={logLoading}
                            pagination={{ defaultPageSize: 10 }}
                        />
                        <Button
                            style={{ marginTop: 16 }}
                            onClick={fetchLogs}
                            loading={logLoading}
                        >
                            刷新日志
                        </Button>
                    </Card>
                </TabPane>
            </Tabs>
        </div>
    );
};

export default AutomatedTrading;
