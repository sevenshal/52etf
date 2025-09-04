import React, { useEffect, useState } from 'react';
import { List, InfiniteScroll, SpinLoading } from 'antd-mobile';
import request from '../utils/request'; // 直接用你已有的封装
import { useNavigate } from 'react-router-dom';

const PAGE_SIZE = 20;

export default function MarketSignalHistory() {
  const [data, setData] = useState([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);

  // 首次加载
  useEffect(() => {
    loadMore();
    // eslint-disable-next-line
  }, []);

  const loadMore = async () => {
    if (loading || !hasMore) return;
    setLoading(true);
    try {
      const res = await request.get('/api/market_signal', { params: { page, page_size: PAGE_SIZE } });
      setData(prev => [...prev, ...res.items]);
      setHasMore(res.page * res.page_size < res.total);
      setPage(prev => prev + 1);
    } catch (e) {
      // 可加错误提示
    }
    setLoading(false);
  };

  return (
    <div>
      <List header="美股信号历史">
        {data.map(item => (
          <List.Item
            key={item.symbol + item.date}
            extra={item.direction}
            description={`收盘:${item.close_price} | 低于200MA:${item.below_200ma_ratio} | 标准差:${item.vol_5_std},${item.today_vol_std}`}
          >
            {item.symbol} {item.date}
          </List.Item>
        ))}
      </List>
      <InfiniteScroll loadMore={loadMore} hasMore={hasMore} />
      {loading && <SpinLoading />}
    </div>
  );
}
