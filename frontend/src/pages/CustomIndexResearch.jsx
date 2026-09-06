import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Empty,
  Progress,
  Space,
  Tabs,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  BarChartOutlined,
  ReloadOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';
import './AStockInnovation100.css';

const { Text } = Typography;

const formatNumber = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toFixed(digits)
);
const formatMoney = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
);
const formatPercent = (value, digits = 2) => (
  value === null || value === undefined ? '-' : `${Number(value || 0).toFixed(digits)}%`
);
const formatDate = (value) => (value ? dayjs(value).format('YYYY-MM-DD') : '-');
const formatDateTime = (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-');
const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(item => {
      if (typeof item === 'string') return item;
      const field = Array.isArray(item.loc) ? item.loc.filter(part => part !== 'body').join('.') : '';
      return field ? `${field}: ${item.msg}` : item.msg;
    }).filter(Boolean).join('；') || fallback;
  }
  return typeof detail === 'object' ? JSON.stringify(detail) : String(detail);
};

/**
 * 自算指数（A创100、微盘400）的研究页。两条指数的接口形状、图表和表格完全一致，
 * 差异都收在 config 里：接口前缀、事件名、文案、编制规则卡片和成分股附加列。
 */
const CustomIndexResearch = ({ config, embedded = false }) => {
  const getRebalanceTypeMeta = useCallback(
    value => config.rebalanceTypeMeta[value] || { label: value || '-', color: 'default' },
    [config],
  );
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [rebuildLoading, setRebuildLoading] = useState(false);
  const [job, setJob] = useState(null);
  const [startDate, setStartDate] = useState(dayjs('2020-01-01'));
  const [selectedRebalanceId, setSelectedRebalanceId] = useState(null);
  const [headerActionHost, setHeaderActionHost] = useState(null);
  const jobTaskIdRef = useRef(null);
  const finishedJobRef = useRef(null);

  const fetchDetail = useCallback(async (rebalanceId = selectedRebalanceId) => {
    setLoading(true);
    try {
      const params = rebalanceId ? { rebalance_id: rebalanceId } : {};
      const { data } = await request.get(`${config.apiPrefix}/detail`, { params });
      setDetail(data);
      const nextSelectedId = data?.selected_rebalance?.id || null;
      setSelectedRebalanceId(nextSelectedId);
      return data;
    } catch (error) {
      message.error(formatErrorMessage(error, `加载${config.indexLabel}失败`));
      return null;
    } finally {
      setLoading(false);
    }
  }, [config, selectedRebalanceId]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  useEffect(() => {
    if (!embedded) return undefined;
    setHeaderActionHost(document.getElementById(config.actionHostId));
    return () => setHeaderActionHost(null);
  }, [config, embedded]);

  useEffect(() => {
    return subscribeBackendEvent(config.eventKey, async (data) => {
      if (data.task_id !== jobTaskIdRef.current) return;
      setJob(data);
      if (data.status === 'completed' && finishedJobRef.current !== data.task_id) {
        finishedJobRef.current = data.task_id;
        jobTaskIdRef.current = null;
        setRebuildLoading(false);
        message.success(`${config.indexLabel}回跑完成`);
        await fetchDetail(data.result?.latest_rebalance_id || null);
      } else if (data.status === 'failed' && finishedJobRef.current !== data.task_id) {
        finishedJobRef.current = data.task_id;
        jobTaskIdRef.current = null;
        setRebuildLoading(false);
        message.error(data.error || `${config.indexLabel}回跑失败`);
      }
    });
  }, [config, fetchDetail]);

  const handleRebuild = async () => {
    setRebuildLoading(true);
    setJob({ status: 'queued', progress: 0, message: '任务已创建，等待执行' });
    jobTaskIdRef.current = null;
    finishedJobRef.current = null;
    try {
      const { data } = await request.post(`${config.apiPrefix}/rebuild`, {
        start_date: startDate ? startDate.format('YYYY-MM-DD') : '2020-01-01',
      });
      jobTaskIdRef.current = data.task_id;
      setJob(data);
    } catch (error) {
      setRebuildLoading(false);
      message.error(formatErrorMessage(error, `启动${config.indexLabel}回跑失败`));
    }
  };

  const summary = detail?.summary || {};
  const rule = summary.rule_snapshot || {};
  const levels = useMemo(() => detail?.levels || [], [detail]);
  const benchmarkLevels = useMemo(() => detail?.benchmark_levels || [], [detail]);
  const selectedConstituents = detail?.selected_constituents || [];
  const selectedRebalance = detail?.selected_rebalance;
  const levelDateList = useMemo(() => levels.map(item => item.date), [levels]);
  const benchmarkSeries = useMemo(() => {
    return benchmarkLevels.map((benchmark, index) => {
      const levelMap = new Map((benchmark.levels || []).map(item => [item.date, item.level]));
      const colors = ['#1677ff', '#52c41a'];
      return {
        name: benchmark.name,
        type: 'line',
        showSymbol: false,
        smooth: true,
        connectNulls: true,
        data: levelDateList.map(date => (levelMap.has(date) ? levelMap.get(date) : null)),
        lineStyle: {
          width: 1.8,
          type: 'dashed',
          color: colors[index % colors.length],
        },
        itemStyle: {
          color: colors[index % colors.length],
        },
      };
    });
  }, [benchmarkLevels, levelDateList]);

  const levelOption = useMemo(() => {
    if (!levels.length) return null;
    const formatTooltipValue = (seriesName, value) => {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
      if (seriesName === '回撤') return formatPercent(value, 2);
      return formatNumber(value, 2);
    };
    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params) => {
          const items = Array.isArray(params) ? params : [params];
          if (!items.length) return '';
          const header = items[0]?.axisValueLabel || items[0]?.axisValue || '';
          const lines = items.map(item => {
            const marker = item.marker || '';
            const valueText = formatTooltipValue(item.seriesName, item.data);
            return `${marker}${item.seriesName}: ${valueText}`;
          });
          return [header, ...lines].join('<br/>');
        },
      },
      legend: { top: 0, type: 'scroll' },
      grid: { top: 48, left: 64, right: 28, bottom: 48 },
      xAxis: { type: 'category', data: levelDateList, boundaryGap: false },
      yAxis: [
        { type: 'value', name: '归一化点位', scale: true },
        { type: 'value', name: '回撤', axisLabel: { formatter: '{value}%' } },
      ],
      dataZoom: [
        { type: 'inside' },
        { type: 'slider', height: 18, bottom: 12 },
      ],
      series: [
        {
          name: config.indexLabel,
          type: 'line',
          showSymbol: false,
          smooth: true,
          data: levels.map(item => item.level),
          lineStyle: { width: 2 },
        },
        ...benchmarkSeries,
        {
          name: '回撤',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: false,
          lineStyle: { width: 1.4, color: '#7cc36b' },
          itemStyle: { color: '#7cc36b' },
          areaStyle: { color: 'rgba(124, 195, 107, 0.16)' },
          data: levels.map(item => item.drawdown_pct),
        },
      ],
    };
  }, [benchmarkSeries, config, levelDateList, levels]);

  const yearlyOption = useMemo(() => {
    const yearly = detail?.yearly_returns || [];
    if (!yearly.length) return null;
    return {
      tooltip: { trigger: 'axis', valueFormatter: value => `${Number(value).toFixed(2)}%` },
      grid: { top: 24, left: 54, right: 24, bottom: 36 },
      xAxis: { type: 'category', data: yearly.map(item => String(item.year)) },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
      series: [
        {
          name: '年度收益',
          type: 'bar',
          data: yearly.map(item => item.return_pct),
          itemStyle: {
            color: params => (Number(params.value || 0) >= 0 ? '#1677ff' : '#cf1322'),
          },
        },
      ],
    };
  }, [detail]);

  const constituentColumns = [
    { title: '权重排名', dataIndex: 'rank', key: 'rank', width: 90 },
    {
      title: '股票',
      key: 'stock',
      width: 170,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{record.name || record.ts_code}</Text>
          <Text type="secondary">{record.ts_code}</Text>
        </Space>
      ),
    },
    { title: '行业', dataIndex: 'industry', key: 'industry', width: 110, render: value => <Tag>{value}</Tag> },
    { title: '权重', dataIndex: 'weight_pct', key: 'weight_pct', width: 100, render: value => formatPercent(value, 3), sorter: (a, b) => Number(a.weight_pct || 0) - Number(b.weight_pct || 0) },
    ...config.constituentValueColumns,
    { title: '60日均成交额(千元)', dataIndex: 'avg_amount_60d', key: 'avg_amount_60d', width: 160, render: value => formatMoney(value, 0) },
    {
      title: '状态',
      dataIndex: 'action',
      key: 'action',
      width: 90,
      render: value => <Tag color={value === 'added' ? 'green' : 'default'}>{value === 'added' ? '新增' : '保留'}</Tag>,
    },
  ];

  const rebalanceColumns = [
    { title: '调样日', dataIndex: 'rebalance_date', key: 'rebalance_date', width: 110, render: formatDate },
    { title: '生效日', dataIndex: 'effective_date', key: 'effective_date', width: 110, render: formatDate },
    {
      title: '类型',
      dataIndex: 'rebalance_type',
      key: 'rebalance_type',
      width: 120,
      render: value => {
        const meta = getRebalanceTypeMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    { title: '成分数', dataIndex: 'constituent_count', key: 'constituent_count', width: 90 },
    { title: '换手', dataIndex: 'turnover_pct', key: 'turnover_pct', width: 100, render: value => formatPercent(value) },
    {
      title: '新增',
      dataIndex: 'additions',
      key: 'additions',
      width: 360,
      render: value => {
        const additions = value || [];
        if (!additions.length) return '-';
        return additions.slice(0, 6).map(item => (
          <Tag key={item.ts_code} color="green">{item.name || item.ts_code}</Tag>
        ));
      },
    },
    {
      title: '移出',
      dataIndex: 'removals',
      key: 'removals',
      width: 280,
      render: value => {
        const removals = value || [];
        if (!removals.length) return '-';
        return removals.slice(0, 6).map(item => {
          const tsCode = typeof item === 'string' ? item : item?.ts_code;
          const name = typeof item === 'string' ? null : item?.name;
          return <Tag key={tsCode}>{name || tsCode}</Tag>;
        });
      },
    },
  ];

  const yearlyColumns = [
    { title: '年份', dataIndex: 'year', key: 'year', width: 90 },
    { title: '起始日', dataIndex: 'start_date', key: 'start_date', width: 110, render: formatDate },
    { title: '结束日', dataIndex: 'end_date', key: 'end_date', width: 110, render: formatDate },
    { title: '年度收益', dataIndex: 'return_pct', key: 'return_pct', width: 110, render: value => formatPercent(value) },
    { title: '年内最大回撤', dataIndex: 'max_drawdown_pct', key: 'max_drawdown_pct', width: 130, render: value => formatPercent(value) },
    { title: '期末点位', dataIndex: 'end_level', key: 'end_level', width: 120, render: value => formatNumber(value, 2) },
  ];

  const metricItems = [
    { label: '最新点位', value: formatNumber(summary.latest_level, 2) },
    { label: '累计收益', value: formatPercent(summary.total_return_pct, 2), tone: Number(summary.total_return_pct || 0) >= 0 ? 'positive' : 'negative' },
    { label: '年化收益', value: formatPercent(summary.annualized_return_pct, 2), tone: Number(summary.annualized_return_pct || 0) >= 0 ? 'positive' : 'negative' },
    { label: '最大回撤', value: formatPercent(summary.max_drawdown_pct, 2), tone: 'negative' },
    { label: '年化波动', value: formatPercent(summary.annualized_volatility_pct, 2) },
    { label: 'Sharpe', value: formatNumber(summary.sharpe_ratio, 3) },
    { label: '成分数', value: summary.constituent_count ?? '-' },
    { label: config.rebalanceNoun, value: summary.rebalances_count ?? '-' },
  ];

  const selectedRebalanceTypeMeta = getRebalanceTypeMeta(selectedRebalance?.rebalance_type);
  const metaItems = [
    { label: '指数代码', value: summary.index_code || '-' },
    { label: '区间', value: `${formatDate(summary.start_date)} ~ ${formatDate(summary.latest_date)}` },
    { label: `最新${config.rebalanceNoun}`, value: formatDate(summary.latest_rebalance_date) },
    { label: '最近生效', value: formatDate(summary.latest_effective_date) },
    {
      label: `${config.rebalanceNoun}类型`,
      value: selectedRebalance ? selectedRebalanceTypeMeta.label : '-',
      tagColor: selectedRebalance ? selectedRebalanceTypeMeta.color : null,
    },
    { label: '换手', value: selectedRebalance ? formatPercent(selectedRebalance.turnover_pct) : '-' },
  ];

  const renderActions = () => (
    <div className="a100-actions">
      <DatePicker value={startDate} onChange={setStartDate} allowClear={false} />
      <Button icon={<ReloadOutlined />} onClick={() => fetchDetail()} loading={loading}>
        刷新
      </Button>
      <Button type="primary" icon={<SyncOutlined />} onClick={handleRebuild} loading={rebuildLoading}>
        从所选日期回跑
      </Button>
    </div>
  );

  const renderJob = () => {
    if (!job || (!rebuildLoading && job.status !== 'failed')) return null;
    return (
      <Alert
        type={job.status === 'failed' ? 'error' : 'info'}
        showIcon
        className="a100-job"
        message={job.message || job.status}
        description={
          <Space direction="vertical" style={{ width: '100%' }}>
            <Progress percent={Number(job.progress || 0)} status={job.status === 'failed' ? 'exception' : 'active'} />
            <Text type="secondary">
              {job.status || '-'} / {formatDateTime(job.updated_at)}
            </Text>
          </Space>
        }
      />
    );
  };

  const renderMetricStrip = () => (
    <div className="a100-metric-strip">
      {metricItems.map(item => (
        <div className={`a100-metric a100-metric--${item.tone || 'neutral'}`} key={item.label}>
          <span>{item.label}</span>
          <strong>{item.value}</strong>
        </div>
      ))}
    </div>
  );

  const renderMetaStrip = () => (
    <div className="a100-meta-strip">
      {metaItems.map(item => (
        <div className="a100-meta-item" key={item.label}>
          <span>{item.label}</span>
          <strong>
            {item.tagColor ? <Tag color={item.tagColor} className="a100-meta-tag">{item.value}</Tag> : item.value}
          </strong>
        </div>
      ))}
    </div>
  );

  const renderRuleCard = () => (
    <Card title="编制规则" className="a100-side-card">
      <Descriptions size="small" column={1}>
        {config.ruleItems(rule).map(item => (
          <Descriptions.Item label={item.label} key={item.label}>{item.value}</Descriptions.Item>
        ))}
      </Descriptions>
    </Card>
  );

  const renderWorkbench = () => {
    if (!summary.has_data) {
      return (
        <Card loading={loading} className="a100-empty-card">
          <Empty description={`还没有${config.indexLabel}历史数据`} />
        </Card>
      );
    }
    return (
      <>
        {renderMetaStrip()}
        {renderMetricStrip()}
        <div className="a100-workbench">
          <Card title={<span><BarChartOutlined /> 指数走势</span>} loading={loading} className="a100-chart-card">
            {levelOption ? <ReactECharts option={levelOption} style={{ height: 'var(--a100-chart-height)' }} /> : <Empty />}
          </Card>
          <div className="a100-side-rail">
            <Card title="年度表现" loading={loading} className="a100-side-card">
              {yearlyOption ? <ReactECharts option={yearlyOption} style={{ height: 220 }} /> : <Empty />}
            </Card>
            {renderRuleCard()}
          </div>
        </div>
      </>
    );
  };

  const tableItems = [
    {
      key: 'constituents',
      label: `成分股 ${selectedConstituents.length || ''}`,
      children: (
        <Table
          rowKey="ts_code"
          columns={constituentColumns}
          dataSource={selectedConstituents}
          pagination={{ defaultPageSize: 25, showSizeChanger: true }}
          scroll={{ x: 1100, y: 560 }}
          size="small"
        />
      ),
    },
    {
      key: 'rebalances',
      label: `${config.rebalanceNoun}记录`,
      children: (
        <Table
          rowKey="id"
          columns={rebalanceColumns}
          dataSource={detail?.rebalances || []}
          pagination={{ defaultPageSize: 12, showSizeChanger: true }}
          scroll={{ x: 1200, y: 560 }}
          size="small"
          rowClassName={record => (record.id === selectedRebalanceId ? 'ant-table-row-selected' : '')}
          onRow={(record) => ({
            onClick: async () => {
              setSelectedRebalanceId(record.id);
              await fetchDetail(record.id);
            },
            style: { cursor: 'pointer' },
          })}
        />
      ),
    },
    {
      key: 'yearly',
      label: '分年收益',
      children: (
        <Table
          rowKey="year"
          columns={yearlyColumns}
          dataSource={detail?.yearly_returns || []}
          pagination={false}
          scroll={{ x: 760, y: 560 }}
          size="small"
        />
      ),
    },
  ];

  return (
    <div className={`a100-page${embedded ? ' is-embedded' : ''}`}>
      {embedded && headerActionHost ? createPortal(renderActions(), headerActionHost) : null}
      {!embedded ? <div className="a100-toolbar">{renderActions()}</div> : null}
      {renderJob()}
      {renderWorkbench()}
      {summary.has_data && (
        <Card className="a100-data-card" loading={loading}>
          <Tabs items={tableItems} />
        </Card>
      )}
    </div>
  );
};

export default CustomIndexResearch;
