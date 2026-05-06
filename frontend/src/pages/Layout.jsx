import React from 'react';
import { Layout, Tabs } from 'antd';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import { useAccount } from '../contexts/AccountContext';

const { Content } = Layout;

const AppLayout = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { accountId } = useAccount();

  const items = [
    {
      key: '/',
      label: 'ETF',
      disabled: !accountId
    },
    {
      key: '/fear',
      label: '贪恐',
      disabled: !accountId
    },
    {
      key: '/evc',
      label: '估值',
      disabled: !accountId
    },
    {
      key: '/options',
      label: '持仓',
      disabled: !accountId
    },
    {
      key: '/a-stock-innovation100',
      label: 'A创100',
      disabled: !accountId
    },
    {
      key: '/a-stock-innovation-momentum-live',
      label: 'A创盘',
      disabled: !accountId
    },
    {
      key: '/profile',
      label: '我的'
    }
  ];

  return (
    <Layout style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <Content style={{ 
        overflow: 'auto',
        flexDirection: 'column'
      }}>
        <div style={{ flex: 1, overflow: 'auto' }}>
          <Outlet />
        </div>
      </Content>
      <Tabs
        items={items}
        activeKey={location.pathname}
        onChange={navigate}
        centered
        size="large"
        style={{
          backgroundColor: '#fff',
          borderTop: '1px solid #f0f0f0'
        }}
      />
    </Layout>
  );
};

export default AppLayout;
