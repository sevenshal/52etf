import React, { useState, useEffect } from 'react';
import { Card, Form, InputNumber, Input, Button, Table, message, Layout, Tabs } from 'antd';
import request from '../utils/request';

const EVCValuation = () => {
    const [form] = Form.useForm();
    const [stocks, setStocks] = useState([]);
    const [favoriteStocks, setFavoriteStocks] = useState([]);
    const [activeTab, setActiveTab] = useState('all');
    const [favorites, setFavorites] = useState([]);

    // 默认值
    const defaultValues = {
        undervalue_threshold: 0.9,
        next_fy_growth_threshold: 1.1,
        symbol: ''
    };

    const calculateChange = (value, marketPrice) => {
        if (!value || !marketPrice) return '';
        return ((value - marketPrice) / marketPrice * 100).toFixed(2) + '%';
    };
    
    const handleSearch = async (values) => {
        try {
            // 如果输入了股票代码，转换为大写
            if (values.symbol) {
                values.symbol = values.symbol.toUpperCase();
            }
            
            const { data } = await request.post('/api/evc/valuation-search', values);
            setStocks(data);
        } catch (error) {
            message.error('查询失败');
        }
    };

    // 获取收藏列表
    const fetchFavorites = async () => {
        try {
            const { data } = await request.get('/api/stock/favorites');
            setFavoriteStocks(data);
            setFavorites(data.map(stock => stock.symbol));
        } catch (error) {
            message.error('获取收藏列表失败');
        }
    };

    // 处理收藏/取消收藏
    const handleToggleFavorite = async (symbol) => {
        try {
            const isFavorited = favorites.includes(symbol);
            if (isFavorited) {
                await request.delete(`/api/stock/favorites/${symbol}`);
                message.success('取消收藏成功');
            } else {
                await request.post(`/api/stock/favorites/${symbol}`);
                message.success('收藏成功');
            }
            fetchFavorites();
        } catch (error) {
            message.error('操作失败');
        }
    };

    // 切换标签页时刷新数据
    const handleTabChange = (key) => {
        setActiveTab(key);
        if (key === 'favorites') {
            fetchFavorites();
        }
    };

    useEffect(() => {
        form.setFieldsValue(defaultValues);
        handleSearch(defaultValues);
        fetchFavorites();
    }, []);

    const columns = [
        { 
            title: '股票代码', 
            dataIndex: 'symbol', 
            key: 'symbol', 
            fixed: 'left', 
            width: 80,
            render: (text) => (
                <a onClick={() => navigate(`/stock/${text}`)}>{text}</a>
            )
        },
        { title: '公司名称', dataIndex: 'company', key: 'company', width: 100 },
        { 
            title: '贪恐指数',
            key: 'emotion_score',
            dataIndex: ['emotion_info', 'score'],
            sorter: (a, b) => (a.emotion_info?.score || -999) - (b.emotion_info?.score || -999),
            render: (score) => {
                if (!score && score !== 0) return '-';
                let color = '#000';
                if (score >= 80) color = '#f50';
                else if (score >= 60) color = '#ffa940';
                else if (score <= -80) color = '#52c41a';
                else if (score <= -60) color = '#73d13d';
                return <span style={{ color }}>{score}</span>;
            },
            width: 80
        },
        { 
            title: '最新价格', 
            dataIndex: 'last_price', 
            key: 'last_price',
            render: (text) => text.toFixed(2),
            width: 80
        },
        {
            title: '估值下限', 
            dataIndex: 'fair_value_lo', 
            key: 'fair_value_lo',
            render: (text) => text ? text.toFixed(2) : '-',
            width: 80
        },
        { 
            title: '估值上限',
            dataIndex: 'fair_value_hi', 
            key: 'fair_value_hi',
            render: (text) => text ? text.toFixed(2) : '-',
            width: 80
        },
        {
            title: '低估率',
            key: 'undervalue_rate',
            sorter: (a, b) => a.last_price / a.fair_value_lo - b.last_price / b.fair_value_lo,
            render: (_, record) => `${calculateChange(record.fair_value_lo, record.last_price)}`,
            width: 80
        },
        {
            title: 'Beta',
            key: 'beta',
            dataIndex: 'beta',
            render: (text) => text ? text.toFixed(2) : '-',
            width: 60
        },
        { 
            title: '下财年估值下限',
            dataIndex: 'forward_next_fy_lo', 
            key: 'forward_next_fy_lo',
            render: (text) => text ? text.toFixed(2) : '-',
            width: 80
        },
        { 
            title: '下财年估值上限',
            dataIndex: 'forward_next_fy_hi', 
            key: 'forward_next_fy_hi',
            render: (text) => text ? text.toFixed(2) : '-',
            width: 80
        },
        {
            title: '下财年增长率',
            key: 'forward_next_fy_growth',
            sorter: (a, b) => (a.forward_next_fy_lo / a.fair_value_lo) - (b.forward_next_fy_lo / b.fair_value_lo),
            render: (_, record) => `${calculateChange(record.forward_next_fy_lo, record.fair_value_lo)} ~ ${calculateChange(record.forward_next_fy_hi, record.fair_value_hi)}`,
            width: 150
        },
        {
            title: 'PE',
            key: 'pe_ratio',
            dataIndex: 'pe_ratio',
            render: (text) => text,
            width: 60
        },
        {
            title: '前瞻PE',
            key: 'forward_pe_ratio',
            dataIndex: 'forward_pe_ratio',
            render: (text) => text,
            width: 60
        },
        {
            title: '市净率',
            key: 'pb_ratio',
            render: (_, record) => {
                const bps = record.static_info?.bps;
                if (!bps || bps === 0) return '-';
                const pb = record.last_price / bps;
                return pb.toFixed(2);
            },
            width: 60
        },
        {
            title: '估值日期(n天前)', 
            dataIndex: 'fair_value_date', 
            key: 'fair_value_date',
            sorter: (a, b) => new Date(a.fair_value_date) - new Date(b.fair_value_date),
            render: (text) => text + '(' + ((new Date()-new Date(text)) / (1000*60*60*24)).toFixed(0) + '天)',
            width: 110
        },
        { 
            title: '更新时间', 
            dataIndex: 'date', 
            key: 'date',
            render: (text) => text,
            width: 100
        },
        {
            title: '操作',
            key: 'action',
            fixed: 'right',
            width: 80,
            render: (_, record) => (
                <Button
                    type={favorites.includes(record.symbol) ? 'primary' : 'default'}
                    onClick={() => handleToggleFavorite(record.symbol)}
                >
                    {favorites.includes(record.symbol) ? '已收藏' : '收藏'}
                </Button>
            )
        }
    ];

    return (
        <Layout>
            <Layout.Content style={{ background: '#fff', overflow: 'auto' }}>
                <Tabs
                    activeKey={activeTab}
                    onChange={handleTabChange}
                    items={[
                        {
                            key: 'all',
                            label: '所有股票',
                            children: (
                                <>
                                    <Form form={form} onFinish={handleSearch} layout="inline">
                                        <Form.Item label="股票代码" name="symbol">
                                            <Input 
                                                placeholder="输入股票代码" 
                                                style={{ width: 120 }}
                                                maxLength={5}
                                                onChange={(e) => {
                                                    // 只允许输入英文字母
                                                    const value = e.target.value.replace(/[^A-Za-z]/g, '').toUpperCase();
                                                    form.setFieldValue('symbol', value);
                                                }}
                                            />
                                        </Form.Item>
                                        <Form.Item label="低估阈值" name="undervalue_threshold">
                                            <InputNumber min={0} max={1} step={0.01} />
                                        </Form.Item>
                                        <Form.Item label="下财年增长阈值" name="next_fy_growth_threshold">
                                            <InputNumber min={1} step={0.01} />
                                        </Form.Item>
                                        <Form.Item>
                                            <Button type="primary" htmlType="submit">
                                                查询
                                            </Button>
                                        </Form.Item>
                                    </Form>
                                    <Table
                                        dataSource={stocks}
                                        columns={columns.filter(col => col.key !== 'emotion_score')}
                                        rowKey="symbol"
                                        scroll={{ x: 'max-content' }}
                                        size="small"
                                    />
                                </>
                            )
                        },
                        {
                            key: 'favorites',
                            label: '我的收藏',
                            children: (
                                <Table
                                    dataSource={favoriteStocks}
                                    columns={columns}
                                    rowKey="symbol"
                                    scroll={{ x: 'max-content' }}
                                    size="small"
                                    pagination={false}
                                />
                            )
                        }
                    ]}
                />
            </Layout.Content>
        </Layout>
    );
};

export default EVCValuation;
