import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Popconfirm,
  Segmented,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';
import { StateTag } from './StockSystemAllocation';

const { Text, Paragraph } = Typography;

const ACTION_META = {
  buy: { label: '买入', color: 'red' },
  no_room: { label: '额度不足', color: 'gold' },
  filtered: { label: '被过滤', color: 'orange' },
  watch: { label: '无触发', color: 'default' },
  sell: { label: '卖出', color: 'green' },
  hold: { label: '持有', color: 'blue' },
};
const XUEQIU_META = {
  pass: { label: '雪球通过', color: 'red' },
  fail: { label: '雪球未过', color: 'orange' },
  missing: { label: '雪球无数据', color: 'default' },
  unavailable: { label: '雪球跳过', color: 'default' },
  disabled: { label: '雪球过滤关', color: 'default' },
};
const ORDER_STATUS = {
  pending: { label: '待成交', color: 'processing' },
  filled: { label: '已成交', color: 'green' },
  cancelled: { label: '已撤单', color: 'default' },
};
const SIGNAL_VIEWS = [
  { label: '买入信号', value: 'buy' },
  { label: '被过滤', value: 'filtered' },
  { label: '无触发', value: 'watch' },
  { label: '持仓出场', value: 'holding' },
];

const isNumber = value => typeof value === 'number' && Number.isFinite(value);
const pct = (value, digits = 1) => (isNumber(value) ? `${value.toFixed(digits)}%` : '-');
const money = value => (isNumber(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '-');
const price = value => (isNumber(value) ? value.toFixed(2) : '-');

const SignedPct = ({ value }) => {
  if (!isNumber(value)) return <span>-</span>;
  const className = value > 0 ? 'stock-system__up' : value < 0 ? 'stock-system__down' : '';
  return <span className={className}>{`${value > 0 ? '+' : ''}${value.toFixed(2)}%`}</span>;
};

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const StockCell = ({ row }) => (
  <div>
    <StockDetailLink symbol={row.ts_code}>{row.name || row.ts_code}</StockDetailLink>
    <div className="stock-system__code">{row.ts_code}</div>
  </div>
);

const DetailTags = ({ items, color }) => (
  <Space size={[2, 2]} wrap>
    {(items || []).map(item => (
      <Tooltip key={item.key} title={item.detail}>
        <Tag color={color}>{item.label}</Tag>
      </Tooltip>
    ))}
  </Space>
);

const NavChart = ({ navs, initialCapital }) => {
  const option = useMemo(() => ({
    grid: { left: 60, right: 20, top: 20, bottom: 30 },
    tooltip: { trigger: 'axis', valueFormatter: value => money(value) },
    xAxis: { type: 'category', data: navs.map(point => point.trade_date) },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: value => `${(value / 10000).toFixed(0)}万` } },
    series: [
      { name: '净值', type: 'line', showSymbol: navs.length < 30, data: navs.map(point => point.nav), lineStyle: { width: 2 } },
      {
        name: '初始资金', type: 'line', symbol: 'none', data: navs.map(() => initialCapital),
        lineStyle: { type: 'dashed', width: 1, color: '#bfbfbf' },
      },
    ],
  }), [navs, initialCapital]);
  if (!navs.length) return null;
  return <ReactECharts option={option} style={{ height: 220 }} notMerge />;
};

