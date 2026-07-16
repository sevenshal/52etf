import React, { useCallback, useEffect, useState } from 'react';
import { Button, Form, Input, message, Modal, Popconfirm, Space, Switch, Table, Tag } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { Navigate } from 'react-router-dom';
import { useAccount } from '../contexts/AccountContext';
import request from '../utils/request';
import { PageShell } from '../components/PageScaffold';

const WebAccountManager = () => {
  const { isAdmin, accountReady } = useAccount();
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editingAccount, setEditingAccount] = useState(null);
  const [form] = Form.useForm();

  const loadAccounts = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await request.get('/api/profile/accounts');
      setAccounts(data);
    } catch (error) {
      message.error(error.response?.data?.detail || '加载账户失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) loadAccounts();
  }, [isAdmin, loadAccounts]);

  if (!accountReady) return null;
  if (!isAdmin) return <Navigate to="/profile" replace />;

  const createAccount = async (values) => {
    setSaving(true);
    try {
      if (editingAccount) {
        await request.patch(`/api/profile/accounts/${encodeURIComponent(editingAccount.account_id)}`, { note: values.note || '' });
        message.success('备注已保存');
      } else {
        await request.post('/api/profile/accounts', { account_id: values.accountId, note: values.note || '', enabled: true });
        message.success('账户已添加');
      }
      setOpen(false);
      setEditingAccount(null);
      form.resetFields();
      loadAccounts();
    } catch (error) {
      message.error(error.response?.data?.detail || '添加账户失败');
    } finally {
      setSaving(false);
    }
  };

  const setEnabled = async (record, enabled) => {
    try {
      await request.patch(`/api/profile/accounts/${encodeURIComponent(record.account_id)}`, { enabled });
      message.success(enabled ? '账户已启用' : '账户已停用');
      loadAccounts();
    } catch (error) {
      message.error(error.response?.data?.detail || '更新账户失败');
    }
  };

  const removeAccount = async (record) => {
    try {
      await request.delete(`/api/profile/accounts/${encodeURIComponent(record.account_id)}`);
      message.success('账户已删除');
      loadAccounts();
    } catch (error) {
      message.error(error.response?.data?.detail || '删除账户失败');
    }
  };

  const columns = [
    { title: '账户ID', dataIndex: 'account_id', render: (value, record) => <Space>{value}{record.is_admin && <Tag color="gold">管理员</Tag>}</Space> },
    { title: '备注', dataIndex: 'note', render: (value) => value || '-' },
    { title: '状态', dataIndex: 'enabled', width: 120, render: (enabled) => <Tag color={enabled ? 'green' : 'default'}>{enabled ? '已启用' : '已停用'}</Tag> },
    { title: '创建时间', dataIndex: 'created_at', width: 210, render: (value) => value ? new Date(value).toLocaleString() : '-' },
    {
      title: '操作', width: 180, render: (_, record) => <Space>
        <Button type="link" onClick={() => {
          setEditingAccount(record);
          form.setFieldsValue({ note: record.note || '' });
          setOpen(true);
        }}>备注</Button>
        <Switch checked={record.enabled} disabled={record.is_admin} onChange={(checked) => setEnabled(record, checked)} />
        <Popconfirm title="确定删除这个账户？" disabled={record.is_admin} onConfirm={() => removeAccount(record)}>
          <Button danger type="link" disabled={record.is_admin}>删除</Button>
        </Popconfirm>
      </Space>
    },
  ];

  return <PageShell title="系统账户管理" actions={<Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditingAccount(null); form.resetFields(); setOpen(true); }}>添加账户</Button>}>
    <Table rowKey="account_id" loading={loading} dataSource={accounts} columns={columns} pagination={false} />
    <Modal title={editingAccount ? '设置备注' : '添加账户'} open={open} onCancel={() => { setOpen(false); setEditingAccount(null); }} onOk={() => form.submit()} confirmLoading={saving} destroyOnClose>
      <Form form={form} layout="vertical" onFinish={createAccount} preserve={false}>
        {!editingAccount && <Form.Item name="accountId" label="账户ID" rules={[{ required: true, whitespace: true, message: '请输入账户ID' }, { max: 128 }]}>
          <Input placeholder="请输入新账户ID" autoComplete="off" />
        </Form.Item>}
        <Form.Item name="note" label="备注" rules={[{ max: 500, message: '备注不能超过500个字符' }]}>
          <Input.TextArea placeholder="例如：家人账户、测试账户" rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  </PageShell>;
};

export default WebAccountManager;
