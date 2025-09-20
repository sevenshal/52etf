import React, { useState } from 'react';
import { List, Space, Switch, Modal, Input, message } from 'antd';
import { RightOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../../utils/request';

const AutoTradingPanel = ({ autoTrading, onAutoTradingChange }) => {
  const navigate = useNavigate();
  const [showActivation, setShowActivation] = useState(false);
  const [activationCode, setActivationCode] = useState('');

  const handleActivate = async () => {
    try {
      await request.post('/api/quant/activate', { code: activationCode });
      message.success('激活码修改成功');
      setShowActivation(false);
      setActivationCode('');
    } catch (error) {
      message.error('激活码修改失败：' + (error.response?.data?.detail || '未知错误'));
    }
  };

  const items = [
    {
      title: '自动交易',
      content: <Switch checked={autoTrading} onChange={onAutoTradingChange} />,
    },
    {
      title: '修改激活码',
      onClick: () => setShowActivation(true),
      arrow: true
    },
    {
      title: '股票列表',
      onClick: () => navigate('/fear/stocks'),
      arrow: true
    },
    {
      title: '交易日志',
      onClick: () => navigate('/fear/logs'),
      arrow: true
    },
    {
      title: 'ETF回测',
      onClick: () => navigate('/fear/backtest'),
      arrow: true
    }
  ];

  return (
    <>
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
              cursor: item.onClick ? 'pointer' : 'default',
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
                {item.content}
                {item.arrow && <RightOutlined style={{ color: '#bfbfbf' }} />}
              </Space>
            </div>
          </List.Item>
        ))}
      </List>

      <Modal
        title="修改激活码"
        open={showActivation}
        onOk={handleActivate}
        onCancel={() => {
          setShowActivation(false);
          setActivationCode('');
        }}
      >
        <Input
          placeholder="请输入激活码"
          value={activationCode}
          onChange={e => setActivationCode(e.target.value)}
        />
      </Modal>
    </>
  );
};

export default AutoTradingPanel;
