import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Table, Button, Space, Popconfirm, message, Modal, Form, Input, Select, Layout, Tooltip, Tabs, Switch, Empty, Spin } from 'antd';
import { EditOutlined, DeleteOutlined, PlusOutlined, LeftOutlined, EyeOutlined, FileTextOutlined, LineChartOutlined, ExperimentOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../../utils/request';
import ReactECharts from 'echarts-for-react';
import { SZDTConfigForm } from '../SZDTAutoTrading';

const normalizeCode = (code) => String(code || '').replace('.', '').toUpperCase();
const toAStockTsCode = (code) => {
  const [market, ticker] = String(code || '').toUpperCase().split('.');
  return market && ticker ? `${ticker}.${market}` : String(code || '').toUpperCase();
};

const formatVolume = (value) => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  if (numeric >= 1e8) return `${(numeric / 1e8).toFixed(2)}亿`;
  if (numeric >= 1e4) return `${(numeric / 1e4).toFixed(2)}万`;
  return numeric.toFixed(0);
};

const ScaleRangeFilter = ({ selectedKeys, setSelectedKeys, confirm, clearFilters }) => {
  const [min = '', max = ''] = String(selectedKeys[0] || '').split('|');
  const update = (nextMin, nextMax) => setSelectedKeys((nextMin !== '' || nextMax !== '') ? [`${nextMin}|${nextMax}`] : []);
  return (
    <div style={{ padding: 8, width: 250 }} onKeyDown={(event) => event.stopPropagation()}>
      <Space size={4}>
        <Input type="number" min="0" value={min} placeholder="最小(亿)" onChange={(event) => update(event.target.value, max)} style={{ width: 100 }} />
        <span>至</span>
        <Input type="number" min="0" value={max} placeholder="最大(亿)" onChange={(event) => update(min, event.target.value)} style={{ width: 100 }} />
      </Space>
      <Space style={{ marginTop: 8 }}>
        <Button type="primary" size="small" onClick={() => confirm()}>筛选</Button>
        <Button size="small" onClick={() => { clearFilters?.(); confirm(); }}>重置</Button>
      </Space>
    </div>
  );
};

