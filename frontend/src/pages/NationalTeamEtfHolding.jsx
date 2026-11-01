import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Radio, Row, Space, Statistic, Table, Tag, Tooltip, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './Market.css';

const { Text } = Typography;
const fmtYi = value => (value === null || value === undefined ? '-' : `${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}亿`);
const fmtPct = value => (value === null || value === undefined ? '-' : `${Number(value).toFixed(2)}%`);
const errorMessage = error => error?.response?.data?.detail || error?.message || '加载国家队ETF持仓代理失败';

const ratioOption = rows => ({
  animation: false,
  tooltip: {
    trigger: 'axis',
    formatter: items => `${items[0]?.axisValueLabel || ''}<br/>${items.map(item => `${item.marker}${item.seriesName}：${item.seriesName === '沪深300收盘价' ? Number(item.value).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : fmtPct(item.value)}`).join('<br/>')}`,
  },
  legend: { top: 0 },
  grid: { left: 58, right: 58, top: 34, bottom: 52 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), boundaryGap: false, axisLabel: { hideOverlap: true } },
  yAxis: [
    { type: 'value', name: '%', splitLine: { lineStyle: { color: '#eef1f6' } } },
    { type: 'value', name: '点', splitLine: { show: false } },
  ],
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [
    { name: '核心ETF占流通市值', type: 'line', yAxisIndex: 0, showSymbol: false, data: rows.map(row => row.holding_ratio_pct), areaStyle: { color: 'rgba(37, 99, 235, .14)' }, lineStyle: { width: 2.2 }, itemStyle: { color: '#2563eb' } },
    { name: '沪深300收盘价', type: 'line', yAxisIndex: 1, showSymbol: false, data: rows.map(row => row.hs300_close), lineStyle: { width: 1.5 }, itemStyle: { color: '#d97706' } },
  ],
});

const scaleOption = rows => ({
  animation: false,
  tooltip: { trigger: 'axis', valueFormatter: fmtYi },
  legend: { top: 0 },
  grid: { left: 66, right: 24, top: 34, bottom: 52 },
  xAxis: { type: 'category', data: rows.map(row => row.trade_date), boundaryGap: false, axisLabel: { hideOverlap: true } },
  yAxis: { type: 'value', name: '亿元', splitLine: { lineStyle: { color: '#eef1f6' } } },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 12 }],
  series: [
    { name: '核心ETF市值', type: 'line', showSymbol: false, data: rows.map(row => row.etf_market_value_yi), areaStyle: { opacity: .08 }, lineStyle: { width: 2 }, itemStyle: { color: '#d97706' } },
    { name: '沪深300流通市值', type: 'line', showSymbol: false, data: rows.map(row => row.hs300_float_market_value_yi), lineStyle: { width: 1.5 }, itemStyle: { color: '#64748b' } },
  ],
});

const Percentile = ({ value }) => {
  const color = value >= 80 ? 'red' : value >= 50 ? 'orange' : value <= 20 ? 'green' : 'blue';
  return <Tag color={color}>{value === null || value === undefined ? '-' : `${Number(value).toFixed(1)}%`}</Tag>;
};

