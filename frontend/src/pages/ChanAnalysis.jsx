import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Form, Input, InputNumber, Progress, Segmented, Select, Space, Spin, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';
import './ChanAnalysis.css';

const { Text, Title } = Typography;
const PERIODS = [
  { label: '1m', value: '1m' },
  { label: '5m', value: '5m' },
  { label: '30m', value: '30m' },
  { label: '日K', value: 'd' },
];

const compactSymbol = value => {
  const text = String(value || '').trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(text)) return text;
  if (/^\d{6}$/.test(text)) {
    if (text.startsWith('6')) return `${text}.SH`;
    if (text.startsWith('8') || text.startsWith('4')) return `${text}.BJ`;
    return `${text}.SZ`;
  }
  return text;
};

const fxIsTop = mark => String(mark || '').toLowerCase().includes('g') || String(mark || '').includes('顶');
const isUp = direction => String(direction || '').toLowerCase().includes('up') || String(direction || '').includes('向上');

const ChanAnalysis = () => {
  const [symbolInput, setSymbolInput] = useState('000001.SZ');
  const [symbol, setSymbol] = useState('000001.SZ');
  const [freq, setFreq] = useState('d');
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [layers, setLayers] = useState(['fractals', 'strokes', 'centers', 'signals']);
  const [scanForm] = Form.useForm();
  const [poolPreview, setPoolPreview] = useState(null);
  const [scan, setScan] = useState(null);
  const [scanLoading, setScanLoading] = useState(false);
  const [boardOptions, setBoardOptions] = useState([]);

  const scanPayload = useCallback(values => ({
    freq: values.freq || 'd',
    signal_side: 'buy',
    realtime: Boolean(values.realtime),
    filters: {
      min_total_mv: values.minTotalMv ? values.minTotalMv * 10000 : null,
      max_total_mv: values.maxTotalMv ? values.maxTotalMv * 10000 : null,
      min_avg_amount: values.minAvgAmount ? values.minAvgAmount * 100000 : null,
      liquidity_days: 20,
      index_codes: values.indexCodes || [],
      board_codes: values.boardCodes || [],
      exclude_st: true,
      limit: values.limit || 500,
    },
  }), []);

  const previewPool = async () => {
    const values = await scanForm.validateFields();
    setScanLoading(true);
    try {
      const response = await request.post('/api/chan-analysis/pools/preview', scanPayload(values).filters);
      setPoolPreview(response.data);
    } finally { setScanLoading(false); }
  };

  const startScan = async () => {
    const values = await scanForm.validateFields();
    setScanLoading(true);
    try {
      const response = await request.post('/api/chan-analysis/scans', scanPayload(values));
      setScan({ ...response.data, signals: [] });
    } finally { setScanLoading(false); }
  };

  useEffect(() => {
    if (!scan?.run_id || !['PENDING', 'RUNNING'].includes(scan.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const response = await request.get(`/api/chan-analysis/scans/${scan.run_id}`);
        setScan({ ...response.data, run_id: response.data.id || scan.run_id });
      } catch (err) { console.error('扫描状态加载失败', err); }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [scan?.run_id, scan?.status]);

  const loadChart = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await request.get(`/api/chan-analysis/chart/${symbol}`, {
        params: {
          freq,
          start_date: (freq === 'd' ? dayjs().subtract(3, 'year') : dayjs().subtract(35, 'day')).format('YYYY-MM-DD'),
          end_date: dayjs().format('YYYY-MM-DD'),
        },
      });
      setPayload(response.data);
    } catch (err) {
      setPayload(null);
      setError(err?.response?.data?.detail || '缠论数据加载失败');
    } finally {
      setLoading(false);
    }
  }, [freq, symbol]);

  useEffect(() => { loadChart(); }, [loadChart]);

  useEffect(() => {
    request.get('/api/chan-analysis/boards').then(response => {
      setBoardOptions((response.data || []).map(item => ({ label: `${item.name} · ${item.type}`, value: item.code })));
    }).catch(err => console.error('板块目录加载失败', err));
  }, []);

  const chartOption = useMemo(() => {
    const bars = payload?.bars || [];
    const analysis = payload?.analysis || {};
    if (!bars.length) return null;
    const dates = bars.map(item => dayjs(item.timestamp).format('YYYY-MM-DD HH:mm'));
    const dateIndex = new Map(dates.map((item, index) => [item, index]));
    const findIndex = value => {
      const key = dayjs(value).format('YYYY-MM-DD HH:mm');
      if (dateIndex.has(key)) return dateIndex.get(key);
      const dayKey = dayjs(value).format('YYYY-MM-DD');
      return dates.findIndex(item => item.startsWith(dayKey));
    };

    const strokeLines = layers.includes('strokes') ? (analysis.strokes || []).map(item => {
      const start = findIndex(item.start);
      const end = findIndex(item.end);
      const startPrice = isUp(item.direction) ? item.low : item.high;
      const endPrice = isUp(item.direction) ? item.high : item.low;
      return [{ coord: [start, startPrice] }, { coord: [end, endPrice] }];
    }).filter(item => item[0].coord[0] >= 0 && item[1].coord[0] >= 0) : [];

    const centerAreas = layers.includes('centers') ? (analysis.centers || []).filter(item => item.valid).map(item => [
      { xAxis: Math.max(0, findIndex(item.start)), yAxis: item.zd },
      { xAxis: Math.max(0, findIndex(item.end)), yAxis: item.zg },
    ]) : [];

    const fractals = layers.includes('fractals') ? (analysis.fractals || []).map(item => ({
      coord: [findIndex(item.dt), item.price],
      name: fxIsTop(item.mark) ? '顶分型' : '底分型',
      value: fxIsTop(item.mark) ? '顶' : '底',
      symbolRotate: fxIsTop(item.mark) ? 180 : 0,
      itemStyle: { color: fxIsTop(item.mark) ? '#cf1322' : '#389e0d' },
    })).filter(item => item.coord[0] >= 0) : [];
    const signalMarkers = layers.includes('signals') ? (analysis.signals || []).map(item => {
      const index = findIndex(item.bar_time);
      const bar = bars[Math.max(0, index)];
      const buy = String(item.type).includes('买');
      return {
        coord: [index, buy ? bar?.low : bar?.high],
        name: item.type,
        value: item.type,
        symbol: 'pin',
        symbolSize: 38,
        itemStyle: { color: item.confirmed ? (buy ? '#389e0d' : '#cf1322') : '#faad14' },
        label: { show: true, formatter: item.type, color: '#fff', fontSize: 10 },
      };
    }).filter(item => item.coord[0] >= 0) : [];

    return {
      animation: false,
      backgroundColor: '#fff',
      legend: { data: ['K线', '成交量'] },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 60, right: 24, top: 42, height: '66%' },
        { left: 60, right: 24, top: '77%', height: '14%' },
      ],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: true, axisLabel: { hideOverlap: true } },
        { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, axisLabel: { show: false } },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2 },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 45, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 4, start: 45, end: 100 },
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: bars.map(item => [item.open, item.close, item.low, item.high]),
          itemStyle: { color: '#ef232a', color0: '#14b143', borderColor: '#ef232a', borderColor0: '#14b143' },
          markPoint: { symbol: 'triangle', symbolSize: 9, label: { show: false }, data: [...fractals, ...signalMarkers] },
          markLine: {
            symbol: ['none', 'none'],
            silent: true,
            label: { show: false },
            lineStyle: { width: 2, color: '#1677ff' },
            data: strokeLines,
          },
          markArea: {
            silent: true,
            itemStyle: { color: 'rgba(250, 173, 20, 0.16)', borderColor: '#faad14', borderWidth: 1 },
            data: centerAreas,
          },
        },
        {
          name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: bars.map((item, index) => ({ value: item.volume, itemStyle: { color: item.close >= item.open ? '#ef232a' : '#14b143' } })),
        },
      ],
    };
  }, [layers, payload]);

  const submitSymbol = () => {
    const normalized = compactSymbol(symbolInput);
    if (normalized) setSymbol(normalized);
  };

  return (
    <div className="chan-page">
      <div className="chan-heading">
        <div><Title level={3}>缠论</Title><Text type="secondary">专业K线、缠论结构与官方CZSC信号</Text></div>
        <Space wrap>
          <Input.Search value={symbolInput} onChange={event => setSymbolInput(event.target.value)} onSearch={submitSymbol}
            enterButton={<SearchOutlined />} placeholder="000001.SZ" style={{ width: 190 }} />
          <Segmented options={PERIODS} value={freq} onChange={setFreq} />
          <Button icon={<ReloadOutlined />} onClick={loadChart}>刷新</Button>
        </Space>
      </div>
      {error && <Alert type="error" showIcon message={error} />}
      <Card className="chan-chart-card" title={`${symbol} · ${freq === 'd' ? '日K' : freq}`}
        extra={payload?.analysis && <Text type="secondary">CZSC {payload.analysis.czsc_version} · {payload.analysis.bar_count}根</Text>}>
        <Checkbox.Group options={[
          { label: '分型', value: 'fractals' }, { label: '笔', value: 'strokes' },
          { label: '中枢', value: 'centers' }, { label: '信号', value: 'signals' },
        ]} value={layers} onChange={setLayers} />
        <Spin spinning={loading}>
          <div className="chan-chart-wrap">
            {chartOption ? <ReactECharts option={chartOption} notMerge style={{ height: 650 }} /> : <Empty description="暂无K线数据" />}
          </div>
        </Spin>
      </Card>
      <Card title="当前缠论信号" className="chan-signal-card">
        {payload?.analysis?.signals?.length ? <Space wrap>{payload.analysis.signals.map(item => (
          <Tag color={String(item.type).includes('买') ? 'green' : 'red'} key={`${item.name}-${item.value}`}>
            {item.type} · {item.detail} · 已确认
          </Tag>
        ))}</Space> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前最后一根K线没有一、二、三类买卖点" />}
      </Card>
      <Card title="股票池缠论扫描" className="chan-signal-card"
        extra={<Text type="secondary">先按市值、流动性、指数过滤，再运行CZSC</Text>}>
        <Form form={scanForm} layout="inline" initialValues={{ freq: 'd', minTotalMv: 50, minAvgAmount: 1, limit: 500, realtime: false }}>
          <Form.Item name="freq" label="周期"><Select style={{ width: 90 }} options={[
            { label: '日K', value: 'd' }, { label: '30m', value: '30m' }, { label: '5m', value: '5m' }, { label: '1m', value: '1m' },
          ]} /></Form.Item>
          <Form.Item name="minTotalMv" label="最小市值(亿)"><InputNumber min={0} style={{ width: 110 }} /></Form.Item>
          <Form.Item name="maxTotalMv" label="最大市值(亿)"><InputNumber min={0} style={{ width: 110 }} /></Form.Item>
          <Form.Item name="minAvgAmount" label="20日均成交额(亿)"><InputNumber min={0} step={0.5} style={{ width: 110 }} /></Form.Item>
          <Form.Item name="indexCodes" label="指数"><Select mode="multiple" allowClear style={{ minWidth: 190 }} options={[
            { label: '沪深300', value: '000300.SH' }, { label: '中证500', value: '000905.SH' },
            { label: '中证1000', value: '000852.SH' }, { label: '中证全指', value: '000985.SH' },
          ]} /></Form.Item>
          <Form.Item name="boardCodes" label="板块"><Select mode="multiple" showSearch optionFilterProp="label" allowClear
            maxTagCount="responsive" style={{ minWidth: 210 }} options={boardOptions} placeholder="行业/概念/主题" /></Form.Item>
          <Form.Item name="limit" label="上限"><InputNumber min={1} max={2000} style={{ width: 90 }} /></Form.Item>
          <Form.Item name="realtime" valuePropName="checked"><Checkbox>盘中刷新实时1m</Checkbox></Form.Item>
          <Form.Item><Space><Button onClick={previewPool} loading={scanLoading}>预览股票池</Button>
            <Button type="primary" onClick={startScan} loading={scanLoading}>扫描买点</Button></Space></Form.Item>
        </Form>
        {poolPreview && <Alert className="chan-pool-alert" type="info" showIcon
          message={`过滤后 ${poolPreview.count} 只股票（受扫描上限约束）`} />}
        {scan && <div className="chan-scan-result">
          <Space wrap><Tag color={scan.status === 'SUCCESS' ? 'green' : 'blue'}>{scan.status}</Tag>
            <Text>候选 {scan.candidate_count ?? 0}</Text><Text>已扫描 {scan.processed_count ?? 0}</Text>
            <Text>买点 {scan.signal_count ?? 0}</Text><Text type={scan.error_count ? 'danger' : 'secondary'}>失败 {scan.error_count ?? 0}</Text></Space>
          {['PENDING', 'RUNNING'].includes(scan.status) && <Progress percent={scan.candidate_count ? Math.round((scan.processed_count || 0) / scan.candidate_count * 100) : 0} />}
          <Table size="small" rowKey="id" pagination={{ pageSize: 20 }} dataSource={scan.signals || []}
            onRow={row => ({ onDoubleClick: () => { setSymbol(row.ts_code); setSymbolInput(row.ts_code); setFreq(scan.freq); window.scrollTo({ top: 0, behavior: 'smooth' }); } })}
            columns={[
              { title: '代码', dataIndex: 'ts_code', width: 110 }, { title: '名称', dataIndex: 'name', width: 100 },
              { title: '信号', dataIndex: 'signal_type', render: (value, row) => <Tag color={row.confirmed ? 'green' : 'gold'}>{value}{row.confirmed ? '' : '·预判'}</Tag>, width: 110 },
              { title: '说明', dataIndex: 'detail' }, { title: '周期', dataIndex: 'bar_time', render: value => dayjs(value).format('YYYY-MM-DD HH:mm'), width: 150 },
              { title: '行业', dataIndex: 'industry', width: 110 },
            ]} />
        </div>}
      </Card>
    </div>
  );
};

export default ChanAnalysis;
