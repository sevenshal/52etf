import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Radio, Row, Select, Space, Statistic, Table, Tag, Tooltip, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './Market.css';

const { Text } = Typography;
const fmt = (value, digits = 2) => (value === null || value === undefined ? '-' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits }));
const fmtPct = value => (value === null || value === undefined ? '-' : `${fmt(value)}%`);
const scopeOptions = [{ label: '全市场', value: 'market' }, { label: '指数', value: 'index' }, { label: '申万一级行业', value: 'industry' }];
const level = value => value >= 85 ? ['极度拥挤', 'red'] : value >= 70 ? ['偏拥挤', 'orange'] : value <= 30 ? ['偏冷', 'green'] : ['中性', 'blue'];
const errorMessage = error => error?.response?.data?.detail || error?.message || '加载拥挤度失败';

const lineOption = (rows, key, label, suffix = '') => ({
  animation: false,
  tooltip: { trigger: 'axis', valueFormatter: value => `${fmt(value)}${suffix}` },
  grid: { left: 54, right: 20, top: 28, bottom: 52 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), boundaryGap: false, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', name: suffix, splitLine: { lineStyle: { color: '#eef1f6' } } },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [{ name: label, type: 'line', showSymbol: false, data: rows.map(row => row[key]), lineStyle: { width: 2 }, areaStyle: { opacity: 0.06 }, itemStyle: { color: '#7c3aed' } }],
});

const scoreOption = rows => ({
  animation: false,
  tooltip: { trigger: 'axis', valueFormatter: value => `${fmt(value, 1)} 分` },
  grid: { left: 46, right: 20, top: 24, bottom: 52 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), boundaryGap: false, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', min: 0, max: 100, splitLine: { lineStyle: { color: '#eef1f6' } } },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [{ name: '综合拥挤度', type: 'line', showSymbol: false, data: rows.map(row => row.crowding_score), lineStyle: { width: 2.5 }, areaStyle: { opacity: 0.12 }, itemStyle: { color: '#dc2626' }, markLine: { symbol: 'none', label: { formatter: '{b}' }, data: [{ yAxis: 70, name: '偏拥挤' }, { yAxis: 85, name: '极度拥挤' }] } }],
});

