import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Card, Collapse, Space, Spin, Statistic, Tag, Tooltip, Typography } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import request from '../utils/request';
import ValueInvestingDetail, {
  formatNumber,
  formatPercent,
  signedClassName,
} from './ValueInvestingDetail';
import './StockValueInvestingCard.css';

const { Text, Paragraph } = Typography;

const STATUS_MESSAGES = {
  not_found: '分析库里没有这只股票的基本面数据，可能不是 A 股、已退市，或基础数据尚未同步。',
  fundamentals_not_synced: '这只股票的财务报表尚未同步到分析库，跑一次 A 股基础数据同步后才能算出估值。',
};

const Metric = ({ label, value, hint }) => (
  <div className="stock-value-investing__metric">
    {hint ? <Tooltip title={hint}>{label}</Tooltip> : label}
    ：
    <strong>{value}</strong>
  </div>
);

const percentileHint = '括号内是当前值在自身近5年历史中的分位数，越低越便宜';

/**
 * 个股详情页的价值投资基本面卡片。
 *
 * 数据走 /api/value-investing/stock/{ts_code}，与「价值投资扫描」页是同一套
 * ROIC-WACC 质量闸门 + DCF(金融股用合理市净率)口径，区别只是这里没通过闸门也把
 * 估值算完并显示——详情页要回答的是"差在哪、差多少"，不是"选不选得中"。
 */
