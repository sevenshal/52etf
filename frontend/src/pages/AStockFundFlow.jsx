import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Segmented,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  LineChartOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import request from '../utils/request';
import './AStockFundFlow.css';

const { Text } = Typography;

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(item => item?.msg || String(item)).join('；') || fallback;
  }
  return typeof detail === 'object' ? JSON.stringify(detail) : String(detail);
};

const formatMoney = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  const number = Number(value);
  const abs = Math.abs(number);
  const sign = number > 0 ? '+' : number < 0 ? '-' : '';
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(0)}万`;
  return `${sign}${abs.toFixed(0)}`;
};

const formatPercent = value => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : `${Number(value).toFixed(2)}%`
);

const formatYi = value => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? '-'
    : `${Number(value).toFixed(2)}亿`
);

const signedClassName = value => {
  const number = Number(value || 0);
  if (number > 0) return 'is-positive';
  if (number < 0) return 'is-negative';
  return '';
};

const SignedMoney = ({ value }) => (
  <Text className={signedClassName(value)}>{formatMoney(value)}</Text>
);

const SignedPercent = ({ value }) => (
  <Text className={signedClassName(value)}>{formatPercent(value)}</Text>
);

const toYi = value => (
  value === null || value === undefined || Number.isNaN(Number(value)) ? null : Number((Number(value) / 1e8).toFixed(4))
);

const getTopItem = rank => rank?.items?.[0] || null;

const industryColumns = [
  { title: '#', dataIndex: 'rank', key: 'rank', width: 56 },
  {
    title: '行业',
    key: 'industry',
    width: 160,
    render: (_, record) => (
      <Space direction="vertical" size={0}>
        <Text strong>{record.name || record.code}</Text>
        <Text type="secondary">{record.code}</Text>
      </Space>
    ),
  },
  {
    title: '主力净额',
    dataIndex: 'main_net',
    key: 'main_net',
    width: 116,
    align: 'right',
    render: value => <SignedMoney value={value} />,
  },
  {
    title: '主力占比',
    dataIndex: 'main_net_pct',
    key: 'main_net_pct',
    width: 96,
    align: 'right',
    render: value => <SignedPercent value={value} />,
  },
  {
    title: '上涨/下跌',
    key: 'breadth',
    width: 110,
    render: (_, record) => `${record.up_count ?? '-'} / ${record.down_count ?? '-'}`,
  },
  {
    title: '领涨',
    key: 'leader',
    width: 150,
    render: (_, record) => (
      <Space direction="vertical" size={0}>
        <Text>{record.leader || '-'}</Text>
        <SignedPercent value={record.leader_change_pct} />
      </Space>
    ),
  },
];

const dailyColumns = [
  { title: '日期', dataIndex: 'date', key: 'date', width: 110 },
  {
    title: '主力净额',
    dataIndex: 'main_net',
    key: 'main_net',
    width: 120,
    align: 'right',
    render: value => <SignedMoney value={value} />,
  },
  {
    title: '超大单',
    dataIndex: 'super_net',
    key: 'super_net',
    width: 110,
    align: 'right',
    render: value => <SignedMoney value={value} />,
  },
  {
    title: '大单',
    dataIndex: 'large_net',
    key: 'large_net',
    width: 110,
    align: 'right',
    render: value => <SignedMoney value={value} />,
  },
  {
    title: '中单',
    dataIndex: 'mid_net',
    key: 'mid_net',
    width: 110,
    align: 'right',
    render: value => <SignedMoney value={value} />,
  },
  {
    title: '小单',
    dataIndex: 'small_net',
    key: 'small_net',
    width: 110,
    align: 'right',
    render: value => <SignedMoney value={value} />,
  },
  {
    title: '涨跌',
    dataIndex: 'change_pct',
    key: 'change_pct',
    width: 84,
    align: 'right',
    render: value => <SignedPercent value={value} />,
  },
];

const rankTableProps = {
  size: 'small',
  pagination: { pageSize: 10, size: 'small' },
  scroll: { x: 720 },
};

const AStockFundFlow = ({ embedded = false }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [stockCode, setStockCode] = useState('');
  const [rankLimit, setRankLimit] = useState(30);

  const fetchDashboard = useCallback(async (code = '', limit = 30) => {
    setLoading(true);
    try {
      const params = { limit };
      if (code) params.stock_code = code;
      const response = await request.get('/api/a-stock-fund-flow/dashboard', { params });
      setData(response.data);
    } catch (error) {
      message.error(formatErrorMessage(error, '加载资金流向失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDashboard('', 30);
  }, [fetchDashboard]);

  const northboundPoints = useMemo(() => data?.northbound?.points || [], [data]);
  const stock = data?.stock || null;
  const stockMinute = useMemo(() => stock?.minute || [], [stock]);
  const stockDaily = useMemo(() => stock?.daily || [], [stock]);
  const marketInflowTop = getTopItem(data?.market_rank?.inflow);
  const marketOutflowTop = getTopItem(data?.market_rank?.outflow);
  const industryInflowTop = getTopItem(data?.industry_rank?.inflow);
  const latestNorthbound = data?.northbound?.latest;
  const selectedStockCode = stock?.code || '';

  const handleStockSelect = useCallback((record) => {
    const nextCode = String(record?.code || '').trim();
    if (!nextCode) return;
    setStockCode(nextCode);
    fetchDashboard(nextCode, rankLimit);
  }, [fetchDashboard, rankLimit]);

  const stockColumns = useMemo(() => [
    { title: '#', dataIndex: 'rank', key: 'rank', width: 56 },
    {
      title: '股票',
      key: 'stock',
      width: 170,
      render: (_, record) => (
        <Space className="fund-flow-stock-cell" size={6}>
          <Space direction="vertical" size={0}>
            <Text strong>{record.name || record.code}</Text>
            <Text type="secondary">{record.code}</Text>
          </Space>
          <Tooltip title="查询资金流向">
            <Button
              type="text"
              size="small"
              icon={<SearchOutlined />}
              onClick={event => {
                event.stopPropagation();
                handleStockSelect(record);
              }}
            />
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '主力净额',
      dataIndex: 'main_net',
      key: 'main_net',
      width: 116,
      align: 'right',
      render: value => <SignedMoney value={value} />,
    },
    {
      title: '主力占比',
      dataIndex: 'main_net_pct',
      key: 'main_net_pct',
      width: 96,
      align: 'right',
      render: value => <SignedPercent value={value} />,
    },
    {
      title: '超大单',
      dataIndex: 'super_net',
      key: 'super_net',
      width: 110,
      align: 'right',
      render: value => <SignedMoney value={value} />,
    },
    {
      title: '大单',
      dataIndex: 'large_net',
      key: 'large_net',
      width: 100,
      align: 'right',
      render: value => <SignedMoney value={value} />,
    },
    {
      title: '涨跌',
      dataIndex: 'change_pct',
      key: 'change_pct',
      width: 84,
      align: 'right',
      render: value => <SignedPercent value={value} />,
    },
  ], [handleStockSelect]);

  const northboundOption = useMemo(() => {
    const points = northboundPoints.filter(item => item.total_yi !== null && item.total_yi !== undefined);
    if (!points.length) return null;
    return {
      tooltip: { trigger: 'axis', valueFormatter: value => `${Number(value).toFixed(2)}亿` },
      legend: { top: 0 },
      grid: { top: 42, left: 52, right: 18, bottom: 34 },
      xAxis: { type: 'category', data: points.map(item => item.time), boundaryGap: false },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}亿' }, scale: true },
      series: [
        {
          name: '合计',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2.2, color: '#1677ff' },
          itemStyle: { color: '#1677ff' },
          data: points.map(item => item.total_yi),
        },
        {
          name: '沪股通',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.6, color: '#08979c' },
          itemStyle: { color: '#08979c' },
          data: points.map(item => item.hgt_yi),
        },
        {
          name: '深股通',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.6, color: '#fa8c16' },
          itemStyle: { color: '#fa8c16' },
          data: points.map(item => item.sgt_yi),
        },
      ],
    };
  }, [northboundPoints]);

  const stockMinuteOption = useMemo(() => {
    if (!stockMinute.length) return null;
    return {
      tooltip: { trigger: 'axis', valueFormatter: value => `${Number(value).toFixed(2)}亿` },
      legend: { top: 0 },
      grid: { top: 42, left: 54, right: 18, bottom: 34 },
      xAxis: { type: 'category', data: stockMinute.map(item => item.time?.slice(11) || item.time), boundaryGap: false },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}亿' }, scale: true },
      series: [
        {
          name: '主力',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2.2, color: '#1677ff' },
          itemStyle: { color: '#1677ff' },
          data: stockMinute.map(item => toYi(item.main_net)),
        },
        {
          name: '超大单',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.6, color: '#722ed1' },
          itemStyle: { color: '#722ed1' },
          data: stockMinute.map(item => toYi(item.super_net)),
        },
        {
          name: '大单',
          type: 'line',
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.6, color: '#13c2c2' },
          itemStyle: { color: '#13c2c2' },
          data: stockMinute.map(item => toYi(item.large_net)),
        },
      ],
    };
  }, [stockMinute]);

  const stockDailyOption = useMemo(() => {
    const rows = stockDaily.slice(-30);
    if (!rows.length) return null;
    return {
      tooltip: { trigger: 'axis', valueFormatter: value => `${Number(value).toFixed(2)}亿` },
      grid: { top: 20, left: 54, right: 18, bottom: 34 },
      xAxis: { type: 'category', data: rows.map(item => item.date) },
      yAxis: { type: 'value', axisLabel: { formatter: '{value}亿' }, scale: true },
      series: [
        {
          name: '主力净额',
          type: 'bar',
          data: rows.map(item => toYi(item.main_net)),
          itemStyle: {
            color: params => (Number(params.value || 0) >= 0 ? '#1677ff' : '#cf1322'),
          },
        },
      ],
    };
  }, [stockDaily]);

  const marketRankTabs = useMemo(() => [
    {
      key: 'inflow',
      label: '净流入',
      children: (
        <Table
          {...rankTableProps}
          columns={stockColumns}
          dataSource={data?.market_rank?.inflow?.items || []}
          rowKey={record => `in-${record.code}`}
          rowClassName={record => (record.code === selectedStockCode ? 'fund-flow-stock-row is-selected' : 'fund-flow-stock-row')}
          onRow={record => ({ onClick: () => handleStockSelect(record) })}
        />
      ),
    },
    {
      key: 'outflow',
      label: '净流出',
      children: (
        <Table
          {...rankTableProps}
          columns={stockColumns}
          dataSource={data?.market_rank?.outflow?.items || []}
          rowKey={record => `out-${record.code}`}
          rowClassName={record => (record.code === selectedStockCode ? 'fund-flow-stock-row is-selected' : 'fund-flow-stock-row')}
          onRow={record => ({ onClick: () => handleStockSelect(record) })}
        />
      ),
    },
  ], [data, handleStockSelect, selectedStockCode, stockColumns]);

  const industryRankTabs = useMemo(() => [
    {
      key: 'inflow',
      label: '净流入',
      children: (
        <Table
          {...rankTableProps}
          columns={industryColumns}
          dataSource={data?.industry_rank?.inflow?.items || []}
          rowKey={record => `industry-in-${record.code}`}
        />
      ),
    },
    {
      key: 'outflow',
      label: '净流出',
      children: (
        <Table
          {...rankTableProps}
          columns={industryColumns}
          dataSource={data?.industry_rank?.outflow?.items || []}
          rowKey={record => `industry-out-${record.code}`}
        />
      ),
    },
  ], [data]);

  const handleSearch = value => {
    const nextCode = String(value || '').trim();
    setStockCode(nextCode);
    fetchDashboard(nextCode, rankLimit);
  };

  const handleLimitChange = value => {
    const nextLimit = Number(value);
    setRankLimit(nextLimit);
    fetchDashboard(stockCode, nextLimit);
  };

  return (
    <div className={`fund-flow-page${embedded ? ' is-embedded' : ''}`}>
      <div className="fund-flow-toolbar">
        <Space wrap>
          <Input.Search
            allowClear
            className="fund-flow-search"
            enterButton={<SearchOutlined />}
            value={stockCode}
            onChange={event => setStockCode(event.target.value)}
            onSearch={handleSearch}
            placeholder="600519 / 600519.SH"
          />
          <Segmented
            value={rankLimit}
            options={[20, 30, 50, 100].map(value => ({ label: `Top ${value}`, value }))}
            onChange={handleLimitChange}
          />
          <Button icon={<ReloadOutlined />} onClick={() => fetchDashboard(stockCode, rankLimit)} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      {data?.errors?.length ? (
        <Alert
          className="fund-flow-alert"
          type="warning"
          showIcon
          message={data.errors.map(item => `${item.section}: ${item.message}`).join('；')}
        />
      ) : null}

      <Spin spinning={loading}>
        <div className="fund-flow-metrics">
          <div className="fund-flow-metric">
            <span>北向合计</span>
            <strong className={signedClassName(latestNorthbound?.total_yi)}>
              {formatYi(latestNorthbound?.total_yi)}
            </strong>
            <small>{latestNorthbound?.time || '-'}</small>
          </div>
          <div className="fund-flow-metric">
            <span>沪股通</span>
            <strong className={signedClassName(latestNorthbound?.hgt_yi)}>
              {formatYi(latestNorthbound?.hgt_yi)}
            </strong>
            <small>实时累计</small>
          </div>
          <div className="fund-flow-metric">
            <span>深股通</span>
            <strong className={signedClassName(latestNorthbound?.sgt_yi)}>
              {formatYi(latestNorthbound?.sgt_yi)}
            </strong>
            <small>实时累计</small>
          </div>
          <div className="fund-flow-metric">
            <span>个股流入首位</span>
            <strong>{marketInflowTop?.name || '-'}</strong>
            <small><SignedMoney value={marketInflowTop?.main_net} /></small>
          </div>
          <div className="fund-flow-metric">
            <span>个股流出首位</span>
            <strong>{marketOutflowTop?.name || '-'}</strong>
            <small><SignedMoney value={marketOutflowTop?.main_net} /></small>
          </div>
          <div className="fund-flow-metric">
            <span>行业流入首位</span>
            <strong>{industryInflowTop?.name || '-'}</strong>
            <small><SignedMoney value={industryInflowTop?.main_net} /></small>
          </div>
        </div>

        <Row gutter={[12, 12]}>
          <Col xs={24} xl={stock ? 12 : 24}>
            <Card
              className="fund-flow-chart-card"
              title={<Space><LineChartOutlined />北向资金</Space>}
              extra={<Tag>{data?.northbound?.unit || '亿元'}</Tag>}
            >
              {northboundOption ? (
                <ReactECharts option={northboundOption} style={{ height: 300 }} notMerge lazyUpdate />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
              )}
            </Card>
          </Col>
          {stock && (
            <Col xs={24} xl={12}>
              <Card
                className="fund-flow-chart-card"
                title={`${stock.name || stock.code} 分钟资金`}
                extra={<Tag>{stock.code}</Tag>}
              >
                {stockMinuteOption ? (
                  <ReactECharts option={stockMinuteOption} style={{ height: 300 }} notMerge lazyUpdate />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
                )}
              </Card>
            </Col>
          )}
        </Row>

        <Row gutter={[12, 12]} className="fund-flow-table-row">
          <Col xs={24} xl={14}>
            <Card className="fund-flow-data-card" title="全市场主力排名">
              <Tabs items={marketRankTabs} />
            </Card>
          </Col>
          <Col xs={24} xl={10}>
            <Card className="fund-flow-data-card" title="行业主力排名">
              <Tabs items={industryRankTabs} />
            </Card>
          </Col>
        </Row>

        {stock && (
          <Row gutter={[12, 12]} className="fund-flow-table-row">
            <Col xs={24} xl={10}>
              <Card
                className="fund-flow-chart-card"
                title={`${stock.name || stock.code} 近30日`}
                extra={<Tag>亿元</Tag>}
              >
                {stockDailyOption ? (
                  <ReactECharts option={stockDailyOption} style={{ height: 300 }} notMerge lazyUpdate />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
                )}
              </Card>
            </Col>
            <Col xs={24} xl={14}>
              <Card
                className="fund-flow-data-card"
                title={`${stock.name || stock.code} 日级资金`}
                extra={<Text type="secondary">{stock.summary?.latest_date || '-'}</Text>}
              >
                <Table
                  size="small"
                  columns={dailyColumns}
                  dataSource={stockDaily.slice().reverse()}
                  rowKey={record => record.date}
                  pagination={{ pageSize: 10, size: 'small' }}
                  scroll={{ x: 760 }}
                />
              </Card>
            </Col>
          </Row>
        )}

        <div className="fund-flow-footer">
          <Text type="secondary">
            {data?.generated_at ? dayjs(data.generated_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
          </Text>
          <Text type="secondary">主力资金为数据源估算口径</Text>
        </div>
      </Spin>
    </div>
  );
};

export default AStockFundFlow;
