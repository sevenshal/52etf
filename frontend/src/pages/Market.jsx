import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Row, Select, Space, Spin, Tabs, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import AStockFundFlow from './AStockFundFlow';
import './Market.css';

const { Text } = Typography;

const MARKET_TAB_ITEMS = [
  { key: 'volume', label: '量能对比', path: '/market' },
  { key: 'fund-flow', label: '资金流向', path: '/market/fund-flow' },
];

const AUTO_REFRESH_MS = 60 * 1000;
const UP_COLOR = '#e5484d';
const DOWN_COLOR = '#2f9e63';
const SH_COLOR = '#2f6fdb';
const SZ_COLOR = '#8b5cf6';

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const fmtYi = value => (value === null || value === undefined ? '-' : `${Number(value).toFixed(1)}亿`);
const fmtPct = (value, digits = 2) => (value === null || value === undefined ? '-' : `${Number(value).toFixed(digits)}%`);
const signedClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');

const indexPctSeries = points => ([
  {
    name: '上证%',
    type: 'line',
    yAxisIndex: 1,
    showSymbol: false,
    lineStyle: { width: 1.5 },
    itemStyle: { color: SH_COLOR },
    data: points.map(point => point.sh_pct),
  },
  {
    name: '深综%',
    type: 'line',
    yAxisIndex: 1,
    showSymbol: false,
    lineStyle: { width: 1.5 },
    itemStyle: { color: SZ_COLOR },
    data: points.map(point => point.sz_pct),
  },
]);

const signedBarData = values => values.map(value => (
  value === null || value === undefined
    ? null
    : { value, itemStyle: { color: value >= 0 ? UP_COLOR : DOWN_COLOR, opacity: 0.75 } }
));

const baseGrid = { left: 52, right: 52, top: 36, bottom: 28 };

const buildCumOption = (data) => {
  const points = data.points;
  return {
    animation: false,
    grid: { ...baseGrid, right: 16 },
    legend: { top: 0 },
    tooltip: { trigger: 'axis', valueFormatter: value => fmtYi(value) },
    xAxis: { type: 'category', data: points.map(point => point.time), boundaryGap: false },
    yAxis: { type: 'value', name: '亿元', nameGap: 8, splitLine: { lineStyle: { color: '#eef1f6' } } },
    series: [
      {
        name: `${data.target_date} 累计`,
        type: 'line',
        showSymbol: false,
        lineStyle: { width: 2 },
        itemStyle: { color: UP_COLOR },
        data: points.map(point => point.target_cum),
      },
      {
        name: `${data.compare_date} 累计`,
        type: 'line',
        showSymbol: false,
        lineStyle: { width: 1.5, type: 'dashed' },
        itemStyle: { color: '#9aa3af' },
        data: points.map(point => point.compare_cum),
      },
    ],
  };
};

const buildDiffOption = (data) => {
  const points = data.points;
  return {
    animation: false,
    grid: baseGrid,
    legend: { top: 0 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: value => (value === null || value === undefined ? '-' : Number(value).toFixed(2)),
    },
    xAxis: { type: 'category', data: points.map(point => point.time) },
    yAxis: [
      { type: 'value', name: '亿元', nameGap: 8, splitLine: { lineStyle: { color: '#eef1f6' } } },
      { type: 'value', name: '%', nameGap: 8, splitLine: { show: false }, axisLabel: { formatter: value => value.toFixed(2) } },
    ],
    series: [
      {
        name: '差额',
        type: 'bar',
        barCategoryGap: '10%',
        itemStyle: { color: UP_COLOR },
        data: signedBarData(points.map(point => point.diff_cum)),
      },
      ...indexPctSeries(points),
    ],
  };
};

const buildDeviationOption = (data) => {
  const points = data.points;
  return {
    animation: false,
    grid: baseGrid,
    legend: { top: 0 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: value => fmtPct(value),
    },
    xAxis: { type: 'category', data: points.map(point => point.time) },
    yAxis: [
      { type: 'value', name: '量能偏离%', nameGap: 8, splitLine: { lineStyle: { color: '#eef1f6' } } },
      { type: 'value', name: '指数%', nameGap: 8, splitLine: { show: false }, axisLabel: { formatter: value => value.toFixed(2) } },
    ],
    series: [
      {
        name: '量能偏离',
        type: 'bar',
        barCategoryGap: '10%',
        itemStyle: { color: UP_COLOR },
        data: signedBarData(points.map(point => point.deviation_pct)),
      },
      ...indexPctSeries(points),
    ],
  };
};

