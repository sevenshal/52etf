import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Card, Col, Empty, Row, Space, Spin, Table, Tag, Typography } from 'antd';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';
import './Market.css';

const { Text } = Typography;

const AUTO_REFRESH_MS = 30 * 1000;
const LABEL_COLORS = { 强势: 'red', 活跃: 'volcano', 观望: 'green', 规避: 'success' };

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const fmtPct = value => (value === null || value === undefined ? '-' : `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%`);
const pctClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');

/** 三级行业表共用的列；点击行下钻到下一级 */
const industryColumns = [
  { title: '#', dataIndex: 'rank_str', width: 72, render: value => <Text type="secondary">{value}</Text> },
  { title: '行业', dataIndex: 'name', width: 130, ellipsis: true },
  {
    title: '涨幅',
    dataIndex: 'pct',
    width: 86,
    align: 'right',
    sorter: (a, b) => a.pct - b.pct,
    render: value => <Text className={pctClass(value)}>{fmtPct(value)}</Text>,
  },
  { title: '成交额(亿)', dataIndex: 'amount_yi', width: 100, align: 'right', sorter: (a, b) => a.amount_yi - b.amount_yi },
  { title: '成分', dataIndex: 'count', width: 66, align: 'right' },
  {
    title: '强/活/观',
    key: 'labels',
    width: 100,
    align: 'right',
    render: (_, record) => (
      <Space size={2}>
        <Text className="is-up">{record.st}</Text>/<Text className="is-up">{record.by}</Text>/
        <Text className="is-down">{record.gw}</Text>
      </Space>
    ),
  },
  {
    title: '涨/跌',
    key: 'breadth',
    width: 86,
    align: 'right',
    render: (_, record) => <span><Text className="is-up">{record.up}</Text> / <Text className="is-down">{record.down}</Text></span>,
  },
  {
    title: '涨停(首/二/多)',
    key: 'boards',
    width: 128,
    align: 'right',
    sorter: (a, b) => a.lu - b.lu,
    render: (_, record) => (
      <span>
        <Text className="is-up">{record.lu}</Text>
        <Text type="secondary">（{record.fb}/{record.eb}/{record.lb3}）</Text>
      </span>
    ),
  },
  { title: '情绪分', dataIndex: 'sentiment', width: 80, align: 'right', sorter: (a, b) => a.sentiment - b.sentiment },
  {
    title: '综合分',
    dataIndex: 'composite',
    width: 84,
    align: 'right',
    defaultSortOrder: 'descend',
    sorter: (a, b) => a.composite - b.composite,
    render: value => <Text strong>{value}</Text>,
  },
];

