import React, { useState, useEffect } from 'react';
import { Card, Input, Button, Form, message } from 'antd';
import { useAccount } from '../contexts/AccountContext';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';

const Profile = () => {
  const { accountId, login, logout } = useAccount();
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (values) => {
    setLoading(true);
    try {
      // 验证账户ID
      const response = await request.get('/api/profile/validate-account', {
        params: { account_id: values.accountId }
      });
      
      if (!response.data.valid) {
        throw new Error(response.data.message || '账户ID无效');
      }
      
      // 验证成功后保存
      login(values.accountId);
      message.success('账户设置成功');
      navigate('/');
    } catch (error) {
      const errorMessage = error.response?.data?.message || error.message || '账户ID无效';
      message.error(errorMessage);
      form.setFields([
        {
          name: 'accountId',
          errors: [errorMessage]
        }
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = () => {
    try {
      logout();
      message.success('已退出账户');
    } catch (error) {
      message.error('退出失败');
    }
  };

  return (
    <div style={{ padding: '24px' }}>
      <Card title="账户设置">
        {accountId ? (
          <div>
            <p>当前账户ID: {accountId}</p>
            <Button type="primary" danger onClick={handleLogout} style={{ marginBottom: '24px' }}>
              退出账户
            </Button>
          </div>
        ) : (
          <Form form={form} onFinish={handleSubmit}>
            <Form.Item
              name="accountId"
              label="账户ID"
              rules={[{ required: true, message: '请输入账户ID' }]}
            >
              <Input placeholder="请输入账户ID" />
            </Form.Item>
            <Form.Item>
              <Button type="primary" htmlType="submit" loading={loading}>
                保存
              </Button>
            </Form.Item>
          </Form>
        )}
      </Card>
    </div>
  );
};

export default Profile;
