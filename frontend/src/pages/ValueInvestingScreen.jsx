import React, { useCallback, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Empty,
  InputNumber,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { InfoCircleOutlined, ReloadOutlined } from '@ant-design/icons';
import request from '../utils/request';
import XueqiuStockLink from '../components/XueqiuStockLink';
import './ValueInvestingScreen.css';

const { Text, Title, Paragraph } = Typography;

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(item => item?.msg || String(item)).join('；') || fallback;
  }
  return typeof detail === 'object' ? JSON.stringify(detail) : String(detail);
};

const formatPercent = (value, digits = 1) => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : `${Number(value).toFixed(digits)}%`
);

const formatNumber = (value, digits = 2) => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : Number(value).toFixed(digits)
);

const signedClassName = value => {
  const number = Number(value);
  if (!Number.isFinite(number)) return '';
  if (number > 0) return 'is-positive';
  if (number < 0) return 'is-negative';
  return '';
};

const SignedPercent = ({ value, digits = 1 }) => (
  <Text className={signedClassName(value)}>{formatPercent(value, digits)}</Text>
);

const STATUS_MESSAGES = {
  no_data: '分析库中暂无市场行情或股票基础信息，请先确认 A 股基础数据同步是否正常。',
  fundamentals_not_synced: (
    '资产负债表/现金流量表/财务指标/利润表财务数据尚未同步到分析库，'
    + '需要先跑一次包含新增财务报表缓存的 A 股基础数据同步才能扫描出结果。'
  ),
};

const DEFAULT_FILTERS = {
  minTotalMv: null,
  excludeSt: true,
  topN: 100,
  minRoicWaccSpreadPct: null,
  minOcfToNp: null,
  minFcfPositiveYears: null,
  maxDebtToAssets: null,
  minAvgRoeFinancial: null,
  minValueGrowthPct: null,
  riskFreeRatePct: null,
  equityRiskPremiumPct: null,
  terminalGrowthRatePct: null,
};

const expandedRowRender = record => {
  const items = record.is_financial
    ? [
        { key: 'roe', label: '近5年平均ROE', children: formatPercent(record.avg_roe_pct) },
        { key: 'coe', label: '股权成本(CAPM)', children: formatPercent(record.cost_of_equity_pct) },
        { key: 'beta', label: 'Beta', children: formatNumber(record.beta) },
        { key: 'justified_pb', label: '合理市净率 fair P/B', children: formatNumber(record.justified_pb) },
        { key: 'pb', label: '当前PB', children: formatNumber(record.pb) },
        {
          key: 'method',
          label: '估值方法',
          children: 'fair P/B = (ROE−永续增长率)/(股权成本−永续增长率)，银行/保险/证券的FCFF不适用DCF',
        },
      ]
    : [
        { key: 'roic', label: '近5年平均ROIC', children: formatPercent(record.avg_roic_pct) },
        { key: 'wacc', label: 'WACC', children: formatPercent(record.wacc_pct) },
        { key: 'spread', label: 'ROIC−WACC价差', children: <SignedPercent value={record.roic_wacc_spread_pct} /> },
        { key: 'beta', label: 'Beta', children: formatNumber(record.beta) },
        { key: 'coe', label: '股权成本(CAPM)', children: formatPercent(record.cost_of_equity_pct) },
        { key: 'cod', label: '税后债权成本', children: formatPercent(record.cost_of_debt_after_tax_pct) },
        { key: 'base_fcff', label: '基准FCFF(近3年均值,亿)', children: formatNumber(record.dcf_base_fcff_yi) },
        { key: 'fcff_cagr', label: 'FCFF复合增速', children: <SignedPercent value={record.fcff_cagr_pct} /> },
        { key: 'terminal_growth', label: '永续增长率', children: formatPercent(record.terminal_growth_pct) },
        { key: 'ev', label: '企业价值(DCF,亿)', children: formatNumber(record.dcf_enterprise_value_yi) },
        { key: 'net_debt', label: '净债务(亿)', children: formatNumber(record.dcf_net_debt_yi) },
        { key: 'equity_value', label: '股权价值(DCF,亿)', children: formatNumber(record.dcf_equity_value_yi) },
        { key: 'market_cap', label: '当前市值(亿)', children: formatNumber(record.market_cap_yi) },
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
      <Descriptions size="small" column={3} className="value-investing-detail" items={[...items, ...crossCheckItems]} />
    </div>
  );
};

