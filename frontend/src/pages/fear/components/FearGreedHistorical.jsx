import React, { useState, useEffect } from 'react';
import { Radio } from 'antd';
import ReactECharts from 'echarts-for-react';
import { fetchFearGreedData } from '../../utils/cnnRequest';
import request from '../../utils/request';
import { TIME_RANGES, fitExponentialCurve } from '../utils';

const FearGreedHistorical = () => {
  const [historicalData, setHistoricalData] = useState(null);
  const [spyEmotionData, setSpyEmotionData] = useState(null);
  const [spyPriceData, setSpyPriceData] = useState(null);
  const [vixData, setVixData] = useState(null);
  const [timeRange, setTimeRange] = useState(-1);

  useEffect(() => {
    fetchHistoricalData();
  }, []);

  const fetchHistoricalData = async () => {
    try {
      const [cnnData, spyEmotion, spyPrice, vixJson] = await Promise.all([
        fetchFearGreedData(-1),
        request.get('/api/quant/etf/emotion/history/US.SPY'),
        request.get('/api/stock/klines/SPY.US', { params: { start_date: '2005-01-01' } }),
        request.get('https://api.52etf.vip/fred/series/observations?series_id=VIXCLS&file_type=json&observation_start=2005-01-01')
      ]);

      setHistoricalData(cnnData);
      setSpyEmotionData(spyEmotion.data);
      setSpyPriceData(spyPrice.data
        .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
        .map(item => ({
          timestamp: item.timestamp.split('T')[0],
          close: item.close
        })));
      setVixData(vixJson.data.observations
        .sort((a, b) => new Date(a.date) - new Date(b.date))
        .map(item => ({
          timestamp: item.date,
          value: parseFloat(item.value)
        })));
    } catch (error) {
      console.error('获取历史数据失败:', error);
    }
  };

  const renderChart = () => {
    if (!historicalData?.fear_and_greed_historical?.data) return null;

    // 处理CNN恐贪指数数据
    const cnnData = historicalData.fear_and_greed_historical.data.map(item => ({
      date: new Date(item.x).toLocaleDateString(),
      value: Number(item.y.toFixed(1)),
      type: 'CNN'
    }));

    // 处理SPY恐贪指数数据
    const spyEmotionDataArray = spyEmotionData?.data?.map(item => ({
      date: new Date(item.date).toLocaleDateString(),
      value: item.score,
      type: '守逮'
    })) || [];

    // 处理SPY股价数据
    const spyPriceDataArray = spyPriceData?.map(item => ({
      date: new Date(item.timestamp).toLocaleDateString(),
      value: item.close,
      type: '股价'
    })) || [];

    // 处理VIX数据
    const vixDataArray = vixData?.map(item => ({
      date: new Date(item.timestamp).toLocaleDateString(),
      value: item.value,
      type: 'VIX'
    })) || [];

    // 根据选择的时间范围过滤数据
    const filterDataByTimeRange = (data) => {
      if (timeRange === -1) return data;

      const cutoffDate = new Date();
      cutoffDate.setFullYear(cutoffDate.getFullYear() - timeRange);

      return data.filter(item => new Date(item.date) >= cutoffDate);
    };

    const filteredCnnData = filterDataByTimeRange(cnnData);
    const filteredSpyEmotionData = filterDataByTimeRange(spyEmotionDataArray);
    const filteredSpyPriceData = filterDataByTimeRange(spyPriceDataArray);
    const filteredVixData = filterDataByTimeRange(vixDataArray);

    // 获取所有日期
    const allDates = filteredSpyPriceData.map(item => item.date);

    // 按日期排序
    const sortedDates = Array.from(allDates).sort((a, b) => new Date(a) - new Date(b));

    // 创建日期到数据的映射
    const cnnDataMap = new Map(filteredCnnData.map(item => [item.date, item.value]));
    const spyEmotionDataMap = new Map(filteredSpyEmotionData.map(item => [item.date, item.value]));
    const spyPriceDataMap = new Map(filteredSpyPriceData.map(item => [item.date, item.value]));
    const vixDataMap = new Map(filteredVixData.map(item => [item.date, item.value]));

    // 为每个日期创建数据点
    const alignedCnnData = sortedDates.map(date => ({
      date,
      value: cnnDataMap.get(date) ?? null,
      type: 'CNN'
    }));

    const alignedSpyEmotionData = sortedDates.map(date => ({
      date,
      value: spyEmotionDataMap.get(date) ?? null,
      type: '守逮'
    }));

    const alignedSpyPriceData = sortedDates.map(date => ({
      date,
      value: spyPriceDataMap.get(date) ?? null,
      type: '股价'
    }));

    const alignedVixData = sortedDates.map(date => ({
      date,
      value: vixDataMap.get(date) ?? null,
      type: 'VIX'
    }));

    // 计算股价的最大最小值
    const priceValues = spyPriceDataArray.map(item => item.value);
    const minPrice = Math.min(...priceValues);
    const maxPrice = Math.max(...priceValues);
    // 添加一些边距
    const pricePadding = (maxPrice - minPrice) * 0.1;
    const priceMin = Math.floor(minPrice - pricePadding);
    const priceMax = Math.ceil(maxPrice + pricePadding);

    // 获取过滤后的价格数据
    const filteredPrices = filteredSpyPriceData.map(item => item.value);

    // 使用过滤后的数据计算趋势线
    const { A, B } = fitExponentialCurve(filteredPrices);

    // 生成拟合曲线数据
    const fittedData = sortedDates.map((date, index) => ({
      date,
      value: A * Math.pow(B, index),
      type: '趋势线'
    }));

    const option = {
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'cross'
        }
      },
      legend: {
        data: ['CNN', '守逮', '股价', '趋势线', 'VIX'],
        top: 0
      },
      grid: {
        left: '3%',
        right: '4%',
        bottom: '3%',
        containLabel: true
      },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: sortedDates
      },
      yAxis: [
        {
          type: 'value',
          name: 'CNN',
          position: 'left',
          min: 0,
          max: 100,
          axisLine: {
            show: true,
            lineStyle: {
              color: '#1890ff'
            }
          },
          axisLabel: {
            formatter: '{value}'
          }
        },
        {
          type: 'value',
          name: '守逮',
          position: 'left',
          offset: 50,
          min: -100,
          max: 100,
          axisLine: {
            show: true,
            lineStyle: {
              color: '#52c41a'
            }
          },
          axisLabel: {
            formatter: '{value}'
          }
        },
        {
          type: 'value',
          name: '股价',
          position: 'right',
          min: priceMin,
          max: priceMax,
          axisLine: {
            show: true,
            lineStyle: {
              color: '#fa8c16'
            }
          },
          axisLabel: {
            formatter: '{value}'
          }
        },
        {
          type: 'value',
          name: 'VIX',
          position: 'right',
          offset: 50,
          min: 0,
          max: 100,
          axisLine: {
            show: true,
            lineStyle: {
              color: '#eb2f96'
            }
          },
          axisLabel: {
            formatter: '{value}'
          }
        }
      ],
      series: [
        {
          name: 'CNN',
          type: 'line',
          smooth: true,
          data: alignedCnnData.map(item => item.value),
          itemStyle: {
            color: '#1890ff'
          },
          lineStyle: {
            width: 1
          }
        },
        {
          name: '守逮',
          type: 'line',
          smooth: true,
          yAxisIndex: 1,
          data: alignedSpyEmotionData.map(item => item.value),
          itemStyle: {
            color: '#52c41a'
          },
          lineStyle: {
            width: 1
          }
        },
        {
          name: '股价',
          type: 'line',
          smooth: true,
          yAxisIndex: 2,
          data: alignedSpyPriceData.map(item => item.value),
          itemStyle: {
            color: '#fa8c16'
          },
          lineStyle: {
            width: 3
          }
        },
        {
          name: '趋势线',
          type: 'line',
          smooth: true,
          yAxisIndex: 2,
          data: fittedData.map(item => item.value.toFixed(3)),
          itemStyle: {
            color: '#722ed1'
          },
          lineStyle: {
            width: 2,
            type: 'dashed'
          }
        },
        {
          name: 'VIX',
          type: 'line',
          smooth: true,
          yAxisIndex: 3,
          data: alignedVixData.map(item => item.value),
          itemStyle: {
            color: '#eb2f96'
          },
          lineStyle: {
            width: 1
          }
        }
      ]
    };

    return (
      <>
        <div style={{ marginBottom: 16, textAlign: 'right' }}>
          <Radio.Group
            value={timeRange}
            onChange={e => setTimeRange(e.target.value)}
            optionType="button"
            buttonStyle="solid"
          >
            {TIME_RANGES.map(range => (
              <Radio.Button key={range.value} value={range.value}>
                {range.label}
              </Radio.Button>
            ))}
          </Radio.Group>
        </div>
        <ReactECharts option={option} style={{ height: '400px' }} />
      </>
    );
  };

  return renderChart();
};

export default FearGreedHistorical;
