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

const symbolOptions = [
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

const fearSourceOptions = [
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
const getFearSourceLabel = (value) => fearSourceOptions.find(item => item.value === value)?.label || value;
const formatFearSourceLabels = (value) => {
  if (Array.isArray(value)) {
    return value.map(item => getFearSourceLabel(item)).join('、');
  }
  return getFearSourceLabel(value);
};

const SoxlFearBacktest = () => {
  const [form] = Form.useForm();
  const location = useLocation();
  const navigate = useNavigate();
  const selectedSymbol = Form.useWatch('symbol', form) || 'SOXL.US';
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
  const pollingTimerRef = useRef(null);
  const detailLoadingRef = useRef(false);
  const hasAutoRunRef = useRef(false);

  const buildPayload = (values) => {
    const sellPriceAboveAvgCostValues = parseBooleanList(values.sell_price_above_avg_cost_values);
    return {
      symbol: values.symbol || 'SOXL.US',
      fear_source_values: values.fear_source_values?.length ? values.fear_source_values : ['cnn'],
      initial_capital: values.initial_capital,
      start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
      end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
      top_n: values.top_n,
      objective: values.objective,
      eval_workers: values.eval_workers,
      rebalance_threshold_pct: values.fit_rebalance_threshold_pct,
      buy_threshold_values: parseNumberList(values.buy_threshold_values),
      greed_threshold_values: parseNumberList(values.greed_threshold_values),
      volume_ratio_threshold_values: parseNumberList(values.volume_ratio_threshold_values),
      buy_position_pct_values: parseNumberList(values.buy_position_pct_values),
      cooldown_days_values: parseNumberList(values.cooldown_days_values, true),
      trailing_stop_pct_values: parseNumberList(values.trailing_stop_pct_values),
      sell_position_pct_values: parseNumberList(values.sell_position_pct_values),
      sell_reduction_basis_values: values.sell_reduction_basis_values,
      sell_price_above_avg_cost_values: sellPriceAboveAvgCostValues.length ? sellPriceAboveAvgCostValues : [true, false],
      max_take_profit_sells_per_cycle_values: parseNumberList(values.max_take_profit_sells_per_cycle_values, true),
      min_position_pct_after_take_profit_values: parseNumberList(values.min_position_pct_after_take_profit_values),
    };
  };

  const buildParamsFromRecord = (record) => ({
    buy_threshold: record.buy_threshold,
    greed_threshold: record.greed_threshold,
    volume_ratio_threshold: record.volume_ratio_threshold,
    buy_position_pct: record.buy_position_pct,
    cooldown_days: record.cooldown_days,
    trailing_stop_pct: record.trailing_stop_pct,
    sell_position_pct: record.sell_position_pct,
    sell_reduction_basis: record.sell_reduction_basis,
    sell_price_above_avg_cost: record.sell_price_above_avg_cost,
    max_take_profit_sells_per_cycle: record.max_take_profit_sells_per_cycle,
    min_position_pct_after_take_profit: record.min_position_pct_after_take_profit,
    rebalance_threshold_pct: record.rebalance_threshold_pct,
  });

  const stopPolling = () => {
    if (pollingTimerRef.current) {
      clearTimeout(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  };

  const pollSearchJob = async (taskId) => {
    try {
      const { data } = await request.get(`/api/soxl-fear-backtest/search/jobs/${taskId}`, {
        timeout: 30 * 1000,
      });

      setSearchStatus(data.status);
      setSearchProgress(data.progress || 0);
      setSearchProgressText(data.message || '');
      setSearchProcessed(data.processed_combinations || 0);
      setSearchTotal(data.total_combinations || 0);

      if (data.status === 'completed') {
        stopPolling();
        setLoading(false);
        setSearchTaskId(null);
        setSearchMeta(data.result?.meta || null);
        setSearchResults(data.result?.results || []);
        setDetailedResult(data.result?.best_result || null);
        message.success(`搜索完成，共评估 ${data.result?.meta?.searched_combinations || 0} 组参数`);
        return;
      }

      if (data.status === 'failed') {
        stopPolling();
        setLoading(false);
        setSearchTaskId(null);
        message.error(data.error || '搜索失败');
        return;
      }

      pollingTimerRef.current = setTimeout(() => {
        pollSearchJob(taskId);
      }, 1000);
    } catch (error) {
      stopPolling();
      setLoading(false);
      setSearchTaskId(null);
      message.error(error.response?.data?.detail || '获取搜索进度失败');
    }
  };

  const handleSearch = async (values) => {
    stopPolling();
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
      const { data } = await request.post('/api/soxl-fear-backtest/search/jobs', payload, {
        timeout: 60 * 1000,
      });
      setSearchTaskId(data.task_id);
      setSearchTotal(data.total_combinations || 0);
      setSearchProgressText(`任务已创建，准备评估 ${data.total_combinations || 0} 组参数`);
      pollSearchJob(data.task_id);
    } catch (error) {
      setSearchStatus('failed');
      message.error(error.response?.data?.detail || '搜索失败');
      setLoading(false);
      setSearchTaskId(null);
    } finally {
    }
  };

  useEffect(() => () => {
    stopPolling();
  }, []);

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

    if (!mergedValues.date_range) {
      mergedValues.date_range = [dayjs('2021-01-01'), dayjs()];
    }

    form.setFieldsValue(mergedValues);
    setTimeout(() => {
      handleSearch(mergedValues);
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
        fear_source: record.fear_source || values.fear_source_values?.[0] || 'cnn',
        compare_fear_sources: values.fear_source_values?.length ? values.fear_source_values : [record.fear_source || 'cnn'],
        initial_capital: values.initial_capital,
        start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
        end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
        params: buildParamsFromRecord(record),
      };
      const { data } = await request.post('/api/soxl-fear-backtest/run', payload);
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
        <Tag color={fearSourceColorMap[record.fear_source] || 'blue'}>
          {value || getFearSourceLabel(record.fear_source)}
        </Tag>
      ),
      filters: fearSourceOptions.map(item => ({ text: item.label, value: item.value })),
      onFilter: (value, record) => record.fear_source === value,
    },
    { title: '买入阈值', dataIndex: 'buy_threshold', width: 90 },
    { title: '进入止盈区阈值(>=)', dataIndex: 'greed_threshold', width: 130 },
    { title: '量比阈值', dataIndex: 'volume_ratio_threshold', width: 90 },
    { title: '买入仓位%', dataIndex: 'buy_position_pct', width: 90 },
    { title: '冷却天数', dataIndex: 'cooldown_days', width: 90 },
    { title: '止盈回撤%', dataIndex: 'trailing_stop_pct', width: 100 },
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

    const buyMarkers = (detailedResult.trades || [])
      .filter(item => item.action === 'BUY')
      .map(item => ({
        name: '买',
        value: 'B',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#cf1322' },
      }));
    const sellMarkers = (detailedResult.trades || [])
      .filter(item => item.action === 'SELL')
      .map(item => ({
        name: '卖',
        value: 'S',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#1677ff' },
      }));

    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: [`${selectedSymbol} K线`, 'MA20', '成交量', '成交量MA20'] },
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
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: 10, start: 60, end: 100 },
      ],
      series: [
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
      ],
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
      <Card title={`${selectedSymbol} 情绪 + 量能 超参数回测`} style={{ marginBottom: 24 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="策略假设"
          description={`使用所选贪恐来源（${selectedFearSourceLabel}）；当贪恐分数低于等于买入触发阈值，且 ${selectedSymbol} 成交量 / 20日均量 放大时分批买入；当贪恐分数高于等于进入止盈区阈值后，若价格再从区内高点回撤，则按回撤规则移动止盈；均价保护开启时，卖出价必须高于当前持仓均价；止盈减仓口径可选按总资产或按持仓股票；同时不会把仓位卖穿最低保留仓位；同一轮止盈区可限制最多卖出次数；买卖后按交易日冷却 n 天。`}
        />
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSearch}
          initialValues={{
            symbol: 'SOXL.US',
            fear_source_values: ['cnn'],
            initial_capital: 100000,
            top_n: 20,
            objective: 'annualized_return',
            eval_workers: 4,
            fit_rebalance_threshold_pct: 5,
            date_range: [dayjs('2021-01-01'), dayjs()],
            buy_threshold_values: '35,40,45',
            greed_threshold_values: '40,41,42',
            volume_ratio_threshold_values: '1.3,1.38,1.45',
            buy_position_pct_values: '50,60,70',
            cooldown_days_values: '5,10,15',
            trailing_stop_pct_values: '3,5,7',
            sell_position_pct_values: '40,50,60',
            sell_reduction_basis_values: ['portfolio', 'holdings'],
            sell_price_above_avg_cost_values: ['true', 'false'],
            max_take_profit_sells_per_cycle_values: '1,2,3',
            min_position_pct_after_take_profit_values: '5,10,15',
          }}
        >
          <Row gutter={16}>
            <Col xs={24} md={4}>
              <Form.Item name="symbol" label="标的">
                <Select options={symbolOptions} />
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
                <Select mode="multiple" maxTagCount="responsive" options={fearSourceOptions} />
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
              <Form.Item name="trailing_stop_pct_values" label="移动止盈回撤% 候选">
                <Input placeholder="例如 6,10" />
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
            rowKey={(record) => `${record.fear_source}-${record.buy_threshold}-${record.greed_threshold}-${record.volume_ratio_threshold}-${record.buy_position_pct}-${record.cooldown_days}-${record.trailing_stop_pct}-${record.sell_position_pct}-${record.sell_reduction_basis}-${record.sell_price_above_avg_cost}-${record.max_take_profit_sells_per_cycle}-${record.min_position_pct_after_take_profit}`}
            pagination={{ pageSize: 10 }}
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
              <Descriptions.Item label="每次买入仓位%">{detailedResult.params?.buy_position_pct}</Descriptions.Item>
              <Descriptions.Item label="冷却天数">{detailedResult.params?.cooldown_days}</Descriptions.Item>
              <Descriptions.Item label="移动止盈回撤%">{detailedResult.params?.trailing_stop_pct}</Descriptions.Item>
              <Descriptions.Item label="止盈减仓%">{detailedResult.params?.sell_position_pct}</Descriptions.Item>
              <Descriptions.Item label="止盈减仓口径">{getSellReductionBasisLabel(detailedResult.params?.sell_reduction_basis)}</Descriptions.Item>
              <Descriptions.Item label="均价保护">{detailedResult.params?.sell_price_above_avg_cost ? '开启' : '关闭'}</Descriptions.Item>
              <Descriptions.Item label="同轮止盈最多卖出次数">{detailedResult.params?.max_take_profit_sells_per_cycle}</Descriptions.Item>
              <Descriptions.Item label="止盈后最低保留仓位%">{detailedResult.params?.min_position_pct_after_take_profit}</Descriptions.Item>
              <Descriptions.Item label="调仓阈值%">{detailedResult.params?.rebalance_threshold_pct}</Descriptions.Item>
              <Descriptions.Item label="贪恐来源">{detailFearSourceLabel}</Descriptions.Item>
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
              pagination={{ pageSize: 12 }}
              scroll={{ x: 1800 }}
            />
          </Card>
        </div>
      )}
    </div>
  );
};

export default SoxlFearBacktest;
