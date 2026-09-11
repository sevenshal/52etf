import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Form, InputNumber, Popover, Segmented, Space, Spin, Switch } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';
import { formatNumber } from '../utils/format';
import {
  appendRollingPocSupportResistance,
  calculateCloseMovingAverage,
  preprocessKlinesVolume,
} from '../utils/klines';
import { appendNineTurnAtr } from '../utils/nineTurn';
import { DEFAULT_MACD_PARAMS, calculateMacd } from '../utils/macd';
import { buildEventMarkers } from '../utils/klineEvents';
import { buildKlinePaneLayout } from '../utils/klineLayout';
import { XUEQIU_DIRECTIONS, XUEQIU_DIRECTION_META, alignXueqiuHistory } from '../utils/xueqiuHoldings';
import KlineEventDrawer from './KlineEventDrawer';

const POC_WINDOW_OPTIONS = [
  { label: '125', value: 125 },
  { label: '250', value: 250 },
  { label: '500', value: 500 },
];
const VOLUME_LOOKBACK_DAYS = 60;
const VOLUME_BASELINE_SERIES_NAME = `成交量${VOLUME_LOOKBACK_DAYS}日几何均线`;
const MACD_HISTOGRAM_SERIES_NAME = 'MACD柱';
const UP_COLOR = '#ef232a';
const DOWN_COLOR = '#14b143';
const DIF_COLOR = '#f5a623';
const DEA_COLOR = '#1890ff';
const RESEARCH_SERIES_NAME = '研报';
const FINANCIAL_SERIES_NAME = '财报';
const RESEARCH_COLOR = '#f0a54a';
const FINANCIAL_COLOR = '#7b61c9';
const EMPTY_EVENTS = { research_days: [], financial_reports: [] };
const XUEQIU_WEIGHT_SERIES_NAME = '雪球综合权重';
const XUEQIU_DIRECTION_SERIES_NAME = '5日权价比方向';
const XUEQIU_WEIGHT_COLOR = '#1677ff';
// 雪球历史接口一次最多 2000 条；5 年 K 线约 1250 个交易日，1300 足够覆盖
const XUEQIU_HISTORY_LIMIT = 1300;
// 副图图例：放在各自副图正上方的缝隙里，只管本副图的系列，不和主图的 K线/MA20 混在一起
const SUB_LEGEND_STYLE = { left: '10%', itemWidth: 12, itemHeight: 8, itemGap: 12, textStyle: { fontSize: 11 } };

/** 在 K 线最高价上方画一个圆形标记，数量 >1 时右上角加红色角标（像券商 App 那样） */
const buildEventSeries = (name, markers, highs, color, glyph) => ({
  name,
  type: 'custom',
  xAxisIndex: 0,
  yAxisIndex: 0,
  z: 20,
  itemStyle: { color },
  tooltip: { show: false },
  data: markers.map(marker => [marker.index, highs[marker.index] ?? 0, marker.slot, marker.count]),
  encode: { x: 0, y: 1 },
  renderItem: (params, api) => {
    const [x, y] = api.coord([api.value(0), api.value(1)]);
    const cy = y - 16 - api.value(2) * 24;
    const count = api.value(3);
    const children = [
      { type: 'circle', shape: { cx: x, cy, r: 9 }, style: { fill: color, stroke: '#fff', lineWidth: 1.5 } },
      {
        type: 'text',
        style: { text: glyph, x, y: cy, fill: '#fff', fontSize: 11, fontWeight: 'bold', align: 'center', verticalAlign: 'middle' },
      },
    ];
    if (count > 1) {
      children.push(
        { type: 'circle', shape: { cx: x + 8, cy: cy - 8, r: 6.5 }, style: { fill: '#f5222d', stroke: '#fff', lineWidth: 1 } },
        {
          type: 'text',
          style: { text: count > 99 ? '99+' : String(count), x: x + 8, y: cy - 8, fill: '#fff', fontSize: 9, align: 'center', verticalAlign: 'middle' },
        },
      );
    }
    return { type: 'group', children, cursor: 'pointer' };
  },
});

const toPositiveNumber = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) && num > 0 ? num : null;
};

const toFiniteNumber = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
};

const renderMetricTitle = (title, content) => (
  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
    <span>{title}</span>
    <Popover
      trigger="click"
      placement="topLeft"
      content={<div style={{ maxWidth: 320, lineHeight: 1.6 }}>{content}</div>}
    >
      <InfoCircleOutlined
        onClick={(e) => e.stopPropagation()}
        style={{ color: '#8c8c8c', cursor: 'pointer', fontSize: 12 }}
      />
    </Popover>
  </span>
);

const formatDateKey = (value) => dayjs(value).format('YYYY-MM-DD');

