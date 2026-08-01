import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Divider, Empty, Radio, Row, Skeleton, Space, Statistic, Tag, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';
import request from '../../utils/request';
import { TIME_RANGES, getFearGreedColor, getFearGreedStatus } from '../utils';
import './SOXXFearGreed.css';

const { Text } = Typography;

const ETF_OPTIONS = [
  { symbol: 'SOXX.US', ticker: 'SOXX', label: '半导体', market: 'US' },
  { symbol: 'SPY.US', ticker: 'SPY', label: '标普500', market: 'US' },
  { symbol: 'QQQ.US', ticker: 'QQQ', label: '纳指100', market: 'US' },
  { symbol: 'DIA.US', ticker: 'DIA', label: '道琼斯', market: 'US' },
  { symbol: 'HSI.HK', ticker: '恒生指数', label: '港股', market: 'HK', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: 'HSCEI.HK', ticker: '国企指数', label: '港股', market: 'HK', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: 'HSTECH.HK', ticker: '恒生科技', label: '港股', market: 'HK', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000985.SH', ticker: '中证全指', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '899050.BJ', ticker: '北证50', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: 'INNO100.CN', ticker: 'A创100', label: '创新100', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000510.SH', ticker: '中证A500', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000905.SH', ticker: '中证500', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000688.SH', ticker: '科创50', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000698.SH', ticker: '科创100', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000699.SH', ticker: '科创200', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399006.SZ', ticker: '创业板指', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399975.SZ', ticker: '证券公司', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: 'H30184.CSI', ticker: '半导体', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399997.SZ', ticker: '中证白酒', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399989.SZ', ticker: '中证医疗', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000819.SH', ticker: '有色金属', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399967.SZ', ticker: '中证军工', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '930997.CSI', ticker: '新能源车', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000932.SH', ticker: '主要消费', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399986.SZ', ticker: '中证银行', label: '板块', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '399998.SZ', ticker: '中证煤炭', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
  { symbol: '000015.SH', ticker: '上证红利', label: '指数', realtime: false, priceLabel: '点位', pricePrecision: 2 },
];

const US_ETF_OPTIONS = ETF_OPTIONS.filter(item => item.market === 'US');
const HK_ETF_OPTIONS = ETF_OPTIONS.filter(item => item.market === 'HK');
const CN_ETF_OPTIONS = ETF_OPTIONS.filter(item => item.market !== 'US' && item.market !== 'HK');

const DEFAULT_DETAIL_STATE = {
  data: [],
  latest: null,
  latest_holdings: [],
};

const isFiniteNumber = value => value !== null && value !== undefined && Number.isFinite(Number(value));

const formatNumber = (value, digits = 1) => (
  isFiniteNumber(value) ? Number(value).toFixed(digits) : '-'
);

const fearColor = value => (isFiniteNumber(value) ? getFearGreedColor(value) : '#8c8c8c');

const fearTextColor = (value) => {
  const color = fearColor(value);
  return color === '#d9d9d9' ? '#595959' : color;
};

const fearStatus = value => (isFiniteNumber(value) ? getFearGreedStatus(value) : '未入库');

const formatCompactVolume = (value) => {
  if (!isFiniteNumber(value)) return '-';
  const number = Number(value);
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(1)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
  return Math.round(number).toLocaleString();
};

const formatVolumeRatio = value => (
  isFiniteNumber(value) ? `${Number(value).toFixed(2)}×` : '-'
);

const valuationColor = rating => ({
  极度低估: '#237804',
  低估: '#389e0d',
  合理: '#1677ff',
  高估: '#cf1322',
  极度高估: '#820014',
}[rating] || '#8c8c8c');

const formatGap = value => (
  isFiniteNumber(value) ? `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(1)}%` : '-'
);

const formatRange = (low, high) => (
  isFiniteNumber(low) && isFiniteNumber(high)
    ? `${Number(low).toFixed(0)}–${Number(high).toFixed(0)}`
    : '-'
);

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
    const volumes = filteredData.map(item => item.etf_price?.volume ?? null);
    const priceValues = prices.filter(value => value !== null && value !== undefined);
    const priceMin = priceValues.length ? Math.floor(Math.min(...priceValues) * 0.92) : undefined;
    const priceMax = priceValues.length ? Math.ceil(Math.max(...priceValues) * 1.08) : undefined;

    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: [`${ticker}贪恐`, `${ticker}价格`, '成交量'], top: 0 },
      grid: [
        { left: 56, right: 64, top: 48, height: '57%' },
        { left: 56, right: 64, top: '72%', bottom: 58 },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], start: 0, end: 100, bottom: 8 },
      ],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [
        {
          type: 'category',
          boundaryGap: false,
          data: dates,
          axisLabel: { show: false },
          axisTick: { show: false },
        },
        {
          type: 'category',
          gridIndex: 1,
          boundaryGap: true,
          data: dates,
        },
      ],
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
        {
          type: 'value',
          name: '成交量',
          gridIndex: 1,
          min: 0,
          axisLabel: { formatter: formatCompactVolume },
          axisLine: { show: true, lineStyle: { color: '#8c8c8c' } },
          splitLine: { lineStyle: { color: '#f5f5f5' } },
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
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: volumes,
          barMaxWidth: 12,
          itemStyle: { color: 'rgba(89, 126, 164, 0.55)' },
          emphasis: { itemStyle: { color: '#597ea4' } },
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
  const valuation = expandedDetail.valuation || expandedSummary?.valuation;
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
        <>
          <div className="soxx-fear-summary-grid">
            {US_ETF_OPTIONS.map(item => (
              <div className="soxx-fear-summary-card" key={item.symbol}>
                <Skeleton active paragraph={{ rows: 3 }} title={false} />
              </div>
            ))}
          </div>
          <Divider className="soxx-fear-market-divider"></Divider>
          <div className="soxx-fear-summary-grid">
            {HK_ETF_OPTIONS.map(item => (
              <div className="soxx-fear-summary-card" key={item.symbol}>
                <Skeleton active paragraph={{ rows: 3 }} title={false} />
              </div>
            ))}
          </div>
          <Divider className="soxx-fear-market-divider"></Divider>
          <div className="soxx-fear-summary-grid">
            {CN_ETF_OPTIONS.slice(0, 4).map(item => (
              <div className="soxx-fear-summary-card" key={item.symbol}>
                <Skeleton active paragraph={{ rows: 3 }} title={false} />
              </div>
            ))}
          </div>
        </>
      ) : (
        <>
          <div className="soxx-fear-summary-grid">
            {US_ETF_OPTIONS.map(item => (
              <SummaryCard
                key={item.symbol}
                option={item}
                summary={summaryBySymbol[item.symbol]}
                active={expandedSymbol === item.symbol}
                onToggle={() => toggleExpanded(item.symbol)}
              />
            ))}
          </div>
          <Divider className="soxx-fear-market-divider">港股</Divider>
          <div className="soxx-fear-summary-grid">
            {HK_ETF_OPTIONS.map(item => (
              <SummaryCard
                key={item.symbol}
                option={item}
                summary={summaryBySymbol[item.symbol]}
                active={expandedSymbol === item.symbol}
                onToggle={() => toggleExpanded(item.symbol)}
              />
            ))}
          </div>
          <Divider className="soxx-fear-market-divider">A股</Divider>
          <div className="soxx-fear-summary-grid">
            {CN_ETF_OPTIONS.map(item => (
              <SummaryCard
                key={item.symbol}
                option={item}
                summary={summaryBySymbol[item.symbol]}
                active={expandedSymbol === item.symbol}
                onToggle={() => toggleExpanded(item.symbol)}
              />
            ))}
          </div>
        </>
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

              {expandedETF?.market !== 'US' && expandedETF?.market !== 'HK' && valuation?.status === 'available' && (
                <Card size="small" className="soxx-fear-valuation-detail" title="指数成分估值">
                  <Row gutter={[12, 12]}>
                    <Col xs={12} sm={6}>
                      <Statistic
                        title="当前低估率"
                        value={formatGap(valuation.current_gap_pct)}
                        valueStyle={{ color: valuationColor(valuation.valuation_position_label), fontSize: 20 }}
                      />
                      <Tag>
                        目标区间 {formatRange(valuation.fair_value_lo, valuation.fair_value_hi)}
                      </Tag>
                    </Col>
                    <Col xs={12} sm={6}>
                      <Statistic
                        title="估值位置"
                        value={valuation.valuation_position_label || '-'}
                        valueStyle={{ color: valuationColor(valuation.valuation_position_label), fontSize: 20 }}
                      />
                      <Tag color={valuationColor(valuation.valuation_position_label)}>
                        近{valuation.valuation_history_days || 0}日百分位 {formatNumber(valuation.valuation_position_pct, 1)}%
                      </Tag>
                    </Col>
                    <Col xs={12} sm={6}>
                      <Statistic title="估值覆盖权重" value={(valuation.coverage_ratio || 0) * 100} precision={1} suffix="%" />
                      <Text type="secondary">
                        {valuation.covered_count}/{valuation.constituent_count || '-'} 个成分
                      </Text>
                    </Col>
                    <Col xs={12} sm={6}>
                      <Statistic title="估值数据日期" value={valuation.valuation_date_max || '-'} />
                      <Text type="secondary">
                        区间 {valuation.valuation_date_min || '-'} 至 {valuation.valuation_date_max || '-'}
                      </Text>
                    </Col>
                  </Row>
                  <Text type="secondary" className="soxx-fear-valuation-note">
                    低估率按指数成分权重聚合一致预期目标价相对当日价格的空间；估值位置为当前低估率在最近252个有效交易日中的历史百分位。
                  </Text>
                </Card>
              )}

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
  const valuation = summary?.valuation;
  const showValuation = option.market !== 'US' && option.market !== 'HK' && valuation?.status === 'available';
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
        <span title="恐贪日期成交量 ÷ 不含当日的前20个交易日平均成交量">
          量比 {formatVolumeRatio(summary?.volume_ratio_20d)}
        </span>
      </div>
      {showValuation && (
        <div className="soxx-fear-summary-valuation">
          <span style={{ color: valuationColor(valuation.valuation_position_label) }}>
            {valuation.valuation_position_label || '-'}
          </span>
          <span>{formatNumber(valuation.valuation_position_pct, 1)}%分位</span>
          <span>覆盖 {(Number(valuation.coverage_ratio || 0) * 100).toFixed(0)}%</span>
        </div>
      )}
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
