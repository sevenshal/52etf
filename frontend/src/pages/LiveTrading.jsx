import React, { useMemo } from 'react';
import { Tabs } from 'antd';
import {
  BarChartOutlined,
  FireOutlined,
  ThunderboltOutlined,
  WalletOutlined,
} from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import ExecutorStatusPage from './ExecutorStatusPage';
import ExternalTradingAccountManager from './ExternalTradingAccountManager';
import FactorLab from './FactorLab';
import SoxlFearStrategy from './SoxlFearStrategy';
import './LiveTrading.css';

const LIVE_TAB_KEYS = ['executor', 'accounts', 'factor', 'sentiment'];

const LiveTrading = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = LIVE_TAB_KEYS.includes(searchParams.get('tab'))
    ? searchParams.get('tab')
    : 'executor';

  const items = useMemo(() => [
    {
      key: 'executor',
      label: (
        <span className="live-trading-tab-label">
          <ThunderboltOutlined />
          <span>执行器</span>
        </span>
      ),
      children: <ExecutorStatusPage embedded />,
    },
    {
      key: 'accounts',
      label: (
        <span className="live-trading-tab-label">
          <WalletOutlined />
          <span>交易账户</span>
        </span>
      ),
      children: <ExternalTradingAccountManager embedded />,
    },
    {
      key: 'factor',
      label: (
        <span className="live-trading-tab-label">
          <BarChartOutlined />
          <span>多因子策略</span>
        </span>
      ),
      children: <FactorLab initialTab="live" liveOnly />,
    },
    {
      key: 'sentiment',
      label: (
        <span className="live-trading-tab-label">
          <FireOutlined />
          <span>情绪量能策略</span>
        </span>
      ),
      children: <SoxlFearStrategy embedded />,
    },
  ], []);

  const handleTabChange = key => {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('tab', key);
    setSearchParams(nextParams, { replace: true });
  };

  return (
    <div className="live-trading-page">
      <Tabs
        className="live-trading-tabs"
        activeKey={activeTab}
        items={items}
        destroyInactiveTabPane
        onChange={handleTabChange}
      />
    </div>
  );
};

export default LiveTrading;
