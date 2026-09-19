import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Input, Space, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';

const { Text } = Typography;

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.message;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const isBlank = value => value === null || value === undefined;
const signedClass = value => (value > 0 ? 'is-up' : value < 0 ? 'is-down' : '');
const fmtSignedPct = value => (isBlank(value) ? '-' : `${value > 0 ? '+' : ''}${Number(value).toFixed(2)}%`);
const renderSignedPct = value => <span className={signedClass(value)}>{fmtSignedPct(value)}</span>;
const numberSorter = key => (a, b) => (a[key] ?? -Infinity) - (b[key] ?? -Infinity);
const stringSorter = key => (a, b) => String(a[key] || '').localeCompare(String(b[key] || ''));

const MarketEarningsGap = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [keyword, setKeyword] = useState('');

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      const response = await request.get('/api/market/earnings-gap', {
        params: refresh ? { refresh: true } : {},
        timeout: 180000,
      });
      setData(response.data);
      setError('');
    } catch (err) {
      setError(formatErrorMessage(err, '加载净利润断层信号失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const items = useMemo(() => {
    const list = data?.items || [];
    const text = keyword.trim().toUpperCase();
    if (!text) return list;
    return list.filter(item => (
      String(item.symbol || '').toUpperCase().includes(text)
      || String(item.name || '').toUpperCase().includes(text)
      || String(item.industry || '').toUpperCase().includes(text)
    ));
  }, [data, keyword]);

  const latestSignalDate = data?.items?.[0]?.signal_date;
  const latestSignalCount = (data?.items || []).filter(item => item.signal_date === data?.trade_date).length;

  const columns = useMemo(() => [
    {
      title: '代码',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 104,
      fixed: 'left',
      render: value => <StockDetailLink symbol={value}>{value}</StockDetailLink>,
    },
    { title: '名称', dataIndex: 'name', key: 'name', width: 96 },
    { title: '行业', dataIndex: 'industry', key: 'industry', width: 96, render: value => value || '-' },
    {
      title: '财报日',
      dataIndex: 'ann_date',
      key: 'ann_date',
      width: 108,
      sorter: stringSorter('ann_date'),
      render: (value, record) => (
        <Space direction="vertical" size={0}>
          <span>{value}</span>
          <Text type="secondary" className="earnings-gap-sub">报告期 {record.end_date}</Text>
        </Space>
      ),
    },
    {
      title: '净利增速',
      dataIndex: 'netprofit_yoy',
      key: 'netprofit_yoy',
      width: 100,
      align: 'right',
      sorter: numberSorter('netprofit_yoy'),
      render: renderSignedPct,
    },
    {
      title: '信号日(T+1)',
      dataIndex: 'signal_date',
      key: 'signal_date',
      width: 116,
      sorter: stringSorter('signal_date'),
      defaultSortOrder: 'descend',
      render: value => (value === data?.trade_date ? <Tag color="red">{value}</Tag> : value),
    },
    {
      title: 'T+1高开',
      dataIndex: 't1_open_gap_pct',
      key: 't1_open_gap_pct',
      width: 92,
      align: 'right',
      sorter: numberSorter('t1_open_gap_pct'),
      render: renderSignedPct,
    },
    {
      title: 'T+1涨跌幅',
      dataIndex: 't1_pct_chg',
      key: 't1_pct_chg',
      width: 100,
      align: 'right',
      sorter: numberSorter('t1_pct_chg'),
      render: renderSignedPct,
    },
    {
      title: 'T+1成交额',
      dataIndex: 't1_amount_yi',
      key: 't1_amount_yi',
      width: 100,
      align: 'right',
      sorter: numberSorter('t1_amount_yi'),
      render: value => (isBlank(value) ? '-' : `${Number(value).toFixed(2)}亿`),
    },
    {
      title: '至今涨跌幅',
      dataIndex: 'since_pct',
      key: 'since_pct',
      width: 108,
      align: 'right',
      sorter: numberSorter('since_pct'),
      render: (value, record) => (
        <Space direction="vertical" size={0} align="end">
          {renderSignedPct(value)}
          {!isBlank(record.holding_days) && (
            <Text type="secondary" className="earnings-gap-sub">{record.holding_days} 个交易日</Text>
          )}
        </Space>
      ),
    },
  ], [data?.trade_date]);

  const criteria = data?.criteria;

  return (
    <div className="market-volume earnings-gap">
      <div className="market-toolbar">
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="代码 / 名称 / 行业"
            className="earnings-gap-search"
            onChange={event => setKeyword(event.target.value)}
          />
          <Button icon={<ReloadOutlined />} onClick={() => load(true)} loading={loading}>重新计算</Button>
        </Space>
        {data && (
          <Space wrap size={[12, 4]} className="market-summary">
            <Text>共 <Text strong>{data.items?.length || 0}</Text> 只</Text>
            <Text>
              {data.trade_date} 新信号 <Text strong className={latestSignalCount ? 'is-up' : ''}>{latestSignalCount}</Text> 只
            </Text>
            {latestSignalDate && <Text type="secondary">最近信号日 {latestSignalDate}</Text>}
            <Text type="secondary">计算于 {data.computed_at}</Text>
          </Space>
        )}
      </div>

      {error && <Alert className="market-alert" type="warning" showIcon message={error} />}
      {(data?.warnings || []).length > 0 && (
        <Alert
          className="market-alert"
          type="info"
          showIcon
          message={data.warnings.map(item => <div key={item}>{item}</div>)}
        />
      )}

      <Card size="small">
        <Table
          rowKey={record => `${record.symbol}-${record.ann_date}`}
          size="small"
          loading={loading}
          columns={columns}
          dataSource={items}
          scroll={{ x: 1020 }}
          pagination={{ defaultPageSize: 50, showSizeChanger: true, pageSizeOptions: [20, 50, 100, 200] }}
        />
      </Card>
      {criteria && (
        <Text type="secondary" className="market-footnote">
          条件：全A上市满 {criteria.min_listed_trade_days} 个交易日；每只股票最近一期财报，
          归母净利润同比 {criteria.min_profit_yoy}%～{criteria.max_profit_yoy}%（上限剔除基数效应）；
          财报公告后首个交易日 T+1 跳空高开 ≥ {criteria.min_gap_pct}%、收盘 &gt; 开盘、收盘未封涨停、
          成交额 ≥ {(criteria.min_amount_yuan / 1e4).toFixed(0)} 万，T+1 收盘出买入信号（每个交易日 A股基础数据同步完成后计算）。
          至今涨跌幅以 T+1 收盘价为基准、前复权计算。
        </Text>
      )}
    </div>
  );
};

export default MarketEarningsGap;
