import React from 'react';
import { Layout, Tabs } from 'antd';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import { useAccount } from '../contexts/AccountContext';

const { Content } = Layout;

const TAB_KEYS = ['/', '/fear', '/evc', '/a-stock-innovation100', '/factor-lab', '/profile'];

const PROFILE_ROUTES = [
  '/automated-trading',
  '/fear/stocks',
  '/portfolio-copy-trading',
  '/soxl-fear-strategy',
  '/options',
  '/ib-account-manager',
  '/longport-account-manager',
  '/external-trading-accounts',
  '/evc-account-manager',
  '/lev-etf-backtest',
  '/all-weather-backtest',
  '/fear/backtest',
  '/soxl-fear-backtest',
  '/w20-momentum-backtest',
  '/monthly-analysis',
  '/scheduled-tasks',
  '/system-log',
];

const isRouteOrChild = (pathname, route) => pathname === route || pathname.startsWith(`${route}/`);

const getActiveTabKey = (pathname, state) => {
  const stateTabKey = state?.mainTabKey;
  if (TAB_KEYS.includes(stateTabKey)) {
    return stateTabKey;
  }

  if (pathname === '/' || pathname.startsWith('/etf/')) {
    return '/';
  }

  if (isRouteOrChild(pathname, '/evc') || pathname.startsWith('/stock/')) {
    return '/evc';
  }

  if (
    isRouteOrChild(pathname, '/fear') &&
    !PROFILE_ROUTES.some(route => isRouteOrChild(pathname, route))
  ) {
    return '/fear';
  }

  if (isRouteOrChild(pathname, '/a-stock-innovation100')) {
    return '/a-stock-innovation100';
  }

  if (isRouteOrChild(pathname, '/db')) {
    return '/factor-lab';
  }

  if (isRouteOrChild(pathname, '/factor-lab')) {
    return '/factor-lab';
  }

  return '/profile';
};

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
      key: '/a-stock-innovation100',
      label: 'A创100',
      disabled: !accountId
    },
    {
      key: '/factor-lab',
      label: '研究',
      disabled: !accountId
    },
    {
      key: '/profile',
      label: '我的'
    }
  ];
  const activeKey = getActiveTabKey(location.pathname, location.state);

  const handleTabChange = (key) => {
    const tab = items.find(item => item.key === key);
    if (!tab || tab.disabled) {
      return;
    }
    navigate(key);
  };

  const handleTabClick = (key) => {
    if (key === activeKey && location.pathname !== key) {
      handleTabChange(key);
    }
  };

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
        activeKey={activeKey}
        onChange={handleTabChange}
        onTabClick={handleTabClick}
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
