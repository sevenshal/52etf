import React, { useState, useEffect } from 'react';
import { Row, Col, Statistic, message } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../../utils/request';
import { formatQuarter } from '../utils';

const FearGreedAiae = () => {
  const [aiaeData, setAiaeData] = useState(null);

  useEffect(() => {
    fetchAiaeData();
  }, []);

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

  const renderChart = () => {
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

  if (!aiaeData) {
    return <div style={{ textAlign: 'center', padding: '20px' }}>加载中...</div>;
  }

  return (
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
      {renderChart()}
    </>
  );
};

export default FearGreedAiae;