const buildColumns = () => [
  {
    title: '#',
    key: 'rank',
    width: 48,
    render: (_, __, index) => index + 1,
  },
  {
    title: '股票',
    key: 'name',
    width: 160,
    render: (_, record) => (
      <Space direction="vertical" size={0}>
        <Text strong>{record.name || record.ts_code}</Text>
        <XueqiuStockLink symbol={record.ts_code}>{record.ts_code}</XueqiuStockLink>
      </Space>
    ),
  },
  {
    title: '行业',
    dataIndex: 'industry',
    key: 'industry',
    width: 110,
    render: value => value || '-',
  },
  {
    title: '现价',
    dataIndex: 'close',
    key: 'close',
    width: 80,
    align: 'right',
    render: value => formatNumber(value),
  },
  {
    title: 'PE(TTM)',
    key: 'pe_ttm',
    width: 110,
    align: 'right',
    render: (_, record) => (
      <Tooltip title="括号内为当前PE在自身近5年历史中的分位数，越低越便宜">
        {formatNumber(record.pe_ttm)}
        {record.pe_percentile_5y !== null && record.pe_percentile_5y !== undefined && (
          <Text type="secondary"> ({formatPercent(record.pe_percentile_5y, 0)})</Text>
        )}
      </Tooltip>
    ),
  },
  {
    title: 'PB',
    key: 'pb',
    width: 100,
    align: 'right',
    render: (_, record) => (
      <Tooltip title="括号内为当前PB在自身近5年历史中的分位数，越低越便宜">
        {formatNumber(record.pb)}
        {record.pb_percentile_5y !== null && record.pb_percentile_5y !== undefined && (
          <Text type="secondary"> ({formatPercent(record.pb_percentile_5y, 0)})</Text>
        )}
      </Tooltip>
    ),
  },
  {
    title: (
      <Tooltip title="非金融显示ROIC(资本回报率，资本结构中性)，银行/保险/证券显示ROE">
        ROIC/ROE
      </Tooltip>
    ),
    key: 'roic_or_roe',
    width: 100,
    align: 'right',
    sorter: (a, b) => {
      const av = a.is_financial ? a.avg_roe_pct : a.avg_roic_pct;
      const bv = b.is_financial ? b.avg_roe_pct : b.avg_roic_pct;
      return (av ?? -Infinity) - (bv ?? -Infinity);
    },
    render: (_, record) => formatPercent(record.is_financial ? record.avg_roe_pct : record.avg_roic_pct),
  },
  {
    title: (
      <Tooltip title="非金融显示WACC(加权资本成本)，银行/保险/证券显示股权成本(CAPM)">
        WACC/股权成本
      </Tooltip>
    ),
    key: 'wacc_or_coe',
    width: 110,
    align: 'right',
    render: (_, record) => formatPercent(record.is_financial ? record.cost_of_equity_pct : record.wacc_pct),
  },
  {
    title: '资产负债率',
    dataIndex: 'debt_to_assets_pct',
    key: 'debt_to_assets_pct',
    width: 100,
    align: 'right',
    render: value => formatPercent(value),
  },
  {
    title: (
      <Space size={4}>
        潜在回报率(悲观~乐观)
        <Tooltip title="非金融：两阶段FCFF DCF算出的股权价值 vs 当前市值；银行/保险/证券：合理市净率 vs 当前PB。悲观/乐观区间来自WACC或股权成本 ±1.5个百分点，展开行查看完整假设">
          <InfoCircleOutlined />
        </Tooltip>
      </Space>
    ),
    dataIndex: 'expected_return_pct',
    key: 'expected_return_pct',
    width: 220,
    align: 'right',
    defaultSortOrder: 'descend',
    sorter: (a, b) => (a.expected_return_pct ?? -Infinity) - (b.expected_return_pct ?? -Infinity),
    render: (value, record) => (
      value === null || value === undefined
        ? <Text type="secondary">{record.dcf_unavailable_reason || '数据不足'}</Text>
        : (
          <Space direction="vertical" size={0} className="value-investing-return-cell">
            <Text strong className={signedClassName(value)}>{formatPercent(value)}</Text>
            <Text type="secondary" className="value-investing-return-range">
              ({formatPercent(record.expected_return_pct_bear)} ~ {formatPercent(record.expected_return_pct_bull)})
            </Text>
          </Space>
        )
    ),
  },
];

