import React, { useEffect, useState } from 'react';
import { Form, InputNumber, Popover, Segmented, Spin, Switch } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';
import { formatNumber } from '../utils/format';
import { appendRollingPocSupportResistance, preprocessKlinesVolume } from '../utils/klines';

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

const buildValuationContext = (dates, valuationHistory, fillMode) => {
  const fairValueHi = [];
  const fairValueLo = [];
  const forwardNextFyHi = [];
  const forwardNextFyLo = [];
  const valuationByDate = {};

  const sortedHistory = [...(valuationHistory || [])]
    .filter(item => item?.date)
    .map(item => ({
      ...item,
      dateKey: formatDateKey(item.date),
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
  valuationHistory = [],
  valuationFillMode = 'exact',
  onKlinesChange,
  height = 600,
}) => {
  const [loading, setLoading] = useState(true);
  const [rawKlines, setRawKlines] = useState([]);
  const [processedKlines, setProcessedKlines] = useState([]);
  const [supportResistanceWindow, setSupportResistanceWindow] = useState(125);
  const [priceChangeRatio, setPriceChangeRatio] = useState(30);
  const [stabilizationPeriod, setStabilizationPeriod] = useState(10);
  const [volumeStdDevMultiplier, setVolumeStdDevMultiplier] = useState(1);
  const [buyPoints, setBuyPoints] = useState([]);
  const [sellPoints, setSellPoints] = useState([]);
  const [showSupportResistance, setShowSupportResistance] = useState(true);
  const [chartOption, setChartOption] = useState({});

  useEffect(() => {
    fetchKlines();
  }, [symbol]);

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
    });
    const processed = preprocessKlinesVolume(enriched, volumeStdDevMultiplier, VOLUME_LOOKBACK_DAYS);
    setProcessedKlines(processed);
  }, [rawKlines, supportResistanceWindow, volumeStdDevMultiplier]);

  useEffect(() => {
    if (typeof onKlinesChange === 'function') {
      onKlinesChange(processedKlines);
    }
  }, [processedKlines, onKlinesChange]);

  useEffect(() => {
    if (processedKlines.length > 0 && priceChangeRatio > 0 && stabilizationPeriod >= 1) {
      calculateBuySellPoints(processedKlines);
    } else {
      setBuyPoints([]);
      setSellPoints([]);
    }
  }, [processedKlines, supportResistanceWindow, priceChangeRatio, stabilizationPeriod, volumeStdDevMultiplier]);

  useEffect(() => {
    if (processedKlines.length > 0) {
      setChartOption(getChartOption());
    } else {
      setChartOption({});
    }
  }, [
    processedKlines,
    buyPoints,
    sellPoints,
    volumeStdDevMultiplier,
    valuationHistory,
    valuationFillMode,
    showSupportResistance,
  ]);

  const fetchKlines = async () => {
    setLoading(true);
    setRawKlines([]);
    try {
      const { data } = await request.get(`/api/stock/klines/${symbol}`, {
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
  };

  const calculateBuySellPoints = (klines) => {
    const newBuyPoints = [];
    const newSellPoints = [];
    const buyPointIndexes = new Set();
    const sellPointIndexes = new Set();
    const signalWindow = Math.max(1, Number(supportResistanceWindow) || 1);
    const changeRatio = priceChangeRatio / 100;
    const stableBars = Math.max(1, Number(stabilizationPeriod) || 1);

    const findHighestPoint = (startIndex, endIndex) => {
      let highestPoint = { price: 0, index: -1 };
      for (let i = startIndex; i <= endIndex; i++) {
        if (klines[i].high > highestPoint.price) {
          highestPoint = { price: klines[i].high, index: i };
        }
      }
      return highestPoint;
    };

    const findLowestPoint = (startIndex, endIndex) => {
      let lowestPoint = { price: Infinity, index: -1 };
      for (let i = startIndex; i <= endIndex; i++) {
        if (klines[i].low < lowestPoint.price) {
          lowestPoint = { price: klines[i].low, index: i };
        }
      }
      return lowestPoint;
    };

    const addFirstVolumeSpikePoint = (startIndex, endIndex, points, pointIndexes, priceKey) => {
      for (let i = startIndex; i <= endIndex; i++) {
        if (klines[i].isVolumeSpike) {
          if (!pointIndexes.has(i)) {
            pointIndexes.add(i);
            points.push({
              index: i,
              price: klines[i][priceKey]
            });
          }
          break;
        }
      }
    };

    for (let windowEnd = 0; windowEnd < klines.length; windowEnd++) {
      const windowStart = Math.max(0, windowEnd - signalWindow + 1);

      const highestPoint = findHighestPoint(windowStart, windowEnd);
      if (highestPoint.index !== -1) {
        const lowestPointAfterHigh = findLowestPoint(highestPoint.index + 1, windowEnd);
        if (lowestPointAfterHigh.index !== -1 && highestPoint.price > 0 &&
          (highestPoint.price - lowestPointAfterHigh.price) / highestPoint.price > changeRatio) {
          const volumeSearchStart = lowestPointAfterHigh.index + stableBars;
          if (volumeSearchStart <= windowEnd) {
            addFirstVolumeSpikePoint(volumeSearchStart, windowEnd, newBuyPoints, buyPointIndexes, 'low');
          }
        }
      }

      const lowestPoint = findLowestPoint(windowStart, windowEnd);
      if (lowestPoint.index !== -1) {
        const highestPointAfterLow = findHighestPoint(lowestPoint.index + 1, windowEnd);
        if (highestPointAfterLow.index !== -1 && lowestPoint.price > 0 &&
          (highestPointAfterLow.price - lowestPoint.price) / lowestPoint.price > changeRatio) {
          const volumeSearchStart = highestPointAfterLow.index + stableBars;
          if (volumeSearchStart <= windowEnd) {
            addFirstVolumeSpikePoint(volumeSearchStart, windowEnd, newSellPoints, sellPointIndexes, 'high');
          }
        }
      }
    }

    setBuyPoints(newBuyPoints);
    setSellPoints(newSellPoints);
  };

  const getChartOption = () => {
    const dates = processedKlines.map(item => formatDateKey(item.timestamp));
    const latestChartDate = dates.length ? dayjs(dates[dates.length - 1]) : null;
    const defaultZoomStartDate = latestChartDate ? latestChartDate.subtract(6, 'month') : null;
    const defaultZoomStartIndex = defaultZoomStartDate
      ? Math.max(0, dates.findIndex(dateStr => dayjs(dateStr).valueOf() >= defaultZoomStartDate.valueOf()))
      : 0;
    const defaultZoomStartValue = dates[defaultZoomStartIndex] || dates[0];
    const defaultZoomEndValue = dates[dates.length - 1];

    const {
      fairValueHi,
      fairValueLo,
      forwardNextFyHi,
      forwardNextFyLo,
      valuationByDate,
    } = buildValuationContext(dates, valuationHistory, valuationFillMode);

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

    const buyPointMarkers = buyPoints.map(point => ({
      name: '买点',
      value: 'B',
      xAxis: point.index,
      yAxis: point.price,
      itemStyle: { color: 'red' }
    }));

    const sellPointMarkers = sellPoints.map(point => ({
      name: '卖点',
      value: 'S',
      xAxis: point.index,
      yAxis: point.price,
      itemStyle: { color: 'green' }
    }));

    const series = [
      {
        name: 'K线',
        type: 'candlestick',
        data: klineData,
        markPoint: {
          data: [...buyPointMarkers, ...sellPointMarkers],
          symbolSize: 30,
          label: {
            show: true, formatter: '{b}', color: '#fff', fontSize: 12
          },
          symbolOffset: [0, '-50%']
        }
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
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: function(params) {
          const fmtPrice = (value) => (Number.isFinite(value) ? value.toFixed(2) : '--');
          const date = params[0].axisValue;
          const klineParam = params.find(p => p.seriesType === 'candlestick' || p.seriesName === 'K线');
          const dataIndex = klineParam?.dataIndex ?? params[0]?.dataIndex;
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
          '成交量',
          VOLUME_BASELINE_SERIES_NAME,
          '买点',
          '卖点',
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
          startValue: defaultZoomStartValue,
          endValue: defaultZoomEndValue,
        },
        {
          show: true,
          xAxisIndex: [0, 1],
          type: 'slider',
          top: '90%',
          startValue: defaultZoomStartValue,
          endValue: defaultZoomEndValue,
        }
      ],
      series
    };
  };

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
            </>
          )}
        >
          <Segmented
            options={POC_WINDOW_OPTIONS}
            value={supportResistanceWindow}
            onChange={value => setSupportResistanceWindow(value)}
          />
        </Form.Item>
        <Form.Item label="涨跌幅(%)">
          <InputNumber
            min={1}
            max={100}
            step={1}
            value={priceChangeRatio}
            onChange={value => setPriceChangeRatio(value)}
          />
        </Form.Item>
        <Form.Item label="企稳时间(K线数)">
          <InputNumber
            min={1}
            max={100}
            step={1}
            value={stabilizationPeriod}
            onChange={value => setStabilizationPeriod(value)}
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
                当 <code>z &gt; 阈值</code> 时，K 线和成交量柱会标记为放量，并参与买卖点识别。
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
      </Form>
      {loading ? (
        <Spin size="large" />
      ) : (
        <ReactECharts
          key={`${symbol}-${supportResistanceWindow}-${priceChangeRatio}-${stabilizationPeriod}-${volumeStdDevMultiplier}-${showSupportResistance}-${buyPoints.length}-${sellPoints.length}-${valuationHistory.length}`}
          option={chartOption}
          notMerge={true}
          style={{ height }}
        />
      )}
    </div>
  );
};

export default StockKlineChart;