const StockValueInvestingCard = ({ symbol }) => {
  const [loading, setLoading] = useState(false);
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState('');

  const fetchProfile = useCallback(async () => {
    if (!symbol) return;
    setLoading(true);
    setError('');
    try {
      const { data } = await request.get(`/api/value-investing/stock/${symbol}`);
      setProfile(data || null);
    } catch (requestError) {
      console.error('获取价值投资基本面失败:', requestError);
      setProfile(null);
      setError('获取价值投资基本面失败');
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    setProfile(null);
    fetchProfile();
  }, [fetchProfile]);

  const record = profile?.candidate;
  const statusMessage = error
    || (profile && profile.status !== 'completed'
      ? profile.message || STATUS_MESSAGES[profile.status] || '暂时算不出这只股票的价值投资估值'
      : '');

  const isFinancial = !!record?.is_financial;
  const returnClassName = signedClassName(record?.expected_return_pct);

  return (
    <Card
      className="stock-value-investing"
      size="small"
      title={(
        <Space size={6}>
          价值投资基本面
          <Tooltip title="质量闸门(ROIC能否持续跑赢WACC + 现金流验证利润) → 两阶段再投资口径FCFF DCF算内在价值(银行/保险/证券改用合理市净率) → 潜在回报率 = 内在价值 vs 当前价格。与「价值投资扫描」页同源同口径。">
            <InfoCircleOutlined />
          </Tooltip>
        </Space>
      )}
      extra={profile?.as_of ? <Text type="secondary">数据截至 {profile.as_of}</Text> : null}
    >
      <Spin spinning={loading}>
        {statusMessage && <Alert type="info" showIcon message={statusMessage} />}
        {record && (
          <>
            <div className="stock-value-investing__gate">
              <Space size={[6, 6]} wrap>
                {record.quality_passed
                  ? <Tag color="green">通过质量闸门</Tag>
                  : <Tag color="volcano">未通过质量闸门</Tag>}
                {(record.quality_reasons || []).map(reason => (
                  <Tag color="volcano" key={reason}>{reason}</Tag>
                ))}
                {(record.quality_notes || []).map(note => (
                  <Tag key={note}>{note}</Tag>
                ))}
                {record.industry && <Tag color="blue">{record.industry}</Tag>}
                {isFinancial && <Tag color="purple">金融口径(合理市净率)</Tag>}
              </Space>
            </div>

            <div className="stock-value-investing__headline">
              <Statistic
                title={(
                  <Tooltip title="非金融：两阶段FCFF DCF算出的归母股权价值 vs 当前市值；银行/保险/证券：合理市净率 vs 当前PB。括号内是贴现率±150bp的悲观~乐观区间。">
                    潜在回报率
                  </Tooltip>
                )}
                value={record.expected_return_pct == null ? '-' : formatPercent(record.expected_return_pct)}
                valueStyle={{ color: returnClassName === 'is-positive' ? '#389e0d' : (returnClassName === 'is-negative' ? '#cf1322' : undefined) }}
              />
              <Statistic
                title={isFinancial ? '合理市净率 / 当前PB' : '归母股权价值(亿) / 当前市值(亿)'}
                value={isFinancial
                  ? `${formatNumber(record.justified_pb)} / ${formatNumber(record.pb)}`
                  : `${formatNumber(record.dcf_equity_value_yi)} / ${formatNumber(record.market_cap_yi)}`}
              />
              <Statistic
                title={isFinancial ? '近5年平均ROE / 股权成本' : '近5年平均ROIC / WACC'}
                value={isFinancial
                  ? `${formatPercent(record.avg_roe_pct)} / ${formatPercent(record.cost_of_equity_pct)}`
                  : `${formatPercent(record.avg_roic_pct)} / ${formatPercent(record.wacc_pct)}`}
              />
              <Statistic
                title={(
                  <Tooltip title="用最新年报和去年年报两个切片跑同一套估值公式，差值就是基本面本身在变好还是变差，不受估值倍数重估影响">
                    内在价值同比
                  </Tooltip>
                )}
                value={record.value_growth_pct == null ? '-' : formatPercent(record.value_growth_pct)}
              />
            </div>

            {record.expected_return_pct != null
              && (record.expected_return_pct_bear != null || record.expected_return_pct_bull != null) && (
              <Paragraph type="secondary" className="stock-value-investing__return-range">
                悲观~乐观区间：{formatPercent(record.expected_return_pct_bear)} ~ {formatPercent(record.expected_return_pct_bull)}
              </Paragraph>
            )}

            {record.dcf_unavailable_reason && (
              <Alert type="warning" showIcon message={record.dcf_unavailable_reason} style={{ marginBottom: 12 }} />
            )}

            <div className="stock-value-investing__metrics">
              <Metric
                label="PE(TTM)"
                hint={percentileHint}
                value={`${formatNumber(record.pe_ttm)}${
                  record.pe_percentile_5y == null ? '' : `（${formatPercent(record.pe_percentile_5y, 0)}）`
                }`}
              />
              <Metric
                label="PB"
                hint={percentileHint}
                value={`${formatNumber(record.pb)}${
                  record.pb_percentile_5y == null ? '' : `（${formatPercent(record.pb_percentile_5y, 0)}）`
                }`}
              />
              {!isFinancial && (
                <Metric
                  label="ROIC−WACC价差"
                  hint="资本回报率跑赢资本成本才是在创造价值；为负意味着每多投一块钱都在毁灭价值"
                  value={(
                    <span className={signedClassName(record.roic_wacc_spread_pct)}>
                      {formatPercent(record.roic_wacc_spread_pct)}
                    </span>
                  )}
                />
              )}
              <Metric label="资产负债率" value={formatPercent(record.debt_to_assets_pct)} />
              <Metric
                label="经营现金流/净利润"
                hint="近5年均值，检验利润是不是真的收到了现金"
                value={formatNumber(record.ocf_to_net_profit)}
              />
              <Metric label="近5年FCFF为正年数" value={record.fcf_positive_years == null ? '-' : `${record.fcf_positive_years} / 5`} />
              <Metric label="Beta" value={formatNumber(record.beta)} />
              <Metric label="营收5年复合增速" value={formatPercent(record.revenue_cagr_pct)} />
              <Metric label="归母净利5年复合增速" value={formatPercent(record.profit_cagr_pct)} />
              <Metric label="估值均值回归(交叉验证)" value={formatPercent(record.reversion_return_pct)} />
              <Metric label="市盈率倒数E/P(交叉验证)" value={formatPercent(record.earnings_yield_pct)} />
              <Metric label="FCFF收益率(交叉验证)" value={formatPercent(record.fcf_yield_pct)} />
            </div>

            <Collapse
              className="stock-value-investing__assumptions"
              items={[
                {
                  key: 'detail',
                  label: '完整估值假设与交叉验证',
                  children: <ValueInvestingDetail record={record} column={2} />,
                },
              ]}
            />

            <Text type="secondary" className="stock-value-investing__disclaimer">
              无风险利率 {formatPercent(profile?.assumptions?.risk_free_rate_pct, 2)}
              （归一化后 {formatPercent(profile?.assumptions?.normalized_risk_free_rate_pct, 2)}）、
              股权风险溢价 {formatPercent(profile?.assumptions?.equity_risk_premium_pct, 2)}、
              永续增长率 {formatPercent(profile?.assumptions?.terminal_growth_rate_pct, 2)}。
              仅做研究提示，不构成投资建议。
            </Text>
          </>
        )}
      </Spin>
    </Card>
  );
};

export default StockValueInvestingCard;
