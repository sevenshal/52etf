import React from 'react';
import { Layout, Tabs } from 'antd';
import {
  DollarOutlined,
  ExperimentOutlined,
  FireOutlined,
  HomeOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import { useAccount } from '../contexts/AccountContext';
import './Layout.css';

const { Content } = Layout;

const TAB_KEYS = ['/', '/fear', '/evc', '/factor-lab', '/live', '/profile'];

const PROFILE_ROUTES = [
  '/automated-trading',
  '/fear/stocks',
  '/portfolio-copy-trading',
  '/options',
  '/ib-account-manager',
  '/longport-account-manager',
  '/evc-account-manager',
  '/lev-etf-backtest',
  '/all-weather-backtest',
  '/fear/backtest',
  '/soxl-fear-backtest',
  '/a-stock-fear-etf-backtest',
  '/monthly-analysis',
  '/scheduled-tasks',
  '/email-settings',
  '/web-account-manager',
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

  if (
    isRouteOrChild(pathname, '/live') ||
    isRouteOrChild(pathname, '/executor-status') ||
    isRouteOrChild(pathname, '/soxl-fear-strategy') ||
    isRouteOrChild(pathname, '/external-trading-accounts') ||
    isRouteOrChild(pathname, '/factor-lab/live')
  ) {
    return '/live';
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
  const { accountId, isAdmin } = useAccount();

  const renderTabLabel = (icon, text) => (
    <span className="app-shell__tab-label">
      {icon}
      <span>{text}</span>
    </span>
  );

  const items = [
    {
      key: '/',
      label: renderTabLabel(<HomeOutlined />, 'ETF'),
      disabled: !accountId
    },
    {
      key: '/fear',
      label: renderTabLabel(<FireOutlined />, '贪恐'),
      disabled: !accountId
    },
    {
      key: '/evc',
      label: renderTabLabel(<DollarOutlined />, '估值'),
      disabled: !accountId
    },
    ...(accountId && isAdmin ? [
      {
        key: '/factor-lab',
        label: renderTabLabel(<ExperimentOutlined />, '研究'),
        disabled: false
      },
      {
        key: '/live',
        label: renderTabLabel(<ThunderboltOutlined />, '实盘'),
        disabled: false
      }
    ] : []),
    {
      key: '/profile',
      label: renderTabLabel(<UserOutlined />, '我的')
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
    <Layout className="app-shell">
      <Content className="app-shell__content">
        <div className="app-shell__scroll">
          <Outlet />
        </div>
      </Content>
      <Tabs
        className="app-shell__tabs"
        items={items}
        activeKey={activeKey}
        onChange={handleTabChange}
        onTabClick={handleTabClick}
        tabBarGutter={0}
      />
    </Layout>
  );
};

export default AppLayout;
