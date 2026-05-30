import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Radio, Row, Skeleton, Space, Statistic, Tag, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../../utils/request';
import { TIME_RANGES, getFearGreedColor, getFearGreedStatus } from '../utils';
import './SOXXFearGreed.css';

const { Text } = Typography;

const ETF_OPTIONS = [
  { symbol: 'SOXX.US', ticker: 'SOXX', label: '半导体' },
  { symbol: 'SPY.US', ticker: 'SPY', label: '标普500' },
  { symbol: 'QQQ.US', ticker: 'QQQ', label: '纳指100' },
  { symbol: 'DIA.US', ticker: 'DIA', label: '道琼斯' },
  { symbol: 'INNO100.CN', ticker: 'A创100', label: '创新100', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000510.SH', ticker: '中证A500', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000905.SH', ticker: '中证500', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000985.SH', ticker: '中证全指', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000699.SH', ticker: '科创200', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399006.SZ', ticker: '创业板指', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399998.SZ', ticker: '中证煤炭', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000015.SH', ticker: '上证红利', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
];

const DEFAULT_DETAIL_STATE = {
  data: [],
  latest: null,
  latest_holdings: [],
};

const isFiniteNumber = value => value !== null && value !== undefined && Number.isFinite(Number(value));

const formatNumber = (value, digits = 1) => (
  isFiniteNumber(value) ? Number(value).toFixed(digits) : '-'
);

const formatSignedNumber = (value, digits = 1, suffix = '') => {
  if (!isFiniteNumber(value)) return '-';
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toFixed(digits)}${suffix}`;
};

const fearColor = value => (isFiniteNumber(value) ? getFearGreedColor(value) : '#8c8c8c');

const fearTextColor = (value) => {
  const color = fearColor(value);
  return color === '#d9d9d9' ? '#595959' : color;
};

const fearStatus = value => (isFiniteNumber(value) ? getFearGreedStatus(value) : '未入库');

const formatDateTime = (value) => {
  if (!value) return '-';
  return new Date(value).toLocaleString();
};

const SOXXFearGreed = () => {
  const [summaries, setSummaries] = useState([]);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState(null);
  const [expandedSymbol, setExpandedSymbol] = useState(null);
  const [detailBySymbol, setDetailBySymbol] = useState({});
  const [detailLoadingSymbol, setDetailLoadingSymbol] = useState(null);
  const [detailErrorBySymbol, setDetailErrorBySymbol] = useState({});
  const [realtimeBySymbol, setRealtimeBySymbol] = useState({});
  const [realtimeLoadingSymbol, setRealtimeLoadingSymbol] = useState(null);
  const [realtimeErrorBySymbol, setRealtimeErrorBySymbol] = useState({});
  const [timeRange, setTimeRange] = useState(3);

  const summaryBySymbol = useMemo(() => {
    const result = {};
    summaries.forEach(item => {
      result[item.symbol] = item;
    });
    return result;
  }, [summaries]);

  const expandedETF = useMemo(
    () => ETF_OPTIONS.find(item => item.symbol === expandedSymbol) || null,
    [expandedSymbol]
  );
  const expandedDetail = expandedSymbol ? (detailBySymbol[expandedSymbol] || DEFAULT_DETAIL_STATE) : DEFAULT_DETAIL_STATE;
  const expandedSummary = expandedSymbol ? summaryBySymbol[expandedSymbol] : null;
  const realtime = expandedSymbol ? realtimeBySymbol[expandedSymbol] : null;
  const realtimeError = expandedSymbol ? realtimeErrorBySymbol[expandedSymbol] : null;
  const detailError = expandedSymbol ? detailErrorBySymbol[expandedSymbol] : null;
  const detailLoading = expandedSymbol && detailLoadingSymbol === expandedSymbol;
  const realtimeLoading = expandedSymbol && realtimeLoadingSymbol === expandedSymbol;

  const fetchSummaries = useCallback(async () => {
    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const { data } = await request.get('/api/cnn/etf-fear-greed-clone/summaries', {
        params: {
          symbols: ETF_OPTIONS.map(item => item.symbol).join(','),
        },
      });
      setSummaries(data?.data || []);
    } catch (error) {
      setSummaryError(error?.response?.data?.detail || error.message || '获取自算贪恐摘要失败');
    } finally {
      setSummaryLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSummaries();
  }, [fetchSummaries]);

  useEffect(() => {
    if (!expandedSymbol || detailBySymbol[expandedSymbol]) return undefined;
    let cancelled = false;
    const fetchHistory = async () => {
      setDetailLoadingSymbol(expandedSymbol);
      setDetailErrorBySymbol(prev => ({ ...prev, [expandedSymbol]: null }));
      try {
        const { data } = await request.get('/api/cnn/etf-fear-greed-clone/history', {
          params: {
            symbol: expandedSymbol,
            include_components: false,
            include_latest_holdings: true,
          },
        });
        if (!cancelled) {
          setDetailBySymbol(prev => ({ ...prev, [expandedSymbol]: data || DEFAULT_DETAIL_STATE }));
        }
      } catch (error) {
        if (!cancelled) {
          setDetailErrorBySymbol(prev => ({
            ...prev,
            [expandedSymbol]: error?.response?.data?.detail || error.message || '获取自算贪恐历史失败',
          }));
        }
      } finally {
        if (!cancelled) setDetailLoadingSymbol(null);
      }
    };
    fetchHistory();
    return () => {
      cancelled = true;
    };
  }, [detailBySymbol, expandedSymbol]);

  useEffect(() => {
    if (!expandedSymbol || !expandedETF || expandedETF.realtime === false || realtimeBySymbol[expandedSymbol]) return undefined;
    let cancelled = false;
    const fetchRealtime = async () => {
      setRealtimeLoadingSymbol(expandedSymbol);
      setRealtimeErrorBySymbol(prev => ({ ...prev, [expandedSymbol]: null }));
      try {
        const { data } = await request.get('/api/cnn/etf-fear-greed-clone/realtime', {
          params: {
            symbol: expandedSymbol,
            include_holdings_quotes: false,
          },
        });
        if (!cancelled) {
          setRealtimeBySymbol(prev => ({ ...prev, [expandedSymbol]: data || null }));
        }
      } catch (error) {
        if (!cancelled) {
          setRealtimeErrorBySymbol(prev => ({
            ...prev,
            [expandedSymbol]: error?.response?.data?.detail || error.message || '获取实时贪恐失败',
          }));
        }
      } finally {
        if (!cancelled) setRealtimeLoadingSymbol(null);
      }
    };
    fetchRealtime();
    return () => {
      cancelled = true;
    };
  }, [expandedETF, expandedSymbol, realtimeBySymbol]);

  const filteredData = useMemo(() => {
    const data = expandedDetail?.data || [];
    if (timeRange === -1) return data;
    const cutoffDate = new Date();
    cutoffDate.setFullYear(cutoffDate.getFullYear() - timeRange);
    return data.filter(item => new Date(item.date) >= cutoffDate);
  }, [expandedDetail, timeRange]);

  const chartOption = useMemo(() => {
    const ticker = expandedETF?.ticker || '标的';
    const dates = filteredData.map(item => item.date);
    const scores = filteredData.map(item => item.score ?? null);
    const prices = filteredData.map(item => item.etf_price?.close ?? null);
    const priceValues = prices.filter(value => value !== null && value !== undefined);
    const priceMin = priceValues.length ? Math.floor(Math.min(...priceValues) * 0.92) : undefined;
    const priceMax = priceValues.length ? Math.ceil(Math.max(...priceValues) * 1.08) : undefined;

    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: [`${ticker}贪恐`, `${ticker}价格`], top: 0 },
      grid: { left: 48, right: 56, top: 48, bottom: 56 },
      dataZoom: [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', start: 0, end: 100 },
      ],
      xAxis: { type: 'category', boundaryGap: false, data: dates },
      yAxis: [
        {
          type: 'value',
          name: '贪恐',
          min: 0,
          max: 100,
          axisLine: { show: true, lineStyle: { color: '#13c2c2' } },
          splitLine: { lineStyle: { color: '#f0f0f0' } },
        },
        {
          type: 'value',
          name: '价格',
          min: priceMin,
          max: priceMax,
          axisLine: { show: true, lineStyle: { color: '#fa8c16' } },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: `${ticker}贪恐`,
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: scores,
          lineStyle: { width: 2, color: '#13c2c2' },
          itemStyle: { color: '#13c2c2' },
        },
        {
          name: `${ticker}价格`,
          type: 'line',
          yAxisIndex: 1,
          smooth: true,
          showSymbol: false,
          data: prices,
          lineStyle: { width: 2, color: '#fa8c16' },
          itemStyle: { color: '#fa8c16' },
        },
      ],
    };
  }, [expandedETF, filteredData]);

  const toggleExpanded = (symbol) => {
    setExpandedSymbol(current => (current === symbol ? null : symbol));
  };

  const realtimeMeta = realtime?.fear_and_greed_clone;
  const realtimeScore = realtimeMeta?.score;
  const realtimePrice = realtime?.etf_price?.close ?? realtime?.etf_price?.quote?.price;
  const latest = expandedDetail.latest || expandedSummary?.latest;
  const latestPrice = latest?.etf_price?.close;
  const displayPrice = realtimePrice ?? latestPrice;
  const topHoldings = (expandedDetail.latest_holdings || []).slice(0, 8);
  const realtimeEnabled = expandedETF?.realtime !== false;
  const pricePrecision = expandedETF?.pricePrecision ?? 2;

  return (
    <Card
      title="自算贪恐"
      className="soxx-fear-card"
      extra={<Button size="small" onClick={fetchSummaries} loading={summaryLoading}>刷新</Button>}
    >
      {summaryError && (
        <Alert type="warning" showIcon message="自算贪恐摘要暂不可用" description={summaryError} style={{ marginBottom: 12 }} />
      )}

      {summaryLoading && !summaries.length ? (
        <div className="soxx-fear-summary-grid">
          {ETF_OPTIONS.slice(0, 8).map(item => (
            <div className="soxx-fear-summary-card" key={item.symbol}>
              <Skeleton active paragraph={{ rows: 3 }} title={false} />
            </div>
          ))}
        </div>
      ) : (
        <div className="soxx-fear-summary-grid">
          {ETF_OPTIONS.map(item => (
            <SummaryCard
              key={item.symbol}
              option={item}
              summary={summaryBySymbol[item.symbol]}
              active={expandedSymbol === item.symbol}
              onToggle={() => toggleExpanded(item.symbol)}
            />
          ))}
        </div>
      )}

      {expandedSymbol && (
        <div className="soxx-fear-detail">
          <div className="soxx-fear-detail-header">
            <div>
              <Text strong>{expandedETF?.ticker}</Text>
              <Text type="secondary" style={{ marginLeft: 8 }}>{expandedETF?.symbol}</Text>
            </div>
            <Tag color={fearColor(realtimeScore ?? latest?.score)}>
              {fearStatus(realtimeScore ?? latest?.score)}
            </Tag>
          </div>

          {detailError && (
            <Alert type="warning" showIcon message={`${expandedETF?.ticker}贪恐历史暂不可用`} description={detailError} />
          )}

          {!detailError && detailLoading && <Skeleton active paragraph={{ rows: 6 }} />}

          {!detailError && !detailLoading && !latest && (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`${expandedETF?.ticker} 暂无入库记录`} />
          )}

          {!detailError && !detailLoading && latest && (
            <>
              {realtimeError && (
                <Alert
                  type="info"
                  showIcon
                  message={`${expandedETF?.ticker} 实时贪恐暂不可用，当前显示最新入库日线值`}
                  description={realtimeError}
                  style={{ marginBottom: 12 }}
                />
              )}

              <Row gutter={[12, 12]} className="soxx-fear-detail-stats">
                <Col xs={12} sm={8} lg={4}>
                  <Statistic
                    title={realtimeEnabled ? '实时/最新' : '最新'}
                    value={realtimeScore ?? latest.score}
                    precision={1}
                    valueStyle={{ color: fearTextColor(realtimeScore ?? latest.score) }}
                    suffix="/100"
                  />
                  {realtimeEnabled && realtimeLoading && <Tag color="processing">实时计算中</Tag>}
                </Col>
                <Col xs={12} sm={8} lg={4}>
                  <Statistic title="入库分数" value={latest.score} precision={1} suffix="/100" />
                </Col>
                <Col xs={12} sm={8} lg={4}>
                  <Statistic
                    title={expandedETF?.priceLabel || '价格'}
                    value={displayPrice}
                    precision={pricePrecision}
                    prefix={expandedETF?.symbol.endsWith('.US') ? '$' : undefined}
                  />
                </Col>
                <Col xs={12} sm={8} lg={4}>
                  <Statistic title="数据日期" value={latest.date} />
                </Col>
                <Col xs={12} sm={8} lg={4}>
                  <Statistic title="报价时间" value={realtimeEnabled ? formatDateTime(realtimeMeta?.timestamp) : '日线'} />
                </Col>
                <Col xs={12} sm={8} lg={4}>
                  <Statistic title="历史样本" value={expandedSummary?.history_points ?? expandedDetail.count ?? '-'} />
                </Col>
              </Row>

              {topHoldings.length > 0 && (
                <Space wrap size={[8, 8]} className="soxx-fear-holdings">
                  {topHoldings.map(holding => (
                    <Tag key={holding.symbol}>
                      {holding.symbol} {(holding.weight * 100).toFixed(2)}%
                    </Tag>
                  ))}
                </Space>
              )}

              <div className="soxx-fear-chart-toolbar">
                <Radio.Group
                  value={timeRange}
                  onChange={event => setTimeRange(event.target.value)}
                  optionType="button"
                  buttonStyle="solid"
                >
                  {TIME_RANGES.filter(range => [1, 3, 5, -1].includes(range.value)).map(range => (
                    <Radio.Button key={range.value} value={range.value}>
                      {range.label}
                    </Radio.Button>
                  ))}
                </Radio.Group>
              </div>

              <ReactECharts option={chartOption} className="soxx-fear-chart" />
            </>
          )}
        </div>
      )}
    </Card>
  );
};

const SummaryCard = ({ option, summary, active, onToggle }) => {
  const latest = summary?.latest;
  const score = latest?.score;
  const scoreColor = fearColor(score);
  const scoreTextColor = fearTextColor(score);
  const sevenDayScore = summary?.seven_day_ago?.score;
  const oneMonthScore = summary?.one_month_ago?.score;
  const price = latest?.etf_price?.close;
  const scoreDeltaText = [
    isFiniteNumber(summary?.score_change_7d) ? `7日${formatSignedNumber(summary.score_change_7d, 1)}` : null,
    isFiniteNumber(summary?.score_change_1m) ? `1月${formatSignedNumber(summary.score_change_1m, 1)}` : null,
  ].filter(Boolean).join(' · ');

  return (
    <button
      type="button"
      className={`soxx-fear-summary-card${active ? ' is-active' : ''}`}
      onClick={onToggle}
    >
      <div className="soxx-fear-summary-top">
        <div>
          <div className="soxx-fear-summary-name">{option.ticker}</div>
          <div className="soxx-fear-summary-meta">{option.label} · {option.symbol}</div>
        </div>
        {latest ? (
          <Tag color={scoreColor}>{fearStatus(score)}</Tag>
        ) : (
          <Tag>未入库</Tag>
        )}
      </div>

      <div className="soxx-fear-summary-score" style={{ color: scoreTextColor }}>
        {formatNumber(score, 1)}
      </div>

      <div className="soxx-fear-summary-metrics">
        <MetricCell label="7天前" value={formatNumber(sevenDayScore, 1)} color={fearTextColor(sevenDayScore)} />
        <MetricCell label="1月前" value={formatNumber(oneMonthScore, 1)} color={fearTextColor(oneMonthScore)} />
        <MetricCell label={option.priceLabel || '价格'} value={formatNumber(price, option.pricePrecision ?? 2)} />
      </div>

      <div className="soxx-fear-summary-footer">
        <span>{latest?.date || '-'}</span>
        <span>
          {summary?.is_stale && summary?.stale_days !== null
            ? `${summary.stale_days}天未更新`
            : scoreDeltaText || `${summary?.history_points ?? 0}条`}
        </span>
      </div>
    </button>
  );
};

const MetricCell = ({ label, value, color }) => (
  <div className="soxx-fear-metric-cell">
    <span>{label}</span>
    <strong style={{ color }}>{value}</strong>
  </div>
);

export default SOXXFearGreed;
