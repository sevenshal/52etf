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
  Modal,
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
import { useLocation, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';
import DatabaseManager from './DatabaseManager';
import AStockInnovation100 from './AStockInnovation100';
import AStockFundFlow from './AStockFundFlow';
import ValuationSimulation from './ValuationSimulation';
import './FactorLab.css';

const { Text } = Typography;
const getLastYearStartDate = () => dayjs().subtract(1, 'year').startOf('year');
const FACTOR_LAB_TAB_ITEMS = [
  { key: 'single', label: '单因子' },
  { key: 'composite', label: '组合因子' },
  { key: 'timing', label: '择时因子' },
  { key: 'backtest', label: '因子回测' },
  { key: 'valuation-sim', label: '估值模拟盘' },
  { key: 'innovation100', label: 'A创100' },
  { key: 'fund-flow', label: '资金流向' },
  { key: 'db', label: 'DB' },
];

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
  custom_symbols: [],
  start_date: dayjs('2020-01-02'),
  end_date: null,
  oos_start_date: getLastYearStartDate(),
  initial_capital: 100000,
  max_positions: 7,
  position_weights: [],
  position_weights_text: '',
  sell_rank_multiplier: 2,
  rebalance_frequency: 'weekly',
  rotation_mode: 'rank_exit_rebalance',
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
const ROTATION_MODE_OPTIONS = [
  { label: '跌出排名再补位调仓', value: 'rank_exit_rebalance' },
  { label: '现金补位不减仓', value: 'cash_fill_rebalance' },
  { label: '定期调仓到目标仓位', value: 'scheduled_rebalance' },
];
const ROTATION_MODE_LABELS = ROTATION_MODE_OPTIONS.reduce((acc, item) => {
  acc[item.value] = item.label;
  return acc;
}, {});
const TIMEZONE_OPTIONS = [
  { label: '上海时区', value: 'Asia/Shanghai' },
  { label: '美东时区', value: 'America/New_York' },
];
const DEFAULT_LIVE_TRADING_VALUES = {
  name: '因子线上交易',
  enabled: false,
  signal_time: '18:35',
  signal_timezone: 'Asia/Shanghai',
  execution_time: '09:31',
  execution_timezone: 'Asia/Shanghai',
};
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
const CUSTOM_A_STOCK_POOL = 'CUSTOM_A_STOCK';
const CUSTOM_US_STOCK_POOL = 'CUSTOM_US_STOCK';
const CUSTOM_POOL_UNSUPPORTED_FACTOR_KEYS = new Set(['index_weight']);
const CUSTOM_BACKTEST_POOL_OPTIONS = [
  { label: '自定义A股股票池', value: CUSTOM_A_STOCK_POOL },
  { label: '自定义美股股票池', value: CUSTOM_US_STOCK_POOL },
];
const normalizeBacktestPoolValue = value => {
  const pool = String(value || DEFAULT_BACKTEST_VALUES.pool).trim().toUpperCase();
  if (pool === CUSTOM_A_STOCK_POOL) return CUSTOM_A_STOCK_POOL;
  if (pool === CUSTOM_US_STOCK_POOL) return CUSTOM_US_STOCK_POOL;
  return pool || DEFAULT_BACKTEST_VALUES.pool;
};
const isCustomBacktestPool = value => (
  normalizeBacktestPoolValue(value) === CUSTOM_A_STOCK_POOL
  || normalizeBacktestPoolValue(value) === CUSTOM_US_STOCK_POOL
);
const getCustomBacktestMarket = value => {
  const pool = normalizeBacktestPoolValue(value);
  if (pool === CUSTOM_A_STOCK_POOL) return 'a_stock';
  if (pool === CUSTOM_US_STOCK_POOL) return 'us_stock';
  return null;
};
const isBacktestFactorAllowedForPool = (factorKey, pool) => (
  !isCustomBacktestPool(pool) || !CUSTOM_POOL_UNSUPPORTED_FACTOR_KEYS.has(String(factorKey || ''))
);
const isAStockPoolValue = value => (
  normalizeBacktestPoolValue(value) === A_STOCK_INNO100_POOL
  || normalizeBacktestPoolValue(value) === CUSTOM_A_STOCK_POOL
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

const pnlNumberFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  const number = Number(value);
  return <Text type={number > 0 ? 'success' : (number < 0 ? 'danger' : undefined)}>{numberFormatter(number)}</Text>;
};

const pnlPercentFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  const number = Number(value);
  return <Text type={number > 0 ? 'success' : (number < 0 ? 'danger' : undefined)}>{percentFormatter(number)}</Text>;
};

const icFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toFixed(4);
};

const factorValueFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 6 });
};

const normalizeDisplaySymbol = value => String(value || '').trim().toUpperCase();

const getSymbolNameFromMap = (symbol, symbolNames = {}) => {
  const normalized = normalizeDisplaySymbol(symbol);
  if (!normalized || !symbolNames) return '';
  const base = normalized.endsWith('.US') ? normalized.slice(0, -3) : normalized.split('.')[0];
  const usSymbol = normalized.includes('.') ? '' : `${normalized}.US`;
  return symbolNames[normalized] || symbolNames[base] || (usSymbol ? symbolNames[usSymbol] : '') || '';
};

const formatSymbolDisplay = (symbol, symbolNames = {}, fallbackName = '') => {
  const normalized = normalizeDisplaySymbol(symbol);
  if (!normalized) return '-';
  const name = String(fallbackName || getSymbolNameFromMap(normalized, symbolNames) || '').trim();
  return name ? `${name} ${normalized}` : normalized;
};

const renderSymbolCell = (value, record = {}, { nameKey = 'symbol_name', symbolNames = {} } = {}) => {
  const normalized = normalizeDisplaySymbol(value);
  if (!normalized) return '-';
  const name = String(record?.[nameKey] || getSymbolNameFromMap(normalized, symbolNames) || '').trim();
  if (!name) return normalized;
  return (
    <Space direction="vertical" size={0}>
      <Text strong>{name}</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>{normalized}</Text>
    </Space>
  );
};

const formatSymbolList = (symbols, symbolNames = {}) => {
  const items = (Array.isArray(symbols) ? symbols : [])
    .map(symbol => formatSymbolDisplay(symbol, symbolNames))
    .filter(Boolean);
  return items.length ? items.join(', ') : '-';
};

const getSymbolWeightValue = (weights, symbol) => {
  if (!weights || typeof weights !== 'object') return undefined;
  const normalized = normalizeDisplaySymbol(symbol);
  if (!normalized) return undefined;
  const base = normalized.endsWith('.US') ? normalized.slice(0, -3) : normalized.split('.')[0];
  return weights[normalized] ?? weights[symbol] ?? weights[base];
};

const formatTargetWeightPercent = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
  return `${(Number(value) * 100).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}%`;
};

const formatTargetSymbolList = (symbols, symbolNames = {}, targetWeights = {}) => {
  const items = (Array.isArray(symbols) ? symbols : [])
    .map(symbol => {
      const display = formatSymbolDisplay(symbol, symbolNames);
      const weightText = formatTargetWeightPercent(getSymbolWeightValue(targetWeights, symbol));
      return weightText ? `${display} ${weightText}` : display;
    })
    .filter(Boolean);
  return items.length ? items.join(', ') : '-';
};

const flattenSymbolOptions = options => (
  (options || []).flatMap(item => (Array.isArray(item?.options) ? item.options : [item])).filter(Boolean)
);

const mergeSymbolOptions = (options, selectedSymbols, symbolNames = {}) => {
  const merged = [...(options || [])];
  const seen = new Set(flattenSymbolOptions(merged).map(item => normalizeDisplaySymbol(item.value)));
  (Array.isArray(selectedSymbols) ? selectedSymbols : []).forEach(symbol => {
    const normalized = normalizeDisplaySymbol(symbol);
    if (!normalized || seen.has(normalized)) return;
    const name = getSymbolNameFromMap(normalized, symbolNames);
    merged.push({
      label: formatSymbolDisplay(normalized, symbolNames, name),
      value: normalized,
      name: name || undefined,
    });
    seen.add(normalized);
  });
  return merged;
};

const formatErrorDetail = detail => {
  if (!detail) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map(item => {
        if (typeof item === 'string') return item;
        if (!item || typeof item !== 'object') return String(item);
        const location = Array.isArray(item.loc) ? item.loc.join('.') : item.loc;
        return [location, item.msg].filter(Boolean).join(': ') || JSON.stringify(item);
      })
      .filter(Boolean)
      .join('；');
  }
  if (typeof detail === 'object') {
    return detail.msg || detail.message || JSON.stringify(detail);
  }
  return String(detail);
};

const getErrorMessage = (error, fallback) => (
  formatErrorDetail(error?.response?.data?.detail)
  || formatErrorDetail(error?.response?.data?.message)
  || formatErrorDetail(error?.message)
  || fallback
);

const normalizeAStockSymbol = value => {
  const text = String(value || '').trim().toUpperCase().replace(/\s+/g, '');
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(text)) return text;
  if (!/^\d{6}$/.test(text)) return text;
  if (/^(43|83|87|88|92)/.test(text)) return `${text}.BJ`;
  if (/^[569]/.test(text)) return `${text}.SH`;
  return `${text}.SZ`;
};

const normalizeCustomStockSymbols = (symbols, pool) => {
  const poolKey = normalizeBacktestPoolValue(pool);
  const items = Array.isArray(symbols) ? symbols : [];
  const normalized = [];
  items.forEach(item => {
    const text = String(item || '').trim().toUpperCase().replace(/\s+/g, '');
    if (!text) return;
    const symbol = poolKey === CUSTOM_US_STOCK_POOL
      ? (text.endsWith('.US') ? text : `${text}.US`)
      : normalizeAStockSymbol(text);
    if (symbol && !normalized.includes(symbol)) normalized.push(symbol);
  });
  return isCustomBacktestPool(poolKey) ? normalized : [];
};

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

const normalizeBacktestDefaultRequest = (payload = {}) => {
  const pool = normalizeBacktestPoolValue(payload.pool || DEFAULT_BACKTEST_VALUES.pool);
  const maxPositions = payload.max_positions || DEFAULT_BACKTEST_VALUES.max_positions;
  const positionWeights = normalizePositionWeights(payload.position_weights, maxPositions);
  return {
    ...DEFAULT_BACKTEST_VALUES,
    ...payload,
    pool,
    custom_symbols: normalizeCustomStockSymbols(payload.custom_symbols, pool),
    position_weights: positionWeights,
    position_weights_text: payload.position_weights?.length
      ? formatPositionWeightsText(positionWeights)
      : (payload.position_weights_text || ''),
    rotation_mode: payload.rotation_mode || DEFAULT_BACKTEST_VALUES.rotation_mode,
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
  };
};

