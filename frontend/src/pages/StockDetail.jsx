import React, { useCallback, useMemo, useState, useEffect } from 'react';
import { Card, Button, Descriptions, Table, Tag, Popover } from 'antd';
import { LeftOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { useParams, useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';
import { formatNumber } from '../utils/format';
import { computeStockWindowMetrics, STOCK_METRIC_WINDOWS } from '../utils/stockMetrics';
import StockKlineChart from '../components/StockKlineChart';

const FIVE_YEAR_TRADING_BARS = 1260;

const formatPercent = (value, digits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Number(value).toFixed(digits)}%`;
};

const formatSignedPercent = (value, digits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  const num = Number(value);
  return `${num >= 0 ? '+' : ''}${num.toFixed(digits)}%`;
};

const renderPercentileTag = (value, higherIsBetter = true) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '-';
  const num = Number(value);
  const color = higherIsBetter
    ? (num >= 80 ? 'green' : num >= 50 ? 'gold' : 'red')
    : (num >= 80 ? 'red' : num >= 50 ? 'gold' : 'green');
  return <Tag color={color}>{`${num.toFixed(1)}%`}</Tag>;
};

const renderMetricTitle = (title, content) => (
  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
    <span>{title}</span>
    <Popover
      trigger="click"
      placement="topLeft"
      content={<div style={{ maxWidth: 320, lineHeight: 1.6 }}>{content}</div>}
    >
      <InfoCircleOutlined
        onClick={(e) => e.stopPropagation()}
        style={{ color: '#8c8c8c', cursor: 'pointer', fontSize: 12 }}
      />
    </Popover>
  </span>
);

const StockDetail = () => {
  const { symbol } = useParams();
  const navigate = useNavigate();
  const normalizedSymbol = useMemo(() => (symbol || '').toUpperCase(), [symbol]);
  const isAStock = useMemo(() => /\.(SH|SZ|BJ)$/.test(normalizedSymbol), [normalizedSymbol]);
  const [klines, setKlines] = useState([]);
  const [evcHistory, setEvcHistory] = useState([]);
  const fetchEvcHistory = useCallback(async () => {
    try {
      const historyUrl = isAStock
        ? `/api/evc/a-stock-consensus/history/${normalizedSymbol}?limit=${FIVE_YEAR_TRADING_BARS}`
        : `/api/evc/stock-evc/history/${normalizedSymbol}?limit=${FIVE_YEAR_TRADING_BARS}`;
      const { data } = await request.get(historyUrl);
      setEvcHistory(data || []);
    } catch (error) {
      console.error('获取估值历史失败:', error);
    }
  }, [isAStock, normalizedSymbol]);

  const stockMetrics = useMemo(() => computeStockWindowMetrics(klines, STOCK_METRIC_WINDOWS), [klines]);
  const latestSnapshot = stockMetrics.latest;
  const latestEvcSnapshot = useMemo(() => {
    if (!evcHistory.length) return null;
    return [...evcHistory].sort((a, b) => new Date(b.date) - new Date(a.date))[0] || null;
  }, [evcHistory]);
  const metricRows = stockMetrics.rows;
  const metricColumns = useMemo(() => ([
    {
      title: '窗口',
      dataIndex: 'window',
      width: 90,
      render: value => `${value}日`,
    },
    {
      title: '年化波动',
      dataIndex: 'annualizedVolatility',
      width: 110,
      render: value => formatPercent(value, 2),
    },
    {
      title: '夏普',
      dataIndex: 'sharpeRatio',
      width: 90,
      render: value => formatNumber(value, 2),
    },
    {
      title: 'ATR',
      dataIndex: 'atr',
      width: 100,
      render: value => formatNumber(value, 2),
    },
    {
      title: 'ATRP',
      dataIndex: 'atrp',
      width: 110,
      render: value => formatPercent(value, 2),
    },
    {
      title: renderMetricTitle(
        '回撤深度分位',
        <>
          <div>先算窗口内每一天的回撤深度：当前收盘相对窗口内历史最高收盘回撤了多少。</div>
          <div style={{ marginTop: 6 }}>
            公式：<code>DD_t = 1 - Close_t / Peak_t</code>，其中 <code>Peak_t</code> 是截至当天的窗口内最高收盘。
          </div>
          <div style={{ marginTop: 6 }}>再看“当前回撤深度”在过去同窗口回撤深度序列中的百分位。数值越高，表示当前回撤越深。</div>
        </>
      ),
      dataIndex: 'drawdownPercentile',
      width: 130,
      render: value => renderPercentileTag(value, false),
    },
    {
      title: renderMetricTitle(
        '风险调整动量',
        <>
          <div>用最近 N 日收盘价做 <code>log</code> 线性回归。</div>
          <div style={{ marginTop: 6 }}>先取斜率年化，再乘 <code>R²</code> 得到原始动量分数。</div>
          <div style={{ marginTop: 6 }}>再除以年化波动率，得到风险调整动量。数值越高，说明趋势越强且更稳定。</div>
        </>
      ),
      dataIndex: 'riskAdjustedMomentum',
      width: 120,
      render: value => formatNumber(value, 2),
    },
    {
      title: renderMetricTitle(
        '动量分位',
        <>
          <div>把当前“风险调整动量”放进过去同窗口的滚动动量分布里，看它处在什么位置。</div>
          <div style={{ marginTop: 6 }}>分位越高，表示当前动量比历史上更多时候都要强。</div>
        </>
      ),
      dataIndex: 'momentumPercentile',
      width: 110,
      render: value => renderPercentileTag(value, true),
    },
  ]), []);

  useEffect(() => {
    fetchEvcHistory();
  }, [fetchEvcHistory]);

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
            <span>{normalizedSymbol} 股票详情</span>
          </div>
        }
      >
        <div style={{ marginBottom: 16 }}>
          <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 4 }}>
            <Descriptions.Item label="最新日期">
              {latestSnapshot ? dayjs(latestSnapshot.date).format('YYYY-MM-DD') : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="最新收盘">
              {formatNumber(latestSnapshot?.close, 2)}
            </Descriptions.Item>
            <Descriptions.Item label="日涨跌幅">
              {latestSnapshot?.changePct === null || latestSnapshot?.changePct === undefined ? '-' : (
                <Tag color={latestSnapshot.changePct > 0 ? 'red' : latestSnapshot.changePct < 0 ? 'green' : undefined}>
                  {formatSignedPercent(latestSnapshot.changePct, 2)}
                </Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="成交量">
              {formatNumber(latestSnapshot?.volume, 0)}
            </Descriptions.Item>
            <Descriptions.Item label="成交额">
              {formatNumber(latestSnapshot?.turnover, 0)}
            </Descriptions.Item>
            <Descriptions.Item label="PE">
              {formatNumber(latestEvcSnapshot?.pe_ratio, 2)}
            </Descriptions.Item>
            <Descriptions.Item label={isAStock ? '下一年PE' : 'Forward PE'}>
              {formatNumber(latestEvcSnapshot?.forward_pe_ratio, 2)}
            </Descriptions.Item>
            {isAStock && (
              <Descriptions.Item label="目标价区间">
                {latestEvcSnapshot
                  ? `${formatNumber(latestEvcSnapshot.fair_value_lo, 2)} ~ ${formatNumber(latestEvcSnapshot.fair_value_hi, 2)}`
                  : '-'}
              </Descriptions.Item>
            )}
            <Descriptions.Item label="样本数">
              {latestSnapshot?.sampleSize || klines.length}
            </Descriptions.Item>
          </Descriptions>
        </div>
        <div style={{ marginBottom: 8, fontWeight: 600 }}>常用窗口指标</div>
        <Table
          rowKey="window"
          size="small"
          pagination={false}
          columns={metricColumns}
          dataSource={metricRows}
          scroll={{ x: 900 }}
          style={{ marginBottom: 16 }}
        />
        <StockKlineChart
          symbol={normalizedSymbol}
          klineUrl={isAStock ? `/api/stock/a-stock/klines/${normalizedSymbol}` : undefined}
          valuationHistory={evcHistory}
          valuationFillMode={isAStock ? 'forward' : 'exact'}
          valuationDateOffsetDays={isAStock ? 0 : -1}
          onKlinesChange={setKlines}
          height={600}
        />
      </Card>
    </div>
  );
};

export default StockDetail;
