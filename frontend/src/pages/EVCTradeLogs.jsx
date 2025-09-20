import React, { useState, useEffect, useRef } from 'react';
import { List, Layout, Typography, Spin } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';

const { Header } = Layout;
const { Title, Text } = Typography;

const EVCTradeLogs = () => {
    const navigate = useNavigate();
    const [logs, setLogs] = useState([]);
    const [loading, setLoading] = useState(false);
    const [hasMore, setHasMore] = useState(true);
    const [page, setPage] = useState(1);
    const containerRef = useRef(null);
    const PAGE_SIZE = 20;

    const fetchLogs = async (pageNum) => {
        if (!hasMore || loading) return;
        
        setLoading(true);
        try {
            const { data } = await request.get('/api/evc/trade-logs', {
                params: { page: pageNum, page_size: PAGE_SIZE }
            });
            
            if (pageNum === 1) {
                setLogs(data.items);
            } else {
                setLogs(prev => [...prev, ...data.items]);
            }
            
            setHasMore(data.items.length === PAGE_SIZE);
        } catch (error) {
            console.error('获取交易日志失败:', error);
        }
        setLoading(false);
    };

    useEffect(() => {
        fetchLogs(1);
    }, []);

    const handleScroll = () => {
        if (!containerRef.current) return;
        
        const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
        if (scrollHeight - scrollTop - clientHeight < 100 && hasMore && !loading) {
            setPage(prev => prev + 1);
            fetchLogs(page + 1);
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
                    onClick={() => navigate('/evc')}
                    style={{ 
                        fontSize: '16px',
                        marginRight: '10px',
                        cursor: 'pointer'
                    }}
                />
                <Title level={4} style={{ margin: 0 }}>EVC交易日志</Title>
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
                    dataSource={logs}
                    renderItem={log => (
                        <List.Item style={{ 
                            display: 'flex', 
                            flexDirection: 'column', 
                            alignItems: 'flex-start',
                            padding: '12px 0',
                            borderBottom: '1px solid #f0f0f0'
                        }}>
                            <Text type="secondary" style={{ fontSize: '12px' }}>
                                {dayjs(log.timestamp).format('YYYY-MM-DD HH:mm:ss')} · {log.operation ? log.operation.toUpperCase() : ''}
                            </Text>
                            <Text style={{ 
                                marginTop: '4px',
                                fontSize: '12px',
                                color: log.operation === 'sell' && log.quantity > 0 ? '#ff4d4f' : (log.operation === 'buy' && log.quantity > 0 ? '#52c41a' : 'inherit')
                            }}>
                                {log.symbol} {log.price} x {log.quantity} {log.reason}
                            </Text>
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

export default EVCTradeLogs; 