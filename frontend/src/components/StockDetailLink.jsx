import React from 'react';
import { toXueqiuSymbol } from './XueqiuStockLink';


export const toStockDetailSymbol = value => {
  const symbol = String(value || '').trim().toUpperCase();
  if (!symbol) return null;
  const xueqiuSymbol = toXueqiuSymbol(symbol);
  const aShareMatch = xueqiuSymbol?.match(/^(SH|SZ|BJ)(\d{6})$/);
  if (aShareMatch) return `${aShareMatch[2]}.${aShareMatch[1]}`;
  return symbol;
};

const StockDetailLink = ({ symbol, children, className = '', ...props }) => {
  const detailSymbol = toStockDetailSymbol(symbol);
  const label = children ?? symbol ?? '-';
  if (!detailSymbol) return <span className={className}>{label}</span>;
  return (
    <a
      {...props}
      className={className}
      href={`/stock/${encodeURIComponent(detailSymbol)}`}
      target="_blank"
      rel="noopener noreferrer"
      onClick={event => event.stopPropagation()}
      title={`查看 ${symbol} 详情`}
    >
      {label}
    </a>
  );
};

export default StockDetailLink;
