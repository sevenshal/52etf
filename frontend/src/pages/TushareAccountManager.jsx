import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, message, Popconfirm, Space, Tag, Typography } from 'antd';
import { KeyOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import { Navigate } from 'react-router-dom';
import { useAccount } from '../contexts/AccountContext';
import request from '../utils/request';
import { PageShell } from '../components/PageScaffold';

const { Text } = Typography;

const sourceLabel = (source) => {
  if (source === 'PAGE') return '页面配置';
  if (source === 'ENVIRONMENT') return '运行环境';
  return '未配置';
};

const TushareAccountManager = () => {
  const { isAdmin, accountReady } = useAccount();
  const [form] = Form.useForm();
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await request.get('/api/tushare-account');
      setSettings(data);
    } catch (error) {
      message.error(error.response?.data?.detail || '加载 Tushare 账户失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) loadSettings();
  }, [isAdmin, loadSettings]);

  if (!accountReady) return null;
  if (!isAdmin) return <Navigate to="/profile" replace />;

  const save = async (values) => {
    const apiToken = String(values.api_token || '').trim();
    if (!apiToken) {
      message.warning('请输入 Tushare Token；留空不会覆盖现有配置');
      return;
    }
    setSaving(true);
    try {
      const { data } = await request.put('/api/tushare-account', { api_token: apiToken });
      setSettings(data);
      form.resetFields();
      message.success('Tushare Token 已保存，新的数据请求会立即使用它');
    } catch (error) {
      message.error(error.response?.data?.detail || '保存 Tushare Token 失败');
    } finally {
      setSaving(false);
    }
  };

  const clearSavedToken = async () => {
    setClearing(true);
    try {
      const { data } = await request.put('/api/tushare-account', { api_token: '' });
      setSettings(data);
      form.resetFields();
      message.success('页面保存的 Tushare Token 已清除');
    } catch (error) {
      message.error(error.response?.data?.detail || '清除 Tushare Token 失败');
    } finally {
      setClearing(false);
    }
  };

  return (
    <PageShell
      title="Tushare账户"
      subtitle="管理 A 股数据接口令牌"
      actions={<Button icon={<ReloadOutlined />} loading={loading} onClick={loadSettings}>刷新</Button>}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Alert
          type={settings?.configured ? 'success' : 'warning'}
          showIcon
          message={settings?.configured ? 'Tushare 已配置' : '尚未配置 Tushare Token'}
          description="令牌只写入服务端，页面不会返回或显示完整内容。保存后，新的行情和 AI 荐股数据请求立即使用新令牌。"
        />
        <Card title="当前账户" loading={loading}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label="状态">
              <Tag color={settings?.configured ? 'green' : 'default'}>{settings?.configured ? '已配置' : '未配置'}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="来源">{sourceLabel(settings?.source)}</Descriptions.Item>
            <Descriptions.Item label="令牌">{settings?.token_hint || <Text type="secondary">未设置</Text>}</Descriptions.Item>
            <Descriptions.Item label="最近更新">{settings?.updated_at ? new Date(settings.updated_at).toLocaleString() : '-'}</Descriptions.Item>
          </Descriptions>
        </Card>
        <Card title="更新 Token">
          <Form form={form} layout="vertical" onFinish={save} preserve={false}>
            <Form.Item
              name="api_token"
              label="Tushare Token"
              extra="输入新 Token 后保存即可替换。完整 Token 只会在本次提交时发送给服务端。"
              rules={[{ required: true, whitespace: true, message: '请输入 Tushare Token' }, { max: 512, message: 'Token 不能超过 512 个字符' }]}
            >
              <Input.Password prefix={<KeyOutlined />} autoComplete="new-password" placeholder="请输入 Tushare Token" />
            </Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={saving}>保存 Token</Button>
              {settings?.source === 'PAGE' && (
                <Popconfirm title="确定清除页面保存的 Tushare Token？" onConfirm={clearSavedToken}>
                  <Button danger loading={clearing}>清除页面配置</Button>
                </Popconfirm>
              )}
            </Space>
          </Form>
        </Card>
      </Space>
    </PageShell>
  );
};

export default TushareAccountManager;