const MarketIndustryRelation = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selectedL1, setSelectedL1] = useState(null);
  const [selectedL2, setSelectedL2] = useState(null);
  const [selectedL3, setSelectedL3] = useState(null);

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const response = await request.get('/api/market/industry-relation');
      setData(response.data);
      setError('');
    } catch (err) {
      setError(formatErrorMessage(err, '加载行业关联失败'));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(() => load({ silent: true }), AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  // 二级只显示选中一级的子行业，三级只显示选中二级的子行业
  const l2Rows = useMemo(
    () => (data?.l2 || []).filter(row => !selectedL1 || row.parent === selectedL1),
    [data, selectedL1],
  );
  const l3Rows = useMemo(
    () => (data?.l3 || []).filter(row => (selectedL2 ? row.parent === selectedL2 : !selectedL1 || l2Rows.some(l2 => l2.name === row.parent))),
    [data, selectedL1, selectedL2, l2Rows],
  );
  const memberRows = useMemo(() => {
    const members = data?.members || [];
    if (selectedL3) return members.filter(row => row.l3_name === selectedL3);
    if (selectedL2) return members.filter(row => row.l2_name === selectedL2);
    if (selectedL1) return members.filter(row => row.l1_name === selectedL1);
    return [];
  }, [data, selectedL1, selectedL2, selectedL3]);

  const selectLevel = (level, name) => {
    if (level === 1) {
      setSelectedL1(prev => (prev === name ? null : name));
      setSelectedL2(null);
      setSelectedL3(null);
    } else if (level === 2) {
      setSelectedL2(prev => (prev === name ? null : name));
      setSelectedL3(null);
    } else {
      setSelectedL3(prev => (prev === name ? null : name));
    }
  };

  const rowProps = (level, selected) => record => ({
    onClick: () => selectLevel(level, record.name),
    className: selected === record.name ? 'market-industry__row--active' : '',
    style: { cursor: 'pointer' },
  });

  const memberColumns = useMemo(() => [
    {
      title: '名称',
      dataIndex: 'name',
      width: 110,
      render: (value, record) => <StockDetailLink symbol={record.ts_code}>{value}</StockDetailLink>,
    },
    { title: '代码', dataIndex: 'code', width: 88, render: value => <Text type="secondary">{value}</Text> },
    { title: '二级行业', dataIndex: 'l2_name', width: 120, ellipsis: true },
    { title: '细分行业', dataIndex: 'l3_name', width: 130, ellipsis: true },
    { title: '现价', dataIndex: 'price', width: 84, align: 'right' },
    {
      title: '涨幅',
      dataIndex: 'pct',
      width: 88,
      align: 'right',
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.pct - b.pct,
      render: value => <Text className={pctClass(value)}>{fmtPct(value)}</Text>,
    },
    { title: '成交额(亿)', dataIndex: 'amount_yi', width: 100, align: 'right', sorter: (a, b) => a.amount_yi - b.amount_yi },
    {
      title: '标签',
      dataIndex: 'label',
      width: 90,
      render: (value, record) => (
        <Space size={4}>
          {value && <Tag color={LABEL_COLORS[value]}>{value}</Tag>}
          {record.limit_up && <Tag color="red">{record.boards > 1 ? `${record.boards}板` : '涨停'}</Tag>}
          {record.limit_down && <Tag color="green">跌停</Tag>}
        </Space>
      ),
    },
  ], []);

  const breadcrumb = [selectedL1, selectedL2, selectedL3].filter(Boolean).join(' › ') || '未选择行业';

  return (
    <div className="market-industry">
      <div className="market-toolbar">
        <Space wrap>
          <Text strong>{breadcrumb}</Text>
          {selectedL1 && <Text type="secondary">（再次点击同一行取消选择）</Text>}
        </Space>
        {data && (
          <Text type="secondary">
            {data.source} · 参与聚合 {data.picked} 只 · 成分股少于 {data.min_rank_count} 只的行业不参与排名 · 更新于 {data.fetched_at}
          </Text>
        )}
      </div>

      {error && <Alert className="market-alert" type="warning" showIcon message={error} />}

      <Spin spinning={loading && !data}>
        {data ? (
          <>
            <Row gutter={[12, 12]}>
              <Col xs={24} xl={8}>
                <Card size="small" title={`一级行业（${data.l1.length}）`} extra={<Text type="secondary">点击下钻</Text>}>
                  <Table
                    size="small"
                    rowKey="name"
                    columns={industryColumns}
                    dataSource={data.l1}
                    pagination={false}
                    scroll={{ x: 900, y: 360 }}
                    onRow={rowProps(1, selectedL1)}
                  />
                </Card>
              </Col>
              <Col xs={24} xl={8}>
                <Card size="small" title={`二级行业（${l2Rows.length}）`} extra={<Text type="secondary">{selectedL1 || '全部'}</Text>}>
                  <Table
                    size="small"
                    rowKey="name"
                    columns={industryColumns}
                    dataSource={l2Rows}
                    pagination={false}
                    scroll={{ x: 900, y: 360 }}
                    onRow={rowProps(2, selectedL2)}
                  />
                </Card>
              </Col>
              <Col xs={24} xl={8}>
                <Card size="small" title={`三级行业（${l3Rows.length}）`} extra={<Text type="secondary">{selectedL2 || selectedL1 || '全部'}</Text>}>
                  <Table
                    size="small"
                    rowKey="name"
                    columns={industryColumns}
                    dataSource={l3Rows}
                    pagination={false}
                    scroll={{ x: 900, y: 360 }}
                    onRow={rowProps(3, selectedL3)}
                  />
                </Card>
              </Col>
            </Row>

            <Card
              size="small"
              className="market-industry__members"
              title={`成分股（${memberRows.length}）`}
              extra={<Text type="secondary">{memberRows.length ? breadcrumb : '先在上方选一个行业'}</Text>}
            >
              {memberRows.length ? (
                <Table
                  size="small"
                  rowKey="ts_code"
                  columns={memberColumns}
                  dataSource={memberRows}
                  pagination={{ pageSize: 30, showSizeChanger: true }}
                  scroll={{ x: 900, y: 420 }}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="点击上方任意行业查看其成分股" />
              )}
            </Card>
          </>
        ) : (
          !loading && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Spin>
    </div>
  );
};

export default MarketIndustryRelation;
