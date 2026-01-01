import React, { useState, useEffect, useRef } from 'react';
import { List, Layout, Typography, Spin, Select } from 'antd';  // 添加 Select
import { LeftOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../../utils/request';
import dayjs from 'dayjs';

const { Header } = Layout;
const { Title, Text } = Typography;

const FearTradingLogs = () => {
    const navigate = useNavigate();
    const [logs, setLogs] = useState([]);
    const [loading, setLoading] = useState(false);
    const [hasMore, setHasMore] = useState(true);
    const [page, setPage] = useState(1);
    const containerRef = useRef(null);
    const PAGE_SIZE = 20;
    const [minLevel, setMinLevel] = useState('DEBUG');

    // 修改 fetchLogs 函数，使用传入的 level 参数
    const fetchLogs = async (pageNum, level = minLevel) => {
        if (!hasMore || loading) return;

        setLoading(true);
        try {
            const { data } = await request.get('/api/trade/trading-logs', {
                params: {
                    page: pageNum,
                    page_size: PAGE_SIZE,
                    min_level: level
                }
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

    // 修改 handleLevelChange 函数
    const handleLevelChange = (value) => {
        setMinLevel(value);
        setPage(1);
        setHasMore(true);
        fetchLogs(1, value);  // 直接传入新的 level 值
    };

    // 修改 handleScroll 函数
    const handleScroll = () => {
        if (!containerRef.current) return;

        const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
        if (scrollHeight - scrollTop - clientHeight < 100 && hasMore && !loading) {
            setPage(prev => prev + 1);
            fetchLogs(page + 1, minLevel);  // 传入当前的 level 值
        }
    };

    // 修改 useEffect，添加 minLevel 依赖
    useEffect(() => {
        fetchLogs(1, minLevel);
    }, [minLevel]);

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
                justifyContent: 'space-between',  // 修改为两端对齐
                borderBottom: '1px solid #f0f0f0'
            }}>
                <div style={{ display: 'flex', alignItems: 'center' }}>
                    <LeftOutlined
                        onClick={() => navigate(-1)}
                        style={{
                            fontSize: '16px',
                            marginRight: '10px',
                            cursor: 'pointer'
                        }}
                    />
                    <Title level={4} style={{ margin: 0 }}>交易日志</Title>
                </div>
                <Select
                    style={{ width: 120 }}
                    placeholder="日志级别"
                    allowClear
                    value={minLevel}
                    onChange={handleLevelChange}
                    options={[
                        { value: 'DEBUG', label: 'DEBUG' },
                        { value: 'INFO', label: 'INFO' },
                        { value: 'WARN', label: 'WARN' },
                        { value: 'ERROR', label: 'ERROR' }
                    ]}
                />
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
                                {dayjs(log.timestamp).format('YYYY-MM-DD HH:mm:ss')} · {log.level}
                            </Text>
                            <Text style={{
                                marginTop: '4px',
                                fontSize: '12px',
                                color: log.level === 'ERROR' ? '#ff4d4f' : 'inherit'
                            }}>
                                {log.message}
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

export default FearTradingLogs;
