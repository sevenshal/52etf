import React, { useEffect, useState } from 'react';
import dayjs from 'dayjs';
import {
  Badge,
  Button,
  Card,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
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
const formatNumber = (value, digits = 0) => {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return '-';
  return num.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
};
const DEFAULT_EXECUTOR_SEQUENCE = [1, 2, 3, 5, -1];
const priceLevelLabel = value => {
  if (value === -1) return 'PTrade兜底';
  if (value === 0) return '最新价';
  return `${value}档`;
};
const priceLevelOptions = [-1, 0, 1, 2, 3, 4, 5].map(value => ({ value, label: priceLevelLabel(value) }));
const sequenceToText = value => (Array.isArray(value) && value.length ? value : DEFAULT_EXECUTOR_SEQUENCE).join(',');
const parseSequence = (value, fallback = DEFAULT_EXECUTOR_SEQUENCE) => {
  if (Array.isArray(value)) return value;
  const text = String(value || '').trim();
  if (!text) return fallback;
  const parsed = text.split(',').map(item => Number(item.trim())).filter(item => [-1, 0, 1, 2, 3, 4, 5].includes(item));
  return parsed.length ? Array.from(new Set(parsed)) : fallback;
};
const formatPolicy = policy => {
  if (!policy) return '-';
  const level = policy.price_level ?? policy.executor_price_level;
  const timeout = policy.order_timeout_seconds ?? policy.executor_order_timeout_seconds;
  const maxReplace = policy.max_replace_count ?? policy.executor_max_replace_count;
  const sequence = sequenceToText(policy.price_level_sequence ?? policy.executor_price_level_sequence);
  return `${priceLevelLabel(level)} / ${timeout || '-'}s / 重定价${maxReplace ?? '-'}次 / ${sequence}`;
};

const ExternalTradingAccountManager = () => {
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modalVisible, setModalVisible] = useState(false);
  const [editingAccount, setEditingAccount] = useState(null);
  const [subAccounts, setSubAccounts] = useState({});
  const [subModalVisible, setSubModalVisible] = useState(false);
  const [editingSubAccount, setEditingSubAccount] = useState(null);
  const [activeAccountForSub, setActiveAccountForSub] = useState(null);
  const [executorModalVisible, setExecutorModalVisible] = useState(false);
  const [executorAccount, setExecutorAccount] = useState(null);
  const [executorPlan, setExecutorPlan] = useState(null);
  const [executorLoading, setExecutorLoading] = useState(false);
  const [form] = Form.useForm();
  const [subForm] = Form.useForm();

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
    form.setFieldsValue({
      enabled: true,
      executor_enabled: true,
      executor_price_level: 1,
      executor_lot_size: 100,
      executor_order_timeout_seconds: 120,
      executor_max_replace_count: 3,
      executor_price_level_sequence: sequenceToText(DEFAULT_EXECUTOR_SEQUENCE)
    });
    setModalVisible(true);
  };

  const openEditModal = record => {
    setEditingAccount(record);
    form.setFieldsValue({
      name: record.name,
      identifier: record.identifier,
      remark: record.remark,
      enabled: record.enabled,
      executor_enabled: record.executor_enabled !== false,
      executor_price_level: record.executor_price_level ?? 1,
      executor_lot_size: record.executor_lot_size ?? 100,
      executor_order_timeout_seconds: record.executor_order_timeout_seconds ?? 120,
      executor_max_replace_count: record.executor_max_replace_count ?? 3,
      executor_price_level_sequence: sequenceToText(record.executor_price_level_sequence)
    });
    setModalVisible(true);
  };

  const handleSave = async values => {
    setSaving(true);
    try {
      const payload = {
        ...values,
        remark: values.remark || null,
        enabled: values.enabled !== false,
        executor_enabled: values.executor_enabled !== false,
        executor_price_level_sequence: parseSequence(values.executor_price_level_sequence)
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

  const fetchSubAccounts = async accountId => {
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${accountId}/sub-accounts`);
      setSubAccounts(prev => ({ ...prev, [accountId]: data || [] }));
    } catch (error) {
      message.error(error.response?.data?.detail || '获取虚拟子账户失败');
    }
  };

  const openSubCreateModal = record => {
    setActiveAccountForSub(record);
    setEditingSubAccount(null);
    subForm.resetFields();
    subForm.setFieldsValue({
      enabled: true,
      cash_allocated: 0,
      executor_price_level: null,
      executor_lot_size: null,
      executor_order_timeout_seconds: null,
      executor_max_replace_count: null,
      executor_price_level_sequence: ''
    });
    setSubModalVisible(true);
  };

  const openSubEditModal = (account, subAccount) => {
    setActiveAccountForSub(account);
    setEditingSubAccount(subAccount);
    subForm.setFieldsValue({
      name: subAccount.name,
      cash_allocated: subAccount.cash_allocated,
      remark: subAccount.remark,
      enabled: subAccount.enabled,
      executor_price_level: subAccount.executor_price_level,
      executor_lot_size: subAccount.executor_lot_size,
      executor_order_timeout_seconds: subAccount.executor_order_timeout_seconds,
      executor_max_replace_count: subAccount.executor_max_replace_count,
      executor_price_level_sequence: Array.isArray(subAccount.executor_price_level_sequence)
        ? sequenceToText(subAccount.executor_price_level_sequence)
        : ''
    });
    setSubModalVisible(true);
  };

  const handleSaveSubAccount = async values => {
    if (!activeAccountForSub?.id) return;
    setSaving(true);
    try {
      const payload = {
        ...values,
        cash_allocated: Number(values.cash_allocated || 0),
        remark: values.remark || null,
        enabled: values.enabled !== false,
        executor_price_level: values.executor_price_level ?? null,
        executor_lot_size: values.executor_lot_size ?? null,
        executor_order_timeout_seconds: values.executor_order_timeout_seconds ?? null,
        executor_max_replace_count: values.executor_max_replace_count ?? null,
        executor_price_level_sequence: values.executor_price_level_sequence
          ? parseSequence(values.executor_price_level_sequence, [])
          : null
      };
      if (editingSubAccount) {
        await request.put(`/api/external-trading-accounts/${activeAccountForSub.id}/sub-accounts/${editingSubAccount.id}`, payload);
      } else {
        await request.post(`/api/external-trading-accounts/${activeAccountForSub.id}/sub-accounts`, payload);
      }
      message.success('虚拟子账户已保存');
      setSubModalVisible(false);
      fetchSubAccounts(activeAccountForSub.id);
    } catch (error) {
      message.error(error.response?.data?.detail || '保存虚拟子账户失败');
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteSubAccount = async (account, subAccount) => {
    try {
      await request.delete(`/api/external-trading-accounts/${account.id}/sub-accounts/${subAccount.id}`);
      message.success('虚拟子账户已删除');
      fetchSubAccounts(account.id);
    } catch (error) {
      message.error(error.response?.data?.detail || '删除虚拟子账户失败');
    }
  };

  const previewNettedExecutor = async account => {
    setExecutorAccount(account);
    setExecutorModalVisible(true);
    setExecutorLoading(true);
    try {
      const { data } = await request.post(`/api/external-trading-accounts/${account.id}/executor/preview`, {});
      setExecutorPlan(data?.plan || null);
    } catch (error) {
      message.error(error.response?.data?.detail || '生成净额撮合预览失败');
      setExecutorPlan(null);
    } finally {
      setExecutorLoading(false);
    }
  };

  const executeNettedExecutor = async () => {
    if (!executorAccount?.id) return;
    setExecutorLoading(true);
    try {
      const { data } = await request.post(`/api/external-trading-accounts/${executorAccount.id}/executor/execute`, {});
      const accountResult = data?.accounts?.[0] || {};
      setExecutorPlan(accountResult?.plan || null);
      if (data?.status === 'SKIPPED' && data?.reason === 'market_closed') {
        message.warning(`当前不在 A股交易时段，执行器将在 ${formatTime(data.next_run_at)} 后继续处理`);
      } else if (accountResult?.status === 'CANCEL_REQUESTED') {
        message.success('已提交撤单，等待回报后执行器会继续撮合');
      } else {
        message.success(accountResult?.result?.message || '已触发净额撮合执行器');
      }
      fetchSubAccounts(executorAccount.id);
    } catch (error) {
      message.error(error.response?.data?.detail || '执行净额撮合失败');
    } finally {
      setExecutorLoading(false);
    }
  };

  const demandColumns = [
    { title: '子账户', dataIndex: 'sub_account_name', key: 'sub_account_name', width: 180 },
    { title: '策略', dataIndex: 'strategy_type', key: 'strategy_type', width: 150, render: value => value || '-' },
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 120 },
    { title: '方向', dataIndex: 'side', key: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '当前', dataIndex: 'current_quantity', key: 'current_quantity', width: 100, render: value => formatNumber(value) },
    { title: '目标', dataIndex: 'target_quantity', key: 'target_quantity', width: 100, render: value => formatNumber(value) },
    { title: '执行策略', dataIndex: 'execution_policy', key: 'execution_policy', width: 240, render: formatPolicy }
  ];

  const internalCrossColumns = [
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 120 },
    { title: '撮合数量', dataIndex: 'quantity', key: 'quantity', width: 110, render: value => formatNumber(value) },
    { title: '参考价', dataIndex: 'price', key: 'price', width: 100, render: value => value ? formatNumber(value, 4) : '-' },
    { title: '买方分配', dataIndex: 'buy_allocations', key: 'buy_allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' },
    { title: '卖方分配', dataIndex: 'sell_allocations', key: 'sell_allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' },
    { title: '状态', dataIndex: 'status', key: 'status', width: 120, render: value => <Tag color={value === 'READY' ? 'green' : 'orange'}>{value}</Tag> }
  ];

  const externalOrderColumns = [
    { title: '标的', dataIndex: 'symbol', key: 'symbol', width: 120 },
    { title: '方向', dataIndex: 'side', key: 'side', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 100, render: value => formatNumber(value) },
    { title: '类型', dataIndex: 'order_type', key: 'order_type', width: 90 },
    { title: '限价规则', dataIndex: 'price_level', key: 'price_level', width: 100, render: priceLevelLabel },
    { title: '执行策略', dataIndex: 'execution_policy', key: 'execution_policy', width: 250, render: formatPolicy },
    { title: '分配', dataIndex: 'allocations', key: 'allocations', render: value => (value || []).map(item => `${item.sub_account_name}:${formatNumber(item.quantity)}`).join(' / ') || '-' }
  ];

  const expandedRowRender = account => {
    const rows = subAccounts[account.id] || [];
    const subColumns = [
      { title: '子账户', dataIndex: 'name', key: 'name' },
      {
        title: '绑定策略',
        dataIndex: 'binding_label',
        key: 'binding_label',
        render: (_, record) => record.binding_status === 'BOUND'
          ? <Tag color="blue">{record.strategy_name || record.binding_label}</Tag>
          : <Tag>空闲</Tag>
      },
      { title: '分配资金', dataIndex: 'cash_allocated', key: 'cash_allocated', render: value => Number(value || 0).toLocaleString() },
      { title: '可用资金', dataIndex: 'cash_available', key: 'cash_available', render: value => Number(value || 0).toLocaleString() },
      { title: '执行策略', dataIndex: 'effective_executor_policy', key: 'effective_executor_policy', width: 260, render: formatPolicy },
      { title: '持仓数', dataIndex: 'positions', key: 'positions', render: value => (value || []).length },
      { title: '启用', dataIndex: 'enabled', key: 'enabled', render: value => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
      {
        title: '操作',
        key: 'action',
        render: (_, subAccount) => (
          <Space>
            <Button size="small" icon={<EditOutlined />} onClick={() => openSubEditModal(account, subAccount)}>
              编辑
            </Button>
            <Popconfirm title="确定删除这个虚拟子账户吗？" onConfirm={() => handleDeleteSubAccount(account, subAccount)}>
              <Button size="small" icon={<DeleteOutlined />} danger>
                删除
              </Button>
            </Popconfirm>
          </Space>
        )
      }
    ];
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Button size="small" icon={<PlusOutlined />} onClick={() => openSubCreateModal(account)}>
          添加虚拟子账户
        </Button>
        <Button size="small" icon={<SyncOutlined />} onClick={() => previewNettedExecutor(account)}>
          净额撮合
        </Button>
        <Table rowKey="id" columns={subColumns} dataSource={rows} pagination={false} size="small" scroll={{ x: 1180 }} />
      </Space>
    );
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
      title: '执行策略',
      key: 'executor_policy',
      width: 260,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text>{formatPolicy(record)}</Text>
          <Text type={record.executor_enabled ? 'secondary' : 'danger'}>
            {record.executor_enabled ? '定时兜底启用' : '定时兜底停用'}
          </Text>
        </Space>
      )
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
          scroll={{ x: 1180 }}
          expandable={{
            expandedRowRender,
            onExpand: (expanded, record) => {
              if (expanded && !subAccounts[record.id]) fetchSubAccounts(record.id);
            }
          }}
        />
      </Card>

      <Modal
        title={editingAccount ? '编辑外部交易账号' : '添加外部交易账号'}
        visible={modalVisible}
        onCancel={() => setModalVisible(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        width={720}
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
          <Divider orientation="left">默认执行策略</Divider>
          <Form.Item name="executor_enabled" label="定时兜底执行器" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
          <Form.Item name="executor_price_level" label="初始限价档位" rules={[{ required: true, message: '请选择初始限价档位' }]}>
            <Select options={priceLevelOptions} />
          </Form.Item>
          <Form.Item name="executor_price_level_sequence" label="重定价档位序列">
            <Input placeholder="例如：1,2,3,5,-1" />
          </Form.Item>
          <Form.Item name="executor_order_timeout_seconds" label="订单超时秒数" rules={[{ required: true, message: '请输入订单超时秒数' }]}>
            <InputNumber min={10} max={3600} step={10} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="executor_max_replace_count" label="最大重定价次数" rules={[{ required: true, message: '请输入最大重定价次数' }]}>
            <InputNumber min={0} max={20} step={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="executor_lot_size" label="默认最小交易单位" rules={[{ required: true, message: '请输入默认最小交易单位' }]}>
            <InputNumber min={1} step={100} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editingSubAccount ? '编辑虚拟子账户' : '添加虚拟子账户'}
        visible={subModalVisible}
        onCancel={() => setSubModalVisible(false)}
        onOk={() => subForm.submit()}
        confirmLoading={saving}
        width={640}
      >
        <Form
          form={subForm}
          layout="vertical"
          onFinish={handleSaveSubAccount}
          initialValues={{ enabled: true, cash_allocated: 0 }}
        >
          <Form.Item name="name" label="子账户名" rules={[{ required: true, message: '请输入子账户名' }]}>
            <Input placeholder="例如：W20 实盘账本" />
          </Form.Item>
          <Form.Item name="cash_allocated" label="分配资金">
            <InputNumber min={0} step={10000} style={{ width: '100%' }} />
          </Form.Item>
          {editingSubAccount ? (
            <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }} size={4}>
              <Text type="secondary">可用资金</Text>
              <Text>{formatNumber(editingSubAccount.cash_available, 2)}</Text>
              <Text type="secondary">绑定策略</Text>
              <Text>{editingSubAccount.strategy_name || editingSubAccount.binding_label || '空闲'}</Text>
              <Text type="secondary">当前生效执行策略</Text>
              <Text>{formatPolicy(editingSubAccount.effective_executor_policy)}</Text>
            </Space>
          ) : null}
          <Divider orientation="left">执行策略覆盖</Divider>
          <Text type="secondary">留空则继承外部交易账户默认策略；W20 绑定保存时会同步它的限价档位和最小交易单位。</Text>
          <Form.Item name="executor_price_level" label="初始限价档位">
            <Select allowClear options={priceLevelOptions} placeholder="继承账户默认" />
          </Form.Item>
          <Form.Item name="executor_price_level_sequence" label="重定价档位序列">
            <Input placeholder="留空继承，例如：1,2,3,5,-1" />
          </Form.Item>
          <Form.Item name="executor_order_timeout_seconds" label="订单超时秒数">
            <InputNumber min={10} max={3600} step={10} style={{ width: '100%' }} placeholder="继承账户默认" />
          </Form.Item>
          <Form.Item name="executor_max_replace_count" label="最大重定价次数">
            <InputNumber min={0} max={20} step={1} style={{ width: '100%' }} placeholder="继承账户默认" />
          </Form.Item>
          <Form.Item name="executor_lot_size" label="最小交易单位">
            <InputNumber min={1} step={100} style={{ width: '100%' }} placeholder="继承账户默认" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={3} placeholder="可选" />
          </Form.Item>
          <Form.Item name="enabled" label="是否启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`净额撮合 - ${executorAccount?.name || ''}`}
        visible={executorModalVisible}
        onCancel={() => setExecutorModalVisible(false)}
        onOk={executeNettedExecutor}
        okText="执行净额限价单"
        cancelText="关闭"
        confirmLoading={executorLoading}
        width={1080}
        okButtonProps={{
          disabled: executorLoading || !executorPlan || (!executorPlan.external_orders?.length && !executorPlan.internal_crosses?.length)
        }}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          <Text type="secondary">
            {executorPlan?.connected ? '账户在线，预览已带入最新参考价。' : '账户离线，预览不会带入实时参考价。'}
          </Text>
          <Table
            title={() => '子账户目标差额'}
            rowKey={(record, index) => `${record.sub_account_id}-${record.symbol}-${record.side}-${index}`}
            columns={demandColumns}
            dataSource={executorPlan?.demands || []}
            pagination={false}
            loading={executorLoading && !executorPlan}
            size="small"
            scroll={{ x: 1080 }}
          />
          <Table
            title={() => '内部撮合'}
            rowKey={(record, index) => `${record.symbol}-${index}`}
            columns={internalCrossColumns}
            dataSource={executorPlan?.internal_crosses || []}
            pagination={false}
            size="small"
            scroll={{ x: 1000 }}
          />
          <Table
            title={() => '提交到 PTrade 的净额限价单'}
            rowKey={(record, index) => `${record.symbol}-${record.side}-${index}`}
            columns={externalOrderColumns}
            dataSource={executorPlan?.external_orders || []}
            pagination={false}
            size="small"
            scroll={{ x: 1180 }}
          />
        </Space>
      </Modal>
    </div>
  );
};

export default ExternalTradingAccountManager;
