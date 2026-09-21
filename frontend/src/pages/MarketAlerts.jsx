import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Radio, Row, Segmented, Select, Space, Spin, Statistic, Table, Tag, Tooltip, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';
import StockInlinePanel from '../components/StockInlinePanel';
import './Market.css';

const { Text } = Typography;

const AUTO_REFRESH_MS = 60 * 1000;
const UP_COLOR = '#e5484d';
const DOWN_COLOR = '#2f9e63';

const LABEL_META = {
  强势: { color: 'red', desc: '九转高计数 3~4 且同时段量比 ≥2' },
  活跃: { color: 'volcano', desc: '九转高计数 ≥2 · 上涨 · 成交额 ≥0.8亿 · 量比 ≥1.5' },
  观望: { color: 'green', desc: '九转低计数 ≥2 或急跌结构' },
  规避: { color: 'success', desc: '九转低计数 ≥4 且当日下跌' },
};
const LABEL_ORDER = ['强势', '活跃', '观望', '规避'];
const INDUSTRY_LEVEL_TITLE = { l1: '一级行业', l2: '二级行业', l3: '细分行业' };
// 行业自身信号的过滤：全部 / 四档标签 / 无信号
const INDUSTRY_LABEL_OPTIONS = [
  { label: '全部', value: '' },
  ...LABEL_ORDER.map(item => ({ label: item, value: item })),
  { label: '无信号', value: 'none' },
];

/** 行业信号小标签：一级/二级申万指数自身的当日最新标签 */
const IndustrySignal = ({ value }) => (value
  ? <Tag color={LABEL_META[value]?.color} className="industry-signal-tag">{value}</Tag>
  : <Text type="secondary">-</Text>);

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const fmtPct = value => (value === null || value === undefined ? '-' : `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%`);
const pctClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');

const signedBars = values => values.map(value => (
  value === null || value === undefined
    ? null
    : { value, itemStyle: { color: value >= 0 ? UP_COLOR : DOWN_COLOR, opacity: 0.8 } }
));

