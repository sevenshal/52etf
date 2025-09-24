import React, { useState, useEffect, useRef } from 'react';
import { List, Layout, Typography, Spin } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';

const { Header } = Layout;
const { Title, Text } = Typography;
const PAGE_SIZE = 20;

const MarketSignalHistory = () => {
    const navigate = useNavigate();
    const [signals, setSignals] = useState([]);
    const [loading, setLoading] = useState(false);
    const [hasMore, setHasMore] = useState(true);
    const [page, setPage] = useState(1);
    const containerRef = useRef(null);

    const fetchSignals = async (pageNum) => {
        if (!hasMore || loading) return;
        setLoading(true);
        try {
            // 注意接口路径要和后端一致
            const { data } = await request.get('/api/market_signal', {
                params: { page: pageNum, page_size: PAGE_SIZE }
            });
            if (pageNum === 1) {
                setSignals(data.items);
            } else {
                setSignals(prev => [...prev, ...data.items]);
            }
            setHasMore(data.items.length === PAGE_SIZE);
        } catch (error) {
            console.error('获取美股信号历史失败:', error);
        }
        setLoading(false);
    };

    useEffect(() => {
        fetchSignals(1);
    }, []);

    const handleScroll = () => {
        if (!containerRef.current) return;
        const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
        if (scrollHeight - scrollTop - clientHeight < 100 && hasMore && !loading) {
            const nextPage = page + 1;
            setPage(nextPage);
            fetchSignals(nextPage);
        }
    };

    return (
        <Layout>
            <Header style={{
                position: 'fixed',
                zIndex: 1,
                width: '100%',
                background: '#fff',
                padding: '0 16px',
                display: 'flex',
                alignItems: 'center',
                borderBottom: '1px solid #f0f0f0'
            }}>
                <LeftOutlined
                    onClick={() => navigate(-1)}
                    style={{
                        fontSize: '16px',
                        marginRight: '10px',
                        cursor: 'pointer'
                    }}
                />
                <Title level={4} style={{ margin: 0 }}>美股信号历史</Title>
            </Header>
            <Layout.Content
                ref={containerRef}
                style={{
                    marginTop: 64,
                    padding: '16px',
                    background: '#fff',
                    height: 'calc(100vh - 64px)',
                    overflowY: 'auto'
                }}
                onScroll={handleScroll}
            >
                <List
                    dataSource={signals}
                    renderItem={item => (
                        <List.Item style={{
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'flex-start',
                            padding: '12px 0',
                            borderBottom: '1px solid #f0f0f0'
                        }}>
                            <Text type="secondary" style={{ fontSize: '12px' }}>
                                {item.date ? dayjs(item.date).format('YYYY-MM-DD') : ''} · {item.direction}
                            </Text>
                            {item.ver === 'v2' ? (
                                <Text style={{
                                    marginTop: '4px',
                                    fontSize: '12px',
                                    color: item.direction === 'SELL' ? '#ff4d4f' : (item.direction === 'BUY' ? '#52c41a' : 'inherit')
                                }}>
                                    <a onClick={() => navigate(`/stock/${item.symbol}`)} >{item.symbol}</a>
                                    &nbsp;收盘价:{item.close_price}
                                    &nbsp;幅度超过{item.v2_price_change_ratio}%
                                    &nbsp;企稳超过{item.v2_stabilization_period}天
                                </Text>
                            ) : (
                                <Text style={{
                                    marginTop: '4px',
                                    fontSize: '12px',
                                    color: item.direction === 'SELL' ? '#ff4d4f' : (item.direction === 'BUY' ? '#52c41a' : 'inherit')
                                }}>
                                    <a onClick={() => navigate(`/stock/${item.symbol}`)} >{item.symbol}</a> 收盘:{item.close_price} 低于200MA比率:{item.below_200ma_ratio * 100}% <br/>
                                    5日成交量高出50日成交量{item.vol_5_std}个标准差, 当日成交量高出{item.today_vol_std}个标准差 <br/>
                                    50日低点:{item.low_50} 收盘vs50日低点比率:{item.close_vs_low_50}
                                </Text>
                            )}
                        </List.Item>
                    )}
                />
                {loading && (
                    <div style={{ textAlign: 'center', padding: '16px' }}>
                        <Spin />
                    </div>
                )}
            </Layout.Content>
        </Layout>
    );
};

export default MarketSignalHistory;