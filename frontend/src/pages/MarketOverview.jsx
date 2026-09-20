import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Radio, Row, Select, Space, Spin, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './Market.css';

const { Text } = Typography;

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const fmtYi = value => (value === null || value === undefined ? '-' : `${Number(value).toFixed(1)}亿`);
const fmtPct = (value, digits = 2) => (value === null || value === undefined ? '-' : `${Number(value).toFixed(digits)}%`);
const signedClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');

const AUTO_REFRESH_MS = 60 * 1000;
const UP_COLOR = '#e5484d';
const DOWN_COLOR = '#2f9e63';
const SH_COLOR = '#2f6fdb';
const SZ_COLOR = '#8b5cf6';

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

const VolumeCompare = ({ onData }) => {
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
      if (onData) onData(response.data);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(formatErrorMessage(err, '加载分时成交额失败'));
    } finally {
      if (!silent && seq === requestSeq.current) setLoading(false);
    }
  }, [targetDate, compareDate, onData]);

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


/* ---------- 指数概览条：现价 / 涨跌幅 / 当日分时迷你图 ---------- */
const Sparkline = ({ values, slots, color }) => {
  if (!values || values.length < 2) return <svg className="market-spark" viewBox="0 0 100 40" preserveAspectRatio="none" />;
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low < 1e-9 ? 1 : high - low;
  const pad = 3;
  const denom = (slots && values.length <= slots ? slots : values.length) - 1;
  const points = values
    .map((value, index) => `${(index / denom * 100).toFixed(2)},${(pad + (1 - (value - low) / span) * (40 - pad * 2)).toFixed(2)}`)
    .join(' ');
  const zeroY = low < 0 && high > 0 ? pad + (1 - (0 - low) / span) * (40 - pad * 2) : null;
  return (
    <svg className="market-spark" viewBox="0 0 100 40" preserveAspectRatio="none">
      {zeroY !== null && (
        <line x1="0" y1={zeroY} x2="100" y2={zeroY} stroke="#c8c8c8" strokeWidth="0.6" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" />
      )}
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
    </svg>
  );
};

