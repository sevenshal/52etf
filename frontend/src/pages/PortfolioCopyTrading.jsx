import React, { useState, useEffect, useMemo } from 'react';
import dayjs from 'dayjs';
import {
    Table, Card, Button, Modal, Form, Input, InputNumber,
    Space, Tag, message, Typography, Switch, Row, Col, List,
    Tabs, Select, Empty
} from 'antd';
import {
    PlusOutlined, ReloadOutlined, PlayCircleOutlined, HistoryOutlined,
    SettingOutlined, DeleteOutlined, EditOutlined, LineChartOutlined
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import { useAccount } from '../contexts/AccountContext';

const { Title, Text } = Typography;
const { TextArea } = Input;

const PORTFOLIO_COPY_PLATFORMS = {
    futu: { label: '富途牛牛', color: 'cyan' },
    star_wealth: { label: '星财富', color: 'gold' },
    yingli: { label: '盈立', color: 'purple' },
};

const PortfolioCopyTrading = () => {
    const { accountId } = useAccount();
    const [configs, setConfigs] = useState([]);
    // Logs State
    const [logModalVisible, setLogModalVisible] = useState(false);
    const [currentLogs, setCurrentLogs] = useState([]);
    const [logLoading, setLogLoading] = useState(false);
    const [currentLogTitle, setCurrentLogTitle] = useState('');
    const [activeLogConfig, setActiveLogConfig] = useState(null); // { record, type }
    const [logPagination, setLogPagination] = useState({ current: 1, pageSize: 20, total: 0 });
    const [logFilters, setLogFilters] = useState({ combination_id: '' });

    const [loading, setLoading] = useState(false);
    const [modalVisible, setModalVisible] = useState(false);
    const [editingConfig, setEditingConfig] = useState(null);
    const [form] = Form.useForm();
    const selectedPortfolioExternalTradingAccountId = Form.useWatch('external_trading_account_id', form);
    const [activeTab, setActiveTab] = useState('ib_configs'); // Changed default to ib_configs
    const [ibAccounts, setIbAccounts] = useState([]);
    const [longportAccounts, setLongportAccounts] = useState([]);
    const [previewVisible, setPreviewVisible] = useState(false);
    const [previewPlan, setPreviewPlan] = useState([]);
    const [previewLoading, setPreviewLoading] = useState(false);

    // Snowball States
    const [snowballConfigs, setSnowballConfigs] = useState([]);
    const [snowballModalVisible, setSnowballModalVisible] = useState(false);
    const [snowballForm] = Form.useForm();
    const [snowballEditingConfig, setSnowballEditingConfig] = useState(null);
    const selectedSnowballExternalTradingAccountId = Form.useWatch('external_trading_account_id', snowballForm);
    const [externalTradingAccounts, setExternalTradingAccounts] = useState([]);
    const [portfolioLiveSubAccounts, setPortfolioLiveSubAccounts] = useState([]);
    const [snowballLiveSubAccounts, setSnowballLiveSubAccounts] = useState([]);
    const [snapshotModalVisible, setSnapshotModalVisible] = useState(false);
    const [snapshotData, setSnapshotData] = useState(null);
    const [snapshotLoading, setSnapshotLoading] = useState(false);
    const [currentSnapshotTitle, setCurrentSnapshotTitle] = useState('');
    const [snowballBacktestModalVisible, setSnowballBacktestModalVisible] = useState(false);
    const [snowballBacktestForm] = Form.useForm();
    const [snowballBacktestTarget, setSnowballBacktestTarget] = useState(null);
    const [snowballBacktestLoading, setSnowballBacktestLoading] = useState(false);
    const [snowballBacktestHistoryVisible, setSnowballBacktestHistoryVisible] = useState(false);
    const [snowballBacktestRuns, setSnowballBacktestRuns] = useState([]);
    const [snowballBacktestRunsLoading, setSnowballBacktestRunsLoading] = useState(false);
    const [selectedSnowballBacktest, setSelectedSnowballBacktest] = useState(null);
    const [selectedSnowballBacktestLoading, setSelectedSnowballBacktestLoading] = useState(false);

    // Snowball Account Config State
    const [snowballAccountModalVisible, setSnowballAccountModalVisible] = useState(false);
    const [snowballAccountForm] = Form.useForm();
    const [snowballAccountConfig, setSnowballAccountConfig] = useState(null);

    const handlePreview = async (configId) => {
        setPreviewLoading(true);
        setPreviewVisible(true);
        setPreviewPlan([]);
        try {
            const response = await request.post(`/api/ib-copy-trading/configs/${configId}/preview`);
            setPreviewPlan(response.data);
        } catch (error) {
            message.error('获取预览失败: ' + (error.response?.data?.detail || error.message));
        } finally {
            setPreviewLoading(false);
        }
    };

    const formatMoney = (value, digits = 2) => Number(value || 0).toLocaleString(undefined, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });

    const formatError = (error, fallback) => error.response?.data?.detail || error.message || fallback;
    const formatPercent = (value, digits = 2) => `${Number(value || 0).toFixed(digits)}%`;
    const formatOptionalPercent = (value, digits = 2) => {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
        return `${Number(value).toFixed(digits)}%`;
    };
    const formatOptionalNumber = (value, digits = 2) => {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
        return Number(value).toFixed(digits);
    };
    const formatSignedPercent = (value, digits = 2) => {
        const number = Number(value || 0);
        return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
    };
    const formatQuantity = (value) => Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
    const formatSignedQuantity = (value) => {
        const number = Number(value || 0);
        return `${number > 0 ? '+' : ''}${number.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    };
    const formatSignedMoney = (value, digits = 2) => {
        const number = Number(value || 0);
        return `${number > 0 ? '+' : ''}${formatMoney(number, digits)}`;
    };
    const diffColor = (value) => {
        const number = Number(value || 0);
        if (number > 0) return '#fa8c16';
        if (number < 0) return '#cf1322';
        return '#389e0d';
    };
    const diffTagColor = (value) => {
        const number = Number(value || 0);
        if (number > 0) return 'orange';
        if (number < 0) return 'red';
        return 'green';
    };

    const fetchExternalTradingAccounts = async () => {
        try {
            const response = await request.get('/api/external-trading-accounts');
            setExternalTradingAccounts(response.data || []);
        } catch (error) {
            message.error(formatError(error, '获取外部交易账户失败'));
        }
    };

    const fetchSnowballLiveSubAccounts = async (externalAccountId) => {
        if (!externalAccountId) {
            setSnowballLiveSubAccounts([]);
            return [];
        }
        try {
            const response = await request.get(`/api/external-trading-accounts/${externalAccountId}/sub-accounts`);
            setSnowballLiveSubAccounts(response.data || []);
            return response.data || [];
        } catch (error) {
            message.error(formatError(error, '获取虚拟子账户失败'));
            setSnowballLiveSubAccounts([]);
            return [];
        }
    };

    const fetchPortfolioLiveSubAccounts = async (externalAccountId) => {
        if (!externalAccountId) {
            setPortfolioLiveSubAccounts([]);
            return [];
        }
        try {
            const response = await request.get(`/api/external-trading-accounts/${externalAccountId}/sub-accounts`);
            setPortfolioLiveSubAccounts(response.data || []);
            return response.data || [];
        } catch (error) {
            message.error(formatError(error, '获取虚拟子账户失败'));
            setPortfolioLiveSubAccounts([]);
            return [];
        }
    };

    const fetchSnowballConfigs = async () => {
        setLoading(true);
        try {
            const response = await request.get('/api/snowball/configs');
            setSnowballConfigs(response.data);
        } catch (error) {
            message.error('获取雪球配置失败');
        } finally {
            setLoading(false);
        }
    };

    const fetchSnowballTabData = () => {
        fetchSnowballConfigs();
    };

    const fetchSnowballAccountConfig = async () => {
        try {
            const response = await request.get('/api/snowball/account-config');
            setSnowballAccountConfig(response.data || null);
            snowballAccountForm.setFieldsValue({ xueqiu_cookie: response.data?.xueqiu_cookie || '' });
        } catch (error) {
            message.error('获取雪球账号配置失败');
        }
    };

    const handleSnowballAccountSave = async (values) => {
        try {
            await request.post('/api/snowball/account-config', values);
            message.success('保存雪球账号配置成功');
            await fetchSnowballAccountConfig();
            setSnowballAccountModalVisible(false);
        } catch (error) {
            message.error('保存失败');
        }
    };

    const fetchIbAccounts = async () => {
        try {
            const response = await request.get('/api/ib-accounts');
            setIbAccounts(response.data);
        } catch (error) {
            message.error('获取 IB 账户列表失败');
        }
    };

    const fetchLongportAccounts = async () => {
        try {
            const response = await request.get('/api/longport-accounts');
            setLongportAccounts(response.data);
        } catch (error) {
            message.error('获取长桥账户列表失败');
        }
    };

    const fetchConfigs = async () => {
        setLoading(true);
        try {
            const response = await request.get('/api/ib-copy-trading/configs');
            setConfigs(response.data);
        } catch (error) {
            message.error('获取配置失败');
        } finally {
            setLoading(false);
        }
    };

    const fetchPortfolioName = async () => {
        const id = form.getFieldValue('portfolio_id');
        const platform = form.getFieldValue('platform') || 'futu';
        if (!id) {
            message.warning('请先输入投资组合 ID');
            return;
        }
        try {
            const params = { platform };
            if (platform === 'yingli') {
                params.invest_id = form.getFieldValue('yingli_invest_id');
                params.authorization = form.getFieldValue('yingli_auth');
            }
            const response = await request.get(`/api/ib-copy-trading/portfolio-info/${id}`, { params });
            form.setFieldsValue({ portfolio_name: response.data.name });
            message.success('获取成功: ' + response.data.name);
        } catch (error) {
            message.error('获取组合名称失败');
        }
    };

    const fetchLogs = async (page = 1, filters = {}, config = null) => {
        const targetConfig = config || activeLogConfig;
        if (!targetConfig) return;

        setLogLoading(true);
        try {
            const { record, type } = targetConfig;
            let params = {
                page: page,
                page_size: logPagination.pageSize,
                ...filters
            };

            if (type === 'ib') {
                params.portfolio_id = record.portfolio_id;
                // Add config_id for IB logs 
                if (record.id) params.config_id = record.id;

                const res = await request.get('/api/ib-copy-trading/logs', { params });
                setCurrentLogs(res.data); // IB returns List
                setLogPagination(prev => ({ ...prev, current: 1, total: res.data.length })); // Fake pagination for IB
            } else {
                // Add specific filters
                if (logFilters.combination_id) params.combination_id = logFilters.combination_id;
                // Override with argument filters if provided (e.g. from search click)
                if (filters.combination_id !== undefined) params.combination_id = filters.combination_id;

                const res = await request.get('/api/snowball/logs', { params });
                // New Response: { total: 100, items: [...] }
                setCurrentLogs(res.data.items);
                setLogPagination(prev => ({ ...prev, current: page, total: res.data.total }));
            }
        } catch (e) {
            message.error('获取日志失败');
            console.error(e);
        } finally {
            setLogLoading(false);
        }
    };

    const handleLogTableChange = (pagination) => {
        setLogPagination(prev => ({ ...prev, current: pagination.current }));
        fetchLogs(pagination.current, logFilters);
    };

    const handleViewLogs = async (record, type) => {
        setLogModalVisible(true);
        setActiveLogConfig({ record, type });
        setCurrentLogs([]);

        // Auto-select combination_id if present (Snowball only)
        const initialCombId = record.combination_id || '';
        const initialFilters = { combination_id: initialCombId };

        // For IB, we don't have combination_id filter in state, but we need to ensure config_id is passed implicitly via 'record' in activeLogConfig
        setLogFilters(initialFilters);
        setLogPagination(prev => ({ ...prev, current: 1, total: 0 }));

        if (type === 'ib') {
            setCurrentLogTitle(`跟单日志 - ${record.portfolio_name} (${record.portfolio_id})`);
        } else {
            setCurrentLogTitle(`跟单日志 - ${record.combination_name || record.combination_id}`);
        }

        // Initial fetch
        fetchLogs(1, initialFilters, { record, type });
    };

    const handleSave = async (values) => {
        try {
            const payload = {
                ...values,
                id: editingConfig?.id
            };
            if (values.account_type !== 'external') {
                payload.external_trading_account_id = undefined;
                payload.live_sub_account_id = undefined;
            }
            if (values.account_type !== 'longport') {
                payload.longport_account_id = undefined;
            }
            if (values.account_type !== 'ib') {
                payload.ib_account_id = undefined;
            }

            // Process Yingli specific fields
            if (values.platform === 'yingli') {
                payload.api_headers = {
                    ...(editingConfig?.api_headers || {}),
                    investId: values.yingli_invest_id,
                    Authorization: values.yingli_auth
                };
            }

            await request.post('/api/ib-copy-trading/configs', payload);
            message.success(editingConfig ? '更新成功' : '添加成功');
            setModalVisible(false);
            fetchConfigs();
        } catch (error) {
            message.error('保存失败: ' + formatError(error, '保存失败'));
        }
    };

    const handleDelete = async (id) => {
        try {
            await request.delete(`/api/ib-copy-trading/configs/${id}`);
            message.success('删除成功');
            fetchConfigs();
        } catch (error) {
            message.error('删除失败');
        }
    };

    const handlePortfolioSyncExternalTargets = async (record) => {
        try {
            await request.post(`/api/ib-copy-trading/configs/${record.id}/sync-external-targets`);
            message.success('已同步目标仓位并触发通用执行器');
            fetchConfigs();
        } catch (error) {
            message.error('同步失败: ' + formatError(error, '同步失败'));
        }
    };

    const fetchSnowballName = async () => {
        const id = snowballForm.getFieldValue('combination_id');
        if (!id) {
            message.warning('请先输入雪球组合 ID');
            return;
        }
        try {
            const response = await request.get(`/api/snowball/info/${id}`);
            snowballForm.setFieldsValue({ combination_name: response.data.name });
            message.success('获取成功: ' + response.data.name);
        } catch (error) {
            message.error('获取组合名称失败: ' + (error.response?.data?.detail || error.message));
        }
    };

    // Snowball Handlers
    const handleSnowballSave = async (values) => {
        try {
            const payload = { ...values };
            delete payload.total_amount;
            delete payload.total_position_ratio;
            if (snowballEditingConfig) {
                await request.put(`/api/snowball/configs/${snowballEditingConfig.id}`, payload);
                message.success('更新成功');
            } else {
                await request.post('/api/snowball/configs', payload);
                message.success('添加成功');
            }
            setSnowballModalVisible(false);
            fetchSnowballConfigs();
        } catch (error) {
            message.error('保存失败: ' + (error.response?.data?.detail || error.message));
        }
    };


    const handleSnowballDelete = async (id) => {
        try {
            await request.delete(`/api/snowball/configs/${id}`);
            message.success('删除成功');
            fetchSnowballConfigs();
        } catch (error) {
            message.error('删除失败');
        }
    };

    const handleSnowballSyncExternalTargets = async (record) => {
        try {
            await request.post(`/api/snowball/configs/${record.id}/sync-external-targets`);
            message.success('已同步目标仓位并触发通用执行器');
            fetchSnowballConfigs();
        } catch (error) {
            message.error('同步失败: ' + formatError(error, '同步失败'));
        }
    };

    const handleViewSnapshot = async (record) => {
        setSnapshotLoading(true);
        setSnapshotModalVisible(true);
        setCurrentSnapshotTitle(`组合详情 - ${record.combination_name || record.combination_id}`);
        setSnapshotData(null);

        try {
            const response = await request.get(`/api/snowball/snapshot/${record.id}`);
            setSnapshotData(response.data);
        } catch (error) {
            message.error('获取组合详情失败: ' + formatError(error, '获取组合详情失败'));
        } finally {
            setSnapshotLoading(false);
        }
    };

    const openSnowballBacktestModal = (record) => {
        setSnowballBacktestTarget(record);
        snowballBacktestForm.setFieldsValue({ slippage_pct: 0.5 });
        setSnowballBacktestModalVisible(true);
    };

    const fetchSnowballBacktestDetail = async (run) => {
        if (!run?.id) return;
        setSelectedSnowballBacktestLoading(true);
        try {
            const response = await request.get(`/api/snowball/backtests/${run.id}`);
            setSelectedSnowballBacktest(response.data);
        } catch (error) {
            message.error('获取回测详情失败: ' + formatError(error, '获取回测详情失败'));
        } finally {
            setSelectedSnowballBacktestLoading(false);
        }
    };

    const fetchSnowballBacktestRuns = async (record = snowballBacktestTarget, preferredRunId = null) => {
        if (!record?.id) return;
        setSnowballBacktestRunsLoading(true);
        try {
            const response = await request.get(`/api/snowball/configs/${record.id}/backtests`);
            const runs = response.data || [];
            setSnowballBacktestRuns(runs);
            const nextRun = runs.find(item => item.id === preferredRunId) || runs[0];
            if (nextRun) {
                await fetchSnowballBacktestDetail(nextRun);
            } else {
                setSelectedSnowballBacktest(null);
            }
        } catch (error) {
            message.error('获取回测历史失败: ' + formatError(error, '获取回测历史失败'));
        } finally {
            setSnowballBacktestRunsLoading(false);
        }
    };

    const handleViewSnowballBacktests = async (record) => {
        setSnowballBacktestTarget(record);
        setSnowballBacktestHistoryVisible(true);
        setSelectedSnowballBacktest(null);
        setSnowballBacktestRuns([]);
        await fetchSnowballBacktestRuns(record);
    };

    const handleSnowballBacktestSubmit = async (values) => {
        if (!snowballBacktestTarget?.id) return;
        setSnowballBacktestLoading(true);
        try {
            const response = await request.post(
                `/api/snowball/configs/${snowballBacktestTarget.id}/backtests`,
                { slippage_pct: values.slippage_pct }
            );
            message.success('回测已开始');
            setSnowballBacktestModalVisible(false);
            setSnowballBacktestHistoryVisible(true);
            await fetchSnowballBacktestRuns(snowballBacktestTarget, response.data?.id);
        } catch (error) {
            message.error('启动回测失败: ' + formatError(error, '启动回测失败'));
        } finally {
            setSnowballBacktestLoading(false);
        }
    };

    const getSnowballBacktestChartOption = () => {
        const rows = selectedSnowballBacktest?.curve_points || [];
        const dates = rows.map(item => item.date);
        return {
            tooltip: {
                trigger: 'axis',
                valueFormatter: value => value === null || value === undefined ? '-' : `${Number(value).toFixed(2)}%`,
            },
            legend: { data: ['滑点后', '原始', '中证500'] },
            grid: { left: 48, right: 24, top: 48, bottom: 72 },
            dataZoom: [{ type: 'inside' }, { type: 'slider', height: 24 }],
            xAxis: { type: 'category', data: dates, boundaryGap: false },
            yAxis: { type: 'value', name: '收益率', axisLabel: { formatter: '{value}%' } },
            series: [
                {
                    name: '滑点后',
                    type: 'line',
                    data: rows.map(item => item.slippage_return_pct),
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { width: 2 },
                },
                {
                    name: '原始',
                    type: 'line',
                    data: rows.map(item => item.raw_return_pct),
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { width: 1.5 },
                },
                {
                    name: '中证500',
                    type: 'line',
                    data: rows.map(item => item.benchmark_return_pct),
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { width: 1.5, type: 'dashed' },
                },
            ],
        };
    };

    useEffect(() => {
        if (!snowballBacktestHistoryVisible || !snowballBacktestTarget?.id) return undefined;
        const hasRunning = snowballBacktestRuns.some(item => item.status === 'RUNNING');
        if (!hasRunning && selectedSnowballBacktest?.status !== 'RUNNING') return undefined;
        const timer = setInterval(() => {
            fetchSnowballBacktestRuns(snowballBacktestTarget, selectedSnowballBacktest?.id);
        }, 5000);
        return () => clearInterval(timer);
    }, [
        snowballBacktestHistoryVisible,
        snowballBacktestTarget,
        snowballBacktestRuns,
        selectedSnowballBacktest?.id,
        selectedSnowballBacktest?.status,
    ]);

    useEffect(() => {
        fetchSnowballLiveSubAccounts(selectedSnowballExternalTradingAccountId);
    }, [selectedSnowballExternalTradingAccountId]);

    useEffect(() => {
        fetchPortfolioLiveSubAccounts(selectedPortfolioExternalTradingAccountId);
    }, [selectedPortfolioExternalTradingAccountId]);

    useEffect(() => {
        if (accountId) {
            if (activeTab === 'ib_configs') {
                fetchConfigs();
            } else if (activeTab === 'snowball_configs') {
                fetchSnowballTabData();
            }
            // IB accounts and Longport accounts are always useful or global
            fetchIbAccounts();
            fetchLongportAccounts();
            fetchExternalTradingAccounts();
        }
    }, [accountId, activeTab]);

    const externalTradingAccountOptions = useMemo(() => externalTradingAccounts.map(account => ({
        label: `${account.name} (${account.identifier})${account.connected ? ' 在线' : ' 离线'}`,
        value: account.id,
        disabled: !account.enabled,
    })), [externalTradingAccounts]);

    const usExternalTradingAccountOptions = useMemo(() => externalTradingAccounts
        .filter(account => account.market_type === 'US_STOCK')
        .map(account => ({
            label: `${account.name} (${account.identifier})${account.connected ? ' 在线' : ' 离线'}`,
            value: account.id,
            disabled: !account.enabled,
        })), [externalTradingAccounts]);

    const portfolioLiveSubAccountOptions = useMemo(() => {
        const currentConfigId = Number(editingConfig?.id || 0);
        return (portfolioLiveSubAccounts || [])
            .filter(item => item.enabled)
            .map(item => {
                const isFree = !item.strategy_type && !item.strategy_config_id;
                const isCurrentBinding = (
                    item.strategy_type === 'portfolio_copy_live'
                    && currentConfigId > 0
                    && Number(item.strategy_config_id) === currentConfigId
                );
                const disabled = !(isFree || isCurrentBinding);
                const statusText = isFree ? '空闲' : `已绑定：${item.strategy_name || item.binding_label || item.strategy_type || '其他策略'}`;
                return {
                    value: item.id,
                    disabled,
                    label: `${item.name} / ${formatMoney(item.cash_allocated, 2)} / ${statusText}`,
                };
            });
    }, [portfolioLiveSubAccounts, editingConfig]);

    const snowballLiveSubAccountOptions = useMemo(() => {
        const currentConfigId = Number(snowballEditingConfig?.id || 0);
        return (snowballLiveSubAccounts || [])
            .filter(item => item.enabled)
            .map(item => {
                const isFree = !item.strategy_type && !item.strategy_config_id;
                const isCurrentBinding = (
                    item.strategy_type === 'snowball_copy_live'
                    && currentConfigId > 0
                    && Number(item.strategy_config_id) === currentConfigId
                );
                const disabled = !(isFree || isCurrentBinding);
                const statusText = isFree ? '空闲' : `已绑定：${item.strategy_name || item.binding_label || item.strategy_type || '其他策略'}`;
                return {
                    value: item.id,
                    disabled,
                    label: `${item.name} / ${formatMoney(item.cash_allocated, 2)} / ${statusText}`,
                };
            });
    }, [snowballLiveSubAccounts, snowballEditingConfig]);

    const configColumns = [
        {
            title: '状态',
            dataIndex: 'enabled',
            key: 'enabled',
            render: (enabled) => <Tag color={enabled ? 'green' : 'gray'}>{enabled ? '开启' : '关闭'}</Tag>
        },
        {
            title: '组合信息',
            key: 'portfolio',
            render: (_, record) => {
                const platform = PORTFOLIO_COPY_PLATFORMS[record.platform || 'futu'];
                return (
                    <Space direction="vertical" size={0}>
                        <Text strong>{record.portfolio_name || '未命名'}</Text>
                        <Space size="small">
                            <Text type="secondary" style={{ fontSize: '12px' }}>ID: {record.portfolio_id || '-'}</Text>
                            <Tag color={platform?.color || 'default'} style={{ fontSize: '10px', lineHeight: '14px', height: '16px' }}>
                                {platform?.label || '不支持的来源'}
                            </Tag>
                        </Space>
                    </Space>
                );
            }
        },
        {
            title: '触发规则',
            key: 'cron_rule',
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    <Text>{record.cron_rule}</Text>
                    <Text type="secondary" style={{ fontSize: '12px' }}>{record.timezone}</Text>
                </Space>
            )
        },
        {
            title: '跟单账户',
            key: 'account',
            render: (_, record) => {
                if (record.account_type === 'external') {
                    return (
                        <Space direction="vertical" size={0}>
                            <Tag color="green">外部交易账户</Tag>
                            <Text>{record.external_trading_account_name || '-'}</Text>
                            <Text type="secondary" style={{ fontSize: '12px' }}>子账户: {record.live_sub_account_name || '-'}</Text>
                            {record.last_external_sync_status && (
                                <Text type="secondary" style={{ fontSize: '12px' }}>
                                    {record.last_external_sync_status}: {record.last_external_sync_message || ''}
                                </Text>
                            )}
                        </Space>
                    );
                }
                if (record.account_type === 'longport') {
                    if (record.longport_account_id) {
                        const account = longportAccounts.find(a => a.lp_account_id === record.longport_account_id);
                        return account ? (
                            <Space direction="vertical" size={0}>
                                <Tag color="purple">Longport</Tag>
                                <Text>{account.name}</Text>
                                <Text type="secondary" style={{ fontSize: '12px' }}>ID: {record.longport_account_id}</Text>
                            </Space>
                        ) : `Longport (ID: ${record.longport_account_id})`;
                    }
                    return <Tag color="purple">Longport</Tag>;
                } else {
                    // Default to IB
                    if (record.ib_account_id) {
                        const account = ibAccounts.find(a => a.id === record.ib_account_id);
                        return account ? (
                            <Space direction="vertical" size={0}>
                                <Tag color="blue">IBKR</Tag>
                                <Text>{account.name}</Text>
                                <Text type="secondary" style={{ fontSize: '12px' }}>Port: {account.ib_port}</Text>
                            </Space>
                        ) : `Unknown IB (ID: ${record.ib_account_id})`;
                    }
                    return (
                        <Space direction="vertical" size={0}>
                            <Tag color="blue">IBKR</Tag>
                            <Text>Port: {record.ib_port}</Text>
                        </Space>
                    );
                }
            }
        },
        {
            title: '配置',
            key: 'settings',
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    {record.total_amount ? (
                        <Text type="secondary" style={{ fontSize: '12px' }}>配置金额: {record.total_amount.toLocaleString()}</Text>
                    ) : (
                        <Text type="secondary" style={{ fontSize: '12px' }}>仓位占比: {record.total_position_ratio}%</Text>
                    )}
                    <Text type="secondary" style={{ fontSize: '12px' }}>跟踪误差: {record.tracking_error_pct}%</Text>
                </Space>
            )
        },
        {
            title: '操作',
            key: 'action',
            render: (_, record) => (
                <Space>
                    <Button
                        icon={<HistoryOutlined />}
                        onClick={() => handleViewLogs(record, 'ib')}
                        size="small"
                        title="查看日志"
                    >日志</Button>
                    <Button
                        icon={<PlayCircleOutlined />}
                        onClick={() => handlePreview(record.id)}
                        size="small"
                        title="预览调仓"
                        disabled={!PORTFOLIO_COPY_PLATFORMS[record.platform || 'futu']}
                    />
                    {record.account_type === 'external' && (
                        <Button
                            onClick={() => handlePortfolioSyncExternalTargets(record)}
                            size="small"
                            title="同步目标仓位并触发通用执行器"
                        >同步目标</Button>
                    )}
                    <Button
                        icon={<EditOutlined />}
                        onClick={() => {
                            setEditingConfig(record);
                            const formValues = { ...record };
                            formValues.platform = record.platform || 'futu';
                            if (record.platform === 'yingli' && record.api_headers) {
                                formValues.yingli_invest_id = record.api_headers.investId;
                                formValues.yingli_auth = record.api_headers.Authorization;
                            }
                            form.setFieldsValue(formValues);
                            fetchPortfolioLiveSubAccounts(record.external_trading_account_id);
                            setModalVisible(true);
                        }}
                        size="small"
                        disabled={!PORTFOLIO_COPY_PLATFORMS[record.platform || 'futu']}
                    />
                    <Button
                        icon={<DeleteOutlined />}
                        onClick={() => handleDelete(record.id)}
                        size="small"
                        danger
                    />
                </Space>
            )
        }
    ];

    const logColumns = [
        {
            title: '时间',
            dataIndex: 'timestamp',
            key: 'timestamp',
            width: 80,
            render: (t) => new Date(t).toLocaleString()
        },
        {
            title: '组合',
            key: 'combination_id',
            width: 80,
            render: (_, record) => {
                if (record.combination_id) {
                    const ids = record.combination_id.split(',');
                    const names = ids.map(id => {
                        const config = snowballConfigs.find(c => c.combination_id === id);
                        return config ? config.combination_name : id;
                    });
                    return <Text style={{ fontSize: '12px' }} ellipsis={{ tooltip: names.join(', ') }}>{names.join(', ')}</Text>;
                }
                if (record.portfolio_id) {
                    const config = configs.find(c => c.portfolio_id === record.portfolio_id);
                    const name = config ? config.portfolio_name : record.portfolio_id;
                    return <Text style={{ fontSize: '12px' }}>{name}</Text>;
                }
                return '-';
            }
        },
        {
            title: '行为',
            dataIndex: 'action',
            key: 'action',
            width: 80,
            render: (text) => {
                let color = 'default';
                if (text === 'BUY') color = 'red';
                if (text === 'SELL') color = 'blue';
                return <Text style={{ color: color, fontWeight: 'bold' }}>{text}</Text>;
            }
        },
        {
            title: '结果',
            key: 'status',
            width: 100,
            render: (_, record) => (
                <Tag color={record.status === 'SUCCESS' ? 'green' : record.status === 'SIGNAL' ? 'blue' : 'red'}>
                    {record.status}
                </Tag>
            )
        },
        {
            title: '消息',
            dataIndex: 'message',
            key: 'message',
            render: (text) => <div style={{ fontSize: '12px', whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'rgba(0, 0, 0, 0.45)' }}>{text}</div>
        }
    ];

    return (
        <div style={{ padding: '24px' }}>
            <Card
                title={
                    <Space>
                        <Title level={4} style={{ margin: 0 }}>自动化跟单交易</Title>
                    </Space>
                }
                extra={
                    <Space>
                        <Button icon={<ReloadOutlined />} onClick={() => {
                            if (activeTab === 'ib_configs') fetchConfigs();
                            else if (activeTab === 'snowball_configs') fetchSnowballTabData();
                        }}>刷新数据</Button>

                    </Space>
                }
            >
                <Tabs activeKey={activeTab} onChange={setActiveTab}>
                    <Tabs.TabPane tab={<span><SettingOutlined />美股账户跟单配置</span>} key="ib_configs">
                        <Table
                            dataSource={configs}
                            columns={configColumns}
                            rowKey="id"
                            loading={loading}
                            pagination={false}
                        />
                        <div style={{ marginTop: 16 }}>
                            <Button type="primary" icon={<PlusOutlined />} onClick={() => {
                                setEditingConfig(null);
                                form.resetFields();
                                setPortfolioLiveSubAccounts([]);
                                setModalVisible(true);
                            }}>添加跟单配置</Button>
                        </div>
                    </Tabs.TabPane>

                    <Tabs.TabPane tab={<span><SettingOutlined />A股雪球跟单配置</span>} key="snowball_configs">
                        <Table
                            dataSource={snowballConfigs}
                            rowKey="id"
                            loading={loading}
                            pagination={false}
                            columns={[
                                {
                                    title: '状态',
                                    dataIndex: 'enabled',
                                    render: (enabled) => <Tag color={enabled ? 'green' : 'gray'}>{enabled ? '开启' : '关闭'}</Tag>
                                },
                                {
                                    title: '组合信息',
                                    key: 'info',
                                    render: (_, r) => (
                                        <Space direction="vertical" size={0}>
                                            <Text strong>{r.combination_name || '未命名'}</Text>
                                            <Text type="secondary">ID: {r.combination_id}</Text>
                                        </Space>
                                    )
                                },
                                {
                                    title: '净值/参数',
                                    key: 'params',
                                    render: (_, r) => (
                                        <Space direction="vertical" size={0}>
                                            <Text strong style={{ color: '#1890ff' }}>
                                                子账户净值: {formatMoney(r.snapshot_value)}
                                            </Text>
                                            <Text type="secondary" style={{ fontSize: '12px' }}>误差: {r.tracking_error_pct}%</Text>
                                            {r.blacklisted_symbols && r.blacklisted_symbols.length > 0 && (
                                                <Text type="secondary" style={{ fontSize: '12px', color: 'red' }}>黑名单: {r.blacklisted_symbols.length}个</Text>
                                            )}
                                        </Space>
                                    )
                                },
                                {
                                    title: '实盘执行',
                                    key: 'live',
                                    render: (_, r) => (
                                        <Space direction="vertical" size={0}>
                                            <Tag color={r.live_trade_enabled ? 'green' : 'default'}>
                                                {r.live_trade_enabled ? '通用执行器' : '未启用'}
                                            </Tag>
                                            {r.live_trade_enabled && (
                                                <>
                                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                                        {r.external_trading_account_name || '-'}
                                                    </Text>
                                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                                        子账户: {r.live_sub_account_name || '-'}
                                                    </Text>
                                                    {r.last_external_sync_status && (
                                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                                            {r.last_external_sync_status}: {r.last_external_sync_message || ''}
                                                        </Text>
                                                    )}
                                                </>
                                            )}
                                        </Space>
                                    )
                                },
                                {
                                    title: '操作',
                                    key: 'action',
                                    render: (_, record) => (
                                        <Space wrap>
                                            <Button
                                                icon={<HistoryOutlined />}
                                                size="small"
                                                onClick={() => handleViewLogs(record, 'snowball')}
                                                title="查看日志"
                                            >日志</Button>
                                            <Button
                                                size="small"
                                                onClick={() => handleViewSnapshot(record)}
                                            >详情</Button>
                                            <Button
                                                icon={<PlayCircleOutlined />}
                                                size="small"
                                                onClick={() => openSnowballBacktestModal(record)}
                                            >回测</Button>
                                            <Button
                                                icon={<LineChartOutlined />}
                                                size="small"
                                                onClick={() => handleViewSnowballBacktests(record)}
                                            >回测历史</Button>
                                            <Button
                                                size="small"
                                                disabled={!record.live_trade_enabled}
                                                onClick={() => handleSnowballSyncExternalTargets(record)}
                                            >同步目标</Button>
                                            <Button icon={<EditOutlined />} size="small" onClick={() => {
                                                setSnowballEditingConfig(record);
                                                snowballForm.setFieldsValue(record);
                                                fetchSnowballLiveSubAccounts(record.external_trading_account_id);
                                                setSnowballModalVisible(true);
                                            }} />
                                            <Button icon={<DeleteOutlined />} size="small" danger onClick={() => handleSnowballDelete(record.id)} />
                                        </Space>
                                    )
                                }
                            ]}
                        />
                        <div style={{ marginTop: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
                            <div>
                                <Button type="primary" icon={<PlusOutlined />} onClick={() => {
                                    setSnowballEditingConfig(null);
                                    snowballForm.resetFields();
                                    setSnowballLiveSubAccounts([]);
                                    setSnowballModalVisible(true);
                                }}>添加雪球跟单配置</Button>
                            </div>
                            <div style={{ flex: 1, minWidth: 320, textAlign: 'center' }}>
                                <Text type="secondary">雪球同步状态以配置列表为准</Text>
                            </div>
                            <div>
                                <Button icon={<SettingOutlined />} onClick={() => {
                                    fetchSnowballAccountConfig();
                                    setSnowballAccountModalVisible(true);
                                }}>雪球账号全局Cookie配置</Button>
                            </div>
                        </div>
                    </Tabs.TabPane>

                </Tabs>
            </Card>

            {/* Logs Modal */}
            <Modal
                title={currentLogTitle}
                visible={logModalVisible}
                onCancel={() => setLogModalVisible(false)}
                footer={null}
                width={1000}
            >
                {activeLogConfig?.type === 'snowball' && (
                    <div style={{ marginBottom: 16, padding: '12px', background: '#f5f5f5', borderRadius: '4px' }}>
                        <Space>
                            <Select
                                placeholder="选择组合"
                                value={logFilters.combination_id}
                                onChange={val => setLogFilters(prev => ({ ...prev, combination_id: val }))}
                                style={{ width: 220 }}
                                allowClear
                            >
                                <Select.Option key="AGGREGATED" value="AGGREGATED">AGGREGATED</Select.Option>
                                {snowballConfigs
                                    .map(c => (
                                        <Select.Option key={c.combination_id} value={c.combination_id}>
                                            {c.combination_name || c.combination_id}
                                        </Select.Option>
                                    ))
                                }
                            </Select>
                            <Button type="primary" icon={<ReloadOutlined />} onClick={() => fetchLogs(1, logFilters)}>
                                搜索
                            </Button>
                        </Space>
                    </div>
                )}

                <Table
                    dataSource={currentLogs}
                    loading={logLoading}
                    rowKey="id"
                    pagination={{
                        current: logPagination.current,
                        pageSize: logPagination.pageSize,
                        total: logPagination.total,
                        showSizeChanger: false,
                        showTotal: (total) => `共 ${total} 条`
                    }}
                    onChange={handleLogTableChange}
                    columns={logColumns}
                    size="small"
                />
            </Modal >

            {/* Existing Modal for IB Config */}
            < Modal
                title="调仓计划预览"
                visible={previewVisible}
                onCancel={() => setPreviewVisible(false)}
                footer={
                    [
                        <Button key="close" onClick={() => setPreviewVisible(false)}>关闭</Button>
                    ]}
                width={800}
            >
                <Table
                    dataSource={previewPlan}
                    loading={previewLoading}
                    rowKey="symbol"
                    size="small"
                    columns={[
                        { title: '代码', dataIndex: 'symbol', key: 'symbol' },
                        {
                            title: '操作',
                            dataIndex: 'action',
                            key: 'action',
                            render: (a) => {
                                let color = 'gold';
                                if (a === 'BUY') color = 'green';
                                if (a === 'SELL') color = 'red';
                                return <Tag color={color}>{a}</Tag>;
                            }
                        },
                        { title: '数量', dataIndex: 'quantity', key: 'quantity' },
                        { title: '价格', dataIndex: 'price', key: 'price', render: (p) => p?.toFixed(2) },
                        {
                            title: '当前/目标股数',
                            key: 'qty_change',
                            render: (_, r) => `${r.current_qty} -> ${r.target_qty}`
                        },
                        {
                            title: '当前/目标占比',
                            key: 'ratio_change',
                            render: (_, r) => `${r.current_ratio?.toFixed(2)}% -> ${r.target_ratio?.toFixed(2)}%`
                        }
                    ]}
                    pagination={false}
                />
            </Modal >

            <Modal
                title={editingConfig ? "编辑跟单配置" : "添加跟单配置"}
                visible={modalVisible}
                onCancel={() => setModalVisible(false)}
                onOk={() => form.submit()}
                width={700}
            >
                <Form form={form} layout="vertical" onFinish={handleSave}
                    initialValues={{
                        enabled: true,
                        cron_rule: '* 9-15 * * 1-5',
                        timezone: 'America/New_York',
                        tracking_error_pct: 5,
                        total_position_ratio: 100,
                        account_type: 'ib',
                        platform: 'futu'
                    }}
                >
                    <Row gutter={16}>
                        <Col span={24}>
                            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 24, marginTop: 12 }}>
                                <span style={{ marginRight: 8, fontSize: '14px' }}>开启状态:</span>
                                <Form.Item name="enabled" valuePropName="checked" noStyle>
                                    <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                                </Form.Item>
                            </div>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={6}>
                            <Form.Item name="platform" label="组合来源">
                                <Select>
                                    {Object.entries(PORTFOLIO_COPY_PLATFORMS).map(([value, platform]) => (
                                        <Select.Option key={value} value={value}>{platform.label}</Select.Option>
                                    ))}
                                </Select>
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            {/* Removed Portfolio fields from here, moved to dynamic render */}
                        </Col>
                    </Row>
                    <Form.Item shouldUpdate={(prev, curr) => prev.platform !== curr.platform} noStyle>
                        {() => {
                            const platform = form.getFieldValue('platform');
                            if (platform === 'yingli') {
                                return (
                                    <Row gutter={16}>
                                        <Col span={6}>
                                            <Form.Item name="yingli_invest_id" label="Invest ID" rules={[{ required: true }]}>
                                                <Input placeholder="1543418964696301568" />
                                            </Form.Item>
                                        </Col>
                                        <Col span={18}>
                                            <Form.Item name="yingli_auth" label="Authorization (Headers)" rules={[{ required: true }]}>
                                                <Input.Password placeholder="eyJh..." />
                                            </Form.Item>
                                        </Col>
                                    </Row>
                                );
                            }
                            return null;
                        }}
                    </Form.Item>

                    <Row gutter={16}>
                        <Col span={10}>
                            <Form.Item label="投资组合 ID" rules={[{ required: true }]}>
                                <Space.Compact style={{ width: '100%' }}>
                                    <Form.Item name="portfolio_id" noStyle rules={[{ required: true }]}>
                                        <Input placeholder="例如: 158919" />
                                    </Form.Item>
                                    <Button onClick={fetchPortfolioName}>获取</Button>
                                </Space.Compact>
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item name="portfolio_name" label="组合名称" rules={[{ required: true }]}>
                                <Input placeholder="自动获取" />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item name="cron_rule" label="触发 Cron 规则" rules={[{ required: true }]}>
                                <Input placeholder="例如: 0 8 * * *" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="timezone" label="触发时区" rules={[{ required: true }]}>
                                <Select>
                                    <Select.Option value="America/New_York">美股 (America/New_York)</Select.Option>
                                    <Select.Option value="Asia/Shanghai">A股 (Asia/Shanghai)</Select.Option>
                                </Select>
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={8}>
                            <Form.Item name="account_type" label="账户类型" rules={[{ required: true }]}>
                                <Select onChange={() => {
                                    form.setFieldsValue({
                                        ib_account_id: undefined,
                                        longport_account_id: undefined,
                                        external_trading_account_id: undefined,
                                        live_sub_account_id: undefined,
                                    });
                                    setPortfolioLiveSubAccounts([]);
                                }}>
                                    <Select.Option value="ib">Interactive Brokers (IB)</Select.Option>
                                    <Select.Option value="longport">长桥证券 (Longport)</Select.Option>
                                    <Select.Option value="external">外部交易账户</Select.Option>
                                </Select>
                            </Form.Item>
                        </Col>
                        <Col span={16}>
                            <Form.Item shouldUpdate={(prev, curr) => prev.account_type !== curr.account_type}>
                                {() => {
                                    const type = form.getFieldValue('account_type');
                                    if (type === 'external') {
                                        return (
                                            <Row gutter={16}>
                                                <Col span={12}>
                                                    <Form.Item name="external_trading_account_id" label="外部交易账户" rules={[{ required: true, message: '请选择外部交易账户' }]}>
                                                        <Select
                                                            allowClear
                                                            showSearch
                                                            optionFilterProp="label"
                                                            options={usExternalTradingAccountOptions}
                                                            placeholder="选择美股外部交易账户"
                                                            onChange={() => form.setFieldsValue({ live_sub_account_id: undefined })}
                                                        />
                                                    </Form.Item>
                                                </Col>
                                                <Col span={12}>
                                                    <Form.Item name="live_sub_account_id" label="虚拟子账户" rules={[{ required: true, message: '请选择虚拟子账户' }]}>
                                                        <Select
                                                            allowClear
                                                            showSearch
                                                            optionFilterProp="label"
                                                            options={portfolioLiveSubAccountOptions}
                                                            placeholder={selectedPortfolioExternalTradingAccountId ? '选择虚拟子账户' : '先选择外部账户'}
                                                            disabled={!selectedPortfolioExternalTradingAccountId}
                                                        />
                                                    </Form.Item>
                                                </Col>
                                            </Row>
                                        );
                                    }
                                    if (type === 'longport') {
                                        return (
                                            <Form.Item name="longport_account_id" label="长桥账户" rules={[{ required: true, message: '请选择长桥账户' }]}>
                                                <Select placeholder="选择长桥账户">
                                                    {longportAccounts.map(account => (
                                                        <Select.Option key={account.lp_account_id} value={account.lp_account_id}>
                                                            {account.name} (ID: {account.lp_account_id})
                                                        </Select.Option>
                                                    ))}
                                                </Select>
                                            </Form.Item>
                                        );
                                    } else {
                                        return (
                                            <Form.Item name="ib_account_id" label="IB 账户" rules={[{ required: true, message: '请选择 IB 账户' }]}>
                                                <Select placeholder="选择 IB 账户">
                                                    {ibAccounts.map(account => (
                                                        <Select.Option key={account.id} value={account.id}>
                                                            {account.name} (Port: {account.ib_port})
                                                        </Select.Option>
                                                    ))}
                                                </Select>
                                            </Form.Item>
                                        );
                                    }
                                }}
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={8}>
                            <Form.Item name="total_position_ratio" label="总仓位比例 (%)">
                                <InputNumber style={{ width: '100%' }} min={0} max={100} />
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item name="total_amount" label="总金额 (优先)">
                                <InputNumber style={{ width: '100%' }} />
                            </Form.Item>
                        </Col>
                        <Col span={8}>
                            <Form.Item name="tracking_error_pct" label="跟踪误差 (%)">
                                <InputNumber style={{ width: '100%' }} min={0} max={100} />
                            </Form.Item>
                        </Col>
                    </Row>
                </Form>
            </Modal>

            {/* Snowball Config Modal */}
            <Modal
                title={snowballEditingConfig ? "编辑雪球跟单配置" : "添加雪球跟单配置"}
                visible={snowballModalVisible}
                onCancel={() => setSnowballModalVisible(false)}
                onOk={() => snowballForm.submit()}
                width={700}
            >
                <Form form={snowballForm} layout="vertical" onFinish={handleSnowballSave} initialValues={{ enabled: true, tracking_error_pct: 1, live_trade_enabled: false }}>
                    <Form.Item name="enabled" valuePropName="checked">
                        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                    </Form.Item>
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item label="雪球组合ID" required>
                                <Space.Compact style={{ width: '100%' }}>
                                    <Form.Item name="combination_id" noStyle rules={[{ required: true, message: '请输入组合ID' }]}>
                                        <Input placeholder="例如: ZH123456" disabled={!!snowballEditingConfig} />
                                    </Form.Item>
                                    <Button onClick={fetchSnowballName}>获取名称</Button>
                                </Space.Compact>
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="combination_name" label="组合名称">
                                <Input placeholder="自动获取或手动输入" />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={16}>
                        <Col span={24}>
                            <Form.Item name="blacklisted_symbols" label="跟单黑名单 (不买入/若持有会卖出)">
                                <Select mode="tags" style={{ width: '100%' }} placeholder="输入股票代码 (如 SH.600519), 回车确认" tokenSeparators={[',', ' ']} />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={16}>
                        <Col span={8}>
                            <Form.Item name="tracking_error_pct" label="跟踪误差 (%)">
                                <InputNumber style={{ width: '100%' }} min={0} max={100} step={0.1} />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={16}>
                        <Col span={6}>
                            <Form.Item name="live_trade_enabled" label="通用执行器实盘" valuePropName="checked">
                                <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                            </Form.Item>
                        </Col>
                        <Col span={9}>
                            <Form.Item
                                name="external_trading_account_id"
                                label="外部交易账户"
                                rules={[
                                    ({ getFieldValue }) => ({
                                        validator(_, value) {
                                            if (!getFieldValue('live_trade_enabled') || value) {
                                                return Promise.resolve();
                                            }
                                            return Promise.reject(new Error('请选择外部交易账户'));
                                        },
                                    }),
                                ]}
                            >
                                <Select
                                    allowClear
                                    showSearch
                                    optionFilterProp="label"
                                    options={externalTradingAccountOptions}
                                    placeholder="选择外部交易账户"
                                    onChange={() => snowballForm.setFieldsValue({ live_sub_account_id: undefined })}
                                />
                            </Form.Item>
                        </Col>
                        <Col span={9}>
                            <Form.Item
                                name="live_sub_account_id"
                                label="虚拟子账户"
                                rules={[
                                    ({ getFieldValue }) => ({
                                        validator(_, value) {
                                            if (!getFieldValue('live_trade_enabled') || value) {
                                                return Promise.resolve();
                                            }
                                            return Promise.reject(new Error('请选择虚拟子账户'));
                                        },
                                    }),
                                ]}
                            >
                                <Select
                                    allowClear
                                    showSearch
                                    optionFilterProp="label"
                                    options={snowballLiveSubAccountOptions}
                                    placeholder={selectedSnowballExternalTradingAccountId ? '选择虚拟子账户' : '先选择外部账户'}
                                    disabled={!selectedSnowballExternalTradingAccountId}
                                />
                            </Form.Item>
                        </Col>
                    </Row>
                </Form>
            </Modal>

            <Modal
                title={currentSnapshotTitle}
                visible={snapshotModalVisible}
                onCancel={() => setSnapshotModalVisible(false)}
                footer={null}
                width={1120}
            >
                {snapshotLoading ? (
                    <div style={{ textAlign: 'center', padding: '20px' }}><ReloadOutlined spin /> 加载中...</div>
                ) : snapshotData ? (
                    <div>
                        <Row gutter={16} style={{ marginBottom: 20 }}>
                            <Col span={6}>
                                <Card size="small" bodyStyle={{ padding: '12px' }}>
                                    <Text type="secondary" style={{ fontSize: '12px' }}>子账户净值</Text>
                                    <div style={{ fontSize: '20px', fontWeight: 'bold', color: '#1890ff' }}>
                                        {formatMoney(snapshotData.ledger_net_asset)}
                                    </div>
                                    <div style={{ marginTop: 4 }}>
                                        <Text type="secondary">{snapshotData.sub_account_name || '未绑定子账户'}</Text>
                                    </div>
                                </Card>
                            </Col>
                            <Col span={6}>
                                <Card size="small" bodyStyle={{ padding: '12px' }}>
                                    <Text type="secondary" style={{ fontSize: '12px' }}>目标股票市值</Text>
                                    <div style={{ fontSize: '18px', fontWeight: 'bold' }}>
                                        {formatMoney(snapshotData.target_market_value)}
                                    </div>
                                    <div style={{ marginTop: 4 }}>
                                        <Text type="secondary">目标现金: </Text>
                                        <Text>{formatMoney(snapshotData.target_cash)}</Text>
                                    </div>
                                </Card>
                            </Col>
                            <Col span={6}>
                                <Card size="small" bodyStyle={{ padding: '12px' }}>
                                    <Text type="secondary" style={{ fontSize: '12px' }}>账本股票市值</Text>
                                    <div style={{ fontSize: '18px', fontWeight: 'bold', color: '#52c41a' }}>
                                        {formatMoney(snapshotData.ledger_market_value)}
                                    </div>
                                    <div style={{ marginTop: 4 }}>
                                        <Text type="secondary">现金: </Text>
                                        <Text>{formatMoney(snapshotData.ledger_cash)}</Text>
                                    </div>
                                </Card>
                            </Col>
                            <Col span={6}>
                                <Card size="small" bodyStyle={{ padding: '12px' }}>
                                    <Text type="secondary" style={{ fontSize: '12px' }}>差异</Text>
                                    <div style={{ fontSize: '18px', fontWeight: 'bold', color: diffColor((snapshotData.target_market_value || 0) - (snapshotData.ledger_market_value || 0)) }}>
                                        {formatSignedMoney((snapshotData.target_market_value || 0) - (snapshotData.ledger_market_value || 0))}
                                    </div>
                                    <div style={{ marginTop: 4 }}>
                                        <Text type="secondary">标的差异: </Text>
                                        <Tag color={snapshotData.diff_count ? 'orange' : 'green'}>{snapshotData.diff_count || 0}</Tag>
                                    </div>
                                </Card>
                            </Col>
                        </Row>

                        <div style={{ marginBottom: 16 }}>
                            <Space wrap>
                                <Text type="secondary">抓取时间: {new Date(snapshotData.updated_at).toLocaleString()}</Text>
                                <Text type="secondary">现金差额: </Text>
                                <Text style={{ color: diffColor(snapshotData.cash_diff) }}>{formatSignedMoney(snapshotData.cash_diff)}</Text>
                            </Space>
                        </div>

                        <Table
                            dataSource={snapshotData.holdings}
                            pagination={false}
                            size="small"
                            rowKey="symbol"
                            scroll={{ x: 1400, y: 400 }}
                            columns={[
                                {
                                    title: '标的',
                                    dataIndex: 'symbol',
                                    key: 'symbol',
                                    width: 180,
                                    fixed: 'left',
                                    render: (text, record) => (
                                        <Space direction="vertical" size={0}>
                                            <Space size={4} wrap>
                                                <Text strong>{text}</Text>
                                                {record.blacklisted && <Tag color="default">黑名单</Tag>}
                                            </Space>
                                            {record.name && <Text type="secondary" style={{ fontSize: '12px' }}>{record.name}</Text>}
                                        </Space>
                                    )
                                },
                                {
                                    title: '雪球权重',
                                    dataIndex: 'xueqiu_weight_pct',
                                    key: 'xueqiu_weight_pct',
                                    width: 100,
                                    align: 'right',
                                    render: (val) => formatPercent(val)
                                },
                                {
                                    title: '目标权重',
                                    dataIndex: 'target_weight_pct',
                                    key: 'target_weight_pct',
                                    width: 100,
                                    align: 'right',
                                    render: (val) => <Tag color="blue">{formatPercent(val)}</Tag>
                                },
                                {
                                    title: '账本权重',
                                    dataIndex: 'ledger_weight_pct',
                                    key: 'ledger_weight_pct',
                                    width: 100,
                                    align: 'right',
                                    render: (val) => formatPercent(val)
                                },
                                {
                                    title: '目标股数',
                                    dataIndex: 'target_quantity',
                                    key: 'target_quantity',
                                    width: 100,
                                    align: 'right',
                                    render: (val) => formatQuantity(val)
                                },
                                {
                                    title: '账本股数',
                                    dataIndex: 'ledger_quantity',
                                    key: 'ledger_quantity',
                                    width: 100,
                                    align: 'right',
                                    render: (val) => formatQuantity(val)
                                },
                                {
                                    title: '股数差额',
                                    dataIndex: 'quantity_diff',
                                    key: 'quantity_diff',
                                    width: 100,
                                    align: 'right',
                                    render: (val) => <Tag color={diffTagColor(val)}>{formatSignedQuantity(val)}</Tag>
                                },
                                {
                                    title: '最新价',
                                    dataIndex: 'price',
                                    key: 'price',
                                    width: 90,
                                    align: 'right',
                                    render: (val) => formatMoney(val, 3)
                                },
                                {
                                    title: '参考/保护价',
                                    key: 'reference_protection_price',
                                    width: 140,
                                    align: 'right',
                                    render: (_, record) => (
                                        <Space direction="vertical" size={0}>
                                            <Text>{record.reference_price ? formatMoney(record.reference_price, 3) : '-'}</Text>
                                            <Text type="secondary" style={{ fontSize: '12px' }}>
                                                {record.execution_protection_price ? `保护 ${formatMoney(record.execution_protection_price, 3)}` : '保护 -'}
                                            </Text>
                                            {record.executor_max_slippage_pct !== null && record.executor_max_slippage_pct !== undefined && (
                                                <Text type="secondary" style={{ fontSize: '12px' }}>
                                                    滑点 {record.executor_max_slippage_pct}%
                                                </Text>
                                            )}
                                        </Space>
                                    )
                                },
                                {
                                    title: '目标市值',
                                    dataIndex: 'target_value',
                                    key: 'target_value',
                                    width: 110,
                                    align: 'right',
                                    render: (val) => formatMoney(val)
                                },
                                {
                                    title: '账本市值',
                                    dataIndex: 'ledger_market_value',
                                    key: 'ledger_market_value',
                                    width: 110,
                                    align: 'right',
                                    render: (val) => formatMoney(val)
                                },
                                {
                                    title: '市值差额',
                                    dataIndex: 'value_diff',
                                    key: 'value_diff',
                                    width: 110,
                                    align: 'right',
                                    render: (val) => <Text style={{ color: diffColor(val) }}>{formatSignedMoney(val)}</Text>
                                },
                                {
                                    title: '权重差额',
                                    dataIndex: 'weight_diff_pct',
                                    key: 'weight_diff_pct',
                                    width: 100,
                                    align: 'right',
                                    render: (val) => <Text style={{ color: diffColor(val) }}>{formatSignedPercent(val)}</Text>
                                },
                                {
                                    title: '差异类型',
                                    dataIndex: 'diff_type',
                                    key: 'diff_type',
                                    width: 100,
                                    render: (val) => {
                                        const colorMap = {
                                            BUY: 'green',
                                            SELL: 'red',
                                            TARGET_ONLY: 'gold',
                                            LEDGER_ONLY: 'orange',
                                            MATCHED: 'default',
                                        };
                                        return <Tag color={colorMap[val] || 'default'}>{val}</Tag>;
                                    }
                                },
                            ]}
                        />
                    </div>
                ) : (
                    <div style={{ textAlign: 'center', color: '#999' }}>暂无数据</div>
                )}
            </Modal>

            <Modal
                title={`雪球回测 - ${snowballBacktestTarget?.combination_name || snowballBacktestTarget?.combination_id || ''}`}
                visible={snowballBacktestModalVisible}
                onCancel={() => setSnowballBacktestModalVisible(false)}
                onOk={() => snowballBacktestForm.submit()}
                confirmLoading={snowballBacktestLoading}
                width={420}
            >
                <Form
                    form={snowballBacktestForm}
                    layout="vertical"
                    onFinish={handleSnowballBacktestSubmit}
                    initialValues={{ slippage_pct: 0.5 }}
                >
                    <Form.Item
                        name="slippage_pct"
                        label="单边滑点 (%)"
                        rules={[{ required: true, message: '请输入单边滑点' }]}
                    >
                        <InputNumber style={{ width: '100%' }} min={0} max={10} step={0.1} precision={2} />
                    </Form.Item>
                </Form>
            </Modal>

            <Modal
                title={`雪球回测历史 - ${snowballBacktestTarget?.combination_name || snowballBacktestTarget?.combination_id || ''}`}
                visible={snowballBacktestHistoryVisible}
                onCancel={() => setSnowballBacktestHistoryVisible(false)}
                footer={null}
                width={1280}
            >
                <Row gutter={16}>
                    <Col span={8}>
                        <div style={{ marginBottom: 12 }}>
                            <Button
                                icon={<ReloadOutlined />}
                                onClick={() => fetchSnowballBacktestRuns()}
                                loading={snowballBacktestRunsLoading}
                            >刷新</Button>
                        </div>
                        <Table
                            dataSource={snowballBacktestRuns}
                            rowKey="id"
                            size="small"
                            loading={snowballBacktestRunsLoading}
                            pagination={{ pageSize: 8, showSizeChanger: false }}
                            onRow={record => ({
                                onClick: () => fetchSnowballBacktestDetail(record),
                            })}
                            columns={[
                                {
                                    title: '时间',
                                    dataIndex: 'created_at',
                                    width: 150,
                                    render: value => value ? dayjs(value).format('MM-DD HH:mm') : '-',
                                },
                                {
                                    title: '滑点',
                                    dataIndex: 'slippage_pct',
                                    width: 70,
                                    align: 'right',
                                    render: value => formatOptionalPercent(value),
                                },
                                {
                                    title: '状态',
                                    dataIndex: 'status',
                                    width: 90,
                                    render: value => {
                                        const color = value === 'SUCCESS' ? 'green' : value === 'RUNNING' ? 'blue' : 'red';
                                        return <Tag color={color}>{value}</Tag>;
                                    },
                                },
                                {
                                    title: '滑点后收益',
                                    key: 'return',
                                    align: 'right',
                                    render: (_, record) => formatOptionalPercent(record.performance_after_slippage?.total_return_pct),
                                },
                            ]}
                        />
                    </Col>
                    <Col span={16}>
                        {selectedSnowballBacktestLoading ? (
                            <div style={{ textAlign: 'center', padding: 48 }}><ReloadOutlined spin /> 加载中...</div>
                        ) : selectedSnowballBacktest ? (
                            <Space direction="vertical" size={16} style={{ width: '100%' }}>
                                <Space wrap>
                                    <Tag color={selectedSnowballBacktest.status === 'SUCCESS' ? 'green' : selectedSnowballBacktest.status === 'RUNNING' ? 'blue' : 'red'}>
                                        {selectedSnowballBacktest.status}
                                    </Tag>
                                    <Text type="secondary">
                                        区间: {selectedSnowballBacktest.actual_nav_start || '-'} 至 {selectedSnowballBacktest.actual_nav_end || '-'}
                                    </Text>
                                    <Text type="secondary">
                                        调仓: {selectedSnowballBacktest.rebalancing?.rebalance_count ?? '-'} 次
                                    </Text>
                                </Space>
                                {selectedSnowballBacktest.error_message && (
                                    <Text type="danger">{selectedSnowballBacktest.error_message}</Text>
                                )}
                                <Row gutter={12}>
                                    {[
                                        ['滑点后', selectedSnowballBacktest.performance_after_slippage],
                                        ['原始', selectedSnowballBacktest.performance_raw],
                                        ['中证500', selectedSnowballBacktest.benchmark_metrics],
                                    ].map(([label, metrics]) => (
                                        <Col span={8} key={label}>
                                            <Card size="small" bodyStyle={{ padding: 12 }}>
                                                <Text type="secondary">{label}</Text>
                                                <div style={{ marginTop: 8, fontSize: 20, fontWeight: 600 }}>
                                                    {formatOptionalPercent(metrics?.total_return_pct)}
                                                </div>
                                                <Space direction="vertical" size={0} style={{ marginTop: 8 }}>
                                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                                        年化 {formatOptionalPercent(metrics?.annualized_return_pct)}
                                                    </Text>
                                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                                        夏普 {formatOptionalNumber(metrics?.sharpe)}
                                                    </Text>
                                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                                        最大回撤 {formatOptionalPercent(metrics?.max_drawdown_pct)}
                                                    </Text>
                                                </Space>
                                            </Card>
                                        </Col>
                                    ))}
                                </Row>
                                {(selectedSnowballBacktest.curve_points || []).length ? (
                                    <ReactECharts option={getSnowballBacktestChartOption()} style={{ height: 380 }} />
                                ) : (
                                    <Empty description="暂无曲线数据" />
                                )}
                                <Table
                                    dataSource={selectedSnowballBacktest.yearly_returns || []}
                                    rowKey="year"
                                    size="small"
                                    pagination={false}
                                    columns={[
                                        { title: '年份', dataIndex: 'year', width: 90 },
                                        {
                                            title: '滑点后',
                                            dataIndex: 'slippage_return_pct',
                                            align: 'right',
                                            render: value => formatOptionalPercent(value),
                                        },
                                        {
                                            title: '原始',
                                            dataIndex: 'raw_return_pct',
                                            align: 'right',
                                            render: value => formatOptionalPercent(value),
                                        },
                                        {
                                            title: '中证500',
                                            dataIndex: 'benchmark_return_pct',
                                            align: 'right',
                                            render: value => formatOptionalPercent(value),
                                        },
                                        {
                                            title: '超额',
                                            dataIndex: 'excess_return_after_slippage_pct',
                                            align: 'right',
                                            render: value => formatOptionalPercent(value),
                                        },
                                    ]}
                                />
                            </Space>
                        ) : (
                            <Empty description="暂无回测记录" />
                        )}
                    </Col>
                </Row>
            </Modal>

            {/* Snowball Account Config Modal */}
            <Modal
                title="雪球账号全局配置"
                visible={snowballAccountModalVisible}
                onCancel={() => setSnowballAccountModalVisible(false)}
                onOk={() => snowballAccountForm.submit()}
                width={700}
            >
                <Form form={snowballAccountForm} layout="vertical" onFinish={handleSnowballAccountSave}>
                    <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
                        最近一次更新时间：{snowballAccountConfig?.updated_at ? dayjs(snowballAccountConfig.updated_at).format('YYYY-MM-DD HH:mm:ss') : '暂无'}
                    </Text>
                    <Row gutter={16}>
                        <Col span={24}>
                            <Form.Item name="xueqiu_cookie" label="雪球全局 Cookie" help="若默认Token失效，可在浏览器抓包获取Cookie并在此时填入。所有组合将共用此配置。支持 'xq_a_token=...' 或完整Cookie字符串。">
                                <Input.TextArea rows={3} placeholder="xq_a_token=..." />
                            </Form.Item>
                        </Col>
                    </Row>
                </Form>
            </Modal>

        </div >
    );
};

export default PortfolioCopyTrading;
