import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Segmented, Spin, Table, Tabs, Tag, Tooltip, Typography } from 'antd';
import request from '../utils/request';
import { formatChineseAmount } from '../utils/format';
import './StockFinancialsCard.css';

const { Text } = Typography;

const toNumber = value => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

// 几个报表上没有、但人人都要看的派生量，统一在这里按报告期现算
const sum = (...values) => {
  const present = values.map(toNumber).filter(value => value !== null);
  return present.length ? present.reduce((total, value) => total + value, 0) : null;
};
const DERIVED = {
  interestBearingDebt: period => sum(
    period.balancesheet.st_borr, period.balancesheet.lt_borr, period.balancesheet.bond_payable,
  ),
  cashFreeCashflow: period => {
    const ocf = toNumber(period.cashflow.n_cashflow_act);
    const capex = toNumber(period.cashflow.c_pay_acq_const_fiolta);
    return ocf !== null && capex !== null ? ocf - capex : null;
  },
  depreciationAmortization: period => sum(
    period.cashflow.depr_fa_coga_dpba, period.cashflow.amort_intang_assets,
  ),
  ocfToNetProfit: period => {
    const ocf = toNumber(period.cashflow.n_cashflow_act);
    const profit = toNumber(period.cashflow.net_profit) ?? toNumber(period.income.n_income);
    return ocf !== null && profit ? ocf / profit : null;
  },
};

const FORMATTERS = {
  amount: value => formatChineseAmount(value),
  pct: value => `${value.toFixed(2)}%`,
  days: value => `${value.toFixed(0)}天`,
  ratio: value => value.toFixed(2),
  perShare: value => value.toFixed(3),
};

// A股惯例红涨绿跌：同比为正标红、为负标绿
const signedClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');

const row = (key, label, get, format, extra = {}) => ({ key, label, get, format, ...extra });
const pick = (section, field) => period => period[section]?.[field];

