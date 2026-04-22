import React from 'react';
import { Card, List, Space } from 'antd';
import { RightOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';

const EVCDashboard = () => {
    const navigate = useNavigate();

    const items = [
        {
            title: '自动化交易策略 / Cookie配置',
            onClick: () => navigate('/evc/strategy'),
            arrow: true
        },
        {
            title: '估值选股',
            onClick: () => navigate('/evc/valuation'),
            arrow: true
        },
        {
            title: 'EVC交易日志',
            onClick: () => navigate('/evc/trade-logs'),
            arrow: true
        }
    ];

    return (
        <Card title="EVC设置">
            <List
                style={{
                    backgroundColor: '#fff'
                }}
            >
                {items.map((item, index) => (
                    <List.Item
                        key={index}
                        onClick={item.onClick}
                        style={{
                            padding: '16px',
                            cursor: 'pointer',
                            borderBottom: '1px solid #f0f0f0'
                        }}
                    >
                        <div style={{ 
                            display: 'flex', 
                            justifyContent: 'space-between', 
                            alignItems: 'center',
                            width: '100%'
                        }}>
                            <span>{item.title}</span>
                            <Space>
                                {item.arrow && <RightOutlined style={{ color: '#bfbfbf' }} />}
                            </Space>
                        </div>
                    </List.Item>
                ))}
            </List>
        </Card>
    );
};

export default EVCDashboard; 
