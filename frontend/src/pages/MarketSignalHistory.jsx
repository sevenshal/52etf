import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    Alert,
    Button,
    Col,
    DatePicker,
    Empty,
    Form,
    Input,
    InputNumber,
    Layout,
    List,
    Row,
    Progress,
    Segmented,
    Space,
    Spin,
    Statistic,
    Switch,
    Table,
    Tabs,
    Tag,
    Tooltip,
    Typography,
    message,
} from 'antd';
import { LeftOutlined, PlayCircleOutlined, ReloadOutlined, SaveOutlined, SyncOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';

const { Header } = Layout;
const { Title, Text } = Typography;
const { RangePicker } = DatePicker;
const PAGE_SIZE = 20;
const DEFAULT_AUTO_SYNC_TIME = '15:58';
const AUTO_SYNC_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

const fallbackStrategies = [
    {
        id: 'v1',
        name: '200MA低位放量买点',
        directions: ['BUY'],
        summary: '低于200日均线、接近50日低点，并出现5日成交量抬升的低位买入信号。',
        params: [
            { key: 'below_200ma_ratio_thresh', label: '低于200MA', default: 0.1, min: 0, max: 0.8, step: 0.01, precision: 3 },
            { key: 'vol_5_std_thresh', label: '5日量Z', default: 1.0, min: -5, max: 10, step: 0.1, precision: 2 },
            { key: 'today_vol_std_thresh', label: '当日量Z上限', default: 0.5, min: -5, max: 10, step: 0.1, precision: 2 },
            { key: 'close_vs_low_50_ratio', label: '50日低点倍数', default: 1.1, min: 0.8, max: 2.0, step: 0.01, precision: 3 },
        ],
    },
    {
        id: 'v2',
        name: '涨跌幅企稳放量拐点',
        directions: ['BUY', 'SELL'],
        summary: '先出现大幅上涨或下跌，再经过企稳期，最后用放量确认买点或卖点。',
        params: [
            { key: 'price_change_ratio', label: '涨跌幅%', default: 30.0, min: 1, max: 200, step: 1, precision: 1 },
            { key: 'stabilization_period', label: '企稳K数', default: 10, min: 1, max: 120, step: 1, precision: 0 },
            { key: 'klines_volume_std_multiplier', label: '放量倍数', default: 2.0, min: 0, max: 10, step: 0.1, precision: 2 },
            { key: 'klines_volume_days', label: '量能窗口', default: 20, min: 2, max: 120, step: 1, precision: 0 },
        ],
    },
    {
        id: 'v3',
        name: '成交量趋势突破买点',
        directions: ['BUY'],
        summary: '近期成交量逐级高于中期和长期成交量，且价格站上均线的趋势买入信号。',
        params: [
            { key: 'recent_days', label: '近期量天数', default: 5, min: 1, max: 60, step: 1, precision: 0 },
            { key: 'mid_days', label: '中期量天数', default: 5, min: 1, max: 120, step: 1, precision: 0 },
            { key: 'long_days', label: '长期量天数', default: 100, min: 5, max: 300, step: 1, precision: 0 },
            { key: 'long_ratio_thresh', label: '长期量倍数', default: 2.0, min: 0, max: 10, step: 0.1, precision: 2 },
            { key: 'ma_days', label: '价格均线', default: 60, min: 5, max: 300, step: 1, precision: 0 },
        ],
    },
];

const parseSymbols = value => String(value || '').split(/[,\n;，；]+/).map(item => item.trim().toUpperCase()).filter(Boolean);
const formatPercent = (value, digits = 2) => (value === null || value === undefined ? '-' : `${Number(value).toFixed(digits)}%`);
const formatNumber = (value, digits = 2) => (value === null || value === undefined ? '-' : Number(value).toFixed(digits));
const formatMoney = (value, digits = 2) => (
    value === null || value === undefined ? '-' : Number(value || 0).toLocaleString(undefined, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    })
);
const formatDateTime = value => (value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-');
const formatDate = value => (value ? dayjs(value).format('YYYY-MM-DD') : '-');
const returnColor = value => {
    const numeric = Number(value || 0);
    if (numeric > 0) return '#3f8600';
    if (numeric < 0) return '#cf1322';
    return 'inherit';
};
const directionColor = direction => {
    if (direction === 'BUY') return 'green';
    if (direction === 'SELL') return 'red';
    return 'default';
};
const getErrorMessage = (error, fallback) => {
    const detail = error?.response?.data?.detail || error?.message;
    if (!detail) return fallback;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map(item => item?.msg || String(item)).join('；');
    return detail?.message || detail?.msg || fallback;
};

const getStrategyParamDefaults = (strategyId, strategyDefs = fallbackStrategies) => {
    const strategy = strategyDefs.find(item => item.id === strategyId) || strategyDefs[0] || fallbackStrategies[0];
    return (strategy?.params || []).reduce((acc, item) => {
        acc[item.key] = item.default;
        return acc;
    }, {});
};

const normalizeConfigForForm = (config, strategyDefs = fallbackStrategies) => ({
    ...config,
    start_date: config?.start_date ? dayjs(config.start_date) : dayjs('2023-01-01'),
    symbols_text: (config?.symbols || []).join(', '),
    auto_sync_enabled: config?.auto_sync_enabled ?? true,
    auto_sync_time: config?.auto_sync_time || DEFAULT_AUTO_SYNC_TIME,
    strategy_params: {
        ...getStrategyParamDefaults(config?.strategy_id || 'v1', strategyDefs),
        ...(config?.strategy_params || {}),
    },
});

const MarketSignalHistory = () => {
    const navigate = useNavigate();
    const [strategies, setStrategies] = useState(fallbackStrategies);
    const [configs, setConfigs] = useState([]);
    const [selectedConfig, setSelectedConfig] = useState(null);
    const [detail, setDetail] = useState(null);
    const [activeStrategy, setActiveStrategy] = useState('v1');
    const [directionFilter, setDirectionFilter] = useState('ALL');
    const [signals, setSignals] = useState([]);
    const [loading, setLoading] = useState(false);
    const [listLoading, setListLoading] = useState(false);
    const [detailLoading, setDetailLoading] = useState(false);
    const [configLoading, setConfigLoading] = useState(false);
    const [syncLoadingId, setSyncLoadingId] = useState(null);
    const [hasMore, setHasMore] = useState(true);
    const [page, setPage] = useState(1);
    const [backtestLoading, setBacktestLoading] = useState(false);
    const [backtestTaskId, setBacktestTaskId] = useState(null);
    const [backtestStatus, setBacktestStatus] = useState(null);
    const [backtestProgress, setBacktestProgress] = useState(0);
    const [backtestProgressText, setBacktestProgressText] = useState('');
    const [backtestResult, setBacktestResult] = useState(null);
    const [defaultSymbols, setDefaultSymbols] = useState([]);
    const [defaultSymbolsMeta, setDefaultSymbolsMeta] = useState(null);
    const [configForm] = Form.useForm();
    const [backtestForm] = Form.useForm();
    const containerRef = useRef(null);
    const backtestPollingRef = useRef(null);

    const activeStrategyDef = useMemo(
        () => strategies.find(item => item.id === activeStrategy) || strategies[0] || fallbackStrategies[0],
        [strategies, activeStrategy]
    );
    const directionOptions = useMemo(() => {
        const options = [{ label: '全部', value: 'ALL' }];
        (activeStrategyDef?.directions || []).forEach(direction => {
            options.push({ label: direction === 'BUY' ? '买入' : '卖出', value: direction });
        });
        return options;
    }, [activeStrategyDef]);

    const stopBacktestPolling = () => {
        if (backtestPollingRef.current) {
            clearTimeout(backtestPollingRef.current);
            backtestPollingRef.current = null;
        }
    };

    const pollBacktestJob = async (id) => {
        try {
            const { data } = await request.get(`/api/market_signal/backtest/jobs/${id}`, { timeout: 30000 });
            setBacktestStatus(data.status);
            setBacktestProgress(data.progress || 0);
            setBacktestProgressText(data.message || '');

            if (data.status === 'completed') {
                stopBacktestPolling();
                setBacktestLoading(false);
                setBacktestTaskId(id);
                setBacktestResult(data.result || null);
                message.success('回测完成');
                return;
            }

            if (data.status === 'failed') {
                stopBacktestPolling();
                setBacktestLoading(false);
                setBacktestTaskId(id);
                message.error(getErrorMessage({ response: { data: { detail: data.error } } }, '回测失败'));
                return;
            }

            backtestPollingRef.current = setTimeout(() => pollBacktestJob(id), 1200);
        } catch (error) {
            stopBacktestPolling();
            setBacktestLoading(false);
            setBacktestTaskId(null);
            message.error(getErrorMessage(error, '获取回测进度失败'));
        }
    };

    const renderStrategyParamFields = () => (activeStrategyDef?.params || []).map(param => (
        <Col xs={12} md={4} key={param.key}>
            <Form.Item label={param.label} name={['strategy_params', param.key]}>
                <InputNumber
                    min={param.min}
                    max={param.max}
                    step={param.step}
                    precision={param.precision}
                    style={{ width: '100%' }}
                />
            </Form.Item>
        </Col>
    ));

    const fetchStrategies = async () => {
        try {
            const { data } = await request.get('/api/market_signal/strategies');
            setStrategies(data.items || fallbackStrategies);
        } catch {
            setStrategies(fallbackStrategies);
        }
    };

    const fetchDefaultSymbols = async () => {
        try {
            const { data } = await request.get('/api/market_signal/default-symbols');
            const symbols = data.symbols || [];
            setDefaultSymbols(symbols);
            setDefaultSymbolsMeta(data);
            if (symbols.length > 0) {
                const symbolText = symbols.join(', ');
                const currentConfigSymbols = configForm.getFieldValue('symbols_text');
                if (!currentConfigSymbols) {
                    configForm.setFieldValue('symbols_text', symbolText);
                }
                const currentBacktestSymbols = backtestForm.getFieldValue('symbols');
                if (!currentBacktestSymbols) {
                    backtestForm.setFieldValue('symbols', symbolText);
                }
                const currentMaxSymbols = backtestForm.getFieldValue('max_symbols');
                if (!currentMaxSymbols || currentMaxSymbols === 30 || currentMaxSymbols === 700) {
                    backtestForm.setFieldValue('max_symbols', symbols.length);
                }
            }
        } catch (error) {
            console.warn('加载默认标的池失败', error);
        }
    };

    const fetchConfigs = async () => {
        setListLoading(true);
        try {
            const { data } = await request.get('/api/market_signal/configs');
            setConfigs(data || []);
            const nextSelected = selectedConfig
                ? (data || []).find(item => item.id === selectedConfig.id)
                : (data || [])[0];
            if (nextSelected) {
                setSelectedConfig(nextSelected);
                setActiveStrategy(nextSelected.strategy_id);
                configForm.setFieldsValue(normalizeConfigForForm(nextSelected, strategies));
                fetchDetail(nextSelected.id);
            }
        } catch (error) {
            message.error(getErrorMessage(error, '加载市场信号虚拟盘配置失败'));
        } finally {
            setListLoading(false);
        }
    };

    const fetchDetail = async (configId) => {
        if (!configId) {
            setDetail(null);
            return;
        }
        setDetailLoading(true);
        try {
            const { data } = await request.get(`/api/market_signal/configs/${configId}/detail`);
            setDetail(data);
            setSelectedConfig(data.config);
            setActiveStrategy(data.config.strategy_id);
            const normalized = normalizeConfigForForm(data.config, strategies);
            configForm.setFieldsValue(normalized);
        } catch (error) {
            message.error(getErrorMessage(error, '加载虚拟盘详情失败'));
        } finally {
            setDetailLoading(false);
        }
    };

    const fetchSignals = async (pageNum, replace = false) => {
        if (!replace && (!hasMore || loading)) return;
        setLoading(true);
        try {
            const params = { page: pageNum, page_size: PAGE_SIZE, strategy_id: activeStrategy };
            if (selectedConfig?.id) params.config_id = selectedConfig.id;
            if (directionFilter !== 'ALL') params.direction = directionFilter;
            const { data } = await request.get('/api/market_signal', { params });
            setSignals(prev => (replace ? data.items : [...prev, ...data.items]));
            setHasMore(data.items.length === PAGE_SIZE);
        } catch (error) {
            message.error(getErrorMessage(error, '获取信号历史失败'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchStrategies();
        fetchDefaultSymbols();
        fetchConfigs();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useEffect(() => () => stopBacktestPolling(), []);

    const fillDefaultSymbols = () => {
        if (!defaultSymbols.length) {
            message.warning('默认标的池还没有加载完成');
            return;
        }
        const symbolText = defaultSymbols.join(', ');
        configForm.setFieldValue('symbols_text', symbolText);
        backtestForm.setFieldValue('symbols', symbolText);
        backtestForm.setFieldValue('max_symbols', defaultSymbols.length);
        message.success(`已填入 ${defaultSymbols.length} 个 SPY/QQQ 并集标的`);
    };

    useEffect(() => {
        const normalizedSelected = selectedConfig ? normalizeConfigForForm(selectedConfig, strategies) : null;
        const nextParams = normalizedSelected?.strategy_id === activeStrategy
            ? normalizedSelected.strategy_params
            : getStrategyParamDefaults(activeStrategy, strategies);
        backtestForm.setFieldsValue({
            strategy_params: nextParams,
        });
    }, [activeStrategy, strategies, selectedConfig, backtestForm]);

    useEffect(() => {
        if (!selectedConfig) return;
        configForm.setFieldsValue(normalizeConfigForForm(selectedConfig, strategies));
    }, [selectedConfig, strategies, configForm]);

    useEffect(() => {
        if (!directionOptions.some(item => item.value === directionFilter)) {
            setDirectionFilter('ALL');
        }
    }, [directionOptions, directionFilter]);

    useEffect(() => {
        setSignals([]);
        setPage(1);
        setHasMore(true);
        setBacktestResult(null);
        fetchSignals(1, true);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeStrategy, directionFilter, selectedConfig?.id]);

    const handleScroll = () => {
        if (!containerRef.current) return;
        const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
        if (scrollHeight - scrollTop - clientHeight < 100 && hasMore && !loading) {
            const nextPage = page + 1;
            setPage(nextPage);
            fetchSignals(nextPage);
        }
    };

    const buildConfigPayload = values => ({
        strategy_id: activeStrategy,
        name: values.name,
        enabled: !!values.enabled,
        symbols: parseSymbols(values.symbols_text),
        initial_capital: values.initial_capital,
        start_date: values.start_date ? values.start_date.format('YYYY-MM-DD') : '2023-01-01',
        holding_days: values.holding_days,
        position_pct: values.position_pct,
        max_positions: values.max_positions,
        min_cash_pct: values.min_cash_pct,
        commission_pct: values.commission_pct,
        slippage_pct: values.slippage_pct,
        lot_size: values.lot_size,
        min_market_cap: values.min_market_cap,
        auto_sync_enabled: !!values.auto_sync_enabled,
        auto_sync_time: values.auto_sync_time || DEFAULT_AUTO_SYNC_TIME,
        strategy_params: values.strategy_params || {},
    });

    const saveConfig = async () => {
        if (!selectedConfig?.id) return;
        const values = await configForm.validateFields();
        setConfigLoading(true);
        try {
            const { data } = await request.put(`/api/market_signal/configs/${selectedConfig.id}`, buildConfigPayload(values));
            message.success('虚拟盘配置已保存');
            setSelectedConfig(data);
            await fetchConfigs();
            await fetchDetail(data.id);
        } catch (error) {
            message.error(getErrorMessage(error, '保存配置失败'));
        } finally {
            setConfigLoading(false);
        }
    };

    const syncConfig = async (record = selectedConfig) => {
        if (!record?.id) return;
        setSyncLoadingId(record.id);
        try {
            await request.post(`/api/market_signal/configs/${record.id}/sync`);
            message.success('虚拟盘同步完成');
            await fetchConfigs();
            await fetchDetail(record.id);
            fetchSignals(1, true);
        } catch (error) {
            message.error(getErrorMessage(error, '同步虚拟盘失败'));
            await fetchConfigs();
        } finally {
            setSyncLoadingId(null);
        }
    };

    const syncAll = async () => {
        setSyncLoadingId('all');
        try {
            const { data } = await request.post('/api/market_signal/configs/sync-enabled');
            if (data.errors?.length) {
                message.warning(`同步完成，${data.errors.length} 个配置失败`);
            } else {
                message.success(`同步完成：${data.synced?.length || 0} 个配置`);
            }
            await fetchConfigs();
            if (selectedConfig?.id) await fetchDetail(selectedConfig.id);
            fetchSignals(1, true);
        } catch (error) {
            message.error(getErrorMessage(error, '同步全部虚拟盘失败'));
        } finally {
            setSyncLoadingId(null);
        }
    };

    const runBacktest = async () => {
        const values = await backtestForm.validateFields();
        const symbols = parseSymbols(values.symbols);
        const payload = {
            strategy_id: activeStrategy,
            start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
            end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
            holding_days: values.holding_days,
            position_pct: values.position_pct,
            max_positions: values.max_positions,
            initial_capital: values.initial_capital,
            max_symbols: values.max_symbols,
            strategy_params: values.strategy_params || {},
        };
        if (symbols.length > 0) payload.symbols = symbols;
        setBacktestLoading(true);
        setBacktestTaskId(null);
        setBacktestStatus('pending');
        setBacktestProgress(0);
        setBacktestProgressText('等待启动');
        setBacktestResult(null);
        stopBacktestPolling();
        try {
            const { data } = await request.post('/api/market_signal/backtest', payload, { timeout: 60000 });
            setBacktestTaskId(data.task_id);
            setBacktestStatus(data.status);
            pollBacktestJob(data.task_id);
        } catch (error) {
            message.error(getErrorMessage(error, '回测失败'));
            setBacktestLoading(false);
        }
    };

    const selectConfig = record => {
        setSelectedConfig(record);
        setActiveStrategy(record.strategy_id);
        setDirectionFilter('ALL');
        fetchDetail(record.id);
    };

    const renderSignalText = item => {
        if (item.strategy_id === 'v2') {
            return <>信号价:{formatNumber(item.signal_price)}&nbsp;幅度超过{item.v2_price_change_ratio || 30}%&nbsp;企稳超过{item.v2_stabilization_period || 10}天</>;
        }
        if (item.strategy_id === 'v3') {
            return <>信号价:{formatNumber(item.signal_price)}&nbsp;连续放量且为上升趋势</>;
        }
        return (
            <>
                信号价:{formatNumber(item.signal_price)} 低于200MA比率:{formatPercent((item.below_200ma_ratio || 0) * 100)}
                <br />
                5日成交量高出50日成交量{formatNumber(item.vol_5_std)}个标准差，当日成交量高出{formatNumber(item.today_vol_std)}个标准差
                <br />
                50日低点:{formatNumber(item.low_50)} 收盘vs50日低点比率:{formatNumber(item.close_vs_low_50)}
            </>
        );
    };

    const configColumns = [
        {
            title: '策略',
            dataIndex: 'name',
            width: 220,
            render: (value, record) => (
                <Space direction="vertical" size={0}>
                    <Text strong>{value}</Text>
                    <Text type="secondary">{record.strategy_name}</Text>
                </Space>
            ),
        },
        { title: '启用', dataIndex: 'enabled', width: 80, render: value => <Tag color={value ? 'success' : 'default'}>{value ? '开启' : '关闭'}</Tag> },
        {
            title: '自动同步',
            width: 130,
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    <Tag color={record.auto_sync_enabled ? 'processing' : 'default'}>{record.auto_sync_enabled ? '开启' : '关闭'}</Tag>
                    <Text type="secondary">{record.auto_sync_time || DEFAULT_AUTO_SYNC_TIME} ET</Text>
                </Space>
            ),
        },
        { title: '标的数', dataIndex: 'symbols', width: 80, render: value => value?.length || 0 },
        { title: '最新日期', dataIndex: ['runtime', 'latest_date'], width: 110, render: formatDate },
        { title: '总资产', dataIndex: ['runtime', 'portfolio_value'], width: 130, render: value => formatMoney(value, 0) },
        { title: '累计收益', dataIndex: ['runtime', 'total_return'], width: 110, render: value => <Text style={{ color: returnColor(value) }}>{formatPercent(value)}</Text> },
        { title: '信号', dataIndex: ['runtime', 'signal_count'], width: 80, render: value => value || 0 },
        { title: '交易', dataIndex: ['runtime', 'trade_count'], width: 80, render: value => value || 0 },
        {
            title: '同步状态',
            width: 170,
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    <Tag color={record.last_sync_status === 'success' ? 'success' : record.last_sync_status === 'failed' ? 'error' : 'blue'}>
                        {record.last_sync_status || '-'}
                    </Tag>
                    <Text type="secondary">{formatDateTime(record.last_sync_at)}</Text>
                    {record.last_auto_sync_at && <Text type="secondary">自动 {formatDateTime(record.last_auto_sync_at)}</Text>}
                </Space>
            ),
        },
        {
            title: '操作',
            width: 120,
            fixed: 'right',
            render: (_, record) => (
                <Space>
                    <Button size="small" onClick={event => { event.stopPropagation(); selectConfig(record); }}>查看</Button>
                    <Button
                        size="small"
                        icon={<SyncOutlined />}
                        loading={syncLoadingId === record.id}
                        onClick={event => { event.stopPropagation(); syncConfig(record); }}
                    />
                </Space>
            ),
        },
    ];

    const holdingColumns = [
        { title: '标的', dataIndex: 'symbol', width: 120 },
        { title: '股数', dataIndex: 'shares', width: 90, render: value => formatMoney(value, 0) },
        { title: '价格', dataIndex: 'price', width: 90, render: value => formatNumber(value, 4) },
        { title: '成本', dataIndex: 'avg_cost', width: 90, render: value => formatNumber(value, 4) },
        { title: '入场日', dataIndex: 'entry_date', width: 110, render: formatDate },
        { title: '市值', dataIndex: 'market_value', width: 120, render: value => formatMoney(value, 2) },
        { title: '仓位', dataIndex: 'actual_weight_pct', width: 90, render: value => formatPercent(value) },
    ];

    const tradeColumns = [
        { title: '日期', dataIndex: 'date', width: 110, render: formatDate },
        { title: '动作', dataIndex: 'action', width: 80, render: value => <Tag color={value === 'BUY' ? 'green' : 'red'}>{value}</Tag> },
        { title: '标的', dataIndex: 'symbol', width: 120 },
        { title: '价格', dataIndex: 'price', width: 90, render: value => formatNumber(value, 4) },
        { title: '数量', dataIndex: 'quantity', width: 90, render: value => formatMoney(value, 0) },
        { title: '收益', dataIndex: 'profit_pct', width: 90, render: value => <Text style={{ color: returnColor(value) }}>{formatPercent(value)}</Text> },
        { title: '原因', dataIndex: 'reason_detail', width: 320 },
        { title: '价格源', dataIndex: 'price_source', width: 110, render: value => <Tag color={value === 'realtime_quote' ? 'blue' : 'default'}>{value === 'realtime_quote' ? '实时价' : '日K'}</Tag> },
    ];

    const eventColumns = [
        { title: '日期', dataIndex: 'date', width: 110, render: formatDate },
        { title: '方向', dataIndex: 'direction', width: 80, render: value => <Tag color={directionColor(value)}>{value}</Tag> },
        { title: '标的', dataIndex: 'symbol', width: 120 },
        { title: '信号价', dataIndex: 'signal_price', width: 90, render: value => formatNumber(value, 4) },
        { title: '价格源', dataIndex: 'price_source', width: 110, render: value => <Tag color={value === 'realtime_quote' ? 'blue' : 'default'}>{value === 'realtime_quote' ? '实时价' : '日K'}</Tag> },
    ];

    const summary = detail?.summary?.metrics || {};
    const btSummary = backtestResult?.metrics || backtestResult?.summary || null;

    return (
        <Layout style={{ minHeight: '100vh', background: '#f6f8fb' }}>
            <Header style={{
                position: 'fixed',
                zIndex: 10,
                width: '100%',
                background: '#fff',
                padding: '0 16px',
                display: 'flex',
                alignItems: 'center',
                borderBottom: '1px solid #edf0f5'
            }}>
                <LeftOutlined onClick={() => navigate(-1)} style={{ fontSize: 16, marginRight: 10, cursor: 'pointer' }} />
                <Title level={4} style={{ margin: 0 }}>美股信号策略虚拟盘</Title>
            </Header>
            <Layout.Content
                ref={containerRef}
                style={{ marginTop: 64, padding: 16, height: 'calc(100vh - 64px)', overflowY: 'auto' }}
                onScroll={handleScroll}
            >
                <div style={{ background: '#fff', border: '1px solid #edf0f5', borderRadius: 8, padding: 16, marginBottom: 12 }}>
                    <Row justify="space-between" align="middle" style={{ marginBottom: 12 }}>
                        <Col><Text strong>策略虚拟盘</Text></Col>
                        <Col>
                            <Space>
                                <Button icon={<ReloadOutlined />} onClick={fetchConfigs}>刷新</Button>
                                <Button type="primary" icon={<SyncOutlined />} loading={syncLoadingId === 'all'} onClick={syncAll}>同步全部启用</Button>
                            </Space>
                        </Col>
                    </Row>
                    <Table
                        rowKey="id"
                        size="small"
                        loading={listLoading}
                        columns={configColumns}
                        dataSource={configs}
                        pagination={false}
                        scroll={{ x: 1320 }}
                        onRow={record => ({ onClick: () => selectConfig(record) })}
                        rowClassName={record => record.id === selectedConfig?.id ? 'ant-table-row-selected' : ''}
                    />
                </div>

                <div style={{ background: '#fff', border: '1px solid #edf0f5', borderRadius: 8, padding: '12px 12px 4px', marginBottom: 12 }}>
                    <Tabs
                        activeKey={activeStrategy}
                        onChange={key => {
                            const matched = configs.find(item => item.strategy_id === key);
                            if (matched) selectConfig(matched);
                            else setActiveStrategy(key);
                        }}
                        items={strategies.map(item => ({ key: item.id, label: item.name }))}
                    />
                    <Row gutter={[12, 12]} align="middle" style={{ paddingBottom: 12 }}>
                        <Col flex="auto"><Text type="secondary">{activeStrategyDef?.summary}</Text></Col>
                        <Col>
                            <Space>
                                <Segmented value={directionFilter} onChange={setDirectionFilter} options={directionOptions} />
                                <Tooltip title="刷新信号">
                                    <Button aria-label="刷新信号" icon={<ReloadOutlined />} onClick={() => fetchSignals(1, true)} />
                                </Tooltip>
                            </Space>
                        </Col>
                    </Row>
                </div>

                {selectedConfig && (
                    <div style={{ background: '#fff', border: '1px solid #edf0f5', borderRadius: 8, padding: 16, marginBottom: 12 }}>
                        <Alert
                            type="info"
                            showIcon
                            message="自动同步触发时间使用美东时间（US/Eastern）"
                            style={{ marginBottom: 12 }}
                        />
                        <Form form={configForm} layout="vertical">
                            <Row gutter={[12, 0]} align="bottom">
                                <Col xs={24} md={6}><Form.Item label="名称" name="name" rules={[{ required: true }]}><Input /></Form.Item></Col>
                                <Col xs={12} md={3}><Form.Item label="启用" name="enabled" valuePropName="checked"><Switch /></Form.Item></Col>
                                <Col xs={12} md={3}><Form.Item label="自动同步" name="auto_sync_enabled" valuePropName="checked"><Switch /></Form.Item></Col>
                                <Col xs={12} md={3}>
                                    <Form.Item
                                        label="触发时间（美东）"
                                        name="auto_sync_time"
                                        rules={[
                                            { required: true, message: '请输入触发时间' },
                                            { pattern: AUTO_SYNC_TIME_PATTERN, message: '格式为 HH:MM，美东时间' },
                                        ]}
                                    >
                                        <Input placeholder={DEFAULT_AUTO_SYNC_TIME} />
                                    </Form.Item>
                                </Col>
                                <Col xs={12} md={4}><Form.Item label="开始日期" name="start_date" rules={[{ required: true }]}><DatePicker style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={3}><Form.Item label="初始资金" name="initial_capital" rules={[{ required: true }]}><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={2}><Form.Item label="持有日" name="holding_days" rules={[{ required: true }]}><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={2}><Form.Item label="单笔%" name="position_pct" rules={[{ required: true }]}><InputNumber min={0.1} max={100} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={2}><Form.Item label="持仓数" name="max_positions" rules={[{ required: true }]}><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={2}><Form.Item><Button type="primary" icon={<SaveOutlined />} loading={configLoading} onClick={saveConfig} block>保存</Button></Form.Item></Col>
                                <Col xs={24} md={10}>
                                    <Form.Item
                                        label={
                                            <Space>
                                                <span>标的池</span>
                                                <Button type="link" size="small" onClick={fillDefaultSymbols} style={{ padding: 0 }}>
                                                    填入SPY∪QQQ
                                                </Button>
                                                {defaultSymbolsMeta?.source && (
                                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                                        {defaultSymbolsMeta.source} · {defaultSymbolsMeta.count || 0}个
                                                    </Text>
                                                )}
                                            </Space>
                                        }
                                        name="symbols_text"
                                        rules={[{ required: true }]}
                                    >
                                        <Input.TextArea rows={2} />
                                    </Form.Item>
                                </Col>
                                <Col xs={12} md={3}><Form.Item label="最低现金%" name="min_cash_pct"><InputNumber min={0} max={100} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={3}><Form.Item label="佣金%" name="commission_pct"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={3}><Form.Item label="滑点%" name="slippage_pct"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={2}><Form.Item label="手数" name="lot_size"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={4}><Form.Item label="最低市值" name="min_market_cap"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item></Col>
                                <Col xs={12} md={2}><Form.Item><Button icon={<SyncOutlined />} loading={syncLoadingId === selectedConfig.id} onClick={() => syncConfig(selectedConfig)} block>同步</Button></Form.Item></Col>
                                <Col xs={24}>
                                    <div style={{ paddingTop: 4 }}>
                                        <Text type="secondary">策略参数</Text>
                                        <Row gutter={[12, 0]} style={{ marginTop: 8 }}>
                                            {renderStrategyParamFields()}
                                        </Row>
                                    </div>
                                </Col>
                            </Row>
                        </Form>

                        <Spin spinning={detailLoading}>
                            <Row gutter={[16, 12]} style={{ marginBottom: 16 }}>
                                <Col xs={12} md={4}><Statistic title="总资产" value={summary.ending_value} formatter={value => formatMoney(value, 0)} /></Col>
                                <Col xs={12} md={4}><Statistic title="累计收益" value={summary.total_return} suffix="%" precision={2} valueStyle={{ color: returnColor(summary.total_return) }} /></Col>
                                <Col xs={12} md={4}><Statistic title="年化收益" value={summary.annualized_return} suffix="%" precision={2} valueStyle={{ color: returnColor(summary.annualized_return) }} /></Col>
                                <Col xs={12} md={4}><Statistic title="最大回撤" value={summary.max_drawdown} suffix="%" precision={2} /></Col>
                                <Col xs={12} md={4}><Statistic title="信号数" value={summary.signal_count || 0} /></Col>
                                <Col xs={12} md={4}><Statistic title="交易数" value={summary.trade_count || 0} /></Col>
                            </Row>
                            <Tabs
                                items={[
                                    { key: 'holdings', label: '持仓', children: <Table rowKey="symbol" size="small" columns={holdingColumns} dataSource={detail?.holdings || []} pagination={false} scroll={{ x: 780 }} /> },
                                    { key: 'trades', label: '交易', children: <Table rowKey="id" size="small" columns={tradeColumns} dataSource={detail?.trades || []} pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 1100 }} /> },
                                    { key: 'events', label: '信号', children: <Table rowKey="id" size="small" columns={eventColumns} dataSource={detail?.events || []} pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 640 }} /> },
                                ]}
                            />
                        </Spin>
                    </div>
                )}

                <div style={{ background: '#fff', border: '1px solid #edf0f5', borderRadius: 8, padding: 16, marginBottom: 12 }}>
                    <Form
                        form={backtestForm}
                        layout="vertical"
                        initialValues={{
                            date_range: [dayjs().subtract(2, 'year'), dayjs()],
                            holding_days: 20,
                            position_pct: 10,
                            max_positions: 10,
                            initial_capital: 100000,
                            max_symbols: defaultSymbols.length || 700,
                            symbols: defaultSymbols.join(', '),
                            strategy_params: getStrategyParamDefaults('v1'),
                        }}
                    >
                        <Row gutter={[12, 0]} align="bottom">
                            <Col xs={24} md={7}><Form.Item label="回测区间" name="date_range" rules={[{ required: true }]}><RangePicker style={{ width: '100%' }} /></Form.Item></Col>
                            <Col xs={12} md={3}><Form.Item label="初始资金" name="initial_capital"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col>
                            <Col xs={12} md={2}><Form.Item label="持有日" name="holding_days"><InputNumber min={1} max={120} style={{ width: '100%' }} /></Form.Item></Col>
                            <Col xs={12} md={2}><Form.Item label="单笔%" name="position_pct"><InputNumber min={0.1} max={100} style={{ width: '100%' }} /></Form.Item></Col>
                            <Col xs={12} md={2}><Form.Item label="持仓数" name="max_positions"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col>
                            <Col xs={12} md={2}><Form.Item label="最多标的" name="max_symbols"><InputNumber min={1} max={700} style={{ width: '100%' }} /></Form.Item></Col>
                            <Col xs={24} md={4}>
                                <Form.Item
                                    label={
                                        <Space>
                                            <span>标的</span>
                                            <Button type="link" size="small" onClick={fillDefaultSymbols} style={{ padding: 0 }}>
                                                默认池
                                            </Button>
                                        </Space>
                                    }
                                    name="symbols"
                                >
                                    <Input placeholder="AAPL.US, MSFT.US" allowClear />
                                </Form.Item>
                            </Col>
                            <Col xs={24} md={2}><Form.Item><Button type="primary" icon={<PlayCircleOutlined />} loading={backtestLoading} onClick={runBacktest} block>回测</Button></Form.Item></Col>
                            <Col xs={24}>
                                <div style={{ paddingTop: 4 }}>
                                    <Text type="secondary">策略参数</Text>
                                    <Row gutter={[12, 0]} style={{ marginTop: 8 }}>
                                        {renderStrategyParamFields()}
                                    </Row>
                                </div>
                            </Col>
                        </Row>
                    </Form>
                    {(backtestLoading || backtestTaskId) && (
                        <div style={{ marginTop: 12 }}>
                            <Progress percent={backtestProgress} status={backtestStatus === 'failed' ? 'exception' : backtestStatus === 'completed' ? 'success' : 'active'} />
                            <Alert
                                type={backtestStatus === 'failed' ? 'error' : 'info'}
                                showIcon
                                message={backtestStatus || 'pending'}
                                description={
                                    <Space direction="vertical" size={4}>
                                        <span>{backtestProgressText || (backtestTaskId ? `任务ID: ${backtestTaskId}` : '正在启动任务')}</span>
                                        {backtestTaskId && <span>任务ID：{backtestTaskId}</span>}
                                    </Space>
                                }
                                style={{ marginTop: 12 }}
                            />
                        </div>
                    )}
                    {btSummary && (
                        <Row gutter={[16, 12]}>
                            <Col xs={12} md={4}><Statistic title="信号数" value={btSummary.signal_count || 0} /></Col>
                            <Col xs={12} md={4}><Statistic title="交易数" value={btSummary.trade_count || 0} /></Col>
                            <Col xs={12} md={4}><Statistic title="累计收益" value={btSummary.total_return} suffix="%" precision={2} valueStyle={{ color: returnColor(btSummary.total_return) }} /></Col>
                            <Col xs={12} md={4}><Statistic title="年化收益" value={btSummary.annualized_return} suffix="%" precision={2} valueStyle={{ color: returnColor(btSummary.annualized_return) }} /></Col>
                            <Col xs={12} md={4}><Statistic title="胜率" value={btSummary.win_rate} suffix="%" precision={2} /></Col>
                            <Col xs={12} md={4}><Statistic title="期末资产" value={btSummary.ending_value} formatter={value => formatMoney(value, 0)} /></Col>
                        </Row>
                    )}
                </div>

                <div style={{ background: '#fff', border: '1px solid #edf0f5', borderRadius: 8, padding: '4px 16px 16px' }}>
                    <List
                        dataSource={signals}
                        locale={{ emptyText: loading ? <Spin /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                        renderItem={item => (
                            <List.Item style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', padding: '12px 0', borderBottom: '1px solid #f0f0f0' }}>
                                <Space size={8} wrap>
                                    <Text type="secondary" style={{ fontSize: 12 }}>{formatDate(item.date)}</Text>
                                    <Tag color={directionColor(item.direction)}>{item.direction}</Tag>
                                    <Tag>{item.strategy_name || item.strategy_id}</Tag>
                                    <Tag color={item.price_source === 'realtime_quote' ? 'blue' : 'default'}>{item.price_source === 'realtime_quote' ? '实时价' : '日K'}</Tag>
                                </Space>
                                <Text style={{ marginTop: 6, fontSize: 12, color: item.direction === 'SELL' ? '#cf1322' : '#3f8600' }}>
                                    <Button type="link" size="small" onClick={() => navigate(`/stock/${item.symbol}`)} style={{ padding: 0, height: 'auto', fontSize: 12 }}>
                                        {item.symbol}
                                    </Button>
                                    &nbsp;{renderSignalText(item)}
                                </Text>
                            </List.Item>
                        )}
                    />
                    {loading && signals.length > 0 && <div style={{ textAlign: 'center', padding: 16 }}><Spin /></div>}
                </div>
            </Layout.Content>
        </Layout>
    );
};

export default MarketSignalHistory;
