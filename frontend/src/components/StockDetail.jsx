import React, { useState, useEffect } from 'react';
import { Card, Spin, Button, InputNumber, Form } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import { useParams, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';
import { calculateSupportResistanceValues } from '../utils/klines'

const StockDetail = () => {
  const { symbol } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [klines, setKlines] = useState([]);
  const [supportLevels, setSupportLevels] = useState([]);
  const [resistanceLevels, setResistanceLevels] = useState([]);
  const [days, setDays] = useState(200);
  const [volumeRatio, setVolumeRatio] = useState(2);
  const [chartOption, setChartOption] = useState({});

  useEffect(() => {
    fetchKlines();
  }, [symbol]);

  useEffect(() => {
    if (klines.length > 0 && volumeRatio > 1 && days > 1) {
      calculateSupportResistance(klines);
    }
  }, [days, volumeRatio, klines]);

  useEffect(() => {
    setChartOption(getChartOption());
  }, [supportLevels, resistanceLevels]);

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
    const {supports, resistances} = calculateSupportResistanceValues(klines, days, volumeRatio)
    setSupportLevels(supports);
    setResistanceLevels(resistances);
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
      if (index < 4) return item.volume; // 前四天没有足够的数据计算平均值
      const avgVolume = klines.slice(index - 4, index).reduce((sum, k) => sum + k.volume, 0) / 4;
      return {
        value: item.volume,
        itemStyle: {
          color: item.volume > avgVolume * volumeRatio ? '#f5222d' : '#14b143' // 红色或绿色
        }
      };
    });

    const stopFallSignals = klines.map((item, index) => {
      if (index < 4) return null; // 前四天没有足够的数据计算平均值
      const avgVolume = klines.slice(index - 4, index).reduce((sum, k) => sum + k.volume, 0) / 4;
      const isVolumeSpike = item.volume > avgVolume * volumeRatio;
      const bodySize = Math.abs(item.close - item.open);
      const upperShadow = item.high - Math.max(item.close, item.open);
      const lowerShadow = Math.min(item.close, item.open) - item.low;
      const isStopFall = isVolumeSpike && bodySize < 0.5 * Math.max(upperShadow, lowerShadow);

      return isStopFall ? item.close : null;
    });

    const series = [{
      name: 'K线',
      type: 'candlestick',
      data: klineData,
      itemStyle: {
        color: '#ef232a',
        color0: '#14b143',
        borderColor: '#ef232a',
        borderColor0: '#14b143'
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
        data: ['K线', '成交量', '止跌滞涨信号', ...supportLevels.map((v) => `支撑位${v}`), ...resistanceLevels.map((v) => `压力位${v}`)]
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
        </Form>
        <ReactECharts 
          key={supportLevels.join(',') + resistanceLevels.join(',')}
          option={chartOption} 
          style={{ height: '600px' }}
        />
      </Card>
    </div>
  );
};

export default StockDetail;