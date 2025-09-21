import React, { useState, useEffect } from 'react';
import { Card, Spin, Button, InputNumber, Form } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { useParams, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';
import { calculateSupportResistanceValuesNew, preprocessKlinesVolume } from '../utils/klines';

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
  const [evcHistory, setEvcHistory] = useState([]); // 新增

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
      const { supports, resistances } = calculateSupportResistanceValuesNew(processedKlines, days);
      setSupportLevels(supports);
      setResistanceLevels(resistances);
      calculateBuySellPoints(processedKlines);
    }
  }, [processedKlines, days, priceChangeRatio, stabilizationPeriod, volumeStdDevMultiplier]);

  useEffect(() => {
    if (processedKlines.length > 0) {
      setChartOption(getChartOption());
    }
  }, [processedKlines, supportLevels, resistanceLevels, buyPoints, sellPoints, volumeStdDevMultiplier, evcHistory]);

  const fetchKlines = async () => {
    setLoading(true);
    try {
      const { data } = await request.get(`/api/stock/klines/${symbol}?days=500`);
      setKlines(data);
      const processed = preprocessKlinesVolume(data, volumeStdDevMultiplier);
      setProcessedKlines(processed);
    } catch (error) {
      console.error('获取K线数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  // fetch EVC 估值历史
  const fetchEvcHistory = async () => {
    try {
      const { data } = await request.get(`https://api.52etf.vip/api/evc/stock-evc/history/${symbol}?limit=365`);
      setEvcHistory(data || []);
    } catch (error) {
      console.error('获取估值历史失败:', error);
    }
  };

  // ... 其它不变的代码 ...

  const calculateBuySellPoints = (klines) => {
    // ... 原有实现不变 ...
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
    // ... K线、成交量、买卖点等原有代码 ...

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

    // series原有内容
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

    const series = [{
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
    }, {
      name: '成交量',
      type: 'bar',
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: volumeData
    }, {
      name: '成交量N日均线',
      type: 'line',
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: volumeMA,
      lineStyle: { color: '#FFA500', width: 1 },
      symbol: 'none'
    }];

    // 支撑压力位
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
          const date = params[0].axisValue;
          let result = `<div style="font-weight: bold; margin-bottom: 8px;">${date}</div>`;
          const klineData = params.find(p => p.seriesName === 'K线');
          if (klineData) {
            const [data, open, close, low, high] = klineData.data.value || klineData.data;
            result += `
              <div style="margin-bottom: 4px;">
                <span style="color: #666;">开盘：</span><span style="color: #ef232a;">${open.toFixed(2)}</span>
                <span style="color: #666; margin-left: 8px;">收盘：</span><span style="color: #ef232a;">${close.toFixed(2)}</span>
              </div>
              <div style="margin-bottom: 4px;">
                <span style="color: #666;">最高：</span><span style="color: #ef232a;">${high.toFixed(2)}</span>
                <span style="color: #666; margin-left: 8px;">最低：</span><span style="color: #ef232a;">${low.toFixed(2)}</span>
              </div>
            `;
          }
          const dataIndex = params[0].dataIndex;
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
          'K线', '成交量', '成交量20日均线', '买点', '卖点',
          ...supportLevels.map((v) => `支撑位${v}`),
          ...resistanceLevels.map((v) => `压力位${v}`),
          '估值上限', '估值下限', '下财年估值上限', '下财年估值下限'
        ]
      },
      grid: [{ left: '10%', right: '8%', height: '60%' }, { left: '10%', right: '8%', top: '75%', height: '20%' }],
      xAxis: [{ type: 'category', data: dates, scale: true, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false }, splitNumber: 20, min: 'dataMin', max: 'dataMax' },
        { type: 'category', gridIndex: 1, data: dates, scale: true, boundaryGap: false, axisLine: { onZero: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, splitNumber: 20, min: 'dataMin', max: 'dataMax' }],
      yAxis: [{ scale: true, splitArea: { show: true } }, { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } }],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }, { show: true, xAxisIndex: [0, 1], type: 'slider', top: '90%', start: 0, end: 100 }],
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
            <span>{symbol} K线图</span>
          </div>
        }
      >
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
        </Form>
        <ReactECharts
          key={`${days}-${priceChangeRatio}-${stabilizationPeriod}-${volumeStdDevMultiplier}-${supportLevels.join(',')}-${resistanceLevels.join(',')}-${buyPoints.length}-${sellPoints.length}-${evcHistory.length}`}
          option={chartOption}
          style={{ height: '600px' }}
        />
      </Card>
    </div>
  );
};

export default StockDetail;
