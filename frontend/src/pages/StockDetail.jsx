import React, { useCallback, useMemo, useRef, useState, useEffect } from 'react';
import { Card, Button, Select, Spin } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import { useParams, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import StockKlineChart from '../components/StockKlineChart';

const FIVE_YEAR_TRADING_BARS = 1260;

const StockDetail = () => {
  const { symbol } = useParams();
  const navigate = useNavigate();
  const normalizedSymbol = useMemo(() => (symbol || '').toUpperCase(), [symbol]);
  const isAStock = useMemo(() => /\.(SH|SZ|BJ)$/.test(normalizedSymbol), [normalizedSymbol]);
  const [evcHistory, setEvcHistory] = useState([]);
  const [stockName, setStockName] = useState('');
  const [symbolOptions, setSymbolOptions] = useState([]);
  const [symbolSearching, setSymbolSearching] = useState(false);
  const symbolSearchTimer = useRef(null);
  const symbolSearchSequence = useRef(0);

  const searchSymbols = useCallback((query, immediate = false) => {
    if (!isAStock) return;
    if (symbolSearchTimer.current) window.clearTimeout(symbolSearchTimer.current);
    const sequence = ++symbolSearchSequence.current;
    const runSearch = async () => {
      setSymbolSearching(true);
      try {
        const { data } = await request.get('/api/stock/a-stock/symbols', {
          params: { q: String(query || '').trim(), limit: 30 },
        });
        if (sequence !== symbolSearchSequence.current) return;
        const options = data || [];
        const current = options.find(item => item.value === normalizedSymbol);
        setSymbolOptions(previousOptions => {
          const currentOption = current
            || previousOptions.find(item => item.value === normalizedSymbol);
          return currentOption && !options.some(item => item.value === normalizedSymbol)
            ? [currentOption, ...options]
            : options;
        });
        if (current?.name) setStockName(current.name);
      } catch (error) {
        console.error('股票搜索失败:', error);
      } finally {
        if (sequence === symbolSearchSequence.current) setSymbolSearching(false);
      }
    };
    if (immediate) runSearch();
    else symbolSearchTimer.current = window.setTimeout(runSearch, 250);
  }, [isAStock, normalizedSymbol]);

  const fetchEvcHistory = useCallback(async () => {
    try {
      const historyUrl = isAStock
        ? `/api/evc/a-stock-consensus/history/${normalizedSymbol}?limit=${FIVE_YEAR_TRADING_BARS}`
        : `/api/evc/stock-evc/history/${normalizedSymbol}?limit=${FIVE_YEAR_TRADING_BARS}`;
      const { data } = await request.get(historyUrl);
      setEvcHistory(data || []);
    } catch (error) {
      console.error('获取估值历史失败:', error);
    }
  }, [isAStock, normalizedSymbol]);

  useEffect(() => {
    setStockName('');
    setEvcHistory([]);
    if (isAStock) searchSymbols(normalizedSymbol, true);
    fetchEvcHistory();
  }, [fetchEvcHistory, isAStock, normalizedSymbol, searchSymbols]);

  useEffect(() => () => {
    if (symbolSearchTimer.current) window.clearTimeout(symbolSearchTimer.current);
  }, []);

  return (
    <div style={{ padding: '24px' }}>
      <Card
        title={
          <div style={{ display: 'flex', alignItems: 'center' }}>
            <Button
              type="text"
              icon={<LeftOutlined />}
              onClick={() => navigate(-1)}
              style={{ marginRight: '12px' }}
            />
            <span>{stockName || normalizedSymbol} 股票详情</span>
          </div>
        }
        extra={isAStock ? (
          <Select
            showSearch
            value={normalizedSymbol}
            options={symbolOptions}
            loading={symbolSearching}
            filterOption={false}
            onSearch={value => searchSymbols(value)}
            onDropdownVisibleChange={open => { if (open) searchSymbols(''); }}
            onChange={value => navigate(`/stock/${value}`)}
            placeholder="搜索股票名称或代码"
            notFoundContent={symbolSearching ? <Spin size="small" /> : '没有匹配股票'}
            style={{ width: 260 }}
          />
        ) : null}
      >
        <StockKlineChart
          symbol={normalizedSymbol}
          klineUrl={isAStock ? `/api/stock/a-stock/klines/${normalizedSymbol}` : undefined}
          valuationHistory={evcHistory}
          valuationFillMode={isAStock ? 'forward' : 'exact'}
          valuationDateOffsetDays={isAStock ? 0 : -1}
          height={600}
        />
      </Card>
    </div>
  );
};

export default StockDetail;
