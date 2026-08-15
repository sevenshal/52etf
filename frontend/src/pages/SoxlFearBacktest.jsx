import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  message,
} from 'antd';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import { useLocation, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';

const { RangePicker } = DatePicker;

const parseNumberList = (value, integer = false) => {
  if (!value) {
    return [];
  }
  return value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => (integer ? parseInt(item, 10) : parseFloat(item)))
    .filter(item => !Number.isNaN(item));
};

const parseBooleanList = (value) => {
  if (!value) {
    return [];
  }
  return Array.from(new Set(value.map(item => item === true || item === 'true')));
};

// 换仓阈值候选：数字列表，支持 none/null/关闭 表示不启用（主辅跷跷板）
const parseSwapThresholdList = (value) => String(value ?? '')
  .split(',')
  .map(item => item.trim().toLowerCase())
  .filter(Boolean)
  .map(item => (item === 'none' || item === 'null' || item === '关闭' ? null : parseFloat(item)))
  .filter(item => item === null || Number.isFinite(item));

const formatPercent = (value, digits = 2) => `${Number(value || 0).toFixed(digits)}%`;
const formatNumber = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toFixed(digits)
);
const formatProfitLossRatio = (value) => formatNumber(value);
const formatMetricValue = (value, { precision = 2, suffix = '', formatter } = {}) => {
  if (formatter) {
    return formatter(value);
  }
  if (value === null || value === undefined) {
    return '-';
  }
  return `${Number(value || 0).toFixed(precision)}${suffix}`;
};
const formatSignedMetricValue = (value, { precision = 2, suffix = '' } = {}) => {
  const numericValue = Number(value || 0);
  const sign = numericValue > 0 ? '+' : '';
  return `${sign}${numericValue.toFixed(precision)}${suffix}`;
};
const getProfitLossRatioSortValue = (record) => {
  if (record.profit_loss_ratio === null || record.profit_loss_ratio === undefined) {
    return 0;
  }
  return Number(record.profit_loss_ratio || 0);
};

const objectiveOptions = [
  { label: '按年化收益最大', value: 'annualized_return' },
  { label: '按夏普最大', value: 'sharpe_ratio' },
];

const DEFAULT_SYMBOL_OPTIONS = [
  { label: 'SOXL.US', value: 'SOXL.US' },
  { label: 'TQQQ.US', value: 'TQQQ.US' },
  { label: 'UPRO.US', value: 'UPRO.US' },
  { label: 'SOXX.US', value: 'SOXX.US' },
  { label: 'QQQ.US', value: 'QQQ.US' },
  { label: 'SPY.US', value: 'SPY.US' },
];

const sellReductionBasisOptions = [
  { label: '按总资产', value: 'portfolio' },
  { label: '按持仓股票', value: 'holdings' },
];

const sellPriceAboveAvgCostOptions = [
  { label: '开启', value: 'true' },
  { label: '关闭', value: 'false' },
];

const executeNextOpenOptions = [
  { label: '开启(次日开盘成交)', value: 'true' },
  { label: '关闭(信号日收盘成交)', value: 'false' },
];

const DEFAULT_FEAR_SOURCE_OPTIONS = [
  { label: 'CNN贪恐', value: 'cnn' },
  { label: 'SOXX 半导体自算贪恐', value: 'soxx_clone' },
  { label: 'SPY 标普500自算贪恐', value: 'spy_clone' },
  { label: 'QQQ 纳指100自算贪恐', value: 'qqq_clone' },
  { label: 'DIA 道琼斯自算贪恐', value: 'dia_clone' },
];
const fearSourceColorMap = {
  cnn: 'blue',
  soxx_clone: 'cyan',
  spy_clone: 'green',
  qqq_clone: 'purple',
  dia_clone: 'geekblue',
};

const getObjectiveLabel = (value) => objectiveOptions.find(item => item.value === value)?.label || value;
const getSellReductionBasisLabel = (value) => sellReductionBasisOptions.find(item => item.value === value)?.label || value;
const getFearSourceLabelFromOptions = (options, value) => options.find(item => item.value === value)?.label || value;
const formatFearSourceLabelsFromOptions = (options, value) => {
  if (Array.isArray(value)) {
    return value.map(item => getFearSourceLabelFromOptions(options, item)).join('、');
  }
  return getFearSourceLabelFromOptions(options, value);
};

