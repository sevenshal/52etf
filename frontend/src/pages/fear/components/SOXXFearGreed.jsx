import React, { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Col, Divider, Empty, Radio, Row, Skeleton, Space, Statistic, Tag, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../../utils/request';
import {
  TIME_RANGES,
  chinaTodayString,
  fearColor,
  fearStatus,
  fearTextColor,
  resolveFearSummaryScore,
} from '../utils';
import {
  CN_GENERAL_GROUPS,
  CN_INDUSTRY_GROUPS,
  ETF_OPTIONS,
  HK_ETF_OPTIONS,
  US_ETF_OPTIONS,
} from './fearMarketTaxonomy';
import './SOXXFearGreed.css';

const { Text } = Typography;

const industryOptionCount = group => (
  (group.options || []).length
  + (group.children || []).reduce((sum, child) => sum + (child.options || []).length, 0)
);

const SORTED_CN_INDUSTRY_GROUPS = [...CN_INDUSTRY_GROUPS]
  .sort((left, right) => industryOptionCount(right) - industryOptionCount(left));
const COMPACT_CN_INDUSTRY_KEYS = ['utilities', 'defense', 'real-estate'];
const COMPACT_CN_INDUSTRY_GROUPS = COMPACT_CN_INDUSTRY_KEYS
  .map(key => CN_INDUSTRY_GROUPS.find(group => group.key === key))
  .filter(Boolean);
const MAIN_CN_INDUSTRY_GROUPS = SORTED_CN_INDUSTRY_GROUPS
  .filter(group => !COMPACT_CN_INDUSTRY_KEYS.includes(group.key));

const DEFAULT_DETAIL_STATE = {
  data: [],
  latest: null,
  latest_holdings: [],
};

const COMPONENT_LABELS = {
  market_momentum: '市场动量',
  stock_price_strength: '成分股强度',
  stock_price_breadth: '成分股广度',
  put_call_options: '期权 P/C（持仓量）',
  put_call_volume_options: '期权 P/C（成交量）',
  market_volatility: '市场波动率',
  implied_volatility: '期权隐含波动率（IV）',
  gold_implied_volatility: '黄金隐含波动率（GVZ）',
  safe_haven_demand: '避险需求',
  junk_bond_demand: 'AA/AAA 信用利差',
  southbound_flow: '南向资金',
  short_selling_ratio: '沽空比率',
  cnn_fear_greed: 'CNN 贪恐',
  szdt_fear_greed: '外部来源贪恐',
};

const COMPONENT_VALUE_META = {
  put_call_options: {
    label: '持仓量 P/C（5日均值）',
    description: '按未平仓合约张数计算的 Put/Call；高 P/C 代表保护性 put 持仓偏高，偏向局部顶部。该分量参与总分',
  },
  put_call_volume_options: {
    label: '成交量 P/C（5日均值）',
    // 后端为使“低 P/C = 高分”而存入 -P/C；展示时还原为通常的正数 P/C。
    displayValue: value => (isFiniteNumber(value) ? -Number(value) : value),
    description: '按每日成交量计算的 Put/Call；高 P/C 通常代表恐慌买 put，作为诊断项，不参与总分',
  },
  southbound_flow: {
    label: '南向净买入（亿元，3日均）',
    description: '港股通（沪+深）每日净买入，正数=净买入；净买入越多，得分越高。仅作诊断，暂未参与总分',
  },
  short_selling_ratio: {
    label: '沽空占成交额（%）',
    // 后端存 -占比；展示时还原成正的百分比。
    displayValue: value => (isFiniteNumber(value) ? -Number(value) * 100 : value),
    description: '全市场指定证券（剔除 ETP）沽空成交额占比，5 日均；沽空越重，得分越低。仅作诊断，暂未参与总分',
  },
  market_volatility: {
    label: '相对波动变化',
    description: '20 日实现波动率相对 50 日均值的变化；波动率越高，得分越低',
  },
  implied_volatility: {
    label: '年化 IV（%）',
    // 后端为使“IV 越高 = 越恐慌”而以 -IV 参与标准化；展示时还原为正数百分比。
    displayValue: value => (isFiniteNumber(value) ? -Number(value) * 100 : value),
    description: '期权隐含的未来波动率代理；IV 越高，得分越低',
  },
  junk_bond_demand: {
    description: '中债 AA/AAA 信用利差诊断项；暂未参与总分',
  },
  gold_implied_volatility: {
    label: 'GVZ（点）',
    description: 'GLD期权隐含的30日波动率；暂未参与总分',
  },
  szdt_fear_greed: {
    label: '外部情绪分',
    description: '外部来源的标的情绪评分，已映射为 0–100 分',
  },
};

// 信用利差组件按市场区分，不能共用一条说明：
// - A股指数：中债 3 年期 AA/AAA 利差均值，仅诊断、不参与总分；
// - 美股 ETF：FRED 美国企业债期权调整利差（高收益−投资级），参与总分。
// 两种后端都存「-利差」，展示时统一还原为正数（利差越阔 = 风险偏好越差 = 得分越低）。
const JUNK_BOND_META_BY_MARKET = {
  CN: {
    label: '中债 AA−AAA 信用利差（pct）',
    displayValue: value => (isFiniteNumber(value) ? -Number(value) : value),
    description: '中债 3 年期 AA/AAA 信用利差均值（中票/企业债/城投债曲线）；仅作诊断，历史顶底方向不稳定，未参与总分',
  },
  DEFAULT: {
    label: '高收益−投资级信用利差（OAS，pct）',
    displayValue: value => (isFiniteNumber(value) ? -Number(value) : value),
    description: '美国企业债期权调整利差（FRED：高收益 BAMLH0A0HYM2 − 投资级 BAMLC0A0CM）；利差越阔（风险偏好越差），得分越低',
  },
};

// P/C 的口径按市场区分：A股/美股用单日成交量，港股用未平仓合约张数。
// 港股后端存的是 +持仓量P/C（顶部 put 持仓偏高 = 贪婪），展示时不要再取负。
const PUT_CALL_META_BY_MARKET = {
  HK: {
    label: 'P/C（持仓量口径，5日均值）',
    description: '按未平仓合约张数计算的 Put/Call。恒指期权的买方以机构和结构性产品发行商为主，put 是给多头上的保险，指数越涨持仓越高，所以高 P/C 偏向局部顶部、得分更高；与美股 Cboe 个股成交量口径的「高 P/C = 恐慌」相反',
  },
};

// 分项的展示元信息按市场取：junk_bond_demand 美股/A股数据源完全不同
const getComponentValueMeta = (key, market) => {
  if (key === 'junk_bond_demand') {
    return JUNK_BOND_META_BY_MARKET[market] || JUNK_BOND_META_BY_MARKET.DEFAULT;
  }
  if (key === 'put_call_options') {
    return PUT_CALL_META_BY_MARKET[market] || COMPONENT_VALUE_META.put_call_options;
  }
  return COMPONENT_VALUE_META[key];
};

const getComponentLabel = (key, market) => {
  if (key === 'junk_bond_demand') {
    return market === 'CN' ? 'AA/AAA 信用利差' : '信用利差（高收益−投资级）';
  }
  if (key === 'put_call_options' && market === 'HK') {
    return '期权 P/C（持仓量）';
  }
  return COMPONENT_LABELS[key];
};

const isFiniteNumber = value => value !== null && value !== undefined && Number.isFinite(Number(value));

const formatNumber = (value, digits = 1) => (
  isFiniteNumber(value) ? Number(value).toFixed(digits) : '-'
);

const formatCompactVolume = (value) => {
  if (!isFiniteNumber(value)) return '-';
  const number = Number(value);
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(1)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
  return Math.round(number).toLocaleString();
};

const formatVolumeRatio = value => (
  isFiniteNumber(value) ? `${Number(value).toFixed(2)}×` : '-'
);

const valuationColor = rating => ({
  极度低估: '#237804',
  低估: '#389e0d',
  合理: '#1677ff',
  高估: '#cf1322',
  极度高估: '#820014',
}[rating] || '#8c8c8c');

const formatRange = (low, high) => (
  isFiniteNumber(low) && isFiniteNumber(high)
    ? `${Number(low).toFixed(0)}-${Number(high).toFixed(0)}`
    : '-'
);

const rollingAverage = (values, windowSize) => values.map((_, index) => {
  if (index < windowSize - 1) return null;
  const window = values.slice(index - windowSize + 1, index + 1);
  if (!window.every(isFiniteNumber)) return null;
  return Number((window.reduce((sum, value) => sum + Number(value), 0) / windowSize).toFixed(4));
});

// 让数据最高/最低值之间的跨度占 y 轴约 80%，上下各保留约 10% 空间。
const getFocusedAxisRange = (values) => {
  const finiteValues = values.filter(isFiniteNumber).map(Number);
  if (!finiteValues.length) return {};

  const dataMin = Math.min(...finiteValues);
  const dataMax = Math.max(...finiteValues);
  const dataSpan = dataMax - dataMin;
  if (dataSpan === 0) {
    const fallbackSpan = Math.max(Math.abs(dataMin) * 0.1, 1);
    return { min: dataMin - fallbackSpan / 2, max: dataMax + fallbackSpan / 2 };
  }

  const padding = dataSpan * 0.125;
  return { min: dataMin - padding, max: dataMax + padding };
};

// 底/顶信号由后端统一计算（history 接口每行 signals 字段），前端只负责渲染。
// 两种信号类型用不同色系 + 形状区分：ma5 均线型 = 绿底/红顶方标；量能型 = 紫底/粉顶菱形。
const SIGNAL_MARK_STYLE = {
  ma5_bottom: { color: '#389e0d', symbol: 'roundRect', symbolSize: [22, 20], symbolOffset: [0, 18], mark: '底' },
  ma5_top: { color: '#cf1322', symbol: 'roundRect', symbolSize: [22, 20], symbolOffset: [0, -18], mark: '顶' },
  volume_bottom: { color: '#722ed1', symbol: 'diamond', symbolSize: [26, 26], symbolOffset: [0, 18], mark: '底' },
  volume_top: { color: '#eb2f96', symbol: 'diamond', symbolSize: [26, 26], symbolOffset: [0, -18], mark: '顶' },
};

const buildSignalMarkPoints = (kind, items) => items
  .filter(item => (item.signals || []).some(signal => signal.kind === kind))
  .map((item) => {
    const signal = (item.signals || []).find(candidate => candidate.kind === kind);
    const style = SIGNAL_MARK_STYLE[kind];
    return {
      name: signal?.label || kind, // tooltip 显示全称（均线底/放量底…）
      value: style.mark,           // 标记内显示 底/顶
      coord: [item.date, signal?.value],
      symbol: style.symbol,
      symbolSize: style.symbolSize,
      symbolOffset: style.symbolOffset,
      itemStyle: { color: style.color },
    };
  });

const formatDateTime = (value) => {
  if (!value) return '-';
  return new Date(value).toLocaleString();
};

const formatIntradayTime = (value) => {
  if (!value) return '-';
  const d = new Date(value);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

// 页面加载时算一次（与原先的 IIFE 行为一致）
const chinaToday = chinaTodayString();

const SOXXFearGreed = () => {
  const [summaries, setSummaries] = useState([]);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState(null);
  const [expandedSymbol, setExpandedSymbol] = useState(null);
  const [detailBySymbol, setDetailBySymbol] = useState({});
  const [detailLoadingSymbol, setDetailLoadingSymbol] = useState(null);
  const [detailErrorBySymbol, setDetailErrorBySymbol] = useState({});
  const [realtimeBySymbol, setRealtimeBySymbol] = useState({});
  const [realtimeLoadingSymbol, setRealtimeLoadingSymbol] = useState(null);
  const [realtimeErrorBySymbol, setRealtimeErrorBySymbol] = useState({});
  const [timeRange, setTimeRange] = useState(3);
  // 点击子分项卡片：把该分项的得分曲线叠加到历史曲线上（再点一次取消）
  const [activeComponentKey, setActiveComponentKey] = useState(null);
  const [shareFlow, setShareFlow] = useState(null);
  const [shareFlowError, setShareFlowError] = useState(null);

  useEffect(() => {
    setActiveComponentKey(null);
  }, [expandedSymbol]);

  const summaryBySymbol = useMemo(() => {
    const result = {};
    summaries.forEach(item => {
      result[item.symbol] = item;
    });
    return result;
  }, [summaries]);
  const shareFlowBySymbol = shareFlow?.by_symbol || {};

  const expandedETF = useMemo(
    () => ETF_OPTIONS.find(item => item.symbol === expandedSymbol) || null,
    [expandedSymbol]
  );
  const expandedDetail = expandedSymbol ? (detailBySymbol[expandedSymbol] || DEFAULT_DETAIL_STATE) : DEFAULT_DETAIL_STATE;
  const expandedSummary = expandedSymbol ? summaryBySymbol[expandedSymbol] : null;
  const expandedShareFlow = expandedETF ? shareFlowBySymbol[expandedETF.shareFlowSymbol || expandedETF.symbol] : null;
  const realtime = expandedSymbol ? realtimeBySymbol[expandedSymbol] : null;
  const realtimeError = expandedSymbol ? realtimeErrorBySymbol[expandedSymbol] : null;
  const detailError = expandedSymbol ? detailErrorBySymbol[expandedSymbol] : null;
  const detailLoading = expandedSymbol && detailLoadingSymbol === expandedSymbol;
  const realtimeLoading = expandedSymbol && realtimeLoadingSymbol === expandedSymbol;

  const fetchSummaries = useCallback(async () => {
    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const { data } = await request.get('/api/cnn/etf-fear-greed-clone/summaries', {
        params: {
          symbols: ETF_OPTIONS.map(item => item.symbol).join(','),
        },
      });
      setSummaries(data?.data || []);
    } catch (error) {
      setSummaryError(error?.response?.data?.detail || error.message || '获取自算贪恐摘要失败');
    } finally {
      setSummaryLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSummaries();
  }, [fetchSummaries]);

  const fetchShareFlow = useCallback(async () => {
    setShareFlowError(null);
    try {
      const { data } = await request.get('/api/a-stock-etf-share-flow/research');
      setShareFlow(data || null);
    } catch (error) {
      setShareFlowError(error?.response?.data?.detail || error.message || '获取宽基ETF净申赎数据失败');
    }
  }, []);

  useEffect(() => {
    fetchShareFlow();
  }, [fetchShareFlow]);

  useEffect(() => {
    if (!expandedSymbol || detailBySymbol[expandedSymbol]) return undefined;
    let cancelled = false;
    const fetchHistory = async () => {
      setDetailLoadingSymbol(expandedSymbol);
      setDetailErrorBySymbol(prev => ({ ...prev, [expandedSymbol]: null }));
      try {
        const { data } = await request.get('/api/cnn/etf-fear-greed-clone/history', {
          params: {
            symbol: expandedSymbol,
            include_components: true,
            include_latest_holdings: true,
          },
        });
        if (!cancelled) {
          setDetailBySymbol(prev => ({ ...prev, [expandedSymbol]: data || DEFAULT_DETAIL_STATE }));
        }
      } catch (error) {
        if (!cancelled) {
          setDetailErrorBySymbol(prev => ({
            ...prev,
            [expandedSymbol]: error?.response?.data?.detail || error.message || '获取自算贪恐历史失败',
          }));
        }
      } finally {
        if (!cancelled) setDetailLoadingSymbol(null);
      }
    };
    fetchHistory();
    return () => {
      cancelled = true;
    };
  }, [detailBySymbol, expandedSymbol]);

  useEffect(() => {
    if (!expandedSymbol || !expandedETF || expandedETF.realtime === false || realtimeBySymbol[expandedSymbol]) return undefined;
    let cancelled = false;
    const isCnIntraday = expandedETF.market === 'CN';
    const fetchRealtime = async () => {
      setRealtimeLoadingSymbol(expandedSymbol);
      setRealtimeErrorBySymbol(prev => ({ ...prev, [expandedSymbol]: null }));
      try {
        const url = isCnIntraday
          ? '/api/cnn/etf-fear-greed-clone/intraday'
          : '/api/cnn/etf-fear-greed-clone/realtime';
        const params = isCnIntraday
          ? { symbol: expandedSymbol }
          : { symbol: expandedSymbol, include_holdings_quotes: false };
        const { data } = await request.get(url, { params });
        if (cancelled) return;
        const payload = isCnIntraday ? (data?.data?.[0] || null) : data;
        if (isCnIntraday && !payload) {
          setRealtimeErrorBySymbol(prev => ({
            ...prev,
            [expandedSymbol]: '盘中贪恐快照尚未生成（每日 12:00 盘中时点生成）',
          }));
          return;
        }
        setRealtimeBySymbol(prev => ({ ...prev, [expandedSymbol]: payload }));
      } catch (error) {
        if (!cancelled) {
          setRealtimeErrorBySymbol(prev => ({
            ...prev,
            [expandedSymbol]: error?.response?.data?.detail || error.message || '获取实时贪恐失败',
          }));
        }
      } finally {
        if (!cancelled) setRealtimeLoadingSymbol(null);
      }
    };
    fetchRealtime();
    return () => {
      cancelled = true;
    };
  }, [expandedETF, expandedSymbol, realtimeBySymbol]);

  const filteredData = useMemo(() => {
    const data = expandedDetail?.data || [];
    if (timeRange === -1) return data;
    const cutoffDate = new Date();
    cutoffDate.setFullYear(cutoffDate.getFullYear() - timeRange);
    return data.filter(item => new Date(item.date) >= cutoffDate);
  }, [expandedDetail, timeRange]);

  const chartOption = useMemo(() => {
    const ticker = expandedETF?.ticker || '标的';
    const fullData = expandedDetail?.data || [];
    const fullScores = fullData.map(item => item.score ?? null);
    const fullScoreMa5 = rollingAverage(fullScores, 5);
    const fullIndexByDate = new Map(fullData.map((item, index) => [item.date, index]));
    const dates = filteredData.map(item => item.date);
    const scores = filteredData.map(item => item.score ?? null);
    const scoreMa5 = filteredData.map((item) => {
      const index = fullIndexByDate.get(item.date);
      return index === undefined ? null : fullScoreMa5[index];
    });
    // 底/顶信号由后端统一计算（含 ma5 均线型与量能型、各自 5 日冷却），前端只按类型渲染色系
    const ma5ReversalSignals = ['ma5_bottom', 'ma5_top'].flatMap(kind => buildSignalMarkPoints(kind, filteredData));
    const volumeTurnSignals = ['volume_bottom', 'volume_top'].flatMap(kind => buildSignalMarkPoints(kind, filteredData));
    const prices = filteredData.map(item => item.etf_price?.close ?? null);
    const volumes = filteredData.map(item => item.etf_price?.volume ?? null);
    const shareFlowByDate = new Map((expandedShareFlow?.history || []).map(item => [item.date, item]));
    const netShareFlowAmounts = filteredData.map(item => shareFlowByDate.get(item.date)?.estimated_flow_amount_yi ?? null);
    const showShareFlow = netShareFlowAmounts.some(isFiniteNumber);
    const volumeYAxisIndex = showShareFlow ? 3 : 2;
    const valuationYAxisIndex = showShareFlow ? 4 : 3;
    const valuationHistoryByDate = new Map(
      (expandedDetail?.valuation_history || []).map(item => [item.date, item])
    );
    const valuationRatios = filteredData.map(item => (
      valuationHistoryByDate.get(item.date)?.valuation_ratio ?? null
    ));
    const showValuationRatio = valuationRatios.some(isFiniteNumber);
    // 历史详情用了 proxy_etf 价格/成交量时，价格曲线名改为「指数名ETF价格」
    const priceSeriesName = expandedDetail?.proxy_etf ? `${ticker}ETF价格` : `${ticker}价格`;
    const priceAxisRange = getFocusedAxisRange(prices);
    const valuationAxisRange = getFocusedAxisRange(valuationRatios);

    // 选中的子分项：得分曲线画在同一个 0-100 轴上，便于和总分对照
    const activeComponentSample = activeComponentKey
      ? filteredData.reduce(
        (found, item) => found || item.components?.[activeComponentKey],
        null,
      )
      : null;
    const activeComponentName = activeComponentKey
      ? (activeComponentKey === 'market_volatility' && activeComponentSample?.raw_label === '-VIXCLS'
        ? '市场隐含波动率（VIX）'
        : getComponentLabel(activeComponentKey, expandedETF?.market)
          || activeComponentSample?.name
          || activeComponentKey)
      : null;
    const activeComponentScores = activeComponentKey
      ? filteredData.map((item) => {
        const value = item.components?.[activeComponentKey]?.score;
        return isFiniteNumber(value) ? Number(value) : null;
      })
      : [];

    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: {
        data: [
          `${ticker}贪恐`,
          '贪恐5日均线',
          ...(activeComponentName ? [activeComponentName] : []),
          priceSeriesName,
          ...(showValuationRatio ? ['估值系数'] : []),
          '成交量',
          ...(showShareFlow ? ['当日净申赎额'] : []),
        ],
        top: 0,
      },
      grid: [
        { left: 56, right: showValuationRatio ? 112 : 64, top: 48, height: showShareFlow ? '46%' : '57%' },
        { left: 56, right: showValuationRatio ? 112 : 64, top: showShareFlow ? '60%' : '72%', height: showShareFlow ? '12%' : undefined, bottom: showShareFlow ? undefined : 58 },
        ...(showShareFlow ? [{ left: 56, right: showValuationRatio ? 112 : 64, top: '79%', bottom: 58 }] : []),
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: showShareFlow ? [0, 1, 2] : [0, 1], start: 0, end: 100 },
        { type: 'slider', xAxisIndex: showShareFlow ? [0, 1, 2] : [0, 1], start: 0, end: 100, bottom: 8 },
      ],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [
        {
          type: 'category',
          boundaryGap: false,
          data: dates,
          axisLabel: { show: false },
          axisTick: { show: false },
        },
        {
          type: 'category',
          gridIndex: 1,
          boundaryGap: true,
          data: dates,
        },
        ...(showShareFlow ? [{
          type: 'category',
          gridIndex: 2,
          boundaryGap: true,
          data: dates,
        }] : []),
      ],
      yAxis: [
        {
          type: 'value',
          name: '贪恐',
          min: 0,
          max: 100,
          axisLine: { show: true, lineStyle: { color: '#13c2c2' } },
          splitLine: { lineStyle: { color: '#f0f0f0' } },
        },
        {
          type: 'value',
          name: '价格',
          ...priceAxisRange,
          axisLine: { show: true, lineStyle: { color: '#fa8c16' } },
          splitLine: { show: false },
        },
        ...(showShareFlow ? [{
          type: 'value',
          name: '净额(亿)',
          gridIndex: 2,
          axisLabel: { formatter: value => `${Number(value).toFixed(0)}亿` },
          axisLine: { show: true, lineStyle: { color: '#722ed1' } },
          splitLine: { lineStyle: { color: '#f5f5f5' } },
        }] : []),
        {
          type: 'value',
          name: '成交量',
          gridIndex: 1,
          min: 0,
          axisLabel: { formatter: formatCompactVolume },
          axisLine: { show: true, lineStyle: { color: '#8c8c8c' } },
          splitLine: { lineStyle: { color: '#f5f5f5' } },
        },
        {
          type: 'value',
          name: showValuationRatio ? '估值' : '',
          ...valuationAxisRange,
          position: 'right',
          offset: 54,
          axisLabel: { show: showValuationRatio, formatter: value => Number(value).toFixed(1) },
          axisLine: { show: showValuationRatio, lineStyle: { color: '#389e0d' } },
          axisTick: { show: showValuationRatio },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: `${ticker}贪恐`,
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: scores,
          lineStyle: { width: 2, color: '#13c2c2' },
          itemStyle: { color: '#13c2c2' },
          markPoint: {
            symbol: 'diamond',
            symbolSize: [26, 26],
            label: {
              show: true,
              color: '#ffffff',
              fontSize: 11,
              fontWeight: 700,
              formatter: '{c}',
            },
            tooltip: {
              formatter: params => `${params.data.coord[0]}<br/>${params.data.name}`,
            },
            data: volumeTurnSignals,
          },
        },
        {
          name: '贪恐5日均线',
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: scoreMa5,
          lineStyle: { width: 1.5, type: 'dashed', color: '#1677ff' },
          itemStyle: { color: '#1677ff' },
          markPoint: {
            symbol: 'roundRect',
            symbolSize: [22, 20],
            label: {
              show: true,
              color: '#ffffff',
              fontSize: 12,
              fontWeight: 700,
              formatter: '{c}',
            },
            tooltip: {
              formatter: params => `${params.data.coord[0]}<br/>${params.data.name}`,
            },
            data: ma5ReversalSignals,
          },
        },
        ...(activeComponentKey ? [{
          name: activeComponentName,
          type: 'line',
          yAxisIndex: 0,
          smooth: true,
          showSymbol: false,
          connectNulls: false,
          data: activeComponentScores,
          lineStyle: { width: 1.5, type: 'dashed', color: '#722ed1' },
          itemStyle: { color: '#722ed1' },
        }] : []),
        {
          name: priceSeriesName,
          type: 'line',
          yAxisIndex: 1,
          smooth: true,
          showSymbol: false,
          data: prices,
          lineStyle: { width: 2, color: '#fa8c16' },
          itemStyle: { color: '#fa8c16' },
        },
        ...(showValuationRatio ? [{
          name: '估值系数',
          type: 'line',
          yAxisIndex: valuationYAxisIndex,
          smooth: true,
          showSymbol: false,
          connectNulls: false,
          data: valuationRatios,
          lineStyle: { width: 1.8, type: 'dashed', color: '#389e0d' },
          itemStyle: { color: '#389e0d' },
        }] : []),
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: volumeYAxisIndex,
          data: volumes,
          barMaxWidth: 12,
          itemStyle: { color: 'rgba(89, 126, 164, 0.55)' },
          emphasis: { itemStyle: { color: '#597ea4' } },
        },
        ...(showShareFlow ? [{
          name: '当日净申赎额',
          type: 'bar',
          xAxisIndex: 2,
          yAxisIndex: 2,
          data: netShareFlowAmounts,
          barMaxWidth: 12,
          itemStyle: {
            color: params => (Number(params.value) >= 0 ? 'rgba(56, 158, 13, 0.65)' : 'rgba(207, 19, 34, 0.65)'),
          },
          emphasis: { itemStyle: { color: params => (Number(params.value) >= 0 ? '#389e0d' : '#cf1322') } },
        }] : []),
      ],
    };
  }, [activeComponentKey, expandedDetail, expandedETF, expandedShareFlow, filteredData]);

  const toggleExpanded = (symbol) => {
    setExpandedSymbol(current => (current === symbol ? null : symbol));
  };

  const realtimePrice = realtime?.etf_price?.close ?? realtime?.etf_price?.quote?.price;
  const latest = expandedDetail.latest || expandedSummary?.latest;
  const latestPrice = latest?.etf_price?.close;
  // 今日已收盘入库：点位/价格与概要卡片、历史曲线保持一致（日线收盘值）；
  // 盘中（history 尚未更新今日）才用实时快照点位（A股为盘中快照指数点位）。
  const closeIsToday = latest?.date === chinaToday;
  // 盘中快照必须是当日的：跨日后接口仍会返回上一交易日最后一条快照，不能让它
  // 覆盖已回填的日线（特别是新旧计算口径的分项不同）。当日日线已入库时，日线仍
  // 是完整且权威的详情来源。
  const realtimeIsCurrentTradeDate = realtime?.trade_date === chinaToday;
  const useRealtimeDetail = Boolean(realtime && realtimeIsCurrentTradeDate && !closeIsToday);
  const displayedRealtime = useRealtimeDetail ? realtime : null;
  const displayedRealtimeMeta = displayedRealtime?.fear_and_greed_clone;
  const displayedRealtimeScore = displayedRealtimeMeta?.score;
  const displayPrice = useRealtimeDetail ? (realtimePrice ?? latestPrice) : latestPrice;
  const topHoldings = (expandedDetail.latest_holdings || []).slice(0, 8);
  const valuation = expandedDetail.valuation || expandedSummary?.valuation;
  const valuation252Label = valuation?.valuation_position_252_label || valuation?.valuation_position_label;
  const valuation252Pct = isFiniteNumber(valuation?.valuation_position_252_pct)
    ? valuation.valuation_position_252_pct
    : valuation?.valuation_position_pct;
  const valuation252Days = Number(valuation?.valuation_history_252_days)
    || Math.min(Number(valuation?.valuation_history_days) || 0, 252);
  const valuation1000Label = valuation?.valuation_position_1000_label;
  const valuation1000Pct = valuation?.valuation_position_1000_pct;
  const valuation1000Days = Number(valuation?.valuation_history_1000_days) || 0;
  const valuationAllTimeLabel = valuation?.valuation_position_all_time_label;
  const valuationAllTimePct = valuation?.valuation_position_all_time_pct;
  const valuationAllTimeDays = Number(valuation?.valuation_history_all_time_days) || 0;
  const renderMarketMultiplePositions = (field) => {
    const metricLabel = field === 'pe_ttm' ? 'PE(TTM)' : 'PB';
    return [252, 504, 1000, 'all_time'].map(window => {
      const suffix = window === 'all_time' ? 'all_time' : window;
      const label = valuation?.[`${field}_position_${suffix}_label`];
      const pct = valuation?.[`${field}_position_${suffix}_pct`];
      const days = Number(valuation?.[`${field}_history_${suffix}_days`]) || 0;
      const windowLabel = window === 'all_time' ? '全可用历史' : `${window}日`;
      return (
        <Tag key={`${field}-${suffix}`} color={valuationColor(label)} title={`有效${days}日`}>
          {windowLabel} {label || '-'} · {isFiniteNumber(pct) ? `${formatNumber(pct, 1)}%` : '样本不足'}
        </Tag>
      );
    });
  };
  const realtimeEnabled = expandedETF?.realtime !== false;
  const pricePrecision = expandedETF?.pricePrecision ?? 2;
  // 日线作为完整基底；盘中快照只覆盖自身确实返回的字段，避免旧快照缺少新分量
  // （例如 A 股 IV）时把最新日线分量抹掉。
  const componentValues = {
    ...(latest?.components || {}),
    ...(displayedRealtime?.components || {}),
  };
  // A 股已下线「实现波动率」诊断项（波动率维度统一用期权 IV 进总分），后端不再计算。
  // 旧入库行的 components 里仍带着这个键，这里再兜一层，避免详情继续显示。
  if (expandedETF?.market === 'CN') {
    delete componentValues.market_volatility;
  }
  const componentEntries = Object.entries(componentValues);

  const detailRef = useRef(null);

  useEffect(() => {
    if (expandedSymbol && detailRef.current) {
      detailRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [expandedSymbol]);

  const detailNode = expandedSymbol ? (
    <div ref={detailRef} className="soxx-fear-detail">

      <div className="soxx-fear-detail-header">
        <div>
          <Text strong>{expandedETF?.ticker}</Text>
          <Text type="secondary" style={{ marginLeft: 8 }}>{expandedETF?.symbol}</Text>
        </div>
        <Tag color={fearColor(displayedRealtimeScore ?? latest?.score)}>
          {fearStatus(displayedRealtimeScore ?? latest?.score)}
        </Tag>
      </div>

      {detailError && (
        <Alert type="warning" showIcon message={`${expandedETF?.ticker}贪恐历史暂不可用`} description={detailError} />
      )}

      {!detailError && detailLoading && <Skeleton active paragraph={{ rows: 6 }} />}

      {!detailError && !detailLoading && !latest && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`${expandedETF?.ticker} 暂无入库记录`} />
      )}

      {!detailError && !detailLoading && latest && (
        <>
          {realtimeError && (
            <Alert
              type="info"
              showIcon
              message={`${expandedETF?.ticker} 实时贪恐暂不可用，当前显示最新入库日线值`}
              description={realtimeError}
              style={{ marginBottom: 12 }}
            />
          )}

          <Row gutter={[12, 12]} className="soxx-fear-detail-stats">
            <Col xs={12} sm={8} lg={4}>
              <Statistic
                title={realtimeEnabled ? '实时/最新' : '最新'}
                value={displayedRealtimeScore ?? latest.score}
                precision={1}
                valueStyle={{ color: fearTextColor(displayedRealtimeScore ?? latest.score) }}
                suffix="/100"
              />
              {realtimeEnabled && realtimeLoading && <Tag color="processing">实时计算中</Tag>}
            </Col>
            <Col xs={12} sm={8} lg={4}>
              <Statistic title="入库分数" value={latest.score} precision={1} suffix="/100" />
            </Col>
            <Col xs={12} sm={8} lg={4}>
              <Statistic
                title={expandedDetail?.proxy_etf ? '价格' : (expandedETF?.priceLabel || '价格')}
                value={displayPrice}
                precision={pricePrecision}
                prefix={expandedETF?.symbol.endsWith('.US') ? '$' : undefined}
              />
            </Col>
            <Col xs={12} sm={8} lg={4}>
              <Statistic title="数据日期" value={latest.date} />
            </Col>
            <Col xs={12} sm={8} lg={4}>
              <Statistic title="报价时间" value={useRealtimeDetail ? formatDateTime(displayedRealtimeMeta?.timestamp) : '日线'} />
            </Col>
            <Col xs={12} sm={8} lg={4}>
              <Statistic title="历史样本" value={expandedSummary?.history_points ?? expandedDetail.count ?? '-'} />
            </Col>
          </Row>

          {componentEntries.length > 0 && (
            <Card
              size="small"
              className="soxx-fear-components-detail"
              title={(
                <span>
                  {displayedRealtime?.components ? '子指标（实时）' : '子指标（最新日线）'}
                  <Text type="secondary" style={{ fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
                    点击分项可叠加到历史曲线
                  </Text>
                </span>
              )}
            >
              <div className="soxx-fear-components-grid">
                {componentEntries.map(([key, component]) => {
                  const meta = getComponentValueMeta(key, expandedETF?.market);
                  const isVixRaw = key === 'market_volatility' && component.raw_label === '-VIXCLS';
                  const isActiveComponent = activeComponentKey === key;
                  const toggleComponent = () => {
                    setActiveComponentKey(current => (current === key ? null : key));
                  };
                  const displayLabel = isVixRaw ? 'VIX（点）' : meta?.label || '模型原始值';
                  const displayRaw = isVixRaw
                    ? (isFiniteNumber(component.raw_value) ? -Number(component.raw_value) : component.raw_value)
                    : meta?.displayValue?.(component.raw_value) ?? component.raw_value;
                  const description = isVixRaw
                    ? 'VIX（市场隐含波动率）越高，得分越低'
                    : meta?.description || component.raw_label;
                  return (
                    <div
                      className={`soxx-fear-component${isActiveComponent ? ' is-active' : ''}`}
                      key={key}
                      role="button"
                      tabIndex={0}
                      title={isActiveComponent ? '点击取消曲线叠加' : '点击在历史曲线上叠加该分项得分'}
                      onClick={toggleComponent}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          toggleComponent();
                        }
                      }}
                    >
                      <div className="soxx-fear-component-header">
                        <Text strong>{isVixRaw
                          ? '市场隐含波动率（VIX）'
                          : getComponentLabel(key, expandedETF?.market) || component.name || key}</Text>
                        <span className="soxx-fear-component-tags">
                          {component.included_in_total === false && (
                            <Tag color="default">暂未参与总分</Tag>
                          )}
                          {isFiniteNumber(component.score) && (
                            // 分项评级统一用前端的五档（极端恐慌/恐慌/中性/贪婪/极端贪婪），
                            // 不再展示后端入库的英文 rating，避免同一分数两套口径。
                            <Tag color={fearColor(component.score)}>{fearStatus(component.score)}</Tag>
                          )}
                        </span>
                      </div>
                      <div className="soxx-fear-component-values">
                        <span>
                          {displayLabel} <b>{formatNumber(displayRaw, 3)}</b>
                        </span>
                        <span>得分 <b>{formatNumber(component.score, 1)}</b>/100</span>
                      </div>
                      {description && (
                        <Text type="secondary">{description}</Text>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>
          )}

          {expandedETF?.market !== 'US' && expandedETF?.market !== 'HK' && valuation?.status === 'available' && (
            <Card size="small" className="soxx-fear-valuation-detail" title="指数成分估值">
              <div className="soxx-fear-valuation-compact">
                <div className="soxx-fear-valuation-current">
                  <span>当前估值系数（越大越贵）</span>
                  <strong style={{ color: valuationColor(valuation.valuation_position_label) }}>
                    {isFiniteNumber(valuation.current_gap_pct) ? (1 - valuation.current_gap_pct / 100).toFixed(2) : '-'}
                  </strong>
                  <Tag>目标区间 {formatRange(valuation.fair_value_lo, valuation.fair_value_hi)}</Tag>
                </div>
                <Space wrap size={[4, 4]} className="soxx-fear-valuation-positions">
                  <Tag color={valuationColor(valuation.valuation_position_label)} title={`有效${valuation.valuation_history_days || 0}日`}>
                    504日 {valuation.valuation_position_label || '-'} · {isFiniteNumber(valuation.valuation_position_pct) ? `${formatNumber(valuation.valuation_position_pct, 1)}%` : '样本不足'}
                  </Tag>
                  <Tag color={valuationColor(valuation252Label)} title={`有效${valuation252Days}日`}>
                    252日 {valuation252Label || '-'} · {isFiniteNumber(valuation252Pct) ? `${formatNumber(valuation252Pct, 1)}%` : '样本不足'}
                  </Tag>
                  <Tag color={valuationColor(valuation1000Label)} title={`有效${valuation1000Days}日`}>
                    1000日 {valuation1000Label || '-'} · {isFiniteNumber(valuation1000Pct) ? `${formatNumber(valuation1000Pct, 1)}%` : '样本不足'}
                  </Tag>
                  <Tag color={valuationColor(valuationAllTimeLabel)} title={`可用历史${valuationAllTimeDays}日`}>
                    全可用历史 {valuationAllTimeLabel || '-'} · {isFiniteNumber(valuationAllTimePct) ? `${formatNumber(valuationAllTimePct, 1)}%` : '样本不足'}
                  </Tag>
                </Space>
                <div className="soxx-fear-market-multiple-row">
                  <strong>PE(TTM) {formatNumber(valuation.pe_ttm, 2)}×</strong>
                  <Space wrap size={[4, 4]}>{renderMarketMultiplePositions('pe_ttm')}</Space>
                </div>
                <div className="soxx-fear-market-multiple-row">
                  <strong>PB {formatNumber(valuation.pb, 2)}×</strong>
                  <Space wrap size={[4, 4]}>{renderMarketMultiplePositions('pb')}</Space>
                </div>
                <div className="soxx-fear-valuation-meta">
                  <span>有效权重 {((valuation.effective_coverage_ratio ?? valuation.coverage_ratio ?? 0) * 100).toFixed(1)}%（{valuation.covered_count}/{valuation.constituent_count || '-'}）</span>
                  <span>估值日 {valuation.valuation_date_max || '-'} </span>
                  <span className={(valuation.stale_weight_ratio ?? 0) > 0.3 ? 'soxx-fear-valuation-stale' : ''}>
                    待更新 {((valuation.stale_weight_ratio ?? 0) * 100).toFixed(1)}%
                  </span>
                </div>
              </div>
              <Text type="secondary" className="soxx-fear-valuation-note">
                估值系数、PE(TTM)、PB 的点位均为历史百分位（≥80 极度高估、低于20 极度低估），少于120个有效交易日不评级。PE/PB 只聚合正数成分倍数；覆盖不足或研报待更新的成分会计入“待更新”。
              </Text>
            </Card>
          )}

          {topHoldings.length > 0 && (
            <Space wrap size={[8, 8]} className="soxx-fear-holdings">
              {topHoldings.map(holding => (
                <Tag key={holding.symbol}>
                  {holding.symbol} {(holding.weight * 100).toFixed(2)}%
                </Tag>
              ))}
            </Space>
          )}

          <div className="soxx-fear-chart-toolbar">
            <Radio.Group
              value={timeRange}
              onChange={event => setTimeRange(event.target.value)}
              optionType="button"
              buttonStyle="solid"
            >
              {TIME_RANGES.filter(range => [1, 3, 5, -1].includes(range.value)).map(range => (
                <Radio.Button key={range.value} value={range.value}>
                  {range.label}
                </Radio.Button>
              ))}
            </Radio.Group>
          </div>

          <ReactECharts option={chartOption} className="soxx-fear-chart" />
        </>
      )}
    </div>
  ) : null;

  return (
    <Card
      title="自算贪恐"
      className="soxx-fear-card"
      extra={<Button size="small" onClick={fetchSummaries} loading={summaryLoading}>刷新</Button>}
    >
      {summaryError && (
        <Alert type="warning" showIcon message="自算贪恐摘要暂不可用" description={summaryError} style={{ marginBottom: 12 }} />
      )}

      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="宽基 ETF 净申赎强度（研究观察）"
        extra={<Button size="small" onClick={fetchShareFlow}>刷新</Button>}
      >
        {shareFlowError && <Alert type="warning" showIcon message="份额数据暂不可用" description={shareFlowError} />}
        {!shareFlowError && !shareFlow && <Skeleton active paragraph={{ rows: 2 }} title={false} />}
        {!shareFlowError && shareFlow && (
          <>
            <Row gutter={[12, 12]}>
              <Col xs={12} sm={8} md={6} xl={4}><Statistic title="净申赎强度" value={shareFlow.latest?.score} precision={1} suffix="/100" /></Col>
              <Col xs={12} sm={8} md={6} xl={4}><Statistic title="5日估算净申赎" value={shareFlow.latest?.estimated_flow_amount_yi} precision={2} suffix="亿元" /></Col>
              <Col xs={12} sm={8} md={6} xl={4}><Statistic title="近20日估算净申赎" value={shareFlow.latest?.estimated_flow_20d_amount_yi} precision={2} suffix="亿元" /></Col>
              <Col xs={12} sm={8} md={6} xl={4}><Statistic title="5日规模占比" value={shareFlow.latest?.flow_5d_ratio_pct} precision={3} suffix="%" /></Col>
              <Col xs={12} sm={8} md={6} xl={4}><Statistic title="数据日期" value={shareFlow.latest?.date || '-'} /></Col>
            </Row>
            <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>{shareFlow.proxy_note}</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>{shareFlow.score_direction}；暂未参与任何自算贪恐总分。</Text>
            <div style={{ marginTop: 10, fontSize: 12 }}>
              <Text strong>历史阶段（沪深300前后30交易日确认）：</Text>{' '}
              {(shareFlow.analysis?.phase_distribution || []).map(item => (
                <Tag key={item.phase} color={item.phase === 'bottom' ? 'green' : 'volcano'}>
                  {item.phase === 'bottom' ? '阶段底' : '阶段顶'} n={item.count}，中位数 {formatNumber(item.median, 1)}
                </Tag>
              ))}
            </div>
            <div style={{ marginTop: 8, fontSize: 12 }}>
              <Text strong>无前视未来收益（按当日分数分桶）：</Text>{' '}
              {(shareFlow.analysis?.future_returns || []).map(item => (
                <Tag key={item.bucket}>
                  {item.bucket}：20日 {formatNumber(item.return_20d_mean_pct, 1)}%，60日 {formatNumber(item.return_60d_mean_pct, 1)}%，120日 {formatNumber(item.return_120d_mean_pct, 1)}%，200日 {formatNumber(item.return_200d_mean_pct, 1)}%（n={item.count}）
                </Tag>
              ))}
            </div>
          </>
        )}
      </Card>

      {summaryLoading && !summaries.length ? (
        <>
          <div className="soxx-fear-summary-grid">
            {US_ETF_OPTIONS.map(item => (
              <div className="soxx-fear-summary-card" key={item.symbol}>
                <Skeleton active paragraph={{ rows: 3 }} title={false} />
              </div>
            ))}
          </div>
          <Divider className="soxx-fear-market-divider"></Divider>
          <div className="soxx-fear-summary-grid">
            {HK_ETF_OPTIONS.map(item => (
              <div className="soxx-fear-summary-card" key={item.symbol}>
                <Skeleton active paragraph={{ rows: 3 }} title={false} />
              </div>
            ))}
          </div>
          <Divider className="soxx-fear-market-divider">A股</Divider>
          <div className="soxx-fear-industry-loading">
            {SORTED_CN_INDUSTRY_GROUPS.slice(0, 4).map(group => (
              <div className="soxx-fear-summary-card" key={group.key}>
                <Skeleton active paragraph={{ rows: 3 }} title={false} />
              </div>
            ))}
          </div>
        </>
      ) : (
        <>
          <CardGrid
            options={US_ETF_OPTIONS}
            summaryBySymbol={summaryBySymbol}
            shareFlowBySymbol={shareFlowBySymbol}
            expandedSymbol={expandedSymbol}
            onToggle={toggleExpanded}
            detailNode={detailNode}
          />
          <Divider className="soxx-fear-market-divider">港股</Divider>
          <CardGrid
            options={HK_ETF_OPTIONS}
            summaryBySymbol={summaryBySymbol}
            shareFlowBySymbol={shareFlowBySymbol}
            expandedSymbol={expandedSymbol}
            onToggle={toggleExpanded}
            detailNode={detailNode}
          />
          <Divider className="soxx-fear-market-divider">A股</Divider>
          <div className="soxx-fear-cn-groups">
            {CN_GENERAL_GROUPS.map(group => (
              <MarketGroup
                key={group.key}
                title={group.title}
                options={group.options}
                summaryBySymbol={summaryBySymbol}
                shareFlowBySymbol={shareFlowBySymbol}
                expandedSymbol={expandedSymbol}
                onToggle={toggleExpanded}
                detailNode={detailNode}
              />
            ))}

            <div className="soxx-fear-industry-heading">
              <div>
                <Text strong>行业分类</Text>
                <Text type="secondary">按通达信一二三级结构归组，卡片使用公开指数计算</Text>
              </div>
              <span>{CN_INDUSTRY_GROUPS.length} 个一级行业</span>
            </div>

            <div className="soxx-fear-industry-groups">
              {MAIN_CN_INDUSTRY_GROUPS.map(group => (
                <Fragment key={group.key}>
                  <IndustryGroup
                    group={group}
                    summaryBySymbol={summaryBySymbol}
                    shareFlowBySymbol={shareFlowBySymbol}
                    expandedSymbol={expandedSymbol}
                    onToggle={toggleExpanded}
                  />
                  {industryGroupContains(group, expandedSymbol) && detailNode}
                </Fragment>
              ))}
            </div>
            <div className="soxx-fear-compact-groups">
              {COMPACT_CN_INDUSTRY_GROUPS.map(group => (
                <Fragment key={group.key}>
                  <IndustryGroup
                    group={group}
                    summaryBySymbol={summaryBySymbol}
                    shareFlowBySymbol={shareFlowBySymbol}
                    expandedSymbol={expandedSymbol}
                    onToggle={toggleExpanded}
                  />
                  {industryGroupContains(group, expandedSymbol) && detailNode}
                </Fragment>
              ))}
            </div>
          </div>
        </>
      )}
    </Card>
  );
};

const CardGrid = ({ options, summaryBySymbol, shareFlowBySymbol = {}, expandedSymbol, onToggle, detailNode }) => {
  const expandedIndex = options.findIndex(item => item.symbol === expandedSymbol);
  return (
    <div className="soxx-fear-summary-grid">
      {options.map((item, index) => (
        <Fragment key={item.symbol}>
          <SummaryCard
            option={item}
            summary={summaryBySymbol[item.symbol]}
            shareFlowSummary={shareFlowBySymbol[item.shareFlowSymbol || item.symbol]}
            active={expandedSymbol === item.symbol}
            onToggle={() => onToggle(item.symbol)}
          />
          {index === expandedIndex && detailNode}
        </Fragment>
      ))}
    </div>
  );
};

const MarketGroup = ({ title, options, summaryBySymbol, shareFlowBySymbol, expandedSymbol, onToggle, detailNode }) => (
  <section className="soxx-fear-market-group">
    <div className="soxx-fear-group-title">
      <Text strong>{title}</Text>
      <span>{options.length}</span>
    </div>
    <CardGrid
      options={options}
      summaryBySymbol={summaryBySymbol}
      shareFlowBySymbol={shareFlowBySymbol}
      expandedSymbol={expandedSymbol}
      onToggle={onToggle}
      detailNode={detailNode}
    />
  </section>
);

const industryGroupContains = (group, symbol) => Boolean(symbol) && [
  ...(group.options || []),
  ...(group.children || []).flatMap(child => child.options || []),
].some(item => item.symbol === symbol);

const IndustryGroup = ({ group, summaryBySymbol, shareFlowBySymbol, expandedSymbol, onToggle }) => {
  const count = industryOptionCount(group);
  const options = [
    ...(group.options || []).map(item => ({
      ...item,
      groupLabel: '一级指数',
    })),
    ...(group.children || []).flatMap(child => (
      (child.options || []).map((item, index) => ({
        ...item,
        groupLabel: child.title,
        groupCode: child.code,
        groupStart: index === 0,
      }))
    )),
  ];
  const sizeClass = group.layout === 'wide' || count >= 6
    ? ' is-wide'
    : count === 2
      ? ' is-pair'
      : count === 1
        ? ' is-compact'
        : '';
  return (
    <details className={`soxx-fear-industry-group${sizeClass}`} open>
      <summary>
        <span className="soxx-fear-industry-name">{group.title}</span>
        <span className="soxx-fear-industry-code">{group.taxonomyCode}</span>
        <span className="soxx-fear-industry-count">{count} 个指数</span>
      </summary>
      <div className="soxx-fear-industry-content">
        <CardGrid
          options={options}
          summaryBySymbol={summaryBySymbol}
          shareFlowBySymbol={shareFlowBySymbol}
          expandedSymbol={expandedSymbol}
          onToggle={onToggle}
        />
      </div>
    </details>
  );
};

const SummaryCard = ({ option, summary, shareFlowSummary, active, onToggle }) => {
  const latest = summary?.latest;
  const intraday = summary?.intraday;
  // 盘中/收盘取舍与个股详情页「所属指数 · 贪恐」共用同一个函数，两处永远显示同一个数
  const { score, mode, closeLabel } = resolveFearSummaryScore(summary, chinaToday);
  const showIntraday = mode === 'intraday';
  const scoreColor = fearColor(score);
  const sevenDayScore = summary?.seven_day_ago?.score;
  const oneMonthScore = summary?.one_month_ago?.score;
  const price = showIntraday ? (intraday?.index_level ?? latest?.etf_price?.close) : latest?.etf_price?.close;
  const valuation = summary?.valuation;
  // A股指数用成分一致预期估值，美股指数ETF用美股ETF估值分析（EVC 成分公允价值），港股暂无
  const showValuation = option.market !== 'HK' && valuation?.status === 'available';
  return (
    <button
      type="button"
      className={`soxx-fear-summary-card${active ? ' is-active' : ''}${option.groupStart ? ' is-subgroup-start' : ''}`}
      onClick={onToggle}
    >
      <div className="soxx-fear-summary-top">
        <div>
          <div className="soxx-fear-summary-name">{option.ticker}</div>
          <div className="soxx-fear-summary-meta">
            {option.groupLabel && (
              <span className="soxx-fear-group-marker" title={option.groupCode || undefined}>
                {option.groupLabel}
              </span>
            )}
            <span>{option.groupLabel ? option.symbol : `${option.leafLabel || option.label} · ${option.symbol}`}</span>
          </div>
        </div>
        {latest ? (
          <Tag color={scoreColor}>{fearStatus(score)}</Tag>
        ) : (
          <Tag>未入库</Tag>
        )}
      </div>

      <div className={`soxx-fear-summary-score${showIntraday ? ' is-dual' : ''}`}>
        {showIntraday ? (
          <>
            <span className="soxx-fear-score-duo">
              <span className="soxx-fear-score-duo-label">盘中</span>
              <span className="soxx-fear-score-duo-value" style={{ color: fearTextColor(intraday.score) }}>
                {formatNumber(intraday.score, 1)}
              </span>
            </span>
            <span className="soxx-fear-score-duo soxx-fear-score-duo-secondary">
              <span className="soxx-fear-score-duo-label">{closeLabel}</span>
              <span className="soxx-fear-score-duo-value" style={{ color: fearTextColor(latest?.score) }}>
                {formatNumber(latest?.score, 1)}
              </span>
            </span>
          </>
        ) : (
          <span className="soxx-fear-score-duo">
            <span className="soxx-fear-score-duo-label">{closeLabel}</span>
            <span className="soxx-fear-score-duo-value" style={{ color: fearTextColor(latest?.score) }}>
              {formatNumber(latest?.score, 1)}
            </span>
          </span>
        )}
      </div>

      <div className="soxx-fear-summary-metrics">
        <MetricCell label="7天前" value={formatNumber(sevenDayScore, 1)} color={fearTextColor(sevenDayScore)} />
        <MetricCell label="1月前" value={formatNumber(oneMonthScore, 1)} color={fearTextColor(oneMonthScore)} />
        <MetricCell label={option.priceLabel || '价格'} value={formatNumber(price, option.pricePrecision ?? 2)} />
      </div>

      <div className="soxx-fear-summary-footer">
        <span>
          {showIntraday ? `盘中 ${formatIntradayTime(intraday.snapshot_time)} · ${closeLabel} ${latest?.date || '-'}` : (latest?.date || '-')}
        </span>
        <span title="恐贪日期成交量 ÷ 不含当日的前20个交易日平均成交量">
          量比 {formatVolumeRatio(summary?.volume_ratio_20d)}
        </span>
        {shareFlowSummary?.latest && (
          <span title="当日ETF总份额 ÷ 不含当日的前20个交易日平均总份额；大于1表示份额高于近期常态">
            份额比 {formatVolumeRatio(shareFlowSummary.latest.share_ratio_20d)}
          </span>
        )}
      </div>
      {showValuation && (
        <div className="soxx-fear-summary-valuation">
          <span style={{ color: valuationColor(valuation.valuation_position_label) }}>
            {valuation.valuation_position_label || '-'}
          </span>
          <span>
            近{valuation.valuation_history_days || 0}日{' '}
            {isFiniteNumber(valuation.valuation_position_pct)
              ? `${formatNumber(valuation.valuation_position_pct, 1)}%分位`
              : '样本不足'}
          </span>
          <span>
            有效权重 {(Number(valuation.effective_coverage_ratio ?? valuation.coverage_ratio ?? 0) * 100).toFixed(0)}%
          </span>
        </div>
      )}
    </button>
  );
};

const MetricCell = ({ label, value, color }) => (
  <div className="soxx-fear-metric-cell">
    <span>{label}</span>
    <strong style={{ color }}>{value}</strong>
  </div>
);

export default SOXXFearGreed;
