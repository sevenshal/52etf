import React, { useEffect, useState } from 'react';
import dayjs from 'dayjs';
import {
  Badge,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  SyncOutlined
} from '@ant-design/icons';
import request from '../utils/request';

const { Text, Title } = Typography;

const formatTime = value => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-');

const ExternalTradingAccountManager = () => {
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [editingAccount, setEditingAccount] = useState(null);
  const [form] = Form.useForm();

  const fetchAccounts = async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    try {
      const { data } = await request.get('/api/external-trading-accounts');
      setAccounts(data || []);
    } catch (error) {
      if (!silent) {
        message.error('获取外部交易账号失败');
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    fetchAccounts();
    const timer = setInterval(() => fetchAccounts(true), 5000);
    return () => clearInterval(timer);
  }, []);

  const openCreateModal = () => {
    setEditingAccount(null);
    form.resetFields();
    form.setFieldsValue({ enabled: true });
    setModalVisible(true);
  };

  const openEditModal = record => {
    setEditingAccount(record);
    form.setFieldsValue({
      name: record.name,
      identifier: record.identifier,
      remark: record.remark,
      enabled: record.enabled
    });
    setModalVisible(true);
  };

  const handleSave = async values => {
    setSaving(true);
    try {
      const payload = {
        ...values,
        remark: values.remark || null,
        enabled: values.enabled !== false
      };
      if (editingAccount) {
        await request.put(`/api/external-trading-accounts/${editingAccount.id}`, payload);
        message.success('更新成功');
      } else {
        await request.post('/api/external-trading-accounts', payload);
        message.success('添加成功');
      }
      setModalVisible(false);
      fetchAccounts();
    } catch (error) {
      message.error(error.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async id => {
    try {
      await request.delete(`/api/external-trading-accounts/${id}`);
      message.success('删除成功');
      fetchAccounts();
    } catch (error) {
      message.error(error.response?.data?.detail || '删除失败');
    }
  };

  const columns = [
    {
      title: '账户名',
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{text}</Text>
          {record.remark ? <Text type="secondary">{record.remark}</Text> : null}
        </Space>
      )
    },
    {
      title: '唯一标识',
      dataIndex: 'identifier',
      key: 'identifier',
      render: value => <Tag>{value}</Tag>
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 90,
      render: value => (value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>)
    },
    {
      title: '连接状态',
      key: 'connected',
      width: 140,
      render: (_, record) => {
        if (record.connected) {
          return <Badge status="success" text="在线" />;
        }
        return (
          <Tooltip title={record.last_disconnect_reason || ''}>
            <Badge status="default" text="离线" />
          </Tooltip>
        );
      }
    },
    {
      title: '最近心跳',
      key: 'last_seen_at',
      render: (_, record) => formatTime(record.runtime_last_seen_at || record.last_seen_at)
    },
    {
      title: '最近连接',
      dataIndex: 'last_connected_at',
      key: 'last_connected_at',
      render: formatTime
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      render: (_, record) => (
        <Space>
          <Tooltip title="刷新">
            <Button icon={<SyncOutlined />} size="small" onClick={() => fetchAccounts()} />
          </Tooltip>
          <Tooltip title="编辑">
            <Button icon={<EditOutlined />} size="small" onClick={() => openEditModal(record)} />
          </Tooltip>
          <Popconfirm title="确定删除这个外部交易账号吗？" onConfirm={() => handleDelete(record.id)}>
            <Button icon={<DeleteOutlined />} size="small" danger />
          </Popconfirm>
        </Space>
      )
    }
  ];

  return (
    <div style={{ padding: 24 }}>
      <Card
        title={
          <Space>
            <Title level={4} style={{ margin: 0 }}>外部交易账号</Title>
            <Text type="secondary">PTrade 与券商侧长连接</Text>
          </Space>
        }
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
            添加账号
          </Button>
        }
      >
        <Table
          rowKey="id"
          columns={columns}
          dataSource={accounts}
          loading={loading}
          pagination={false}
        />
      </Card>

      <Modal
        title={editingAccount ? '编辑外部交易账号' : '添加外部交易账号'}
        visible={modalVisible}
        onCancel={() => setModalVisible(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        width={600}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSave}
          initialValues={{ enabled: true }}
        >
          <Form.Item name="name" label="账户名" rules={[{ required: true, message: '请输入账户名' }]}>
            <Input placeholder="例如：PTrade-A股实盘" />
          </Form.Item>
          <Form.Item name="identifier" label="唯一标识" rules={[{ required: true, message: '请输入唯一标识' }]}>
            <Input placeholder="例如：GS66301027527" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={3} placeholder="可选" />
          </Form.Item>
          <Form.Item name="enabled" label="是否启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default ExternalTradingAccountManager;
