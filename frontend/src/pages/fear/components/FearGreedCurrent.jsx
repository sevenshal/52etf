import React from 'react';
import { Col, Row, Tag, Tooltip, Typography } from 'antd';
import { FEAR_GREED_BANDS, fearStatus, fearTextColor } from '../utils';

const { Text } = Typography;

// CNN 官方七个分项。接口里另有 market_momentum_sp125 / market_volatility_vix_50，
// 只是动量与 VIX 的另一套均线口径，与 sp500 / vix 是同一个分项，不重复展示。
const CNN_COMPONENTS = [
  { key: 'market_momentum_sp500', name: '股价动量', hint: '标普500 与 125 日均线比较' },
  { key: 'stock_price_strength', name: '股价强度', hint: '创 52 周新高与新低的股票数量之差' },
  { key: 'stock_price_breadth', name: '市场广度', hint: '上涨股票成交量占全部成交量的比重' },
  { key: 'put_call_options', name: '期权 Put/Call', hint: '看跌与看涨期权成交量之比（5 日均）' },
  { key: 'market_volatility_vix', name: '市场波动率', hint: 'VIX 与 50 日均值比较' },
  { key: 'safe_haven_demand', name: '避险需求', hint: '股票与国债近 20 日收益差' },
  { key: 'junk_bond_demand', name: '垃圾债需求', hint: '垃圾债与国债的收益率差' },
];

// 分档说明直接由 FEAR_GREED_BANDS 生成，改档位时只需要改 utils 一处
const BAND_HINT = [...FEAR_GREED_BANDS]
  .sort((left, right) => left.min - right.min)
  .map(band => `${band.min}-${band.max} ${band.label}`)
  .join(' · ');

const isFiniteScore = value => value !== null && value !== undefined && Number.isFinite(Number(value));

// 分档标签：白底 + 档位色描边，保证“中性”这种浅色档位上的文字也看得清
const bandTagStyle = score => ({
  color: fearTextColor(score),
  borderColor: fearTextColor(score),
  background: '#fff',
  marginInlineEnd: 0,
});

const formatDateTime = (value) => {
  if (value === null || value === undefined) return null;

  const timestamp = Number(value);
  if (!Number.isFinite(timestamp)) return null;

  const date = new Date(timestamp < 10000000000 ? timestamp * 1000 : timestamp);
  if (Number.isNaN(date.getTime())) return null;

  const pad = (num) => String(num).padStart(2, '0');
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate())
  ].join('-') + ' ' + [
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds())
  ].join(':');
};

const getDataTime = (fearGreedData) => {
  return formatDateTime(
    fearGreedData?.fear_and_greed_historical?.timestamp ??
    fearGreedData?.fear_and_greed?.timestamp ??
    fearGreedData?.timestamp
  );
};

const boxStyle = { background: '#fafafa', borderRadius: 10, padding: '12px 16px', height: '100%' };

// 中间一列：昨收 / 一周前 / 一月前 / 一年前，紧凑两栏行
const CompareRow = ({ label, score }) => {
  const hasScore = isFiniteScore(score);
  const color = fearTextColor(score);
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', padding: '4px 0' }}>
      <span style={{ fontSize: 12, color: '#8c8c8c' }}>{label}</span>
      <span style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <span style={{ fontSize: 16, fontWeight: 700, color }}>{hasScore ? Math.round(Number(score)) : '-'}</span>
        <span style={{ fontSize: 11, color }}>{hasScore ? fearStatus(score) : '-'}</span>
      </span>
    </div>
  );
};

