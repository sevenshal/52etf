import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Form,
  InputNumber,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  TimePicker,
  Tag,
  message,
} from 'antd';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import { ExperimentOutlined } from '@ant-design/icons';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';

const sellReductionBasisOptions = [
  { label: '按总资产', value: 'portfolio' },
  { label: '按持仓股票', value: 'holdings' },
];

// run_time 可能是 dayjs（表单）或字符串（后端），统一格式化，避免把 dayjs 对象直接渲染成 React child
const formatRunTime = (value) => {
  if (!value) return '09:30';
  if (dayjs.isDayjs(value)) return value.format('HH:mm');
  return String(value);
};

// 默认参数 = 回测那套：极恐<30 + 20日量比≥1.3 买100%，极贪>70 卖100%，移动止盈=0（贪恐即卖），冷却0
const defaultValues = {
  enabled: false,
  symbol: '510880.SH',
  fear_source: 'a_stock_000015_sh',
  volume_signal_symbol: undefined,
  external_trading_account_id: undefined,
  live_sub_account_id: undefined,
  run_time: dayjs('09:30', 'HH:mm'),
  buy_threshold: 30,
  greed_threshold: 70,
  volume_ratio_threshold: 1.3,
  buy_position_pct: 100,
  cooldown_days: 0,
  trailing_stop_pct: 0,
  sell_position_pct: 100,
  sell_reduction_basis: 'holdings',
  sell_price_above_avg_cost: false,
  max_take_profit_sells_per_cycle: 2,
  min_position_pct_after_take_profit: 0,
  rebalance_threshold_pct: 0,
  // 跷跷板候补（可选）：主标的空仓时，候补极恐放量则买入候补；主标的出信号换回
  sub_symbol: undefined,
  sub_fear_source: 'a_stock_000688_sh',
  sub_volume_signal_symbol: undefined,
  sub_buy_threshold: 25,
  sub_volume_ratio_threshold: 1.6,
  // 第二候补（三标的轮动，可选）
  sub2_symbol: undefined,
  sub2_fear_source: 'qqq_clone',
  sub2_volume_signal_symbol: undefined,
  sub2_buy_threshold: 20,
  sub2_volume_ratio_threshold: 1.3,
  // 换仓阈值：留空=主辅跷跷板；有值=对称双轮动
  swap_threshold: null,
};

const normalizeConfig = (config) => ({
  ...defaultValues,
  ...config,
  run_time: config?.run_time ? dayjs(config.run_time, 'HH:mm') : dayjs('09:30', 'HH:mm'),
  volume_signal_symbol: config?.volume_signal_symbol ?? undefined,
  external_trading_account_id: config?.external_trading_account_id ?? undefined,
  live_sub_account_id: config?.live_sub_account_id ?? undefined,
  sub_symbol: config?.sub_symbol ?? undefined,
  sub_fear_source: config?.sub_fear_source ?? 'a_stock_000688_sh',
  sub_volume_signal_symbol: config?.sub_volume_signal_symbol ?? undefined,
  sub_buy_threshold: config?.sub_buy_threshold ?? 25,
  sub_volume_ratio_threshold: config?.sub_volume_ratio_threshold ?? 1.6,
  sub2_symbol: config?.sub2_symbol ?? undefined,
  sub2_fear_source: config?.sub2_fear_source ?? 'qqq_clone',
  sub2_volume_signal_symbol: config?.sub2_volume_signal_symbol ?? undefined,
  sub2_buy_threshold: config?.sub2_buy_threshold ?? 20,
  sub2_volume_ratio_threshold: config?.sub2_volume_ratio_threshold ?? 1.3,
  swap_threshold: config?.swap_threshold ?? null,
});

const normalizeStateFormValues = (state) => ({
  last_processed_date: state?.last_processed_date ? dayjs(state.last_processed_date) : null,
  cooldown_remaining_days: state?.cooldown_remaining_days ?? 0,
  greed_peak_price: state?.greed_peak_price ?? null,
  take_profit_cycle_sell_count: state?.take_profit_cycle_sell_count ?? 0,
});

