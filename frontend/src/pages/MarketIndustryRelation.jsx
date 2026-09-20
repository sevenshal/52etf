import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Row, Segmented, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';
import './Market.css';

const { Text } = Typography;

const AUTO_REFRESH_MS = 30 * 1000;
const UP_COLOR = '#e5484d';
const LABEL_COLORS = { 强势: 'red', 活跃: 'volcano', 观望: 'green', 规避: 'success' };
const LABEL_CLASS = { 强势: 'strong', 活跃: 'active', 观望: 'watch', 规避: 'avoid' };
const LEVEL_TITLE = { l1: '一级行业', l2: '二级行业', l3: '细分行业' };

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

/** 三张行业表共用的紧凑列；名称列点击下钻 */
const buildIndustryColumns = level => [
  {
    title: '#',
    dataIndex: 'rank',
    width: 48,
    render: (value, record) => <Text type="secondary">{value ?? record.rank_str}</Text>,
  },
  {
    title: LEVEL_TITLE[level],
    dataIndex: 'name',
    width: 108,
    ellipsis: true,
    render: (value, record) => (
      <Tooltip title={`${record.count} 只成分 · 成交额 ${record.amount_yi} 亿${record.parent ? ` · 属于 ${record.parent}` : ''}`}>
        <span>{value}</span>
      </Tooltip>
    ),
  },
  {
    title: '涨幅',
    dataIndex: 'pct',
    width: 72,
    align: 'right',
    sorter: (a, b) => a.pct - b.pct,
    render: value => <Text className={pctClass(value)}>{fmtPct(value)}</Text>,
  },
  {
    title: '强/活/观',
    key: 'labels',
    width: 84,
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
    width: 74,
    align: 'right',
    sorter: (a, b) => a.lu - b.lu,
    render: (value, record) => (
      <Tooltip title={`首板 ${record.fb} · 二板 ${record.eb} · 多板 ${record.lb3} · 跌停 ${record.ld}`}>
        <span><Text className="is-up">{value}</Text>{record.lb3 ? <Text type="secondary">+{record.lb3}</Text> : null}</span>
      </Tooltip>
    ),
  },
  {
    title: '综合',
    dataIndex: 'composite',
    width: 68,
    align: 'right',
    defaultSortOrder: 'descend',
    sorter: (a, b) => a.composite - b.composite,
    render: (value, record) => (
      <Tooltip title={`情绪分 ${record.sentiment} · 涨跌 ${record.up}/${record.down}`}>
        <Text strong>{value}</Text>
      </Tooltip>
    ),
  },
];

const MarketIndustryRelation = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [universe, setUniverse] = useState('all');
  const [focus, setFocus] = useState('');
  const [selected, setSelected] = useState({ l1: null, l2: null, l3: null });
  const [history, setHistory] = useState(null);

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const response = await request.get('/api/market/industry-relation', { params: { universe, focus } });
      setData(response.data);
      setError('');
    } catch (err) {
      setError(formatErrorMessage(err, '加载行业关联失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [universe, focus]);

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
      render: (value, record) => <StockDetailLink symbol={record.ts_code}>{value}</StockDetailLink>,
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
      title: '今日标签',
      dataIndex: 'label',
      width: 96,
      render: (value, record) => (
        <Space size={2}>
          {value && <Tag color={LABEL_COLORS[value]}>{value}</Tag>}
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
          <Text type="secondary">焦点</Text>
          <Segmented
            size="small"
            value={focus}
            onChange={setFocus}
            options={(data?.focus_options || [{ key: '', name: '全部' }]).map(item => ({ label: item.name, value: item.key }))}
          />
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

      <Spin spinning={loading && !data}>
        {data?.picked ? (
          <Row gutter={[12, 12]} className="market-industry__body">
            <Col xs={24} xl={9}>
              <Space direction="vertical" size={12} className="market-industry__stack">
                <Card size="small" title={`一级行业（${data.l1.length}）`} extra={<Text type="secondary">点击下钻</Text>}>
                  <Table
                    size="small" rowKey="name" columns={buildIndustryColumns('l1')} dataSource={data.l1}
                    pagination={false} scroll={{ y: 300 }} onRow={rowProps('l1')}
                  />
                </Card>
                <Card size="small" title={`二级行业（${l2Rows.length}）`} extra={<Text type="secondary">{selected.l1 || '全部'}</Text>}>
                  <Table
                    size="small" rowKey="name" columns={buildIndustryColumns('l2')} dataSource={l2Rows}
                    pagination={false} scroll={{ y: 240 }} onRow={rowProps('l2')}
                  />
                </Card>
                <Card size="small" title={`细分行业（${l3Rows.length}）`} extra={<Text type="secondary">{selected.l2 || selected.l1 || '全部'}</Text>}>
                  <Table
                    size="small" rowKey="name" columns={buildIndustryColumns('l3')} dataSource={l3Rows}
                    pagination={false} scroll={{ y: 240 }} onRow={rowProps('l3')}
                  />
                </Card>
              </Space>
            </Col>

            <Col xs={24} xl={15}>
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
                  pagination={{ pageSize: 30, showSizeChanger: true, size: 'small' }}
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