const StockSystemTrading = ({ refreshToken }) => {
  const [signals, setSignals] = useState({ trade_date: null, rows: [] });
  const [paperData, setPaperData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [view, setView] = useState('buy');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [signalResponse, paperResponse] = await Promise.all([
        request.get('/api/stock-system/signals'),
        request.get('/api/stock-system/paper'),
      ]);
      setSignals({ trade_date: signalResponse.data?.trade_date, rows: signalResponse.data?.rows || [] });
      setPaperData(paperResponse.data || null);
    } catch (loadError) {
      setError(formatErrorMessage(loadError, '技术信号与模拟盘加载失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshToken]);

  const resetPaper = useCallback(async () => {
    try {
      const { data } = await request.post('/api/stock-system/paper/reset', {});
      setPaperData(data);
      message.success('模拟盘已重置，下一次计算从当天开始');
    } catch (resetError) {
      message.error(formatErrorMessage(resetError, '重置失败'));
    }
  }, []);

  const rows = signals.rows;
  const counts = useMemo(() => ({
    buy: rows.filter(row => row.role === 'candidate' && ['buy', 'no_room'].includes(row.action)).length,
    filtered: rows.filter(row => row.action === 'filtered').length,
    watch: rows.filter(row => row.action === 'watch').length,
    holding: rows.filter(row => row.role === 'holding').length,
  }), [rows]);
  const visibleRows = useMemo(() => {
    if (view === 'buy') return rows.filter(row => row.role === 'candidate' && ['buy', 'no_room'].includes(row.action));
    if (view === 'holding') return rows.filter(row => row.role === 'holding');
    return rows.filter(row => row.action === view);
  }, [rows, view]);

  const candidateColumns = [
    { title: '排名', dataIndex: 'pool_rank', width: 60 },
    { title: '股票', key: 'stock', width: 130, render: (_, row) => <StockCell row={row} /> },
    {
      title: '板块',
      key: 'sector',
      width: 170,
      render: (_, row) => (
        <Space size={4}><span>{row.sector_name || '-'}</span><StateTag state={row.sector_state} /></Space>
      ),
    },
    {
      title: '操作',
      dataIndex: 'action',
      width: 90,
      render: action => <Tag color={ACTION_META[action]?.color}>{ACTION_META[action]?.label || action}</Tag>,
    },
    { title: '触发', dataIndex: 'triggers', render: triggers => <DetailTags items={triggers} color="volcano" /> },
    {
      title: '雪球',
      key: 'xueqiu',
      width: 130,
      render: (_, row) => {
        const meta = XUEQIU_META[row.xueqiu_status];
        if (!meta) return '-';
        return (
          <Tooltip title={row.xueqiu_detail}>
            <Tag color={meta.color}>{meta.label}{isNumber(row.xueqiu_ratio) ? ` ${row.xueqiu_ratio.toFixed(2)}` : ''}</Tag>
          </Tooltip>
        );
      },
    },
    { title: '收盘', dataIndex: 'close', width: 70, render: price },
    {
      title: '止损',
      key: 'stop',
      width: 110,
      render: (_, row) => (isNumber(row.stop_price) ? `${price(row.stop_price)}（-${pct(row.stop_pct)}）` : '-'),
    },
    { title: '计划仓位', dataIndex: 'planned_weight_pct', width: 80, render: value => pct(value, 2) },
    { title: '说明', dataIndex: 'note', render: value => value || '' },
  ];
  const holdingColumns = [
    { title: '股票', key: 'stock', width: 130, render: (_, row) => <StockCell row={row} /> },
    {
      title: '板块',
      key: 'sector',
      width: 170,
      render: (_, row) => (
        <Space size={4}><span>{row.sector_name || '-'}</span><StateTag state={row.sector_state} /></Space>
      ),
    },
    { title: '排名', dataIndex: 'pool_rank', width: 60, render: value => value ?? '-' },
    { title: '收盘', dataIndex: 'close', width: 70, render: price },
    {
      title: '操作',
      dataIndex: 'action',
      width: 80,
      render: action => <Tag color={ACTION_META[action]?.color}>{ACTION_META[action]?.label || action}</Tag>,
    },
    { title: '出场理由', dataIndex: 'exits', render: exits => <DetailTags items={exits} color="green" /> },
    { title: '说明', dataIndex: 'note', render: value => value || '' },
  ];

  const positionColumns = [
    { title: '股票', key: 'stock', width: 130, render: (_, row) => <StockCell row={row} /> },
    { title: '板块', dataIndex: 'sector_name', width: 100 },
    { title: '股数', dataIndex: 'quantity', width: 80 },
    { title: '买入日', dataIndex: 'entry_date', width: 100 },
    { title: '买入价', dataIndex: 'entry_price', width: 80, render: price },
    { title: '现价', dataIndex: 'last_price', width: 80, render: price },
    { title: '收益', dataIndex: 'return_pct', width: 90, render: value => <SignedPct value={value} /> },
    { title: '市值', dataIndex: 'market_value', width: 100, render: money },
    { title: '权重', dataIndex: 'weight_pct', width: 70, render: value => pct(value) },
    { title: '止损线', dataIndex: 'stop_pct', width: 80, render: value => (isNumber(value) ? `-${pct(value)}` : '-') },
    { title: '买入理由', dataIndex: 'entry_reason', render: value => <Text type="secondary">{value}</Text> },
  ];
  const orderColumns = [
    { title: '信号日', dataIndex: 'signal_date', width: 100 },
    { title: '股票', key: 'stock', width: 130, render: (_, row) => <StockCell row={row} /> },
    {
      title: '方向',
      dataIndex: 'side',
      width: 60,
      render: side => <Tag color={side === 'buy' ? 'red' : 'green'}>{side === 'buy' ? '买' : '卖'}</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: status => <Tag color={ORDER_STATUS[status]?.color}>{ORDER_STATUS[status]?.label || status}</Tag>,
    },
    { title: '成交日', dataIndex: 'exec_date', width: 100 },
    { title: '成交价', dataIndex: 'fill_price', width: 80, render: price },
    { title: '股数', dataIndex: 'quantity', width: 80 },
    { title: '金额', dataIndex: 'amount', width: 100, render: money },
    { title: '盈亏', dataIndex: 'realized_pnl', width: 90, render: value => (isNumber(value) ? money(value) : '') },
    {
      title: '原因 / 说明',
      key: 'reason',
      render: (_, row) => (
        <span>
          <Text type="secondary">{row.reason}</Text>
          {row.message ? <Text type="warning">{row.reason ? '；' : ''}{row.message}</Text> : null}
        </span>
      ),
    },
  ];

  const account = paperData?.account;
  const stats = paperData?.stats || {};

  return (
    <Spin spinning={loading}>
      <div className="stock-system__allocation">
        <div className="stock-system__toolbar">
          <Paragraph type="secondary" style={{ margin: 0, flex: 1 }}>
            只对第二层分到目标仓位的股票判定入场：任一触发（九转低位反转 / 回踩支撑企稳 / MACD 金叉 / 放量突破）且通过
            雪球过滤（5 日权价比与持有组合数，数据覆盖不到时自动跳过）。模拟盘持仓按止损、买入后红点回撤的移动止损、
            基本面和股票池排名判定出场。信号日收盘出单，下一交易日开盘撮合；指标与 K 线图同一套算法。
          </Paragraph>
          <Space>
            <Popconfirm title="清空模拟盘（持仓、订单、净值）并用配置里的初始资金重建？" onConfirm={resetPaper}>
              <Button danger>重置模拟盘</Button>
            </Popconfirm>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          </Space>
        </div>

        {error ? <Alert type="error" showIcon message={error} /> : null}

        {account ? (
          <>
            <div className="stock-system__stats">
              <Statistic title="净值" value={money(account.nav)} />
              <Statistic title="总收益" valueRender={() => <SignedPct value={stats.total_return_pct} />} />
              <Statistic title="最大回撤" value={pct(stats.max_drawdown_pct, 2)} />
              <Statistic title="仓位" value={pct(stats.invested_pct)} />
              <Statistic title="持仓" value={stats.position_count ?? 0} suffix="只" />
              <Statistic title="待成交" value={stats.pending_orders ?? 0} suffix="笔" />
            </div>
            <Text type="secondary">
              初始资金 {money(account.initial_capital)} · 现金 {money(account.cash)}
              {account.started_on ? ` · ${account.started_on} 开始` : ' · 下一次计算开始'}
              {account.last_trade_date ? ` · 已结算到 ${account.last_trade_date}` : ''}
            </Text>
            <NavChart navs={paperData.navs || []} initialCapital={account.initial_capital} />
          </>
        ) : (
          <Empty description="还没有模拟盘，下一次计算时自动按配置的初始资金开户" />
        )}

        <div className="stock-system__toolbar">
          <Space>
            <Segmented
              value={view}
              onChange={setView}
              options={SIGNAL_VIEWS.map(option => ({ ...option, label: `${option.label} ${counts[option.value] ?? 0}` }))}
            />
          </Space>
          <Text type="secondary">{signals.trade_date ? `信号日 ${signals.trade_date}` : '还没有信号'}</Text>
        </div>
        <Table
          rowKey="ts_code"
          size="small"
          dataSource={visibleRows}
          columns={view === 'holding' ? holdingColumns : candidateColumns}
          scroll={{ x: 1100 }}
          pagination={{ pageSize: 50, hideOnSinglePage: true }}
        />

        <Table
          rowKey="ts_code"
          size="small"
          title={() => <Text strong>模拟盘持仓</Text>}
          dataSource={paperData?.positions || []}
          columns={positionColumns}
          scroll={{ x: 1100 }}
          pagination={false}
        />
        <Table
          rowKey="id"
          size="small"
          title={() => <Text strong>订单（最近 200 笔）</Text>}
          dataSource={paperData?.orders || []}
          columns={orderColumns}
          scroll={{ x: 1100 }}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
        />
      </div>
    </Spin>
  );
};

export default StockSystemTrading;
