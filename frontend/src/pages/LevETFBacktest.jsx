import React, { useState, useEffect, useRef } from 'react';
import { Card, Form, Select, InputNumber, Button, DatePicker, Table, Statistic, Row, Col, message, Progress } from 'antd';
import request from '../utils/request';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';

const { Option } = Select;
const { RangePicker } = DatePicker;

const LevETFBacktest = () => {
    const [loading, setLoading] = useState(false);
    const [batchResults, setBatchResults] = useState(null);
    const [detailedResult, setDetailedResult] = useState(null);

    // Async Polling State
    const [taskId, setTaskId] = useState(null);
    const [progress, setProgress] = useState(0);
    const pollingTimer = useRef(null);

    const [form] = Form.useForm();

    // Clean up timer on unmount
    useEffect(() => {
        return () => {
            if (pollingTimer.current) clearInterval(pollingTimer.current);
        };
    }, []);

    const pollStatus = async (id) => {
        try {
            const response = await request.get(`/api/lev-etf-backtest/batch-run/${id}`);
            const { status, result, progress: jobProgress, error } = response.data;

            setProgress(jobProgress);

            if (status === 'completed') {
                clearInterval(pollingTimer.current);
                setBatchResults(result);
                setLoading(false);
                setTaskId(null);
                message.success(`Backtest completed. Found ${result.length} combinations.`);
            } else if (status === 'failed') {
                clearInterval(pollingTimer.current);
                setLoading(false);
                setTaskId(null);
                message.error(`Backtest failed: ${error}`);
            }
            // If running or pending, continue polling...

        } catch (error) {
            console.error("Polling error", error);
        }
    };

    const onFinish = async (values) => {
        setLoading(true);
        setDetailedResult(null);
        setBatchResults(null);
        setTaskId(null);
        setProgress(0);

        try {
            const payload = {
                etf_code: values.etf_code,
                short_window_min: values.short_window_min,
                short_window_max: values.short_window_max,
                long_window_min: values.long_window_min,
                long_window_max: values.long_window_max,
                initial_capital: values.initial_capital,
                start_date: values.date_range ? values.date_range[0].format('YYYY-MM-DD') : undefined,
                end_date: values.date_range ? values.date_range[1].format('YYYY-MM-DD') : undefined,
            };

            const response = await request.post('/api/lev-etf-backtest/batch-run', payload);
            const { task_id, status } = response.data;

            setTaskId(task_id);

            // Start Polling
            pollingTimer.current = setInterval(() => pollStatus(task_id), 1000);

        } catch (error) {
            console.error(error);
            setLoading(false);
            message.error(error.response?.data?.detail || 'Backtest failed');
        }
    };

    const runDetailedBacktest = async (record) => {
        setLoading(true);
        try {
            const values = form.getFieldsValue();
            const payload = {
                etf_code: values.etf_code,
                short_window: record.short_window,
                long_window: record.long_window,
                initial_capital: values.initial_capital,
                start_date: values.date_range ? values.date_range[0].format('YYYY-MM-DD') : undefined,
                end_date: values.date_range ? values.date_range[1].format('YYYY-MM-DD') : undefined,
            };

            const response = await request.post('/api/lev-etf-backtest/run', payload);
            setDetailedResult(response.data);

            // Scroll to detailed view
            setTimeout(() => {
                document.getElementById('detailed-view')?.scrollIntoView({ behavior: 'smooth' });
            }, 100);

        } catch (error) {
            message.error('Failed to load detailed view');
        } finally {
            setLoading(false);
        }
    };

    const getPriceChartOption = () => {
        if (!detailedResult) return {};

        const dates = detailedResult.daily_data.map(item => item.date);
        const data = detailedResult.daily_data.map(item => [item.open, item.close, item.low, item.high]);
        const emaShort = detailedResult.daily_data.map(item => item.ema_short);
        const emaLong = detailedResult.daily_data.map(item => item.ema_long);

        // Prepare Markers
        const buyPoints = detailedResult.trades.filter(t => t.action === 'BUY').map(t => ({
            coord: [t.date, t.price],
            value: 'Buy',
            itemStyle: { color: '#ef5350' }
        }));

        const sellPoints = detailedResult.trades.filter(t => t.action === 'SELL').map(t => ({
            coord: [t.date, t.price],
            value: 'Sell',
            itemStyle: { color: '#66bb6a' }
        }));

        return {
            title: { text: `Price History & Signals (${detailedResult.params.etf_code})` },
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'cross' }
            },
            legend: { data: ['K-Line', `EMA ${detailedResult.params.short_window}`, `EMA ${detailedResult.params.long_window}`] },
            grid: { left: '10%', right: '10%', bottom: '15%' },
            xAxis: { type: 'category', data: dates, scale: true, boundaryGap: false },
            yAxis: { scale: true, splitLine: { show: false } },
            dataZoom: [
                { type: 'inside', start: 50, end: 100 },
                { type: 'slider', show: true }
            ],
            series: [
                {
                    name: 'K-Line',
                    type: 'candlestick',
                    data: data,
                    itemStyle: {
                        color: '#ef5350',
                        color0: '#66bb6a',
                        borderColor: '#ef5350',
                        borderColor0: '#66bb6a'
                    },
                    markPoint: {
                        data: [...buyPoints, ...sellPoints],
                        symbol: 'arrow',
                        symbolSize: 10,
                        label: { show: false }
                    }
                },
                {
                    name: `EMA ${detailedResult.params.short_window}`,
                    type: 'line',
                    data: emaShort,
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { opacity: 0.5 }
                },
                {
                    name: `EMA ${detailedResult.params.long_window}`,
                    type: 'line',
                    data: emaLong,
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { opacity: 0.5 }
                }
            ]
        };
    };

    const getEquityOption = () => {
        if (!detailedResult) return {};
        const dates = detailedResult.equity_curve.map(item => item.date);
        const values = detailedResult.equity_curve.map(item => item.value);
        return {
            title: { text: '资金曲线' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: dates },
            yAxis: { type: 'value', scale: true },
            series: [{
                data: values,
                type: 'line',
                smooth: true,
                name: '账户净值',
                areaStyle: {}
            }]
        };
    };

    const getYearlyReturnOption = () => {
        if (!detailedResult || !detailedResult.yearly_returns) return {};
        const years = detailedResult.yearly_returns.map(item => item.year);
        const values = detailedResult.yearly_returns.map(item => item.return);

        return {
            title: { text: '年度回报' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: years },
            yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
            series: [{
                data: values,
                type: 'bar',
                name: '回报率',
                itemStyle: {
                    color: (params) => {
                        return params.value >= 0 ? '#ef5350' : '#66bb6a';
                    }
                },
                label: {
                    show: true,
                    position: 'top',
                    formatter: (params) => params.value.toFixed(2) + '%'
                }
            }]
        };
    };

    const columns = [
        { title: '日期', dataIndex: 'date', key: 'date' },
        { title: '操作', dataIndex: 'action', key: 'action', render: (text) => <span style={{ color: text === 'BUY' ? '#ef5350' : '#66bb6a' }}>{text === 'BUY' ? '买入' : '卖出'}</span> },
        { title: '价格', dataIndex: 'price', key: 'price', render: (val) => val.toFixed(2) },
        { title: '数量', dataIndex: 'quantity', key: 'quantity', render: (val) => val.toFixed(2) },
        { title: '金额', dataIndex: 'amount', key: 'amount', render: (val) => val.toFixed(2) },
        {
            title: '盈亏',
            dataIndex: 'profit',
            key: 'profit',
            render: (val, record) => record.action === 'SELL' ? <span style={{ color: val >= 0 ? '#ef5350' : '#66bb6a' }}>{val.toFixed(2)}</span> : '-'
        },
        {
            title: '回报率',
            dataIndex: 'percent',
            key: 'percent',
            render: (val, record) => record.action === 'SELL' ? <span style={{ color: val >= 0 ? '#ef5350' : '#66bb6a' }}>{val.toFixed(2)}%</span> : '-'
        },
    ];

    const batchColumns = [
        { title: '快线', dataIndex: 'short_window', key: 'short_window', sorter: (a, b) => a.short_window - b.short_window },
        { title: '慢线', dataIndex: 'long_window', key: 'long_window', sorter: (a, b) => a.long_window - b.long_window },
        { title: '总回报率', dataIndex: 'total_return', key: 'total_return', render: (val) => <span style={{ color: val >= 0 ? '#ef5350' : '#66bb6a' }}>{val.toFixed(2)}%</span>, sorter: (a, b) => a.total_return - b.total_return },
        { title: '年化 (CAGR)', dataIndex: 'annualized_return', key: 'annualized_return', render: (val) => `${val.toFixed(2)}%`, sorter: (a, b) => a.annualized_return - b.annualized_return },
        { title: '最大回撤', dataIndex: 'max_drawdown', key: 'max_drawdown', render: (val) => `${val.toFixed(2)}%`, sorter: (a, b) => a.max_drawdown - b.max_drawdown },
        { title: '赢率', dataIndex: 'win_rate', key: 'win_rate', render: (val) => `${val.toFixed(2)}%`, sorter: (a, b) => a.win_rate - b.win_rate },
        { title: 'Sharpe', dataIndex: 'sharpe_ratio', key: 'sharpe_ratio', render: (val) => val.toFixed(2), sorter: (a, b) => a.sharpe_ratio - b.sharpe_ratio },
        { title: '操作', key: 'action', render: (_, record) => <Button type="link" size="small" onClick={() => runDetailedBacktest(record)}>详情</Button> }
    ];

    // Calculate Average Annual Return (Simple Average of Yearly Returns)
    const avgAnnualReturn = detailedResult && detailedResult.yearly_returns && detailedResult.yearly_returns.length > 0
        ? detailedResult.yearly_returns.reduce((sum, item) => sum + item.return, 0) / detailedResult.yearly_returns.length
        : 0;

    return (
        <div style={{ padding: '24px' }}>
            <Card title="杠杆ETF均线穿越策略回测 (批量)" style={{ marginBottom: 24 }}>
                <Form
                    form={form}
                    layout="inline"
                    onFinish={onFinish}
                    initialValues={{
                        etf_code: 'TQQQ',
                        short_window_min: 1,
                        short_window_max: 10,
                        long_window_min: 11,
                        long_window_max: 60,
                        initial_capital: 10000,
                        date_range: [dayjs('2015-01-01'), dayjs()],
                    }}
                >
                    <Form.Item name="etf_code" label="标的" rules={[{ required: true }]}>
                        <Select style={{ width: 100 }}>
                            <Option value="TQQQ">TQQQ</Option>
                            <Option value="SOXL">SOXL</Option>
                            <Option value="CONL">CONL</Option>
                            <Option value="SQQQ">SQQQ</Option>
                            <Option value="QQQ">QQQ</Option>
                            <Option value="SPMO">SPMO</Option>
                            <Option value="NAIL">NAIL</Option>
                            <Option value="LABU">LABU</Option>
                            <Option value="UPRO">UPRO</Option>
                            <Option value="TNA">TNA</Option>
                            <Option value="YINN">YINN</Option>
                        </Select>
                    </Form.Item>
                    <Form.Item label="快线范围" style={{ marginBottom: 0 }}>
                        <Form.Item name="short_window_min" style={{ display: 'inline-block', width: '70px' }} rules={[{ required: true }]}>
                            <InputNumber min={1} placeholder="Min" />
                        </Form.Item>
                        <span style={{ display: 'inline-block', width: '10px', lineHeight: '32px', textAlign: 'center' }}>-</span>
                        <Form.Item name="short_window_max" style={{ display: 'inline-block', width: '70px' }} rules={[{ required: true }]}>
                            <InputNumber min={1} placeholder="Max" />
                        </Form.Item>
                    </Form.Item>
                    <Form.Item label="慢线范围" style={{ marginBottom: 0 }}>
                        <Form.Item name="long_window_min" style={{ display: 'inline-block', width: '70px' }} rules={[{ required: true }]}>
                            <InputNumber min={1} placeholder="Min" />
                        </Form.Item>
                        <span style={{ display: 'inline-block', width: '10px', lineHeight: '32px', textAlign: 'center' }}>-</span>
                        <Form.Item name="long_window_max" style={{ display: 'inline-block', width: '70px' }} rules={[{ required: true }]}>
                            <InputNumber min={1} placeholder="Max" />
                        </Form.Item>
                    </Form.Item>
                    <Form.Item name="initial_capital" label="初始资金">
                        <InputNumber min={100} step={100} style={{ width: 100 }} />
                    </Form.Item>
                    <Form.Item name="date_range" label="日期范围">
                        <RangePicker />
                    </Form.Item>
                    <Form.Item>
                        <Button type="primary" htmlType="submit" loading={loading} disabled={loading && taskId !== null}>
                            {loading && taskId !== null ? '运行中...' : '批量回测'}
                        </Button>
                    </Form.Item>
                </Form>

                {taskId && (
                    <div style={{ marginTop: 16 }}>
                        <Progress percent={progress} status="active" />
                        <div style={{ textAlign: 'center', marginTop: 8 }}>正在进行回测 computation...</div>
                    </div>
                )}
            </Card>

            {/* Batch Results Table */}
            {batchResults && (
                <Card title={`回测结果汇总 (共 ${batchResults.length} 组)`} style={{ marginBottom: 24 }}>
                    <Table
                        dataSource={batchResults}
                        columns={batchColumns}
                        rowKey={(record) => `${record.short_window}-${record.long_window}`}
                        pagination={{ pageSize: 10 }}
                        size="small"
                        onRow={(record) => {
                            return {
                                onClick: () => runDetailedBacktest(record),
                                style: { cursor: 'pointer' }
                            };
                        }}
                    />
                </Card>
            )}

            {/* Detailed View */}
            {detailedResult && (
                <div id="detailed-view">
                    <Row gutter={16} style={{ marginBottom: 24 }}>
                        <Col span={4}>
                            <Card>
                                <Statistic title="总回报率" value={detailedResult.total_return} precision={2} suffix="%" valueStyle={{ color: detailedResult.total_return >= 0 ? '#ef5350' : '#66bb6a' }} />
                            </Card>
                        </Col>
                        <Col span={4}>
                            <Card>
                                <Statistic title="年化复合回报 (CAGR)" value={detailedResult.annualized_return} precision={2} suffix="%" valueStyle={{ color: detailedResult.annualized_return >= 0 ? '#ef5350' : '#66bb6a' }} />
                            </Card>
                        </Col>
                        <Col span={4}>
                            <Card>
                                <Statistic title="平均年回报" value={avgAnnualReturn} precision={2} suffix="%" valueStyle={{ color: avgAnnualReturn >= 0 ? '#ef5350' : '#66bb6a' }} />
                            </Card>
                        </Col>
                        <Col span={4}>
                            <Card>
                                <Statistic title="最大回撤" value={detailedResult.max_drawdown} precision={2} suffix="%" valueStyle={{ color: detailedResult.max_drawdown <= 0 ? '#ef5350' : '#66bb6a' }} />
                            </Card>
                        </Col>
                        <Col span={4}>
                            <Card>
                                <Statistic title="赢率" value={detailedResult.win_rate} precision={2} suffix="%" />
                            </Card>
                        </Col>
                        <Col span={4}>
                            <Card>
                                <Statistic title="Sharpe Ratio" value={detailedResult.sharpe_ratio} precision={2} />
                            </Card>
                        </Col>
                    </Row>

                    <Card title={`价格走势与信号 (MA ${detailedResult.params.short_window} / ${detailedResult.params.long_window})`} style={{ marginBottom: 24 }}>
                        <ReactECharts option={getPriceChartOption()} style={{ height: 500 }} />
                    </Card>

                    <Row gutter={16}>
                        <Col span={12}>
                            <Card title="资金曲线" style={{ marginBottom: 24 }}>
                                <ReactECharts option={getEquityOption()} style={{ height: 300 }} />
                            </Card>
                        </Col>
                        <Col span={12}>
                            <Card title="年度回报分布" style={{ marginBottom: 24 }}>
                                <ReactECharts option={getYearlyReturnOption()} style={{ height: 300 }} />
                            </Card>
                        </Col>
                    </Row>

                    <Card title="交易记录">
                        <Table
                            dataSource={detailedResult.trades}
                            columns={columns}
                            rowKey="date"
                            pagination={{ pageSize: 10 }}
                        />
                    </Card>
                </div>
            )}
        </div>
    );
};

export default LevETFBacktest;
