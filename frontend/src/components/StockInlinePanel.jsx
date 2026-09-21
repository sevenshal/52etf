import React, { useEffect, useMemo, useState } from 'react';
import { Button, Card, Col, Row, Space, Tag, Typography } from 'antd';
import { CloseOutlined } from '@ant-design/icons';
import request from '../utils/request';
import MinuteMiniChart, { SIGNAL_COLORS } from './MinuteMiniChart';
import StockKlineChart from './StockKlineChart';

const { Text } = Typography;

const fmtPct = value => (value === null || value === undefined ? '-' : `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%`);

/**
 * 个股内嵌行情面板：左边当日分时 + 5日分时（都带成交量），右边日K线。
 *
 * 分时数据只在面板打开时按需拉一次（/api/market/intraday-minutes：
 * 库里分钟历史 + 当日实时补齐），关闭即丢弃，不进任何轮询。
 */
const StockInlinePanel = ({ stock, onClose, extraMeta = null, highlightTime = null, className = '' }) => {
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!stock?.ts_code) {
      setSeries(null);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    request.get('/api/market/intraday-minutes', { params: { ts_code: stock.ts_code, days: 5 } })
      .then(response => { if (!cancelled) setSeries(response.data); })
      .catch(() => { if (!cancelled) setSeries(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [stock?.ts_code]);

  // 当日分时 = 最后一个交易日那一段
  const todaySeries = useMemo(() => {
    if (!series?.days?.length) return null;
    const day = series.days[series.days.length - 1];
    return {
      days: [{ ...day, start_index: 0 }],
      points: series.points.slice(day.start_index, day.start_index + day.count),
      today: day.date,
    };
  }, [series]);

  const allEvents = useMemo(() => series?.events || [], [series]);
  const todayEvents = useMemo(
    () => allEvents.filter(event => event.date === todaySeries?.today),
    [allEvents, todaySeries],
  );

  if (!stock) return null;
  const today = todaySeries?.days?.[0];

  return (
    <Card
      size="small"
      className={`stock-inline-panel ${className}`.trim()}
      title={(
        <Space size={8} wrap>
          <span>{stock.name} {stock.code || stock.ts_code}</span>
          {extraMeta}
        </Space>
      )}
      extra={<Button type="text" size="small" icon={<CloseOutlined />} onClick={onClose} />}
    >
      <Row gutter={[12, 12]}>
        <Col xs={24} xl={9}>
          <div className="minute-mini__title">
            当日分时
            {series?.realtime_merged && <Tag color="processing">实时补齐</Tag>}
            {today && (
              <Text type="secondary">
                {today.date} · 收 {today.close} · {fmtPct(today.pct)} · 额 {today.amount_yi}亿
              </Text>
            )}
          </div>
          <MinuteMiniChart
            series={todaySeries}
            loading={loading}
            height={180}
            singleDay
            highlightTime={highlightTime}
            events={todayEvents}
          />
          <div className="minute-mini__title">
            5日分时
            {series?.days?.length ? <Text type="secondary">{series.days.length} 个交易日</Text> : null}
          </div>
          <MinuteMiniChart series={series} loading={loading} height={180} events={allEvents} />
          {allEvents.length > 0 && (
            <div className="signal-legend">
              {Object.entries(SIGNAL_COLORS).map(([name, color]) => (
                <span key={name}><i style={{ background: color }} />{name}</span>
              ))}
              <Text type="secondary">共 {allEvents.length} 次信号</Text>
            </div>
          )}
        </Col>
        <Col xs={24} xl={15}>
          <StockKlineChart symbol={stock.ts_code} height={392} />
        </Col>
      </Row>
    </Card>
  );
};

export default StockInlinePanel;
