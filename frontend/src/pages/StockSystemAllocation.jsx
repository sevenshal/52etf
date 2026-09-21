import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  Empty,
  Segmented,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';

const { Text, Paragraph } = Typography;

// A 股习惯：红色偏多、绿色偏空
const STATE_COLORS = { offense: 'red', neutral: 'default', defense: 'green' };
const STATE_LABELS = { offense: '进攻', neutral: '中性', defense: '防守' };
const STATUS_OPTIONS = [
  { label: '目标持仓', value: 'target' },
  { label: '未分配', value: 'other' },
  { label: '全部', value: 'all' },
];

const isNumber = value => typeof value === 'number' && Number.isFinite(value);
const formatPct = (value, digits = 1) => (isNumber(value) ? `${value.toFixed(digits)}%` : '-');

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

export const StateTag = ({ state, overheated, stale }) => (
  <Space size={2}>
    <Tag color={STATE_COLORS[state] || 'default'}>{STATE_LABELS[state] || state || '-'}</Tag>
    {overheated ? <Tag color="orange">过热</Tag> : null}
    {stale ? <Tag>数据过期</Tag> : null}
  </Space>
);

const SignalText = ({ label, date, days }) => {
  if (!label) return <Text type="secondary">暂无信号</Text>;
  return (
    <span>
      {label}
      <Text type="secondary"> {date}{isNumber(days) ? `（${days} 个交易日前）` : ''}</Text>
    </span>
  );
};

const regimeColumns = [
  {
    title: '板块',
    dataIndex: 'name',
    width: 130,
    render: (name, row) => (
      <Tooltip title={row.note || row.index_code}>
        <span>{name}</span>
      </Tooltip>
    ),
  },
  {
    title: '状态',
    key: 'state',
    width: 130,
    render: (_, row) => <StateTag state={row.state} overheated={row.overheated} stale={row.stale} />,
  },
  {
    title: '贪恐',
    dataIndex: 'score',
    width: 70,
    sorter: (a, b) => (a.score ?? -1) - (b.score ?? -1),
    render: value => (isNumber(value) ? value.toFixed(1) : '-'),
  },
  {
    title: '最近信号',
    key: 'signal',
    render: (_, row) => <SignalText label={row.signal_label} date={row.signal_date} days={row.days_since_signal} />,
  },
  { title: '板块上限', dataIndex: 'cap_pct', width: 90, render: value => formatPct(value, 0) },
  {
    title: '入池',
    dataIndex: 'pool_members',
    width: 70,
    sorter: (a, b) => (a.pool_members || 0) - (b.pool_members || 0),
  },
  {
    title: '已配',
    dataIndex: 'allocated_pct',
    width: 80,
    sorter: (a, b) => (a.allocated_pct || 0) - (b.allocated_pct || 0),
    render: value => (value ? formatPct(value) : '-'),
  },
];

