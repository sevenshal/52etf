import { useState, useEffect } from 'react';
import { fetchFearGreedData } from '../../utils/cnnRequest';

// 恐贪指数数据 hook
export const useFearGreedData = () => {
  const [fearGreedData, setFearGreedData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const data = await fetchFearGreedData(0);
        setFearGreedData(data);
      } catch (error) {
        console.error('获取恐贪指数数据失败:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  return { fearGreedData, loading };
};
