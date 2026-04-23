import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  message,
} from 'antd';
import dayjs from 'dayjs';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';

const { RangePicker } = DatePicker;

const parseNumberList = (value, integer = false) => {
  if (!value) {
    return [];
  }
  return value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => (integer ? parseInt(item, 10) : parseFloat(item)))
    .filter(item => !Number.isNaN(item));
};

const formatPercent = (value, digits = 2) => `${Number(value || 0).toFixed(digits)}%`;

const objectiveOptions = [
  { label: '按年化收益最大', value: 'annualized_return' },
  { label: '按夏普最大', value: 'sharpe_ratio' },
];

const getObjectiveLabel = (value) => objectiveOptions.find(item => item.value === value)?.label || value;

const SoxlFearBacktest = () => {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [searchMeta, setSearchMeta] = useState(null);
  const [searchResults, setSearchResults] = useState([]);
  const [detailedResult, setDetailedResult] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [searchTaskId, setSearchTaskId] = useState(null);
  const [searchProgress, setSearchProgress] = useState(0);
  const [searchProgressText, setSearchProgressText] = useState('');
  const [searchProcessed, setSearchProcessed] = useState(0);
  const [searchTotal, setSearchTotal] = useState(0);
  const [searchStatus, setSearchStatus] = useState(null);
  const pollingTimerRef = useRef(null);

  const buildPayload = (values) => ({
    symbol: 'SOXL.US',
    initial_capital: values.initial_capital,
    start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
    end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
    top_n: values.top_n,
    objective: values.objective,
    eval_workers: values.eval_workers,
    a_values: parseNumberList(values.a_values),
    b_values: parseNumberList(values.b_values),
    buy_threshold_values: parseNumberList(values.buy_threshold_values),
    greed_threshold_values: parseNumberList(values.greed_threshold_values),
    volume_ratio_threshold_values: parseNumberList(values.volume_ratio_threshold_values),
    buy_position_pct_values: parseNumberList(values.buy_position_pct_values),
    cooldown_days_values: parseNumberList(values.cooldown_days_values, true),
    trailing_stop_pct_values: parseNumberList(values.trailing_stop_pct_values),
    sell_position_pct_values: parseNumberList(values.sell_position_pct_values),
  });

  const buildParamsFromRecord = (record) => ({
    a: record.a,
    b: record.b,
    buy_threshold: record.buy_threshold,
    greed_threshold: record.greed_threshold,
    volume_ratio_threshold: record.volume_ratio_threshold,
    buy_position_pct: record.buy_position_pct,
    cooldown_days: record.cooldown_days,
    trailing_stop_pct: record.trailing_stop_pct,
    sell_position_pct: record.sell_position_pct,
  });

  const stopPolling = () => {
    if (pollingTimerRef.current) {
      clearTimeout(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  };

  const pollSearchJob = async (taskId) => {
    try {
      const { data } = await request.get(`/api/soxl-fear-backtest/search/jobs/${taskId}`, {
        timeout: 30 * 1000,
      });

      setSearchStatus(data.status);
      setSearchProgress(data.progress || 0);
      setSearchProgressText(data.message || '');
      setSearchProcessed(data.processed_combinations || 0);
      setSearchTotal(data.total_combinations || 0);

      if (data.status === 'completed') {
        stopPolling();
        setLoading(false);
        setSearchTaskId(null);
        setSearchMeta(data.result?.meta || null);
        setSearchResults(data.result?.results || []);
        setDetailedResult(data.result?.best_result || null);
        message.success(`搜索完成，共评估 ${data.result?.meta?.searched_combinations || 0} 组参数`);
        return;
      }

      if (data.status === 'failed') {
        stopPolling();
        setLoading(false);
        setSearchTaskId(null);
        message.error(data.error || '搜索失败');
        return;
      }

      pollingTimerRef.current = setTimeout(() => {
        pollSearchJob(taskId);
      }, 1000);
    } catch (error) {
      stopPolling();
      setLoading(false);
      setSearchTaskId(null);
      message.error(error.response?.data?.detail || '获取搜索进度失败');
    }
  };

  const handleSearch = async (values) => {
    stopPolling();
    setLoading(true);
    setSearchMeta(null);
    setSearchResults([]);
    setDetailedResult(null);
    setSearchProgress(0);
    setSearchProgressText('正在创建搜索任务');
    setSearchProcessed(0);
    setSearchStatus('pending');
    try {
      const payload = buildPayload(values);
      const { data } = await request.post('/api/soxl-fear-backtest/search/jobs', payload, {
        timeout: 60 * 1000,
      });
      setSearchTaskId(data.task_id);
      setSearchTotal(data.total_combinations || 0);
      setSearchProgressText(`任务已创建，准备评估 ${data.total_combinations || 0} 组参数`);
      pollSearchJob(data.task_id);
    } catch (error) {
      setSearchStatus('failed');
      message.error(error.response?.data?.detail || '搜索失败');
      setLoading(false);
      setSearchTaskId(null);
    } finally {
    }
  };

  useEffect(() => () => {
    stopPolling();
  }, []);

  const loadDetail = async (record) => {
    setDetailLoading(true);
    try {
      const values = form.getFieldsValue();
      const payload = {
        symbol: 'SOXL.US',
        initial_capital: values.initial_capital,
        start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
        end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
        params: buildParamsFromRecord(record),
      };
      const { data } = await request.post('/api/soxl-fear-backtest/run', payload);
      setDetailedResult(data);
      setTimeout(() => {
        document.getElementById('soxl-fear-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 100);
    } catch (error) {
      message.error(error.response?.data?.detail || '加载详细回测失败');
    } finally {
      setDetailLoading(false);
    }
  };

  const resultColumns = [
    { title: 'a', dataIndex: 'a', width: 70 },
    { title: 'b', dataIndex: 'b', width: 70 },
    { title: '恐慌阈值', dataIndex: 'buy_threshold', width: 90 },
    { title: '贪婪阈值', dataIndex: 'greed_threshold', width: 90 },
    { title: '量比阈值', dataIndex: 'volume_ratio_threshold', width: 90 },
    { title: '买入仓位%', dataIndex: 'buy_position_pct', width: 90 },
    { title: '冷却天数', dataIndex: 'cooldown_days', width: 90 },
    { title: '止盈回撤%', dataIndex: 'trailing_stop_pct', width: 100 },
    { title: '止盈卖出%', dataIndex: 'sell_position_pct', width: 100 },
    {
      title: '年化收益',
      dataIndex: 'annualized_return',
      width: 110,
      render: value => <span style={{ color: value >= 0 ? '#cf1322' : '#1677ff' }}>{formatPercent(value)}</span>,
      sorter: (a, b) => a.annualized_return - b.annualized_return,
      defaultSortOrder: 'descend',
    },
    {
      title: 'Sharpe',
      dataIndex: 'sharpe_ratio',
      width: 90,
      render: value => Number(value || 0).toFixed(2),
      sorter: (a, b) => a.sharpe_ratio - b.sharpe_ratio,
    },
    {
      title: 'Calmar',
      dataIndex: 'calmar_ratio',
      width: 90,
      render: value => Number(value || 0).toFixed(2),
      sorter: (a, b) => a.calmar_ratio - b.calmar_ratio,
    },
    {
      title: '最大回撤',
      dataIndex: 'max_drawdown',
      width: 100,
      render: value => formatPercent(value),
      sorter: (a, b) => a.max_drawdown - b.max_drawdown,
    },
    {
      title: '交易数',
      dataIndex: 'trade_count',
      width: 80,
      sorter: (a, b) => a.trade_count - b.trade_count,
    },
    {
      title: '操作',
      key: 'action',
      width: 80,
      fixed: 'right',
      render: (_, record) => (
        <Button type="link" size="small" onClick={() => loadDetail(record)}>
          详情
        </Button>
      ),
    },
  ];

  const tradeColumns = [
    { title: '日期', dataIndex: 'date', width: 110 },
    {
      title: '方向',
      dataIndex: 'action',
      width: 90,
      render: value => <Tag color={value === 'BUY' ? 'red' : 'green'}>{value}</Tag>,
    },
    { title: '价格', dataIndex: 'price', width: 90, render: value => Number(value || 0).toFixed(2) },
    { title: '股数', dataIndex: 'shares', width: 100, render: value => Number(value || 0).toFixed(2) },
    { title: '金额', dataIndex: 'amount', width: 110, render: value => Number(value || 0).toFixed(2) },
    {
      title: '收益',
      dataIndex: 'profit',
      width: 100,
      render: value => (
        <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>
          {Number(value || 0).toFixed(2)}
        </span>
      ),
    },
    {
      title: '收益率',
      dataIndex: 'profit_pct',
      width: 100,
      render: value => (
        <span style={{ color: Number(value || 0) >= 0 ? '#cf1322' : '#1677ff' }}>
          {formatPercent(value)}
        </span>
      ),
    },
    { title: '原因', dataIndex: 'reason' },
  ];

  const equityOption = useMemo(() => {
    if (!detailedResult?.equity_curve?.length) {
      return {};
    }
    const dates = detailedResult.equity_curve.map(item => item.date);
    const values = detailedResult.equity_curve.map(item => item.value);
    const benchmark = detailedResult.equity_curve.map(item => item.benchmark_value);
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略净值', 'SOXL买入持有'] },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside', start: 50, end: 100 }, { type: 'slider' }],
      series: [
        {
          name: '策略净值',
          type: 'line',
          data: values,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#cf1322' },
          areaStyle: { opacity: 0.12, color: '#cf1322' },
        },
        {
          name: 'SOXL买入持有',
          type: 'line',
          data: benchmark,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#1677ff' },
        },
      ],
    };
  }, [detailedResult]);

  const priceVolumeOption = useMemo(() => {
    if (!detailedResult?.daily_data?.length) {
      return {};
    }

    const dates = detailedResult.daily_data.map(item => item.date);
    const klineData = detailedResult.daily_data.map(item => [item.open, item.close, item.low, item.high]);
    const ma20Data = detailedResult.daily_data.map(item => item.ma20);
    const volumeData = detailedResult.daily_data.map(item => ({
      value: item.volume,
      itemStyle: { color: item.close >= item.open ? '#cf1322' : '#1677ff' },
    }));
    const volumeMA20Data = detailedResult.daily_data.map(item => item.volume_ma20);

    const buyMarkers = (detailedResult.trades || [])
      .filter(item => item.action === 'BUY')
      .map(item => ({
        name: '买',
        value: 'B',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#cf1322' },
      }));
    const sellMarkers = (detailedResult.trades || [])
      .filter(item => item.action === 'SELL')
      .map(item => ({
        name: '卖',
        value: 'S',
        xAxis: dates.indexOf(item.date),
        yAxis: item.price,
        itemStyle: { color: '#1677ff' },
      }));

    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: ['SOXL K线', 'MA20', '成交量', '成交量MA20'] },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: '8%', right: '8%', top: 40, height: '54%' },
        { left: '8%', right: '8%', top: '72%', height: '18%' },
      ],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
        { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: { onZero: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, min: 'dataMin', max: 'dataMax' },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2 },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: 10, start: 60, end: 100 },
      ],
      series: [
        {
          name: 'SOXL K线',
          type: 'candlestick',
          data: klineData,
          itemStyle: {
            color: '#cf1322',
            color0: '#1677ff',
            borderColor: '#cf1322',
            borderColor0: '#1677ff',
          },
          markPoint: {
            data: [...buyMarkers, ...sellMarkers],
            symbolSize: 26,
            label: { color: '#fff', fontWeight: 'bold' },
          },
        },
        {
          name: 'MA20',
          type: 'line',
          data: ma20Data,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: '#faad14' },
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumeData,
        },
        {
          name: '成交量MA20',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumeMA20Data,
          showSymbol: false,
          lineStyle: { width: 2, color: '#52c41a' },
        },
      ],
    };
  }, [detailedResult]);

  const sentimentOption = useMemo(() => {
    if (!detailedResult?.daily_data?.length) {
      return {};
    }
    const dates = detailedResult.daily_data.map(item => item.date);
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['复合恐贪', 'VIX', 'CNN恐贪'] },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', scale: true },
      dataZoom: [{ type: 'inside', start: 60, end: 100 }, { type: 'slider', start: 60, end: 100 }],
      series: [
        {
          name: '复合恐贪',
          type: 'line',
          data: detailedResult.daily_data.map(item => item.composite_fear),
          showSymbol: false,
          lineStyle: { width: 2, color: '#722ed1' },
        },
        {
          name: 'VIX',
          type: 'line',
          data: detailedResult.daily_data.map(item => item.vix),
          showSymbol: false,
          lineStyle: { width: 2, color: '#fa8c16' },
        },
        {
          name: 'CNN恐贪',
          type: 'line',
          data: detailedResult.daily_data.map(item => item.cnn_fear_greed),
          showSymbol: false,
          lineStyle: { width: 2, color: '#13c2c2' },
        },
      ],
    };
  }, [detailedResult]);

  return (
    <div style={{ padding: 24 }}>
      <Card title="SOXL 情绪 + 量能 超参数回测" style={{ marginBottom: 24 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="策略假设"
          description="用 a * VIX + b * (100 - CNN恐贪) 构造复合恐贪指数；恐慌且 SOXL 成交量 / 20日均量 放大时分批买入；贪婪区触发移动止盈且卖价高于当前持仓均价时卖出；买卖后按交易日冷却 n 天。"
        />
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSearch}
          initialValues={{
            initial_capital: 100000,
            top_n: 20,
            objective: 'annualized_return',
            eval_workers: 4,
            date_range: [dayjs('2021-01-01'), dayjs()],
            a_values: '0.8,1.2',
            b_values: '0.8,1.2',
            buy_threshold_values: '85,95',
            greed_threshold_values: '40,50',
            volume_ratio_threshold_values: '1.3,1.6',
            buy_position_pct_values: '10,20',
            cooldown_days_values: '3,5',
            trailing_stop_pct_values: '6,10',
            sell_position_pct_values: '25,50',
          }}
        >
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="date_range" label="回测区间" rules={[{ required: true, message: '请选择回测区间' }]}>
                <RangePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="initial_capital" label="初始资金">
                <InputNumber min={1000} step={1000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="top_n" label="返回前N组">
                <InputNumber min={1} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="objective" label="搜索目标">
                <Select options={objectiveOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item name="eval_workers" label="并发进程数">
                <InputNumber min={1} max={16} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={4}>
              <Form.Item label="标的">
                <Input value="SOXL.US" disabled />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="a_values" label="a 候选值">
                <Input placeholder="例如 0.8,1.2" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="b_values" label="b 候选值">
                <Input placeholder="例如 0.8,1.2" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="buy_threshold_values" label="恐慌阈值候选">
                <Input placeholder="例如 85,95" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="greed_threshold_values" label="贪婪阈值候选">
                <Input placeholder="例如 40,50" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="volume_ratio_threshold_values" label="量比阈值候选">
                <Input placeholder="例如 1.3,1.6" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="buy_position_pct_values" label="每次买入仓位% 候选">
                <Input placeholder="例如 10,20" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item name="cooldown_days_values" label="冷却天数候选">
                <Input placeholder="例如 3,5" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="trailing_stop_pct_values" label="移动止盈回撤% 候选">
                <Input placeholder="例如 6,10" />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="sell_position_pct_values" label="止盈卖出仓位% 候选">
                <Input placeholder="例如 25,50" />
              </Form.Item>
            </Col>
          </Row>

          <Space>
            <Button type="primary" htmlType="submit" loading={loading}>
              搜索最佳参数
            </Button>
            <span style={{ color: '#8c8c8c' }}>
              默认组合数约 512 组，支持按年化收益率或夏普排序。
            </span>
          </Space>
        </Form>
      </Card>

      {loading && (
        <Card title="搜索进度" style={{ marginBottom: 24 }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Progress percent={searchProgress} status={searchStatus === 'failed' ? 'exception' : 'active'} />
            <Descriptions column={{ xs: 1, md: 3 }} bordered size="small">
              <Descriptions.Item label="任务状态">{searchStatus || 'pending'}</Descriptions.Item>
              <Descriptions.Item label="已评估组合">{searchProcessed}</Descriptions.Item>
              <Descriptions.Item label="总组合数">{searchTotal}</Descriptions.Item>
            </Descriptions>
            <Alert
              type="info"
              showIcon
              message={searchProgressText || '正在搜索最佳参数'}
              description={searchTaskId ? `任务ID: ${searchTaskId}` : '正在初始化任务'}
            />
          </Space>
        </Card>
      )}

      {searchMeta && (
        <Card title="搜索摘要" style={{ marginBottom: 24 }}>
          <Descriptions column={{ xs: 1, md: 2, lg: 4 }} bordered size="small">
            <Descriptions.Item label="请求区间">{searchMeta.requested_start_date} ~ {searchMeta.requested_end_date}</Descriptions.Item>
            <Descriptions.Item label="有效区间">{searchMeta.effective_start_date} ~ {searchMeta.effective_end_date}</Descriptions.Item>
            <Descriptions.Item label="交易日数">{searchMeta.trading_days}</Descriptions.Item>
            <Descriptions.Item label="搜索组合数">{searchMeta.searched_combinations}</Descriptions.Item>
            <Descriptions.Item label="搜索目标">{getObjectiveLabel(searchMeta.objective)}</Descriptions.Item>
            <Descriptions.Item label="并发进程数">{searchMeta.eval_workers}</Descriptions.Item>
            <Descriptions.Item label="有效组合数">{searchMeta.valid_combinations}</Descriptions.Item>
            <Descriptions.Item label="跳过组合数">{searchMeta.skipped_combinations}</Descriptions.Item>
          </Descriptions>
        </Card>
      )}

      {searchResults.length > 0 && (
        <Card title={`最优参数候选 (${searchResults.length} 组)`} style={{ marginBottom: 24 }}>
          <Table
            dataSource={searchResults}
            columns={resultColumns}
            rowKey={(record) => `${record.a}-${record.b}-${record.buy_threshold}-${record.greed_threshold}-${record.volume_ratio_threshold}-${record.buy_position_pct}-${record.cooldown_days}-${record.trailing_stop_pct}-${record.sell_position_pct}`}
            pagination={{ pageSize: 10 }}
            scroll={{ x: 1500 }}
            onRow={(record) => ({
              onClick: () => loadDetail(record),
              style: { cursor: 'pointer' },
            })}
          />
        </Card>
      )}

      {detailedResult && (
        <div id="soxl-fear-detail">
          <Row gutter={16} style={{ marginBottom: 24 }}>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="总收益率" value={detailedResult.total_return} precision={2} suffix="%" />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="年化收益率" value={detailedResult.annualized_return} precision={2} suffix="%" />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="Sharpe" value={detailedResult.sharpe_ratio} precision={2} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="Calmar" value={detailedResult.calmar_ratio} precision={2} />
              </Card>
            </Col>
          </Row>

          <Row gutter={16} style={{ marginBottom: 24 }}>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="最大回撤" value={detailedResult.max_drawdown} precision={2} suffix="%" />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="胜率" value={detailedResult.win_rate} precision={2} suffix="%" />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="买入次数" value={detailedResult.buy_count} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card loading={detailLoading}>
                <Statistic title="卖出次数" value={detailedResult.sell_count} />
              </Card>
            </Col>
          </Row>

          <Card title="参数明细" style={{ marginBottom: 24 }} loading={detailLoading}>
            <Descriptions column={{ xs: 1, md: 2, lg: 3 }} bordered size="small">
              <Descriptions.Item label="a">{detailedResult.params?.a}</Descriptions.Item>
              <Descriptions.Item label="b">{detailedResult.params?.b}</Descriptions.Item>
              <Descriptions.Item label="恐慌阈值">{detailedResult.params?.buy_threshold}</Descriptions.Item>
              <Descriptions.Item label="贪婪阈值">{detailedResult.params?.greed_threshold}</Descriptions.Item>
              <Descriptions.Item label="量比阈值">{detailedResult.params?.volume_ratio_threshold}</Descriptions.Item>
              <Descriptions.Item label="每次买入仓位%">{detailedResult.params?.buy_position_pct}</Descriptions.Item>
              <Descriptions.Item label="冷却天数">{detailedResult.params?.cooldown_days}</Descriptions.Item>
              <Descriptions.Item label="移动止盈回撤%">{detailedResult.params?.trailing_stop_pct}</Descriptions.Item>
              <Descriptions.Item label="止盈卖出仓位%">{detailedResult.params?.sell_position_pct}</Descriptions.Item>
              <Descriptions.Item label="有效区间">{detailedResult.meta?.effective_start_date} ~ {detailedResult.meta?.effective_end_date}</Descriptions.Item>
              <Descriptions.Item label="交易日数">{detailedResult.meta?.trading_days}</Descriptions.Item>
              <Descriptions.Item label="初始资金">{detailedResult.meta?.initial_capital}</Descriptions.Item>
            </Descriptions>
          </Card>

          <Card title="回测资金曲线" style={{ marginBottom: 24 }} loading={detailLoading}>
            <ReactECharts option={equityOption} style={{ height: 360 }} />
          </Card>

          <Card title="SOXL K线 / 买卖点 / 成交量 / MA20" style={{ marginBottom: 24 }} loading={detailLoading}>
            <ReactECharts option={priceVolumeOption} style={{ height: 680 }} />
          </Card>

          <Card title="VIX / CNN / 复合恐贪指数" style={{ marginBottom: 24 }} loading={detailLoading}>
            <ReactECharts option={sentimentOption} style={{ height: 320 }} />
          </Card>

          <Card title="交易记录" loading={detailLoading}>
            <Table
              dataSource={detailedResult.trades || []}
              columns={tradeColumns}
              rowKey={(record, index) => `${record.date}-${record.action}-${index}`}
              pagination={{ pageSize: 12 }}
              scroll={{ x: 1200 }}
            />
          </Card>
        </div>
      )}
    </div>
  );
};

export default SoxlFearBacktest;