const AStockFearStrategy = ({ embedded = false }) => {
  const [form] = Form.useForm();
  const [stateForm] = Form.useForm();
  const navigate = useNavigate();
  const [options, setOptions] = useState({ target_options: [], fear_source_options: [], preset_pairs: [] });
  const [configs, setConfigs] = useState([]);
  const [selectedConfig, setSelectedConfig] = useState(null);
  const [activeTab, setActiveTab] = useState('config');
  const [viewMode, setViewMode] = useState('list');
  const [listLoading, setListLoading] = useState(false);
  const [configLoading, setConfigLoading] = useState(false);
  const [logLoading, setLogLoading] = useState(false);
  const [stateSaving, setStateSaving] = useState(false);
  const [manualLoadingId, setManualLoadingId] = useState(null);
  const [externalTradingAccounts, setExternalTradingAccounts] = useState([]);
  const [liveSubAccounts, setLiveSubAccounts] = useState([]);
  const [logs, setLogs] = useState([]);
  const [strategyState, setStrategyState] = useState(null);
  const logRequestSeqRef = useRef(0);
  const stateRequestSeqRef = useRef(0);

  const selectedExternalTradingAccountId = Form.useWatch('external_trading_account_id', form) || undefined;

  const targetOptions = options.target_options?.length
    ? options.target_options
    : [{ label: '红利ETF 510880.SH', value: '510880.SH' }];
  const fearSourceOptions = options.fear_source_options?.length
    ? options.fear_source_options
    : [{ label: '上证红利 指数贪恐', value: 'a_stock_000015_sh' }];
  const presetPairs = options.preset_pairs || [];

  const externalAccountOptions = useMemo(() => externalTradingAccounts
    .filter((account) => account.market_type === 'A_STOCK')
    .map((account) => ({
      label: `${account.name} (${account.identifier})${account.connected ? ' 在线' : ' 离线'}`,
      value: account.id,
      disabled: !account.enabled,
    })), [externalTradingAccounts]);

  const liveSubAccountOptions = useMemo(() => {
    const currentConfigId = Number(selectedConfig?.id || 0);
    return (liveSubAccounts || [])
      .filter((item) => item.enabled)
      .map((item) => {
        const isFree = !item.strategy_type && !item.strategy_config_id;
        const isCurrentBinding = (
          item.strategy_type === 'a_stock_fear_strategy'
          && currentConfigId > 0
          && Number(item.strategy_config_id) === currentConfigId
        );
        const disabled = !(isFree || isCurrentBinding);
        const statusText = isFree ? '空闲' : `已绑定：${item.strategy_name || item.binding_label || item.strategy_type || '其他策略'}`;
        return {
          label: `${item.name} / ${Number(item.cash_allocated || 0).toFixed(2)} / ${statusText}`,
          value: item.id,
          disabled,
        };
      });
  }, [liveSubAccounts, selectedConfig?.id]);

  const getFearSourceLabel = (value) => fearSourceOptions.find(item => item.value === value)?.label || value;

  const fetchOptions = useCallback(async () => {
    try {
      const { data } = await request.get('/api/a-stock-fear-strategy/options');
      setOptions(data || {});
    } catch (error) {
      message.error(error.response?.data?.detail || '加载策略选项失败');
    }
  }, []);

  const fetchExternalTradingAccounts = useCallback(async () => {
    try {
      const { data } = await request.get('/api/external-trading-accounts');
      setExternalTradingAccounts(data || []);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取外部交易账户失败');
    }
  }, []);

  const fetchLiveSubAccounts = useCallback(async (externalAccountId) => {
    if (!externalAccountId) {
      setLiveSubAccounts([]);
      return;
    }
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${externalAccountId}/sub-accounts`);
      setLiveSubAccounts(data || []);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取虚拟子账户失败');
      setLiveSubAccounts([]);
    }
  }, []);

  const fetchConfigs = useCallback(async () => {
    setListLoading(true);
    try {
      const { data } = await request.get('/api/a-stock-fear-strategy/configs');
      setConfigs(data.map(normalizeConfig));
    } catch (error) {
      message.error(error.response?.data?.detail || '加载策略配置列表失败');
    } finally {
      setListLoading(false);
    }
  }, []);

  const fetchConfigDetail = useCallback(async (configId) => {
    setConfigLoading(true);
    try {
      const { data } = await request.get(`/api/a-stock-fear-strategy/configs/${configId}`);
      const merged = normalizeConfig(data);
      setSelectedConfig(merged);
      form.setFieldsValue(merged);
      if (merged.external_trading_account_id) {
        await fetchLiveSubAccounts(merged.external_trading_account_id);
      }
      return merged;
    } catch (error) {
      message.error(error.response?.data?.detail || '加载策略配置失败');
      return null;
    } finally {
      setConfigLoading(false);
    }
  }, [form, fetchLiveSubAccounts]);

  const fetchLogs = useCallback(async (configId = selectedConfig?.id) => {
    if (!configId) {
      setLogs([]);
      return;
    }
    const requestSeq = logRequestSeqRef.current + 1;
    logRequestSeqRef.current = requestSeq;
    setLogLoading(true);
    try {
      const { data } = await request.get(`/api/a-stock-fear-strategy/configs/${configId}/logs`);
      if (requestSeq === logRequestSeqRef.current) {
        setLogs(data);
      }
    } catch (error) {
      if (requestSeq === logRequestSeqRef.current) {
        message.error(error.response?.data?.detail || '加载运行日志失败');
      }
    } finally {
      if (requestSeq === logRequestSeqRef.current) {
        setLogLoading(false);
      }
    }
  }, [selectedConfig?.id]);

  const fetchState = useCallback(async (configId = selectedConfig?.id) => {
    if (!configId) {
      setStrategyState(null);
      stateForm.resetFields();
      return null;
    }
    const requestSeq = stateRequestSeqRef.current + 1;
    stateRequestSeqRef.current = requestSeq;
    try {
      const { data } = await request.get(`/api/a-stock-fear-strategy/configs/${configId}/state`);
      if (requestSeq === stateRequestSeqRef.current) {
        setStrategyState(data);
        stateForm.setFieldsValue(normalizeStateFormValues(data));
      }
      return data;
    } catch (error) {
      if (requestSeq === stateRequestSeqRef.current) {
        message.error(error.response?.data?.detail || '加载策略状态失败');
      }
      return null;
    }
  }, [selectedConfig?.id, stateForm]);

  const fetchInitialData = useCallback(async () => {
    await Promise.all([fetchConfigs(), fetchExternalTradingAccounts(), fetchOptions()]);
  }, [fetchConfigs, fetchExternalTradingAccounts, fetchOptions]);

  useEffect(() => {
    fetchInitialData();
  }, [fetchInitialData]);

  useEffect(() => {
    fetchLiveSubAccounts(selectedExternalTradingAccountId);
  }, [fetchLiveSubAccounts, selectedExternalTradingAccountId]);

  useEffect(() => {
    return subscribeBackendEvent('a_stock_fear_strategy_run', async (data) => {
      await fetchConfigs();
      if (selectedConfig?.id === data.config_id) {
        await fetchConfigDetail(data.config_id);
        if (activeTab === 'logs') {
          await fetchLogs(data.config_id);
        }
        if (activeTab === 'state') {
          await fetchState(data.config_id);
        }
      }
    });
  }, [activeTab, fetchConfigDetail, fetchConfigs, fetchLogs, fetchState, selectedConfig?.id]);

  const applyPresetPair = (pair) => {
    if (!pair) {
      return;
    }
    form.setFieldsValue({
      symbol: pair.target_symbol,
      volume_signal_symbol: undefined,
      fear_source: pair.fear_source,
    });
  };

  const openCreate = () => {
    setSelectedConfig(null);
    setLogs([]);
    setStrategyState(null);
    setLiveSubAccounts([]);
    form.resetFields();
    stateForm.resetFields();
    form.setFieldsValue(defaultValues);
    setActiveTab('config');
    setViewMode('detail');
  };

  const openConfig = async (record, tabKey = 'config') => {
    setViewMode('detail');
    setActiveTab(tabKey);
    setLogs([]);
    setStrategyState(null);
    stateForm.resetFields();
    const config = await fetchConfigDetail(record.id);
    if (tabKey === 'logs' && config?.id) {
      await fetchLogs(config.id);
    }
    if (tabKey === 'state' && config?.id) {
      await fetchState(config.id);
    }
  };

  const returnToList = async () => {
    setViewMode('list');
    setSelectedConfig(null);
    setLogs([]);
    setStrategyState(null);
    form.resetFields();
    stateForm.resetFields();
    await fetchConfigs();
  };

  const buildPayload = (values) => ({
    ...values,
    symbol: (values.symbol || '510880.SH').trim().toUpperCase(),
    volume_signal_symbol: values.volume_signal_symbol || undefined,
    run_time: values.run_time ? values.run_time.format('HH:mm') : '09:30',
    sub_symbol: values.sub_symbol || undefined,
    sub_volume_signal_symbol: values.sub_volume_signal_symbol || undefined,
    sub_buy_threshold: values.sub_buy_threshold ?? 25,
    sub_volume_ratio_threshold: values.sub_volume_ratio_threshold ?? 1.6,
    sub2_symbol: values.sub2_symbol || undefined,
    sub2_fear_source: values.sub2_fear_source || 'qqq_clone',
    sub2_volume_signal_symbol: values.sub2_volume_signal_symbol || undefined,
    sub2_buy_threshold: values.sub2_buy_threshold ?? 20,
    sub2_volume_ratio_threshold: values.sub2_volume_ratio_threshold ?? 1.3,
    swap_threshold: values.swap_threshold ?? null,
  });

  const handleBacktest = () => {
    const values = {
      ...defaultValues,
      ...form.getFieldsValue(),
    };
    // 跳转到「情绪 + 量能 超参数回测」页（SOXL 配置页同一个回测页面，支持 A股标的与 a_stock_* 恐贪来源）
    // 实盘隔天信号语义 → 回测开启 execute_next_open（信号日收盘决策、次日开盘成交）、
    // trailing_stop_pct=0 = 贪恐即卖，与实盘完全对齐
    navigate('/fear-volume-backtest', {
      state: {
        autoRunBacktest: true,
        presetValues: {
          symbol: values.symbol || '510880.SH',
          volume_signal_symbol: values.volume_signal_symbol || undefined,
          fear_source_values: [values.fear_source || 'a_stock_000015_sh'],
          initial_capital: 1000000,
          top_n: 1,
          objective: 'annualized_return',
          eval_workers: 1,
          fit_rebalance_threshold_pct: values.rebalance_threshold_pct ?? 0,
          // history state 会被 structuredClone，dayjs 会丢原型，传字符串由回测页转 dayjs
          date_range: ['2023-03-22', dayjs().format('YYYY-MM-DD')],
          buy_threshold_values: String(values.buy_threshold ?? defaultValues.buy_threshold),
          greed_threshold_values: String(values.greed_threshold ?? defaultValues.greed_threshold),
          volume_ratio_threshold_values: String(values.volume_ratio_threshold ?? defaultValues.volume_ratio_threshold),
          volume_ratio_consecutive_days_values: '1',
          buy_position_pct_values: String(values.buy_position_pct ?? defaultValues.buy_position_pct),
          cooldown_days_values: String(values.cooldown_days ?? defaultValues.cooldown_days),
          trailing_stop_pct_values: String(values.trailing_stop_pct ?? defaultValues.trailing_stop_pct),
          sell_position_pct_values: String(values.sell_position_pct ?? defaultValues.sell_position_pct),
          sell_reduction_basis_values: [values.sell_reduction_basis || defaultValues.sell_reduction_basis],
          // 回测页表单 Select 的 option value 是字符串 'true'/'false'，必须传字符串才能正确显示/解析
          sell_price_above_avg_cost_values: [values.sell_price_above_avg_cost ? 'true' : 'false'],
          max_take_profit_sells_per_cycle_values: String(
            values.max_take_profit_sells_per_cycle ?? defaultValues.max_take_profit_sells_per_cycle
          ),
          min_position_pct_after_take_profit_values: String(
            values.min_position_pct_after_take_profit ?? defaultValues.min_position_pct_after_take_profit
          ),
          execute_next_open_values: ['true'],
          sub_symbol: values.sub_symbol || undefined,
          sub_fear_source: values.sub_fear_source || 'a_stock_000688_sh',
          sub_volume_signal_symbol: values.sub_volume_signal_symbol || undefined,
          sub_buy_threshold_values: String(values.sub_buy_threshold ?? 25),
          sub_volume_ratio_threshold_values: String(values.sub_volume_ratio_threshold ?? 1.6),
          swap_threshold_values: [values.swap_threshold ?? null],
          sub2_symbol: values.sub2_symbol || undefined,
          sub2_fear_source: values.sub2_fear_source || 'qqq_clone',
          sub2_volume_signal_symbol: values.sub2_volume_signal_symbol || undefined,
          sub2_buy_threshold_values: String(values.sub2_buy_threshold ?? 20),
          sub2_volume_ratio_threshold_values: String(values.sub2_volume_ratio_threshold ?? 1.3),
        },
      },
    });
  };

  const handleSave = async (values) => {
    setConfigLoading(true);
    try {
      const payload = buildPayload(values);
      const requestAction = selectedConfig?.id
        ? request.put(`/api/a-stock-fear-strategy/configs/${selectedConfig.id}`, payload)
        : request.post('/api/a-stock-fear-strategy/configs', payload);
      const { data } = await requestAction;
      const merged = normalizeConfig(data);
      setSelectedConfig(merged);
      form.setFieldsValue(merged);
      message.success('策略配置已保存');
      await fetchConfigs();
    } catch (error) {
      message.error(error.response?.data?.detail || '保存策略配置失败');
    } finally {
      setConfigLoading(false);
    }
  };

  const handleDelete = async (configId = selectedConfig?.id) => {
    if (!configId) {
      return;
    }
    try {
      await request.delete(`/api/a-stock-fear-strategy/configs/${configId}`);
      message.success('策略配置已删除');
      if (selectedConfig?.id === configId) {
        returnToList();
      } else {
        await fetchConfigs();
      }
    } catch (error) {
      message.error(error.response?.data?.detail || '删除策略配置失败');
    }
  };

  const handleManualCheck = async (configId = selectedConfig?.id) => {
    if (!configId) {
      return;
    }
    setManualLoadingId(configId);
    try {
      const { data } = await request.post(`/api/a-stock-fear-strategy/configs/${configId}/manual-check`);
      message.success(data?.message || '已触发手动检查');
    } catch (error) {
      message.error(error.response?.data?.detail || '触发手动检查失败');
    } finally {
      setManualLoadingId(null);
    }
  };

  const handleSaveState = async (values) => {
    if (!selectedConfig?.id) {
      message.warning('请先保存配置');
      return;
    }
    setStateSaving(true);
    try {
      const payload = {
        ...values,
        last_processed_date: values.last_processed_date
          ? values.last_processed_date.format('YYYY-MM-DD')
          : null,
        greed_peak_price: values.greed_peak_price ?? null,
      };
      await request.put(`/api/a-stock-fear-strategy/configs/${selectedConfig.id}/state`, payload);
      message.success('策略状态已更新');
      await fetchState(selectedConfig.id);
    } catch (error) {
      message.error(error.response?.data?.detail || '更新策略状态失败');
    } finally {
      setStateSaving(false);
    }
  };

  const columns = [
    {
      title: '启用',
      dataIndex: 'enabled',
      width: 80,
      render: (value) => <Tag color={value ? 'green' : 'default'}>{value ? '是' : '否'}</Tag>,
    },
    {
      title: '标的',
      dataIndex: 'symbol',
      width: 110,
      render: (value) => <Tag color="orange">{value}</Tag>,
    },
    {
      title: '恐贪来源',
      dataIndex: 'fear_source',
      width: 150,
      ellipsis: true,
      render: (value) => getFearSourceLabel(value),
    },
    {
      title: '量比来源',
      dataIndex: 'volume_signal_symbol',
      width: 110,
      render: (value) => value || '自身',
    },
    {
      title: '跷跷板候补',
      dataIndex: 'sub_symbol',
      width: 200,
      ellipsis: true,
      render: (value, record) => (value
        ? `${value} 恐慌≤${record.sub_buy_threshold}/量比≥${record.sub_volume_ratio_threshold}${record.swap_threshold != null ? `/换仓>${record.swap_threshold}` : ''}`
        : '-'),
    },
    {
      title: '第二候补',
      dataIndex: 'sub2_symbol',
      width: 190,
      ellipsis: true,
      render: (value, record) => (value
        ? `${value} 恐慌≤${record.sub2_buy_threshold}/量比≥${record.sub2_volume_ratio_threshold}`
        : '-'),
    },
    {
      title: '触发时间',
      dataIndex: 'run_time',
      width: 100,
      render: (value) => (value ? dayjs(value).format('HH:mm') : '09:30'),
    },
    {
      title: '账户',
      dataIndex: 'external_trading_account_name',
      width: 250,
      ellipsis: true,
      render: (value, record) => value || `${record.external_trading_account_id || '-'} / 子账户: ${record.live_sub_account_id || '-'}`,
    },
    { title: '买入阈值', dataIndex: 'buy_threshold', width: 90 },
    { title: '贪恐卖出阈值', dataIndex: 'greed_threshold', width: 110 },
    { title: '量比阈值', dataIndex: 'volume_ratio_threshold', width: 90 },
    {
      title: '移动止盈%',
      dataIndex: 'trailing_stop_pct',
      width: 120,
      render: (value) => (Number(value) === 0 ? '0(贪恐即卖)' : value),
    },
    { title: '买入仓位%', dataIndex: 'buy_position_pct', width: 100 },
    { title: '卖出仓位%', dataIndex: 'sell_position_pct', width: 100 },
    { title: '冷却天数', dataIndex: 'cooldown_days', width: 90 },
    {
      title: '最近运行',
      dataIndex: 'last_run_at',
      width: 160,
      render: (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-'),
    },
    {
      title: '最近状态',
      dataIndex: 'last_run_status',
      width: 100,
      render: (value) => value ? <Tag color={value === 'SUCCESS' ? 'green' : value === 'ERROR' ? 'red' : 'blue'}>{value}</Tag> : '-',
    },
    {
      title: '操作',
      key: 'action',
      width: 200,
      fixed: 'right',
      render: (_, record) => (
        <Space>
          <Button
            type="link"
            size="small"
            onClick={(event) => {
              event.stopPropagation();
              openConfig(record, 'config');
            }}
          >
            编辑
          </Button>
          <Button
            type="link"
            size="small"
            danger
            onClick={(event) => {
              event.stopPropagation();
              handleDelete(record.id);
            }}
          >
            删除
          </Button>
          <Button
            type="link"
            size="small"
            disabled={manualLoadingId !== null}
            onClick={(event) => {
              event.stopPropagation();
              handleManualCheck(record.id);
            }}
          >
            手动检查
          </Button>
        </Space>
      ),
    },
  ];

  const logColumns = [
    { title: '时间', dataIndex: 'timestamp', width: 170, render: (value) => dayjs(value).format('YYYY-MM-DD HH:mm:ss') },
    {
      title: '动作',
      dataIndex: 'action',
      width: 90,
      render: (value) => <Tag color={value === 'BUY' ? 'red' : value === 'SELL' ? 'green' : 'default'}>{value}</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value) => <Tag color={value === 'SUCCESS' ? 'green' : value === 'ERROR' ? 'red' : 'blue'}>{value}</Tag>,
    },
    { title: '价格', dataIndex: 'price', width: 90, render: (value) => (value == null ? '-' : Number(value).toFixed(4)) },
    { title: '数量', dataIndex: 'quantity', width: 90, render: (value) => (value == null ? '-' : Number(value).toFixed(0)) },
    { title: '恐贪分', dataIndex: 'fear_score', width: 90, render: (value) => (value == null ? '-' : Number(value).toFixed(2)) },
    { title: '量比', dataIndex: 'volume_ratio', width: 90, render: (value) => (value == null ? '-' : Number(value).toFixed(2)) },
    { title: '消息', dataIndex: 'message' },
  ];

  return (
    <div style={{ padding: embedded ? 0 : 24 }}>
      {viewMode === 'list' ? (
        <Card
          title={embedded ? 'A股情绪量能策略' : '策略配置列表'}
          loading={listLoading}
          style={{ marginBottom: 24 }}
          extra={
            <Button type="primary" onClick={openCreate}>新建策略配置</Button>
          }
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="策略说明"
            description="信号与交易隔天：每个 A 股交易日在触发时间（默认 09:30 开盘）用前一交易日的恐贪分数和 20 日量比判断信号，下市价单在开盘成交。买入：恐贪 ≤ 买入阈值 且 量比 ≥ 阈值；卖出：恐贪 ≥ 贪恐卖出阈值（移动止盈=0 即卖）或移动止盈回撤触发。"
          />
          <Table
            rowKey="id"
            dataSource={configs}
            columns={columns}
            loading={listLoading}
            scroll={{ x: 2200 }}
            onRow={(record) => ({
              onClick: () => openConfig(record),
              style: { cursor: 'pointer' },
            })}
          />
        </Card>
      ) : (
        <>
          <Card
            title={selectedConfig?.id ? `${selectedConfig.symbol} 策略配置` : '新建 A股策略配置'}
            style={{ marginBottom: 24 }}
            extra={
              <Space>
                <Button onClick={returnToList}>返回列表</Button>
                {selectedConfig?.id && (
                  <Button danger onClick={handleDelete}>删除</Button>
                )}
                <Button
                  type="primary"
                  loading={manualLoadingId === selectedConfig?.id}
                  onClick={() => handleManualCheck()}
                >
                  手动检查
                </Button>
              </Space>
            }
          >
            <Tabs
              activeKey={activeTab}
              onChange={setActiveTab}
              items={[
                {
                  key: 'config',
                  label: '配置',
                  children: (
                    <Form
                      form={form}
                      layout="vertical"
                      onFinish={handleSave}
                    >
                      <Row gutter={16}>
                        <Col xs={24} md={8}>
                          <Form.Item name="preset_pair" label="A股指数ETF预设组合">
                            <Select
                              allowClear
                              showSearch
                              optionFilterProp="label"
                              placeholder="选择后自动填入标的和恐贪来源"
                              options={presetPairs.map(item => ({ label: item.label, value: item.key }))}
                              onChange={applyPresetPair}
                            />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="symbol" label="交易标的" rules={[{ required: true, message: '请选择交易标的' }]}>
                            <Select showSearch optionFilterProp="label" options={targetOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="volume_signal_symbol" label="量比来源标的">
                            <Select
                              allowClear
                              showSearch
                              optionFilterProp="label"
                              placeholder="默认使用标的自身"
                              options={targetOptions}
                            />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="fear_source" label="恐贪来源" rules={[{ required: true, message: '请选择恐贪来源' }]}>
                            <Select showSearch optionFilterProp="label" options={fearSourceOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="run_time" label="每日触发时间(Shanghai)" rules={[{ required: true, message: '请选择触发时间' }]}>
                            <TimePicker format="HH:mm" minuteStep={5} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={6}>
                          <Form.Item name="external_trading_account_id" label="外部交易账户(A股)" rules={[{ required: true, message: '请选择 A 股外部交易账户' }]}>
                            <Select options={externalAccountOptions} placeholder="仅显示 A 股账户" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={6}>
                          <Form.Item name="live_sub_account_id" label="虚拟子账户" rules={[{ required: true, message: '请选择虚拟子账户' }]}>
                            <Select options={liveSubAccountOptions} placeholder="选择空闲或已绑定本策略的子账户" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="buy_threshold" label="买入恐贪阈值(<=)">
                            <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="greed_threshold" label="贪恐卖出阈值(>=)">
                            <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="volume_ratio_threshold" label="买入量比阈值(>=)">
                            <InputNumber min={0.1} max={20} step={0.1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="trailing_stop_pct" label="移动止盈回撤%(0=贪恐即卖)">
                            <InputNumber min={0} max={100} step={0.5} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="buy_position_pct" label="买入仓位%">
                            <InputNumber min={1} max={100} step={5} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sell_position_pct" label="卖出仓位%">
                            <InputNumber min={0} max={100} step={5} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="cooldown_days" label="冷却天数">
                            <InputNumber min={0} max={60} step={1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sell_reduction_basis" label="止盈减仓口径">
                            <Select options={sellReductionBasisOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sell_price_above_avg_cost" label="均价保护" valuePropName="checked">
                            <Switch />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="max_take_profit_sells_per_cycle" label="单轮止盈次数">
                            <InputNumber min={1} max={20} step={1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="min_position_pct_after_take_profit" label="止盈后保留仓位%">
                            <InputNumber min={0} max={100} step={5} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="rebalance_threshold_pct" label="调仓阈值%">
                            <InputNumber min={0} max={100} step={0.5} style={{ width: '100%' }} />
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
                            <Select allowClear showSearch optionFilterProp="label" placeholder="留空=单标的" options={targetOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub_fear_source" label="候补恐贪来源">
                            <Select showSearch optionFilterProp="label" options={fearSourceOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub_volume_signal_symbol" label="候补量比来源">
                            <Select allowClear showSearch optionFilterProp="label" placeholder="默认候补自身" options={targetOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub_buy_threshold" label="候补恐慌阈值(<=)">
                            <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub_volume_ratio_threshold" label="候补量比阈值(>=)">
                            <InputNumber min={0.1} max={20} step={0.1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="swap_threshold" label="换仓阈值(留空=主辅跷跷板)">
                            <InputNumber min={0} max={100} step={1} placeholder="留空关闭" style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={24}>
                          <Alert
                            type="info"
                            showIcon
                            style={{ marginBottom: 12 }}
                            message="第二候补（三标的轮动，可选）"
                            description="再填一个候补可做三标的对称轮动（需换仓阈值非空）：空仓时任一标的极恐放量都买（都触发买恐贪最低的）；持有 X 时 X 恐贪超过换仓阈值且任一其他标的有信号则换仓。例如纳指ETF 159941.SZ 配 QQQ 自算贪恐（美股恐贪上海凌晨已算出）。"
                          />
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub2_symbol" label="第二候补标的">
                            <Select allowClear showSearch optionFilterProp="label" placeholder="留空=双标的" options={targetOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub2_fear_source" label="第二候补恐贪来源">
                            <Select showSearch optionFilterProp="label" options={fearSourceOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub2_volume_signal_symbol" label="第二候补量比来源">
                            <Select allowClear showSearch optionFilterProp="label" placeholder="默认自身" options={targetOptions} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub2_buy_threshold" label="第二候补恐慌阈值(<=)">
                            <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="sub2_volume_ratio_threshold" label="第二候补量比阈值(>=)">
                            <InputNumber min={0.1} max={20} step={0.1} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="enabled" label="启用" valuePropName="checked">
                            <Switch />
                          </Form.Item>
                        </Col>
                      </Row>
                      <Space>
                        <Button type="primary" htmlType="submit" loading={configLoading}>
                          保存配置
                        </Button>
                        <Button icon={<ExperimentOutlined />} onClick={handleBacktest}>
                          回测
                        </Button>
                        <Button onClick={returnToList}>返回</Button>
                      </Space>
                    </Form>
                  ),
                },
                {
                  key: 'state',
                  label: '状态',
                  children: (
                    <Form
                      form={stateForm}
                      layout="vertical"
                      onFinish={handleSaveState}
                      initialValues={normalizeStateFormValues(strategyState)}
                    >
                      <Row gutter={16}>
                        <Col xs={24} md={6}>
                          <Form.Item name="last_processed_date" label="已处理信号日">
                            <DatePicker style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="cooldown_remaining_days" label="剩余冷却天数">
                            <InputNumber min={0} max={60} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="greed_peak_price" label="止盈峰值价">
                            <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item name="take_profit_cycle_sell_count" label="本轮止盈卖出次数">
                            <InputNumber min={0} max={20} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                      </Row>
                      <Space>
                        <Button type="primary" htmlType="submit" loading={stateSaving}>保存状态</Button>
                        <Button onClick={() => fetchState()}>刷新</Button>
                      </Space>
                      {strategyState && (
                        <Descriptions size="small" column={{ xs: 1, md: 3 }} style={{ marginTop: 16 }} bordered>
                          <Descriptions.Item label="配置ID">{strategyState.config_id}</Descriptions.Item>
                          <Descriptions.Item label="标的">{strategyState.symbol}</Descriptions.Item>
                          <Descriptions.Item label="状态更新时间">
                            {strategyState.updated_at ? dayjs(strategyState.updated_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
                          </Descriptions.Item>
                        </Descriptions>
                      )}
                    </Form>
                  ),
                },
                {
                  key: 'logs',
                  label: '运行日志',
                  children: (
                    <Table
                      rowKey={(record, index) => `${record.id}-${index}`}
                      dataSource={logs}
                      columns={logColumns}
                      loading={logLoading}
                      size="small"
                      scroll={{ x: 1200 }}
                      pagination={{ defaultPageSize: 10 }}
                    />
                  ),
                },
              ]}
            />
          </Card>
          {selectedConfig?.id && (
            <Card title="策略参数说明" size="small">
              <Descriptions column={{ xs: 1, md: 3 }} size="small" bordered>
                <Descriptions.Item label="标的">{selectedConfig.symbol}</Descriptions.Item>
                <Descriptions.Item label="恐贪来源">{getFearSourceLabel(selectedConfig.fear_source)}</Descriptions.Item>
                <Descriptions.Item label="量比来源">{selectedConfig.volume_signal_symbol || '标的自身'}</Descriptions.Item>
                <Descriptions.Item label="触发时间">{formatRunTime(selectedConfig.run_time)} (Asia/Shanghai)</Descriptions.Item>
                <Descriptions.Item label="信号日">前一交易日恐贪 + 量比，触发时开盘成交</Descriptions.Item>
                <Descriptions.Item label="移动止盈">
                  {Number(selectedConfig.trailing_stop_pct) === 0 ? '0 = 到达贪恐阈值即卖' : `${selectedConfig.trailing_stop_pct}% 回撤触发`}
                </Descriptions.Item>
                {selectedConfig.sub_symbol && (
                  <>
                    <Descriptions.Item label="跷跷板候补">{selectedConfig.sub_symbol}</Descriptions.Item>
                    <Descriptions.Item label="候补恐贪来源">{getFearSourceLabel(selectedConfig.sub_fear_source)}</Descriptions.Item>
                    <Descriptions.Item label="候补买入门槛">恐慌≤{selectedConfig.sub_buy_threshold} 且 量比≥{selectedConfig.sub_volume_ratio_threshold}</Descriptions.Item>
                    <Descriptions.Item label="换仓阈值">{selectedConfig.swap_threshold ?? '关闭（主辅跷跷板）'}</Descriptions.Item>
                    {selectedConfig.sub2_symbol && (
                      <>
                        <Descriptions.Item label="第二候补">{selectedConfig.sub2_symbol}</Descriptions.Item>
                        <Descriptions.Item label="第二候补恐贪来源">{getFearSourceLabel(selectedConfig.sub2_fear_source)}</Descriptions.Item>
                        <Descriptions.Item label="第二候补买入门槛">恐慌≤{selectedConfig.sub2_buy_threshold} 且 量比≥{selectedConfig.sub2_volume_ratio_threshold}</Descriptions.Item>
                      </>
                    )}
                  </>
                )}
              </Descriptions>
            </Card>
          )}
        </>
      )}
    </div>
  );
};

export default AStockFearStrategy;
