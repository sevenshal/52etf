import React, { useState } from 'react';
import { Card, Form, Input, InputNumber, Button, DatePicker, Row, Col, Statistic, Table, Divider, message, Space } from 'antd';
import { PlusOutlined, MinusCircleOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';

const { RangePicker } = DatePicker;

const AllWeatherBacktest = () => {
    const [loading, setLoading] = useState(false);
    const [result, setResult] = useState(null);
    const [form] = Form.useForm();

    const onFinish = async (values) => {
        setLoading(true);
        try {
            const payload = {
                assets: values.assets,
                initial_capital: values.initial_capital,
                rebalance_months: values.rebalance_months,
                drift_threshold: values.drift_threshold / 100, // Convert percentage to ratio
                start_date: values.date_range ? values.date_range[0].format('YYYY-MM-DD') : '2015-01-01',
                end_date: values.date_range ? values.date_range[1].format('YYYY-MM-DD') : dayjs().format('YYYY-MM-DD'),
            };

            const response = await request.post('/api/all-weather-backtest', payload);
            setResult(response.data);
            message.success('回测完成');
        } catch (error) {
            console.error(error);
            message.error(error.response?.data?.detail || '回测失败');
        } finally {
            setLoading(false);
        }
    };

    const getChartOption = () => {
        if (!result) return {};

        const dates = result.equity_curve.map(item => item.date);
        const values = result.equity_curve.map(item => item.value.toFixed(2));

        // Find rebalance markings
        const markPoints = result.rebalance_events.map(event => ({
            name: event.reason,
            xAxis: event.date,
            yAxis: event.value,
            value: 'R'
        }));

        return {
            title: { text: '净值曲线', left: 'center' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: dates },
            yAxis: { type: 'value', scale: true },
            series: [{
                name: '组合价值',
                type: 'line',
                data: values,
                smooth: true,
                showSymbol: false,
                lineStyle: { width: 2, color: '#1890ff' },
                markPoint: {
                    data: markPoints,
                    symbol: 'pin',
                    symbolSize: 20,
                    itemStyle: { color: '#fadb14' },
                    label: { show: false }
                }
            }],
            grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true }
        };
    };

    const columns = [
        { title: '日期', dataIndex: 'date', key: 'date' },
        { title: '原因', dataIndex: 'reason', key: 'reason' },
        { title: '当时市值', dataIndex: 'value', key: 'value', render: val => val.toFixed(2) },
    ];

    return (
        <div style={{ padding: '24px' }}>
            <Card title="全天候策略回测" style={{ marginBottom: '24px' }}>
                <Form
                    form={form}
                    layout="vertical"
                    onFinish={onFinish}
                    initialValues={{
                        assets: [
                            { symbol: 'SPMO.US', weight: 0.3 },
                            { symbol: 'TLT.US', weight: 0.4 },
                            { symbol: 'IEF.US', weight: 0.15 },
                            { symbol: 'GLD.US', weight: 0.075 },
                            { symbol: 'DBC.US', weight: 0.075 }
                        ],
                        initial_capital: 10000,
                        rebalance_months: 6,
                        drift_threshold: 20,
                        date_range: [dayjs('2015-01-01'), dayjs()],
                    }}
                >
                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item label="初始资金" name="initial_capital" rules={[{ required: true }]}>
                                <InputNumber style={{ width: '100%' }} prefix="$" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item label="回测时间范围" name="date_range">
                                <RangePicker style={{ width: '100%' }} />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Row gutter={16}>
                        <Col span={12}>
                            <Form.Item label="定期再平衡周期 (月)" name="rebalance_months">
                                <InputNumber style={{ width: '100%' }} min={0} placeholder="0表示不定期平衡" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item label="偏离阈值 (%)" name="drift_threshold">
                                <InputNumber style={{ width: '100%' }} min={0} placeholder="0表示不按偏离平衡" />
                            </Form.Item>
                        </Col>
                    </Row>

                    <Divider orientation="left">资产配置</Divider>
                    <Form.List name="assets">
                        {(fields, { add, remove }) => (
                            <>
                                {fields.map(({ key, name, ...restField }) => (
                                    <Space key={key} style={{ display: 'flex', marginBottom: 8 }} align="baseline">
                                        <Form.Item
                                            {...restField}
                                            name={[name, 'symbol']}
                                            rules={[{ required: true, message: '请输入代码' }]}
                                        >
                                            <Input placeholder="代码 (如 SPMO.US)" />
                                        </Form.Item>
                                        <Form.Item
                                            {...restField}
                                            name={[name, 'weight']}
                                            rules={[{ required: true, message: '请输入权重' }]}
                                        >
                                            <InputNumber
                                                min={0}
                                                max={1}
                                                step={0.01}
                                                placeholder="权重 (0-1)"
                                                style={{ width: '120px' }}
                                            />
                                        </Form.Item>
                                        <MinusCircleOutlined onClick={() => remove(name)} />
                                    </Space>
                                ))}
                                <Form.Item>
                                    <Button type="dashed" onClick={() => add()} block icon={<PlusOutlined />}>
                                        添加资产
                                    </Button>
                                </Form.Item>
                            </>
                        )}
                    </Form.List>

                    <Form.Item>
                        <Button type="primary" htmlType="submit" loading={loading} block>
                            开始回测
                        </Button>
                    </Form.Item>
                </Form>
            </Card>

            {result && (
                <Card>
                    <Row gutter={16} style={{ marginBottom: '24px' }}>
                        <Col span={6}>
                            <Statistic title="总收益率" value={result.metrics.total_return * 100} precision={2} suffix="%" />
                        </Col>
                        <Col span={6}>
                            <Statistic title="年化收益率" value={result.metrics.annualized_return * 100} precision={2} suffix="%" />
                        </Col>
                        <Col span={6}>
                            <Statistic title="最大回撤" value={result.metrics.max_drawdown * 100} precision={2} suffix="%" valueStyle={{ color: '#cf1322' }} />
                        </Col>
                        <Col span={6}>
                            <Statistic title="Sharpe Ratio" value={result.metrics.sharpe_ratio} precision={2} />
                        </Col>
                    </Row>

                    <ReactECharts option={getChartOption()} style={{ height: '400px', marginBottom: '24px' }} />

                    <Divider orientation="left">再平衡历史</Divider>
                    <Table
                        dataSource={result.rebalance_events}
                        columns={columns}
                        rowKey="date"
                        pagination={{ pageSize: 5 }}
                        size="small"
                    />
                </Card>
            )}
        </div>
    );
};

export default AllWeatherBacktest;
