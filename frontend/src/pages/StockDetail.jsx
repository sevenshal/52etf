import React, { useState, useEffect, useMemo } from 'react';
import { Card, Spin, Button, InputNumber, Form, Switch, Descriptions, Table, Tag, Popover } from 'antd';
import { LeftOutlined, InfoCircleOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { useParams, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';
import { formatNumber } from '../utils/format';
import { calculateSupportResistanceValuesNew, preprocessKlinesVolume } from '../utils/klines';
import { computeStockWindowMetrics, STOCK_METRIC_WINDOWS } from '../utils/stockMetrics';

const formatPercent = (value, digits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Number(value).toFixed(digits)}%`;
};

const formatSignedPercent = (value, digits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  const num = Number(value);
  return `${num >= 0 ? '+' : ''}${num.toFixed(digits)}%`;
};

const renderPercentileTag = (value, higherIsBetter = true) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '-';
  const num = Number(value);
  const color = higherIsBetter
    ? (num >= 80 ? 'green' : num >= 50 ? 'gold' : 'red')
    : (num >= 80 ? 'red' : num >= 50 ? 'gold' : 'green');
  return <Tag color={color}>{`${num.toFixed(1)}%`}</Tag>;
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

const StockDetail = () => {
  const { symbol } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [klines, setKlines] = useState([]);
  const [processedKlines, setProcessedKlines] = useState([]);
  const [supportLevels, setSupportLevels] = useState([]);
  const [resistanceLevels, setResistanceLevels] = useState([]);
  const [days, setDays] = useState(200);
  const [priceChangeRatio, setPriceChangeRatio] = useState(30);
  const [stabilizationPeriod, setStabilizationPeriod] = useState(10);
  const [volumeStdDevMultiplier, setVolumeStdDevMultiplier] = useState(2);
  const [chartOption, setChartOption] = useState({});
  const [buyPoints, setBuyPoints] = useState([]);
  const [sellPoints, setSellPoints] = useState([]);
  const [evcHistory, setEvcHistory] = useState([]);
  const [showSupportResistance, setShowSupportResistance] = useState(false);
  const stockMetrics = useMemo(() => computeStockWindowMetrics(klines, STOCK_METRIC_WINDOWS), [klines]);
  const latestSnapshot = stockMetrics.latest;
  const latestEvcSnapshot = useMemo(() => {
    if (!evcHistory.length) return null;
    return [...evcHistory].sort((a, b) => new Date(b.date) - new Date(a.date))[0] || null;
  }, [evcHistory]);
  const metricRows = stockMetrics.rows;
  const metricColumns = useMemo(() => ([
    {
      title: '窗口',
      dataIndex: 'window',
      width: 90,
      render: value => `${value}日`,
    },
    {
      title: '年化波动',
      dataIndex: 'annualizedVolatility',
      width: 110,
      render: value => formatPercent(value, 2),
    },
    {
      title: '夏普',
      dataIndex: 'sharpeRatio',
      width: 90,
      render: value => formatNumber(value, 2),
    },
    {
      title: 'ATR',
      dataIndex: 'atr',
      width: 100,
      render: value => formatNumber(value, 2),
    },
    {
      title: 'ATRP',
      dataIndex: 'atrp',
      width: 110,
      render: value => formatPercent(value, 2),
    },
    {
      title: renderMetricTitle(
        '回撤深度分位',
        <>
          <div>先算窗口内每一天的回撤深度：当前收盘相对窗口内历史最高收盘回撤了多少。</div>
          <div style={{ marginTop: 6 }}>
            公式：<code>DD_t = 1 - Close_t / Peak_t</code>，其中 <code>Peak_t</code> 是截至当天的窗口内最高收盘。
          </div>
          <div style={{ marginTop: 6 }}>再看“当前回撤深度”在过去同窗口回撤深度序列中的百分位。数值越高，表示当前回撤越深。</div>
        </>
      ),
      dataIndex: 'drawdownPercentile',
      width: 130,
      render: value => renderPercentileTag(value, false),
    },
    {
      title: renderMetricTitle(
        '风险调整动量',
        <>
          <div>用最近 N 日收盘价做 <code>log</code> 线性回归。</div>
          <div style={{ marginTop: 6 }}>先取斜率年化，再乘 <code>R²</code> 得到原始动量分数。</div>
          <div style={{ marginTop: 6 }}>再除以年化波动率，得到风险调整动量。数值越高，说明趋势越强且更稳定。</div>
        </>
      ),
      dataIndex: 'riskAdjustedMomentum',
      width: 120,
      render: value => formatNumber(value, 2),
    },
    {
      title: renderMetricTitle(
        '动量分位',
        <>
          <div>把当前“风险调整动量”放进过去同窗口的滚动动量分布里，看它处在什么位置。</div>
          <div style={{ marginTop: 6 }}>分位越高，表示当前动量比历史上更多时候都要强。</div>
        </>
      ),
      dataIndex: 'momentumPercentile',
      width: 110,
      render: value => renderPercentileTag(value, true),
    },
  ]), []);

  useEffect(() => {
    fetchKlines();
    fetchEvcHistory();
  }, [symbol]);

  useEffect(() => {
    if (klines.length > 0) {
      const processed = preprocessKlinesVolume(klines, volumeStdDevMultiplier);
      setProcessedKlines(processed);
    }
  }, [klines, volumeStdDevMultiplier]);

  useEffect(() => {
    if (processedKlines.length > 0 && days > 1 && priceChangeRatio > 0 && stabilizationPeriod >= 1) {
      if (showSupportResistance) {
        const { supports, resistances } = calculateSupportResistanceValuesNew(processedKlines, days);
        setSupportLevels(supports);
        setResistanceLevels(resistances);
      } else {
        setSupportLevels([]);
        setResistanceLevels([]);
      }
      calculateBuySellPoints(processedKlines);
    }
  }, [processedKlines, days, priceChangeRatio, stabilizationPeriod, volumeStdDevMultiplier, showSupportResistance]);

  useEffect(() => {
    if (processedKlines.length > 0) {
      setChartOption(getChartOption());
    }
  }, [processedKlines, supportLevels, resistanceLevels, buyPoints, sellPoints, volumeStdDevMultiplier, evcHistory, showSupportResistance]);

  const fetchKlines = async () => {
    setLoading(true);
    try {
      const { data } = await request.get(`/api/stock/klines/${symbol}?days=500`);
      const normalized = [...(data || [])].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
      setKlines(normalized);
      const processed = preprocessKlinesVolume(normalized, volumeStdDevMultiplier);
      setProcessedKlines(processed);
    } catch (error) {
      console.error('获取K线数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const fetchEvcHistory = async () => {
    try {
      const { data } = await request.get(`/api/evc/stock-evc/history/${symbol}?limit=365`);
      setEvcHistory(data || []);
    } catch (error) {
      console.error('获取估值历史失败:', error);
    }
  };

  const calculateBuySellPoints = (klines) => {
    const newBuyPoints = [];
    const newSellPoints = [];

    let highestPoint = { price: 0, index: -1 };
    for (let i = 0; i < klines.length; i++) {
      if (klines[i].high > highestPoint.price) {
        highestPoint = { price: klines[i].high, index: i };
      }
    }

    if (highestPoint.index !== -1) {
      let lowestPointAfterHigh = { price: Infinity, index: -1 };
      for (let i = highestPoint.index + 1; i < klines.length; i++) {
        if (klines[i].low < lowestPointAfterHigh.price) {
          lowestPointAfterHigh = { price: klines[i].low, index: i };
        }
      }

      if (lowestPointAfterHigh.index !== -1 && highestPoint.price > 0 &&
        (highestPoint.price - lowestPointAfterHigh.price) / highestPoint.price > (priceChangeRatio / 100)) {
        if (klines.length - 1 - lowestPointAfterHigh.index > stabilizationPeriod) {
          for (let i = lowestPointAfterHigh.index + stabilizationPeriod; i < klines.length; i++) {
            if (i >= 19 && klines[i].isVolumeSpike) {
              newBuyPoints.push({
                index: i,
                price: klines[i].low
              });
              break;
            }
          }
        }
      }
    }

    let lowestPoint = { price: Infinity, index: -1 };
    for (let i = 0; i < klines.length; i++) {
      if (klines[i].low < lowestPoint.price) {
        lowestPoint = { price: klines[i].low, index: i };
      }
    }

    if (lowestPoint.index !== -1) {
      let highestPointAfterLow = { price: 0, index: -1 };
      for (let i = lowestPoint.index + 1; i < klines.length; i++) {
        if (klines[i].high > highestPointAfterLow.price) {
          highestPointAfterLow = { price: klines[i].high, index: i };
        }
      }

      if (highestPointAfterLow.index !== -1 && lowestPoint.price > 0 &&
        (highestPointAfterLow.price - lowestPoint.price) / lowestPoint.price > (priceChangeRatio / 100)) {
        if (klines.length - 1 - highestPointAfterLow.index > stabilizationPeriod) {
          for (let i = highestPointAfterLow.index + stabilizationPeriod; i < klines.length; i++) {
            if (i >= 19 && klines[i].isVolumeSpike) {
              newSellPoints.push({
                index: i,
                price: klines[i].high
              });
              break;
            }
          }
        }
      }
    }

    setBuyPoints(newBuyPoints);
    setSellPoints(newSellPoints);
  };

  const getChartOption = () => {
    const dates = processedKlines.map(item => dayjs(item.timestamp).format('YYYY-MM-DD'));

    // 估值线：和日期对齐
    const fairValueHi = [];
    const fairValueLo = [];
    const forwardNextFyHi = [];
    const forwardNextFyLo = [];

    if (evcHistory.length > 0) {
      const evcMap = {};
      evcHistory.forEach(item => {
        evcMap[dayjs(item.date).format('YYYY-MM-DD')] = item;
      });
      dates.forEach(dateStr => {
        const evc = evcMap[dateStr];
        fairValueHi.push(evc?.fair_value_hi ?? null);
        fairValueLo.push(evc?.fair_value_lo ?? null);
        forwardNextFyHi.push(evc?.forward_next_fy_hi ?? null);
        forwardNextFyLo.push(evc?.forward_next_fy_lo ?? null);
      });
    }

    const klineData = processedKlines.map((item, index) => {
      const isUp = item.close >= item.open;
      if (index < 19) {
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
          color: color,
          color0: color,
          borderColor: color,
          borderColor0: color
        }
      };
    });

    const volumeData = processedKlines.map((item, index) => {
      const isUp = item.close >= item.open;
      if (index < 19) {
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

    const volumeMA = processedKlines.map(item => item.volumeMA);

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
        name: '成交量N日均线',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumeMA,
        lineStyle: { color: '#FFA500', width: 1 },
        symbol: 'none'
      }
    ];

    // 支撑压力位（只在开关为true时显示）
    if (showSupportResistance) {
      supportLevels.forEach(level => {
        series.push({
          name: `支撑位${level}`,
          type: 'line',
          data: Array(dates.length).fill(level),
          lineStyle: { color: '#00FF00', type: 'dashed' },
          symbol: 'none'
        });
      });
      resistanceLevels.forEach(level => {
        series.push({
          name: `压力位${level}`,
          type: 'line',
          data: Array(dates.length).fill(level),
          lineStyle: { color: '#FF0000', type: 'dashed' },
          symbol: 'none'
        });
      });
    }

    // 估值线
    if (evcHistory.length > 0) {
      series.push(
        {
          name: '估值上限',
          type: 'line',
          data: fairValueHi,
          lineStyle: { color: '#FF0000', width: 2 },
          symbol: 'none',
          connectNulls: true
        },
        {
          name: '估值下限',
          type: 'line',
          data: fairValueLo,
          lineStyle: { color: '#0066FF', width: 2 },
          symbol: 'none',
          connectNulls: true
        },
        {
          name: '下财年估值上限',
          type: 'line',
          data: forwardNextFyHi,
          lineStyle: { color: '#FFA6A6', width: 2 },
          symbol: 'none',
          connectNulls: true
        },
        {
          name: '下财年估值下限',
          type: 'line',
          data: forwardNextFyLo,
          lineStyle: { color: '#66CCFF', width: 2 },
          symbol: 'none',
          connectNulls: true
        }
      );
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
            const volume = processedKlines[dataIndex].volume;
            result += `
              <div style="margin-bottom: 4px;">
                <span style="color: #666;">成交量：</span><span style="color: #1890ff;">${volume.toLocaleString()}</span>
              </div>
            `;
          }
          // 估值信息
          if (evcHistory.length > 0 && dataIndex !== undefined && evcHistory.length > 0) {
            const evcMap = {};
            evcHistory.forEach(item => {
              evcMap[dayjs(item.date).format('YYYY-MM-DD')] = item;
            });
            const evc = evcMap[date];
            if (evc) {
              result += `
                <div style="margin-bottom: 4px;">
                  <span style="color:#FF0000;">估值上限：</span>${evc.fair_value_hi?.toFixed(2) || '--'}
                  <span style="color:#0066FF;margin-left:10px;">估值下限：</span>${evc.fair_value_lo?.toFixed(2) || '--'}
                </div>
                <div style="margin-bottom: 4px;">
                  <span style="color:#FFA6A6;">下财年上限：</span>${evc.forward_next_fy_hi?.toFixed(2) || '--'}
                  <span style="color:#66CCFF;margin-left:10px;">下财年下限：</span>${evc.forward_next_fy_lo?.toFixed(2) || '--'}
                </div>
              `;
            }
          }
          return result;
        }
      },
      legend: {
        data: [
          'K线',
          '成交量',
          '成交量N日均线',
          '买点',
          '卖点',
          ...(showSupportResistance ? supportLevels.map((v) => `支撑位${v}`) : []),
          ...(showSupportResistance ? resistanceLevels.map((v) => `压力位${v}`) : []),
          '估值上限',
          '估值下限',
          '下财年估值上限',
          '下财年估值下限'
        ]
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
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', top: '90%', start: 0, end: 100 }
      ],
      series: series
    };
  };

  if (loading) {
    return <Spin size="large" />;
  }

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
            <span>{symbol} 股票详情</span>
          </div>
        }
      >
        <div style={{ marginBottom: 16 }}>
          <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 4 }}>
            <Descriptions.Item label="最新日期">
              {latestSnapshot ? dayjs(latestSnapshot.date).format('YYYY-MM-DD') : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="最新收盘">
              {formatNumber(latestSnapshot?.close, 2)}
            </Descriptions.Item>
            <Descriptions.Item label="日涨跌幅">
              {latestSnapshot?.changePct === null || latestSnapshot?.changePct === undefined ? '-' : (
                <Tag color={latestSnapshot.changePct > 0 ? 'red' : latestSnapshot.changePct < 0 ? 'green' : undefined}>
                  {formatSignedPercent(latestSnapshot.changePct, 2)}
                </Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="成交量">
              {formatNumber(latestSnapshot?.volume, 0)}
            </Descriptions.Item>
            <Descriptions.Item label="成交额">
              {formatNumber(latestSnapshot?.turnover, 0)}
            </Descriptions.Item>
            <Descriptions.Item label="PE">
              {formatNumber(latestEvcSnapshot?.pe_ratio, 2)}
            </Descriptions.Item>
            <Descriptions.Item label="Forward PE">
              {formatNumber(latestEvcSnapshot?.forward_pe_ratio, 2)}
            </Descriptions.Item>
            <Descriptions.Item label="样本数">
              {latestSnapshot?.sampleSize || klines.length}
            </Descriptions.Item>
          </Descriptions>
        </div>
        <div style={{ marginBottom: 8, fontWeight: 600 }}>常用窗口指标</div>
        <Table
          rowKey="window"
          size="small"
          pagination={false}
          columns={metricColumns}
          dataSource={metricRows}
          scroll={{ x: 900 }}
          style={{ marginBottom: 16 }}
        />
        <Form layout="inline" style={{ marginBottom: '16px' }}>
          <Form.Item label="天数">
            <InputNumber
              min={1}
              max={500}
              value={days}
              onChange={value => setDays(value)}
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
          <Form.Item label="成交量标准差倍数">
            <InputNumber
              min={0.1}
              max={5}
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
        <ReactECharts
          key={`${days}-${priceChangeRatio}-${stabilizationPeriod}-${volumeStdDevMultiplier}-${showSupportResistance}-${supportLevels.join(',')}-${resistanceLevels.join(',')}-${buyPoints.length}-${sellPoints.length}-${evcHistory.length}`}
          option={chartOption}
          style={{ height: '600px' }}
        />
      </Card>
    </div>
  );
};

export default StockDetail;
