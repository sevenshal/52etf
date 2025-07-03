import React, { useState, useEffect } from 'react';
import { Card, Form, InputNumber, Button, message, Space, Table, Progress, Modal, DatePicker, Input, Select } from 'antd';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';
import dayjs from 'dayjs';

const { RangePicker } = DatePicker;

const SzdtBacktest = () => {
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const [verifyForm] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [verifyResult, setVerifyResult] = useState(null);
  const [polling, setPolling] = useState(false);
  const [showVerifyModal, setShowVerifyModal] = useState(false);
  const [selectedEtf, setSelectedEtf] = useState(null);
  const [etfList, setEtfList] = useState([]);

  // 设置默认日期范围
  const defaultDateRange = [
    dayjs().subtract(1, 'year'),
    dayjs()
  ];

  useEffect(() => {
    checkStatus();
    fetchEtfList();
    return () => {
      setPolling(false);
    };
  }, []);

  useEffect(() => {
    let timer;
    if (polling) {
      timer = setInterval(checkStatus, 2000);
    }
    return () => {
      if (timer) {
        clearInterval(timer);
      }
    };
  }, [polling]);

  const fetchEtfList = async () => {
    try {
      const response = await request.get('/api/quant/etf/emotion/3');
      if (response.data?.data) {
        setEtfList(response.data.data);
      }
    } catch (error) {
      message.error('获取ETF列表失败');
    }
  };

  const checkStatus = async () => {
    try {
      const response = await request.get('/api/backtest/status');
      setStatus(response.data);
      
      // 如果任务已完成（有结果或错误）或任务未运行
      if (!response.data.is_running || response.data.result || response.data.error) {
        if (response.data.result) {
          if (response.data.result.trades) {
            // 如果是回测结果
            setResult(response.data.result);
          } else {
            // 如果是验证结果
            setVerifyResult(response.data.result);
          }
        }
        setPolling(false);  // 停止轮询
      } else if (response.data.is_running && !polling) {
        setPolling(true);
      }
    } catch (error) {
      message.error('获取状态失败');
      setPolling(false);  // 发生错误时也停止轮询
    }
  };

  const handleStart = async (values) => {
    try {
      setLoading(true);
      // 从etfList中获取完整的ETF信息
      const selectedEtfs = values.etf_list.map(code => {
        const etf = etfList.find(e => e.code === code);
        return {
          code: etf.code,
          name: etf.name
        };
      });

      const params = {
        ...values,
        start_date: values.dateRange?.[0]?.format('YYYY-MM-DD'),
        end_date: values.dateRange?.[1]?.format('YYYY-MM-DD'),
        max_position_range: [
          values.max_position_range[0] / 100,
          values.max_position_range[1] / 100,
          values.max_position_range[2] / 100
        ],
        trade_amount_range: [
          values.trade_amount_range[0],
          values.trade_amount_range[1],
          values.trade_amount_range[2]
        ],
        buy_score_range: [
          values.buy_score_range[0],
          values.buy_score_range[1],
          values.buy_score_range[2]
        ],
        sell_score_range: [
          values.sell_score_range[0],
          values.sell_score_range[1],
          values.sell_score_range[2]
        ],
        etf_list: selectedEtfs
      };
      delete params.dateRange;
      
      await request.post('/api/backtest/start', params);
      message.success('回测任务已启动');
      setPolling(true);
    } catch (error) {
      message.error('启动回测失败：' + (error.response?.data?.detail || '未知错误'));
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = async () => {
    try {
      await request.post('/api/backtest/cancel');
      message.success('回测任务已取消');
      setPolling(false);
    } catch (error) {
      message.error('取消回测失败：' + (error.response?.data?.detail || '未知错误'));
    }
  };

  const handleVerify = async (values) => {
    try {
      setVerifying(true);
      const params = {
        initial_cash: form.getFieldValue('initial_cash'),
        start_date: form.getFieldValue('dateRange')?.[0]?.format('YYYY-MM-DD'),
        end_date: form.getFieldValue('dateRange')?.[1]?.format('YYYY-MM-DD'),
        etf_params: {
          [selectedEtf.code]: {
            max_position_ratio: values.max_position_ratio / 100,
            trade_amount: values.trade_amount,
            buy_score: values.buy_score,
            sell_score: values.sell_score
          }
        }
      };
      
      await request.post('/api/backtest/verify', params);
      message.success('验证任务已启动');
      setPolling(true);
      setShowVerifyModal(false);
    } catch (error) {
      message.error('启动验证失败：' + (error.response?.data?.detail || '未知错误'));
    } finally {
      setVerifying(false);
    }
  };

  const showVerifyModalForEtf = (etf) => {
    setSelectedEtf(etf);
    verifyForm.setFieldsValue({
      max_position_ratio: etf.max_position_ratio * 100,
      trade_amount: etf.trade_amount,
      buy_score: etf.buy_score,
      sell_score: etf.sell_score
    });
    setShowVerifyModal(true);
  };

  const columns = [
    {
      title: 'ETF代码',
      dataIndex: 'code',
      key: 'code',
    },
    {
      title: 'ETF名称',
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: '最大持仓比例',
      dataIndex: 'max_position_ratio',
      key: 'max_position',
      render: (value) => `${(value * 100).toFixed(0)}%`,
    },
    {
      title: '交易金额',
      dataIndex: 'trade_amount',
      key: 'trade_amount',
      render: (value) => `¥${value.toLocaleString()}`,
    },
    {
      title: '买入阈值',
      dataIndex: 'buy_score',
      key: 'buy_score',
    },
    {
      title: '卖出阈值',
      dataIndex: 'sell_score',
      key: 'sell_score',
    },
    {
      title: '操作',
      key: 'action',
      render: (_, record) => (
        <Button type="link" onClick={() => showVerifyModalForEtf(record)}>
          验证参数
        </Button>
      ),
    },
  ];

  const etfStatsColumns = [
    {
      title: 'ETF代码',
      dataIndex: 'code',
      key: 'code',
    },
    {
      title: 'ETF名称',
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => result?.parameters?.[record.code]?.name || '',
    },
    {
      title: '当前持仓',
      dataIndex: 'position',
      key: 'position',
      render: (value) => value.toLocaleString(),
    },
    {
      title: '买入次数',
      dataIndex: 'buy_count',
      key: 'buy_count',
    },
    {
      title: '卖出次数',
      dataIndex: 'sell_count',
      key: 'sell_count',
    },
    {
      title: '总买入金额',
      dataIndex: 'total_buy_amount',
      key: 'total_buy_amount',
      render: (value) => `¥${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    },
    {
      title: '总卖出金额',
      dataIndex: 'total_sell_amount',
      key: 'total_sell_amount',
      render: (value) => `¥${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    },
    {
      title: '当前持仓市值',
      dataIndex: 'current_position_value',
      key: 'current_position_value',
      render: (value) => `¥${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    },
    {
      title: '总收益',
      dataIndex: 'total_profit',
      key: 'total_profit',
      render: (value) => (
        <span style={{ color: value >= 0 ? '#52c41a' : '#f5222d' }}>
          ¥{value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </span>
      ),
    },
  ];

  const tradeColumns = [
    {
      title: '日期',
      dataIndex: 'date',
      key: 'date',
      render: (date) => dayjs(date).format('YYYY-MM-DD'),
    },
    {
      title: 'ETF代码',
      dataIndex: 'code',
      key: 'code',
    },
    {
      title: 'ETF名称',
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => result?.parameters?.[record.code]?.name || '',
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      render: (action) => action === 'BUY' ? '买入' : '卖出',
    },
    {
      title: '数量',
      dataIndex: 'quantity',
      key: 'quantity',
      render: (value) => value.toLocaleString(),
    },
    {
      title: '价格',
      dataIndex: 'price',
      key: 'price',
      render: (value) => `¥${value.toFixed(3)}`,
    },
    {
      title: '金额',
      dataIndex: 'amount',
      key: 'amount',
      render: (value) => `¥${value.toFixed(2)}`,
    },
    {
      title: '分数',
      dataIndex: 'score',
      key: 'score',
      render: (value) => value.toFixed(1),
    },
  ];

  return (
    <div style={{ padding: '24px' }}>
      <Card title="ETF回测参数设置" style={{ marginBottom: 24 }}>
        <Form
          form={form}
          layout="vertical"
          onFinish={handleStart}
          initialValues={{
            initial_cash: 1000000,
            max_position_range: [5, 50, 5],
            trade_amount_range: [5000, 200000, 5000],
            buy_score_range: [-100, -60, 5],
            sell_score_range: [60, 100, 5],
            dateRange: defaultDateRange
          }}
        >
          <Form.Item
            label="选择ETF"
            name="etf_list"
            rules={[{ required: true, message: '请选择至少一个ETF' }]}
          >
            <Select
              mode="multiple"
              placeholder="请选择ETF"
              style={{ width: '100%' }}
              options={etfList.map(etf => ({
                value: etf.code,
                label: `${etf.code} ${etf.name}`,
                ...etf
              }))}
              optionFilterProp="label"
              showSearch
              filterOption={(input, option) =>
                (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
            />
          </Form.Item>

          <Form.Item
            label="初始资金"
            name="initial_cash"
            rules={[{ required: true }]}
          >
            <InputNumber
              style={{ width: '100%' }}
              min={100000}
              step={100000}
              formatter={value => `¥ ${value}`.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}
              parser={value => value.replace(/\¥\s?|(,*)/g, '')}
            />
          </Form.Item>

          <Form.Item
            label="回测日期范围"
            name="dateRange"
          >
            <RangePicker style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            label="最大持仓比例范围(%)"
            rules={[{ required: true }]}
          >
            <Space>
              <Form.Item name={['max_position_range', 0]} noStyle>
                <InputNumber 
                  min={0} 
                  max={100} 
                  step={1}
                />
              </Form.Item>
              ~ 
              <Form.Item name={['max_position_range', 1]} noStyle>
                <InputNumber 
                  min={0} 
                  max={101} 
                  step={1}
                />
              </Form.Item>
              step:
              <Form.Item name={['max_position_range', 2]} noStyle>
                <InputNumber 
                  min={1} 
                  max={100} 
                  step={1}
                />
              </Form.Item>
            </Space>
          </Form.Item>

          <Form.Item
            label="交易金额范围(￥)"
            rules={[{ required: true }]}
          >
            <Space>
              <Form.Item name={['trade_amount_range', 0]} noStyle>
                <InputNumber 
                  min={1000} 
                  max={1000000} 
                  step={1000}
                />
              </Form.Item>
              ~ 
              <Form.Item name={['trade_amount_range', 1]} noStyle>
                <InputNumber 
                  min={1000} 
                  max={1000000} 
                  step={1000}
                />
              </Form.Item>
              step:
              <Form.Item name={['trade_amount_range', 2]} noStyle>
                <InputNumber 
                  min={1000} 
                  max={100000} 
                  step={1000}
                />
              </Form.Item>
            </Space>
          </Form.Item>

          <Form.Item
            label="买入阈值范围"
            rules={[{ required: true }]}
          >
            <Space>
              <Form.Item name={['buy_score_range', 0]} noStyle>
                <InputNumber min={-100} max={0} step={5} />
              </Form.Item>
              ~ 
              <Form.Item name={['buy_score_range', 1]} noStyle>
                <InputNumber min={-100} max={0} step={5} />
              </Form.Item>
              step:
              <Form.Item name={['buy_score_range', 2]} noStyle>
                <InputNumber min={1} max={100} step={1} />
              </Form.Item>
            </Space>
          </Form.Item>

          <Form.Item
            label="卖出阈值范围"
            rules={[{ required: true }]}
          >
            <Space>
              <Form.Item name={['sell_score_range', 0]} noStyle>
                <InputNumber min={0} max={100} step={5} />
              </Form.Item>
              ~ 
              <Form.Item name={['sell_score_range', 1]} noStyle>
                <InputNumber min={0} max={100} step={5} />
              </Form.Item>
              step:
              <Form.Item name={['sell_score_range', 2]} noStyle>
                <InputNumber min={1} max={100} step={1} />
              </Form.Item>
            </Space>
          </Form.Item>

          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={loading}>
                开始回测
              </Button>
              {status?.is_running && (
                <Button onClick={handleCancel}>
                  取消回测
                </Button>
              )}
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {status?.is_running && (
        <Card title={verifyResult ? "验证进度" : "回测进度"} style={{ marginBottom: 24 }}>
          <Progress 
            percent={Number(status.progress.toFixed(3))} 
            format={percent => (
              <span style={{ display: 'inline-block', width: '80px', textAlign: 'right' }}>
                {percent}%
              </span>
            )}
          />
          <div style={{ marginTop: 16 }}>
            开始时间：{status.start_time}
          </div>
        </Card>
      )}

      {result && (
        <Card title="回测结果" style={{ marginBottom: 24 }}>
          <div style={{ marginBottom: 16 }}>
            <Space>
              <span>总收益率：{result.total_return.toFixed(2)}%</span>
              <span>最大回撤：{result.max_drawdown.toFixed(2)}%</span>
              <span>夏普比率：{result.sharpe_ratio.toFixed(2)}</span>
            </Space>
          </div>
          
          <div style={{ marginBottom: 24 }}>
            <h3>参数配置</h3>
            <Table
              dataSource={result?.parameters ? 
                Object.entries(result.parameters).map(([code, v]) => ({
                  code,
                  ...v
                })) : []
              }  
              columns={columns}
              pagination={false}
            />
          </div>

          <div style={{ marginBottom: 24 }}>
            <h3>ETF交易统计</h3>
            <Table
              dataSource={result?.final_positions ? 
                Object.entries(result.final_positions).map(([code, v]) => ({
                  code,
                  ...v
                })) : []
              }
              columns={etfStatsColumns}
              pagination={false}
              scroll={{ x: true }}
            />
          </div>

          <div>
            <h3>交易记录</h3>
            <Table
              dataSource={result?.trades || []}
              columns={tradeColumns}
              pagination={{ pageSize: 10 }}
              scroll={{ x: true }}
            />
          </div>
        </Card>
      )}

      {verifyResult && (
        <Card title="参数验证结果" style={{ marginBottom: 24 }}>
          <div style={{ marginBottom: 16 }}>
            <Space>
              <span>总收益率：{verifyResult.total_return.toFixed(2)}%</span>
              <span>最大回撤：{verifyResult.max_drawdown.toFixed(2)}%</span>
              <span>夏普比率：{verifyResult.sharpe_ratio.toFixed(2)}</span>
            </Space>
          </div>
          {verifyResult.trades && (
            <div>
              <h3>交易记录</h3>
              <Table
                dataSource={verifyResult.trades}
                columns={tradeColumns}
                pagination={{ pageSize: 10 }}
                scroll={{ x: true }}
              />
            </div>
          )}
        </Card>
      )}

      <Modal
        title={`验证参数 - ${selectedEtf?.code}`}
        open={showVerifyModal}
        onCancel={() => setShowVerifyModal(false)}
        footer={null}
      >
        <Form
          form={verifyForm}
          layout="vertical"
          onFinish={handleVerify}
        >
          <Form.Item
            label="最大持仓比例(%)"
            name="max_position_ratio"
            rules={[{ required: true }]}
            initialValue={0.2}
          >
            <InputNumber 
              min={0} 
              max={100} 
              step={1} 
              style={{ width: '100%' }}
            />
          </Form.Item>

          <Form.Item
            label="交易金额(￥)"
            name="trade_amount"
            rules={[{ required: true }]}
            initialValue={100000}
          >
            <InputNumber 
              min={10000} 
              max={1000000} 
              step={10000} 
              style={{ width: '100%' }}
            />
          </Form.Item>

          <Form.Item
            label="买入阈值"
            name="buy_score"
            rules={[{ required: true }]}
            initialValue={-70}
          >
            <InputNumber min={-100} max={0} step={5} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            label="卖出阈值"
            name="sell_score"
            rules={[{ required: true }]}
            initialValue={70}
          >
            <InputNumber min={0} max={100} step={5} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={verifying}>
                开始验证
              </Button>
              <Button onClick={() => setShowVerifyModal(false)}>
                取消
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default SzdtBacktest;