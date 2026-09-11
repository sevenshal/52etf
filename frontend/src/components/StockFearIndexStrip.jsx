import React, { useEffect, useState } from 'react';
import { Space, Tag, Tooltip, Typography } from 'antd';
import request from '../utils/request';
import {
  fearColor,
  fearStatus,
  fearTextColor,
  resolveFearSummaryScore,
} from '../pages/fear/utils';

const { Text } = Typography;

const formatScore = value => (Number.isFinite(Number(value)) && value !== null ? Number(value).toFixed(1) : '--');

const IndexPill = ({ index, summary }) => {
  const resolved = resolveFearSummaryScore(summary);
  const latest = summary?.latest;
  const tip = (
    <div style={{ lineHeight: 1.7 }}>
      <div>{index.label} · {index.symbol}</div>
      {resolved.hasData ? (
        <>
          <div>
            {resolved.mode === 'intraday' ? '盘中' : resolved.closeLabel} {formatScore(resolved.score)}
            {resolved.mode === 'intraday' && `（${resolved.closeLabel} ${formatScore(latest?.score)}）`}
          </div>
          <div>7天前 {formatScore(summary?.seven_day_ago?.score)} · 1月前 {formatScore(summary?.one_month_ago?.score)}</div>
          {latest?.date && <div>收盘数据日期 {latest.date}</div>}
          {summary?.is_stale && <div style={{ color: '#faad14' }}>数据已 {summary?.stale_days ?? '若干'} 天未更新</div>}
        </>
      ) : <div>该指数暂无入库的贪恐数据</div>}
    </div>
  );
  return (
    <Tooltip title={tip}>
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          padding: '2px 8px',
          border: '1px solid #f0f0f0',
          borderRadius: 12,
          background: '#fff',
          cursor: 'default',
        }}
      >
        <Text strong style={{ fontSize: 13 }}>{index.label}</Text>
        {resolved.hasData ? (
          <>
            <span style={{ fontWeight: 700, color: fearTextColor(resolved.score), fontVariantNumeric: 'tabular-nums' }}>
              {formatScore(resolved.score)}
            </span>
            <Tag color={fearColor(resolved.score)} style={{ marginInlineEnd: 0 }}>{fearStatus(resolved.score)}</Tag>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {resolved.mode === 'intraday' ? '盘中' : resolved.closeLabel}
            </Text>
          </>
        ) : <Tag style={{ marginInlineEnd: 0 }}>未入库</Tag>}
      </span>
    </Tooltip>
  );
};

/**
 * 个股详情页：所属指数及各指数贪恐值。
 *
 * 成员关系与雪球持仓表的「所属贪恐指数」同一口径（后端复用 _attach_xueqiu_fear_index_memberships）；
 * 贪恐值读贪恐看板同一个 summaries 接口，盘中/收盘的取舍用和看板卡片同一个
 * resolveFearSummaryScore，所以这里显示的数和看板上的数永远一致。
 */
const StockFearIndexStrip = ({ symbol }) => {
  const [indexes, setIndexes] = useState(null);
  const [summaryBySymbol, setSummaryBySymbol] = useState({});

  useEffect(() => {
    let cancelled = false;
    setIndexes(null);
    setSummaryBySymbol({});
    if (!symbol) return undefined;
    (async () => {
      try {
        const { data } = await request.get(`/api/stock/a-stock/fear-indexes/${symbol}`);
        const list = data?.indexes || [];
        if (cancelled) return;
        setIndexes(list);
        if (!list.length) return;
        const { data: summaries } = await request.get('/api/cnn/etf-fear-greed-clone/summaries', {
          params: { symbols: list.map(item => item.symbol).join(',') },
        });
        if (cancelled) return;
        setSummaryBySymbol(Object.fromEntries((summaries?.data || []).map(item => [item.symbol, item])));
      } catch (error) {
        console.error('获取所属指数贪恐失败:', error);
        if (!cancelled) setIndexes(current => current || []);
      }
    })();
    return () => { cancelled = true; };
  }, [symbol]);

  if (indexes === null) return null;
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: '6px 10px',
        margin: '-8px 0 16px',
      }}
    >
      <Text type="secondary">所属指数 · 贪恐</Text>
      {indexes.length ? (
        <Space size={[8, 6]} wrap>
          {indexes.map(index => (
            <IndexPill key={index.symbol} index={index} summary={summaryBySymbol[index.symbol]} />
          ))}
        </Space>
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }}>未纳入任何有贪恐计算的指数</Text>
      )}
    </div>
  );
};

export default StockFearIndexStrip;
