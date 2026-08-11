import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Card, Col, Collapse, DatePicker, Descriptions, Form, Input,
  InputNumber, Progress, Row, Select, Space, Statistic, Table, Tag, Typography, message,
} from 'antd';
import {
  ExperimentOutlined, LineChartOutlined, PlayCircleOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './AStockFearEtfBacktest.css';

const { RangePicker } = DatePicker;
const { Text, Title } = Typography;

const PARAM_FIELDS = [
  { key: 'extreme_fear_threshold', label: '极度恐慌阈值', value: '20,25,30', group: 'entry', note: '恐贪严格小于' },
  { key: 'volume_ratio_threshold', label: '放量量比', value: '1.0,1.1,1.3', group: 'entry', note: '当日量 ÷ 前N日均量' },
  { key: 'volume_window', label: '成交量均值窗口', value: '20', group: 'entry', integer: true, note: '不含信号日' },
  { key: 'bottom_fear_threshold', label: '“底”恐贪阈值', value: '20,25', group: 'entry', note: '最近均线窗口内曾低于阈值，且均线转升' },
  { key: 'bottom_ma_window', label: '“底”均线窗口', value: '5', group: 'entry', integer: true, note: '与贪恐曲线的5日线同口径' },
  { key: 'extreme_buy_fraction', label: '放量恐慌买入仓位', value: '1.0', group: 'entry', note: '1 = 100%可用组合仓位' },
  { key: 'bottom_buy_fraction', label: '“底”信号买入仓位', value: '0.5', group: 'entry', note: '0.5 = 50%组合仓位' },
  { key: 'max_positions', label: '最大持仓数', value: '1,2', group: 'entry', integer: true, note: '同一天只选量比最高的一只' },
  { key: 'greed_threshold', label: '极度贪婪阈值', value: '70,75,80', group: 'exit', note: '恐贪严格大于' },
  { key: 'greed_sell_fraction', label: '贪婪减仓比例', value: '0.5', group: 'exit', note: '首次触发只执行一次' },
  { key: 'stop_loss_pct', label: '固定止损%', value: '10', group: 'exit', note: '收盘价较买入成交价跌幅严格超过阈值' },
  { key: 'stop_cooldown_days', label: '止损冷静期', value: '20', group: 'exit', integer: true, note: '止损成交后N个交易日不再买入' },
  { key: 'volatility_window', label: '当前波动率窗口', value: '20', group: 'exit', integer: true },
  { key: 'volatility_baseline_window', label: '波动率基准窗口', value: '20', group: 'exit', integer: true, note: '不含信号日' },
  { key: 'volatility_std_multiplier', label: '波动率突破标准差', value: '0.5,1,1.5', group: 'exit', note: '当前波动率 > 均值 + Nσ' },
  { key: 'trailing_drawdown_pct', label: '移动止盈回撤%', value: '5,7,10', group: 'exit', note: '相对买入后最高价' },
  { key: 'commission_pct', label: '佣金%', value: '0.03', group: 'cost' },
  { key: 'min_commission', label: '最低佣金（元）', value: '5', group: 'cost' },
  { key: 'slippage_pct', label: '单边滑点%', value: '0.02', group: 'cost' },
  { key: 'stamp_duty_pct', label: '卖出印花税%', value: '0', group: 'cost', note: 'ETF默认0' },
  { key: 'lot_size', label: '每手份数', value: '100', group: 'cost', integer: true },
];

const objectiveOptions = [
  { label: '夏普比率', value: 'sharpe_zero_rf' },
  { label: '年化收益', value: 'annualized_return_pct' },
  { label: '总收益', value: 'total_return_pct' },
  { label: '卡玛比率', value: 'calmar_ratio' },
];

const reasonLabels = {
  extreme_fear_volume: '极恐放量买入',
  fear_bottom_reversal: '见底信号买入',
  extreme_greed_partial: '极贪减仓',
  stop_loss: '固定止损',
  volatility_trailing_stop: '波动突破后移动止盈',
};

const parseNumbers = (value, integer = false) => String(value ?? '')
  .split(',')
  .map(item => item.trim())
  .filter(Boolean)
  .map(item => (integer ? parseInt(item, 10) : parseFloat(item)))
  .filter(Number.isFinite);

const pct = value => (value === null || value === undefined ? '-' : `${Number(value).toFixed(2)}%`);
const num = (value, digits = 2) => (value === null || value === undefined ? '-' : Number(value).toFixed(digits));
const money = value => (value === null || value === undefined ? '-' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 }));

