import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
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
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RestOutlined,
} from '@ant-design/icons';
import request from '../utils/request';
import ExternalLedgerPositionsTable from '../components/ExternalLedgerPositionsTable';

const { Text } = Typography;

const DEFAULT_VALUES = {
  name: '纳指100估值成长模拟盘',
  enabled: false,
  universe_tag_ids: [],
  min_market_cap_100m: 100,
  max_market_cap_100m: null,
  max_positions: 5,
  external_trading_account_id: null,
  live_sub_account_id: null,
  trigger_time: '18:00',
  trigger_timezone: 'America/New_York',
  undervalue_threshold: 0.9,
  next_fy_growth_threshold: 1.1,
  ema_window: 120,
  price_below_ema_pct: 10,
  volume_lookback_days: 20,
  volume_consecutive_days: 3,
  volume_ratio_threshold: 1.4,
  trailing_stop_pct: 5,
  trailing_stop_atr_window: 20,
  trailing_stop_atr_multiple: 2.5,
  stale_high_days: 5,
};

const TIMEZONE_OPTIONS = [
  { label: '美东时区', value: 'America/New_York' },
  { label: '上海时区', value: 'Asia/Shanghai' },
];

const formatNumber = (value, precision = 2) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return number.toLocaleString(undefined, {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  });
};

