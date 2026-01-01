import React, { useState, useEffect } from 'react';
import {
    Table, Card, Button, Modal, Form, Input, InputNumber,
    Space, Tag, message, Typography, Switch, Row, Col, List,
    Tabs
} from 'antd';
import {
    PlusOutlined, ReloadOutlined, HistoryOutlined,
    SettingOutlined, DeleteOutlined, EditOutlined
} from '@ant-design/icons';
import request from '../utils/request';
import { useAccount } from '../contexts/AccountContext';

const { Title, Text } = Typography;
const { TextArea } = Input;

const PortfolioCopyTrading = () => {
    const { accountId } = useAccount();
    const [configs, setConfigs] = useState([]);
    const [logs, setLogs] = useState([]);
    const [loading, setLoading] = useState(false);
    const [modalVisible, setModalVisible] = useState(false);
    const [editingConfig, setEditingConfig] = useState(null);
    const [form] = Form.useForm();
    const [activeTab, setActiveTab] = useState('configs');
    const [previewVisible, setPreviewVisible] = useState(false);
    const [previewPlan, setPreviewPlan] = useState([]);
    const [previewLoading, setPreviewLoading] = useState(false);

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

    useEffect(() => {
        if (accountId) {
            fetchConfigs();
            fetchLogs();
        }
    }, [accountId]);

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

    const fetchLogs = async () => {
        try {
            const response = await request.get('/api/ib-copy-trading/logs', {
                params: { account_id: accountId }
            });
            setLogs(response.data);
        } catch (error) {
            message.error('获取日志失败');
        }
    };

    const handleSave = async (values) => {
        try {
            const payload = {
                ...values,
                account_id: accountId,
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
            dataIndex: 'cron_rule',
            key: 'cron_rule',
        },
        {
            title: 'IB 端口',
            dataIndex: 'ib_port',
            key: 'ib_port',
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
                        <Button icon={<ReloadOutlined />} onClick={() => { fetchConfigs(); fetchLogs(); }}>刷新数据</Button>
                        <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => {
                                setEditingConfig(null);
                                form.resetFields();
                                setModalVisible(true);
                            }}
                        >
                            添加配置
                        </Button>
                    </Space>
                }
            >
                <Tabs activeKey={activeTab} onChange={setActiveTab}>
                    <Tabs.TabPane tab={<span><SettingOutlined />跟单配置</span>} key="configs">
                        <Table
                            dataSource={configs}
                            columns={configColumns}
                            rowKey="id"
                            loading={loading}
                            pagination={false}
                        />
                    </Tabs.TabPane>
                    <Tabs.TabPane tab={<span><HistoryOutlined />跟单日志</span>} key="logs">
                        <Table
                            dataSource={logs}
                            columns={logColumns}
                            rowKey="id"
                            pagination={{ pageSize: 20 }}
                        />
                    </Tabs.TabPane>
                </Tabs>
            </Card>

            <Modal
                title="调仓计划预览"
                visible={previewVisible}
                onCancel={() => setPreviewVisible(false)}
                footer={[
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
            </Modal>

            <Modal
                title={editingConfig ? "编辑跟单配置" : "添加跟单配置"}
                visible={modalVisible}
                onCancel={() => setModalVisible(false)}
                onOk={() => form.submit()}
                width={700}
            >
                <Form form={form} layout="vertical" onFinish={handleSave} initialValues={{ enabled: true, cron_rule: '0 8 * * *', tracking_error_pct: 10, total_position_ratio: 100 }}>
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="enabled" label="开启状态" valuePropName="checked">
                                <Switch />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item label="投资组合 ID">
                                <Space.Compact style={{ width: '100%' }}>
                                    <Form.Item name="portfolio_id" noStyle rules={[{ required: true }]}>
                                        <Input placeholder="例如: 158919" />
                                    </Form.Item>
                                    <Button onClick={fetchPortfolioName}>获取名称</Button>
                                </Space.Compact>
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="portfolio_name" label="组合名称">
                                <Input placeholder="自动获取或手动输入" />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="cron_rule" label="触发 Cron 规则" rules={[{ required: true }]}>
                                <Input placeholder="例如: 0 8 * * * 或 */30 * * * *" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="ib_port" label="IB Gateway 端口" rules={[{ required: true }]}>
                                <InputNumber style={{ width: '100%' }} />
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
                            <Form.Item name="total_amount" label="总金额 (可选)">
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
        </div>
    );
};

export default PortfolioCopyTrading;