const excludedColumns = [
  {
    title: '股票',
    key: 'name',
    width: 160,
    render: (_, record) => (
      <Space direction="vertical" size={0}>
        <Text strong>{record.name || record.ts_code}</Text>
        <XueqiuStockLink symbol={record.ts_code}>{record.ts_code}</XueqiuStockLink>
      </Space>
    ),
  },
  {
    title: '行业',
    dataIndex: 'industry',
    key: 'industry',
    width: 110,
    render: value => value || '-',
  },
  {
    title: 'ROIC/ROE',
    key: 'roic_or_roe',
    width: 100,
    align: 'right',
    render: (_, record) => formatPercent(record.is_financial ? record.avg_roe_pct : record.avg_roic_pct),
  },
  {
    title: 'WACC',
    dataIndex: 'wacc_pct',
    key: 'wacc_pct',
    width: 90,
    align: 'right',
    render: value => formatPercent(value),
  },
  {
    title: '内在价值同比',
    dataIndex: 'value_growth_pct',
    key: 'value_growth_pct',
    width: 100,
    align: 'right',
    render: value => <SignedPercent value={value} />,
  },
  {
    title: '未通过原因',
    dataIndex: 'quality_reasons',
    key: 'quality_reasons',
    render: reasons => (
      <Space direction="vertical" size={2}>
        {(reasons || []).map(reason => <Tag color="volcano" key={reason}>{reason}</Tag>)}
      </Space>
    ),
  },
];