const formatMoney = value => {
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return `$${number.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

const formatPct = (value, precision = 2) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return `${number.toFixed(precision)}%`;
};

const formatTime = value => {
  if (!value) return '-';
  return String(value).replace('T', ' ').slice(0, 19);
};

const statusColor = status => (status === 'OK' ? 'green' : status === 'SKIPPED' ? 'gold' : status === 'ERROR' ? 'red' : 'default');
const VALUATION_SIM_STRATEGY_TYPE = 'valuation_sim';
const MARKET_TYPE_US_STOCK = 'US_STOCK';
const isUsStockExternalAccount = account => String(account?.market_type || '').toUpperCase() === MARKET_TYPE_US_STOCK;

const isCurrentValuationSimSubAccount = (subAccount, configId) => (
  Boolean(
    configId
    && subAccount?.strategy_type === VALUATION_SIM_STRATEGY_TYPE
    && Number(subAccount?.strategy_config_id) === Number(configId),
  )
);

const isAvailableValuationSimSubAccount = (subAccount, configId = null) => (
  Boolean(
    subAccount?.enabled
    && (
      subAccount?.binding_status === 'FREE'
      || (!subAccount?.strategy_type && !subAccount?.strategy_config_id)
      || isCurrentValuationSimSubAccount(subAccount, configId)
    ),
  )
);

const formatSubAccountOptionLabel = (subAccount, configId = null) => {
  const name = subAccount?.name || subAccount?.id || '-';
  if (!subAccount?.enabled) return `${name}（停用）`;
  if (isCurrentValuationSimSubAccount(subAccount, configId)) return `${name}（当前配置）`;
  if (isAvailableValuationSimSubAccount(subAccount, configId)) return name;
  return `${name}（已占用：${subAccount?.binding_label || subAccount?.strategy_name || subAccount?.strategy_type || '其他策略'}）`;
};

const ValuationSimulation = () => {
  const [form] = Form.useForm();
  const [configs, setConfigs] = useState([]);
  const [selectedConfigId, setSelectedConfigId] = useState(null);
  const [positions, setPositions] = useState([]);
  const [positionRefreshToken, setPositionRefreshToken] = useState(0);
  const [logs, setLogs] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [candidateMeta, setCandidateMeta] = useState({});
  const [tagOptions, setTagOptions] = useState([]);
  const [externalTradingAccounts, setExternalTradingAccounts] = useState([]);
  const [externalTradingSubAccounts, setExternalTradingSubAccounts] = useState([]);
  const [externalTradingAccountsLoading, setExternalTradingAccountsLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [candidateLoading, setCandidateLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingConfigId, setEditingConfigId] = useState(null);
  const detailRequestIdRef = useRef(0);

  const selectedConfig = useMemo(
    () => configs.find(item => item.id === selectedConfigId) || null,
    [configs, selectedConfigId],
  );
  const selectedConfigExternalAccount = useMemo(
    () => externalTradingAccounts.find(item => Number(item.id) === Number(selectedConfig?.external_trading_account_id)) || null,
    [externalTradingAccounts, selectedConfig?.external_trading_account_id],
  );
  const selectedConfigMarketType = selectedConfigExternalAccount?.market_type || MARKET_TYPE_US_STOCK;
  const selectedExternalTradingAccountId = Form.useWatch('external_trading_account_id', form);
  const hasSelectedExternalTradingAccount = Boolean(selectedExternalTradingAccountId);

  const loadedPositionValue = positions.reduce((sum, item) => sum + (Number(item.market_value) || 0), 0);
  const configPositionValue = Number(selectedConfig?.external_position_value);
  const positionValue = Number.isFinite(configPositionValue) ? configPositionValue : loadedPositionValue;
  const totalEquity = selectedConfig?.external_net_asset
    ?? ((Number(selectedConfig?.external_cash_available) || 0) + positionValue);
  const baselineEquity = Number(selectedConfig?.external_cash_allocated)
    || Number(selectedConfig?.external_net_asset)
    || 0;
  const totalReturnPct = baselineEquity > 0 ? ((totalEquity / baselineEquity) - 1) * 100 : null;
  const tagNameMap = useMemo(() => {
    const map = {};
    tagOptions.forEach(item => {
      map[item.value] = item.rawName || item.label;
    });
    return map;
  }, [tagOptions]);
  const renderUniverseTags = record => {
    const ids = Array.isArray(record?.universe_tag_ids) ? record.universe_tag_ids : [];
    if (!ids.length) return '默认Nasdaq 100+';
    return ids.map(id => tagNameMap[id] || id).join(' / ');
  };

  const loadConfigs = useCallback(async (preferredId) => {
    setLoading(true);
    try {
      const { data } = await request.get('/api/valuation-sim/configs');
      const rows = data || [];
      setConfigs(rows);
      setSelectedConfigId(currentId => {
        const nextId = preferredId || currentId || rows[0]?.id || null;
        return rows.some(item => item.id === nextId) ? nextId : rows[0]?.id || null;
      });
    } catch (error) {
      message.error('加载估值模拟盘配置失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTags = useCallback(async () => {
    try {
      const { data } = await request.get('/api/evc/tags');
      setTagOptions((data || []).map(tag => ({
        label: tag.stock_count ? `${tag.name} (${tag.stock_count})` : tag.name,
        rawName: tag.name,
        value: tag.id,
      })));
    } catch (error) {
      message.error('加载估值标签失败');
    }
  }, []);

  const loadExternalTradingAccounts = useCallback(async () => {
    setExternalTradingAccountsLoading(true);
    try {
      const { data } = await request.get('/api/external-trading-accounts');
      setExternalTradingAccounts(Array.isArray(data) ? data : []);
    } catch (error) {
      message.error(error?.response?.data?.detail || '加载外部交易账户失败');
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
      message.error(error?.response?.data?.detail || '加载外部交易子账户失败');
      setExternalTradingSubAccounts([]);
    }
  }, []);

  const loadDetails = useCallback(async (configId) => {
    if (!configId) {
      setPositions([]);
      setLogs([]);
      setCandidates([]);
      setCandidateMeta({});
      setPositionRefreshToken(v => v + 1);
      return;
    }
    const requestId = ++detailRequestIdRef.current;
    setDetailLoading(true);
    setCandidateLoading(true);
    try {
      setPositionRefreshToken(v => v + 1);
      const [logResp, candidateResp] = await Promise.all([
        request.get(`/api/valuation-sim/configs/${configId}/logs`),
        request.get(`/api/valuation-sim/configs/${configId}/candidates?limit=50`),
      ]);
      if (detailRequestIdRef.current !== requestId) return;
      setLogs(logResp.data || []);
      setCandidates(candidateResp.data?.candidates || []);
      setCandidateMeta(candidateResp.data || {});
    } catch (error) {
      if (detailRequestIdRef.current === requestId) {
        message.error('加载估值模拟盘明细失败');
      }
    } finally {
      if (detailRequestIdRef.current === requestId) {
        setDetailLoading(false);
        setCandidateLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    loadConfigs();
    loadTags();
    loadExternalTradingAccounts();
  }, [loadConfigs, loadTags, loadExternalTradingAccounts]);

  useEffect(() => {
    loadDetails(selectedConfigId);
  }, [loadDetails, selectedConfigId]);

  useEffect(() => {
    if (!selectedExternalTradingAccountId) {
      setExternalTradingSubAccounts([]);
      return;
    }
    loadExternalTradingSubAccounts(selectedExternalTradingAccountId);
  }, [selectedExternalTradingAccountId, loadExternalTradingSubAccounts]);

  const refreshAll = async (preferredId = selectedConfigId) => {
    await loadConfigs(preferredId);
  };

  const handlePositionDataChange = useCallback(({ rows: nextRows = [] }) => {
    setPositions(nextRows);
  }, []);

  const openCreateModal = () => {
    setEditingConfigId(null);
    form.setFieldsValue(DEFAULT_VALUES);
    setExternalTradingSubAccounts([]);
    setModalOpen(true);
  };

  const openEditModal = () => {
    if (!selectedConfig) return;
    setEditingConfigId(selectedConfig.id);
    form.setFieldsValue({ ...DEFAULT_VALUES, ...selectedConfig });
    setModalOpen(true);
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      if (
        values.min_market_cap_100m !== null && values.min_market_cap_100m !== undefined &&
        values.max_market_cap_100m !== null && values.max_market_cap_100m !== undefined &&
        Number(values.min_market_cap_100m) > Number(values.max_market_cap_100m)
      ) {
        message.error('市值下限不能大于上限');
        return;
      }
      setSaving(true);
      const payload = {
        ...DEFAULT_VALUES,
        ...values,
        external_trading_account_id: values.external_trading_account_id ? Number(values.external_trading_account_id) : null,
        live_sub_account_id: values.live_sub_account_id ? Number(values.live_sub_account_id) : null,
      };
      const response = editingConfigId
        ? await request.put(`/api/valuation-sim/configs/${editingConfigId}`, payload)
        : await request.post('/api/valuation-sim/configs', payload);
      message.success('配置已保存');
      setModalOpen(false);
      await refreshAll(response.data?.id);
    } catch (error) {
      if (!error?.errorFields) {
        message.error(error?.response?.data?.detail || '保存失败');
      }
    } finally {
      setSaving(false);
    }
  };

  const handleRun = async () => {
    if (!selectedConfigId) return;
    setRunning(true);
    try {
      const { data } = await request.post(`/api/valuation-sim/configs/${selectedConfigId}/run`);
      message.success(data?.message || '已同步到外部账户');
      await refreshAll(selectedConfigId);
    } catch (error) {
      message.error(error?.response?.data?.detail || '运行失败');
    } finally {
      setRunning(false);
    }
  };

  const handleReset = async () => {
    if (!selectedConfigId) return;
    setRunning(true);
    try {
      await request.post(`/api/valuation-sim/configs/${selectedConfigId}/reset`);
      message.success('模拟盘策略输出和日志已重置');
      await refreshAll(selectedConfigId);
    } catch (error) {
      message.error(error?.response?.data?.detail || '重置失败');
    } finally {
      setRunning(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedConfigId) return;
    setRunning(true);
    try {
      await request.delete(`/api/valuation-sim/configs/${selectedConfigId}`);
      message.success('配置已删除');
      await loadConfigs();
    } catch (error) {
      message.error(error?.response?.data?.detail || '删除失败');
    } finally {
      setRunning(false);
    }
  };

  const externalTradingAccountOptions = useMemo(() => (
    externalTradingAccounts.map(item => {
      const disabledReason = item.enabled === false ? '（停用）' : !isUsStockExternalAccount(item) ? '（非美股）' : '';
      return {
        label: `${item.name || item.identifier || item.id}${disabledReason}`,
        value: item.id,
        disabled: Boolean(disabledReason),
      };
    })
  ), [externalTradingAccounts]);

  const externalTradingSubAccountOptions = useMemo(() => (
    externalTradingSubAccounts.map(item => ({
      label: formatSubAccountOptionLabel(item, editingConfigId),
      value: item.id,
      disabled: !isAvailableValuationSimSubAccount(item, editingConfigId),
    }))
  ), [externalTradingSubAccounts, editingConfigId]);

  const configColumns = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      fixed: 'left',
      width: 220,
      render: (value, record) => (
        <Button type="link" size="small" onClick={() => setSelectedConfigId(record.id)}>
          {value}
        </Button>
      ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 80,
      render: value => <Tag color={value ? 'green' : 'default'}>{value ? '启用' : '停用'}</Tag>,
    },
    {
      title: '账户',
      dataIndex: 'account_source',
      key: 'account_source',
      width: 90,
      render: () => <Tag color="blue">外部</Tag>,
    },
    {
      title: '外部账户',
      dataIndex: 'external_trading_account_id',
      key: 'external_trading_account_id',
      width: 160,
      render: (value, record) => record.external_trading_account_name || value || '-',
    },
    {
      title: '子账户',
      dataIndex: 'live_sub_account_id',
      key: 'live_sub_account_id',
      width: 140,
      render: (value, record) => record.live_sub_account_name || value || '-',
    },
    { title: '现金', dataIndex: 'external_cash_available', key: 'external_cash_available', width: 120, render: formatMoney },
    { title: '净资产', dataIndex: 'external_net_asset', key: 'external_net_asset', width: 120, render: formatMoney },
    { title: '持仓数', dataIndex: 'max_positions', key: 'max_positions', width: 80 },
    {
      title: '触发',
      key: 'trigger',
      width: 170,
      render: (_, record) => `${record.trigger_timezone || '-'} ${record.trigger_time || '-'}`,
    },
    {
      title: '核心阈值',
      key: 'thresholds',
      width: 260,
      render: (_, record) => (
        <Space size={4} wrap>
          <Tag>估值x{formatNumber(record.undervalue_threshold, 2)}</Tag>
          <Tag>增长x{formatNumber(record.next_fy_growth_threshold, 2)}</Tag>
          <Tag>量比x{formatNumber(record.volume_ratio_threshold, 2)}</Tag>
          <Tag>ATR{record.trailing_stop_atr_window || 20} x{formatNumber(record.trailing_stop_atr_multiple || 2.5, 2)}</Tag>
          <Tag>市值{record.min_market_cap_100m || 0}-{record.max_market_cap_100m || '∞'}亿</Tag>
          <Tag>{renderUniverseTags(record)}</Tag>
        </Space>
      ),
    },
    {
      title: '最近运行',
      key: 'last_run',
      width: 260,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Space size={4}>
            <Tag color={statusColor(record.last_run_status)}>{record.last_run_status || '-'}</Tag>
            <span>{record.last_run_date || '-'}</span>
          </Space>
          <Text type="secondary" ellipsis style={{ maxWidth: 230 }}>{record.last_run_message || '-'}</Text>
        </Space>
      ),
    },
  ];

  const candidateColumns = [
    { title: '股票', dataIndex: 'symbol', key: 'symbol', fixed: 'left', width: 100 },
    { title: '公司', dataIndex: 'company', key: 'company', width: 160, ellipsis: true },
    { title: '价格', dataIndex: 'price', key: 'price', width: 90, render: formatMoney },
    { title: '市值(亿美元)', dataIndex: 'market_cap_100m', key: 'market_cap_100m', width: 110, render: value => formatNumber(value, 1) },
    { title: '估值下限', dataIndex: 'fair_value_lo', key: 'fair_value_lo', width: 100, render: formatMoney },
    { title: '低估率', dataIndex: 'undervalue_pct', key: 'undervalue_pct', width: 100, render: formatPct },
    { title: '增长下限', dataIndex: 'next_fy_growth_lo_pct', key: 'next_fy_growth_lo_pct', width: 100, render: formatPct },
    { title: 'EMA偏离', dataIndex: 'price_vs_ema_pct', key: 'price_vs_ema_pct', width: 100, render: formatPct },
    { title: 'ATRP', dataIndex: 'atrp_pct', key: 'atrp_pct', width: 90, render: formatPct },
    { title: '量比', dataIndex: 'volume_ratio', key: 'volume_ratio', width: 90, render: value => formatNumber(value, 2) },
    { title: '估值日', dataIndex: 'valuation_date', key: 'valuation_date', width: 110 },
  ];

  const logColumns = [
    { title: '时间', dataIndex: 'timestamp', key: 'timestamp', width: 170, render: formatTime },
    { title: '来源', dataIndex: 'trigger_source', key: 'trigger_source', width: 80 },
    { title: '状态', dataIndex: 'status', key: 'status', width: 90, render: value => <Tag color={statusColor(value)}>{value}</Tag> },
    { title: '交易日', dataIndex: 'trade_date', key: 'trade_date', width: 110 },
    { title: '候选', dataIndex: 'candidate_count', key: 'candidate_count', width: 80 },
    { title: '买入', dataIndex: 'buy_count', key: 'buy_count', width: 80 },
    { title: '卖出', dataIndex: 'sell_count', key: 'sell_count', width: 80 },
    { title: '消息', dataIndex: 'message', key: 'message', ellipsis: true },
  ];

  return (
    <Spin spinning={loading}>
      <Modal
        title={editingConfigId ? '编辑估值模拟盘' : '添加估值模拟盘'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        width={980}
        destroyOnClose={false}
        maskClosable={false}
        footer={(
          <Space>
            <Button onClick={() => setModalOpen(false)}>取消</Button>
            <Button type="primary" loading={saving} onClick={handleSave}>保存</Button>
          </Space>
        )}
      >
        <Form form={form} layout="vertical" initialValues={DEFAULT_VALUES}>
          <Row gutter={[12, 8]}>
            <Col xs={24} md={10}>
              <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="enabled" label="启用" rules={[{ required: true }]}>
                <Select options={[{ label: '启用', value: true }, { label: '停用', value: false }]} />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="max_positions" label="最大持仓" rules={[{ required: true }]}>
                <InputNumber min={1} max={50} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={24} md={10}>
              <Form.Item name="external_trading_account_id" label="外部交易账户" rules={[{ required: true, message: '请选择外部交易账户' }]}>
                <Select
                  options={externalTradingAccountOptions}
                  loading={externalTradingAccountsLoading}
                  onChange={() => form.setFieldsValue({ live_sub_account_id: null })}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={10}>
              <Form.Item
                name="live_sub_account_id"
                label="外部交易子账户"
                rules={[{ required: true, message: '请选择外部交易子账户' }]}
              >
                <Select
                  options={externalTradingSubAccountOptions}
                  disabled={!hasSelectedExternalTradingAccount}
                  placeholder={hasSelectedExternalTradingAccount ? '请选择子账户' : '先选择外部账户'}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="universe_tag_ids" label="候选池标签">
                <Select
                  mode="multiple"
                  allowClear
                  showSearch
                  placeholder="不选则默认 Nasdaq 100+"
                  options={tagOptions}
                  maxTagCount="responsive"
                  optionFilterProp="label"
                />
              </Form.Item>
            </Col>
            <Col xs={12} md={5}>
              <Form.Item name="min_market_cap_100m" label="市值下限(亿美元)">
                <InputNumber min={0} step={10} precision={2} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={5}>
              <Form.Item name="max_market_cap_100m" label="市值上限(亿美元)">
                <InputNumber min={0} step={10} precision={2} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={5}>
              <Form.Item name="trigger_time" label="触发时间" rules={[{ required: true }]}>
                <Input placeholder="18:00" />
              </Form.Item>
            </Col>
            <Col xs={12} md={7}>
              <Form.Item name="trigger_timezone" label="触发时区" rules={[{ required: true }]}>
                <Select options={TIMEZONE_OPTIONS} showSearch />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="undervalue_threshold" label="低估系数" rules={[{ required: true }]}>
                <InputNumber min={0.01} max={2} step={0.01} precision={3} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="next_fy_growth_threshold" label="增长系数" rules={[{ required: true }]}>
                <InputNumber min={0.01} max={5} step={0.01} precision={3} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="ema_window" label="EMA窗口" rules={[{ required: true }]}>
                <InputNumber min={1} max={1000} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="price_below_ema_pct" label="低于EMA%" rules={[{ required: true }]}>
                <InputNumber min={0.01} max={99} step={0.5} precision={2} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="volume_lookback_days" label="量能均值天数" rules={[{ required: true }]}>
                <InputNumber min={1} max={250} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="volume_consecutive_days" label="连续放量天数" rules={[{ required: true }]}>
                <InputNumber min={1} max={20} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="volume_ratio_threshold" label="量比阈值" rules={[{ required: true }]}>
                <InputNumber min={0.01} max={20} step={0.05} precision={3} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="trailing_stop_atr_window" label="ATR窗口" rules={[{ required: true }]}>
                <InputNumber min={1} max={250} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="trailing_stop_atr_multiple" label="ATR倍数" rules={[{ required: true }]}>
                <InputNumber min={0.1} max={20} step={0.1} precision={2} className="factor-lab-full" />
              </Form.Item>
            </Col>
            <Col xs={12} md={4}>
              <Form.Item name="stale_high_days" label="未创新高天数" rules={[{ required: true }]}>
                <InputNumber min={1} max={250} className="factor-lab-full" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      <Row gutter={[12, 12]}>
        <Col xs={24}>
          <Card
            title="估值成长模拟盘"
            bordered={false}
            extra={(
              <Space wrap>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => refreshAll()}
                  loading={loading || detailLoading || candidateLoading}
                  disabled={loading || detailLoading || candidateLoading}
                />
                <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>添加模拟盘</Button>
                <Button icon={<EditOutlined />} onClick={openEditModal} disabled={!selectedConfig}>编辑</Button>
                <Button
                  icon={<PlayCircleOutlined />}
                  onClick={handleRun}
                  loading={running}
                  disabled={!selectedConfig || running || loading}
                />
                <Popconfirm
                  title="重置后会清空策略输出和运行日志，不会影响外部账户持仓或历史记录"
                  onConfirm={handleReset}
                  disabled={!selectedConfig || running || loading}
                >
                  <Button icon={<RestOutlined />} disabled={!selectedConfig || running || loading}>
                    重置
                  </Button>
                </Popconfirm>
                <Popconfirm title="删除该模拟盘及全部记录？" onConfirm={handleDelete} disabled={!selectedConfig || running || loading}>
                  <Button danger icon={<DeleteOutlined />} disabled={!selectedConfig || running || loading}>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            )}
          >
            <div className="valuation-sim-table">
              <Table
                rowKey="id"
                size="small"
                columns={configColumns}
                dataSource={configs}
                pagination={false}
                scroll={{ x: 1760 }}
                rowClassName={row => (row.id === selectedConfigId ? 'factor-lab-table-row-selected' : '')}
                onRow={row => ({ onClick: () => setSelectedConfigId(row.id) })}
              />
            </div>
            {!configs.length && !loading ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> : null}
          </Card>
        </Col>
      </Row>

      {selectedConfig ? (
        <>
          <div className="factor-lab-metrics valuation-sim-metrics">
            <Statistic title="总权益" value={formatMoney(totalEquity)} />
            <Statistic title="现金" value={formatMoney(selectedConfig.external_cash_available)} />
            <Statistic title="持仓市值" value={formatMoney(positionValue)} />
            <Statistic title="收益率" value={formatPct(totalReturnPct)} />
            <Statistic title="候选数" value={candidates.length} />
          </div>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card
                title={`当前候选 ${candidateMeta.trade_date || ''}`}
                bordered={false}
                extra={<Text type="secondary">候选池 {candidateMeta.universe_count || 0} / 估值 {candidateMeta.valuation_count || 0}</Text>}
              >
                <Table
                  rowKey="symbol"
                  size="small"
                  loading={candidateLoading}
                  columns={candidateColumns}
                  dataSource={candidates}
                  pagination={false}
                  scroll={{ x: 980, y: 320 }}
                  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title="当前持仓" bordered={false}>
                <ExternalLedgerPositionsTable
                  showSubAccount={false}
                  marketType={selectedConfigMarketType}
                  accountId={selectedConfig?.external_trading_account_id}
                  subAccountId={selectedConfig?.live_sub_account_id}
                  reloadToken={positionRefreshToken}
                  enabled={Boolean(selectedConfig?.external_trading_account_id && selectedConfig?.live_sub_account_id)}
                  onDataChange={handlePositionDataChange}
                  defaultPageSize={200}
                  pagination={false}
                  scroll={{ y: 320 }}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[12, 12]} className="factor-lab-table-row">
            <Col xs={24}>
              <Card title="运行日志" bordered={false}>
                <Table
                  rowKey="id"
                  size="small"
                  loading={detailLoading}
                  columns={logColumns}
                  dataSource={logs}
                  pagination={false}
                  scroll={{ x: 1080, y: 320 }}
                  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                />
              </Card>
            </Col>
          </Row>
        </>
      ) : (
        <Card bordered={false} className="factor-lab-table-row">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
        </Card>
      )}
    </Spin>
  );
};

export default ValuationSimulation;
