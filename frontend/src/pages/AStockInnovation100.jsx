import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Empty,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  BarChartOutlined,
  HistoryOutlined,
  ReloadOutlined,
  RocketOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';

const { Title, Text } = Typography;

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

const rebalanceTypeMeta = {
  inception: { label: '初始建仓', color: 'purple' },
  annual_reconstitution: { label: '年度重构', color: 'blue' },
  quarterly_reweight: { label: '季度再平衡', color: 'cyan' },
};
const getRebalanceTypeMeta = (value) => rebalanceTypeMeta[value] || { label: value || '-', color: 'default' };

const AStockInnovation100 = () => {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [rebuildLoading, setRebuildLoading] = useState(false);
  const [job, setJob] = useState(null);
  const [startDate, setStartDate] = useState(dayjs('2020-01-01'));
  const [selectedRebalanceId, setSelectedRebalanceId] = useState(null);
  const pollingRef = useRef(null);

  useEffect(() => {
    fetchDetail();
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, []);

  const fetchDetail = async (rebalanceId = selectedRebalanceId) => {
    setLoading(true);
    try {
      const params = rebalanceId ? { rebalance_id: rebalanceId } : {};
      const { data } = await request.get('/api/a-stock-innovation100/detail', { params });
      setDetail(data);
      const nextSelectedId = data?.selected_rebalance?.id || null;
      setSelectedRebalanceId(nextSelectedId);
      return data;
    } catch (error) {
      message.error(formatErrorMessage(error, '加载A股创新100失败'));
      return null;
    } finally {
      setLoading(false);
    }
  };

  const pollJob = (taskId) => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    pollingRef.current = setInterval(async () => {
      try {
        const { data } = await request.get(`/api/a-stock-innovation100/jobs/${taskId}`);
        setJob(data);
        if (data.status === 'completed') {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
          setRebuildLoading(false);
          message.success('A股创新100回跑完成');
          await fetchDetail(data.result?.latest_rebalance_id || null);
        } else if (data.status === 'failed') {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
          setRebuildLoading(false);
          message.error(data.error || 'A股创新100回跑失败');
        }
      } catch (error) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
        setRebuildLoading(false);
        message.error(formatErrorMessage(error, '查询回跑任务失败'));
      }
    }, 2500);
  };

  const handleRebuild = async () => {
    setRebuildLoading(true);
    setJob({ status: 'queued', progress: 0, message: '任务已创建，等待执行' });
    try {
      const { data } = await request.post('/api/a-stock-innovation100/rebuild', {
        start_date: startDate ? startDate.format('YYYY-MM-DD') : '2020-01-01',
      });
      setJob(data);
      pollJob(data.task_id);
    } catch (error) {
      setRebuildLoading(false);
      message.error(formatErrorMessage(error, '启动A股创新100回跑失败'));
    }
  };

  const summary = detail?.summary || {};
  const rule = summary.rule_snapshot || {};
  const levels = detail?.levels || [];
  const selectedConstituents = detail?.selected_constituents || [];
  const selectedRebalance = detail?.selected_rebalance;

  const levelOption = useMemo(() => {
    if (!levels.length) return null;
    return {
      tooltip: {
        trigger: 'axis',
        valueFormatter: value => Number(value).toFixed(2),
      },
      legend: { top: 0 },
      grid: { top: 48, left: 64, right: 28, bottom: 48 },
      xAxis: { type: 'category', data: levels.map(item => item.date), boundaryGap: false },
      yAxis: [
        { type: 'value', name: '点位', scale: true },
        { type: 'value', name: '回撤', axisLabel: { formatter: '{value}%' } },
      ],
      dataZoom: [
        { type: 'inside' },
        { type: 'slider', height: 18, bottom: 12 },
      ],
      series: [
        {
          name: 'A股创新100',
          type: 'line',
          showSymbol: false,
          smooth: true,
          data: levels.map(item => item.level),
          lineStyle: { width: 2 },
        },
        {
          name: '回撤',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: false,
          areaStyle: {},
          data: levels.map(item => item.drawdown_pct),
        },
      ],
    };
  }, [levels]);

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
    { title: '权重', dataIndex: 'weight_pct', key: 'weight_pct', width: 100, render: value => formatPercent(value, 3), sorter: (a, b) => Number(a.weight_pct || 0) - Number(b.weight_pct || 0), defaultSortOrder: 'descend' },
    { title: '原始权重', dataIndex: 'raw_weight_pct', key: 'raw_weight_pct', width: 110, render: value => formatPercent(value, 3) },
    { title: '流通市值(万元)', dataIndex: 'circ_mv', key: 'circ_mv', width: 140, render: value => formatMoney(value, 0) },
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
    { title: '调仓日', dataIndex: 'rebalance_date', key: 'rebalance_date', width: 110, render: formatDate },
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
        return removals.slice(0, 6).map(item => <Tag key={item}>{item}</Tag>);
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

  const renderToolbar = () => (
    <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
      <Space direction="vertical" size={0}>
        <Title level={3} style={{ margin: 0 }}>A股创新100</Title>
        <Text type="secondary">沪深A股创新行业大市值指数，年度重构、季度再平衡、自由流通市值改良加权</Text>
      </Space>
      <Space wrap>
        <DatePicker value={startDate} onChange={setStartDate} allowClear={false} />
        <Button icon={<ReloadOutlined />} onClick={() => fetchDetail()} loading={loading}>
          刷新
        </Button>
        <Button type="primary" icon={<SyncOutlined />} onClick={handleRebuild} loading={rebuildLoading}>
          从所选日期回跑
        </Button>
      </Space>
    </Space>
  );

  const renderJob = () => {
    if (!job || (!rebuildLoading && job.status !== 'failed')) return null;
    return (
      <Alert
        type={job.status === 'failed' ? 'error' : 'info'}
        showIcon
        style={{ marginBottom: 16 }}
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

  const renderOverview = () => {
    if (!summary.has_data) {
      return (
        <Card loading={loading}>
          <Empty description="还没有A股创新100历史数据" />
        </Card>
      );
    }
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={16}>
        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}>
            <Card>
              <Statistic title="最新点位" value={summary.latest_level || 0} precision={2} />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic title="累计收益" value={summary.total_return_pct || 0} precision={2} suffix="%" />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic title="年化收益" value={summary.annualized_return_pct || 0} precision={2} suffix="%" />
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card>
              <Statistic title="最大回撤" value={summary.max_drawdown_pct || 0} precision={2} suffix="%" />
            </Card>
          </Col>
        </Row>
        <Card title={<span><BarChartOutlined /> 指数走势</span>} loading={loading}>
          {levelOption ? <ReactECharts option={levelOption} style={{ height: 460 }} /> : <Empty />}
        </Card>
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={12}>
            <Card title="年度表现" loading={loading}>
              {yearlyOption ? <ReactECharts option={yearlyOption} style={{ height: 300 }} /> : <Empty />}
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card title="编制规则">
              <Descriptions size="small" column={1}>
                <Descriptions.Item label="样本">{rule.universe || '-'}</Descriptions.Item>
                <Descriptions.Item label="选样">{rule.reconstitution || '-'}</Descriptions.Item>
                <Descriptions.Item label="调权">{rule.rebalance || '-'}</Descriptions.Item>
                <Descriptions.Item label="权重上限">
                  单票 {formatPercent(rule.max_single_weight_pct)} / 前五 {formatPercent(rule.top5_weight_cap_pct)} / 大权重合计 {formatPercent(rule.large_weight_cap_pct)}
                </Descriptions.Item>
                <Descriptions.Item label="流动性">
                  近{rule.liquidity_window || 60}日均成交额不低于 {formatMoney(rule.min_avg_amount_60d, 0)} 千元
                </Descriptions.Item>
              </Descriptions>
            </Card>
          </Col>
        </Row>
      </Space>
    );
  };

  return (
    <div style={{ padding: 24 }}>
      {renderToolbar()}
      {renderJob()}
      {summary.has_data && (
        <Descriptions bordered size="small" column={{ xs: 1, md: 4 }} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="区间">{formatDate(summary.start_date)} ~ {formatDate(summary.latest_date)}</Descriptions.Item>
          <Descriptions.Item label="成分数">{summary.constituent_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="再平衡次数">{summary.rebalances_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="最新再平衡">{formatDate(summary.latest_rebalance_date)}</Descriptions.Item>
          <Descriptions.Item label="年化波动">{formatPercent(summary.annualized_volatility_pct)}</Descriptions.Item>
          <Descriptions.Item label="Sharpe">{formatNumber(summary.sharpe_ratio, 3)}</Descriptions.Item>
          <Descriptions.Item label="指数代码">{summary.index_code}</Descriptions.Item>
          <Descriptions.Item label="最近生效">{formatDate(summary.latest_effective_date)}</Descriptions.Item>
        </Descriptions>
      )}
      {renderOverview()}
      <Space direction="vertical" style={{ width: '100%', marginTop: 16 }} size={16}>
        <Card
          title={<span><RocketOutlined /> 当前/选中期成分股</span>}
          extra={selectedRebalance ? `${formatDate(selectedRebalance.rebalance_date)} 调整，${formatDate(selectedRebalance.effective_date)} 生效` : null}
          loading={loading}
        >
          <Table
            rowKey="ts_code"
            columns={constituentColumns}
            dataSource={selectedConstituents}
            pagination={{ pageSize: 20 }}
            scroll={{ x: 1100 }}
          />
        </Card>
        <Card title={<span><HistoryOutlined /> 再平衡记录</span>} loading={loading}>
          <Table
            rowKey="id"
            columns={rebalanceColumns}
            dataSource={detail?.rebalances || []}
            pagination={{ pageSize: 12 }}
            scroll={{ x: 1200 }}
            rowClassName={record => (record.id === selectedRebalanceId ? 'ant-table-row-selected' : '')}
            onRow={(record) => ({
              onClick: async () => {
                setSelectedRebalanceId(record.id);
                await fetchDetail(record.id);
              },
              style: { cursor: 'pointer' },
            })}
          />
        </Card>
        <Card title="分年收益追溯" loading={loading}>
          <Table
            rowKey="year"
            columns={yearlyColumns}
            dataSource={detail?.yearly_returns || []}
            pagination={false}
            scroll={{ x: 760 }}
          />
        </Card>
      </Space>
    </div>
  );
};

export default AStockInnovation100;