const FearStockList = () => {
  const navigate = useNavigate();
  const [data, setData] = useState([]);
  const [loading, setLoading] = useState(false);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [editingRecord, setEditingRecord] = useState(null);
  const [form] = Form.useForm();
  const [candidates, setCandidates] = useState([]);
  const [previewVisible, setPreviewVisible] = useState(false);
  const [previewType, setPreviewType] = useState('buy'); // 'buy' or 'sell'
  const [previewRange, setPreviewRange] = useState([60, 100]);
  const [previewAmount, setPreviewAmount] = useState(0);
  const [activeType, setActiveType] = useState(3);
  const [isConfigModalVisible, setIsConfigModalVisible] = useState(false);
  const [backtestVisible, setBacktestVisible] = useState(false);
  const [backtestLoading, setBacktestLoading] = useState(false);
  const [backtestResult, setBacktestResult] = useState(null);
  const [backtestParams, setBacktestParams] = useState({ initial_capital: 1000000, allow_leverage: false });
  const [historyRecord, setHistoryRecord] = useState(null);
  const [historyData, setHistoryData] = useState([]);
  const [historyVolumeByDate, setHistoryVolumeByDate] = useState({});
  const [historyLoading, setHistoryLoading] = useState(false);

  const tabItems = [
    { key: '1', label: '美股杠杆' },
    { key: '2', label: '美股常规' },
    { key: '7', label: '美股个股' },
    { key: '3', label: 'A股ETF' },
    { key: '4', label: '全球ETF' },
    { key: '5', label: '港股杠杆' },
    { key: '6', label: '港股常规' },
    { key: '8', label: '港股个股' },
  ];

  const fetchStocks = useCallback(async () => {
    setLoading(true);
    try {
      const [stocksResponse, emoResponse] = await Promise.all([
        request.get(`/api/quant/stocks`, { params: { etf_type: activeType } }),
        request.get(`/api/quant/etf/emotion/${activeType}`)
      ]);

      // 守猪逮兔列表是标的唯一来源；本地表仅覆盖交易配置。
      const formattedCandidates = emoResponse.data.data.map(item => ({
        code: item.code,
        name: item.name,
        lever: item.lever,
        emo_area: item.emo_area,
        tag: item.tag || '',
        index: item.index || ''
      }));
      setCandidates(formattedCandidates);

      let volumeMetrics = {};
      if (activeType === 3 && formattedCandidates.length > 0) {
        try {
          const volumeResponse = await request.get('/api/quant/etf/volume-metrics', {
            params: { codes: formattedCandidates.map(item => item.code).join(',') },
          });
          volumeMetrics = volumeResponse.data?.data || {};
        } catch (error) {
          // 实时行情失败不影响贪恐主列表；下次刷新会走后端的短缓存重试。
        }
      }

      const configuredStocks = new Map(
        stocksResponse.data.map(stock => [normalizeCode(stock.code), stock])
      );
      const stocksWithEmo = emoResponse.data.data.map(emoData => {
        const stock = configuredStocks.get(normalizeCode(emoData.code));
        return {
          ...(stock || {}),
          ...emoData,
          // 后续保存统一使用守猪逮兔当前返回的代码格式。
          code: emoData.code,
          name: emoData.name,
          // 守猪逮兔也有自己的 id；编辑/删除必须使用本地配置表的主键。
          id: stock?.id,
          isConfigured: Boolean(stock),
          enabled: Boolean(stock?.enabled),
          etf_scale: emoData.scale || -1,
          turnover: emoData.turnover ?? emoData.amount ?? emoData.emotion?.turnover ?? emoData.emotion?.amount ?? null,
          emo_name: emoData.name || '-',
          emo_score: emoData.emotion?.score ?? '-',
          emo_price: emoData.emotion?.price ?? '-',
          volume: volumeMetrics[toAStockTsCode(emoData.code)]?.volume ?? null,
          volume_ratio: volumeMetrics[toAStockTsCode(emoData.code)]?.volume_ratio ?? null,
        };
      }).sort((a, b) => b.etf_scale - a.etf_scale);

      setData(stocksWithEmo);
    } catch (error) {
      const errorMessage = error.response?.detail || error.message || '获取数据失败';
      message.error(errorMessage);
    } finally {
      setLoading(false);
    }
  }, [activeType]);

  useEffect(() => {
    // 检查是否有账户ID
    const accountId = localStorage.getItem('accountId');
    if (!accountId) {
      navigate('/profile');
      return;
    }
    fetchStocks();
  }, [fetchStocks, navigate]);

  // 处理编辑
  const handleEdit = (record) => {
    setEditingRecord(record);
    form.setFieldsValue(record);
    setIsModalVisible(true);
  };

  // 处理删除
  const handleDelete = async (id) => {
    try {
      await request.delete(`/api/quant/stocks/${id}`);
      message.success('删除成功');
      fetchStocks();
    } catch (error) {
      const errorMessage = error.response?.detail || error.message || '删除失败';
      message.error(errorMessage);
    }
  };

  // 处理表单提交
  const handleSubmit = async (values) => {
    try {
      const stockInfo = candidates.find(item => item.code === values.code);

      const data = {
        ...values,
        name: stockInfo?.name || '',
        lever: Number(values.lever ?? stockInfo?.lever ?? 1),
        emo_area: values.emo_area || stockInfo?.emo_area || 'a',
        when_buy: Number(values.when_buy),
        when_sell: Number(values.when_sell),
        buy_factor: Number(values.buy_factor),
        sell_factor: Number(values.sell_factor),
        buy_volume_ratio: Number(values.buy_volume_ratio || 0),
        max_position: Number(values.max_position),
        buy_amount: Number(values.buy_amount),
        sell_amount: Number(values.sell_amount)
      };

      const method = editingRecord ? 'put' : 'post';
      const url = editingRecord
        ? `/api/quant/stocks/${editingRecord.id}`
        : '/api/quant/stocks';

      await request[method](url, { ...data, type: Number(activeType) });
      message.success(editingRecord ? '更新成功' : '添加成功');
      setIsModalVisible(false);
      form.resetFields();
      setEditingRecord(null);
      fetchStocks();
    } catch (error) {
      const errorMessage = error.response?.detail || error.message || '操作失败';
      message.error(errorMessage);
    }
  };

  const handleAdd = () => {
    form.resetFields();
    // 设置默认值
    form.setFieldsValue({
      when_buy: -60,
      when_sell: 60,
      buy_amount: 2000,
      sell_amount: 2000,
      buy_factor: 1,
      sell_factor: 1,
      max_position: 5,
      buy_volume_ratio: activeType === 3 ? 1 : 0,
      lever: 1,
      emo_area: 'a',
      type: Number(activeType)
    });
    setEditingRecord(null);
    setIsModalVisible(true);
  };

  const handleAddForRecord = (record) => {
    handleAdd();
    form.setFieldsValue({
      code: record.code,
      name: record.emo_name || record.name,
      lever: record.lever || 1,
      emo_area: record.emo_area || 'a',
      enabled: true,
    });
  };

  const handleEnabledChange = async (record, enabled) => {
    if (!record.isConfigured) return;
    try {
      await request.put(`/api/quant/stocks/${record.id}`, {
        code: record.code,
        name: record.name,
        type: Number(activeType),
        when_buy: record.when_buy,
        when_sell: record.when_sell,
        max_position: record.max_position,
        buy_amount: record.buy_amount,
        sell_amount: record.sell_amount,
        buy_factor: record.buy_factor,
        sell_factor: record.sell_factor,
        buy_volume_ratio: record.buy_volume_ratio || 0,
        lever: record.lever,
        emo_area: record.emo_area,
        enabled,
      });
      message.success(enabled ? '已启用' : '已停用');
      fetchStocks();
    } catch (error) {
      const errorMessage = error.response?.detail || error.message || '更新启用状态失败';
      message.error(errorMessage);
    }
  };

  const handleShowHistory = async (record) => {
    setHistoryRecord(record);
    setHistoryData([]);
    setHistoryVolumeByDate({});
    setHistoryLoading(true);
    try {
      const response = await request.get(`/api/quant/etf/emotion/history/${record.code}`);
      const rows = response.data?.data || [];
      const validRows = rows.filter((item) => (
        item.date
        && Number.isFinite(Number(item.score))
        && Number.isFinite(Number(item.price))
      ));
      setHistoryData(validRows);

      if (validRows.length > 0) {
        const [market, ticker] = String(record.code || '').split('.');
        const startDate = validRows[0].date;
        const endDate = validRows[validRows.length - 1].date;
        let klineUrl = null;
        if (market === 'SH' || market === 'SZ') {
          klineUrl = `/api/stock/a-stock/klines/${ticker}.${market}`;
        } else if (market === 'US' || market === 'HK') {
          klineUrl = `/api/stock/klines/${ticker}.${market}`;
        }

        if (klineUrl) {
          try {
            const klineResponse = await request.get(klineUrl, {
              params: { start_date: startDate, end_date: endDate, period: 'd' },
            });
            setHistoryVolumeByDate(Object.fromEntries(
              (klineResponse.data || []).map((item) => [
                String(item.timestamp || '').slice(0, 10),
                Number(item.volume),
              ]).filter(([date, volume]) => date && Number.isFinite(volume))
            ));
          } catch (error) {
            // 不让成交量数据源失败影响贪恐和价格曲线。
            setHistoryVolumeByDate({});
          }
        }
      }
    } catch (error) {
      const errorMessage = error.response?.detail || error.message || '获取历史曲线失败';
      message.error(errorMessage);
    } finally {
      setHistoryLoading(false);
    }
  };

  const runBacktest = async () => {
    setBacktestLoading(true);
    try {
      const { data: result } = await request.post('/api/quant/a-stock-backtest', backtestParams);
      setBacktestResult(result);
    } catch (error) {
      message.error(error.response?.data?.detail || '回测失败');
    } finally {
      setBacktestLoading(false);
    }
  };

  const backtestChartOption = {
    tooltip: { trigger: 'axis' },
    grid: { left: 55, right: 28, top: 28, bottom: 42 },
    xAxis: { type: 'category', data: (backtestResult?.curve || []).map(item => item.date), axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', name: '净值', scale: true },
    dataZoom: [{ type: 'inside' }, { type: 'slider' }],
    series: [{
      type: 'line', name: '组合净值', showSymbol: false, smooth: true,
      data: (backtestResult?.curve || []).map(item => item.nav),
      lineStyle: { color: '#1677ff', width: 2 },
    }],
  };

  const historyChartOption = {
    tooltip: { trigger: 'axis' },
    legend: { data: ['贪恐指数', '价格'], top: 4 },
    grid: [
      { left: 52, right: 58, top: 42, height: '46%' },
      { left: 52, right: 58, top: '62%', height: '19%' },
    ],
    xAxis: [
      { type: 'category', data: historyData.map(item => item.date), axisLabel: { show: false } },
      { type: 'category', gridIndex: 1, data: historyData.map(item => item.date), axisLabel: { hideOverlap: true } },
    ],
    yAxis: [
      { type: 'value', name: '贪恐', min: -100, max: 100 },
      { type: 'value', name: '价格', scale: true },
      { type: 'value', name: '成交量', gridIndex: 1, scale: true, splitNumber: 3 },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 65, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], start: 65, end: 100 },
    ],
    series: [
      {
        name: '贪恐指数',
        type: 'line',
        data: historyData.map(item => Number(item.score)),
        smooth: true,
        showSymbol: false,
        lineStyle: { color: '#fa8c16', width: 2 },
        markLine: {
          silent: true,
          symbol: 'none',
          data: [{ yAxis: -60 }, { yAxis: 60 }],
          lineStyle: { type: 'dashed', color: '#bfbfbf' },
        },
      },
      {
        name: '价格',
        type: 'line',
        yAxisIndex: 1,
        data: historyData.map(item => Number(item.price)),
        smooth: true,
        showSymbol: false,
        lineStyle: { color: '#1677ff', width: 2 },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 2,
        data: historyData.map(item => historyVolumeByDate[item.date] ?? null),
        itemStyle: { color: '#91caff' },
      },
    ],
  };

  // 预览按钮点击
  const handlePreview = (type) => {
    const values = form.getFieldsValue();
    let start, amount;
    if (type === 'buy') {
      start = Math.abs(Number(values.when_buy) || 60);
      amount = Number(values.buy_amount) || 0;
    } else {
      start = Math.abs(Number(values.when_sell) || 60);
      amount = Number(values.sell_amount) || 0;
    }
    // 保证范围在[60, 100]
    if (start < 0) start = 0;
    if (start > 100) start = 100;
    setPreviewRange([start, 100]);
    setPreviewType(type);
    setPreviewAmount(amount);
    setPreviewVisible(true);
  };

  // 表格列定义
  const columns = [
    {
      title: '序号',
      dataIndex: 'id',
      key: 'id',
      width: 60,
      fixed: 'left',
      render: (_, __, index) => index + 1
    },
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      fixed: 'left',
      width: 100,
      render: (text, record) => record.emo_name || text
    },
    {
      title: '代码',
      dataIndex: 'code',
      key: 'code',
      width: 100,
      sorter: (a, b) => a.code.localeCompare(b.code)
    },
    {
      title: '价格',
      dataIndex: 'emo_price',
      key: 'emo_price',
      width: 80
    },
    {
      title: '恐贪指数',
      dataIndex: 'emo_score',
      key: 'emo_score',
      width: 80,
      sorter: (a, b) => {
        // 处理 '-' 的情况
        if (a.emo_score === '-') return -1;
        if (b.emo_score === '-') return 1;
        return a.emo_score - b.emo_score;
      },
      render: (value) => {
        let color = '';
        if (value <= -60) color = '#52c41a'; // 绿色
        else if (value >= 60) color = '#ff4d4f'; // 红色
        return <span style={{ color }}>{value}</span>;
      }
    },
    {
      title: '成交量',
      dataIndex: 'volume',
      key: 'volume',
      width: 100,
      align: 'right',
      render: (value) => formatVolume(value),
    },
    {
      title: <Tooltip title="当日截至当前分钟累计成交量 ÷ 前20个交易日同一分钟累计成交量均值（与市场提示看板完全一致）">量比</Tooltip>,
      dataIndex: 'volume_ratio',
      key: 'volume_ratio',
      width: 95,
      align: 'right',
      sorter: (a, b) => (a.volume_ratio || -1) - (b.volume_ratio || -1),
      render: (value) => (Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '-'),
    },
    {
      title: '何时买',
      dataIndex: 'when_buy',
      key: 'when_buy',
      width: 80,
      sorter: (a, b) => a.when_buy - b.when_buy
    },
    {
      title: '何时卖',
      dataIndex: 'when_sell',
      key: 'when_sell',
      width: 80,
      sorter: (a, b) => a.when_sell - b.when_sell
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 70,
      filters: [
        { text: '已启用', value: 'enabled' },
        { text: '未启用', value: 'disabled' },
      ],
      onFilter: (value, record) => value === 'enabled' ? Boolean(record.enabled) : !record.enabled,
      render: (_, record) => (
        <Tooltip title={record.isConfigured ? '控制该标的是否参与自动交易' : '请先添加并保存交易参数'}>
          <Switch
            size="small"
            checked={Boolean(record.enabled)}
            disabled={!record.isConfigured}
            onChange={(enabled) => handleEnabledChange(record, enabled)}
          />
        </Tooltip>
      ),
    },
    {
      title: (
        <Tooltip
          title={<span>3^((0~1)^<span style={{ textDecoration: 'underline dashed', color: 'darkorange' }}>x</span>)</span>}
          trigger={['hover']}
        >
          <span style={{ textDecoration: 'underline dashed', cursor: 'pointer' }}>
            买系数
          </span>
        </Tooltip>
      ),
      dataIndex: 'buy_factor',
      key: 'buy_factor',
      width: 50
    },
    {
      title: '卖系数',
      dataIndex: 'sell_factor',
      key: 'sell_factor',
      width: 50
    },
    {
      title: '最大仓位%',
      dataIndex: 'max_position',
      key: 'max_position',
      width: 100,
      sorter: (a, b) => a.max_position - b.max_position
    },
    {
      title: '买入金额',
      dataIndex: 'buy_amount',
      key: 'buy_amount',
      width: 100
    },
    {
      title: '卖出金额',
      dataIndex: 'sell_amount',
      key: 'sell_amount',
      width: 100
    },
    {
      title: '市场',
      dataIndex: 'emo_area',
      key: 'emo_area',
      width: 80,
      render: (text) => {
        const areaMap = {
          'a': 'A股',
          'us': '美股',
          'coin': '数字货币',
          'other': '其他'
        };
        return areaMap[text] || text;
      }
    },
    {
      title: '规模(亿)',
      dataIndex: 'etf_scale',
      key: 'etf_scale',
      width: 80,
      sorter: (a, b) => a.etf_scale - b.etf_scale,
      filterDropdown: (props) => <ScaleRangeFilter {...props} />,
      onFilter: (value, record) => {
        const [minText, maxText] = String(value || '').split('|');
        const scale = Number(record.etf_scale);
        return Number.isFinite(scale)
          && (minText === '' || scale >= Number(minText))
          && (maxText === '' || scale <= Number(maxText));
      },
    },
    {
      title: '成交额',
      dataIndex: 'turnover',
      key: 'turnover',
      width: 100,
      align: 'right',
      sorter: (a, b) => (Number(a.turnover) || -1) - (Number(b.turnover) || -1),
      render: (value) => formatVolume(value),
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      render: (_, record) => (
        <Space size="small">
          <Tooltip title="查看贪恐与价格历史曲线">
            <Button
              type="text"
              icon={<LineChartOutlined />}
              onClick={() => handleShowHistory(record)}
            />
          </Tooltip>
          {record.isConfigured ? <>
            <Button
              type="text"
              icon={<EditOutlined />}
              onClick={() => handleEdit(record)}
            />
            <Popconfirm
              title="确定删除该标的的交易配置吗？"
              onConfirm={() => handleDelete(record.id)}
            >
              <Button type="text" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </> : <Button
            type="link"
            icon={<PlusOutlined />}
            onClick={() => handleAddForRecord(record)}
          >
            添加
          </Button>}
        </Space>
      ),
    },
  ];

  const FactorPreviewModal = ({
    visible,
    onClose,
    initialFactor,
    range,
    type,
    amount
  }) => {
    const chartRef = useRef();
    const [factor, setFactor] = useState(initialFactor);

    useEffect(() => {
      if (visible) setFactor(initialFactor);
    }, [visible, initialFactor]);

    useEffect(() => {
      if (visible && chartRef.current) {
        setTimeout(() => {
          if (chartRef.current && chartRef.current.getEchartsInstance) {
            chartRef.current.getEchartsInstance().resize();
          }
        }, 200);
      }
    }, [visible, range]);

    // 计算操作倍数
    const calculateOperationMultiplier = (score, start, factor) => {
      const normalizedScore = Math.min(1, Math.max(0, (score - start) / (100 - start)));
      return Math.pow(3, Math.pow(normalizedScore, factor));
    };

    // 生成预览数据
    const generatePreviewData = () => {
      const data = [];
      const [start, end] = range;
      for (let score = start; score <= end; score += 0.5) {
        const multiplier = calculateOperationMultiplier(score, start, factor);
        data.push([score, amount * multiplier]);
      }
      return data;
    };

    const previewData = generatePreviewData();
    const yMin = amount;
    const yMax = amount * 3;

    const chartOption = {
      title: {
        text: `${type === 'buy' ? '买入' : '卖出'}金额预览`,
        left: 'center',
        textStyle: {
          fontSize: 16,
          fontWeight: 'bold'
        }
      },
      tooltip: {
        trigger: 'axis',
        formatter: function (params) {
          const score = params[0].data[0];
          const value = params[0].data[1];
          return `Score: ${score.toFixed(1)}<br/>金额: ${value.toFixed(2)}`;
        }
      },
      grid: {
        left: '10%',
        right: '10%',
        bottom: '15%',
        top: '15%',
        containLabel: true
      },
      xAxis: {
        type: 'value',
        name: 'Score',
        nameLocation: 'middle',
        nameGap: 30,
        min: range[0],
        max: range[1],
        axisLabel: {
          formatter: '{value}'
        }
      },
      yAxis: {
        type: 'value',
        name: '金额',
        nameLocation: 'middle',
        nameGap: 40,
        min: yMin,
        max: yMax,
        axisLabel: {
          formatter: '{value}'
        }
      },
      series: [
        {
          name: '金额',
          type: 'line',
          smooth: true,
          data: previewData,
          lineStyle: {
            width: 3,
            color: '#1890ff'
          },
          itemStyle: {
            color: '#1890ff'
          },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                {
                  offset: 0,
                  color: 'rgba(24,144,255,0.3)'
                },
                {
                  offset: 1,
                  color: 'rgba(24,144,255,0.1)'
                }
              ]
            }
          }
        }
      ]
    };

    return (
      <Modal
        open={visible}
        title={`${type === 'buy' ? '买入' : '卖出'}金额预览`}
        onCancel={() => onClose(factor)}
        footer={null}
        width={600}
        destroyOnClose={false}
      >
        <div style={{ marginBottom: 16 }}>
          <span style={{ fontWeight: 500, marginRight: 8 }}>{type === 'buy' ? '买入' : '卖出'}系数: </span>
          <Input
            type="number"
            min={0}
            max={10}
            step={0.01}
            value={factor}
            onChange={e => {
              let v = Number(e.target.value);
              if (isNaN(v)) v = 1;
              if (v < 0) v = 0;
              if (v > 10) v = 10;
              setFactor(v);
            }}
            style={{ width: 70, marginRight: 16 }}
          />
          <input
            type="range"
            min={0}
            max={10}
            step={0.01}
            value={factor}
            onChange={e => {
              const v = Number(e.target.value);
              setFactor(v);
            }}
            style={{ width: 180, verticalAlign: 'middle' }}
          />
        </div>
        <ReactECharts ref={chartRef} option={chartOption} style={{ height: '350px' }} />
      </Modal>
    );
  };

  // 预览弹窗联动表单
  const handlePreviewClose = (factor) => {
    if (previewType === 'buy') {
      form.setFieldsValue({ buy_factor: factor });
    } else {
      form.setFieldsValue({ sell_factor: factor });
    }
    setPreviewVisible(false);
  };

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <Layout.Header
        style={{
          height: '48px',
          lineHeight: '48px',
          padding: '0 16px',
          backgroundColor: '#fff',
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between'
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <Button
            type="text"
            icon={<LeftOutlined />}
            onClick={() => navigate(-1)}
            style={{ marginRight: '12px' }}
          />
          <span style={{ fontSize: '16px', fontWeight: 500 }}>股票列表</span>
        </div>

        <Space>
          <Button
            icon={<FileTextOutlined />}
            onClick={() => navigate('/fear/logs')}
            style={{ marginRight: 8 }}
          >
            日志
          </Button>
          <Button
            onClick={() => setIsConfigModalVisible(true)}
            style={{ marginRight: 8 }}
          >
            策略配置
          </Button>
          <Button icon={<ExperimentOutlined />} onClick={() => setBacktestVisible(true)}>
            回测
          </Button>
        </Space>
      </Layout.Header>

      <Tabs
        activeKey={String(activeType)}
        onChange={(key) => {
          setActiveType(Number(key));
        }}
        items={tabItems.map(t => ({ key: t.key, label: t.label }))}
      />

      <Table
        loading={loading}
        columns={columns}
        dataSource={data}
        rowKey="code"
        scroll={{ x: 'max-content' }}
        size="small"
        pagination={false}
      />

      {/* Config Modal */}
      <Modal
        title="贪恐策略配置"
        open={isConfigModalVisible}
        onCancel={() => setIsConfigModalVisible(false)}
        footer={null}
        destroyOnClose
      >
        <SZDTConfigForm onSuccess={() => setIsConfigModalVisible(false)} />
      </Modal>

      <Modal
        title="守猪逮兔 A股ETF 回测"
        open={backtestVisible}
        onCancel={() => setBacktestVisible(false)}
        width={920}
        footer={null}
        destroyOnClose={false}
      >
        <Space wrap style={{ marginBottom: 16 }}>
          <span>初始资金</span>
          <Input
            type="number"
            min="10000"
            value={backtestParams.initial_capital}
            onChange={(event) => setBacktestParams(prev => ({ ...prev, initial_capital: Number(event.target.value) || 0 }))}
            style={{ width: 130 }}
          />
          <span>开始日期</span>
          <Input type="date" value={backtestParams.start_date || ''} onChange={(event) => setBacktestParams(prev => ({ ...prev, start_date: event.target.value || undefined }))} style={{ width: 145 }} />
          <span>结束日期</span>
          <Input type="date" value={backtestParams.end_date || ''} onChange={(event) => setBacktestParams(prev => ({ ...prev, end_date: event.target.value || undefined }))} style={{ width: 145 }} />
          <span>允许杠杆</span>
          <Switch checked={backtestParams.allow_leverage} onChange={(allow_leverage) => setBacktestParams(prev => ({ ...prev, allow_leverage }))} />
          <Button type="primary" loading={backtestLoading} onClick={runBacktest}>开始回测</Button>
        </Space>
        <div style={{ color: '#8c8c8c', marginBottom: 12 }}>
          使用当前启用 A股ETF 的贪恐阈值、金额、系数、最大仓位及量比配置；日线信号在下一交易日开盘成交。
          {backtestResult?.rules?.sell_on_ema5_breakdown ? ' 当前已启用“贪婪且跌破 EMA5 才卖”。' : ' 当前为贪婪达到阈值即卖。'}
        </div>
        {backtestResult && <>
          <Table
            size="small"
            pagination={false}
            rowKey="key"
            style={{ marginBottom: 12 }}
            columns={[
              { title: '累计收益', dataIndex: 'total_return_pct', render: value => `${Number(value).toFixed(2)}%` },
              { title: '年化收益', dataIndex: 'annualized_return_pct', render: value => `${Number(value).toFixed(2)}%` },
              { title: '最大回撤', dataIndex: 'max_drawdown_pct', render: value => `${Number(value).toFixed(2)}%` },
              { title: '夏普', dataIndex: 'sharpe', render: value => value ?? '-' },
              { title: '成交', dataIndex: 'trade_count', render: (_, row) => `${row.trade_count}（买 ${row.buy_count} / 卖 ${row.sell_count}）` },
              { title: '平均资金利用率', dataIndex: 'avg_gross_exposure_pct', render: value => `${Number(value).toFixed(2)}%` },
              { title: '资金利用效率', dataIndex: 'capital_efficiency_pct', render: value => value == null ? '-' : `${Number(value).toFixed(2)}%` },
            ]}
            dataSource={[{ key: 'metrics', ...backtestResult.metrics }]}
          />
          <ReactECharts option={backtestChartOption} style={{ height: 310 }} />
          <Table
            size="small"
            rowKey={(record, index) => `${record.date}-${record.code}-${index}`}
            pagination={{ pageSize: 8, showSizeChanger: false }}
            columns={[
              { title: '日期', dataIndex: 'date' }, { title: '标的', dataIndex: 'name' }, { title: '代码', dataIndex: 'code' },
              { title: '方向', dataIndex: 'side', render: value => value === 'BUY' ? '买入' : '卖出' },
              { title: '数量', dataIndex: 'quantity' }, { title: '价格', dataIndex: 'price' }, { title: '贪恐', dataIndex: 'score' },
              { title: '金额', dataIndex: 'amount', render: value => Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) },
            ]}
            dataSource={backtestResult.trades}
          />
        </>}
      </Modal>

      <Modal
        title={editingRecord ? "编辑股票" : "添加股票"}
        open={isModalVisible}
        onOk={form.submit}
        onCancel={() => setIsModalVisible(false)}
        footer={null}
        width={360}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{
            when_buy: -60,
            when_sell: 60,
            buy_amount: 2000,
            sell_amount: 2000,
            max_position: 5,
            lever: 1,
            emo_area: 'a'
          }}
        >
          <Form.Item
            name="code"
            label="股票"
            rules={[{ required: true, message: '请选择股票' }]}
          >
            <Input disabled />
          </Form.Item>
          <Form.Item
            name="name"
            label="名称"
            hidden
          >
            <Input />
          </Form.Item>
          <Form.Item label="何时买 / 买系数 / 买入金额" required style={{ marginBottom: 16 }}>
            <Input.Group compact>
              <Form.Item
                name="when_buy"
                noStyle
                rules={[
                  { required: true, message: '请输入何时买' },
                  {
                    validator: (_, value) => {
                      const num = Number(value);
                      if (isNaN(num)) {
                        return Promise.reject('请输入数字');
                      }
                      if (num >= -100 && num <= 100) {
                        return Promise.resolve();
                      }
                      return Promise.reject('请输入-100到100之间的数字');
                    }
                  }
                ]}
              >
                <Input style={{ width: 80 }} placeholder="何时买" type="number" />
              </Form.Item>
              <Form.Item
                name="buy_factor"
                noStyle
                rules={[
                  { required: true, message: '请输入买系数' },
                  {
                    validator: (_, value) => {
                      const num = Number(value);
                      if (isNaN(num)) {
                        return Promise.reject('请输入数字');
                      }
                      if (num >= 0 && num <= 10) {
                        return Promise.resolve();
                      }
                      return Promise.reject('请输入0到10之间的数字');
                    }
                  }
                ]}
                initialValue={1}
              >
                <Input style={{ width: 80, marginLeft: 8 }} placeholder="买系数" type="number" step={0.01} min={0} max={10} />
              </Form.Item>
              <Form.Item
                name="buy_amount"
                noStyle
                rules={[
                  { required: true, message: '请输入买入金额' },
                  {
                    validator: (_, value) => {
                      const num = Number(value);
                      if (isNaN(num)) {
                        return Promise.reject('请输入数字');
                      }
                      if (num <= 0) {
                        return Promise.reject('买入金额必须大于0');
                      }
                      return Promise.resolve();
                    }
                  }
                ]}
              >
                <Input style={{ width: 95, marginLeft: 8 }} placeholder="买入金额" type="number" />
              </Form.Item>
              <Button icon={<EyeOutlined />} onClick={() => handlePreview('buy')} style={{ marginLeft: 8 }} />
            </Input.Group>
          </Form.Item>
          <Form.Item label="何时卖 / 卖系数 / 卖出金额" required style={{ marginBottom: 16 }}>
            <Input.Group compact>
              <Form.Item
                name="when_sell"
                noStyle
                rules={[
                  { required: true, message: '请输入何时卖' },
                  {
                    validator: (_, value) => {
                      const num = Number(value);
                      if (isNaN(num)) {
                        return Promise.reject('请输入数字');
                      }
                      if (num >= -100 && num <= 100) {
                        return Promise.resolve();
                      }
                      return Promise.reject('请输入-100到100之间的数字');
                    }
                  }
                ]}
              >
                <Input style={{ width: 80 }} placeholder="何时卖" type="number" />
              </Form.Item>
              <Form.Item
                name="sell_factor"
                noStyle
                rules={[
                  { required: true, message: '请输入卖系数' },
                  {
                    validator: (_, value) => {
                      const num = Number(value);
                      if (isNaN(num)) {
                        return Promise.reject('请输入数字');
                      }
                      if (num >= 0 && num <= 10) {
                        return Promise.resolve();
                      }
                      return Promise.reject('请输入0到10之间的数字');
                    }
                  }
                ]}
                initialValue={1}
              >
                <Input style={{ width: 80, marginLeft: 8 }} placeholder="卖系数" type="number" step={0.01} min={0} max={10} />
              </Form.Item>
              <Form.Item
                name="sell_amount"
                noStyle
                rules={[
                  { required: true, message: '请输入卖出金额' },
                  {
                    validator: (_, value) => {
                      const num = Number(value);
                      if (isNaN(num)) {
                        return Promise.reject('请输入数字');
                      }
                      if (num <= 0) {
                        return Promise.reject('卖出金额必须大于0');
                      }
                      return Promise.resolve();
                    }
                  }
                ]}
              >
                <Input style={{ width: 95, marginLeft: 8 }} placeholder="卖出金额" type="number" />
              </Form.Item>
              <Button icon={<EyeOutlined />} onClick={() => handlePreview('sell')} style={{ marginLeft: 8 }} />
            </Input.Group>
          </Form.Item>
          <Form.Item
            name="max_position"
            label="最大仓位%"
            rules={[
              { required: true, message: '请输入最大仓位' },
              {
                validator: (_, value) => {
                  const num = Number(value);
                  if (isNaN(num)) {
                    return Promise.reject('请输入数字');
                  }
                  if (num >= 0 && num <= 100) {
                    return Promise.resolve();
                  }
                  return Promise.reject('请输入0到100之间的数字');
                }
              }
            ]}
          >
            <Input type="number" />
          </Form.Item>
          {activeType === 3 && <Form.Item
            name="buy_volume_ratio"
            label="买入量比下限(>=)"
            tooltip="与市场提示看板同口径的同时段累计量比达到该值后，恐贪买入条件才会触发；填 0 表示不限制。"
            rules={[{ required: true, message: '请输入买入量比下限' }]}
          >
            <Input type="number" min="0" max="20" step="0.05" />
          </Form.Item>}
          <Form.Item
            name="lever"
            label="杠杆"
            rules={[{ required: true, message: '请选择杠杆' }]}
          >
            <Select
              options={[
                { value: 1, label: '1' },
                { value: 2, label: '2' },
                { value: 3, label: '3' },
              ]}
              style={{ width: '100%' }}
            />
          </Form.Item>
          <Form.Item
            name="emo_area"
            label="市场"
            rules={[{ required: true, message: '请选择市场' }]}
          >
            <Select
              options={[
                { value: 'a', label: 'A股' },
                { value: 'us', label: '美股' },
                { value: 'coin', label: '数字货币' },
                { value: 'other', label: '其他' },
              ]}
              style={{ width: '100%' }}
            />
          </Form.Item>
          <Form.Item
            name="type"
            hidden
          >
            <Input />
          </Form.Item>
          <Form.Item name="enabled" hidden initialValue>
            <Input />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit">
                {editingRecord ? '更新' : '添加'}
              </Button>
              <Button onClick={() => setIsModalVisible(false)}>
                取消
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={historyRecord ? `${historyRecord.emo_name || historyRecord.name}（${historyRecord.code}）历史曲线` : '历史曲线'}
        open={Boolean(historyRecord)}
        onCancel={() => setHistoryRecord(null)}
        footer={null}
        width={880}
        destroyOnClose
      >
        {historyLoading ? <div style={{ height: 460, display: 'grid', placeItems: 'center' }}><Spin /></div>
          : historyData.length > 0 ? <>
            <div style={{ color: '#8c8c8c', marginBottom: 8 }}>共 {historyData.length} 个交易日</div>
            <ReactECharts
              key={`${historyRecord?.code}-${historyData.length}`}
              option={historyChartOption}
              notMerge
              lazyUpdate={false}
              style={{ height: 430 }}
            />
          </> : <Empty description="该标的暂无可用的贪恐与价格历史数据" style={{ padding: '150px 0' }} />}
      </Modal>

      <FactorPreviewModal
        visible={previewVisible}
        onClose={handlePreviewClose}
        initialFactor={Number(previewType === 'buy' ? form.getFieldValue('buy_factor') : form.getFieldValue('sell_factor')) || 1}
        range={previewRange}
        type={previewType}
        amount={previewAmount}
      />
    </div>
  );
};

export default FearStockList;
