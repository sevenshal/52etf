import React from 'react';
import { Tabs, Tag, Typography } from 'antd';
import { useNavigate } from 'react-router-dom';
import AStockFundFlow from './AStockFundFlow';
import MarketOverview from './MarketOverview';
import MarketEarningsGap from './MarketEarningsGap';
import './Market.css';

const { Text } = Typography;

const MARKET_TAB_ITEMS = [
  { key: 'overview', label: '大盘观测', path: '/market' },
  { key: 'fund-flow', label: '资金流向', path: '/market/fund-flow' },
  { key: 'earnings-gap', label: '净利润断层', path: '/market/earnings-gap' },
];

const Market = ({ initialTab = 'overview' }) => {
  const navigate = useNavigate();
  const activeTab = MARKET_TAB_ITEMS.some(item => item.key === initialTab) ? initialTab : 'overview';
  const activeLabel = MARKET_TAB_ITEMS.find(item => item.key === activeTab)?.label;

  const handleTabChange = (key) => {
    const target = MARKET_TAB_ITEMS.find(item => item.key === key);
    if (target) navigate(target.path);
  };

  return (
    <div className="market-page">
      <div className="market-header">
        <Text type="secondary">Market</Text>
        <h1>市场</h1>
        <Tag color="blue">{activeLabel}</Tag>
      </div>
      <div className="market-tab-strip">
        <Tabs activeKey={activeTab} onChange={handleTabChange} items={MARKET_TAB_ITEMS} />
      </div>
      {activeTab === 'overview' && <MarketOverview />}
      {activeTab === 'fund-flow' && <AStockFundFlow embedded />}
      {activeTab === 'earnings-gap' && <MarketEarningsGap />}
    </div>
  );
};

export default Market;