const MarketVolumeCompare = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [targetDate, setTargetDate] = useState(null);
  const [compareDate, setCompareDate] = useState(null);
  const requestSeq = useRef(0);

  const load = useCallback(async ({ silent = false } = {}) => {
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    if (!silent) setLoading(true);
    try {
      const params = {};
      if (targetDate) params.target_date = targetDate;
      if (compareDate) params.compare_date = compareDate;
      const response = await request.get('/api/market/intraday-volume', { params });
      if (seq !== requestSeq.current) return;
      setData(response.data);
      setError('');
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(formatErrorMessage(err, '加载分时成交额失败'));
    } finally {
      if (!silent && seq === requestSeq.current) setLoading(false);
    }
  }, [targetDate, compareDate]);

  useEffect(() => {
    load();
  }, [load]);

  // 看的是最新交易日且仍在盘中时，每分钟静默刷新
  const isLiveView = Boolean(data?.is_intraday && data?.target_date === data?.selectable_dates?.[0]);
  useEffect(() => {
    if (!isLiveView) return undefined;
    const timer = setInterval(() => load({ silent: true }), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [isLiveView, load]);

  const cumOption = useMemo(() => (data ? buildCumOption(data) : null), [data]);
  const diffOption = useMemo(() => (data ? buildDiffOption(data) : null), [data]);
  const deviationOption = useMemo(() => (data ? buildDeviationOption(data) : null), [data]);

  const targetOptions = (data?.selectable_dates || []).map((date, index) => ({
    value: date,
    label: index === 0 ? `最新（${date}）` : date,
  }));
  const compareOptions = (data?.dates || [])
    .filter(date => date !== data?.target_date)
    .slice()
    .reverse()
    .map(date => ({ value: date, label: date }));

  const handleTargetChange = (value) => {
    setTargetDate(value);
    setCompareDate(null); // 切换目标日时对比日回到"前一交易日"
  };

  return (
    <div className="market-volume">
      <div className="market-toolbar">
        <Space wrap>
          <Text type="secondary">目标日</Text>
          <Select
            className="market-date-select"
            value={data?.target_date || targetDate}
            options={targetOptions}
            onChange={handleTargetChange}
            placeholder="最新交易日"
          />
          <Text type="secondary">对比日</Text>
          <Select
            className="market-date-select"
            value={data?.compare_date || compareDate}
            options={compareOptions}
            onChange={setCompareDate}
            placeholder="前一交易日"
          />
          <Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>刷新</Button>
        </Space>
        {data && (
          <Space wrap size={[12, 4]} className="market-summary">
            <Tag color={data.is_intraday ? 'processing' : 'default'}>
              {data.is_intraday ? `盘中 ${data.last_time || ''}` : '已收盘'}
            </Tag>
            <Text>{data.target_date} <Text strong>{fmtYi(data.target_total)}</Text></Text>
            <Text type="secondary">{data.compare_date} 同期 {fmtYi(data.compare_same_time_total)}</Text>
            <Text className={signedClass(data.diff)}>
              {data.diff > 0 ? '放量' : '缩量'} {fmtYi(Math.abs(data.diff))}（{fmtPct(data.diff_pct)}）
            </Text>
            <Text type="secondary">对比日全天 {fmtYi(data.compare_full_total)}</Text>
          </Space>
        )}
      </div>

      {error && <Alert className="market-alert" type="warning" showIcon message={error} />}

      <Spin spinning={loading && !data}>
        {data ? (
          <Row gutter={[12, 12]}>
            <Col xs={24} xl={8}>
              <Card size="small" title="累计成交对比" extra={<Text type="secondary">沪+深 · 亿元</Text>}>
                <ReactECharts option={cumOption} style={{ height: 300 }} notMerge lazyUpdate />
              </Card>
            </Col>
            <Col xs={24} xl={8}>
              <Card size="small" title="累计缩放量对比" extra={<Text type="secondary">差额 = 目标日累计 − 对比日累计</Text>}>
                <ReactECharts option={diffOption} style={{ height: 300 }} notMerge lazyUpdate />
              </Card>
            </Col>
            <Col xs={24} xl={8}>
              <Card size="small" title="分时缩放量对比" extra={<Text type="secondary">柱：每分钟 ÷ 对比日同一时刻 − 1</Text>}>
                <ReactECharts option={deviationOption} style={{ height: 300 }} notMerge lazyUpdate />
              </Card>
            </Col>
          </Row>
        ) : (
          !loading && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Spin>
      {data && (
        <Text type="secondary" className="market-footnote">
          数据源：tushare 指数分钟线（上证指数 + 深证综指成交额），近 5 个交易日；更新于 {data.fetched_at}
          {isLiveView ? '，盘中每分钟自动刷新' : ''}
        </Text>
      )}
    </div>
  );
};

const Market = ({ initialTab = 'volume' }) => {
  const navigate = useNavigate();
  const activeTab = MARKET_TAB_ITEMS.some(item => item.key === initialTab) ? initialTab : 'volume';
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
      {activeTab === 'volume' && <MarketVolumeCompare />}
      {activeTab === 'fund-flow' && <AStockFundFlow embedded />}
    </div>
  );
};

export default Market;