const buildValuationContext = (dates, valuationHistory, fillMode, dateOffsetDays = 0) => {
  const fairValueHi = [];
  const fairValueLo = [];
  const forwardNextFyHi = [];
  const forwardNextFyLo = [];
  const forwardNext2FyHi = [];
  const forwardNext2FyLo = [];
  const valuationByDate = {};

  const sortedHistory = [...(valuationHistory || [])]
    .filter(item => item?.date)
    .map(item => ({
      ...item,
      dateKey: dayjs(item.date).add(dateOffsetDays, 'day').format('YYYY-MM-DD'),
    }))
    .sort((a, b) => a.dateKey.localeCompare(b.dateKey));

  if (!sortedHistory.length) {
    dates.forEach(() => {
      fairValueHi.push(null);
      fairValueLo.push(null);
      forwardNextFyHi.push(null);
      forwardNextFyLo.push(null);
      forwardNext2FyHi.push(null);
      forwardNext2FyLo.push(null);
    });
    return { fairValueHi, fairValueLo, forwardNextFyHi, forwardNextFyLo, forwardNext2FyHi, forwardNext2FyLo, valuationByDate };
  }

  if (fillMode === 'forward') {
    let historyIndex = 0;
    let latestHistory = null;
    dates.forEach(dateStr => {
      while (historyIndex < sortedHistory.length && sortedHistory[historyIndex].dateKey <= dateStr) {
        latestHistory = sortedHistory[historyIndex];
        historyIndex += 1;
      }
      valuationByDate[dateStr] = latestHistory;
      fairValueHi.push(toFiniteNumber(latestHistory?.fair_value_hi));
      fairValueLo.push(toFiniteNumber(latestHistory?.fair_value_lo));
      forwardNextFyHi.push(toFiniteNumber(latestHistory?.forward_next_fy_hi));
      forwardNextFyLo.push(toFiniteNumber(latestHistory?.forward_next_fy_lo));
      forwardNext2FyHi.push(toFiniteNumber(latestHistory?.forward_next2_fy_hi));
      forwardNext2FyLo.push(toFiniteNumber(latestHistory?.forward_next2_fy_lo));
    });
    return { fairValueHi, fairValueLo, forwardNextFyHi, forwardNextFyLo, forwardNext2FyHi, forwardNext2FyLo, valuationByDate };
  }

  const historyMap = {};
  sortedHistory.forEach(item => {
    historyMap[item.dateKey] = item;
  });
  dates.forEach(dateStr => {
    const item = historyMap[dateStr];
    valuationByDate[dateStr] = item;
    fairValueHi.push(toFiniteNumber(item?.fair_value_hi));
    fairValueLo.push(toFiniteNumber(item?.fair_value_lo));
    forwardNextFyHi.push(toFiniteNumber(item?.forward_next_fy_hi));
    forwardNextFyLo.push(toFiniteNumber(item?.forward_next_fy_lo));
    forwardNext2FyHi.push(toFiniteNumber(item?.forward_next2_fy_hi));
    forwardNext2FyLo.push(toFiniteNumber(item?.forward_next2_fy_lo));
  });
  return { fairValueHi, fairValueLo, forwardNextFyHi, forwardNextFyLo, forwardNext2FyHi, forwardNext2FyLo, valuationByDate };
};

const hasSeriesData = (values) => values.some(value => value !== null && value !== undefined);

