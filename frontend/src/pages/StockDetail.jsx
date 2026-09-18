import React, { useCallback, useMemo, useRef, useState, useEffect } from 'react';
import { Card, Select, Spin } from 'antd';
import { useParams, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import StockKlineChart from '../components/StockKlineChart';
import XueqiuStockLink from '../components/XueqiuStockLink';
import useRealtimeQuotes from '../hooks/useRealtimeQuotes';
import { useAccount } from '../contexts/AccountContext';
import { resolveXueqiuRank } from '../utils/xueqiuHoldings';
import AStockQuoteSummary from '../components/AStockQuoteSummary';
import StockFearIndexStrip from '../components/StockFearIndexStrip';
import StockFinancialsCard from '../components/StockFinancialsCard';
import StockValueInvestingCard from '../components/StockValueInvestingCard';
import AStockConsensusValuationModal from '../components/AStockConsensusValuationModal';

const FIVE_YEAR_TRADING_BARS = 1260;
// 雪球历史接口一次最多 2000 条；5 年 K 线约 1250 个交易日，1300 足够覆盖
const XUEQIU_HISTORY_LIMIT = 1300;
const PE_BAND_STORAGE_KEY = 'stockDetail.peBandEnabled';
const FUND_FLOW_REFRESH_MS = 60 * 1000;

// 工作日 9:25–15:05（北京时间）才刷新盘中资金流；节假日多请求几次无妨，库里没有新数据
const isAStockTradingTime = () => {
  const beijing = new Date(Date.now() + (new Date().getTimezoneOffset() + 480) * 60 * 1000);
  const day = beijing.getDay();
  const minutes = beijing.getHours() * 60 + beijing.getMinutes();
  return day >= 1 && day <= 5 && minutes >= 9 * 60 + 25 && minutes <= 15 * 60 + 5;
};

const readPeBandPreference = () => {
  try {
    return window.localStorage.getItem(PE_BAND_STORAGE_KEY) === '1';
  } catch (error) {
    return false;
  }
};

const StockDetail = () => {
  const { symbol } = useParams();
  const navigate = useNavigate();
  const normalizedSymbol = useMemo(() => (symbol || '').toUpperCase(), [symbol]);
  const isAStock = useMemo(() => /\.(SH|SZ|BJ)$/.test(normalizedSymbol), [normalizedSymbol]);
  const [evcHistory, setEvcHistory] = useState([]);
  const [stockName, setStockName] = useState('');
  const [symbolOptions, setSymbolOptions] = useState([]);
  const [symbolSearching, setSymbolSearching] = useState(false);
  const [stockSummary, setStockSummary] = useState({});
  const [klines, setKlines] = useState([]);
  const [consensusDetail, setConsensusDetail] = useState(null);
  const [consensusModal, setConsensusModal] = useState({ open: false, offset: 0, bound: 'lo' });
  // 没给目标价的机构是否用前瞻 PE 通道补估值；影响估值线和估值上下限，记在本地。
  const [peBandEnabled, setPeBandEnabled] = useState(readPeBandPreference);
  const peBandEnabledRef = useRef(peBandEnabled);
  const historyRequestSequence = useRef(0);
  const consensusRequestSequence = useRef(0);
  const symbolSearchTimer = useRef(null);
  const symbolSearchSequence = useRef(0);
  const { quotes, register } = useRealtimeQuotes('stock_detail_page');
  // 雪球持仓数据属于管理员专属的因子实验室（接口是 valid_admin_account），
  // 非管理员不请求、K 线上也就不出现雪球副图，维持原有权限不变
  const { isAdmin } = useAccount();

  // 雪球持仓历史只取一次：K 线的雪球副图和行情摘要的「雪球持仓排行」共用这一份。
  // 与「雪球持仓」模块同一接口、同一默认口径（只统计主理人活跃组合）。
  const [xueqiuPayload, setXueqiuPayload] = useState(null);
  useEffect(() => {
    setXueqiuPayload(null);
    if (!isAStock || !isAdmin) return undefined;
    let cancelled = false;
    request.get('/api/factor-lab/xueqiu-top-holdings/history', {
      params: { symbol: normalizedSymbol, active_only: true, limit: XUEQIU_HISTORY_LIMIT },
    })
      .then(({ data }) => {
        if (!cancelled) setXueqiuPayload(data || null);
      })
      .catch(error => console.error('获取雪球持仓历史失败:', error));
    return () => { cancelled = true; };
  }, [isAStock, isAdmin, normalizedSymbol]);
  const xueqiuHistoryRows = useMemo(() => xueqiuPayload?.history || [], [xueqiuPayload]);
  const xueqiuRank = useMemo(
    () => (xueqiuPayload ? resolveXueqiuRank(xueqiuPayload.history, xueqiuPayload.latest_snapshot_date) : null),
    [xueqiuPayload],
  );

  // 历史日级主力资金流：K 线下方的主力资金副图。后端把日终同步还没写入的日子
  // （含今天盘中）用东财实时数据补上，盘中每分钟刷新一次，跟实时 K 线同步
  const [fundFlowHistory, setFundFlowHistory] = useState([]);
  useEffect(() => {
    setFundFlowHistory([]);
    if (!isAStock) return undefined;
    let cancelled = false;
    const load = () => request.get(`/api/stock/a-stock/fund-flow/${normalizedSymbol}`)
      .then(({ data }) => {
        if (!cancelled) setFundFlowHistory(data || []);
      })
      .catch(error => console.error('获取历史资金流失败:', error));
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && isAStockTradingTime()) load();
    }, FUND_FLOW_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [isAStock, normalizedSymbol]);

  useEffect(() => {
    register(isAStock ? [normalizedSymbol] : []);
  }, [isAStock, normalizedSymbol, register]);

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

  const fetchEvcHistory = useCallback(async (usePeBand = false) => {
    const sequence = ++historyRequestSequence.current;
    try {
      const historyUrl = isAStock
        ? `/api/evc/a-stock-consensus/history/${normalizedSymbol}?limit=${FIVE_YEAR_TRADING_BARS}&use_pe_band=${usePeBand}`
        : `/api/evc/stock-evc/history/${normalizedSymbol}?limit=${FIVE_YEAR_TRADING_BARS}`;
      const { data } = await request.get(historyUrl);
      if (sequence !== historyRequestSequence.current) return;
      setEvcHistory(data || []);
    } catch (error) {
      console.error('获取估值历史失败:', error);
    }
  }, [isAStock, normalizedSymbol]);

  const fetchStockSummary = useCallback(async () => {
    if (!isAStock) {
      setStockSummary({});
      return;
    }
    try {
      const { data } = await request.get(`/api/stock/a-stock/summary/${normalizedSymbol}`);
      setStockSummary(data || {});
      if (data?.name) setStockName(data.name);
    } catch (error) {
      console.error('获取股票摘要失败:', error);
      setStockSummary({});
    }
  }, [isAStock, normalizedSymbol]);

  const fetchConsensusDetail = useCallback(async (usePeBand = false) => {
    if (!isAStock) {
      setConsensusDetail(null);
      return;
    }
    const sequence = ++consensusRequestSequence.current;
    try {
      const { data } = await request.get(`/api/evc/a-stock-consensus/detail/${normalizedSymbol}`, {
        params: { use_pe_band: usePeBand },
      });
      if (sequence !== consensusRequestSequence.current) return;
      setConsensusDetail(data || null);
    } catch (error) {
      console.error('获取一致预期估值失败:', error);
      setConsensusDetail(null);
    }
  }, [isAStock, normalizedSymbol]);

  const week52 = useMemo(() => {
    const cutoff = day => new Date(day).getTime() >= Date.now() - 366 * 24 * 60 * 60 * 1000;
    const recent = (klines || []).filter(item => cutoff(item.timestamp));
    const highs = recent.map(item => Number(item.high)).filter(Number.isFinite);
    const lows = recent.map(item => Number(item.low)).filter(Number.isFinite);
    return {
      high: highs.length ? Math.max(...highs) : null,
      low: lows.length ? Math.min(...lows) : null,
    };
  }, [klines]);

  useEffect(() => {
    setStockName('');
    setStockSummary({});
    setKlines([]);
    setEvcHistory([]);
    setConsensusDetail(null);
    setConsensusModal(previous => ({ ...previous, open: false }));
    if (isAStock) searchSymbols(normalizedSymbol, true);
    fetchEvcHistory(peBandEnabledRef.current);
    fetchStockSummary();
    fetchConsensusDetail(peBandEnabledRef.current);
  }, [fetchConsensusDetail, fetchEvcHistory, fetchStockSummary, isAStock, normalizedSymbol, searchSymbols]);

  const handlePeBandToggle = useCallback(checked => {
    peBandEnabledRef.current = checked;
    setPeBandEnabled(checked);
    try {
      window.localStorage.setItem(PE_BAND_STORAGE_KEY, checked ? '1' : '0');
    } catch (error) {
      // 浏览器禁用本地存储时，开关只在本次打开的页面里生效。
    }
    fetchEvcHistory(checked);
    fetchConsensusDetail(checked);
  }, [fetchConsensusDetail, fetchEvcHistory]);

  useEffect(() => () => {
    if (symbolSearchTimer.current) window.clearTimeout(symbolSearchTimer.current);
  }, []);

  return (
    <div style={{ padding: '24px' }}>
      <Card
        title={
          <span>
            {stockName ? `${stockName}（` : ''}
            <XueqiuStockLink symbol={normalizedSymbol}>{normalizedSymbol}</XueqiuStockLink>
            {stockName ? '）' : ''}
            {' 股票详情'}
          </span>
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
        {isAStock ? (
          <AStockQuoteSummary
            quote={quotes[normalizedSymbol]}
            summary={stockSummary}
            week52={week52}
            consensus={consensusDetail}
            onOpenConsensus={(offset, bound) => setConsensusModal({ open: true, offset, bound })}
            peBandEnabled={peBandEnabled}
            onTogglePeBand={handlePeBandToggle}
            xueqiuRank={xueqiuRank}
          />
        ) : null}
        {isAStock ? <StockFearIndexStrip symbol={normalizedSymbol} /> : null}
        <StockKlineChart
          symbol={normalizedSymbol}
          klineUrl={isAStock ? `/api/stock/a-stock/klines/${normalizedSymbol}` : undefined}
          valuationHistory={evcHistory}
          valuationFillMode={isAStock ? 'forward' : 'exact'}
          valuationDateOffsetDays={isAStock ? 0 : -1}
          realtimeQuote={isAStock ? quotes[normalizedSymbol] : null}
          eventsUrl={isAStock ? `/api/stock/a-stock/chart-events/${normalizedSymbol}` : undefined}
          xueqiuHistory={xueqiuHistoryRows}
          fundFlowHistory={fundFlowHistory}
          onKlinesChange={setKlines}
          height={600}
        />
      </Card>
      {isAStock && consensusDetail?.status === 'available' ? (
        <AStockConsensusValuationModal
          open={consensusModal.open}
          detail={consensusDetail}
          horizonOffset={consensusModal.offset}
          bound={consensusModal.bound}
          onHorizonChange={offset => setConsensusModal(previous => ({ ...previous, offset }))}
          onClose={() => setConsensusModal(previous => ({ ...previous, open: false }))}
        />
      ) : null}
      {/* 先报表本身、再算出来的估值：人得先看见财报长什么样，才谈得上判断估值 */}
      {isAStock ? <StockFinancialsCard symbol={normalizedSymbol} /> : null}
      {isAStock ? <StockValueInvestingCard symbol={normalizedSymbol} /> : null}
    </div>
  );
};

export default StockDetail;
