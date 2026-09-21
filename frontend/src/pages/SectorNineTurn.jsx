import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { PlayCircleOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';
import './StockSystem.css';

const { Title, Text, Paragraph } = Typography;

const ACTION_META = {
  buy: { label: '买入', color: 'red' },
  sell: { label: '卖出', color: 'green' },
  hold: { label: '持有', color: 'blue' },
  none: { label: '无信号', color: 'default' },
};
const ORDER_STATUS = {
  pending: { label: '待成交', color: 'processing' },
  filled: { label: '已成交', color: 'green' },
  cancelled: { label: '已撤单', color: 'default' },
};
const RUN_STATUS = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  completed: { label: '已完成', color: 'green' },
  failed: { label: '失败', color: 'red' },
  cancelled: { label: '已取消', color: 'default' },
};

const isNumber = value => typeof value === 'number' && Number.isFinite(value);
const pct = (value, digits = 1) => (isNumber(value) ? `${value.toFixed(digits)}%` : '-');
const money = value => (isNumber(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '-');
const price = value => (isNumber(value) ? value.toFixed(2) : '-');
const errorText = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const SignedPct = ({ value }) => {
  if (!isNumber(value)) return <span>-</span>;
  const className = value > 0 ? 'stock-system__up' : value < 0 ? 'stock-system__down' : '';
  return <span className={className}>{`${value > 0 ? '+' : ''}${value.toFixed(2)}%`}</span>;
};

const StockCell = ({ row }) => (
  <div>
    <StockDetailLink symbol={row.ts_code}>{row.name || row.ts_code}</StockDetailLink>
    <div className="stock-system__code">{row.ts_code}</div>
  </div>
);

const ActionTag = ({ action }) => {
  const meta = ACTION_META[action] || ACTION_META.none;
  return <Tag color={meta.color}>{meta.label}</Tag>;
};

// ---------------------------------------------------------------------------
// 每日信号
// ---------------------------------------------------------------------------

const DailyTab = ({ refreshToken }) => {
  const [data, setData] = useState(null);
  const [tradeDate, setTradeDate] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async day => {
    setLoading(true);
    try {
      const response = await request.get('/api/sector-nine-turn/daily', {
        params: day ? { trade_date: day } : undefined,
      });
      setData(response.data);
      setTradeDate(response.data?.trade_date || null);
      setError('');
    } catch (requestError) {
      setError(errorText(requestError, '加载每日信号失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load, refreshToken]);

  const sectors = data?.sectors || [];
  const signals = data?.signals || [];
  const armed = sectors.filter(row => row.armed);
  const buys = signals.filter(row => row.action === 'buy');
  const sells = signals.filter(row => row.action === 'sell');
  const holdings = signals.filter(row => row.action === 'hold');

  const sectorColumns = [
    { title: '板块', dataIndex: 'index_name', render: (value, row) => (
      <div>
        <div>{value || row.index_code}</div>
        <div className="stock-system__code">{row.index_code}</div>
      </div>
    ) },
    { title: '类型', dataIndex: 'category', width: 90, render: value => <Tag>{value}</Tag> },
    { title: '收盘', dataIndex: 'close', width: 100, align: 'right', render: price },
    { title: '九转', width: 120, render: (_, row) => (
      <Space size={4}>
        {row.high_count > 0 ? <Tag color="red">{`高${row.high_count}`}</Tag> : null}
        {row.low_count > 0 ? <Tag color="green">{`低${row.low_count}`}</Tag> : null}
        {row.high_count === 0 && row.low_count === 0 ? <Text type="secondary">-</Text> : null}
      </Space>
    ) },
    { title: '贪恐', dataIndex: 'fear_score', width: 90, align: 'right',
      render: value => (isNumber(value) ? value.toFixed(1) : '-'),
      sorter: (a, b) => (a.fear_score ?? 999) - (b.fear_score ?? 999) },
    { title: '低9', dataIndex: 'low9_date', width: 110, render: value => value || '-' },
    { title: '状态', width: 150, render: (_, row) => {
      if (row.armed) return <Tag color="red">{`布防中（${row.armed_since}）`}</Tag>;
      if (row.turn_signal) return <Tag color="orange">今日触发·贪恐未过</Tag>;
      if (row.low9_armed) return <Tag color="blue">已低9，等高2</Tag>;
      return <Text type="secondary">-</Text>;
    } },
  ];

  const signalColumns = [
    { title: '股票', width: 160, render: (_, row) => <StockCell row={row} /> },
    { title: '信号', dataIndex: 'action', width: 90, render: value => <ActionTag action={value} /> },
    { title: '序号', dataIndex: 'rank', width: 70, align: 'right', render: value => value ?? '-' },
    { title: '板块', dataIndex: 'sector_name', width: 130, render: (value, row) => value || row.sector_code || '-' },
    { title: '板块贪恐', dataIndex: 'sector_fear_score', width: 100, align: 'right',
      render: value => (isNumber(value) ? value.toFixed(1) : '-') },
    { title: '收盘', dataIndex: 'close', width: 90, align: 'right', render: price },
    { title: '九转', width: 110, render: (_, row) => (
      <Space size={4}>
        {row.high_count > 0 ? <Tag color="red">{`高${row.high_count}`}</Tag> : null}
        {row.low_count > 0 ? <Tag color="green">{`低${row.low_count}`}</Tag> : null}
      </Space>
    ) },
    { title: '回撤(ATR)', dataIndex: 'rising_drawdown_atr', width: 110, align: 'right',
      render: value => (isNumber(value) ? value.toFixed(2) : '-') },
    { title: '说明', dataIndex: 'note', render: value => value || '-' },
  ];

  return (
    <Spin spinning={loading}>
      {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} /> : null}
      <div className="stock-system__toolbar">
        <Space wrap>
          <Select
            style={{ width: 160 }}
            value={tradeDate}
            options={(data?.available_dates || []).map(day => ({ label: day, value: day }))}
            onChange={value => load(value)}
            placeholder="交易日"
          />
          <Button icon={<ReloadOutlined />} onClick={() => load(tradeDate)} />
        </Space>
        <Space size="large" wrap>
          <Statistic title="布防板块" value={armed.length} suffix={`/ ${sectors.length}`} />
          <Statistic title="买入信号" value={buys.length} />
          <Statistic title="卖出信号" value={sells.length} />
        </Space>
      </div>

      {!sectors.length ? (
        <Empty description="还没有快照，先点右上角「立即计算」" />
      ) : (
        <>
          <Title level={5} style={{ marginTop: 16 }}>个股信号</Title>
          <Paragraph type="secondary" style={{ marginTop: 0 }}>
            买入信号按板块贪恐分数从低到高排序，模拟盘按空仓位从上往下取；卖出信号来自持仓。
          </Paragraph>
          <Table
            rowKey="ts_code"
            size="small"
            columns={signalColumns}
            dataSource={[...buys, ...sells, ...holdings]}
            pagination={{ defaultPageSize: 20, hideOnSinglePage: true }}
            locale={{ emptyText: '当天没有个股信号' }}
          />

          <Title level={5} style={{ marginTop: 24 }}>板块状态</Title>
          <Table
            rowKey="index_code"
            size="small"
            columns={sectorColumns}
            dataSource={sectors}
            pagination={{ defaultPageSize: 20, hideOnSinglePage: true }}
          />
        </>
      )}
    </Spin>
  );
};

// ---------------------------------------------------------------------------
// 模拟盘
// ---------------------------------------------------------------------------

const PaperTab = ({ refreshToken }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await request.get('/api/sector-nine-turn/paper');
      setData(response.data);
      setError('');
    } catch (requestError) {
      setError(errorText(requestError, '加载模拟盘失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load, refreshToken]);

  const reset = async () => {
    try {
      const response = await request.post('/api/sector-nine-turn/paper/reset');
      setData(response.data);
      message.success('模拟盘已重置');
    } catch (requestError) {
      message.error(errorText(requestError, '重置失败'));
    }
  };

  const account = data?.account;
  const navs = useMemo(() => data?.navs || [], [data]);
  const chartOption = useMemo(() => ({
    grid: { left: 60, right: 20, top: 20, bottom: 40 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: navs.map(point => point.trade_date) },
    yAxis: { type: 'value', scale: true },
    series: [{ name: '净值', type: 'line', showSymbol: false, data: navs.map(point => point.nav) }],
  }), [navs]);

  const positionColumns = [
    { title: '股票', width: 160, render: (_, row) => <StockCell row={row} /> },
    { title: '板块', dataIndex: 'sector_name', width: 120, render: value => value || '-' },
    { title: '股数', dataIndex: 'quantity', width: 90, align: 'right' },
    { title: '成本', dataIndex: 'entry_price', width: 90, align: 'right', render: price },
    { title: '现价', dataIndex: 'last_price', width: 90, align: 'right', render: price },
    { title: '市值', dataIndex: 'market_value', width: 110, align: 'right', render: money },
    { title: '收益', dataIndex: 'return_pct', width: 100, align: 'right', render: value => <SignedPct value={value} /> },
    { title: '仓位', dataIndex: 'weight_pct', width: 90, align: 'right', render: value => pct(value) },
    { title: '买入日', dataIndex: 'entry_date', width: 110 },
    { title: '卖出规则', width: 140, render: (_, row) => (row.high9_date
      ? <Tag color="orange">{`高9 ${row.high9_date}，等低2`}</Tag>
      : <Text type="secondary">还没出现高9</Text>) },
  ];

  const orderColumns = [
    { title: '信号日', dataIndex: 'signal_date', width: 110 },
    { title: '股票', width: 160, render: (_, row) => <StockCell row={row} /> },
    { title: '方向', dataIndex: 'side', width: 80,
      render: value => <Tag color={value === 'buy' ? 'red' : 'green'}>{value === 'buy' ? '买入' : '卖出'}</Tag> },
    { title: '状态', dataIndex: 'status', width: 100, render: value => {
      const meta = ORDER_STATUS[value] || { label: value, color: 'default' };
      return <Tag color={meta.color}>{meta.label}</Tag>;
    } },
    { title: '成交日', dataIndex: 'exec_date', width: 110, render: value => value || '-' },
    { title: '成交价', dataIndex: 'fill_price', width: 90, align: 'right', render: price },
    { title: '金额', dataIndex: 'amount', width: 110, align: 'right', render: money },
    { title: '盈亏', dataIndex: 'realized_pnl', width: 110, align: 'right',
      render: value => (isNumber(value) ? money(value) : '-') },
    { title: '说明', render: (_, row) => row.message || row.reason || '-' },
  ];

  return (
    <Spin spinning={loading}>
      {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} /> : null}
      {!account ? (
        <Empty description="模拟盘还没开始，先跑一次每日计算" />
      ) : (
        <>
          <div className="stock-system__toolbar">
            <Space size="large" wrap>
              <Statistic title="净值" value={money(account.nav)} />
              <Statistic title="现金" value={money(account.cash)} />
              <Statistic title="总收益" value={pct(data.stats?.total_return_pct, 2)} />
              <Statistic title="最大回撤" value={pct(data.stats?.max_drawdown_pct, 2)} />
              <Statistic title="持仓" value={data.stats?.position_count ?? 0} />
            </Space>
            <Space>
              <Button icon={<ReloadOutlined />} onClick={load} />
              <Popconfirm title="清空持仓、订单和净值，按配置的初始资金重建？" onConfirm={reset}>
                <Button danger>重置模拟盘</Button>
              </Popconfirm>
            </Space>
          </div>
          <Paragraph type="secondary">
            起始 {account.started_on || '-'}，已结算到 {account.last_trade_date || '-'}；
            初始资金 {money(account.initial_capital)}。信号在收盘确定，下一交易日开盘撮合。
          </Paragraph>
          {navs.length ? <ReactECharts option={chartOption} style={{ height: 260 }} notMerge /> : null}

          <Title level={5} style={{ marginTop: 16 }}>持仓</Title>
          <Table rowKey="ts_code" size="small" columns={positionColumns} dataSource={data.positions || []}
                 pagination={false} locale={{ emptyText: '空仓' }} />

          <Title level={5} style={{ marginTop: 24 }}>订单</Title>
          <Table rowKey="id" size="small" columns={orderColumns} dataSource={data.orders || []}
                 pagination={{ defaultPageSize: 20, hideOnSinglePage: true }} />
        </>
      )}
    </Spin>
  );
};

// ---------------------------------------------------------------------------
// 回测
// ---------------------------------------------------------------------------

const BacktestTab = () => {
  const [state, setState] = useState(null);
  const [detail, setDetail] = useState(null);
  const [selected, setSelected] = useState(null);
  const [form, setForm] = useState({ start_date: '', end_date: '', random_trials: 25 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadList = useCallback(async () => {
    try {
      const response = await request.get('/api/sector-nine-turn/backtests');
      setState(response.data);
      setForm(current => ({
        start_date: current.start_date || response.data.defaults.start_date,
        end_date: current.end_date || response.data.defaults.end_date,
        random_trials: current.random_trials ?? response.data.defaults.random_trials,
      }));
      const active = (response.data.runs || []).find(run => run.status === 'running' || run.status === 'queued');
      if (!selected && response.data.runs?.length) setSelected(active?.id || response.data.runs[0].id);
      setError('');
    } catch (requestError) {
      setError(errorText(requestError, '加载回测列表失败'));
    }
  }, [selected]);

  const loadDetail = useCallback(async runId => {
    if (!runId) return;
    try {
      const response = await request.get(`/api/sector-nine-turn/backtests/${runId}`);
      setDetail(response.data);
    } catch (requestError) {
      setError(errorText(requestError, '加载回测结果失败'));
    }
  }, []);

  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { loadDetail(selected); }, [loadDetail, selected]);
  useEffect(() => {
    const active = (state?.runs || []).some(run => run.status === 'running' || run.status === 'queued');
    if (!active) return undefined;
    const timer = setInterval(() => { loadList(); loadDetail(selected); }, 4000);
    return () => clearInterval(timer);
  }, [state, loadList, loadDetail, selected]);

  const start = async () => {
    setLoading(true);
    try {
      const response = await request.post('/api/sector-nine-turn/backtests', form);
      setSelected(response.data.id);
      message.success('回测已开始');
      loadList();
    } catch (requestError) {
      message.error(errorText(requestError, '启动回测失败'));
    } finally {
      setLoading(false);
    }
  };

  const summary = detail?.summary;
  const variants = summary?.variants || {};
  const rows = [
    ...Object.values(variants),
    ...(summary?.benchmark ? [summary.benchmark] : []),
  ];

  const navs = useMemo(() => detail?.navs || {}, [detail]);
  const calendar = useMemo(
    () => (navs.strategy || navs.benchmark || []).map(point => point.trade_date),
    [navs],
  );
  const variantLabels = useMemo(() => {
    const labels = { benchmark: '中证全指' };
    (detail?.variants || []).forEach(item => { labels[item.key] = item.label; });
    return labels;
  }, [detail]);
  const chartOption = useMemo(() => ({
    grid: { left: 60, right: 20, top: 30, bottom: 40 },
    tooltip: { trigger: 'axis' },
    legend: { data: Object.keys(navs).map(key => variantLabels[key] || key) },
    xAxis: { type: 'category', data: calendar },
    yAxis: { type: 'value', scale: true },
    series: Object.entries(navs).map(([key, points]) => ({
      name: variantLabels[key] || key, type: 'line', showSymbol: false,
      data: points.map(point => point.nav),
    })),
  }), [navs, calendar, variantLabels]);

  const summaryColumns = [
    { title: '方案', dataIndex: 'label' },
    { title: '总收益', dataIndex: 'total_return_pct', align: 'right', render: value => <SignedPct value={value} /> },
    { title: '年化', dataIndex: 'cagr_pct', align: 'right', render: value => <SignedPct value={value} /> },
    { title: '最大回撤', dataIndex: 'max_drawdown_pct', align: 'right', render: value => pct(value, 1) },
    { title: '信号数', dataIndex: 'trades', align: 'right', render: value => value ?? '-' },
    { title: '入组合', dataIndex: 'portfolio_trades', align: 'right', render: value => value ?? '-' },
    { title: '胜率', dataIndex: 'win_rate_pct', align: 'right', render: value => pct(value, 1) },
    { title: '单笔均值', dataIndex: 'mean_return_pct', align: 'right', render: value => <SignedPct value={value} /> },
    { title: '期末未平仓', dataIndex: 'open_at_end', align: 'right', render: value => value ?? '-' },
    { title: '随机挑股中位', dataIndex: 'random_pick_return_median_pct', align: 'right',
      render: (value, row) => (isNumber(value)
        ? <Tooltip title={`10%~90% 分位：${pct(row.random_pick_return_p10_pct, 1)} ~ ${pct(row.random_pick_return_p90_pct, 1)}`}>
            <span><SignedPct value={value} /></span>
          </Tooltip>
        : '-') },
  ];

  const tradeColumns = [
    { title: '股票', width: 160, render: (_, row) => <StockCell row={row} /> },
    { title: '板块', dataIndex: 'sector_name', width: 120, render: value => value || '-' },
    { title: '买入', dataIndex: 'entry_date', width: 110 },
    { title: '卖出', dataIndex: 'exit_date', width: 110 },
    { title: '持有(交易日)', dataIndex: 'holding_days', width: 110, align: 'right' },
    { title: '收益', dataIndex: 'return_pct', width: 100, align: 'right', render: value => <SignedPct value={value} /> },
    { title: '出场', dataIndex: 'closed', width: 110,
      render: value => (value ? <Tag color="green">规则卖出</Tag> : <Tag>期末未平仓</Tag>) },
  ];

  return (
    <div>
      {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} /> : null}
      <Space wrap className="stock-system__backtest-form">
        <Input type="date" value={form.start_date} style={{ width: 150 }}
               onChange={event => setForm({ ...form, start_date: event.target.value })} />
        <Input type="date" value={form.end_date} style={{ width: 150 }}
               onChange={event => setForm({ ...form, end_date: event.target.value })} />
        <Tooltip title="同一天信号多于空仓位时，随机挑股重跑多次，看结果里有多少是运气">
          <InputNumber min={0} max={200} value={form.random_trials} addonBefore="随机试验"
                       onChange={value => setForm({ ...form, random_trials: value })} />
        </Tooltip>
        <Button type="primary" icon={<PlayCircleOutlined />} loading={loading} onClick={start}>开始回测</Button>
        <Button icon={<ReloadOutlined />} onClick={() => { loadList(); loadDetail(selected); }} />
      </Space>

      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          style={{ width: 340 }}
          value={selected}
          onChange={setSelected}
          placeholder="选择一次回测"
          options={(state?.runs || []).map(run => ({
            value: run.id,
            label: `#${run.id} ${run.params?.start_date} ~ ${run.params?.end_date} · ${(RUN_STATUS[run.status] || {}).label || run.status}`,
          }))}
        />
        {detail?.status === 'running' || detail?.status === 'queued' ? (
          <Button danger onClick={async () => {
            await request.post(`/api/sector-nine-turn/backtests/${detail.id}/cancel`);
            loadList();
          }}>取消</Button>
        ) : null}
        {detail && detail.status !== 'running' && detail.status !== 'queued' ? (
          <Popconfirm title="删除这次回测？" onConfirm={async () => {
            await request.delete(`/api/sector-nine-turn/backtests/${detail.id}`);
            setSelected(null); setDetail(null); loadList();
          }}>
            <Button>删除</Button>
          </Popconfirm>
        ) : null}
      </Space>

      {!detail ? <Empty description="还没有回测记录" /> : (
        <>
          {detail.status === 'running' || detail.status === 'queued' ? (
            <div style={{ marginBottom: 12 }}>
              <Progress percent={Math.round(detail.progress || 0)} status="active" />
              <Text type="secondary">{detail.message}</Text>
            </div>
          ) : null}
          {detail.status === 'failed' ? (
            <Alert type="error" showIcon message="回测失败" description={<pre className="stock-system__pre">{detail.message}</pre>} />
          ) : null}
          {summary ? (
            <>
              <Paragraph type="secondary">
                {summary.start} ~ {summary.end}，板块 {summary.sectors} 个，板块触发 {summary.sector_triggers} 次
                （过贪恐闸门 {summary.sector_triggers_fear_passed} 次），候选股票 {summary.candidate_stocks} 只，
                最多持仓 {summary.max_positions} 只。
              </Paragraph>
              <Table rowKey="key" size="small" pagination={false} columns={summaryColumns} dataSource={rows} />
              {calendar.length ? <ReactECharts option={chartOption} style={{ height: 320, marginTop: 16 }} notMerge /> : null}
              <Title level={5} style={{ marginTop: 24 }}>完整规则的成交明细</Title>
              <Table rowKey={(row, index) => `${row.ts_code}-${row.entry_date}-${index}`} size="small"
                     columns={tradeColumns} dataSource={detail.trades?.strategy || []}
                     pagination={{ defaultPageSize: 20, hideOnSinglePage: true }} />
            </>
          ) : null}
        </>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// 参数配置
// ---------------------------------------------------------------------------

const ConfigEditor = ({ state, draft, onChange }) => {
  if (!state || !draft) return <Spin />;
  const setSection = (section, key, value) => onChange({ ...draft, [section]: { ...draft[section], [key]: value } });
  const selected = new Set(draft.universe?.index_codes?.length
    ? draft.universe.index_codes
    : state.indexes.filter(item => item.default_selected).map(item => item.index_code));
  const toggle = (code, checked) => {
    const next = new Set(selected);
    if (checked) next.add(code); else next.delete(code);
    onChange({ ...draft, universe: { index_codes: state.indexes.filter(item => next.has(item.index_code)).map(item => item.index_code) } });
  };
  const groups = [['宽基', state.indexes.filter(item => item.category === '宽基')],
                  ['行业主题', state.indexes.filter(item => item.category === '行业主题')]];

  return (
    <div className="stock-system__config">
      <h5>信号</h5>
      <div className="stock-system__config-list">
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">贪恐闸门（不高于）</span>
          <InputNumber min={0} max={100} value={draft.signal.fear_threshold}
                       onChange={value => setSection('signal', 'fear_threshold', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">低 N 起算</span>
          <InputNumber min={4} max={30} value={draft.signal.low_count_min}
                       onChange={value => setSection('signal', 'low_count_min', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">买入高 M</span>
          <InputNumber min={1} max={9} value={draft.signal.buy_high_count}
                       onChange={value => setSection('signal', 'buy_high_count', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">布防窗口（交易日，0=同日）</span>
          <InputNumber min={0} max={60} value={draft.signal.arm_window_days}
                       onChange={value => setSection('signal', 'arm_window_days', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">卖出需先出现高 K</span>
          <InputNumber min={4} max={30} value={draft.signal.high_count_min}
                       onChange={value => setSection('signal', 'high_count_min', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">卖出低 L</span>
          <InputNumber min={1} max={9} value={draft.signal.sell_low_count}
                       onChange={value => setSection('signal', 'sell_low_count', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">卖出回撤（ATR 倍数）</span>
          <InputNumber min={0} max={10} step={0.5} value={draft.signal.sell_atr_multiple}
                       onChange={value => setSection('signal', 'sell_atr_multiple', value)} />
        </div>
        <div className="stock-system__config-row stock-system__config-row--wide">
          <span className="stock-system__config-label">卖出口径</span>
          <Select style={{ width: 360 }} value={draft.signal.sell_mode}
                  onChange={value => setSection('signal', 'sell_mode', value)}
                  options={Object.entries(state.sell_modes).map(([value, label]) => ({ value, label }))} />
        </div>
      </div>

      <h5>组合与模拟盘</h5>
      <div className="stock-system__config-list">
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">最大持仓数</span>
          <InputNumber min={1} max={100} value={draft.portfolio.max_positions}
                       onChange={value => setSection('portfolio', 'max_positions', value)} />
        </div>
        <div className="stock-system__config-row stock-system__config-row--wide">
          <span className="stock-system__config-label">信号多于仓位时</span>
          <Select style={{ width: 360 }} value={draft.portfolio.pick_order}
                  onChange={value => setSection('portfolio', 'pick_order', value)}
                  options={Object.entries(state.pick_orders).map(([value, label]) => ({ value, label }))} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">初始资金</span>
          <InputNumber min={10000} step={100000} style={{ width: 180 }} value={draft.paper.initial_capital}
                       onChange={value => setSection('paper', 'initial_capital', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">佣金 %</span>
          <InputNumber min={0} max={1} step={0.01} value={draft.paper.commission_pct}
                       onChange={value => setSection('paper', 'commission_pct', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">印花税 %</span>
          <InputNumber min={0} max={1} step={0.01} value={draft.paper.stamp_tax_pct}
                       onChange={value => setSection('paper', 'stamp_tax_pct', value)} />
        </div>
        <div className="stock-system__config-row">
          <span className="stock-system__config-label">模拟盘自动调仓</span>
          <Checkbox checked={draft.paper.enabled} onChange={event => setSection('paper', 'enabled', event.target.checked)}>
            开启
          </Checkbox>
        </div>
      </div>

      <h5>板块范围（已选 {selected.size} 个）</h5>
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        默认剔除宽基/风格指数——它们的低9其实就是大盘信号、成分太多、回测里单独跑是负收益；
        只保留科创50/100/200 和上证红利。
      </Paragraph>
      {groups.map(([label, items]) => (
        <div key={label} className="stock-system__config-group">
          <Text strong>{label}</Text>
          <div className="stock-system__config-grid">
            {items.map(item => (
              <Checkbox key={item.index_code} checked={selected.has(item.index_code)}
                        onChange={event => toggle(item.index_code, event.target.checked)}>
                {item.name}
              </Checkbox>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
};

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

const SectorNineTurn = () => {
  const [activeTab, setActiveTab] = useState('daily');
  const [refreshToken, setRefreshToken] = useState(0);
  const [asOf, setAsOf] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskStatus, setTaskStatus] = useState('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [configState, setConfigState] = useState(null);
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const loadConfig = useCallback(async () => {
    try {
      const response = await request.get('/api/sector-nine-turn/config');
      setConfigState(response.data);
      setDraft(response.data.config);
    } catch (requestError) {
      setError(errorText(requestError, '加载参数失败'));
    }
  }, []);

  useEffect(() => { loadConfig(); }, [loadConfig]);

  const pollTask = useCallback(async () => {
    try {
      const response = await request.get('/api/sector-nine-turn/task');
      const task = response.data;
      const running = task?.is_running || task?.status === 'running';
      setTaskStatus(running ? `正在计算：${task?.last_message || ''}` : (task?.last_message || ''));
      if (!running) {
        setBusy(false);
        setRefreshToken(token => token + 1);
      }
      return running;
    } catch (requestError) {
      setBusy(false);
      return false;
    }
  }, []);

  useEffect(() => {
    if (!busy) return undefined;
    const timer = setInterval(pollTask, 3000);
    return () => clearInterval(timer);
  }, [busy, pollTask]);

  const runTask = async () => {
    setBusy(true);
    try {
      await request.post('/api/sector-nine-turn/run', asOf ? { as_of: asOf } : {});
      message.success('已提交计算');
      pollTask();
    } catch (requestError) {
      setBusy(false);
      message.error(errorText(requestError, '提交失败'));
    }
  };

  const saveConfig = async (andRun = false) => {
    setSaving(true);
    try {
      const response = await request.put('/api/sector-nine-turn/config', draft);
      setConfigState(response.data);
      setDraft(response.data.config);
      message.success('已保存');
      if (andRun) runTask();
    } catch (requestError) {
      message.error(errorText(requestError, '保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const resetConfig = async () => {
    setSaving(true);
    try {
      const response = await request.post('/api/sector-nine-turn/config/reset');
      setConfigState(response.data);
      setDraft(response.data.config);
      message.success('已恢复默认');
    } catch (requestError) {
      message.error(errorText(requestError, '恢复默认失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="stock-system">
      <div className="stock-system__header">
        <div>
          <Title level={4} style={{ margin: 0 }}>板块九转</Title>
          <Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
            板块自身出现神奇九转低 9 后首次高 2、且当天自算贪恐分数 ≤ 40 时进入布防；布防中的板块里，
            成分股自己也出现低 9 后首次高 2 就买入（默认要求板块与个股同日）。持仓在出现高 9 后首次低 2
            且回撤超过 2 个 ATR 时卖出。九转和 ATR 与个股详情页 K 线图同一套算法，每个交易日 19:20 自动计算。
          </Paragraph>
        </div>
        <Space wrap>
          <Input type="date" value={asOf} onChange={event => setAsOf(event.target.value)}
                 style={{ width: 150 }} placeholder="交易日" allowClear />
          <Button type="primary" icon={<PlayCircleOutlined />} loading={busy} onClick={runTask}>
            {asOf ? '按该日计算' : '立即计算'}
          </Button>
          <Button icon={<SettingOutlined />} onClick={() => setDrawerOpen(true)}>参数配置</Button>
        </Space>
      </div>

      {taskStatus ? <div className="stock-system__task">{taskStatus}</div> : null}
      {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} /> : null}

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: 'daily', label: '每日信号', children: <DailyTab refreshToken={refreshToken} /> },
          { key: 'paper', label: '模拟盘', children: <PaperTab refreshToken={refreshToken} /> },
          { key: 'backtest', label: '回测与消融', children: <BacktestTab /> },
        ]}
      />

      <Drawer
        title="板块九转参数"
        width={680}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        extra={(
          <Space>
            <Popconfirm title="恢复全部默认参数？" onConfirm={resetConfig}>
              <Button loading={saving}>恢复默认</Button>
            </Popconfirm>
            <Button loading={saving} onClick={() => saveConfig(false)}>保存</Button>
            <Button type="primary" loading={saving} onClick={() => saveConfig(true)}>保存并计算</Button>
          </Space>
        )}
      >
        {configState?.config?.updated_at ? (
          <Paragraph type="secondary">
            上次保存：{configState.config.updated_at.replace('T', ' ').slice(0, 19)}
            {configState.config.updated_by ? `（${configState.config.updated_by}）` : ''}。保存后下一次计算生效。
          </Paragraph>
        ) : null}
        <ConfigEditor state={configState} draft={draft} onChange={setDraft} />
      </Drawer>
    </div>
  );
};

export default SectorNineTurn;
