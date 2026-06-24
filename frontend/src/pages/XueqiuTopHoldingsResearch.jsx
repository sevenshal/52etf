import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  LineChartOutlined,
  ReloadOutlined,
  SearchOutlined,
  StockOutlined,
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './XueqiuTopHoldingsResearch.css';

const { Text } = Typography;

const percentFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Number(value).toFixed(2)}%`;
};

const numberFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
};

const normalizeSearchText = value => String(value || '').trim().toUpperCase();

const getHistoryChartOption = (historyRows = []) => {
  const dates = historyRows.map(row => row.snapshot_date);
  const weights = historyRows.map(row => (
    row.composite_weight_pct === null || row.composite_weight_pct === undefined
      ? null
      : Number(row.composite_weight_pct)
  ));
  const ranks = historyRows.map(row => (
    row.composite_rank === null || row.composite_rank === undefined
      ? null
      : Number(row.composite_rank)
  ));
  const maxRank = Math.max(12, ...ranks.filter(value => Number.isFinite(value)));

  return {
    color: ['#1677ff', '#fa8c16'],
    grid: { left: 54, right: 58, top: 48, bottom: 54 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: value => (
        value === null || value === undefined || Number.isNaN(Number(value))
          ? '-'
          : Number(value).toFixed(2)
      ),
    },
    legend: {
      top: 8,
      data: ['综合权重', '综合排名'],
    },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLabel: { color: '#64748b' },
    },
    yAxis: [
      {
        type: 'value',
        name: '权重%',
        min: 0,
        axisLabel: { formatter: value => `${value}%`, color: '#64748b' },
        splitLine: { lineStyle: { color: '#edf1f7' } },
      },
      {
        type: 'value',
        name: '排名',
        inverse: true,
        min: 1,
        max: maxRank,
        axisLabel: { formatter: value => `#${Math.round(value)}`, color: '#64748b' },
        splitLine: { show: false },
      },
    ],
    dataZoom: dates.length > 80
      ? [
          { type: 'inside', start: 70, end: 100 },
          { type: 'slider', start: 70, end: 100, height: 18, bottom: 16 },
        ]
      : [],
    series: [
      {
        name: '综合权重',
        type: 'line',
        yAxisIndex: 0,
        data: weights,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { width: 2.2 },
        areaStyle: { opacity: 0.08 },
      },
      {
        name: '综合排名',
        type: 'line',
        yAxisIndex: 1,
        data: ranks,
        symbol: 'diamond',
        symbolSize: 5,
        lineStyle: { width: 1.8 },
      },
    ],
  };
};

