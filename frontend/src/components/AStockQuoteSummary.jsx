import React, { useMemo } from 'react';
import dayjs from 'dayjs';

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

const formatFixed = (value, digits = 2) => {
  const number = toNumber(value);
  return number === null ? '--' : number.toFixed(digits);
};

const formatChinese = (value, unit = '') => {
  const number = toNumber(value);
  if (number === null) return '--';
  const absolute = Math.abs(number);
  if (absolute >= 1e12) return `${(number / 1e12).toFixed(2)}万亿${unit}`;
  if (absolute >= 1e8) return `${(number / 1e8).toFixed(2)}亿${unit}`;
  if (absolute >= 1e4) return `${(number / 1e4).toFixed(2)}万${unit}`;
  return `${number.toFixed(2)}${unit}`;
};

const formatTickTime = value => {
  const text = String(value || '').trim();
  const compact = text.match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/);
  if (compact) return `${compact[2]}-${compact[3]} ${compact[4]}:${compact[5]}:${compact[6]}`;
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('MM-DD HH:mm:ss') : '--';
};

const STATUS_LABELS = {
  START: '市场启动', PRETR: '盘前', OCALL: '集合竞价', TRADE: '交易中',
  HALT: '暂停交易', SUSP: '停牌', BREAK: '休市', POSTR: '盘后',
  ENDTR: '已收盘', STOPT: '长期停牌', DELISTED: '已退市', POSMT: '盘后交易',
  PCALL: '盘后竞价', INIT: '盘后待启动', ENDPT: '盘后闭市', POSSP: '盘后停牌',
};

const Metric = ({ label, value }) => (
  <div style={{ minWidth: 0, color: '#666', lineHeight: 1.8 }}>
    {label}：<strong style={{ color: '#262626', fontWeight: 600 }}>{value}</strong>
  </div>
);

const AStockQuoteSummary = ({ symbol, name, quote = {}, summary = {}, week52 = {} }) => {
  const values = useMemo(() => {
    const last = toNumber(quote.last_px);
    const preclose = toNumber(quote.preclose_px);
    const high = toNumber(quote.high_px);
    const low = toNumber(quote.low_px);
    const change = last !== null && preclose !== null ? last - preclose : null;
    const changePct = change !== null && preclose > 0 ? change / preclose * 100 : null;
    const amplitude = high !== null && low !== null && preclose > 0
      ? (high - low) / preclose * 100
      : null;
    const totalShares = toNumber(summary.total_shares);
    const circulatingShares = toNumber(summary.circulating_shares);
    return {
      last, preclose, high, low, change, changePct, amplitude,
      totalShares, circulatingShares,
      totalMarketCap: last !== null && totalShares !== null ? last * totalShares : null,
      circulatingMarketCap: last !== null && circulatingShares !== null ? last * circulatingShares : null,
    };
  }, [quote, summary]);

  const directionColor = values.change === null ? '#595959' : (values.change >= 0 ? '#cf1322' : '#389e0d');
  const currencySymbol = summary.currency === 'CNY' ? '¥' : `${summary.currency || ''} `;
  const status = STATUS_LABELS[quote.trade_status] || quote.trade_status || '--';
  const metrics = [
    ['最高', formatFixed(values.high)],
    ['今开', formatFixed(quote.open_px)],
    ['涨停', formatFixed(quote.up_px)],
    ['成交量', formatChinese(quote.volume, '手')],
    ['最低', formatFixed(values.low)],
    ['昨收', formatFixed(values.preclose)],
    ['跌停', formatFixed(quote.down_px)],
    ['成交额', formatChinese(toNumber(quote.amount) === null ? null : Number(quote.amount) * 1000)],
    ['量比', formatFixed(quote.vol_ratio)],
    ['换手', toNumber(quote.turnover_ratio) === null ? '--' : `${formatFixed(quote.turnover_ratio)}%`],
    ['市盈率(动)', formatFixed(quote.pe_rate)],
    ['市净率', formatFixed(quote.pb_rate)],
    ['委比', toNumber(quote.entrust_rate) === null ? '--' : `${formatFixed(quote.entrust_rate)}%`],
    ['振幅', values.amplitude === null ? '--' : `${formatFixed(values.amplitude)}%`],
    ['每股收益', formatFixed(summary.eps)],
    ['每股净资产', formatFixed(summary.bps)],
    ['总股本', formatChinese(values.totalShares)],
    ['总市值', formatChinese(values.totalMarketCap)],
    ['流通股', formatChinese(values.circulatingShares)],
    ['流通值', formatChinese(values.circulatingMarketCap)],
    ['52周最高', formatFixed(week52.high)],
    ['52周最低', formatFixed(week52.low)],
    ['货币单位', summary.currency || 'CNY'],
  ];

  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>
        {name || summary.name || symbol} ({symbol})
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', flexWrap: 'wrap', gap: '4px 12px', marginBottom: 12 }}>
        <span style={{ color: directionColor, fontSize: 26, fontWeight: 700 }}>
          {values.last === null ? '--' : `${currencySymbol}${values.last.toFixed(2)}`}
        </span>
        <span style={{ color: directionColor, fontWeight: 600 }}>
          {values.change === null ? '--' : `${values.change >= 0 ? '+' : ''}${values.change.toFixed(2)}`}
        </span>
        <span style={{ color: directionColor, fontWeight: 600 }}>
          {values.changePct === null ? '--' : `${values.changePct >= 0 ? '+' : ''}${values.changePct.toFixed(2)}%`}
        </span>
        <span style={{ color: '#8c8c8c' }}>{status} {formatTickTime(quote.hs_time || quote.updated_at)}</span>
      </div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: '2px 18px',
        padding: '10px 14px',
        background: '#fafafa',
        border: '1px solid #f0f0f0',
        borderRadius: 6,
      }}>
        {metrics.map(([label, value]) => <Metric key={label} label={label} value={value} />)}
      </div>
    </div>
  );
};

export default AStockQuoteSummary;