const normalizeLiveConfigFormValues = (config = {}) => {
  const timezone = config.signal_timezone || config.execution_timezone || DEFAULT_LIVE_TRADING_VALUES.signal_timezone;
  return normalizeBacktestDefaultRequest({
    ...DEFAULT_BACKTEST_VALUES,
    ...(config.request || {}),
    name: config.name || DEFAULT_LIVE_TRADING_VALUES.name,
    enabled: Object.prototype.hasOwnProperty.call(config, 'enabled')
      ? config.enabled
      : DEFAULT_LIVE_TRADING_VALUES.enabled,
    external_trading_account_id: config.external_trading_account_id ?? null,
    live_sub_account_id: config.live_sub_account_id ?? null,
    signal_time: config.signal_time || DEFAULT_LIVE_TRADING_VALUES.signal_time,
    signal_timezone: timezone,
    execution_time: config.execution_time || DEFAULT_LIVE_TRADING_VALUES.execution_time,
    execution_timezone: timezone,
  });
};

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

const buildFactorSelectOptions = (factors, filterFactor = () => true) => {
  const groups = {};
  (factors || []).forEach(factor => {
    if (!filterFactor(factor)) return;
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

const isBacktestFactorOptionAllowedForPool = (factor, pool) => (
  isBacktestFactorAllowedForPool(factor?.key, pool)
  && !(isCustomBacktestPool(pool) && (factor?.unsupported_pool_types || []).includes('custom'))
);

const sanitizeBacktestLegsForPool = (legs, pool, factors = []) => {
  const source = Array.isArray(legs) && legs.length ? legs : DEFAULT_BACKTEST_VALUES.legs;
  const filtered = source.filter(leg => {
    const factor = getFactorByKey(factors, leg?.factor) || { key: leg?.factor };
    return isBacktestFactorOptionAllowedForPool(factor, pool);
  });
  if (filtered.length) {
    if (filtered.length === 1 && filtered.length !== source.length) {
      return [{ ...filtered[0], weight: 1 }];
    }
    return filtered;
  }
  return [buildDefaultBacktestLeg('risk_adjusted_momentum', getFactorByKey(factors, 'risk_adjusted_momentum'))];
};

const sanitizeCompositeLegsForPool = (legs, pool, factors = []) => {
  const source = Array.isArray(legs) && legs.length ? legs : DEFAULT_COMPOSITE_VALUES.legs;
  const filtered = source.filter(leg => {
    const factor = getFactorByKey(factors, leg?.factor) || { key: leg?.factor };
    return isBacktestFactorOptionAllowedForPool(factor, pool);
  });
  return filtered.length ? filtered : DEFAULT_COMPOSITE_VALUES.legs;
};

const validateBacktestLegsForPool = (legs, pool, factors = []) => {
  const unsupported = (legs || []).filter(leg => {
    const factor = getFactorByKey(factors, leg?.factor) || { key: leg?.factor };
    return !isBacktestFactorOptionAllowedForPool(factor, pool);
  });
  if (!unsupported.length) return;
  const labels = unsupported
    .map(leg => getFactorByKey(factors, leg?.factor)?.label || leg?.factor)
    .filter(Boolean);
  throw new Error(`自定义股票池不支持因子：${[...new Set(labels)].join('、')}`);
};

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

const buildBacktestPayload = values => {
  const pool = normalizeBacktestPoolValue(values.pool);
  const fallbackMaxPositions = Number(values.max_positions || DEFAULT_BACKTEST_VALUES.max_positions);
  const positionWeights = parsePositionWeightsText(values.position_weights_text, fallbackMaxPositions);
  const payload = {
    pool,
    custom_symbols: normalizeCustomStockSymbols(values.custom_symbols, pool),
    position_weights: positionWeights,
    start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : DEFAULT_BACKTEST_VALUES.start_date.format('YYYY-MM-DD'),
    end_date: values.end_date ? values.end_date.format('YYYY-MM-DD') : null,
    oos_start_date: values.oos_start_date ? values.oos_start_date.format('YYYY-MM-DD') : null,
    initial_capital: Number(values.initial_capital || DEFAULT_BACKTEST_VALUES.initial_capital),
    max_positions: positionWeights.length || fallbackMaxPositions,
    sell_rank_multiplier: Number(values.sell_rank_multiplier || DEFAULT_BACKTEST_VALUES.sell_rank_multiplier),
    rebalance_frequency: values.rebalance_frequency || DEFAULT_BACKTEST_VALUES.rebalance_frequency,
    rotation_mode: values.rotation_mode || DEFAULT_BACKTEST_VALUES.rotation_mode,
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
  };
  validateBacktestLegsForPool(payload.legs, pool);
  return payload;
};

const buildLiveConfigPayload = values => {
  const timezone = values.signal_timezone || values.execution_timezone || DEFAULT_LIVE_TRADING_VALUES.signal_timezone;
  const liveRequest = {
    ...buildBacktestPayload(values),
    end_date: null,
    oos_start_date: null,
  };
  return {
    name: String(values.name || DEFAULT_LIVE_TRADING_VALUES.name).trim() || DEFAULT_LIVE_TRADING_VALUES.name,
    enabled: Boolean(values.enabled),
    request: liveRequest,
    external_trading_account_id: values.external_trading_account_id ? Number(values.external_trading_account_id) : null,
    live_sub_account_id: values.live_sub_account_id ? Number(values.live_sub_account_id) : null,
    signal_time: values.signal_time || DEFAULT_LIVE_TRADING_VALUES.signal_time,
    signal_timezone: timezone,
    execution_time: values.execution_time || DEFAULT_LIVE_TRADING_VALUES.execution_time,
    execution_timezone: timezone,
  };
};

const buildBacktestRequestFromMetadata = (metadata = {}) => ({
  pool: normalizeBacktestPoolValue(metadata.pool || DEFAULT_BACKTEST_VALUES.pool),
  custom_symbols: normalizeCustomStockSymbols(metadata.custom_symbols || [], metadata.pool || DEFAULT_BACKTEST_VALUES.pool),
  position_weights: metadata.position_weights || [],
  start_date: metadata.start_date || DEFAULT_BACKTEST_VALUES.start_date.format('YYYY-MM-DD'),
  end_date: metadata.end_date || null,
  oos_start_date: metadata.oos_start_date || null,
  initial_capital: Number(metadata.initial_capital || DEFAULT_BACKTEST_VALUES.initial_capital),
  max_positions: Number(metadata.max_positions || DEFAULT_BACKTEST_VALUES.max_positions),
  sell_rank_multiplier: Number(metadata.sell_rank_multiplier || DEFAULT_BACKTEST_VALUES.sell_rank_multiplier),
  rebalance_frequency: metadata.rebalance_frequency || DEFAULT_BACKTEST_VALUES.rebalance_frequency,
  rotation_mode: metadata.rotation_mode || DEFAULT_BACKTEST_VALUES.rotation_mode,
  commission_pct: Number(metadata.commission_pct ?? DEFAULT_BACKTEST_VALUES.commission_pct),
  slippage_pct: Number(metadata.slippage_pct ?? DEFAULT_BACKTEST_VALUES.slippage_pct),
  lot_size: Number(metadata.lot_size || DEFAULT_BACKTEST_VALUES.lot_size),
  min_listing_days: Number(metadata.min_listing_days ?? DEFAULT_BACKTEST_VALUES.min_listing_days),
  legs: (metadata.components || []).map(component => ({
    factor: component.factor_key || component.factor,
    window: component.window,
    weight: Number(component.raw_weight ?? component.weight ?? 0),
    neutralization: component.neutralization || 'none',
    standardization: component.standardization || 'rank_percentile',
    momentum_weights: normalizeMomentumWeights(component.momentum_weights),
  })).filter(leg => leg.factor),
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

const getBacktestEquityOption = (equityRows = [], benchmarkRows = [], candidateEtfs = [], symbolNames = {}) => {
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
        name: formatSymbolDisplay(symbol, symbolNames),
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
  {
    title: '主基准',
    dataIndex: 'primary_benchmark_symbol',
    width: 160,
    render: (value, row) => renderSymbolCell(value, { symbol_name: row.primary_benchmark_symbol_name }),
  },
  { title: '基准收益', dataIndex: 'primary_benchmark_return_pct', align: 'right', render: percentFormatter },
  { title: '超额收益', dataIndex: 'primary_excess_return_pct', align: 'right', render: percentFormatter },
];

const backtestHoldingColumns = [
  { title: '标的', dataIndex: 'symbol', width: 160, fixed: 'left', render: renderSymbolCell },
  { title: '股数', dataIndex: 'shares', align: 'right', render: numberFormatter },
  { title: '价格', dataIndex: 'price', align: 'right', render: numberFormatter },
  { title: '成本', dataIndex: 'avg_cost', align: 'right', render: numberFormatter },
  { title: '入场日', dataIndex: 'entry_date', width: 112 },
  { title: '市值', dataIndex: 'market_value', align: 'right', render: numberFormatter },
  { title: '权重', dataIndex: 'actual_weight_pct', align: 'right', render: percentFormatter },
];

const backtestSymbolPnlColumns = [
  { title: '标的', dataIndex: 'symbol', width: 160, fixed: 'left', render: renderSymbolCell },
  { title: '状态', dataIndex: 'is_open', width: 88, render: value => <Tag color={value ? 'blue' : 'default'}>{value ? '持仓中' : '已清仓'}</Tag> },
  { title: '买入次数', dataIndex: 'buy_trade_count', align: 'right', render: numberFormatter },
  { title: '卖出次数', dataIndex: 'sell_trade_count', align: 'right', render: numberFormatter },
  { title: '当前股数', dataIndex: 'current_shares', align: 'right', render: numberFormatter },
  { title: '投入成本', dataIndex: 'total_cost_basis', align: 'right', render: numberFormatter },
  { title: '已实现盈亏', dataIndex: 'realized_profit', align: 'right', render: pnlNumberFormatter },
  { title: '未实现盈亏', dataIndex: 'unrealized_profit', align: 'right', render: pnlNumberFormatter },
  { title: '总盈亏', dataIndex: 'total_profit', align: 'right', render: pnlNumberFormatter },
  { title: '盈亏率', dataIndex: 'total_profit_pct', align: 'right', render: pnlPercentFormatter },
  { title: '当前市值', dataIndex: 'current_market_value', align: 'right', render: numberFormatter },
  { title: '持有天数', dataIndex: 'holding_days', align: 'right', render: numberFormatter },
  { title: '首次买入', dataIndex: 'first_buy_date', width: 112 },
  { title: '最后交易', dataIndex: 'last_trade_date', width: 112 },
];

const backtestTradeColumns = [
  { title: '日期', dataIndex: 'date', width: 112, fixed: 'left' },
  { title: '信号日', dataIndex: 'signal_date', width: 112 },
  { title: '方向', dataIndex: 'action', width: 80, render: value => <Tag color={value === 'BUY' ? 'green' : 'orange'}>{value}</Tag> },
  { title: '标的', dataIndex: 'symbol', width: 160, render: renderSymbolCell },
  { title: '分数', dataIndex: 'decision_score', align: 'right', render: factorValueFormatter },
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

const normalizePositionWeights = (weights, maxPositions = 1) => {
  const items = Array.isArray(weights) ? weights : [];
  const parsed = items
    .map(item => Number(item))
    .filter(item => Number.isFinite(item) && item > 0);
  if (!parsed.length) {
    const count = Math.max(1, Math.min(100, Number(maxPositions) || 1));
    return Array.from({ length: count }, () => Number((1 / count).toFixed(10)));
  }
  const total = parsed.reduce((sum, item) => sum + item, 0);
  if (!Number.isFinite(total) || total <= 0) return [];
  return (total > 1.000001 ? parsed.map(item => item / total) : parsed)
    .map(item => Number(item.toFixed(10)));
};

const parsePositionWeightsText = (value, maxPositions = 1, label = '仓位权重') => {
  const text = String(value || '').trim();
  if (!text) return normalizePositionWeights([], maxPositions);
  const items = text
    .split(/[:：/\s]+/)
    .map(item => item.trim())
    .filter(Boolean);
  if (!items.length) return normalizePositionWeights([], maxPositions);
  const weights = items.map(item => {
    const number = Number(item);
    if (!Number.isFinite(number) || number <= 0) {
      throw new Error(`${label}只能包含大于0的数字`);
    }
    return number;
  });
  if (weights.length > 100) {
    throw new Error(`${label}最多支持100个标的`);
  }
  return normalizePositionWeights(weights, maxPositions);
};

const parsePositionWeightCandidates = (value, fallbackWeights, fallbackMaxPositions) => {
  const parts = String(value || '')
    .split(/[,\uff0c]+/)
    .map(item => item.trim())
    .filter(Boolean);
  const source = parts.length ? parts : [formatPositionWeightsText(fallbackWeights?.length ? fallbackWeights : normalizePositionWeights([], fallbackMaxPositions))];
  const normalized = [];
  source.forEach(item => {
    const weights = parsePositionWeightsText(item, fallbackMaxPositions, '仓位候选项');
    const key = weights.map(weight => weight.toFixed(10)).join(':');
    if (!normalized.some(candidate => candidate.map(weight => weight.toFixed(10)).join(':') === key)) {
      normalized.push(weights);
    }
  });
  if (normalized.length > 50) {
    throw new Error('仓位候选项最多支持50组');
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
  { title: '仓位', dataIndex: 'position_weights_label', width: 128 },
  numericColumn({ title: '卖出倍数', dataIndex: 'sell_rank_multiplier', width: 96, render: icFormatter }),
  { title: '调仓方式', dataIndex: 'rotation_mode_label', width: 142 },
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

const formatPositionWeightsText = weights => (
  (weights || [])
    .map(weight => Number(weight || 0).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') || '0')
    .join(':')
);

const formatFactorWeight = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toFixed(4);
};

const getRebalanceFrequencyLabel = value => REBALANCE_FREQUENCY_LABELS[value] || value || '-';
const getRotationModeLabel = value => ROTATION_MODE_LABELS[value] || value || '-';
const FACTOR_LIVE_STRATEGY_TYPE = 'factor_live_trading';

const isCurrentFactorLiveSubAccount = (subAccount, configId) => (
  Boolean(
    configId
    && subAccount?.strategy_type === FACTOR_LIVE_STRATEGY_TYPE
    && Number(subAccount?.strategy_config_id) === Number(configId),
  )
);

const isAvailableLiveSubAccount = (subAccount, configId = null) => (
  Boolean(
    subAccount?.enabled
    && (
      subAccount?.binding_status === 'FREE'
      || (!subAccount?.strategy_type && !subAccount?.strategy_config_id)
      || isCurrentFactorLiveSubAccount(subAccount, configId)
    ),
  )
);

const formatLiveSubAccountOptionLabel = (subAccount, configId = null) => {
  const name = subAccount?.name || subAccount?.id || '-';
  if (!subAccount?.enabled) return `${name}（停用）`;
  if (isCurrentFactorLiveSubAccount(subAccount, configId)) return `${name}（当前配置）`;
  if (isAvailableLiveSubAccount(subAccount, configId)) return name;
  return `${name}（已占用：${subAccount?.binding_label || subAccount?.strategy_name || subAccount?.strategy_type || '其他策略'}）`;
};

const FactorLab = ({ initialTab = 'single', liveOnly = false }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const [form] = Form.useForm();
  const [compositeForm] = Form.useForm();
  const [backtestForm] = Form.useForm();
  const [timingForm] = Form.useForm();
  const [liveForm] = Form.useForm();
  const [activeTab, setActiveTab] = useState(liveOnly ? 'live' : initialTab);
  const [options, setOptions] = useState(null);
  const [result, setResult] = useState(null);
  const [compositeResult, setCompositeResult] = useState(null);
  const [backtestResult, setBacktestResult] = useState(null);
  const [timingResult, setTimingResult] = useState(null);
  const [liveConfigs, setLiveConfigs] = useState([]);
  const [liveLogs, setLiveLogs] = useState([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveSaving, setLiveSaving] = useState(false);
  const [liveActionLoading, setLiveActionLoading] = useState(false);
  const [selectedLiveConfigId, setSelectedLiveConfigId] = useState(null);
  const [liveConfigModalOpen, setLiveConfigModalOpen] = useState(false);
  const [editingLiveConfigId, setEditingLiveConfigId] = useState(null);
  const [externalTradingAccounts, setExternalTradingAccounts] = useState([]);
  const [externalTradingSubAccounts, setExternalTradingSubAccounts] = useState([]);
  const [externalTradingAccountsLoading, setExternalTradingAccountsLoading] = useState(false);
  const [liveCustomSymbolOptions, setLiveCustomSymbolOptions] = useState([]);
  const [liveCustomSymbolSearching, setLiveCustomSymbolSearching] = useState(false);
  const [backtestSearchJob, setBacktestSearchJob] = useState(null);
  const [backtestSearchObjective, setBacktestSearchObjective] = useState('annualized_return');
  const [backtestSearchWindowBucketCount, setBacktestSearchWindowBucketCount] = useState(20);
  const [backtestSearchFactorBucketCount, setBacktestSearchFactorBucketCount] = useState(20);
  const [backtestSearchPositionWeights, setBacktestSearchPositionWeights] = useState('0.7:0.3,0.7:0.2:0.1');
  const [backtestSearchSellMultipliers, setBacktestSearchSellMultipliers] = useState('2');
  const [backtestSearchRotationModes, setBacktestSearchRotationModes] = useState(['rank_exit_rebalance', 'cash_fill_rebalance', 'scheduled_rebalance']);
  const [backtestSearchRows, setBacktestSearchRows] = useState([]);
  const [backtestSearchResultsLoading, setBacktestSearchResultsLoading] = useState(false);
  const [customSymbolOptions, setCustomSymbolOptions] = useState([]);
  const [customSymbolSearching, setCustomSymbolSearching] = useState(false);
  const [backtestSearchTableState, setBacktestSearchTableState] = useState({
    current: 1,
    pageSize: 20,
    sortField: 'objective_value',
    sortOrder: 'descend',
    filters: {},
    total: 0,
  });
  const backtestSearchTableStateRef = useRef(backtestSearchTableState);
  const customSymbolSearchTimerRef = useRef(null);
  const customSymbolSearchSeqRef = useRef(0);
  const liveCustomSymbolSearchTimerRef = useRef(null);
  const liveCustomSymbolSearchSeqRef = useRef(0);
  const selectedLiveConfigIdRef = useRef(null);
  const pendingBacktestStateKeyRef = useRef(null);
  const pendingLiveDraftStateKeyRef = useRef(null);
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
  const selectedSinglePool = Form.useWatch('pool', form);
  const selectedCompositePool = Form.useWatch('pool', compositeForm);
  const selectedBacktestPool = Form.useWatch('pool', backtestForm);
  const selectedLivePool = Form.useWatch('pool', liveForm);
  const selectedBacktestCustomSymbols = Form.useWatch('custom_symbols', backtestForm);
  const selectedLiveCustomSymbols = Form.useWatch('custom_symbols', liveForm);
  const selectedLiveExternalTradingAccountId = Form.useWatch('external_trading_account_id', liveForm);
  const selectedLiveSubAccountId = Form.useWatch('live_sub_account_id', liveForm);
  const compositeLegs = Form.useWatch('legs', compositeForm);
  const backtestLegs = Form.useWatch('legs', backtestForm);
  const customBacktestMarket = getCustomBacktestMarket(selectedBacktestPool);
  const customBacktestSymbolsVisible = isCustomBacktestPool(selectedBacktestPool);
  const customLiveMarket = getCustomBacktestMarket(selectedLivePool);
  const customLiveSymbolsVisible = isCustomBacktestPool(selectedLivePool);
  const selectedFactor = useMemo(() => (
    (options?.factors || []).find(item => item.key === selectedFactorKey)
  ), [options, selectedFactorKey]);
  const liveFactorSelectOptions = useMemo(() => (
    buildFactorSelectOptions(
      options?.factors,
      factor => isBacktestFactorOptionAllowedForPool(factor, selectedLivePool),
    )
  ), [options, selectedLivePool]);
  const showMomentumWeights = Boolean(
    selectedFactor?.supports_mixed_windows
    && normalizeHeatmapWindows(selectedHeatmapWindows, []).some(isMixedWindow)
  );

  useEffect(() => {
    backtestSearchTableStateRef.current = backtestSearchTableState;
  }, [backtestSearchTableState]);

  useEffect(() => {
    selectedLiveConfigIdRef.current = selectedLiveConfigId;
  }, [selectedLiveConfigId]);

  const clearLocationStateKeys = useCallback((keys) => {
    const currentState = location.state || {};
    const nextState = { ...currentState };
    keys.forEach(key => {
      delete nextState[key];
    });
    navigate(`${location.pathname}${location.search}`, {
      replace: true,
      state: Object.keys(nextState).length ? nextState : null,
    });
  }, [location.pathname, location.search, location.state, navigate]);

  const loadOptions = useCallback(async () => {
    setLoadingOptions(true);
    try {
      const { data } = await request.get('/api/factor-lab/options');
      setOptions(data);
      form.setFieldsValue(normalizeDefaultRequest(data.default_request));
      compositeForm.setFieldsValue(normalizeCompositeDefaultRequest(data.default_composite_request));
      backtestForm.setFieldsValue(normalizeBacktestDefaultRequest(data.default_backtest_request));
      timingForm.setFieldsValue(normalizeTimingDefaultRequest(data.default_timing_request));
      liveForm.setFieldsValue(normalizeLiveConfigFormValues({
        request: data.default_backtest_request,
        ...DEFAULT_LIVE_TRADING_VALUES,
      }));
    } catch (error) {
      message.error(getErrorMessage(error, '加载因子实验室配置失败'));
    } finally {
      setLoadingOptions(false);
    }
  }, [form, compositeForm, backtestForm, timingForm, liveForm]);

  const loadExternalTradingAccounts = useCallback(async () => {
    setExternalTradingAccountsLoading(true);
    try {
      const { data } = await request.get('/api/external-trading-accounts');
      setExternalTradingAccounts(Array.isArray(data) ? data : []);
    } catch (error) {
      message.error(getErrorMessage(error, '加载外部交易账户失败'));
    } finally {
      setExternalTradingAccountsLoading(false);
    }
  }, []);

  const loadExternalTradingSubAccounts = useCallback(async (accountId) => {
    if (!accountId) {
      setExternalTradingSubAccounts([]);
      return;
    }
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${accountId}/sub-accounts`);
      setExternalTradingSubAccounts(Array.isArray(data) ? data : []);
    } catch (error) {
      message.error(getErrorMessage(error, '加载外部交易子账户失败'));
      setExternalTradingSubAccounts([]);
    }
  }, []);

  const loadLiveConfigs = useCallback(async (preferredConfigId = undefined) => {
    setLiveLoading(true);
    try {
      const { data } = await request.get('/api/factor-lab/live-configs');
      const rows = Array.isArray(data) ? data : [];
      setLiveConfigs(rows);
      const targetConfigId = preferredConfigId === undefined ? selectedLiveConfigIdRef.current : preferredConfigId;
      const nextConfig = rows.find(item => item.id === targetConfigId) || rows[0] || null;
      if (nextConfig) {
        setSelectedLiveConfigId(nextConfig.id);
      } else {
        setSelectedLiveConfigId(null);
        setLiveLogs([]);
      }
    } catch (error) {
      message.error(getErrorMessage(error, '加载线上交易配置失败'));
    } finally {
      setLiveLoading(false);
    }
  }, []);

  const loadLiveConfigLogs = useCallback(async (configId) => {
    if (!configId) {
      setLiveLogs([]);
      return;
    }
    try {
      const { data } = await request.get(`/api/factor-lab/live-configs/${configId}/logs`, {
        params: { limit: 100 },
      });
      setLiveLogs(Array.isArray(data) ? data : []);
    } catch (error) {
      message.error(getErrorMessage(error, '加载线上交易日志失败'));
      setLiveLogs([]);
    }
  }, []);

  const loadLiveCustomSymbolOptions = useCallback((market, query = '') => {
    if (!market) {
      setLiveCustomSymbolOptions([]);
      setLiveCustomSymbolSearching(false);
      return;
    }
    if (liveCustomSymbolSearchTimerRef.current) {
      window.clearTimeout(liveCustomSymbolSearchTimerRef.current);
    }
    const requestSeq = liveCustomSymbolSearchSeqRef.current + 1;
    liveCustomSymbolSearchSeqRef.current = requestSeq;
    liveCustomSymbolSearchTimerRef.current = window.setTimeout(async () => {
      setLiveCustomSymbolSearching(true);
      try {
        const { data } = await request.get('/api/factor-lab/symbol-search', {
          params: { market, q: query, limit: 30 },
        });
        if (requestSeq === liveCustomSymbolSearchSeqRef.current) {
          setLiveCustomSymbolOptions(data.options || []);
        }
      } catch (error) {
        if (requestSeq === liveCustomSymbolSearchSeqRef.current) {
          message.error(getErrorMessage(error, '搜索股票代码失败'));
        }
      } finally {
        if (requestSeq === liveCustomSymbolSearchSeqRef.current) {
          setLiveCustomSymbolSearching(false);
        }
      }
    }, 250);
  }, []);

  const loadCustomSymbolOptions = useCallback((market, query = '') => {
    if (!market) {
      setCustomSymbolOptions([]);
      setCustomSymbolSearching(false);
      return;
    }
    if (customSymbolSearchTimerRef.current) {
      window.clearTimeout(customSymbolSearchTimerRef.current);
    }
    const requestSeq = customSymbolSearchSeqRef.current + 1;
    customSymbolSearchSeqRef.current = requestSeq;
    customSymbolSearchTimerRef.current = window.setTimeout(async () => {
      setCustomSymbolSearching(true);
      try {
        const { data } = await request.get('/api/factor-lab/symbol-search', {
          params: { market, q: query, limit: 30 },
        });
        if (requestSeq === customSymbolSearchSeqRef.current) {
          setCustomSymbolOptions(data.options || []);
        }
      } catch (error) {
        if (requestSeq === customSymbolSearchSeqRef.current) {
          message.error(getErrorMessage(error, '搜索股票代码失败'));
        }
      } finally {
        if (requestSeq === customSymbolSearchSeqRef.current) {
          setCustomSymbolSearching(false);
        }
      }
    }, 250);
  }, []);

  useEffect(() => () => {
    if (customSymbolSearchTimerRef.current) {
      window.clearTimeout(customSymbolSearchTimerRef.current);
    }
    customSymbolSearchSeqRef.current += 1;
  }, []);

  useEffect(() => () => {
    if (liveCustomSymbolSearchTimerRef.current) {
      window.clearTimeout(liveCustomSymbolSearchTimerRef.current);
    }
    liveCustomSymbolSearchSeqRef.current += 1;
  }, []);

  useEffect(() => {
    if (!customBacktestMarket) {
      setCustomSymbolOptions([]);
      setCustomSymbolSearching(false);
      return;
    }
    loadCustomSymbolOptions(customBacktestMarket, '');
  }, [customBacktestMarket, loadCustomSymbolOptions]);

  useEffect(() => {
    if (!isCustomBacktestPool(selectedBacktestPool)) return;
    const currentLegs = backtestForm.getFieldValue('legs') || [];
    const nextLegs = sanitizeBacktestLegsForPool(currentLegs, selectedBacktestPool, options?.factors);
    const currentKeys = currentLegs.map(leg => leg?.factor).join('|');
    const nextKeys = nextLegs.map(leg => leg?.factor).join('|');
    if (currentLegs.length !== nextLegs.length || currentKeys !== nextKeys) {
      backtestForm.setFieldsValue({ legs: nextLegs });
    }
  }, [selectedBacktestPool, options?.factors, backtestForm]);

  useEffect(() => {
    if (!selectedLiveExternalTradingAccountId) {
      setExternalTradingSubAccounts([]);
      liveForm.setFieldsValue({ live_sub_account_id: null });
      return;
    }
    loadExternalTradingSubAccounts(selectedLiveExternalTradingAccountId);
  }, [selectedLiveExternalTradingAccountId, loadExternalTradingSubAccounts, liveForm]);

  useEffect(() => {
    if (!customLiveMarket) {
      setLiveCustomSymbolOptions([]);
      setLiveCustomSymbolSearching(false);
      return;
    }
    loadLiveCustomSymbolOptions(customLiveMarket, '');
  }, [customLiveMarket, loadLiveCustomSymbolOptions]);

  useEffect(() => {
    if (!isCustomBacktestPool(selectedLivePool)) return;
    const currentLegs = liveForm.getFieldValue('legs') || [];
    const nextLegs = sanitizeBacktestLegsForPool(currentLegs, selectedLivePool, options?.factors);
    const currentKeys = currentLegs.map(leg => leg?.factor).join('|');
    const nextKeys = nextLegs.map(leg => leg?.factor).join('|');
    if (currentLegs.length !== nextLegs.length || currentKeys !== nextKeys) {
      liveForm.setFieldsValue({ legs: nextLegs });
    }
  }, [selectedLivePool, options?.factors, liveForm]);

  const selectedLiveSubAccount = useMemo(() => (
    externalTradingSubAccounts.find(item => Number(item.id) === Number(selectedLiveSubAccountId)) || null
  ), [externalTradingSubAccounts, selectedLiveSubAccountId]);
  const selectedLiveExternalTradingAccount = useMemo(() => (
    externalTradingAccounts.find(item => Number(item.id) === Number(selectedLiveExternalTradingAccountId)) || null
  ), [externalTradingAccounts, selectedLiveExternalTradingAccountId]);
  const selectedLiveSubAccountLotSizeNumber = Number(
    selectedLiveSubAccount?.effective_executor_policy?.lot_size
      ?? selectedLiveSubAccount?.executor_lot_size
      ?? selectedLiveExternalTradingAccount?.executor_lot_size,
  );
  const selectedLiveSubAccountLotSize = Number.isFinite(selectedLiveSubAccountLotSizeNumber)
    && selectedLiveSubAccountLotSizeNumber > 0
    ? selectedLiveSubAccountLotSizeNumber
    : null;
  const selectedLiveSubAccountNetAsset = Number(selectedLiveSubAccount?.net_asset);
  const selectedLiveSubAccountNetAssetValue = Number.isFinite(selectedLiveSubAccountNetAsset)
    ? selectedLiveSubAccountNetAsset
    : null;

  useEffect(() => {
    liveForm.setFieldsValue({
      initial_capital: selectedLiveSubAccountNetAssetValue && selectedLiveSubAccountNetAssetValue > 0
        ? selectedLiveSubAccountNetAssetValue
        : DEFAULT_BACKTEST_VALUES.initial_capital,
      lot_size: selectedLiveSubAccountLotSize
        ? selectedLiveSubAccountLotSize
        : (isAStockPoolValue(selectedLivePool) ? 100 : 1),
    });
  }, [selectedLiveSubAccountLotSize, selectedLiveSubAccountNetAssetValue, selectedLivePool, liveForm]);

  useEffect(() => {
    if (!selectedSinglePool) return;
    const currentFactorKey = form.getFieldValue('factor');
    const currentFactor = getFactorByKey(options?.factors, currentFactorKey);
    if (currentFactor && isBacktestFactorOptionAllowedForPool(currentFactor, selectedSinglePool)) {
      return;
    }
    const nextFactor = (options?.factors || []).find(factor => (
      isBacktestFactorOptionAllowedForPool(factor, selectedSinglePool)
    ));
    if (!nextFactor) return;
    form.setFieldsValue({
      factor: nextFactor.key,
      heatmap_windows: nextFactor.default_windows || DEFAULT_FORM_VALUES.heatmap_windows,
      momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
    });
  }, [selectedSinglePool, options?.factors, form]);

  useEffect(() => {
    if (!selectedCompositePool) return;
    const currentLegs = compositeForm.getFieldValue('legs') || [];
    const nextLegs = sanitizeCompositeLegsForPool(currentLegs, selectedCompositePool, options?.factors);
    const currentKeys = currentLegs.map(leg => leg?.factor).join('|');
    const nextKeys = nextLegs.map(leg => leg?.factor).join('|');
    if (currentLegs.length !== nextLegs.length || currentKeys !== nextKeys) {
      compositeForm.setFieldsValue({ legs: nextLegs });
    }
  }, [selectedCompositePool, options?.factors, compositeForm]);

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
    loadExternalTradingAccounts();
    loadLiveConfigs();
  }, [loadOptions, loadBacktestSearchHistory, loadExternalTradingAccounts, loadLiveConfigs]);

  useEffect(() => {
    if (!selectedLiveConfigId) {
      setLiveLogs([]);
      return;
    }
    loadLiveConfigLogs(selectedLiveConfigId);
  }, [selectedLiveConfigId, loadLiveConfigLogs]);

  useEffect(() => {
    return subscribeBackendEvent('factor_backtest_search', (data) => {
      setBacktestSearchJob(data);
    });
  }, []);

  useEffect(() => {
    if (!backtestSearchJob || backtestSearchJob.status === 'idle') return;
    loadBacktestSearchResults();
  }, [backtestSearchJob?.completed_cases, backtestSearchJob?.status, loadBacktestSearchResults]);

  const handleFactorChange = value => {
    const factor = (options?.factors || []).find(item => item.key === value);
    if (!factor) return;
    const nextWindows = factor.default_windows || DEFAULT_FORM_VALUES.heatmap_windows;
    form.setFieldsValue({
      heatmap_windows: nextWindows,
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
    backtestForm.setFieldsValue({
      lot_size: isAStockPoolValue(value) ? 100 : 1,
      custom_symbols: [],
    });
    setCustomSymbolOptions([]);
  };

  const handleLiveConfigSelect = async configId => {
    setSelectedLiveConfigId(configId || null);
  };

  const openLiveConfigModal = (config = null) => {
    const formValues = normalizeLiveConfigFormValues(config || {
      request: options?.default_backtest_request,
      ...DEFAULT_LIVE_TRADING_VALUES,
    });
    setEditingLiveConfigId(config?.id || null);
    liveForm.setFieldsValue(formValues);
    setLiveCustomSymbolOptions(mergeSymbolOptions(
      [],
      formValues.custom_symbols,
      {
        ...(config?.request_summary?.custom_symbol_names || {}),
        ...(config?.last_signal_payload?.symbol_names || {}),
      },
    ));
    setLiveConfigModalOpen(true);
  };

  const handleLiveCreate = () => {
    openLiveConfigModal();
  };

  const handleLiveEdit = configId => {
    const nextConfig = liveConfigs.find(item => item.id === configId) || null;
    if (!nextConfig) return;
    setSelectedLiveConfigId(nextConfig.id);
    openLiveConfigModal(nextConfig);
  };

  const handleLiveConfigModalCancel = () => {
    setLiveConfigModalOpen(false);
    setEditingLiveConfigId(null);
  };

  const handleLivePoolChange = value => {
    liveForm.setFieldsValue({
      lot_size: isAStockPoolValue(value) ? 100 : 1,
      custom_symbols: [],
    });
    setLiveCustomSymbolOptions([]);
  };

  const handleLiveLegFactorChange = (index, value) => {
    const factor = getFactorByKey(options?.factors, value);
    const legs = [...(liveForm.getFieldValue('legs') || [])];
    legs[index] = {
      ...(legs[index] || {}),
      factor: value,
      window: getDefaultWindowForFactor(factor),
      momentum_weights: DEFAULT_MOMENTUM_WEIGHTS,
    };
    liveForm.setFieldsValue({ legs });
  };

  const handleLiveBacktest = async () => {
    const values = await liveForm.validateFields();
    try {
      const payload = buildBacktestPayload({
        ...values,
        initial_capital: selectedLiveSubAccountNetAssetValue && selectedLiveSubAccountNetAssetValue > 0
          ? selectedLiveSubAccountNetAssetValue
          : values.initial_capital,
        lot_size: selectedLiveSubAccountLotSize || values.lot_size,
      });
      if (liveOnly) {
        setLiveConfigModalOpen(false);
        navigate('/factor-lab', {
          state: {
            mainTabKey: '/factor-lab',
            factorLabBacktest: {
              key: `live-backtest-${Date.now()}`,
              request: payload,
              symbolNames: {
                ...(selectedLiveConfig?.request_summary?.custom_symbol_names || {}),
                ...(selectedLiveConfig?.last_signal_payload?.symbol_names || {}),
              },
              successMessage: '已带入多因子策略参数并完成回测',
            },
          },
        });
        return;
      }
      backtestForm.setFieldsValue(normalizeBacktestDefaultRequest(payload));
      setLiveConfigModalOpen(false);
      setActiveTab('backtest');
      await executeBacktestPayload(payload);
    } catch (error) {
      message.warning(error.message || '线上交易参数无法回测');
    }
  };

  const handleAddBacktestToLive = () => {
    if (!backtestResult) return;
    const payload = buildBacktestRequestFromMetadata(backtestMetadata);
    navigate('/live?tab=factor', {
      state: {
        mainTabKey: '/live',
        factorLabLiveConfigDraft: {
          key: `backtest-live-${Date.now()}`,
          name: `${backtestMetadata.pool_label || '因子回测'}线上交易`,
          request: payload,
          symbolNames: backtestMetadata.symbol_names,
        },
      },
    });
  };

  const handleLiveSave = async () => {
    const values = await liveForm.validateFields();
    setLiveSaving(true);
    try {
      const payload = buildLiveConfigPayload(values);
      const { data } = editingLiveConfigId
        ? await request.put(`/api/factor-lab/live-configs/${editingLiveConfigId}`, payload, { timeout: 300000 })
        : await request.post('/api/factor-lab/live-configs', payload, { timeout: 300000 });
      message.success('线上交易配置已保存');
      setLiveConfigModalOpen(false);
      setEditingLiveConfigId(null);
      setSelectedLiveConfigId(data.id);
      await loadLiveConfigs(data.id);
      await loadLiveConfigLogs(data.id);
    } catch (error) {
      message.error(getErrorMessage(error, '保存线上交易配置失败'));
    } finally {
      setLiveSaving(false);
    }
  };

  const resolveLiveActionConfigId = configId => {
    const numericId = Number(configId);
    return Number.isFinite(numericId) && numericId > 0 ? numericId : selectedLiveConfigId;
  };

  const handleLiveDelete = async (configId = selectedLiveConfigId) => {
    const targetConfigId = resolveLiveActionConfigId(configId);
    if (!targetConfigId) return;
    setSelectedLiveConfigId(targetConfigId);
    setLiveActionLoading(true);
    try {
      await request.delete(`/api/factor-lab/live-configs/${targetConfigId}`, { timeout: 300000 });
      message.success('线上交易配置已删除');
      if (editingLiveConfigId === targetConfigId) {
        setLiveConfigModalOpen(false);
        setEditingLiveConfigId(null);
      }
      await loadLiveConfigs(null);
    } catch (error) {
      message.error(getErrorMessage(error, '删除线上交易配置失败'));
    } finally {
      setLiveActionLoading(false);
    }
  };

  const handleLiveGenerateSignal = async (configId = selectedLiveConfigId) => {
    const targetConfigId = resolveLiveActionConfigId(configId);
    if (!targetConfigId) return;
    setSelectedLiveConfigId(targetConfigId);
    setLiveActionLoading(true);
    try {
      const { data } = await request.post(`/api/factor-lab/live-configs/${targetConfigId}/signal`, {}, { timeout: 300000 });
      message.success('已生成信号');
      setLiveConfigs(previous => previous.map(item => (item.id === data.config.id ? data.config : item)));
      setSelectedLiveConfigId(data.config.id);
      await loadLiveConfigLogs(targetConfigId);
      await loadLiveConfigs(data.config.id);
    } catch (error) {
      message.error(getErrorMessage(error, '生成线上交易信号失败'));
    } finally {
      setLiveActionLoading(false);
    }
  };

  const handleLiveExecute = async (configId = selectedLiveConfigId) => {
    const targetConfigId = resolveLiveActionConfigId(configId);
    if (!targetConfigId) return;
    setSelectedLiveConfigId(targetConfigId);
    setLiveActionLoading(true);
    try {
      const { data } = await request.post(`/api/factor-lab/live-configs/${targetConfigId}/execute`, {}, { timeout: 300000 });
      message.success('已执行线上交易');
      setLiveConfigs(previous => previous.map(item => (item.id === data.config.id ? data.config : item)));
      setSelectedLiveConfigId(data.config.id);
      await loadLiveConfigLogs(targetConfigId);
      await loadLiveConfigs(data.config.id);
    } catch (error) {
      message.error(getErrorMessage(error, '执行线上交易失败'));
    } finally {
      setLiveActionLoading(false);
    }
  };

  const handleLiveRefresh = async () => {
    await loadLiveConfigs(selectedLiveConfigId);
    if (selectedLiveConfigId) {
      await loadLiveConfigLogs(selectedLiveConfigId);
    }
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

  const executeBacktestPayload = useCallback(async (payload, successMessage = '因子回测完成') => {
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
  }, []);

  useEffect(() => {
    if (liveOnly || !options) return;
    const pendingBacktest = location.state?.factorLabBacktest;
    if (!pendingBacktest?.request) return;
    const stateKey = pendingBacktest.key || JSON.stringify(pendingBacktest.request);
    if (pendingBacktestStateKeyRef.current === stateKey) return;
    pendingBacktestStateKeyRef.current = stateKey;
    const pendingRequest = normalizeBacktestDefaultRequest(pendingBacktest.request);
    setActiveTab('backtest');
    setBacktestResult(null);
    backtestForm.setFieldsValue(pendingRequest);
    setCustomSymbolOptions(previous => mergeSymbolOptions(
      previous,
      pendingRequest.custom_symbols,
      pendingBacktest.symbolNames || pendingBacktest.request?.custom_symbol_names || {},
    ));
    clearLocationStateKeys(['factorLabBacktest']);
    executeBacktestPayload(
      {
        ...pendingBacktest.request,
        pool: pendingRequest.pool,
        custom_symbols: pendingRequest.custom_symbols,
      },
      pendingBacktest.successMessage || '已带入多因子策略参数并完成回测',
    );
  }, [liveOnly, options, location.state, backtestForm, clearLocationStateKeys, executeBacktestPayload]);

  useEffect(() => {
    if (!liveOnly || !options) return;
    const pendingDraft = location.state?.factorLabLiveConfigDraft;
    if (!pendingDraft?.request) return;
    const stateKey = pendingDraft.key || JSON.stringify(pendingDraft.request);
    if (pendingLiveDraftStateKeyRef.current === stateKey) return;
    pendingLiveDraftStateKeyRef.current = stateKey;
    const formValues = normalizeLiveConfigFormValues({
      name: pendingDraft.name || `${pendingDraft.request.pool || '因子回测'}线上交易`,
      enabled: false,
      request: pendingDraft.request,
    });
    setEditingLiveConfigId(null);
    liveForm.setFieldsValue(formValues);
    setLiveCustomSymbolOptions(mergeSymbolOptions([], formValues.custom_symbols, pendingDraft.symbolNames));
    setLiveConfigModalOpen(true);
    clearLocationStateKeys(['factorLabLiveConfigDraft']);
  }, [liveOnly, options, location.state, liveForm, clearLocationStateKeys]);

  const runBacktest = async () => {
    const values = await backtestForm.validateFields();
    try {
      await executeBacktestPayload(buildBacktestPayload(values));
    } catch (error) {
      message.warning(error.message || '仓位权重格式不正确');
    }
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
    let baseRequest;
    try {
      baseRequest = buildBacktestPayload(values);
    } catch (error) {
      message.warning(error.message || '仓位权重格式不正确');
      return;
    }
    if (
      (String(backtestSearchObjective).startsWith('in_sample_') || String(backtestSearchObjective).startsWith('oos_'))
      && !baseRequest.oos_start_date
    ) {
      message.warning('选择样本内/样本外目标时，请先设置样本外起始日期');
      return;
    }
    let positionWeightCandidates;
    let sellMultiplierCandidates;
    try {
      positionWeightCandidates = parsePositionWeightCandidates(
        backtestSearchPositionWeights,
        baseRequest.position_weights,
        baseRequest.max_positions,
      );
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
        position_weight_candidates: positionWeightCandidates,
        sell_rank_multiplier_candidates: sellMultiplierCandidates,
        rotation_mode_candidates: backtestSearchRotationModes?.length ? backtestSearchRotationModes : [baseRequest.rotation_mode],
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

  const singleFactorSelectOptions = useMemo(() => (
    buildFactorSelectOptions(
      options?.factors,
      factor => isBacktestFactorOptionAllowedForPool(factor, selectedSinglePool),
    )
  ), [options, selectedSinglePool]);
  const compositeFactorSelectOptions = useMemo(() => (
    buildFactorSelectOptions(
      options?.factors,
      factor => isBacktestFactorOptionAllowedForPool(factor, selectedCompositePool),
    )
  ), [options, selectedCompositePool]);
  const backtestFactorSelectOptions = useMemo(() => (
    buildFactorSelectOptions(
      options?.factors,
      factor => isBacktestFactorOptionAllowedForPool(factor, selectedBacktestPool),
    )
  ), [options, selectedBacktestPool]);
  const windowOptions = useMemo(() => {
    const windowValues = [
      ...(options?.windows || [20, 60, 120]),
      ...(selectedFactor?.default_windows || []),
    ];
    const baseOptions = [...new Set(windowValues)].sort((a, b) => Number(a) - Number(b)).map(item => ({
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
  const backtestPoolOptions = useMemo(() => {
    const presetOptions = (options?.pools || []).map(item => ({
      label: item.label,
      value: normalizeBacktestPoolValue(item.key),
    }));
    const existing = new Set(presetOptions.map(item => item.value));
    return [
      ...presetOptions,
      ...CUSTOM_BACKTEST_POOL_OPTIONS.filter(item => !existing.has(item.value)),
    ];
  }, [options]);
  const livePoolOptions = backtestPoolOptions;
  const rebalanceFrequencyOptions = useMemo(() => REBALANCE_FREQUENCY_OPTIONS, []);
  const selectedLiveConfig = useMemo(() => (
    liveConfigs.find(item => item.id === selectedLiveConfigId) || null
  ), [liveConfigs, selectedLiveConfigId]);
  const selectedLiveConfigTitle = selectedLiveConfig?.name || '未选择配置';
  const externalTradingAccountOptions = useMemo(() => (
    externalTradingAccounts.map(item => ({
      label: `${item.name || item.identifier || item.id}${item.enabled === false ? '（停用）' : ''}`,
      value: item.id,
    }))
  ), [externalTradingAccounts]);
  const externalTradingSubAccountOptions = useMemo(() => (
    externalTradingSubAccounts.map(item => ({
      label: formatLiveSubAccountOptionLabel(item, editingLiveConfigId),
      value: item.id,
      disabled: !isAvailableLiveSubAccount(item, editingLiveConfigId),
    }))
  ), [externalTradingSubAccounts, editingLiveConfigId]);
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
      { label: '三倍做多半导体ETF SOXL.US', value: 'SOXL.US' },
      { label: '三倍做多纳指100ETF TQQQ.US', value: 'TQQQ.US' },
      { label: '纳斯达克100ETF QQQ.US', value: 'QQQ.US' },
      { label: '标普500ETF SPY.US', value: 'SPY.US' },
    ]
  ), [options]);
  const liveConfigColumns = useMemo(() => ([
    {
      title: '名称',
      dataIndex: 'name',
      width: 180,
      fixed: 'left',
      render: value => <Text strong>{value}</Text>,
    },
    {
      title: '股票池',
      key: 'pool',
      width: 180,
      render: (_, row) => row.request_summary?.pool_label || row.request_summary?.pool || '-',
    },
    {
      title: '调仓频率',
      key: 'rebalance_frequency',
      width: 104,
      render: (_, row) => getRebalanceFrequencyLabel(row.request_summary?.rebalance_frequency),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      width: 96,
      render: value => <Tag color={value ? 'green' : 'default'}>{value ? '启用' : '停用'}</Tag>,
    },
    {
      title: '外部账户',
      dataIndex: 'external_trading_account_id',
      width: 180,
      render: (value, row) => row.external_trading_account_name || value || '-',
    },
    {
      title: '子账户',
      dataIndex: 'live_sub_account_id',
      width: 150,
      render: (value, row) => row.live_sub_account_name || value || '-',
    },
    { title: '信号', dataIndex: 'last_signal_status', width: 104, render: value => value ? <Tag>{value}</Tag> : '-' },
    { title: '信号日', dataIndex: 'last_signal_date', width: 112, render: value => value || '-' },
    { title: '执行', dataIndex: 'last_execution_status', width: 104, render: value => value ? <Tag color={value === 'OK' ? 'green' : 'red'}>{value}</Tag> : '-' },
    { title: '执行日', dataIndex: 'last_execution_signal_date', width: 112, render: value => value || '-' },
    { title: '更新时间', dataIndex: 'updated_at', width: 180, render: value => value || '-' },
    {
      title: '操作',
      key: 'action',
      width: 300,
      fixed: 'right',
      render: (_, row) => (
        <Space size={6} wrap>
          <Button size="small" onClick={event => { event.stopPropagation(); handleLiveEdit(row.id); }}>编辑</Button>
          <Button size="small" onClick={event => { event.stopPropagation(); handleLiveGenerateSignal(row.id); }} loading={liveActionLoading && selectedLiveConfigId === row.id}>信号</Button>
          <Button size="small" onClick={event => { event.stopPropagation(); handleLiveExecute(row.id); }} loading={liveActionLoading && selectedLiveConfigId === row.id}>执行</Button>
          <Button size="small" danger onClick={event => { event.stopPropagation(); handleLiveDelete(row.id); }} loading={liveActionLoading && selectedLiveConfigId === row.id}>删除</Button>
        </Space>
      ),
    },
  ]), [handleLiveDelete, handleLiveEdit, handleLiveExecute, handleLiveGenerateSignal, liveActionLoading, selectedLiveConfigId]);
  const liveLogColumns = useMemo(() => ([
    { title: '时间', dataIndex: 'timestamp', width: 180 },
    { title: '动作', dataIndex: 'action', width: 96, render: value => <Tag>{value}</Tag> },
    { title: '状态', dataIndex: 'status', width: 96, render: value => <Tag color={value === 'OK' ? 'green' : value === 'SKIPPED' ? 'gold' : 'red'}>{value}</Tag> },
    { title: '信号日', dataIndex: 'signal_date', width: 110, render: value => value || '-' },
    { title: '消息', dataIndex: 'message', render: value => value || '-' },
  ]), []);
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
  const backtestSymbolPnlRows = backtestResult?.symbol_pnl || [];
  const backtestComponents = backtestMetadata.components || [];
  const backtestTradeColumnsWithFilters = useMemo(() => {
    const itemsBySymbol = new Map();
    backtestTradeRows.forEach(row => {
      const symbol = normalizeDisplaySymbol(row?.symbol);
      if (!symbol || itemsBySymbol.has(symbol)) return;
      const name = String(row?.symbol_name || getSymbolNameFromMap(symbol, backtestMetadata.symbol_names) || '').trim();
      itemsBySymbol.set(symbol, {
        text: formatSymbolDisplay(symbol, backtestMetadata.symbol_names, name),
        value: symbol,
      });
    });
    const filters = Array.from(itemsBySymbol.values())
      .sort((left, right) => left.text.localeCompare(right.text, 'zh-CN'));
    return backtestTradeColumns.map(column => {
      if (column.dataIndex !== 'symbol') return column;
      return {
        ...column,
        filters,
        filterSearch: true,
        onFilter: (value, row) => normalizeDisplaySymbol(row?.symbol) === normalizeDisplaySymbol(value),
      };
    });
  }, [backtestTradeRows, backtestMetadata.symbol_names]);
  const backtestCustomSymbolSelectOptions = useMemo(() => (
    mergeSymbolOptions(
      customSymbolOptions,
      selectedBacktestCustomSymbols,
      backtestMetadata.symbol_names,
    )
  ), [customSymbolOptions, selectedBacktestCustomSymbols, backtestMetadata.symbol_names]);
  const liveCustomSymbolSelectOptions = useMemo(() => (
    mergeSymbolOptions(
      liveCustomSymbolOptions,
      selectedLiveCustomSymbols,
      {
        ...(selectedLiveConfig?.request_summary?.custom_symbol_names || {}),
        ...(selectedLiveConfig?.last_signal_payload?.symbol_names || {}),
      },
    )
  ), [liveCustomSymbolOptions, selectedLiveCustomSymbols, selectedLiveConfig]);
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
  const isLiveTab = activeTab === 'live';
  const isValuationSimTab = activeTab === 'valuation-sim';
  const isInnovationTab = activeTab === 'innovation100';
  const isFundFlowTab = activeTab === 'fund-flow';
  const handleRun = activeTab === 'composite'
    ? runCompositeAnalysis
    : (activeTab === 'backtest' ? runBacktest : (activeTab === 'timing' ? runTimingAnalysis : runAnalysis));
  const activeRunning = activeTab === 'composite'
    ? compositeRunning
    : (activeTab === 'backtest' ? backtestRunning : (activeTab === 'timing' ? timingRunning : running));
  const activeTabLabel = liveOnly ? '多因子策略' : (FACTOR_LAB_TAB_ITEMS.find(item => item.key === activeTab)?.label || '研究');
  const renderLiveConfigCard = config => {
    const selected = config.id === selectedLiveConfigId;
    const requestSummary = config.request_summary || {};
    return (
      <div
        key={config.id}
        className={`factor-lab-live-card${selected ? ' is-selected' : ''}`}
        role="button"
        tabIndex={0}
        onClick={() => handleLiveConfigSelect(config.id)}
        onKeyDown={event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            handleLiveConfigSelect(config.id);
          }
        }}
      >
        <div className="factor-lab-live-card__header">
          <Text strong>{config.name}</Text>
          <Tag color={config.enabled ? 'green' : 'default'}>{config.enabled ? '启用' : '停用'}</Tag>
        </div>
        <div className="factor-lab-live-card__meta">
          <span>{requestSummary.pool_label || requestSummary.pool || '-'}</span>
          <span>{getRebalanceFrequencyLabel(requestSummary.rebalance_frequency)}</span>
        </div>
        <div className="factor-lab-live-card__grid">
          <div>
            <span>外部账户</span>
            <strong>{config.external_trading_account_name || config.external_trading_account_id || '-'}</strong>
          </div>
          <div>
            <span>子账户</span>
            <strong>{config.live_sub_account_name || config.live_sub_account_id || '-'}</strong>
          </div>
          <div>
            <span>信号</span>
            <strong>{config.last_signal_status || '-'}</strong>
          </div>
          <div>
            <span>执行</span>
            <strong>{config.last_execution_status || '-'}</strong>
          </div>
        </div>
        <div className="factor-lab-live-card__dates">
          <span>信号日 {config.last_signal_date || '-'}</span>
          <span>执行日 {config.last_execution_signal_date || '-'}</span>
        </div>
        <div className="factor-lab-live-card__actions">
          <Button size="small" onClick={event => { event.stopPropagation(); handleLiveEdit(config.id); }}>编辑</Button>
          <Button size="small" onClick={event => { event.stopPropagation(); handleLiveGenerateSignal(config.id); }} loading={liveActionLoading && selectedLiveConfigId === config.id}>信号</Button>
          <Button size="small" onClick={event => { event.stopPropagation(); handleLiveExecute(config.id); }} loading={liveActionLoading && selectedLiveConfigId === config.id}>执行</Button>
          <Button size="small" danger onClick={event => { event.stopPropagation(); handleLiveDelete(config.id); }} loading={liveActionLoading && selectedLiveConfigId === config.id}>删除</Button>
        </div>
      </div>
    );
  };
  const renderLiveLogCards = () => {
    if (!liveLogs.length) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />;
    }
    return liveLogs.map((row, index) => (
      <div className="factor-lab-live-log-card" key={row.id || `${row.timestamp}-${index}`}>
        <div className="factor-lab-live-log-card__header">
          <Text strong>{row.timestamp || '-'}</Text>
          <Tag color={row.status === 'OK' ? 'green' : row.status === 'SKIPPED' ? 'gold' : 'red'}>
            {row.status || '-'}
          </Tag>
        </div>
        <div className="factor-lab-live-log-card__meta">
          <Tag>{row.action || '-'}</Tag>
          <span>信号日 {row.signal_date || '-'}</span>
        </div>
        {row.message ? <p>{row.message}</p> : null}
      </div>
    ));
  };

  return (
    <div className={`factor-lab-page${liveOnly ? ' factor-lab-page--live-only' : ''}`}>
      {!liveOnly && (
        <>
          <div className="factor-lab-header">
            <div className="factor-lab-title-block">
              <Text type="secondary">Factor Lab</Text>
              <h1>研究</h1>
              <Tag color="blue">{activeTabLabel}</Tag>
            </div>
            {!isDatabaseTab && !isLiveTab && !isValuationSimTab && !isInnovationTab && !isFundFlowTab && (
              <Space className="factor-lab-actions">
                <Button icon={<ReloadOutlined />} onClick={loadOptions} loading={loadingOptions} />
                <Button type="primary" icon={<PlayCircleOutlined />} onClick={handleRun} loading={activeRunning}>
                  运行
                </Button>
              </Space>
            )}
            {isInnovationTab && <div id="factor-lab-innovation100-actions" className="factor-lab-innovation-actions" />}
          </div>

          <div className="factor-lab-tab-strip">
            <Tabs
              className="factor-lab-tabs"
              activeKey={activeTab}
              onChange={setActiveTab}
              items={FACTOR_LAB_TAB_ITEMS}
            />
            <div className="factor-lab-mobile-tabs">
              {FACTOR_LAB_TAB_ITEMS.map(item => (
                <button
                  key={item.key}
                  type="button"
                  className={`factor-lab-mobile-tab${activeTab === item.key ? ' is-active' : ''}`}
                  aria-pressed={activeTab === item.key}
                  onClick={() => setActiveTab(item.key)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
        </>
      )}

      {activeTab === 'db' && <DatabaseManager />}

      {activeTab === 'innovation100' && <AStockInnovation100 embedded />}

      {activeTab === 'fund-flow' && <AStockFundFlow embedded />}

      {activeTab === 'valuation-sim' && <ValuationSimulation embedded />}

      {activeTab === 'live' && (
        <>
          <Modal
            className="factor-lab-live-modal"
            title={editingLiveConfigId ? '编辑线上交易配置' : '添加线上交易配置'}
            open={liveConfigModalOpen}
            onCancel={handleLiveConfigModalCancel}
            width={1120}
            maskClosable={false}
            destroyOnClose={false}
            styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
            footer={(
              <Space>
                <Button onClick={handleLiveConfigModalCancel}>取消</Button>
                <Button loading={backtestRunning} onClick={handleLiveBacktest}>
                  回测
                </Button>
                <Button loading={liveSaving} type="primary" onClick={handleLiveSave}>
                  保存
                </Button>
              </Space>
            )}
          >
                <Spin spinning={loadingOptions || liveLoading}>
                  <Form form={liveForm} layout="vertical" initialValues={normalizeLiveConfigFormValues()}>
                    <Form.Item name="start_date" hidden>
                      <DatePicker />
                    </Form.Item>
                    <Form.Item name="end_date" hidden>
                      <DatePicker />
                    </Form.Item>
                    <Form.Item name="oos_start_date" hidden>
                      <DatePicker />
                    </Form.Item>
                    <Form.Item name="initial_capital" hidden>
                      <InputNumber />
                    </Form.Item>
                    <Form.Item name="commission_pct" hidden>
                      <InputNumber />
                    </Form.Item>
                    <Form.Item name="slippage_pct" hidden>
                      <InputNumber />
                    </Form.Item>
                    <Form.Item name="lot_size" hidden>
                      <InputNumber />
                    </Form.Item>
                    <Row gutter={[12, 8]}>
                      <Col xs={24} sm={12} md={8} lg={6}>
                        <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                          <Input />
                        </Form.Item>
                      </Col>
                      <Col xs={12} sm={6} md={4} lg={3}>
                        <Form.Item name="enabled" label="启用" rules={[{ required: true }]}>
                          <Select options={[{ label: '启用', value: true }, { label: '停用', value: false }]} />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={12} md={8} lg={6}>
                        <Form.Item name="external_trading_account_id" label="外部交易账户">
                          <Select
                            allowClear
                            options={externalTradingAccountOptions}
                            loading={externalTradingAccountsLoading}
                          />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={12} md={8} lg={6}>
                        <Form.Item name="live_sub_account_id" label="外部交易子账户">
                          <Select
                            allowClear
                            options={externalTradingSubAccountOptions}
                            placeholder="请选择子账户"
                          />
                        </Form.Item>
                      </Col>
                      <Col xs={12} sm={6} md={4} lg={3}>
                        <Form.Item label="交易单位">
                          <Input
                            disabled
                            readOnly
                            value={selectedLiveSubAccountLotSize ? numberFormatter(selectedLiveSubAccountLotSize) : '-'}
                          />
                        </Form.Item>
                      </Col>
                      <Col xs={12} sm={6} md={4} lg={3}>
                        <Form.Item label="子账户净资产">
                          <Input
                            disabled
                            readOnly
                            value={selectedLiveSubAccount ? numberFormatter(selectedLiveSubAccountNetAssetValue) : '-'}
                          />
                        </Form.Item>
                      </Col>
                      <Col xs={12} sm={6} md={4} lg={3}>
                        <Form.Item name="signal_time" label="信号时间" rules={[{ required: true }]}>
                          <Input placeholder="18:35" />
                        </Form.Item>
                      </Col>
                      <Col xs={12} sm={6} md={4} lg={3}>
                        <Form.Item name="execution_time" label="执行时间" rules={[{ required: true }]}>
                          <Input placeholder="09:31" />
                        </Form.Item>
                      </Col>
                      <Col xs={12} sm={6} md={4} lg={3}>
                        <Form.Item name="signal_timezone" label="时区" rules={[{ required: true }]}>
                          <Select options={TIMEZONE_OPTIONS} showSearch />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={12} md={6} lg={4}>
                        <Form.Item name="pool" label="股票池" rules={[{ required: true }]}>
                          <Select options={livePoolOptions} onChange={handleLivePoolChange} />
                        </Form.Item>
                      </Col>
                      {customLiveSymbolsVisible && (
                        <Col xs={24} md={12} lg={8}>
                          <Form.Item name="custom_symbols" label="自定义标的" rules={[{ required: true, message: '请至少选择一个标的' }]}>
                            <Select
                              mode="tags"
                              showSearch
                              allowClear
                              maxTagCount="responsive"
                              tokenSeparators={[',', '，', ' ']}
                              filterOption={false}
                              options={liveCustomSymbolSelectOptions}
                              loading={liveCustomSymbolSearching}
                              optionLabelProp="label"
                              notFoundContent={liveCustomSymbolSearching ? <Spin size="small" /> : null}
                              placeholder={customLiveMarket === 'a_stock' ? '输入代码或名称搜索' : '输入美股代码'}
                              onFocus={() => loadLiveCustomSymbolOptions(customLiveMarket, '')}
                              onSearch={query => loadLiveCustomSymbolOptions(customLiveMarket, query)}
                            />
                          </Form.Item>
                        </Col>
                      )}
                      <Col xs={12} sm={6} md={4} lg={3}>
                        <Form.Item name="max_positions" label="持仓数" rules={[{ required: true }]}>
                          <InputNumber min={1} max={100} controls className="factor-lab-full" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={12} md={6} lg={4}>
                        <Form.Item name="position_weights_text" label="仓位权重">
                          <Input placeholder="0.7:0.3,0.7:0.2:0.1" />
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
                      <Col xs={24} sm={12} md={6} lg={4}>
                        <Form.Item name="rotation_mode" label="调仓方式" rules={[{ required: true }]}>
                          <Select options={ROTATION_MODE_OPTIONS} />
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
                            <Text strong>组合因子</Text>
                            <Button
                              size="small"
                              icon={<PlusOutlined />}
                              onClick={() => add(buildDefaultBacktestLeg('raw_momentum', getFactorByKey(options?.factors, 'raw_momentum')))}
                            >
                              添加因子
                            </Button>
                          </div>
                          {fields.map(field => {
                            const leg = (liveForm.getFieldValue('legs') || [])[field.name] || {};
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
                                        options={liveFactorSelectOptions}
                                        onChange={value => handleLiveLegFactorChange(field.name, value)}
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
                  </Form>
                </Spin>
          </Modal>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card
                title="配置列表"
                bordered={false}
                extra={(
                  <Space>
                    <Button type="primary" icon={<PlusOutlined />} onClick={handleLiveCreate}>
                      添加配置
                    </Button>
                    <Button icon={<ReloadOutlined />} onClick={handleLiveRefresh} loading={liveLoading || externalTradingAccountsLoading} />
                  </Space>
                )}
              >
                <div className="factor-lab-live-mobile-list">
                  {liveLoading ? (
                    <div className="factor-lab-mobile-loading"><Spin /></div>
                  ) : liveConfigs.length ? (
                    liveConfigs.map(renderLiveConfigCard)
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  )}
                </div>
                <div className="factor-lab-live-table">
                  <Table
                    className="factor-lab-live-config-table"
                    rowKey="id"
                    size="small"
                    loading={liveLoading}
                    columns={liveConfigColumns}
                    dataSource={liveConfigs}
                    pagination={false}
                    scroll={{ x: 1800 }}
                    rowClassName={row => (row.id === selectedLiveConfigId ? 'factor-lab-table-row-selected' : '')}
                    onRow={row => ({
                      onClick: () => handleLiveConfigSelect(row.id),
                    })}
                  />
                </div>
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title={`最近信号：${selectedLiveConfigTitle}`} bordered={false}>
                {selectedLiveConfig?.last_signal_payload ? (
                  <Space direction="vertical" size={8} className="factor-lab-full">
                    <Space size={8} wrap>
                      <Tag color={selectedLiveConfig.last_signal_status === 'OK' ? 'green' : selectedLiveConfig.last_signal_status === 'SKIPPED' ? 'gold' : 'red'}>
                        {selectedLiveConfig.last_signal_status || '-'}
                      </Tag>
                      <span>信号日 {selectedLiveConfig.last_signal_date || '-'}</span>
                      <span>{selectedLiveConfig.last_signal_message || '-'}</span>
                    </Space>
                    <div className="factor-lab-compact-stats">
                      <span>卖出 {formatSymbolList(selectedLiveConfig.last_signal_payload.sell_symbols, selectedLiveConfig.last_signal_payload.symbol_names)}</span>
                      <span>补位 {formatSymbolList(selectedLiveConfig.last_signal_payload.buy_symbols, selectedLiveConfig.last_signal_payload.symbol_names)}</span>
                      <span>
                        目标 {formatTargetSymbolList(
                          selectedLiveConfig.last_signal_payload.target_symbols,
                          selectedLiveConfig.last_signal_payload.symbol_names,
                          selectedLiveConfig.last_signal_payload.target_weights,
                        )}
                      </span>
                    </div>
                  </Space>
                ) : (
                  <Empty />
                )}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title={`最近日志：${selectedLiveConfigTitle}`} bordered={false}>
                <div className="factor-lab-live-log-mobile-list">
                  {renderLiveLogCards()}
                </div>
                <div className="factor-lab-live-table">
                  <Table
                    rowKey="id"
                    size="small"
                    columns={liveLogColumns}
                    dataSource={liveLogs}
                    pagination={false}
                    scroll={{ x: 640, y: 420 }}
                  />
                </div>
              </Card>
            </Col>
          </Row>
        </>
      )}

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
                  <Select options={singleFactorSelectOptions} onChange={handleFactorChange} />
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
                                options={compositeFactorSelectOptions}
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
                <span>目标 {formatSymbolDisplay(timingMetadata.target_symbol, timingMetadata.symbol_names, timingMetadata.target_symbol_name)}</span>
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
                    options={backtestPoolOptions}
                    onChange={handleBacktestPoolChange}
                  />
                </Form.Item>
              </Col>
              {customBacktestSymbolsVisible && (
                <Col xs={24} md={12} lg={8}>
                  <Form.Item name="custom_symbols" label="股票标的" rules={[{ required: true, message: '请至少选择一个标的' }]}>
                    <Select
                      mode="tags"
                      showSearch
                      allowClear
                      maxTagCount="responsive"
                      tokenSeparators={[',', '，', ' ']}
                      filterOption={false}
                      options={backtestCustomSymbolSelectOptions}
                      loading={customSymbolSearching}
                      optionLabelProp="label"
                      notFoundContent={customSymbolSearching ? <Spin size="small" /> : null}
                      placeholder={customBacktestMarket === 'a_stock' ? '输入代码或名称搜索' : '输入美股代码'}
                      onFocus={() => loadCustomSymbolOptions(customBacktestMarket, '')}
                      onSearch={query => loadCustomSymbolOptions(customBacktestMarket, query)}
                    />
                  </Form.Item>
                </Col>
              )}
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
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="position_weights_text" label="仓位权重">
                  <Input placeholder="0.7:0.3" />
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
              <Col xs={24} sm={12} md={6} lg={4}>
                <Form.Item name="rotation_mode" label="调仓方式" rules={[{ required: true }]}>
                  <Select options={ROTATION_MODE_OPTIONS} />
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
                                options={backtestFactorSelectOptions}
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
              <Col xs={24} sm={12} md={8} lg={5}>
                <Form.Item label="仓位候选">
                  <Input
                    value={backtestSearchPositionWeights}
                    onChange={event => setBacktestSearchPositionWeights(event.target.value)}
                    placeholder="0.7:0.3,0.7:0.2:0.1"
                  />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12} md={8} lg={5}>
                <Form.Item label="调仓方式候选">
                  <Select
                    mode="multiple"
                    allowClear
                    value={backtestSearchRotationModes}
                    options={ROTATION_MODE_OPTIONS}
                    onChange={setBacktestSearchRotationModes}
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
              <Text type="secondary">默认参数用于美股多因子组合：风险调整动量 60% + 指数成分权重 40%，默认标准化为截面排名分位，因子间按标准化结果加权。</Text>
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
            scroll={{ x: 3210 }}
          />
        </Card>
      )}

      {backtestResult && (
        <Spin spinning={backtestRunning}>
          <Card
            className="factor-lab-backtest-params"
            title="回测参数"
            bordered={false}
            extra={<Button onClick={handleAddBacktestToLive}>加入线上交易</Button>}
          >
            <div className="factor-lab-param-grid">
              <span>股票池：{backtestMetadata.pool_label || backtestMetadata.pool}</span>
              {backtestMetadata.custom_symbol_count > 0 && <span>自定义标的：{numberFormatter(backtestMetadata.custom_symbol_count)}</span>}
              <span>区间：{backtestMetadata.start_date} 至 {backtestMetadata.end_date}</span>
              {backtestMetadata.oos_start_date && <span>样本外：{backtestMetadata.oos_start_date} 起</span>}
              <span>初始资金：{numberFormatter(backtestMetadata.initial_capital)}</span>
              <span>持仓数：Top{backtestMetadata.max_positions}</span>
              {backtestMetadata.position_weights_label && <span>仓位：{backtestMetadata.position_weights_label}</span>}
              <span>卖出阈值：Top{backtestMetadata.sell_rank_threshold}</span>
              <span>卖出倍数：{formatFactorWeight(backtestMetadata.sell_rank_multiplier)}</span>
              <span>调仓频率：{getRebalanceFrequencyLabel(backtestMetadata.rebalance_frequency)}</span>
              <span>调仓方式：{backtestMetadata.rotation_mode_label || getRotationModeLabel(backtestMetadata.rotation_mode)}</span>
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
                {backtestMetadata.custom_symbol_count > 0 && <span>自定义标的 {numberFormatter(backtestMetadata.custom_symbol_count)}</span>}
                <span>{backtestMetadata.universe_symbols} 只股票</span>
                <span>Top{backtestMetadata.max_positions}</span>
                {backtestMetadata.position_weights_label && <span>仓位 {backtestMetadata.position_weights_label}</span>}
                <span>卖出阈值 Top{backtestMetadata.sell_rank_threshold}</span>
                <span>{getRebalanceFrequencyLabel(backtestMetadata.rebalance_frequency)}</span>
                <span>{backtestMetadata.rotation_mode_label || getRotationModeLabel(backtestMetadata.rotation_mode)}</span>
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
                    option={getBacktestEquityOption(
                      backtestEquityRows,
                      backtestBenchmarkRows,
                      backtestMetadata.candidate_etfs,
                      backtestMetadata.symbol_names,
                    )}
                    style={{ height: 380 }}
                  />
                ) : <Empty />}
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title="标的盈亏" bordered={false}>
                <Table
                  rowKey="symbol"
                  size="small"
                  columns={backtestSymbolPnlColumns}
                  dataSource={backtestSymbolPnlRows}
                  pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
                  scroll={{ x: 1500 }}
                />
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
                  columns={backtestTradeColumnsWithFilters}
                  dataSource={backtestTradeRows}
                  pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
                  scroll={{ x: 1200 }}
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
