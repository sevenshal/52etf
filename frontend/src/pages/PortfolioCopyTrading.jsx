import React, { useState, useEffect } from 'react';
import {
    Table, Card, Button, Modal, Form, Input, InputNumber,
    Space, Tag, message, Typography, Switch, Row, Col, List,
    Tabs, Select
} from 'antd';
import {
    PlusOutlined, ReloadOutlined, PlayCircleOutlined, HistoryOutlined,
    SettingOutlined, DeleteOutlined, EditOutlined
} from '@ant-design/icons';
import request from '../utils/request';
import { useAccount } from '../contexts/AccountContext';

const { Title, Text } = Typography;
const { TextArea } = Input;

const PortfolioCopyTrading = () => {
    const { accountId } = useAccount();
    const [configs, setConfigs] = useState([]);
    // Logs State
    const [logModalVisible, setLogModalVisible] = useState(false);
    const [currentLogs, setCurrentLogs] = useState([]);
    const [logLoading, setLogLoading] = useState(false);
    const [currentLogTitle, setCurrentLogTitle] = useState('');

    const [loading, setLoading] = useState(false);
    const [modalVisible, setModalVisible] = useState(false);
    const [editingConfig, setEditingConfig] = useState(null);
    const [form] = Form.useForm();
    const [activeTab, setActiveTab] = useState('ib_configs'); // Changed default to ib_configs
    const [ibAccounts, setIbAccounts] = useState([]);
    const [previewVisible, setPreviewVisible] = useState(false);
    const [previewPlan, setPreviewPlan] = useState([]);
    const [previewLoading, setPreviewLoading] = useState(false);

    // Snowball States
    const [snowballConfigs, setSnowballConfigs] = useState([]);
    const [snowballModalVisible, setSnowballModalVisible] = useState(false);
    const [snowballForm] = Form.useForm();
    const [snowballEditingConfig, setSnowballEditingConfig] = useState(null);

    const handlePreview = async (configId) => {
        setPreviewLoading(true);
        setPreviewVisible(true);
        setPreviewPlan([]);
        try {
            const response = await request.post(`/api/ib-copy-trading/configs/${configId}/preview`);
            setPreviewPlan(response.data);
        } catch (error) {
            message.error('获取预览失败: ' + (error.response?.data?.detail || error.message));
        } finally {
            setPreviewLoading(false);
        }
    };

    const fetchSnowballConfigs = async () => {
        setLoading(true);
        try {
            const response = await request.get('/api/snowball/configs');
            setSnowballConfigs(response.data);
        } catch (error) {
            message.error('获取雪球配置失败');
        } finally {
            setLoading(false);
        }
    };

    const fetchIbAccounts = async () => {
        try {
            const response = await request.get('/api/ib-accounts');
            setIbAccounts(response.data);
        } catch (error) {
            message.error('获取 IB 账户列表失败');
        }
    };

    const fetchConfigs = async () => {
        setLoading(true);
        try {
            const response = await request.get('/api/ib-copy-trading/configs');
            setConfigs(response.data);
        } catch (error) {
            message.error('获取配置失败');
        } finally {
            setLoading(false);
        }
    };

    const fetchPortfolioName = async () => {
        const id = form.getFieldValue('portfolio_id');
        if (!id) {
            message.warning('请先输入投资组合 ID');
            return;
        }
        try {
            const response = await request.get(`/api/ib-copy-trading/portfolio-info/${id}`);
            form.setFieldsValue({ portfolio_name: response.data.name });
            message.success('获取成功: ' + response.data.name);
        } catch (error) {
            message.error('获取组合名称失败');
        }
    };

    const handleViewLogs = async (record, type) => {
        setLogLoading(true);
        setLogModalVisible(true);
        setCurrentLogs([]);
        if (type === 'ib') {
            setCurrentLogTitle(`跟单日志 - ${record.portfolio_name} (${record.portfolio_id})`);
        } else {
            setCurrentLogTitle(`跟单日志 - ${record.cli_id}`);
        }

        try {
            let res;
            if (type === 'ib') {
                res = await request.get('/api/ib-copy-trading/logs', { params: { portfolio_id: record.portfolio_id } });
            } else {
                res = await request.get('/api/snowball/logs', { params: { cli_id: record.cli_id } });
            }
            setCurrentLogs(res.data);
        } catch (e) {
            message.error('获取日志失败');
        } finally {
            setLogLoading(false);
        }
    };

    const handleSave = async (values) => {
        try {
            const payload = {
                ...values,
                id: editingConfig?.id
            };

            await request.post('/api/ib-copy-trading/configs', payload);
            message.success(editingConfig ? '更新成功' : '添加成功');
            setModalVisible(false);
            fetchConfigs();
        } catch (error) {
            message.error('保存失败');
        }
    };

    const handleDelete = async (id) => {
        try {
            await request.delete(`/api/ib-copy-trading/configs/${id}`);
            message.success('删除成功');
            fetchConfigs();
        } catch (error) {
            message.error('删除失败');
        }
    };

    const fetchSnowballName = async () => {
        const id = snowballForm.getFieldValue('combination_id');
        if (!id) {
            message.warning('请先输入雪球组合 ID');
            return;
        }
        try {
            const response = await request.get(`/api/snowball/info/${id}`);
            snowballForm.setFieldsValue({ combination_name: response.data.name });
            message.success('获取成功: ' + response.data.name);
        } catch (error) {
            message.error('获取组合名称失败: ' + (error.response?.data?.detail || error.message));
        }
    };

    // Snowball Handlers
    const handleSnowballSave = async (values) => {
        try {
            const payload = { ...values };
            if (snowballEditingConfig) {
                await request.put(`/api/snowball/configs/${snowballEditingConfig.id}`, payload);
                message.success('更新成功');
            } else {
                await request.post('/api/snowball/configs', payload);
                message.success('添加成功');
            }
            setSnowballModalVisible(false);
            fetchSnowballConfigs();
        } catch (error) {
            message.error('保存失败: ' + (error.response?.data?.detail || error.message));
        }
    };


    const handleSnowballDelete = async (id) => {
        try {
            await request.delete(`/api/snowball/configs/${id}`);
            message.success('删除成功');
            fetchSnowballConfigs();
        } catch (error) {
            message.error('删除失败');
        }
    };

    useEffect(() => {
        if (accountId) {
            if (activeTab === 'ib_configs') {
                fetchConfigs();
            } else if (activeTab === 'snowball_configs') {
                fetchSnowballConfigs();
            }
            // IB accounts are always useful or global
            fetchIbAccounts();
        }
    }, [accountId, activeTab]);

    const configColumns = [
        {
            title: '状态',
            dataIndex: 'enabled',
            key: 'enabled',
            render: (enabled) => <Tag color={enabled ? 'green' : 'gray'}>{enabled ? '开启' : '关闭'}</Tag>
        },
        {
            title: '组合信息',
            key: 'portfolio',
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    <Text strong>{record.portfolio_name || '未命名'}</Text>
                    <Text type="secondary" style={{ fontSize: '12px' }}>ID: {record.portfolio_id}</Text>
                </Space>
            )
        },
        {
            title: '触发规则',
            key: 'cron_rule',
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    <Text>{record.cron_rule}</Text>
                    <Text type="secondary" style={{ fontSize: '12px' }}>{record.timezone}</Text>
                </Space>
            )
        },
        {
            title: 'IB 账户',
            key: 'ib_account',
            render: (_, record) => {
                if (record.ib_account_id) {
                    const account = ibAccounts.find(a => a.id === record.ib_account_id);
                    return account ? (
                        <Space direction="vertical" size={0}>
                            <Text>{account.name}</Text>
                            <Text type="secondary" style={{ fontSize: '12px' }}>Port: {account.ib_port}</Text>
                        </Space>
                    ) : `Unknown (ID: ${record.ib_account_id})`;
                }
                return `Port: ${record.ib_port}`;
            }
        },
        {
            title: '配置',
            key: 'settings',
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    <Text type="secondary" style={{ fontSize: '12px' }}>仓位占比: {record.total_position_ratio}%</Text>
                    <Text type="secondary" style={{ fontSize: '12px' }}>跟踪误差: {record.tracking_error_pct}%</Text>
                </Space>
            )
        },
        {
            title: '操作',
            key: 'action',
            render: (_, record) => (
                <Space>
                    <Button
                        icon={<HistoryOutlined />}
                        onClick={() => handleViewLogs(record, 'ib')}
                        size="small"
                        title="查看日志"
                    >日志</Button>
                    <Button
                        icon={<PlayCircleOutlined />}
                        onClick={() => handlePreview(record.id)}
                        size="small"
                        title="预览调仓"
                    />
                    <Button
                        icon={<EditOutlined />}
                        onClick={() => {
                            setEditingConfig(record);
                            form.setFieldsValue(record);
                            setModalVisible(true);
                        }}
                        size="small"
                    />
                    <Button
                        icon={<DeleteOutlined />}
                        onClick={() => handleDelete(record.id)}
                        size="small"
                        danger
                    />
                </Space>
            )
        }
    ];

    const logColumns = [
        {
            title: '时间',
            dataIndex: 'timestamp',
            key: 'timestamp',
            render: (t) => new Date(t).toLocaleString()
        },
        {
            title: '组合',
            key: 'portfolio_id',
            width: 100,
            render: (_, record) => {
                if (record.combination_id) {
                    // Handle comma-separated IDs for Snowball
                    const ids = record.combination_id.split(',');
                    const names = ids.map(id => {
                        const config = snowballConfigs.find(c => c.combination_id === id);
                        return config ? config.combination_name : id;
                    });
                    return names.join(', ');
                }
                if (record.portfolio_id) {
                    const config = configs.find(c => c.portfolio_id === record.portfolio_id);
                    const name = config ? config.portfolio_name : '';
                    return name ? `${name} (${record.portfolio_id})` : record.portfolio_id;
                }
                return '-';
            }
        },
        {
            title: '行为',
            dataIndex: 'action',
            key: 'action',
        },
        {
            title: '标的',
            dataIndex: 'symbol',
            key: 'symbol',
        },
        {
            title: '数量',
            dataIndex: 'quantity',
            key: 'quantity',
        },
        {
            title: '结果',
            key: 'status',
            render: (_, record) => (
                <Tag color={record.status === 'SUCCESS' ? 'green' : 'red'}>
                    {record.status}
                </Tag>
            )
        },
        {
            title: '消息',
            dataIndex: 'message',
            key: 'message',
        }
    ];

    return (
        <div style={{ padding: '24px' }}>
            <Card
                title={
                    <Space>
                        <Title level={4} style={{ margin: 0 }}>投资组合自动化跟单</Title>
                    </Space>
                }
                extra={
                    <Space>
                        <Button icon={<ReloadOutlined />} onClick={() => {
                            if (activeTab === 'ib_configs') fetchConfigs();
                            else if (activeTab === 'snowball_configs') fetchSnowballConfigs();
                            // fetchLogs(); // Removed
                        }}>刷新数据</Button>

                    </Space>
                }
            >
                <Tabs activeKey={activeTab} onChange={setActiveTab}>
                    <Tabs.TabPane tab={<span><SettingOutlined />IB账户跟单配置</span>} key="ib_configs">
                        <Table
                            dataSource={configs}
                            columns={configColumns}
                            rowKey="id"
                            loading={loading}
                            pagination={false}
                        />
                        <div style={{ marginTop: 16 }}>
                            <Button type="primary" icon={<PlusOutlined />} onClick={() => {
                                setEditingConfig(null);
                                form.resetFields();
                                setModalVisible(true);
                            }}>添加IB跟单配置</Button>
                        </div>
                    </Tabs.TabPane>

                    <Tabs.TabPane tab={<span><SettingOutlined />A股雪球跟单配置</span>} key="snowball_configs">
                        <Table
                            dataSource={snowballConfigs}
                            rowKey="id"
                            loading={loading}
                            pagination={false}
                            columns={[
                                {
                                    title: '状态',
                                    dataIndex: 'enabled',
                                    render: (enabled) => <Tag color={enabled ? 'green' : 'gray'}>{enabled ? '开启' : '关闭'}</Tag>
                                },
                                {
                                    title: '组合信息',
                                    key: 'info',
                                    render: (_, r) => (
                                        <Space direction="vertical" size={0}>
                                            <Text strong>{r.combination_name || '未命名'}</Text>
                                            <Text type="secondary">ID: {r.combination_id}</Text>
                                        </Space>
                                    )
                                },
                                {
                                    title: 'API标识',
                                    dataIndex: 'cli_id',
                                    render: (id, record) => (
                                        <Space>
                                            <Tag color="blue">{id}</Tag>
                                            <Button
                                                icon={<HistoryOutlined />}
                                                size="small"
                                                type="text"
                                                onClick={() => handleViewLogs(record, 'snowball')}
                                                title="查看日志"
                                            />
                                        </Space>
                                    )
                                },
                                {
                                    title: '资金/参数',
                                    key: 'params',
                                    render: (_, r) => (
                                        <Space direction="vertical" size={0}>
                                            <Text strong style={{ color: '#1890ff' }}>
                                                当前市值: {r.snapshot_value ? r.snapshot_value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : 0}
                                            </Text>
                                            {r.total_amount && (
                                                <Text type="secondary" style={{ fontSize: '12px' }}>配置金额: {r.total_amount.toLocaleString()}</Text>
                                            )}
                                            <Text type="secondary" style={{ fontSize: '12px' }}>误差: {r.tracking_error_pct}%</Text>
                                            {r.blacklisted_symbols && r.blacklisted_symbols.length > 0 && (
                                                <Text type="secondary" style={{ fontSize: '12px', color: 'red' }}>黑名单: {r.blacklisted_symbols.length}个</Text>
                                            )}
                                        </Space>
                                    )
                                },
                                {
                                    title: '操作',
                                    key: 'action',
                                    render: (_, record) => (
                                        <Space>
                                            <Button icon={<EditOutlined />} size="small" onClick={() => {
                                                setSnowballEditingConfig(record);
                                                snowballForm.setFieldsValue(record);
                                                setSnowballModalVisible(true);
                                            }} />
                                            <Button icon={<DeleteOutlined />} size="small" danger onClick={() => handleSnowballDelete(record.id)} />
                                        </Space>
                                    )
                                }
                            ]}
                        />
                        <div style={{ marginTop: 16 }}>
                            <Button type="primary" icon={<PlusOutlined />} onClick={() => {
                                setSnowballEditingConfig(null);
                                snowballForm.resetFields();
                                setSnowballModalVisible(true);
                            }}>添加雪球跟单配置</Button>
                        </div>
                    </Tabs.TabPane>

                </Tabs>
            </Card>

            {/* Logs Modal */}
            <Modal
                title={currentLogTitle}
                visible={logModalVisible}
                onCancel={() => setLogModalVisible(false)}
                footer={null}
                width={900}
            >
                <Table
                    dataSource={currentLogs}
                    loading={logLoading}
                    rowKey="id"
                    pagination={{ pageSize: 20 }}
                    columns={logColumns}
                />
            </Modal >

            {/* Existing Modal for IB Config */}
            < Modal
                title="调仓计划预览"
                visible={previewVisible}
                onCancel={() => setPreviewVisible(false)}
                footer={
                    [
                        <Button key="close" onClick={() => setPreviewVisible(false)}>关闭</Button>
                    ]}
                width={800}
            >
                <Table
                    dataSource={previewPlan}
                    loading={previewLoading}
                    rowKey="symbol"
                    size="small"
                    columns={[
                        { title: '代码', dataIndex: 'symbol', key: 'symbol' },
                        {
                            title: '操作',
                            dataIndex: 'action',
                            key: 'action',
                            render: (a) => {
                                let color = 'gold';
                                if (a === 'BUY') color = 'green';
                                if (a === 'SELL') color = 'red';
                                return <Tag color={color}>{a}</Tag>;
                            }
                        },
                        { title: '数量', dataIndex: 'quantity', key: 'quantity' },
                        { title: '价格', dataIndex: 'price', key: 'price', render: (p) => p?.toFixed(2) },
                        {
                            title: '当前/目标股数',
                            key: 'qty_change',
                            render: (_, r) => `${r.current_qty} -> ${r.target_qty}`
                        },
                        {
                            title: '当前/目标占比',
                            key: 'ratio_change',
                            render: (_, r) => `${r.current_ratio?.toFixed(2)}% -> ${r.target_ratio?.toFixed(2)}%`
                        }
                    ]}
                    pagination={false}
                />
            </Modal >

            <Modal
                title={editingConfig ? "编辑跟单配置" : "添加跟单配置"}
                visible={modalVisible}
                onCancel={() => setModalVisible(false)}
                onOk={() => form.submit()}
                width={700}
            >
                <Form form={form} layout="vertical" onFinish={handleSave} initialValues={{ enabled: true, cron_rule: '0 8 * * *', timezone: 'America/New_York', tracking_error_pct: 5, total_position_ratio: 100 }}>
                    <Row gutter={16}>
                        <Col span={24}>
                            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 24, marginTop: 12 }}>
                                <span style={{ marginRight: 8, fontSize: '14px' }}>开启状态:</span>
                                <Form.Item name="enabled" valuePropName="checked" noStyle>
                                    <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                                </Form.Item>
                            </div>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item label="投资组合 ID" rules={[{ required: true }]}>
                                <Space.Compact style={{ width: '100%' }}>
                                    <Form.Item name="portfolio_id" noStyle rules={[{ required: true }]}>
                                        <Input placeholder="例如: 158919" />
                                    </Form.Item>
                                    <Button onClick={fetchPortfolioName}>获取名称</Button>
                                </Space.Compact>
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="portfolio_name" label="组合名称" rules={[{ required: true }]}>
                                <Input placeholder="自动获取或手动输入" />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="cron_rule" label="触发 Cron 规则" rules={[{ required: true }]}>
                                <Input placeholder="例如: 0 8 * * *" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="timezone" label="触发时区" rules={[{ required: true }]}>
                                <Select>
                                    <Select.Option value="America/New_York">美股 (America/New_York)</Select.Option>
                                    <Select.Option value="Asia/Shanghai">A股 (Asia/Shanghai)</Select.Option>
                                </Select>
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="ib_account_id" label="IB 账户" rules={[{ required: true, message: '请选择 IB 账户' }]}>
                                <Select placeholder="选择 IB 账户">
                                    {ibAccounts.map(account => (
                                        <Select.Option key={account.id} value={account.id}>
                                            {account.name} (Port: {account.ib_port})
                                        </Select.Option>
                                    ))}
                                </Select>
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={8}>
                            <Form.Item name="total_position_ratio" label="总仓位比例 (%)">
                                <InputNumber style={{ width: '100%' }} min={0} max={100} />
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item name="total_amount" label="总金额 (优先)">
                                <InputNumber style={{ width: '100%' }} />
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item name="tracking_error_pct" label="跟踪误差 (%)">
                                <InputNumber style={{ width: '100%' }} min={0} max={100} />
                            </Form.Item>
                        </Col>
                    </Row>
                </Form>
            </Modal>

            {/* Snowball Config Modal */}
            <Modal
                title={snowballEditingConfig ? "编辑雪球跟单配置" : "添加雪球跟单配置"}
                visible={snowballModalVisible}
                onCancel={() => setSnowballModalVisible(false)}
                onOk={() => snowballForm.submit()}
                width={700}
            >
                <Form form={snowballForm} layout="vertical" onFinish={handleSnowballSave} initialValues={{ enabled: true, total_position_ratio: 100, tracking_error_pct: 1 }}>
                    <Form.Item name="enabled" valuePropName="checked">
                        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                    </Form.Item>
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item label="雪球组合ID" required>
                                <Space.Compact style={{ width: '100%' }}>
                                    <Form.Item name="combination_id" noStyle rules={[{ required: true, message: '请输入组合ID' }]}>
                                        <Input placeholder="例如: ZH123456" />
                                    </Form.Item>
                                    <Button onClick={fetchSnowballName}>获取名称</Button>
                                </Space.Compact>
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="combination_name" label="组合名称">
                                <Input placeholder="自动获取或手动输入" />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="cli_id" label="API调用标识 (CLI_ID)" rules={[{ required: true }]}>
                                <Input placeholder="唯一ID, 用于API调用" />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={16}>
                        <Col span={24}>
                            <Form.Item name="blacklisted_symbols" label="跟单黑名单 (不买入/若持有会卖出)">
                                <Select mode="tags" style={{ width: '100%' }} placeholder="输入股票代码 (如 SH.600519), 回车确认" tokenSeparators={[',', ' ']} />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="total_amount" label="总金额">
                                <InputNumber style={{ width: '100%' }} placeholder="为空则使用Portfolio" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="tracking_error_pct" label="跟踪误差 (%)">
                                <InputNumber style={{ width: '100%' }} min={0} max={100} step={0.1} />
                            </Form.Item>
                        </Col>
                    </Row>
                </Form>
            </Modal>
        </div >
    );
};

export default PortfolioCopyTrading;
