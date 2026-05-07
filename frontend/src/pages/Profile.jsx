import React, { useState } from 'react';
import { Card, Input, Button, Form, message, List, Space } from 'antd';
import { RightOutlined } from '@ant-design/icons';
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

  const sections = [
    {
      title: '交易与执行',
      items: [
        {
          title: '杠杆ETF策略自动化交易',
          onClick: () => navigate('/automated-trading'),
        },
        {
          title: '恐贪策略自动化交易',
          onClick: () => navigate('/fear/stocks'),
          arrow: true
        },
        {
          title: '自动化跟单交易',
          onClick: () => navigate('/portfolio-copy-trading'),
          arrow: true
        },
        {
          title: 'SOXL情绪量能自动交易',
          onClick: () => navigate('/soxl-fear-strategy'),
          arrow: true
        },
      ]
    },
    {
      title: '模拟盘',
      items: [
        {
          title: 'A股ETF风险调整动量虚拟盘',
          onClick: () => navigate('/w20-momentum-live'),
          arrow: true
        },
        {
          title: '美股风险调整混合动量虚拟盘',
          onClick: () => navigate('/us-stock-signal-live'),
          arrow: true
        },
        {
          title: 'A股创新100动量虚拟盘',
          onClick: () => navigate('/a-stock-innovation-momentum-live'),
          arrow: true
        },
      ]
    },
    {
      title: '账户管理',
      items: [
        {
          title: 'IBKR 账户管理',
          onClick: () => navigate('/ib-account-manager'),
          arrow: true
        },
        {
          title: '长桥账户管理',
          onClick: () => navigate('/longport-account-manager'),
          arrow: true
        },
        {
          title: 'EVC账户',
          onClick: () => navigate('/evc-account-manager'),
          arrow: true
        },
      ]
    },
    {
      title: '策略与回测',
      items: [
        {
          title: '杠杆ETF均线穿越策略回测',
          onClick: () => navigate('/lev-etf-backtest'),
          arrow: true
        },
        {
          title: '全天候策略回测',
          onClick: () => navigate('/all-weather-backtest'),
          arrow: true
        },
        {
          title: '恐贪策略回测',
          onClick: () => navigate('/fear/backtest'),
          arrow: true
        },
        {
          title: 'SOXL情绪量能回测',
          onClick: () => navigate('/soxl-fear-backtest'),
          arrow: true
        },
        {
          title: 'W20风险调整动量回测',
          onClick: () => navigate('/w20-momentum-backtest'),
          arrow: true
        }
      ]
    },
    {
      title: '分析与监控',
      items: [
        {
          title: '历史每月分析',
          onClick: () => navigate('/monthly-analysis'),
          arrow: true
        },
      ]
    },
    {
      title: '系统管理',
      items: [
        {
          title: '定时任务',
          onClick: () => navigate('/scheduled-tasks'),
          arrow: true
        },
        {
          title: '系统日志',
          onClick: () => navigate('/system-log'),
          arrow: true
        }
      ]
    }
  ];

  const renderList = (items) => (
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
  );

  return (
    <div>
      {sections.map((section, index) => (
        <Card key={index} title={section.title} style={{ marginBottom: '6px' }}>
          {renderList(section.items)}
        </Card>
      ))}

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