const XueqiuTopHoldingsResearch = () => {
  const [activeOnly, setActiveOnly] = useState(true);
  const [searchText, setSearchText] = useState('');
  const [latestData, setLatestData] = useState(null);
  const [historyData, setHistoryData] = useState(null);
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [latestLoading, setLatestLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const latestRequestRef = useRef(0);
  const historyRequestRef = useRef(0);

  const latestItems = latestData?.items || [];
  const selectedItem = useMemo(
    () => latestItems.find(item => item.stock_symbol === selectedSymbol) || null,
    [latestItems, selectedSymbol],
  );
  const historyRows = historyData?.history || [];

  const filteredItems = useMemo(() => {
    const keyword = normalizeSearchText(searchText);
    if (!keyword) return latestItems;
    return latestItems.filter(item => (
      normalizeSearchText(item.stock_symbol).includes(keyword)
      || normalizeSearchText(item.raw_stock_symbol).includes(keyword)
      || normalizeSearchText(item.stock_name).includes(keyword)
      || normalizeSearchText(item.segment_name).includes(keyword)
    ));
  }, [latestItems, searchText]);

  const fetchLatest = useCallback(async () => {
    const requestId = latestRequestRef.current + 1;
    latestRequestRef.current = requestId;
    setLatestLoading(true);
    try {
      const response = await request.get('/api/factor-lab/xueqiu-top-holdings/latest', {
        params: { active_only: activeOnly, limit: 800 },
      });
      if (latestRequestRef.current !== requestId) return;
      const payload = response.data || {};
      const items = payload.items || [];
      setLatestData(payload);
      setSelectedSymbol(previous => {
        if (previous && items.some(item => item.stock_symbol === previous)) {
          return previous;
        }
        return items[0]?.stock_symbol || '';
      });
    } catch (error) {
      if (latestRequestRef.current === requestId) {
        message.error(error?.response?.data?.detail || error.message || '加载雪球持仓失败');
      }
    } finally {
      if (latestRequestRef.current === requestId) {
        setLatestLoading(false);
      }
    }
  }, [activeOnly]);

  const fetchHistory = useCallback(async symbol => {
    if (!symbol) {
      setHistoryData(null);
      return;
    }
    const requestId = historyRequestRef.current + 1;
    historyRequestRef.current = requestId;
    setHistoryLoading(true);
    try {
      const response = await request.get('/api/factor-lab/xueqiu-top-holdings/history', {
        params: { symbol, active_only: activeOnly, limit: 800 },
      });
      if (historyRequestRef.current !== requestId) return;
      setHistoryData(response.data || {});
    } catch (error) {
      if (historyRequestRef.current === requestId) {
        message.error(error?.response?.data?.detail || error.message || '加载权重历史失败');
      }
    } finally {
      if (historyRequestRef.current === requestId) {
        setHistoryLoading(false);
      }
    }
  }, [activeOnly]);

  useEffect(() => {
    fetchLatest();
  }, [fetchLatest]);

  useEffect(() => {
    fetchHistory(selectedSymbol);
  }, [fetchHistory, selectedSymbol]);

  const columns = useMemo(() => [
    {
      title: '排名',
      dataIndex: 'composite_rank',
      width: 76,
      fixed: 'left',
      sorter: (a, b) => Number(a.composite_rank || 0) - Number(b.composite_rank || 0),
      render: value => <Tag color={Number(value) <= 12 ? 'blue' : 'default'}>#{value}</Tag>,
    },
    {
      title: '股票',
      dataIndex: 'stock_symbol',
      width: 126,
      fixed: 'left',
      render: (value, record) => (
        <Space size={6}>
          <Text strong>{value}</Text>
          {record.stock_symbol === 'CASH' ? <Tag color="gold">现金</Tag> : null}
        </Space>
      ),
    },
    {
      title: '名称',
      dataIndex: 'stock_name',
      width: 150,
      ellipsis: true,
    },
    {
      title: '综合权重',
      dataIndex: 'composite_weight_pct',
      width: 116,
      align: 'right',
      sorter: (a, b) => Number(a.composite_weight_pct || 0) - Number(b.composite_weight_pct || 0),
      render: percentFormatter,
    },
    {
      title: '持仓组合',
      dataIndex: 'holding_cube_count',
      width: 112,
      align: 'right',
      sorter: (a, b) => Number(a.holding_cube_count || 0) - Number(b.holding_cube_count || 0),
      render: value => `${numberFormatter(value)} / ${numberFormatter(latestData?.cube_count)}`,
    },
    {
      title: '组合占比',
      dataIndex: 'holding_cube_ratio_pct',
      width: 106,
      align: 'right',
      sorter: (a, b) => Number(a.holding_cube_ratio_pct || 0) - Number(b.holding_cube_ratio_pct || 0),
      render: percentFormatter,
    },
    {
      title: '持有均重',
      dataIndex: 'average_weight_pct',
      width: 106,
      align: 'right',
      sorter: (a, b) => Number(a.average_weight_pct || 0) - Number(b.average_weight_pct || 0),
      render: percentFormatter,
    },
    {
      title: '最好年榜',
      dataIndex: 'best_year_rank',
      width: 96,
      align: 'right',
      sorter: (a, b) => Number(a.best_year_rank || 999999) - Number(b.best_year_rank || 999999),
      render: value => (value ? `#${value}` : '-'),
    },
    {
      title: '板块',
      dataIndex: 'segment_name',
      width: 120,
      ellipsis: true,
      render: value => value || '-',
    },
  ], [latestData?.cube_count]);

  const historyColumns = useMemo(() => [
    { title: '日期', dataIndex: 'snapshot_date', width: 118 },
    { title: '排名', dataIndex: 'composite_rank', width: 88, align: 'right', render: value => (value ? `#${value}` : '-') },
    { title: '综合权重', dataIndex: 'composite_weight_pct', width: 116, align: 'right', render: percentFormatter },
    { title: '持仓组合', dataIndex: 'holding_cube_count', width: 112, align: 'right' },
    { title: '组合占比', dataIndex: 'holding_cube_ratio_pct', width: 106, align: 'right', render: percentFormatter },
    { title: '持有均重', dataIndex: 'average_weight_pct', width: 106, align: 'right', render: percentFormatter },
  ], []);

  const chartOption = useMemo(() => getHistoryChartOption(historyRows), [historyRows]);
  const latestRow = historyData?.latest || selectedItem;

  return (
    <div className="xueqiu-holdings-page">
      <Row gutter={[12, 12]} className="xueqiu-holdings-metrics">
        <Col xs={12} md={6}>
          <Card bordered={false}>
            <Statistic title="最新日期" value={latestData?.snapshot_date || '-'} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card bordered={false}>
            <Statistic title="统计组合" value={latestData?.cube_count || 0} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card bordered={false}>
            <Statistic title="活跃/来源" value={`${numberFormatter(latestData?.active_cube_count || 0)} / ${numberFormatter(latestData?.source_cube_count || 0)}`} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card bordered={false}>
            <Statistic title="覆盖标的" value={latestItems.length} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} xl={14}>
          <Card
            bordered={false}
            title={<Space><StockOutlined />最新综合持仓</Space>}
            extra={(
              <Space wrap className="xueqiu-holdings-toolbar">
                <Switch
                  checked={activeOnly}
                  checkedChildren="活跃"
                  unCheckedChildren="全部"
                  onChange={setActiveOnly}
                />
                <Input
                  allowClear
                  prefix={<SearchOutlined />}
                  placeholder="搜索股票/名称"
                  value={searchText}
                  onChange={event => setSearchText(event.target.value)}
                />
                <Button icon={<ReloadOutlined />} onClick={fetchLatest} loading={latestLoading} />
              </Space>
            )}
          >
            <Table
              rowKey="stock_symbol"
              size="small"
              loading={latestLoading}
              columns={columns}
              dataSource={filteredItems}
              pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100, 200] }}
              scroll={{ x: 980, y: 560 }}
              rowClassName={record => (record.stock_symbol === selectedSymbol ? 'xueqiu-holdings-row-selected' : '')}
              onRow={record => ({
                onClick: () => setSelectedSymbol(record.stock_symbol),
              })}
              locale={{ emptyText: latestData?.available === false ? '暂无雪球持仓快照' : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={10}>
          <Card
            bordered={false}
            title={<Space><LineChartOutlined />权重和排名历史</Space>}
            loading={historyLoading}
          >
            {selectedSymbol ? (
              <>
                <div className="xueqiu-holdings-selected">
                  <div>
                    <Text type="secondary">当前标的</Text>
                    <h2>{selectedSymbol} {selectedItem?.stock_name || historyData?.latest?.stock_name || ''}</h2>
                  </div>
                  <Space size={6} wrap>
                    <Tag color="blue">#{latestRow?.composite_rank || '-'}</Tag>
                    <Tag color="geekblue">{percentFormatter(latestRow?.composite_weight_pct)}</Tag>
                  </Space>
                </div>
                {historyRows.length ? (
                  <ReactECharts option={chartOption} style={{ height: 340 }} notMerge lazyUpdate />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
                )}
                <Table
                  rowKey="snapshot_date"
                  size="small"
                  className="xueqiu-holdings-history-table"
                  columns={historyColumns}
                  dataSource={[...historyRows].reverse()}
                  pagination={{ defaultPageSize: 8, hideOnSinglePage: true }}
                  scroll={{ x: 640 }}
                />
              </>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default XueqiuTopHoldingsResearch;