const MarketAlerts = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [tradeDate, setTradeDate] = useState(null);
  const [label, setLabel] = useState('');
  const [industry, setIndustry] = useState(null);     // 点击行业柱筛选
  const [industryLevel, setIndustryLevel] = useState('l1'); // 申万级别，默认一级（与行业关联同一口径）
  const [l1Label, setL1Label] = useState('');   // 按所属申万一级行业的当日标签过滤
  const [l2Label, setL2Label] = useState('');   // 按所属申万二级行业的当日标签过滤
  // 分页必须受控：只传 pageSize 常量而不接 onChange 时，antd 会把切换器的改动丢掉（点了没反应）
  const [tablePage, setTablePage] = useState({ current: 1, pageSize: 50 });
  const [klineStock, setKlineStock] = useState(null); // 内嵌K线面板（点名称打开，不弹窗）

  // 筛选条件变了回到第 1 页，否则停在第 5 页时切个行业会看到空表
  useEffect(() => {
    setTablePage(prev => ({ ...prev, current: 1 }));
  }, [industry, label, tradeDate, industryLevel, l1Label, l2Label]);

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const params = {};
      if (tradeDate) params.date = tradeDate;
      if (label) params.label = label;
      if (l1Label) params.l1_label = l1Label;
      if (l2Label) params.l2_label = l2Label;
      params.level = industryLevel;
      const response = await request.get('/api/market/alerts', { params });
      setData(response.data);
      setError('');
    } catch (err) {
      setError(formatErrorMessage(err, '加载提示看板失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [tradeDate, label, industryLevel, l1Label, l2Label]);

  useEffect(() => {
    load();
    const timer = setInterval(() => load({ silent: true }), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const summary = data?.summary;

  const distributionOption = useMemo(() => {
    if (!summary?.return_distribution?.length) return null;
    return {
      animation: false,
      grid: { left: 8, right: 8, top: 24, bottom: 24, containLabel: true },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: summary.return_distribution.map(item => item.label), axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [{
        type: 'bar',
        barCategoryGap: '30%',
        label: { show: true, position: 'top', fontSize: 11 },
        data: summary.return_distribution.map((item, index) => ({
          value: item.count,
          itemStyle: { color: ['#b01818', '#e23a3a', '#f2a3a3', '#a9ddb5', '#0a8f3c'][index] },
        })),
      }],
    };
  }, [summary]);

  const timeOption = useMemo(() => {
    if (!summary?.time_buckets?.length) return null;
    return {
      animation: false,
      grid: { left: 8, right: 8, top: 24, bottom: 40, containLabel: true },
      tooltip: {
        trigger: 'axis',
        formatter: params => {
          const bucket = summary.time_buckets[params[0].dataIndex];
          return bucket.count
            ? `${bucket.label}<br/>命中 ${bucket.count} 只 · 平均 ${fmtPct(bucket.avg)}`
            : `${bucket.label}<br/>无命中`;
        },
      },
      xAxis: {
        type: 'category',
        data: summary.time_buckets.map(item => item.label),
        axisLabel: { fontSize: 10, rotate: 35, interval: 0 },
      },
      yAxis: { type: 'value', axisLabel: { formatter: value => `${value}%` }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [{ type: 'bar', barWidth: '55%', data: signedBars(summary.time_buckets.map(item => item.avg)) }],
    };
  }, [summary]);

  const industryOption = useMemo(() => {
    if (!summary?.industries?.length) return null;
    const rows = summary.industries.slice(0, 12);
    return {
      animation: false,
      grid: { left: 8, right: 40, top: 24, bottom: 40, containLabel: true },
      tooltip: { trigger: 'axis' },
      legend: { top: 0, data: ['命中数', '平均涨幅'] },
      xAxis: { type: 'category', data: rows.map(item => item.industry), axisLabel: { fontSize: 10, rotate: 35, interval: 0 } },
      yAxis: [
        { type: 'value', splitLine: { lineStyle: { color: '#f0f0f0' } } },
        { type: 'value', axisLabel: { formatter: value => `${value}%` }, splitLine: { show: false } },
      ],
      series: [
        {
          name: '命中数',
          type: 'bar',
          barWidth: '50%',
          data: rows.map(item => ({
            value: item.count,
            itemStyle: { color: industry === item.industry ? '#2f6fdb' : '#8aa6d8' },
          })),
        },
        {
          name: '平均涨幅',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: true,
          itemStyle: { color: '#e58a1a' },
          data: rows.map(item => item.avg),
        },
      ],
    };
  }, [summary, industry]);

  const visibleRows = useMemo(
    () => (data?.rows || []).filter(row => !industry || row[`industry_${industryLevel}`] === industry),
    [data, industry, industryLevel],
  );

  const columns = useMemo(() => [
    { title: '命中时间', dataIndex: 'hit_time', width: 86, sorter: (a, b) => (a.hit_time || '').localeCompare(b.hit_time || '') },
    {
      title: '名称',
      dataIndex: 'name',
      width: 110,
      render: (value, record) => (
        <Space size={4}>
          <Text className="alert-name-link" onClick={() => setKlineStock(record)}>{value || record.code}</Text>
          <StockDetailLink symbol={record.ts_code} className="alert-name-external">↗</StockDetailLink>
        </Space>
      ),
    },
    { title: '代码', dataIndex: 'code', width: 90, render: value => <Text type="secondary">{value}</Text> },
    {
      title: INDUSTRY_LEVEL_TITLE[industryLevel],
      dataIndex: `industry_${industryLevel}`,
      width: 100,
      ellipsis: true,
      render: value => (
        <Text
          className="alert-industry-link"
          onClick={() => setIndustry(prev => (prev === value ? null : value))}
        >
          {value || '-'}
        </Text>
      ),
    },
    {
      title: '标签',
      dataIndex: 'label',
      width: 86,
      render: (value, record) => (
        <Tooltip title={(
          <span>
            {LABEL_META[value]?.desc}
            <br />
            首次命中 {record.hit_time}
            {record.change_count ? ` · 盘中变化 ${record.change_count} 次，最近 ${record.last_change_time}` : ''}
          </span>
        )}
        >
          <Tag color={LABEL_META[value]?.color}>{value}{record.change_count ? '*' : ''}</Tag>
        </Tooltip>
      ),
      sorter: (a, b) => LABEL_ORDER.indexOf(a.label) - LABEL_ORDER.indexOf(b.label),
    },
    {
      title: <Tooltip title="所属申万一级行业指数自身的当日最新信号">一级信号</Tooltip>,
      dataIndex: 'l1_label',
      width: 78,
      render: value => <IndustrySignal value={value} />,
      sorter: (a, b) => LABEL_ORDER.indexOf(a.l1_label) - LABEL_ORDER.indexOf(b.l1_label),
    },
    {
      title: <Tooltip title="所属申万二级行业指数自身的当日最新信号">二级信号</Tooltip>,
      dataIndex: 'l2_label',
      width: 78,
      render: value => <IndustrySignal value={value} />,
      sorter: (a, b) => LABEL_ORDER.indexOf(a.l2_label) - LABEL_ORDER.indexOf(b.l2_label),
    },
    { title: '综合分', dataIndex: 'score', width: 84, align: 'right', sorter: (a, b) => (a.score || 0) - (b.score || 0) },
    {
      title: '命中涨幅',
      dataIndex: 'pct',
      width: 92,
      align: 'right',
      sorter: (a, b) => (a.pct || 0) - (b.pct || 0),
      render: value => <Text className={pctClass(value)}>{fmtPct(value)}</Text>,
    },
    { title: '量比', dataIndex: 'volume_ratio', width: 76, align: 'right', sorter: (a, b) => (a.volume_ratio || 0) - (b.volume_ratio || 0) },
    { title: '成交额(亿)', dataIndex: 'amount_yi', width: 100, align: 'right', sorter: (a, b) => (a.amount_yi || 0) - (b.amount_yi || 0) },
    {
      title: '九转',
      key: 'td',
      width: 76,
      align: 'right',
      render: (_, record) => (record.td_up ? `高${record.td_up}` : record.td_down ? `低${record.td_down}` : '-'),
    },
    { title: '命中价', dataIndex: 'price', width: 84, align: 'right' },
    { title: '现价', dataIndex: 'last_price', width: 84, align: 'right' },
    {
      title: '命中后涨幅',
      dataIndex: 'cum_pct',
      width: 106,
      align: 'right',
      defaultSortOrder: 'descend',
      sorter: (a, b) => (a.cum_pct || 0) - (b.cum_pct || 0),
      render: value => <Text className={pctClass(value)}>{fmtPct(value)}</Text>,
    },
  ], [industryLevel]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="market-alerts">
      <div className="market-toolbar">
        <Space wrap>
          <Text type="secondary">交易日</Text>
          <Select
            className="market-date-select"
            value={data?.date || tradeDate}
            options={(data?.dates || []).map(item => ({ value: item, label: item }))}
            onChange={setTradeDate}
            placeholder="最新交易日"
          />
          <Text type="secondary">个股</Text>
          <Radio.Group
            size="small"
            value={label}
            onChange={event => setLabel(event.target.value)}
            optionType="button"
            options={[{ label: '全部', value: '' }, ...LABEL_ORDER.map(item => ({ label: item, value: item }))]}
          />
          <Text type="secondary">一级</Text>
          <Radio.Group
            size="small"
            value={l1Label}
            onChange={event => setL1Label(event.target.value)}
            optionType="button"
            options={INDUSTRY_LABEL_OPTIONS}
          />
          <Text type="secondary">二级</Text>
          <Radio.Group
            size="small"
            value={l2Label}
            onChange={event => setL2Label(event.target.value)}
            optionType="button"
            options={INDUSTRY_LABEL_OPTIONS}
          />
        </Space>
        {data?.thresholds && (
          <Text type="secondary">
            口径：九转高计数 ≥{data.thresholds.active_td_up_min} · 成交额 ≥{data.thresholds.min_amount_yi}亿 ·
            同时段量比 ≥{data.thresholds.min_volume_ratio}（强势 ≥{data.thresholds.strong_volume_ratio}）
            · 标签盘中变化取最新（带 * 表示变过）· 命中价以首次命中为准
          </Text>
        )}
      </div>

      {error && <Alert className="market-alert" type="warning" showIcon message={error} />}

      {data?.sw_signals?.length > 0 && (
        <Card size="small" className="market-alerts__sw" title="行业信号（申万一/二级指数自身）">
          {[['sw_l1', '一级'], ['sw_l2', '二级']].map(([type, title]) => {
            const items = data.sw_signals.filter(item => item.entity_type === type);
            if (!items.length) return null;
            return (
              <div className="sw-signal-row" key={type}>
                <Text type="secondary" className="sw-signal-row__title">{title}</Text>
                {LABEL_ORDER.map(name => items.filter(item => item.label === name).map(item => (
                  <Tooltip
                    key={item.ts_code}
                    title={`${item.name} · ${fmtPct(item.pct)} · 首次 ${item.hit_time}${item.change_count ? ` · 变化 ${item.change_count} 次` : ''}`}
                  >
                    <Tag color={LABEL_META[name]?.color}>{item.name}</Tag>
                  </Tooltip>
                )))}
              </div>
            );
          })}
        </Card>
      )}

      <Spin spinning={loading && !data}>
        {summary?.total ? (
          <>
            <Row gutter={[12, 12]} className="market-alerts__stats">
              <Col xs={12} md={6} xl={4}><Card size="small"><Statistic title="命中总数" value={summary.total} suffix="只" /></Card></Col>
              <Col xs={12} md={6} xl={4}>
                <Card size="small">
                  <Statistic title="平均命中后涨幅" value={summary.avg ?? 0} precision={2} suffix="%"
                    valueStyle={{ color: summary.avg >= 0 ? UP_COLOR : DOWN_COLOR }} />
                </Card>
              </Col>
              <Col xs={12} md={6} xl={4}><Card size="small"><Statistic title="胜率" value={summary.win_rate ?? 0} precision={1} suffix="%" /></Card></Col>
              <Col xs={12} md={6} xl={4}>
                <Card size="small">
                  <Statistic title="最强" value={summary.best?.name || '-'}
                    suffix={summary.best ? fmtPct(summary.best.cum_pct) : ''} valueStyle={{ fontSize: 16 }} />
                </Card>
              </Col>
              <Col xs={12} md={6} xl={4}>
                <Card size="small">
                  <Statistic title="最弱" value={summary.worst?.name || '-'}
                    suffix={summary.worst ? fmtPct(summary.worst.cum_pct) : ''} valueStyle={{ fontSize: 16 }} />
                </Card>
              </Col>
              <Col xs={12} md={6} xl={4}>
                <Card size="small" className="market-alerts__labels">
                  <div className="ant-statistic-title">各标签表现</div>
                  {LABEL_ORDER.filter(item => summary.by_label?.[item]).map(item => (
                    <div key={item} className="market-alerts__label-row">
                      <Tag color={LABEL_META[item]?.color}>{item}</Tag>
                      <span>{summary.by_label[item].count} 只</span>
                      <span className={pctClass(summary.by_label[item].avg)}>{fmtPct(summary.by_label[item].avg)}</span>
                    </div>
                  ))}
                </Card>
              </Col>
            </Row>

            <Row gutter={[12, 12]}>
              <Col xs={24} xl={8}>
                <Card size="small" title="命中后涨幅分布" extra={<Text type="secondary">{summary.scored} 条有后续</Text>}>
                  {distributionOption ? <ReactECharts option={distributionOption} style={{ height: 240 }} notMerge lazyUpdate /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
                </Card>
              </Col>
              <Col xs={24} xl={8}>
                <Card size="small" title="按命中时段的平均涨幅" extra={<Text type="secondary">看什么时候的提示值得跟</Text>}>
                  {timeOption ? <ReactECharts option={timeOption} style={{ height: 240 }} notMerge lazyUpdate /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
                </Card>
              </Col>
              <Col xs={24} xl={8}>
                <Card
                  size="small"
                  title="行业分布"
                  extra={(
                    <Segmented
                      size="small"
                      value={industryLevel}
                      onChange={value => { setIndustryLevel(value); setIndustry(null); }}
                      options={[
                        { label: '一级', value: 'l1' },
                        { label: '二级', value: 'l2' },
                        { label: '细分', value: 'l3' },
                      ]}
                    />
                  )}
                >
                  {industryOption ? (
                    <ReactECharts
                      option={industryOption}
                      style={{ height: 240 }}
                      notMerge
                      lazyUpdate
                      onEvents={{
                        click: params => setIndustry(prev => (prev === params.name ? null : params.name)),
                      }}
                    />
                  ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
                </Card>
              </Col>
            </Row>

            {klineStock && (
              <StockInlinePanel
                stock={klineStock}
                onClose={() => setKlineStock(null)}
                highlightTime={klineStock.hit_time}
                className="market-alerts__kline"
                extraMeta={(
                  <>
                    <Tag color={LABEL_META[klineStock.label]?.color}>{klineStock.label}</Tag>
                    <Text type="secondary">
                      命中 {klineStock.hit_time} · {fmtPct(klineStock.pct)} · 命中后 {fmtPct(klineStock.cum_pct)}
                    </Text>
                  </>
                )}
              />
            )}

            <Card
              size="small"
              className="market-alerts__table"
              title={`命中列表（${visibleRows.length}${industry ? ` / ${data.rows.length}` : ''} 只）`}
              extra={industry ? (
                <Space size={6}>
                  <Tag closable color="blue" onClose={() => setIndustry(null)}>{industry}</Tag>
                  <Text type="secondary">点击行业或柱子取消筛选</Text>
                </Space>
              ) : <Text type="secondary">点击名称看K线 · 点击行业筛选</Text>}
            >
              <Table
                size="small"
                rowKey="ts_code"
                columns={columns}
                dataSource={visibleRows}
                pagination={{
                  current: tablePage.current,
                  pageSize: tablePage.pageSize,
                  showSizeChanger: true,
                  pageSizeOptions: [20, 50, 100, 200],
                  showTotal: total => `共 ${total} 只`,
                  // 改每页条数时回到第 1 页，避免当前页码超出新的总页数
                  onChange: (current, pageSize) => setTablePage(prev => ({
                    current: pageSize !== prev.pageSize ? 1 : current,
                    pageSize,
                  })),
                }}
                scroll={{ x: 1100, y: 480 }}
              />
            </Card>
          </>
        ) : (
          !loading && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={data?.date ? `${data.date} 暂无命中记录` : '还没有命中记录，盘中扫描任务跑起来后会自动出现'}
            />
          )
        )}
      </Spin>
    </div>
  );
};

export default MarketAlerts;