const AStockFearEtfBacktest = () => {
  const [form] = Form.useForm();
  const [options, setOptions] = useState({ targets: [], max_search_combinations: 5000 });
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [searchResults, setSearchResults] = useState([]);
  const [searchMeta, setSearchMeta] = useState(null);
  const [job, setJob] = useState(null);
  const [formValues, setFormValues] = useState({});
  const pollRef = useRef(null);

  useEffect(() => {
    request.get('/api/a-stock-fear-etf-backtest/options')
      .then(({ data }) => {
        setOptions(data);
        const defaults = Object.fromEntries(PARAM_FIELDS.map(field => [`${field.key}_values`, field.value]));
        const initialValues = {
          ...defaults,
          date_range: [dayjs(data.default_request?.start_date || '2023-01-01'), dayjs()],
          initial_capital: data.default_request?.initial_capital || 1000000,
          benchmark_symbol: data.default_request?.benchmark_symbol || '000300.SH',
          included_indexes: [], objective: 'sharpe_zero_rf', top_n: 20,
        };
        form.setFieldsValue(initialValues);
        setFormValues(initialValues);
      })
      .catch(error => message.error(error.response?.data?.detail || '加载回测选项失败'));
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [form]);

  const candidateGroups = useMemo(() => Object.fromEntries(PARAM_FIELDS.map(field => [
    field.key,
    parseNumbers(formValues?.[`${field.key}_values`], field.integer),
  ])), [formValues]);
  const combinationCount = Object.values(candidateGroups).reduce((total, values) => total * values.length, 1);
  const combinationInvalid = combinationCount < 1 || combinationCount > options.max_search_combinations;

  const etfNames = useMemo(() => Object.fromEntries(
    options.targets.map(item => [String(item.etf_symbol || '').toUpperCase(), item.etf_label])
  ), [options.targets]);

  const formatEtf = symbol => {
    if (!symbol) return '-';
    const normalized = String(symbol).toUpperCase();
    const name = etfNames[normalized];
    return name && name !== normalized ? `${name}（${normalized}）` : normalized;
  };

  const targetOptions = options.targets.map(item => ({
    value: item.index_symbol,
    label: `${item.index_label} ${item.index_symbol} · ${item.etf_label} ${item.etf_symbol}`,
  }));

  const commonPayload = values => ({
    start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
    end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
    initial_capital: values.initial_capital,
    benchmark_symbol: values.benchmark_symbol,
    included_indexes: values.included_indexes || [],
  });

  const firstParams = () => Object.fromEntries(PARAM_FIELDS.map(field => [field.key, candidateGroups[field.key]?.[0]]));

  const runDetail = async params => {
    const values = await form.validateFields();
    const selected = params || firstParams();
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
      clearInterval(pollRef.current);
      setSearchResults(data.result?.results || []);
      setSearchMeta(data.result?.meta || null);
      setResult(data.result?.best_result || null);
      setLoading(false);
      message.success('参数搜索完成');
    } else if (data.status === 'failed') {
      clearInterval(pollRef.current);
      setLoading(false);
      message.error(data.error || '参数搜索失败');
    }
  };

  const startSearch = async () => {
    const values = await form.validateFields();
    if (combinationInvalid) {
      message.warning(`参数组合数必须在1到${options.max_search_combinations}之间`);
      return;
    }
    setLoading(true);
    setSearchResults([]);
    setResult(null);
    try {
      const payload = { ...commonPayload(values), top_n: values.top_n, objective: values.objective };
      PARAM_FIELDS.forEach(field => { payload[`${field.key}_values`] = candidateGroups[field.key]; });
      const { data } = await request.post('/api/a-stock-fear-etf-backtest/search/jobs', payload);
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

  const renderFields = group => (
    <Row gutter={[14, 0]}>
      {PARAM_FIELDS.filter(field => field.group === group).map(field => (
        <Col xs={24} sm={12} lg={8} xl={6} key={field.key}>
          <Form.Item
            name={`${field.key}_values`}
            label={field.label}
            extra={field.note || '逗号分隔候选值'}
            rules={[{ required: true, message: '请填写至少一个候选值' }]}
          >
            <Input placeholder={field.value} />
          </Form.Item>
        </Col>
      ))}
    </Row>
  );

  const searchColumns = [
    { title: '#', width: 48, render: (_, __, index) => index + 1 },
    { title: '极恐', dataIndex: ['params', 'extreme_fear_threshold'], width: 62 },
    { title: '量比', dataIndex: ['params', 'volume_ratio_threshold'], width: 62 },
    { title: '底阈值', dataIndex: ['params', 'bottom_fear_threshold'], width: 70 },
    { title: '极贪', dataIndex: ['params', 'greed_threshold'], width: 62 },
    { title: '止损', dataIndex: ['params', 'stop_loss_pct'], render: value => `${value}%`, width: 68 },
    { title: '冷静期', dataIndex: ['params', 'stop_cooldown_days'], render: value => `${value}日`, width: 72 },
    { title: '波动σ', dataIndex: ['params', 'volatility_std_multiplier'], width: 68 },
    { title: '回撤止盈', dataIndex: ['params', 'trailing_drawdown_pct'], render: value => `${value}%`, width: 88 },
    { title: '持仓', dataIndex: ['params', 'max_positions'], width: 62 },
    { title: '年化', dataIndex: ['summary', 'annualized_return_pct'], render: pct, sorter: (a, b) => a.summary.annualized_return_pct - b.summary.annualized_return_pct },
    { title: '回撤', dataIndex: ['summary', 'max_drawdown_pct'], render: pct },
    { title: '夏普', dataIndex: ['summary', 'sharpe_zero_rf'], render: value => num(value) },
    { title: '', width: 70, fixed: 'right', render: (_, record) => <Button type="link" onClick={() => runDetail(record.params)}>复盘</Button> },
  ];

  const tradeColumns = [
    { title: '成交日', dataIndex: 'date', width: 108 },
    { title: '信号日', dataIndex: 'signal_date', width: 108 },
    { title: '动作', dataIndex: 'action', width: 66, render: value => <Tag color={value === 'buy' ? 'green' : 'red'}>{value === 'buy' ? '买' : '卖'}</Tag> },
    { title: 'ETF', dataIndex: 'etf_symbol', width: 190, render: formatEtf },
    { title: '指数', dataIndex: 'index_symbol', width: 104 },
    { title: '价格', dataIndex: 'price', render: value => num(value, 4) },
    { title: '数量', dataIndex: 'quantity', render: value => Number(value).toLocaleString() },
    { title: '恐贪', dataIndex: 'fear_score', render: value => num(value) },
    { title: '量比', dataIndex: 'volume_ratio', render: value => num(value) },
    { title: '盈亏', dataIndex: 'pnl', render: value => value == null ? '-' : money(value) },
    { title: '原因', dataIndex: 'reason', width: 170, render: value => reasonLabels[value] || value },
  ];

  const equityOption = useMemo(() => {
    const rows = result?.equity_curve || [];
    const benchmarkLabel = result?.benchmark?.label || result?.benchmark?.symbol || '基准';
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略', benchmarkLabel] },
      grid: { left: 62, right: 24, top: 44, bottom: 50 },
      xAxis: { type: 'category', data: rows.map(item => item.date), boundaryGap: false },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
      series: [
        { name: '策略', type: 'line', showSymbol: false, data: rows.map(item => item.value), lineStyle: { color: '#1677ff', width: 2 } },
        { name: benchmarkLabel, type: 'line', showSymbol: false, data: rows.map(item => item.benchmark_value), lineStyle: { color: '#8c8c8c', width: 1.4 } },
      ],
    };
  }, [result]);

  return (
    <div className="a-fear-etf-page">
      <header className="a-fear-etf-heading">
        <div>
          <div className="a-fear-etf-eyebrow"><ThunderboltOutlined /> FEAR / GREED PORTFOLIO LAB</div>
          <Title level={2}>A股贪恐 ETF 组合回测</Title>
          <Text type="secondary">恐慌负责入场，贪婪先减仓，波动率突破后由移动止盈保护利润。</Text>
        </div>
        <div className="a-fear-etf-badges">
          <Tag>全量ETF指数池</Tag><Tag>次日开盘成交</Tag><Tag>量比择优</Tag><Tag>10%固定止损</Tag>
        </div>
      </header>

      <div className="a-fear-etf-flow">
        <div><b>01</b><span>极恐放量买满<br />或“底”信号买半仓</span></div>
        <i />
        <div><b>02</b><span>多信号同时出现<br />优先选择量比最大</span></div>
        <i />
        <div><b>03</b><span>极贪卖出一半<br />开始观察波动率</span></div>
        <i />
        <div><b>04</b><span>亏损10%止损并冷静20日<br />波动突破后回撤7%止盈</span></div>
      </div>

      <Card className="a-fear-etf-card a-fear-etf-config" title={<span><ExperimentOutlined /> 参数搜索空间</span>}>
        <Form form={form} layout="vertical" onValuesChange={(_, values) => setFormValues(values)}>
          <Row gutter={14}>
            <Col xs={24} md={12} xl={6}><Form.Item name="date_range" label="回测区间" rules={[{ required: true }]}><RangePicker style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} md={6} xl={4}><Form.Item name="initial_capital" label="初始资金"><InputNumber min={10000} step={100000} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={12} md={6} xl={4}><Form.Item name="top_n" label="保留前N组"><InputNumber min={1} max={100} style={{ width: '100%' }} /></Form.Item></Col>
            <Col xs={24} md={8} xl={4}><Form.Item name="objective" label="搜索目标"><Select options={objectiveOptions} /></Form.Item></Col>
            <Col xs={24} md={8} xl={6}>
              <Form.Item name="benchmark_symbol" label="对比基准" rules={[{ required: true, message: '请选择对比基准' }]}>
                <Select showSearch optionFilterProp="label" options={targetOptions} placeholder="从标的池选择基准" />
              </Form.Item>
            </Col>
            <Col xs={24}>
              <Form.Item name="included_indexes" label="标的池" extra={`留空使用全部 ${options.targets.length} 个有ETF的指数`}>
                <Select mode="multiple" allowClear showSearch optionFilterProp="label" options={targetOptions} maxTagCount="responsive" placeholder="全部可交易指数" />
              </Form.Item>
            </Col>
          </Row>
          <Collapse
            ghost
            defaultActiveKey={['entry', 'exit']}
            items={[
              { key: 'entry', label: '买入、仓位与择优', children: renderFields('entry') },
              { key: 'exit', label: '减仓、波动率与移动止盈', children: renderFields('exit') },
              { key: 'cost', label: '成交与成本', children: renderFields('cost') },
            ]}
          />
          <div className="a-fear-etf-actions">
            <Space wrap>
              <Button type="primary" icon={<ExperimentOutlined />} onClick={startSearch} loading={loading} disabled={combinationInvalid}>搜索最优参数</Button>
              <Button icon={<PlayCircleOutlined />} onClick={() => runDetail()} loading={detailLoading}>回测每项首个值</Button>
            </Space>
            <Text type={combinationInvalid ? 'danger' : 'secondary'}>
              {combinationCount.toLocaleString()} 组组合 · 上限 {options.max_search_combinations?.toLocaleString()}
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
          <Table rowKey={record => JSON.stringify(record.params)} columns={searchColumns} dataSource={searchResults} size="small" scroll={{ x: 1120 }} pagination={{ pageSize: 20 }} />
        </Card>
      )}

      <div id="a-fear-etf-detail">
        {result && (
          <>
            <Card className="a-fear-etf-card a-fear-etf-result" title={<span><LineChartOutlined /> 回测结果</span>} loading={detailLoading}>
              <Row gutter={[12, 16]}>
                <Col xs={12} sm={8} lg={4}><Statistic title="总收益" value={result.summary?.total_return_pct} precision={2} suffix="%" /></Col>
                <Col xs={12} sm={8} lg={4}><Statistic title="年化收益" value={result.summary?.annualized_return_pct} precision={2} suffix="%" /></Col>
                <Col xs={12} sm={8} lg={4}><Statistic title="最大回撤" value={result.summary?.max_drawdown_pct} precision={2} suffix="%" /></Col>
                <Col xs={12} sm={8} lg={4}><Statistic title="夏普" value={result.summary?.sharpe_zero_rf} precision={2} /></Col>
                <Col xs={12} sm={8} lg={4}><Statistic title="平均仓位" value={result.summary?.average_exposure_pct} precision={1} suffix="%" /></Col>
                <Col xs={12} sm={8} lg={4}><Statistic title="平均持仓" value={result.summary?.average_holding_count} precision={2} suffix="只" /></Col>
              </Row>
              <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} className="a-fear-etf-descriptions">
                <Descriptions.Item label="极恐 + 量比">&lt; {result.params?.extreme_fear_threshold} / ≥ {result.params?.volume_ratio_threshold}</Descriptions.Item>
                <Descriptions.Item label="见底条件">近{result.params?.bottom_ma_window}日曾 &lt; {result.params?.bottom_fear_threshold}，MA{result.params?.bottom_ma_window}转升</Descriptions.Item>
                <Descriptions.Item label="极贪减仓">&gt; {result.params?.greed_threshold}，卖 {num(result.params?.greed_sell_fraction * 100, 0)}%</Descriptions.Item>
                <Descriptions.Item label="固定止损">跌幅 &gt; {result.params?.stop_loss_pct}%，冷静 {result.params?.stop_cooldown_days} 个交易日</Descriptions.Item>
                <Descriptions.Item label="移动止盈">波动 +{result.params?.volatility_std_multiplier}σ，回撤 {result.params?.trailing_drawdown_pct}%</Descriptions.Item>
                <Descriptions.Item label="对比基准">{result.benchmark?.label || result.benchmark?.symbol || '-'}</Descriptions.Item>
                <Descriptions.Item label="期末资产">{money(result.summary?.final_value)}</Descriptions.Item>
                <Descriptions.Item label="换手率">{pct(result.summary?.turnover_pct)}</Descriptions.Item>
                <Descriptions.Item label="卖出胜率">{pct(result.summary?.closed_trade_win_rate_pct)}</Descriptions.Item>
                <Descriptions.Item label="期末持仓">{result.summary?.ending_positions?.map(formatEtf).join('、') || '现金'}</Descriptions.Item>
              </Descriptions>
            </Card>
            <Card className="a-fear-etf-card" title="资金曲线"><ReactECharts option={equityOption} style={{ height: 390 }} /></Card>
            <Row gutter={16}>
              <Col xs={24} lg={7}><Card className="a-fear-etf-card" title="年度收益"><Table rowKey="year" size="small" pagination={false} dataSource={result.yearly_returns || []} columns={[{ title: '年度', dataIndex: 'year' }, { title: '收益', dataIndex: 'return_pct', render: pct }]} /></Card></Col>
              <Col xs={24} lg={17}><Card className="a-fear-etf-card" title="交易流水"><Table rowKey={(row, index) => `${row.date}-${row.action}-${row.etf_symbol}-${index}`} size="small" dataSource={result.trades || []} columns={tradeColumns} scroll={{ x: 1350 }} pagination={{ pageSize: 12 }} /></Card></Col>
            </Row>
          </>
        )}
      </div>
      {!result && !loading && <Alert showIcon type="info" message="先用默认参数回测，或搜索候选值组合；所有信号均在下一交易日开盘执行。" />}
    </div>
  );
};

export default AStockFearEtfBacktest;
