import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Radio, Row, Space, Statistic, Tag } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../../utils/request';
import { TIME_RANGES, getFearGreedColor, getFearGreedStatus } from '../utils';

const SOXXFearGreed = () => {
  const [data, setData] = useState([]);
  const [latest, setLatest] = useState(null);
  const [latestHoldings, setLatestHoldings] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [timeRange, setTimeRange] = useState(3);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await request.get('/api/cnn/etf-fear-greed-clone/history', {
          params: {
            symbol: 'SOXX.US',
            include_components: false,
            include_latest_holdings: true,
          },
        });
        setData(response.data?.data || []);
        setLatest(response.data?.latest || null);
        setLatestHoldings(response.data?.latest_holdings || []);
      } catch (err) {
        setError(err?.response?.data?.detail || err.message || '获取 SOXX 贪恐历史失败');
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

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
        data: ['SOXX贪恐', 'SOXX价格'],
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
          name: 'SOXX贪恐',
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: scores,
          lineStyle: { width: 2, color: '#13c2c2' },
          itemStyle: { color: '#13c2c2' },
        },
        {
          name: 'SOXX价格',
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
  }, [filteredData]);

  const latestScore = latest?.score;
  const latestPrice = latest?.etf_price?.close;
  const topHoldings = latestHoldings.slice(0, 6);

  return (
    <Card title="SOXX贪恐" style={{ marginBottom: 16 }} loading={loading}>
      {error && (
        <Alert
          type="warning"
          showIcon
          message="SOXX贪恐数据暂不可用"
          description={error}
          style={{ marginBottom: 16 }}
        />
      )}

      {!error && latest && (
        <>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={12} md={6}>
              <Statistic
                title="最新贪恐"
                value={latestScore}
                precision={1}
                valueStyle={{ color: getFearGreedColor(latestScore) }}
                suffix="/100"
              />
              <Tag color={getFearGreedColor(latestScore)} style={{ marginTop: 8 }}>
                {getFearGreedStatus(latestScore)}
              </Tag>
            </Col>
            <Col xs={12} md={6}>
              <Statistic title="SOXX价格" value={latestPrice} precision={2} prefix="$" />
            </Col>
            <Col xs={12} md={6}>
              <Statistic title="数据日期" value={latest.date} />
            </Col>
            <Col xs={12} md={6}>
              <Statistic title="持仓快照" value={latest.holdings_as_of || '-'} />
            </Col>
          </Row>

          <Space wrap size={[8, 8]} style={{ marginBottom: 16 }}>
            {topHoldings.map(item => (
              <Tag key={item.symbol}>
                {item.symbol} {(item.weight * 100).toFixed(2)}%
              </Tag>
            ))}
          </Space>

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