const TABS = [
  {
    key: 'profitability',
    label: '盈利能力',
    rows: [
      row('gpm', '销售毛利率', pick('indicator', 'grossprofit_margin'), 'pct'),
      row('npm', '销售净利率', pick('indicator', 'netprofit_margin'), 'pct'),
      row('roe', '净资产收益率(摊薄)', pick('indicator', 'roe'), 'pct', {
        hint: '归母净利润 ÷ 期末归母净资产。期末口径，报告期内增发/回购会让它和加权口径拉开差距',
      }),
      row('roe_waa', '净资产收益率(加权)', pick('indicator', 'roe_waa'), 'pct', {
        hint: '分母用报告期内加权平均净资产，年报/业绩快报通常披露的是这个口径',
      }),
      row('roe_dt', '净资产收益率(扣非摊薄)', pick('indicator', 'roe_dt'), 'pct', {
        hint: '分子换成扣除非经常性损益后的净利润，剔掉卖资产、政府补助这类一次性收益',
      }),
      row('roa', '总资产收益率', pick('indicator', 'roa'), 'pct'),
      row('roic', '投入资本回报率', pick('indicator', 'roic'), 'pct'),
      row('eps', '每股收益', pick('indicator', 'eps'), 'perShare'),
      row('dt_eps', '扣非每股收益', pick('indicator', 'dt_eps'), 'perShare'),
      row('bps', '每股净资产', pick('indicator', 'bps'), 'perShare'),
      row('ocfps', '每股经营现金流', pick('indicator', 'ocfps'), 'perShare'),
    ],
  },
  {
    key: 'income',
    label: '利润表',
    rows: [
      row('total_revenue', '营业总收入', pick('income', 'total_revenue'), 'amount'),
      row('revenue', '营业收入', pick('income', 'revenue'), 'amount'),
      row('oper_cost', '营业成本', pick('income', 'oper_cost'), 'amount'),
      row('gross_profit', '毛利', pick('income', 'gross_profit'), 'amount', { hint: '营业收入 − 营业成本' }),
      row('sell_exp', '销售费用', pick('income', 'sell_exp'), 'amount'),
      row('admin_exp', '管理费用', pick('income', 'admin_exp'), 'amount'),
      row('rd_exp', '研发费用', pick('income', 'rd_exp'), 'amount'),
      row('fin_exp', '财务费用', pick('income', 'fin_exp'), 'amount'),
      row('operate_profit', '营业利润', pick('income', 'operate_profit'), 'amount'),
      row('total_profit', '利润总额', pick('income', 'total_profit'), 'amount'),
      row('income_tax', '所得税费用', pick('income', 'income_tax'), 'amount'),
      row('n_income', '净利润', pick('income', 'n_income'), 'amount'),
      row('n_income_attr_p', '归母净利润', pick('income', 'n_income_attr_p'), 'amount', { strong: true }),
      row('minority_gain', '少数股东损益', pick('income', 'minority_gain'), 'amount'),
      row('ebitda', 'EBITDA', pick('income', 'ebitda'), 'amount'),
    ],
  },
  {
    key: 'balancesheet',
    label: '资产负债表',
    rows: [
      row('total_assets', '资产总计', pick('balancesheet', 'total_assets'), 'amount', { strong: true }),
      row('total_cur_assets', '流动资产合计', pick('balancesheet', 'total_cur_assets'), 'amount'),
      row('money_cap', '货币资金', pick('balancesheet', 'money_cap'), 'amount'),
      row('accounts_receiv', '应收账款', pick('balancesheet', 'accounts_receiv'), 'amount'),
      row('inventories', '存货', pick('balancesheet', 'inventories'), 'amount'),
      row('fix_assets', '固定资产', pick('balancesheet', 'fix_assets'), 'amount'),
      row('cip', '在建工程', pick('balancesheet', 'cip'), 'amount'),
      row('goodwill', '商誉', pick('balancesheet', 'goodwill'), 'amount'),
      row('total_liab', '负债合计', pick('balancesheet', 'total_liab'), 'amount', { strong: true }),
      row('total_cur_liab', '流动负债合计', pick('balancesheet', 'total_cur_liab'), 'amount'),
      row('interest_debt', '有息负债', DERIVED.interestBearingDebt, 'amount', {
        hint: '短期借款 + 长期借款 + 应付债券（不含一年内到期的非流动负债与租赁负债，是偏保守的下限）',
      }),
      row('equity', '归母股东权益', pick('balancesheet', 'total_hldr_eqy_exc_min_int'), 'amount', { strong: true }),
      row('minority_int', '少数股东权益', pick('balancesheet', 'minority_int'), 'amount'),
      row('debt_to_assets', '资产负债率', pick('indicator', 'debt_to_assets'), 'pct'),
      row('current_ratio', '流动比率', pick('indicator', 'current_ratio'), 'ratio'),
      row('quick_ratio', '速动比率', pick('indicator', 'quick_ratio'), 'ratio'),
    ],
  },
  {
    key: 'cashflow',
    label: '现金流量表',
    rows: [
      row('ocf', '经营活动现金流净额', pick('cashflow', 'n_cashflow_act'), 'amount', { strong: true }),
      row('icf', '投资活动现金流净额', pick('cashflow', 'n_cashflow_inv_act'), 'amount'),
      row('fcf_fnc', '筹资活动现金流净额', pick('cashflow', 'n_cash_flows_fnc_act'), 'amount'),
      row('capex', '资本开支', pick('cashflow', 'c_pay_acq_const_fiolta'), 'amount', {
        hint: '购建固定资产、无形资产和其他长期资产支付的现金',
      }),
      row('cash_fcf', '自由现金流(报表口径)', DERIVED.cashFreeCashflow, 'amount', {
        hint: '经营活动现金流净额 − 资本开支',
      }),
      row('da', '折旧与摊销', DERIVED.depreciationAmortization, 'amount', {
        hint: '固定资产折旧 + 无形资产摊销。资本开支长期远大于它，说明在扩产',
      }),
      row('ocf_np', '经营现金流 ÷ 净利润', DERIVED.ocfToNetProfit, 'ratio', {
        hint: '利润有没有真的收成现金。长期明显小于1要警惕应收、存货在吃利润',
      }),
    ],
  },
  {
    key: 'growth',
    label: '成长与营运',
    rows: [
      row('or_yoy', '营业收入同比', pick('indicator', 'or_yoy'), 'pct', { signed: true }),
      row('np_yoy', '归母净利润同比', pick('indicator', 'netprofit_yoy'), 'pct', { signed: true }),
      row('dt_np_yoy', '扣非归母净利润同比', pick('indicator', 'dt_netprofit_yoy'), 'pct', { signed: true }),
      row('ocf_yoy', '经营现金流同比', pick('indicator', 'ocf_yoy'), 'pct', { signed: true }),
      row('assets_turn', '总资产周转率', pick('indicator', 'assets_turn'), 'ratio'),
      row('arturn_days', '应收账款周转天数', pick('indicator', 'arturn_days'), 'days'),
      row('invturn_days', '存货周转天数', pick('indicator', 'invturn_days'), 'days'),
    ],
  },
];

