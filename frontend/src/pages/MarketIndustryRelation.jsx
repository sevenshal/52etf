import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Row, Segmented, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';
import StockInlinePanel from '../components/StockInlinePanel';
import './Market.css';

const { Text } = Typography;

const AUTO_REFRESH_MS = 30 * 1000;
const UP_COLOR = '#e5484d';
const LABEL_COLORS = { 强势: 'red', 活跃: 'volcano', 观望: 'green', 规避: 'success' };
const LABEL_CLASS = { 强势: 'strong', 活跃: 'active', 观望: 'watch', 规避: 'avoid' };
const LEVEL_TITLE = { l1: '一级行业', l2: '二级行业', l3: '细分行业' };
const LABEL_ORDER = ['强势', '活跃', '观望', '规避'];
// 行业自身信号过滤：全部 / 四档标签 / 无信号
const INDUSTRY_LABEL_OPTIONS = [
  { label: '全部', value: '' },
  ...LABEL_ORDER.map(item => ({ label: item, value: item })),
  { label: '无信号', value: 'none' },
];

/** 申万一/二级指数自身的当日信号；三级 tushare 没有行情，不显示 */
const SignalDot = ({ value, prefix = '' }) => (value ? (
  <Tooltip title={`${prefix}行业指数自身信号：${value}`}>
    <Tag color={LABEL_COLORS[value]} className="industry-signal-tag">{prefix}{value}</Tag>
  </Tooltip>
) : null);

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const fmtPct = value => (value === null || value === undefined ? '-' : `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%`);
const pctClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');

/** 前3标签：三段色块，从左到右是前第三日→前一日；涨停那天把连板数写进色块 */
const TrendSegments = ({ history }) => {
  const items = [0, 1, 2].map(index => (history || [])[index] || null);
  return (
    <span className="trend-tri">
      {items.map((item, index) => {
        const cls = item?.label ? `trend-seg trend-${LABEL_CLASS[item.label]}` : 'trend-seg';
        const tip = item
          ? `${item.d} ${item.label || '无标签'}${item.boards ? ` · ${item.boards}板` : ''}${item.limit_down ? ' · 跌停' : ''}`
          : '无数据';
        return (
          <Tooltip title={tip} key={index}>
            <span className={item?.limit_down ? `${cls} trend-limit-down` : cls}>{item?.boards ? item.boards : ''}</span>
          </Tooltip>
        );
      })}
    </span>
  );
};

/** 行业表列。完整模式给全字段，精简模式只留最常看的几列；
 *  名次与名称固定在左侧，其余横向滚动，这样列多也不会挤掉关键信息。 */
