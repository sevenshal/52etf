import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Radio, Row, Space, Statistic, Tabs, Tag } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../../utils/request';
import { TIME_RANGES, getFearGreedColor, getFearGreedStatus } from '../utils';

const ETF_OPTIONS = [
  { symbol: 'SOXX.US', ticker: 'SOXX', label: '半导体' },
  { symbol: 'SPY.US', ticker: 'SPY', label: '标普500' },
  { symbol: 'QQQ.US', ticker: 'QQQ', label: '纳指100' },
  { symbol: 'DIA.US', ticker: 'DIA', label: '道琼斯' },
  { symbol: 'INNO100.CN', ticker: 'A创100', label: '创新100', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000510.SH', ticker: '中证A500', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000905.SH', ticker: '中证500', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000985.SH', ticker: '中证全指', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000699.SH', ticker: '科创200', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399006.SZ', ticker: '创业板指', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399998.SZ', ticker: '中证煤炭', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000015.SH', ticker: '上证红利', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
];

const SOXXFearGreed = () => {
  const [activeSymbol, setActiveSymbol] = useState('SOXX.US');
  const [data, setData] = useState([]);
  const [latest, setLatest] = useState(null);
  const [realtime, setRealtime] = useState(null);
  const [latestHoldings, setLatestHoldings] = useState([]);
  const [loading, setLoading] = useState(false);
  const [realtimeLoading, setRealtimeLoading] = useState(false);
  const [error, setError] = useState(null);
  const [realtimeError, setRealtimeError] = useState(null);
  const [timeRange, setTimeRange] = useState(3);

  const activeETF = useMemo(
    () => ETF_OPTIONS.find(item => item.symbol === activeSymbol) || ETF_OPTIONS[0],
    [activeSymbol]
  );

  useEffect(() => {
    const fetchHistory = async () => {
      setLoading(true);
      setError(null);
      setData([]);
      setLatest(null);
      setLatestHoldings([]);
      try {
        const response = await request.get('/api/cnn/etf-fear-greed-clone/history', {
          params: {
            symbol: activeSymbol,
            include_components: false,
            include_latest_holdings: true,
          },
        });
        setData(response.data?.data || []);
        setLatest(response.data?.latest || null);
        setLatestHoldings(response.data?.latest_holdings || []);
      } catch (err) {
        setError(err?.response?.data?.detail || err.message || `获取 ${activeETF.ticker} 贪恐历史失败`);
      } finally {
        setLoading(false);
      }
    };

    const fetchRealtime = async () => {
      if (activeETF.realtime === false) {
        setRealtime(null);
        setRealtimeError(null);
        setRealtimeLoading(false);
        return;
      }
      setRealtimeLoading(true);
      setRealtimeError(null);
      setRealtime(null);
      try {
        const response = await request.get('/api/cnn/etf-fear-greed-clone/realtime', {
          params: {
            symbol: activeSymbol,
            include_holdings_quotes: false,
          },
        });
        setRealtime(response.data || null);
      } catch (err) {
        setRealtime(null);
        setRealtimeError(
          err?.response?.data?.detail
            || err?.message
            || `获取 ${activeETF.ticker} 实时贪恐失败`
        );
      } finally {
        setRealtimeLoading(false);
      }
    };

    fetchHistory();
    fetchRealtime();
  }, [activeSymbol, activeETF]);

  const filteredData = useMemo(() => {
    if (timeRange === -1) return data;

    const cutoffDate = new Date();
    cutoffDate.setFullYear(cutoffDate.getFullYear() - timeRange);
    return data.filter(item => new Date(item.date) >= cutoffDate);
  }, [data, timeRange]);

  const chartOption = useMemo(() => {
    const dates = filteredData.map(item => item.date);
    const scores = filteredData.map(item => item.score ?? null);
    const prices = filteredData.map(item => item.etf_price?.close ?? null);
    const priceValues = prices.filter(value => value !== null && value !== undefined);
    const priceMin = priceValues.length ? Math.floor(Math.min(...priceValues) * 0.92) : undefined;
    const priceMax = priceValues.length ? Math.ceil(Math.max(...priceValues) * 1.08) : undefined;

    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
      },
      legend: {
        data: [`${activeETF.ticker}贪恐`, `${activeETF.ticker}价格`],
        top: 0,
      },
      grid: {
        left: 48,
        right: 56,
        top: 48,
        bottom: 56,
      },
      dataZoom: [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', start: 0, end: 100 },
      ],
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: dates,
      },
      yAxis: [
        {
          type: 'value',
          name: '贪恐',
          min: 0,
          max: 100,
          axisLine: { show: true, lineStyle: { color: '#13c2c2' } },
          splitLine: { lineStyle: { color: '#f0f0f0' } },
        },
        {
          type: 'value',
          name: '价格',
          min: priceMin,
          max: priceMax,
          axisLine: { show: true, lineStyle: { color: '#fa8c16' } },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: `${activeETF.ticker}贪恐`,
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: scores,
          lineStyle: { width: 2, color: '#13c2c2' },
          itemStyle: { color: '#13c2c2' },
        },
        {
          name: `${activeETF.ticker}价格`,
          type: 'line',
          yAxisIndex: 1,
          smooth: true,
          showSymbol: false,
          data: prices,
          lineStyle: { width: 2, color: '#fa8c16' },
          itemStyle: { color: '#fa8c16' },
        },
      ],
    };
  }, [filteredData, activeETF.ticker]);

  const realtimeMeta = realtime?.fear_and_greed_clone;
  const realtimeScore = realtimeMeta?.score;
  const realtimePrice = realtime?.etf_price?.close ?? realtime?.etf_price?.quote?.price;
  const realtimeTimestamp = realtimeMeta?.timestamp
    ? new Date(realtimeMeta.timestamp).toLocaleString()
    : null;
  const latestScore = latest?.score;
  const latestPrice = latest?.etf_price?.close;
  const displayPrice = realtimePrice ?? latestPrice;
  const topHoldings = latestHoldings.slice(0, 6);
  const realtimeEnabled = activeETF.realtime !== false;
  const pricePrecision = activeETF.pricePrecision ?? 2;

  return (
    <Card title="自算贪恐" style={{ marginBottom: 16 }} loading={loading}>
      <Tabs activeKey={activeSymbol} onChange={setActiveSymbol} style={{ marginBottom: 16 }}>
        {ETF_OPTIONS.map(item => (
          <Tabs.TabPane tab={`${item.ticker} ${item.label}`} key={item.symbol} />
        ))}
      </Tabs>

      {error && (
        <Alert
          type="warning"
          showIcon
          message={`${activeETF.ticker}贪恐数据暂不可用`}
          description={error}
          style={{ marginBottom: 16 }}
        />
      )}

      {!error && realtimeError && (
        <Alert
          type="info"
          showIcon
          message={`${activeETF.ticker} 实时贪恐暂不可用，当前显示最新入库日线值`}
          description={realtimeError}
          style={{ marginBottom: 16 }}
        />
      )}

      {!error && !loading && !latest && (
        <Alert
          type="info"
          showIcon
          message={`${activeETF.ticker}贪恐数据暂无入库记录`}
          description="请先执行对应的贪恐回跑入库任务。"
        />
      )}

      {!error && latest && (
        <>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={12} md={6} xl={4}>
              <Statistic
                title={
                  <Space size={6}>
                    <span>{realtimeEnabled ? '实时贪恐' : '最新贪恐'}</span>
                    {realtimeEnabled && realtimeScore !== undefined && <Tag color="processing">实时值</Tag>}
                    {realtimeEnabled && realtimeScore === undefined && realtimeLoading && <Tag color="processing">加载中</Tag>}
                  </Space>
                }
                value={realtimeScore ?? latestScore}
                precision={1}
                valueStyle={{ color: getFearGreedColor(realtimeScore ?? latestScore) }}
                suffix="/100"
              />
              <Tag color={getFearGreedColor(realtimeScore ?? latestScore)} style={{ marginTop: 8 }}>
                {getFearGreedStatus(realtimeScore ?? latestScore)}
              </Tag>
            </Col>
            <Col xs={12} md={6} xl={4}>
              <Statistic
                title="最新入库日线"
                value={latestScore}
                precision={1}
                valueStyle={{ color: getFearGreedColor(latestScore) }}
                suffix="/100"
              />
            </Col>
            <Col xs={12} md={6} xl={4}>
              <Statistic
                title={`${activeETF.ticker}${activeETF.priceLabel || '价格'}`}
                value={displayPrice}
                precision={pricePrecision}
                prefix={activeETF.symbol.endsWith('.US') ? '$' : undefined}
              />
            </Col>
            <Col xs={12} md={6} xl={4}>
              <Statistic title={realtimeEnabled ? '报价时间' : '实时状态'} value={realtimeEnabled ? (realtimeTimestamp || '-') : '日线'} />
            </Col>
            <Col xs={12} md={6} xl={4}>
              <Statistic title="数据日期" value={latest.date} />
            </Col>
            <Col xs={12} md={6} xl={4}>
              <Statistic title="持仓快照" value={latest.holdings_as_of || '-'} />
            </Col>
          </Row>

          {topHoldings.length > 0 && (
            <Space wrap size={[8, 8]} style={{ marginBottom: 16 }}>
              {topHoldings.map(item => (
                <Tag key={item.symbol}>
                  {item.symbol} {(item.weight * 100).toFixed(2)}%
                </Tag>
              ))}
            </Space>
          )}

          <div style={{ marginBottom: 12, textAlign: 'right' }}>
            <Radio.Group
              value={timeRange}
              onChange={event => setTimeRange(event.target.value)}
              optionType="button"
              buttonStyle="solid"
            >
              {TIME_RANGES.filter(range => [1, 3, 5, -1].includes(range.value)).map(range => (
                <Radio.Button key={range.value} value={range.value}>
                  {range.label}
                </Radio.Button>
              ))}
            </Radio.Group>
          </div>

          <ReactECharts option={chartOption} style={{ height: 420 }} />
        </>
      )}
    </Card>
  );
};

export default SOXXFearGreed;
