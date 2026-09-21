import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Segmented,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { PlayCircleOutlined, ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';

const { Text, Title, Paragraph } = Typography;

const STATUS_META = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  completed: { label: '完成', color: 'green' },
  failed: { label: '失败', color: 'red' },
  cancelled: { label: '已取消', color: 'default' },
};
const ACTIVE = ['queued', 'running'];
const POLL_MS = 5000;
const SERIES_COLORS = {
  pool_hold: '#8c8c8c',
  sentiment: '#1677ff',
  technical: '#fa8c16',
  full: '#cf1322',
  benchmark: '#52c41a',
};

const isNumber = value => typeof value === 'number' && Number.isFinite(value);
const pct = (value, digits = 1) => (isNumber(value) ? `${value.toFixed(digits)}%` : '-');
const num = (value, digits = 2) => (isNumber(value) ? value.toFixed(digits) : '-');

const Signed = ({ value, digits = 1 }) => {
  if (!isNumber(value)) return <span>-</span>;
  const className = value > 0 ? 'stock-system__up' : value < 0 ? 'stock-system__down' : '';
  return <span className={className}>{`${value > 0 ? '+' : ''}${value.toFixed(digits)}%`}</span>;
};

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const NavChart = ({ navs, labels }) => {
  const keys = Object.keys(navs || {});
  const option = useMemo(() => {
    const dates = (navs?.benchmark || navs?.[keys[0]] || []).map(point => point.trade_date);
    return {
      grid: { left: 60, right: 20, top: 40, bottom: 40 },
      legend: { top: 0 },
      tooltip: { trigger: 'axis', valueFormatter: value => (isNumber(value) ? value.toFixed(3) : '-') },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 8 }],
      series: keys.map(key => {
        const points = navs[key] || [];
        const base = points.length ? points[0].nav : 1;
        return {
          name: labels[key] || key,
          type: 'line',
          showSymbol: false,
          data: points.map(point => point.nav / base),
          lineStyle: { width: key === 'full' ? 2.5 : 1.5, type: key === 'benchmark' ? 'dashed' : 'solid' },
          itemStyle: { color: SERIES_COLORS[key] },
        };
      }),
    };
  }, [navs, keys, labels]);
  if (!keys.length) return null;
  return <ReactECharts option={option} style={{ height: 320 }} notMerge />;
};

const PoolScoreTable = ({ analysis }) => {
  const rows = Object.entries(analysis || {})
    .filter(([, value]) => value && value.dates)
    .map(([horizon, value]) => ({ horizon, ...value }));
  if (!rows.length) return <Text type="secondary">调仓日太少或前瞻窗口不够，没有分组检验结果</Text>;
  return (
    <Table
      rowKey="horizon"
      size="small"
      pagination={false}
      dataSource={rows}
      columns={[
        { title: '前瞻', dataIndex: 'horizon', render: value => `${value} 个交易日` },
        { title: '调仓日数', dataIndex: 'dates' },
        ...[0, 1, 2, 3, 4].map(index => ({
          title: `第${index + 1}组${index === 0 ? '（低分）' : index === 4 ? '（高分）' : ''}`,
          key: `q${index}`,
          render: (_, row) => <Signed value={row.quintile_mean_pct?.[index]} digits={2} />,
        })),
        { title: '高-低', dataIndex: 'spread_mean_pct', render: value => <Signed value={value} digits={2} /> },
        { title: '高>低占比', dataIndex: 'spread_positive_pct', render: value => pct(value) },
        {
          title: <Tooltip title="每个调仓日综合分与之后收益的 Spearman 秩相关的均值，t = 均值 / 标准差 × √期数">IC（t）</Tooltip>,
          key: 'ic',
          render: (_, row) => `${num(row.ic_mean, 3)}（${num(row.ic_t, 2)}）`,
        },
        { title: '入池相对全体', dataIndex: 'pool_excess_mean_pct', render: value => <Signed value={value} digits={2} /> },
      ]}
    />
  );
};

