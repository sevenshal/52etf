import React from 'react';


const normalizeCode = value => {
  const text = String(value || '').trim().toUpperCase();
  const match = text.match(/^(?:SH|SZ|BJ)[._]?(\d{6})$/) || text.match(/^(\d{6})\.(?:SH|SZ|BJ)$/);
  return match?.[1] || null;
};

const EastmoneyStockLink = ({ symbol, children, className = '', ...props }) => {
  const code = normalizeCode(symbol);
  const label = children ?? symbol ?? '-';
  if (!code) return <span className={className}>{label}</span>;
  return (
    <a
      {...props}
      className={`eastmoney-stock-link${className ? ` ${className}` : ''}`}
      href={`https://quote.eastmoney.com/unify/r/1.${encodeURIComponent(code)}`}
      target="_blank"
      rel="noopener noreferrer"
      onClick={event => event.stopPropagation()}
      title={`在东方财富查看 ${symbol}`}
    >
      {label}
    </a>
  );
};

export const EastmoneyPortfolioLink = ({ children, className = '' }) => (
  <span className={className}>{children ?? '-'}</span>
);

export default EastmoneyStockLink;
