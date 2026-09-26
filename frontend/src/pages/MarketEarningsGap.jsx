import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Card, Checkbox, Col, Drawer, Form, Input, InputNumber, Row, Space, Switch,
  Table, Tag, Tooltip, Typography, message,
} from 'antd';
import { FilterFilled, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import request from '../utils/request';
import { useAccount } from '../contexts/AccountContext';
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
const fmtNumber = (value, digits = 2) => (isBlank(value) ? '-' : Number(value).toFixed(digits));
const fmtCriteriaRange = (label, min, max) => {
  if (isBlank(min) && isBlank(max)) return null;
  if (isBlank(min)) return `${label} ≤ ${max}%`;
  if (isBlank(max)) return `${label} ≥ ${min}%`;
  return `${label} ${min}%～${max}%`;
};

const SOURCE_COLORS = { report: 'blue', express: 'purple', forecast: 'orange' };
const GAP_FILTER_OPTIONS = [
  { text: '有缺口·未回补', value: 'open' },
  { text: '有缺口·已回补', value: 'filled' },
  { text: '无缺口', value: 'none' },
];
const gapKey = record => {
  if (!record.has_true_gap) return 'none';
  return record.gap_filled ? 'filled' : 'open';
};

// 数值区间过滤：filteredValue 存 [min, max]，任一端可空；设了过滤后空值不再显示
const RangeFilterDropdown = ({ selectedKeys, setSelectedKeys, confirm, clearFilters }) => {
  const [min, max] = selectedKeys[0] || [null, null];
  const update = (index, value) => {
    const next = [min, max];
    next[index] = value ?? null;
    setSelectedKeys(next.every(isBlank) ? [] : [next]);
  };
  return (
    <div className="earnings-gap-range-filter" onKeyDown={event => event.stopPropagation()}>
      <Space size={4}>
        <InputNumber size="small" placeholder="最小" value={min} onChange={value => update(0, value)} />
        <span>~</span>
        <InputNumber size="small" placeholder="最大" value={max} onChange={value => update(1, value)} />
      </Space>
      <Space className="earnings-gap-range-actions">
        <Button size="small" onClick={() => { clearFilters?.(); confirm(); }}>重置</Button>
        <Button size="small" type="primary" onClick={() => confirm()}>确定</Button>
      </Space>
    </div>
  );
};

const rangeFilter = key => ({
  filterDropdown: props => <RangeFilterDropdown {...props} />,
  filterIcon: filtered => <FilterFilled style={{ color: filtered ? '#1677ff' : undefined }} />,
  onFilter: (range, record) => {
    const [min, max] = range || [];
    const value = record[key];
    if (isBlank(value)) return false;
    return (isBlank(min) || value >= min) && (isBlank(max) || value <= max);
  },
});

const numericColumn = (title, key, extra = {}) => ({
  title,
  dataIndex: key,
  key,
  width: 96,
  align: 'right',
  sorter: numberSorter(key),
  render: value => fmtNumber(value),
  ...rangeFilter(key),
  ...extra,
});

const pctColumn = (title, key, extra = {}) => numericColumn(title, key, { render: renderSignedPct, ...extra });

const ConfigDrawer = ({ open, config, defaults, onClose, onSaved }) => {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open && config) form.setFieldsValue({ ...config, min_amount_wan: config.min_amount_yuan / 1e4 });
  }, [open, config, form]);

  const submit = async () => {
    const values = await form.validateFields();
    const { min_amount_wan: minAmountWan, ...rest } = values;
    setSaving(true);
    try {
      const response = await request.put('/api/market/earnings-gap/config', {
        ...rest,
        min_amount_yuan: Number(minAmountWan) * 1e4,
      }, { timeout: 300000 });
      message.success('阈值已保存，已按新阈值重算');
      onSaved(response.data);
      onClose();
    } catch (error) {
      message.error(formatErrorMessage(error, '保存阈值失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      title="净利润断层参数"
      width={480}
      open={open}
      onClose={onClose}
      extra={(
        <Space>
          <Button onClick={() => form.setFieldsValue({ ...defaults, min_amount_wan: defaults.min_amount_yuan / 1e4 })}>
            恢复默认
          </Button>
          <Button type="primary" loading={saving} onClick={submit}>保存并重算</Button>
        </Space>
      )}
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="sources" label="事件源" rules={[{ required: true, message: '至少选一个事件源' }]}>
          <Checkbox.Group
            options={[
              { label: '财报', value: 'report' },
              { label: '快报', value: 'express' },
              { label: '预告（取变动下限）', value: 'forecast' },
            ]}
          />
        </Form.Item>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="min_profit_yoy" label="净利同比下限 %">
              <InputNumber className="earnings-gap-full" step={5} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="max_profit_yoy" label="净利同比上限 %">
              <InputNumber className="earnings-gap-full" step={100} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="min_profit_qoq" label="净利环比下限 %" tooltip="留空表示不限制；设置后会过滤不提供该指标的快报和预告">
              <InputNumber className="earnings-gap-full" step={5} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="max_profit_qoq" label="净利环比上限 %" tooltip="留空表示不限制；设置后会过滤不提供该指标的快报和预告">
              <InputNumber className="earnings-gap-full" step={5} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="min_revenue_yoy" label="营收同比下限 %" tooltip="留空表示不限制；设置后会过滤不提供该指标的预告">
              <InputNumber className="earnings-gap-full" step={5} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="max_revenue_yoy" label="营收同比上限 %" tooltip="留空表示不限制；设置后会过滤不提供该指标的预告">
              <InputNumber className="earnings-gap-full" step={5} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="min_revenue_qoq" label="营收环比下限 %" tooltip="留空表示不限制；设置后会过滤不提供该指标的快报和预告">
              <InputNumber className="earnings-gap-full" step={5} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="max_revenue_qoq" label="营收环比上限 %" tooltip="留空表示不限制；设置后会过滤不提供该指标的快报和预告">
              <InputNumber className="earnings-gap-full" step={5} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="min_gap_pct" label="跳空高开下限 %">
              <InputNumber className="earnings-gap-full" step={0.5} min={0} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="min_amount_wan" label="成交额下限（万元）">
              <InputNumber className="earnings-gap-full" step={500} min={0} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="min_listed_trade_days" label="上市满（交易日）">
              <InputNumber className="earnings-gap-full" step={10} min={0} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="amount_ratio_days" label="量比均线（交易日）">
              <InputNumber className="earnings-gap-full" step={1} min={1} max={250} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item
          name="min_amount_ratio"
          label="成交额/均额倍数下限"
          tooltip="T+1 成交额至少是前 N 个交易日均额的多少倍；0 表示不限制"
        >
          <InputNumber className="earnings-gap-full" step={0.5} min={0} />
        </Form.Item>
        <Form.Item name="require_bullish_close" label="要求收阳（收盘 > 开盘）" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item name="require_unsealed" label="要求未封涨停" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item
          name="require_true_gap"
          label="要求留真缺口（T+1 最低价 > T 日最高价）"
          valuePropName="checked"
          tooltip="历史回测里加这个条件信号减半、收益没提升，默认关闭"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Drawer>
  );
};

const MarketEarningsGap = () => {
  // 改阈值、全市场重算只有管理员能做（后端同样只放行管理员）
  const { isAdmin } = useAccount();
  const [data, setData] = useState(null);
  const [config, setConfig] = useState(null);
  const [defaults, setDefaults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [keyword, setKeyword] = useState('');
  const [configOpen, setConfigOpen] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      const response = await request.get('/api/market/earnings-gap', {
        params: refresh ? { refresh: true } : {},
        timeout: 300000,
      });
      setData(response.data);
      setError('');
    } catch (err) {
      setError(formatErrorMessage(err, '加载净利润断层信号失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const response = await request.get('/api/market/earnings-gap/config');
      setConfig(response.data.config);
      setDefaults(response.data.defaults);
    } catch (err) {
      // 阈值读不到不影响看列表
    }
  }, []);

  useEffect(() => {
    load();
    loadConfig();
  }, [load, loadConfig]);

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
    { title: '名称', dataIndex: 'name', key: 'name', width: 92 },
    { title: '行业', dataIndex: 'industry', key: 'industry', width: 92, render: value => value || '-' },
    {
      title: '公告',
      dataIndex: 'source',
      key: 'source',
      width: 132,
      filters: Object.entries(data?.source_labels || {}).map(([value, text]) => ({ text, value })),
      onFilter: (value, record) => record.source === value,
      render: (value, record) => (
        <Space direction="vertical" size={0}>
          <Space size={4}>
            <Tag color={SOURCE_COLORS[value]}>{record.source_label}</Tag>
            {record.forecast_type && <Text type="secondary" className="earnings-gap-sub">{record.forecast_type}</Text>}
          </Space>
          <Text type="secondary" className="earnings-gap-sub">{record.ann_date} · {record.end_date}</Text>
        </Space>
      ),
    },
    pctColumn(
      <Tooltip title="财报/快报为归母净利同比；预告取变动幅度下限">净利同比</Tooltip>,
      'np_yoy',
      {
        width: 104,
        render: (value, record) => (
          <Space direction="vertical" size={0} align="end">
            {renderSignedPct(value)}
            {!isBlank(record.np_yoy_max) && (
              <Text type="secondary" className="earnings-gap-sub">上限 {fmtSignedPct(record.np_yoy_max)}</Text>
            )}
          </Space>
        ),
      },
    ),
    pctColumn('营收同比', 'or_yoy'),
    pctColumn(<Tooltip title="单季归母净利环比">净利环比</Tooltip>, 'np_qoq'),
    pctColumn(<Tooltip title="单季营收环比">营收环比</Tooltip>, 'or_qoq'),
    {
      title: '信号日(T+1)',
      dataIndex: 'signal_date',
      key: 'signal_date',
      width: 116,
      sorter: stringSorter('signal_date'),
      defaultSortOrder: 'descend',
      render: value => (value === data?.trade_date ? <Tag color="red">{value}</Tag> : value),
    },
    pctColumn('T+1高开', 't1_open_gap_pct', { width: 92 }),
    pctColumn('T+1涨跌幅', 't1_pct_chg', { width: 100 }),
    numericColumn(
      <Tooltip title="T+1 成交额，括号下方是它相对前 N 个交易日平均成交额的倍数">T+1成交额</Tooltip>,
      't1_amount_yi',
      {
        width: 108,
        render: (value, record) => (
          <Space direction="vertical" size={0} align="end">
            <span>{isBlank(value) ? '-' : `${Number(value).toFixed(2)}亿`}</span>
            {!isBlank(record.amount_ratio) && (
              <Text type="secondary" className="earnings-gap-sub">{fmtNumber(record.amount_ratio)}× 均额</Text>
            )}
          </Space>
        ),
      },
    ),
    numericColumn(<Tooltip title="成交额 ÷ 前 N 个交易日平均成交额">量比</Tooltip>, 'amount_ratio', { width: 84 }),
    numericColumn(<Tooltip title="T 日（公告日所在交易日）最高价，即缺口下沿">缺口</Tooltip>, 'prev_high', {
      width: 88, render: value => fmtNumber(value, 3),
    }),
    numericColumn(<Tooltip title="T+1 跳空开盘价">跳空价</Tooltip>, 't1_open', { width: 88, render: value => fmtNumber(value, 3) }),
    numericColumn(<Tooltip title="T+1 收盘价，即买入价">跳空收盘价</Tooltip>, 't1_close', {
      width: 100, render: value => fmtNumber(value, 3),
    }),
    numericColumn(<Tooltip title="分析库最新交易日收盘价">现价</Tooltip>, 'latest_price', {
      width: 88, render: value => fmtNumber(value, 3),
    }),
    pctColumn('至今涨跌幅', 'since_pct', {
      width: 108,
      render: (value, record) => (
        <Space direction="vertical" size={0} align="end">
          {renderSignedPct(value)}
          {!isBlank(record.holding_days) && (
            <Text type="secondary" className="earnings-gap-sub">{record.holding_days} 个交易日</Text>
          )}
        </Space>
      ),
    }),
    pctColumn(<Tooltip title="信号后（T+1 之后）最高价相对买入价的涨幅">最大涨幅</Tooltip>, 'max_gain_pct', { width: 100 }),
    {
      title: <Tooltip title="缺口 = T+1 最低价 > T 日最高价；回补 = 之后有一天最低价回落到缺口下沿之下">缺口回补</Tooltip>,
      key: 'gap_status',
      width: 116,
      filters: GAP_FILTER_OPTIONS,
      onFilter: (value, record) => gapKey(record) === value,
      sorter: (a, b) => gapKey(a).localeCompare(gapKey(b)),
      render: (_, record) => {
        if (!record.has_true_gap) return <Text type="secondary">无缺口</Text>;
        if (!record.gap_filled) return <Tag color="green">未回补</Tag>;
        return (
          <Space direction="vertical" size={0}>
            <Tag color="default">已回补</Tag>
            <Text type="secondary" className="earnings-gap-sub">{record.gap_filled_date}</Text>
          </Space>
        );
      },
    },
    numericColumn(<Tooltip title="市盈率 TTM；亏损时为空">PE</Tooltip>, 'pe_ttm', { width: 84 }),
    numericColumn(<Tooltip title="市净率">PB</Tooltip>, 'pb', { width: 76 }),
    numericColumn(<Tooltip title="市销率 TTM">PS</Tooltip>, 'ps_ttm', { width: 76 }),
    numericColumn(
      <Tooltip title="扣非 ROE(TTM, %) ÷ PB，即扣非盈利收益率(%)，越大越便宜">ROE/PB</Tooltip>,
      'roe_pb',
      {
        width: 96,
        render: (value, record) => (
          <Space direction="vertical" size={0} align="end">
            <span>{fmtNumber(value)}</span>
            {!isBlank(record.roe_dedt_ttm) && (
              <Text type="secondary" className="earnings-gap-sub">ROE {fmtNumber(record.roe_dedt_ttm)}%</Text>
            )}
          </Space>
        ),
      },
    ),
  ], [data?.trade_date, data?.source_labels]);

  const criteria = data?.criteria;
  const extraGrowthCriteria = criteria ? [
    fmtCriteriaRange('净利环比', criteria.min_profit_qoq, criteria.max_profit_qoq),
    fmtCriteriaRange('营收同比', criteria.min_revenue_yoy, criteria.max_revenue_yoy),
    fmtCriteriaRange('营收环比', criteria.min_revenue_qoq, criteria.max_revenue_qoq),
  ].filter(Boolean) : [];

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
          {isAdmin && <Button icon={<SettingOutlined />} onClick={() => setConfigOpen(true)} disabled={!config}>参数</Button>}
          <Button icon={<ReloadOutlined />} onClick={() => load(isAdmin)} loading={loading}>{isAdmin ? '重新计算' : '刷新'}</Button>
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
          rowKey={record => `${record.symbol}-${record.source}-${record.ann_date}`}
          size="small"
          loading={loading}
          columns={columns}
          dataSource={items}
          scroll={{ x: 2200 }}
          pagination={{ defaultPageSize: 50, showSizeChanger: true, pageSizeOptions: [20, 50, 100, 200] }}
        />
      </Card>
      {criteria && (
        <Text type="secondary" className="market-footnote">
          条件（可在「参数」里改）：事件源 {(criteria.sources || []).map(key => data?.source_labels?.[key] || key).join('/')}；
          全A上市满 {criteria.min_listed_trade_days} 个交易日；每只股票最新一个报告期的财报/快报/预告（同一报告期多个都触发时只留最早一条），
          净利同比 {criteria.min_profit_yoy}%～{criteria.max_profit_yoy}%（上限剔除基数效应，预告取变动下限）；
          {extraGrowthCriteria.length > 0 ? `${extraGrowthCriteria.join('；')}；` : ''}
          公告后首个交易日 T+1 跳空高开 ≥ {criteria.min_gap_pct}%
          {criteria.require_bullish_close ? '、收盘 > 开盘' : ''}
          {criteria.require_unsealed ? '、收盘未封涨停' : ''}
          {criteria.require_true_gap ? '、留真缺口' : ''}
          {criteria.min_amount_ratio ? `、成交额 ≥ ${criteria.min_amount_ratio}× ${criteria.amount_ratio_days}日均额` : ''}
          、成交额 ≥ {(criteria.min_amount_yuan / 1e4).toFixed(0)} 万，T+1 收盘出买入信号。
          至今涨跌幅、最大涨幅以 T+1 收盘价为基准、前复权计算；估值取最新交易日，PE/PS 为 TTM，
          ROE 为扣非 ROE(TTM)。
        </Text>
      )}

      <ConfigDrawer
        open={configOpen}
        config={config}
        defaults={defaults}
        onClose={() => setConfigOpen(false)}
        onSaved={response => {
          setConfig(response.config);
          if (response.signals) setData(response.signals);
        }}
      />
    </div>
  );
};

export default MarketEarningsGap;