const ValueInvestingScreen = () => {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  const runScreen = useCallback(async () => {
    setLoading(true);
    try {
      const params = {
        top_n: filters.topN || 100,
        exclude_st: filters.excludeSt,
      };
      if (filters.minTotalMv !== null && filters.minTotalMv !== undefined) params.min_total_mv = filters.minTotalMv;
      if (filters.minRoicWaccSpreadPct !== null && filters.minRoicWaccSpreadPct !== undefined) {
        params.min_roic_wacc_spread_pct = filters.minRoicWaccSpreadPct;
      }
      if (filters.minOcfToNp !== null && filters.minOcfToNp !== undefined) params.min_ocf_to_np = filters.minOcfToNp;
      if (filters.minFcfPositiveYears !== null && filters.minFcfPositiveYears !== undefined) {
        params.min_fcf_positive_years = filters.minFcfPositiveYears;
      }
      if (filters.maxDebtToAssets !== null && filters.maxDebtToAssets !== undefined) {
        params.max_debt_to_assets = filters.maxDebtToAssets;
      }
      if (filters.minAvgRoeFinancial !== null && filters.minAvgRoeFinancial !== undefined) {
        params.min_avg_roe_financial = filters.minAvgRoeFinancial;
      }
      if (filters.minValueGrowthPct !== null && filters.minValueGrowthPct !== undefined) {
        params.min_value_growth_pct = filters.minValueGrowthPct;
      }
      if (filters.riskFreeRatePct !== null && filters.riskFreeRatePct !== undefined) {
        params.risk_free_rate_pct = filters.riskFreeRatePct;
      }
      if (filters.equityRiskPremiumPct !== null && filters.equityRiskPremiumPct !== undefined) {
        params.equity_risk_premium_pct = filters.equityRiskPremiumPct;
      }
      if (filters.terminalGrowthRatePct !== null && filters.terminalGrowthRatePct !== undefined) {
        params.terminal_growth_rate_pct = filters.terminalGrowthRatePct;
      }
      const { data } = await request.get('/api/value-investing/screen', { params });
      setResult(data);
    } catch (error) {
      message.error(formatErrorMessage(error, '价值投资扫描失败'));
    } finally {
      setLoading(false);
    }
  }, [filters]);

  const columns = useMemo(buildColumns, []);
  const candidates = result?.candidates || [];
  const excluded = result?.excluded_sample || [];
  const statusMessage = result?.status && result.status !== 'completed' ? STATUS_MESSAGES[result.status] : null;

  return (
    <div className="value-investing-screen">
      <div className="value-investing-toolbar">
        <div>
          <Title level={3}>价值投资扫描</Title>
          <Paragraph type="secondary" className="value-investing-intro">
            质量闸门(ROIC能否持续跑赢WACC + 现金流验证利润) → 两阶段FCFF DCF算内在价值(银行/保险/证券改用合理市净率)
            → 潜在回报率 = 内在价值 vs 当前价格，并给出WACC±150bp的悲观/乐观区间。仅做研究提示，不构成投资建议。
          </Paragraph>
        </div>
        <Space wrap align="start" className="value-investing-toolbar__controls">
          <InputNumber
            addonBefore="最小市值(万元)"
            min={0}
            value={filters.minTotalMv}
            onChange={value => setFilters(current => ({ ...current, minTotalMv: value }))}
            style={{ width: 220 }}
          />
          <InputNumber
            addonBefore="返回数量"
            min={1}
            max={1000}
            value={filters.topN}
            onChange={value => setFilters(current => ({ ...current, topN: value }))}
            style={{ width: 160 }}
          />
          <Space size={4}>
            <Text>剔除ST</Text>
            <Switch
              checked={filters.excludeSt}
              onChange={value => setFilters(current => ({ ...current, excludeSt: value }))}
            />
          </Space>
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={runScreen}>
            开始扫描
          </Button>
        </Space>
      </div>

      <Collapse
        className="value-investing-advanced"
        items={[
          {
            key: 'advanced',
            label: '质量闸门与WACC/DCF假设(留空使用默认值)',
            children: (
              <Space wrap size="middle">
                <InputNumber addonBefore="ROIC-WACC价差下限(pp)" value={filters.minRoicWaccSpreadPct}
                  onChange={value => setFilters(current => ({ ...current, minRoicWaccSpreadPct: value }))} style={{ width: 240 }} />
                <InputNumber addonBefore="经营现金流/净利润下限" step={0.1} value={filters.minOcfToNp}
                  onChange={value => setFilters(current => ({ ...current, minOcfToNp: value }))} style={{ width: 240 }} />
                <InputNumber addonBefore="FCFF为正最少年数" min={0} max={5} value={filters.minFcfPositiveYears}
                  onChange={value => setFilters(current => ({ ...current, minFcfPositiveYears: value }))} style={{ width: 220 }} />
                <InputNumber addonBefore="资产负债率上限(%)" value={filters.maxDebtToAssets}
                  onChange={value => setFilters(current => ({ ...current, maxDebtToAssets: value }))} style={{ width: 220 }} />
                <InputNumber addonBefore="金融类ROE下限(%)" value={filters.minAvgRoeFinancial}
                  onChange={value => setFilters(current => ({ ...current, minAvgRoeFinancial: value }))} style={{ width: 220 }} />
                <InputNumber addonBefore="内在价值同比增幅下限(%)" step={0.1} value={filters.minValueGrowthPct}
                  onChange={value => setFilters(current => ({ ...current, minValueGrowthPct: value }))} style={{ width: 260 }} />
                <InputNumber addonBefore="无风险利率(%)" step={0.1} value={filters.riskFreeRatePct}
                  onChange={value => setFilters(current => ({ ...current, riskFreeRatePct: value }))} style={{ width: 220 }} />
                <InputNumber addonBefore="股权风险溢价(%)" step={0.1} value={filters.equityRiskPremiumPct}
                  onChange={value => setFilters(current => ({ ...current, equityRiskPremiumPct: value }))} style={{ width: 220 }} />
                <InputNumber addonBefore="永续增长率(%)" step={0.1} value={filters.terminalGrowthRatePct}
                  onChange={value => setFilters(current => ({ ...current, terminalGrowthRatePct: value }))} style={{ width: 220 }} />
              </Space>
            ),
          },
        ]}
      />

      {statusMessage && (
        <Alert type="warning" showIcon className="value-investing-status" message={statusMessage} />
      )}

      {result && !statusMessage && (
        <Space size="large" className="value-investing-summary" wrap>
          <Statistic title="扫描日期" value={result.as_of} />
          <Statistic title="扫描范围" value={result.universe_size} suffix="只" />
          <Statistic title="通过质量闸门" value={result.quality_passed} suffix="只" />
          <Statistic title="通过但估值数据不足" value={result.insufficient_return_data} suffix="只" />
          <Statistic
            title={(
              <Tooltip title={
                result.assumptions?.risk_free_rate_source === 'chinabond_10y'
                  ? '取自中债国债收益率曲线10年期利率'
                  : '国债收益率曲线尚未同步到，使用静态假设兜底'
              }>
                无风险利率{result.assumptions?.risk_free_rate_source !== 'chinabond_10y' && '(兜底值)'}
              </Tooltip>
            )}
            value={result.assumptions?.risk_free_rate_pct}
            precision={2}
            suffix="%"
          />
        </Space>
      )}

      <Spin spinning={loading}>
        {candidates.length ? (
          <Table
            rowKey="ts_code"
            columns={columns}
            dataSource={candidates}
            size="small"
            pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
            scroll={{ x: 960 }}
            expandable={{ expandedRowRender }}
            className="value-investing-table"
          />
        ) : (!loading && result && !statusMessage && <Empty description="没有股票通过当前的质量闸门与筛选条件" />)}
      </Spin>

      {excluded.length > 0 && (
        <Collapse
          className="value-investing-excluded"
          items={[
            {
              key: 'excluded',
              label: `未通过质量闸门的样本(${excluded.length})`,
              children: (
                <Table
                  rowKey="ts_code"
                  columns={excludedColumns}
                  dataSource={excluded}
                  size="small"
                  pagination={{ defaultPageSize: 10 }}
                />
              ),
            },
          ]}
        />
      )}
    </div>
  );
};

export default ValueInvestingScreen;