const IndexStrip = ({ amountData }) => {
  const [items, setItems] = useState([]);

  const load = useCallback(async () => {
    try {
      const response = await request.get('/api/market/index-overview');
      setItems(response.data?.items || []);
    } catch (err) {
      // 指数条是辅助信息，失败时保持上一轮数据，不打断页面
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const lastIndex = amountData?.points
    ? amountData.points.reduce((acc, point, index) => (point.target_cum === null || point.target_cum === undefined ? acc : index), -1)
    : -1;
  const todayCum = lastIndex >= 0 ? amountData.points[lastIndex].target_cum : null;
  const prevCum = lastIndex >= 0 ? amountData.points[lastIndex].compare_cum : null;
  const deltaPct = todayCum !== null && prevCum ? (todayCum / prevCum - 1) * 100 : null;

  return (
    <div className="market-strip">
      <div className="market-tile market-tile--amount">
        <div className="market-tile__name">总成交额<Text type="secondary">（沪+深）</Text></div>
        <div className="market-tile__value">
          <b>{todayCum === null ? '--' : todayCum.toFixed(0)}</b><small>亿</small>
          {deltaPct !== null && (
            <span className={signedClass(deltaPct)}>
              {deltaPct >= 0 ? '▲' : '▼'} {fmtYi(Math.abs(todayCum - prevCum))} · {fmtPct(deltaPct, 1)}
            </span>
          )}
        </div>
        <div className="market-tile__sub">
          {amountData ? `${amountData.compare_date} 同期 ${fmtYi(prevCum)}` : '加载中…'}
        </div>
      </div>
      {items.map(item => (
        <div className="market-tile" key={item.key} title={`${item.name} ${item.price ?? '--'}`}>
          <div className="market-tile__name">{item.name}</div>
          <div className="market-tile__value">
            <b>{item.price ?? '--'}</b>
            <span className={signedClass(item.pct)}>
              {item.pct === null || item.pct === undefined ? '--' : `${item.pct > 0 ? '+' : ''}${item.pct.toFixed(2)}%`}
            </span>
          </div>
          <Sparkline values={item.spark} slots={item.spark_slots} color={(item.pct ?? 0) < 0 ? DOWN_COLOR : UP_COLOR} />
        </div>
      ))}
    </div>
  );
};

/* ---------- 大盘涨跌分布 ---------- */
const DIST_COLORS = ['#a31414', '#e0443c', '#f28073', '#f7bfb4', '#b9dcbf', '#6cc087', '#2e9e55', '#0a7a38'];

const DistributionCard = () => {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    request.get('/api/market/breadth-distribution')
      .then(response => setData(response.data))
      .catch(err => setError(formatErrorMessage(err, '加载涨跌分布失败')));
  }, []);

  const option = useMemo(() => {
    if (!data) return null;
    return {
      animation: false,
      grid: { left: 8, right: 8, top: 24, bottom: 24, containLabel: true },
      tooltip: {
        trigger: 'axis',
        formatter: params => params
          .map(item => `${item.name}：${item.value} 只（${(item.value / data.total * 100).toFixed(1)}%）`)
          .join('<br>'),
      },
      xAxis: { type: 'category', data: data.names, axisLabel: { interval: 0, fontSize: 11 } },
      yAxis: { type: 'value', splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [{
        type: 'bar',
        barCategoryGap: '25%',
        label: { show: true, position: 'top', fontSize: 11 },
        data: (data.counts || []).map((value, index) => ({ value, itemStyle: { color: DIST_COLORS[index] } })),
      }],
    };
  }, [data]);

  return (
    <Card
      size="small"
      title="大盘涨跌分布"
      extra={(
        <Text type="secondary">
          {data ? `${data.date} · 共 ${data.total} 只 · 涨 ${data.up_count} / 跌 ${data.down_count}` : '全A按当日涨跌幅分档'}
        </Text>
      )}
    >
      {error && <Alert type="warning" showIcon message={error} />}
      {option ? <ReactECharts option={option} style={{ height: 220 }} notMerge lazyUpdate /> : !error && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
    </Card>
  );
};

/* ---------- 每日成交额 ---------- */
const DAILY_RANGES = [60, 120, 250];

const DailyAmountCard = ({ todayAmount }) => {
  const [days, setDays] = useState(120);
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    request.get('/api/market/daily-amount', { params: { days } })
      .then(response => { if (!cancelled) { setData(response.data); setError(''); } })
      .catch(err => { if (!cancelled) setError(formatErrorMessage(err, '加载每日成交额失败')); });
    return () => { cancelled = true; };
  }, [days]);

  const option = useMemo(() => {
    if (!data) return null;
    return {
      animation: false,
      grid: { left: 64, right: 24, top: 30, bottom: 52 },
      legend: { top: 0, data: ['成交额', '区间均值'] },
      tooltip: { trigger: 'axis', valueFormatter: value => fmtYi(value) },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 8 }],
      xAxis: { type: 'category', data: data.dates, axisLabel: { interval: Math.floor(data.dates.length / 8) } },
      yAxis: { type: 'value', name: '亿元', splitLine: { lineStyle: { color: '#eef1f6' } } },
      series: [
        {
          name: '成交额',
          type: 'bar',
          data: (data.amounts || []).map((value, index) => ({
            value,
            itemStyle: { color: data.up[index] ? UP_COLOR : DOWN_COLOR },
          })),
          markLine: todayAmount ? {
            symbol: 'none',
            silent: true,
            lineStyle: { color: '#1f6fd1', width: 1.4 },
            label: { formatter: `当前量 ${todayAmount.toFixed(0)} 亿`, color: '#1f6fd1', position: 'insideEndTop', fontSize: 11 },
            data: [{ yAxis: todayAmount }],
          } : undefined,
        },
        {
          name: '区间均值',
          type: 'line',
          showSymbol: false,
          data: data.dates.map(() => data.avg),
          lineStyle: { color: '#e58a1a', width: 1.6, type: 'dashed' },
          itemStyle: { color: '#e58a1a' },
        },
      ],
    };
  }, [data, todayAmount]);

  return (
    <Card
      size="small"
      title="每日成交额"
      extra={(
        <Space size={8} wrap>
          <Text type="secondary">沪+深 · 亿元 · 均值 {data ? fmtYi(data.avg) : '--'}</Text>
          <Radio.Group
            size="small"
            value={days}
            onChange={event => setDays(event.target.value)}
            options={DAILY_RANGES.map(value => ({ label: `近${value}日`, value }))}
            optionType="button"
          />
        </Space>
      )}
    >
      {error && <Alert type="warning" showIcon message={error} />}
      {option ? <ReactECharts option={option} style={{ height: 260 }} notMerge lazyUpdate /> : !error && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
    </Card>
  );
};

const MarketOverview = () => {
  const [amountData, setAmountData] = useState(null);
  const handleVolumeData = useCallback(data => setAmountData(data), []);
  const todayAmount = amountData?.target_total ?? null;

  return (
    <div className="market-overview">
      <IndexStrip amountData={amountData} />
      <VolumeCompare onData={handleVolumeData} />
      <Row gutter={[12, 12]} className="market-overview__lower">
        <Col xs={24} xl={12}><DistributionCard /></Col>
        <Col xs={24} xl={12}><DailyAmountCard todayAmount={todayAmount} /></Col>
      </Row>
    </div>
  );
};

export default MarketOverview;
