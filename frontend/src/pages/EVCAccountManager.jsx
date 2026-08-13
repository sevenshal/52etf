import React, { useCallback, useEffect, useState } from 'react';
import dayjs from 'dayjs';
import { Badge, Button, Card, Form, Input, Space, Typography, message } from 'antd';
import { KeyOutlined, MailOutlined, LoginOutlined, SaveOutlined } from '@ant-design/icons';
import request from '../utils/request';

const { Title, Text } = Typography;

const EVCAccountManager = () => {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [loginLoading, setLoginLoading] = useState(false);
  const [accountInfo, setAccountInfo] = useState(null);

  const fetchAccount = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await request.get('/api/evc-accounts');
      setAccountInfo(data);
      form.setFieldsValue({
        username: data.username || '',
        password: '',
      });
    } catch (error) {
      message.error('获取 EVC 账户失败');
    } finally {
      setLoading(false);
    }
  }, [form]);

  useEffect(() => {
    fetchAccount();
  }, [fetchAccount]);

  const handleSave = async (values) => {
    setLoading(true);
    try {
      const payload = {
        username: values.username,
      };
      if (values.password) {
        payload.password = values.password;
      }
      const { data } = await request.post('/api/evc-accounts', payload);
      setAccountInfo(data);
      form.setFieldValue('password', '');
      message.success('EVC 账户已保存');
    } catch (error) {
      message.error(error.response?.data?.detail || '保存失败');
    } finally {
      setLoading(false);
    }
  };

  const handleLogin = async () => {
    setLoginLoading(true);
    try {
      const { data } = await request.post('/api/evc-accounts/login');
      message.success(`登录成功${data.cookie_expires_at ? `，Cookie 到期时间 ${dayjs(data.cookie_expires_at).format('YYYY-MM-DD HH:mm:ss')}` : ''}`);
      fetchAccount();
    } catch (error) {
      message.error(error.response?.data?.detail || '登录失败');
    } finally {
      setLoginLoading(false);
    }
  };

  return (
    <div style={{ padding: '24px' }}>
      <Card
        title={
          <Space direction="vertical" size={0}>
            <Title level={4} style={{ margin: 0 }}>EVC账户</Title>
            <Text type="secondary">配置 EasyValueCheck 登录账户，系统会在首次需要或 Cookie 失效时自动登录。</Text>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%', marginBottom: 24 }}>
          <Space>
            <Text>密码状态：</Text>
            {accountInfo?.password_configured ? <Badge status="success" text="已配置" /> : <Badge status="default" text="未配置" />}
          </Space>
          <Space>
            <Text>Cookie状态：</Text>
            {accountInfo?.cookie_configured ? <Badge status="success" text="已缓存" /> : <Badge status="default" text="未缓存" />}
          </Space>
          <Text type="secondary">
            Cookie到期时间：{accountInfo?.cookie_expires_at ? dayjs(accountInfo.cookie_expires_at).format('YYYY-MM-DD HH:mm:ss') : '暂无'}
          </Text>
          <Text type="secondary">
            最后更新时间：{accountInfo?.updated_at ? dayjs(accountInfo.updated_at).format('YYYY-MM-DD HH:mm:ss') : '暂无'}
          </Text>
        </Space>

        <Form
          form={form}
          layout="vertical"
          onFinish={handleSave}
        >
          <Form.Item
            name="username"
            label="用户名 / 邮箱"
            rules={[{ required: true, message: '请输入 EVC 用户名或邮箱' }]}
          >
            <Input prefix={<MailOutlined />} placeholder="请输入 EVC 用户名或邮箱" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            extra="首次保存必须填写；后续只改用户名时可留空，系统会保留当前密码。"
          >
            <Input.Password prefix={<KeyOutlined />} placeholder="请输入 EVC 密码" />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={loading}>
                保存账户
              </Button>
              <Button icon={<LoginOutlined />} onClick={handleLogin} loading={loginLoading}>
                立即登录测试
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
};

export default EVCAccountManager;
