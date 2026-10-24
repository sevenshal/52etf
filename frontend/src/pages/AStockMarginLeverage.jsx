import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Radio, Row, Space, Statistic, Table, Tag, Tooltip, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './Market.css';

const { Text } = Typography;

const fmtYi = value => (value === null || value === undefined ? '-' : `${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}亿`);
const fmtPct = value => (value === null || value === undefined ? '-' : `${Number(value).toFixed(2)}%`);
const errorMessage = error => error?.response?.data?.detail || error?.message || '加载两融数据失败';

const ratioOption = rows => ({
  animation: false,
  tooltip: { trigger: 'axis', valueFormatter: value => fmtPct(value) },
  legend: { top: 0 },
  grid: { left: 54, right: 48, top: 34, bottom: 52 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), boundaryGap: false, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', name: '%', splitLine: { lineStyle: { color: '#eef1f6' } } },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [
    { name: '融资占流通市值', type: 'line', showSymbol: false, data: rows.map(row => row.financing_ratio_pct), lineStyle: { width: 2 }, itemStyle: { color: '#e5484d' } },
    { name: '两融杠杆率', type: 'line', showSymbol: false, data: rows.map(row => row.leverage_ratio_pct), lineStyle: { width: 1.5 }, itemStyle: { color: '#2563eb' } },
    { name: '融券占流通市值', type: 'line', showSymbol: false, data: rows.map(row => row.short_ratio_pct), lineStyle: { width: 1.2 }, itemStyle: { color: '#8b5cf6' } },
  ],
});

const balanceOption = rows => ({
  animation: false,
  tooltip: { trigger: 'axis', valueFormatter: value => fmtYi(value) },
  legend: { top: 0 },
  grid: { left: 66, right: 22, top: 34, bottom: 52 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), boundaryGap: false, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', name: '亿元', splitLine: { lineStyle: { color: '#eef1f6' } } },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [
    { name: '融资余额', type: 'line', showSymbol: false, data: rows.map(row => row.financing_balance_yi), areaStyle: { opacity: 0.08 }, lineStyle: { width: 2 }, itemStyle: { color: '#e5484d' } },
    { name: '融券余额', type: 'line', showSymbol: false, data: rows.map(row => row.short_balance_yi), lineStyle: { width: 1.5 }, itemStyle: { color: '#8b5cf6' } },
  ],
});