const SoxlFearBacktest = () => {
  const [form] = Form.useForm();
  const location = useLocation();
  const navigate = useNavigate();
  const [backtestOptions, setBacktestOptions] = useState({
    symbol_options: DEFAULT_SYMBOL_OPTIONS,
    volume_signal_symbol_options: DEFAULT_SYMBOL_OPTIONS,
    fear_source_options: DEFAULT_FEAR_SOURCE_OPTIONS,
    a_stock_preset_pairs: [],
  });
  const [optionsLoading, setOptionsLoading] = useState(false);
  const symbolOptions = backtestOptions.symbol_options?.length
    ? backtestOptions.symbol_options
    : DEFAULT_SYMBOL_OPTIONS;
  const volumeSignalSymbolOptions = backtestOptions.volume_signal_symbol_options?.length
    ? backtestOptions.volume_signal_symbol_options
    : symbolOptions;
  const fearSourceOptions = backtestOptions.fear_source_options?.length
    ? backtestOptions.fear_source_options
    : DEFAULT_FEAR_SOURCE_OPTIONS;
  const aStockPresetPairs = backtestOptions.a_stock_preset_pairs || [];
  const getFearSourceLabel = (value) => getFearSourceLabelFromOptions(fearSourceOptions, value);
  const formatFearSourceLabels = (value) => formatFearSourceLabelsFromOptions(fearSourceOptions, value);
  const selectedSymbol = Form.useWatch('symbol', form) || 'SOXL.US';
  const selectedVolumeSignalSymbol = Form.useWatch('volume_signal_symbol', form) || selectedSymbol;
  const selectedFearSources = Form.useWatch('fear_source_values', form) || ['cnn'];
  const selectedFearSourceLabel = formatFearSourceLabels(selectedFearSources);
  const [loading, setLoading] = useState(false);
  const [searchMeta, setSearchMeta] = useState(null);
  const [searchResults, setSearchResults] = useState([]);
  const [detailedResult, setDetailedResult] = useState(null);
  const detailFearSourceLabel = detailedResult?.meta?.fear_source_label || selectedFearSourceLabel;
  const isComparingFearSources = (detailedResult?.fear_series?.sources?.length || 0) > 1;
  const [detailLoading, setDetailLoading] = useState(false);
  const [searchTaskId, setSearchTaskId] = useState(null);
  const [searchProgress, setSearchProgress] = useState(0);
  const [searchProgressText, setSearchProgressText] = useState('');
  const [searchProcessed, setSearchProcessed] = useState(0);
  const [searchTotal, setSearchTotal] = useState(0);
  const [searchStatus, setSearchStatus] = useState(null);
  const searchTaskIdRef = useRef(null);
  const detailLoadingRef = useRef(false);
  const hasAutoRunRef = useRef(false);
  const handleSearchRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    const loadOptions = async () => {
      setOptionsLoading(true);
      try {
        const { data } = await request.get('/api/fear-volume-backtest/options');
        if (!cancelled) {
          setBacktestOptions({
            symbol_options: data.symbol_options?.length ? data.symbol_options : DEFAULT_SYMBOL_OPTIONS,
            volume_signal_symbol_options: data.volume_signal_symbol_options?.length
              ? data.volume_signal_symbol_options
              : (data.symbol_options?.length ? data.symbol_options : DEFAULT_SYMBOL_OPTIONS),
            fear_source_options: data.fear_source_options?.length ? data.fear_source_options : DEFAULT_FEAR_SOURCE_OPTIONS,
            a_stock_preset_pairs: data.a_stock_preset_pairs || [],
          });
        }
      } catch (error) {
        if (!cancelled) {
          message.error(error.response?.data?.detail || '加载回测选项失败');
        }
      } finally {
        if (!cancelled) {
          setOptionsLoading(false);
        }
      }
    };
    loadOptions();
    return () => {
      cancelled = true;
    };
  }, []);

  const buildPayload = (values) => {
    const sellPriceAboveAvgCostValues = parseBooleanList(values.sell_price_above_avg_cost_values);
    const executeNextOpenValues = parseBooleanList(values.execute_next_open_values);
    return {
      symbol: values.symbol || 'SOXL.US',
      volume_signal_symbol: values.volume_signal_symbol || undefined,
      fear_source_values: values.fear_source_values?.length ? values.fear_source_values : ['cnn'],
      initial_capital: values.initial_capital,
      start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
      end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
      top_n: values.top_n,
      objective: values.objective,
      eval_workers: values.eval_workers,
      rebalance_threshold_pct: values.fit_rebalance_threshold_pct,
      slippage_pct: values.slippage_pct ?? -1,
      stamp_duty_pct: values.stamp_duty_pct ?? 0,
      buy_threshold_values: parseNumberList(values.buy_threshold_values),
      greed_threshold_values: parseNumberList(values.greed_threshold_values),
      volume_ratio_threshold_values: parseNumberList(values.volume_ratio_threshold_values),
      volume_ratio_consecutive_days_values: parseNumberList(values.volume_ratio_consecutive_days_values, true),
      buy_position_pct_values: parseNumberList(values.buy_position_pct_values),
      cooldown_days_values: parseNumberList(values.cooldown_days_values, true),
      trailing_stop_pct_values: parseNumberList(values.trailing_stop_pct_values),
      sell_position_pct_values: parseNumberList(values.sell_position_pct_values),
      sell_reduction_basis_values: values.sell_reduction_basis_values,
      sell_price_above_avg_cost_values: sellPriceAboveAvgCostValues.length ? sellPriceAboveAvgCostValues : [true, false],
      max_take_profit_sells_per_cycle_values: parseNumberList(values.max_take_profit_sells_per_cycle_values, true),
      min_position_pct_after_take_profit_values: parseNumberList(values.min_position_pct_after_take_profit_values),
      execute_next_open_values: executeNextOpenValues.length ? executeNextOpenValues : [false, true],
      // 跷跷板候补（阈值参与组合搜索；sub_symbol 固定）
      sub_symbol: values.sub_symbol || undefined,
      sub_fear_source: values.sub_fear_source || 'a_stock_000688_sh',
      sub_volume_signal_symbol: values.sub_volume_signal_symbol || undefined,
      sub_buy_threshold_values: parseNumberList(values.sub_buy_threshold_values),
      sub_volume_ratio_threshold_values: parseNumberList(values.sub_volume_ratio_threshold_values),
      swap_threshold_values: parseSwapThresholdList(values.swap_threshold_values),
      // 第二候补（三标的轮动，可选）
      sub2_symbol: values.sub2_symbol || undefined,
      sub2_fear_source: values.sub2_fear_source || 'qqq_clone',
      sub2_volume_signal_symbol: values.sub2_volume_signal_symbol || undefined,
      sub2_buy_threshold_values: parseNumberList(values.sub2_buy_threshold_values),
      sub2_volume_ratio_threshold_values: parseNumberList(values.sub2_volume_ratio_threshold_values),
    };
  };

  const buildParamsFromRecord = (record) => ({
    buy_threshold: record.buy_threshold,
    greed_threshold: record.greed_threshold,
    volume_ratio_threshold: record.volume_ratio_threshold,
    volume_ratio_consecutive_days: record.volume_ratio_consecutive_days ?? 1,
    buy_position_pct: record.buy_position_pct,
    cooldown_days: record.cooldown_days,
    trailing_stop_pct: record.trailing_stop_pct,
    sell_position_pct: record.sell_position_pct,
    sell_reduction_basis: record.sell_reduction_basis,
    sell_price_above_avg_cost: record.sell_price_above_avg_cost,
    max_take_profit_sells_per_cycle: record.max_take_profit_sells_per_cycle,
    min_position_pct_after_take_profit: record.min_position_pct_after_take_profit,
    execute_next_open: record.execute_next_open ?? false,
    slippage_pct: record.slippage_pct ?? -1,
    stamp_duty_pct: record.stamp_duty_pct ?? 0,
    sub_symbol: record.sub_symbol ?? undefined,
    sub_fear_source: record.sub_fear_source ?? 'a_stock_000688_sh',
    sub_volume_signal_symbol: record.sub_volume_signal_symbol ?? undefined,
    sub_buy_threshold: record.sub_buy_threshold ?? 25,
    sub_volume_ratio_threshold: record.sub_volume_ratio_threshold ?? 1.6,
    swap_threshold: record.swap_threshold ?? null,
    sub2_symbol: record.sub2_symbol ?? undefined,
    sub2_fear_source: record.sub2_fear_source ?? 'qqq_clone',
    sub2_volume_signal_symbol: record.sub2_volume_signal_symbol ?? undefined,
    sub2_buy_threshold: record.sub2_buy_threshold ?? 20,
    sub2_volume_ratio_threshold: record.sub2_volume_ratio_threshold ?? 1.3,
    rebalance_threshold_pct: record.rebalance_threshold_pct,
  });

  const applyAStockPresetPair = (pair) => {
    if (!pair) {
      return;
    }
    form.setFieldsValue({
      a_stock_pair: pair.key,
      symbol: pair.target_symbol,
      volume_signal_symbol: undefined,
      fear_source_values: [pair.fear_source],
    });
  };

  const handleAStockPresetPairChange = (value) => {
    const pair = aStockPresetPairs.find(item => item.key === value);
    applyAStockPresetPair(pair);
  };

  const handleSymbolChange = (value) => {
    const pair = aStockPresetPairs.find(item => item.target_symbol === value);
    if (pair) {
      applyAStockPresetPair(pair);
      return;
    }
    if (value === '501225.SH') {
      form.setFieldsValue({
        a_stock_pair: undefined,
        fear_source_values: ['cnn'],
        volume_signal_symbol: 'SOXL.US',
      });
      return;
    }
    form.setFieldsValue({ a_stock_pair: undefined, volume_signal_symbol: undefined });
  };

  const applySearchJobStatus = (data) => {
    setSearchStatus(data.status);
    setSearchProgress(data.progress || 0);
    setSearchProgressText(data.message || '');
    setSearchProcessed(data.processed_combinations || 0);
    setSearchTotal(data.total_combinations || 0);

    if (data.status === 'completed') {
      setLoading(false);
      searchTaskIdRef.current = null;
      setSearchTaskId(null);
      setSearchMeta(data.result?.meta || null);
      setSearchResults(data.result?.results || []);
      setDetailedResult(data.result?.best_result || null);
      message.success(`搜索完成，共评估 ${data.result?.meta?.searched_combinations || 0} 组参数`);
      return;
    }

    if (data.status === 'failed') {
      setLoading(false);
      searchTaskIdRef.current = null;
      setSearchTaskId(null);
      message.error(data.error || '搜索失败');
    }
  };

  useEffect(() => {
    return subscribeBackendEvent('soxl_fear_search', (data) => {
      if (data.task_id !== searchTaskIdRef.current) return;
      applySearchJobStatus(data);
    });
  }, []);

  const handleSearch = async (values) => {
    setLoading(true);
    setSearchMeta(null);
    setSearchResults([]);
    setDetailedResult(null);
    setSearchProgress(0);
    setSearchProgressText('正在创建搜索任务');
    setSearchProcessed(0);
    setSearchStatus('pending');
    try {
      const payload = buildPayload(values);
      const { data } = await request.post('/api/fear-volume-backtest/search/jobs', payload, {
        timeout: 60 * 1000,
      });
      searchTaskIdRef.current = data.task_id;
      setSearchTaskId(data.task_id);
      setSearchTotal(data.total_combinations || 0);
      setSearchProgressText(`任务已创建，准备评估 ${data.total_combinations || 0} 组参数`);
    } catch (error) {
      setSearchStatus('failed');
      message.error(error.response?.data?.detail || '搜索失败');
      setLoading(false);
      searchTaskIdRef.current = null;
      setSearchTaskId(null);
    } finally {
    }
  };
  handleSearchRef.current = handleSearch;

  // 从实盘策略配置页跳转：autoRunBacktest=true 时把 presetValues 填进表单并自动回测一次
  useEffect(() => {
    const autoRunBacktest = location.state?.autoRunBacktest;
    const presetValues = location.state?.presetValues;
    if (!autoRunBacktest || !presetValues || hasAutoRunRef.current) {
      return;
    }

    hasAutoRunRef.current = true;
    const mergedValues = {
      ...form.getFieldsValue(),
      ...presetValues,
    };

    // history state 会被浏览器 structuredClone，dayjs 实例会丢失原型方法（isValid 报错），
    // 这里统一把日期字符串/对象转成 dayjs
    const rawRange = mergedValues.date_range || [];
    mergedValues.date_range = rawRange.map(value => (dayjs.isDayjs(value) ? value : dayjs(value)));
    if (!mergedValues.date_range.length) {
      mergedValues.date_range = [dayjs('2021-01-01'), dayjs()];
    }

    form.setFieldsValue(mergedValues);
    setTimeout(() => {
      handleSearchRef.current?.(mergedValues);
    }, 0);

    navigate(location.pathname, { replace: true, state: null });
  }, [form, location.pathname, location.state, navigate]);

  const loadDetail = async (record) => {
    if (detailLoadingRef.current) {
      return;
    }

    detailLoadingRef.current = true;
    setDetailLoading(true);
    try {
      const values = form.getFieldsValue();
      const payload = {
        symbol: values.symbol || 'SOXL.US',
        volume_signal_symbol: values.volume_signal_symbol || undefined,
        fear_source: record.fear_source || values.fear_source_values?.[0] || 'cnn',
        compare_fear_sources: values.fear_source_values?.length ? values.fear_source_values : [record.fear_source || 'cnn'],
        initial_capital: values.initial_capital,
        start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
        end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
        params: buildParamsFromRecord(record),
      };
      const { data } = await request.post('/api/fear-volume-backtest/run', payload);
      setDetailedResult(data);
      setTimeout(() => {
        document.getElementById('soxl-fear-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 100);
    } catch (error) {
      message.error(error.response?.data?.detail || '加载详细回测失败');
    } finally {
      detailLoadingRef.current = false;
      setDetailLoading(false);
    }
  };

  const resultColumns = [
    {
      title: '贪恐来源',
      dataIndex: 'fear_source_label',
      width: 190,
      fixed: 'left',
      render: (value, record) => (
        <Tag color={fearSourceColorMap[record.fear_source] || (String(record.fear_source || '').startsWith('a_stock_') ? 'orange' : 'blue')}>
          {value || getFearSourceLabel(record.fear_source)}
        </Tag>
      ),
      filters: fearSourceOptions.map(item => ({ text: item.label, value: item.value })),
      onFilter: (value, record) => record.fear_source === value,
    },
    { title: '买入阈值', dataIndex: 'buy_threshold', width: 90 },
    { title: '进入止盈区阈值(>=)', dataIndex: 'greed_threshold', width: 130 },
    { title: '量比阈值', dataIndex: 'volume_ratio_threshold', width: 90 },
    { title: '连续量比天数', dataIndex: 'volume_ratio_consecutive_days', width: 110 },
    { title: '买入仓位%', dataIndex: 'buy_position_pct', width: 90 },
    { title: '冷却天数', dataIndex: 'cooldown_days', width: 90 },
    { title: '止盈回撤%', dataIndex: 'trailing_stop_pct', width: 110, render: value => (Number(value) === 0 ? `${value}(贪恐即卖)` : value) },
    {
      title: '次日开盘成交',
      dataIndex: 'execute_next_open',
      width: 120,
      render: value => <Tag color={value ? 'green' : 'default'}>{value ? '开启' : '关闭'}</Tag>,
    },
    {
      title: '跷跷板候补',
      dataIndex: 'sub_symbol',
      width: 200,
      ellipsis: true,
      render: (value, record) => (value
        ? <Tag color="purple">{value} 恐慌≤{record.sub_buy_threshold}/量比≥{record.sub_volume_ratio_threshold}{record.swap_threshold != null ? `/换仓>${record.swap_threshold}` : ''}</Tag>
        : '-'),
    },
    {
      title: '第二候补',
      dataIndex: 'sub2_symbol',
      width: 190,
      ellipsis: true,
      render: (value, record) => (value
        ? <Tag color="magenta">{value} 恐慌≤{record.sub2_buy_threshold}/量比≥{record.sub2_volume_ratio_threshold}</Tag>
        : '-'),
    },
    { title: '止盈减仓%', dataIndex: 'sell_position_pct', width: 100 },
    {
      title: '止盈减仓口径',
      dataIndex: 'sell_reduction_basis',
      width: 110,
      render: value => getSellReductionBasisLabel(value),
    },
    {
      title: '均价保护',
      dataIndex: 'sell_price_above_avg_cost',
      width: 100,
      render: value => <Tag color={value ? 'green' : 'default'}>{value ? '开启' : '关闭'}</Tag>,
    },
    { title: '单轮止盈次数', dataIndex: 'max_take_profit_sells_per_cycle', width: 110 },
    { title: '保留仓位%', dataIndex: 'min_position_pct_after_take_profit', width: 100 },
    {
      title: '年化收益',
      dataIndex: 'annualized_return',
      width: 110,
      render: value => <span style={{ color: value >= 0 ? '#cf1322' : '#1677ff' }}>{formatPercent(value)}</span>,
      sorter: (a, b) => a.annualized_return - b.annualized_return,
      defaultSortOrder: 'descend',
    },
    {
      title: '年化波动率',
      dataIndex: 'annualized_volatility',
      width: 120,
      render: value => formatPercent(value),
      sorter: (a, b) => Number(a.annualized_volatility || 0) - Number(b.annualized_volatility || 0),
    },
    {
      title: 'Sharpe',
      dataIndex: 'sharpe_ratio',
      width: 90,
      render: value => Number(value || 0).toFixed(2),
      sorter: (a, b) => a.sharpe_ratio - b.sharpe_ratio,
    },
    {
      title: '所提诺比率',
      dataIndex: 'sortino_ratio',
      width: 110,
      render: value => formatNumber(value),
      sorter: (a, b) => Number(a.sortino_ratio || 0) - Number(b.sortino_ratio || 0),
    },
    {
      title: 'Calmar',
      dataIndex: 'calmar_ratio',
      width: 90,
      render: value => Number(value || 0).toFixed(2),
      sorter: (a, b) => a.calmar_ratio - b.calmar_ratio,
    },
    {
      title: '最大回撤',
      dataIndex: 'max_drawdown',
      width: 100,
      render: value => formatPercent(value),
      sorter: (a, b) => a.max_drawdown - b.max_drawdown,
    },
    {
      title: '最大回撤持续天数',
      dataIndex: 'max_drawdown_duration_days',
      width: 150,
      render: value => `${Number(value || 0)}天`,
      sorter: (a, b) => Number(a.max_drawdown_duration_days || 0) - Number(b.max_drawdown_duration_days || 0),
    },
    {
      title: '日盈亏比',
      dataIndex: 'profit_loss_ratio',
      width: 90,
      render: value => formatProfitLossRatio(value),
      sorter: (a, b) => getProfitLossRatioSortValue(a) - getProfitLossRatioSortValue(b),
    },
    {
      title: '交易数',
      dataIndex: 'trade_count',
      width: 80,
      sorter: (a, b) => a.trade_count - b.trade_count,
    },
    {
      title: '操作',
      key: 'action',
      width: 80,
      fixed: 'right',
      render: (_, record) => (
        <Button
          type="link"
          size="small"
          disabled={detailLoading}
          onClick={(event) => {
            event.stopPropagation();
            loadDetail(record);
          }}
        >
          详情
        </Button>
      ),
    },
  ];

  const tradeColumns = [
    { title: '日期', dataIndex: 'date', width: 110 },
    { title: '标的', dataIndex: 'symbol', width: 110, render: value => (value ? <Tag color={value === '510880.SH' ? 'orange' : 'purple'}>{value}</Tag> : '-') },
    {
      title: '方向',
      dataIndex: 'action',
      width: 90,
      render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag>,
    },
    { title: '价格', dataIndex: 'price', width: 90, render: value => Number(value || 0).toFixed(2) },
    { title: '交易股数', dataIndex: 'shares', width: 90, render: value => Number(value || 0).toFixed(0) },
    { title: '金额', dataIndex: 'amount', width: 110, render: value => Number(value || 0).toFixed(2) },
    { title: '持仓股数', dataIndex: 'position_after', width: 100, render: value => Number(value || 0).toFixed(0) },
    { title: '交易后仓位%', dataIndex: 'position_pct_after', width: 110, render: value => formatPercent(value) },
    { title: '现金', dataIndex: 'cash_after', width: 110, render: value => Number(value || 0).toFixed(2) },
    { title: '持仓市值', dataIndex: 'holdings_value_after', width: 120, render: value => Number(value || 0).toFixed(2) },
    { title: '净值', dataIndex: 'net_value_after', width: 120, render: value => Number(value || 0).toFixed(2) },
    {
      title: '收益',
      dataIndex: 'profit',
      width: 100,
      render: value => (
        <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>
          {Number(value || 0).toFixed(2)}
        </span>
      ),
    },
    {
      title: '收益率',
      dataIndex: 'profit_pct',
      width: 100,
      render: value => (
        <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>
          {formatPercent(value)}
        </span>
      ),
    },
    { title: '触发量比', dataIndex: 'buy_volume_ratio', width: 100, render: value => (value == null ? '-' : Number(value).toFixed(2)) },
    { title: '原因', dataIndex: 'reason' },
  ];

  const yearlyReturnColumns = [
    { title: '年份', dataIndex: 'year', width: 100 },
    {
      title: '策略收益',
      dataIndex: 'strategy_return',
      width: 120,
      render: value => (
        <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>
          {formatPercent(value)}
        </span>
      ),
    },
    {
      title: `${selectedSymbol}买入持有`,
      dataIndex: 'benchmark_return',
      width: 140,
      render: value => (
        <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>
          {formatPercent(value)}
        </span>
      ),
    },
    {
      title: '超额收益',
      dataIndex: 'excess_return',
      width: 120,
      render: value => (
        <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>
          {formatPercent(value)}
        </span>
      ),
    },
    { title: '总交易', dataIndex: 'trade_count', width: 90 },
    { title: '买入', dataIndex: 'buy_count', width: 80 },
    { title: '卖出', dataIndex: 'sell_count', width: 80 },
  ];

  const equityOption = useMemo(() => {
    if (!detailedResult?.equity_curve?.length) {
      return {};
    }
    const dates = detailedResult.equity_curve.map(item => item.date);
    const values = detailedResult.equity_curve.map(item => item.value);
    const benchmark = detailedResult.equity_curve.map(item => item.benchmark_value);
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略净值', `${selectedSymbol}买入持有`] },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside', start: 50, end: 100 }, { type: 'slider' }],
      series: [
        {
          name: '策略净值',
          type: 'line',
          data: values,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#cf1322' },
          areaStyle: { opacity: 0.12, color: '#cf1322' },
        },
        {
          name: `${selectedSymbol}买入持有`,
          type: 'line',
          data: benchmark,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#1677ff' },
        },
      ],
    };
  }, [detailedResult, selectedSymbol]);

  const drawdownOption = useMemo(() => {
    if (!detailedResult?.equity_curve?.length) {
      return {};
    }
    const dates = detailedResult.equity_curve.map(item => item.date);
    return {
      tooltip: {
        trigger: 'axis',
        valueFormatter: value => `${Number(value || 0).toFixed(2)}%`,
      },
      legend: { data: ['策略回撤', `${selectedSymbol}买入持有回撤`] },
      xAxis: { type: 'category', data: dates },
      yAxis: {
        type: 'value',
        max: 0,
        axisLabel: {
          formatter: value => `${value}%`,
        },
      },
      dataZoom: [{ type: 'inside', start: 50, end: 100 }, { type: 'slider' }],
      series: [
        {
          name: '策略回撤',
          type: 'line',
          data: detailedResult.equity_curve.map(item => item.drawdown),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#cf1322' },
          areaStyle: { opacity: 0.12, color: '#cf1322' },
        },
        {
          name: `${selectedSymbol}买入持有回撤`,
          type: 'line',
          data: detailedResult.equity_curve.map(item => item.benchmark_drawdown),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#1677ff' },
          areaStyle: { opacity: 0.08, color: '#1677ff' },
        },
      ],
    };
  }, [detailedResult, selectedSymbol]);

  const priceVolumeOption = useMemo(() => {
    if (!detailedResult?.daily_data?.length) {
      return {};
    }

    const dates = detailedResult.daily_data.map(item => item.date);
    const klineData = detailedResult.daily_data.map(item => [item.open, item.close, item.low, item.high]);
    const ma20Data = detailedResult.daily_data.map(item => item.ma20);
    const volumeData = detailedResult.daily_data.map(item => ({
      value: item.volume,
      itemStyle: { color: item.close >= item.open ? '#cf1322' : '#1677ff' },
    }));
    const volumeMA20Data = detailedResult.daily_data.map(item => item.volume_ma20);

    // 跷跷板候补：K线/成交量叠加到同一图（蓝色系区分主次）
    const firstSub = detailedResult.daily_data.find(item => item.sub_symbol);
    const subSymbol = firstSub?.sub_symbol || null;
    // 第二候补（三标的）
    const firstSub2 = detailedResult.daily_data.find(item => item.sub2_symbol);
    const sub2Symbol = firstSub2?.sub2_symbol || null;
    const subKlineData = detailedResult.daily_data.map(item => (
      item.sub_open == null ? null : [item.sub_open, item.sub_close, item.sub_low, item.sub_high]
    ));
    const subVolumeData = detailedResult.daily_data.map(item => ({
      value: item.sub_volume,
      itemStyle: { color: '#13c2c2' },
    }));

    const buyMarkers = (detailedResult.trades || [])
      .filter(item => item.action === 'BUY' && (!subSymbol || item.symbol !== subSymbol) && (!sub2Symbol || item.symbol !== sub2Symbol))
      .map(item => ({
        name: '买',
        value: 'B',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#cf1322' },
      }))
      .filter(item => item.xAxis >= 0 && Number.isFinite(item.yAxis));
    const sellMarkers = (detailedResult.trades || [])
      .filter(item => item.action === 'SELL' && (!subSymbol || item.symbol !== subSymbol) && (!sub2Symbol || item.symbol !== sub2Symbol))
      .map(item => ({
        name: '卖',
        value: 'S',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#1677ff' },
      }))
      .filter(item => item.xAxis >= 0 && Number.isFinite(item.yAxis));
    const subBuyMarkers = subSymbol ? (detailedResult.trades || [])
      .filter(item => item.action === 'BUY' && item.symbol === subSymbol)
      .map(item => ({
        name: '候补买',
        value: 'B',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#722ed1' },
      })) : [];
    const subSellMarkers = subSymbol ? (detailedResult.trades || [])
      .filter(item => item.action === 'SELL' && item.symbol === subSymbol)
      .map(item => ({
        name: '候补卖',
        value: 'S',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#13c2c2' },
      })) : [];
    const sub2KlineData = sub2Symbol ? detailedResult.daily_data.map(item => (
      item.sub2_open == null ? null : [item.sub2_open, item.sub2_close, item.sub2_low, item.sub2_high]
    )) : [];
    const sub2HasData = sub2KlineData.some(d => d != null);
    const sub2VolumeData = sub2Symbol ? detailedResult.daily_data.map(item => ({
      value: Number.isFinite(item.sub2_volume) ? item.sub2_volume : null,
      itemStyle: { color: '#eb2f96' },
    })) : [];
    const sub2BuyMarkers = sub2Symbol ? (detailedResult.trades || [])
      .filter(item => item.action === 'BUY' && item.symbol === sub2Symbol)
      .map(item => ({ name: '第二候补买', value: 'B', xAxis: dates.indexOf(item.date), yAxis: item.price, itemStyle: { color: '#fa8c16' } }))
      .filter(item => item.xAxis >= 0 && Number.isFinite(item.yAxis)) : [];
    const sub2SellMarkers = sub2Symbol ? (detailedResult.trades || [])
      .filter(item => item.action === 'SELL' && item.symbol === sub2Symbol)
      .map(item => ({ name: '第二候补卖', value: 'S', xAxis: dates.indexOf(item.date), yAxis: item.price, itemStyle: { color: '#eb2f96' } }))
      .filter(item => item.xAxis >= 0 && Number.isFinite(item.yAxis)) : [];

    const legendData = [`${selectedSymbol} K线`, 'MA20', '成交量', '成交量MA20'];
    if (subSymbol) {
      legendData.push(`${subSymbol} K线`, `${subSymbol} 成交量`);
    }
    if (sub2Symbol && sub2HasData) {
      legendData.push(`${sub2Symbol} K线`, `${sub2Symbol} 成交量`);
    }

    const series = [
      {
        name: `${selectedSymbol} K线`,
        type: 'candlestick',
        data: klineData,
        itemStyle: {
          color: '#cf1322',
          color0: '#1677ff',
          borderColor: '#cf1322',
          borderColor0: '#1677ff',
        },
        markPoint: {
          data: [...buyMarkers, ...sellMarkers],
          symbolSize: 26,
          label: { color: '#fff', fontWeight: 'bold' },
        },
      },
    ];
    if (subSymbol) {
      series.push({
        name: `${subSymbol} K线`,
        type: 'candlestick',
        data: subKlineData,
        barWidth: 6,
        itemStyle: {
          color: 'rgba(19,194,194,0.55)',
          color0: 'rgba(47,84,235,0.45)',
          borderColor: '#13c2c2',
          borderColor0: '#2f54eb',
        },
        markPoint: {
          data: [...subBuyMarkers, ...subSellMarkers],
          symbolSize: 24,
          symbol: 'pin',
          label: { color: '#fff', fontWeight: 'bold' },
        },
      });
    }
    if (sub2Symbol && sub2HasData) {
      series.push({
        name: `${sub2Symbol} K线`,
        type: 'candlestick',
        data: sub2KlineData,
        barWidth: 5,
        itemStyle: {
          color: 'rgba(235,47,150,0.4)',
          color0: 'rgba(250,140,22,0.4)',
          borderColor: '#eb2f96',
          borderColor0: '#fa8c16',
        },
        markPoint: {
          data: [...sub2BuyMarkers, ...sub2SellMarkers],
          symbolSize: 22,
          symbol: 'pin',
          label: { color: '#fff', fontWeight: 'bold' },
        },
      });
    }
    series.push(
      {
        name: 'MA20',
        type: 'line',
        data: ma20Data,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: '#faad14' },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumeData,
      },
      {
        name: '成交量MA20',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumeMA20Data,
        showSymbol: false,
        lineStyle: { width: 2, color: '#52c41a' },
      },
    );
    if (subSymbol) {
      series.push({
        name: `${subSymbol} 成交量`,
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 2,
        data: subVolumeData,
        barWidth: 5,
      });
    }
    if (sub2Symbol && sub2HasData) {
      series.push({
        name: `${sub2Symbol} 成交量`,
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 3,
        data: sub2VolumeData,
        barWidth: 4,
      });
    }

    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: legendData },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: '8%', right: '8%', top: 40, height: '54%' },
        { left: '8%', right: '8%', top: '72%', height: '18%' },
      ],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
        { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: { onZero: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, min: 'dataMin', max: 'dataMax' },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2 },
        ...(subSymbol ? [{ scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false } }] : []),
        ...(sub2Symbol && sub2HasData ? [{ scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false } }] : []),
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: 10, start: 60, end: 100 },
      ],
      series,
    };
  }, [detailedResult, selectedSymbol]);

  const sentimentOption = useMemo(() => {
    if (!detailedResult?.daily_data?.length) {
      return {};
    }
    const compareSeries = detailedResult.fear_series;
    const hasCompareSeries = compareSeries?.data?.length && compareSeries?.sources?.length;
    const dates = hasCompareSeries
      ? compareSeries.data.map(item => item.date)
      : detailedResult.daily_data.map(item => item.date);
    const sources = hasCompareSeries
      ? compareSeries.sources
      : [{ key: 'selected', label: detailFearSourceLabel }];
    const colorMap = {
      cnn: '#1677ff',
      soxx_clone: '#13c2c2',
      spy_clone: '#52c41a',
      qqq_clone: '#722ed1',
      dia_clone: '#2f54eb',
      selected: '#13c2c2',
    };
    const series = sources.map(source => ({
      name: source.label,
      type: 'line',
      data: hasCompareSeries
        ? compareSeries.data.map(item => item[source.key] ?? null)
        : detailedResult.daily_data.map(item => item.fear_greed ?? item.cnn_fear_greed),
      showSymbol: false,
      connectNulls: true,
      lineStyle: {
        width: source.label === detailFearSourceLabel ? 3 : 2,
        color: colorMap[source.key] || '#13c2c2',
      },
    }));

    return {
      tooltip: { trigger: 'axis' },
      legend: { data: sources.map(item => item.label) },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside', start: 60, end: 100 }, { type: 'slider', start: 60, end: 100 }],
      series,
    };
  }, [detailedResult, detailFearSourceLabel]);

  const renderComparedMetric = ({
    title,
    dataIndex,
    precision = 2,
    suffix = '',
    formatter,
    higherIsBetter = true,
  }) => {
    const strategyValue = detailedResult?.[dataIndex];
    const benchmarkValue = detailedResult?.benchmark_metrics?.[dataIndex];
    const hasNumericDiff = (
      typeof strategyValue === 'number'
      && typeof benchmarkValue === 'number'
      && Number.isFinite(strategyValue)
      && Number.isFinite(benchmarkValue)
    );
    const diffValue = hasNumericDiff ? strategyValue - benchmarkValue : null;
    const isBetter = hasNumericDiff && (higherIsBetter ? diffValue >= 0 : diffValue <= 0);

    return (
      <Col xs={12} md={6} key={dataIndex}>
        <Card loading={detailLoading}>
          <Statistic
            title={title}
            value={strategyValue}
            precision={precision}
            suffix={suffix}
            formatter={formatter ? () => formatter(strategyValue) : undefined}
          />
          <div style={{ marginTop: 8, fontSize: 12, color: '#666', lineHeight: 1.6 }}>
            <span>基准 {formatMetricValue(benchmarkValue, { precision, suffix, formatter })}</span>
            {hasNumericDiff && (
              <span style={{ marginLeft: 12, color: isBetter ? '#cf1322' : '#1677ff' }}>
                差值 {formatSignedMetricValue(diffValue, { precision, suffix })}
              </span>
            )}
          </div>
        </Card>
      </Col>
    );
  };

  return (
    <div style={{ padding: 24 }}>
      <Card title={`${selectedSymbol} 情绪 + 量能 超参数回测`} style={{ marginBottom: 24 }} loading={optionsLoading}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="策略假设"
          description={`使用所选贪恐来源（${selectedFearSourceLabel}）和 ${selectedVolumeSignalSymbol} 的信号日量比；当贪恐分数低于等于买入触发阈值，且量比放大满足连续天数要求时买入；连续 N 天量比使用最近 N 个交易日成交量对比再往前 20 个交易日均量；成交模式可选信号日收盘价成交，或信号日收盘决策、下一交易日开盘价成交；当贪恐分数高于等于进入止盈区阈值后，若移动止盈回撤% 设为 0，则到达贪恐阈值当天即卖出；否则按收盘价较止盈区内最高价回撤的规则移动止盈；均价保护开启时，卖出价必须高于当前持仓均价；止盈减仓口径可选按总资产或按持仓股票；同时不会把仓位卖穿最低保留仓位；同一轮止盈区可限制最多卖出次数；买卖后按交易日冷却 n 天。`}
        />
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSearch}
          initialValues={{
            // 推荐配置：红利 + 科创50 + 纳指(159941, 量比用 QQQ, 恐贪 qqq_clone) 对称轮动 换仓45
            symbol: '510880.SH',
            fear_source_values: ['a_stock_000015_sh'],
            initial_capital: 1000000,
            top_n: 20,
            objective: 'annualized_return',
            eval_workers: 4,
            fit_rebalance_threshold_pct: 0,
            slippage_pct: -1,
            stamp_duty_pct: 0,
            date_range: [dayjs('2023-03-22'), dayjs()],
            buy_threshold_values: '30',
            greed_threshold_values: '70',
            volume_ratio_threshold_values: '1.6',
            volume_ratio_consecutive_days_values: '1',
            buy_position_pct_values: '100',
            cooldown_days_values: '0',
            trailing_stop_pct_values: '0',
            sell_position_pct_values: '100',
            sell_reduction_basis_values: ['holdings'],
            sell_price_above_avg_cost_values: ['false'],
            max_take_profit_sells_per_cycle_values: '2',
            min_position_pct_after_take_profit_values: '0',
            execute_next_open_values: ['true'],
            sub_symbol: '588000.SH',
            sub_fear_source: 'a_stock_000688_sh',
            sub_volume_signal_symbol: undefined,
            sub_buy_threshold_values: '25',
            sub_volume_ratio_threshold_values: '1.6',
            swap_threshold_values: '45',
            sub2_symbol: '159941.SZ',
            sub2_fear_source: 'qqq_clone',
            sub2_volume_signal_symbol: 'QQQ.US',
            sub2_buy_threshold_values: '20',
            sub2_volume_ratio_threshold_values: '1.3',
          }}
        >
          <Row gutter={16}>
            <Col xs={24} md={6}>
              <Form.Item name="a_stock_pair" label="A股指数ETF组合">
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="选择后自动填入标的和贪恐来源"
                  options={aStockPresetPairs.map(item => ({ label: item.label, value: item.key }))}
                  onChange={handleAStockPresetPairChange}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="symbol" label="标的">
                <Select showSearch optionFilterProp="label" options={symbolOptions} onChange={handleSymbolChange} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="volume_signal_symbol" label="量比来源标的">
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="默认使用标的自身"
                  options={volumeSignalSymbolOptions}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="date_range" label="回测区间" rules={[{ required: true, message: '请选择回测区间' }]}>
                <RangePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="initial_capital" label="初始资金">
                <InputNumber min={1000} step={1000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="top_n" label="返回前N组">
                <InputNumber min={1} max={500} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="objective" label="搜索目标">
                <Select options={objectiveOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="eval_workers" label="并发进程数">
                <InputNumber min={1} max={16} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="fear_source_values" label="贪恐来源候选">
                <Select mode="multiple" showSearch optionFilterProp="label" maxTagCount="responsive" options={fearSourceOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="buy_threshold_values" label="买入贪恐阈值(<=)候选">
                <Input placeholder="例如 35,40,45" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="buy_position_pct_values" label="每次买入仓位% 候选">
                <Input placeholder="例如 50,60,70" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="volume_ratio_threshold_values" label="买入量比阈值(>=)候选">
                <Input placeholder="例如 1.3,1.38,1.45" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="volume_ratio_consecutive_days_values" label="连续量比天数候选">
                <Input placeholder="例如 1,3" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="greed_threshold_values" label="止盈区贪恐阈值(>=) 候选">
                <Input placeholder="例如 40,41,42" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sell_position_pct_values" label="止盈减仓% 候选">
                <Input placeholder="例如 25,50" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="trailing_stop_pct_values" label="移动止盈回撤% 候选(0=贪恐即卖)">
                <Input placeholder="例如 0,3,5,7" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="execute_next_open_values" label="次日开盘成交候选">
                <Select mode="multiple" options={executeNextOpenOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sell_reduction_basis_values" label="止盈减仓口径候选">
                <Select mode="multiple" options={sellReductionBasisOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sell_price_above_avg_cost_values" label="均价保护开关候选">
                <Select mode="multiple" options={sellPriceAboveAvgCostOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="max_take_profit_sells_per_cycle_values" label="单轮止盈次数候选">
                <Input placeholder="例如 2,3" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="cooldown_days_values" label="冷却天数候选">
                <Input placeholder="例如 3,5" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="min_position_pct_after_take_profit_values" label="保留仓位% 候选">
                <Input placeholder="例如 10,20" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="fit_rebalance_threshold_pct" label="调仓阈值%">
                <InputNumber min={0.1} max={100} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="slippage_pct" label="滑点%(-1=最悲观)">
                <InputNumber min={-1} max={10} step={0.1} placeholder="0=无滑点；-1=买最高卖最低" style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="stamp_duty_pct" label="印花税%(卖出收取)">
                <InputNumber min={0} max={10} step={0.05} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={24}>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="跷跷板候补 / 对称双轮动（可选）"
                description="留空候补标的 = 单标的模式。填写候补标的：若「换仓阈值」留空 = 主辅跷跷板（主标的优先，空仓时才买候补，主标的出信号换回）；若填了换仓阈值 = 对称双轮动（任一标的极恐放量都买、都触发买更恐慌的；持有 X 时若 X 恐贪超过换仓阈值且另一标的有买入信号则换仓；恐贪≥贪恐卖出阈值则卖出）。"
              />
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub_symbol" label="候补标的">
                <Select allowClear showSearch optionFilterProp="label" placeholder="留空=单标的" options={symbolOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub_fear_source" label="候补恐贪来源">
                <Select showSearch optionFilterProp="label" options={fearSourceOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub_volume_signal_symbol" label="候补量比来源">
                <Select allowClear showSearch optionFilterProp="label" placeholder="默认候补自身" options={volumeSignalSymbolOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub_buy_threshold_values" label="候补恐慌阈值(<=)候选">
                <Input placeholder="例如 25,30（参与组合搜索）" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub_volume_ratio_threshold_values" label="候补量比阈值(>=)候选">
                <Input placeholder="例如 1.3,1.6" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="swap_threshold_values" label="换仓阈值候选(none=关闭)">
                <Input placeholder="例如 none,45,55（none=主辅跷跷板）" />
              </Form.Item>
            </Col>
            <Col xs={24} md={24}>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="第二候补（三标的轮动，可选）"
                description="再填一个候补可做三标的对称轮动（需换仓阈值非空）：空仓时任一标的极恐放量都买（都触发买恐贪最低的）；持有 X 时 X 恐贪超过换仓阈值且任一其他标的有信号则换仓。例如纳指ETF 159941.SZ 配 QQQ 自算贪恐（美股恐贪上海凌晨已算出，信号日与 A股对齐）。"
              />
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub2_symbol" label="第二候补标的">
                <Select allowClear showSearch optionFilterProp="label" placeholder="留空=双标的" options={symbolOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub2_fear_source" label="第二候补恐贪来源">
                <Select showSearch optionFilterProp="label" options={fearSourceOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub2_volume_signal_symbol" label="第二候补量比来源">
                <Select allowClear showSearch optionFilterProp="label" placeholder="默认自身" options={volumeSignalSymbolOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub2_buy_threshold_values" label="第二候补恐慌阈值(<=)候选">
                <Input placeholder="例如 20,25" />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="sub2_volume_ratio_threshold_values" label="第二候补量比阈值(>=)候选">
                <Input placeholder="例如 1.3,1.6" />
              </Form.Item>
            </Col>
          </Row>

          <Space>
            <Button type="primary" htmlType="submit" loading={loading}>
              搜索最佳参数
            </Button>
          </Space>
        </Form>
      </Card>

      {loading && (
        <Card title="搜索进度" style={{ marginBottom: 24 }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Progress percent={searchProgress} status={searchStatus === 'failed' ? 'exception' : 'active'} />
            <Descriptions column={{ xs: 1, md: 3 }} bordered size="small">
              <Descriptions.Item label="任务状态">{searchStatus || 'pending'}</Descriptions.Item>
              <Descriptions.Item label="已评估组合">{searchProcessed}</Descriptions.Item>
              <Descriptions.Item label="总组合数">{searchTotal}</Descriptions.Item>
            </Descriptions>
            <Alert
              type="info"
              showIcon
              message={searchProgressText || '正在搜索最佳参数'}
              description={searchTaskId ? `任务ID: ${searchTaskId}` : '正在初始化任务'}
            />
          </Space>
        </Card>
      )}

      {searchMeta && (
        <Card title="搜索摘要" style={{ marginBottom: 24 }}>
          <Descriptions column={{ xs: 1, md: 2, lg: 4 }} bordered size="small">
            <Descriptions.Item label="请求区间">{searchMeta.requested_start_date} ~ {searchMeta.requested_end_date}</Descriptions.Item>
            <Descriptions.Item label="有效区间">{searchMeta.effective_start_date} ~ {searchMeta.effective_end_date}</Descriptions.Item>
            <Descriptions.Item label="交易日数">{searchMeta.trading_days}</Descriptions.Item>
            <Descriptions.Item label="搜索组合数">{searchMeta.searched_combinations}</Descriptions.Item>
            <Descriptions.Item label="搜索目标">{getObjectiveLabel(searchMeta.objective)}</Descriptions.Item>
            <Descriptions.Item label="贪恐来源">{searchMeta.fear_source_labels || formatFearSourceLabels(searchMeta.fear_sources || searchMeta.fear_source)}</Descriptions.Item>
            <Descriptions.Item label="量比来源">{searchMeta.volume_signal_label || searchMeta.volume_signal_symbol || selectedVolumeSignalSymbol}</Descriptions.Item>
            <Descriptions.Item label="成交口径">{searchMeta.execution_price_label || '信号日收盘价'}</Descriptions.Item>
            <Descriptions.Item label="贪恐数据点">{searchMeta.fear_points}</Descriptions.Item>
            <Descriptions.Item label="并发进程数">{searchMeta.eval_workers}</Descriptions.Item>
            <Descriptions.Item label="有效组合数">{searchMeta.valid_combinations}</Descriptions.Item>
            <Descriptions.Item label="跳过组合数">{searchMeta.skipped_combinations}</Descriptions.Item>
          </Descriptions>
        </Card>
      )}

      {searchResults.length > 0 && (
        <Card title={`最优参数候选 (${searchResults.length} 组)`} style={{ marginBottom: 24 }}>
          <Table
            dataSource={searchResults}
            columns={resultColumns}
            rowKey={(record) => `${record.fear_source}-${record.buy_threshold}-${record.greed_threshold}-${record.volume_ratio_threshold}-${record.volume_ratio_consecutive_days}-${record.buy_position_pct}-${record.cooldown_days}-${record.trailing_stop_pct}-${record.sell_position_pct}-${record.sell_reduction_basis}-${record.sell_price_above_avg_cost}-${record.max_take_profit_sells_per_cycle}-${record.min_position_pct_after_take_profit}-${record.execute_next_open}`}
            pagination={{ defaultPageSize: 10 }}
            scroll={{ x: 1980 }}
            onRow={(record) => ({
              onClick: () => loadDetail(record),
              style: { cursor: 'pointer' },
            })}
          />
        </Card>
      )}

      {detailedResult && (
        <div id="soxl-fear-detail">
          <Row gutter={16} style={{ marginBottom: 24 }}>
            {renderComparedMetric({ title: '总收益率', dataIndex: 'total_return', suffix: '%' })}
            {renderComparedMetric({ title: '年化收益率', dataIndex: 'annualized_return', suffix: '%' })}
            {renderComparedMetric({ title: '年化波动率', dataIndex: 'annualized_volatility', suffix: '%', higherIsBetter: false })}
            {renderComparedMetric({ title: '最大回撤', dataIndex: 'max_drawdown', suffix: '%', higherIsBetter: false })}
          </Row>

          <Row gutter={16} style={{ marginBottom: 24 }}>
            {renderComparedMetric({ title: 'Sharpe', dataIndex: 'sharpe_ratio' })}
            {renderComparedMetric({ title: '所提诺比率', dataIndex: 'sortino_ratio' })}
            {renderComparedMetric({ title: 'Calmar', dataIndex: 'calmar_ratio' })}
            {renderComparedMetric({ title: '最大回撤持续天数', dataIndex: 'max_drawdown_duration_days', precision: 0, suffix: '天', higherIsBetter: false })}
          </Row>

          <Row gutter={16} style={{ marginBottom: 24 }}>
            {renderComparedMetric({ title: '日胜率', dataIndex: 'win_rate', suffix: '%' })}
            {renderComparedMetric({ title: '日盈亏比', dataIndex: 'profit_loss_ratio', formatter: formatProfitLossRatio })}
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="买入次数" value={detailedResult.buy_count} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="卖出次数" value={detailedResult.sell_count} />
              </Card>
            </Col>
          </Row>

          <Card title="参数明细" style={{ marginBottom: 24 }} loading={detailLoading}>
            <Descriptions column={{ xs: 1, md: 2, lg: 3 }} bordered size="small">
              <Descriptions.Item label="买入触发阈值">{detailedResult.params?.buy_threshold}</Descriptions.Item>
              <Descriptions.Item label="进入止盈区阈值(>=)">{detailedResult.params?.greed_threshold}</Descriptions.Item>
              <Descriptions.Item label="量比阈值">{detailedResult.params?.volume_ratio_threshold}</Descriptions.Item>
              <Descriptions.Item label="连续量比天数">{detailedResult.params?.volume_ratio_consecutive_days ?? 1}</Descriptions.Item>
              <Descriptions.Item label="每次买入仓位%">{detailedResult.params?.buy_position_pct}</Descriptions.Item>
              <Descriptions.Item label="冷却天数">{detailedResult.params?.cooldown_days}</Descriptions.Item>
              <Descriptions.Item label="移动止盈回撤%">{detailedResult.params?.trailing_stop_pct}{Number(detailedResult.params?.trailing_stop_pct) === 0 ? '（到达贪恐即卖）' : ''}</Descriptions.Item>
              <Descriptions.Item label="止盈减仓%">{detailedResult.params?.sell_position_pct}</Descriptions.Item>
              <Descriptions.Item label="止盈减仓口径">{getSellReductionBasisLabel(detailedResult.params?.sell_reduction_basis)}</Descriptions.Item>
              <Descriptions.Item label="均价保护">{detailedResult.params?.sell_price_above_avg_cost ? '开启' : '关闭'}</Descriptions.Item>
              <Descriptions.Item label="同轮止盈最多卖出次数">{detailedResult.params?.max_take_profit_sells_per_cycle}</Descriptions.Item>
              <Descriptions.Item label="止盈后最低保留仓位%">{detailedResult.params?.min_position_pct_after_take_profit}</Descriptions.Item>
              <Descriptions.Item label="次日开盘成交">{detailedResult.params?.execute_next_open ? '开启（信号日收盘决策，次日开盘成交）' : '关闭（信号日收盘价成交）'}</Descriptions.Item>
              {detailedResult.params?.sub_symbol && (
                <>
                  <Descriptions.Item label="跷跷板候补">{detailedResult.params.sub_symbol}</Descriptions.Item>
                  <Descriptions.Item label="候补恐贪来源">{getFearSourceLabel(detailedResult.params.sub_fear_source)}</Descriptions.Item>
                  <Descriptions.Item label="候补恐慌阈值">{detailedResult.params.sub_buy_threshold}</Descriptions.Item>
                  <Descriptions.Item label="候补量比阈值">{detailedResult.params.sub_volume_ratio_threshold}</Descriptions.Item>
                  <Descriptions.Item label="换仓阈值">{detailedResult.params.swap_threshold ?? '关闭（主辅跷跷板）'}</Descriptions.Item>
                </>
              )}
              {detailedResult.params?.sub2_symbol && (
                <>
                  <Descriptions.Item label="第二候补">{detailedResult.params.sub2_symbol}</Descriptions.Item>
                  <Descriptions.Item label="第二候补恐贪来源">{getFearSourceLabel(detailedResult.params.sub2_fear_source)}</Descriptions.Item>
                  <Descriptions.Item label="第二候补恐慌阈值">{detailedResult.params.sub2_buy_threshold}</Descriptions.Item>
                  <Descriptions.Item label="第二候补量比阈值">{detailedResult.params.sub2_volume_ratio_threshold}</Descriptions.Item>
                </>
              )}
              <Descriptions.Item label="调仓阈值%">{detailedResult.params?.rebalance_threshold_pct}</Descriptions.Item>
              <Descriptions.Item label="滑点%">{detailedResult.params?.slippage_pct === -1 ? '-1（最悲观：买最高卖最低）' : `${detailedResult.params?.slippage_pct ?? 0}%`}</Descriptions.Item>
              <Descriptions.Item label="印花税%(卖出)">{detailedResult.params?.stamp_duty_pct ?? 0}%</Descriptions.Item>
              <Descriptions.Item label="贪恐来源">{detailFearSourceLabel}</Descriptions.Item>
              <Descriptions.Item label="量比来源">{detailedResult.meta?.volume_signal_label || detailedResult.meta?.volume_signal_symbol || selectedVolumeSignalSymbol}</Descriptions.Item>
              <Descriptions.Item label="成交口径">{detailedResult.meta?.execution_price_label || '信号日收盘价'}</Descriptions.Item>
              <Descriptions.Item label="有效区间">{detailedResult.meta?.effective_start_date} ~ {detailedResult.meta?.effective_end_date}</Descriptions.Item>
              <Descriptions.Item label="交易日数">{detailedResult.meta?.trading_days}</Descriptions.Item>
              <Descriptions.Item label="初始资金">{detailedResult.meta?.initial_capital}</Descriptions.Item>
            </Descriptions>
          </Card>

          <Card title="回测资金曲线" style={{ marginBottom: 24 }} loading={detailLoading}>
            <ReactECharts option={equityOption} style={{ height: 360 }} />
          </Card>

          <Row gutter={16} style={{ marginBottom: 24 }}>
            <Col xs={24} lg={12}>
              <Card title="年度收益" loading={detailLoading} style={{ height: '100%' }}>
                <Table
                  dataSource={detailedResult.yearly_returns || []}
                  columns={yearlyReturnColumns}
                  rowKey={(record) => record.year}
                  pagination={false}
                  size="small"
                  scroll={{ x: 480 }}
                />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="回撤曲线对比" loading={detailLoading} style={{ height: '100%' }}>
                <ReactECharts option={drawdownOption} style={{ height: 320 }} />
              </Card>
            </Col>
          </Row>

          <Card title={`${selectedSymbol} K线 / 买卖点 / 成交量 / MA20`} style={{ marginBottom: 24 }} loading={detailLoading}>
            <ReactECharts option={priceVolumeOption} style={{ height: 680 }} />
          </Card>

          <Card title={isComparingFearSources ? '贪恐分数对比' : `${detailFearSourceLabel} 分数`} style={{ marginBottom: 24 }} loading={detailLoading}>
            <ReactECharts option={sentimentOption} style={{ height: 320 }} />
          </Card>

          <Card title="交易记录" loading={detailLoading}>
            <Table
              dataSource={detailedResult.trades || []}
              columns={tradeColumns}
              rowKey={(record, index) => `${record.date}-${record.action}-${index}`}
              pagination={{ defaultPageSize: 12 }}
              scroll={{ x: 1800 }}
            />
          </Card>
        </div>
      )}
    </div>
  );
};

export default SoxlFearBacktest;
