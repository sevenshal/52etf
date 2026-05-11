import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Form,
  Input,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tabs,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  BarChartOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  FilterOutlined,
  FireOutlined,
  PlusOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';
import DatabaseManager from './DatabaseManager';
import './FactorLab.css';

const { Text } = Typography;
const getLastYearStartDate = () => dayjs().subtract(1, 'year').startOf('year');

const DEFAULT_FORM_VALUES = {
  pool: 'QQQ',
  factor: 'risk_adjusted_momentum',
  bucket_count: 10,
  start_date: dayjs('2020-01-02'),
  end_date: null,
  neutralization: 'none',
  standardization: 'zscore',
  oos_start_date: getLastYearStartDate(),
  heatmap_metric: 'non_overlap_annualized_median_pct',
  heatmap_windows: [20, 60, 120],
  heatmap_forward_windows: [5, 20, 60],
  momentum_weights: { 20: 0.05, 60: 0.2, 120: 0.75 },
};

const DEFAULT_COMPOSITE_VALUES = {
  pool: 'QQQ',
  bucket_count: 10,
  start_date: dayjs('2020-01-02'),
  end_date: null,
  oos_start_date: getLastYearStartDate(),
  forward_window: 20,
  legs: [
    {
      factor: 'risk_adjusted_momentum',
      window: 'mixed',
      weight: 0.7,
      neutralization: 'none',
      standardization: 'rank_percentile',
      momentum_weights: { 20: 0.05, 60: 0.2, 120: 0.75 },
    },
    {
      factor: 'volume_z',
      window: 20,
      weight: 0.3,
      neutralization: 'none',
      standardization: 'rank_percentile',
      momentum_weights: { 20: 0.05, 60: 0.2, 120: 0.75 },
    },
  ],
};

const DEFAULT_BACKTEST_VALUES = {
  pool: 'QQQ',
  start_date: dayjs('2020-01-02'),
  end_date: null,
  oos_start_date: getLastYearStartDate(),
  initial_capital: 100000,
  max_positions: 7,
  sell_rank_multiplier: 2,
  rebalance_frequency: 'weekly',
  commission_pct: 0.03,
  slippage_pct: 0.02,
  lot_size: 1,
  min_listing_days: 365,
  legs: [
    {
      factor: 'risk_adjusted_momentum',
      window: 'mixed',
      weight: 0.6,
      neutralization: 'none',
      standardization: 'rank_percentile',
      momentum_weights: { 20: 0.05, 60: 0.2, 120: 0.75 },
    },
    {
      factor: 'index_weight',
      window: 20,
      weight: 0.4,
      neutralization: 'none',
      standardization: 'rank_percentile',
      momentum_weights: { 20: 0.05, 60: 0.2, 120: 0.75 },
    },
  ],
};

const DEFAULT_TIMING_VALUES = {
  target_symbol: 'SOXL.US',
  fear_symbol: 'CNN*.US',
  ma_window: 1,
  bucket_count: 10,
  start_date: dayjs('2020-01-02'),
  end_date: null,
  forward_window: 20,
  heatmap_metric: 'annualized_low_minus_high_avg_return_pct',
  heatmap_forward_windows: [5, 20, 60],
  heatmap_ma_windows: [1, 5, 20],
};

const DEFAULT_HEATMAP_METRIC = 'non_overlap_annualized_median_pct';
const DEFAULT_TIMING_HEATMAP_METRIC = 'annualized_low_minus_high_avg_return_pct';
const DEFAULT_TIMING_HEATMAP_METRICS = [
  { key: 'annualized_low_minus_high_avg_return_pct', label: '年化低-高桶差', kind: 'percent' },
  { key: 'low_minus_high_avg_return_pct', label: 'T+n 低-高桶差', kind: 'percent' },
  { key: 'annualized_top_minus_bottom_avg_return_pct', label: '年化高-低桶差', kind: 'percent' },
  { key: 'top_minus_bottom_avg_return_pct', label: 'T+n 高-低桶差', kind: 'percent' },
  { key: 'rank_ic_mean', label: '时间序列 IC', kind: 'ic' },
  { key: 'rank_ic_t_stat', label: 'IC t-stat', kind: 'ic' },
  { key: 'monotonicity_spearman', label: '单调性 Spearman', kind: 'ic' },
  { key: 'adjacent_hit_rate_pct', label: '相邻命中率', kind: 'percent' },
];
const DEFAULT_MIN_LISTING_DAYS = 365;
const MIXED_WINDOW_KEY = 'mixed';
const DEFAULT_MOMENTUM_WEIGHTS = DEFAULT_FORM_VALUES.momentum_weights;
const MOMENTUM_WEIGHT_WINDOWS = [20, 60, 120];
const REBALANCE_FREQUENCY_OPTIONS = [
  { label: '每日', value: 'daily' },
  { label: '每周', value: 'weekly' },
  { label: '每月', value: 'monthly' },
  { label: '季度', value: 'quarterly' },
  { label: '半年', value: 'semiannual' },
];
const REBALANCE_FREQUENCY_LABELS = REBALANCE_FREQUENCY_OPTIONS.reduce((acc, item) => {
  acc[item.value] = item.label;
  return acc;
}, {});
const DEFAULT_BACKTEST_SEARCH_OBJECTIVES = [
  { key: 'annualized_return', label: '全区间年化收益最大' },
  { key: 'total_return', label: '全区间总收益最大' },
  { key: 'sharpe', label: '全区间夏普最大' },
  { key: 'calmar', label: '全区间卡玛最大' },
  { key: 'in_sample_annualized_return', label: '样本内年化收益最大' },
  { key: 'in_sample_total_return', label: '样本内总收益最大' },
  { key: 'in_sample_sharpe', label: '样本内夏普最大' },
  { key: 'in_sample_calmar', label: '样本内卡玛最大' },
  { key: 'oos_annualized_return', label: '样本外年化收益最大' },
  { key: 'oos_total_return', label: '样本外总收益最大' },
  { key: 'oos_sharpe', label: '样本外夏普最大' },
  { key: 'oos_calmar', label: '样本外卡玛最大' },
];
const BACKTEST_SEARCH_RUNNING_STATUSES = ['queued', 'running'];
const BACKTEST_SEARCH_STATUS_META = {
  queued: { color: 'blue', label: '排队中' },
  running: { color: 'processing', label: '运行中' },
  completed: { color: 'green', label: '已完成' },
  failed: { color: 'red', label: '失败' },
  cancelled: { color: 'orange', label: '已取消' },
  interrupted: { color: 'orange', label: '已中断' },
};
const A_STOCK_INNO100_POOL = 'INNO100';
const A_STOCK_INNO100_SYMBOL = 'INNO100.CN';
const isAStockPoolValue = value => (
  String(value || '').toUpperCase() === A_STOCK_INNO100_POOL
  || /\.(SH|SZ|BJ)$/.test(String(value || '').toUpperCase())
);

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

const factorValueFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 6 });
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
  oos_start_date: Object.prototype.hasOwnProperty.call(payload, 'oos_start_date')
    ? (payload.oos_start_date ? dayjs(payload.oos_start_date) : null)
    : DEFAULT_FORM_VALUES.oos_start_date,
  neutralization: payload.neutralization || DEFAULT_FORM_VALUES.neutralization,
  standardization: payload.standardization || DEFAULT_FORM_VALUES.standardization,
  heatmap_metric: payload.heatmap_metric || DEFAULT_FORM_VALUES.heatmap_metric,
  heatmap_windows: payload.heatmap_windows || payload.windows || DEFAULT_FORM_VALUES.heatmap_windows,
  heatmap_forward_windows: payload.heatmap_forward_windows || DEFAULT_FORM_VALUES.heatmap_forward_windows,
  momentum_weights: payload.momentum_weights || DEFAULT_MOMENTUM_WEIGHTS,
});

const normalizeCompositeDefaultRequest = (payload = {}) => ({
  ...DEFAULT_COMPOSITE_VALUES,
  ...payload,
  start_date: payload.start_date ? dayjs(payload.start_date) : DEFAULT_COMPOSITE_VALUES.start_date,
  end_date: payload.end_date ? dayjs(payload.end_date) : null,
  oos_start_date: Object.prototype.hasOwnProperty.call(payload, 'oos_start_date')
    ? (payload.oos_start_date ? dayjs(payload.oos_start_date) : null)
    : DEFAULT_COMPOSITE_VALUES.oos_start_date,
  forward_window: payload.forward_window || DEFAULT_COMPOSITE_VALUES.forward_window,
  legs: (payload.legs?.length ? payload.legs : DEFAULT_COMPOSITE_VALUES.legs).map(leg => ({
    ...leg,
    neutralization: leg.neutralization || 'none',
    standardization: leg.standardization || 'rank_percentile',
    momentum_weights: normalizeMomentumWeights(leg.momentum_weights),
  })),
});

const normalizeBacktestDefaultRequest = (payload = {}) => ({
  ...DEFAULT_BACKTEST_VALUES,
  ...payload,
  start_date: payload.start_date ? dayjs(payload.start_date) : DEFAULT_BACKTEST_VALUES.start_date,
  end_date: payload.end_date ? dayjs(payload.end_date) : null,
  oos_start_date: Object.prototype.hasOwnProperty.call(payload, 'oos_start_date')
    ? (payload.oos_start_date ? dayjs(payload.oos_start_date) : null)
    : DEFAULT_BACKTEST_VALUES.oos_start_date,
  legs: (payload.legs?.length ? payload.legs : DEFAULT_BACKTEST_VALUES.legs).map(leg => ({
    ...leg,
    neutralization: leg.neutralization || 'none',
    standardization: leg.standardization || 'rank_percentile',
    momentum_weights: normalizeMomentumWeights(leg.momentum_weights),
  })),
});

