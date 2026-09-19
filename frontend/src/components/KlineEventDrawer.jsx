import React from 'react';
import { Descriptions, Drawer, Empty, Space, Table, Tag, Tooltip, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';
import { formatChineseAmount } from '../utils/format';
import {
  REVISION_DOWN_COLOR,
  REVISION_UP_COLOR,
  describeRevision,
  revisionTooltip,
} from '../utils/epsRevision';

const { Text, Paragraph } = Typography;

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

const pct = value => (toNumber(value) === null ? '--' : `${Number(value).toFixed(2)}%`);
const price = value => (toNumber(value) === null ? '--' : Number(value).toFixed(2));

// A股惯例红涨绿跌
const SignedPct = ({ value }) => {
  const number = toNumber(value);
  if (number === null) return <Text type="secondary">--</Text>;
  const color = number > 0 ? '#cf1322' : number < 0 ? '#389e0d' : undefined;
  return <span style={{ color }}>{`${number > 0 ? '+' : ''}${number.toFixed(2)}%`}</span>;
};

const RATING_COLORS = [
  [/买入|强烈推荐|增持|推荐|跑赢/, 'red'],
  [/卖出|减持|跑输/, 'green'],
];
const ratingColor = rating => (RATING_COLORS.find(([pattern]) => pattern.test(rating)) || [])[1];

const TargetPrice = ({ report }) => {
  if (report.target_low === null || report.target_low === undefined) return <Text type="secondary">未给目标价</Text>;
  const range = report.target_low === report.target_high
    ? price(report.target_low)
    : `${price(report.target_low)} ~ ${price(report.target_high)}`;
  const adjusted = toNumber(report.price_scale) !== null && Math.abs(report.price_scale - 1) > 1e-4;
  return (
    <span>
      <Text strong>{range}</Text>
      {adjusted && (
        <Tooltip title="写研报之后发生过送转/分红除权，目标价已按复权因子换算到当前前复权口径，和 K 线同一口径">
          <Text type="secondary" style={{ marginInlineStart: 6, fontSize: 12 }}>
            （原始 {report.target_low_raw === report.target_high_raw
              ? price(report.target_low_raw)
              : `${price(report.target_low_raw)} ~ ${price(report.target_high_raw)}`}）
          </Text>
        </Tooltip>
      )}
    </span>
  );
};

const eps3 = value => (toNumber(value) === null ? '--' : Number(value).toFixed(3));

// 展开后：这家机构对这一年的历次预测。和当前这篇有共同分析师的行正常显示，换了人的行置灰
const historyColumns = [
  { title: '日期', dataIndex: 'report_date', key: 'report_date', width: 92 },
  {
    title: '分析师',
    dataIndex: 'author_name',
    key: 'author_name',
    ellipsis: true,
    render: (value, row) => (
      <Tooltip title={row.shares_analyst ? '与当前这篇有共同分析师' : '分析师不同（同机构）'}>
        <Text type={row.shares_analyst ? undefined : 'secondary'}>{value || '--'}</Text>
      </Tooltip>
    ),
  },
  { title: 'EPS', dataIndex: 'eps', key: 'eps', align: 'right', width: 64, render: eps3 },
  {
    title: '较前一篇',
    dataIndex: 'change_pct',
    key: 'change_pct',
    align: 'right',
    width: 84,
    render: (value, row) => {
      const number = toNumber(value);
      if (number === null) return <Text type="secondary">--</Text>;
      const color = number > 0 ? REVISION_UP_COLOR : number < 0 ? REVISION_DOWN_COLOR : undefined;
      const text = `${number > 0 ? '+' : ''}${number.toFixed(2)}%`;
      return row.disclosed_between?.length
        ? <Tooltip title={`期间披露 ${row.disclosed_between.join('、')}`}><span style={{ color }}>{text} ⓘ</span></Tooltip>
        : <span style={{ color }}>{text}</span>;
    },
  },
  { title: '评级', dataIndex: 'rating', key: 'rating', width: 64, render: value => value || '--' },
];

const historyChartOption = history => ({
  animation: false,
  grid: { left: 40, right: 12, top: 10, bottom: 22 },
  xAxis: { type: 'category', data: history.map(row => row.report_date), axisLabel: { fontSize: 10 } },
  yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
  tooltip: { trigger: 'axis', valueFormatter: value => eps3(value) },
  series: [{
    type: 'line',
    name: 'EPS',
    data: history.map(row => ({
      value: row.eps,
      // 换了分析师的点画成空心灰点，一眼分清是"同一批人在调"还是"换人后的新口径"
      itemStyle: row.shares_analyst ? { color: '#1677ff' } : { color: '#fff', borderColor: '#bfbfbf' },
    })),
    symbolSize: 7,
    lineStyle: { color: '#1677ff', width: 1.5 },
  }],
});

const EpsHistory = ({ history }) => (
  <div style={{ padding: '4px 0' }}>
    {history.length >= 3 && (
      <ReactECharts option={historyChartOption(history)} style={{ height: 110 }} notMerge />
    )}
    <Table
      size="small"
      rowKey={row => `${row.report_date}-${row.author_name}-${row.eps}`}
      columns={historyColumns}
      dataSource={[...history].reverse()}
      pagination={false}
    />
  </div>
);

const forecastColumns = [
  { title: '预测年度', dataIndex: 'fiscal_year', key: 'fiscal_year', width: 80 },
  { title: 'EPS', dataIndex: 'eps', key: 'eps', align: 'right', render: eps3 },
  {
    title: '较上次',
    dataIndex: 'revision',
    key: 'revision',
    align: 'right',
    render: revision => {
      const { text, color } = describeRevision(revision);
      return (
        <Tooltip title={revisionTooltip(revision)}>
          <span style={{ color, whiteSpace: 'nowrap' }}>
            {text}
            {revision?.match === 'org' && <Text type="secondary" style={{ fontSize: 11 }}> 换人</Text>}
          </span>
        </Tooltip>
      );
    },
  },
  // 研报预测净利润单位是万元
  { title: '净利润', dataIndex: 'np', key: 'np', align: 'right', render: value => (toNumber(value) === null ? '--' : formatChineseAmount(value * 1e4)) },
  { title: 'PE', dataIndex: 'pe', key: 'pe', align: 'right', render: value => (toNumber(value) === null ? '--' : Number(value).toFixed(1)) },
];

const ResearchList = ({ items }) => (
  <Space direction="vertical" size={16} style={{ width: '100%' }}>
    {items.map((report, index) => (
      <div
        key={`${report.org_name}-${report.report_title}-${index}`}
        style={{ paddingBottom: 12, borderBottom: '1px dashed #f0f0f0' }}
      >
        <Paragraph strong style={{ marginBottom: 6 }}>{report.report_title || '（无标题）'}</Paragraph>
        <Space size={[8, 4]} wrap style={{ marginBottom: 8 }}>
          <Text type="secondary">🏛 {report.org_name}</Text>
          {report.author_name && <Text type="secondary">{report.author_name}</Text>}
          {report.rating && <Tag color={ratingColor(report.rating)}>{report.rating}</Tag>}
          <Text type="secondary" style={{ fontSize: 12 }}>发布于 {report.report_date}</Text>
        </Space>
        <div style={{ marginBottom: 8 }}>
          <Text type="secondary">目标价：</Text>
          <TargetPrice report={report} />
        </div>
        {report.forecasts?.length > 0 && (
          <Table
            size="small"
            rowKey="fiscal_year"
            columns={forecastColumns}
            dataSource={report.forecasts}
            pagination={false}
            expandable={{
              // 展开看这家机构对这一年的历次 EPS 预测
              expandedRowRender: record => <EpsHistory history={record.history || []} />,
              rowExpandable: record => (record.history?.length || 0) > 1,
            }}
          />
        )}
      </div>
    ))}
  </Space>
);

const FinancialSummary = ({ report }) => (
  <div style={{ marginBottom: 16 }}>
    <Space size={8} style={{ marginBottom: 8 }}>
      <Text strong style={{ fontSize: 15 }}>{report.period_label}</Text>
      <Text type="secondary">报告期 {report.end_date} · 首次披露 {report.date}</Text>
    </Space>
    {!report.has_values ? (
      <Text type="secondary">分析库里暂无这一期的财务数据</Text>
    ) : (
      <Descriptions size="small" column={1} bordered labelStyle={{ width: 120 }}>
        <Descriptions.Item label="营业收入">
          {formatChineseAmount(report.revenue)}　<Text type="secondary">同比</Text> <SignedPct value={report.revenue_yoy} />
        </Descriptions.Item>
        <Descriptions.Item label="归母净利润">
          {formatChineseAmount(report.n_income_attr_p)}　<Text type="secondary">同比</Text> <SignedPct value={report.netprofit_yoy} />
        </Descriptions.Item>
        <Descriptions.Item label="扣非归母净利润">
          {formatChineseAmount(report.profit_dedt)}　<Text type="secondary">同比</Text> <SignedPct value={report.dt_netprofit_yoy} />
        </Descriptions.Item>
        <Descriptions.Item label="销售毛利率">{pct(report.grossprofit_margin)}</Descriptions.Item>
        <Descriptions.Item label="销售净利率">{pct(report.netprofit_margin)}</Descriptions.Item>
        <Descriptions.Item label="ROE(加权)">{pct(report.roe_waa)}</Descriptions.Item>
        <Descriptions.Item label="每股收益">{toNumber(report.eps) === null ? '--' : Number(report.eps).toFixed(3)}</Descriptions.Item>
        <Descriptions.Item label="经营现金流净额">{formatChineseAmount(report.n_cashflow_act)}</Descriptions.Item>
      </Descriptions>
    )}
    {!report.is_annual && report.has_values && (
      <Text type="secondary" style={{ display: 'block', marginTop: 6, fontSize: 12 }}>
        非年报期为年初至今累计数（中报＝上半年、三季报＝前三季度），比率未年化。
      </Text>
    )}
  </div>
);

/**
 * K 线上点击研报/财报标记后弹出的侧栏。
 * event: { kind: 'research' | 'financial', tradeDate, items }
 */
const KlineEventDrawer = ({ event, onClose }) => {
  const isResearch = event?.kind === 'research';
  const title = !event
    ? ''
    : isResearch
      ? `${event.tradeDate} 研报（${event.items.length} 篇）`
      : `${event.tradeDate} 财报披露`;
  return (
    <Drawer open={!!event} onClose={onClose} title={title} width={520} destroyOnClose>
      {!event || !event.items.length ? <Empty /> : isResearch
        ? <ResearchList items={event.items} />
        : event.items.map(report => <FinancialSummary key={report.end_date} report={report} />)}
    </Drawer>
  );
};

export default KlineEventDrawer;