const StockKlineChart = ({
  symbol,
  klineUrl,
  valuationHistory = [],
  valuationFillMode = 'exact',
  valuationDateOffsetDays = -1,
  onKlinesChange,
  realtimeQuote,
  eventsUrl,
  xueqiuHistoryUrl,
  height = 600,
}) => {
  const [loading, setLoading] = useState(true);
  const [rawKlines, setRawKlines] = useState([]);
  const [processedKlines, setProcessedKlines] = useState([]);
  const [supportResistanceWindow, setSupportResistanceWindow] = useState(125);
  const [volumeStdDevMultiplier, setVolumeStdDevMultiplier] = useState(1);
  const [showSupportResistance, setShowSupportResistance] = useState(true);
  const [enableTurnoverDecay, setEnableTurnoverDecay] = useState(true);
  const [macdParams, setMacdParams] = useState(DEFAULT_MACD_PARAMS);
  const [chartOption, setChartOption] = useState({});
  const [chartEvents, setChartEvents] = useState(EMPTY_EVENTS);
  const [xueqiuHistory, setXueqiuHistory] = useState([]);
  const [activeEvent, setActiveEvent] = useState(null);
  const zoomRef = useRef(null);
  // 图表点击回调在 onChartReady 时只绑定一次，最新的标记和日期经 ref 传进去
  const eventMarkersRef = useRef({ dates: [], research: [], financial: [] });

  useEffect(() => {
    setChartEvents(EMPTY_EVENTS);
    setActiveEvent(null);
    if (!eventsUrl) return undefined;
    let cancelled = false;
    request.get(eventsUrl, {
      params: {
        start_date: dayjs().subtract(5, 'year').format('YYYY-MM-DD'),
        end_date: dayjs().format('YYYY-MM-DD'),
      },
    })
      .then(({ data }) => {
        if (!cancelled) {
          setChartEvents({
            research_days: data?.research_days || [],
            financial_reports: data?.financial_reports || [],
          });
        }
      })
      .catch(error => console.error('获取K线事件标记失败:', error));
    return () => { cancelled = true; };
  }, [eventsUrl]);

  // 雪球持仓历史：与「雪球持仓」模块同一个接口、同一默认口径(只统计主理人活跃组合)
  useEffect(() => {
    setXueqiuHistory([]);
    if (!xueqiuHistoryUrl) return undefined;
    let cancelled = false;
    request.get(xueqiuHistoryUrl, { params: { active_only: true, limit: XUEQIU_HISTORY_LIMIT } })
      .then(({ data }) => {
        if (!cancelled) setXueqiuHistory(data?.history || []);
      })
      .catch(error => console.error('获取雪球持仓历史失败:', error));
    return () => { cancelled = true; };
  }, [xueqiuHistoryUrl]);

  const fetchKlines = useCallback(async () => {
    setLoading(true);
    setRawKlines([]);
    zoomRef.current = null;
    try {
      const { data } = await request.get(klineUrl || `/api/stock/klines/${symbol}`, {
        params: {
          start_date: dayjs().subtract(5, 'year').format('YYYY-MM-DD'),
          end_date: dayjs().format('YYYY-MM-DD')
        }
      });
      const normalized = [...(data || [])].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
      setRawKlines(normalized);
    } catch (error) {
      console.error('获取K线数据失败:', error);
    } finally {
      setLoading(false);
    }
  }, [klineUrl, symbol]);

  useEffect(() => {
    fetchKlines();
  }, [fetchKlines]);

  useEffect(() => {
    if (!realtimeQuote) return;
    const marketTime = dayjs(realtimeQuote.hs_time);
    const quoteTime = marketTime.isValid() ? marketTime : dayjs(realtimeQuote.updated_at);
    if (!quoteTime.isValid() || quoteTime.format('YYYY-MM-DD') !== dayjs().format('YYYY-MM-DD')) return;
    const close = toPositiveNumber(realtimeQuote.last_px);
    if (close === null) return;

    setRawKlines(previous => {
      const todayKey = dayjs().format('YYYY-MM-DD');
      const index = previous.findIndex(item => formatDateKey(item.timestamp) === todayKey);
      const existing = index >= 0 ? previous[index] : null;
      const open = toPositiveNumber(realtimeQuote.open_px) ?? existing?.open ?? close;
      const high = Math.max(toPositiveNumber(realtimeQuote.high_px) ?? existing?.high ?? close, open, close);
      const low = Math.min(toPositiveNumber(realtimeQuote.low_px) ?? existing?.low ?? close, open, close);
      const updated = {
        ...(existing || {}),
        timestamp: existing?.timestamp || `${todayKey}T15:00:00`,
        open,
        high,
        low,
        close,
        volume: toFiniteNumber(realtimeQuote.volume) ?? existing?.volume ?? 0,
        turnover: toFiniteNumber(realtimeQuote.amount) ?? existing?.turnover ?? 0,
        turnover_rate: existing?.turnover_rate ?? null,
      };
      if (index < 0) return [...previous, updated];
      const next = [...previous];
      next[index] = updated;
      return next;
    });
  }, [realtimeQuote]);

  useEffect(() => {
    if (!rawKlines.length) {
      setProcessedKlines([]);
      return;
    }

    const enriched = appendRollingPocSupportResistance(rawKlines, {
      window: supportResistanceWindow,
      binCount: 48,
      maxLevelsPerSide: 2,
      minPeriods: supportResistanceWindow,
      volumeStdDevMultiplier,
      enableTurnoverDecay,
    });
    const processed = appendNineTurnAtr(
      preprocessKlinesVolume(enriched, volumeStdDevMultiplier, VOLUME_LOOKBACK_DAYS)
    );
    setProcessedKlines(processed);
  }, [rawKlines, supportResistanceWindow, volumeStdDevMultiplier, enableTurnoverDecay]);

  useEffect(() => {
    if (typeof onKlinesChange === 'function') {
      onKlinesChange(processedKlines);
    }
  }, [processedKlines, onKlinesChange]);

  // 雪球副图只在这只股票真的被雪球组合持有过时才出现，整张图随之加高
  const xueqiuPoints = useMemo(
    () => alignXueqiuHistory(processedKlines.map(item => formatDateKey(item.timestamp)), xueqiuHistory),
    [processedKlines, xueqiuHistory],
  );
  const hasXueqiuPane = useMemo(() => xueqiuPoints.some(point => point && point.weight !== null), [xueqiuPoints]);
  const paneLayout = useMemo(
    () => buildKlinePaneLayout(height, hasXueqiuPane ? ['volume', 'macd', 'xueqiu'] : ['volume', 'macd']),
    [height, hasXueqiuPane],
  );

  const getChartOption = useCallback(() => {
    const dates = processedKlines.map(item => formatDateKey(item.timestamp));
    const latestChartDate = dates.length ? dayjs(dates[dates.length - 1]) : null;
    const defaultZoomStartDate = latestChartDate ? latestChartDate.subtract(6, 'month') : null;
    const defaultZoomStartIndex = defaultZoomStartDate
      ? Math.max(0, dates.findIndex(dateStr => dayjs(dateStr).valueOf() >= defaultZoomStartDate.valueOf()))
      : 0;
    const defaultZoomStartValue = dates[defaultZoomStartIndex] || dates[0];
    const defaultZoomEndValue = dates[dates.length - 1];
    const zoomRange = zoomRef.current
      ? { start: zoomRef.current.start, end: zoomRef.current.end }
      : { startValue: defaultZoomStartValue, endValue: defaultZoomEndValue };

    const {
      fairValueHi,
      fairValueLo,
      forwardNextFyHi,
      forwardNextFyLo,
      forwardNext2FyHi,
      forwardNext2FyLo,
      valuationByDate,
    } = buildValuationContext(dates, valuationHistory, valuationFillMode, valuationDateOffsetDays);

    const hasFairValueHi = hasSeriesData(fairValueHi);
    const hasFairValueLo = hasSeriesData(fairValueLo);
    const hasForwardNextFyHi = hasSeriesData(forwardNextFyHi);
    const hasForwardNextFyLo = hasSeriesData(forwardNextFyLo);
    const hasForwardNext2FyHi = hasSeriesData(forwardNext2FyHi);
    const hasForwardNext2FyLo = hasSeriesData(forwardNext2FyLo);

    const klineData = processedKlines.map((item) => {
      const isUp = item.close >= item.open;
      if (!Number.isFinite(item.volumeZScore)) {
        return {
          value: [item.open, item.close, item.low, item.high],
          itemStyle: {
            color: isUp ? '#ef232a' : '#14b143',
            color0: isUp ? '#ef232a' : '#14b143',
            borderColor: isUp ? '#ef232a' : '#14b143',
            borderColor0: isUp ? '#ef232a' : '#14b143'
          }
        };
      }
      let color;
      if (item.isVolumeSpike) {
        color = isUp ? '#8B0000' : '#006400';
      } else {
        color = isUp ? '#ef232a' : '#14b143';
      }
      return {
        value: [item.open, item.close, item.low, item.high],
        itemStyle: {
          color,
          color0: color,
          borderColor: color,
          borderColor0: color
        }
      };
    });

    const volumeData = processedKlines.map((item) => {
      const isUp = item.close >= item.open;
      if (!Number.isFinite(item.volumeZScore)) {
        return {
          value: item.volume,
          itemStyle: {
            color: isUp ? '#ef232a' : '#14b143'
          }
        };
      }
      let color;
      if (item.isVolumeSpike) {
        color = isUp ? '#8B0000' : '#006400';
      } else {
        color = isUp ? '#ef232a' : '#14b143';
      }
      return {
        value: item.volume,
        itemStyle: { color }
      };
    });

    const volumeBaseline = processedKlines.map(item => item.volumeMA);
    const ma20 = calculateCloseMovingAverage(processedKlines, 20);
    const risingTrendPoints = processedKlines
      .map((item, index) => item.highCount >= 2
        ? [index, item.low - (item.atr14 || item.low * 0.01) * 0.22]
        : null)
      .filter(Boolean);
    const fallingTrendPoints = processedKlines
      .map((item, index) => item.lowCount >= 2
        ? [index, item.high + (item.atr14 || item.high * 0.01) * 0.22]
        : null)
      .filter(Boolean);

    const series = [
      {
        name: 'K线',
        type: 'candlestick',
        data: klineData,
      },
      {
        name: 'MA20',
        type: 'line',
        data: ma20,
        symbol: 'none',
        smooth: false,
        connectNulls: false,
        lineStyle: { color: '#f5a623', width: 1.5 },
        tooltip: { show: false },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        itemStyle: { color: UP_COLOR },
        data: volumeData
      },
      {
        name: VOLUME_BASELINE_SERIES_NAME,
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumeBaseline,
        lineStyle: { color: '#FFA500', width: 1 },
        symbol: 'none'
      },
      {
        name: '连续走强',
        type: 'scatter',
        data: risingTrendPoints,
        symbol: 'circle',
        symbolSize: 5,
        itemStyle: { color: '#a80710' },
        tooltip: { show: false },
        z: 12,
      },
      {
        name: '连续走弱',
        type: 'scatter',
        data: fallingTrendPoints,
        symbol: 'circle',
        symbolSize: 5,
        itemStyle: { color: '#0a7a2e' },
        tooltip: { show: false },
        z: 12,
      }
    ];

    // MACD 副图：放在成交量下面，用第 3 个 grid。
    const {
      dif,
      dea,
      histogram,
      growing: macdGrowing,
      params: appliedMacdParams,
    } = calculateMacd(processedKlines, macdParams);
    const macdLabel = `MACD(${appliedMacdParams.fast},${appliedMacdParams.slow},${appliedMacdParams.signal})`;
    // 柱子的实心/空心跟随动能方向，而不只是正负：红色实心=正且在放大(多头动能增强)，
    // 红色空心=正但在收缩(多头动能衰减)，绿色同理。这是通达信/同花顺的画法，动能拐点
    // 一眼能看出来，只涂正负两色是看不出来的。
    const macdHistogramData = histogram.map((value, index) => {
      if (value === null) return null;
      const color = value >= 0 ? UP_COLOR : DOWN_COLOR;
      return {
        value,
        itemStyle: macdGrowing[index]
          ? { color }
          : { color: 'transparent', borderColor: color, borderWidth: 1 },
      };
    });

    series.push(
      {
        name: MACD_HISTOGRAM_SERIES_NAME,
        type: 'bar',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: macdHistogramData,
        itemStyle: { color: UP_COLOR },
        barWidth: '60%',
        tooltip: { show: false },
        markLine: {
          silent: true,
          symbol: 'none',
          label: { show: false },
          lineStyle: { color: '#bfbfbf', width: 1, type: 'solid' },
          data: [{ yAxis: 0 }],
        },
      },
      {
        name: 'DIF',
        type: 'line',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: dif,
        symbol: 'none',
        connectNulls: false,
        itemStyle: { color: DIF_COLOR },
        lineStyle: { color: DIF_COLOR, width: 1 },
        tooltip: { show: false },
      },
      {
        name: 'DEA',
        type: 'line',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: dea,
        symbol: 'none',
        connectNulls: false,
        itemStyle: { color: DEA_COLOR },
        lineStyle: { color: DEA_COLOR, width: 1 },
        tooltip: { show: false },
      },
    );

    // 雪球持仓副图：综合权重曲线 + 每个快照日的 5 日权价比方向（新/平/加/吸/抛/减）
    const xueqiuPaneIndex = paneLayout.indexOf('xueqiu');
    if (xueqiuPaneIndex >= 0) {
      series.push(
        {
          name: XUEQIU_WEIGHT_SERIES_NAME,
          type: 'line',
          xAxisIndex: xueqiuPaneIndex,
          yAxisIndex: xueqiuPaneIndex,
          data: xueqiuPoints.map(point => (point ? point.weight : null)),
          connectNulls: true,
          symbol: 'none',
          itemStyle: { color: XUEQIU_WEIGHT_COLOR },
          lineStyle: { color: XUEQIU_WEIGHT_COLOR, width: 1.5 },
          areaStyle: { color: XUEQIU_WEIGHT_COLOR, opacity: 0.08 },
          tooltip: { show: false },
        },
        {
          name: XUEQIU_DIRECTION_SERIES_NAME,
          type: 'custom',
          xAxisIndex: xueqiuPaneIndex,
          yAxisIndex: xueqiuPaneIndex,
          z: 10,
          itemStyle: { color: XUEQIU_DIRECTION_META['逆势吸筹'].color },
          tooltip: { show: false },
          data: xueqiuPoints
            .map((point, index) => (point && point.direction && point.weight !== null
              ? [index, point.weight, XUEQIU_DIRECTIONS.indexOf(point.direction), point.changed ? 1 : 0]
              : null))
            .filter(Boolean),
          encode: { x: 0, y: 1 },
          renderItem: (params, api) => {
            const meta = XUEQIU_DIRECTION_META[XUEQIU_DIRECTIONS[api.value(2)]];
            if (!meta) return null;
            const [x, y] = api.coord([api.value(0), api.value(1)]);
            // 缩得很小时每天写一个字会糊成一片：一根 K 线不到 14px 宽时，
            // 非转折日只画小圆点，方向转折的那天才写字
            if (api.size([1, 0])[0] < 14 && !api.value(3)) {
              return { type: 'circle', shape: { cx: x, cy: y, r: 2 }, style: { fill: meta.color } };
            }
            return {
              type: 'group',
              children: [
                { type: 'circle', shape: { cx: x, cy: y, r: 7 }, style: { fill: meta.color, stroke: '#fff', lineWidth: 1 } },
                {
                  type: 'text',
                  style: { text: meta.glyph, x, y, fill: '#fff', fontSize: 9, fontWeight: 'bold', align: 'center', verticalAlign: 'middle' },
                },
              ],
            };
          },
        },
      );
    }

    // 研报/财报标记：画在当天 K 线最高价上方，点击打开侧栏
    const eventMarkers = buildEventMarkers(dates, chartEvents.research_days, chartEvents.financial_reports);
    eventMarkersRef.current = { dates, ...eventMarkers };
    const highs = processedKlines.map(item => toFiniteNumber(item.high));
    const eventLegendNames = [];
    if (eventMarkers.financial.length) {
      eventLegendNames.push(FINANCIAL_SERIES_NAME);
      series.push(buildEventSeries(FINANCIAL_SERIES_NAME, eventMarkers.financial, highs, FINANCIAL_COLOR, '财'));
    }
    if (eventMarkers.research.length) {
      eventLegendNames.push(RESEARCH_SERIES_NAME);
      series.push(buildEventSeries(RESEARCH_SERIES_NAME, eventMarkers.research, highs, RESEARCH_COLOR, '研'));
    }

    const indicatorLegendNames = [];
    const getSegmentPrice = value => (value && typeof value === 'object' ? value.price : value);
    const hasLevelData = data => data.some(value => toPositiveNumber(getSegmentPrice(value)) !== null);
    const addRollingSegmentSeries = (name, data, color, lineWidth = 4) => {
      if (!hasLevelData(data)) return;
      indicatorLegendNames.push(name);
      series.push({
        name,
        type: 'custom',
        data: data
          .map((item, index) => {
            const positivePrice = toPositiveNumber(getSegmentPrice(item));
            if (positivePrice === null) return null;
            const itemLineWidth = Number(item?.lineWidth);
            const resolvedLineWidth = Number.isFinite(itemLineWidth) && itemLineWidth > 0
              ? itemLineWidth
              : lineWidth;
            return [index, positivePrice, resolvedLineWidth];
          })
          .filter(Boolean),
        encode: { x: 0, y: 1 },
        renderItem: (params, api) => {
          const point = api.coord([api.value(0), api.value(1)]);
          const dayWidth = Math.max(2, api.size([1, 0])[0] * 0.88);
          const segmentLineWidth = Number(api.value(2));
          return {
            type: 'line',
            shape: {
              x1: point[0] - dayWidth / 2,
              y1: point[1],
              x2: point[0] + dayWidth / 2,
              y2: point[1],
            },
            style: {
              stroke: color,
              lineWidth: Number.isFinite(segmentLineWidth) && segmentLineWidth > 0 ? segmentLineWidth : lineWidth,
              lineCap: 'butt',
              opacity: 0.85,
            },
          };
        },
        z: 8,
        emphasis: {
          itemStyle: {
            opacity: 1,
          },
        }
      });
    };

    if (showSupportResistance) {
      const findLevel = (supportResistance, side, role) => {
        const levels = side === 'support'
          ? supportResistance?.supports || []
          : supportResistance?.resistances || [];
        return levels.find(level => Array.isArray(level.roles) && level.roles.includes(role));
      };
      const getStrongestLineWidth = (supportResistance, side) => {
        const support = findLevel(supportResistance, 'support', 'strongest');
        const resistance = findLevel(supportResistance, 'resistance', 'strongest');
        if (!support && !resistance) return 2;
        if (!support) return side === 'resistance' ? 3 : 2;
        if (!resistance) return side === 'support' ? 3 : 2;

        const supportVolume = Number(support.volume);
        const resistanceVolume = Number(resistance.volume);
        if (supportVolume > resistanceVolume) return side === 'support' ? 3 : 2;
        if (resistanceVolume > supportVolume) return side === 'resistance' ? 3 : 2;

        const supportDistance = Number(support.distance_pct);
        const resistanceDistance = Number(resistance.distance_pct);
        if (Number.isFinite(supportDistance) && Number.isFinite(resistanceDistance)) {
          if (supportDistance < resistanceDistance) return side === 'support' ? 3 : 2;
          if (resistanceDistance < supportDistance) return side === 'resistance' ? 3 : 2;
        }

        return 2;
      };
      const getSelectedLevelData = (side, role, baseLineWidth) => processedKlines.map(item => {
        const supportResistance = item.support_resistance;
        const level = findLevel(supportResistance, side, role);
        const price = toPositiveNumber(level?.price);
        if (price === null) return null;
        if (role === 'nearest') {
          const strongest = findLevel(supportResistance, side, 'strongest');
          if (strongest && toPositiveNumber(strongest.price) === price) return null;
        }
        const lineWidth = role === 'strongest'
          ? getStrongestLineWidth(supportResistance, side)
          : baseLineWidth;
        return { price, lineWidth };
      });

      [
        { name: '最强支撑', side: 'support', role: 'strongest', color: '#00a854', lineWidth: 2 },
        { name: '最近支撑', side: 'support', role: 'nearest', color: '#52c41a', lineWidth: 1 },
        { name: '最强压力', side: 'resistance', role: 'strongest', color: '#f5222d', lineWidth: 2 },
        { name: '最近压力', side: 'resistance', role: 'nearest', color: '#ff7875', lineWidth: 1 },
      ].forEach(config => {
        addRollingSegmentSeries(
          config.name,
          getSelectedLevelData(config.side, config.role, config.lineWidth),
          config.color,
          config.lineWidth
        );
      });
    }

    if (hasFairValueHi) {
      series.push({
        name: '估值上限',
        type: 'line',
        data: fairValueHi,
        lineStyle: { color: '#FF0000', width: 2 },
        symbol: 'none',
        connectNulls: true
      });
    }
    if (hasFairValueLo) {
      series.push({
        name: '估值下限',
        type: 'line',
        data: fairValueLo,
        lineStyle: { color: '#0066FF', width: 2 },
        symbol: 'none',
        connectNulls: true
      });
    }
    if (hasForwardNextFyHi) {
      series.push({
        name: '下财年估值上限',
        type: 'line',
        data: forwardNextFyHi,
        lineStyle: { color: '#FFA6A6', width: 2 },
        symbol: 'none',
        connectNulls: true
      });
    }
    if (hasForwardNextFyLo) {
      series.push({
        name: '下财年估值下限',
        type: 'line',
        data: forwardNextFyLo,
        lineStyle: { color: '#66CCFF', width: 2 },
        symbol: 'none',
        connectNulls: true
      });
    }
    if (hasForwardNext2FyHi) {
      series.push({
        name: '下下财年估值上限',
        type: 'line',
        data: forwardNext2FyHi,
        lineStyle: { color: '#FF9C6E', width: 2, type: 'dashed' },
        symbol: 'none',
        connectNulls: true
      });
    }
    if (hasForwardNext2FyLo) {
      series.push({
        name: '下下财年估值下限',
        type: 'line',
        data: forwardNext2FyLo,
        lineStyle: { color: '#5CDBD3', width: 2, type: 'dashed' },
        symbol: 'none',
        connectNulls: true
      });
    }

    return {
      animation: false,
      tooltip: {
        trigger: 'item',
        triggerOn: 'click',
        alwaysShowContent: false,
        enterable: true,
        formatter: function(params) {
          if (params?.seriesName !== 'K线') return '';
          const fmtPrice = (value) => (Number.isFinite(value) ? value.toFixed(2) : '--');
          const dataIndex = params?.dataIndex;
          const date = dates[dataIndex] || params?.name;
          const rawKline = dataIndex !== undefined ? processedKlines[dataIndex] : null;
          const open = rawKline?.open;
          const close = rawKline?.close;
          const high = rawKline?.high;
          const low = rawKline?.low;
          const isUp = Number.isFinite(open) && Number.isFinite(close) ? close >= open : null;
          const directionLabel = isUp === null ? '--' : (isUp ? '阳线' : '阴线');
          const directionColor = isUp === null ? '#999' : (isUp ? '#ef232a' : '#14b143');
          let result = `<div style="font-weight: bold; margin-bottom: 8px;">${date}</div>`;
          if (rawKline) {
            result += `
              <div style="margin-bottom: 4px;">
                <span style="color: #666;">开盘：</span><span style="color: #ef232a;">${fmtPrice(open)}</span>
                <span style="color: #666; margin-left: 8px;">收盘：</span><span style="color: #ef232a;">${fmtPrice(close)}</span>
              </div>
              <div style="margin-bottom: 4px;">
                <span style="color: #666;">最高：</span><span style="color: #ef232a;">${fmtPrice(high)}</span>
                <span style="color: #666; margin-left: 8px;">最低：</span><span style="color: #ef232a;">${fmtPrice(low)}</span>
              </div>
              <div style="margin-bottom: 4px;">
                <span style="color: #666;">方向：</span><span style="color: ${directionColor};">${directionLabel}</span>
              </div>
            `;
          }
          if (dataIndex !== undefined && processedKlines[dataIndex]) {
            const currentKline = processedKlines[dataIndex];
            const volume = currentKline.volume;
            result += `
              <div style="margin-bottom: 4px;">
                <span style="color: #666;">成交量：</span><span style="color: #1890ff;">${formatNumber(volume, 0)}</span>
              </div>
            `;
            if (Number.isFinite(currentKline.volumeZScore)) {
              result += `
                <div style="margin-bottom: 4px;">
                  <span style="color: #666;">${VOLUME_LOOKBACK_DAYS}日几何均量：</span>${formatNumber(currentKline.volumeMA, 0)}
                  <span style="color:#999;margin-left:8px;">倍数 ${formatNumber(currentKline.volumeMultiple, 2)}</span>
                  <span style="color:#999;margin-left:8px;">logZ ${formatNumber(currentKline.volumeZScore, 2)}</span>
                </div>
              `;
            }
            if (Number.isFinite(currentKline.atr14)) {
              result += `
                <div style="margin-bottom: 4px;">
                  <span style="color: #666;">ATR14：</span>${fmtPrice(currentKline.atr14)}
                </div>
              `;
            }
            const difValue = dif[dataIndex];
            const deaValue = dea[dataIndex];
            const histogramValue = histogram[dataIndex];
            if (Number.isFinite(difValue) && Number.isFinite(deaValue)) {
              const histogramColor = histogramValue >= 0 ? UP_COLOR : DOWN_COLOR;
              result += `
                <div style="margin-bottom: 4px;">
                  <span style="color: #666;">${macdLabel}　</span>
                  <span style="color: ${DIF_COLOR};">DIF ${formatNumber(difValue, 3)}</span>
                  <span style="color: ${DEA_COLOR}; margin-left: 8px;">DEA ${formatNumber(deaValue, 3)}</span>
                  <span style="color: ${histogramColor}; margin-left: 8px;">柱 ${formatNumber(histogramValue, 3)}</span>
                </div>
              `;
            }
            const xueqiuPoint = xueqiuPoints[dataIndex];
            if (xueqiuPoint && xueqiuPoint.weight !== null) {
              const meta = XUEQIU_DIRECTION_META[xueqiuPoint.direction];
              const multiples = xueqiuPoint.weightMultiple !== null && xueqiuPoint.momentumMultiple !== null
                ? `（权×${formatNumber(xueqiuPoint.weightMultiple, 3)} / 价×${formatNumber(xueqiuPoint.momentumMultiple, 3)}）`
                : '';
              result += `
                <div style="margin-bottom: 4px;">
                  <span style="color: #666;">雪球综合权重：</span><span style="color: ${XUEQIU_WEIGHT_COLOR};">${formatNumber(xueqiuPoint.weight, 2)}%</span>
                  ${xueqiuPoint.ratio !== null ? `<span style="color:#999;margin-left:8px;">5日权价比 ${formatNumber(xueqiuPoint.ratio, 2)}${multiples}</span>` : ''}
                  ${meta ? `<span style="color:${meta.color};margin-left:8px;font-weight:bold;">${xueqiuPoint.direction}</span>` : ''}
                </div>
              `;
            }
            if ([2, 3, 4].includes(currentKline.lowCount) && Number.isFinite(currentKline.latestRisingClose)) {
              result += `
                <div style="margin-bottom: 4px;">
                  <span style="color:#14b143;">低${currentKline.lowCount}：</span>
                  最近红点（高${currentKline.latestRisingCount}）收盘 ${fmtPrice(currentKline.latestRisingClose)}
                </div>
                <div style="margin-bottom: 4px;">
                  <span style="color:#666;">相对该红点回撤：</span>${formatNumber(currentKline.risingDrawdownPct, 2)}%
                  <span style="color:#999;margin-left:8px;">${formatNumber(currentKline.risingDrawdownAtr, 2)} ATR</span>
                </div>
              `;
            }
            const supportResistance = processedKlines[dataIndex].support_resistance;
            if (showSupportResistance && supportResistance) {
              const roleLabelMap = { strongest: '最强', nearest: '最近' };
              const appendLevels = (levels, color, label) => {
                (levels || []).forEach(level => {
                  const price = toPositiveNumber(level.price);
                  if (price === null) return;
                  const roles = (level.roles || [])
                    .map(role => roleLabelMap[role] || role)
                    .join('/');
                  const levelLabel = roles ? `${roles}${label}` : label;
                  result += `
                    <div style="margin-bottom: 4px;">
                      <span style="color:${color};">${levelLabel}：</span>${fmtPrice(price)}
                      <span style="color:#999;margin-left:8px;">覆盖量 ${formatNumber(level.volume, 0)}</span>
                      <span style="color:#999;margin-left:8px;">覆盖Z ${formatNumber(level.volume_zscore, 2)}</span>
                    </div>
                  `;
                });
              };
              appendLevels(supportResistance.supports, '#00a854', '支撑');
              appendLevels(supportResistance.resistances, '#f5222d', '压力');
            }
          }
          const valuation = valuationByDate[date];
          if (valuation) {
            result += `
              <div style="margin-bottom: 4px;">
                <span style="color:#FF0000;">估值上限：</span>${fmtPrice(toFiniteNumber(valuation.fair_value_hi))}
                <span style="color:#0066FF;margin-left:10px;">估值下限：</span>${fmtPrice(toFiniteNumber(valuation.fair_value_lo))}
              </div>
              <div style="margin-bottom: 4px;">
                <span style="color:#FFA6A6;">下财年上限：</span>${fmtPrice(toFiniteNumber(valuation.forward_next_fy_hi))}
                <span style="color:#66CCFF;margin-left:10px;">下财年下限：</span>${fmtPrice(toFiniteNumber(valuation.forward_next_fy_lo))}
              </div>
              ${valuation.forward_next2_fy_hi !== undefined ? `
              <div style="margin-bottom: 4px;">
                <span style="color:#FF9C6E;">下下财年上限：</span>${fmtPrice(toFiniteNumber(valuation.forward_next2_fy_hi))}
                <span style="color:#5CDBD3;margin-left:10px;">下下财年下限：</span>${fmtPrice(toFiniteNumber(valuation.forward_next2_fy_lo))}
              </div>` : ''}
            `;
          }
          return result;
        }
      },
      legend: [{
        type: 'scroll',
        left: '10%',
        right: '8%',
        data: [
          'K线',
          'MA20',
          '连续走强',
          '连续走弱',
          ...eventLegendNames,
          ...indicatorLegendNames,
          ...(hasFairValueHi ? ['估值上限'] : []),
          ...(hasFairValueLo ? ['估值下限'] : []),
          ...(hasForwardNextFyHi ? ['下财年估值上限'] : []),
          ...(hasForwardNextFyLo ? ['下财年估值下限'] : []),
          ...(hasForwardNext2FyHi ? ['下下财年估值上限'] : []),
          ...(hasForwardNext2FyLo ? ['下下财年估值下限'] : []),
        ],
        selected: {
          '最近支撑': false,
          '最近压力': false,
          // 估值线默认不画：主图上一共六条估值线，全开会把 K 线本身淹没，需要时再点开
          '估值上限': false,
          '估值下限': false,
          '下财年估值上限': false,
          '下财年估值下限': false,
          '下下财年估值上限': false,
          '下下财年估值下限': false,
        }
      }, {
        ...SUB_LEGEND_STYLE,
        top: paneLayout.legendTops.volume,
        data: ['成交量', VOLUME_BASELINE_SERIES_NAME],
      }, {
        ...SUB_LEGEND_STYLE,
        top: paneLayout.legendTops.macd,
        data: [MACD_HISTOGRAM_SERIES_NAME, 'DIF', 'DEA'],
        formatter: name => (name === MACD_HISTOGRAM_SERIES_NAME ? `${macdLabel} 柱` : name),
      }, ...(xueqiuPaneIndex >= 0 ? [{
        ...SUB_LEGEND_STYLE,
        top: paneLayout.legendTops.xueqiu,
        data: [XUEQIU_WEIGHT_SERIES_NAME, XUEQIU_DIRECTION_SERIES_NAME],
        formatter: name => (name === XUEQIU_DIRECTION_SERIES_NAME
          ? `${name}（新进/持平/顺势加仓/逆势吸筹/借涨减仓/减仓）`
          : name),
      }] : [])],
      // 窗格上下叠放：主图 / 成交量 / MACD / (雪球持仓)，像素位置由 buildKlinePaneLayout 统一给出。
      // 日期标签只留在最下面那个窗格上，中间几处重复的日期轴纯属噪声。
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: paneLayout.grids.map(grid => ({ left: '10%', right: '8%', top: grid.top, height: grid.height })),
      xAxis: paneLayout.grids.map((grid, index) => {
        const isLast = index === paneLayout.grids.length - 1;
        return {
          type: 'category',
          gridIndex: index,
          data: dates,
          scale: true,
          boundaryGap: false,
          axisLine: { onZero: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: isLast },
          ...(index === 0 ? { min: 'dataMin', max: 'dataMax' } : {}),
          ...(index === 0 || isLast ? { splitNumber: 20 } : {}),
        };
      }),
      yAxis: [
        {
          scale: true,
          // 有研报/财报标记时给顶部留白：标记画在最高价上方(叠两层时约 50px)，
          // 不留白的话窗口里最高那根 K 线上的标记会顶出主图、压到图例上
          boundaryGap: ['3%', eventLegendNames.length ? '20%' : '3%'],
          splitArea: { show: true }
        },
        {
          scale: true,
          gridIndex: 1,
          splitNumber: 2,
          axisLabel: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false }
        },
        {
          scale: true,
          gridIndex: 2,
          splitNumber: 3,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: true, lineStyle: { color: '#f0f0f0' } },
          axisLabel: { fontSize: 10, formatter: value => formatNumber(value, 2) }
        },
        ...(xueqiuPaneIndex >= 0 ? [{
          scale: true,
          gridIndex: xueqiuPaneIndex,
          splitNumber: 2,
          // 上下各留 15%：方向标记半径 7px，贴着窗格边缘的点会被切掉一半
          boundaryGap: ['15%', '15%'],
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: true, lineStyle: { color: '#f0f0f0' } },
          axisLabel: { fontSize: 10, formatter: value => `${formatNumber(value, 2)}%` }
        }] : []),
      ],
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: paneLayout.grids.map((_, index) => index),
          ...zoomRange,
        },
        {
          show: true,
          xAxisIndex: paneLayout.grids.map((_, index) => index),
          type: 'slider',
          top: paneLayout.sliderTop,
          height: paneLayout.sliderHeight,
          ...zoomRange,
        }
      ],
      series
    };
  }, [
    chartEvents,
    macdParams,
    paneLayout,
    xueqiuPoints,
    processedKlines,
    showSupportResistance,
    valuationDateOffsetDays,
    valuationFillMode,
    valuationHistory,
  ]);

  useEffect(() => {
    if (processedKlines.length > 0) {
      setChartOption(getChartOption());
    } else {
      setChartOption({});
    }
  }, [getChartOption, processedKlines.length]);

  const handleChartReady = useCallback((chart) => {
    const renderer = chart.getZr();
    renderer.on('click', event => {
      if (!event.target) chart.dispatchAction({ type: 'hideTip' });
    });
    chart.on('click', params => {
      if (params?.seriesName !== 'K线') {
        chart.dispatchAction({ type: 'hideTip' });
      }
      if (params?.seriesName === RESEARCH_SERIES_NAME || params?.seriesName === FINANCIAL_SERIES_NAME) {
        const { dates, research, financial } = eventMarkersRef.current;
        const kind = params.seriesName === RESEARCH_SERIES_NAME ? 'research' : 'financial';
        const marker = (kind === 'research' ? research : financial)[params.dataIndex];
        if (marker) setActiveEvent({ kind, tradeDate: dates[marker.index], items: marker.items });
      }
    });
    chart.on('datazoom', () => {
      const zoom = chart.getOption()?.dataZoom?.[0];
      if (zoom && Number.isFinite(zoom.start) && Number.isFinite(zoom.end)) {
        zoomRef.current = { start: zoom.start, end: zoom.end };
      }
    });
  }, []);

  return (
    <div>
      <Form layout="inline" style={{ marginBottom: 16 }}>
        <Form.Item
          label={renderMetricTitle(
            '支持压力位窗口(K线数)',
            <>
              <div>每一天只用它之前的 N 根 K 线计算，不包含当天 K 线。</div>
              <div style={{ marginTop: 6 }}>
                筹码分布粒度固定为 48 个价格桶：<code>桶宽 = (窗口最高价 - 窗口最低价) / 48</code>。
              </div>
              <div style={{ marginTop: 6 }}>
                单根 K 线成交量先按价格桶重叠比例分配；收盘价所在桶使用 10 倍权重，剩余成交量均分到其他覆盖桶。
              </div>
            </>
          )}
        >
          <Segmented
            options={POC_WINDOW_OPTIONS}
            value={supportResistanceWindow}
            onChange={value => setSupportResistanceWindow(value)}
          />
        </Form.Item>
        <Form.Item
          label={renderMetricTitle(
            '成交量Z阈值',
            <>
              <div>
                K 线放量使用：<code>z = (log10(当天成交量) - 过去{VOLUME_LOOKBACK_DAYS}日log均值) / 过去{VOLUME_LOOKBACK_DAYS}日log标准差</code>，不包含当天。
              </div>
              <div style={{ marginTop: 6 }}>
                当 <code>z &gt; 阈值</code> 时，K 线和成交量柱会标记为放量。
              </div>
              <div style={{ marginTop: 6 }}>
                支撑压力线的价格桶覆盖量也使用这个阈值筛选高覆盖量价格位。
              </div>
            </>
          )}
        >
          <InputNumber
            min={0}
            max={100}
            step={0.1}
            value={volumeStdDevMultiplier}
            onChange={value => setVolumeStdDevMultiplier(value)}
          />
        </Form.Item>
        <Form.Item label="显示支撑压力线">
          <Switch
            checked={showSupportResistance}
            onChange={setShowSupportResistance}
          />
        </Form.Item>
        <Form.Item
          label={renderMetricTitle(
            'MACD(快/慢/信号)',
            <>
              <div>
                <code>DIF = EMA(收盘,快) − EMA(收盘,慢)</code>，
                <code>DEA = EMA(DIF,信号)</code>，
                <code>MACD柱 = (DIF − DEA) × 2</code>（A股口径，乘2）。
              </div>
              <div style={{ marginTop: 6 }}>
                EMA 用首个收盘价递推播种，与通达信/同花顺一致，便于和券商软件对数；
                前 max(快,慢,信号)−1 根带初始化偏差，不画。
              </div>
              <div style={{ marginTop: 6 }}>
                柱子<strong>实心</strong>表示动能在放大，<strong>空心</strong>表示动能在收缩——
                空心红柱意味着还在多头区间但势头已经在衰减。
              </div>
            </>
          )}
        >
          <Space.Compact>
            <InputNumber
              min={1}
              max={200}
              style={{ width: 64 }}
              value={macdParams.fast}
              onChange={value => setMacdParams(current => ({ ...current, fast: value }))}
            />
            <InputNumber
              min={1}
              max={400}
              style={{ width: 64 }}
              value={macdParams.slow}
              onChange={value => setMacdParams(current => ({ ...current, slow: value }))}
            />
            <InputNumber
              min={1}
              max={200}
              style={{ width: 64 }}
              value={macdParams.signal}
              onChange={value => setMacdParams(current => ({ ...current, signal: value }))}
            />
          </Space.Compact>
        </Form.Item>
        <Form.Item
          label={renderMetricTitle(
            '换手衰减',
            <>
              <div>开启后，每根历史 K 线会先用当天换手率衰减已有筹码，再加入当天新成交量。</div>
              <div style={{ marginTop: 6 }}>
                换手率由后端按 <code>成交量 / 流通股本</code> 计算；没有流通股本数据时，该 K 线不会触发衰减。
              </div>
            </>
          )}
        >
          <Switch
            checked={enableTurnoverDecay}
            onChange={setEnableTurnoverDecay}
          />
        </Form.Item>
      </Form>
      {loading ? (
        <Spin size="large" />
      ) : (
        <ReactECharts
          key={`${symbol}-${supportResistanceWindow}-${volumeStdDevMultiplier}-${showSupportResistance}-${enableTurnoverDecay}-${valuationHistory.length}-${valuationDateOffsetDays}-${macdParams.fast}-${macdParams.slow}-${macdParams.signal}-${hasXueqiuPane}`}
          option={chartOption}
          notMerge={false}
          onChartReady={handleChartReady}
          style={{ height: paneLayout.totalHeight }}
        />
      )}
      <KlineEventDrawer event={activeEvent} onClose={() => setActiveEvent(null)} />
    </div>
  );
};

export default StockKlineChart;