const StockSystemBacktest = () => {
  const [runs, setRuns] = useState([]);
  const [variants, setVariants] = useState([]);
  const [form, setForm] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [tradeVariant, setTradeVariant] = useState('full');

  const loadRuns = useCallback(async () => {
    try {
      const { data } = await request.get('/api/stock-system/backtests');
      setRuns(data.runs || []);
      setVariants(data.variants || []);
      setForm(previous => previous || {
        ...data.defaults,
        variants: (data.variants || []).map(variant => variant.key),
      });
      return data.runs || [];
    } catch (error) {
      message.error(formatErrorMessage(error, '回测列表加载失败'));
      return [];
    }
  }, []);

  const loadDetail = useCallback(async (runId) => {
    if (!runId) return;
    setLoading(true);
    try {
      const { data } = await request.get(`/api/stock-system/backtests/${runId}`);
      setDetail(data);
    } catch (error) {
      message.error(formatErrorMessage(error, '回测结果加载失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRuns().then(list => {
      const first = list.find(run => run.status === 'completed') || list[0];
      if (first) setSelectedId(first.id);
    });
  }, [loadRuns]);

  useEffect(() => {
    loadDetail(selectedId);
  }, [loadDetail, selectedId]);

  const hasActive = runs.some(run => ACTIVE.includes(run.status));
  useEffect(() => {
    if (!hasActive) return undefined;
    const timer = setInterval(async () => {
      const list = await loadRuns();
      const selected = list.find(run => run.id === selectedId);
      if (selected && !ACTIVE.includes(selected.status)) loadDetail(selectedId);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [hasActive, loadRuns, loadDetail, selectedId]);

  const submit = useCallback(async () => {
    setSubmitting(true);
    try {
      const { data } = await request.post('/api/stock-system/backtests', form);
      message.success('回测已开始，进度会自动刷新');
      await loadRuns();
      setSelectedId(data.id);
    } catch (error) {
      message.error(formatErrorMessage(error, '启动回测失败'));
    } finally {
      setSubmitting(false);
    }
  }, [form, loadRuns]);

  const cancel = useCallback(async (runId) => {
    try {
      await request.post(`/api/stock-system/backtests/${runId}/cancel`);
      loadRuns();
    } catch (error) {
      message.error(formatErrorMessage(error, '取消失败'));
    }
  }, [loadRuns]);

  const remove = useCallback(async (runId) => {
    try {
      await request.delete(`/api/stock-system/backtests/${runId}`);
      if (runId === selectedId) {
        setSelectedId(null);
        setDetail(null);
      }
      loadRuns();
    } catch (error) {
      message.error(formatErrorMessage(error, '删除失败'));
    }
  }, [loadRuns, selectedId]);

  const labels = useMemo(() => ({
    ...Object.fromEntries(variants.map(variant => [variant.key, variant.label])),
    benchmark: '中证全指',
  }), [variants]);

  const summary = detail?.summary;
  const metricRows = summary ? [
    ...Object.entries(summary.variants || {}).map(([key, value]) => ({ key, ...value })),
    { key: 'benchmark', ...summary.benchmark },
  ] : [];
  const yearly = summary?.yearly || {};
  const years = [...new Set(Object.values(yearly).flatMap(item => Object.keys(item || {})))].sort();

  const runColumns = [
    { title: '#', dataIndex: 'id', width: 50 },
    {
      title: '状态',
      key: 'status',
      width: 170,
      render: (_, run) => (ACTIVE.includes(run.status)
        ? <Tooltip title={run.message}><Progress percent={Math.round(run.progress || 0)} size="small" /></Tooltip>
        : <Tooltip title={run.message}><Tag color={STATUS_META[run.status]?.color}>{STATUS_META[run.status]?.label || run.status}</Tag></Tooltip>),
    },
    { title: '区间', key: 'range', render: (_, run) => `${run.params?.start_date} ~ ${run.params?.end_date}` },
    { title: '调仓', key: 'frequency', width: 60, render: (_, run) => (run.params?.frequency === 'weekly' ? '每周' : '每月') },
    { title: '创建', dataIndex: 'created_at', width: 150, render: value => value?.replace('T', ' ').slice(0, 16) },
    {
      title: '操作',
      key: 'actions',
      width: 160,
      render: (_, run) => (
        <Space size={4}>
          <Button size="small" type={run.id === selectedId ? 'primary' : 'default'} onClick={() => setSelectedId(run.id)}>查看</Button>
          {ACTIVE.includes(run.status) ? (
            <Button size="small" onClick={() => cancel(run.id)} disabled={run.cancel_requested}>取消</Button>
          ) : (
            <Popconfirm title="删除这次回测？" onConfirm={() => remove(run.id)}>
              <Button size="small" danger>删除</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div className="stock-system__allocation">
      <Paragraph type="secondary" style={{ margin: 0 }}>
        按历史交易日回放三层系统（与实盘同一批函数、同一套撮合），对比「股票池等权持有 → +情绪择时与仓位 → +技术面进出 →
        完整系统」每一层的贡献，并检验第一层综合分分组后的前瞻收益。回测在独立进程里跑，开始时从生产分析库复制所需数据到工作区
        （约一两分钟）；第一次按某套第一层参数回测时要逐个调仓日计算历史股票池（每个约一分钟），之后同样的参数直接用缓存。
        使用提交时的当前配置。
      </Paragraph>

      {form ? (
        <div className="stock-system__backtest-form">
          <Space wrap>
            <Input type="date" value={form.start_date} onChange={e => setForm({ ...form, start_date: e.target.value })} style={{ width: 150 }} />
            <Text type="secondary">至</Text>
            <Input type="date" value={form.end_date} onChange={e => setForm({ ...form, end_date: e.target.value })} style={{ width: 150 }} />
            <Segmented
              value={form.frequency}
              onChange={value => setForm({ ...form, frequency: value })}
              options={[{ label: '每月调仓', value: 'monthly' }, { label: '每周调仓', value: 'weekly' }]}
            />
            <InputNumber
              value={form.initial_capital}
              min={10000}
              step={100000}
              addonBefore="初始资金"
              style={{ width: 220 }}
              onChange={value => setForm({ ...form, initial_capital: value })}
            />
          </Space>
          <Checkbox.Group
            value={form.variants}
            onChange={value => setForm({ ...form, variants: value })}
            options={variants.map(variant => ({ value: variant.key, label: <Tooltip title={variant.description}>{variant.label}</Tooltip> }))}
          />
          <Space>
            <Button type="primary" icon={<PlayCircleOutlined />} loading={submitting} disabled={hasActive} onClick={submit}>
              开始回测
            </Button>
            <Button icon={<ReloadOutlined />} onClick={loadRuns}>刷新</Button>
            {hasActive ? <Text type="secondary">同一时间只能跑一个回测</Text> : null}
          </Space>
        </div>
      ) : null}

      <Table rowKey="id" size="small" dataSource={runs} columns={runColumns} pagination={{ defaultPageSize: 5, hideOnSinglePage: true }} />

      <Spin spinning={loading}>
        {!detail ? <Empty description="还没有回测结果" /> : null}
        {detail && detail.status !== 'completed' ? (
          <Alert
            type={detail.status === 'failed' ? 'error' : 'info'}
            showIcon
            message={`回测 #${detail.id}：${STATUS_META[detail.status]?.label || detail.status}`}
            description={<pre className="stock-system__pre">{detail.message}</pre>}
          />
        ) : null}
        {summary ? (
          <div className="stock-system__allocation">
            <Title level={5} style={{ margin: 0 }}>
              回测 #{detail.id} · {detail.params?.start_date} ~ {detail.params?.end_date} · {summary.trading_days} 个交易日 ·
              {' '}{summary.rebalance_days?.length} 个调仓日 · 耗时 {Math.round(summary.duration_seconds || 0)}s
            </Title>
            {(summary.notes || []).map(note => <Alert key={note} type="warning" showIcon message={note} />)}
            <Table
              rowKey="key"
              size="small"
              pagination={false}
              scroll={{ x: 1100 }}
              dataSource={metricRows}
              columns={[
                { title: '方案', dataIndex: 'label', fixed: 'left', width: 150 },
                { title: '总收益', dataIndex: 'total_return_pct', render: value => <Signed value={value} /> },
                { title: '年化', dataIndex: 'cagr_pct', render: value => <Signed value={value} /> },
                { title: '最大回撤', dataIndex: 'max_drawdown_pct', render: value => pct(value) },
                { title: '夏普', dataIndex: 'sharpe', render: value => num(value) },
                { title: '卡玛', dataIndex: 'calmar', render: value => num(value) },
                { title: '波动', dataIndex: 'volatility_pct', render: value => pct(value) },
                { title: '平均仓位', dataIndex: 'avg_exposure_pct', render: value => pct(value) },
                { title: '平仓笔数', dataIndex: 'trades', render: value => value ?? '-' },
                { title: '胜率', dataIndex: 'win_rate_pct', render: value => pct(value) },
                { title: '平均单笔', dataIndex: 'avg_return_pct', render: value => <Signed value={value} digits={2} /> },
                { title: '盈亏比', dataIndex: 'profit_factor', render: value => num(value) },
                { title: '平均持有(天)', dataIndex: 'avg_holding_days', render: value => num(value, 1) },
              ]}
            />
            <NavChart navs={detail.navs} labels={labels} />
            {years.length ? (
              <Table
                rowKey="key"
                size="small"
                pagination={false}
                title={() => <Text strong>分年度收益</Text>}
                dataSource={Object.entries(yearly).map(([key, value]) => ({ key, label: labels[key] || key, ...value }))}
                columns={[
                  { title: '方案', dataIndex: 'label', width: 150 },
                  ...years.map(year => ({ title: year, dataIndex: year, render: value => <Signed value={value} /> })),
                ]}
              />
            ) : null}
            <Text strong>第一层验收：综合分五等分后的平均前瞻收益</Text>
            <PoolScoreTable analysis={summary.pool_score} />
            <Space>
              <Text strong>平仓记录（最近 300 笔）</Text>
              <Segmented
                value={tradeVariant}
                onChange={setTradeVariant}
                options={Object.keys(detail.trades || {}).map(key => ({ value: key, label: labels[key] || key }))}
              />
            </Space>
            <Table
              rowKey={(row, index) => `${row.ts_code}-${row.exit_date}-${index}`}
              size="small"
              dataSource={(detail.trades || {})[tradeVariant] || []}
              pagination={{ defaultPageSize: 20, hideOnSinglePage: true }}
              scroll={{ x: 900 }}
              columns={[
                {
                  title: '股票',
                  key: 'stock',
                  render: (_, row) => <StockDetailLink symbol={row.ts_code}>{row.name || row.ts_code}</StockDetailLink>,
                },
                { title: '买入', dataIndex: 'entry_date' },
                { title: '卖出', dataIndex: 'exit_date' },
                { title: '持有(天)', dataIndex: 'holding_days' },
                { title: '收益', dataIndex: 'return_pct', render: value => <Signed value={value} digits={2} /> },
                { title: '盈亏', dataIndex: 'pnl', render: value => (isNumber(value) ? value.toFixed(0) : '-') },
                { title: '卖出原因', dataIndex: 'reason', render: value => <Text type="secondary">{value}</Text> },
              ]}
            />
          </div>
        ) : null}
      </Spin>
    </div>
  );
};

export default StockSystemBacktest;
