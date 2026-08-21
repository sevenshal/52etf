import React from 'react';


export const toXueqiuSymbol = value => {
  const symbol = String(value || '').trim().toUpperCase();
  if (!symbol) return null;

  let match = symbol.match(/^(\d{6})\.(SH|SZ|BJ)$/);
  if (match) return `${match[2]}${match[1]}`;

  match = symbol.match(/^(SH|SZ|BJ)[._](\d{6})$/);
  if (match) return `${match[1]}${match[2]}`;

  match = symbol.match(/^(SH|SZ|BJ)(\d{6})$/);
  if (match) return `${match[1]}${match[2]}`;

  if (/^\d{6}$/.test(symbol)) {
    if (/^(4|8|92)/.test(symbol)) return `BJ${symbol}`;
    if (/^[5679]/.test(symbol)) return `SH${symbol}`;
    return `SZ${symbol}`;
  }

  match = symbol.match(/^([A-Z][A-Z0-9.-]*)\.US$/);
  if (match) return match[1];
  return null;
};

export const toXueqiuStockUrl = value => {
  const symbol = toXueqiuSymbol(value);
  return symbol ? `https://xueqiu.com/S/${encodeURIComponent(symbol)}` : undefined;
};

export const toXueqiuPortfolioUrl = value => {
  const symbol = String(value || '').trim().toUpperCase();
  return symbol ? `https://xueqiu.com/P/${encodeURIComponent(symbol)}` : undefined;
};


const XueqiuStockLink = ({ symbol, children, className = '', ...props }) => {
  const href = toXueqiuStockUrl(symbol);
  const label = children ?? symbol ?? '-';
  if (!href) return <span className={className}>{label}</span>;
  return (
    <a
      {...props}
      className={`xueqiu-stock-link${className ? ` ${className}` : ''}`}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={event => event.stopPropagation()}
      title={`在雪球查看 ${symbol}`}
    >
      {label}
    </a>
  );
};

export const XueqiuPortfolioLink = ({ symbol, children, className = '', ...props }) => {
  const href = toXueqiuPortfolioUrl(symbol);
  const label = children ?? symbol ?? '-';
  if (!href) return <span className={className}>{label}</span>;
  return (
    <a
      {...props}
      className={`xueqiu-portfolio-link${className ? ` ${className}` : ''}`}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={event => event.stopPropagation()}
      title={`在雪球查看组合 ${symbol}`}
    >
      {label}
    </a>
  );
};

export default XueqiuStockLink;