const StockSystemAllocation = ({ refreshToken, busy, onRunAllocation }) => {
  const [data, setData] = useState({ run: null, regimes: [], rows: [] });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [statusFilter, setStatusFilter] = useState('target');
  // 分页必须受控：只传 pageSize 常量而不接 onChange 时，antd 会把切换器的改动丢掉（点了没反应）
  const [tablePage, setTablePage] = useState({ current: 1, pageSize: 50 });

  // 状态筛选变了回到第 1 页，否则停在后几页时切筛选会看到空表
  useEffect(() => {
    setTablePage(prev => ({ ...prev, current: 1 }));
  }, [statusFilter]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { data: payload } = await request.get('/api/stock-system/allocation');
      setData({ run: payload?.run || null, regimes: payload?.regimes || [], rows: payload?.rows || [] });
    } catch (loadError) {
      setError(formatErrorMessage(loadError, '情绪择时与仓位加载失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshToken]);

  const { run, regimes, rows } = data;
  const summary = run?.summary || {};
  const market = summary.market || {};
  const sectorRegimes = useMemo(
    () => regimes
      .filter(row => row.category === 'sector' || row.category === 'unmapped')
      .sort((a, b) => (
        (b.allocated_pct || 0) - (a.allocated_pct || 0)
        || (b.pool_members || 0) - (a.pool_members || 0)
        || (b.score ?? -1) - (a.score ?? -1)
      )),
    [regimes],
  );
  const broadRegimes = useMemo(() => regimes.filter(row => row.category === 'broad'), [regimes]);
  const visibleRows = useMemo(() => {
    if (statusFilter === 'target') return rows.filter(row => row.status === 'target');
    if (statusFilter === 'other') return rows.filter(row => row.status !== 'target');
    return rows;
  }, [rows, statusFilter]);

  const allocationColumns = [
    { title: '排名', dataIndex: 'pool_rank', width: 64 },
    {
      title: '股票',
      key: 'stock',
      width: 150,
      render: (_, row) => (
        <div>
          <StockDetailLink symbol={row.ts_code}>{row.name || row.ts_code}</StockDetailLink>
          <div className="stock-system__code">{row.ts_code} · {row.industry || '-'}</div>
        </div>
      ),
    },
    {
      title: '所属板块',
      key: 'sector',
      width: 200,
      render: (_, row) => (
        <Space size={4}>
          <span>{row.sector_name || '-'}</span>
          <StateTag state={row.sector_state} overheated={row.sector_overheated} />
        </Space>
      ),
    },
    {
      title: '综合分',
      dataIndex: 'composite_score',
      width: 80,
      render: value => (isNumber(value) ? value.toFixed(1) : '-'),
    },
    {
      title: '目标仓位',
      dataIndex: 'target_weight_pct',
      width: 90,
      render: value => (isNumber(value) ? <Text strong>{formatPct(value, 2)}</Text> : '-'),
    },
    { title: '说明', dataIndex: 'reason', render: value => value || <Text type="secondary">按规则分配</Text> },
  ];

  return (
    <Spin spinning={loading}>
      <div className="stock-system__allocation">
        <div className="stock-system__toolbar">
          <Paragraph type="secondary" style={{ margin: 0, flex: 1 }}>
            市场状态定总仓位，板块状态定板块上限：最后一个贪恐信号是底 → 进攻，是顶 → 防守（默认上限 0、不开新仓，
            已有持仓交给技术层收紧止损），没有信号 → 中性；分数过高视为过热、一律不追新仓。信号与贪恐历史曲线上的顶/底标记同源。
          </Paragraph>
          <Space>
            <Button icon={<ThunderboltOutlined />} loading={busy} onClick={onRunAllocation}>只重算择时与仓位</Button>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          </Space>
        </div>

        {error ? <Alert type="error" showIcon message={error} /> : null}

        {!run && !loading ? (
          <Empty description="还没有情绪择时与仓位结果，先计算一次股票池（会自动接着算仓位）" />
        ) : null}

        {run ? (
          <>
            <div className="stock-system__stats">
              <Statistic
                title={`市场（${market.name || run.market_index}）`}
                valueRender={() => <StateTag state={market.state} overheated={market.overheated} stale={market.stale} />}
              />
              <Statistic title="市场贪恐" value={isNumber(market.score) ? market.score.toFixed(1) : '-'} suffix={market.score_date ? <Text type="secondary" style={{ fontSize: 12 }}>{market.score_date}</Text> : null} />
              <Statistic title="总仓位上限" value={formatPct(summary.exposure_pct, 0)} />
              <Statistic title="目标持仓" value={summary.positions ?? '-'} suffix="只" />
              <Statistic title="已配置" value={formatPct(summary.invested_pct)} />
              <Statistic title="现金" value={formatPct(summary.cash_pct)} />
            </div>
            <div className="stock-system__reasons">
              <span>市场最近信号：<SignalText label={market.signal_label} date={market.signal_date} days={market.days_since_signal} /></span>
              {market.note ? <Text type="secondary">{market.note}</Text> : null}
              <Text type="secondary">
                交易日 {run.trade_date} · 板块进攻 {summary.sector_states?.offense || 0} / 中性 {summary.sector_states?.neutral || 0} / 防守 {summary.sector_states?.defense || 0}
                {summary.sectors_overheated ? ` · 过热 ${summary.sectors_overheated}` : ''}
                {' · '}单只基准 {formatPct(summary.base_weight_pct, 2)}
              </Text>
            </div>

            <div className="stock-system__toolbar">
              <Segmented options={STATUS_OPTIONS} value={statusFilter} onChange={setStatusFilter} />
              <Text type="secondary">入池 {summary.pool_size ?? rows.length} 只 · 未开新仓 {summary.blocked ?? 0} · 额度不足 {summary.skipped ?? 0}</Text>
            </div>
            <Table
              rowKey="ts_code"
              size="small"
              dataSource={visibleRows}
              columns={allocationColumns}
              scroll={{ x: 900 }}
              pagination={{
                current: tablePage.current,
                pageSize: tablePage.pageSize,
                showSizeChanger: true,
                // 改每页条数时回到第 1 页，避免当前页码超出新的总页数
                onChange: (current, pageSize) => setTablePage(prev => ({
                  current: pageSize !== prev.pageSize ? 1 : current,
                  pageSize,
                })),
              }}
            />

            <Table
              rowKey="index_code"
              size="small"
              title={() => <Text strong>板块状态</Text>}
              dataSource={sectorRegimes}
              columns={regimeColumns}
              scroll={{ x: 800 }}
              pagination={false}
            />
            <Collapse
              ghost
              items={[{
                key: 'broad',
                label: `宽基 / 风格指数（${broadRegimes.length}，仅参考，不参与板块归属）`,
                children: (
                  <Table
                    rowKey="index_code"
                    size="small"
                    dataSource={broadRegimes}
                    columns={regimeColumns.filter(column => !['cap_pct', 'pool_members', 'allocated_pct'].includes(column.dataIndex))}
                    pagination={false}
                  />
                ),
              }]}
            />
          </>
        ) : null}
      </div>
    </Spin>
  );
};

export default StockSystemAllocation;
