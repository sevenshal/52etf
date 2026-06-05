import React, { useState, useEffect, useRef } from 'react';
import { Table, Button, Space, Popconfirm, message, Modal, Form, Input, Select, Layout, Tooltip, Tabs } from 'antd';
import { EditOutlined, DeleteOutlined, PlusOutlined, LeftOutlined, EyeOutlined, FileTextOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../../utils/request';
import ReactECharts from 'echarts-for-react';
import { SZDTConfigForm } from '../SZDTAutoTrading';

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

  useEffect(() => {
    // 检查是否有账户ID
    const accountId = localStorage.getItem('accountId');
    if (!accountId) {
      navigate('/profile');
      return;
    }
    loadStocks();
  }, [navigate, activeType]);

  const loadStocks = async () => {
    // 只有在有账户ID的情况下才获取数据
    fetchStocks();
  }

  // 获取股票列表
  const fetchStocks = async () => {
    setLoading(true);
    try {
      const [stocksResponse, emoResponse] = await Promise.all([
        request.get(`/api/quant/stocks`, { params: { etf_type: activeType } }),
        request.get(`/api/quant/etf/emotion/${activeType}`)
      ]);

      // 处理ETF情绪数据作为候选股票列表
      const formattedCandidates = emoResponse.data.data.map(item => ({
        code: item.code,
        name: item.name,
        lever: item.lever,
        emo_area: item.emo_area,
        tag: item.tag || '',
        index: item.index || ''
      }));
      setCandidates(formattedCandidates);

      // 组合数据
      const stocksWithEmo = stocksResponse.data.map(stock => {
        const emoData = emoResponse.data.data.find(emo => emo.code === stock.code);
        return {
          ...stock,
          etf_scale: emoData?.scale || -1,
          emo_name: emoData?.name || '-',
          emo_score: emoData?.emotion?.score || '-',
          emo_price: emoData?.emotion?.price || '-'
        };
      }).sort((a, b) => b.etf_scale - a.etf_scale);

      setData(stocksWithEmo);
    } catch (error) {
      const errorMessage = error.response?.detail || error.message || '获取数据失败';
      message.error(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  // 过滤已添加的股票
  const getAvailableCandidates = () => {
    const existingCodes = new Set(data.map(item => item.code));
    return candidates.filter(item => !existingCodes.has(item.code));
  };

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
      lever: 1,
      emo_area: 'a',
      type: Number(activeType)
    });
    setEditingRecord(null);
    setIsModalVisible(true);
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
      sorter: (a, b) => a.etf_scale - b.etf_scale
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: (_, record) => (
        <Space size="small">
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
          />
          <Popconfirm
            title="确定删除吗？"
            onConfirm={() => handleDelete(record.id)}
          >
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
            />
          </Popconfirm>
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
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleAdd}
          >
            添加
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
        rowKey="id"
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
            <Select
              showSearch
              placeholder="请选择股票"
              optionFilterProp="children"
              disabled={!!editingRecord}
              options={getAvailableCandidates().map(item => ({
                value: item.code,
                label: `${item.code} ${item.name}`
              }))}
              filterOption={(input, option) =>
                (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
            />
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
