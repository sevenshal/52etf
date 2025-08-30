import React, { useState } from 'react';
import { Card, Input, DatePicker, Select, Button, Table, Statistic, Row, Col, message } from 'antd';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';

const MonthlyAnalysis = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [summary, setSummary] = useState(null);
  const [timeRange, setTimeRange] = useState(null);
  
  const [formData, setFormData] = useState({
    symbol: 'QQQ.US',
    startDate: '2005-01-01',
    confidenceLevel: 0.95
  });

  // 置信度选项
  const confidenceOptions = [
    { label: '99%', value: 0.99 },
    { label: '95%', value: 0.95 },
    { label: '90%', value: 0.90 },
    { label: '80%', value: 0.80 }
  ];

  // 计算显著性检验（t检验）
  const calculateSignificance = (values, testValue = 0) => {
    if (values.length === 0) return 1;
    
    const n = values.length;
    const mean = values.reduce((sum, val) => sum + val, 0) / n;
    const variance = values.reduce((sum, val) => sum + Math.pow(val - mean, 2), 0) / (n - 1);
    const stdError = Math.sqrt(variance / n);
    
    if (stdError === 0) return 1;
    
    const tStat = Math.abs(mean - testValue) / stdError;
    
    // 计算p值（使用正态分布近似）
    if (tStat > 2.58) return 0.01;
    if (tStat > 1.96) return 0.05;
    if (tStat > 1.645) return 0.1;
    if (tStat > 1.28) return 0.2;
    return 0.3;
  };

  // 计算涨跌数显著性（卡方检验）
  const calculateDirectionSignificance = (upCount, downCount, totalUp, totalDown) => {
    const total = upCount + downCount;
    if (total === 0) return 1;
    
    // 使用总体涨跌比例计算期望值
    const grandTotal = totalUp + totalDown;
    if (grandTotal === 0) return 1;
    
    const upProbability = totalUp / grandTotal;
    const downProbability = totalDown / grandTotal;
    
    const expectedUp = total * upProbability;
    const expectedDown = total * downProbability;
    
    // 避免除零错误
    if (expectedUp === 0 || expectedDown === 0) return 1;
    
    const chiSquare = Math.pow(upCount - expectedUp, 2) / expectedUp + Math.pow(downCount - expectedDown, 2) / expectedDown;
    
    // 计算p值（卡方分布，自由度=1）
    if (chiSquare > 6.635) return 0.01;
    if (chiSquare > 3.841) return 0.05;
    if (chiSquare > 2.706) return 0.1;
    if (chiSquare > 1.642) return 0.2;
    return 0.3;
  };

  const handleAnalyze = async () => {
    setLoading(true);
    try {
      // 计算从开始日期到现在的天数
      const startDate = new Date(formData.startDate);
      const endDate = new Date();
      const days = Math.ceil((endDate - startDate) / (1000 * 60 * 60 * 24));
      
      // 获取月度K线数据
      const response = await request.get(`/api/stock/klines/${formData.symbol}`, {
        params: {
          days: days,
          period: 'm'
        }
      });
      
      const klines = response.data;
      
      // 先处理pre_close值
      const processedKlines = klines.map((kline, index) => {
        if (index === 0) {
          // 第一条数据，使用open作为pre_close
          return { ...kline, pre_close: kline.open };
        } else {
          // 使用前一条数据的close作为pre_close
          return { ...kline, pre_close: klines[index - 1].close };
        }
      });
      
      // 过滤数据：筛选用户选择的开始时间后的数据，并删除今年的数据
      const currentYear = new Date().getFullYear();
      const filteredKlines = processedKlines.filter(kline => {
        const klineDate = new Date(kline.timestamp);
        const klineYear = klineDate.getFullYear();
        
        // 筛选条件：在用户选择的开始时间之后，且不是今年
        return kline.timestamp >= formData.startDate && klineYear < currentYear;
      });
      
      // 计算时间范围
      if (filteredKlines.length > 0) {
        const firstDate = filteredKlines[0].timestamp.substring(0, 7);
        const lastDate = filteredKlines[filteredKlines.length - 1].timestamp.substring(0, 7);
        setTimeRange({
          start: firstDate,
          end: lastDate
        });
      }
      
      // 按月份分组数据
      const monthlyData = {};
      filteredKlines.forEach(kline => {
        const date = new Date(kline.timestamp);
        const month = date.getMonth() + 1; // 1-12月
        
        if (!monthlyData[month]) {
          monthlyData[month] = [];
        }
        
        const changeRate = (kline.close / kline.pre_close) - 1;
        monthlyData[month].push({
          changeRate,
          isUp: changeRate > 0
        });
      });
      
       // 先计算总体涨跌数
       let totalUp = 0;
       let totalDown = 0;
       const allChangeRates = [];
       
       for (let month = 1; month <= 12; month++) {
         const monthData = monthlyData[month] || [];
         const upCount = monthData.filter(d => d.isUp).length;
         const downCount = monthData.filter(d => !d.isUp).length;
         totalUp += upCount;
         totalDown += downCount;
         allChangeRates.push(...monthData.map(d => d.changeRate));
       }
       
       // 计算每个月的统计信息
       const tableData = [];

       // 计算总体统计
      const overallAvgRate = allChangeRates.length > 0 ? allChangeRates.reduce((sum, rate) => sum + rate, 0) / allChangeRates.length : 0;
      
       for (let month = 1; month <= 12; month++) {
         const monthData = monthlyData[month] || [];
         const changeRates = monthData.map(d => d.changeRate);
         const upCount = monthData.filter(d => d.isUp).length;
         const downCount = monthData.filter(d => !d.isUp).length;
         
         const avgChangeRate = changeRates.length > 0 ? changeRates.reduce((sum, rate) => sum + rate, 0) / changeRates.length : 0;
         const directionSignificance = calculateDirectionSignificance(upCount, downCount, totalUp, totalDown);
         const rateSignificance = calculateSignificance(changeRates, 0);
         
         // 根据置信度水平确定显著性阈值
         const alpha = Math.round((1 - formData.confidenceLevel) * 100) / 100;
         
         // 调试信息（可以删除）
         if (month === 1) {
           console.log(`1月份数据: 涨${upCount}次, 跌${downCount}次`);
           console.log(`总体数据: 涨${totalUp}次, 跌${totalDown}次`);
           console.log(`1月份涨跌显著性p值: ${directionSignificance}`);
           console.log(`当前置信度: ${formData.confidenceLevel}, alpha: ${alpha}`);
         }
         
         // 计算幅度显著性（区分正负）
         let rateSignificanceText = '不显著';
         if (rateSignificance <= alpha) {
           rateSignificanceText = avgChangeRate > overallAvgRate ? '正显著' : '负显著';
         }
         
         // 计算涨跌显著性（区分正负）
         let directionSignificanceText = '不显著';
         if (directionSignificance <= alpha) {
           const expectedUp = (upCount + downCount) * (totalUp / (totalUp + totalDown));
           directionSignificanceText = upCount > expectedUp ? '正显著' : '负显著';
         }
         
         tableData.push({
           key: month,
           month: `${month}月`,
           changeRate: (avgChangeRate * 100).toFixed(2) + '%',
           upCount: upCount,
           downCount: downCount,
           rateSignificance: rateSignificanceText,
           directionSignificance: directionSignificanceText
         });
       }
      
      
      setData(tableData);
      setSummary({
        overallAvgRate: (overallAvgRate * 100).toFixed(2) + '%',
        totalUp,
        totalDown
      });
      
    } catch (error) {
      console.error('分析失败:', error);
      message.error('分析失败：' + (error.response?.data?.detail || error.message));
    } finally {
      setLoading(false);
    }
  };

  const columns = [
    {
      title: '月份',
      dataIndex: 'month',
      key: 'month',
      width: 80
    },
    {
      title: '涨跌幅',
      dataIndex: 'changeRate',
      key: 'changeRate',
      width: 100,
      render: (text) => {
        const value = parseFloat(text);
        return <span style={{ color: value >= 0 ? '#52c41a' : '#ff4d4f' }}>{text}</span>;
      }
    },
    {
      title: '涨数',
      dataIndex: 'upCount',
      key: 'upCount',
      width: 80,
      render: (text) => <span style={{ color: '#52c41a' }}>{text}</span>
    },
    {
      title: '跌数',
      dataIndex: 'downCount',
      key: 'downCount',
      width: 80,
      render: (text) => <span style={{ color: '#ff4d4f' }}>{text}</span>
    },
    {
      title: '幅度显著性',
      dataIndex: 'rateSignificance',
      key: 'rateSignificance',
      width: 120,
      render: (text) => {
        let color = '#999'; // 不显著
        if (text === '正显著') color = '#52c41a'; // 绿色
        if (text === '负显著') color = '#ff4d4f'; // 红色
        return <span style={{ color }}>{text}</span>;
      }
    },
    {
      title: '涨跌显著性',
      dataIndex: 'directionSignificance',
      key: 'directionSignificance',
      width: 120,
      render: (text) => {
        let color = '#999'; // 不显著
        if (text === '正显著') color = '#52c41a'; // 绿色
        if (text === '负显著') color = '#ff4d4f'; // 红色
        return <span style={{ color }}>{text}</span>;
      }
    }
  ];

  return (
    <div style={{ padding: '24px' }}>
      <Card title="历史每月分析">
        <div style={{ marginBottom: '24px' }}>
          <Row gutter={16}>
            <Col span={8}>
              <div style={{ marginBottom: '8px' }}>股票代码:</div>
              <Input
                placeholder="例如 QQQ.US"
                value={formData.symbol}
                onChange={(e) => setFormData({ ...formData, symbol: e.target.value })}
              />
            </Col>
            <Col span={8}>
              <div style={{ marginBottom: '8px' }}>开始时间:</div>
              <DatePicker
                style={{ width: '100%' }}
                value={formData.startDate ? dayjs(formData.startDate) : dayjs('2005-01-01')}
                onChange={(date) => setFormData({ 
                  ...formData, 
                  startDate: date ? date.format('YYYY-MM-DD') : '2005-01-01' 
                })}
              />
            </Col>
            <Col span={8}>
              <div style={{ marginBottom: '8px' }}>置信度:</div>
              <Select
                style={{ width: '100%' }}
                value={formData.confidenceLevel}
                onChange={(value) => setFormData({ ...formData, confidenceLevel: value })}
                options={confidenceOptions}
              />
            </Col>

          </Row>
          <Button 
            type="primary" 
            onClick={handleAnalyze} 
            loading={loading}
            style={{ marginTop: '16px' }}
          >
            立即分析
          </Button>
        </div>

        {data && (
          <>
            <Table
              columns={columns}
              dataSource={data}
              pagination={false}
              size="small"
              style={{ marginBottom: '24px' }}
            />
            
            {summary && (
              <>
                <Row gutter={16} style={{ marginBottom: '16px' }}>
                  <Col span={8}>
                    <Statistic
                      title="所有月平均涨跌幅"
                      value={summary.overallAvgRate}
                      valueStyle={{ color: parseFloat(summary.overallAvgRate) >= 0 ? '#52c41a' : '#ff4d4f' }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="上涨总数"
                      value={summary.totalUp}
                      valueStyle={{ color: '#52c41a' }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="下跌总数"
                      value={summary.totalDown}
                      valueStyle={{ color: '#ff4d4f' }}
                    />
                  </Col>
                </Row>
                
                {timeRange && (
                  <Row>
                    <Col span={24}>
                      <div style={{ 
                        textAlign: 'center', 
                        color: '#666', 
                        fontSize: '14px',
                        padding: '8px 0',
                        borderTop: '1px solid #f0f0f0'
                      }}>
                        数据时间范围：{timeRange.start} 至 {timeRange.end}
                      </div>
                    </Col>
                  </Row>
                )}
              </>
            )}
          </>
        )}
      </Card>
    </div>
  );
};

export default MonthlyAnalysis; 