const flowOption = rows => ({
  animation: false,
  tooltip: { trigger: 'axis', valueFormatter: value => fmtYi(value) },
  grid: { left: 62, right: 20, top: 20, bottom: 52 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', name: '亿元', splitLine: { lineStyle: { color: '#eef1f6' } } },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [{ name: '融资净买入', type: 'bar', data: rows.map(row => ({ value: row.financing_net_buy_yi, itemStyle: { color: Number(row.financing_net_buy_yi) >= 0 ? '#e5484d' : '#2f9e63' } })) }],
});

const Percentile = ({ value }) => {
  const color = value >= 80 ? 'red' : value >= 50 ? 'orange' : value <= 20 ? 'green' : 'blue';
  return <Tag color={color}>{value === null || value === undefined ? '-' : `${Number(value).toFixed(1)}%`}</Tag>;
};

const AStockMarginLeverage = () => {
  const [days, setDays] = useState(1250);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await request.get('/api/market/margin-leverage', { params: { days } });
      setData(response.data); setError('');
    } catch (err) { setError(errorMessage(err)); } finally { setLoading(false); }
  }, [days]);
  useEffect(() => { load(); }, [load]);
  const latest = data?.latest;
  const rows = data?.history || [];
  const ratioChart = useMemo(() => ratioOption(rows), [rows]);
  const balanceChart = useMemo(() => balanceOption(rows), [rows]);
  const flowChart = useMemo(() => flowOption(rows), [rows]);
  const incompleteDates = data?.data_quality?.excluded_incomplete_dates || [];
  const industryItems = data?.industry?.items || [];
  const industryColumns = [
    { title: '申万一级行业', dataIndex: 'industry', key: 'industry', width: 140 },
    { title: '融资余额', dataIndex: 'financing_balance_yi', key: 'financing_balance_yi', align: 'right', render: fmtYi, sorter: (a, b) => (a.financing_balance_yi || 0) - (b.financing_balance_yi || 0) },
    { title: '融资净买入', dataIndex: 'financing_net_buy_yi', key: 'financing_net_buy_yi', align: 'right', render: value => <span className={value >= 0 ? 'is-up' : 'is-down'}>{fmtYi(value)}</span>, sorter: (a, b) => (a.financing_net_buy_yi || 0) - (b.financing_net_buy_yi || 0) },
    { title: '融券余额', dataIndex: 'short_balance_yi', key: 'short_balance_yi', align: 'right', render: fmtYi, sorter: (a, b) => (a.short_balance_yi || 0) - (b.short_balance_yi || 0) },
    { title: '融资占流通市值', dataIndex: 'financing_ratio_pct', key: 'financing_ratio_pct', align: 'right', render: fmtPct, defaultSortOrder: 'descend', sorter: (a, b) => (a.financing_ratio_pct || 0) - (b.financing_ratio_pct || 0) },
    { title: '两融杠杆率', dataIndex: 'leverage_ratio_pct', key: 'leverage_ratio_pct', align: 'right', render: fmtPct, sorter: (a, b) => (a.leverage_ratio_pct || 0) - (b.leverage_ratio_pct || 0) },
    { title: '覆盖股票', dataIndex: 'stock_count', key: 'stock_count', align: 'right', sorter: (a, b) => (a.stock_count || 0) - (b.stock_count || 0) },
  ];

  return <div className="margin-leverage">
    <div className="market-toolbar">
      <Space wrap>
        <Radio.Group value={days} onChange={event => setDays(event.target.value)} options={[
          { label: '1年', value: 250 }, { label: '3年', value: 750 }, { label: '5年', value: 1250 },
        ]} optionType="button" buttonStyle="solid" />
        <Tooltip title="从本地分析库读取；随 A 股基础数据同步更新"><ReloadOutlined onClick={load} spin={loading} style={{ cursor: 'pointer' }} /></Tooltip>
        {latest && <Text type="secondary">数据截至 {latest.trade_date}</Text>}
      </Space>
    </div>
    {error && <Alert type="warning" showIcon message={error} className="market-alert" />}
    {!error && incompleteDates.length > 0 && (
      <Alert
        type="info"
        showIcon
        className="market-alert"
        message={`已跳过 ${incompleteDates.map(item => item.trade_date).join('、')} 的不完整两融数据`}
        description="融资比例与历史分位仅使用同时具备沪交所和深交所汇总数据的交易日；数据补齐后会自动恢复。"
      />
    )}
    {latest && <>
      <Row gutter={[12, 12]} className="margin-leverage__stats">
        <Col xs={12} sm={8} lg={4}><Card size="small"><Statistic title="融资余额" value={latest.financing_balance_yi} precision={0} suffix="亿" /></Card></Col>
        <Col xs={12} sm={8} lg={4}><Card size="small"><Statistic title="融券余额" value={latest.short_balance_yi} precision={1} suffix="亿" /></Card></Col>
        <Col xs={12} sm={8} lg={4}><Card size="small"><Statistic title="两融余额" value={latest.margin_balance_yi} precision={0} suffix="亿" /></Card></Col>
        <Col xs={12} sm={8} lg={4}><Card size="small"><Statistic title="融资净买入" value={latest.financing_net_buy_yi} precision={1} suffix="亿" valueStyle={{ color: latest.financing_net_buy_yi >= 0 ? '#e5484d' : '#2f9e63' }} /></Card></Col>
        <Col xs={12} sm={8} lg={4}><Card size="small"><Statistic title="融资占流通市值" value={latest.financing_ratio_pct} precision={2} suffix="%" /></Card></Col>
        <Col xs={12} sm={8} lg={4}><Card size="small"><Statistic title="两融杠杆率" value={latest.leverage_ratio_pct} precision={2} suffix="%" /></Card></Col>
      </Row>
      <Row gutter={[12, 12]} className="margin-leverage__charts">
        <Col xs={24} lg={14}><Card title="杠杆水平与融资比例"><ReactECharts option={ratioChart} style={{ height: 340 }} notMerge lazyUpdate /></Card></Col>
        <Col xs={24} lg={10}><Card title="余额结构"><ReactECharts option={balanceChart} style={{ height: 340 }} notMerge lazyUpdate /></Card></Col>
        <Col xs={24}><Card title="融资净买入"><ReactECharts option={flowChart} style={{ height: 260 }} notMerge lazyUpdate /></Card></Col>
      </Row>
      <Card size="small" className="margin-leverage__interpretation" title="市场杠杆解读">
        <Space wrap size={[24, 8]}>
          <span>融资比例 5 年分位 <Percentile value={latest.financing_ratio_pct_percentile_5y} /></span>
          <span>两融杠杆率 5 年分位 <Percentile value={latest.leverage_ratio_pct_percentile_5y} /></span>
          <Text type="secondary">{data?.methodology?.percentile}</Text>
        </Space>
        <div><Text type="secondary">口径：{data?.methodology?.note}</Text></div>
      </Card>
      <Card
        className="margin-leverage__industry"
        title="申万行业两融"
        extra={<Text type="secondary">明细截至 {data?.industry?.trade_date || '-'} · 按当前申万一级归属</Text>}
      >
        {industryItems.length
          ? <Table size="small" rowKey="industry" columns={industryColumns} dataSource={industryItems} pagination={{ pageSize: 12, hideOnSinglePage: true }} scroll={{ x: 820 }} />
          : <Alert type="info" showIcon message="行业两融明细尚未同步" description="运行“ A股两融明细同步 ”任务后，将展示申万一级行业的融资、融券与杠杆分布。" />}
      </Card>
    </>}
    {!loading && !latest && !error && <Empty description="暂无两融数据" />}
  </div>;
};

export default AStockMarginLeverage;
