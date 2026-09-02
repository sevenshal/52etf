import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Form, InputNumber, Popover, Segmented, Spin, Switch } from 'antd';
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

const POC_WINDOW_OPTIONS = [
  { label: '125', value: 125 },
  { label: '250', value: 250 },
  { label: '500', value: 500 },
];
const VOLUME_LOOKBACK_DAYS = 60;
const VOLUME_BASELINE_SERIES_NAME = `成交量${VOLUME_LOOKBACK_DAYS}日几何均线`;

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
    });
    return { fairValueHi, fairValueLo, forwardNextFyHi, forwardNextFyLo, valuationByDate };
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
    });
    return { fairValueHi, fairValueLo, forwardNextFyHi, forwardNextFyLo, valuationByDate };
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
  });
  return { fairValueHi, fairValueLo, forwardNextFyHi, forwardNextFyLo, valuationByDate };
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
  height = 600,
}) => {
  const [loading, setLoading] = useState(true);
  const [rawKlines, setRawKlines] = useState([]);
  const [processedKlines, setProcessedKlines] = useState([]);
  const [supportResistanceWindow, setSupportResistanceWindow] = useState(125);
  const [volumeStdDevMultiplier, setVolumeStdDevMultiplier] = useState(1);
  const [showSupportResistance, setShowSupportResistance] = useState(true);
  const [enableTurnoverDecay, setEnableTurnoverDecay] = useState(true);
  const [chartOption, setChartOption] = useState({});
  const zoomRef = useRef(null);

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
      valuationByDate,
    } = buildValuationContext(dates, valuationHistory, valuationFillMode, valuationDateOffsetDays);

    const hasFairValueHi = hasSeriesData(fairValueHi);
    const hasFairValueLo = hasSeriesData(fairValueLo);
    const hasForwardNextFyHi = hasSeriesData(forwardNextFyHi);
    const hasForwardNextFyLo = hasSeriesData(forwardNextFyLo);

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
        symbolSize: 7,
        itemStyle: { color: '#ef232a' },
        tooltip: { show: false },
        z: 12,
      },
      {
        name: '连续走弱',
        type: 'scatter',
        data: fallingTrendPoints,
        symbol: 'circle',
        symbolSize: 7,
        itemStyle: { color: '#14b143' },
        tooltip: { show: false },
        z: 12,
      }
    ];

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
            `;
          }
          return result;
        }
      },
      legend: {
        data: [
          'K线',
          'MA20',
          '成交量',
          VOLUME_BASELINE_SERIES_NAME,
          '连续走强',
          '连续走弱',
          ...indicatorLegendNames,
          ...(hasFairValueHi ? ['估值上限'] : []),
          ...(hasFairValueLo ? ['估值下限'] : []),
          ...(hasForwardNextFyHi ? ['下财年估值上限'] : []),
          ...(hasForwardNextFyLo ? ['下财年估值下限'] : []),
        ],
        selected: {
          '最近支撑': false,
          '最近压力': false,
          '下财年估值上限': false,
          '下财年估值下限': false,
        }
      },
      grid: [
        { left: '10%', right: '8%', height: '60%' },
        { left: '10%', right: '8%', top: '75%', height: '20%' }
      ],
      xAxis: [
        {
          type: 'category',
          data: dates,
          scale: true,
          boundaryGap: false,
          axisLine: { onZero: false },
          splitLine: { show: false },
          splitNumber: 20,
          min: 'dataMin',
          max: 'dataMax'
        },
        {
          type: 'category',
          gridIndex: 1,
          data: dates,
          scale: true,
          boundaryGap: false,
          axisLine: { onZero: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false }
        }
      ],
      yAxis: [
        {
          scale: true,
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
        }
      ],
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0, 1],
          ...zoomRange,
        },
        {
          show: true,
          xAxisIndex: [0, 1],
          type: 'slider',
          top: '90%',
          ...zoomRange,
        }
      ],
      series
    };
  }, [
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
          key={`${symbol}-${supportResistanceWindow}-${volumeStdDevMultiplier}-${showSupportResistance}-${enableTurnoverDecay}-${valuationHistory.length}-${valuationDateOffsetDays}`}
          option={chartOption}
          notMerge={false}
          onChartReady={handleChartReady}
          style={{ height }}
        />
      )}
    </div>
  );
};

export default StockKlineChart;
