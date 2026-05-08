import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Card, Spin, Button, InputNumber, Form, Descriptions, Statistic, Row, Col, Typography, Table, Tag } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import dayjs from 'dayjs';
import { calculateSupportResistanceValues } from '../utils/klines'

const { Text } = Typography;


const ETFDetail = () => {
  const { symbol } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [klines, setKlines] = useState([]);
  const [report, setReport] = useState(null);
  const [reportHistory, setReportHistory] = useState([]);
  const [components, setComponents] = useState([]);

  useEffect(() => {
    fetchKlines();
    fetchReport();
    fetchReportHistory();
    fetchComponents();
  }, [symbol]);

  const fetchReport = async () => {
    try {
      const { data } = await request.get(`/api/etf/reports/${symbol}`);
      setReport(data);
    } catch (error) {
      console.error('获取ETF报告失败:', error);
    }
  };

  const fetchKlines = async () => {
    setLoading(true);
    try {
      const { data } = await request.get(`/api/stock/klines/${symbol}`, {
        params: {
          start_date: dayjs().subtract(365, 'day').format('YYYY-MM-DD'),
          end_date: dayjs().format('YYYY-MM-DD')
        }
      });
      setKlines(data);
    } catch (error) {
      console.error('获取K线数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const fetchReportHistory = async () => {
    try {
      const { data } = await request.get(`/api/etf/reports/${symbol}/history?days=500`);
      setReportHistory(data || []);
    } catch (error) {
      console.error('获取ETF历史估值失败:', error);
    }
  };

  const fetchComponents = async () => {
    try {
      const { data } = await request.get(`/api/etf/components/${symbol}`);
      setComponents(data);
    } catch (error) {
      console.error('获取成分股信息失败:', error);
    }
  };

  // 2. 添加新的状态变量
  const [supportLevels, setSupportLevels] = useState([]);
  const [resistanceLevels, setResistanceLevels] = useState([]);
  const [days, setDays] = useState(200);
  const [volumeRatio, setVolumeRatio] = useState(2);

  // 3. 添加计算支撑压力位
  useEffect(() => {
    if (klines.length > 0 && volumeRatio > 1 && days > 1) {
      const { supports, resistances } = calculateSupportResistanceValues(klines, days, volumeRatio);
      setSupportLevels(supports);
      setResistanceLevels(resistances);
    }
  }, [days, volumeRatio, klines]);

  // 4. 修改 getChartOption 函数
  const getChartOption = () => {
    const dates = klines.map(item => dayjs(item.timestamp).format('YYYY-MM-DD'));
    const toFiniteNumber = (value) => {
      if (value === null || value === undefined) return null;
      const num = Number(value);
      return Number.isFinite(num) ? num : null;
    };
    const sortedReportHistory = [...reportHistory]
      .map(item => ({
        ...item,
        date: dayjs(item.date).format('YYYY-MM-DD'),
        fair_value_lo: toFiniteNumber(item.fair_value_lo),
        fair_value_hi: toFiniteNumber(item.fair_value_hi)
      }))
      .sort((a, b) => a.date.localeCompare(b.date));
    const fairValueLoData = [];
    const fairValueHiData = [];
    let historyIndex = 0;
    let latestHistory = null;

    dates.forEach(dateStr => {
      while (
        historyIndex < sortedReportHistory.length &&
        sortedReportHistory[historyIndex].date <= dateStr
      ) {
        latestHistory = sortedReportHistory[historyIndex];
        historyIndex += 1;
      }
      fairValueLoData.push(latestHistory?.fair_value_lo ?? null);
      fairValueHiData.push(latestHistory?.fair_value_hi ?? null);
    });
    const hasFairValueLo = fairValueLoData.some(value => value !== null);
    const hasFairValueHi = fairValueHiData.some(value => value !== null);
    const klineData = klines.map(item => [
      item.open,
      item.close,
      item.low,
      item.high,
      item.volume
    ]);

    const series = [{
      name: 'K线',
      type: 'candlestick',
      data: klineData.map(item => [
        item[0],
        item[1],
        item[2],
        item[3]
      ]),
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
      data: klineData.map(item => item[4]),
      itemStyle: {
        color: (params) => {
          const kline = klineData[params.dataIndex];
          return kline[1] > kline[0] ? '#ef232a' : '#14b143';
        }
      }
    }];

    if (hasFairValueLo) {
      series.push({
        name: '估值下限',
        type: 'line',
        data: fairValueLoData,
        symbol: 'none',
        lineStyle: {
          width: 2,
          color: '#0066FF',
          type: 'dashed'
        }
      });
    }

    if (hasFairValueHi) {
      series.push({
        name: '估值上限',
        type: 'line',
        data: fairValueHiData,
        symbol: 'none',
        lineStyle: {
          width: 2,
          color: '#FF0000',
          type: 'dashed'
        }
      });
    }

    // 添加支撑位线
    supportLevels.forEach((level) => {
      series.push({
        name: `支撑位${level}`,
        type: 'line',
        data: Array(dates.length).fill(level),
        lineStyle: {
          color: '#00FF00',
          type: 'dashed',
          opacity: 0.5
        },
        symbol: 'none'
      });
    });

    // 添加压力位线
    resistanceLevels.forEach((level) => {
      series.push({
        name: `压力位${level}`,
        type: 'line',
        data: Array(dates.length).fill(level),
        lineStyle: {
          color: '#FF0000',
          type: 'dashed',
          opacity: 0.5
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
        data: ['K线', '成交量',
          ...(hasFairValueLo ? ['估值下限'] : []),
          ...(hasFairValueHi ? ['估值上限'] : []),
          ...supportLevels.map(v => `支撑位${v}`),
          ...resistanceLevels.map(v => `压力位${v}`)
        ]
      },
      grid: [{
        left: '5%',
        right: '5%',
        height: '60%'
      }, {
        left: '5%',
        right: '5%',
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
        splitArea: { show: true }
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
        top: '97%',
        start: 0,
        end: 100
      }],
      series: series
    };
  };

  // 响应式布局配置
  const isMobile = window.innerWidth <= window.innerHeight;

  // 格式化数字显示
  const formatNumber = (num) => {
    if (!num) return '-';
    return num.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  };

  // 格式化百分比显示
  const formatPercent = (value) => {
    if (!value && value !== 0) return '-';
    return `${(value * 100).toFixed(2)}%`;
  };

  // 格式化涨跌幅显示（带颜色）
  const formatChange = (value, base) => {
    if (!value || !base) return '-';
    const percent = ((value - base) / base * 100).toFixed(2);
    const color = percent >= 0 ? '#cf1322' : '#3f8600';
    return <Text style={{ color }}>{percent}%</Text>;
  };

  // 判断股票估值状态
  const getValuationStatus = (record) => {
    if (!record.last_price || !record.fair_value_lo || !record.fair_value_hi) {
      return 'normal';
    }
    if (record.last_price < record.fair_value_lo) {
      return 'undervalued';
    }
    if (record.last_price > record.fair_value_hi) {
      return 'overvalued';
    }
    return 'normal';
  };

  // 成分股表格列定义
  const componentColumns = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 100,
      fixed: 'left',
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 150
    },
    {
      title: '类型',
      dataIndex: 'asset_class',
      width: 50,
      render: (text) => (
        <Tag color={text === 'Equity' ? 'blue' : 'green'}>
          {text === 'Equity' ? '股票' : '其他'}
        </Tag>
      ),
    },
    {
      title: '权重',
      dataIndex: 'weight',
      width: 80,
      render: formatPercent,
      sorter: (a, b) => a.weight - b.weight,
    },
    {
      title: '最新价',
      dataIndex: 'last_price',
      width: 80,
      render: (value) => value?.toFixed(2) || '-',
    },
    {
      title: '当前估值',
      children: [
        {
          title: '下限',
          dataIndex: 'fair_value_lo',
          width: 80,
          render: (value) => value?.toFixed(2) || '-',
        },
        {
          title: '上限',
          dataIndex: 'fair_value_hi',
          width: 80,
          render: (value) => value?.toFixed(2) || '-',
        },
      ],
    },
    {
      title: '下财年估值',
      children: [
        {
          title: '下限',
          dataIndex: 'forward_next_fy_lo',
          width: 80,
          render: (value) => value?.toFixed(2) || '-',
        },
        {
          title: '上限',
          dataIndex: 'forward_next_fy_hi',
          width: 80,
          render: (value) => value?.toFixed(2) || '-',
        },
      ],
    },
    {
      title: 'PE',
      dataIndex: 'pe_ratio',
      width: 60,
      render: (value) => value?.toFixed(1) || '-',
    },
    {
      title: '前瞻PE',
      dataIndex: 'forward_pe_ratio',
      width: 60,
      render: (value) => value?.toFixed(1) || '-',
    },
  ];

  if (loading) {
    return <Spin size="large" />;
  }

  return (
    <div style={{ padding: '6px' }}>
      <Card
        title={
          <div style={{ display: 'flex', alignItems: 'center' }}>
            <Button
              type="text"
              icon={<LeftOutlined />}
              onClick={() => navigate('/')}
              style={{ marginRight: '12px' }}
            />
            <span>{symbol} {report?.name}</span>
          </div>
        }
        bodyStyle={{ padding: '6px' }}
      >
        {report && (
          <>
            {/* 基础统计信息 */}
            <Row gutter={[16, 16]}>
              <Col xs={8} sm={6}>
                <Card size="small">
                  <Statistic
                    title="市场价格"
                    value={report.market_price || report.current_price}
                    precision={2}
                    valueStyle={{ fontSize: '16px' }}
                  />
                </Card>
              </Col>
              <Col xs={8} sm={6}>
                <Card size="small">
                  <Statistic
                    title="总市值"
                    value={report.total_market_value / 100000000}
                    precision={2}
                    suffix="亿"
                    valueStyle={{ fontSize: '16px' }}
                  />
                </Card>
              </Col>
              <Col xs={8} sm={6}>
                <Card size="small">
                  <Statistic
                    title="总发行量"
                    value={report.total_shares / 100000000}
                    precision={2}
                    suffix="亿"
                    valueStyle={{ fontSize: '16px' }}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={6}>
                <Card size="small">
                  <Row gutter={[8, 0]}>
                    <Col span={12}>
                      <Statistic
                        title="PE"
                        value={report.market_price && report.eps ? (report.market_price / report.eps).toFixed(2) : '-'}
                        valueStyle={{ fontSize: '16px' }}
                      />
                    </Col>
                    <Col span={12}>
                      <Statistic
                        title="前瞻PE"
                        value={report.market_price && report.eps_forward ? (report.market_price / report.eps_forward).toFixed(2) : '-'}
                        valueStyle={{ fontSize: '16px' }}
                      />
                    </Col>
                  </Row>
                </Card>
              </Col>
            </Row>

            {/* 估值信息 */}
            <Card
              size="small"
              style={{ marginTop: '16px' }}
              bodyStyle={{ padding: '6px' }}
            >
              <Row gutter={[16, 16]}>
                <Col xs={12} md={6}>
                  <Card size="small" title="当前估值">
                    <div style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: '18px', marginBottom: '4px' }}>
                        {formatNumber(report.fair_value_lo)} ~ {formatNumber(report.fair_value_hi)}
                      </div>
                      <div>
                        <Text type="secondary">
                          {formatChange(report.fair_value_lo, report.market_price)} ~ {formatChange(report.fair_value_hi, report.market_price)}
                        </Text>
                      </div>
                    </div>
                  </Card>
                </Col>
                <Col xs={12} md={6}>
                  <Card size="small" title="下财年估值">
                    <div style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: '18px', marginBottom: '4px' }}>
                        {formatNumber(report.forward_next_fy_lo)} ~ {formatNumber(report.forward_next_fy_hi)}
                      </div>
                      <div>
                        <Text type="secondary">
                          {formatChange(report.forward_next_fy_lo, report.market_price)} ~ {formatChange(report.forward_next_fy_hi, report.market_price)}
                        </Text>
                      </div>
                    </div>
                  </Card>
                </Col>
                <Col xs={12} sm={6}>
                  <Card size="small" title="有估值成分股权重" >
                    <Statistic
                      value={report.forward_stocks_weight * 100}
                      precision={2}
                      suffix="%"
                    />
                  </Card>
                </Col>
                <Col xs={12} sm={6}>
                  <Card size="small" title="估值日期范围">
                    <div style={{ textAlign: 'center', fontSize: '12px' }}>
                      {report.min_fair_value_date?.split('T')[0]}~
                      {report.max_fair_value_date?.split('T')[0]}
                    </div>
                  </Card>
                </Col>
                {report.leveraged_symbol && (
                  <>
                    <Col xs={12} sm={8}>
                      <Card size="small">
                        <Statistic
                          title={`${report.leveraged_symbol}价格`}
                          value={report.leveraged_price}
                          precision={2}
                        />
                      </Card>
                    </Col>
                    <Col xs={12} sm={8}>
                      <Card size="small">
                        <Statistic
                          title="情绪指数"
                          value={report.leveraged_szdt_score}
                          color={report.leveraged_szdt_score >= 60 ? '#cf1322' : report.leveraged_szdt_score <= -60 ? '#3f8600' : ''}
                          precision={0}
                        />
                      </Card>
                    </Col>
                    <Col xs={24} sm={8}>
                      <Card size="small">
                        <Statistic
                          title="情绪指数更新时间"
                          value={report.leveraged_szdt_update_time}
                          valueStyle={{ fontSize: '12px' }}
                        />
                      </Card>
                    </Col>
                  </>
                )}
              </Row>
            </Card>
          </>
        )}
      </Card>
      {/*  支撑压力阈值参数设置 */}
      <Card
        size="small"
        style={{ marginTop: '16px' }}
        bodyStyle={{ padding: '6px' }}
      >
        <Form layout="inline" style={{ paddingLeft: '6px' }}>
          <Form.Item label="计算天数">
            <InputNumber
              min={1}
              max={300}
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
      </Card>
      {/* K线图表 */}
      <Card
        size="small"
        style={{ marginTop: '16px' }}
        bodyStyle={{ padding: '4px' }}
      >
        <ReactECharts
          option={getChartOption()}
          notMerge={true}
          key={[
            supportLevels.join(','),
            resistanceLevels.join(','),
            reportHistory.length,
            reportHistory[reportHistory.length - 1]?.date ?? ''
          ].join('|')}
          style={{ height: '400px' }}
        />
      </Card>

      {/* 成分股信息表格 */}
      <Card
        title="成分股信息"
        size="small"
        style={{ marginTop: '16px' }}
        bodyStyle={{ padding: '4px' }}
      >
        <Table
          dataSource={components}
          columns={componentColumns}
          rowKey="symbol"
          size="small"
          scroll={{ x: 'max-content' }}
          pagination={{
            pageSize: 20,
            showSizeChanger: false,
          }}
          rowClassName={(record) => {
            const status = getValuationStatus(record);
            if (status === 'undervalued') return 'row-undervalued';
            if (status === 'overvalued') return 'row-overvalued';
            return '';
          }}
        />
      </Card>
    </div>
  );
};

// 添加到组件的 CSS 样式
const styles = `
  .row-undervalued {
    background-color: rgba(24, 144, 255, 0.1);  // 低估用淡蓝色
  }
  .row-overvalued {
    background-color: rgba(255, 77, 79, 0.1);   // 高估用淡红色
  }
  .row-undervalued:hover,
  .row-overvalued:hover {
    background-color: rgba(0, 0, 0, 0.05) !important;  // 保持hover效果
  }
`;

// 将样式添加到 document 中
if (typeof document !== 'undefined') {
  const styleElement = document.createElement('style');
  styleElement.innerHTML = styles;
  document.head.appendChild(styleElement);
}

export default ETFDetail;