const gaugeOption = score => ({
  animation: false, series: [{ type: 'gauge', min: 0, max: 100, startAngle: 210, endAngle: -30,
    progress: { show: true, width: 12 }, axisLine: { lineStyle: { width: 12, color: [[.3, '#4caf50'], [.7, '#f59e0b'], [1, '#dc2626']] } },
    pointer: { show: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { distance: -32, fontSize: 10 },
    detail: { valueAnimation: false, formatter: value => `${fmt(value, 1)}\n综合分`, fontSize: 22, offsetCenter: [0, '12%'] },
    data: [{ value: score || 0 }] }],
});

const AStockCrowding = () => {
  const [scope, setScope] = useState('market');
  const [entity, setEntity] = useState('');
  const [days, setDays] = useState(250);
  const [data, setData] = useState(null);
  const [northbound, setNorthbound] = useState(null);
  const [etfFlow, setEtfFlow] = useState(null);
  const [selectedKey, setSelectedKey] = useState('amount_to_float_mv_pct');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    if (scope !== 'market' && !entity) return;
    setLoading(true);
    try {
      const response = await request.get('/api/market/crowding', { params: { scope, entity, days } });
      setData(response.data); setError('');
    } catch (err) { setError(errorMessage(err)); } finally { setLoading(false); }
  }, [scope, entity, days]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    request.get('/api/a-stock-fund-flow/northbound').then(response => setNorthbound(response.data)).catch(() => setNorthbound(null));
    request.get('/api/a-stock-etf-share-flow/research', { params: { history_days: 250 } }).then(response => setEtfFlow(response.data)).catch(() => setEtfFlow(null));
  }, []);
  const options = scope === 'index' ? data?.options?.indexes || [] : data?.options?.industries || [];
  const rows = data?.history || [];
  const latest = data?.latest;
  const selected = data?.components?.find(item => item.key === selectedKey) || data?.components?.[0];
  const scoreChart = useMemo(() => scoreOption(rows), [rows]);
  const componentChart = useMemo(() => lineOption(rows, selected?.key, selected?.label || '', selected?.key?.includes('pct') ? '%' : ''), [rows, selected]);
  const diagnosticChart = useMemo(() => ({
    animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 }, grid: { left: 48, right: 22, top: 34, bottom: 52 },
    xAxis: { type: 'category', data: rows.map(row => row.trade_date), axisLabel: { hideOverlap: true } }, yAxis: { type: 'value', name: '%', splitLine: { lineStyle: { color: '#eef1f6' } } }, dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
    series: [
      { name: '上涨家数占比', type: 'line', showSymbol: false, data: rows.map(row => row.up_ratio_pct), itemStyle: { color: '#dc2626' } },
      { name: '涨停家数占比', type: 'line', showSymbol: false, data: rows.map(row => row.limit_up_ratio_pct), itemStyle: { color: '#f59e0b' } },
      { name: '收益离散度', type: 'line', showSymbol: false, data: rows.map(row => row.return_dispersion_pct), itemStyle: { color: '#2563eb' } },
    ],
  }), [rows]);
  const columns = [
    { title: '分项', dataIndex: 'label', key: 'label' },
    { title: '权重', dataIndex: 'weight', key: 'weight', align: 'right', render: value => `${fmt(value * 100, 0)}%` },
    { title: '当前值', dataIndex: 'value', key: 'value', align: 'right', render: value => fmt(value) },
    { title: '历史分位', dataIndex: 'percentile', key: 'percentile', align: 'right', render: value => <Tag color={level(value)[1]}>{fmtPct(value)}</Tag> },
  ];
  const northValue = northbound?.latest?.total_yi;
  return <div className="crowding-page">
    <div className="market-toolbar"><Space wrap>
      <Radio.Group value={scope} onChange={event => { setScope(event.target.value); setEntity(''); }} options={scopeOptions} optionType="button" buttonStyle="solid" />
      {scope !== 'market' && <Select className="crowding-page__select" placeholder={`选择${scope === 'index' ? '指数' : '行业'}`} value={entity || undefined} options={options} onChange={setEntity} showSearch optionFilterProp="label" />}
      <Radio.Group value={days} onChange={event => setDays(event.target.value)} options={[{ label: '1年', value: 250 }, { label: '3年', value: 750 }, { label: '5年', value: 1250 }]} optionType="button" buttonStyle="solid" />
      <Tooltip title="从本地分析库重新读取"><ReloadOutlined onClick={load} spin={loading} style={{ cursor: 'pointer' }} /></Tooltip>
      {latest && <Text type="secondary">{data.scope_label} · 数据截至 {latest.trade_date} · 覆盖 {fmt(latest.stock_count, 0)} 只证券</Text>}
    </Space></div>
    {error && <Alert type="warning" showIcon message={error} className="market-alert" />}
    {!error && scope !== 'market' && !entity && <Empty description="请选择分析范围" />}
    {latest && <>
      <Row gutter={[12, 12]} className="crowding-page__top">
        <Col xs={24} md={7}><Card title="综合拥挤度"><ReactECharts option={gaugeOption(latest.crowding_score)} style={{ height: 210 }} /><div className="crowding-page__score-note"><Tag color={level(latest.crowding_score)[1]}>{level(latest.crowding_score)[0]}</Tag><Text type="secondary">历史分位综合，不构成交易建议</Text></div></Card></Col>
        <Col xs={24} md={17}><Card title="综合拥挤度历史"><ReactECharts option={scoreChart} style={{ height: 270 }} notMerge lazyUpdate /></Card></Col>
      </Row>
      <Row gutter={[12, 12]} className="crowding-page__components">{data.components.map(item => <Col xs={12} sm={8} lg={6} xl={3} key={item.key}><Card size="small" hoverable onClick={() => setSelectedKey(item.key)} className={selected?.key === item.key ? 'is-selected' : ''}><Statistic title={item.label} value={item.percentile} precision={1} suffix="分位%" valueStyle={{ fontSize: 18, color: level(item.percentile)[1] === 'red' ? '#dc2626' : undefined }} /><Text type="secondary">值 {fmt(item.value)} · 权重 {fmt(item.weight * 100, 0)}%</Text></Card></Col>)}</Row>
      <Row gutter={[12, 12]}>
        <Col xs={24} lg={14}><Card title={`${selected?.label || '分项'}：历史水平`} extra={<Text type="secondary">点击上方分项切换</Text>}><ReactECharts option={componentChart} style={{ height: 320 }} notMerge lazyUpdate /></Card></Col>
        <Col xs={24} lg={10}><Card title="资金与期权补充指标（独立参考）"><Statistic title="沪深港通当日净流入" value={northValue} precision={1} suffix="亿元" valueStyle={{ color: northValue >= 0 ? '#dc2626' : '#16a34a' }} /><Text type="secondary">{northbound?.latest?.time ? `北向截至 ${northbound.latest.time}` : '北向：非交易时段或数据暂不可用'}</Text><Statistic className="crowding-page__etf-stat" title="宽基ETF近5日估算净申赎" value={etfFlow?.latest?.estimated_flow_amount_yi} precision={1} suffix="亿元" valueStyle={{ color: (etfFlow?.latest?.estimated_flow_amount_yi || 0) >= 0 ? '#dc2626' : '#16a34a' }} /><Text type="secondary">{etfFlow ? `数据截至 ${etfFlow.latest?.date} · ${etfFlow.proxy_note}` : 'ETF份额数据暂不可用'}</Text><Statistic className="crowding-page__etf-stat" title="全市场期权成交量 P/C" value={data.option_sentiment?.volume_pcr} precision={2} /><Text type="secondary">{data.option_sentiment?.trade_date ? `期权数据截至 ${data.option_sentiment.trade_date} · 持仓量 P/C ${fmt(data.option_sentiment.oi_pcr)}` : '期权日线尚未同步'}</Text></Card><Card size="small" className="crowding-page__method" title="计算口径"><Text type="secondary">{data.methodology.score}</Text></Card></Col>
        <Col xs={24} lg={14}><Card title="市场扩散与波动"><ReactECharts option={diagnosticChart} style={{ height: 290 }} notMerge lazyUpdate /></Card></Col>
        <Col xs={24} lg={10}><Card title="分项明细"><Table rowKey="key" size="small" columns={columns} dataSource={data.components} pagination={false} onRow={record => ({ onClick: () => setSelectedKey(record.key) })} /></Card></Col>
      </Row>
      <Alert className="crowding-page__caveat" type="info" showIcon message="口径与限制" description={`${data.methodology.index_membership} ${data.methodology.scope} 主力资金为数据源按成交单规模估算，并非交易所官方资金流；北向与ETF净申赎作为独立参考，暂不混入历史综合分。`} />
    </>}
  </div>;
};

export default AStockCrowding;
