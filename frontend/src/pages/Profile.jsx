import React, { useState } from 'react';
import { Card, Input, Button, Form, message, List, Space } from 'antd';
import { RightOutlined } from '@ant-design/icons';
import { useAccount } from '../contexts/AccountContext';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import AutoTradingPanel from './fear/components/AutoTradingPanel';
import { useAutoTrading } from './fear/hooks/useAutoTrading';

const Profile = () => {
  const { accountId, login, logout } = useAccount();
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const { autoTrading, handleAutoTradingChange } = useAutoTrading();

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

  const items = [
    {
      title: '历史每月分析',
      onClick: () => navigate('/monthly-analysis'),
      arrow: true
    },
    {
      title: '个股买卖信号',
      onClick: () => navigate('/market-signal-history'),
      arrow: true
    },
    {
      title: '系统日志',
      onClick: () => navigate('/system-log'),
      arrow: true
    }
  ];

  return (
    <div>
      <Card title="我的功能" style={{ marginBottom: '6px' }}>
        <List
          style={{
            backgroundColor: '#fff'
          }}
        >
          {items.map((item, index) => (
            <List.Item
              key={index}
              onClick={item.onClick}
              style={{
                cursor: item.onClick ? 'pointer' : 'default',
                borderBottom: '1px solid #f0f0f0'
              }}
            >
              <div style={{ 
                display: 'flex', 
                justifyContent: 'space-between', 
                alignItems: 'center',
                width: '100%'
              }}>
                <span>{item.title}</span>
                <Space>
                  {item.arrow && <RightOutlined style={{ color: '#bfbfbf' }} />}
                </Space>
              </div>
            </List.Item>
          ))}
        </List>
      </Card>
      <Card title='自动化交易' style={{ marginBottom: '6px' }}>
        <AutoTradingPanel 
          autoTrading={autoTrading}
          onAutoTradingChange={handleAutoTradingChange}
        />
      </Card>
      <Card title="账户设置">
        {accountId ? (
          <div>
            <p>账户ID: {accountId}</p>
            <Button type="primary" danger onClick={handleLogout} style={{ marginBottom: '6px' }}>
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