const buildColumns = periods => [
  {
    title: '指标',
    dataIndex: 'label',
    key: 'label',
    fixed: 'left',
    width: 168,
    render: (label, record) => (
      <span className="stock-financials__label">
        {record.hint
          ? <Tooltip title={record.hint}><Text strong={record.strong} underline>{label}</Text></Tooltip>
          : <Text strong={record.strong}>{label}</Text>}
      </span>
    ),
  },
  ...periods.map(period => ({
    title: (
      <span className="stock-financials__period">
        <span>
          {period.period_label}
          {!period.is_annual && <Tag bordered={false} style={{ marginInlineStart: 4, marginInlineEnd: 0 }}>累计</Tag>}
        </span>
        <span className="stock-financials__period-date">{period.end_date}</span>
      </span>
    ),
    dataIndex: period.end_date,
    key: period.end_date,
    align: 'right',
    width: 128,
    render: (_, record) => {
      const value = toNumber(record.get(period));
      if (value === null) return <Text type="secondary">--</Text>;
      const text = FORMATTERS[record.format](value);
      return (
        <span className={`stock-financials__cell ${record.signed ? signedClass(value) : ''}`}>
          {record.strong ? <strong>{text}</strong> : text}
        </span>
      );
    },
  })),
];

/**
 * 个股详情页的财务数据卡片：三张表的核心科目 + 盈利能力/营运/偿债/成长指标。
 *
 * 和「价值投资基本面」卡片分工：那边展示**算出来的**东西（质量闸门、DCF、市场隐含假设），
 * 这里只展示**报表本身**。人得先看见财报长什么样，才谈得上判断估值合不合理。
 */
const StockFinancialsCard = ({ symbol }) => {
  const [annualOnly, setAnnualOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [periods, setPeriods] = useState([]);
  const [error, setError] = useState('');

  const fetchFinancials = useCallback(async () => {
    if (!symbol) return;
    setLoading(true);
    setError('');
    try {
      const { data } = await request.get(`/api/stock/a-stock/financials/${symbol}`, {
        params: { periods: 8, annual_only: annualOnly },
      });
      setPeriods(data?.periods || []);
    } catch (requestError) {
      console.error('获取财务数据失败:', requestError);
      setPeriods([]);
      setError('获取财务数据失败');
    } finally {
      setLoading(false);
    }
  }, [symbol, annualOnly]);

  useEffect(() => {
    fetchFinancials();
  }, [fetchFinancials]);

  const columns = useMemo(() => buildColumns(periods), [periods]);
  const hasInterim = periods.some(period => !period.is_annual);

  const tabItems = TABS.map(tab => ({
    key: tab.key,
    label: tab.label,
    children: (
      <Table
        rowKey="key"
        size="small"
        columns={columns}
        dataSource={tab.rows}
        pagination={false}
        scroll={{ x: 168 + periods.length * 128 }}
      />
    ),
  }));

  return (
    <Card className="stock-financials" size="small" title="财务数据">
      <div className="stock-financials__toolbar">
        <Text type="secondary">
          {periods.length ? `最近 ${periods.length} 个报告期，最新 ${periods[0].period_label}` : ''}
        </Text>
        <Segmented
          size="small"
          value={annualOnly ? 'annual' : 'all'}
          onChange={value => setAnnualOnly(value === 'annual')}
          options={[{ label: '全部报告期', value: 'all' }, { label: '仅年报', value: 'annual' }]}
        />
      </div>
      <Spin spinning={loading}>
        {error && <Alert type="error" showIcon message={error} />}
        {!error && !loading && periods.length === 0 && (
          <Alert type="info" showIcon message="分析库里暂无这只股票的财务报表数据" />
        )}
        {periods.length > 0 && <Tabs size="small" items={tabItems} />}
      </Spin>
      {hasInterim && (
        <Text type="secondary" className="stock-financials__note">
          标「累计」的列是年初至今累计数（中报＝上半年、三季报＝前三季度），不是全年；
          比率类指标同样是该报告期口径，未年化，不能直接和年报列横向比较。
        </Text>
      )}
    </Card>
  );
};

export default StockFinancialsCard;
