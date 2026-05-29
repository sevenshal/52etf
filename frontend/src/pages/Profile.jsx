import React, { useState } from 'react';
import { Input, Button, Form, message } from 'antd';
import {
  HistoryOutlined,
  KeyOutlined,
  LogoutOutlined,
  MonitorOutlined,
  RightOutlined,
  RocketOutlined,
  SettingOutlined,
  WalletOutlined,
} from '@ant-design/icons';
import { useAccount } from '../contexts/AccountContext';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import { PageSection, PageShell } from '../components/PageScaffold';
import './Profile.css';

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
      icon: <RocketOutlined />,
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
        {
          title: '因子线上交易',
          onClick: () => navigate('/factor-lab/live'),
          arrow: true
        },
        {
          title: '订单执行器状态',
          onClick: () => navigate('/executor-status'),
          arrow: true
        },
      ]
    },
    {
      title: '账户管理',
      icon: <WalletOutlined />,
      items: [
        {
          title: '持仓',
          onClick: () => navigate('/options'),
          arrow: true
        },
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
          title: '外部交易账号',
          onClick: () => navigate('/external-trading-accounts'),
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
      icon: <HistoryOutlined />,
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
          title: 'ETF情绪量能回测',
          onClick: () => navigate('/soxl-fear-backtest'),
          arrow: true
        },
      ]
    },
    {
      title: '分析与监控',
      icon: <MonitorOutlined />,
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
      icon: <SettingOutlined />,
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

  const renderMenu = (items) => (
    <div className="profile-menu">
      {items.map((item, index) => (
        <button
          type="button"
          key={index}
          onClick={item.onClick}
          className="profile-menu__item"
        >
          <span>{item.title}</span>
          <RightOutlined />
        </button>
      ))}
    </div>
  );

  return (
    <PageShell
      className="profile-page"
      title="我的"
    >
      <div className="profile-sections">
        {sections.map((section, index) => (
          <PageSection
            key={index}
            title={
              <span className="profile-section-title">
                {section.icon}
                <span>{section.title}</span>
              </span>
            }
          >
            {renderMenu(section.items)}
          </PageSection>
        ))}
      </div>

      <PageSection
        title={
          <span className="profile-section-title">
            <KeyOutlined />
            <span>账户设置</span>
          </span>
        }
      >
        {accountId ? (
          <div className="profile-account">
            <div>
              <span className="profile-account__label">账户ID</span>
              <strong>{accountId}</strong>
            </div>
            <Button danger icon={<LogoutOutlined />} onClick={handleLogout}>
              退出账户
            </Button>
          </div>
        ) : (
          <Form form={form} onFinish={handleSubmit} layout="vertical" className="profile-account-form">
            <Form.Item
              name="accountId"
              label="账户ID"
              rules={[{ required: true, message: '请输入账户ID' }]}
            >
              <Input placeholder="请输入账户ID" />
            </Form.Item>
            <Form.Item>
              <Button type="primary" htmlType="submit" loading={loading} block>
                保存
              </Button>
            </Form.Item>
          </Form>
        )}
      </PageSection>
    </PageShell>
  );
};

export default Profile;
