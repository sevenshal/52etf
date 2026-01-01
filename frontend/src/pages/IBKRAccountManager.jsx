import React, { useState, useEffect } from 'react';
import dayjs from 'dayjs';
import {
    Table, Card, Button, Modal, Form, Input, InputNumber,
    Space, Tag, message, Typography, Descriptions, Popconfirm,
    Badge, Row, Col, Tooltip, Select, TimePicker, Radio
} from 'antd';
import {
    PlusOutlined, ReloadOutlined, SyncOutlined,
    ThunderboltOutlined, DeleteOutlined, EditOutlined,
    GlobalOutlined, ContainerOutlined, LockOutlined,
    ClockCircleOutlined, SettingOutlined
} from '@ant-design/icons';
import request from '../utils/request';

const { Title, Text } = Typography;

const IBKRAccountManager = () => {
    const [accounts, setAccounts] = useState([]);
    const [loading, setLoading] = useState(false);
    const [modalVisible, setModalVisible] = useState(false);
    const [editingAccount, setEditingAccount] = useState(null);
    const [form] = Form.useForm();
    const [statuses, setStatuses] = useState({}); // {accountId: statusData}
    const [statusLoading, setStatusLoading] = useState({});

    useEffect(() => {
        fetchAccounts();
    }, []);

    const fetchAccounts = async () => {
        setLoading(true);
        try {
            const response = await request.get('/api/ib-accounts');
            setAccounts(response.data);
            // 自动检查所有账户状态
            response.data.forEach(acc => checkStatus(acc.id));
        } catch (error) {
            message.error('获取账户列表失败');
        } finally {
            setLoading(false);
        }
    };

    const checkStatus = async (id) => {
        setStatusLoading(prev => ({ ...prev, [id]: true }));
        try {
            const response = await request.get(`/api/ib-accounts/${id}/status`);
            setStatuses(prev => ({ ...prev, [id]: response.data }));
        } catch (error) {
            setStatuses(prev => ({ ...prev, [id]: { connected: false, message: '连接失败' } }));
        } finally {
            setStatusLoading(prev => ({ ...prev, [id]: false }));
        }
    };

    const handleRestart = async (id) => {
        try {
            message.loading({ content: '正在发送重启指令...', key: 'restart' });
            await request.post(`/api/ib-accounts/${id}/restart`);
            message.success({ content: '重启指令已发送', key: 'restart' });
            // 重启后延迟检查状态
            setTimeout(() => checkStatus(id), 30000);
        } catch (error) {
            message.error({ content: `重启失败: ${error.response?.data?.detail || error.message}`, key: 'restart' });
        }
    };

    const handleDeploy = async (id) => {
        try {
            message.loading({ content: '正在部署容器...', key: 'deploy' });
            await request.post(`/api/ib-accounts/${id}/deploy`);
            message.success({ content: '容器部署成功', key: 'deploy' });
            // 部署后延迟检查状态
            setTimeout(() => checkStatus(id), 10000);
        } catch (error) {
            message.error({ content: `部署失败: ${error.response?.data?.detail || error.message}`, key: 'deploy' });
        }
    };

    const handleSave = async (values) => {
        try {
            // 转换时间选择器为字符串格式 "hh:mm A"
            const formattedValues = {
                ...values,
                auto_restart_time: values.auto_restart_time ? values.auto_restart_time.format('hh:mm A') : '08:59 PM'
            };
            const payload = editingAccount ? { ...formattedValues, id: editingAccount.id } : formattedValues;
            await request.post('/api/ib-accounts', payload);
            message.success(editingAccount ? '更新成功' : '添加成功');
            setModalVisible(false);
            fetchAccounts();
        } catch (error) {
            message.error(error.response?.data?.detail || '保存失败');
        }
    };

    const handleDelete = async (id) => {
        try {
            await request.delete(`/api/ib-accounts/${id}`);
            message.success('删除成功');
            fetchAccounts();
        } catch (error) {
            message.error('删除失败');
        }
    };

    const columns = [
        {
            title: '账户名称',
            dataIndex: 'name',
            key: 'name',
            render: (text, record) => (
                <Space direction="vertical" size={0}>
                    <Text strong>{text}</Text>
                    <Text type="secondary" style={{ fontSize: '12px' }}>
                        {record.ib_host}:{record.ib_port} (ID: {record.client_id})
                    </Text>
                </Space>
            )
        },
        {
            title: '容器名称',
            dataIndex: 'container_name',
            key: 'container_name',
            render: (text) => text || <Text type="warning">未配置</Text>
        },
        {
            title: '连接状态',
            key: 'status',
            render: (_, record) => {
                const status = statuses[record.id];
                const loading = statusLoading[record.id];

                if (loading) return <Badge status="processing" text="检查中..." />;
                if (!status) return <Badge status="default" text="未知" />;

                return status.connected ?
                    <Badge status="success" text="在线" /> :
                    <Tooltip title={status.message}>
                        <Badge status="error" text="离线" />
                    </Tooltip>;
            }
        },
        {
            title: '资金状况',
            key: 'funds',
            render: (_, record) => {
                const status = statuses[record.id];
                if (!status || !status.connected) return '-';
                return (
                    <Space direction="vertical" size={0}>
                        <Text>净资产: <Text strong>{status.currency} {status.net_liquidation?.toLocaleString()}</Text></Text>
                        <Text type="secondary" style={{ fontSize: '12px' }}>可用: {status.available_funds?.toLocaleString()}</Text>
                    </Space>
                );
            }
        },
        {
            title: '盈亏/市值',
            key: 'pnl',
            render: (_, record) => {
                const status = statuses[record.id];
                if (!status || !status.connected) return '-';
                return (
                    <Space direction="vertical" size={0}>
                        <Text>当日盈亏: <Text color={status.daily_pnl >= 0 ? 'green' : 'red'}>{status.daily_pnl?.toLocaleString()}</Text></Text>
                        <Text type="secondary" style={{ fontSize: '12px' }}>持仓市值: {status.gross_position_value?.toLocaleString()}</Text>
                    </Space>
                );
            }
        },
        {
            title: '操作',
            key: 'action',
            render: (_, record) => (
                <Space>
                    <Tooltip title="刷新状态">
                        <Button
                            icon={<SyncOutlined spin={statusLoading[record.id]} />}
                            onClick={() => checkStatus(record.id)}
                            size="small"
                        />
                    </Tooltip>
                    <Tooltip title="一键部署/更新">
                        <Popconfirm title="确定部署（或更新）该 IB Gateway 容器吗？操作将重启容器。" onConfirm={() => handleDeploy(record.id)}>
                            <Button icon={<ThunderboltOutlined />} size="small" type="primary" ghost />
                        </Popconfirm>
                    </Tooltip>
                    <Tooltip title="重启容器">
                        <Popconfirm title="确定重启该 IB Gateway 容器吗？" onConfirm={() => handleRestart(record.id)}>
                            <Button icon={<ReloadOutlined />} size="small" danger />
                        </Popconfirm>
                    </Tooltip>
                    <Button
                        icon={<EditOutlined />}
                        onClick={() => {
                            setEditingAccount(record);
                            // 转换时间字符串为 dayjs 对象
                            form.setFieldsValue({
                                ...record,
                                auto_restart_time: record.auto_restart_time ? dayjs(record.auto_restart_time, 'hh:mm A') : dayjs('08:59 PM', 'hh:mm A')
                            });
                            setModalVisible(true);
                        }}
                        size="small"
                    />
                    <Popconfirm title="确定删除吗？" onConfirm={() => handleDelete(record.id)}>
                        <Button icon={<DeleteOutlined />} size="small" danger />
                    </Popconfirm>
                </Space>
            )
        }
    ];

    return (
        <div style={{ padding: '24px' }}>
            <Card
                title={
                    <Space>
                        <Title level={4} style={{ margin: 0 }}>IBKR 账户管理</Title>
                        <Text type="secondary">管理 IB Gateway 实例与账户状态</Text>
                    </Space>
                }
                extra={
                    <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() => {
                            setEditingAccount(null);
                            form.resetFields();
                            setModalVisible(true);
                        }}
                    >
                        添加账户
                    </Button>
                }
            >
                <Table
                    dataSource={accounts}
                    columns={columns}
                    rowKey="id"
                    loading={loading}
                    pagination={false}
                />
            </Card>

            <Modal
                title={editingAccount ? "编辑 IB 账户" : "添加 IB 账户"}
                visible={modalVisible}
                onCancel={() => setModalVisible(false)}
                onOk={() => form.submit()}
                width={700}
            >
                <Form
                    form={form}
                    layout="vertical"
                    onFinish={handleSave}
                    initialValues={{
                        ib_host: '127.0.0.1',
                        ib_port: 4001,
                        client_id: 1,
                        trading_mode: 'paper',
                        twofa_timeout_action: 'restart',
                        auto_restart_time: dayjs('08:59 PM', 'hh:mm A'),
                        relogin_after_twofa_timeout: 'yes'
                    }}
                >
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="name" label="账户别名" rules={[{ required: true }]}>
                                <Input placeholder="例如: Paper-01" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="container_name" label="Docker 容器名称">
                                <Input placeholder="例如: ib-gateway-paper" prefix={<ContainerOutlined />} />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={8}>
                            <Form.Item name="ib_host" label="Host">
                                <Input prefix={<GlobalOutlined />} />
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item name="ib_port" label="Port" rules={[{ required: true }]}>
                                <InputNumber style={{ width: '100%' }} />
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item name="client_id" label="Client ID">
                                <InputNumber style={{ width: '100%' }} />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="tws_userid" label="TWS 用户名">
                                <Input />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="tws_password" label="TWS 密码">
                                <Input.Password prefix={<LockOutlined />} />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Form.Item label="高级配置 (Gateway)">
                        <Card size="small" type="inner">
                            <Row gutter={16}>
                                <Col span={12}>
                                    <Form.Item name="twofa_timeout_action" label="2FA 超时操作" style={{ marginBottom: 0 }}>
                                        <Select>
                                            <Select.Option value="restart">重启容器 (restart)</Select.Option>
                                            <Select.Option value="exit">退出 (exit)</Select.Option>
                                        </Select>
                                    </Form.Item>
                                </Col>
                                <Col span={12}>
                                    <Form.Item name="relogin_after_twofa_timeout" label="2FA 超时后重新登录" style={{ marginBottom: 0 }}>
                                        <Select>
                                            <Select.Option value="yes">是 (yes)</Select.Option>
                                            <Select.Option value="no">否 (no)</Select.Option>
                                        </Select>
                                    </Form.Item>
                                </Col>
                            </Row>
                            <Row gutter={16} style={{ marginTop: 12 }}>
                                <Col span={24}>
                                    <Form.Item name="auto_restart_time" label="自动重启时间" style={{ marginBottom: 0 }}>
                                        <TimePicker format="hh:mm A" style={{ width: '100%' }} use12Hours />
                                    </Form.Item>
                                </Col>
                            </Row>
                        </Card>
                    </Form.Item>

                    <Form.Item name="trading_mode" label="交易模式" rules={[{ required: true }]}>
                        <Radio.Group optionType="button" buttonStyle="solid" style={{ width: '100%', display: 'flex' }}>
                            <Radio.Button value="paper" style={{ flex: 1, textAlign: 'center' }}>模拟 (Paper)</Radio.Button>
                            <Radio.Button value="live" style={{ flex: 1, textAlign: 'center' }}>实盘 (Live)</Radio.Button>
                        </Radio.Group>
                    </Form.Item>
                </Form>
            </Modal>
        </div>
    );
};

export default IBKRAccountManager;
