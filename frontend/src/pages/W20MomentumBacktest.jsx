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
  Table,
  Tag,
  message,
} from 'antd';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';

const { RangePicker } = DatePicker;

const formatPercent = (value, digits = 2) => `${Number(value || 0).toFixed(digits)}%`;
const formatNullablePercent = (value, digits = 2) => (
  value === null || value === undefined ? '-' : formatPercent(value, digits)
);
const formatNumber = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toFixed(digits)
);
const formatMoney = (value, digits = 2) => (
  value === null || value === undefined ? '-' : Number(value || 0).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
);
const formatWeights = (value) => (value || []).join(' / ');
const parseSemicolonItems = (value) => {
  if (Array.isArray(value)) {
    return value.map(item => String(item || '').trim()).filter(Boolean);
  }
  return String(value || '').split(/[;；\n]+/).map(item => item.trim()).filter(Boolean);
};
const parseNumberList = (value) => String(value || '')
  .split(/[,，]+/)
  .map(item => item.trim())
  .filter(Boolean)
  .map(item => Number(item));
const parseWeightCandidates = (value) => parseSemicolonItems(value)
  .map(item => parseNumberList(item))
  .filter(weights => weights.length > 0);
const parseNumericCandidates = (value) => {
  if (Array.isArray(value)) {
    return value.map(item => Number(item));
  }
  return String(value || '')
    .split(/[;；,，\n]+/)
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => Number(item));
};
const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) {
    return fallback;
  }
  if (typeof detail === 'string') {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map(item => {
        if (typeof item === 'string') {
          return item;
        }
        const field = Array.isArray(item.loc) ? item.loc.filter(part => part !== 'body').join('.') : '';
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .filter(Boolean)
      .join('；') || fallback;
  }
  if (typeof detail === 'object') {
    return detail.msg || detail.message || JSON.stringify(detail);
  }
  return String(detail);
};

const universeOptions = [
  { label: '纳指ETF 513100.SH', value: '513100.SH' },
  { label: 'A500ETF 563360.SH', value: '563360.SH' },
  { label: '沪深300ETF 510300.SH', value: '510300.SH' },
  { label: '创业板ETF 159915.SZ', value: '159915.SZ' },
  { label: '科创200ETF 588230.SH', value: '588230.SH' },
  { label: '中证500ETF 510500.SH', value: '510500.SH' },
  { label: '红利ETF 510880.SH', value: '510880.SH' },
  { label: '煤炭ETF 515220.SH', value: '515220.SH' },
  { label: '30年国债ETF 511090.SH', value: '511090.SH' },
  { label: '豆粕ETF 159985.SZ', value: '159985.SZ' },
  { label: '有色ETF 159980.SZ', value: '159980.SZ' },
  { label: '医疗ETF 512170.SH', value: '512170.SH' },
  { label: '军工ETF 512660.SH', value: '512660.SH' },
  { label: '酒ETF 512690.SH', value: '512690.SH' },
  { label: '银行ETF 512800.SH', value: '512800.SH' },
  { label: '证券ETF 512880.SH', value: '512880.SH' },
  { label: '日本ETF 513000.SH', value: '513000.SH' },
  { label: '德国ETF 513030.SH', value: '513030.SH' },
  { label: '香港创新药ETF 513120.SH', value: '513120.SH' },
  { label: '纳指生物科技ETF 513290.SH', value: '513290.SH' },
  { label: '标普500ETF 513500.SH', value: '513500.SH' },
  { label: '港股红利ETF 513630.SH', value: '513630.SH' },
  { label: '东南亚科技ETF 513730.SH', value: '513730.SH' },
  { label: '能源化工ETF 159981.SZ', value: '159981.SZ' },
  { label: '可转债ETF 511380.SH', value: '511380.SH' },
  { label: '恒生科技ETF 513130.SH', value: '513130.SH' },
  { label: '新能源车ETF 515030.SH', value: '515030.SH' },
  { label: '5G通信ETF 515050.SH', value: '515050.SH' },
  { label: '软件ETF 515230.SH', value: '515230.SH' },
  { label: '光伏ETF 515790.SH', value: '515790.SH' },
  { label: '黄金ETF 518880.SH', value: '518880.SH' },
  { label: '沙特ETF 520830.SH', value: '520830.SH' },
  { label: '巴西ETF 520870.SH', value: '520870.SH' },
  { label: '机器人ETF 562500.SH', value: '562500.SH' },
  { label: '科创板ETF 588000.SH', value: '588000.SH' },
  { label: '纳指科技ETF 159509.SZ', value: '159509.SZ' },
  { label: '标普消费ETF 159529.SZ', value: '159529.SZ' },
  { label: '亚太精选ETF 159687.SZ', value: '159687.SZ' },
  { label: '人工智能ETF 159819.SZ', value: '159819.SZ' },
  { label: '养殖ETF 159865.SZ', value: '159865.SZ' },
  { label: '游戏ETF 159869.SZ', value: '159869.SZ' },
  { label: '纳指100ETF 159941.SZ', value: '159941.SZ' },
  { label: '芯片ETF 159995.SZ', value: '159995.SZ' },
  { label: '印度ETF 164824.SZ', value: '164824.SZ' },
];

const defaultUniverseSymbols = [
  '513100.SH',
  '159915.SZ',
  '563360.SH',
  '588230.SH',
  '510500.SH',
  '510880.SH',
  '515220.SH',
  '518880.SH',
];

const benchmarkOptions = [
  { label: '沪深300ETF 510300.SH', value: '510300.SH' },
  { label: '中证500ETF 510500.SH', value: '510500.SH' },
  { label: '纳指ETF 513100.SH', value: '513100.SH' },
];

const symbolLabelMap = [...universeOptions, ...benchmarkOptions].reduce((acc, item) => {
  acc[item.value] = item.label;
  return acc;
}, {});
const formatSymbolLabel = (value) => symbolLabelMap[value] || value || '-';
const formatTextWithSymbolLabels = (value) => {
  if (!value) {
    return '-';
  }
  return Object.entries(symbolLabelMap).reduce((text, [symbol, label]) => (
    text.replaceAll(symbol, label)
  ), String(value));
};

const rebalanceFrequencyOptions = [
  { label: '每日', value: 'daily' },
  { label: '每周', value: 'weekly' },
  { label: '每月', value: 'monthly' },
];
const getRebalanceFrequencyLabel = (value) => (
  rebalanceFrequencyOptions.find(item => item.value === value)?.label || value
);
const tradeReasonMeta = {
  initial_entry: { label: '首次建仓', color: 'purple' },
  basket_symbols_changed: { label: '标的变化', color: 'orange' },
  rank_order_changed: { label: '排名变化', color: 'gold' },
  target_weights_changed: { label: '权重变化', color: 'cyan' },
  drift_threshold: { label: '阈值触发', color: 'volcano' },
  basket_change: { label: '篮子变化', color: 'orange' },
  drift: { label: '权重漂移', color: 'volcano' },
  target_refresh: { label: '目标刷新', color: 'default' },
};
const getTradeReasonMeta = (value) => (
  tradeReasonMeta[value] || { label: value || '-', color: 'default' }
);

const W20MomentumBacktest = () => {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [taskId, setTaskId] = useState(null);
  const [completedTaskId, setCompletedTaskId] = useState(null);
  const [status, setStatus] = useState(null);
  const [progressText, setProgressText] = useState('');
  const [processed, setProcessed] = useState(0);
  const [total, setTotal] = useState(0);
  const [batchResult, setBatchResult] = useState(null);
  const [result, setResult] = useState(null);
  const [selectedResultId, setSelectedResultId] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [formError, setFormError] = useState(null);
  const pollingTimerRef = useRef(null);

  useEffect(() => () => {
    if (pollingTimerRef.current) {
      clearTimeout(pollingTimerRef.current);
    }
  }, []);

  const stopPolling = () => {
    if (pollingTimerRef.current) {
      clearTimeout(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  };

  const pollJob = async (id) => {
    try {
      const { data } = await request.get(`/api/w20-momentum-backtest/jobs/${id}`, { timeout: 30000 });
      setStatus(data.status);
      setProgress(data.progress || 0);
      setProgressText(data.message || '');
      setProcessed(data.processed_combinations || 0);
      setTotal(data.total_combinations || 0);

      if (data.status === 'completed') {
        stopPolling();
        setLoading(false);
        setCompletedTaskId(id);
        setTaskId(id);
        setBatchResult(data.result);
        setResult(data.result?.best_result || null);
        setSelectedResultId(data.result?.results?.[0]?.result_id || null);
        message.success(`W20 批量回测完成，返回前 ${data.result?.results?.length || 0} 组`);
        return;
      }

      if (data.status === 'failed') {
        stopPolling();
        setLoading(false);
        setTaskId(null);
        setFormError(data.error || 'W20 动量回测失败');
        message.error(data.error || 'W20 动量回测失败');
        return;
      }

      pollingTimerRef.current = setTimeout(() => pollJob(id), 1200);
    } catch (error) {
      stopPolling();
      setLoading(false);
      setTaskId(null);
      const errorText = formatErrorMessage(error, '获取回测进度失败');
      setFormError(errorText);
      message.error(errorText);
    }
  };

  const onFinish = async (values) => {
    stopPolling();
    setLoading(true);
    setProgress(0);
    setStatus('pending');
    setProgressText('正在创建批量回测任务');
    setProcessed(0);
    setTotal(0);
    setCompletedTaskId(null);
    setBatchResult(null);
    setResult(null);
    setSelectedResultId(null);
    setFormError(null);

    try {
      const parsedWindows = parseNumericCandidates(values.window_values);
      if (!parsedWindows.length) {
        throw new Error('回归窗口候选不能为空');
      }
      if (parsedWindows.some(item => Number.isNaN(item))) {
        throw new Error('回归窗口候选包含无法识别的数字，例如 10;20;60 或 10,20,60');
      }
      if (parsedWindows.some(item => !Number.isInteger(item))) {
        throw new Error('回归窗口候选必须是整数，例如 10;20;60 或 10,20,60');
      }
      if (parsedWindows.some(item => item < 2)) {
        throw new Error('回归窗口不能小于 2');
      }

      const parsedDriftThresholds = parseNumericCandidates(values.drift_threshold_pct_values);
      if (!parsedDriftThresholds.length) {
        throw new Error('绝对漂移阈值候选不能为空');
      }
      if (parsedDriftThresholds.some(item => Number.isNaN(item))) {
        throw new Error('绝对漂移阈值候选包含无法识别的数字，例如 5;100 或 5,100');
      }
      if (parsedDriftThresholds.some(item => item < 0)) {
        throw new Error('绝对漂移阈值不能为负数');
      }

      const parsedTopWeights = parseWeightCandidates(values.top_weights_values);
      const symbolCount = values.symbols?.length || 0;
      if (!parsedTopWeights.length) {
        throw new Error('前N权重候选不能为空');
      }
      const invalidNumberWeights = parsedTopWeights.find(weights => weights.some(weight => Number.isNaN(weight)));
      if (invalidNumberWeights) {
        throw new Error(`权重候选 ${invalidNumberWeights.join(',')} 包含无法识别的数字`);
      }
      const oversizedWeights = parsedTopWeights.find(weights => weights.length > symbolCount);
      if (oversizedWeights) {
        throw new Error(`权重候选 ${oversizedWeights.join(',')} 表示 Top${oversizedWeights.length}，但当前策略标的池只有 ${symbolCount} 个标的`);
      }
      const nonPositiveWeights = parsedTopWeights.find(weights => weights.reduce((sum, weight) => sum + weight, 0) <= 0);
      if (nonPositiveWeights) {
        throw new Error(`权重候选 ${nonPositiveWeights.join(',')} 的和必须大于 0`);
      }

      const payload = {
        symbols: values.symbols,
        benchmark_symbols: values.benchmark_symbols,
        initial_capital: values.initial_capital,
        start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
        end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
        window_values: parsedWindows,
        top_weights_values: parsedTopWeights,
        rebalance_frequency_values: values.rebalance_frequency_values,
        drift_threshold_pct_values: parsedDriftThresholds,
        commission_pct: values.commission_pct,
        slippage_pct: values.slippage_pct,
        lot_size: values.lot_size,
        eval_workers: values.eval_workers,
      };

      const { data } = await request.post('/api/w20-momentum-backtest/batch/start', payload, { timeout: 60000 });
      setTaskId(data.task_id);
      setStatus(data.status);
      setProgress(1);
      setTotal(data.total_combinations || 0);
      pollJob(data.task_id);
    } catch (error) {
      stopPolling();
      setLoading(false);
      setTaskId(null);
      setStatus('failed');
      const errorText = formatErrorMessage(error, '启动回测失败');
      setFormError(errorText);
      message.error(errorText);
    }
  };

  const loadDetail = async (record) => {
    const id = completedTaskId || taskId;
    if (!id || !record?.result_id) {
      return;
    }
    setSelectedResultId(record.result_id);
    setDetailLoading(true);
    try {
      const { data } = await request.get(`/api/w20-momentum-backtest/jobs/${id}/results/${record.result_id}`, {
        timeout: 30000,
      });
      setResult(data);
      setTimeout(() => {
        document.getElementById('w20-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 80);
    } catch (error) {
      message.error(formatErrorMessage(error, '获取组合详情失败'));
    } finally {
      setDetailLoading(false);
    }
  };

  const equityOption = useMemo(() => {
    if (!result?.equity_curve?.length) {
      return {};
    }
    const dates = result.equity_curve.map(item => item.date);
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略净值', '动态等权基准'] },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside', start: 60, end: 100 }, { type: 'slider', start: 60, end: 100 }],
      series: [
        {
          name: '策略净值',
          type: 'line',
          data: result.equity_curve.map(item => item.value),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#cf1322' },
          areaStyle: { opacity: 0.12, color: '#cf1322' },
        },
        {
          name: '动态等权基准',
          type: 'line',
          data: result.equity_curve.map(item => item.benchmark_value),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#1677ff' },
        },
      ],
    };
  }, [result]);

  const drawdownOption = useMemo(() => {
    if (!result?.equity_curve?.length) {
      return {};
    }
    const dates = result.equity_curve.map(item => item.date);
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略回撤', '动态等权回撤'] },
      xAxis: { type: 'category', data: dates },
      yAxis: {
        type: 'value',
        max: 0,
        axisLabel: { formatter: '{value}%' },
      },
      dataZoom: [{ type: 'inside', start: 60, end: 100 }, { type: 'slider', start: 60, end: 100 }],
      series: [
        {
          name: '策略回撤',
          type: 'line',
          data: result.equity_curve.map(item => item.drawdown),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#cf1322' },
          areaStyle: { opacity: 0.12, color: '#cf1322' },
        },
        {
          name: '动态等权回撤',
          type: 'line',
          data: result.equity_curve.map(item => item.benchmark_drawdown),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#1677ff' },
        },
      ],
    };
  }, [result]);

  const rankingColumns = [
    { title: '排名', dataIndex: 'rank', width: 70 },
    {
      title: '标的',
      dataIndex: 'symbol',
      width: 180,
      render: value => <Tag color="blue">{formatSymbolLabel(value)}</Tag>,
    },
    {
      title: '风险调整分数',
      dataIndex: 'risk_adjusted_score',
      width: 120,
      render: value => <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>{formatNumber(value)}</span>,
      sorter: (a, b) => Number(a.risk_adjusted_score || 0) - Number(b.risk_adjusted_score || 0),
    },
    { title: '原始分数', dataIndex: 'raw_score', width: 110, render: value => formatNumber(value) },
    { title: '20日收益', dataIndex: 'window_return_pct', width: 100, render: value => formatPercent(value) },
    { title: '年化斜率', dataIndex: 'annualized_slope_pct', width: 110, render: value => formatPercent(value) },
    { title: '年化波动', dataIndex: 'annualized_volatility_pct', width: 110, render: value => formatPercent(value) },
    { title: 'R²', dataIndex: 'r_squared', width: 90, render: value => formatNumber(value, 4) },
  ];

  const holdingsColumns = [
    { title: '标的', dataIndex: 'symbol', width: 180, render: value => <Tag color="green">{formatSymbolLabel(value)}</Tag> },
    { title: '股数', dataIndex: 'shares', width: 90, render: value => Number(value || 0).toLocaleString() },
    { title: '价格', dataIndex: 'price', width: 90, render: value => formatNumber(value) },
    { title: '市值', dataIndex: 'market_value', width: 120, render: value => formatNumber(value) },
    { title: '实际权重', dataIndex: 'actual_weight_pct', width: 110, render: value => formatPercent(value) },
    { title: '目标权重', dataIndex: 'target_weight_pct', width: 110, render: value => formatPercent(value) },
  ];

  const benchmarkColumns = [
    { title: '基准', dataIndex: 'symbol', width: 180, render: value => <Tag color="blue">{formatSymbolLabel(value)}</Tag> },
    { title: '起始日期', dataIndex: 'effective_start_date', width: 120 },
    { title: '交易日数', dataIndex: 'trading_days', width: 90 },
    { title: '累计收益', dataIndex: 'total_return', width: 110, render: value => formatPercent(value) },
    { title: '年化收益', dataIndex: 'annualized_return', width: 110, render: value => formatPercent(value) },
    { title: '年化波动', dataIndex: 'annualized_volatility', width: 110, render: value => formatPercent(value) },
    { title: '最大回撤', dataIndex: 'max_drawdown', width: 110, render: value => formatPercent(value) },
    { title: 'Sharpe', dataIndex: 'sharpe_ratio', width: 90, render: value => formatNumber(value, 3), sorter: (a, b) => Number(a.sharpe_ratio || 0) - Number(b.sharpe_ratio || 0) },
    { title: 'Calmar', dataIndex: 'calmar_ratio', width: 90, render: value => formatNumber(value, 3) },
  ];

  const tradeColumns = [
    { title: '日期', dataIndex: 'date', width: 110 },
    { title: '信号日', dataIndex: 'signal_date', width: 110, render: value => value || '-' },
    { title: '方向', dataIndex: 'action', width: 80, render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag> },
    { title: '标的', dataIndex: 'symbol', width: 180, render: value => formatSymbolLabel(value) },
    { title: '价格', dataIndex: 'price', width: 100, render: value => formatNumber(value, 4) },
    { title: '数量', dataIndex: 'quantity', width: 100, render: value => Number(value || 0).toLocaleString() },
    { title: '金额', dataIndex: 'amount', width: 110, render: value => formatNumber(value) },
    { title: '佣金', dataIndex: 'commission', width: 100, render: value => formatNumber(value) },
    { title: '交易后总资产', dataIndex: 'portfolio_value_after', width: 130, render: value => formatNumber(value) },
    { title: '标的市值', dataIndex: 'symbol_market_value_after', width: 120, render: value => formatNumber(value) },
    { title: '标的仓位', dataIndex: 'symbol_weight_pct_after', width: 110, render: value => formatPercent(value) },
    {
      title: '触发原因',
      dataIndex: 'reason',
      width: 120,
      render: value => {
        const meta = getTradeReasonMeta(value);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: '目标仓位',
      width: 220,
      render: (_, record) => {
        const symbols = record.target_symbols || [];
        const weights = record.target_weights_pct || [];
        if (!symbols.length) {
          return '-';
        }
        return symbols.map((symbol, index) => (
          <Tag key={`${symbol}-${index}`} color="blue">{formatSymbolLabel(symbol)} {formatPercent(weights[index])}</Tag>
        ));
      },
    },
    {
      title: '原因详情',
      dataIndex: 'reason_detail',
      width: 420,
      render: value => <span style={{ whiteSpace: 'normal' }}>{formatTextWithSymbolLabels(value)}</span>,
    },
  ];

  const symbolTradeStatsColumns = [
    { title: '标的', dataIndex: 'symbol', width: 180, render: value => <Tag color="blue">{formatSymbolLabel(value)}</Tag> },
    { title: '有效区间', width: 210, render: (_, record) => `${record.effective_start_date || '-'} ~ ${record.effective_end_date || '-'}` },
    { title: '有效交易日', dataIndex: 'trading_days', width: 100, sorter: (a, b) => Number(a.trading_days || 0) - Number(b.trading_days || 0) },
    {
      title: '盈利金额',
      dataIndex: 'profit_amount',
      width: 120,
      render: value => <span style={{ color: Number(value || 0) >= 0 ? '#3f8600' : '#cf1322' }}>{formatMoney(value, 2)}</span>,
      sorter: (a, b) => Number(a.profit_amount || 0) - Number(b.profit_amount || 0),
    },
    { title: '买入次数', dataIndex: 'buy_count', width: 100, sorter: (a, b) => Number(a.buy_count || 0) - Number(b.buy_count || 0) },
    { title: '卖出次数', dataIndex: 'sell_count', width: 100, sorter: (a, b) => Number(a.sell_count || 0) - Number(b.sell_count || 0) },
    { title: '合计次数', dataIndex: 'trade_count', width: 100, sorter: (a, b) => Number(a.trade_count || 0) - Number(b.trade_count || 0), defaultSortOrder: 'descend' },
  ];

  const batchColumns = [
    { title: '排名', dataIndex: 'rank', width: 70, fixed: 'left' },
    { title: '窗口', dataIndex: 'window', width: 80 },
    { title: 'Top N', dataIndex: 'top_n', width: 80 },
    {
      title: '权重',
      dataIndex: 'top_weights_pct',
      width: 100,
      render: value => `${formatWeights(value)}%`,
    },
    {
      title: '排名/调仓',
      dataIndex: 'rebalance_frequency',
      width: 90,
      render: value => getRebalanceFrequencyLabel(value),
    },
    { title: '绝对阈值', dataIndex: 'drift_threshold_pct', width: 100, render: value => formatPercent(value) },
    { title: '累计收益', dataIndex: 'total_return', width: 110, render: value => formatPercent(value), sorter: (a, b) => Number(a.total_return || 0) - Number(b.total_return || 0), defaultSortOrder: 'descend' },
    { title: '年化收益', dataIndex: 'annualized_return', width: 110, render: value => formatPercent(value) },
    { title: '最大回撤', dataIndex: 'max_drawdown', width: 110, render: value => formatPercent(value) },
    { title: 'Sharpe', dataIndex: 'sharpe_ratio', width: 90, render: value => formatNumber(value, 3), sorter: (a, b) => Number(a.sharpe_ratio || 0) - Number(b.sharpe_ratio || 0) },
    { title: 'Calmar', dataIndex: 'calmar_ratio', width: 90, render: value => formatNumber(value, 3) },
    { title: '交易笔数', dataIndex: 'trade_count', width: 100 },
    { title: '有效区间', width: 210, render: (_, record) => `${record.effective_start_date || '-'} ~ ${record.effective_end_date || '-'}` },
    {
      title: '当前信号',
      dataIndex: 'selected_symbols',
      width: 180,
      render: value => (value || []).map(symbol => <Tag key={symbol} color="green">{formatSymbolLabel(symbol)}</Tag>),
    },
    {
      title: '操作',
      width: 100,
      fixed: 'right',
      render: (_, record) => (
        <Button
          type={record.result_id === selectedResultId ? 'primary' : 'link'}
          size="small"
          loading={detailLoading && record.result_id === selectedResultId}
          onClick={() => loadDetail(record)}
        >
          查看详情
        </Button>
      ),
    },
  ];

  const coreMetricRows = useMemo(() => {
    if (!result?.metrics) {
      return [];
    }
    const strategy = {
      key: 'strategy',
      name: '策略',
      type: '策略',
      effective_start_date: result.meta?.effective_start_date,
      effective_end_date: result.meta?.effective_end_date,
      total_return: result.metrics.total_return,
      annualized_return: result.metrics.annualized_return,
      annualized_volatility: result.metrics.annualized_volatility,
      max_drawdown: result.metrics.max_drawdown,
      sharpe_ratio: result.metrics.sharpe_ratio,
      calmar_ratio: result.metrics.calmar_ratio,
      trade_count: result.metrics.trade_count,
      excess_vs_strategy: 0,
    };
    const equalWeight = {
      key: 'equal_weight',
      name: '动态等权基准',
      type: '基准',
      effective_start_date: result.meta?.effective_start_date,
      effective_end_date: result.meta?.effective_end_date,
      total_return: result.metrics.equal_weight_total_return,
      annualized_return: result.metrics.equal_weight_annualized_return,
      annualized_volatility: result.metrics.equal_weight_annualized_volatility,
      max_drawdown: result.metrics.equal_weight_max_drawdown,
      sharpe_ratio: result.metrics.equal_weight_sharpe_ratio,
      calmar_ratio: result.metrics.equal_weight_calmar_ratio,
      trade_count: result.metrics.equal_weight_trade_count || 0,
      excess_vs_strategy: Number(result.metrics.total_return || 0) - Number(result.metrics.equal_weight_total_return || 0),
    };
    const benchmarkRows = (result.benchmark_metrics || []).map(item => ({
      key: item.symbol,
      name: formatSymbolLabel(item.symbol),
      type: '买入持有',
      effective_start_date: item.effective_start_date,
      effective_end_date: item.effective_end_date,
      total_return: item.total_return,
      annualized_return: item.annualized_return,
      annualized_volatility: item.annualized_volatility,
      max_drawdown: item.max_drawdown,
      sharpe_ratio: item.sharpe_ratio,
      calmar_ratio: item.calmar_ratio,
      trade_count: 0,
      excess_vs_strategy: Number(result.metrics.total_return || 0) - Number(item.total_return || 0),
    }));
    return [strategy, equalWeight, ...benchmarkRows];
  }, [result]);

  const coreMetricColumns = [
    {
      title: '对象',
      dataIndex: 'name',
      width: 130,
      fixed: 'left',
      render: (value, record) => <Tag color={record.type === '策略' ? 'red' : 'blue'}>{value}</Tag>,
    },
    { title: '类型', dataIndex: 'type', width: 90 },
    { title: '有效区间', width: 210, render: (_, record) => `${record.effective_start_date || '-'} ~ ${record.effective_end_date || '-'}` },
    { title: '累计收益', dataIndex: 'total_return', width: 110, render: value => formatPercent(value), sorter: (a, b) => Number(a.total_return || 0) - Number(b.total_return || 0) },
    { title: '年化收益', dataIndex: 'annualized_return', width: 110, render: value => formatPercent(value), sorter: (a, b) => Number(a.annualized_return || 0) - Number(b.annualized_return || 0) },
    { title: '年化波动', dataIndex: 'annualized_volatility', width: 110, render: value => formatPercent(value), sorter: (a, b) => Number(a.annualized_volatility || 0) - Number(b.annualized_volatility || 0) },
    { title: '最大回撤', dataIndex: 'max_drawdown', width: 110, render: value => formatPercent(value), sorter: (a, b) => Number(a.max_drawdown || 0) - Number(b.max_drawdown || 0) },
    { title: 'Sharpe', dataIndex: 'sharpe_ratio', width: 90, render: value => formatNumber(value, 3), sorter: (a, b) => Number(a.sharpe_ratio || 0) - Number(b.sharpe_ratio || 0) },
    { title: 'Calmar', dataIndex: 'calmar_ratio', width: 90, render: value => formatNumber(value, 3), sorter: (a, b) => Number(a.calmar_ratio || 0) - Number(b.calmar_ratio || 0) },
    { title: '交易笔数', dataIndex: 'trade_count', width: 100, render: value => Number(value || 0).toLocaleString(), sorter: (a, b) => Number(a.trade_count || 0) - Number(b.trade_count || 0) },
    { title: '策略超额', dataIndex: 'excess_vs_strategy', width: 110, render: value => formatPercent(value), sorter: (a, b) => Number(a.excess_vs_strategy || 0) - Number(b.excess_vs_strategy || 0) },
  ];

  const annualPerformanceColumns = useMemo(() => {
    const benchmarkSymbols = result?.benchmark_metrics?.map(item => item.symbol) || [];
    return [
      { title: '年份', dataIndex: 'year', width: 90, fixed: 'left', sorter: (a, b) => Number(a.year || 0) - Number(b.year || 0) },
      {
        title: '策略',
        dataIndex: 'strategy_return',
        width: 110,
        render: value => formatNullablePercent(value),
        sorter: (a, b) => Number(a.strategy_return || 0) - Number(b.strategy_return || 0),
      },
      {
        title: '动态等权基准',
        dataIndex: 'equal_weight_return',
        width: 140,
        render: value => formatNullablePercent(value),
        sorter: (a, b) => Number(a.equal_weight_return || 0) - Number(b.equal_weight_return || 0),
      },
      {
        title: '超额等权',
        dataIndex: 'excess_equal_weight_return',
        width: 110,
        render: value => formatNullablePercent(value),
        sorter: (a, b) => Number(a.excess_equal_weight_return || 0) - Number(b.excess_equal_weight_return || 0),
      },
      ...benchmarkSymbols.map(symbol => ({
        title: formatSymbolLabel(symbol),
        width: 160,
        render: (_, record) => formatNullablePercent(record.benchmark_returns?.[symbol]),
        sorter: (a, b) => Number(a.benchmark_returns?.[symbol] || 0) - Number(b.benchmark_returns?.[symbol] || 0),
      })),
    ];
  }, [result]);

  return (
    <div style={{ padding: 24 }}>
      <Card title="W20 风险调整 ETF 动量回测" style={{ marginBottom: 24 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="策略假设"
          description="在排名/调仓日用候选回归窗口做 log 线性回归，斜率年化后乘 R² 得到原始动量分数，再除以年化波动得到风险调整分数；每组权重长度决定 Top N，例如 70,20,10 表示取前三；漂移阈值按实际权重与目标权重的绝对差计算，next_open 成交。"
        />
        <Form
          form={form}
          layout="vertical"
          onFinish={onFinish}
          initialValues={{
            symbols: defaultUniverseSymbols,
            benchmark_symbols: benchmarkOptions.map(item => item.value),
            initial_capital: 1000000,
            date_range: [dayjs('2018-01-02'), dayjs()],
            window_values: '20',
            top_weights_values: '70,30',
            rebalance_frequency_values: ['weekly'],
            drift_threshold_pct_values: '100',
            commission_pct: 0.03,
            slippage_pct: 0.02,
            lot_size: 100,
            eval_workers: undefined,
          }}
        >
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="date_range" label="回测区间" rules={[{ required: true, message: '请选择回测区间' }]}>
                <RangePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="initial_capital" label="初始资金">
                <InputNumber style={{ width: '100%' }} min={1000} step={10000} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="window_values" label="回归窗口候选">
                <Input placeholder="例如 20;30;40 或 20,30,40" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="top_weights_values" label="前N权重候选(%)">
                <Input placeholder="例如 70,20,10;80,20;100" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="rebalance_frequency_values" label="排名/调仓频率候选">
                <Select
                  mode="multiple"
                  options={rebalanceFrequencyOptions}
                  style={{ width: '100%' }}
                  placeholder="请选择一个或多个频率"
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="drift_threshold_pct_values" label="绝对漂移阈值候选(%)">
                <Input placeholder="例如 100;10;5 或 5,100" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="commission_pct" label="佣金(%)">
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="slippage_pct" label="滑点(%)">
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="lot_size" label="最小交易单位">
                <InputNumber style={{ width: '100%' }} min={1} step={1} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="eval_workers" label="并发进程数">
                <InputNumber style={{ width: '100%' }} min={1} max={16} step={1} placeholder="自动" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="symbols" label="策略标的池">
                <Select mode="tags" options={universeOptions} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="benchmark_symbols" label="基准标的">
                <Select mode="tags" options={benchmarkOptions} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Space>
            <Button type="primary" htmlType="submit" loading={loading}>
              开始批量回测
            </Button>
            {taskId && <Tag color="blue">任务 {taskId.slice(0, 8)}</Tag>}
          </Space>
        </Form>
        {formError && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 16 }}
            message="参数或回测错误"
            description={formError}
          />
        )}
      </Card>

      {loading && (
        <Card title="回测进度" style={{ marginBottom: 24 }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Progress percent={progress} status={status === 'failed' ? 'exception' : 'active'} />
            <Alert
              type="info"
              showIcon
              message={status || 'pending'}
              description={
                <Space direction="vertical" size={4}>
                  <span>{progressText || (taskId ? `任务ID: ${taskId}` : '正在启动任务')}</span>
                  {total > 0 && <span>组合进度：{processed} / {total}</span>}
                </Space>
              }
            />
          </Space>
        </Card>
      )}

      {batchResult && (
        <Card title="批量回测结果（按累计收益排序，最多前 200 组）" style={{ marginBottom: 24 }}>
          <Descriptions column={{ xs: 1, md: 2, lg: 4 }} bordered size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="请求区间">{batchResult.meta?.requested_start_date} ~ {batchResult.meta?.requested_end_date}</Descriptions.Item>
            <Descriptions.Item label="组合数">{batchResult.meta?.total_combinations}</Descriptions.Item>
            <Descriptions.Item label="成功组合">{batchResult.meta?.evaluated_combinations}</Descriptions.Item>
            <Descriptions.Item label="失败组合">{batchResult.meta?.failed_combinations}</Descriptions.Item>
            <Descriptions.Item label="并发进程">{batchResult.meta?.eval_workers || '-'}</Descriptions.Item>
            <Descriptions.Item label="回归窗口">{(batchResult.meta?.window_values || []).join(' / ')}</Descriptions.Item>
            <Descriptions.Item label="权重候选">{(batchResult.meta?.top_weights_values || []).map(formatWeights).join('；')}</Descriptions.Item>
            <Descriptions.Item label="排名/调仓频率">{(batchResult.meta?.rebalance_frequency_values || []).map(getRebalanceFrequencyLabel).join(' / ')}</Descriptions.Item>
            <Descriptions.Item label="绝对阈值候选">{(batchResult.meta?.drift_threshold_pct_values || []).map(value => `${value}%`).join(' / ')}</Descriptions.Item>
          </Descriptions>
          <Table
            dataSource={batchResult.results || []}
            columns={batchColumns}
            rowKey={(record) => record.result_id}
            pagination={{ pageSize: 20, showSizeChanger: true }}
            scroll={{ x: 1550 }}
            size="small"
          />
          {!!batchResult.errors?.length && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 16 }}
              message={`有 ${batchResult.meta?.failed_combinations || batchResult.errors.length} 组参数回测失败，已展示前 ${batchResult.errors.length} 条错误`}
            />
          )}
        </Card>
      )}

      {result && (
        <div id="w20-detail">
          <Card title="核心指标" style={{ marginBottom: 24 }}>
            <Table
              dataSource={coreMetricRows}
              columns={coreMetricColumns}
              rowKey={(record) => record.key}
              pagination={false}
              size="small"
              scroll={{ x: 1360 }}
            />
          </Card>

          <Card title="分年表现对比" style={{ marginBottom: 24 }}>
            <Table
              dataSource={result.annual_performance || []}
              columns={annualPerformanceColumns}
              rowKey={(record) => record.year}
              pagination={false}
              size="small"
              scroll={{ x: 650 + (result.benchmark_metrics?.length || 0) * 160 }}
            />
          </Card>

          <Card title="参数摘要" style={{ marginBottom: 24 }}>
            <Descriptions column={{ xs: 1, md: 2, lg: 4 }} bordered size="small">
              <Descriptions.Item label="请求区间">{result.meta?.requested_start_date} ~ {result.meta?.requested_end_date}</Descriptions.Item>
              <Descriptions.Item label="有效区间">{result.meta?.effective_start_date} ~ {result.meta?.effective_end_date}</Descriptions.Item>
              <Descriptions.Item label="交易日数">{result.meta?.trading_days}</Descriptions.Item>
              <Descriptions.Item label="信号日数">{result.meta?.signal_days}</Descriptions.Item>
              <Descriptions.Item label="排名/调仓频率">{getRebalanceFrequencyLabel(result.meta?.rebalance_frequency)}</Descriptions.Item>
              <Descriptions.Item label="绝对漂移阈值">{formatPercent(result.meta?.drift_threshold_pct)}</Descriptions.Item>
              <Descriptions.Item label="回归窗口">{result.meta?.window}</Descriptions.Item>
              <Descriptions.Item label="最小交易单位">{result.meta?.lot_size}</Descriptions.Item>
              <Descriptions.Item label="佣金">{formatPercent(result.meta?.commission_pct)}</Descriptions.Item>
              <Descriptions.Item label="滑点">{formatPercent(result.meta?.slippage_pct)}</Descriptions.Item>
              <Descriptions.Item label="Top N">{result.meta?.top_n}</Descriptions.Item>
              <Descriptions.Item label="目标权重">{(result.meta?.top_weights_pct || []).join(' / ')}%</Descriptions.Item>
              <Descriptions.Item label="基准标的">{(result.meta?.benchmark_symbols || []).map(formatSymbolLabel).join('、')}</Descriptions.Item>
            </Descriptions>
          </Card>

          <Row gutter={16} style={{ marginBottom: 24 }}>
            <Col xs={24} lg={12}>
              <Card title="净值曲线" style={{ height: '100%' }}>
                <ReactECharts option={equityOption} style={{ height: 360 }} />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="回撤曲线" style={{ height: '100%' }}>
                <ReactECharts option={drawdownOption} style={{ height: 360 }} />
              </Card>
            </Col>
          </Row>

          <Card title="最新风险调整排名" style={{ marginBottom: 24 }}>
            <Table
              dataSource={result.latest_signal?.ranking || []}
              columns={rankingColumns}
              rowKey={(record) => `${record.symbol}-${record.rank}`}
              pagination={{ pageSize: 8 }}
              scroll={{ x: 1000 }}
            />
          </Card>

          <Row gutter={16} style={{ marginBottom: 24 }}>
            <Col xs={24} lg={12}>
              <Card title="当前信号与持仓" style={{ height: '100%' }}>
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  <Descriptions column={1} bordered size="small">
                    <Descriptions.Item label="信号日期">{result.latest_signal?.date || '-'}</Descriptions.Item>
                    <Descriptions.Item label="选中标的">
                      {(result.latest_signal?.selected_symbols || []).map(symbol => (
                        <Tag key={symbol} color="green">{formatSymbolLabel(symbol)}</Tag>
                      ))}
                    </Descriptions.Item>
                    <Descriptions.Item label="目标权重">
                      {(result.latest_signal?.target_weights_pct || []).map((weight, index) => (
                        <Tag key={`${weight}-${index}`}>{weight}%</Tag>
                      ))}
                    </Descriptions.Item>
                    <Descriptions.Item label="现金">{formatNumber(result.cash)}</Descriptions.Item>
                    <Descriptions.Item label="总资产">{formatNumber(result.portfolio_value)}</Descriptions.Item>
                  </Descriptions>
                  <Table
                    dataSource={result.current_holdings || []}
                    columns={holdingsColumns}
                    rowKey={(record) => record.symbol}
                    pagination={false}
                    size="small"
                  />
                </Space>
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="基准表现" style={{ height: '100%' }}>
                <Table
                  dataSource={result.benchmark_metrics || []}
                  columns={benchmarkColumns}
                  rowKey={(record) => record.symbol}
                  pagination={false}
                  size="small"
                  scroll={{ x: 1000 }}
                />
              </Card>
            </Col>
          </Row>

          <Card title="标的表现" style={{ marginBottom: 24 }}>
            <Table
              dataSource={result.symbol_trade_stats || []}
              columns={symbolTradeStatsColumns}
              rowKey={(record) => record.symbol}
              pagination={false}
              size="small"
            />
          </Card>

          <Card title="交易明细" style={{ marginBottom: 24 }}>
            <Table
              dataSource={result.trades || []}
              columns={tradeColumns}
              rowKey={(record, index) => `${record.date}-${record.symbol}-${record.action}-${index}`}
              pagination={{ pageSize: 12 }}
              scroll={{ x: 2000 }}
              size="small"
            />
          </Card>
        </div>
      )}
    </div>
  );
};

export default W20MomentumBacktest;