const buildIndustryColumns = (level, compact) => {
  const num = (title, key, { width = 46, color = '', tip = '' } = {}) => ({
    title: tip ? <Tooltip title={tip}>{title}</Tooltip> : title,
    dataIndex: key,
    width,
    align: 'right',
    sorter: (a, b) => (a[key] || 0) - (b[key] || 0),
    render: value => (value ? <Text className={color}>{value}</Text> : <Text type="secondary">0</Text>),
  });

  const head = [
    {
      title: '#',
      dataIndex: 'rank',
      width: 44,
      fixed: 'left',
      render: (value, record) => <Text type="secondary">{value ?? record.rank_str}</Text>,
    },
    {
      title: LEVEL_TITLE[level],
      dataIndex: 'name',
      width: compact ? 104 : 96,
      fixed: 'left',
      ellipsis: true,
      render: (value, record) => (
        <span className="industry-name-cell">
          <Tooltip title={`${record.count} 只成分 · 成交额 ${record.amount_yi} 亿${record.parent ? ` · 属于 ${record.parent}` : ''}`}>
            <span>{value}</span>
          </Tooltip>
          {record.label && <span className={`industry-name-dot dot-${LABEL_ORDER.indexOf(record.label)}`} title={`行业信号：${record.label}`} />}
        </span>
      ),
    },
    {
      title: '涨幅',
      dataIndex: 'pct',
      width: 68,
      align: 'right',
      sorter: (a, b) => a.pct - b.pct,
      render: value => <Text className={pctClass(value)}>{fmtPct(value)}</Text>,
    },
  ];

  const tail = [
    {
      title: <Tooltip title="行业情绪分，0~100">情绪</Tooltip>,
      dataIndex: 'sentiment',
      width: 56,
      align: 'right',
      sorter: (a, b) => a.sentiment - b.sentiment,
      render: value => <Text>{value}</Text>,
    },
    {
      title: <Tooltip title="行业综合分，按它排名">综合</Tooltip>,
      dataIndex: 'composite',
      width: 60,
      align: 'right',
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.composite - b.composite,
      render: value => <Text strong>{value}</Text>,
    },
  ];

  if (compact) {
    return [
      ...head,
      {
        title: <Tooltip title="强势 / 活跃 / 观望 家数">强/活/观</Tooltip>,
        key: 'labels',
        width: 82,
        align: 'right',
        sorter: (a, b) => (a.st * 2 + a.by) - (b.st * 2 + b.by),
        render: (_, record) => (
          <span className="industry-labels">
            <Text className="is-up" strong>{record.st}</Text>
            <Text type="secondary">/</Text>
            <Text className="is-up">{record.by}</Text>
            <Text type="secondary">/</Text>
            <Text className="is-down">{record.gw}</Text>
          </span>
        ),
      },
      {
        title: '涨停',
        dataIndex: 'lu',
        width: 62,
        align: 'right',
        sorter: (a, b) => a.lu - b.lu,
        render: (value, record) => (
          <Tooltip title={`首板 ${record.fb} · 二板 ${record.eb} · 多板 ${record.lb3} · 跌停 ${record.ld}`}>
            <Text className="is-up">{value}</Text>
          </Tooltip>
        ),
      },
      ...tail,
    ];
  }

  // 涨跌、涨跌停、连板三组各并成一列：红绿分色即可区分，省下 4 列宽度
  const pair = (title, upKey, downKey, { width = 72, tip = '' } = {}) => ({
    title: tip ? <Tooltip title={tip}>{title}</Tooltip> : title,
    key: `${upKey}_${downKey}`,
    width,
    align: 'right',
    sorter: (a, b) => (a[upKey] || 0) - (b[upKey] || 0),
    render: (_, record) => (
      <span className="industry-pair">
        <Text className="is-up">{record[upKey] || 0}</Text>
        <Text type="secondary">/</Text>
        <Text className="is-down">{record[downKey] || 0}</Text>
      </span>
    ),
  });

  return [
    ...head,
    { title: '成交额', dataIndex: 'amount_yi', width: 72, align: 'right', sorter: (a, b) => a.amount_yi - b.amount_yi },
    num('成分', 'count', { width: 50 }),
    num('强势', 'st', { color: 'is-up', tip: '强势家数' }),
    num('活跃', 'by', { color: 'is-up' }),
    num('观望', 'gw', { color: 'is-down' }),
    num('规避', 'av', { color: 'is-down' }),
    pair('涨/跌', 'up', 'down', { width: 78, tip: '上涨家数 / 下跌家数' }),
    pair('涨停/跌停', 'lu', 'ld', { width: 82, tip: '涨停家数 / 跌停家数' }),
    {
      title: <Tooltip title="多板（三板及以上） / 二板 / 首板">多/二/首</Tooltip>,
      key: 'boards',
      width: 80,
      align: 'right',
      // 按连板梯队排序：多板权重最高，其次二板、首板
      sorter: (a, b) => ((a.lb3 || 0) * 100 + (a.eb || 0) * 10 + (a.fb || 0))
        - ((b.lb3 || 0) * 100 + (b.eb || 0) * 10 + (b.fb || 0)),
      render: (_, record) => (
        <span className="industry-pair">
          <Text className="is-up" strong={Boolean(record.lb3)}>{record.lb3 || 0}</Text>
          <Text type="secondary">/</Text>
          <Text className="is-up">{record.eb || 0}</Text>
          <Text type="secondary">/</Text>
          <Text className="is-up">{record.fb || 0}</Text>
        </span>
      ),
    },
    ...tail,
  ];
};

