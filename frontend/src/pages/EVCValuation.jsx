import React, { useState, useEffect } from 'react';
import { Empty, Form, Grid, InputNumber, Input, Button, Table, message, Tabs, Select, Tag } from 'antd';
import { FilterOutlined, SearchOutlined, StarFilled, StarOutlined } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import request from '../utils/request';
import { MobileFilterDrawer, PageSection, PageShell, ResponsiveToolbar } from '../components/PageScaffold';
import './EVCValuation.css';

const DEFAULT_VALUES = {
    undervalue_threshold: 0.9,
    next_fy_growth_threshold: 1.1,
    symbol: '',
    tag_ids: [],
    min_market_cap_100m: 100,
    max_market_cap_100m: null
};

const EVCValuation = () => {
    const [form] = Form.useForm();
    const screens = Grid.useBreakpoint();
    const isMobile = !screens.md;
    const [stocks, setStocks] = useState([]);
    const [favoriteStocks, setFavoriteStocks] = useState([]);
    const [activeTab, setActiveTab] = useState('all');
    const [favorites, setFavorites] = useState([]);
    const [tagOptions, setTagOptions] = useState([]);
    const [searching, setSearching] = useState(false);
    const [filterOpen, setFilterOpen] = useState(false);

    const calculateChange = (value, marketPrice) => {
        if (!value || !marketPrice) return '';
        return ((value - marketPrice) / marketPrice * 100).toFixed(2) + '%';
    };

    const formatFixed = (value, precision = 2) => {
        if (value === null || value === undefined || value === '') return '-';
        const number = Number(value);
        return Number.isFinite(number) ? number.toFixed(precision) : '-';
    };

    const getEmotionColor = (score) => {
        if (!score && score !== 0) return 'default';
        if (score >= 80) return 'red';
        if (score >= 60) return 'orange';
        if (score <= -80) return 'green';
        if (score <= -60) return 'lime';
        return 'default';
    };

    const handleSearch = async (values) => {
        setSearching(true);
        try {
            const payload = { ...values };
            if (payload.min_market_cap_100m !== null && payload.min_market_cap_100m !== undefined &&
                payload.max_market_cap_100m !== null && payload.max_market_cap_100m !== undefined &&
                Number(payload.min_market_cap_100m) > Number(payload.max_market_cap_100m)) {
                message.error('市值下限不能大于上限');
                return;
            }
            // 如果输入了股票代码，转换为大写
            if (payload.symbol) {
                payload.symbol = payload.symbol.toUpperCase();
            }
            ['tag_ids', 'min_market_cap_100m', 'max_market_cap_100m'].forEach((key) => {
                if (payload[key] === null || payload[key] === undefined || payload[key] === '') {
                    delete payload[key];
                }
            });

            const { data } = await request.post('/api/evc/valuation-search', payload);
            setStocks(data);
            setFilterOpen(false);
        } catch (error) {
            message.error({ content: '查询失败', key: 'evc-search' });
        } finally {
            setSearching(false);
        }
    };

    const fetchTags = async () => {
        try {
            const { data } = await request.get('/api/evc/tags');
            setTagOptions((data || []).map(tag => ({
                label: tag.stock_count ? `${tag.name} (${tag.stock_count})` : tag.name,
                value: tag.id
            })));
        } catch (error) {
            message.error({ content: '获取标签列表失败', key: 'evc-tags' });
        }
    };

    // 获取收藏列表
    const fetchFavorites = async () => {
        try {
            const { data } = await request.get('/api/stock/favorites');
            setFavoriteStocks(data);
            setFavorites(data.map(stock => stock.symbol));
        } catch (error) {
            message.error({ content: '获取收藏列表失败', key: 'evc-favorites' });
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

    const handleResetFilters = () => {
        form.setFieldsValue(DEFAULT_VALUES);
        handleSearch(DEFAULT_VALUES);
    };

    useEffect(() => {
        form.setFieldsValue(DEFAULT_VALUES);
        fetchTags();
        handleSearch(DEFAULT_VALUES);
        fetchFavorites();
    }, [form]);

    const columns = [
        {
            title: '股票代码',
            dataIndex: 'symbol',
            key: 'symbol',
            fixed: 'left',
            width: 80,
            render: (text) => (
                <Link to={`/stock/${text}`} state={{ mainTabKey: '/evc' }}>
                    {text}
                </Link>
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
            render: (text) => formatFixed(text),
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
            title: '市值(亿美元)',
            key: 'market_cap_100m',
            dataIndex: 'market_cap_100m',
            sorter: (a, b) => {
                const aValue = a.market_cap_100m;
                const bValue = b.market_cap_100m;
                if (aValue === null || aValue === undefined) return 1;
                if (bValue === null || bValue === undefined) return -1;
                return aValue - bValue;
            },
            render: (text) => text === null || text === undefined ? '-' : text.toFixed(2),
            width: 110
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
            render: (text) => text ?? '-',
            width: 60
        },
        {
            title: '前瞻PE',
            key: 'forward_pe_ratio',
            dataIndex: 'forward_pe_ratio',
            render: (text) => text ?? '-',
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
            render: (text) => text + '(' + ((new Date() - new Date(text)) / (1000 * 60 * 60 * 24)).toFixed(0) + '天)',
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

    const allStockColumns = columns.filter(col => (
        col.key !== 'emotion_score' &&
        (col.key !== 'pb_ratio' || stocks.some(stock => stock.static_info?.bps)) &&
        (col.key !== 'market_cap_100m' || stocks.some(stock => stock.market_cap_100m !== null && stock.market_cap_100m !== undefined))
    ));

    const renderFilterForm = (compact = false) => (
        <Form
            form={form}
            onFinish={handleSearch}
            layout="vertical"
            className={compact ? 'evc-filter-form evc-filter-form--mobile' : 'evc-filter-form'}
        >
            <Form.Item label="股票代码" name="symbol">
                <Input
                    placeholder="输入股票代码"
                    maxLength={5}
                    prefix={<SearchOutlined />}
                    onChange={(e) => {
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
            <Form.Item label="市值下限(亿美元)" name="min_market_cap_100m">
                <InputNumber min={0} step={10} />
            </Form.Item>
            <Form.Item label="市值上限(亿美元)" name="max_market_cap_100m">
                <InputNumber min={0} step={10} />
            </Form.Item>
            <Form.Item label="标签" name="tag_ids" className="evc-filter-form__tags">
                <Select
                    mode="multiple"
                    allowClear
                    showSearch
                    placeholder="选择标签"
                    options={tagOptions}
                    maxTagCount="responsive"
                    optionFilterProp="label"
                />
            </Form.Item>
            {!compact && (
                <Form.Item className="evc-filter-form__submit">
                    <Button type="primary" htmlType="submit" loading={searching}>
                        查询
                    </Button>
                </Form.Item>
            )}
        </Form>
    );

    const renderMetric = (label, value, tone) => (
        <div className="evc-stock-card__metric">
            <span>{label}</span>
            <strong className={tone ? `evc-tone-${tone}` : ''}>{value || '-'}</strong>
        </div>
    );

    const renderMobileStockList = (data, includeEmotion = false) => {
        if (!data.length) {
            return (
                <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={includeEmotion ? '暂无收藏股票' : '暂无符合条件的股票'}
                />
            );
        }

        return (
            <div className="evc-mobile-list">
                {data.map(record => {
                    const isFavorited = favorites.includes(record.symbol);
                    const undervalueRate = calculateChange(record.fair_value_lo, record.last_price);
                    const nextGrowth = `${calculateChange(record.forward_next_fy_lo, record.fair_value_lo)} ~ ${calculateChange(record.forward_next_fy_hi, record.fair_value_hi)}`;
                    const emotionScore = record.emotion_info?.score;

                    return (
                        <article className="evc-stock-card" key={record.symbol}>
                            <div className="evc-stock-card__header">
                                <div className="evc-stock-card__title">
                                    <Link to={`/stock/${record.symbol}`} state={{ mainTabKey: '/evc' }}>
                                        {record.symbol}
                                    </Link>
                                    <span>{record.company || '-'}</span>
                                </div>
                                <Button
                                    type={isFavorited ? 'primary' : 'default'}
                                    shape="circle"
                                    icon={isFavorited ? <StarFilled /> : <StarOutlined />}
                                    aria-label={isFavorited ? '取消收藏' : '收藏'}
                                    onClick={() => handleToggleFavorite(record.symbol)}
                                />
                            </div>

                            <div className="evc-stock-card__metrics">
                                {renderMetric('最新', formatFixed(record.last_price))}
                                {renderMetric('下限', formatFixed(record.fair_value_lo))}
                                {renderMetric('上限', formatFixed(record.fair_value_hi))}
                                {renderMetric('低估率', undervalueRate, undervalueRate?.startsWith('-') ? 'buy' : 'sell')}
                            </div>

                            <div className="evc-stock-card__details">
                                {includeEmotion && (
                                    <span>
                                        贪恐
                                        <Tag color={getEmotionColor(emotionScore)}>{emotionScore ?? '-'}</Tag>
                                    </span>
                                )}
                                <span>Beta {formatFixed(record.beta)}</span>
                                <span>市值 {formatFixed(record.market_cap_100m)} 亿美元</span>
                                <span>下财年 {nextGrowth}</span>
                                <span>估值日 {record.fair_value_date || '-'}</span>
                            </div>
                        </article>
                    );
                })}
            </div>
        );
    };

    const renderValuationContent = (data, tableColumns, includeEmotion = false, pagination = true) => (
        isMobile ? (
            renderMobileStockList(data, includeEmotion)
        ) : (
            <Table
                dataSource={data}
                columns={tableColumns}
                rowKey="symbol"
                scroll={{ x: 'max-content' }}
                size="small"
                loading={searching && activeTab === 'all'}
                pagination={pagination}
            />
        )
    );

    return (
        <PageShell
            className="evc-page"
            title="估值"
            subtitle="按估值区间、增长阈值和标签筛选股票"
            actions={
                isMobile && activeTab === 'all' ? (
                    <Button type="primary" icon={<FilterOutlined />} onClick={() => setFilterOpen(true)}>
                        筛选
                    </Button>
                ) : null
            }
        >
            <PageSection className="evc-page__section">
                <Tabs
                    activeKey={activeTab}
                    onChange={handleTabChange}
                    className="evc-tabs"
                    items={[
                        {
                            key: 'all',
                            label: `所有股票 ${stocks.length}`,
                            children: (
                                <>
                                    {!isMobile && (
                                        <ResponsiveToolbar>
                                            {renderFilterForm(false)}
                                        </ResponsiveToolbar>
                                    )}
                                    {renderValuationContent(stocks, allStockColumns)}
                                </>
                            )
                        },
                        {
                            key: 'favorites',
                            label: `我的收藏 ${favoriteStocks.length}`,
                            children: renderValuationContent(favoriteStocks, columns, true, false)
                        }
                    ]}
                />
            </PageSection>

            <MobileFilterDrawer
                open={filterOpen}
                onClose={() => setFilterOpen(false)}
                footer={[
                    <Button key="reset" onClick={handleResetFilters}>
                        重置
                    </Button>,
                    <Button key="submit" type="primary" loading={searching} onClick={() => form.submit()}>
                        查询
                    </Button>
                ]}
            >
                {renderFilterForm(true)}
            </MobileFilterDrawer>
        </PageShell>
    );
};

export default EVCValuation;
