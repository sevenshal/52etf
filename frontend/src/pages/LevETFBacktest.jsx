import React, { useState } from 'react';
import { Card, Form, Select, InputNumber, Button, DatePicker, Table, Statistic, Row, Col, message } from 'antd';
import request from '../utils/request';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';

const { Option } = Select;
const { RangePicker } = DatePicker;

const LevETFBacktest = () => {
    const [loading, setLoading] = useState(false);
    const [result, setResult] = useState(null);
    const [form] = Form.useForm();

    const onFinish = async (values) => {
        setLoading(true);
        try {
            const payload = {
                etf_code: values.etf_code,
                short_window: values.short_window,
                long_window: values.long_window,
                initial_capital: values.initial_capital,
                start_date: values.date_range ? values.date_range[0].format('YYYY-MM-DD') : undefined,
                end_date: values.date_range ? values.date_range[1].format('YYYY-MM-DD') : undefined,
            };

            const response = await request.post('/api/lev-etf-backtest/run', payload);
            setResult(response.data);
            message.success('Backtest completed successfully');
        } catch (error) {
            console.error(error);
            message.error(error.response?.data?.detail || 'Backtest failed');
        } finally {
            setLoading(false);
        }
    };

    const columns = [
        { title: 'Date', dataIndex: 'date', key: 'date' },
        { title: 'Action', dataIndex: 'action', key: 'action', render: (text) => <span style={{ color: text === 'BUY' ? 'green' : 'red' }}>{text}</span> },
        { title: 'Price', dataIndex: 'price', key: 'price', render: (val) => val.toFixed(2) },
        { title: 'Quantity', dataIndex: 'quantity', key: 'quantity', render: (val) => val.toFixed(2) },
        { title: 'Amount', dataIndex: 'amount', key: 'amount', render: (val) => val.toFixed(2) },
        {
            title: 'Profit',
            dataIndex: 'profit',
            key: 'profit',
            render: (val, record) => record.action === 'SELL' ? <span style={{ color: val >= 0 ? 'red' : 'green' }}>{val.toFixed(2)}</span> : '-'
        },
        {
            title: 'Return %',
            dataIndex: 'percent',
            key: 'percent',
            render: (val, record) => record.action === 'SELL' ? <span style={{ color: val >= 0 ? 'red' : 'green' }}>{val.toFixed(2)}%</span> : '-'
        },
    ];

    const getPriceChartOption = () => {
        if (!result) return {};

        const dates = result.daily_data.map(item => item.date);
        const data = result.daily_data.map(item => [item.open, item.close, item.low, item.high]);
        const emaShort = result.daily_data.map(item => item.ema_short);
        const emaLong = result.daily_data.map(item => item.ema_long);

        // Prepare Markers
        const buyPoints = result.trades.filter(t => t.action === 'BUY').map(t => ({
            coord: [t.date, t.price],
            value: 'Buy',
            itemStyle: { color: '#ef5350' }
        }));

        const sellPoints = result.trades.filter(t => t.action === 'SELL').map(t => ({
            coord: [t.date, t.price],
            value: 'Sell',
            itemStyle: { color: '#66bb6a' }
        }));

        return {
            title: { text: `Price History & Signals (${result.params.etf_code})` },
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'cross' }
            },
            legend: { data: ['K-Line', `EMA ${result.params.short_window}`, `EMA ${result.params.long_window}`] },
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
                    name: `EMA ${result.params.short_window}`,
                    type: 'line',
                    data: emaShort,
                    smooth: true,
                    lineStyle: { opacity: 0.5 }
                },
                {
                    name: `EMA ${result.params.long_window}`,
                    type: 'line',
                    data: emaLong,
                    smooth: true,
                    lineStyle: { opacity: 0.5 }
                }
            ]
        };
    };

    const getEquityOption = () => {
        if (!result) return {};

        const dates = result.equity_curve.map(item => item.date);
        const values = result.equity_curve.map(item => item.value);

        return {
            title: { text: 'Equity Curve' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: dates },
            yAxis: { type: 'value', scale: true },
            series: [{
                data: values,
                type: 'line',
                smooth: true,
                name: 'Account Value',
                areaStyle: {}
            }]
        };
    };

    return (
        <div style={{ padding: '24px' }}>
            <Card title="Leveraged ETF Moving Average Crossover Backtest" style={{ marginBottom: 24 }}>
                <Form
                    form={form}
                    layout="inline"
                    onFinish={onFinish}
                    initialValues={{
                        etf_code: 'TQQQ',
                        short_window: 5,
                        long_window: 30,
                        initial_capital: 10000,
                        date_range: [dayjs('2015-01-01'), dayjs()],
                    }}
                >
                    <Form.Item name="etf_code" label="ETF" rules={[{ required: true }]}>
                        <Select style={{ width: 120 }}>
                            <Option value="TQQQ">TQQQ</Option>
                            <Option value="SOXL">SOXL</Option>
                            <Option value="NAIL">NAIL</Option>
                            <Option value="LABU">LABU</Option>
                            <Option value="UPRO">UPRO</Option>
                            <Option value="TNA">TNA</Option>
                            <Option value="YINN">YINN</Option>
                        </Select>
                    </Form.Item>
                    <Form.Item name="short_window" label="Short MA" rules={[{ required: true }]}>
                        <InputNumber min={1} />
                    </Form.Item>
                    <Form.Item name="long_window" label="Long MA" rules={[{ required: true }]}>
                        <InputNumber min={2} />
                    </Form.Item>
                    <Form.Item name="initial_capital" label="Initial Capital">
                        <InputNumber min={100} step={100} />
                    </Form.Item>
                    <Form.Item name="date_range" label="Date Range">
                        <RangePicker />
                    </Form.Item>
                    <Form.Item>
                        <Button type="primary" htmlType="submit" loading={loading}>
                            Run Backtest
                        </Button>
                    </Form.Item>
                </Form>
            </Card>

            {result && (
                <>
                    <Row gutter={16} style={{ marginBottom: 24 }}>
                        <Col span={6}>
                            <Card>
                                <Statistic title="Total Return" value={result.total_return} precision={2} suffix="%" valueStyle={{ color: result.total_return >= 0 ? '#cf1322' : '#3f8600' }} />
                            </Card>
                        </Col>
                        <Col span={6}>
                            <Card>
                                <Statistic title="Annualized Return" value={result.annualized_return} precision={2} suffix="%" valueStyle={{ color: '#cf1322' }} />
                            </Card>
                        </Col>
                        <Col span={6}>
                            <Card>
                                <Statistic title="Max Drawdown" value={result.max_drawdown} precision={2} suffix="%" valueStyle={{ color: '#3f8600' }} />
                            </Card>
                        </Col>
                        <Col span={6}>
                            <Card>
                                <Statistic title="Win Rate" value={result.win_rate} precision={2} suffix="%" />
                            </Card>
                        </Col>
                    </Row>

                    <Card title="Price History with Signals" style={{ marginBottom: 24 }}>
                        <ReactECharts option={getPriceChartOption()} style={{ height: 500 }} />
                    </Card>

                    <Card title="Equity Curve" style={{ marginBottom: 24 }}>
                        <ReactECharts option={getEquityOption()} style={{ height: 300 }} />
                    </Card>

                    <Card title="Trade History">
                        <Table
                            dataSource={result.trades}
                            columns={columns}
                            rowKey="date"
                            pagination={{ pageSize: 10 }}
                        />
                    </Card>
                </>
            )}
        </div>
    );
};

export default LevETFBacktest;