// 右侧一列：分项一行一个，名称 + 细进度条 + 分数 + 档位标签
const ComponentRow = ({ name, hint, score }) => {
  const hasScore = isFiniteScore(score);
  const color = fearTextColor(score);
  const percent = hasScore ? Math.min(100, Math.max(0, Number(score))) : 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '3px 0' }}>
      <div
        style={{ width: 76, flex: '0 0 76px', fontSize: 12, color: '#262626', whiteSpace: 'nowrap' }}
        title={hint}
      >
        {name}
      </div>
      <div style={{ flex: 1, minWidth: 60 }}>
        <div style={{ height: 6, borderRadius: 3, background: '#f0f0f0', overflow: 'hidden' }}>
          <div style={{ width: `${percent}%`, height: '100%', borderRadius: 3, background: color }} />
        </div>
      </div>
      <div style={{ width: 40, textAlign: 'right', fontSize: 13, fontWeight: 700, color }}>
        {hasScore ? Number(score).toFixed(1) : '-'}
      </div>
      <div style={{ width: 68, textAlign: 'right' }}>
        {hasScore
          ? <Tag style={{ ...bandTagStyle(score), fontSize: 11, lineHeight: '18px', padding: '0 6px' }}>{fearStatus(score)}</Tag>
          : <Text type="secondary" style={{ fontSize: 11 }}>-</Text>}
      </div>
    </div>
  );
};

const FearGreedCurrent = ({ fearGreedData }) => {
  if (!fearGreedData) return null;

  const fearAndGreed = fearGreedData.fear_and_greed || {};
  const score = fearAndGreed.score;
  const hasScore = isFiniteScore(score);
  const scoreColor = fearTextColor(score);
  const dataTime = getDataTime(fearGreedData);
  const change = hasScore && isFiniteScore(fearAndGreed.previous_close)
    ? Number(score) - Number(fearAndGreed.previous_close)
    : null;

  const components = CNN_COMPONENTS.map(meta => ({
    ...meta,
    score: (fearGreedData[meta.key] || {}).score,
  }));

  return (
    // 一行三列：当前分数 | 昨收/一周/一月/一年前 | 七个分项
    <Row gutter={[16, 12]} align="stretch">
      <Col xs={24} md={6} lg={4}>
        <div style={boxStyle}>
          <Text type="secondary" style={{ fontSize: 12 }}>当前恐贪值</Text>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 36, fontWeight: 700, lineHeight: 1.15, color: scoreColor }}>
              {hasScore ? Math.round(Number(score)) : '-'}
            </span>
            <span style={{ fontSize: 13, color: '#8c8c8c' }}>/100</span>
            {hasScore && <Tag style={{ ...bandTagStyle(score), marginInlineStart: 2 }}>{fearStatus(score)}</Tag>}
          </div>
          <div style={{ marginTop: 6, fontSize: 12, color: '#8c8c8c' }}>
            较昨收 {change === null ? '-' : `${change > 0 ? '+' : ''}${change.toFixed(1)}`}
          </div>
          {dataTime && (
            <div style={{ fontSize: 12, color: '#8c8c8c' }}>数据时间：{dataTime}</div>
          )}
        </div>
      </Col>
      <Col xs={24} md={6} lg={4}>
        <div style={boxStyle}>
          <CompareRow label="昨日收盘" score={fearAndGreed.previous_close} />
          <CompareRow label="一周前" score={fearAndGreed.previous_1_week} />
          <CompareRow label="一月前" score={fearAndGreed.previous_1_month} />
          <CompareRow label="一年前" score={fearAndGreed.previous_1_year} />
        </div>
      </Col>
      <Col xs={24} md={12} lg={16}>
        <div style={boxStyle}>
          <Tooltip title={BAND_HINT}>
            <Text type="secondary" style={{ fontSize: 12, cursor: 'default' }}>
              分项得分（档位说明见悬浮提示）
            </Text>
          </Tooltip>
          <div style={{ marginTop: 2 }}>
            {components.map(({ key, name, hint, score: componentScore }) => (
              <ComponentRow key={key} name={name} hint={hint} score={componentScore} />
            ))}
          </div>
        </div>
      </Col>
    </Row>
  );
};

export default FearGreedCurrent;
