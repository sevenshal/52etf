import React, { useState, useEffect } from 'react';
import dayjs from 'dayjs';
import {
    Table, Card, Button, Modal, Form, Input,
    Space, message, Typography, Popconfirm,
    Row, Col, Tag as AntTag, Badge, Tooltip
} from 'antd';
import {
    PlusOutlined, DeleteOutlined, KeyOutlined,
    UserOutlined, IdcardOutlined, EditOutlined, SyncOutlined
} from '@ant-design/icons';
import request from '../utils/request';

// Rename imported Tag to avoid conflict with our helper if needed, though we shadow it anyway. 
// Actually we can just avoid importing Tag from antd if we don't use it directly or use AntTag.
// But we use <Tag> as a custom component below. So let's keep AntTag alias in case.

const { Title, Text } = Typography;

const LongPortAccountManager = () => {
    const [accounts, setAccounts] = useState([]);
    const [loading, setLoading] = useState(false);
    const [modalVisible, setModalVisible] = useState(false);
    const [editingAccount, setEditingAccount] = useState(null);
    const [form] = Form.useForm();
    const [statuses, setStatuses] = useState({});

    useEffect(() => {
        fetchAccounts();
    }, []);

    const fetchAccounts = async () => {
        setLoading(true);
        try {
            const response = await request.get('/api/longport-accounts');
            setAccounts(response.data);
            // Check status for each
            response.data.forEach(acc => checkStatus(acc.id));
        } catch (error) {
            message.error('获取账户列表失败');
        } finally {
            setLoading(false);
        }
    };

    const checkStatus = async (id) => {
        try {
            const response = await request.get(`/api/longport-accounts/${id}/status`);
            setStatuses(prev => ({ ...prev, [id]: response.data }));
        } catch (error) {
            setStatuses(prev => ({ ...prev, [id]: { status: 'error', message: '检查失败' } }));
        }
    };

    const handleSave = async (values) => {
        try {
            if (editingAccount) {
                await request.put(`/api/longport-accounts/${editingAccount.id}`, {
                    ...values,
                    lp_account_id: values.lp_account_id // Although usually ID is not editable, we pass it for validation if needed, but backend uses ID from URL
                });
                message.success('更新成功');
            } else {
                await request.post('/api/longport-accounts', values);
                message.success('添加成功');
            }
            setModalVisible(false);
            fetchAccounts();
        } catch (error) {
            message.error(error.response?.data?.detail || '保存失败');
        }
    };

    const handleDelete = async (id) => {
        try {
            await request.delete(`/api/longport-accounts/${id}`);
            message.success('删除成功');
            fetchAccounts();
        } catch (error) {
            message.error('删除失败');
        }
    };

    // ...

    const columns = [
        {
            title: '账户名称',
            dataIndex: 'name',
            key: 'name',
            render: (text) => <Text strong>{text}</Text>
        },
        {
            title: '长桥账号ID',
            dataIndex: 'lp_account_id',
            key: 'lp_account_id',
            render: (text) => <Tag>{text}</Tag>
        },
        {
            title: '状态',
            key: 'status',
            render: (_, record) => {
                const status = statuses[record.id];
                if (!status) return <Badge status="default" text="检查中..." />;
                return status.status === 'ok' ?
                    <Badge status="success" text="正常" /> :
                    <Tooltip title={status.message}>
                        <Badge status="error" text="异常" />
                    </Tooltip>;
            }
        },
        {
            title: 'App Key',
            dataIndex: 'app_key',
            key: 'app_key',
            render: (text) => <Text type="secondary">{text ? text.substring(0, 8) + '...' : '-'}</Text>
        },
        {
            title: '创建时间',
            dataIndex: 'created_at',
            key: 'created_at',
            render: (text) => dayjs(text).format('YYYY-MM-DD HH:mm:ss')
        },
        {
            title: '操作',
            key: 'action',
            render: (_, record) => (
                <Space>
                    <Tooltip title="刷新状态">
                        <Button
                            icon={<SyncOutlined />}
                            size="small"
                            onClick={() => checkStatus(record.id)}
                        />
                    </Tooltip>
                    <Button
                        icon={<EditOutlined />}
                        size="small"
                        onClick={() => {
                            setEditingAccount(record);
                            form.setFieldsValue(record);
                            setModalVisible(true);
                        }}
                    />
                    <Popconfirm title="确定删除吗？" onConfirm={() => handleDelete(record.id)}>
                        <Button icon={<DeleteOutlined />} size="small" danger />
                    </Popconfirm>
                </Space>
            )
        }
    ];

    // Helper component for Tag
    const Tag = ({ children }) => <span style={{ backgroundColor: '#f5f5f5', border: '1px solid #d9d9d9', borderRadius: '2px', padding: '0 7px', fontSize: '12px', display: 'inline-block' }}>{children}</span>;

    return (
        <div style={{ padding: '24px' }}>
            <Card
                title={
                    <Space>
                        <Title level={4} style={{ margin: 0 }}>长桥账户管理</Title>
                        <Text type="secondary">配置长桥 OpenApi 账户信息</Text>
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
                title={editingAccount ? "编辑长桥账户" : "添加长桥账户"}
                visible={modalVisible}
                onCancel={() => setModalVisible(false)}
                onOk={() => form.submit()}
                width={600}
            >
                <Form
                    form={form}
                    layout="vertical"
                    onFinish={handleSave}
                >
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="name" label="账户名称" rules={[{ required: true }]}>
                                <Input placeholder="例如: 主账户" prefix={<UserOutlined />} />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item
                                name="lp_account_id"
                                label="长桥账号ID"
                                rules={[{ required: true }]}
                            >
                                <Input
                                    placeholder="例如: LBPT..."
                                    prefix={<IdcardOutlined />}
                                    disabled={!!editingAccount} // Disable ID editing if desired, or allow it but check uniqueness
                                />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Form.Item name="app_key" label="App Key" rules={[{ required: true }]}>
                        <Input prefix={<KeyOutlined />} />
                    </Form.Item>

                    <Form.Item name="app_secret" label="App Secret" rules={[{ required: true }]}>
                        <Input.Password prefix={<KeyOutlined />} />
                    </Form.Item>

                    <Form.Item name="access_token" label="Access Token" extra="初始 Access Token，可选 (更新时填入可修改)">
                        <Input.Password prefix={<KeyOutlined />} />
                    </Form.Item>
                </Form>
            </Modal>
        </div>
    );
};

export default LongPortAccountManager;
