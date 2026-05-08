import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Form,
  InputNumber,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  BarChartOutlined,
  ExperimentOutlined,
  FireOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';
import './FactorLab.css';

const { Text, Title } = Typography;

const DEFAULT_FORM_VALUES = {
  pool: 'SPY_QQQ',
  factor: 'risk_adjusted_momentum',
  bucket_count: 10,
  start_date: dayjs('2020-01-02'),
  end_date: null,
  heatmap_windows: [20, 60, 120],
  heatmap_forward_windows: [5, 20, 60],
};

const DEFAULT_MIN_LISTING_DAYS = 365;
const DEFAULT_MOMENTUM_WEIGHTS = { 20: 0.05, 60: 0.2, 120: 0.75 };

const numberFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
};

const percentFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Number(value).toFixed(2)}%`;
};

const icFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toFixed(4);
};

const getErrorMessage = (error, fallback) => (
  error?.response?.data?.detail
  || error?.response?.data?.message
  || error?.message
  || fallback
);

const normalizeDefaultRequest = (payload = {}) => ({
  ...DEFAULT_FORM_VALUES,
  ...payload,
  start_date: payload.start_date ? dayjs(payload.start_date) : DEFAULT_FORM_VALUES.start_date,
  end_date: payload.end_date ? dayjs(payload.end_date) : null,
  heatmap_windows: payload.heatmap_windows || payload.windows || DEFAULT_FORM_VALUES.heatmap_windows,
  heatmap_forward_windows: payload.heatmap_forward_windows || DEFAULT_FORM_VALUES.heatmap_forward_windows,
});

const buildFactorSelectOptions = factors => {
  const groups = {};
  (factors || []).forEach(factor => {
    const group = factor.group || '因子';
    if (!groups[group]) groups[group] = [];
    groups[group].push({
      label: factor.label,
      value: factor.key,
    });
  });
  return Object.entries(groups).map(([label, options]) => ({ label, options }));
};

const normalizeNumberArray = (value, fallback) => {
  const items = Array.isArray(value) ? value : [];
  const normalized = [...new Set(items.map(item => Number(item)).filter(item => Number.isFinite(item)))];
  return normalized.length ? normalized : fallback;
};

const buildAnalyzePayload = values => {
  const heatmapWindows = normalizeNumberArray(values.heatmap_windows, DEFAULT_FORM_VALUES.heatmap_windows);
  const heatmapForwardWindows = normalizeNumberArray(values.heatmap_forward_windows, DEFAULT_FORM_VALUES.heatmap_forward_windows);
  return {
    pool: values.pool,
    factor: values.factor,
    bucket_count: values.bucket_count,
    start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : DEFAULT_FORM_VALUES.start_date.format('YYYY-MM-DD'),
    end_date: values.end_date ? values.end_date.format('YYYY-MM-DD') : null,
    momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
    min_listing_days: DEFAULT_MIN_LISTING_DAYS,
    include_heatmap: true,
    heatmap_windows: heatmapWindows,
    heatmap_forward_windows: heatmapForwardWindows,
  };
};

const getBucketChartOption = rows => {
  const buckets = rows.map(item => `B${item.bucket}`);
  return {
    grid: { top: 32, right: 48, bottom: 36, left: 48 },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const items = Array.isArray(params) ? params : [params];
        return items.map(item => `${item.marker}${item.seriesName}: ${numberFormatter(item.value)}`).join('<br/>');
      },
    },
    legend: { top: 0 },
    xAxis: { type: 'category', data: buckets, axisTick: { alignWithLabel: true } },
    yAxis: [
      { type: 'value', name: '收益%', splitLine: { lineStyle: { color: '#edf1f7' } } },
      { type: 'value', name: '胜率%', min: 0, max: 100 },
    ],
    series: [
      {
        name: '平均收益',
        type: 'bar',
        data: rows.map(item => item.avg_return_pct),
        itemStyle: { color: '#2477b3' },
      },
      {
        name: '超额收益',
        type: 'bar',
        data: rows.map(item => item.avg_excess_return_pct),
        itemStyle: { color: '#d95f59' },
      },
      {
        name: '胜率',
        type: 'line',
        yAxisIndex: 1,
        data: rows.map(item => item.win_rate_pct),
        symbolSize: 6,
        lineStyle: { width: 2, color: '#2f9e6d' },
        itemStyle: { color: '#2f9e6d' },
      },
    ],
  };
};

const getIcOption = rows => ({
  grid: { top: 28, right: 24, bottom: 36, left: 52 },
  tooltip: {
    trigger: 'axis',
    formatter: params => {
      const item = Array.isArray(params) ? params[0] : params;
      return `${item.axisValue}<br/>${item.marker}Rank IC: ${icFormatter(item.value)}`;
    },
  },
  xAxis: {
    type: 'category',
    data: rows.map(item => item.trade_date),
    axisLabel: { hideOverlap: true },
  },
  yAxis: { type: 'value', name: 'IC', splitLine: { lineStyle: { color: '#edf1f7' } } },
  series: [
    {
      name: 'Rank IC',
      type: 'line',
      showSymbol: false,
      data: rows.map(item => item.rank_ic),
      lineStyle: { color: '#d95f59', width: 1.8 },
    },
  ],
});

const isSameCombo = (combo, row) => (
  combo
  && Number(combo.window) === Number(row.window)
  && Number(combo.forward_window) === Number(row.forward_window)
);

const getHeatmapValue = item => (
  item?.heatmap_value_pct
  ?? item?.non_overlap_annualized_top_minus_bottom_pct
  ?? item?.annualized_top_minus_bottom_avg_return_pct
  ?? item?.top_minus_bottom_avg_return_pct
);

const getHeatmapOption = (rows, selectedCombo) => {
  const validRows = (rows || []).filter(item => getHeatmapValue(item) !== null && getHeatmapValue(item) !== undefined);
  const windows = [...new Set(validRows.map(item => item.window))].sort((a, b) => a - b);
  const forwards = [...new Set(validRows.map(item => item.forward_window))].sort((a, b) => a - b);
  const values = validRows.map(getHeatmapValue);
  const min = values.length ? Math.min(...values) : -1;
  const max = values.length ? Math.max(...values) : 1;
  return {
    grid: { top: 36, right: 88, bottom: 40, left: 72 },
    tooltip: {
      position: 'top',
      formatter: params => {
        const item = validRows.find(row => (
          row.forward_window === forwards[params.value[0]] && row.window === windows[params.value[1]]
        ));
        if (!item) return '';
        return [
          `窗口: ${item.window}`,
          `T+${item.forward_window}`,
          `非重叠年化多空差: ${percentFormatter(getHeatmapValue(item))}`,
          `重叠年化多空差: ${percentFormatter(item.annualized_top_minus_bottom_avg_return_pct)}`,
          `T+n多空差: ${percentFormatter(item.top_minus_bottom_avg_return_pct)}`,
          `样本: ${numberFormatter(item.samples)}`,
        ].join('<br/>');
      },
    },
    xAxis: { type: 'category', data: forwards.map(item => `T+${item}`), splitArea: { show: true } },
    yAxis: { type: 'category', data: windows.map(item => `${item}日`), splitArea: { show: true } },
    visualMap: {
      min,
      max,
      calculable: true,
      orient: 'vertical',
      right: 8,
      top: 40,
      inRange: { color: ['#2f70b7', '#f6f7f9', '#cb3a31'] },
    },
    series: [
      {
        name: '非重叠年化多空差',
        type: 'heatmap',
        data: validRows.map(item => ({
          value: [
            forwards.indexOf(item.forward_window),
            windows.indexOf(item.window),
            getHeatmapValue(item),
          ],
          itemStyle: isSameCombo(selectedCombo, item)
            ? { borderColor: '#111827', borderWidth: 3 }
            : undefined,
        })),
        label: {
          show: true,
          formatter: params => `${Number(params.value[2]).toFixed(2)}%`,
        },
        emphasis: {
          itemStyle: {
            shadowBlur: 8,
            shadowColor: 'rgba(0, 0, 0, 0.18)',
          },
        },
      },
    ],
  };
};

const bucketColumns = [
  { title: '桶', dataIndex: 'bucket', width: 64, fixed: 'left' },
  { title: '样本', dataIndex: 'samples', align: 'right', render: numberFormatter },
  { title: '日期数', dataIndex: 'trade_dates', align: 'right', render: numberFormatter },
  { title: '因子均值', dataIndex: 'avg_factor_value', align: 'right', render: icFormatter },
  { title: '平均收益', dataIndex: 'avg_return_pct', align: 'right', render: percentFormatter },
  { title: '超额收益', dataIndex: 'avg_excess_return_pct', align: 'right', render: percentFormatter },
  { title: '胜率', dataIndex: 'win_rate_pct', align: 'right', render: percentFormatter },
  { title: '超额胜率', dataIndex: 'excess_win_rate_pct', align: 'right', render: percentFormatter },
];

const nonOverlapColumns = [
  { title: 'Offset', dataIndex: 'offset', width: 80, fixed: 'left' },
  { title: '期数', dataIndex: 'periods', align: 'right', render: numberFormatter },
  { title: '开始', dataIndex: 'start_date', width: 112 },
  { title: '结束', dataIndex: 'end_date', width: 112 },
  { title: '平均多空', dataIndex: 'avg_top_minus_bottom_return_pct', align: 'right', render: percentFormatter },
  { title: '年化多空', dataIndex: 'annualized_top_minus_bottom_return_pct', align: 'right', render: percentFormatter },
  { title: '正收益期', dataIndex: 'positive_period_rate_pct', align: 'right', render: percentFormatter },
  { title: 't-stat', dataIndex: 't_stat', align: 'right', render: icFormatter },
];

const yearlyColumns = [
  { title: '年份', dataIndex: 'year', width: 76, fixed: 'left' },
  { title: '样本', dataIndex: 'samples', align: 'right', render: numberFormatter },
  { title: '日期数', dataIndex: 'trade_dates', align: 'right', render: numberFormatter },
  { title: '平均多空', dataIndex: 'avg_top_minus_bottom_return_pct', align: 'right', render: percentFormatter },
  { title: '年化多空', dataIndex: 'annualized_top_minus_bottom_return_pct', align: 'right', render: percentFormatter },
  { title: '非重叠年化', dataIndex: 'non_overlap_annualized_median_pct', align: 'right', render: percentFormatter },
  { title: 'Rank IC', dataIndex: 'avg_rank_ic', align: 'right', render: icFormatter },
  { title: 'IC为正', dataIndex: 'positive_ic_rate_pct', align: 'right', render: percentFormatter },
  { title: '多空为正', dataIndex: 'positive_spread_rate_pct', align: 'right', render: percentFormatter },
];

const FactorLab = () => {
  const [form] = Form.useForm();
  const [options, setOptions] = useState(null);
  const [result, setResult] = useState(null);
  const [selectedCombo, setSelectedCombo] = useState(null);
  const [loadingOptions, setLoadingOptions] = useState(false);
  const [running, setRunning] = useState(false);

  const selectedFactorKey = Form.useWatch('factor', form);
  const selectedFactor = useMemo(() => (
    (options?.factors || []).find(item => item.key === selectedFactorKey)
  ), [options, selectedFactorKey]);

  const loadOptions = useCallback(async () => {
    setLoadingOptions(true);
    try {
      const { data } = await request.get('/api/factor-lab/options');
      setOptions(data);
      form.setFieldsValue(normalizeDefaultRequest(data.default_request));
    } catch (error) {
      message.error(getErrorMessage(error, '加载因子实验室配置失败'));
    } finally {
      setLoadingOptions(false);
    }
  }, [form]);

  useEffect(() => {
    loadOptions();
  }, [loadOptions]);

  const handleFactorChange = value => {
    const factor = (options?.factors || []).find(item => item.key === value);
    if (!factor) return;
    const nextWindows = factor.supports_windows ? factor.default_windows : DEFAULT_FORM_VALUES.heatmap_windows;
    form.setFieldsValue({
      heatmap_windows: factor.supports_windows ? nextWindows : DEFAULT_FORM_VALUES.heatmap_windows,
    });
  };

  const runAnalysis = async () => {
    const values = await form.validateFields();
    setRunning(true);
    try {
      const payload = buildAnalyzePayload(values);
      const { data } = await request.post('/api/factor-lab/analyze', payload, { timeout: 300000 });
      setResult(data);
      setSelectedCombo(data?.metadata?.selected_combo || null);
      message.success('因子分析完成');
    } catch (error) {
      message.error(getErrorMessage(error, '因子分析失败'));
    } finally {
      setRunning(false);
    }
  };

  const factorSelectOptions = useMemo(() => buildFactorSelectOptions(options?.factors), [options]);
  const windowOptions = useMemo(() => (options?.windows || [20, 60, 120]).map(item => ({
    label: `${item}日`,
    value: item,
  })), [options]);
  const forwardOptions = useMemo(() => {
    const values = [...new Set([...(options?.forward_windows || [5, 20, 60]), 10, 120])].sort((a, b) => a - b);
    return values.map(item => ({ label: `T+${item}`, value: item }));
  }, [options]);

  const summary = result?.summary || {};
  const metadata = result?.metadata || {};
  const bucketRows = result?.bucket_returns || [];
  const icRows = result?.rank_ic_series || [];
  const heatmapRows = result?.parameter_heatmap || [];
  const nonOverlapSummary = result?.non_overlapping_summary || {};
  const nonOverlapRows = result?.non_overlapping_offsets || [];
  const yearlyRows = result?.yearly_stability || [];
  const selectedComboText = selectedCombo
    ? `${selectedCombo.window}日 × T+${selectedCombo.forward_window}`
    : '-';

  return (
    <div className="factor-lab-page">
      <div className="factor-lab-header">
        <div>
          <Title level={3}>因子研究平台</Title>
          <Space size={8} wrap>
            <Tag color="blue">Polars</Tag>
            {metadata.factor?.label && <Tag>{metadata.factor.label}</Tag>}
            {metadata.pool_label && <Tag>{metadata.pool_label}</Tag>}
          </Space>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadOptions} loading={loadingOptions} />
          <Button type="primary" icon={<PlayCircleOutlined />} onClick={runAnalysis} loading={running}>
            运行
          </Button>
        </Space>
      </div>

      <Card className="factor-lab-control-card" bordered={false}>
        <Spin spinning={loadingOptions}>
          <Form form={form} layout="vertical" initialValues={DEFAULT_FORM_VALUES}>
            <Row gutter={[12, 8]}>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="pool" label="股票池" rules={[{ required: true }]}>
                  <Select options={(options?.pools || []).map(item => ({ label: item.label, value: item.key }))} />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={8} lg={6}>
                <Form.Item name="factor" label="因子" rules={[{ required: true }]}>
                  <Select options={factorSelectOptions} onChange={handleFactorChange} />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="bucket_count" label="分桶" rules={[{ required: true }]}>
                  <InputNumber min={2} max={20} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="start_date" label="开始日期" rules={[{ required: true }]}>
                  <DatePicker className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="end_date" label="结束日期">
                  <DatePicker className="factor-lab-full" allowClear />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={8} lg={5}>
                <Form.Item name="heatmap_windows" label="滑动窗口" rules={[{ required: true }]}>
                  <Select
                    mode="multiple"
                    maxTagCount="responsive"
                    options={windowOptions}
                    disabled={selectedFactor && !selectedFactor.supports_windows}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={8} lg={5}>
                <Form.Item name="heatmap_forward_windows" label="收益窗口" rules={[{ required: true }]}>
                  <Select mode="multiple" maxTagCount="responsive" options={forwardOptions} />
                </Form.Item>
              </Col>
            </Row>

            {selectedFactor?.description && (
              <div className="factor-lab-factor-note">
                <Text type="secondary">{selectedFactor.description}</Text>
              </div>
            )}
          </Form>
        </Spin>
      </Card>

      {!result && (
        <div className="factor-lab-empty">
          <ExperimentOutlined />
          <Text type="secondary">选择参数后运行分析</Text>
        </div>
      )}

      {result && (
        <Spin spinning={running}>
          <Card
            className="factor-lab-heatmap-card"
            title={<Space><FireOutlined />参数热力图（非重叠年化多空差）</Space>}
            extra={<Tag color="blue">当前：{selectedComboText}</Tag>}
            bordered={false}
          >
            {heatmapRows.length ? (
              <ReactECharts
                option={getHeatmapOption(heatmapRows, selectedCombo)}
                style={{ height: 360 }}
              />
            ) : <Empty />}
          </Card>

          <div className="factor-lab-metrics">
            <Statistic title="样本" value={summary.samples} formatter={numberFormatter} />
            <Statistic title="交易日" value={summary.trade_dates} formatter={numberFormatter} />
            <Statistic title="Rank IC" value={summary.rank_ic_mean} formatter={icFormatter} />
            <Statistic title="ICIR" value={summary.icir} formatter={icFormatter} />
            <Statistic title="最高桶收益" value={summary.top_bucket_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="T+n多空差" value={summary.top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="年化多空差" value={summary.annualized_top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="非重叠年化" value={nonOverlapSummary.annualized_median_pct} formatter={percentFormatter} />
            <Statistic title="单调性" value={summary.monotonicity_spearman} formatter={icFormatter} />
            <Statistic title="相邻命中" value={summary.adjacent_hit_rate_pct} formatter={percentFormatter} />
            <Statistic
              title="正收益年份"
              value={summary.positive_spread_years}
              suffix={summary.total_years ? `/${summary.total_years}` : ''}
              formatter={numberFormatter}
            />
          </div>

          <Alert
            className="factor-lab-meta"
            type="info"
            showIcon
            message={(
              <Space size={12} wrap>
                <span>{metadata.start_date} 至 {metadata.end_date}</span>
                <span>{metadata.universe_symbols} 只股票</span>
                {metadata.min_listing_days !== undefined && <span>上市满 {metadata.min_listing_days} 天</span>}
                <span>{metadata.price_rows?.toLocaleString?.('zh-CN') || metadata.price_rows} 行行情</span>
                <span>{selectedComboText}</span>
                <span>{numberFormatter(summary.elapsed_ms)} ms</span>
              </Space>
            )}
          />

          <Row gutter={[12, 12]}>
            <Col xs={24} xl={12}>
              <Card title={<Space><BarChartOutlined />分桶收益</Space>} bordered={false}>
                {bucketRows.length ? <ReactECharts option={getBucketChartOption(bucketRows)} style={{ height: 340 }} /> : <Empty />}
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title={<Space><ExperimentOutlined />Rank IC</Space>} bordered={false}>
                {icRows.length ? <ReactECharts option={getIcOption(icRows)} style={{ height: 340 }} /> : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24} xl={12}>
              <Card title="非重叠统计" bordered={false}>
                {nonOverlapRows.length ? (
                  <>
                    <div className="factor-lab-compact-stats">
                      <span>Offset {numberFormatter(nonOverlapSummary.offsets)}</span>
                      <span>总期数 {numberFormatter(nonOverlapSummary.total_periods)}</span>
                      <span>年化中位 {percentFormatter(nonOverlapSummary.annualized_median_pct)}</span>
                      <span>最好 {percentFormatter(nonOverlapSummary.best_offset_annualized_pct)}</span>
                      <span>最差 {percentFormatter(nonOverlapSummary.worst_offset_annualized_pct)}</span>
                    </div>
                    <Table
                      rowKey="offset"
                      size="small"
                      columns={nonOverlapColumns}
                      dataSource={nonOverlapRows}
                      pagination={{ defaultPageSize: 8, showSizeChanger: true, pageSizeOptions: [8, 20, 50] }}
                      scroll={{ x: 880 }}
                    />
                  </>
                ) : <Empty />}
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="年度稳定性" bordered={false}>
                {yearlyRows.length ? (
                  <Table
                    rowKey="year"
                    size="small"
                    columns={yearlyColumns}
                    dataSource={yearlyRows}
                    pagination={false}
                    scroll={{ x: 980 }}
                  />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title="分桶明细" bordered={false}>
                <Table
                  rowKey="bucket"
                  size="small"
                  columns={bucketColumns}
                  dataSource={bucketRows}
                  pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 20, 50] }}
                  scroll={{ x: 920 }}
                />
              </Card>
            </Col>
          </Row>
        </Spin>
      )}
    </div>
  );
};

export default FactorLab;
