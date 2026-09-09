import React from 'react';
import { Alert, Descriptions, Typography } from 'antd';
import './ValueInvestingDetail.css';

const { Text } = Typography;

export const formatPercent = (value, digits = 1) => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : `${Number(value).toFixed(digits)}%`
);

export const formatNumber = (value, digits = 2) => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : Number(value).toFixed(digits)
);

export const signedClassName = value => {
  const number = Number(value);
  if (!Number.isFinite(number)) return '';
  if (number > 0) return 'is-positive';
  if (number < 0) return 'is-negative';
  return '';
};

export const SignedPercent = ({ value, digits = 1 }) => (
  <Text className={signedClassName(value)}>{formatPercent(value, digits)}</Text>
);

/**
 * 一只股票在价值投资口径下的完整估值假设明细。
 *
 * 「价值投资扫描」的展开行和个股详情页共用这一份渲染：两处展示的是同一个接口算出的
 * 同一组字段，各写一份迟早会漂移成两套说法。
 */
const ValueInvestingDetail = ({ record, column = 3 }) => {
  if (!record) return null;
  const items = record.is_financial
    ? [
        { key: 'roe', label: '近5年平均ROE', children: formatPercent(record.avg_roe_pct) },
        { key: 'coe', label: '股权成本(CAPM)', children: formatPercent(record.cost_of_equity_pct) },
        { key: 'beta', label: 'Beta', children: formatNumber(record.beta) },
        { key: 'roe_used', label: '采用ROE(加权平均)', children: formatPercent(record.financial_roe_pct) },
        { key: 'justified_pb', label: '合理市净率 fair P/B', children: formatNumber(record.justified_pb) },
        { key: 'pb', label: '当前PB', children: formatNumber(record.pb) },
        {
          key: 'implied_roe',
          label: '市场隐含可持续ROE',
          children: formatPercent(record.implied_sustainable_roe_pct),
        },
        {
          key: 'roe_gap',
          label: '财报ROE − 市场隐含ROE',
          children: <SignedPercent value={record.roe_vs_implied_gap_pct} />,
        },
        {
          key: 'method',
          label: '估值方法',
          children: `剩余收益模型 P/B = 1 + Σ(ROE_t − 股权成本)×(1+g)^(t-1)/(1+r)^t，ROE 在 ${
            record.excess_return_fade_years ?? 10
          } 年内线性衰减到股权成本。金融机构的FCFF不适用DCF；该框架捕捉不到市场对资产质量的定价，「财报ROE − 市场隐含ROE」的差距就是需要人工判断的部分，结果不可直接采信。`,
        },
      ]
    : [
        { key: 'roic', label: '近5年平均ROIC', children: formatPercent(record.avg_roic_pct) },
        { key: 'wacc', label: 'WACC', children: formatPercent(record.wacc_pct) },
        { key: 'spread', label: 'ROIC−WACC价差', children: <SignedPercent value={record.roic_wacc_spread_pct} /> },
        { key: 'beta', label: 'Beta', children: formatNumber(record.beta) },
        { key: 'coe', label: '股权成本(CAPM)', children: formatPercent(record.cost_of_equity_pct) },
        { key: 'cod', label: '税后债权成本', children: formatPercent(record.cost_of_debt_after_tax_pct) },
        {
          key: 'tax_rate',
          label: '实际税率',
          children:
            record.effective_tax_rate_pct == null
              ? '-'
              : `${formatPercent(record.effective_tax_rate_pct)}（${
                  {
                    latest_annual: '最新年报',
                    five_year_window: '五年窗口合计',
                    tushare_tax_to_ebt: 'tushare指标',
                    default_fallback: '默认假设',
                  }[record.effective_tax_rate_source] || '—'
                }）`,
        },
        {
          key: 'interest_debt',
          label: '有息负债(亿)',
          children:
            record.interest_bearing_debt_yi == null
              ? '-'
              : `${formatNumber(record.interest_bearing_debt_yi)}（${
                  record.interest_bearing_debt_source === 'balancesheet_components'
                    ? '资产负债表加总'
                    : 'tushare指标'
                }${
                  record.interest_bearing_debt_cross_check_ratio == null
                    ? ''
                    : `，报表/指标 ${formatNumber(record.interest_bearing_debt_cross_check_ratio)}倍`
                }）`,
        },
        { key: 'base_nopat', label: '基准NOPAT(近3年均值,亿)', children: formatNumber(record.dcf_base_nopat_yi) },
        {
          key: 'roic_dcf',
          label: 'ROIC(决定增长成本)',
          children:
            record.dcf_roic_pct == null
              ? '-'
              : `${formatPercent(record.dcf_roic_pct)}（${
                  record.dcf_roic_source === 'computed_nopat_over_invested_capital' ? '自算' : 'tushare'
                }${
                  record.roic_cross_check_ratio == null
                    ? ''
                    : `，自算/tushare ${formatNumber(record.roic_cross_check_ratio)}倍`
                }）`,
        },
        { key: 'near_term_growth', label: '近端增速(DCF采用)', children: <SignedPercent value={record.near_term_growth_pct} /> },
        {
          key: 'growth_sources',
          label: '增速来源(营收/EBIT/净利)',
          children: `${formatPercent(record.revenue_cagr_pct)} / ${formatPercent(record.ebit_cagr_pct)} / ${formatPercent(record.profit_cagr_pct)}`,
        },
        { key: 'terminal_growth', label: '永续增长率', children: formatPercent(record.terminal_growth_pct) },
        { key: 'terminal_roic', label: '终值期ROIC(向WACC收敛一半)', children: formatPercent(record.dcf_terminal_roic_pct) },
        {
          key: 'terminal_reinvest',
          label: '终值期再投资率(g/ROIC)',
          children: formatPercent(record.dcf_terminal_reinvestment_rate_pct),
        },
        { key: 'ev', label: '企业价值(DCF,亿)', children: formatNumber(record.dcf_enterprise_value_yi) },
        { key: 'terminal_share', label: '终值占企业价值', children: formatPercent(record.dcf_terminal_value_share_pct) },
        { key: 'net_debt', label: '净债务(亿)', children: formatNumber(record.dcf_net_debt_yi) },
        { key: 'parent_share', label: '归母利润占比', children: formatPercent(record.parent_profit_share_pct) },
        {
          key: 'minority_interest',
          label: '少数股东扣除(亿)',
          children:
            record.dcf_minority_interest_yi == null
              ? '-'
              : `${formatNumber(record.dcf_minority_interest_yi)}（${
                  record.dcf_minority_basis === 'proportionate' ? '按归母利润占比折算' : '账面少数股东权益'
                }，账面 ${formatNumber(record.dcf_minority_book_yi)}）`,
        },
        { key: 'equity_value', label: '归母股权价值(DCF,亿)', children: formatNumber(record.dcf_equity_value_yi) },
        { key: 'market_cap', label: '当前市值(亿)', children: formatNumber(record.market_cap_yi) },
        {
          key: 'cash_fcff',
          label: '交叉验证-报表口径FCFF(亿)',
          children:
            record.cash_fcff_yi == null
              ? '-'
              : `${formatNumber(record.cash_fcff_yi)}${
                  record.fcff_cross_check_ratio == null
                    ? ''
                    : `（tushare/报表 ${formatNumber(record.fcff_cross_check_ratio)}倍）`
                }`,
        },
        { key: 'ocf_to_np', label: '经营现金流/净利润', children: formatNumber(record.ocf_to_net_profit) },
      ];
  const crossCheckItems = [
    {
      key: 'value_growth',
      label: '内在价值同比(最新年报 vs 去年年报)',
      children: <SignedPercent value={record.value_growth_pct} />,
    },
    { key: 'reversion', label: '交叉验证-估值均值回归', children: <SignedPercent value={record.reversion_return_pct} /> },
    { key: 'earnings_yield', label: '交叉验证-市盈率倒数(E/P)', children: <SignedPercent value={record.earnings_yield_pct} /> },
    { key: 'fcf_yield', label: '交叉验证-FCFF收益率', children: <SignedPercent value={record.fcf_yield_pct} /> },
  ];
  return (
    <div className="value-investing-detail-wrap">
      {record.dcf_unavailable_reason && (
        <Alert type="warning" showIcon className="value-investing-detail-warning" message={record.dcf_unavailable_reason} />
      )}
      <Descriptions
        size="small"
        column={column}
        className="value-investing-detail"
        items={[...items, ...crossCheckItems]}
      />
    </div>
  );
};

export default ValueInvestingDetail;
