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
  minAvgRoe: null,
  minOcfToNp: null,
  minFcfPositiveYears: null,
  maxDebtToAssets: null,
  minAvgRoeFinancial: null,
};

const expandedRowRender = record => (
  <Descriptions
    size="small"
    column={3}
    className="value-investing-detail"
    items={[
      {
        key: 'reversion',
        label: '估值均值回归',
        children: <SignedPercent value={record.reversion_return_pct} />,
      },
      {
        key: 'growth',
        label: 'FCF收益率+利润复合增速',
        children: <SignedPercent value={record.growth_return_pct} />,
      },
      {
        key: 'earnings_yield',
        label: '市盈率倒数(E/P)',
        children: <SignedPercent value={record.earnings_yield_pct} />,
      },
      {
        key: 'fcf_yield',
        label: 'FCF收益率',
        children: <SignedPercent value={record.fcf_yield_pct} />,
      },
      {
        key: 'profit_cagr',
        label: '净利润复合增速(CAGR)',
        children: <SignedPercent value={record.profit_cagr_pct} />,
      },
      {
        key: 'ocf_to_np',
        label: '经营现金流/净利润',
        children: formatNumber(record.ocf_to_net_profit),
      },
    ]}
  />
);

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
    title: '近5年平均ROE',
    dataIndex: 'avg_roe_pct',
    key: 'avg_roe_pct',
    width: 120,
    align: 'right',
    sorter: (a, b) => (a.avg_roe_pct ?? -Infinity) - (b.avg_roe_pct ?? -Infinity),
    render: value => formatPercent(value),
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
        潜在回报率
        <Tooltip title="估值均值回归 / FCF收益率+历史利润复合增速 / 市盈率倒数三种估算的中位数，展开行查看明细">
          <InfoCircleOutlined />
        </Tooltip>
      </Space>
    ),
    dataIndex: 'expected_return_pct',
    key: 'expected_return_pct',
    width: 130,
    align: 'right',
    defaultSortOrder: 'descend',
    sorter: (a, b) => (a.expected_return_pct ?? -Infinity) - (b.expected_return_pct ?? -Infinity),
    render: value => (
      value === null || value === undefined
        ? <Text type="secondary">数据不足</Text>
        : <Text strong className={signedClassName(value)}>{formatPercent(value)}</Text>
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
    title: '近5年平均ROE',
    dataIndex: 'avg_roe_pct',
    key: 'avg_roe_pct',
    width: 120,
    align: 'right',
    render: value => formatPercent(value),
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
      if (filters.minAvgRoe !== null && filters.minAvgRoe !== undefined) params.min_avg_roe = filters.minAvgRoe;
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
            质量闸门(现金流验证利润 + 资产负债健康度) → 估值分位数(自身近5年历史) → 潜在回报率(三种独立测算取中位数)。
            仅做研究提示，不构成投资建议。
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
            label: '质量闸门高级参数(留空使用默认阈值)',
            children: (
              <Space wrap size="middle">
                <InputNumber addonBefore="近5年平均ROE下限(%)" value={filters.minAvgRoe}
                  onChange={value => setFilters(current => ({ ...current, minAvgRoe: value }))} style={{ width: 240 }} />
                <InputNumber addonBefore="经营现金流/净利润下限" step={0.1} value={filters.minOcfToNp}
                  onChange={value => setFilters(current => ({ ...current, minOcfToNp: value }))} style={{ width: 240 }} />
                <InputNumber addonBefore="FCF为正最少年数" min={0} max={5} value={filters.minFcfPositiveYears}
                  onChange={value => setFilters(current => ({ ...current, minFcfPositiveYears: value }))} style={{ width: 220 }} />
                <InputNumber addonBefore="资产负债率上限(%)" value={filters.maxDebtToAssets}
                  onChange={value => setFilters(current => ({ ...current, maxDebtToAssets: value }))} style={{ width: 220 }} />
                <InputNumber addonBefore="金融类ROE下限(%)" value={filters.minAvgRoeFinancial}
                  onChange={value => setFilters(current => ({ ...current, minAvgRoeFinancial: value }))} style={{ width: 220 }} />
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
