import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Card, Col, DatePicker, Descriptions, Form, Input, InputNumber,
  Progress, Row, Select, Space, Statistic, Table, Tag, Typography, message,
} from 'antd';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './AStockFearEtfBacktest.css';

const { RangePicker } = DatePicker;
const { Text, Title } = Typography;

const parseNumbers = (value, integer = false) => String(value || '')
  .split(',')
  .map(item => item.trim())
  .filter(Boolean)
  .map(item => (integer ? parseInt(item, 10) : parseFloat(item)))
  .filter(item => Number.isFinite(item));

const pct = value => (value === null || value === undefined ? '-' : `${Number(value).toFixed(2)}%`);
const num = (value, digits = 2) => (value === null || value === undefined ? '-' : Number(value).toFixed(digits));
const money = value => (value === null || value === undefined
  ? '-'
  : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 }));

const objectiveOptions = [
  { label: '夏普比率', value: 'sharpe_zero_rf' },
  { label: '总收益', value: 'total_return_pct' },
  { label: '年化收益', value: 'annualized_return_pct' },
  { label: '卡玛比率', value: 'calmar_ratio' },
];

const AStockFearEtfBacktest = () => {
  const [form] = Form.useForm();
  const [options, setOptions] = useState({ targets: [], max_search_combinations: 5000 });
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [searchResults, setSearchResults] = useState([]);
  const [searchMeta, setSearchMeta] = useState(null);
  const [job, setJob] = useState(null);
  const pollRef = useRef(null);
  const watched = Form.useWatch([], form);

  useEffect(() => {
    request.get('/api/a-stock-fear-etf-backtest/options')
      .then(({ data }) => {
        setOptions(data);
        const defaults = data.default_request || {};
        form.setFieldsValue({
          date_range: [dayjs(defaults.start_date || '2020-01-02'), dayjs()],
          initial_capital: defaults.initial_capital || 1000000,
          commission_pct: defaults.commission_pct ?? 0.03,
          slippage_pct: defaults.slippage_pct ?? 0.02,
          stamp_duty_pct: defaults.stamp_duty_pct ?? 0.05,
          lot_size: defaults.lot_size || 100,
          excluded_indexes: data.default_excluded_indexes || ['INNO100.CN', '000905.SH'],
          fear_entry_values: '20,25,30',
          volume_std_multiplier_values: '0.5,1,1.5',
          no_new_high_days_values: '5,10,20,60',
          fear_exit_values: '65,70,75',
          objective: 'sharpe_zero_rf',
          top_n: 20,
        });
      })
      .catch(error => message.error(error.response?.data?.detail || '加载回测选项失败'));
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [form]);

  const candidateGroups = useMemo(() => ({
    fear: parseNumbers(watched?.fear_entry_values),
    volume: parseNumbers(watched?.volume_std_multiplier_values),
    range: parseNumbers(watched?.no_new_high_days_values, true),
    exit: parseNumbers(watched?.fear_exit_values),
  }), [watched]);
  const combinationCount = Object.values(candidateGroups)
    .reduce((total, values) => total * values.length, 1);
  const combinationInvalid = combinationCount < 1 || combinationCount > options.max_search_combinations;

  const commonPayload = values => ({
    start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
    end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
    initial_capital: values.initial_capital,
    commission_pct: values.commission_pct,
    slippage_pct: values.slippage_pct,
    stamp_duty_pct: values.stamp_duty_pct,
    lot_size: values.lot_size,
    excluded_indexes: values.excluded_indexes || [],
  });

  const runDetail = async params => {
    const values = await form.validateFields();
    const selected = params || {
      fear_entry: candidateGroups.fear[0],
      volume_std_multiplier: candidateGroups.volume[0],
      no_new_high_days: candidateGroups.range[0],
      fear_exit: candidateGroups.exit[0],
    };
    if (Object.values(selected).some(value => value === undefined)) {
      message.warning('请先填写完整候选值');
      return;
    }
    setDetailLoading(true);
    try {
      const { data } = await request.post('/api/a-stock-fear-etf-backtest/run', {
        ...commonPayload(values), params: selected,
      });
      setResult(data);
      setTimeout(() => document.getElementById('a-fear-etf-detail')?.scrollIntoView({ behavior: 'smooth' }), 0);
    } catch (error) {
      message.error(error.response?.data?.detail || '回测失败');
    } finally {
      setDetailLoading(false);
    }
  };

  const consumeJob = data => {
    setJob(data);
    if (data.status === 'completed') {
      if (pollRef.current) clearInterval(pollRef.current);
      setSearchResults(data.result?.results || []);
      setSearchMeta(data.result?.meta || null);
      setResult(data.result?.best_result || null);
      setLoading(false);
      message.success('参数搜索完成');
    } else if (data.status === 'failed') {
      if (pollRef.current) clearInterval(pollRef.current);
      setLoading(false);
      message.error(data.error || '参数搜索失败');
    }
  };

  const startSearch = async () => {
    const values = await form.validateFields();
    if (combinationInvalid) {
      message.warning(`参数组合数必须在 1 到 ${options.max_search_combinations} 之间`);
      return;
    }
    setLoading(true);
    setSearchResults([]);
    setResult(null);
    try {
      const { data } = await request.post('/api/a-stock-fear-etf-backtest/search/jobs', {
        ...commonPayload(values),
        top_n: values.top_n,
        objective: values.objective,
        fear_entry_values: candidateGroups.fear,
        volume_std_multiplier_values: candidateGroups.volume,
        no_new_high_days_values: candidateGroups.range,
        fear_exit_values: candidateGroups.exit,
      });
      setJob(data);
      pollRef.current = setInterval(async () => {
        try {
          const response = await request.get(`/api/a-stock-fear-etf-backtest/search/jobs/${data.task_id}`);
          consumeJob(response.data);
        } catch (error) {
          clearInterval(pollRef.current);
          setLoading(false);
          message.error(error.response?.data?.detail || '查询搜索进度失败');
        }
      }, 1500);
    } catch (error) {
      setLoading(false);
      message.error(error.response?.data?.detail || '启动参数搜索失败');
    }
  };

  const targetOptions = options.targets.map(item => ({
    value: item.index_symbol,
    label: `${item.index_label} ${item.index_symbol} · ${item.etf_label} ${item.etf_symbol}`,
  }));
  const searchColumns = [
    { title: '#', width: 54, render: (_, __, index) => index + 1 },
    { title: '买入恐贪', dataIndex: ['params', 'fear_entry'], width: 92 },
    { title: '放量σ', dataIndex: ['params', 'volume_std_multiplier'], width: 78 },
    { title: '未新高日', dataIndex: ['params', 'no_new_high_days'], width: 94 },
    { title: '退出恐贪', dataIndex: ['params', 'fear_exit'], width: 92 },
    { title: '总收益', dataIndex: ['summary', 'total_return_pct'], render: pct, sorter: (a, b) => a.summary.total_return_pct - b.summary.total_return_pct },
    { title: '年化', dataIndex: ['summary', 'annualized_return_pct'], render: pct },
    { title: '最大回撤', dataIndex: ['summary', 'max_drawdown_pct'], render: pct },
    { title: '夏普', dataIndex: ['summary', 'sharpe_zero_rf'], render: value => num(value) },
    { title: '交易', dataIndex: ['summary', 'closed_trade_count'], width: 64 },
    { title: '', width: 72, fixed: 'right', render: (_, record) => <Button type="link" onClick={() => runDetail(record.params)}>复盘</Button> },
  ];
  const tradeColumns = [
    { title: '日期', dataIndex: 'date', width: 110 },
    { title: '动作', dataIndex: 'action', width: 70, render: value => <Tag color={value === 'buy' ? 'green' : 'red'}>{value === 'buy' ? '买入' : '卖出'}</Tag> },
    { title: 'ETF', dataIndex: 'etf_symbol', width: 105 },
    { title: '指数', dataIndex: 'index_symbol', width: 105 },
    { title: '价格', dataIndex: 'price', render: value => num(value, 4) },
    { title: '数量', dataIndex: 'quantity', render: value => Number(value).toLocaleString() },
    { title: '恐贪', dataIndex: 'fear_score', render: value => num(value) },
    { title: '盈亏', dataIndex: 'pnl', render: value => value === null || value === undefined ? '-' : money(value) },
    { title: '原因', dataIndex: 'reason', width: 210 },
  ];

  const equityOption = useMemo(() => {
    const rows = result?.equity_curve || [];
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略', '沪深300'] },
      grid: { left: 60, right: 24, top: 44, bottom: 48 },
      xAxis: { type: 'category', data: rows.map(item => item.date), boundaryGap: false },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
      series: [
        { name: '策略', type: 'line', showSymbol: false, data: rows.map(item => item.value), lineStyle: { color: '#1677ff', width: 2 } },
        { name: '沪深300', type: 'line', showSymbol: false, data: rows.map(item => item.benchmark_value), lineStyle: { color: '#8c8c8c', width: 1.5 } },
      ],
    };
  }, [result]);

  return (
    <div className="a-fear-etf-page">
      <div className="a-fear-etf-heading">
        <div>
          <Title level={2}>A股恐贪 · ETF震荡退出回测</Title>
          <Text type="secondary">低恐贪放量满仓买入，震荡且高贪婪后回到区间中值清仓。</Text>
        </div>
        <Tag color="blue">单ETF · 次日开盘 · 100份整数手</Tag>
      </div>

      <Card title="参数空间" className="a-fear-etf-card">
        <Form form={form} layout="vertical">
          <Row gutter={16}>
            <Col xs={24} md={12} lg={6}><Form.Item name="date_range" label="回测区间" rules={[{ required: true }]}><RangePicker style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} md={6} lg={3}><Form.Item name="initial_capital" label="初始资金"><InputNumber min={10000} step={100000} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} md={6} lg={3}><Form.Item name="top_n" label="保留前N组"><InputNumber min={1} max={100} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={24} md={12} lg={5}><Form.Item name="objective" label="搜索目标"><Select options={objectiveOptions} /></Form.Item></Col>
            <Col xs={24} md={12} lg={7}><Form.Item name="excluded_indexes" label="排除指数"><Select mode="multiple" allowClear options={targetOptions} maxTagCount="responsive" /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col xs={24} md={12} lg={6}><Form.Item name="fear_entry_values" label="买入恐贪候选值" extra="逗号分隔，严格小于"><Input placeholder="20,25,30" /></Form.Item></Col>
            <Col xs={24} md={12} lg={6}><Form.Item name="volume_std_multiplier_values" label="放量标准差倍数候选值" extra="当日量 > 前20日均量 + Nσ"><Input placeholder="0.5,1,1.5" /></Form.Item></Col>
            <Col xs={24} md={12} lg={6}><Form.Item name="no_new_high_days_values" label="未创新高天数候选值" extra="买入次日起按交易日计数"><Input placeholder="5,10,20,60" /></Form.Item></Col>
            <Col xs={24} md={12} lg={6}><Form.Item name="fear_exit_values" label="退出恐贪候选值" extra="进入震荡后严格大于"><Input placeholder="65,70,75" /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col xs={12} md={6}><Form.Item name="commission_pct" label="佣金%"><InputNumber min={0} step={0.01} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} md={6}><Form.Item name="slippage_pct" label="滑点%"><InputNumber min={0} step={0.01} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} md={6}><Form.Item name="stamp_duty_pct" label="卖出印花税%"><InputNumber min={0} step={0.01} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} md={6}><Form.Item name="lot_size" label="每手份数"><InputNumber min={1} step={100} style={{ width: '100%' }} /></Form.Item></Col>
          </Row>
          <div className="a-fear-etf-actions">
            <Space wrap>
              <Button type="primary" onClick={startSearch} loading={loading} disabled={combinationInvalid}>开始参数搜索</Button>
              <Button onClick={() => runDetail()} loading={detailLoading}>运行每组首个值</Button>
            </Space>
            <Text type={combinationInvalid ? 'danger' : 'secondary'}>
              共 {combinationCount.toLocaleString()} 组组合（上限 {options.max_search_combinations?.toLocaleString()}）
            </Text>
          </div>
        </Form>
      </Card>

      {job && ['pending', 'running'].includes(job.status) && (
        <Card className="a-fear-etf-card" title="搜索进度">
          <Progress percent={job.progress || 0} status="active" />
          <Text type="secondary">{job.message || `已完成 ${job.processed_combinations || 0}/${job.total_combinations || 0}`}</Text>
        </Card>
      )}

      {searchResults.length > 0 && (
        <Card className="a-fear-etf-card" title={`搜索排名 · ${searchMeta?.total_combinations || searchResults.length}组`}>
          <Table rowKey={record => JSON.stringify(record.params)} columns={searchColumns} dataSource={searchResults} size="small" scroll={{ x: 1100 }} pagination={{ pageSize: 20 }} />
        </Card>
      )}

      <div id="a-fear-etf-detail">
        {result && (
          <>
            <Card className="a-fear-etf-card" title="详细回测" loading={detailLoading}>
              <Row gutter={[16, 16]}>
                <Col xs={12} md={6}><Statistic title="总收益" value={result.summary?.total_return_pct} precision={2} suffix="%" valueStyle={{ color: result.summary?.total_return_pct >= 0 ? '#1677ff' : '#cf1322' }} /></Col>
                <Col xs={12} md={6}><Statistic title="年化收益" value={result.summary?.annualized_return_pct} precision={2} suffix="%" /></Col>
                <Col xs={12} md={6}><Statistic title="最大回撤" value={result.summary?.max_drawdown_pct} precision={2} suffix="%" /></Col>
                <Col xs={12} md={6}><Statistic title="夏普" value={result.summary?.sharpe_zero_rf} precision={2} /></Col>
              </Row>
              <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} style={{ marginTop: 20 }}>
                <Descriptions.Item label="买入恐贪">&lt; {result.params?.fear_entry}</Descriptions.Item>
                <Descriptions.Item label="放量阈值">均量 + {result.params?.volume_std_multiplier}σ</Descriptions.Item>
                <Descriptions.Item label="未创新高">{result.params?.no_new_high_days}日</Descriptions.Item>
                <Descriptions.Item label="退出恐贪">&gt; {result.params?.fear_exit}</Descriptions.Item>
                <Descriptions.Item label="期末资产">{money(result.summary?.final_value)}</Descriptions.Item>
                <Descriptions.Item label="完整交易">{result.summary?.closed_trade_count}</Descriptions.Item>
                <Descriptions.Item label="胜率">{pct(result.summary?.closed_trade_win_rate_pct)}</Descriptions.Item>
                <Descriptions.Item label="期末持仓">{result.summary?.ending_position || '现金'}</Descriptions.Item>
              </Descriptions>
            </Card>
            <Card className="a-fear-etf-card" title="资金曲线"><ReactECharts option={equityOption} style={{ height: 400 }} /></Card>
            <Row gutter={16}>
              <Col xs={24} lg={8}><Card className="a-fear-etf-card" title="年度收益"><Table rowKey="year" size="small" pagination={false} dataSource={result.yearly_returns || []} columns={[{ title: '年度', dataIndex: 'year' }, { title: '收益', dataIndex: 'return_pct', render: pct }]} /></Card></Col>
              <Col xs={24} lg={16}><Card className="a-fear-etf-card" title="交易明细"><Table rowKey={row => `${row.date}-${row.action}-${row.etf_symbol}-${row.quantity}-${row.price}`} size="small" dataSource={result.trades || []} columns={tradeColumns} scroll={{ x: 1050 }} pagination={{ pageSize: 10 }} /></Card></Col>
            </Row>
          </>
        )}
      </div>
      {!result && !loading && <Alert showIcon type="info" message="填写候选值后开始搜索，或先运行每组候选值中的第一组。" />}
    </div>
  );
};

export default AStockFearEtfBacktest;