const NationalTeamEtfHolding = () => {
  const [days, setDays] = useState(1250);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await request.get('/api/market/national-team-etf-holding', { params: { days } });
      setData(response.data); setError('');
    } catch (err) { setError(errorMessage(err)); } finally { setLoading(false); }
  }, [days]);
  useEffect(() => { load(); }, [load]);
  const latest = data?.latest;
  const rows = data?.history || [];
  const funds = latest?.funds || [];
  const ratioChart = useMemo(() => ratioOption(rows), [rows]);
  const scaleChart = useMemo(() => scaleOption(rows), [rows]);
  const fundColumns = [
    { title: '核心沪深300ETF', dataIndex: 'name', key: 'name' },
    { title: '代码', dataIndex: 'ts_code', key: 'ts_code', width: 105 },
    { title: '估算市值', dataIndex: 'market_value_yi', key: 'market_value_yi', align: 'right', render: fmtYi },
    { title: '组合权重', dataIndex: 'weight_pct', key: 'weight_pct', align: 'right', render: fmtPct },
    { title: '收盘价', dataIndex: 'close', key: 'close', align: 'right', render: value => value === null || value === undefined ? '-' : Number(value).toFixed(3) },
  ];

  return <div className="national-team-holding">
    <div className="market-toolbar">
      <Space wrap>
        <Radio.Group value={days} onChange={event => setDays(event.target.value)} options={[
          { label: '1年', value: 250 }, { label: '3年', value: 750 }, { label: '5年', value: 1250 },
        ]} optionType="button" buttonStyle="solid" />
        <Tooltip title="从本地分析库读取；随A股基础数据与宽基ETF份额同步更新"><ReloadOutlined onClick={load} spin={loading} style={{ cursor: 'pointer' }} /></Tooltip>
        {latest && <Text type="secondary">数据截至 {latest.trade_date} · 四只核心ETF齐备</Text>}
      </Space>
    </div>
    {error && <Alert type="warning" showIcon message={error} className="market-alert" description="请先运行“A股基础数据同步”和“宽基ETF份额定向同步”；后者需回补四只核心沪深300ETF的历史份额。" />}
    {latest && <>
      <Alert className="national-team-holding__notice" type="info" showIcon message="国家队ETF持仓占比：观察政策资金配置宽基蓝筹的规模代理" description="指标只聚合华泰柏瑞、易方达、华夏、嘉实四只核心沪深300ETF；不代表国家队实际持仓或交易方向。" />
      <Row gutter={[12, 12]} className="national-team-holding__stats">
        <Col xs={12} sm={12} lg={6}><Card size="small"><Statistic title="核心ETF市值" value={latest.etf_market_value_yi} precision={0} suffix="亿" /></Card></Col>
        <Col xs={12} sm={12} lg={6}><Card size="small"><Statistic title="沪深300流通市值" value={latest.hs300_float_market_value_yi} precision={0} suffix="亿" /></Card></Col>
        <Col xs={12} sm={12} lg={6}><Card size="small"><Statistic title="ETF持仓占比" value={latest.holding_ratio_pct} precision={2} suffix="%" valueStyle={{ color: '#2563eb' }} /></Card></Col>
        <Col xs={12} sm={12} lg={6}><Card size="small"><Statistic title="5年历史分位" value={latest.holding_ratio_pct_percentile_5y} precision={1} suffix="%" /></Card></Col>
      </Row>
      <Row gutter={[12, 12]} className="national-team-holding__charts">
        <Col xs={24} lg={14}><Card title="核心ETF持仓占流通市值"><ReactECharts option={ratioChart} style={{ height: 350 }} notMerge lazyUpdate /></Card></Col>
        <Col xs={24} lg={10}><Card title="规模演变"><ReactECharts option={scaleChart} style={{ height: 350 }} notMerge lazyUpdate /></Card></Col>
      </Row>
      <Card className="national-team-holding__funds" title="核心基金拆分" extra={<Text type="secondary">按 {latest.trade_date} 收盘价估算</Text>}>
        <Table size="small" rowKey="ts_code" columns={fundColumns} dataSource={funds} pagination={false} scroll={{ x: 680 }} />
      </Card>
      <Card size="small" className="national-team-holding__method" title="指标说明">
        <Space wrap size={[24, 8]}><span>占比5年分位 <Percentile value={latest.holding_ratio_pct_percentile_5y} /></span><Text type="secondary">{data?.methodology?.percentile}</Text></Space>
        <div><Text type="secondary">公式：{data?.methodology?.formula}</Text></div>
        <div><Text type="secondary">口径与限制：{data?.methodology?.note}</Text></div>
      </Card>
    </>}
    {!loading && !latest && !error && <Empty description="暂无国家队ETF持仓代理数据" />}
  </div>;
};

export default NationalTeamEtfHolding;