const normalizeTimingDefaultRequest = (payload = {}) => ({
  ...DEFAULT_TIMING_VALUES,
  ...payload,
  start_date: payload.start_date ? dayjs(payload.start_date) : DEFAULT_TIMING_VALUES.start_date,
  end_date: payload.end_date ? dayjs(payload.end_date) : null,
  target_symbol: payload.target_symbol || DEFAULT_TIMING_VALUES.target_symbol,
  fear_symbol: payload.fear_symbol || DEFAULT_TIMING_VALUES.fear_symbol,
  ma_window: payload.ma_window || DEFAULT_TIMING_VALUES.ma_window,
  bucket_count: payload.bucket_count || DEFAULT_TIMING_VALUES.bucket_count,
  forward_window: payload.forward_window || DEFAULT_TIMING_VALUES.forward_window,
  heatmap_metric: payload.heatmap_metric || DEFAULT_TIMING_VALUES.heatmap_metric,
  heatmap_forward_windows: payload.heatmap_forward_windows || DEFAULT_TIMING_VALUES.heatmap_forward_windows,
  heatmap_ma_windows: payload.heatmap_ma_windows || DEFAULT_TIMING_VALUES.heatmap_ma_windows,
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

const isMixedWindow = value => String(value).toLowerCase() === MIXED_WINDOW_KEY;

const normalizeHeatmapWindows = (value, fallback) => {
  const items = Array.isArray(value) ? value : [];
  const normalized = [];
  items.forEach(item => {
    if (isMixedWindow(item)) {
      if (!normalized.includes(MIXED_WINDOW_KEY)) normalized.push(MIXED_WINDOW_KEY);
      return;
    }
    const numberValue = Number(item);
    if (Number.isFinite(numberValue) && !normalized.includes(numberValue)) {
      normalized.push(numberValue);
    }
  });
  return normalized.length ? normalized : fallback;
};

const getWindowKey = value => (isMixedWindow(value) ? MIXED_WINDOW_KEY : String(Number(value)));

const isSameWindow = (left, right) => getWindowKey(left) === getWindowKey(right);

const formatWindowLabel = value => (isMixedWindow(value) ? '多窗口合成' : `${Number(value)}日`);

const getWindowSortValue = value => (isMixedWindow(value) ? Number.MAX_SAFE_INTEGER : Number(value));

const getHeatmapWindowItems = rows => {
  const map = new Map();
  (rows || []).forEach(row => {
    const key = getWindowKey(row.window);
    if (!map.has(key)) {
      map.set(key, {
        key,
        value: row.window,
        label: row.window_label || formatWindowLabel(row.window),
      });
    }
  });
  return [...map.values()].sort((left, right) => (
    getWindowSortValue(left.value) - getWindowSortValue(right.value)
  ));
};

const normalizeMomentumWeights = weights => {
  const source = weights || DEFAULT_MOMENTUM_WEIGHTS;
  return MOMENTUM_WEIGHT_WINDOWS.reduce((acc, window) => {
    const rawValue = source[String(window)] ?? source[window] ?? DEFAULT_MOMENTUM_WEIGHTS[window] ?? 0;
    const numberValue = Number(rawValue);
    acc[String(window)] = Number.isFinite(numberValue) ? Math.max(0, numberValue) : 0;
    return acc;
  }, {});
};

const getFactorByKey = (factors, key) => (
  (factors || []).find(item => item.key === key)
);

const getDefaultWindowForFactor = factor => {
  if (!factor?.supports_windows) return factor?.default_windows?.[0] || 20;
  if (factor.supports_mixed_windows) return MIXED_WINDOW_KEY;
  return factor.default_windows?.[0] || 20;
};

const getWindowOptionsForFactor = (factor, baseWindows = [20, 60, 120]) => {
  const numericWindows = factor?.supports_windows
    ? (factor.default_windows?.length ? factor.default_windows : baseWindows)
    : (factor?.default_windows || [20]);
  const options = numericWindows.map(item => ({ label: `${item}日`, value: item }));
  if (factor?.supports_mixed_windows) {
    options.push({ label: '多窗口合成', value: MIXED_WINDOW_KEY });
  }
  return options;
};

const buildDefaultCompositeLeg = (factorKey = 'raw_momentum', factor = null) => ({
  factor: factorKey,
  window: factor ? getDefaultWindowForFactor(factor) : 120,
  weight: 0.1,
  neutralization: 'none',
  standardization: 'rank_percentile',
  momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
});

const buildDefaultBacktestLeg = (factorKey = 'risk_adjusted_momentum', factor = null) => ({
  factor: factorKey,
  window: factor ? getDefaultWindowForFactor(factor) : MIXED_WINDOW_KEY,
  weight: 1,
  neutralization: 'none',
  standardization: 'rank_percentile',
  momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
});

const buildAnalyzePayload = (values, overrides = {}) => {
  const heatmapWindows = normalizeHeatmapWindows(
    overrides.heatmap_windows || values.heatmap_windows,
    DEFAULT_FORM_VALUES.heatmap_windows,
  );
  const heatmapForwardWindows = normalizeNumberArray(
    overrides.heatmap_forward_windows || values.heatmap_forward_windows,
    DEFAULT_FORM_VALUES.heatmap_forward_windows,
  );
  return {
    pool: values.pool,
    factor: values.factor,
    bucket_count: values.bucket_count,
    start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : DEFAULT_FORM_VALUES.start_date.format('YYYY-MM-DD'),
    end_date: values.end_date ? values.end_date.format('YYYY-MM-DD') : null,
    neutralization: values.neutralization || DEFAULT_FORM_VALUES.neutralization,
    standardization: values.standardization || DEFAULT_FORM_VALUES.standardization,
    oos_start_date: values.oos_start_date ? values.oos_start_date.format('YYYY-MM-DD') : null,
    heatmap_metric: values.heatmap_metric || DEFAULT_HEATMAP_METRIC,
    momentum_weights: normalizeMomentumWeights(overrides.momentum_weights || values.momentum_weights),
    min_listing_days: DEFAULT_MIN_LISTING_DAYS,
    include_heatmap: overrides.include_heatmap ?? true,
    heatmap_windows: heatmapWindows,
    heatmap_forward_windows: heatmapForwardWindows,
  };
};

const buildCompositePayload = values => {
  const legs = (values.legs || [])
    .filter(leg => leg?.factor)
    .map(leg => ({
      factor: leg.factor,
      window: isMixedWindow(leg.window) ? MIXED_WINDOW_KEY : Number(leg.window),
      weight: Number(leg.weight || 0),
      neutralization: leg.neutralization || 'none',
      standardization: leg.standardization || 'rank_percentile',
      momentum_weights: normalizeMomentumWeights(leg.momentum_weights),
    }));

  return {
    pool: values.pool,
    bucket_count: values.bucket_count,
    start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : DEFAULT_COMPOSITE_VALUES.start_date.format('YYYY-MM-DD'),
    end_date: values.end_date ? values.end_date.format('YYYY-MM-DD') : null,
    oos_start_date: values.oos_start_date ? values.oos_start_date.format('YYYY-MM-DD') : null,
    forward_window: Number(values.forward_window || DEFAULT_COMPOSITE_VALUES.forward_window),
    min_listing_days: Number(values.min_listing_days ?? DEFAULT_MIN_LISTING_DAYS),
    legs,
  };
};

const buildBacktestPayload = values => ({
  pool: values.pool,
  start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : DEFAULT_BACKTEST_VALUES.start_date.format('YYYY-MM-DD'),
  end_date: values.end_date ? values.end_date.format('YYYY-MM-DD') : null,
  oos_start_date: values.oos_start_date ? values.oos_start_date.format('YYYY-MM-DD') : null,
  initial_capital: Number(values.initial_capital || DEFAULT_BACKTEST_VALUES.initial_capital),
  max_positions: Number(values.max_positions || DEFAULT_BACKTEST_VALUES.max_positions),
  sell_rank_multiplier: Number(values.sell_rank_multiplier || DEFAULT_BACKTEST_VALUES.sell_rank_multiplier),
  rebalance_frequency: values.rebalance_frequency || DEFAULT_BACKTEST_VALUES.rebalance_frequency,
  commission_pct: Number(values.commission_pct ?? DEFAULT_BACKTEST_VALUES.commission_pct),
  slippage_pct: Number(values.slippage_pct ?? DEFAULT_BACKTEST_VALUES.slippage_pct),
  lot_size: Number(values.lot_size || DEFAULT_BACKTEST_VALUES.lot_size),
  min_listing_days: Number(values.min_listing_days ?? DEFAULT_BACKTEST_VALUES.min_listing_days),
  legs: (values.legs || [])
    .filter(leg => leg?.factor)
    .map(leg => ({
      factor: leg.factor,
      window: isMixedWindow(leg.window) ? MIXED_WINDOW_KEY : Number(leg.window),
      weight: Number(leg.weight || 0),
      neutralization: leg.neutralization || 'none',
      standardization: leg.standardization || 'rank_percentile',
      momentum_weights: normalizeMomentumWeights(leg.momentum_weights),
    })),
});

const buildTimingPayload = values => ({
  target_symbol: String(values.target_symbol || DEFAULT_TIMING_VALUES.target_symbol).trim().toUpperCase(),
  fear_symbol: String(values.fear_symbol || DEFAULT_TIMING_VALUES.fear_symbol).trim().toUpperCase(),
  ma_window: Number(values.ma_window || DEFAULT_TIMING_VALUES.ma_window),
  bucket_count: Number(values.bucket_count || DEFAULT_TIMING_VALUES.bucket_count),
  start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : DEFAULT_TIMING_VALUES.start_date.format('YYYY-MM-DD'),
  end_date: values.end_date ? values.end_date.format('YYYY-MM-DD') : null,
  forward_window: Number(values.forward_window || DEFAULT_TIMING_VALUES.forward_window),
  heatmap_metric: values.heatmap_metric || DEFAULT_TIMING_HEATMAP_METRIC,
  heatmap_forward_windows: normalizeNumberArray(
    values.heatmap_forward_windows,
    DEFAULT_TIMING_VALUES.heatmap_forward_windows,
  ),
  heatmap_ma_windows: normalizeNumberArray(
    values.heatmap_ma_windows,
    DEFAULT_TIMING_VALUES.heatmap_ma_windows,
  ),
  include_heatmap: true,
});

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

const getFactorDistributionOption = rows => {
  const safeRows = Array.isArray(rows) ? rows : [];
  const bucketIds = [...new Set(safeRows.map(item => Number(item.bucket_id)).filter(Number.isFinite))]
    .sort((a, b) => a - b);
  const rowBySeriesBucket = new Map(
    safeRows.map(item => [`${item.series}:${Number(item.bucket_id)}`, item]),
  );
  const seriesSpecs = [
    { key: 'raw', fallbackName: '原始因子值', color: '#7a5ccf' },
    { key: 'analysis', fallbackName: '标准化/分析值', color: '#2477b3' },
  ].map(spec => {
    const seriesRows = safeRows
      .filter(item => item.series === spec.key)
      .sort((left, right) => Number(left.bucket_id) - Number(right.bucket_id));
    const firstRow = seriesRows[0] || {};
    return {
      ...spec,
      rows: seriesRows,
      name: firstRow.series_label || spec.fallbackName,
      meanValue: firstRow.mean_value,
      stdValue: firstRow.std_value,
    };
  }).filter(spec => spec.rows.length);
  const statText = seriesSpecs
    .map(spec => `${spec.name} 均值 ${factorValueFormatter(spec.meanValue)} / 标准差 ${factorValueFormatter(spec.stdValue)}`)
    .join('    ');

  return {
    title: {
      text: statText,
      left: 4,
      top: 0,
      textStyle: { fontSize: 12, fontWeight: 500, color: '#334155' },
    },
    grid: { top: 58, right: 38, bottom: 42, left: 56 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: params => {
        const items = Array.isArray(params) ? params : [params];
        const first = items[0] || {};
        const lines = [first.axisValue || ''];
        items.forEach(item => {
          const itemRow = item.data?.row || {};
          lines.push(
            `${item.marker}样本占比: ${percentFormatter(itemRow.pct)}（${numberFormatter(itemRow.samples)}）`
          );
          lines.push(`${item.seriesName} 区间: ${factorValueFormatter(itemRow.value_from)} ~ ${factorValueFormatter(itemRow.value_to)}`);
        });
        return lines.join('<br/>');
      },
    },
    legend: { top: 24 },
    xAxis: {
      type: 'category',
      data: bucketIds.map(item => `B${item}`),
      axisTick: { alignWithLabel: true },
      axisLabel: {
        hideOverlap: true,
        formatter: (_, labelIndex) => {
          const bucketId = bucketIds[labelIndex];
          if (!bucketId) return '';
          const isEdge = labelIndex === 0 || labelIndex === bucketIds.length - 1;
          return isEdge || labelIndex % 8 === 0 ? `B${bucketId}` : '';
        },
      },
    },
    yAxis: {
      type: 'value',
      name: '样本占比%',
      axisLabel: { formatter: value => `${value}%` },
      splitLine: { lineStyle: { color: '#edf1f7' } },
    },
    series: seriesSpecs.map(spec => (
      {
        name: spec.name,
        type: 'bar',
        data: bucketIds.map(bucketId => {
          const row = rowBySeriesBucket.get(`${spec.key}:${bucketId}`);
          return { value: row?.pct ?? 0, row };
        }),
        itemStyle: { color: spec.color },
        barMaxWidth: 14,
      }
    )),
  };
};

const getIcOption = rows => ({
  grid: { top: 52, right: 58, bottom: 36, left: 52 },
  tooltip: {
    trigger: 'axis',
    formatter: params => {
      const items = Array.isArray(params) ? params : [params];
      const dateLabel = items[0]?.axisValue || '';
      return [
        dateLabel,
        ...items.map(item => `${item.marker}${item.seriesName}: ${icFormatter(item.value)}`),
      ].join('<br/>');
    },
  },
  legend: { top: 0, type: 'scroll' },
  xAxis: {
    type: 'category',
    data: rows.map(item => item.trade_date),
    axisLabel: { hideOverlap: true },
  },
  yAxis: [
    { type: 'value', name: 'IC', splitLine: { lineStyle: { color: '#edf1f7' } } },
    { type: 'value', name: '累计IC', splitLine: { show: false } },
  ],
  series: [
    {
      name: 'Rank IC',
      type: 'bar',
      data: rows.map(item => item.rank_ic),
      itemStyle: { color: 'rgba(36, 119, 179, 0.32)' },
      barMaxWidth: 5,
    },
    {
      name: 'MA20',
      type: 'line',
      showSymbol: false,
      data: rows.map(item => item.rank_ic_ma20),
      lineStyle: { color: '#d95f59', width: 2 },
    },
    {
      name: 'MA60',
      type: 'line',
      showSymbol: false,
      data: rows.map(item => item.rank_ic_ma60),
      lineStyle: { color: '#2f9e6d', width: 2 },
    },
    {
      name: '累计IC',
      type: 'line',
      yAxisIndex: 1,
      showSymbol: false,
      data: rows.map(item => item.cumulative_rank_ic),
      lineStyle: { color: '#14213d', width: 2 },
    },
  ],
});

const isSameCombo = (combo, row) => (
  combo
  && isSameWindow(combo.window, row.window)
  && Number(combo.forward_window) === Number(row.forward_window)
);

const getHeatmapMetricMeta = (metric, metrics = []) => (
  (metrics || []).find(item => item.key === metric)
  || { key: metric, label: metric, kind: metric?.endsWith('_pct') ? 'percent' : 'number' }
);

const formatHeatmapMetricValue = (metric, value, metrics = []) => {
  const meta = getHeatmapMetricMeta(metric, metrics);
  if (meta.kind === 'percent') return percentFormatter(value);
  return icFormatter(value);
};

const getHeatmapValue = (item, metric = DEFAULT_HEATMAP_METRIC) => (
  item?.[metric]
  ?? (metric === DEFAULT_HEATMAP_METRIC ? item?.non_overlap_annualized_top_minus_bottom_pct : undefined)
  ?? item?.heatmap_value
  ?? item?.heatmap_value_pct
  ?? item?.non_overlap_annualized_top_minus_bottom_pct
  ?? item?.annualized_top_minus_bottom_avg_return_pct
  ?? item?.top_minus_bottom_avg_return_pct
);

const getHeatmapOption = (rows, selectedCombo, metric = DEFAULT_HEATMAP_METRIC, metrics = []) => {
  const metricMeta = getHeatmapMetricMeta(metric, metrics);
  const validRows = (rows || []).filter(item => getHeatmapValue(item, metric) !== null && getHeatmapValue(item, metric) !== undefined);
  const windowItems = getHeatmapWindowItems(validRows);
  const windowKeys = windowItems.map(item => item.key);
  const forwards = [...new Set(validRows.map(item => item.forward_window))].sort((a, b) => a - b);
  const values = validRows.map(item => getHeatmapValue(item, metric));
  const min = values.length ? Math.min(...values) : -1;
  const max = values.length ? Math.max(...values) : 1;
  return {
    grid: { top: 36, right: 88, bottom: 40, left: 72 },
    tooltip: {
      position: 'top',
      formatter: params => {
        const item = validRows.find(row => (
          row.forward_window === forwards[params.value[0]] && getWindowKey(row.window) === windowKeys[params.value[1]]
        ));
        if (!item) return '';
        const lines = [
          `窗口: ${item.window_label || formatWindowLabel(item.window)}`,
          `T+${item.forward_window}`,
          `${metricMeta.label}: ${formatHeatmapMetricValue(metric, getHeatmapValue(item, metric), metrics)}`,
          `Rank IC: ${icFormatter(item.rank_ic_mean)}`,
          `Rank IC t-stat: ${icFormatter(item.rank_ic_t_stat)}`,
          `重叠年化多空差: ${percentFormatter(item.annualized_top_minus_bottom_avg_return_pct)}`,
          `T+n多空差: ${percentFormatter(item.top_minus_bottom_avg_return_pct)}`,
          `样本: ${numberFormatter(item.samples)}`,
        ];
        if (metric !== DEFAULT_HEATMAP_METRIC) {
          lines.splice(3, 0, `非重叠年化多空差: ${percentFormatter(item.non_overlap_annualized_median_pct ?? item.non_overlap_annualized_top_minus_bottom_pct)}`);
        }
        return lines.join('<br/>');
      },
    },
    xAxis: { type: 'category', data: forwards.map(item => `T+${item}`), splitArea: { show: true } },
    yAxis: { type: 'category', data: windowItems.map(item => item.label), splitArea: { show: true } },
    visualMap: {
      min,
      max,
      calculable: true,
      orient: 'vertical',
      right: 8,
      top: 40,
      inRange: { color: ['#2f70b7', '#f6f7f9', '#cb3a31'] },
      formatter: value => formatHeatmapMetricValue(metric, value, metrics),
    },
    series: [
      {
        name: metricMeta.label,
        type: 'heatmap',
        data: validRows.map(item => ({
          value: [
            forwards.indexOf(item.forward_window),
            windowKeys.indexOf(getWindowKey(item.window)),
            getHeatmapValue(item, metric),
          ],
          itemStyle: isSameCombo(selectedCombo, item)
            ? { borderColor: '#111827', borderWidth: 3 }
            : undefined,
        })),
        label: {
          show: true,
          formatter: params => formatHeatmapMetricValue(metric, params.value[2], metrics),
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

const getTimingHeatmapValue = (item, metric = DEFAULT_TIMING_HEATMAP_METRIC) => (
  item?.[metric]
  ?? (metric === DEFAULT_TIMING_HEATMAP_METRIC ? item?.heatmap_value : undefined)
  ?? item?.selected_heatmap_value
  ?? item?.heatmap_value
  ?? item?.annualized_low_minus_high_avg_return_pct
  ?? item?.low_minus_high_avg_return_pct
);

const getTimingHeatmapOption = (
  rows,
  selectedCombo,
  metric = DEFAULT_TIMING_HEATMAP_METRIC,
  metrics = DEFAULT_TIMING_HEATMAP_METRICS,
) => {
  const metricMeta = getHeatmapMetricMeta(metric, metrics);
  const validRows = (rows || []).filter(item => (
    getTimingHeatmapValue(item, metric) !== null
    && getTimingHeatmapValue(item, metric) !== undefined
  ));
  const maItems = [...new Map(validRows.map(item => [item.ma_window, item.ma_window_label || `${item.ma_window}日均值`])).entries()]
    .sort((left, right) => Number(left[0]) - Number(right[0]));
  const maKeys = maItems.map(item => item[0]);
  const maLabels = maItems.map(item => item[1]);
  const forwards = [...new Set(validRows.map(item => item.forward_window))].sort((a, b) => a - b);
  const values = validRows.map(item => Number(getTimingHeatmapValue(item, metric))).filter(Number.isFinite);
  const min = values.length ? Math.min(...values) : -1;
  const max = values.length ? Math.max(...values) : 1;
  return {
    grid: { top: 36, right: 88, bottom: 40, left: 180 },
    tooltip: {
      position: 'top',
      formatter: params => {
        const item = validRows.find(row => (
          row.forward_window === forwards[params.value[0]] && Number(row.ma_window) === Number(maKeys[params.value[1]])
        ));
        if (!item) return '';
        const lines = [
          `贪恐均线: ${item.ma_window_label || `${item.ma_window}日均值`}`,
          `T+${item.forward_window}`,
          `${metricMeta.label}: ${formatHeatmapMetricValue(metric, getTimingHeatmapValue(item, metric), metrics)}`,
          `年化低-高桶差: ${percentFormatter(item.annualized_low_minus_high_avg_return_pct)}`,
          `T+n低-高桶差: ${percentFormatter(item.low_minus_high_avg_return_pct)}`,
          `年化高-低桶差: ${percentFormatter(item.annualized_top_minus_bottom_avg_return_pct)}`,
          `T+n高-低桶差: ${percentFormatter(item.top_minus_bottom_avg_return_pct)}`,
          `时间序列IC: ${icFormatter(item.rank_ic_mean)}`,
          `IC t-stat: ${icFormatter(item.rank_ic_t_stat)}`,
          `样本: ${numberFormatter(item.samples)}`,
        ];
        return lines.join('<br/>');
      },
    },
    xAxis: { type: 'category', data: forwards.map(item => `T+${item}`), splitArea: { show: true } },
    yAxis: { type: 'category', data: maLabels, splitArea: { show: true } },
    visualMap: {
      min,
      max,
      calculable: true,
      orient: 'vertical',
      right: 8,
      top: 40,
      inRange: { color: ['#2f70b7', '#f6f7f9', '#cb3a31'] },
      formatter: value => formatHeatmapMetricValue(metric, value, metrics),
    },
    series: [
      {
        name: metricMeta.label,
        type: 'heatmap',
        data: validRows.map(item => ({
          value: [
            forwards.indexOf(item.forward_window),
            maKeys.findIndex(value => Number(value) === Number(item.ma_window)),
            getTimingHeatmapValue(item, metric),
          ],
          itemStyle: selectedCombo
            && Number(selectedCombo.ma_window) === Number(item.ma_window)
            && Number(selectedCombo.forward_window) === Number(item.forward_window)
            ? { borderColor: '#111827', borderWidth: 3 }
            : undefined,
        })),
        label: {
          show: true,
          formatter: params => formatHeatmapMetricValue(metric, params.value[2], metrics),
        },
      },
    ],
  };
};

const getBacktestEquityOption = (equityRows = [], benchmarkRows = [], candidateEtfs = []) => {
  const dates = equityRows.map(item => item.date);
  const benchmarkByDate = new Map((benchmarkRows || []).map(item => [item.date, item.values || {}]));
  const drawdownValues = equityRows
    .map(item => Number(item.drawdown))
    .filter(value => Number.isFinite(value));
  const drawdownMin = drawdownValues.length
    ? Math.floor(Math.min(...drawdownValues, -1) / 5) * 5
    : -10;
  return {
    grid: { top: 48, right: 58, bottom: 36, left: 64 },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const items = Array.isArray(params) ? params : [params];
        return items.map(item => {
          const value = item.seriesName === '回撤'
            ? percentFormatter(item.value)
            : numberFormatter(item.value);
          return `${item.marker}${item.seriesName}: ${value}`;
        }).join('<br/>');
      },
    },
    legend: { top: 0, type: 'scroll' },
    xAxis: { type: 'category', data: dates, axisLabel: { hideOverlap: true } },
    yAxis: [
      { type: 'value', name: '净值', splitLine: { lineStyle: { color: '#edf1f7' } } },
      {
        type: 'value',
        name: '回撤',
        min: drawdownMin,
        max: 0,
        axisLabel: { formatter: value => `${value}%` },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '策略',
        type: 'line',
        showSymbol: false,
        data: equityRows.map(item => item.value),
        lineStyle: { width: 2.5, color: '#2477b3' },
      },
      ...(candidateEtfs || []).map((symbol, index) => ({
        name: symbol,
        type: 'line',
        showSymbol: false,
        data: dates.map(item => benchmarkByDate.get(item)?.[symbol] ?? null),
        lineStyle: {
          width: 1.8,
          color: ['#d95f59', '#2f9e6d', '#7a5ccf'][index % 3],
        },
      })),
      {
        name: '回撤',
        type: 'line',
        yAxisIndex: 1,
        showSymbol: false,
        data: equityRows.map(item => item.drawdown),
        lineStyle: { width: 1.2, color: 'rgba(217, 95, 89, 0.55)' },
        areaStyle: {
          origin: 'end',
          color: 'rgba(217, 95, 89, 0.12)',
        },
        emphasis: { focus: 'series' },
        z: 0,
      },
    ],
  };
};

const bucketColumns = [
  { title: '桶', dataIndex: 'bucket', width: 64, fixed: 'left' },
  { title: '样本', dataIndex: 'samples', align: 'right', render: numberFormatter },
  { title: '日期数', dataIndex: 'trade_dates', align: 'right', render: numberFormatter },
  { title: '分析因子均值', dataIndex: 'avg_factor_value', align: 'right', render: icFormatter },
  { title: '原始因子均值', dataIndex: 'avg_factor_value_raw', align: 'right', render: icFormatter },
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

const componentIcColumns = [
  { title: '子因子', dataIndex: 'component_label', width: 220, fixed: 'left' },
  { title: '窗口', dataIndex: 'window_label', width: 110 },
  { title: '原始权重', dataIndex: 'raw_weight', align: 'right', render: icFormatter },
  { title: '归一权重', dataIndex: 'weight', align: 'right', render: icFormatter },
  { title: '样本', dataIndex: 'samples', align: 'right', render: numberFormatter },
  { title: '日期数', dataIndex: 'trade_dates', align: 'right', render: numberFormatter },
  { title: 'Rank IC', dataIndex: 'rank_ic_mean', align: 'right', render: icFormatter },
  { title: 'ICIR', dataIndex: 'icir', align: 'right', render: icFormatter },
  { title: 'IC t-stat', dataIndex: 'rank_ic_t_stat', align: 'right', render: icFormatter },
];

const backtestYearlyColumns = [
  { title: '年份', dataIndex: 'year', width: 80, fixed: 'left' },
  { title: '开始', dataIndex: 'start_date', width: 112 },
  { title: '结束', dataIndex: 'end_date', width: 112 },
  { title: '策略收益', dataIndex: 'strategy_return_pct', align: 'right', render: percentFormatter },
  { title: '主基准', dataIndex: 'primary_benchmark_symbol', width: 110 },
  { title: '基准收益', dataIndex: 'primary_benchmark_return_pct', align: 'right', render: percentFormatter },
  { title: '超额收益', dataIndex: 'primary_excess_return_pct', align: 'right', render: percentFormatter },
];

const backtestHoldingColumns = [
  { title: '代码', dataIndex: 'symbol', width: 110, fixed: 'left' },
  { title: '股数', dataIndex: 'shares', align: 'right', render: numberFormatter },
  { title: '价格', dataIndex: 'price', align: 'right', render: numberFormatter },
  { title: '成本', dataIndex: 'avg_cost', align: 'right', render: numberFormatter },
  { title: '入场日', dataIndex: 'entry_date', width: 112 },
  { title: '市值', dataIndex: 'market_value', align: 'right', render: numberFormatter },
  { title: '权重', dataIndex: 'actual_weight_pct', align: 'right', render: percentFormatter },
];

const backtestTradeColumns = [
  { title: '日期', dataIndex: 'date', width: 112, fixed: 'left' },
  { title: '信号日', dataIndex: 'signal_date', width: 112 },
  { title: '方向', dataIndex: 'action', width: 80, render: value => <Tag color={value === 'BUY' ? 'green' : 'orange'}>{value}</Tag> },
  { title: '代码', dataIndex: 'symbol', width: 110 },
  { title: '价格', dataIndex: 'price', align: 'right', render: numberFormatter },
  { title: '数量', dataIndex: 'quantity', align: 'right', render: numberFormatter },
  { title: '金额', dataIndex: 'amount', align: 'right', render: numberFormatter },
  { title: '手续费', dataIndex: 'commission', align: 'right', render: numberFormatter },
  { title: '盈亏', dataIndex: 'profit', align: 'right', render: numberFormatter },
  { title: '现金', dataIndex: 'cash_after', align: 'right', render: numberFormatter },
];

const isPercentObjective = objective => ['annualized_return', 'total_return'].includes(objective);

const formatBacktestSearchObjective = (value, objective) => (
  isPercentObjective(objective) ? percentFormatter(value) : icFormatter(value)
);

const parseCandidateNumbers = (value, { integer = false, min = -Infinity, max = Infinity, fallback = [], label = '候选项' } = {}) => {
  const items = String(value || '')
    .split(/[,\uff0c\s]+/)
    .map(item => item.trim())
    .filter(Boolean);
  const source = items.length ? items : fallback;
  const normalized = [];
  source.forEach(item => {
    const number = Number(item);
    if (!Number.isFinite(number)) {
      throw new Error(`${label}只能包含数字`);
    }
    const nextValue = integer ? Math.trunc(number) : number;
    if (integer && Math.abs(nextValue - number) > 1e-9) {
      throw new Error(`${label}必须是整数`);
    }
    if (nextValue < min || nextValue > max) {
      throw new Error(`${label}必须在 ${min} 到 ${max} 之间`);
    }
    if (!normalized.includes(nextValue)) {
      normalized.push(nextValue);
    }
  });
  if (!normalized.length) {
    throw new Error(`请填写${label}`);
  }
  return normalized;
};

const getNumericValue = (record, key) => {
  const value = Number(record?.[key]);
  return Number.isFinite(value) ? value : null;
};

const setNumericFilterValue = (setSelectedKeys, current, key, value) => {
  const next = { ...(current || {}), [key]: value };
  const hasMin = next.min !== null && next.min !== undefined && next.min !== '';
  const hasMax = next.max !== null && next.max !== undefined && next.max !== '';
  setSelectedKeys(hasMin || hasMax ? [next] : []);
};

const numericColumn = ({ title, dataIndex, width, render = numberFormatter, fixed }) => ({
  title,
  dataIndex,
  width,
  fixed,
  align: 'right',
  render,
  sorter: true,
  filterIcon: filtered => <FilterOutlined style={{ color: filtered ? '#1677ff' : undefined }} />,
  filterDropdown: ({ setSelectedKeys, selectedKeys, confirm, clearFilters }) => {
    const value = selectedKeys[0] || {};
    return (
      <div style={{ padding: 8, width: 180 }} onKeyDown={event => event.stopPropagation()}>
        <Space direction="vertical" size={8} className="factor-lab-full">
          <InputNumber
            placeholder="最小值"
            value={value.min}
            onChange={nextValue => setNumericFilterValue(setSelectedKeys, value, 'min', nextValue)}
            className="factor-lab-full"
          />
          <InputNumber
            placeholder="最大值"
            value={value.max}
            onChange={nextValue => setNumericFilterValue(setSelectedKeys, value, 'max', nextValue)}
            className="factor-lab-full"
          />
          <Space>
            <Button size="small" type="primary" onClick={() => confirm()}>过滤</Button>
            <Button
              size="small"
              onClick={() => {
                clearFilters?.();
                confirm();
              }}
            >
              重置
            </Button>
          </Space>
        </Space>
      </div>
    );
  },
});

const normalizeSearchTableFilters = filters => {
  const normalized = {};
  Object.entries(filters || {}).forEach(([key, value]) => {
    const item = Array.isArray(value) ? value[0] : value;
    if (!item || typeof item !== 'object') return;
    const next = {};
    const min = Number(item.min);
    const max = Number(item.max);
    if (Number.isFinite(min)) next.min = min;
    if (Number.isFinite(max)) next.max = max;
    if (Object.keys(next).length) normalized[key] = next;
  });
  return normalized;
};

const getBacktestSearchColumns = (objective, onApply, onAnalyze, applyingCaseIndex, analyzingCaseIndex) => [
  { title: '排名', dataIndex: 'rank', width: 76, fixed: 'left', align: 'right', render: numberFormatter },
  {
    title: '操作',
    dataIndex: 'action',
    width: 176,
    fixed: 'left',
    render: (_, row) => (
      <Space size={6}>
        <Button
          size="small"
          loading={applyingCaseIndex === row.case_index}
          onClick={() => onApply(row)}
        >
          应用回测
        </Button>
        <Button
          size="small"
          icon={<BarChartOutlined />}
          loading={analyzingCaseIndex === row.case_index}
          onClick={() => onAnalyze(row)}
        >
          看分析
        </Button>
      </Space>
    ),
  },
  numericColumn({ title: '目标值', dataIndex: 'objective_value', width: 112, render: value => formatBacktestSearchObjective(value, objective) }),
  numericColumn({ title: '全区间年化', dataIndex: 'annualized_return', width: 116, render: percentFormatter }),
  numericColumn({ title: '全区间总收益', dataIndex: 'total_return', width: 126, render: percentFormatter }),
  numericColumn({ title: '全区间夏普', dataIndex: 'sharpe', width: 112, render: icFormatter }),
  numericColumn({ title: '全区间卡玛', dataIndex: 'calmar', width: 112, render: icFormatter }),
  numericColumn({ title: '全区间回撤', dataIndex: 'max_drawdown', width: 116, render: percentFormatter }),
  numericColumn({ title: '样本内年化', dataIndex: 'in_sample_annualized_return', width: 116, render: percentFormatter }),
  numericColumn({ title: '样本内总收益', dataIndex: 'in_sample_total_return', width: 126, render: percentFormatter }),
  numericColumn({ title: '样本内夏普', dataIndex: 'in_sample_sharpe', width: 112, render: icFormatter }),
  numericColumn({ title: '样本内卡玛', dataIndex: 'in_sample_calmar', width: 112, render: icFormatter }),
  numericColumn({ title: '样本内回撤', dataIndex: 'in_sample_max_drawdown', width: 116, render: percentFormatter }),
  numericColumn({ title: '样本外年化', dataIndex: 'oos_annualized_return', width: 116, render: percentFormatter }),
  numericColumn({ title: '样本外总收益', dataIndex: 'oos_total_return', width: 126, render: percentFormatter }),
  numericColumn({ title: '样本外夏普', dataIndex: 'oos_sharpe', width: 112, render: icFormatter }),
  numericColumn({ title: '样本外卡玛', dataIndex: 'oos_calmar', width: 112, render: icFormatter }),
  numericColumn({ title: '样本外回撤', dataIndex: 'oos_max_drawdown', width: 116, render: percentFormatter }),
  numericColumn({ title: '年化波动', dataIndex: 'annualized_volatility', width: 112, render: percentFormatter }),
  numericColumn({ title: '持仓', dataIndex: 'max_positions', width: 76, render: numberFormatter }),
  numericColumn({ title: '卖出倍数', dataIndex: 'sell_rank_multiplier', width: 96, render: icFormatter }),
  numericColumn({ title: '胜率', dataIndex: 'win_rate', width: 90, render: percentFormatter }),
  numericColumn({ title: '交易', dataIndex: 'trade_count', width: 82, render: numberFormatter }),
  { title: '参数', dataIndex: 'params_label', width: 800 },
];

const getCorrelationColumns = components => [
  { title: '子因子', dataIndex: 'component_label', width: 220, fixed: 'left' },
  ...(components || []).map(component => ({
    title: component.factor_label || component.component_key,
    dataIndex: component.component_key,
    align: 'right',
    render: icFormatter,
  })),
];

const formatMomentumWeightsText = weights => {
  const source = weights || {};
  return MOMENTUM_WEIGHT_WINDOWS
    .map(window => {
      const value = source[String(window)] ?? source[window] ?? 0;
      return `${window}:${Number(value || 0).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') || '0'}`;
    })
    .join(' / ');
};

const formatFactorWeight = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toFixed(4);
};

const getRebalanceFrequencyLabel = value => REBALANCE_FREQUENCY_LABELS[value] || value || '-';

const FactorLab = ({ initialTab = 'single' }) => {
  const [form] = Form.useForm();
  const [compositeForm] = Form.useForm();
  const [backtestForm] = Form.useForm();
  const [timingForm] = Form.useForm();
  const [activeTab, setActiveTab] = useState(initialTab);
  const [options, setOptions] = useState(null);
  const [result, setResult] = useState(null);
  const [compositeResult, setCompositeResult] = useState(null);
  const [backtestResult, setBacktestResult] = useState(null);
  const [timingResult, setTimingResult] = useState(null);
  const [backtestSearchJob, setBacktestSearchJob] = useState(null);
  const [backtestSearchObjective, setBacktestSearchObjective] = useState('annualized_return');
  const [backtestSearchWindowBucketCount, setBacktestSearchWindowBucketCount] = useState(20);
  const [backtestSearchFactorBucketCount, setBacktestSearchFactorBucketCount] = useState(20);
  const [backtestSearchMaxPositions, setBacktestSearchMaxPositions] = useState('7');
  const [backtestSearchSellMultipliers, setBacktestSearchSellMultipliers] = useState('2');
  const [backtestSearchRows, setBacktestSearchRows] = useState([]);
  const [backtestSearchResultsLoading, setBacktestSearchResultsLoading] = useState(false);
  const [backtestSearchTableState, setBacktestSearchTableState] = useState({
    current: 1,
    pageSize: 20,
    sortField: 'objective_value',
    sortOrder: 'descend',
    filters: {},
    total: 0,
  });
  const backtestSearchTableStateRef = useRef(backtestSearchTableState);
  const [selectedCombo, setSelectedCombo] = useState(null);
  const [loadingOptions, setLoadingOptions] = useState(false);
  const [running, setRunning] = useState(false);
  const [compositeRunning, setCompositeRunning] = useState(false);
  const [backtestRunning, setBacktestRunning] = useState(false);
  const [timingRunning, setTimingRunning] = useState(false);
  const [backtestSearchStarting, setBacktestSearchStarting] = useState(false);
  const [backtestSearchHistoryLoading, setBacktestSearchHistoryLoading] = useState(false);
  const [applyingSearchCaseIndex, setApplyingSearchCaseIndex] = useState(null);
  const [analyzingSearchCaseIndex, setAnalyzingSearchCaseIndex] = useState(null);
  const [selectingCombo, setSelectingCombo] = useState(false);

  const selectedFactorKey = Form.useWatch('factor', form);
  const selectedHeatmapMetric = Form.useWatch('heatmap_metric', form);
  const selectedTimingHeatmapMetric = Form.useWatch('heatmap_metric', timingForm);
  const selectedHeatmapWindows = Form.useWatch('heatmap_windows', form);
  const compositeLegs = Form.useWatch('legs', compositeForm);
  const backtestLegs = Form.useWatch('legs', backtestForm);
  const selectedFactor = useMemo(() => (
    (options?.factors || []).find(item => item.key === selectedFactorKey)
  ), [options, selectedFactorKey]);
  const showMomentumWeights = Boolean(
    selectedFactor?.supports_mixed_windows
    && normalizeHeatmapWindows(selectedHeatmapWindows, []).some(isMixedWindow)
  );

  useEffect(() => {
    backtestSearchTableStateRef.current = backtestSearchTableState;
  }, [backtestSearchTableState]);

  const loadOptions = useCallback(async () => {
    setLoadingOptions(true);
    try {
      const { data } = await request.get('/api/factor-lab/options');
      setOptions(data);
      form.setFieldsValue(normalizeDefaultRequest(data.default_request));
      compositeForm.setFieldsValue(normalizeCompositeDefaultRequest(data.default_composite_request));
      backtestForm.setFieldsValue(normalizeBacktestDefaultRequest(data.default_backtest_request));
      timingForm.setFieldsValue(normalizeTimingDefaultRequest(data.default_timing_request));
    } catch (error) {
      message.error(getErrorMessage(error, '加载因子实验室配置失败'));
    } finally {
      setLoadingOptions(false);
    }
  }, [form, compositeForm, backtestForm, timingForm]);

  const loadBacktestSearchResults = useCallback(async (nextState = {}) => {
    const queryState = {
      ...backtestSearchTableStateRef.current,
      ...nextState,
    };
    setBacktestSearchResultsLoading(true);
    try {
      const { data } = await request.get('/api/factor-lab/backtest-search/results', {
        params: {
          page: queryState.current || 1,
          page_size: queryState.pageSize || 20,
          sort_field: queryState.sortField || 'objective_value',
          sort_order: queryState.sortOrder || 'descend',
          filters: JSON.stringify(queryState.filters || {}),
        },
      });
      setBacktestSearchRows(data.rows || []);
      const tableState = {
        ...backtestSearchTableStateRef.current,
        ...queryState,
        current: data.page || queryState.current || 1,
        pageSize: data.page_size || queryState.pageSize || 20,
        sortField: data.sort_field || queryState.sortField || 'objective_value',
        sortOrder: data.sort_order || queryState.sortOrder || 'descend',
        filters: data.filters || queryState.filters || {},
        total: data.total || 0,
      };
      backtestSearchTableStateRef.current = tableState;
      setBacktestSearchTableState(tableState);
    } catch (error) {
      message.error(getErrorMessage(error, '加载批量搜索结果失败'));
    } finally {
      setBacktestSearchResultsLoading(false);
    }
  }, []);

  const loadBacktestSearchHistory = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setBacktestSearchHistoryLoading(true);
    try {
      const { data } = await request.get('/api/factor-lab/backtest-search/history');
      if (data?.status === 'idle' && !data?.total_cases) {
        setBacktestSearchJob(null);
        setBacktestSearchRows([]);
        const tableState = { ...backtestSearchTableStateRef.current, current: 1, total: 0 };
        backtestSearchTableStateRef.current = tableState;
        setBacktestSearchTableState(tableState);
        if (!silent) message.info('还没有历史搜索结果');
      } else {
        setBacktestSearchJob(data);
        await loadBacktestSearchResults({ current: 1 });
        if (!silent) message.success('已加载最近一次搜索结果');
      }
    } catch (error) {
      if (!silent) {
        message.error(getErrorMessage(error, '加载批量搜索历史失败'));
      }
    } finally {
      if (!silent) setBacktestSearchHistoryLoading(false);
    }
  }, [loadBacktestSearchResults]);

  useEffect(() => {
    loadOptions();
    loadBacktestSearchHistory({ silent: true });
  }, [loadOptions, loadBacktestSearchHistory]);

  useEffect(() => {
    const shouldPoll = BACKTEST_SEARCH_RUNNING_STATUSES.includes(backtestSearchJob?.status);
    if (!shouldPoll) return undefined;

    let disposed = false;
    const timer = window.setInterval(async () => {
      try {
        const { data } = await request.get('/api/factor-lab/backtest-search/status');
        if (!disposed) {
          setBacktestSearchJob(data);
        }
      } catch (error) {
        if (!disposed) {
          message.error(getErrorMessage(error, '刷新批量搜索进度失败'));
        }
      }
    }, 2000);

    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [backtestSearchJob?.status]);

  useEffect(() => {
    if (!backtestSearchJob || backtestSearchJob.status === 'idle') return;
    loadBacktestSearchResults();
  }, [backtestSearchJob?.completed_cases, backtestSearchJob?.status, loadBacktestSearchResults]);

  const handleFactorChange = value => {
    const factor = (options?.factors || []).find(item => item.key === value);
    if (!factor) return;
    const nextWindows = factor.supports_windows ? factor.default_windows : DEFAULT_FORM_VALUES.heatmap_windows;
    form.setFieldsValue({
      heatmap_windows: factor.supports_windows ? nextWindows : DEFAULT_FORM_VALUES.heatmap_windows,
      momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
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

  const runSelectedComboAnalysis = useCallback(async (combo) => {
    if (!combo) return;
    const values = await form.validateFields();
    const comboWindow = isMixedWindow(combo.window) ? MIXED_WINDOW_KEY : Number(combo.window);
    const comboWindows = Array.isArray(combo.windows) && combo.windows.length
      ? combo.windows.map(item => Number(item)).filter(item => Number.isFinite(item))
      : (isMixedWindow(comboWindow) ? (selectedFactor?.default_windows || DEFAULT_FORM_VALUES.heatmap_windows) : [Number(comboWindow)]);
    const manualCombo = {
      window: comboWindow,
      window_label: combo.window_label || formatWindowLabel(comboWindow),
      forward_window: Number(combo.forward_window),
      windows: comboWindows,
      selection_mode: 'manual',
      reason: 'heatmap_selection',
    };
    setSelectedCombo(manualCombo);
    setSelectingCombo(true);
    try {
      const payload = buildAnalyzePayload(values, {
        include_heatmap: false,
        heatmap_windows: [manualCombo.window],
        heatmap_forward_windows: [manualCombo.forward_window],
      });
      const { data } = await request.post('/api/factor-lab/analyze', payload, { timeout: 300000 });
      setResult(previous => ({
        ...data,
        metadata: {
          ...data.metadata,
          selected_combo: manualCombo,
        },
        parameter_heatmap: previous?.parameter_heatmap?.length
          ? previous.parameter_heatmap
          : data.parameter_heatmap,
      }));
      message.success('参数组合已切换');
    } catch (error) {
      message.error(getErrorMessage(error, '切换参数组合失败'));
    } finally {
      setSelectingCombo(false);
    }
  }, [form, selectedFactor]);

  const handleCompositeLegFactorChange = (index, value) => {
    const factor = getFactorByKey(options?.factors, value);
    const legs = [...(compositeForm.getFieldValue('legs') || [])];
    legs[index] = {
      ...(legs[index] || {}),
      factor: value,
      window: getDefaultWindowForFactor(factor),
      momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
    };
    compositeForm.setFieldsValue({ legs });
  };

  const handleBacktestLegFactorChange = (index, value) => {
    const factor = getFactorByKey(options?.factors, value);
    const legs = [...(backtestForm.getFieldValue('legs') || [])];
    legs[index] = {
      ...(legs[index] || {}),
      factor: value,
      window: getDefaultWindowForFactor(factor),
      momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
    };
    backtestForm.setFieldsValue({ legs });
  };

  const handleBacktestPoolChange = value => {
    backtestForm.setFieldsValue({ lot_size: isAStockPoolValue(value) ? 100 : 1 });
  };

  const handleTimingTargetChange = value => {
    const symbol = String(value || '').toUpperCase();
    const fearSymbols = new Set(
      (options?.timing_fear_sources || []).map(item => String(item.value || item.symbol || '').toUpperCase())
    );
    if (symbol === A_STOCK_INNO100_SYMBOL || fearSymbols.has(symbol)) {
      timingForm.setFieldsValue({ fear_symbol: symbol });
    }
  };

  const runCompositeAnalysis = async () => {
    const values = await compositeForm.validateFields();
    setCompositeRunning(true);
    try {
      const payload = buildCompositePayload(values);
      const { data } = await request.post('/api/factor-lab/analyze-composite', payload, { timeout: 300000 });
      setCompositeResult(data);
      message.success('组合因子分析完成');
    } catch (error) {
      message.error(getErrorMessage(error, '组合因子分析失败'));
    } finally {
      setCompositeRunning(false);
    }
  };

  const executeBacktestPayload = async (payload, successMessage = '因子回测完成') => {
    setBacktestRunning(true);
    try {
      const { data } = await request.post('/api/factor-lab/backtest', payload, { timeout: 300000 });
      setBacktestResult(data);
      message.success(successMessage);
    } catch (error) {
      message.error(getErrorMessage(error, '因子回测失败'));
    } finally {
      setBacktestRunning(false);
    }
  };

  const runBacktest = async () => {
    const values = await backtestForm.validateFields();
    await executeBacktestPayload(buildBacktestPayload(values));
  };

  const runTimingAnalysis = async () => {
    const values = await timingForm.validateFields();
    setTimingRunning(true);
    try {
      const payload = buildTimingPayload(values);
      const { data } = await request.post('/api/factor-lab/analyze-timing', payload, { timeout: 300000 });
      setTimingResult(data);
      message.success('择时因子分析完成');
    } catch (error) {
      message.error(getErrorMessage(error, '择时因子分析失败'));
    } finally {
      setTimingRunning(false);
    }
  };

  const runSelectedTimingComboAnalysis = useCallback(async combo => {
    if (!combo) return;
    const values = await timingForm.validateFields();
    setTimingRunning(true);
    try {
      const payload = {
        ...buildTimingPayload(values),
        ma_window: Number(combo.ma_window),
        forward_window: Number(combo.forward_window),
        include_heatmap: false,
      };
      timingForm.setFieldsValue({
        ma_window: Number(combo.ma_window),
        forward_window: Number(combo.forward_window),
      });
      const { data } = await request.post('/api/factor-lab/analyze-timing', payload, { timeout: 300000 });
      setTimingResult(previous => ({
        ...data,
        parameter_heatmap: previous?.parameter_heatmap?.length
          ? previous.parameter_heatmap
          : data.parameter_heatmap,
      }));
      message.success('择时参数组合已切换');
    } catch (error) {
      message.error(getErrorMessage(error, '切换择时参数组合失败'));
    } finally {
      setTimingRunning(false);
    }
  }, [timingForm]);

  const startBacktestSearch = async () => {
    const values = await backtestForm.validateFields();
    const baseRequest = buildBacktestPayload(values);
    if (
      (String(backtestSearchObjective).startsWith('in_sample_') || String(backtestSearchObjective).startsWith('oos_'))
      && !baseRequest.oos_start_date
    ) {
      message.warning('选择样本内/样本外目标时，请先设置样本外起始日期');
      return;
    }
    let maxPositionsCandidates;
    let sellMultiplierCandidates;
    try {
      maxPositionsCandidates = parseCandidateNumbers(backtestSearchMaxPositions, {
        integer: true,
        min: 1,
        max: 100,
        fallback: [baseRequest.max_positions],
        label: '持仓数候选项',
      });
      sellMultiplierCandidates = parseCandidateNumbers(backtestSearchSellMultipliers, {
        min: 1,
        max: 10,
        fallback: [baseRequest.sell_rank_multiplier],
        label: '卖出倍数候选项',
      });
    } catch (error) {
      message.warning(error.message);
      return;
    }
    setBacktestSearchStarting(true);
    try {
      const resetTableState = {
        ...backtestSearchTableStateRef.current,
        current: 1,
        sortField: 'objective_value',
        sortOrder: 'descend',
        filters: {},
        total: 0,
      };
      backtestSearchTableStateRef.current = resetTableState;
      setBacktestSearchTableState(resetTableState);
      setBacktestSearchRows([]);
      const payload = {
        request: baseRequest,
        objective: backtestSearchObjective,
        window_weight_bucket_count: Number(backtestSearchWindowBucketCount ?? 20),
        factor_weight_bucket_count: Number(backtestSearchFactorBucketCount ?? 20),
        max_positions_candidates: maxPositionsCandidates,
        sell_rank_multiplier_candidates: sellMultiplierCandidates,
      };
      const { data } = await request.post('/api/factor-lab/backtest-search/start', payload, { timeout: 60000 });
      setBacktestSearchJob(data);
      message.success('批量搜索已启动');
    } catch (error) {
      message.error(getErrorMessage(error, '启动批量搜索失败'));
    } finally {
      setBacktestSearchStarting(false);
    }
  };

  const cancelBacktestSearch = async () => {
    if (!backtestSearchRunning) return;
    try {
      const { data } = await request.post('/api/factor-lab/backtest-search/cancel');
      setBacktestSearchJob(data);
      message.success('已请求取消批量搜索');
    } catch (error) {
      message.error(getErrorMessage(error, '取消批量搜索失败'));
    }
  };

  const applyBacktestSearchRow = async row => {
    if (!row?.request) {
      message.warning('该搜索结果缺少可回填参数');
      return;
    }
    setApplyingSearchCaseIndex(row.case_index);
    try {
      const nextValues = normalizeBacktestDefaultRequest(row.request);
      backtestForm.setFieldsValue(nextValues);
      await executeBacktestPayload(row.request, '已应用参数并完成回测');
    } finally {
      setApplyingSearchCaseIndex(null);
    }
  };

  const analyzeBacktestSearchRow = async row => {
    if (!row?.request) {
      message.warning('该搜索结果缺少可分析参数');
      return;
    }
    setAnalyzingSearchCaseIndex(row.case_index);
    try {
      const legs = row.request.legs || [];
      if (legs.length === 1) {
        const [leg] = legs;
        const currentValues = form.getFieldsValue();
        const currentCompositeValues = compositeForm.getFieldsValue();
        const heatmapWindow = isMixedWindow(leg.window) ? MIXED_WINDOW_KEY : Number(leg.window);
        const nextValues = normalizeDefaultRequest({
          ...row.request,
          factor: leg.factor,
          bucket_count: currentValues.bucket_count || DEFAULT_FORM_VALUES.bucket_count,
          neutralization: leg.neutralization || 'none',
          standardization: leg.standardization || 'rank_percentile',
          heatmap_metric: currentValues.heatmap_metric || DEFAULT_HEATMAP_METRIC,
          heatmap_windows: [heatmapWindow],
          heatmap_forward_windows: [currentCompositeValues.forward_window || DEFAULT_COMPOSITE_VALUES.forward_window],
          momentum_weights: normalizeMomentumWeights(leg.momentum_weights),
        });
        form.setFieldsValue(nextValues);
        setActiveTab('single');
        setRunning(true);
        const payload = buildAnalyzePayload(nextValues);
        const { data } = await request.post('/api/factor-lab/analyze', payload, { timeout: 300000 });
        setResult(data);
        setSelectedCombo(data?.metadata?.selected_combo || null);
        message.success('已按该参数组合完成因子分析');
        return;
      }

      const currentCompositeValues = compositeForm.getFieldsValue();
      const nextValues = normalizeCompositeDefaultRequest({
        ...row.request,
        bucket_count: currentCompositeValues.bucket_count || DEFAULT_COMPOSITE_VALUES.bucket_count,
        forward_window: currentCompositeValues.forward_window || DEFAULT_COMPOSITE_VALUES.forward_window,
        min_listing_days: row.request.min_listing_days ?? DEFAULT_MIN_LISTING_DAYS,
      });
      compositeForm.setFieldsValue(nextValues);
      setActiveTab('composite');
      setCompositeRunning(true);
      const payload = buildCompositePayload(nextValues);
      const { data } = await request.post('/api/factor-lab/analyze-composite', payload, { timeout: 300000 });
      setCompositeResult(data);
      message.success('已按该参数组合完成因子分析');
    } catch (error) {
      message.error(getErrorMessage(error, '查看该参数组合的因子分析失败'));
    } finally {
      setRunning(false);
      setCompositeRunning(false);
      setAnalyzingSearchCaseIndex(null);
    }
  };

  const factorSelectOptions = useMemo(() => buildFactorSelectOptions(options?.factors), [options]);
  const windowOptions = useMemo(() => {
    const baseOptions = (options?.windows || [20, 60, 120]).map(item => ({
      label: `${item}日`,
      value: item,
    }));
    if (selectedFactor?.supports_mixed_windows) {
      baseOptions.push({ label: '多窗口合成', value: MIXED_WINDOW_KEY });
    }
    return baseOptions;
  }, [options, selectedFactor]);
  const forwardOptions = useMemo(() => {
    const values = [...new Set([...(options?.forward_windows || [5, 20, 60]), 10, 120])].sort((a, b) => a - b);
    return values.map(item => ({ label: `T+${item}`, value: item }));
  }, [options]);
  const heatmapMetricOptions = useMemo(() => (
    (options?.heatmap_metrics || [{ key: DEFAULT_HEATMAP_METRIC, label: '非重叠年化多空差' }])
      .map(item => ({ label: item.label, value: item.key }))
  ), [options]);
  const timingHeatmapMetricOptions = useMemo(() => (
    (options?.timing_heatmap_metrics || DEFAULT_TIMING_HEATMAP_METRICS)
      .map(item => ({ label: item.label, value: item.key }))
  ), [options]);
  const neutralizationOptions = useMemo(() => (
    (options?.neutralization_options || [
      { key: 'none', label: '不做中性化' },
      { key: 'sector', label: '行业大类中性化（Sector）' },
      { key: 'sector_market_cap', label: '行业大类+市值中性化' },
      { key: 'fine_industry', label: '细行业中性化（Industry，小样本回退Sector）' },
      { key: 'fine_industry_market_cap', label: '细行业+市值中性化（小样本回退Sector）' },
    ]).map(item => ({ label: item.label, value: item.key }))
  ), [options]);
  const standardizationOptions = useMemo(() => (
    (options?.standardization_options || [
      { key: 'none', label: '不标准化' },
      { key: 'zscore', label: '截面 Z-Score' },
      { key: 'rank_percentile', label: '截面排名分位' },
    ]).map(item => ({ label: item.label, value: item.key }))
  ), [options]);
  const rebalanceFrequencyOptions = useMemo(() => REBALANCE_FREQUENCY_OPTIONS, []);
  const timingMaWindowOptions = useMemo(() => [1, 5, 20].map(item => ({
    label: item === 1 ? '原始值' : `${item}日均值`,
    value: item,
  })), []);
  const timingFearSourceOptions = useMemo(() => (
    (options?.timing_fear_sources || [{ label: 'CNN Fear & Greed', value: 'CNN*.US' }])
      .map(item => ({
        label: item.start_date && item.end_date
          ? `${item.label}（${item.start_date} ~ ${item.end_date}）`
          : item.label,
        value: item.value || item.symbol,
      }))
  ), [options]);
  const timingTargetOptions = useMemo(() => (
    options?.timing_target_options || [
      { label: 'SOXL', value: 'SOXL.US' },
      { label: 'TQQQ', value: 'TQQQ.US' },
      { label: 'QQQ', value: 'QQQ.US' },
      { label: 'SPY', value: 'SPY.US' },
    ]
  ), [options]);
  const summary = result?.summary || {};
  const metadata = result?.metadata || {};
  const oosSummary = result?.oos_summary || {};
  const hasOosSummary = Number(oosSummary.samples || 0) > 0;
  const bucketRows = result?.bucket_returns || [];
  const factorDistributionRows = result?.factor_distribution || [];
  const icRows = result?.rank_ic_series || [];
  const heatmapRows = result?.parameter_heatmap || [];
  const nonOverlapSummary = result?.non_overlapping_summary || {};
  const nonOverlapRows = result?.non_overlapping_offsets || [];
  const yearlyRows = result?.yearly_stability || [];
  const heatmapMetric = selectedHeatmapMetric || metadata.heatmap_metric || DEFAULT_HEATMAP_METRIC;
  const heatmapMetricMeta = getHeatmapMetricMeta(heatmapMetric, options?.heatmap_metrics);
  const selectedComboText = selectedCombo
    ? `${selectedCombo.window_label || formatWindowLabel(selectedCombo.window)} × T+${selectedCombo.forward_window}`
    : '-';
  const heatmapEvents = useMemo(() => ({
    click: params => {
      if (params.seriesType !== 'heatmap' || !Array.isArray(params.value)) return;
      const validRows = heatmapRows.filter(item => getHeatmapValue(item, heatmapMetric) !== null && getHeatmapValue(item, heatmapMetric) !== undefined);
      const windowItems = getHeatmapWindowItems(validRows);
      const windowKeys = windowItems.map(item => item.key);
      const forwards = [...new Set(validRows.map(item => item.forward_window))].sort((a, b) => a - b);
      const row = validRows.find(item => (
        Number(item.forward_window) === Number(forwards[params.value[0]])
        && getWindowKey(item.window) === windowKeys[params.value[1]]
      ));
      if (row) {
        runSelectedComboAnalysis(row);
      }
    },
  }), [heatmapRows, heatmapMetric, runSelectedComboAnalysis]);
  const compositeSummary = compositeResult?.summary || {};
  const compositeMetadata = compositeResult?.metadata || {};
  const compositeOosSummary = compositeResult?.oos_summary || {};
  const hasCompositeOosSummary = Number(compositeOosSummary.samples || 0) > 0;
  const compositeBucketRows = compositeResult?.bucket_returns || [];
  const compositeFactorDistributionRows = compositeResult?.factor_distribution || [];
  const compositeIcRows = compositeResult?.rank_ic_series || [];
  const compositeNonOverlapSummary = compositeResult?.non_overlapping_summary || {};
  const compositeNonOverlapRows = compositeResult?.non_overlapping_offsets || [];
  const compositeYearlyRows = compositeResult?.yearly_stability || [];
  const componentRows = compositeResult?.component_ic || [];
  const componentCorrelationRows = compositeResult?.component_correlation || [];
  const componentCorrelationColumns = getCorrelationColumns(compositeMetadata.components || []);
  const backtestMetrics = backtestResult?.metrics || {};
  const backtestMetadata = backtestResult?.metadata || {};
  const backtestEquityRows = backtestResult?.equity_curve || [];
  const backtestBenchmarkRows = backtestResult?.benchmark_curve || [];
  const backtestYearlyRows = backtestResult?.yearly_stats || [];
  const backtestHoldingRows = backtestResult?.current_holdings || [];
  const backtestTradeRows = backtestResult?.trades || [];
  const backtestComponents = backtestMetadata.components || [];
  const timingSummary = timingResult?.summary || {};
  const timingMetadata = timingResult?.metadata || {};
  const timingBucketRows = timingResult?.bucket_returns || [];
  const timingFactorDistributionRows = timingResult?.factor_distribution || [];
  const timingIcRows = timingResult?.rank_ic_series || [];
  const timingHeatmapRows = timingResult?.parameter_heatmap || [];
  const timingHeatmapMetrics = options?.timing_heatmap_metrics || DEFAULT_TIMING_HEATMAP_METRICS;
  const timingHeatmapMetric = selectedTimingHeatmapMetric
    || timingMetadata.heatmap_metric
    || DEFAULT_TIMING_HEATMAP_METRIC;
  const timingNonOverlapSummary = timingResult?.non_overlapping_summary || {};
  const timingNonOverlapRows = timingResult?.non_overlapping_offsets || [];
  const timingYearlyRows = timingResult?.yearly_stability || [];
  const timingSelectedCombo = timingMetadata.ma_window ? {
    ma_window: timingMetadata.ma_window,
    forward_window: timingMetadata.forward_window,
  } : null;
  const timingHeatmapEvents = useMemo(() => ({
    click: params => {
      if (params.seriesType !== 'heatmap' || !Array.isArray(params.value)) return;
      const validRows = timingHeatmapRows.filter(item => (
        getTimingHeatmapValue(item, timingHeatmapMetric) !== null
        && getTimingHeatmapValue(item, timingHeatmapMetric) !== undefined
      ));
      const maWindows = [...new Set(validRows.map(item => item.ma_window))].sort((a, b) => Number(a) - Number(b));
      const forwards = [...new Set(validRows.map(item => item.forward_window))].sort((a, b) => a - b);
      const row = validRows.find(item => (
        Number(item.ma_window) === Number(maWindows[params.value[1]])
        && Number(item.forward_window) === Number(forwards[params.value[0]])
      ));
      if (row) {
        void runSelectedTimingComboAnalysis(row);
      }
    },
  }), [timingHeatmapRows, timingHeatmapMetric, runSelectedTimingComboAnalysis]);
  const backtestSearchSummary = backtestSearchJob?.summary || {};
  const backtestSearchRunning = BACKTEST_SEARCH_RUNNING_STATUSES.includes(backtestSearchJob?.status);
  const effectiveBacktestSearchObjective = backtestSearchJob?.objective || backtestSearchObjective;
  const backtestSearchColumns = useMemo(
    () => getBacktestSearchColumns(
      effectiveBacktestSearchObjective,
      applyBacktestSearchRow,
      analyzeBacktestSearchRow,
      applyingSearchCaseIndex,
      analyzingSearchCaseIndex,
    ),
    [effectiveBacktestSearchObjective, applyingSearchCaseIndex, analyzingSearchCaseIndex],
  );
  const backtestSearchObjectiveOptions = useMemo(() => (
    (options?.backtest_search_objectives || DEFAULT_BACKTEST_SEARCH_OBJECTIVES)
      .map(item => ({ label: item.label, value: item.key }))
  ), [options]);
  const isDatabaseTab = activeTab === 'db';
  const handleRun = activeTab === 'composite'
    ? runCompositeAnalysis
    : (activeTab === 'backtest' ? runBacktest : (activeTab === 'timing' ? runTimingAnalysis : runAnalysis));
  const activeRunning = activeTab === 'composite'
    ? compositeRunning
    : (activeTab === 'backtest' ? backtestRunning : (activeTab === 'timing' ? timingRunning : running));

  return (
    <div className="factor-lab-page">
      <div className="factor-lab-header">
        <Tabs
          className="factor-lab-tabs"
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            { key: 'single', label: '单因子' },
            { key: 'composite', label: '组合因子' },
            { key: 'timing', label: '择时因子' },
            { key: 'backtest', label: '因子回测' },
            { key: 'db', label: 'DB' },
          ]}
        />
        {!isDatabaseTab && (
          <Space>
            <Button icon={<ReloadOutlined />} onClick={loadOptions} loading={loadingOptions} />
            <Button type="primary" icon={<PlayCircleOutlined />} onClick={handleRun} loading={activeRunning}>
              运行
            </Button>
          </Space>
        )}
      </div>

      {activeTab === 'db' && <DatabaseManager />}

      {activeTab === 'single' && (
      <>
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
                <Form.Item name="neutralization" label="中性化" rules={[{ required: true }]}>
                  <Select options={neutralizationOptions} />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="standardization" label="标准化" rules={[{ required: true }]}>
                  <Select options={standardizationOptions} />
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
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="oos_start_date" label="样本外起始">
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
              {showMomentumWeights && MOMENTUM_WEIGHT_WINDOWS.map(window => (
                <Col xs={8} sm={8} md={4} lg={3} key={window}>
                  <Form.Item name={['momentum_weights', String(window)]} label={`${window}日权重`}>
                    <InputNumber
                      min={0}
                      step={0.05}
                      precision={4}
                      controls
                      className="factor-lab-full"
                    />
                  </Form.Item>
                </Col>
              ))}
            </Row>
            <Form.Item name="heatmap_metric" hidden>
              <Input />
            </Form.Item>

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
        <Spin spinning={running || selectingCombo}>
          <Card
            className="factor-lab-heatmap-card"
            title={(
              <Space className="factor-lab-heatmap-title" size={8} wrap>
                <span><FireOutlined /> 参数热力图</span>
                <Select
                  size="small"
                  className="factor-lab-heatmap-metric-select"
                  value={heatmapMetric}
                  options={heatmapMetricOptions}
                  onChange={value => form.setFieldsValue({ heatmap_metric: value })}
                />
              </Space>
            )}
            extra={<Tag color="blue">当前：{selectedComboText}</Tag>}
            bordered={false}
          >
            {heatmapRows.length ? (
              <ReactECharts
                option={getHeatmapOption(heatmapRows, selectedCombo, heatmapMetric, options?.heatmap_metrics)}
                style={{ height: 360 }}
                onEvents={heatmapEvents}
              />
            ) : <Empty />}
          </Card>

          <div className="factor-lab-metrics">
            <Statistic title="样本" value={summary.samples} formatter={numberFormatter} />
            <Statistic title="交易日" value={summary.trade_dates} formatter={numberFormatter} />
            <Statistic title="Rank IC" value={summary.rank_ic_mean} formatter={icFormatter} />
            <Statistic title="ICIR" value={summary.icir} formatter={icFormatter} />
            <Statistic title="IC t-stat" value={summary.rank_ic_t_stat} formatter={icFormatter} />
            <Statistic title="最高桶收益" value={summary.top_bucket_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="T+n多空差" value={summary.top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="年化多空差" value={summary.annualized_top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="非重叠年化" value={nonOverlapSummary.annualized_median_pct} formatter={percentFormatter} />
            <Statistic title="多空 t-stat" value={summary.spread_t_stat} formatter={icFormatter} />
            <Statistic title="单调性" value={summary.monotonicity_spearman} formatter={icFormatter} />
            <Statistic title="相邻命中" value={summary.adjacent_hit_rate_pct} formatter={percentFormatter} />
            <Statistic
              title="正收益年份"
              value={summary.positive_spread_years}
              suffix={summary.total_years ? `/${summary.total_years}` : ''}
              formatter={numberFormatter}
            />
          </div>

          {hasOosSummary && (
            <Card className="factor-lab-oos-card" title="样本外摘要" bordered={false}>
              <div className="factor-lab-metrics factor-lab-metrics-compact">
                <Statistic title="样本" value={oosSummary.samples} formatter={numberFormatter} />
                <Statistic title="交易日" value={oosSummary.trade_dates} formatter={numberFormatter} />
                <Statistic title="Rank IC" value={oosSummary.rank_ic_mean} formatter={icFormatter} />
                <Statistic title="IC t-stat" value={oosSummary.rank_ic_t_stat} formatter={icFormatter} />
                <Statistic title="T+n多空差" value={oosSummary.top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
                <Statistic title="年化多空差" value={oosSummary.annualized_top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
                <Statistic title="非重叠年化" value={oosSummary.non_overlap_annualized_median_pct} formatter={percentFormatter} />
                <Statistic title="多空 t-stat" value={oosSummary.spread_t_stat} formatter={icFormatter} />
                <Statistic title="单调性" value={oosSummary.monotonicity_spearman} formatter={icFormatter} />
                <Statistic title="相邻命中" value={oosSummary.adjacent_hit_rate_pct} formatter={percentFormatter} />
              </div>
            </Card>
          )}

          <Alert
            className="factor-lab-meta"
            type="info"
            showIcon
            message={(
              <Space size={12} wrap>
                <span>{metadata.start_date} 至 {metadata.end_date}</span>
                <span>{metadata.universe_symbols} 只股票</span>
                {metadata.min_listing_days !== undefined && <span>上市满 {metadata.min_listing_days} 天</span>}
                {metadata.oos_start_date && <span>样本外自 {metadata.oos_start_date}</span>}
                <span>{metadata.price_rows?.toLocaleString?.('zh-CN') || metadata.price_rows} 行行情</span>
                <span>{selectedComboText}</span>
                {heatmapMetricMeta.label && <span>热力图：{heatmapMetricMeta.label}</span>}
                {metadata.neutralization_label && <span>{metadata.neutralization_label}</span>}
                {metadata.standardization_label && <span>{metadata.standardization_label}</span>}
                {metadata.industry_snapshot_mode && <span>行业快照 {numberFormatter(metadata.industry_rows)} 行</span>}
                {metadata.neutralization_warning && <Tag color="orange">{metadata.neutralization_warning}</Tag>}
                {metadata.factor_direction_adjusted && <span>已按方向反转因子值</span>}
                <span>{numberFormatter(summary.elapsed_ms)} ms</span>
              </Space>
            )}
          />

          <Row gutter={[12, 12]}>
            <Col xs={24}>
              <Card title={<Space><BarChartOutlined />因子值分布（等宽）</Space>} bordered={false}>
                {factorDistributionRows.length ? (
                  <ReactECharts option={getFactorDistributionOption(factorDistributionRows)} style={{ height: 420 }} />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

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
                  scroll={{ x: 1040 }}
                />
              </Card>
            </Col>
          </Row>
        </Spin>
      )}
      </>
      )}

      {activeTab === 'composite' && (
      <>
      <Card className="factor-lab-control-card" bordered={false}>
        <Spin spinning={loadingOptions}>
          <Form form={compositeForm} layout="vertical" initialValues={DEFAULT_COMPOSITE_VALUES}>
            <Row gutter={[12, 8]}>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="pool" label="股票池" rules={[{ required: true }]}>
                  <Select options={(options?.pools || []).map(item => ({ label: item.label, value: item.key }))} />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="bucket_count" label="分桶" rules={[{ required: true }]}>
                  <InputNumber min={2} max={20} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="forward_window" label="收益窗口" rules={[{ required: true }]}>
                  <Select options={forwardOptions} />
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
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="oos_start_date" label="样本外起始">
                  <DatePicker className="factor-lab-full" allowClear />
                </Form.Item>
              </Col>
            </Row>

            <Form.List name="legs">
              {(fields, { add, remove }) => (
                <div className="factor-lab-leg-list">
                  <div className="factor-lab-leg-list-header">
                    <Text strong>因子腿</Text>
                    <Button
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() => add(buildDefaultCompositeLeg('raw_momentum', getFactorByKey(options?.factors, 'raw_momentum')))}
                    >
                      添加因子
                    </Button>
                  </div>
                  {fields.map(field => {
                    const leg = (compositeLegs || [])[field.name] || {};
                    const factor = getFactorByKey(options?.factors, leg.factor);
                    return (
                      <div key={field.key} className="factor-lab-leg-row">
                        <Row gutter={[12, 8]} align="middle">
                          <Col xs={24} md={7}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'factor']}
                              label="因子"
                              rules={[{ required: true, message: '请选择因子' }]}
                            >
                              <Select
                                options={factorSelectOptions}
                                onChange={value => handleCompositeLegFactorChange(field.name, value)}
                              />
                            </Form.Item>
                          </Col>
                          <Col xs={12} md={4}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'window']}
                              label="窗口"
                              rules={[{ required: true, message: '请选择窗口' }]}
                            >
                              <Select
                                options={getWindowOptionsForFactor(factor, options?.windows)}
                                disabled={factor && !factor.supports_windows}
                              />
                            </Form.Item>
                          </Col>
                          <Col xs={12} md={3}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'weight']}
                              label="权重"
                              rules={[{ required: true, message: '请输入权重' }]}
                            >
                              <InputNumber min={-100} max={100} step={0.1} precision={4} controls className="factor-lab-full" />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={5}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'neutralization']}
                              label="中性化"
                              rules={[{ required: true, message: '请选择中性化' }]}
                            >
                              <Select options={neutralizationOptions} />
                            </Form.Item>
                          </Col>
                          <Col xs={20} md={4}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'standardization']}
                              label="标准化"
                              rules={[{ required: true, message: '请选择标准化' }]}
                            >
                              <Select options={standardizationOptions} />
                            </Form.Item>
                          </Col>
                          <Col xs={4} md={1}>
                            <Button
                              danger
                              icon={<DeleteOutlined />}
                              disabled={fields.length <= 2}
                              onClick={() => remove(field.name)}
                            />
                          </Col>
                        </Row>
                        {isMixedWindow(leg.window) && (
                          <Row gutter={[12, 8]} className="factor-lab-leg-subrow">
                            {MOMENTUM_WEIGHT_WINDOWS.map(window => (
                              <Col xs={8} sm={8} md={4} lg={3} key={window}>
                                <Form.Item name={[field.name, 'momentum_weights', String(window)]} label={`${window}日权重`}>
                                  <InputNumber min={0} step={0.05} precision={4} controls className="factor-lab-full" />
                                </Form.Item>
                              </Col>
                            ))}
                          </Row>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Form.List>

            <div className="factor-lab-factor-note">
              <Text type="secondary">每个子因子先独立做方向调整、中性化和标准化，再按权重线性合成；权重按绝对值总和归一，负权重可用于反向暴露。</Text>
            </div>
          </Form>
        </Spin>
      </Card>

      {!compositeResult && (
        <div className="factor-lab-empty">
          <ExperimentOutlined />
          <Text type="secondary">配置因子腿后运行组合分析</Text>
        </div>
      )}

      {compositeResult && (
        <Spin spinning={compositeRunning}>
          <div className="factor-lab-metrics">
            <Statistic title="样本" value={compositeSummary.samples} formatter={numberFormatter} />
            <Statistic title="交易日" value={compositeSummary.trade_dates} formatter={numberFormatter} />
            <Statistic title="Rank IC" value={compositeSummary.rank_ic_mean} formatter={icFormatter} />
            <Statistic title="ICIR" value={compositeSummary.icir} formatter={icFormatter} />
            <Statistic title="IC t-stat" value={compositeSummary.rank_ic_t_stat} formatter={icFormatter} />
            <Statistic title="最高桶收益" value={compositeSummary.top_bucket_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="T+n多空差" value={compositeSummary.top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="年化多空差" value={compositeSummary.annualized_top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="非重叠年化" value={compositeNonOverlapSummary.annualized_median_pct} formatter={percentFormatter} />
            <Statistic title="多空 t-stat" value={compositeSummary.spread_t_stat} formatter={icFormatter} />
            <Statistic title="单调性" value={compositeSummary.monotonicity_spearman} formatter={icFormatter} />
            <Statistic title="相邻命中" value={compositeSummary.adjacent_hit_rate_pct} formatter={percentFormatter} />
          </div>

          {hasCompositeOosSummary && (
            <Card className="factor-lab-oos-card" title="样本外摘要" bordered={false}>
              <div className="factor-lab-metrics factor-lab-metrics-compact">
                <Statistic title="样本" value={compositeOosSummary.samples} formatter={numberFormatter} />
                <Statistic title="交易日" value={compositeOosSummary.trade_dates} formatter={numberFormatter} />
                <Statistic title="Rank IC" value={compositeOosSummary.rank_ic_mean} formatter={icFormatter} />
                <Statistic title="IC t-stat" value={compositeOosSummary.rank_ic_t_stat} formatter={icFormatter} />
                <Statistic title="T+n多空差" value={compositeOosSummary.top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
                <Statistic title="非重叠年化" value={compositeOosSummary.non_overlap_annualized_median_pct} formatter={percentFormatter} />
              </div>
            </Card>
          )}

          <Alert
            className="factor-lab-meta"
            type="info"
            showIcon
            message={(
              <Space size={12} wrap>
                <span>{compositeMetadata.start_date} 至 {compositeMetadata.end_date}</span>
                <span>{compositeMetadata.universe_symbols} 只股票</span>
                {compositeMetadata.min_listing_days !== undefined && <span>上市满 {compositeMetadata.min_listing_days} 天</span>}
                {compositeMetadata.oos_start_date && <span>样本外自 {compositeMetadata.oos_start_date}</span>}
                <span>{compositeMetadata.price_rows?.toLocaleString?.('zh-CN') || compositeMetadata.price_rows} 行行情</span>
                <span>T+{compositeMetadata.forward_window}</span>
                <span>{compositeMetadata.neutralization_label}</span>
                <span>{compositeMetadata.standardization_label}</span>
                {compositeMetadata.industry_snapshot_mode && <span>行业快照 {numberFormatter(compositeMetadata.industry_rows)} 行</span>}
                {compositeMetadata.neutralization_warning && <Tag color="orange">{compositeMetadata.neutralization_warning}</Tag>}
                <span>{numberFormatter(compositeSummary.elapsed_ms)} ms</span>
              </Space>
            )}
          />

          <Row gutter={[12, 12]}>
            <Col xs={24} xl={12}>
              <Card title="子因子 Rank IC" bordered={false}>
                <Table
                  rowKey="component_key"
                  size="small"
                  columns={componentIcColumns}
                  dataSource={componentRows}
                  pagination={false}
                  scroll={{ x: 980 }}
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="子因子相关矩阵" bordered={false}>
                <Table
                  rowKey="component_key"
                  size="small"
                  columns={componentCorrelationColumns}
                  dataSource={componentCorrelationRows}
                  pagination={false}
                  scroll={{ x: 760 }}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title={<Space><BarChartOutlined />组合因子值分布（等宽）</Space>} bordered={false}>
                {compositeFactorDistributionRows.length ? (
                  <ReactECharts option={getFactorDistributionOption(compositeFactorDistributionRows)} style={{ height: 420 }} />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24} xl={12}>
              <Card title={<Space><BarChartOutlined />组合分桶收益</Space>} bordered={false}>
                {compositeBucketRows.length ? <ReactECharts option={getBucketChartOption(compositeBucketRows)} style={{ height: 340 }} /> : <Empty />}
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title={<Space><ExperimentOutlined />组合 Rank IC</Space>} bordered={false}>
                {compositeIcRows.length ? <ReactECharts option={getIcOption(compositeIcRows)} style={{ height: 340 }} /> : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24} xl={12}>
              <Card title="非重叠统计" bordered={false}>
                {compositeNonOverlapRows.length ? (
                  <>
                    <div className="factor-lab-compact-stats">
                      <span>Offset {numberFormatter(compositeNonOverlapSummary.offsets)}</span>
                      <span>总期数 {numberFormatter(compositeNonOverlapSummary.total_periods)}</span>
                      <span>年化中位 {percentFormatter(compositeNonOverlapSummary.annualized_median_pct)}</span>
                      <span>最好 {percentFormatter(compositeNonOverlapSummary.best_offset_annualized_pct)}</span>
                      <span>最差 {percentFormatter(compositeNonOverlapSummary.worst_offset_annualized_pct)}</span>
                    </div>
                    <Table
                      rowKey="offset"
                      size="small"
                      columns={nonOverlapColumns}
                      dataSource={compositeNonOverlapRows}
                      pagination={{ defaultPageSize: 8, showSizeChanger: true, pageSizeOptions: [8, 20, 50] }}
                      scroll={{ x: 880 }}
                    />
                  </>
                ) : <Empty />}
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="年度稳定性" bordered={false}>
                {compositeYearlyRows.length ? (
                  <Table
                    rowKey="year"
                    size="small"
                    columns={yearlyColumns}
                    dataSource={compositeYearlyRows}
                    pagination={false}
                    scroll={{ x: 980 }}
                  />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title="组合分桶明细" bordered={false}>
                <Table
                  rowKey="bucket"
                  size="small"
                  columns={bucketColumns}
                  dataSource={compositeBucketRows}
                  pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 20, 50] }}
                  scroll={{ x: 1040 }}
                />
              </Card>
            </Col>
          </Row>
        </Spin>
      )}
      </>
      )}

      {activeTab === 'timing' && (
      <>
      <Card className="factor-lab-control-card" bordered={false}>
        <Spin spinning={loadingOptions}>
          <Form form={timingForm} layout="vertical" initialValues={DEFAULT_TIMING_VALUES}>
            <Row gutter={[12, 8]}>
              <Col xs={24} sm={12} md={5} lg={4}>
                <Form.Item name="target_symbol" label="目标标的" rules={[{ required: true }]}>
                  <Select showSearch options={timingTargetOptions} onChange={handleTimingTargetChange} />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={7} lg={6}>
                <Form.Item name="fear_symbol" label="恐贪来源" rules={[{ required: true }]}>
                  <Select showSearch options={timingFearSourceOptions} />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={3} lg={3}>
                <Form.Item name="bucket_count" label="分桶" rules={[{ required: true }]}>
                  <InputNumber min={2} max={20} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={5} lg={4}>
                <Form.Item name="start_date" label="开始日期" rules={[{ required: true }]}>
                  <DatePicker className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={5} lg={4}>
                <Form.Item name="end_date" label="结束日期">
                  <DatePicker className="factor-lab-full" allowClear />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={6} lg={5}>
                <Form.Item name="heatmap_ma_windows" label="贪恐均线窗口" rules={[{ required: true }]}>
                  <Select mode="multiple" maxTagCount="responsive" options={timingMaWindowOptions} />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={5} lg={4}>
                <Form.Item name="heatmap_forward_windows" label="热力图收益窗口" rules={[{ required: true }]}>
                  <Select mode="multiple" maxTagCount="responsive" options={forwardOptions} />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item name="ma_window" hidden><InputNumber /></Form.Item>
            <Form.Item name="forward_window" hidden><InputNumber /></Form.Item>
            <Form.Item name="heatmap_metric" hidden><Input /></Form.Item>
            <div className="factor-lab-factor-note">
              <Text type="secondary">择时因子按日期分桶。热力图会先比较“贪恐均线窗口 × T+n”，详情由当前选中的格子驱动；低桶代表恐慌，高桶代表贪婪。</Text>
            </div>
          </Form>
        </Spin>
      </Card>

      {!timingResult && (
        <div className="factor-lab-empty">
          <ExperimentOutlined />
          <Text type="secondary">选择目标标的和恐贪来源后运行择时分析</Text>
        </div>
      )}

      {timingResult && (
        <Spin spinning={timingRunning}>
          <Card
            className="factor-lab-heatmap-card"
            title={(
              <Space className="factor-lab-heatmap-title" size={8}>
                <FireOutlined />
                <span>择时参数热力图</span>
                <Select
                  size="small"
                  className="factor-lab-heatmap-metric-select"
                  value={timingHeatmapMetric}
                  options={timingHeatmapMetricOptions}
                  onChange={value => timingForm.setFieldsValue({ heatmap_metric: value })}
                />
              </Space>
            )}
            extra={<Tag color="blue">当前：{timingMetadata.ma_window_label} × T+{timingMetadata.forward_window}</Tag>}
            bordered={false}
          >
            {timingHeatmapRows.length ? (
              <ReactECharts
                option={getTimingHeatmapOption(
                  timingHeatmapRows,
                  timingSelectedCombo,
                  timingHeatmapMetric,
                  timingHeatmapMetrics,
                )}
                style={{ height: 340 }}
                onEvents={timingHeatmapEvents}
              />
            ) : <Empty />}
          </Card>

          <div className="factor-lab-metrics">
            <Statistic title="样本" value={timingSummary.samples} formatter={numberFormatter} />
            <Statistic title="交易日" value={timingSummary.trade_dates} formatter={numberFormatter} />
            <Statistic title="时间序列IC" value={timingSummary.rank_ic_mean} formatter={icFormatter} />
            <Statistic title="滚动ICIR" value={timingSummary.icir} formatter={icFormatter} />
            <Statistic title="IC t-stat" value={timingSummary.rank_ic_t_stat} formatter={icFormatter} />
            <Statistic title="高桶收益" value={timingSummary.top_bucket_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="低桶收益" value={timingSummary.bottom_bucket_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="低-高桶" value={timingSummary.low_minus_high_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="年化低高差" value={timingSummary.annualized_low_minus_high_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="高-低桶" value={timingSummary.top_minus_bottom_avg_return_pct} formatter={percentFormatter} />
            <Statistic title="非重叠年化" value={timingNonOverlapSummary.annualized_median_pct} formatter={percentFormatter} />
            <Statistic title="单调性" value={timingSummary.monotonicity_spearman} formatter={icFormatter} />
          </div>

          <Alert
            className="factor-lab-meta"
            type="info"
            showIcon
            message={(
              <Space size={12} wrap>
                <span>{timingMetadata.effective_start_date || timingMetadata.start_date} 至 {timingMetadata.effective_end_date || timingMetadata.end_date}</span>
                <span>目标 {timingMetadata.target_symbol}</span>
                <span>{timingMetadata.fear_label}</span>
                <span>{timingMetadata.ma_window_label}</span>
                <span>T+{timingMetadata.forward_window}</span>
                <span>{numberFormatter(timingMetadata.price_rows)} 行行情</span>
                <span>{numberFormatter(timingMetadata.fear_points)} 条恐贪</span>
                <span>{numberFormatter(timingSummary.elapsed_ms)} ms</span>
              </Space>
            )}
          />

          <Row gutter={[12, 12]}>
            <Col xs={24}>
              <Card title={<Space><BarChartOutlined />择时因子值分布（等宽）</Space>} bordered={false}>
                {timingFactorDistributionRows.length ? (
                  <ReactECharts option={getFactorDistributionOption(timingFactorDistributionRows)} style={{ height: 420 }} />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]}>
            <Col xs={24} xl={12}>
              <Card title={<Space><BarChartOutlined />择时分桶收益</Space>} bordered={false}>
                {timingBucketRows.length ? <ReactECharts option={getBucketChartOption(timingBucketRows)} style={{ height: 340 }} /> : <Empty />}
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title={<Space><ExperimentOutlined />滚动时间序列 IC</Space>} bordered={false}>
                {timingIcRows.length ? <ReactECharts option={getIcOption(timingIcRows)} style={{ height: 340 }} /> : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24} xl={12}>
              <Card title="非重叠统计" bordered={false}>
                {timingNonOverlapRows.length ? (
                  <>
                    <div className="factor-lab-compact-stats">
                      <span>Offset {numberFormatter(timingNonOverlapSummary.offsets)}</span>
                      <span>总期数 {numberFormatter(timingNonOverlapSummary.total_periods)}</span>
                      <span>年化中位 {percentFormatter(timingNonOverlapSummary.annualized_median_pct)}</span>
                      <span>正收益Offset {percentFormatter(timingNonOverlapSummary.positive_period_rate_pct)}</span>
                    </div>
                    <Table
                      rowKey="offset"
                      size="small"
                      columns={nonOverlapColumns}
                      dataSource={timingNonOverlapRows}
                      pagination={{ defaultPageSize: 8, showSizeChanger: true, pageSizeOptions: [8, 20, 50] }}
                      scroll={{ x: 880 }}
                    />
                  </>
                ) : <Empty />}
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="年度稳定性" bordered={false}>
                {timingYearlyRows.length ? (
                  <Table
                    rowKey="year"
                    size="small"
                    columns={yearlyColumns}
                    dataSource={timingYearlyRows}
                    pagination={false}
                    scroll={{ x: 980 }}
                  />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title="择时分桶明细" bordered={false}>
                <Table
                  rowKey="bucket"
                  size="small"
                  columns={bucketColumns}
                  dataSource={timingBucketRows}
                  pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 20, 50] }}
                  scroll={{ x: 1040 }}
                />
              </Card>
            </Col>
          </Row>
        </Spin>
      )}
      </>
      )}

      {activeTab === 'backtest' && (
      <>
      <Card className="factor-lab-control-card" bordered={false}>
        <Spin spinning={loadingOptions}>
          <Form form={backtestForm} layout="vertical" initialValues={DEFAULT_BACKTEST_VALUES}>
            <Row gutter={[12, 8]}>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="pool" label="股票池" rules={[{ required: true }]}>
                  <Select
                    options={(options?.pools || []).map(item => ({ label: item.label, value: item.key }))}
                    onChange={handleBacktestPoolChange}
                  />
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
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="oos_start_date" label="样本外起始">
                  <DatePicker className="factor-lab-full" allowClear />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="initial_capital" label="初始资金" rules={[{ required: true }]}>
                  <InputNumber min={1000} step={10000} precision={2} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="max_positions" label="持仓数" rules={[{ required: true }]}>
                  <InputNumber min={1} max={100} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="sell_rank_multiplier" label="卖出倍数" rules={[{ required: true }]}>
                  <InputNumber min={1} max={10} step={0.25} precision={2} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="rebalance_frequency" label="调仓频率" rules={[{ required: true }]}>
                  <Select options={rebalanceFrequencyOptions} />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="commission_pct" label="手续费%" rules={[{ required: true }]}>
                  <InputNumber min={0} max={10} step={0.01} precision={4} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="slippage_pct" label="滑点%" rules={[{ required: true }]}>
                  <InputNumber min={0} max={10} step={0.01} precision={4} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="lot_size" label="交易单位" rules={[{ required: true }]}>
                  <InputNumber min={1} max={10000} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item name="min_listing_days" label="上市天数" rules={[{ required: true }]}>
                  <InputNumber min={0} max={3650} controls className="factor-lab-full" />
                </Form.Item>
              </Col>
            </Row>

            <Form.List name="legs">
              {(fields, { add, remove }) => (
                <div className="factor-lab-leg-list">
                  <div className="factor-lab-leg-list-header">
                    <Text strong>回测因子</Text>
                    <Button
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() => add(buildDefaultBacktestLeg('raw_momentum', getFactorByKey(options?.factors, 'raw_momentum')))}
                    >
                      添加因子
                    </Button>
                  </div>
                  {fields.map(field => {
                    const leg = (backtestLegs || [])[field.name] || {};
                    const factor = getFactorByKey(options?.factors, leg.factor);
                    return (
                      <div key={field.key} className="factor-lab-leg-row">
                        <Row gutter={[12, 8]} align="middle">
                          <Col xs={24} md={7}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'factor']}
                              label="因子"
                              rules={[{ required: true, message: '请选择因子' }]}
                            >
                              <Select
                                options={factorSelectOptions}
                                onChange={value => handleBacktestLegFactorChange(field.name, value)}
                              />
                            </Form.Item>
                          </Col>
                          <Col xs={12} md={4}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'window']}
                              label="窗口"
                              rules={[{ required: true, message: '请选择窗口' }]}
                            >
                              <Select
                                options={getWindowOptionsForFactor(factor, options?.windows)}
                                disabled={factor && !factor.supports_windows}
                              />
                            </Form.Item>
                          </Col>
                          <Col xs={12} md={3}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'weight']}
                              label="权重"
                              rules={[{ required: true, message: '请输入权重' }]}
                            >
                              <InputNumber min={-100} max={100} step={0.1} precision={4} controls className="factor-lab-full" />
                            </Form.Item>
                          </Col>
                          <Col xs={24} md={5}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'neutralization']}
                              label="中性化"
                              rules={[{ required: true, message: '请选择中性化' }]}
                            >
                              <Select options={neutralizationOptions} />
                            </Form.Item>
                          </Col>
                          <Col xs={20} md={4}>
                            <Form.Item
                              {...field}
                              name={[field.name, 'standardization']}
                              label="标准化"
                              rules={[{ required: true, message: '请选择标准化' }]}
                            >
                              <Select options={standardizationOptions} />
                            </Form.Item>
                          </Col>
                          <Col xs={4} md={1}>
                            <Button
                              danger
                              icon={<DeleteOutlined />}
                              disabled={fields.length <= 1}
                              onClick={() => remove(field.name)}
                            />
                          </Col>
                        </Row>
                        {isMixedWindow(leg.window) && (
                          <Row gutter={[12, 8]} className="factor-lab-leg-subrow">
                            {MOMENTUM_WEIGHT_WINDOWS.map(window => (
                              <Col xs={8} sm={8} md={4} lg={3} key={window}>
                                <Form.Item name={[field.name, 'momentum_weights', String(window)]} label={`${window}日权重`}>
                                  <InputNumber min={0} step={0.05} precision={4} controls className="factor-lab-full" />
                                </Form.Item>
                              </Col>
                            ))}
                          </Row>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Form.List>

            <Row gutter={[12, 8]} className="factor-lab-batch-row">
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item label="批量目标">
                  <Select
                    value={backtestSearchObjective}
                    options={backtestSearchObjectiveOptions}
                    onChange={setBacktestSearchObjective}
                  />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item label="窗口权重分桶">
                  <InputNumber
                    min={0}
                    max={100}
                    precision={0}
                    controls
                    value={backtestSearchWindowBucketCount}
                    onChange={value => setBacktestSearchWindowBucketCount(value ?? 20)}
                    className="factor-lab-full"
                  />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item label="因子权重分桶">
                  <InputNumber
                    min={0}
                    max={100}
                    precision={0}
                    controls
                    value={backtestSearchFactorBucketCount}
                    onChange={value => setBacktestSearchFactorBucketCount(value ?? 20)}
                    className="factor-lab-full"
                  />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item label="持仓数候选">
                  <Input
                    value={backtestSearchMaxPositions}
                    onChange={event => setBacktestSearchMaxPositions(event.target.value)}
                    placeholder="7,10,15"
                  />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item label="卖出倍数候选">
                  <Input
                    value={backtestSearchSellMultipliers}
                    onChange={event => setBacktestSearchSellMultipliers(event.target.value)}
                    placeholder="1.5,2,3"
                  />
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item label=" ">
                  <Button
                    loading={backtestSearchStarting}
                    disabled={backtestSearchRunning}
                    onClick={startBacktestSearch}
                    className="factor-lab-full"
                  >
                    启动搜索
                  </Button>
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item label=" ">
                  <Button
                    loading={backtestSearchHistoryLoading}
                    onClick={() => loadBacktestSearchHistory()}
                    className="factor-lab-full"
                  >
                    查看历史
                  </Button>
                </Form.Item>
              </Col>
              <Col xs={12} sm={6} md={4} lg={3}>
                <Form.Item label=" ">
                  <Button
                    danger
                    disabled={!backtestSearchRunning}
                    onClick={cancelBacktestSearch}
                    className="factor-lab-full"
                  >
                    取消
                  </Button>
                </Form.Item>
              </Col>
            </Row>

            <div className="factor-lab-factor-note">
              <Text type="secondary">默认参数用于复现“美股多因子策略虚拟盘”：风险调整动量 60% + 指数成分权重 40%，默认标准化为截面排名分位，因子间按标准化结果加权。</Text>
            </div>
          </Form>
        </Spin>
      </Card>

      {!backtestResult && !backtestSearchJob && (
        <div className="factor-lab-empty">
          <ExperimentOutlined />
          <Text type="secondary">配置因子和交易参数后运行回测</Text>
        </div>
      )}

      {backtestSearchJob && (
        <Card
          className="factor-lab-table-row"
          title="批量搜索"
          extra={(() => {
            const meta = BACKTEST_SEARCH_STATUS_META[backtestSearchJob.status] || { color: 'default', label: backtestSearchJob.status };
            return <Tag color={meta.color}>{meta.label}</Tag>;
          })()}
          bordered={false}
        >
          <Progress
            percent={Number(backtestSearchJob.progress_pct || 0)}
            status={['failed', 'interrupted'].includes(backtestSearchJob.status) ? 'exception' : (backtestSearchJob.status === 'completed' ? 'success' : (backtestSearchRunning ? 'active' : 'normal'))}
          />
          <div className="factor-lab-compact-stats">
            <span>完成 {numberFormatter(backtestSearchJob.completed_cases)} / {numberFormatter(backtestSearchJob.total_cases)}</span>
            <span>失败 {numberFormatter(backtestSearchJob.failed_cases)}</span>
            <span>结果 {numberFormatter(backtestSearchJob.result_count)}</span>
            <span>并发 {numberFormatter(backtestSearchJob.worker_count)}</span>
            <span>目标 {backtestSearchJob.objective_label}</span>
            <span>{numberFormatter(backtestSearchSummary.elapsed_ms)} ms</span>
          </div>
          {backtestSearchJob.current_case && (
            <Text className="factor-lab-search-current" type="secondary" ellipsis={{ tooltip: backtestSearchJob.current_case }}>
              当前 {backtestSearchJob.current_case}
            </Text>
          )}
          {backtestSearchJob.error && <Alert className="factor-lab-meta" type="error" showIcon message={backtestSearchJob.error} />}
          <Table
            rowKey="case_index"
            size="small"
            loading={backtestSearchResultsLoading}
            columns={backtestSearchColumns}
            dataSource={backtestSearchRows}
            pagination={{
              current: backtestSearchTableState.current,
              pageSize: backtestSearchTableState.pageSize,
              total: backtestSearchTableState.total,
              showSizeChanger: true,
              pageSizeOptions: [20, 50, 100, 200],
              showTotal: total => `共 ${numberFormatter(total)} 条`,
            }}
            onChange={(pagination, filters, sorter) => {
              const sorterItem = Array.isArray(sorter) ? sorter[0] : sorter;
              const nextState = {
                current: pagination.current || 1,
                pageSize: pagination.pageSize || 20,
                sortField: sorterItem?.field || 'objective_value',
                sortOrder: sorterItem?.order || 'descend',
                filters: normalizeSearchTableFilters(filters),
              };
              backtestSearchTableStateRef.current = {
                ...backtestSearchTableStateRef.current,
                ...nextState,
              };
              setBacktestSearchTableState(previous => ({ ...previous, ...nextState }));
              loadBacktestSearchResults(nextState);
            }}
            scroll={{ x: 2940 }}
          />
        </Card>
      )}

      {backtestResult && (
        <Spin spinning={backtestRunning}>
          <Card className="factor-lab-backtest-params" title="回测参数" bordered={false}>
            <div className="factor-lab-param-grid">
              <span>股票池：{backtestMetadata.pool_label || backtestMetadata.pool}</span>
              <span>区间：{backtestMetadata.start_date} 至 {backtestMetadata.end_date}</span>
              {backtestMetadata.oos_start_date && <span>样本外：{backtestMetadata.oos_start_date} 起</span>}
              <span>初始资金：{numberFormatter(backtestMetadata.initial_capital)}</span>
              <span>持仓数：Top{backtestMetadata.max_positions}</span>
              <span>卖出阈值：Top{backtestMetadata.sell_rank_threshold}</span>
              <span>卖出倍数：{formatFactorWeight(backtestMetadata.sell_rank_multiplier)}</span>
              <span>调仓频率：{getRebalanceFrequencyLabel(backtestMetadata.rebalance_frequency)}</span>
              <span>手续费：{percentFormatter(backtestMetadata.commission_pct)}</span>
              <span>滑点：{percentFormatter(backtestMetadata.slippage_pct)}</span>
              <span>交易单位：{numberFormatter(backtestMetadata.lot_size)}</span>
              <span>上市天数：{numberFormatter(backtestMetadata.min_listing_days)}</span>
            </div>
            <div className="factor-lab-param-components">
              {backtestComponents.map(component => (
                <div className="factor-lab-param-component" key={component.component_key}>
                  <Text strong>{component.factor_label || component.factor_key}</Text>
                  <Space size={6} wrap>
                    <Tag>{component.window_label || formatWindowLabel(component.window)}</Tag>
                    <Tag>权重 {formatFactorWeight(component.raw_weight ?? component.weight)}</Tag>
                    <Tag>{component.neutralization_label || component.neutralization}</Tag>
                    <Tag>{component.standardization_label || component.standardization}</Tag>
                    {isMixedWindow(component.window) && (
                      <Tag color="blue">{formatMomentumWeightsText(component.momentum_weights)}</Tag>
                    )}
                  </Space>
                </div>
              ))}
            </div>
          </Card>

          <div className="factor-lab-metrics">
            <Statistic title="总收益" value={backtestMetrics.total_return} formatter={percentFormatter} />
            <Statistic title="年化收益" value={backtestMetrics.annualized_return} formatter={percentFormatter} />
            <Statistic title="最大回撤" value={backtestMetrics.max_drawdown} formatter={percentFormatter} />
            <Statistic title="期末资产" value={backtestMetrics.ending_value} formatter={numberFormatter} />
            <Statistic title="现金" value={backtestMetrics.cash} formatter={numberFormatter} />
            <Statistic title="持仓数" value={backtestMetrics.holding_count} formatter={numberFormatter} />
            <Statistic title="调仓次数" value={backtestMetrics.rebalance_count} formatter={numberFormatter} />
            <Statistic title="交易次数" value={backtestMetrics.trade_count} formatter={numberFormatter} />
            <Statistic title="胜率" value={backtestMetrics.win_rate} formatter={percentFormatter} />
          </div>

          <Alert
            className="factor-lab-meta"
            type={backtestMetadata.replicates_virtual_strategy ? 'success' : 'info'}
            showIcon
            message={(
              <Space size={12} wrap>
                <span>{backtestMetadata.start_date} 至 {backtestMetadata.end_date}</span>
                <span>{backtestMetadata.pool_label}</span>
                <span>{backtestMetadata.universe_symbols} 只股票</span>
                <span>Top{backtestMetadata.max_positions}</span>
                <span>卖出阈值 Top{backtestMetadata.sell_rank_threshold}</span>
                <span>{getRebalanceFrequencyLabel(backtestMetadata.rebalance_frequency)}</span>
                <span>{backtestMetadata.factor_combination_method_label || '子因子标准化后加权'}</span>
                <span>上市满 {backtestMetadata.min_listing_days} 天</span>
                <span>{numberFormatter(backtestMetadata.price_rows)} 行行情</span>
                {backtestMetadata.replicates_virtual_strategy && <Tag color="green">虚拟盘口径</Tag>}
                <span>{numberFormatter(backtestMetadata.elapsed_ms)} ms</span>
              </Space>
            )}
          />

          <Row gutter={[12, 12]}>
            <Col xs={24}>
              <Card title="净值曲线" bordered={false}>
                {backtestEquityRows.length ? (
                  <ReactECharts
                    option={getBacktestEquityOption(backtestEquityRows, backtestBenchmarkRows, backtestMetadata.candidate_etfs)}
                    style={{ height: 380 }}
                  />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24} xl={12}>
              <Card title="当前持仓" bordered={false}>
                <Table
                  rowKey="symbol"
                  size="small"
                  columns={backtestHoldingColumns}
                  dataSource={backtestHoldingRows}
                  pagination={false}
                  scroll={{ x: 760 }}
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card title="年度收益" bordered={false}>
                <Table
                  rowKey="year"
                  size="small"
                  columns={backtestYearlyColumns}
                  dataSource={backtestYearlyRows}
                  pagination={false}
                  scroll={{ x: 900 }}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title="成交记录" bordered={false}>
                <Table
                  rowKey={(row, index) => `${row.date}-${row.action}-${row.symbol}-${index}`}
                  size="small"
                  columns={backtestTradeColumns}
                  dataSource={backtestTradeRows}
                  pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
                  scroll={{ x: 1120 }}
                />
              </Card>
            </Col>
          </Row>
        </Spin>
      )}
      </>
      )}
    </div>
  );
};

export default FactorLab;
