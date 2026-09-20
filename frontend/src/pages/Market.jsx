import React, { useCallback, useEffect, useState } from 'react';
import { Tabs, Tag, Tooltip, Typography } from 'antd';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import AStockFundFlow from './AStockFundFlow';
import MarketOverview from './MarketOverview';
import MarketAlerts from './MarketAlerts';
import MarketEarningsGap from './MarketEarningsGap';
import './Market.css';

const { Text } = Typography;

const MARKET_TAB_ITEMS = [
  { key: 'overview', label: '大盘观测', path: '/market' },
  { key: 'alerts', label: '提示看板', path: '/market/alerts' },
  { key: 'fund-flow', label: '资金流向', path: '/market/fund-flow' },
  { key: 'earnings-gap', label: '净利润断层', path: '/market/earnings-gap' },
];

const STATS_REFRESH_MS = 60 * 1000;

const pctClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');
const fmtSignedPct = value => (
  value === null || value === undefined ? '--' : `${value > 0 ? '+' : ''}${Number(value).toFixed(2)}%`
);

/** 标题栏行情条：涨跌家数 / 涨停跌停 / 四大指数涨跌幅，盘中每分钟刷新 */
const MarketHeaderStats = () => {
  const [breadth, setBreadth] = useState(null);
  const [indexes, setIndexes] = useState([]);

  const load = useCallback(async () => {
    const [breadthResult, indexResult] = await Promise.allSettled([
      request.get('/api/market/breadth-distribution'),
      request.get('/api/market/index-overview'),
    ]);
    // 任一接口失败都保留上一轮数据，标题栏不闪空
    if (breadthResult.status === 'fulfilled') setBreadth(breadthResult.value.data);
    if (indexResult.status === 'fulfilled') setIndexes(indexResult.value.data?.items || []);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, STATS_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  if (!breadth && !indexes.length) return null;

  return (
    <div className="market-header-stats">
      {breadth && (
        <Tooltip title={`${breadth.date} · 共 ${breadth.total} 只 · ${breadth.mode === 'realtime' ? '实时行情' : '收盘快照'}`}>
          <span className="market-header-stats__group">
            <span className="is-up">上涨 {breadth.up_count}</span>
            <span className="market-header-stats__sep">/</span>
            <span className="is-down">下跌 {breadth.down_count}</span>
            <span className="market-header-stats__sep">/</span>
            <span>平 {breadth.flat_count}</span>
          </span>
        </Tooltip>
      )}
      {breadth && (
        <span className="market-header-stats__group">
          <span className="is-up">涨停 {breadth.limit_up}</span>
          <span className="market-header-stats__sep">/</span>
          <span className="is-down">跌停 {breadth.limit_down}</span>
        </span>
      )}
      {indexes.map(item => (
        <span className="market-header-stats__group" key={item.key}>
          {item.name}
          <span className={pctClass(item.pct)}>{fmtSignedPct(item.pct)}</span>
        </span>
      ))}
    </div>
  );
};

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
        <MarketHeaderStats />
      </div>
      <div className="market-tab-strip">
        <Tabs activeKey={activeTab} onChange={handleTabChange} items={MARKET_TAB_ITEMS} />
      </div>
      {activeTab === 'overview' && <MarketOverview />}
      {activeTab === 'alerts' && <MarketAlerts />}
      {activeTab === 'fund-flow' && <AStockFundFlow embedded />}
      {activeTab === 'earnings-gap' && <MarketEarningsGap />}
    </div>
  );
};

export default Market;
