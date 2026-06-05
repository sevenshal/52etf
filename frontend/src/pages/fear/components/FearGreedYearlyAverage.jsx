import React, { useState, useEffect } from 'react';
import ReactECharts from 'echarts-for-react';
import { message } from 'antd';
import request from '../../utils/request';

const FearGreedYearlyAverage = () => {
  const [yearlyAverageData, setYearlyAverageData] = useState(null);

  useEffect(() => {
    fetchYearlyAverageData();
  }, []);

  const fetchYearlyAverageData = async () => {
    try {
      const response = await request.get('/api/stock/klines/SPY.US', {
        params: {
          start_date: '2005-01-01'
        }
      });
      const historicalData = response.data.map(item => ({
        date: item.timestamp.split('T')[0],
        close: item.close
      }));

      // 按日期排序
      const sortedData = historicalData.sort((a, b) => new Date(a.date) - new Date(b.date));

      // 填充缺失的日期数据
      const filledData = [];
      let currentDate = new Date(sortedData[0].date);
      const endDate = new Date(sortedData[sortedData.length - 1].date);

      while (currentDate <= endDate) {
        const dateStr = currentDate.toISOString().split('T')[0];

        // 查找当前日期是否有数据
        const existingData = sortedData.find(item => item.date === dateStr);

        if (existingData) {
          // 如果找到数据，使用实际数据
          filledData.push(existingData);
        } else {
          // 如果没有数据，使用前一天的收盘价填充
          const previousData = filledData[filledData.length - 1];
          if (previousData) {
            filledData.push({
              date: dateStr,
              close: previousData.close
            });
          }
        }

        // 移动到下一天
        currentDate.setDate(currentDate.getDate() + 1);
      }

      // 按年份分组
      const yearlyData = {};
      filledData.forEach(item => {
        const year = new Date(item.date).getFullYear();
        if (!yearlyData[year]) {
          yearlyData[year] = [];
        }
        yearlyData[year].push(item);
      });

      // 对每年数据进行归一化处理
      const normalizedData = {};
      Object.keys(yearlyData).forEach(year => {
        const yearData = yearlyData[year];
        if (yearData.length === 0) return;

        // 找到该年第一天的股价作为基准
        const firstDayPrice = yearData[0].close;

        // 归一化处理：第一天的股价记为100，其他天按比例折算
        normalizedData[year] = yearData.map(item => ({
          date: item.date,
          normalizedPrice: (item.close / firstDayPrice) * 100
        }));
      });

      // 计算除今年外其他年份按月日的平均值
      const currentYear = new Date().getFullYear();

      // 将日期转换为月日格式（MM-DD）
      const formatMonthDay = (dateStr) => {
        const date = new Date(dateStr);
        const month = (date.getMonth() + 1).toString().padStart(2, '0');
        const day = date.getDate().toString().padStart(2, '0');
        return `${month}-${day}`;
      };

      // 按月日分组计算平均值
      const monthDayToPrices = {};

      Object.keys(normalizedData).forEach(year => {
        if (parseInt(year) !== currentYear) {
          normalizedData[year].forEach(item => {
            const monthDay = formatMonthDay(item.date);
            if (!monthDayToPrices[monthDay]) {
              monthDayToPrices[monthDay] = [];
            }
            monthDayToPrices[monthDay].push(item.normalizedPrice);
          });
        }
      });

      // 计算每个月日的平均值
      const averageData = [];
      Object.keys(monthDayToPrices).forEach(monthDay => {
        const prices = monthDayToPrices[monthDay];
        const averagePrice = prices.reduce((sum, price) => sum + price, 0) / prices.length;
        averageData.push({
          monthDay: monthDay,
          averagePrice: averagePrice
        });
      });

      // 按月日排序
      averageData.sort((a, b) => a.monthDay.localeCompare(b.monthDay));

      // 获取今年的数据
      const currentYearData = normalizedData[currentYear] || [];

      // 创建月日到数据的映射
      const monthDayToAverage = {};
      averageData.forEach(item => {
        monthDayToAverage[item.monthDay] = item.averagePrice;
      });

      const monthDayToCurrent = {};
      currentYearData.forEach(item => {
        const monthDay = formatMonthDay(item.date);
        monthDayToCurrent[monthDay] = item.normalizedPrice;
      });

      // 获取所有月日并排序
      const allMonthDays = new Set();
      Object.keys(monthDayToAverage).forEach(monthDay => allMonthDays.add(monthDay));
      Object.keys(monthDayToCurrent).forEach(monthDay => allMonthDays.add(monthDay));
      const sortedMonthDays = Array.from(allMonthDays).sort();

      // 识别高点和低点的函数
      const findHighLowPoints = (data, monthDays) => {
        const dataArray = monthDays.map(monthDay => data[monthDay] || null);
        const highPoints = [];
        const lowPoints = [];

        for (let i = 15; i < dataArray.length - 15; i++) {
          if (dataArray[i] === null) continue;

          // 检查是否是低点（前15天和后15天的最低点）
          const windowStart = Math.max(0, i - 15);
          const windowEnd = Math.min(dataArray.length - 1, i + 15);
          const windowData = dataArray.slice(windowStart, windowEnd + 1).filter(val => val !== null);

          if (windowData.length === 0) continue;
          const minInWindow = Math.min(...windowData);
          const maxInWindow = Math.max(...windowData);

          // 如果是低点
          if (dataArray[i] === minInWindow) {
            lowPoints.push({
              index: i,
              value: dataArray[i],
              date: monthDays[i]
            });
          }

          // 如果是高点
          if (dataArray[i] === maxInWindow) {
            highPoints.push({
              index: i,
              value: dataArray[i],
              date: monthDays[i]
            });
          }
        }

        return { highPoints, lowPoints };
      };

      // 只为历史平均数据找到高点和低点
      const averagePoints = findHighLowPoints(monthDayToAverage, sortedMonthDays);

      // 调试信息
      console.log('阶段高点:', averagePoints.highPoints);
      console.log('阶段低点:', averagePoints.lowPoints);

      // 创建图表配置
      const option = {
        tooltip: {
          trigger: 'axis',
          formatter: function (params) {
            let result = `${params[0].name}<br/>`;
            params.forEach(param => {
              const value = param.value;
              if (value !== null && value !== undefined && !isNaN(value)) {
                result += `${param.seriesName}: ${value.toFixed(2)}<br/>`;
              } else {
                result += `${param.seriesName}: --<br/>`;
              }
            });
            return result;
          }
        },
        legend: {
          data: ['历史平均', '阶段高点', '阶段低点', '今年实际'],
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
          data: sortedMonthDays
        },
        yAxis: {
          type: 'value',
          name: '归一化价格',
          axisLabel: {
            formatter: '{value}'
          },
          scale: true,
          min: function (value) {
            return Math.floor(value.min * 0.95);
          },
          max: function (value) {
            return Math.ceil(value.max * 1.05);
          }
        },
        series: [
          // 历史平均线
          {
            name: '历史平均',
            type: 'line',
            smooth: true,
            data: sortedMonthDays.map(monthDay => monthDayToAverage[monthDay] || null),
            itemStyle: {
              color: '#1890ff'
            },
            lineStyle: {
              width: 2
            }
          },
          // 历史平均高点标记
          {
            name: '阶段高点',
            type: 'scatter',
            data: averagePoints.highPoints.map(point => [point.date, point.value]),
            itemStyle: {
              color: '#ff4d4f',
              size: 8
            },
            symbol: 'circle',
            symbolSize: 8,
            z: 10
          },
          // 历史平均低点标记
          {
            name: '阶段低点',
            type: 'scatter',
            data: averagePoints.lowPoints.map(point => [point.date, point.value]),
            itemStyle: {
              color: '#52c41a',
              size: 8
            },
            symbol: 'circle',
            symbolSize: 8,
            z: 10
          },
          // 今年实际线
          {
            name: '今年实际',
            type: 'line',
            smooth: true,
            data: sortedMonthDays.map(monthDay => monthDayToCurrent[monthDay] || null),
            itemStyle: {
              color: '#fa8c16'
            },
            lineStyle: {
              width: 2
            }
          }
        ]
      };

      setYearlyAverageData(option);
    } catch (error) {
      console.error('获取年度平均数据失败:', error);
      message.error('获取年度平均数据失败');
    }
  };

  if (!yearlyAverageData) {
    return <div style={{ textAlign: 'center', padding: '20px' }}>加载中...</div>;
  }

  return <ReactECharts option={yearlyAverageData} style={{ height: '300px' }} />;
};

export default FearGreedYearlyAverage;
