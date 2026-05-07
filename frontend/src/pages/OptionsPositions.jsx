import React, { useState, useEffect } from 'react';
import { Table, Card, Typography, Tag, Row, Col, Statistic, Tabs, Tooltip, Select, message } from 'antd';
import { formatNumber, formatDate } from '../utils/format';
import request from '../utils/request';
import { InfoCircleOutlined, RocketFilled } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';

const { Title } = Typography;
const { TabPane } = Tabs;


const OptionsPositions = () => {
  const navigate = useNavigate();  // 添加这行

  const { Option } = Select;

  const [loading, setLoading] = useState(false);
  const [accounts, setAccounts] = useState([]);
  const [selectedAccount, setSelectedAccount] = useState(null);

  const [positions, setPositions] = useState({
    Call: [],
    Put: []
  });
  const [stockPositions, setStockPositions] = useState({
    positions: [],
    summary: {}
  });
  const [activeTab, setActiveTab] = useState('Call');
  const [isMobile, setIsMobile] = useState(window.innerWidth < window.innerHeight);

  // 添加窗口大小变化监听
  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth < window.innerHeight);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Fetch Longport Accounts
  useEffect(() => {
    const fetchAccounts = async () => {
      try {
        const { data } = await request.get('/api/longport-accounts');
        setAccounts(data);
        if (data && data.length > 0) {
          // Default select the first account if not selected
          if (!selectedAccount) {
            const savedAccount = localStorage.getItem('longport_selected_account');
            const accountToSelect = data.find(acc => acc.lp_account_id === savedAccount)
              ? savedAccount
              : data[0].lp_account_id;

            setSelectedAccount(accountToSelect);
          }
        }
      } catch (error) {
        console.error('Failed to fetch longport accounts:', error);
        message.error('获取长桥账户列表失败');
      }
    }
    fetchAccounts();
  }, []);

  const handleAccountChange = (value) => {
    setSelectedAccount(value);
    localStorage.setItem('longport_selected_account', value);
  };

  const [riskFreeRate, setRiskFreeRate] = useState(null);

  const fetchPositions = async (optionType) => {
    if (!selectedAccount) return;

    setLoading(true);
    try {
      const { data } = await request.get('/api/positions/options', {
        params: {
          option_type: optionType,
          lp_account_id: selectedAccount
        }
      });
      setPositions(prev => ({
        ...prev,
        [optionType]: processPositionsData(data.positions)
      }));
      setRiskFreeRate(data.risk_free_rate);
    } catch (error) {
      console.error('Failed to fetch options positions:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // 初始只加载当前激活tab的数据
    if (selectedAccount) {
      fetchPositions(activeTab);
    }
  }, [activeTab, selectedAccount]);

  const processPositionsData = (data) => {
    return data.map(group => {
      // 计算距离到期天数
      const daysRemaining = Math.ceil((new Date(group.expiry) - new Date()) / (1000 * 60 * 60 * 24));

      // 处理每个持仓的数据
      const processedPositions = group.positions.map(pos => {
        // 计算价差百分比
        const price_diff = ((pos.strike_price - pos.stock_price) / pos.stock_price * 100).toFixed(2);

        // 计算持仓市值
        const position_value = pos.quantity * pos.market_price * 100;

        // 计算持仓成本
        const position_cost = pos.quantity * pos.cost_price * 100;

        // 计算行权成本
        const strike_cost = pos.option_type === 'Call' ? pos.strike_price + pos.cost_price : pos.strike_price - pos.cost_price;

        // 计算已实现盈亏
        const realized_amount = (pos.market_price - pos.cost_price) * pos.quantity * 100;
        const realized_percentage = (realized_amount / (Math.abs(pos.quantity) * pos.cost_price * 100) * 100).toFixed(2);

        // 计算时间进度
        const total_days = Math.ceil((new Date(pos.expiry) - new Date(pos.created_at || Date.now())) / (1000 * 60 * 60 * 24));
        const time_progress = Math.min(100, Math.max(0, ((total_days - daysRemaining) / total_days * 100))).toFixed(1);

        return {
          ...pos,
          price_diff: Number(price_diff),
          position_value,
          position_cost,
          strike_value: pos.strike_amount,
          strike_cost: strike_cost,
          realized_pnl: {
            amount: realized_amount,
            percentage: Number(realized_percentage)
          },
          time_progress: Number(time_progress)
        };
      });

      // 计算组统计数据
      const call_count = processedPositions
        .filter(p => p.option_type === 'Call')
        .reduce((sum, p) => sum + p.quantity, 0);
      const put_count = processedPositions
        .filter(p => p.option_type === 'Put')
        .reduce((sum, p) => sum + p.quantity, 0);

      // 计算价值区间统计
      const positions_by_price = processedPositions.reduce((acc, pos) => {
        const price_diff_percent = ((pos.strike_price - pos.stock_price) / pos.stock_price * 100);
        const position_value = pos.quantity * pos.strike_price * 100;
        const position_cost = pos.quantity * pos.cost_price * 100;

        // 计算行权期望值
        const expected_value = (pos.strike_amount * (pos.exercise_probability / 100));
        acc.total_expected_value += expected_value;

        if (price_diff_percent < -10) {
          acc.below_market_value += position_value;
        } else if (price_diff_percent > 10) {
          acc.above_market_value += position_value;
        } else {
          acc.near_market_value += position_value;
        }
        acc.total_realized_pnl += pos.realized_pnl.amount;
        acc.total_position_cost += position_cost;
        return acc;
      }, {
        below_market_value: 0,
        near_market_value: 0,
        above_market_value: 0,
        total_realized_pnl: 0,
        total_position_cost: 0,
        total_expected_value: 0
      });

      return {
        ...group,
        days_remaining: daysRemaining,
        call_count,
        put_count,
        ...positions_by_price,
        positions: processedPositions
      };
    });
  };

  useEffect(() => {
    if (activeTab === 'Stock') {
      fetchStockPositions();
    }
  }, [activeTab]);


  // 修改分组头部的统计卡片
  const renderExpiryHeader = (record, optionType) => {
    return (
      <div style={{ padding: '4px 0' }}>
        <Row justify="space-between" align="middle">
          <Col>
            <div style={{ fontSize: '16px', fontWeight: 'bold' }}>
              到期日: {formatDate(record.expiry)}
            </div>
            <div style={{ color: '#666', fontSize: '14px' }}>
              (还剩 {record.days_remaining} 天)
            </div>
          </Col>
          <Col>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: '16px', fontWeight: 'bold' }}>
                行权价值: ${formatNumber(record.total_strike_value, 2)}
              </div>
              <div style={{ color: '#666', fontSize: '14px' }}>
                ({formatNumber(record.total_strike_value / 10000, 1)}万)
              </div>
            </div>
          </Col>
        </Row>

        <Row gutter={[16, 0]} style={{ marginTop: '16px', flexDirection: isMobile ? 'column' : 'row' }}>
          <Col xs={24} md={8}>
            <Card size="small" bodyStyle={{ padding: '4px' }}>
              <Row gutter={16}>
                <Col span={8}>
                  <Statistic
                    title={
                      <span style={{ fontSize: '12px', color: optionType === 'Call' ? '#f5222d' : '#52c41a' }}>
                        {optionType === 'Call' ? '看涨期权' : '看跌期权'}
                      </span>
                    }
                    value={optionType === 'Call' ? record.call_count : record.put_count}
                    suffix="个"
                    valueStyle={{
                      color: optionType === 'Call' ? '#f5222d' : '#52c41a',
                      fontSize: '14px',
                      wordBreak: 'break-all'
                    }}
                  />
                </Col>
                <Col span={8}>
                  <Statistic
                    title={
                      <span style={{ fontSize: '12px' }}>
                        持仓成本
                      </span>
                    }
                    value={formatNumber(record.total_position_cost, 2)}
                    prefix="$"
                    valueStyle={{
                      fontSize: '14px',
                      fontWeight: 'bold',
                      wordBreak: 'break-all'
                    }}
                  />
                </Col>
                <Col span={8}>
                  <Statistic
                    title={
                      <span style={{ fontSize: '12px' }}>
                        已实现盈亏
                      </span>
                    }
                    value={formatNumber(record.total_realized_pnl, 2)}
                    formatter={value => (
                      <>
                        $ {value}
                        <span style={{ fontSize: '12px', marginLeft: '4px', display: isMobile ? 'block' : 'inline' }}>
                          ({formatNumber(record.total_realized_pnl / Math.abs(record.total_position_cost) * 100, 2)}%)
                        </span>
                      </>
                    )}
                    valueStyle={{
                      color: record.total_realized_pnl >= 0 ? '#52c41a' : '#f5222d',
                      fontSize: '14px',
                      fontWeight: 'bold',
                      wordBreak: 'break-all',
                      display: 'flex',
                      alignItems: 'center'
                    }}
                  />
                </Col>
              </Row>
            </Card>
          </Col>
          <Col xs={24} md={12}>
            <Card size="small" bodyStyle={{ padding: '4px' }}>
              <Row gutter={16}>
                <Col span={8}>
                  <Statistic
                    title={
                      <Tooltip title={optionType === 'Call' ? "行权概率高" : "行权概率低"}>
                        <span style={{ fontSize: '12px', cursor: 'help' }}>低于市价10%</span>
                      </Tooltip>
                    }
                    value={record.below_market_value}
                    valueStyle={{ fontSize: '14px', color: optionType == 'Call' ? '#f5222d' : '#52c41a', fontWeight: 'bold' }}
                    formatter={value => (
                      <>
                        $ {formatNumber(value, 2)}
                        <span style={{ fontSize: '12px', color: '#666', display: isMobile ? 'block' : 'inline' }}>
                          ({formatNumber(value / 10000, 1)}万)
                        </span>
                      </>
                    )}
                  />
                </Col>
                <Col span={8}>
                  <Statistic
                    title={
                      <Tooltip title="行权概率中等">
                        <span style={{ fontSize: '12px', cursor: 'help' }}>接近市价</span>
                      </Tooltip>
                    }
                    value={record.near_market_value}
                    valueStyle={{ fontSize: '14px', color: '#faad14', fontWeight: 'bold' }}
                    formatter={value => (
                      <>
                        $ {formatNumber(value, 2)}
                        <span style={{ fontSize: '12px', color: '#666', display: isMobile ? 'block' : 'inline' }}>
                          ({formatNumber(value / 10000, 1)}万)
                        </span>
                      </>
                    )}
                  />
                </Col>
                <Col span={8}>
                  <Statistic
                    title={
                      <Tooltip title={optionType === 'Call' ? "行权概率低" : "行权概率高"}>
                        <span style={{ fontSize: '12px', cursor: 'help' }}>高于市价10%</span>
                      </Tooltip>
                    }
                    value={record.above_market_value}
                    valueStyle={{ fontSize: '14px', color: optionType == 'Call' ? '#52c41a' : '#f5222d', fontWeight: 'bold' }}
                    formatter={value => (
                      <>
                        $ {formatNumber(value, 2)}
                        <span style={{ fontSize: '12px', color: '#666', display: isMobile ? 'block' : 'inline' }}>
                          ({formatNumber(value / 10000, 1)}万)
                        </span>
                      </>
                    )}
                  />
                </Col>
              </Row>
            </Card>
          </Col>
          <Col xs={24} md={4}>
            <Card size="small" bodyStyle={{ padding: '4px 8px' }}>
              <Statistic
                style={{ display: isMobile ? "flex" : "" }}
                title={
                  <Tooltip title="根据行权概率计算的行权价值的数学期望值">
                    <span style={{ fontSize: '12px', cursor: 'help', marginRight: '4px' }}>行权期望值</span>
                  </Tooltip>
                }
                value={record.total_expected_value}
                prefix="$"
                valueStyle={{
                  fontSize: '14px',
                  fontWeight: 'bold',
                  color: '#f5222d',
                  whiteSpace: 'nowrap'
                }}
                formatter={value => (
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: '4px' }}>
                    <span>{formatNumber(value, 2)}</span>
                    <span style={{ fontSize: '12px', color: '#666', whiteSpace: 'nowrap' }}>
                      ({formatNumber(value / 10000, 1)}万)
                    </span>
                  </div>
                )}
              />
            </Card>
          </Col>
        </Row>
      </div>
    );
  };

  const renderSummary = (optionType, positions) => {
    const summary = positions.reduce((acc, group) => {
      acc.totalCount += optionType === 'Call' ? group.call_count : group.put_count;
      acc.totalStrikeValue += group.total_strike_value;
      acc.totalRealizedPnl += group.total_realized_pnl;
      acc.totalPositionCost += group.total_position_cost;
      acc.totalExpectedValue += group.total_expected_value;
      return acc;
    }, {
      totalCount: 0,
      totalStrikeValue: 0,
      totalRealizedPnl: 0,
      totalPositionCost: 0,
      totalExpectedValue: 0
    });

    const renderStatItem = (label, value, extra, tooltip) => {
      const content = (
        <div style={{
          display: 'flex',
          flexDirection: 'row',
          gap: '4px',
          display: 'flex',
          alignItems: 'center',
        }}>
          <div style={{
            fontSize: '13px',
            color: '#666',
            whiteSpace: 'nowrap'
          }}>
            {label}
          </div>
          <div style={{
            fontSize: '14px',
            fontWeight: 'bold',
            wordBreak: 'break-all',
            gap: '4px'
          }}>
            {value}
            {extra && (
              <span style={{
                fontSize: '12px',
                color: '#666',
                display: 'inline'
              }}>
                {extra}
              </span>
            )}
          </div>
        </div>
      );

      return tooltip ? <Tooltip title={tooltip}>{content}</Tooltip> : content;
    };

    return (
      <Card style={{ marginBottom: '16px' }}>
        <Row gutter={[8, 16]}>
          <Col xs={12} sm={8} md={4}>
            {renderStatItem(
              optionType === 'Call' ? '看涨期权总数' : '看跌期权总数',
              `${summary.totalCount}个`
            )}
          </Col>
          <Col xs={12} sm={8} md={5}>
            {renderStatItem(
              '持仓总成本',
              `$ ${formatNumber(summary.totalPositionCost, 2)}`
            )}
          </Col>
          <Col xs={12} sm={8} md={5}>
            {renderStatItem(
              '已实现总盈亏',
              <span style={{ color: summary.totalRealizedPnl >= 0 ? '#52c41a' : '#f5222d' }}>
                $ {formatNumber(summary.totalRealizedPnl, 2)}
              </span>,
              ` (${formatNumber(summary.totalRealizedPnl / Math.abs(summary.totalPositionCost) * 100, 2)}%)`
            )}
          </Col>
          <Col xs={12} sm={12} md={5}>
            {renderStatItem(
              '行权总价值',
              `$ ${formatNumber(summary.totalStrikeValue, 2)}`,
              ` (${formatNumber(summary.totalStrikeValue / 10000, 1)}万)`
            )}
          </Col>
          <Col xs={24} sm={12} md={5}>
            {renderStatItem(
              '行权期望值',
              <span style={{ color: summary.totalExpectedValue >= 0 ? '#52c41a' : '#f5222d' }}>
                $ {formatNumber(summary.totalExpectedValue, 2)}
              </span>,
              ` (${formatNumber(summary.totalExpectedValue / 10000, 1)}万)`,
              "根据行权概率计算的总行权价值的数学期望值"
            )}
          </Col>
        </Row>
      </Card>
    );
  };

  const renderPositionsList = (optionType) => (
    <div style={{ padding: '4px' }}>
      {/* 添加汇总信息 */}
      {renderSummary(optionType, positions[optionType])}

      {/* 原有的分组列表 */}
      {positions[optionType].map(group => (
        <Card
          key={group.expiry}
          style={{ marginBottom: '16px' }}
          bodyStyle={{ padding: '0 16px' }}
        >
          {renderExpiryHeader(group, optionType)}
          <Table
            columns={columns}
            dataSource={group.positions}
            pagination={false}
            scroll={{ x: 1200 }}
            size="small"
            rowKey="symbol"
            loading={loading}
          />
        </Card>
      ))}
    </div>
  );

  const renderColorByPriceDiff = (record) => {
    let value = record.price_diff
    if (Math.abs(value) <= 10) {
      return '#faad14'; // 黄色，市价附近
    } else if (value > 10) {
      return record.option_type === 'Call' ? '#52c41a' : '#f5222d'; // Call绿色/Put红色
    } else {
      return record.option_type === 'Call' ? '#f5222d' : '#52c41a'; // Call红色/Put绿色
    }
  }

  const columns = [
    {
      title: '标的',
      dataIndex: 'stock_symbol',
      width: 80,
      render: (symbol) => (
        <a
          onClick={() => navigate(`/stock/${symbol}`, { state: { mainTabKey: '/options' } })}
          style={{ color: '#1890ff' }}
        >
          {symbol}
        </a>
      ),
    },
    {
      title: '类型',
      dataIndex: 'option_type',
      width: 70,
      render: type => (
        <Tag color={type === 'Call' ? '#f5222d' : '#52c41a'}>
          {type === 'Call' ? '看涨' : '看跌'}
        </Tag>
      ),
    },
    {
      title: '行权成本',
      dataIndex: 'strike_cost',
      width: 90,
      render: (value, record) => {
        // 判断颜色显示条件
        const isRed = record.option_type === 'Put'
          ? value > record.stock_price  // Put时行权成本高于市价显示红色
          : value < record.stock_price; // Call时行权成本低于市价显示红色

        return (
          <span style={{
            color: isRed ? '#f5222d' : 'inherit',
            fontWeight: isRed ? 'bold' : 'normal'
          }}>
            {formatNumber(value, 3)}
          </span>
        );
      },
    },
    {
      title: '股票市价',
      dataIndex: 'stock_price',
      width: 80,
      render: price => formatNumber(price, 2),
    },
    {
      title: '行权价',
      dataIndex: 'strike_price',
      width: 80,
      render: price => formatNumber(price, 2),
    },
    {
      title: '行权价差%',
      dataIndex: 'price_diff',
      width: 80,
      render: (value, record) => {
        let color = renderColorByPriceDiff(record);
        return (
          <span style={{ color, fontWeight: 'bold' }}>
            {value > 0 ? '+' : ''}{formatNumber(value, 1)}%
          </span>
        );
      },
    },
    {
      title: '数量',
      dataIndex: 'quantity',
      width: 70,
    },
    {
      title: '期权成本价',
      dataIndex: 'cost_price',
      width: 80,
      render: price => formatNumber(price, 3),
    },
    {
      title: '期权市价',
      dataIndex: 'market_price',
      width: 80,
      render: price => formatNumber(price, 3),
    },
    {
      title: '行权价值',
      dataIndex: 'strike_value',
      width: 120,
      render: (value, record) => {
        let color = renderColorByPriceDiff(record);
        return (
          <span style={{ color: color, fontWeight: 'bold' }}>
            ${formatNumber(value, 2)}
          </span>
        )
      },
    },
    {
      title: '持仓成本',
      dataIndex: 'position_cost',
      width: 100,
      render: value => formatNumber(value, 2),
    },
    {
      title: '持仓市值',
      dataIndex: 'position_value',
      width: 100,
      render: value => formatNumber(value, 2),
    },
    {
      title: '已实现盈亏',
      dataIndex: 'realized_pnl',
      width: 140,
      render: (value, record) => {
        // 检查是否达到平仓建议条件
        const shouldClose = value.percentage > (record.time_progress * 1.2);

        return (
          <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
            <span style={{ color: value.amount >= 0 ? '#52c41a' : '#f5222d', fontWeight: 'bold' }}>
              ${formatNumber(value.amount, 2)}
              <small>({value.amount >= 0 ? '+' : ''}{formatNumber(value.percentage, 1)}%)</small>
            </span>
            {shouldClose && (
              <Tooltip title={`盈利进度(${formatNumber(value.percentage, 1)}%)超过时间进度(${formatNumber(record.time_progress, 1)}%)的1.2倍，建议考虑平仓`}>
                <RocketFilled style={{
                  color: '#52c41a',
                  fontSize: '16px',
                  animation: 'bounce 1s infinite'
                }} />
              </Tooltip>
            )}
          </div>
        );
      },
    },
    {
      title: 'IV',
      dataIndex: 'implied_volatility',
      width: 60,
      render: value => (
        <span style={{ color: value > 0.5 ? '#f5222d' : '#52c41a' }}>
          {formatNumber(value * 100, 1)}%
        </span>
      ),
    },
    {
      title: (
        <Tooltip title={`按美联储存款利率 ${formatNumber((riskFreeRate || 0.05) * 100, 2)}% 计算`}>
          <span>行权概率<InfoCircleOutlined style={{ fontSize: '12px' }} /></span>
        </Tooltip>
      ),
      dataIndex: 'exercise_probability',
      width: 75,
      render: (value, record) => {
        const color = value > 50 ? '#f5222d' : (value > 30 ? '#faad14' : '#52c41a');
        return (
          <Tooltip title={`${record.option_type === 'Call' ? '突破' : '跌破'}行权价概率`}>
            <span style={{ color, fontWeight: 'bold' }}>
              {formatNumber(value, 1)}%
            </span>
          </Tooltip>
        );
      },
    },
    {
      title: '时间进度',
      dataIndex: 'time_progress',
      width: 90,
      render: value => (
        <div style={{
          background: '#f0f0f0',
          borderRadius: '10px',
          padding: '2px 8px',
          width: '100%'
        }}>
          <div style={{
            background: value > 66 ? '#f5222d' : '#52c41a',
            width: `${value}%`,
            height: '16px',
            borderRadius: '8px',
            color: '#fff',
            fontSize: '12px',
            lineHeight: '16px',
            textAlign: 'center'
          }}>
            {formatNumber(value, 1)}
          </div>
        </div>
      ),
    }
  ];


  // 添加获取股票持仓的函数
  const fetchStockPositions = async () => {
    if (!selectedAccount) return;

    setLoading(true);
    try {
      const { data } = await request.get('/api/positions/stocks', {
        params: { lp_account_id: selectedAccount }
      });
      setStockPositions(data);
    } catch (error) {
      console.error('Failed to fetch stock positions:', error);
    } finally {
      setLoading(false);
    }
  };

  // 添加渲染股票持仓汇总的函数
  const renderStockSummary = () => {
    const { summary } = stockPositions;

    return (
      <Card style={{ marginBottom: '16px' }}>
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="总资产"
              value={formatNumber(summary.total_assets, 2)}
              prefix="$"
              formatter={value => (
                <>
                  {value}
                  <span style={{ fontSize: '12px', marginLeft: '4px' }}>
                    ({formatNumber(summary.total_assets / 10000, 1)}万)
                  </span>
                </>
              )}
              valueStyle={{
                fontSize: '16px',
                fontWeight: 'bold'
              }}
            />
          </Col>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="持仓总成本"
              value={formatNumber(summary.total_cost, 2)}
              prefix="$"
              valueStyle={{ fontSize: '16px' }}
            />
          </Col>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="持仓市值"
              value={formatNumber(summary.total_market_value, 2)}
              prefix="$"
              valueStyle={{ fontSize: '16px' }}
            />
          </Col>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="持仓盈亏"
              value={formatNumber(summary.total_unrealized_pnl, 2)}
              prefix="$"
              formatter={value => (
                <>
                  {value}
                  <span style={{ fontSize: '12px', marginLeft: '4px' }}>
                    ({formatNumber(summary.total_unrealized_pnl_percent, 2)}%)
                  </span>
                </>
              )}
              valueStyle={{
                fontSize: '16px',
                color: summary.total_unrealized_pnl >= 0 ? '#52c41a' : '#f5222d'
              }}
            />
          </Col>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="现金"
              value={formatNumber(summary.cash_balance, 2)}
              prefix="$"
              formatter={value => (
                <>
                  {value}
                  <span style={{ fontSize: '12px', marginLeft: '4px' }}>
                    ({formatNumber(summary.cash_balance / summary.total_assets * 100, 1)}%)
                  </span>
                </>
              )}
              valueStyle={{ fontSize: '16px' }}
            />
          </Col>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="股票资产"
              value={formatNumber(summary.stock_amount, 2)}
              prefix="$"
              formatter={value => (
                <>
                  {value}
                  <span style={{ fontSize: '12px', marginLeft: '4px' }}>
                    ({formatNumber(summary.stock_amount / summary.total_assets * 100, 1)}%)
                  </span>
                </>
              )}
              valueStyle={{
                fontSize: '16px',
                color: '#1890ff'
              }}
            />
          </Col>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="债券资产"
              value={formatNumber(summary.bond_amount, 2)}
              prefix="$"
              formatter={value => (
                <>
                  {value}
                  <span style={{ fontSize: '12px', marginLeft: '4px' }}>
                    ({formatNumber(summary.bond_amount / summary.total_assets * 100, 1)}%)
                  </span>
                </>
              )}
              valueStyle={{
                fontSize: '16px',
                color: '#52c41a'
              }}
            />
          </Col>
          <Col xs={12} sm={8} md={3}>
            <Statistic
              title="期权资产"
              value={formatNumber(summary.option_market_value, 2)}
              prefix="$"
              formatter={value => (
                <>
                  {value}
                  <span style={{ fontSize: '12px', marginLeft: '4px' }}>
                    ({formatNumber(summary.option_market_value / summary.total_assets * 100, 1)}%)
                  </span>
                </>
              )}
              valueStyle={{
                fontSize: '16px',
                color: '#faad14'
              }}
            />
          </Col>
        </Row>
      </Card>
    );
  };

  // 添加股票持仓列表的列定义
  const stockColumns = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 100,
      render: (symbol) => (
        <a
          onClick={() => navigate(`/stock/${symbol}`, { state: { mainTabKey: '/options' } })}
          style={{ color: '#1890ff' }}
        >
          {symbol}
        </a>
      ),
    },
    {
      title: '名称',
      dataIndex: 'symbol_name',
      width: 120,
    },
    {
      title: '现价',
      dataIndex: 'current_price',
      width: 90,
      render: price => formatNumber(price, 2),
    },
    {
      title: '成本',
      dataIndex: 'cost_price',
      width: 90,
      render: price => formatNumber(price, 2),
    },
    {
      title: '持仓数量',
      dataIndex: 'quantity',
      width: 90,
      render: value => formatNumber(value, 0),
    },
    {
      title: '持仓市值',
      dataIndex: 'market_value',
      width: 120,
      render: value => (
        <>
          ${formatNumber(value, 2)}
          <small style={{ marginLeft: '4px', color: '#666' }}>
            ({formatNumber(value / 10000, 1)}万)
          </small>
        </>
      ),
    },
    {
      title: '持仓成本',
      dataIndex: 'position_cost',
      width: 120,
      render: value => (
        <>
          ${formatNumber(value, 2)}
          <small style={{ marginLeft: '4px', color: '#666' }}>
            ({formatNumber(value / 10000, 1)}万)
          </small>
        </>
      ),
    },
    {
      title: '持仓盈亏',
      dataIndex: 'unrealized_pnl',
      width: 140,
      sorter: (a, b) => a.unrealized_pnl - b.unrealized_pnl,
      sortDirections: ['descend', 'ascend'],
      render: (value, record) => (
        <span style={{ color: record.unrealized_pnl >= 0 ? '#52c41a' : '#f5222d' }}>
          ${formatNumber(value, 2)}
          <small style={{ marginLeft: '4px' }}>
            ({record.unrealized_pnl >= 0 ? '+' : ''}{formatNumber(record.unrealized_pnl_percent, 1)}%)
          </small>
        </span>
      ),
    },
    {
      title: '持仓比例',
      dataIndex: 'position_ratio',
      width: 90,
      render: value => `${formatNumber(value, 1)}%`,
    },
  ];

  // 添加渲染股票持仓的函数
  const renderStockPositions = () => (
    <div style={{ padding: '4px' }}>
      {renderStockSummary()}
      <Card>
        <Table
          columns={stockColumns}
          dataSource={stockPositions.positions}
          pagination={false}
          scroll={{ x: 800 }}
          size="small"
          rowKey="symbol"
          loading={loading}
        />
      </Card>
    </div>
  );

  return (
    <div style={{ padding: '24px' }}>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        defaultActiveKey="Call"
        tabBarExtraContent={
          <div style={{ display: 'flex', alignItems: 'center' }}>
            <span style={{ marginRight: 8 }}>账户:</span>
            <Select
              style={{ width: 100 }}
              value={selectedAccount}
              onChange={handleAccountChange}
              placeholder="长桥账户"
              loading={!accounts.length}
            >
              {accounts.map(acc => (
                <Option key={acc.lp_account_id} value={acc.lp_account_id}>
                  {acc.name}
                </Option>
              ))}
            </Select>
          </div>
        }
      >
        <TabPane
          tab={<span style={{ color: '#f5222d' }}>看涨期权</span>}
          key="Call"
        >
          {renderPositionsList('Call')}
        </TabPane>
        <TabPane
          tab={<span style={{ color: '#52c41a' }}>看跌期权</span>}
          key="Put"
        >
          {renderPositionsList('Put')}
        </TabPane>
        <TabPane tab="正股" key="Stock">
          {renderStockPositions()}
        </TabPane>
      </Tabs>
    </div >
  );
};

export default OptionsPositions;
