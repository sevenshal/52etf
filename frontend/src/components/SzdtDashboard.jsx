import React, { useState, useEffect } from 'react';
import { Card, Button, Switch, Input, Modal, message, List, Space, Statistic, Row, Col, Tooltip, Tabs, Radio } from 'antd';
import { RightOutlined, ArrowUpOutlined, ArrowDownOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import { fetchFearGreedData } from '../utils/cnnRequest';
import FearGreedCompass from './FearGreedCompass';

// 时间范围选项
const TIME_RANGES = [
  { label: '1年', value: 1 },
  { label: '3年', value: 3 },
  { label: '5年', value: 5 },
  { label: '10年', value: 10 },
  { label: '20年', value: 20 },
  { label: '全部', value: -1 }
];

const SzdtDashboard = () => {
  const navigate = useNavigate();
  const [showActivation, setShowActivation] = useState(false);
  const [activationCode, setActivationCode] = useState('');
  const [autoTrading, setAutoTrading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [fearGreedData, setFearGreedData] = useState(null);
  const [historicalData, setHistoricalData] = useState(null);
  const [spyEmotionData, setSpyEmotionData] = useState(null);
  const [spyPriceData, setSpyPriceData] = useState(null);
  const [vixData, setVixData] = useState(null);
  const [aiaeData, setAiaeData] = useState(null);
  const [activeTab, setActiveTab] = useState('current');
  const [timeRange, setTimeRange] = useState(-1);
  const [us10y, setUs10y] = useState(null); // 实时10年期国债收益率
  const [fedRateFrom, setFedRateFrom] = useState(null); // 当前存款利率from
  const [fedRateTo, setFedRateTo] = useState(null); // 当前存款利率to
  const [forwardMin, setForwardMin] = useState(null); // 未来一年预测区间下限
  const [forwardMax, setForwardMax] = useState(null); // 未来一年预测区间上限
  const [bondFearGreed, setBondFearGreed] = useState(null); // 美债贪恐值
  const [forwardTable, setForwardTable] = useState({ columns: [], rows: [] });
  const [predictionData, setPredictionData] = useState(null); // 走势预测数据

  useEffect(() => {
    // 并发请求初始数据
    const fetchInitialData = async () => {
      try {
        const [fearGreedResp, tradingResp] = await Promise.all([
          fetchFearGreedData(0),
          request.get('/api/quant/auto-trading-status')
        ]);
        
        setFearGreedData(fearGreedResp);
        setAutoTrading(tradingResp.data.enabled);
        setLoading(false);
      } catch (error) {
        message.error('获取数据失败');
        setLoading(false);
      }
    };

    fetchInitialData();
  }, []);

  // 获取当前利率from/to
  useEffect(() => {
    async function fetchFedRate() {
      const resp = await fetch('https://markets.newyorkfed.org/read?productCode=50&eventCodes=500&limit=1&startPosition=0&format=json');
      const data = await resp.json();
      if (data && data.refRates && data.refRates.length > 0) {
        setFedRateFrom(data.refRates[0].targetRateFrom);
        setFedRateTo(data.refRates[0].targetRateTo);
      }
    }
    fetchFedRate();
  }, []);

  // 获取未来一年所有预测区间和表格数据
  useEffect(() => {
    async function fetchForward1y() {
      const resp = await request.get('/api/fed-rate/monitor');
      const result = resp.data;
      if (!result || result.status !== 'success' || !Array.isArray(result.data) || result.data.length === 0) return;
      const data = result.data;
      const now = new Date();
      const oneYearLater = new Date(now);
      oneYearLater.setFullYear(now.getFullYear() + 1);

      // 1. 收集所有日期（列头）
      const columns = [];
      const dateMap = {};
      for (const item of data) {
        item.date = item.date.replace(/年|月/g, '-').replace('日', '');
        columns.push(item.date);
        dateMap[item.date] = item;
      }

      // 2. 收集所有区间（行头，去重升序，所有rate_info都要）
      const rateSet = new Set();
      for (const item of data) {
        if (item.rate_info && item.rate_info.length > 0) {
          for (const rate of item.rate_info) {
            rateSet.add(rate.target_rate);
          }
        }
      }
      const rates = Array.from(rateSet).sort((a, b) => {
        const aLow = parseFloat(a.split('-')[0]);
        const bLow = parseFloat(b.split('-')[0]);
        return aLow - bLow;
      });

      // 3. 构建表格内容（所有区间都显示，去除全为0或空的行）
      const rows = rates.map(rate => {
        const row = { rate };
        let hasNonZero = false;
        for (const dateStr of columns) {
          const item = dateMap[dateStr];
          let prob = '';
          if (item && item.rate_info) {
            const found = item.rate_info.find(r => r.target_rate === rate);
            prob = found ? found.current_probability : '';
          }
          row[dateStr] = prob;
          // 判断是否有非0且非空概率
          if (prob && parseFloat(prob.replace('%', '')) > 1) {
            hasNonZero = true;
          }
        }
        return hasNonZero ? row : null;
      }).filter(Boolean);
      setForwardTable({ columns, rows });

      // 4. 计算贪恐区间（只用每个会议概率最高的区间）
      let allLowers = [], allUppers = [];
      for (const item of data) {
        if (new Date(item.date.replace(/年|月/g, '-').replace('日', '')) > oneYearLater) continue;
        if (item.rate_info && item.rate_info.length > 0) {
          let maxProb = -1, bestRate = null;
          for (const rate of item.rate_info) {
            const prob = parseFloat(rate.current_probability.replace('%', ''));
            if (prob > maxProb) {
              maxProb = prob;
              bestRate = rate.target_rate;
            }
          }
          if (bestRate) {
            const [low, up] = bestRate.split('-').map(s => parseFloat(s));
            allLowers.push(low);
            allUppers.push(up);
          }
        }
      }
      setForwardMin(allLowers.length > 0 ? Math.min(...allLowers) : null);
      setForwardMax(allUppers.length > 0 ? Math.max(...allUppers) : null);
    }
    fetchForward1y();
  }, []);

  // 实时获取10年期国债收益率
  useEffect(() => {
    const { US10YWS } = require('../utils/us10yWS');
    const ws = new US10YWS({
      onYieldUpdate: (val) => setUs10y(val)
    });
    ws.connect();
    return () => ws.disconnect();
  }, []);

  // 计算美债贪恐值
  useEffect(() => {
    if (us10y && fedRateFrom !== null && fedRateTo !== null && forwardMin !== null && forwardMax !== null) {
      // 取所有下限和上限的最小值和最大值
      const minRate = Math.min(fedRateFrom, fedRateTo, forwardMin, forwardMax);
      const maxRate = Math.max(fedRateFrom, fedRateTo, forwardMin, forwardMax);
      let val = 100 * (maxRate - us10y) / (maxRate - minRate);
      if (us10y <= minRate) val = 100;
      if (us10y >= maxRate) val = 0;
      setBondFearGreed(Math.round(val));
    }
  }, [us10y, fedRateFrom, fedRateTo, forwardMin, forwardMax]);

  // 监听标签切换，按需加载数据
  useEffect(() => {
    if (activeTab === 'historical' && !historicalData) {
      fetchHistoricalData();
    } else if (activeTab === 'aiae' && !aiaeData) {
      fetchAiaeData();
    } else if (activeTab === 'prediction' && !predictionData) {
      fetchPredictionData();
    }
  }, [activeTab]);

  const fetchPredictionData = async () => {
    try {
      const response = await request.get('https://api.52etf.vip/fmp/api/v3/historical-price-full/SPY?from=2005-01-01&serietype=line');
      const historicalData = response.data.historical;
      
      // 按日期排序
      const sortedData = historicalData.sort((a, b) => new Date(a.date) - new Date(b.date));
      
      // 填充缺失的日期数据
      const filledData = [];
      for (let i = 0; i < sortedData.length; i++) {
        filledData.push(sortedData[i]);
        // 如果不是最后一天，检查是否有缺失的日期
        if (i < sortedData.length - 1) {
          const currentDate = new Date(sortedData[i].date);
          const nextDate = new Date(sortedData[i + 1].date);
          const daysDiff = Math.floor((nextDate - currentDate) / (1000 * 60 * 60 * 24));
          
          // 如果有缺失的日期，用前一天的数据填充
          for (let j = 1; j < daysDiff; j++) {
            const missingDate = new Date(currentDate);
            missingDate.setDate(currentDate.getDate() + j);
            filledData.push({
              date: missingDate.toISOString().split('T')[0],
              close: sortedData[i].close
            });
          }
        }
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
      
      // 计算除今年外其他年份的平均值
      const currentYear = new Date().getFullYear();
      const allDates = new Set();
      
      // 收集所有日期
      Object.keys(normalizedData).forEach(year => {
        if (parseInt(year) !== currentYear) {
          normalizedData[year].forEach(item => {
            allDates.add(item.date);
          });
        }
      });
      
      // 计算每个日期的平均值
      const averageData = [];
      const sortedDates = Array.from(allDates).sort();
      
      sortedDates.forEach(date => {
        let sum = 0;
        let count = 0;
        
        Object.keys(normalizedData).forEach(year => {
          if (parseInt(year) !== currentYear) {
            const yearData = normalizedData[year];
            const item = yearData.find(d => d.date === date);
            if (item) {
              sum += item.normalizedPrice;
              count++;
            }
          }
        });
        
        if (count > 0) {
          averageData.push({
            date: date,
            averagePrice: sum / count
          });
        }
      });
      
      // 获取今年的数据
      const currentYearData = normalizedData[currentYear] || [];
      
      // 将日期转换为月日格式（MM-DD）
      const formatMonthDay = (dateStr) => {
        const date = new Date(dateStr);
        const month = (date.getMonth() + 1).toString().padStart(2, '0');
        const day = date.getDate().toString().padStart(2, '0');
        return `${month}-${day}`;
      };
      
      // 创建月日到数据的映射
      const monthDayToAverage = {};
      averageData.forEach(item => {
        const monthDay = formatMonthDay(item.date);
        monthDayToAverage[monthDay] = item.averagePrice;
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
      
      // 创建图表配置
      const option = {
        tooltip: {
          trigger: 'axis',
          formatter: function(params) {
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
          data: ['历史平均', '今年实际'],
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
          }
        },
        series: [
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
      
      setPredictionData(option);
    } catch (error) {
      console.error('获取预测数据失败:', error);
      message.error('获取预测数据失败');
    }
  };

  const fetchHistoricalData = async () => {
    try {
      const [cnnData, spyEmotion, spyPrice, vixJson] = await Promise.all([
        fetchFearGreedData(-1),
        request.get('/api/quant/etf/emotion/history/US.SPY'),
        request.get('/fmp/api/v3/historical-price-full/SPY?from=2005-01-01&serietype=line'),
        request.get('/fred/series/observations?series_id=VIXCLS&file_type=json&observation_start=2005-01-01')
      ]);
      
      setHistoricalData(cnnData);
      setSpyEmotionData(spyEmotion.data);
      setSpyPriceData(spyPrice.data.historical
        .sort((a, b) => new Date(a.date) - new Date(b.date))
        .map(item => ({
          timestamp: item.date,
          close: item.close
        })));
      setVixData(vixJson.data.observations
        .sort((a, b) => new Date(a.date) - new Date(b.date))
        .map(item => ({
          timestamp: item.date,
          value: parseFloat(item.value)
        })));
    } catch (error) {
      message.error('获取历史数据失败');
    }
  };

  const fetchAiaeData = async () => {
    try {
      const { data } = await request.get('/fred/series/observations?series_id=BOGZ1FL153064476Q&file_type=json');
      // 处理数据
      const aiaeData = data.observations
        .map(item => ({
          date: item.date,
          value: parseFloat(item.value)
        }))
        .filter(item => !isNaN(item.value))
        .filter(item => new Date(item.date) >= new Date('1951-10-01')); // 只保留1951-10-01及之后的数据

      setAiaeData(aiaeData);
      return aiaeData;
    } catch (error) {
      console.error('获取AIAE数据失败:', error);
      message.error('获取AIAE数据失败');
      return null;
    }
  };

  const formatQuarter = (dateStr) => {
    const date = new Date(dateStr);
    const year = date.getFullYear();
    const month = date.getMonth() + 1;
    const quarter = Math.ceil(month / 3);
    return `${year}Q${quarter}`;
  };

  const handleAutoTradingChange = async (checked) => {
    try {
      await request.post('/api/quant/auto-trading', { enabled: checked });
      setAutoTrading(checked);
      message.success(checked ? '自动交易已开启' : '自动交易已关闭');
    } catch (error) {
      message.error('操作失败：' + (error.response?.data?.detail || '未知错误'));
    }
  };

  const handleActivate = async () => {
    try {
      await request.post('/api/quant/activate', { code: activationCode });
      message.success('激活码修改成功');
      setShowActivation(false);
      setActivationCode('');
    } catch (error) {
      message.error('激活码修改失败：' + (error.response?.data?.detail || '未知错误'));
    }
  };

  const items = [
    {
      title: '自动交易',
      content: <Switch checked={autoTrading} onChange={handleAutoTradingChange} />,
    },
    {
      title: '修改激活码',
      onClick: () => setShowActivation(true),
      arrow: true
    },
    {
      title: '股票列表',
      onClick: () => navigate('/szdt/stocks'),
      arrow: true
    },
    {
      title: '交易日志',
      onClick: () => navigate('/szdt/logs'),
      arrow: true
    },
    {
      title: 'ETF回测',
      onClick: () => navigate('/szdt/backtest'),
      arrow: true
    }
  ];

  if (loading) {
    return <div>加载中...</div>;
  }

  const getFearGreedColor = (value) => {
    if (value >= 75) return '#cf1322';  // 极度贪婪
    if (value >= 55) return '#fa8c16';  // 贪婪
    if (value >= 45) return '#d9d9d9';  // 中性
    if (value >= 25) return '#52c41a';  // 恐惧
    return '#237804';  // 极度恐惧
  };

  const getFearGreedStatus = (score) => {
    if (score >= 75) return '极度贪婪';
    if (score >= 55) return '贪婪';
    if (score >= 45) return '中性';
    if (score >= 25) return '恐惧';
    return '极度恐惧';
  };

  const getCellColor = (forwardTable) => {
    // 预处理每一列的最大最小概率
    const colProbMap = {};
    for (const dateStr of forwardTable.columns) {
      const probs = forwardTable.rows.map(row => parseFloat((row[dateStr] || '').replace('%', ''))).filter(v => !isNaN(v));
      if (probs.length === 0) continue;
      const max = Math.max(...probs);
      const min = Math.min(...probs);
      colProbMap[dateStr] = { max, min };
    }
    // 返回一个函数用于渲染
    return (row, dateStr) => {
      const val = parseFloat((row[dateStr] || '').replace('%', ''));
      if (isNaN(val)) return {};
      const { max, min } = colProbMap[dateStr] || {};
      if (val === max) return { background: '#003a8c', color: '#fff' };
      if (val === min) return { background: '#fff' };
      // 渐变色，最大深蓝，最小白色
      const percent = (val - min) / (max - min || 1);
      const blue = Math.round(255 - percent * 100);
      return { background: `rgb(${blue},${blue + 30},255)` };
    };
  };

  const renderHistoricalChart = () => {
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

    // 计算拟合曲线
    const fitExponentialCurve = (data) => {
      if (!data || data.length === 0) return { A: 0, B: 0 };
      
      // 将数据转换为对数形式进行线性拟合
      const n = data.length;
      let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
      const x = Array.from({length: n}, (_, i) => i);
      const y = data.map(v => Math.log(v));
      
      for (let i = 0; i < n; i++) {
        sumX += x[i];
        sumY += y[i];
        sumXY += x[i] * y[i];
        sumXX += x[i] * x[i];
      }
      
      // 计算线性回归系数
      const b = (n * sumXY - sumX * sumY) / (n * sumXX - sumX * sumX);
      const a = (sumY - b * sumX) / n;
      
      // 转换回指数形式
      const A = Math.exp(a);
      const B = Math.exp(b);
      
      return { A, B };
    };

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

  const renderAiaeChart = () => {
    if (!aiaeData) return null;

    const option = {
      tooltip: {
        trigger: 'axis',
        formatter: function(params) {
          return `${formatQuarter(params[0].name)}<br/>${params[0].value}%`;
        }
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
        data: aiaeData.map(item => item.date),
        axisLabel: {
          formatter: function(value) {
            return formatQuarter(value);
          }
        }
      },
      yAxis: {
        type: 'value',
        name: '百分比',
        axisLabel: {
          formatter: '{value}%'
        }
      },
      series: [
        {
          name: 'AIAE',
          type: 'line',
          smooth: true,
          data: aiaeData.map(item => item.value),
          itemStyle: {
            color: '#1890ff'
          },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [{
                offset: 0,
                color: 'rgba(24,144,255,0.3)'
              }, {
                offset: 1,
                color: 'rgba(24,144,255,0.1)'
              }]
            }
          }
        }
      ]
    };

    return <ReactECharts option={option} style={{ height: '300px' }} />;
  };

  return (
    <>
      {fearGreedData && (
        <Card title='标普500恐贪指数' style={{ marginBottom: 16 }}>
          <Tabs activeKey={activeTab} onChange={setActiveTab}>
            <Tabs.TabPane tab="当前指数" key="current">
              <Row gutter={[16, 16]}>
                <Col xs={24} sm={24} md={8} lg={8} xl={8}>
                  <FearGreedCompass 
                    score={fearGreedData.fear_and_greed.score}
                    rating={getFearGreedStatus(fearGreedData.fear_and_greed.score)}
                  />
                </Col>
                <Col xs={24} sm={24} md={16} lg={16} xl={16}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
                      <span style={{ color: '#666' }}>昨日收盘</span>
                      <div style={{ textAlign: 'right' }}>
                        <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_close) }}>
                          {Math.round(fearGreedData.fear_and_greed.previous_close)}
                        </div>
                        <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_close) }}>
                          {getFearGreedStatus(fearGreedData.fear_and_greed.previous_close)}
                        </div>
                      </div>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
                      <span style={{ color: '#666' }}>一周前</span>
                      <div style={{ textAlign: 'right' }}>
                        <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_week) }}>
                          {Math.round(fearGreedData.fear_and_greed.previous_1_week)}
                        </div>
                        <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_week) }}>
                          {getFearGreedStatus(fearGreedData.fear_and_greed.previous_1_week)}
                        </div>
                      </div>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
                      <span style={{ color: '#666' }}>一月前</span>
                      <div style={{ textAlign: 'right' }}>
                        <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_month) }}>
                          {Math.round(fearGreedData.fear_and_greed.previous_1_month)}
                        </div>
                        <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_month) }}>
                          {getFearGreedStatus(fearGreedData.fear_and_greed.previous_1_month)}
                        </div>
                      </div>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
                      <span style={{ color: '#666' }}>一年前</span>
                      <div style={{ textAlign: 'right' }}>
                        <div style={{ fontSize: '16px', fontWeight: 'bold', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_year) }}>
                          {Math.round(fearGreedData.fear_and_greed.previous_1_year)}
                        </div>
                        <div style={{ fontSize: '12px', color: getFearGreedColor(fearGreedData.fear_and_greed.previous_1_year) }}>
                          {getFearGreedStatus(fearGreedData.fear_and_greed.previous_1_year)}
                        </div>
                      </div>
                    </div>
                  </div>
                </Col>
              </Row>
            </Tabs.TabPane>
            <Tabs.TabPane tab="历史走势" key="historical">
              {renderHistoricalChart()}
            </Tabs.TabPane>
            <Tabs.TabPane tab="走势预测" key="prediction">
              {predictionData ? (
                <ReactECharts option={predictionData} style={{ height: '300px' }} />
              ) : (
                <div style={{ textAlign: 'center', padding: '20px' }}>加载中...</div>
              )}
            </Tabs.TabPane>
            <Tabs.TabPane tab="AIAE" key="aiae">
              {aiaeData ? (
                <>
                  <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
                    <Col span={24}>
                      <Statistic
                        title="家庭和非营利组织；直接和间接持有的公司股权占总资产的百分比"
                        value={aiaeData[aiaeData.length - 1].value}
                        precision={2}
                        suffix="%"
                        valueStyle={{ color: '#1890ff' }}
                      />
                    </Col>
                  </Row>
                  {renderAiaeChart()}
                </>
              ) : (
                <div style={{ textAlign: 'center', padding: '20px' }}>加载中...</div>
              )}
            </Tabs.TabPane>
          </Tabs>
        </Card>
      )}

      <Card title='美债贪恐指数及联邦概率预测' style={{ marginBottom: 16 }}>
        <Row>
          <Col span={4} xs={24} sm={24} md={4} lg={4} xl={4}>
            <Statistic
              title="美债贪恐值"
              style={{ marginBottom: 16 }}
              value={
                bondFearGreed !== null
                  ? `${bondFearGreed}/100${
                      bondFearGreed <= 30
                        ? ' (恐慌)'
                        : bondFearGreed >= 70
                        ? ' (贪婪)'
                        : ' (中性)'
                    }`
                  : '...'}
              valueStyle={{
                color:
                  bondFearGreed >= 75
                    ? '#cf1322'
                    : bondFearGreed >= 55
                    ? '#fa8c16'
                    : bondFearGreed >= 45
                    ? '#d9d9d9'
                    : bondFearGreed >= 25
                    ? '#52c41a'
                    : '#237804',
              }}
            />
            <div>10Y国债实时利率：{us10y || '...'}</div>
            <div>当前执行利率：{fedRateFrom} - {fedRateTo}</div>
            <div>未来一年预测利率：{forwardMin} - {forwardMax}</div>
          </Col>
          <Col span={20} xs={24} sm={24} md={20} lg={20} xl={20}>
            <div style={{ display: 'flex', flexDirection: 'row', alignItems: 'flex-start' }}>
                {/* 表格 */}
                <div style={{ minWidth: 320, overflowX: 'auto', marginRight: 16 }}>
                  <table className="forward-table" style={{ borderCollapse: 'collapse', width: '100%' }}>
                    <thead>
                      <tr>
                        <th style={{ background: '#f0f0f0', position: 'sticky', left: 0, zIndex: 1, padding: '0 6px' }}>区间</th>
                        {forwardTable.columns.map(dateStr => (
                          <th
                            key={dateStr}
                            style={{
                              background: '#f0f0f0',
                              padding: '0 6px',
                              whiteSpace: 'nowrap'
                            }}
                          >
                            {dateStr}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {forwardTable.rows.slice().reverse().map(row => {
                        const cellColor = getCellColor(forwardTable);
                        return (
                          <tr key={row.rate}>
                            <td style={{ background: '#fafafa', position: 'sticky', left: 0, zIndex: 1, whiteSpace: 'nowrap', padding: '0 6px' }}>{row.rate}</td>
                            {forwardTable.columns.map(dateStr => (
                              <td key={dateStr} style={{...cellColor(row, dateStr), whiteSpace: 'nowrap', padding: '0 6px'}}>
                                {row[dateStr]}
                              </td>
                            ))}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
          </Col>
        </Row>
      </Card>

      <Card title='守猪逮兔恐贪模型'>
        <List
          style={{
            backgroundColor: '#fff'
          }}
        >
          {items.map((item, index) => (
            <List.Item
              key={index}
              onClick={item.onClick}
              style={{
                padding: '16px',
                cursor: item.onClick ? 'pointer' : 'default',
                borderBottom: '1px solid #f0f0f0'
              }}
            >
              <div style={{ 
                display: 'flex', 
                justifyContent: 'space-between', 
                alignItems: 'center',
                width: '100%'
              }}>
                <span>{item.title}</span>
                <Space>
                  {item.content}
                  {item.arrow && <RightOutlined style={{ color: '#bfbfbf' }} />}
                </Space>
              </div>
            </List.Item>
          ))}
        </List>
      </Card>

      <Modal
        title="修改激活码"
        open={showActivation}
        onOk={handleActivate}
        onCancel={() => {
          setShowActivation(false);
          setActivationCode('');
        }}
      >
        <Input
          placeholder="请输入激活码"
          value={activationCode}
          onChange={e => setActivationCode(e.target.value)}
        />
      </Modal>
    </>
  );
};

export default SzdtDashboard; 
