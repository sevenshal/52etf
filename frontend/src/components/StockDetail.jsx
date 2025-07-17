import React, { useState, useEffect } from 'react';
import { Card, Spin, Button, InputNumber, Form } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { useParams, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';
import { calculateSupportResistanceValues } from '../utils/klines';

const StockDetail = () => {
  const { symbol } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [klines, setKlines] = useState([]);
  const [supportLevels, setSupportLevels] = useState([]);
  const [resistanceLevels, setResistanceLevels] = useState([]);
  const [days, setDays] = useState(200);
  const [volumeRatio, setVolumeRatio] = useState(1.5);
  const [priceChangeRatio, setPriceChangeRatio] = useState(30);
  const [stabilizationPeriod, setStabilizationPeriod] = useState(10); // 新增：企稳时间 K线数量
  const [chartOption, setChartOption] = useState({});
  const [buyPoints, setBuyPoints] = useState([]);
  const [sellPoints, setSellPoints] = useState([]);

  useEffect(() => {
    fetchKlines();
  }, [symbol]);

  useEffect(() => {
    // 确保所有参数都有效才进行计算
    if (klines.length > 0 && volumeRatio > 1 && days > 1 && priceChangeRatio > 0 && stabilizationPeriod >= 1) {
      calculateSupportResistance(klines);
      calculateBuySellPoints(klines);
    }
  }, [days, volumeRatio, klines, priceChangeRatio, stabilizationPeriod]); // 依赖中添加 stabilizationPeriod

  useEffect(() => {
    setChartOption(getChartOption());
  }, [supportLevels, resistanceLevels, buyPoints, sellPoints]);

  const fetchKlines = async () => {
    setLoading(true);
    try {
      const { data } = await request.get(`/api/stock/klines/${symbol}?days=500`);
      setKlines(data);
    } catch (error) {
      console.error('获取K线数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const calculateSupportResistance = (klines) => {
    const { supports, resistances } = calculateSupportResistanceValues(klines, days, volumeRatio);
    setSupportLevels(supports);
    setResistanceLevels(resistances);
  };

  /**
   * 计算股票买卖点
   */
  const calculateBuySellPoints = (klines) => {
    const newBuyPoints = [];
    const newSellPoints = [];

    // 计算买点
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

      // 并且这个最低点和最高点的跌幅超过用户设定的百分比
      if (lowestPointAfterHigh.index !== -1 && highestPoint.price > 0 &&
          (highestPoint.price - lowestPointAfterHigh.price) / highestPoint.price > (priceChangeRatio / 100)) {

        // 从最低点之后到最新k线超过用户设定的K线数量
        if (klines.length - 1 - lowestPointAfterHigh.index > stabilizationPeriod) { // 使用 stabilizationPeriod
          for (let i = lowestPointAfterHigh.index + stabilizationPeriod; i < klines.length; i++) {
            if (i >= 4) {
              const avgVolume = klines.slice(i - 4, i).reduce((sum, k) => sum + k.volume, 0) / 4;
              if (klines[i].volume > avgVolume * volumeRatio) {
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
    }

    // 计算卖点（相反逻辑）
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

      // 并且这个最高点和最低点的涨幅超过用户设定的百分比
      if (highestPointAfterLow.index !== -1 && lowestPoint.price > 0 &&
          (highestPointAfterLow.price - lowestPoint.price) / lowestPoint.price > (priceChangeRatio / 100)) {

        // 从最高点之后到最新k线超过用户设定的K线数量
        if (klines.length - 1 - highestPointAfterLow.index > stabilizationPeriod) { // 使用 stabilizationPeriod
          for (let i = highestPointAfterLow.index + stabilizationPeriod; i < klines.length; i++) {
            if (i >= 4) {
              const avgVolume = klines.slice(i - 4, i).reduce((sum, k) => sum + k.volume, 0) / 4;
              if (klines[i].volume > avgVolume * volumeRatio) {
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
    }

    setBuyPoints(newBuyPoints);
    setSellPoints(newSellPoints);
  };

  const getChartOption = () => {
    const dates = klines.map(item => dayjs(item.timestamp).format('YYYY-MM-DD'));
    const klineData = klines.map(item => [
      item.open,
      item.close,
      item.low,
      item.high
    ]);

    const volumeData = klines.map((item, index) => {
      if (index < 4) return item.volume;
      const avgVolume = klines.slice(index - 4, index).reduce((sum, k) => sum + k.volume, 0) / 4;
      return {
        value: item.volume,
        itemStyle: {
          color: item.volume > avgVolume * volumeRatio ? '#f5222d' : '#14b143'
        }
      };
    });

    const stopFallSignals = klines.map((item, index) => {
      if (index < 4) return null;
      const avgVolume = klines.slice(index - 4, index).reduce((sum, k) => sum + k.volume, 0) / 4;
      const isVolumeSpike = item.volume > avgVolume * volumeRatio;
      const bodySize = Math.abs(item.close - item.open);
      const upperShadow = item.high - Math.max(item.close, item.open);
      const lowerShadow = Math.min(item.close, item.open) - item.low;
      const isStopFall = isVolumeSpike && bodySize < 0.5 * Math.max(upperShadow, lowerShadow);

      return isStopFall ? item.close : null;
    });

    const buyPointMarkers = buyPoints.map(point => ({
      name: '买点',
      value: 'B',
      xAxis: point.index,
      yAxis: point.price,
      itemStyle: {
        color: 'red'
      }
    }));

    const sellPointMarkers = sellPoints.map(point => ({
      name: '卖点',
      value: 'S',
      xAxis: point.index,
      yAxis: point.price,
      itemStyle: {
        color: 'green'
      }
    }));

    const series = [{
      name: 'K线',
      type: 'candlestick',
      data: klineData,
      itemStyle: {
        color: '#ef232a',
        color0: '#14b143',
        borderColor: '#ef232a',
        borderColor0: '#14b143'
      },
      markPoint: {
        data: [...buyPointMarkers, ...sellPointMarkers],
        symbolSize: 30,
        label: {
          show: true,
          formatter: '{b}',
          color: '#fff',
          fontSize: 16
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
      name: '止跌滞涨信号',
      type: 'scatter',
      data: stopFallSignals,
      symbolSize: 8,
      itemStyle: {
        color: '#0000FF'
      }
    }];

    supportLevels.forEach((level) => {
      series.push({
        name: `支撑位${level}`,
        type: 'line',
        data: Array(dates.length).fill(level),
        lineStyle: {
          color: '#00FF00',
          type: 'dashed'
        },
        symbol: 'none'
      });
    });

    resistanceLevels.forEach((level) => {
      series.push({
        name: `压力位${level}`,
        type: 'line',
        data: Array(dates.length).fill(level),
        lineStyle: {
          color: '#FF0000',
          type: 'dashed'
        },
        symbol: 'none'
      });
    });

    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'cross'
        }
      },
      legend: {
        data: ['K线', '成交量', '止跌滞涨信号', '买点', '卖点', ...supportLevels.map((v) => `支撑位${v}`), ...resistanceLevels.map((v) => `压力位${v}`)]
      },
      grid: [{
        left: '10%',
        right: '8%',
        height: '60%'
      }, {
        left: '10%',
        right: '8%',
        top: '75%',
        height: '20%'
      }],
      xAxis: [{
        type: 'category',
        data: dates,
        scale: true,
        boundaryGap: false,
        axisLine: { onZero: false },
        splitLine: { show: false },
        splitNumber: 20,
        min: 'dataMin',
        max: 'dataMax'
      }, {
        type: 'category',
        gridIndex: 1,
        data: dates,
        scale: true,
        boundaryGap: false,
        axisLine: { onZero: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        splitNumber: 20,
        min: 'dataMin',
        max: 'dataMax'
      }],
      yAxis: [{
        scale: true,
        splitArea: {
          show: true
        }
      }, {
        scale: true,
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false }
      }],
      dataZoom: [{
        type: 'inside',
        xAxisIndex: [0, 1],
        start: 0,
        end: 100
      }, {
        show: true,
        xAxisIndex: [0, 1],
        type: 'slider',
        top: '90%',
        start: 0,
        end: 100
      }],
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
          <Form.Item label="放量比率">
            <InputNumber
              min={1}
              step={0.1}
              value={volumeRatio}
              onChange={value => setVolumeRatio(value)}
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
          <Form.Item label="企稳时间(K线数)"> {/* 新增：企稳时间输入框 */}
            <InputNumber
              min={1}
              max={100}
              step={1}
              value={stabilizationPeriod}
              onChange={value => setStabilizationPeriod(value)}
            />
          </Form.Item>
        </Form>
        <ReactECharts
          key={supportLevels.join(',') + resistanceLevels.join(',') + buyPoints.length + sellPoints.length}
          option={chartOption}
          style={{ height: '600px' }}
        />
      </Card>
    </div>
  );
};

export default StockDetail;