const MarketIndustryRelation = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [universe, setUniverse] = useState('all');
  const [focus, setFocus] = useState('');
  const [l1Label, setL1Label] = useState('');   // 按所属一级行业的当日信号过滤
  const [l2Label, setL2Label] = useState('');   // 按所属二级行业的当日信号过滤
  const [selected, setSelected] = useState({ l1: null, l2: null, l3: null });
  const [history, setHistory] = useState(null);
  const [columnMode, setColumnMode] = useState('full');   // full=完整列 / compact=精简列
  const [panelStock, setPanelStock] = useState(null);     // 点成分股名称打开的行情面板
  // 分页必须受控：只传 pageSize 常量而不接 onChange 时，antd 会把切换器的改动丢掉（点了没反应）
  const [memberPage, setMemberPage] = useState({ current: 1, pageSize: 30 });

  // 行业选择或过滤条件变了回到第 1 页
  useEffect(() => {
    setMemberPage(prev => ({ ...prev, current: 1 }));
  }, [selected, universe, focus, l1Label, l2Label]);

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const response = await request.get('/api/market/industry-relation', {
        params: { universe, focus, l1_label: l1Label, l2_label: l2Label },
      });
      setData(response.data);
      setError('');
    } catch (err) {
      setError(formatErrorMessage(err, '加载行业关联失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [universe, focus, l1Label, l2Label]);

  useEffect(() => {
    load();
    const timer = setInterval(() => load({ silent: true }), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  // 关联结构：选中的最深一级行业的指数走势
  const deepest = selected.l3 ? { level: 'l3', name: selected.l3 }
    : selected.l2 ? { level: 'l2', name: selected.l2 }
      : selected.l1 ? { level: 'l1', name: selected.l1 } : null;

  useEffect(() => {
    if (!deepest) {
      setHistory(null);
      return undefined;
    }
    let cancelled = false;
    request.get('/api/market/industry-relation/history', { params: { level: deepest.level, name: deepest.name, days: 60 } })
      .then(response => { if (!cancelled) setHistory(response.data); })
      .catch(() => { if (!cancelled) setHistory(null); });
    return () => { cancelled = true; };
  }, [deepest?.level, deepest?.name]);   // eslint-disable-line react-hooks/exhaustive-deps

  const l2Rows = useMemo(
    () => (data?.l2 || []).filter(row => !selected.l1 || row.parent === selected.l1),
    [data, selected.l1],
  );
  const l3Rows = useMemo(
    () => (data?.l3 || []).filter(row => (selected.l2
      ? row.parent === selected.l2
      : !selected.l1 || l2Rows.some(item => item.name === row.parent))),
    [data, selected.l1, selected.l2, l2Rows],
  );
  const memberRows = useMemo(() => {
    const members = data?.members || [];
    if (selected.l3) return members.filter(row => row.l3_name === selected.l3);
    if (selected.l2) return members.filter(row => row.l2_name === selected.l2);
    if (selected.l1) return members.filter(row => row.l1_name === selected.l1);
    return members;
  }, [data, selected]);

  const pick = (level, name) => {
    setSelected(prev => {
      if (level === 'l1') return { l1: prev.l1 === name ? null : name, l2: null, l3: null };
      if (level === 'l2') {
        const next = prev.l2 === name ? null : name;
        const parent = (data?.l2 || []).find(row => row.name === name)?.parent;
        return { l1: next ? parent || prev.l1 : prev.l1, l2: next, l3: null };
      }
      const next = prev.l3 === name ? null : name;
      const row = (data?.l3 || []).find(item => item.name === name);
      const parentL2 = row?.parent;
      const parentL1 = (data?.l2 || []).find(item => item.name === parentL2)?.parent;
      return next ? { l1: parentL1 || prev.l1, l2: parentL2 || prev.l2, l3: next } : { ...prev, l3: null };
    });
  };

  const compact = columnMode === 'compact';
  const industryColumns = useMemo(
    () => ({
      l1: buildIndustryColumns('l1', compact),
      l2: buildIndustryColumns('l2', compact),
      l3: buildIndustryColumns('l3', compact),
    }),
    [compact],
  );
  // 完整模式列多，给一个横向滚动宽度；精简模式正好铺满
  const industryScrollX = compact ? undefined : 860;

  const rowProps = level => record => ({
    onClick: () => pick(level, record.name),
    className: selected[level] === record.name ? 'market-industry__row--active' : '',
    style: { cursor: 'pointer' },
  });

  const memberColumns = useMemo(() => [
    {
      title: '名称',
      dataIndex: 'name',
      width: 100,
      fixed: 'left',
      render: (value, record) => (
        <Space size={4}>
          <Text className="industry-link" onClick={() => setPanelStock(record)}>{value}</Text>
          <StockDetailLink symbol={record.ts_code} className="stock-external-link">↗</StockDetailLink>
        </Space>
      ),
    },
    { title: '代码', dataIndex: 'code', width: 80, render: value => <Text type="secondary">{value}</Text> },
    {
      title: '一级行业',
      dataIndex: 'l1_name',
      width: 96,
      ellipsis: true,
      render: value => (
        <Text className="industry-link" onClick={() => pick('l1', value)}>{value}</Text>
      ),
    },
    {
      title: '细分行业',
      dataIndex: 'l3_name',
      width: 116,
      ellipsis: true,
      render: value => (
        <Text className="industry-link" onClick={() => pick('l3', value)}>{value}</Text>
      ),
    },
    { title: '现价', dataIndex: 'price', width: 76, align: 'right' },
    {
      title: '涨幅',
      dataIndex: 'pct',
      width: 80,
      align: 'right',
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.pct - b.pct,
      render: value => <Text className={pctClass(value)}>{fmtPct(value)}</Text>,
    },
    { title: '成交额(亿)', dataIndex: 'amount_yi', width: 92, align: 'right', sorter: (a, b) => a.amount_yi - b.amount_yi },
    {
      title: <Tooltip title="所属申万一级 / 二级行业指数自身的当日最新信号">行业信号</Tooltip>,
      key: 'industry_signals',
      width: 96,
      render: (_, record) => (
        <Space size={2}>
          <SignalDot value={record.l1_label} prefix="一" />
          <SignalDot value={record.l2_label} prefix="二" />
          {!record.l1_label && !record.l2_label && <Text type="secondary">-</Text>}
        </Space>
      ),
    },
    {
      title: '今日标签',
      dataIndex: 'label',
      width: 96,
      render: (value, record) => (
        <Space size={2}>
          {value && (
            <Tooltip title={record.hit_time
              ? `${record.hit_time} 命中（与提示看板同一条记录）${record.live_label && record.live_label !== value ? ` · 此刻实时状态：${record.live_label}` : ''}`
              : '按此刻快照实时判定'}>
              <Tag color={LABEL_COLORS[value]}>{value}</Tag>
            </Tooltip>
          )}
          {record.limit_up && <Tag color="red">{record.boards > 1 ? `${record.boards}板` : '涨停'}</Tag>}
          {record.limit_down && <Tag color="green">跌停</Tag>}
        </Space>
      ),
    },
    {
      title: '前3标签',
      key: 'labels_3d',
      width: 76,
      // 排序按最近一天的连板数，其次看有没有标签，方便把连续走强的排到前面
      sorter: (a, b) => {
        const score = row => (row.labels_3d || []).reduce((acc, item, index) => acc + (item?.boards || 0) * (index + 1) + (item?.label ? 1 : 0), 0);
        return score(a) - score(b);
      },
      render: (_, record) => <TrendSegments history={record.labels_3d} />,
    },
    {
      title: '命中时间',
      dataIndex: 'hit_time',
      width: 84,
      render: value => (value ? <Text strong>{value}</Text> : <Text type="secondary">-</Text>),
    },
  ], [data]);   // eslint-disable-line react-hooks/exhaustive-deps

  const historyOption = useMemo(() => {
    if (!history?.dates?.length) return null;
    return {
      animation: false,
      grid: { left: 52, right: 52, top: 24, bottom: 28 },
      tooltip: { trigger: 'axis' },
      legend: { top: 0, data: ['指数', '成交额'] },
      xAxis: { type: 'category', data: history.dates, axisLabel: { fontSize: 10, interval: Math.floor(history.dates.length / 6) } },
      yAxis: [
        { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
        { type: 'value', name: '亿', splitLine: { show: false } },
      ],
      series: [
        {
          name: '指数',
          type: 'line',
          showSymbol: false,
          lineStyle: { width: 2, color: UP_COLOR },
          itemStyle: { color: UP_COLOR },
          data: history.close,
        },
        {
          name: '成交额',
          type: 'bar',
          yAxisIndex: 1,
          itemStyle: { color: '#a9b8d0', opacity: 0.6 },
          data: history.amount_yi,
        },
      ],
    };
  }, [history]);

  const breadcrumb = [selected.l1, selected.l2, selected.l3].filter(Boolean).join(' › ');

  return (
    <div className="market-industry">
      <div className="market-industry__filters">
        <Space wrap size={[8, 6]}>
          <Text type="secondary">范围</Text>
          <Segmented
            size="small"
            value={universe}
            onChange={setUniverse}
            options={(data?.universe_options || [{ key: 'all', name: '全A' }]).map(item => ({ label: item.name, value: item.key }))}
          />
        </Space>
        <Space wrap size={[8, 6]}>
          <Text type="secondary">列</Text>
          <Segmented
            size="small"
            value={columnMode}
            onChange={setColumnMode}
            options={[{ label: '完整', value: 'full' }, { label: '精简', value: 'compact' }]}
          />
        </Space>
        <Space wrap size={[8, 6]}>
          <Text type="secondary">个股</Text>
          <Segmented
            size="small"
            value={focus}
            onChange={setFocus}
            options={(data?.focus_options || [{ key: '', name: '全部' }]).map(item => ({ label: item.name, value: item.key }))}
          />
        </Space>
        <Space wrap size={[8, 6]}>
          <Text type="secondary">一级信号</Text>
          <Segmented size="small" value={l1Label} onChange={setL1Label} options={INDUSTRY_LABEL_OPTIONS} />
        </Space>
        <Space wrap size={[8, 6]}>
          <Text type="secondary">二级信号</Text>
          <Segmented size="small" value={l2Label} onChange={setL2Label} options={INDUSTRY_LABEL_OPTIONS} />
        </Space>
      </div>

      {data && (
        <div className="market-industry__meta">
          <Space wrap size={[12, 4]}>
            <Text strong>{breadcrumb || '全部行业'}</Text>
            {breadcrumb && <Text type="secondary">（再点同一行取消）</Text>}
            <Text type="secondary">
              {data.universe_name}{data.focus ? ` · ${(data.focus_options || []).find(item => item.key === data.focus)?.name}` : ''}
              {' · '}样本 {data.picked} 只 · 成分&lt;{data.min_rank_count} 不参与排名 · {data.fetched_at}
            </Text>
          </Space>
        </div>
      )}

      {error && <Alert className="market-alert" type="warning" showIcon message={error} />}
      {(data?.warnings || []).map(item => (
        <Alert key={item} className="market-alert" type="warning" showIcon message={item} />
      ))}

      {/* 首次加载时里面没有内容，Spin 会塌成 0 高度、看起来像"点了没反应"，所以给个占位；
          切换过滤条件时也要转圈（盖在旧数据上），否则几秒内没有任何反馈 */}
      <Spin spinning={loading} tip="加载行业关联…">
        {!data && loading && <div className="market-industry__loading" />}
        {data?.picked ? (
          <Row gutter={[12, 12]} className="market-industry__body">
            <Col xs={24} xl={10}>
              <Space direction="vertical" size={12} className="market-industry__stack">
                <Card size="small" title={`一级行业（${data.l1.length}）`} extra={<Text type="secondary">点击下钻</Text>}>
                  <Table
                    size="small" rowKey="name" columns={industryColumns.l1} dataSource={data.l1}
                    pagination={false} scroll={{ x: industryScrollX, y: 300 }} onRow={rowProps('l1')}
                  />
                </Card>
                <Card size="small" title={`二级行业（${l2Rows.length}）`} extra={<Text type="secondary">{selected.l1 || '全部'}</Text>}>
                  <Table
                    size="small" rowKey="name" columns={industryColumns.l2} dataSource={l2Rows}
                    pagination={false} scroll={{ x: industryScrollX, y: 240 }} onRow={rowProps('l2')}
                  />
                </Card>
                <Card size="small" title={`细分行业（${l3Rows.length}）`} extra={<Text type="secondary">{selected.l2 || selected.l1 || '全部'}</Text>}>
                  <Table
                    size="small" rowKey="name" columns={industryColumns.l3} dataSource={l3Rows}
                    pagination={false} scroll={{ x: industryScrollX, y: 240 }} onRow={rowProps('l3')}
                  />
                </Card>
              </Space>
            </Col>

            <Col xs={24} xl={14}>
              <Card
                size="small"
                className="market-industry__structure"
                title={history ? `关联结构 · ${history.name}` : '关联结构'}
                extra={history?.pct_range !== null && history?.pct_range !== undefined
                  ? <Text className={pctClass(history.pct_range)}>近 {history.dates.length} 日 {fmtPct(history.pct_range)}</Text>
                  : <Text type="secondary">选中行业后显示指数走势</Text>}
              >
                {historyOption
                  ? <ReactECharts option={historyOption} style={{ height: 190 }} notMerge lazyUpdate />
                  : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="点击左侧任意行业，这里显示它的行业指数走势与成交额" />}
              </Card>

              {panelStock && (
                <StockInlinePanel
                  stock={panelStock}
                  onClose={() => setPanelStock(null)}
                  highlightTime={panelStock.hit_time}
                  className="market-industry__panel"
                  extraMeta={(
                    <>
                      {panelStock.label && <Tag color={LABEL_COLORS[panelStock.label]}>{panelStock.label}</Tag>}
                      <Text type="secondary">
                        {panelStock.l1_name} · {panelStock.l3_name} · {fmtPct(panelStock.pct)}
                      </Text>
                    </>
                  )}
                />
              )}

              <Card
                size="small"
                className="market-industry__members"
                title={`成分股（${memberRows.length}）`}
                extra={<Text type="secondary">行业列可点击联动 · 前3标签自左向右为前第三日→前一日</Text>}
              >
                <Table
                  size="small"
                  rowKey="ts_code"
                  columns={memberColumns}
                  dataSource={memberRows}
                  pagination={{
                    current: memberPage.current,
                    pageSize: memberPage.pageSize,
                    showSizeChanger: true,
                    pageSizeOptions: [30, 50, 100, 200],
                    size: 'small',
                    showTotal: total => `共 ${total} 只`,
                    onChange: (current, pageSize) => setMemberPage(prev => ({
                      current: pageSize !== prev.pageSize ? 1 : current,
                      pageSize,
                    })),
                  }}
                  scroll={{ x: 900, y: 460 }}
                />
              </Card>
            </Col>
          </Row>
        ) : (
          !loading && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={data ? `${data.universe_name}${data.focus ? ' · 当前焦点' : ''}下没有符合条件的个股` : '暂无数据'}
            />
          )
        )}
      </Spin>
    </div>
  );
};

export default MarketIndustryRelation;
