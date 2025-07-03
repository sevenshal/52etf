import React, { useState, useEffect } from 'react';
import { Card, Row, Col, Statistic, Table, Spin, Empty, Tag, Typography } from 'antd';
import request from '../utils/request';
import dayjs from 'dayjs'
import { useNavigate } from 'react-router-dom';

const { Title, Text } = Typography;

const ETFReport = () => {
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    fetchData();
  }, []);

  const fetchData = async () => {
    try {
      // 并行获取ETF报告和贪恐指数数据
      const [reportsResponse, emotionResponse] = await Promise.all([
        request.get('/api/etf/reports'),
        request.get('/api/quant/etf/emotion/1')
      ]);

      const reportsData = reportsResponse.data;
      const emotionData = emotionResponse.data?.data || [];

      // 创建贪恐指数数据的映射
      const emotionMap = new Map(
        emotionData.map(item => [item.code, item])
      );

      // 合并数据
      const mergedReports = reportsData.map(report => {
        // 转换股票代码格式：SPY.US -> US.SPY
        const convertedSymbol = report.leveraged_symbol?.split('.').reverse().join('.');
        const emotion = emotionMap.get(convertedSymbol);
        return {
          ...report,
          leveraged_szdt_score: emotion?.emotion?.score || null,
          leveraged_szdt_update_time: emotion?.emotion?.updated_at || null
        };
      });

      setReports(mergedReports);
    } catch (error) {
      console.error('获取数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  const formatValue = (value) => {
    if (value === null || value === undefined) return '-';
    return value.toFixed(2);
  };

  const calculateChange = (value, marketPrice) => {
    if (!value || !marketPrice) return null;
    return ((value - marketPrice) / marketPrice * 100).toFixed(2);
  };

  const renderChangeValue = (value, marketPrice) => {
    const change = calculateChange(value, marketPrice);
    if (!change) return null;
    
    const color = change > 0 ? '#52c41a' : '#f5222d';
    return (
      <Text style={{ marginLeft: 4, color }}>
        {change > 0 ? '+' : ''}{change}%
      </Text>
    );
  };

  const getValueColor = (current, low, high) => {
    if (!current || !low || !high) return '';
    if (current < low) return '#52c41a';  // 低估 - 绿色
    if (current > high) return '#f5222d';  // 高估 - 红色
    return '#faad14';  // 合理 - 黄色
  };

  const renderETFCard = (report) => {
    const valueColor = getValueColor(
      report.market_price,
      report.fair_value_lo,
      report.fair_value_hi
    );

    const leveragedScoreValueColor = getValueColor(
      report.leveraged_szdt_score,
      -60,
      60
    );

    // 计算 PE、PE Forward、PE V2 和 PE TTM
    const pe = report.eps && report.market_price ? (report.market_price / report.eps).toFixed(2) : null;
    const pe_forward = report.eps_forward && report.market_price ? (report.market_price / report.eps_forward).toFixed(2) : null;
    const pe_v2 = report.eps_v2 && report.market_price ? (report.market_price / report.eps_v2).toFixed(2) : null;
    const pe_ttm = report.eps_ttm && report.market_price ? (report.market_price / report.eps_ttm).toFixed(2) : null;

    // Determine card background color based on valuation
    let cardBackgroundColor = '';
    if (report.current_price > report.fair_value_hi) {
      cardBackgroundColor = '#ffe6e6'; // 淡红色
    } else if (report.current_price < report.fair_value_lo) {
      cardBackgroundColor = '#e6f7ff'; // 淡蓝色
    }

    return (
      <Col xs={24} sm={12} md={8} lg={6} key={report.symbol}>
        <Card
          title={
            <div>
              <Title level={4}>{report.name}</Title>
              <Text type="secondary">{report.symbol}</Text>
              {report.leveraged_symbol && (
                <Text type="secondary" style={{ marginLeft: 4 }}>
                  {report.leveraged_symbol}
                </Text>
              )}
            </div>
          }
          style={{ marginBottom: 8, cursor: 'pointer', backgroundColor: cardBackgroundColor }}
          onClick={() => navigate(`/etf/${report.symbol}`)}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Statistic
                title="市场价格"
                value={report.market_price || '-'}
                precision={2}
                prefix="$"
                valueStyle={{ color: valueColor }}
              />
            </Col>
            <Col span={12}>
              <Statistic
                title="加权价格(权重)"
                value={report.current_price}
                precision={2}
                suffix={<Text style={{ color: '#8c8c8c', fontSize: '12px', marginLeft: 4 }}>${(report.forward_stocks_weight*100).toFixed(1)}%</Text>}
                prefix="$"
              />
            </Col>
          </Row>

          {/* 添加 PE 相关行 */}
          <Row gutter={16} style={{ marginTop: 8 }}>
            <Col span={12}>
              <Statistic
                title="PE"
                value={pe || '-'}
                precision={2}
              />
            </Col>
            <Col span={12}>
              <Statistic
                title="前瞻PE"
                value={pe_forward || '-'}
                precision={2}
              />
            </Col>
          </Row>

          {/* 新增 PE V2 和 PE TTM 行 */}
          <Row gutter={16} style={{ marginTop: 8 }}>
            <Col span={12}>
              <Statistic
                title="PE V2"
                value={pe_v2 || '-'}
                precision={2}
              />
            </Col>
            <Col span={12}>
              <Statistic
                title="PE TTM"
                value={pe_ttm || '-'}
                precision={2}
              />
            </Col>
          </Row>

          {report.leveraged_symbol && (
            <Row gutter={16}>
              <Col span={12}>
                <Statistic
                  title={`${report.leveraged_symbol} 价格`}
                  value={report.leveraged_price || '-'}
                  precision={2}
                  prefix="$"
                />
              </Col>
              <Col span={12}>
                <Statistic
                  title="贪恐指数"
                  value={report.leveraged_szdt_score || '-'}
                  valueStyle={{ color: leveragedScoreValueColor }}
                />
              </Col>
            </Row>
          )}
          <Row gutter={16} style={{ marginTop: 8 }}>
            <Col span={12}>
              <Statistic
                title="估值下限"
                value={formatValue(report.fair_value_lo)}
                prefix="$"
                precision={2}
                suffix={renderChangeValue(report.fair_value_lo, report.market_price)}
              />
            </Col>
            <Col span={12}>
              <Statistic
                title="估值上限"
                value={formatValue(report.fair_value_hi)}
                prefix="$"
                precision={2}
                suffix={renderChangeValue(report.fair_value_hi, report.market_price)}
              />
            </Col>
          </Row>
          <Row gutter={16} style={{ marginTop: 8 }}>
            <Col span={12}>
              <Statistic
                title="下财年下限"
                value={formatValue(report.forward_next_fy_lo)}
                prefix="$"
                precision={2}
                suffix={renderChangeValue(report.forward_next_fy_lo, report.market_price)}
              />
            </Col>
            <Col span={12}>
              <Statistic
                title="下财年上限"
                value={formatValue(report.forward_next_fy_hi)}
                prefix="$"
                precision={2}
                suffix={renderChangeValue(report.forward_next_fy_hi, report.market_price)}
              />
            </Col>
          </Row>
          <div style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: '12px' }}>
              更新时间,持仓:{report.update_date} 报告:{dayjs(report.updated_at).format('MM-DD HH:mm')}
            </Text><br/>
            <Text type="secondary" style={{ fontSize: '12px' }}>
              估值日期:{report.min_fair_value_date} - {report.max_fair_value_date}
            </Text><br/>
            <Text type="secondary" style={{ fontSize: '12px' }}>
              贪恐日期:{report.leveraged_szdt_update_time || '-'}
            </Text>
          </div>
        </Card>
      </Col>
    );
  };

  if (loading) {
    return <Spin size="large" />;
  }

  if (!reports.length) {
    return <Empty description="暂无ETF分析报告" />;
  }

  return (
    <div style={{ padding: '6px' }}>
      <Row gutter={16}>
        {reports.map(renderETFCard)}
      </Row>
    </div>
  );
};

export default ETFReport;
