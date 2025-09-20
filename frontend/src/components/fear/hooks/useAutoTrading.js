import { useState, useEffect } from 'react';
import { message } from 'antd';
import request from '../../utils/request';

// 自动交易控制 hook
export const useAutoTrading = () => {
  const [autoTrading, setAutoTrading] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const resp = await request.get('/api/quant/auto-trading-status');
        setAutoTrading(resp.data.enabled);
      } catch (error) {
        console.error('获取自动交易状态失败:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchStatus();
  }, []);

  const handleAutoTradingChange = async (checked) => {
    try {
      await request.post('/api/quant/auto-trading', { enabled: checked });
      setAutoTrading(checked);
      message.success(checked ? '自动交易已开启' : '自动交易已关闭');
    } catch (error) {
      message.error('操作失败：' + (error.response?.data?.detail || '未知错误'));
    }
  };

  return {
    autoTrading,
    loading,
    handleAutoTradingChange
  };
};
