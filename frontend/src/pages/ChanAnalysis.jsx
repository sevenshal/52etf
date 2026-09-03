import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Form, InputNumber, Progress, Segmented, Select, Space, Spin, Switch, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
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
// 120 个交易日约对应 180 个自然日，实际交易日由后端行情数据决定。
const MINUTE_LOOKBACK_CALENDAR_DAYS = 180;

const fxIsTop = mark => {
  const value = String(mark || '').toLowerCase();
  return value.includes('top') || value.includes('g') || String(mark || '').includes('顶');
};
const isUp = direction => String(direction || '').toLowerCase().includes('up') || String(direction || '').includes('向上');

const ChanAnalysis = () => {
  const [symbol, setSymbol] = useState('000001.SZ');
  const [symbolOptions, setSymbolOptions] = useState([{ label: '平安银行 · 000001.SZ', value: '000001.SZ' }]);
  const [symbolSearching, setSymbolSearching] = useState(false);
  const symbolSearchTimer = useRef(null);
  const symbolSearchSequence = useRef(0);
  const [freq, setFreq] = useState('d');
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [scanForm] = Form.useForm();
  const [poolPreview, setPoolPreview] = useState(null);
  const [scan, setScan] = useState(null);
  const [scanLoading, setScanLoading] = useState(false);
  const [scanHistory, setScanHistory] = useState([]);
  const [boardOptions, setBoardOptions] = useState([]);
  const [showHistorySignals, setShowHistorySignals] = useState(false);

  const loadScanHistory = useCallback(async () => {
    try {
      const response = await request.get('/api/chan-analysis/scans', { params: { limit: 30 } });
      const rows = response.data || [];
      setScanHistory(rows);
      return rows;
    } catch (err) {
      console.error('扫描历史加载失败', err);
      return [];
    }
  }, []);

  const loadScanDetail = useCallback(async runId => {
    if (!runId) return;
    try {
      const response = await request.get(`/api/chan-analysis/scans/${runId}`);
      setScan({ ...response.data, run_id: response.data.id || runId });
    } catch (err) {
      console.error('扫描详情加载失败', err);
    }
  }, []);

  const searchSymbols = useCallback(query => {
    if (symbolSearchTimer.current) window.clearTimeout(symbolSearchTimer.current);
    const sequence = ++symbolSearchSequence.current;
    symbolSearchTimer.current = window.setTimeout(async () => {
      setSymbolSearching(true);
      try {
        const response = await request.get('/api/chan-analysis/symbols', {
          params: { q: String(query || '').trim(), limit: 30 },
        });
        if (sequence === symbolSearchSequence.current) setSymbolOptions(response.data || []);
      } catch (err) {
        console.error('股票搜索失败', err);
      } finally {
        if (sequence === symbolSearchSequence.current) setSymbolSearching(false);
      }
    }, 250);
  }, []);

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
      loadScanHistory();
    } catch (err) {
      // 扫描是单例的：已有任务在跑时，把正在跑的那条挂上来继续看进度。
      const rows = await loadScanHistory();
      const running = rows.find(row => ['PENDING', 'RUNNING'].includes(row.status));
      if (running) {
        loadScanDetail(running.id);
      } else {
        setError(err?.response?.data?.detail || '启动扫描失败');
      }
    } finally { setScanLoading(false); }
  };

  // 首次进入 / 刷新页面：拉历史，并自动挂上正在运行（或最近一条）的扫描。
  useEffect(() => {
    loadScanHistory().then(rows => {
      if (!rows.length) return;
      const active = rows.find(row => ['PENDING', 'RUNNING'].includes(row.status));
      loadScanDetail((active || rows[0]).id);
    });
  }, [loadScanHistory, loadScanDetail]);

  useEffect(() => {
    if (!scan?.run_id || !['PENDING', 'RUNNING'].includes(scan.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const response = await request.get(`/api/chan-analysis/scans/${scan.run_id}`);
        setScan({ ...response.data, run_id: response.data.id || scan.run_id });
        if (!['PENDING', 'RUNNING'].includes(response.data.status)) loadScanHistory();
      } catch (err) { console.error('扫描状态加载失败', err); }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [scan?.run_id, scan?.status, loadScanHistory]);

  const loadChart = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await request.get(`/api/chan-analysis/chart/${symbol}`, {
        params: {
          freq,
          start_date: (freq === 'd'
            ? dayjs().subtract(3, 'year')
            : dayjs().subtract(MINUTE_LOOKBACK_CALENDAR_DAYS, 'day')).format('YYYY-MM-DD'),
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

  useEffect(() => () => {
    if (symbolSearchTimer.current) window.clearTimeout(symbolSearchTimer.current);
  }, []);

  const handleChartReady = useCallback(chart => {
    chart.getZr().on('click', event => {
      if (!event.target) chart.dispatchAction({ type: 'hideTip' });
    });
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

    const strokeLines = (analysis.strokes || []).map(item => {
      const start = findIndex(item.start);
      const end = findIndex(item.end);
      const startPrice = isUp(item.direction) ? item.low : item.high;
      const endPrice = isUp(item.direction) ? item.high : item.low;
      const lineStyle = { color: '#595959', width: 1.4, opacity: 0.72, type: [3, 3] };
      return [
        { coord: [start, startPrice], lineStyle, segmentInfo: { ...item, start_price: startPrice, end_price: endPrice, confirmed: true, structure: '笔' } },
        { coord: [end, endPrice] },
      ];
    }).filter(item => item[0].coord[0] >= 0 && item[1].coord[0] >= 0);
    const segmentLines = (analysis.segments || []).map(item => {
      const start = findIndex(item.start);
      const end = findIndex(item.end);
      const up = isUp(item.direction);
      const startPrice = item.start_price ?? (up ? item.low : item.high);
      const endPrice = item.end_price ?? (up ? item.high : item.low);
      return {
        value: [start, startPrice, end, endPrice, 1],
        segmentInfo: { ...item, start_price: startPrice, end_price: endPrice, confirmed: true },
        itemStyle: { color: up ? '#722ed1' : '#eb2f96' },
      };
    }).filter(item => item.value[0] >= 0 && item.value[2] >= 0);
    const confirmedSegments = analysis.segments || [];
    const strokes = analysis.strokes || [];
    const lastSegment = confirmedSegments[confirmedSegments.length - 1];
    const lastStroke = strokes[strokes.length - 1];
    const candidateLines = [];
    if (lastStroke && lastSegment && dayjs(lastStroke.end).isAfter(dayjs(lastSegment.end))) {
      const up = isUp(lastStroke.direction);
      const start = findIndex(lastSegment.end);
      const end = findIndex(lastStroke.end);
      if (start >= 0 && end >= 0 && end > start) {
        const startPrice = lastSegment.end_price ?? (up ? lastSegment.high : lastSegment.low);
        const endPrice = up ? lastStroke.high : lastStroke.low;
        candidateLines.push({
          value: [start, startPrice, end, endPrice, 0],
          itemStyle: { color: up ? '#722ed1' : '#eb2f96' },
          segmentInfo: {
            direction: lastStroke.direction, start: lastSegment.end, end: lastStroke.end,
            start_price: startPrice, end_price: endPrice, stroke_count: 1,
            power_price: Math.abs(Number(endPrice) - Number(startPrice)), confirmed: false,
          },
        });
      }
    }

    const segmentTooltip = params => {
      const info = params?.data?.segmentInfo;
      if (!info) return '';
      const label = info.confirmed ? '方向性线段' : '当前方向候选 · 未确认';
      const direction = isUp(info.direction) ? '向上' : '向下';
      return `${label}<br/>方向：${direction}<br/>区间：${dayjs(info.start).format('YYYY-MM-DD HH:mm')} → ${dayjs(info.end).format('YYYY-MM-DD HH:mm')}<br/>起止价：${Number(info.start_price).toFixed(2)} → ${Number(info.end_price).toFixed(2)}<br/>笔数：${info.stroke_count || 1}<br/>强度：${Number(info.power_price || 0).toFixed(4)}`;
    };
    const renderSegment = (params, api) => {
      const start = api.coord([api.value(0), api.value(1)]);
      const end = api.coord([api.value(2), api.value(3)]);
      const chartLeft = params.coordSys.x;
      const chartRight = chartLeft + params.coordSys.width;
      if (Math.max(start[0], end[0]) < chartLeft || Math.min(start[0], end[0]) > chartRight) return null;
      const confirmed = Boolean(api.value(4));
      const color = api.visual('color');
      return {
        type: 'group',
        children: [
          {
            type: 'line',
            shape: { x1: start[0], y1: start[1], x2: end[0], y2: end[1] },
            style: { stroke: color, lineWidth: confirmed ? 2.2 : 1.8, opacity: 0.78, lineDash: confirmed ? [7, 5] : [2, 3] },
            emphasis: { style: { lineWidth: 4, opacity: 1 } },
          },
          {
            // 宽透明命中线让细线段容易悬停，同时不遮挡 K 线。
            type: 'line',
            shape: { x1: start[0], y1: start[1], x2: end[0], y2: end[1] },
            style: { stroke: 'rgba(0,0,0,0.001)', lineWidth: 14 },
          },
        ],
      };
    };

    const trendMark = trend => (trend === 'up' ? '↑' : trend === 'down' ? '↓' : trend === 'range' ? '⇄' : '');
    const centerAreas = (analysis.centers || []).filter(item => item.start && item.end).flatMap(item => {
      const x0 = Math.max(0, findIndex(item.start));
      const x1 = Math.max(0, findIndex(item.end));
      const broken = item.status === 'broken';
      const rects = [];
      // GG/DD 真实极值影线带：更浅，衬在固定区间后面。
      if (item.gg != null && item.dd != null && (item.gg > item.zg || item.dd < item.zd)) {
        rects.push([
          { xAxis: x0, yAxis: item.dd },
          { xAxis: x1, yAxis: item.gg, itemStyle: {
            color: broken ? 'rgba(140, 140, 140, 0.05)' : 'rgba(250, 173, 20, 0.06)',
            borderColor: broken ? '#bfbfbf' : '#ffd591', borderWidth: 1, borderType: 'dashed',
          } },
        ]);
      }
      // 固定区间 [ZD, ZG]，左上角标级别与中枢关系。
      rects.push([
        { xAxis: x0, yAxis: item.zd, label: {
          show: true,
          formatter: `L${item.level ?? 0}${trendMark(item.trend) ? ` ${trendMark(item.trend)}` : ''}`,
          position: 'insideTopLeft', color: broken ? '#8c8c8c' : '#d48806', fontSize: 10,
        } },
        { xAxis: x1, yAxis: item.zg, itemStyle: {
          color: broken ? 'rgba(140, 140, 140, 0.10)' : 'rgba(250, 173, 20, 0.16)',
          borderColor: broken ? '#999' : '#faad14', borderWidth: 1,
        } },
      ]);
      return rects;
    });

    const fractalPoints = (analysis.fractals || []).map(item => ({
      index: findIndex(item.dt), price: item.price,
      top: fxIsTop(item.mark),
    })).filter(item => item.index >= 0);
    // 分型只用极细的折线连接，保留“高-低-高/低-高-低”的节奏，避免三角标记遮挡K线。
    const fractalLines = fractalPoints.slice(1).map((point, index) => {
      const previous = fractalPoints[index];
      return [
        { coord: [previous.index, previous.price], lineStyle: { color: '#8c8c8c', width: 0.8, opacity: 0.55 } },
        { coord: [point.index, point.price], lineStyle: { color: '#8c8c8c', width: 0.8, opacity: 0.55 } },
      ];
    });
    const signalSlots = new Map();
    const visibleSignals = showHistorySignals ? (analysis.signal_history || analysis.signals || []) : (analysis.signals || []);
    const signalMarkers = visibleSignals.map(item => {
      const index = findIndex(item.bar_time);
      const bar = bars[Math.max(0, index)];
      const buy = String(item.type).includes('买');
      const slotKey = `${index}-${buy ? 'buy' : 'sell'}`;
      const slot = signalSlots.get(slotKey) || 0;
      signalSlots.set(slotKey, slot + 1);
      const horizontalOffset = slot === 0 ? 0 : Math.ceil(slot / 2) * 18 * (slot % 2 ? 1 : -1);
      const detail = item.detail && item.detail !== '任意' ? ` · ${item.detail}` : '';
      return {
        coord: [index, buy ? bar?.low : bar?.high],
        name: item.type,
        value: item.type,
        symbol: 'pin',
        symbolSize: 38,
        symbolOffset: [horizontalOffset, 0],
        itemStyle: { color: item.confirmed ? (buy ? '#389e0d' : '#cf1322') : '#faad14' },
        label: { show: true, formatter: item.type, color: '#fff', fontSize: 10 },
        tooltip: {
          formatter: `${item.type}${detail}<br/>${dayjs(item.bar_time).format('YYYY-MM-DD HH:mm')}<br/>${item.name}`,
        },
      };
    }).filter(item => item.coord[0] >= 0);

    return {
      animation: false,
      backgroundColor: '#fff',
      legend: { data: ['K线', '成交量'] },
      tooltip: { trigger: 'item', triggerOn: 'click', enterable: true },
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
        { type: 'inside', xAxisIndex: [0, 1], startValue: Math.max(0, bars.length - (freq === '1m' ? 600 : 1000)), endValue: bars.length - 1 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 4, startValue: Math.max(0, bars.length - (freq === '1m' ? 600 : 1000)), endValue: bars.length - 1 },
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: bars.map(item => [item.open, item.close, item.low, item.high]),
          tooltip: {
            show: true,
            formatter: params => {
              const values = params?.data || [];
              const change = Number(values[0]) ? ((Number(values[1]) - Number(values[0])) / Number(values[0]) * 100) : 0;
              return `K线<br/>时间：${params?.name || '-'}<br/>开盘：${Number(values[0]).toFixed(2)}<br/>收盘：${Number(values[1]).toFixed(2)}<br/>最低：${Number(values[2]).toFixed(2)}<br/>最高：${Number(values[3]).toFixed(2)}<br/>涨跌：${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
            },
          },
          itemStyle: { color: '#ef232a', color0: '#14b143', borderColor: '#ef232a', borderColor0: '#14b143' },
          markPoint: { label: { show: false }, data: signalMarkers },
          markLine: {
            symbol: ['none', 'none'],
            silent: true,
            label: { show: false },
            lineStyle: { width: 2, color: '#1677ff' },
            data: [
              ...fractalLines,
              ...strokeLines,
            ],
          },
          markArea: {
            silent: true,
            itemStyle: { color: 'rgba(250, 173, 20, 0.16)', borderColor: '#faad14', borderWidth: 1 },
            data: centerAreas,
          },
        },
        {
          name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          large: true, largeThreshold: 1000,
          tooltip: { show: false },
          data: bars.map((item, index) => ({ value: item.volume, itemStyle: { color: item.close >= item.open ? '#ef232a' : '#14b143' } })),
        },
        {
          id: 'directional-segments',
          name: '方向性线段',
          type: 'custom',
          coordinateSystem: 'cartesian2d',
          renderItem: renderSegment,
          data: [...segmentLines, ...candidateLines],
          encode: { x: [0, 2], y: [1, 3] },
          tooltip: { trigger: 'item', formatter: segmentTooltip },
          z: 20,
        },
      ],
    };
  }, [freq, payload, showHistorySignals]);

  return (
    <div className="chan-page">
      <div className="chan-heading">
        <div><Title level={3}>缠论</Title><Text type="secondary">专业K线、笔/线段/中枢与递归结构</Text></div>
        <Space wrap>
          <Select showSearch value={symbol} options={symbolOptions} loading={symbolSearching}
            filterOption={false} onSearch={searchSymbols} onDropdownVisibleChange={open => { if (open) searchSymbols(''); }}
            onChange={setSymbol} placeholder="输入股票名称或代码" notFoundContent={symbolSearching ? <Spin size="small" /> : '没有匹配股票'}
            style={{ width: 240 }} />
          <Segmented options={PERIODS} value={freq} onChange={setFreq} />
          <Button icon={<ReloadOutlined />} onClick={loadChart}>刷新</Button>
        </Space>
      </div>
      {error && <Alert type="error" showIcon message={error} />}
      <Card className="chan-chart-card" title={`${symbol} · ${freq === 'd' ? '日K' : freq}`}
        extra={payload?.analysis && <Space size="middle" wrap>
          <Space size={6}><Text type="secondary">历史买卖点</Text><Switch size="small" checked={showHistorySignals} onChange={setShowHistorySignals} /></Space>
          <Text type="secondary">方向性线段常显 · 悬停查看详情</Text>
          <Text type="secondary">自算结构引擎 v{payload.analysis.engine_version} · {payload.analysis.bar_count}根</Text>
        </Space>}>
        <Spin spinning={loading}>
          <div className="chan-chart-wrap">
            {chartOption ? <ReactECharts option={chartOption} lazyUpdate opts={{ renderer: 'canvas' }} onChartReady={handleChartReady} style={{ height: 650 }} /> : <Empty description="暂无K线数据" />}
          </div>
        </Spin>
      </Card>
      <Card title="当前缠论信号" className="chan-signal-card">
        {payload?.analysis?.signals?.length ? <Space wrap>{payload.analysis.signals.map(item => (
          <Tag color={item.confirmed ? (String(item.type).includes('买') ? 'green' : 'red') : 'gold'} key={`${item.name}-${item.value}`}>
            {item.type} · {item.detail} · {item.confirmed ? '已确认' : '盘中预判'}
          </Tag>
        ))}</Space> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前最后一根K线没有一、二、三类买卖点" />}
      </Card>
      <Card title="股票池缠论扫描" className="chan-signal-card"
        extra={<Text type="secondary">先按市值、流动性、指数过滤，再运行严格结构引擎</Text>}>
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
          <Form.Item name="limit" label="上限"><InputNumber min={1} max={5000} style={{ width: 90 }} /></Form.Item>
          <Form.Item name="realtime" valuePropName="checked"><Checkbox>盘中刷新实时1m</Checkbox></Form.Item>
          <Form.Item><Space><Button onClick={previewPool} loading={scanLoading}>预览股票池</Button>
            <Button type="primary" onClick={startScan} loading={scanLoading}>扫描买点</Button></Space></Form.Item>
        </Form>
        <Space wrap style={{ marginTop: 8 }}>
          <Text type="secondary">历史扫描</Text>
          <Select
            style={{ minWidth: 340 }} placeholder="选择一次扫描查看进度/结果"
            value={scan?.run_id || undefined}
            onChange={value => loadScanDetail(value)}
            options={scanHistory.map(row => ({
              value: row.id,
              label: `${dayjs(row.started_at).format('MM-DD HH:mm')} · ${row.freq} · ${row.status} · 候选${row.candidate_count ?? 0} · 买点${row.signal_count ?? 0}`,
            }))}
          />
          <Button icon={<ReloadOutlined />} size="small" onClick={loadScanHistory}>刷新列表</Button>
          {scan && ['PENDING', 'RUNNING'].includes(scan.status) && (
            <Button size="small" danger onClick={async () => {
              try { await request.post(`/api/chan-analysis/scans/${scan.run_id}/cancel`); } catch (err) { console.error('取消扫描失败', err); }
            }}>取消扫描</Button>
          )}
        </Space>
        {poolPreview && <Alert className="chan-pool-alert" type="info" showIcon
          message={`过滤后 ${poolPreview.count} 只股票（受扫描上限约束）`} />}
        {scan && <div className="chan-scan-result">
          <Space wrap><Tag color={scan.status === 'SUCCESS' ? 'green' : ['PENDING', 'RUNNING'].includes(scan.status) ? 'blue' : scan.status === 'FAILED' ? 'red' : 'default'}>{scan.status}</Tag>
            <Text type="secondary">{dayjs(scan.started_at).format('MM-DD HH:mm')}{scan.finished_at ? ` → ${dayjs(scan.finished_at).format('HH:mm')}` : ''}</Text>
            <Text>候选 {scan.candidate_count ?? 0}</Text><Text>已扫描 {scan.processed_count ?? 0}</Text>
            <Text>买点 {scan.signal_count ?? 0}</Text><Text type={scan.error_count ? 'danger' : 'secondary'}>失败 {scan.error_count ?? 0}</Text></Space>
          {['PENDING', 'RUNNING'].includes(scan.status) && <Progress percent={scan.candidate_count ? Math.round((scan.processed_count || 0) / scan.candidate_count * 100) : 0} />}
          <Table size="small" rowKey="id" pagination={{ pageSize: 20 }} dataSource={scan.signals || []}
            onRow={row => ({ onDoubleClick: () => {
              setSymbolOptions(previous => [
                { label: `${row.name || row.ts_code} · ${row.ts_code}`, value: row.ts_code },
                ...previous.filter(item => item.value !== row.ts_code),
              ]);
              setSymbol(row.ts_code);
              setFreq(scan.freq);
              window.scrollTo({ top: 0, behavior: 'smooth' });
            } })}
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
