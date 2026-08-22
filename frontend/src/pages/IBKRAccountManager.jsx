import React, { useCallback, useState, useEffect } from 'react';
import dayjs from 'dayjs';
import {
    Table, Card, Button, Modal, Form, Input, InputNumber,
    Space, Tag, message, Typography, Popconfirm,
    Badge, Row, Col, Tooltip, Select, TimePicker, Radio
} from 'antd';
import {
    PlusOutlined, ReloadOutlined, SyncOutlined,
    ThunderboltOutlined, DeleteOutlined, EditOutlined,
    GlobalOutlined, ContainerOutlined, LockOutlined,
    FileTextOutlined, StopOutlined
} from '@ant-design/icons';
import request from '../utils/request';

const { Title, Text } = Typography;

const IBKRAccountManager = ({ embedded = false }) => {
    const [accounts, setAccounts] = useState([]);
    const [loading, setLoading] = useState(false);
    const [modalVisible, setModalVisible] = useState(false);
    const [editingAccount, setEditingAccount] = useState(null);
    const [form] = Form.useForm();
    const [statuses, setStatuses] = useState({}); // {accountId: statusData}
    const [statusLoading, setStatusLoading] = useState({});
    const [logVisible, setLogVisible] = useState(false);
    const [currentLogAccount, setCurrentLogAccount] = useState(null);
    const [logs, setLogs] = useState([]);

  const checkStatus = useCallback(async (id) => {
    setStatusLoading(prev => ({ ...prev, [id]: true }));
    try {
      const response = await request.get(`/api/ib-accounts/${id}/status`);
      setStatuses(prev => ({ ...prev, [id]: response.data }));
    } catch (error) {
      setStatuses(prev => ({ ...prev, [id]: { connected: false, message: '连接失败' } }));
    } finally {
      setStatusLoading(prev => ({ ...prev, [id]: false }));
    }
  }, []);

  const fetchAccounts = useCallback(async () => {
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
  }, [checkStatus]);

  useEffect(() => {
    fetchAccounts();
  }, [fetchAccounts]);

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

    const handleRemoveContainer = async (id) => {
        try {
            message.loading({ content: '正在停止并删除容器...', key: 'remove-container' });
            await request.delete(`/api/ib-accounts/${id}/container`);
            message.success({ content: '容器已停止并删除，现在可以删除账户记录', key: 'remove-container' });
            setStatuses(prev => ({ ...prev, [id]: { connected: false, message: '容器已删除' } }));
        } catch (error) {
            message.error({
                content: `删除容器失败: ${error.response?.data?.detail || error.message}`,
                key: 'remove-container'
            });
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
            message.error(error.response?.data?.detail || '删除失败');
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
                    <Tooltip title="查看日志">
                        <Button
                            icon={<FileTextOutlined />}
                            onClick={() => {
                                setCurrentLogAccount(record);
                                setLogVisible(true);
                                setLogs([]);
                            }}
                            size="small"
                        />
                    </Tooltip>
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
                    <Tooltip title="停止并删除容器">
                        <Popconfirm
                            title="确定停止并删除该 IB Gateway 容器吗？该操作不会删除账户记录。"
                            onConfirm={() => handleRemoveContainer(record.id)}
                            disabled={!record.container_name}
                        >
                            <Button icon={<StopOutlined />} size="small" danger disabled={!record.container_name} />
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
                    <Popconfirm title="确定删除账户记录吗？删除前必须先停止并删除容器。" onConfirm={() => handleDelete(record.id)}>
                        <Button icon={<DeleteOutlined />} size="small" danger />
                    </Popconfirm>
                </Space>
            )
        }
    ];

    return (
        <div style={{ padding: embedded ? '16px' : '24px' }}>
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
                            <Form.Item name="container_name" label="Docker 容器名称" rules={[{ required: true, message: '请输入容器名称' }]}>
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

            <Modal
                title={
                    <Space>
                        <FileTextOutlined />
                        <span>容器日志: {currentLogAccount?.name}</span>
                        <Tag color="blue">{currentLogAccount?.container_name}</Tag>
                    </Space>
                }
                visible={logVisible}
                onCancel={() => setLogVisible(false)}
                footer={[
                    <Button key="clear" onClick={() => setLogs([])}>清空</Button>,
                    <Button key="close" type="primary" onClick={() => setLogVisible(false)}>关闭</Button>
                ]}
                width={800}
                destroyOnClose
            >
                <LogViewer accountId={currentLogAccount?.id} visible={logVisible} logs={logs} setLogs={setLogs} />
            </Modal>
        </div>
    );
};

const LogViewer = ({ accountId, visible, logs, setLogs }) => {
    const scrollRef = React.useRef(null);

    useEffect(() => {
        if (!visible || !accountId) return;

        const apiUrl = process.env.REACT_APP_API_URL || '';
        let wsHost = '';

        if (apiUrl) {
            // 如果定义了 REACT_APP_API_URL (例如 https://api.52etf.vip)
            // 去掉协议头，换成 ws/wss
            wsHost = apiUrl.replace(/^http/, 'ws');
        } else {
            // 默认回退到当前 window.location
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            wsHost = `${protocol}//${window.location.host}`;
        }

        const wsUrl = `${wsHost}/api/ib-accounts/${accountId}/logs`;
        const ws = new WebSocket(wsUrl);

        ws.onmessage = (event) => {
            setLogs(prev => [...prev, event.data].slice(-500)); // 最多保留500行
        };

        ws.onclose = () => {
            setLogs(prev => [...prev, '\n--- 连接已断开 ---']);
        };

        ws.onerror = () => {
            setLogs(prev => [...prev, '\n--- 发生错误，无法连接日志服务器 ---']);
        };

        return () => ws.close();
    }, [accountId, setLogs, visible]);

    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
    }, [logs]);

    return (
        <div
            ref={scrollRef}
            style={{
                backgroundColor: '#1e1e1e',
                color: '#d4d4d4',
                padding: '12px',
                borderRadius: '4px',
                height: '400px',
                overflowY: 'auto',
                fontFamily: 'monospace',
                whiteSpace: 'pre-wrap',
                fontSize: '12px',
                border: '1px solid #333'
            }}
        >
            {logs.length === 0 ? <div style={{ color: '#666', textAlign: 'center', marginTop: '180px' }}>正在加载日志...</div> : logs.join('')}
        </div>
    );
};

export default IBKRAccountManager;
