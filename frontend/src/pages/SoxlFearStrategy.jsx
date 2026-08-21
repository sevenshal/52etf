import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  ArrowLeftOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  FileSearchOutlined,
  HistoryOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';
import './SoxlFearStrategy.css';

const { Title, Text } = Typography;

const sellReductionBasisOptions = [
  { label: '按总资产', value: 'portfolio' },
  { label: '按持仓股票', value: 'holdings' },
];

const accountTypeOptions = [
  { label: 'Interactive Brokers (IB)', value: 'ib' },
  { label: '长桥证券 (Longport)', value: 'longport' },
  { label: '外部交易账户', value: 'external' },
];

const accountTypeLabels = {
  ib: 'IB',
  longport: '长桥',
  external: '外部',
};

const defaultValues = {
  enabled: false,
  symbol: 'SOXL.US',
  account_type: 'ib',
  ib_account_id: undefined,
  longport_account_id: undefined,
  external_trading_account_id: undefined,
  live_sub_account_id: undefined,
  buy_threshold: 40,
  greed_threshold: 41,
  volume_ratio_threshold: 1.38,
  buy_position_pct: 60,
  cooldown_days: 10,
  trailing_stop_pct: 5,
  sell_position_pct: 50,
  sell_reduction_basis: 'portfolio',
  max_take_profit_sells_per_cycle: 2,
  min_position_pct_after_take_profit: 5,
  rebalance_threshold_pct: 5,
};

const normalizeConfig = (config) => ({
  ...defaultValues,
  ...config,
  ib_account_id: config?.ib_account_id ?? undefined,
  longport_account_id: config?.longport_account_id ?? undefined,
  external_trading_account_id: config?.external_trading_account_id ?? undefined,
  live_sub_account_id: config?.live_sub_account_id ?? undefined,
});

const normalizeStateFormValues = (state) => ({
  last_processed_date: state?.last_processed_date ? dayjs(state.last_processed_date) : null,
  cooldown_remaining_days: state?.cooldown_remaining_days ?? 0,
  greed_peak_price: state?.greed_peak_price ?? null,
  take_profit_cycle_sell_count: state?.take_profit_cycle_sell_count ?? 0,
});

