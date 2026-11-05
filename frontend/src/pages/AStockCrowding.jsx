import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Row, Space, Statistic, Table, Tag, Tooltip, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './Market.css';

const { Text } = Typography;
const format = (value, digits = 2) => (value === null || value === undefined ? '-' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits }));
const formatPct = value => (value === null || value === undefined ? '-' : `${format(value)}%`);
const errorMessage = error => error?.response?.data?.detail || error?.message || '加载成交额集中度失败';

const trendOption = rows => ({
  animation: false,
  tooltip: { trigger: 'axis' },
  legend: { top: 0, data: ['成交额集中度', '上证指数'] },
  grid: { left: 54, right: 62, top: 38, bottom: 54 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), boundaryGap: false, axisLabel: { hideOverlap: true } },
  yAxis: [
    { type: 'value', name: '集中度 (%)', scale: true, axisLabel: { formatter: '{value}%' }, splitLine: { lineStyle: { color: '#eef1f6' } } },
    { type: 'value', name: '上证指数', scale: true, splitLine: { show: false }, axisLabel: { formatter: value => format(value, 0) } },
  ],
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [{
    name: '成交额集中度', type: 'line', showSymbol: false, data: rows.map(row => row.concentration_pct),
    lineStyle: { width: 2.5 }, areaStyle: { opacity: 0.1 }, itemStyle: { color: '#7c3aed' },
    markLine: { symbol: 'none', label: { formatter: '极端拥挤 45%' }, lineStyle: { color: '#dc2626', type: 'dashed' }, data: [{ yAxis: 45 }] },
  }, {
    name: '上证指数', type: 'line', yAxisIndex: 1, showSymbol: false, data: rows.map(row => row.shanghai_close),
    lineStyle: { width: 1.8 }, itemStyle: { color: '#16a34a' },
  }],
});

const industryOption = rows => ({
  animation: false,
  tooltip: { trigger: 'axis', valueFormatter: value => `${format(value)}%` },
  grid: { left: 88, right: 30, top: 16, bottom: 24 },
  xAxis: { type: 'value', name: '前5%篮子占比 (%)', splitLine: { lineStyle: { color: '#eef1f6' } } },
  yAxis: { type: 'category', data: rows.map(row => row.industry).reverse(), axisLabel: { width: 76, overflow: 'truncate' } },
  series: [{ name: '成交额占比', type: 'bar', data: rows.map(row => row.top_basket_share_pct).reverse(), itemStyle: { color: '#2563eb' }, barMaxWidth: 24 }],
});

const AStockCrowding = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await request.get('/api/market/crowding');
      setData(response.data);
      setError('');
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const latest = data?.latest;
  const history = data?.history || [];
  const industries = (data?.industry_composition || []).slice(0, 10);
  const concentrationChart = useMemo(() => trendOption(history), [history]);
  const industryChart = useMemo(() => industryOption(industries), [industries]);
  const extreme = latest?.level === '极端拥挤';
  const constituentColumns = [
    { title: '排名', dataIndex: 'rank', width: 72, align: 'right' },
    { title: '证券代码', dataIndex: 'ts_code', width: 112 },
    { title: '证券名称', dataIndex: 'name', ellipsis: true },
    { title: '成交额', dataIndex: 'amount_yi', align: 'right', render: value => `${format(value, 1)} 亿` },
    { title: '全市场成交额占比', dataIndex: 'market_amount_share_pct', align: 'right', render: formatPct },
    { title: '累计占比', dataIndex: 'cumulative_market_share_pct', align: 'right', render: formatPct },
  ];
  const industryColumns = [
    { title: '一级行业', dataIndex: 'industry' },
    { title: '入选股票数', dataIndex: 'stock_count', align: 'right' },
    { title: '成交额', dataIndex: 'amount_yi', align: 'right', render: value => `${format(value, 1)} 亿` },
    { title: '前5%篮子成交额占比', dataIndex: 'top_basket_share_pct', align: 'right', render: formatPct },
  ];

  return <div className="crowding-page crowding-concentration">
    <div className="market-toolbar"><Space wrap>
      <Tooltip title="从本地分析库重新读取"><ReloadOutlined onClick={load} spin={loading} style={{ cursor: 'pointer' }} /></Tooltip>
      {latest && <Text type="secondary">全市场 · 近五年 · 数据截至 {latest.trade_date} · 覆盖 {format(latest.stock_count, 0)} 只成交股票</Text>}
    </Space></div>
    {error && <Alert type="warning" showIcon message={error} className="market-alert" />}
    {!error && !latest && !loading && <Empty description="暂无可用的全市场成交额数据" />}
    {latest && <>
      <Row gutter={[12, 12]} className="crowding-page__top">
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="成交额集中度" value={latest.concentration_pct} precision={2} suffix="%" valueStyle={{ color: extreme ? '#dc2626' : '#2563eb' }} /><Tag className="crowding-concentration__tag" color={extreme ? 'red' : 'blue'}>{latest.level}</Tag><Text type="secondary">成交额前5%股票 ÷ 全市场</Text></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="纳入计算的头部股票" value={latest.top_stock_count} suffix="只" /><Text type="secondary">全市场成交股票的前5%</Text></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="头部股票成交额" value={latest.top_amount_yi} precision={1} suffix="亿元" /><Text type="secondary">全市场 {format(latest.total_amount_yi, 1)} 亿元</Text></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="连续极端拥挤" value={data.extreme_streak_days} suffix="个交易日" valueStyle={{ color: data.extreme_streak_days ? '#dc2626' : undefined }} /><Text type="secondary">仅统计严格高于45%的连续天数</Text></Card></Col>
      </Row>
      <Row gutter={[12, 12]}>
        <Col xs={24}><Card title="全市场成交额集中度历史" extra={<Text type="secondary">近五年 · 红线为极端拥挤阈值 45%</Text>}><ReactECharts option={concentrationChart} style={{ height: 350 }} notMerge lazyUpdate /></Card></Col>
        <Col xs={24} xl={15}><Card title="最新交易日前5%成交额股票"><Table rowKey="ts_code" size="small" columns={constituentColumns} dataSource={data.constituents} pagination={{ pageSize: 20, showSizeChanger: false }} scroll={{ x: 760 }} /></Card></Col>
        <Col xs={24} xl={9}><Card title="头部成交额的行业构成" extra={<Text type="secondary">仅为前5%篮子的拆解</Text>}><ReactECharts option={industryChart} style={{ height: 260 }} notMerge lazyUpdate /><Table rowKey="industry" size="small" columns={industryColumns} dataSource={industries} pagination={false} scroll={{ x: 480 }} /></Card></Col>
      </Row>
      <Alert className="crowding-page__caveat" type="info" showIcon message="唯一计算口径" description={<span>{data.methodology.formula} {data.methodology.threshold} {data.methodology.unit}</span>} />
    </>}
  </div>;
};

export default AStockCrowding;
