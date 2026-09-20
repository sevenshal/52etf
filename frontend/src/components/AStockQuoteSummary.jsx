import React, { useMemo } from 'react';
import dayjs from 'dayjs';
import { Tooltip } from 'antd';
import { Switch } from 'antd';
import { formatChineseAmount as formatChinese } from '../utils/format';
import { scaleToLivePrice } from '../utils/valuationMetrics';

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

const formatFixed = (value, digits = 2) => {
  const number = toNumber(value);
  return number === null ? '--' : number.toFixed(digits);
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

const Metric = ({ label, value, hint }) => (
  <div style={{ minWidth: 0, color: '#666', lineHeight: 1.8 }}>
    {hint
      ? <Tooltip title={hint}><span style={{ borderBottom: '1px dotted #bfbfbf' }}>{label}</span></Tooltip>
      : label}
    ：<strong style={{ color: '#262626', fontWeight: 600 }}>{value}</strong>
  </div>
);

const CONSENSUS_UNAVAILABLE_REASONS = {
  no_target_price_in_pool: 'T-1期披露日之后没有能给出估值的研报，不计算估值',
  disclosure_missing: '缺少定期报告披露日，无法划定研报池',
  market_data_missing: '缺少行情数据',
};

// 卖方一致预期的三个财年估值上下限；点击某个上下限打开各机构研报明细。
const PE_BAND_UNAVAILABLE_REASONS = {
  insufficient_history: '前瞻PE有效样本不足一年',
  unprofitable_or_uncovered: '近3年较多交易日没有正的一致预期EPS（亏损或无人覆盖）',
  unstable_multiple: '近3年前瞻PE波动过大（80%分位超过20%分位3倍），通道不可信',
};

const describePeBand = band => {
  if (!band) return '--';
  if (band.status !== 'available') return PE_BAND_UNAVAILABLE_REASONS[band.reason] || '不可用';
  const current = toNumber(band.current_pe);
  return `${formatFixed(band.low_pe, 1)} ~ ${formatFixed(band.high_pe, 1)}倍`
    + (current === null ? '' : `（当前 ${current.toFixed(1)}倍）`);
};

const ConsensusValuationMetrics = ({ consensus, onOpenConsensus, peBandEnabled, onTogglePeBand }) => {
  if (!consensus) return null;
  const available = consensus.status === 'available';
  const renderBound = (horizon, bound, color) => {
    const value = toNumber(horizon?.[bound]);
    if (value === null) return '--';
    const org = horizon[`${bound}_org`];
    return (
      <button
        type="button"
        onClick={() => onOpenConsensus?.(horizon.offset, bound)}
        style={{
          color, padding: 0, border: 0, background: 'none', cursor: 'pointer', font: 'inherit', fontWeight: 600,
        }}
        title={`${bound === 'lo' ? '下限' : '上限'}来自${org || '--'}，点击查看各机构研报`}
      >
        {value.toFixed(2)}
      </button>
    );
  };
  const poolTip = available
    ? `T期 ${consensus.t_period_label || '-'}（${consensus.t_disclosure_date || '-'} 披露），`
      + `研报取自 ${consensus.pool_start_date} 之后`
      + (consensus.pool === 'T-1' ? '；T期后给出目标价的机构不足2家，已退到T-1期' : '')
    : CONSENSUS_UNAVAILABLE_REASONS[consensus.reason] || '暂无一致预期估值';
  const metrics = available
    ? [
      ...(consensus.horizons || []).map(horizon => [
        `${horizon.label}估值(${horizon.fiscal_year})`,
        <span>{renderBound(horizon, 'lo', '#0066FF')} ~ {renderBound(horizon, 'hi', '#FF0000')}</span>,
      ]),
      [
        '估值研报池',
        <span title={poolTip} style={{ color: consensus.pool === 'T' ? '#389e0d' : '#d46b08' }}>
          {consensus.pool === 'T' ? 'T池' : 'T-1池（待更新）'} · {consensus.organization_count}家机构
          {consensus.method_counts?.pe_band ? `（${consensus.method_counts.pe_band}家用PE通道）` : ''}
        </span>,
      ],
    ]
    : [['一致预期估值', <span title={poolTip}>--</span>]];
  if (onTogglePeBand) {
    metrics.push([
      'PE通道补估值',
      <Switch
        size="small"
        checked={Boolean(peBandEnabled)}
        onChange={onTogglePeBand}
        title="开启后，没给目标价的机构用该股近3年前瞻PE的20%~80%分位 × 该机构未来12个月EPS估值"
      />,
    ]);
    if (peBandEnabled) {
      metrics.push(['前瞻PE通道(近3年)', <span>{describePeBand(consensus.pe_band)}</span>]);
    }
  }

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
      gap: '2px 18px',
      marginTop: 8,
      padding: '10px 14px',
      background: '#f0f5ff',
      border: '1px solid #d6e4ff',
      borderRadius: 6,
    }}>
      {metrics.map(([label, value]) => <Metric key={label} label={label} value={value} />)}
    </div>
  );
};