const SoxlFearStrategy = ({ embedded = false }) => {
  const [form] = Form.useForm();
  const [stateForm] = Form.useForm();
  const selectedExternalTradingAccountId = Form.useWatch('external_trading_account_id', form);
  const navigate = useNavigate();
  const [viewMode, setViewMode] = useState('list');
  const [activeTab, setActiveTab] = useState('config');
  const [configs, setConfigs] = useState([]);
  const [selectedConfig, setSelectedConfig] = useState(null);
  const [listLoading, setListLoading] = useState(false);
  const [configLoading, setConfigLoading] = useState(false);
  const [logLoading, setLogLoading] = useState(false);
  const [stateLoading, setStateLoading] = useState(false);
  const [stateSaving, setStateSaving] = useState(false);
  const [manualLoadingId, setManualLoadingId] = useState(null);
  const logRequestSeqRef = useRef(0);
  const stateRequestSeqRef = useRef(0);
  const isManualRunBusy = manualLoadingId !== null;
  const [ibAccounts, setIbAccounts] = useState([]);
  const [longportAccounts, setLongportAccounts] = useState([]);
  const [externalTradingAccounts, setExternalTradingAccounts] = useState([]);
  const [liveSubAccounts, setLiveSubAccounts] = useState([]);
  const [logs, setLogs] = useState([]);
  const [strategyState, setStrategyState] = useState(null);

  const accountMaps = useMemo(() => {
    const ibMap = new Map(ibAccounts.map((account) => [account.id, account]));
    const longportMap = new Map(longportAccounts.map((account) => [account.lp_account_id, account]));
    const externalMap = new Map(externalTradingAccounts.map((account) => [account.id, account]));
    return { ibMap, longportMap, externalMap };
  }, [ibAccounts, longportAccounts, externalTradingAccounts]);

  const externalTradingAccountOptions = useMemo(() => externalTradingAccounts
    .filter((account) => account.market_type === 'US_STOCK')
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
          item.strategy_type === 'soxl_fear_strategy'
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

  const fetchLiveSubAccounts = useCallback(async (externalAccountId) => {
    if (!externalAccountId) {
      setLiveSubAccounts([]);
      return [];
    }
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${externalAccountId}/sub-accounts/options`);
      setLiveSubAccounts(data || []);
      return data || [];
    } catch (error) {
      message.error(error.response?.data?.detail || '获取虚拟子账户失败');
      setLiveSubAccounts([]);
      return [];
    }
  }, []);

  const fetchConfigs = useCallback(async () => {
    setListLoading(true);
    try {
      const { data } = await request.get('/api/soxl-fear-strategy/configs');
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
      const { data } = await request.get(`/api/soxl-fear-strategy/configs/${configId}`);
      const merged = normalizeConfig(data);
      setSelectedConfig(merged);
      form.setFieldsValue(merged);
      if (merged.account_type === 'external' && merged.external_trading_account_id) {
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
      const { data } = await request.get(`/api/soxl-fear-strategy/configs/${configId}/logs`);
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
    setStateLoading(true);
    try {
      const { data } = await request.get(`/api/soxl-fear-strategy/configs/${configId}/state`);
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
    } finally {
      if (requestSeq === stateRequestSeqRef.current) {
        setStateLoading(false);
      }
    }
  }, [selectedConfig?.id, stateForm]);

  const fetchIbAccounts = useCallback(async () => {
    try {
      const { data } = await request.get('/api/ib-accounts/options');
      setIbAccounts(data);
    } catch (error) {
      message.error('获取 IB 账户失败');
    }
  }, []);

  const fetchLongportAccounts = useCallback(async () => {
    try {
      const { data } = await request.get('/api/longport-accounts');
      setLongportAccounts(data);
    } catch (error) {
      message.error('获取长桥账户失败');
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

  const fetchInitialData = useCallback(async () => {
    await Promise.all([fetchConfigs(), fetchIbAccounts(), fetchLongportAccounts(), fetchExternalTradingAccounts()]);
  }, [fetchConfigs, fetchIbAccounts, fetchLongportAccounts, fetchExternalTradingAccounts]);

  useEffect(() => {
    fetchInitialData();
  }, [fetchInitialData]);

  useEffect(() => {
    return subscribeBackendEvent('soxl_fear_strategy_run', async (data) => {
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

  useEffect(() => {
    fetchLiveSubAccounts(selectedExternalTradingAccountId);
  }, [fetchLiveSubAccounts, selectedExternalTradingAccountId]);

  const getAccountLabel = (record) => {
    if (!record) return '-';
    if (record.account_type === 'external') {
      const account = accountMaps.externalMap.get(record.external_trading_account_id);
      const accountLabel = record.external_trading_account_name
        || (account ? `${account.name} (${account.identifier})` : record.external_trading_account_id || '-');
      const subAccountLabel = record.live_sub_account_name || record.live_sub_account_id || '-';
      return `${accountLabel} / 子账户: ${subAccountLabel}`;
    }
    if (record.account_type === 'longport') {
      const account = accountMaps.longportMap.get(record.longport_account_id || record.trading_account_id);
      return account ? `${account.name} (ID: ${account.lp_account_id})` : (record.longport_account_id || record.trading_account_id || '-');
    }

    const accountId = record.ib_account_id ?? Number(record.trading_account_id);
    const account = accountMaps.ibMap.get(accountId);
    return account ? `${account.name} (ID: ${account.id}, Port: ${account.ib_port})` : (record.ib_account_id || record.trading_account_id || '-');
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

  const buildPayload = (values) => {
    const payload = {
      ...values,
      symbol: (values.symbol || 'SOXL.US').trim().toUpperCase(),
    };
    if (payload.account_type === 'external') {
      payload.ib_account_id = undefined;
      payload.longport_account_id = undefined;
    } else if (payload.account_type === 'longport') {
      payload.ib_account_id = undefined;
      payload.external_trading_account_id = undefined;
      payload.live_sub_account_id = undefined;
    } else {
      payload.longport_account_id = undefined;
      payload.external_trading_account_id = undefined;
      payload.live_sub_account_id = undefined;
    }
    return payload;
  };

  const handleSave = async (values) => {
    setConfigLoading(true);
    try {
      const payload = buildPayload(values);
      const requestAction = selectedConfig?.id
        ? request.put(`/api/soxl-fear-strategy/configs/${selectedConfig.id}`, payload)
        : request.post('/api/soxl-fear-strategy/configs', payload);
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

  const handleSaveState = async (values) => {
    if (!selectedConfig?.id) {
      message.warning('请先保存配置');
      return;
    }
    const payload = {
      ...values,
      last_processed_date: values.last_processed_date
        ? values.last_processed_date.format('YYYY-MM-DD')
        : null,
      greed_peak_price: values.greed_peak_price ?? null,
    };
    setStateSaving(true);
    try {
      const { data } = await request.put(`/api/soxl-fear-strategy/configs/${selectedConfig.id}/state`, payload);
      setStrategyState(data);
      stateForm.setFieldsValue(normalizeStateFormValues(data));
      message.success('策略状态已保存');
    } catch (error) {
      message.error(error.response?.data?.detail || '保存策略状态失败');
    } finally {
      setStateSaving(false);
    }
  };

  const handleManualRun = async (record = selectedConfig) => {
    if (isManualRunBusy) return;
    if (!record?.id) {
      message.warning('请先保存配置');
      return;
    }

    setManualLoadingId(record.id);
    try {
      await request.post(`/api/soxl-fear-strategy/configs/${record.id}/manual-check`);
      message.success('已触发一次后台检查，完成后会自动刷新');
    } catch (error) {
      message.error(error.response?.data?.detail || '手动执行失败');
    } finally {
      setManualLoadingId(null);
    }
  };

  const handleDelete = async (record) => {
    try {
      await request.delete(`/api/soxl-fear-strategy/configs/${record.id}`);
      message.success('配置已删除');
      if (selectedConfig?.id === record.id) {
        setSelectedConfig(null);
        setViewMode('list');
        setLogs([]);
        setStrategyState(null);
        form.resetFields();
        stateForm.resetFields();
      }
      await fetchConfigs();
    } catch (error) {
      message.error(error.response?.data?.detail || '删除配置失败');
    }
  };

  const handleBacktest = () => {
    const values = {
      ...defaultValues,
      ...form.getFieldsValue(),
    };
    navigate('/fear-volume-backtest', {
      state: {
        autoRunBacktest: true,
        presetValues: {
          symbol: values.symbol || 'SOXL.US',
          initial_capital: 100000,
          top_n: 1,
          objective: 'annualized_return',
          eval_workers: 1,
          fit_rebalance_threshold_pct: values.rebalance_threshold_pct,
          buy_threshold_values: String(values.buy_threshold ?? defaultValues.buy_threshold),
          greed_threshold_values: String(values.greed_threshold ?? defaultValues.greed_threshold),
          volume_ratio_threshold_values: String(values.volume_ratio_threshold ?? defaultValues.volume_ratio_threshold),
          buy_position_pct_values: String(values.buy_position_pct ?? defaultValues.buy_position_pct),
          cooldown_days_values: String(values.cooldown_days ?? defaultValues.cooldown_days),
          trailing_stop_pct_values: String(values.trailing_stop_pct ?? defaultValues.trailing_stop_pct),
          sell_position_pct_values: String(values.sell_position_pct ?? defaultValues.sell_position_pct),
          sell_reduction_basis_values: [values.sell_reduction_basis || defaultValues.sell_reduction_basis],
          max_take_profit_sells_per_cycle_values: String(
            values.max_take_profit_sells_per_cycle ?? defaultValues.max_take_profit_sells_per_cycle
          ),
          min_position_pct_after_take_profit_values: String(
            values.min_position_pct_after_take_profit ?? defaultValues.min_position_pct_after_take_profit
          ),
        },
      },
    });
  };

  const renderActionButton = (eventHandler) => (event) => {
    event.stopPropagation();
    eventHandler();
  };

  const renderActionTitle = () => (
    <div className="soxl-fear-action-title">
      <span>操作</span>
      <Space size={4}>
        <Tooltip title="刷新">
          <Button
            aria-label="刷新"
            icon={<ReloadOutlined />}
            loading={listLoading}
            disabled={listLoading}
            size="small"
            onClick={(event) => {
              event.stopPropagation();
              fetchConfigs();
            }}
          />
        </Tooltip>
        <Tooltip title="新增配置">
          <Button
            aria-label="新增配置"
            icon={<PlusOutlined />}
            size="small"
            type="primary"
            onClick={(event) => {
              event.stopPropagation();
              openCreate();
            }}
          />
        </Tooltip>
      </Space>
    </div>
  );

  const configColumns = [
    {
      title: '交易标的',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 120,
      render: (value) => <Text strong>{value}</Text>,
    },
    {
      title: '账户类型',
      dataIndex: 'account_type',
      key: 'account_type',
      width: 110,
      render: (value) => {
        const color = value === 'external' ? 'green' : value === 'longport' ? 'purple' : 'blue';
        return <Tag color={color}>{accountTypeLabels[value] || value}</Tag>;
      },
    },
    {
      title: '账户ID',
      key: 'trading_account_id',
      width: 220,
      render: (_, record) => getAccountLabel(record),
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 90,
      render: (value) => <Tag color={value ? 'success' : 'default'}>{value ? '开启' : '关闭'}</Tag>,
    },
    {
      title: '买入阈值',
      dataIndex: 'buy_threshold',
      key: 'buy_threshold',
      width: 100,
      render: (value) => Number(value).toFixed(2),
    },
    {
      title: '止盈区阈值',
      dataIndex: 'greed_threshold',
      key: 'greed_threshold',
      width: 110,
      render: (value) => Number(value).toFixed(2),
    },
    {
      title: '投影量比',
      dataIndex: 'volume_ratio_threshold',
      key: 'volume_ratio_threshold',
      width: 100,
      render: (value) => Number(value).toFixed(2),
    },
    {
      title: '最近执行',
      dataIndex: 'last_run_at',
      key: 'last_run_at',
      width: 160,
      render: (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '最近状态',
      dataIndex: 'last_run_status',
      key: 'last_run_status',
      width: 100,
      render: (value) => {
        if (!value) return '-';
        const color = value === 'ERROR' ? 'error' : value === 'SUCCESS' ? 'success' : 'blue';
        return <Tag color={color}>{value}</Tag>;
      },
    },
    {
      title: renderActionTitle,
      key: 'action',
      fixed: 'right',
      width: 110,
      render: (_, record) => (
        <Space size={8}>
          <Button
            icon={<EditOutlined />}
            size="small"
            disabled={isManualRunBusy}
            onClick={renderActionButton(() => openConfig(record))}
          />
          <Button
            icon={<HistoryOutlined />}
            size="small"
            disabled={isManualRunBusy}
            onClick={renderActionButton(() => openConfig(record, 'logs'))}
          />
          <Button
            icon={<PlayCircleOutlined />}
            size="small"
            loading={manualLoadingId === record.id}
            disabled={isManualRunBusy && manualLoadingId !== record.id}
            onClick={renderActionButton(() => handleManualRun(record))}
          />
          <Popconfirm
            title="删除配置"
            description="确认删除这条策略配置和对应日志吗？"
            okText="删除"
            cancelText="取消"
            onConfirm={(event) => {
              event?.stopPropagation?.();
              handleDelete(record);
            }}
          >
            <Button
              icon={<DeleteOutlined />}
              size="small"
              danger
              disabled={isManualRunBusy}
              onClick={(event) => event.stopPropagation()}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const logColumns = [
    {
      title: '时间',
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 160,
      render: (value) => dayjs(value).format('YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '来源',
      dataIndex: 'trigger_source',
      key: 'trigger_source',
      width: 90,
      render: (value) => <Tag color={value === 'manual' ? 'gold' : 'blue'}>{value}</Tag>,
    },
    {
      title: '动作',
      dataIndex: 'action',
      key: 'action',
      width: 90,
      render: (value) => {
        let color = 'default';
        if (value === 'BUY') color = 'red';
        if (value === 'SELL') color = 'green';
        if (value === 'ERROR') color = 'error';
        if (value === 'CHECK') color = 'blue';
        return <Tag color={color}>{value}</Tag>;
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value) => <Tag color={value === 'SUCCESS' ? 'success' : value === 'ERROR' ? 'error' : 'default'}>{value}</Tag>,
    },
    {
      title: '价格',
      dataIndex: 'price',
      key: 'price',
      width: 90,
      render: (value) => (value ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '数量',
      dataIndex: 'quantity',
      key: 'quantity',
      width: 80,
      render: (value) => value || '-',
    },
    {
      title: '信号分(CNN)',
      dataIndex: 'fear_score',
      key: 'fear_score',
      width: 100,
      render: (value) => (value !== null && value !== undefined ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '投影量比',
      dataIndex: 'volume_ratio',
      key: 'volume_ratio',
      width: 90,
      render: (value) => (value !== null && value !== undefined ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '详情',
      dataIndex: 'message',
      key: 'message',
    },
  ];

  const renderConfigForm = () => (
    <Form
      form={form}
      layout="vertical"
      initialValues={defaultValues}
      onFinish={handleSave}
    >
      <Form.Item name="enabled" label="启用策略" valuePropName="checked">
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>

      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Form.Item name="symbol" label="交易标的" rules={[{ required: true, message: '请输入交易标的' }]}>
            <Input placeholder="SOXL.US" />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="account_type" label="账户类型" rules={[{ required: true, message: '请选择账户类型' }]}>
            <Select
              options={accountTypeOptions}
              onChange={() => {
                form.setFieldsValue({
                  ib_account_id: undefined,
                  longport_account_id: undefined,
                  external_trading_account_id: undefined,
                  live_sub_account_id: undefined,
                });
                setLiveSubAccounts([]);
              }}
            />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item noStyle shouldUpdate={(prev, curr) => prev.account_type !== curr.account_type}>
            {() => {
              const accountType = form.getFieldValue('account_type');
              if (accountType === 'external') {
                return (
                  <>
                    <Form.Item name="external_trading_account_id" label="外部交易账户" rules={[{ required: true, message: '请选择外部交易账户' }]}>
                      <Select
                        allowClear
                        showSearch
                        optionFilterProp="label"
                        options={externalTradingAccountOptions}
                        placeholder="选择美股外部交易账户"
                        onChange={() => form.setFieldsValue({ live_sub_account_id: undefined })}
                      />
                    </Form.Item>
                    <Form.Item name="live_sub_account_id" label="虚拟子账户" rules={[{ required: true, message: '请选择虚拟子账户' }]}>
                      <Select
                        allowClear
                        showSearch
                        optionFilterProp="label"
                        options={liveSubAccountOptions}
                        placeholder={selectedExternalTradingAccountId ? '选择虚拟子账户' : '先选择外部账户'}
                        disabled={!selectedExternalTradingAccountId}
                      />
                    </Form.Item>
                  </>
                );
              }
              if (accountType === 'longport') {
                return (
                  <Form.Item name="longport_account_id" label="长桥账户" rules={[{ required: true, message: '请选择长桥账户' }]}>
                    <Select placeholder="选择长桥账户">
                      {longportAccounts.map((account) => (
                        <Select.Option key={account.lp_account_id} value={account.lp_account_id}>
                          {account.name} (ID: {account.lp_account_id})
                        </Select.Option>
                      ))}
                    </Select>
                  </Form.Item>
                );
              }
              return (
                <Form.Item name="ib_account_id" label="IB 账户" rules={[{ required: true, message: '请选择 IB 账户' }]}>
                  <Select placeholder="选择 IB 账户">
                    {ibAccounts.map((account) => (
                      <Select.Option key={account.id} value={account.id}>
                        {account.name} (ID: {account.id}, Port: {account.ib_port})
                      </Select.Option>
                    ))}
                  </Select>
                </Form.Item>
              );
            }}
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Form.Item name="buy_threshold" label="买入触发阈值(<=)" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="greed_threshold" label="进入止盈区阈值(>=)" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="volume_ratio_threshold" label="投影量比阈值" rules={[{ required: true }]}>
            <InputNumber min={0} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Form.Item name="buy_position_pct" label="每次买入仓位%" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="cooldown_days" label="冷却天数" rules={[{ required: true }]}>
            <InputNumber min={0} max={60} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="trailing_stop_pct" label="移动止盈回撤%" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Form.Item name="sell_position_pct" label="止盈减仓%" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="sell_reduction_basis" label="止盈减仓口径" rules={[{ required: true }]}>
            <Select options={sellReductionBasisOptions} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="max_take_profit_sells_per_cycle" label="同轮止盈最多卖出次数" rules={[{ required: true }]}>
            <InputNumber min={1} max={20} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Form.Item name="min_position_pct_after_take_profit" label="止盈后最低保留仓位%" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col xs={24} md={8}>
          <Form.Item name="rebalance_threshold_pct" label="调仓阈值%" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>

      <Divider />
      <Row gutter={[16, 8]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={8}>
          <Text type="secondary">最近执行时间</Text>
          <div>{selectedConfig?.last_run_at ? dayjs(selectedConfig.last_run_at).format('YYYY-MM-DD HH:mm:ss') : '-'}</div>
        </Col>
        <Col xs={24} md={8}>
          <Text type="secondary">最近执行状态</Text>
          <div>{selectedConfig?.last_run_status || '-'}</div>
        </Col>
        <Col xs={24} md={8}>
          <Text type="secondary">最近执行说明</Text>
          <div>{selectedConfig?.last_run_message || '-'}</div>
        </Col>
      </Row>

      <Form.Item>
        <Space wrap>
          <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={configLoading}>
            保存配置
          </Button>
          <Button icon={<FileSearchOutlined />} onClick={handleBacktest}>
            回测
          </Button>
          <Button
            icon={<PlayCircleOutlined />}
            onClick={() => handleManualRun(selectedConfig)}
            loading={manualLoadingId === selectedConfig?.id}
            disabled={!selectedConfig?.id || isManualRunBusy}
          >
            立即执行一次
          </Button>
        </Space>
      </Form.Item>
    </Form>
  );

  const renderLogs = () => {
    if (!selectedConfig?.id) {
      return <Empty description="保存配置后查看日志" />;
    }

    return (
      <>
        <Table
          columns={logColumns}
          dataSource={logs}
          rowKey="id"
          loading={logLoading}
          pagination={{ defaultPageSize: 10 }}
          scroll={{ x: 1400 }}
        />
        <Button
          style={{ marginTop: 16 }}
          icon={<ReloadOutlined />}
          onClick={() => fetchLogs()}
          loading={logLoading}
          disabled={isManualRunBusy}
        >
          刷新日志
        </Button>
      </>
    );
  };

  const renderStateValue = (value, formatter) => {
    if (value === null || value === undefined || value === '') return '-';
    return formatter ? formatter(value) : value;
  };

  const renderState = () => {
    if (!selectedConfig?.id) {
      return <Empty description="保存配置后查看状态" />;
    }

    return (
      <>
        <Space style={{ width: '100%', justifyContent: 'flex-end', marginBottom: 16 }} wrap>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => fetchState()}
            loading={stateLoading}
            disabled={isManualRunBusy}
          >
            刷新状态
          </Button>
        </Space>
        <Descriptions
          bordered
          size="small"
          column={{ xs: 1, sm: 2, lg: 3 }}
          style={{ marginBottom: 16 }}
        >
          <Descriptions.Item label="状态行">
            <Tag color={strategyState?.has_state ? 'success' : 'default'}>
              {strategyState?.has_state ? '已创建' : '未创建'}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="配置ID">{selectedConfig.id}</Descriptions.Item>
          <Descriptions.Item label="标的">{strategyState?.symbol || selectedConfig.symbol || '-'}</Descriptions.Item>
          <Descriptions.Item label="最近处理交易日">
            {renderStateValue(strategyState?.last_processed_date)}
          </Descriptions.Item>
          <Descriptions.Item label="剩余冷却交易日">
            {renderStateValue(strategyState?.cooldown_remaining_days)}
          </Descriptions.Item>
          <Descriptions.Item label="止盈峰值">
            {renderStateValue(strategyState?.greed_peak_price, (value) => Number(value).toFixed(4))}
          </Descriptions.Item>
          <Descriptions.Item label="本轮止盈次数">
            {renderStateValue(strategyState?.take_profit_cycle_sell_count)}
          </Descriptions.Item>
          <Descriptions.Item label="更新时间">
            {renderStateValue(strategyState?.updated_at, (value) => dayjs(value).format('YYYY-MM-DD HH:mm:ss'))}
          </Descriptions.Item>
        </Descriptions>

        <Form
          form={stateForm}
          layout="vertical"
          onFinish={handleSaveState}
          initialValues={{
            last_processed_date: null,
            cooldown_remaining_days: 0,
            greed_peak_price: null,
            take_profit_cycle_sell_count: 0,
          }}
        >
          <Row gutter={16}>
            <Col xs={24} md={6}>
              <Form.Item name="last_processed_date" label="最近处理交易日">
                <DatePicker allowClear style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item
                name="cooldown_remaining_days"
                label="剩余冷却交易日"
                rules={[{ required: true, message: '请输入剩余冷却交易日' }]}
              >
                <InputNumber min={0} max={60} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item name="greed_peak_price" label="止盈峰值价格">
                <InputNumber min={0} precision={4} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item
                name="take_profit_cycle_sell_count"
                label="本轮止盈次数"
                rules={[{ required: true, message: '请输入本轮止盈次数' }]}
              >
                <InputNumber min={0} max={20} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item>
            <Space wrap>
              <Button
                type="primary"
                htmlType="submit"
                icon={<SaveOutlined />}
                loading={stateSaving}
                disabled={isManualRunBusy}
              >
                保存状态
              </Button>
              <Button
                icon={<ReloadOutlined />}
                onClick={() => fetchState()}
                loading={stateLoading}
                disabled={isManualRunBusy}
              >
                重新加载
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </>
    );
  };

  const renderList = () => (
    <Card>
      {!embedded && <Title level={4} style={{ margin: '0 0 16px' }}>情绪量能策略</Title>}
      <Table
        columns={configColumns}
        dataSource={configs}
        rowKey="id"
        loading={listLoading}
        pagination={{ defaultPageSize: 10 }}
        scroll={{ x: 1500 }}
        onRow={(record) => ({
          onClick: () => openConfig(record),
          style: { cursor: 'pointer' },
        })}
      />
    </Card>
  );

  const renderDetail = () => (
    <>
      <Space className="soxl-fear-detail-header" style={{ width: '100%', justifyContent: 'space-between', marginBottom: embedded ? 12 : 16 }} wrap>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={returnToList}>
            返回列表
          </Button>
          <Title level={4} style={{ margin: 0 }}>
            {selectedConfig?.id ? `${selectedConfig.symbol} 策略配置` : '新增策略配置'}
          </Title>
        </Space>
        {selectedConfig?.id && (
          <Text type="secondary">
            {accountTypeLabels[selectedConfig.account_type] || selectedConfig.account_type} / {getAccountLabel(selectedConfig)}
          </Text>
        )}
      </Space>
      <Card className="soxl-fear-detail-card" loading={configLoading && activeTab === 'config'}>
        <Tabs
          className="soxl-fear-detail-tabs"
          activeKey={activeTab}
          onChange={(key) => {
            setActiveTab(key);
            if (key === 'logs') {
              fetchLogs();
            }
            if (key === 'state') {
              fetchState();
            }
          }}
          items={[
            {
              key: 'config',
              label: <span className="soxl-fear-detail-tab-label"><SettingOutlined />策略配置</span>,
              children: renderConfigForm(),
            },
            {
              key: 'logs',
              label: <span className="soxl-fear-detail-tab-label"><HistoryOutlined />运行日志</span>,
              disabled: !selectedConfig?.id,
              children: renderLogs(),
            },
            {
              key: 'state',
              label: <span className="soxl-fear-detail-tab-label"><DatabaseOutlined />状态</span>,
              disabled: !selectedConfig?.id,
              children: renderState(),
            },
          ]}
        />
      </Card>
    </>
  );

  return (
    <div className={`soxl-fear-page${embedded ? ' is-embedded' : ''}`}>
      {viewMode === 'list' ? renderList() : renderDetail()}
    </div>
  );
};

export default SoxlFearStrategy;