// 雪球持仓排行：只有最新一期快照仍在榜上才显示 #N；已跌出榜单显示"未上榜"并注明最后一次上榜
const renderXueqiuRank = (rank) => {
  if (rank.onLatest === false) {
    const tip = rank.lastDate
      ? `最近一次上榜是 ${rank.lastDate}，当时第 ${rank.rank} 名；最新快照（${rank.latestSnapshotDate}）已不在榜上`
      : '雪球组合持仓快照里没有这只股票';
    return <Tooltip title={tip}><span style={{ color: '#8c8c8c' }}>未上榜</span></Tooltip>;
  }
  const text = rank.rank === null || rank.rank === undefined ? '--' : `#${rank.rank}`;
  const tip = rank.onLatest === null
    ? `截至 ${rank.lastDate} 的排名（未能确认最新快照日，不保证是当前排名）`
    : `最新快照 ${rank.lastDate}，按雪球组合综合持仓权重排名`;
  return <Tooltip title={tip}><span>{text}</span></Tooltip>;
};

const AStockQuoteSummary = ({
  xueqiuRank,
  quote = {},
  summary = {},
  week52 = {},
  consensus = null,
  onOpenConsensus,
  peBandEnabled = false,
  onTogglePeBand,
}) => {
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
    const valuationClose = toNumber(summary.valuation_close);
    const priceRatio = last !== null && valuationClose > 0 ? last / valuationClose : null;
    const dynamicPe = toNumber(quote.pe_rate);
    const livePb = toNumber(quote.pb_rate) ?? scaleToLivePrice(summary.pb, priceRatio);
    const dividendTtm = valuationClose !== null && toNumber(summary.dv_ttm) !== null
      ? valuationClose * Number(summary.dv_ttm) / 100
      : null;
    // 快照里的 pe/pb/ps 是上一个收盘口径，盘中一律按当前价折算
    const peTtm = scaleToLivePrice(summary.pe_ttm, priceRatio);
    return {
      last, preclose, high, low, change, changePct, amplitude,
      totalShares, circulatingShares, dynamicPe, livePb, dividendTtm, peTtm,
      dynamicEps: last !== null && dynamicPe > 0 ? last / dynamicPe : null,
      bps: last !== null && livePb > 0 ? last / livePb : null,
      pe: scaleToLivePrice(summary.pe, priceRatio),
      psTtm: scaleToLivePrice(summary.ps_ttm, priceRatio),
      ps: scaleToLivePrice(summary.ps, priceRatio),
      // 扣非ROE 是财报口径的比率，不随盘中股价变动，直接用接口给的值
      roeDtTtm: toNumber(summary.roe_dt_ttm_pct),
      dividendYieldTtm: dividendTtm !== null && last > 0 ? dividendTtm / last * 100 : null,
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
    ['市盈率(动)', formatFixed(values.dynamicPe)],
    ['市盈率(TTM)', formatFixed(values.peTtm)],
    ['市盈率(静)', formatFixed(values.pe)],
    ['市净率', formatFixed(values.livePb)],
    ['委比', toNumber(quote.entrust_rate) === null ? '--' : `${formatFixed(quote.entrust_rate)}%`],
    ['振幅', values.amplitude === null ? '--' : `${formatFixed(values.amplitude)}%`],
    ['每股收益(动)', formatFixed(values.dynamicEps)],
    ['股息(TTM)', formatFixed(values.dividendTtm)],
    ['股息率(TTM)', values.dividendYieldTtm === null ? '--' : `${formatFixed(values.dividendYieldTtm)}%`],
    ['每股净资产', formatFixed(values.bps)],
    ['市销率(TTM)', formatFixed(values.psTtm)],
    ['市销率(静)', formatFixed(values.ps)],
    [
      '扣非ROE(TTM)',
      values.roeDtTtm === null ? '--' : `${formatFixed(values.roeDtTtm)}%`,
      '最近 12 个月扣除非经常性损益的归母净利 ÷ 最新一期期末归母净资产。'
      + '扣掉卖资产、政府补助这类一次性收益，看的是主业本身的回报；'
      + '非年报期按「最新累计 + 上一年年报 − 去年同期累计」滚动。'
      + `数据截至 ${summary.roe_dt_ttm_period || '最新报告期'}。`,
    ],
    ['总股本', formatChinese(values.totalShares)],
    ['总市值', formatChinese(values.totalMarketCap)],
    ['流通股', formatChinese(values.circulatingShares)],
    ['流通值', formatChinese(values.circulatingMarketCap)],
    ['52周最高', formatFixed(week52.high)],
    ['52周最低', formatFixed(week52.low)],
    ['货币单位', summary.currency || 'CNY'],
    // 雪球持仓排行只有管理员拿得到（接口专属），没有就不显示这一项
    ...(xueqiuRank ? [['雪球持仓排行', renderXueqiuRank(xueqiuRank)]] : []),
  ];

  return (
    <div style={{ marginBottom: 20 }}>
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
        {metrics.map(([label, value, hint]) => (
          <Metric key={label} label={label} value={value} hint={hint} />
        ))}
      </div>
      <ConsensusValuationMetrics
        consensus={consensus}
        onOpenConsensus={onOpenConsensus}
        peBandEnabled={peBandEnabled}
        onTogglePeBand={onTogglePeBand}
      />
    </div>
  );
};

export default AStockQuoteSummary;
