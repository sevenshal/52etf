import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import dayjs from 'dayjs';
import {
  FilterOutlined,
  LineChartOutlined,
  ReloadOutlined,
  SearchOutlined,
  SettingOutlined,
  StockOutlined,
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import EastmoneyStockLink, { EastmoneyPortfolioLink } from '../components/EastmoneyStockLink';
import request from '../utils/request';
import './EastmoneyHoldingsResearch.css';

const { Text } = Typography;

const percentFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${Number(value).toFixed(2)}%`;
};

const numberFormatter = value => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
};

const normalizeSearchText = value => String(value || '').trim().toUpperCase();
const rankDeltaNumber = value => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? null
    : Number(value)
);
const isNewRankEntry = (record, hasCompareSnapshot) => (
  hasCompareSnapshot && rankDeltaNumber(record?.rank_5d_ago) === null
);
const compareRankDelta = (a, b, hasCompareSnapshot) => {
  const aIsNew = isNewRankEntry(a, hasCompareSnapshot);
  const bIsNew = isNewRankEntry(b, hasCompareSnapshot);
  if (aIsNew !== bIsNew) return aIsNew ? 1 : -1;
  if (aIsNew && bIsNew) {
    const aRank = rankDeltaNumber(a.composite_rank) ?? Number.MAX_SAFE_INTEGER;
    const bRank = rankDeltaNumber(b.composite_rank) ?? Number.MAX_SAFE_INTEGER;
    return bRank - aRank;
  }
  return (rankDeltaNumber(a.rank_change_5d) ?? 0)
    - (rankDeltaNumber(b.rank_change_5d) ?? 0);
};
const renderRankDelta = value => {
  const delta = rankDeltaNumber(value);
  if (delta === null || delta === 0) return '-';
  return (
    <Text style={{ color: delta > 0 ? '#389e0d' : '#cf1322' }}>
      {delta > 0 ? `+${delta}` : `${delta}`}
    </Text>
  );
};

// 权重/股价 5日倍数归一化：null/NaN 一律返回 null
const weightMomentumRatioNumber = value => (
  value === null || value === undefined || Number.isNaN(Number(value))
    ? null
    : Number(value)
);

// 东方财富组合行为方向（与后端 _eastmoney_direction 同规则，后端优先、前端兜底）
// 双条件判定：权重升幅>5% 且 权价比>1.05 才算加仓方向；权重降幅>5% 且 权价比<0.95 才算减仓方向；其余持平
const EASTMONEY_DIRECTIONS = ['新进', '持平', '顺势加仓', '逆势吸筹', '借涨减仓', '减仓'];
const EASTMONEY_DIRECTION_COLORS = {
  '新进': 'blue',
  '持平': 'default',
  '顺势加仓': 'green',
  '逆势吸筹': 'cyan',
  '借涨减仓': 'orange',
  '减仓': 'red',
};
const THS_BOARD_TYPE_LABELS = { I: '行业', N: '概念', TH: '主题' };

const eastmoneyDirectionOf = record => {
  if (record?.direction) return record.direction;
  const weight = weightMomentumRatioNumber(record?.weight_multiple_5d);
  const price = weightMomentumRatioNumber(record?.momentum_multiple_5d);
  const ratio = weightMomentumRatioNumber(record?.weight_price_ratio_5d);
  if (weight === null) return '新进';
  if (weight > 1.05 && ratio !== null && ratio > 1.05) {
    return price !== null && price >= 1 ? '顺势加仓' : '逆势吸筹';
  }
  if (weight < 0.95 && ratio !== null && ratio < 0.95) {
    return price !== null && price >= 1 ? '借涨减仓' : '减仓';
  }
  return '持平';
};

const isCashSymbol = record => {
  const symbol = String(record?.stock_symbol || '').toUpperCase();
  return symbol === 'CASH' || symbol === 'CN_CASH';
};

const signedFixed = value => (
  value !== null && value !== undefined
    ? `${value >= 0 ? '+' : ''}${Number(value).toFixed(2)}`
    : '-'
);

const multipleFixed = value => (
  value !== null && value !== undefined ? Number(value).toFixed(2) : '-'
);

const renderWeightMomentumRatio = (value, record) => {
  const ratio = weightMomentumRatioNumber(value);
  if (ratio === null) return '-';
  const weightMultiple = weightMomentumRatioNumber(record?.weight_multiple_5d);
  const momentumMultiple = weightMomentumRatioNumber(record?.momentum_multiple_5d);
  const weightChange = weightMomentumRatioNumber(record?.weight_change_5d);
  const momentum = weightMomentumRatioNumber(record?.momentum_5d);
  let color = '#595959';
  if (ratio > 1.15) color = '#389e0d';
  else if (ratio < 0.85) color = '#cf1322';
  const priceColor = momentumMultiple === null
    ? undefined
    : (momentumMultiple > 1 ? '#389e0d' : momentumMultiple < 1 ? '#cf1322' : undefined);
  const direction = isCashSymbol(record) ? null : eastmoneyDirectionOf(record);
  return (
    <Tooltip
      title={
        `5日权重 ${weightChange !== null ? `${signedFixed(weightChange)}pp` : '-'}`
        + `（${weightMultiple !== null ? `${multipleFixed(weightMultiple)}x` : '-'}）`
        + ` / 5日股价 ${momentum !== null ? `${signedFixed(momentum)}%` : '-'}`
        + `（${momentumMultiple !== null ? `${multipleFixed(momentumMultiple)}x` : '-'}）`
        + (direction ? ` / 行为方向：${direction}` : '')
      }
    >
      <Space size={4} direction="vertical" style={{ gap: 0 }}>
        <Space size={4}>
          <Text style={{ color, fontWeight: 400 }}>{ratio.toFixed(2)}x</Text>
          {direction ? <Tag color={EASTMONEY_DIRECTION_COLORS[direction] || 'default'} style={{ marginInlineEnd: 0 }}>{direction}</Tag> : null}
        </Space>
        <Text type="secondary" style={{ fontSize: 11, lineHeight: '14px' }}>
          权{weightMultiple !== null ? multipleFixed(weightMultiple) : '-'}x
          {' / '}
          <span style={{ color: priceColor }}>
            价{momentumMultiple !== null ? multipleFixed(momentumMultiple) : '-'}x
          </span>
        </Text>
      </Space>
    </Tooltip>
  );
};

const renderTodayWeightPriceRatio = (value, record) => {
  const ratio = weightMomentumRatioNumber(value);
  if (ratio === null) return '-';
  const weightMultiple = weightMomentumRatioNumber(record?.weight_multiple_today);
  const priceMultiple = weightMomentumRatioNumber(record?.momentum_multiple_today);
  const currentWeight = weightMomentumRatioNumber(record?.composite_weight_pct);
  const previousWeight = weightMomentumRatioNumber(record?.weight_previous_close);
  const weightChange = currentWeight !== null && previousWeight !== null
    ? currentWeight - previousWeight
    : null;
  const priceChange = weightMomentumRatioNumber(record?.momentum_today);
  let color = '#595959';
  if (ratio > 1.15) color = '#389e0d';
  else if (ratio < 0.85) color = '#cf1322';
  return (
    <Tooltip
      title={
        `今日权重 ${Number.isFinite(weightChange) ? `${signedFixed(weightChange)}pp` : '-'}`
        + `（${weightMultiple !== null ? `${multipleFixed(weightMultiple)}x` : '-'}）`
        + ` / 今日股价 ${priceChange !== null ? `${signedFixed(priceChange)}%` : '-'}`
        + `（${priceMultiple !== null ? `${multipleFixed(priceMultiple)}x` : '-'}）`
      }
    >
      <Text style={{ color }}>{ratio.toFixed(2)}x</Text>
    </Tooltip>
  );
};

const setNumericFilterValue = (setSelectedKeys, current, key, value) => {
  const next = { ...(current || {}), [key]: value };
  const hasMin = next.min !== null && next.min !== undefined && next.min !== '';
  const hasMax = next.max !== null && next.max !== undefined && next.max !== '';
  setSelectedKeys(hasMin || hasMax ? [next] : []);
};

const numericRangeFilterDropdown = ({
  setSelectedKeys,
  selectedKeys,
  confirm,
  clearFilters,
  minPlaceholder = '最小组合数',
  maxPlaceholder = '最大组合数',
}) => {
  const value = selectedKeys[0] || {};
  return (
    <div style={{ padding: 8, width: 180 }} onKeyDown={event => event.stopPropagation()}>
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <InputNumber
          min={0}
          precision={0}
          placeholder={minPlaceholder}
          value={value.min}
          onChange={nextValue => setNumericFilterValue(setSelectedKeys, value, 'min', nextValue)}
          style={{ width: '100%' }}
        />
        <InputNumber
          min={0}
          precision={0}
          placeholder={maxPlaceholder}
          value={value.max}
          onChange={nextValue => setNumericFilterValue(setSelectedKeys, value, 'max', nextValue)}
          style={{ width: '100%' }}
        />
        <Space>
          <Button size="small" type="primary" onClick={() => confirm()}>筛选</Button>
          <Button
            size="small"
            onClick={() => {
              clearFilters?.();
              confirm();
            }}
          >
            重置
          </Button>
        </Space>
      </Space>
    </div>
  );
};

const textSearchFilterDropdown = ({
  selectedKeys,
  setSelectedKeys,
  confirm,
  clearFilters,
}) => (
  <div style={{ padding: 8, width: 220 }} onKeyDown={event => event.stopPropagation()}>
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Input
        allowClear
        autoFocus
        placeholder="输入板块名"
        value={selectedKeys[0] || ''}
        onChange={event => setSelectedKeys(event.target.value ? [event.target.value] : [])}
        onPressEnter={() => confirm()}
      />
      <Space>
        <Button size="small" type="primary" onClick={() => confirm()}>筛选</Button>
        <Button
          size="small"
          onClick={() => {
            clearFilters?.();
            confirm();
          }}
        >
          重置
        </Button>
      </Space>
    </Space>
  </div>
);

const getHistoryChartOption = (historyRows = []) => {
  const dates = historyRows.map(row => row.snapshot_date);
  const weights = historyRows.map(row => (
    row.composite_weight_pct === null || row.composite_weight_pct === undefined
      ? null
      : Number(row.composite_weight_pct)
  ));
  const ranks = historyRows.map(row => (
    row.composite_rank === null || row.composite_rank === undefined
      ? null
      : Number(row.composite_rank)
  ));
  const priceKlines = historyRows.map(row => {
    const values = [row.open_price, row.close_price, row.low_price, row.high_price];
    return values.every(value => value !== null && value !== undefined && Number.isFinite(Number(value)))
      ? values.map(Number)
      : '-';
  });
  const maxRank = Math.max(12, ...ranks.filter(value => Number.isFinite(value)));

  return {
    color: ['#1677ff', '#fa8c16', '#52c41a'],
    grid: { left: 54, right: 112, top: 48, bottom: 54 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: value => (
        value === null || value === undefined || Number.isNaN(Number(value))
          ? '-'
          : Number(value).toFixed(2)
      ),
    },
    legend: {
      top: 8,
      data: ['综合权重', '综合排名', '价格K线'],
    },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: true,
      axisLabel: { color: '#64748b' },
    },
    yAxis: [
      {
        type: 'value',
        name: '权重%',
        min: 0,
        axisLabel: { formatter: value => `${value}%`, color: '#64748b' },
        splitLine: { lineStyle: { color: '#edf1f7' } },
      },
      {
        type: 'value',
        name: '排名',
        inverse: true,
        min: 1,
        max: maxRank,
        axisLabel: { formatter: value => `#${Math.round(value)}`, color: '#64748b' },
        splitLine: { show: false },
      },
      {
        type: 'value',
        name: '价格',
        position: 'right',
        offset: 54,
        scale: true,
        axisLabel: { formatter: value => Number(value).toFixed(2), color: '#64748b' },
        splitLine: { show: false },
      },
    ],
    dataZoom: dates.length > 80
      ? [
          { type: 'inside', start: 70, end: 100 },
          { type: 'slider', start: 70, end: 100, height: 18, bottom: 16 },
        ]
      : [],
    series: [
      {
        name: '综合权重',
        type: 'line',
        yAxisIndex: 0,
        data: weights,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { width: 2.2 },
        areaStyle: { opacity: 0.08 },
      },
      {
        name: '综合排名',
        type: 'line',
        yAxisIndex: 1,
        data: ranks,
        symbol: 'diamond',
        symbolSize: 5,
        lineStyle: { width: 1.8 },
      },
      {
        name: '价格K线',
        type: 'candlestick',
        yAxisIndex: 2,
        data: priceKlines,
        itemStyle: {
          color: '#ef4444',
          color0: '#22c55e',
          borderColor: '#ef4444',
          borderColor0: '#22c55e',
        },
      },
    ],
  };
};

const getBoardHistoryChartOption = (historyRows = []) => {
  const dates = historyRows.map(row => row.snapshot_date);
  const priceKlines = historyRows.map(row => {
    const values = [row.open_price, row.close_price, row.low_price, row.high_price];
    return values.every(value => value !== null && value !== undefined && Number.isFinite(Number(value)))
      ? values.map(Number)
      : '-';
  });
  return {
    color: ['#1677ff', '#fa8c16'],
    grid: { left: 56, right: 62, top: 48, bottom: dates.length > 80 ? 72 : 42 },
    tooltip: { trigger: 'axis' },
    legend: { top: 8, data: ['板块综合权重', '板块价格K线'] },
    xAxis: { type: 'category', data: dates, boundaryGap: true },
    yAxis: [
      {
        type: 'value',
        name: '权重%',
        min: 0,
        axisLabel: { formatter: value => `${value}%` },
        splitLine: { lineStyle: { color: '#edf1f7' } },
      },
      {
        type: 'value',
        name: '价格',
        scale: true,
        axisLabel: { formatter: value => Number(value).toFixed(2) },
        splitLine: { show: false },
      },
    ],
    dataZoom: dates.length > 80
      ? [{ type: 'inside', start: 70, end: 100 }, { type: 'slider', start: 70, end: 100, height: 18 }]
      : [],
    series: [
      {
        name: '板块综合权重',
        type: 'line',
        showSymbol: false,
        smooth: true,
        data: historyRows.map(row => row.composite_weight_pct),
      },
      {
        name: '板块价格K线',
        type: 'candlestick',
        yAxisIndex: 1,
        data: priceKlines,
        itemStyle: {
          color: '#ef4444',
          color0: '#22c55e',
          borderColor: '#ef4444',
          borderColor0: '#22c55e',
        },
      },
    ],
  };
};



const EastmoneyHoldingsResearch = () => {
  const [activeOnly, setActiveOnly] = useState(true);
  const [snapshotDate, setSnapshotDate] = useState('');
  const [searchText, setSearchText] = useState('');
  const [latestData, setLatestData] = useState(null);
  const [historyData, setHistoryData] = useState(null);
  const [detailData, setDetailData] = useState(null);
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [selectedHistoryDate, setSelectedHistoryDate] = useState('');
  const [selectedBoard, setSelectedBoard] = useState(null);
  const [boardHistoryData, setBoardHistoryData] = useState(null);
  const [boardHoldingsLoading, setBoardHoldingsLoading] = useState(false);
  const [boardHistoryLoading, setBoardHistoryLoading] = useState(false);
  const [latestLoading, setLatestLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const latestRequestRef = useRef(0);
  const historyRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const boardHoldingsRequestRef = useRef(0);
  const boardHistoryRequestRef = useRef(0);
  const [credentialOpen, setCredentialOpen] = useState(false);
  const [credentialSaving, setCredentialSaving] = useState(false);
  const [credentialForm] = Form.useForm();

  const openCredentialModal = useCallback(async () => {
    setCredentialOpen(true);
    credentialForm.resetFields();
    try {
      const response = await request.get('/api/factor-lab/eastmoney-credentials');
      credentialForm.setFieldValue('user_id', response.data?.user_id || '');
    } catch (error) {
      message.error(error?.response?.data?.detail || '加载东方财富凭据状态失败');
    }
  }, [credentialForm]);

  const saveCredentials = useCallback(async () => {
    try {
      const values = await credentialForm.validateFields();
      setCredentialSaving(true);
      await request.put('/api/factor-lab/eastmoney-credentials', values);
      message.success('东方财富凭据已保存');
      setCredentialOpen(false);
    } catch (error) {
      if (error?.errorFields) return;
      message.error(error?.response?.data?.detail || '保存东方财富凭据失败');
    } finally {
      setCredentialSaving(false);
    }
  }, [credentialForm]);


  const latestItems = useMemo(() => latestData?.items || [], [latestData]);
  const boardItems = useMemo(() => latestData?.board_items || [], [latestData]);
  const contrarianBoards = useMemo(() => latestData?.contrarian_boards || [], [latestData]);
  const ratioStats = useMemo(() => {
    const values = latestItems
      .map(item => weightMomentumRatioNumber(item.weight_price_ratio_5d))
      .filter(value => value !== null)
      .sort((a, b) => a - b);
    if (!values.length) return { mean: null, median: null };
    const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
    const mid = Math.floor(values.length / 2);
    const median = values.length % 2
      ? values[mid]
      : (values[mid - 1] + values[mid]) / 2;
    return { mean, median };
  }, [latestItems]);
  const selectedItem = useMemo(
    () => latestItems.find(item => item.stock_symbol === selectedSymbol) || null,
    [latestItems, selectedSymbol],
  );
  const historyRows = historyData?.history || [];
  const detailRows = detailData?.details || [];

  const filteredItems = useMemo(() => {
    const boardSymbols = selectedBoard
      ? new Set(selectedBoard.stockSymbols || [])
      : null;
    const boardFilteredItems = boardSymbols
      ? latestItems.filter(item => boardSymbols.has(item.stock_symbol))
      : latestItems;
    const keyword = normalizeSearchText(searchText);
    if (!keyword) return boardFilteredItems;
    return boardFilteredItems.filter(item => (
      normalizeSearchText(item.stock_symbol).includes(keyword)
      || normalizeSearchText(item.raw_stock_symbol).includes(keyword)
      || normalizeSearchText(item.stock_name).includes(keyword)
      || normalizeSearchText(item.segment_name).includes(keyword)
      || (item.fear_indexes || []).some(index => (
        normalizeSearchText(index.symbol).includes(keyword)
        || normalizeSearchText(index.label).includes(keyword)
      ))
    ));
  }, [latestItems, searchText, selectedBoard]);

  const clearBoardSelection = useCallback(() => {
    boardHoldingsRequestRef.current += 1;
    boardHistoryRequestRef.current += 1;
    setSelectedBoard(null);
    setBoardHistoryData(null);
    setBoardHoldingsLoading(false);
    setBoardHistoryLoading(false);
  }, []);

  const selectBoard = useCallback(async board => {
    const requestId = boardHoldingsRequestRef.current + 1;
    const historyRequestId = boardHistoryRequestRef.current + 1;
    boardHoldingsRequestRef.current = requestId;
    boardHistoryRequestRef.current = historyRequestId;
    setBoardHoldingsLoading(true);
    setBoardHistoryLoading(true);
    setBoardHistoryData(null);
    try {
      const [holdingsResult, historyResult] = await Promise.allSettled([
        request.get('/api/factor-lab/eastmoney-top-holdings/board-holdings', {
          params: {
            ths_code: board.ths_code,
            active_only: activeOnly,
            snapshot_date: snapshotDate || undefined,
          },
        }),
        request.get('/api/factor-lab/eastmoney-top-holdings/board-history', {
          params: { ths_code: board.ths_code, active_only: activeOnly, limit: 800 },
        }),
      ]);
      if (boardHoldingsRequestRef.current !== requestId) return;
      if (holdingsResult.status !== 'fulfilled') throw holdingsResult.reason;
      const response = holdingsResult.value;
      const stockSymbols = response.data?.stock_symbols || [];
      setSelectedBoard({
        thsCode: board.ths_code,
        name: board.name,
        stockSymbols,
      });
      if (boardHistoryRequestRef.current === historyRequestId && historyResult.status === 'fulfilled') {
        setBoardHistoryData(historyResult.value.data || null);
      } else if (historyResult.status === 'rejected') {
        message.error(historyResult.reason?.response?.data?.detail || '加载板块历史失败');
      }
      const firstVisible = latestItems.find(item => stockSymbols.includes(item.stock_symbol));
      if (firstVisible) setSelectedSymbol(firstVisible.stock_symbol);
    } catch (error) {
      if (boardHoldingsRequestRef.current === requestId) {
        message.error(error?.response?.data?.detail || error.message || '加载板块持仓股失败');
      }
    } finally {
      if (boardHoldingsRequestRef.current === requestId) setBoardHoldingsLoading(false);
      if (boardHistoryRequestRef.current === historyRequestId) setBoardHistoryLoading(false);
    }
  }, [activeOnly, latestItems, snapshotDate]);

  const fetchLatest = useCallback(async () => {
    const requestId = latestRequestRef.current + 1;
    latestRequestRef.current = requestId;
    setLatestLoading(true);
    try {
      const response = await request.get('/api/factor-lab/eastmoney-top-holdings/latest', {
        params: {
          active_only: activeOnly,
          limit: 800,
          snapshot_date: snapshotDate || undefined,
        },
      });
      if (latestRequestRef.current !== requestId) return;
      const payload = response.data || {};
      const items = payload.items || [];
      setLatestData(payload);
      setSelectedSymbol(previous => {
        if (previous && items.some(item => item.stock_symbol === previous)) {
          return previous;
        }
        return items[0]?.stock_symbol || '';
      });
    } catch (error) {
      if (latestRequestRef.current === requestId) {
        message.error(error?.response?.data?.detail || error.message || '加载东方财富持仓失败');
      }
    } finally {
      if (latestRequestRef.current === requestId) {
        setLatestLoading(false);
      }
    }
  }, [activeOnly, snapshotDate]);

  const fetchHistory = useCallback(async symbol => {
    if (!symbol) {
      setHistoryData(null);
      setSelectedHistoryDate('');
      return;
    }
    const requestId = historyRequestRef.current + 1;
    historyRequestRef.current = requestId;
    setHistoryLoading(true);
    try {
      const response = await request.get('/api/factor-lab/eastmoney-top-holdings/history', {
        params: { symbol, active_only: activeOnly, limit: 800 },
      });
      if (historyRequestRef.current !== requestId) return;
      const payload = response.data || {};
      const rows = payload.history || [];
      setHistoryData(payload);
      setSelectedHistoryDate(previous => {
        if (previous && rows.some(row => row.snapshot_date === previous)) {
          return previous;
        }
        return payload.latest?.snapshot_date || rows[rows.length - 1]?.snapshot_date || '';
      });
    } catch (error) {
      if (historyRequestRef.current === requestId) {
        message.error(error?.response?.data?.detail || error.message || '加载权重历史失败');
      }
    } finally {
      if (historyRequestRef.current === requestId) {
        setHistoryLoading(false);
      }
    }
  }, [activeOnly]);

  const fetchDetails = useCallback(async (symbol, snapshotDate) => {
    if (!symbol || !snapshotDate) {
      setDetailData(null);
      return;
    }
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    setDetailLoading(true);
    setDetailData(null);
    try {
      const response = await request.get('/api/factor-lab/eastmoney-top-holdings/details', {
        params: {
          symbol,
          snapshot_date: snapshotDate,
          active_only: activeOnly,
          limit: 2000,
        },
      });
      if (detailRequestRef.current !== requestId) return;
      setDetailData(response.data || {});
    } catch (error) {
      if (detailRequestRef.current === requestId) {
        message.error(error?.response?.data?.detail || error.message || '加载组合详情失败');
      }
    } finally {
      if (detailRequestRef.current === requestId) {
        setDetailLoading(false);
      }
    }
  }, [activeOnly]);

  useEffect(() => {
    fetchLatest();
  }, [fetchLatest]);

  useEffect(() => {
    fetchHistory(selectedSymbol);
  }, [fetchHistory, selectedSymbol]);

  useEffect(() => {
    fetchDetails(selectedSymbol, selectedHistoryDate);
  }, [fetchDetails, selectedHistoryDate, selectedSymbol]);

  const hasRankCompareSnapshot = Boolean(latestData?.rank_compare_snapshot_date);
  const latestColumns = useMemo(() => [
    {
      title: '排名',
      dataIndex: 'composite_rank',
      width: 76,
      fixed: 'left',
      sorter: (a, b) => Number(a.composite_rank || 0) - Number(b.composite_rank || 0),
      render: value => <Tag color={Number(value) <= 12 ? 'blue' : 'default'}>#{value}</Tag>,
    },
    {
      title: '5日排名上升',
      dataIndex: 'rank_change_5d',
      width: 118,
      align: 'right',
      sorter: (a, b) => compareRankDelta(a, b, hasRankCompareSnapshot),
      sortDirections: ['descend', 'ascend'],
      filters: [
        { text: '新进', value: 'new' },
        { text: '非新进', value: 'existing' },
      ],
      filterMultiple: false,
      onFilter: (value, record) => (
        value === 'new'
          ? isNewRankEntry(record, hasRankCompareSnapshot)
          : !isNewRankEntry(record, hasRankCompareSnapshot)
      ),
      render: (value, record) => (
        isNewRankEntry(record, hasRankCompareSnapshot)
          ? <Tag color="green">新进</Tag>
          : renderRankDelta(value)
      ),
    },
    {
      title: (
        <Tooltip title="最新榜单时点相对上一交易日15:00快照：权重倍数 ÷ 股价倍数。">
          今日权价比
        </Tooltip>
      ),
      dataIndex: 'weight_price_ratio_today',
      width: 118,
      align: 'right',
      sorter: (a, b) => (
        (weightMomentumRatioNumber(a.weight_price_ratio_today) ?? Number.NEGATIVE_INFINITY)
        - (weightMomentumRatioNumber(b.weight_price_ratio_today) ?? Number.NEGATIVE_INFINITY)
      ),
      sortDirections: ['descend', 'ascend'],
      render: renderTodayWeightPriceRatio,
    },
    {
      title: (
        <Tooltip title="权重5日倍数 ÷ 股价5日倍数。若权重上升纯粹由股价上涨带动，比值≈1；明显>1（尤其权重升但股价跌）说明权重涨幅超过股价，疑似主动加仓。">
          权重/股价 5日
        </Tooltip>
      ),
      dataIndex: 'weight_price_ratio_5d',
      width: 142,
      align: 'right',
      sorter: (a, b) => (
        (weightMomentumRatioNumber(a.weight_price_ratio_5d) ?? Number.NEGATIVE_INFINITY)
        - (weightMomentumRatioNumber(b.weight_price_ratio_5d) ?? Number.NEGATIVE_INFINITY)
      ),
      sortDirections: ['descend', 'ascend'],
      filters: EASTMONEY_DIRECTIONS.map(direction => ({ text: direction, value: direction })),
      filterMultiple: true,
      onFilter: (value, record) => eastmoneyDirectionOf(record) === value,
      render: renderWeightMomentumRatio,
    },
    {
      title: '股票',
      dataIndex: 'stock_symbol',
      width: 126,
      fixed: 'left',
      render: (value, record) => (
        <Space size={6}>
          {isCashSymbol(record)
            ? <Text strong>{value}</Text>
            : <EastmoneyStockLink symbol={value}><Text strong>{value}</Text></EastmoneyStockLink>}
          {isCashSymbol(record) ? <Tag color="gold">现金</Tag> : null}
        </Space>
      ),
    },
    {
      title: '名称',
      dataIndex: 'stock_name',
      width: 150,
      ellipsis: true,
    },
    {
      title: '所属指数',
      dataIndex: 'fear_indexes',
      width: 220,
      filters: (latestData?.index_options || []).map(index => ({
        text: `${index.label} (${index.symbol})`,
        value: index.symbol,
      })),
      filterSearch: true,
      onFilter: (value, record) => (
        (record.fear_indexes || []).some(index => index.symbol === value)
      ),
      render: indexes => (
        indexes?.length
          ? <Space size={[4, 4]} wrap>{indexes.map(index => <Tag key={index.symbol}>{index.label}</Tag>)}</Space>
          : '-'
      ),
    },
    {
      title: '综合权重',
      dataIndex: 'composite_weight_pct',
      width: 116,
      align: 'right',
      sorter: (a, b) => Number(a.composite_weight_pct || 0) - Number(b.composite_weight_pct || 0),
      render: percentFormatter,
    },
    {
      title: '持仓组合',
      dataIndex: 'holding_cube_count',
      width: 112,
      align: 'right',
      sorter: (a, b) => Number(a.holding_cube_count || 0) - Number(b.holding_cube_count || 0),
      filterIcon: filtered => (
        <FilterOutlined style={{ color: filtered ? '#1677ff' : undefined }} />
      ),
      filterDropdown: numericRangeFilterDropdown,
      onFilter: (value, record) => {
        const count = Number(record.holding_cube_count);
        if (!Number.isFinite(count)) return false;
        const hasMin = value?.min !== null && value?.min !== undefined && value?.min !== '';
        const hasMax = value?.max !== null && value?.max !== undefined && value?.max !== '';
        const min = hasMin ? Number(value.min) : null;
        const max = hasMax ? Number(value.max) : null;
        return (min === null || count >= min) && (max === null || count <= max);
      },
      render: value => `${numberFormatter(value)} / ${numberFormatter(latestData?.cube_count)}`,
    },
    {
      title: '组合占比',
      dataIndex: 'holding_cube_ratio_pct',
      width: 106,
      align: 'right',
      sorter: (a, b) => Number(a.holding_cube_ratio_pct || 0) - Number(b.holding_cube_ratio_pct || 0),
      render: percentFormatter,
    },
    {
      title: '持有均重',
      dataIndex: 'average_weight_pct',
      width: 106,
      align: 'right',
      sorter: (a, b) => Number(a.average_weight_pct || 0) - Number(b.average_weight_pct || 0),
      render: percentFormatter,
    },
    {
      title: '最好榜单',
      dataIndex: 'best_year_rank',
      width: 96,
      align: 'right',
      sorter: (a, b) => Number(a.best_year_rank || 999999) - Number(b.best_year_rank || 999999),
      render: value => (value ? `#${value}` : '-'),
    },
    {
      title: '板块',
      dataIndex: 'segment_name',
      width: 120,
      ellipsis: true,
      render: value => value || '-',
    },
  ], [hasRankCompareSnapshot, latestData?.cube_count, latestData?.index_options]);

  const boardColumns = useMemo(() => [
    {
      title: '细分板块',
      dataIndex: 'name',
      width: 180,
      fixed: 'left',
      filterIcon: filtered => (
        <FilterOutlined style={{ color: filtered ? '#1677ff' : undefined }} />
      ),
      filterDropdown: textSearchFilterDropdown,
      onFilter: (value, record) => (
        normalizeSearchText(record.name).includes(normalizeSearchText(value))
      ),
      render: (value, record) => (
        <Space size={6}>
          <Text strong>{value}</Text>
          <Tag>{THS_BOARD_TYPE_LABELS[record.board_type] || record.board_type}</Tag>
        </Space>
      ),
    },
    {
      title: '方向',
      dataIndex: 'direction',
      width: 112,
      filters: EASTMONEY_DIRECTIONS.filter(value => value !== '新进').map(value => ({ text: value, value })),
      onFilter: (value, record) => record.direction === value,
      render: value => <Tag color={EASTMONEY_DIRECTION_COLORS[value]}>{value}</Tag>,
    },
    {
      title: '5日权价比',
      dataIndex: 'weight_price_ratio_5d',
      width: 112,
      align: 'right',
      sorter: (a, b) => Number(a.weight_price_ratio_5d || 0) - Number(b.weight_price_ratio_5d || 0),
      render: value => (value == null ? '-' : `${Number(value).toFixed(2)}x`),
    },
    {
      title: (
        <Tooltip title="板块覆盖的东方财富持仓股中，个股方向为逆势吸筹且至少被3个榜单组合持有的数量占比。">
          吸筹占比
        </Tooltip>
      ),
      dataIndex: 'contrarian_stock_ratio_pct',
      width: 112,
      align: 'right',
      defaultSortOrder: 'descend',
      sorter: (a, b) => Number(a.contrarian_stock_ratio_pct || 0) - Number(b.contrarian_stock_ratio_pct || 0),
      render: (value, record) => (
        <Tooltip title={`${numberFormatter(record.contrarian_stock_count || 0)} / ${numberFormatter(record.stock_count || 0)} 只`}>
          {percentFormatter(value)}
        </Tooltip>
      ),
    },
    {
      title: '权重变化',
      dataIndex: 'weight_change_5d',
      width: 110,
      align: 'right',
      sorter: (a, b) => Number(a.weight_change_5d || 0) - Number(b.weight_change_5d || 0),
      render: value => `${signedFixed(value)}pp`,
    },
    {
      title: '板块涨跌',
      dataIndex: 'momentum_5d',
      width: 110,
      align: 'right',
      sorter: (a, b) => Number(a.momentum_5d || 0) - Number(b.momentum_5d || 0),
      render: value => `${signedFixed(value)}%`,
    },
    {
      title: '东方财富综合权重',
      dataIndex: 'composite_weight_pct',
      width: 132,
      align: 'right',
      sorter: (a, b) => Number(a.composite_weight_pct || 0) - Number(b.composite_weight_pct || 0),
      render: percentFormatter,
    },
    {
      title: '覆盖持仓股',
      dataIndex: 'stock_count',
      width: 110,
      align: 'right',
      sorter: (a, b) => Number(a.stock_count || 0) - Number(b.stock_count || 0),
      filterIcon: filtered => (
        <FilterOutlined style={{ color: filtered ? '#1677ff' : undefined }} />
      ),
      filterDropdown: props => numericRangeFilterDropdown({
        ...props,
        minPlaceholder: '最少持仓股',
        maxPlaceholder: '最多持仓股',
      }),
      onFilter: (value, record) => {
        const count = Number(record.stock_count);
        if (!Number.isFinite(count)) return false;
        const hasMin = value?.min !== null && value?.min !== undefined && value?.min !== '';
        const hasMax = value?.max !== null && value?.max !== undefined && value?.max !== '';
        const min = hasMin ? Number(value.min) : null;
        const max = hasMax ? Number(value.max) : null;
        return (min === null || count >= min) && (max === null || count <= max);
      },
      render: value => `${numberFormatter(value)}只`,
    },
  ], []);

  const historyColumns = useMemo(() => [
    { title: '日期', dataIndex: 'snapshot_date', width: 118 },
    { title: '收盘价', dataIndex: 'close_price', width: 100, align: 'right', render: value => (value == null ? '-' : Number(value).toFixed(2)) },
    { title: '排名', dataIndex: 'composite_rank', width: 88, align: 'right', render: value => (value ? `#${value}` : '-') },
    { title: '综合权重', dataIndex: 'composite_weight_pct', width: 116, align: 'right', render: percentFormatter },
    { title: '持仓组合', dataIndex: 'holding_cube_count', width: 112, align: 'right' },
    { title: '组合占比', dataIndex: 'holding_cube_ratio_pct', width: 106, align: 'right', render: percentFormatter },
    { title: '持有均重', dataIndex: 'average_weight_pct', width: 106, align: 'right', render: percentFormatter },
  ], []);

  const detailColumns = useMemo(() => [
    {
      title: '实盘榜',
      dataIndex: 'year_rank',
      width: 78,
      align: 'right',
      sorter: (a, b) => Number(a.year_rank || 999999) - Number(b.year_rank || 999999),
      render: value => (value ? `#${value}` : '-'),
    },
    {
      title: '组合',
      dataIndex: 'cube_symbol',
      width: 112,
      render: value => (
        value
          ? <EastmoneyPortfolioLink symbol={value}>{value}</EastmoneyPortfolioLink>
          : '-'
      ),
    },
    {
      title: '组合名称',
      dataIndex: 'cube_name',
      width: 180,
      ellipsis: true,
      render: value => value || '-',
    },
    {
      title: '仓位',
      dataIndex: 'weight_pct',
      width: 92,
      align: 'right',
      sorter: (a, b) => Number(a.weight_pct || 0) - Number(b.weight_pct || 0),
      render: percentFormatter,
    },
    {
      title: '榜单调仓',
      dataIndex: 'active_rebalance_at',
      width: 168,
      render: value => (value ? String(value).replace('T', ' ').slice(0, 19) : '-'),
    },
    {
      title: '持仓来源',
      dataIndex: 'holdings_source',
      width: 92,
      render: value => value || '-',
    },
  ], []);

  const chartOption = useMemo(() => getHistoryChartOption(historyRows), [historyRows]);
  const boardHistoryRows = boardHistoryData?.history || [];
  const boardChartOption = useMemo(
    () => getBoardHistoryChartOption(boardHistoryRows),
    [boardHistoryRows],
  );
  const latestRow = historyData?.latest || selectedItem;
  const detailSummaryText = selectedHistoryDate
    ? `${selectedHistoryDate} · ${numberFormatter(detailData?.holding_cube_count || 0)} / ${numberFormatter(detailData?.cube_count || 0)} 个组合 · 合计 ${percentFormatter(detailData?.total_weight_pct)}`
    : '-';

  return (
    <div className="eastmoney-holdings-page">
      <Row gutter={[12, 12]} className="eastmoney-holdings-metrics">
        <Col xs={12} md={4}>
          <Card bordered={false}>
            <Statistic title={snapshotDate ? '所选日期' : '最新日期'} value={latestData?.snapshot_date || snapshotDate || '-'} />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card bordered={false}>
            <Statistic title="统计组合" value={latestData?.cube_count || 0} />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card bordered={false}>
            <Statistic title="活跃/来源" value={`${numberFormatter(latestData?.active_cube_count || 0)} / ${numberFormatter(latestData?.source_cube_count || 0)}`} />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card bordered={false}>
            <Statistic title="覆盖标的" value={latestItems.length} />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card bordered={false}>
            <Statistic
              title={
                <Tooltip title="全部标的中，5日权重倍数÷股价倍数比值的均值（≈1为被动，>1疑似主动加仓）">
                  5日权价比均值
                </Tooltip>
              }
              value={ratioStats.mean !== null ? `${ratioStats.mean.toFixed(2)}x` : '-'}
            />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card bordered={false}>
            <Statistic
              title={
                <Tooltip title="全部标的中，5日权重倍数÷股价倍数比值的中位数（≈1为被动，>1疑似主动加仓）">
                  5日权价比中位数
                </Tooltip>
              }
              value={ratioStats.median !== null ? `${ratioStats.median.toFixed(2)}x` : '-'}
            />
          </Card>
        </Col>
      </Row>

      <Card
        bordered={false}
        title={(
          <Tooltip title="仅显示板块自身方向为逆势吸筹、覆盖持仓股大于10只且吸筹股不为空的板块；按‘逆势吸筹且至少被3个榜单组合持有’的个股占比从高到低取前15。">
            正在逆势吸筹的细分板块
          </Tooltip>
        )}
        style={{ marginBottom: 12 }}
      >
        {contrarianBoards.length ? (
          <Space size={[8, 8]} wrap>
            {contrarianBoards.map(board => (
              <Tooltip
                key={board.ths_code}
                title={`逆势吸筹 ${board.contrarian_stock_count} / ${board.stock_count} 只；东方财富综合权重 ${percentFormatter(board.composite_weight_pct)}`}
              >
                <Tag color="cyan">
                  {board.name} 吸筹{percentFormatter(board.contrarian_stock_ratio_pct)} · {board.contrarian_stock_count}/{board.stock_count}只 · 板块{signedFixed(board.momentum_5d)}%
                </Tag>
              </Tooltip>
            ))}
          </Space>
        ) : (
          <Text type="secondary">
            {boardItems.length ? '当前没有满足条件的板块' : '暂无板块成分或行情缓存，请先运行A股基础数据同步'}
          </Text>
        )}
      </Card>

      <Row gutter={[12, 12]} style={{ marginBottom: 12 }}>
        <Col xs={24} xl={15}>
          <Card
            bordered={false}
            title={(
              <Space wrap>
                <span>细分板块5日权价比</span>
                {selectedBoard ? (
                  <Tag color="blue" closable onClose={clearBoardSelection}>
                    已联动：{selectedBoard.name}（{selectedBoard.stockSymbols.length}只）
                  </Tag>
                ) : null}
              </Space>
            )}
            extra={selectedBoard ? <Button size="small" onClick={clearBoardSelection}>查看全部持仓</Button> : null}
          >
            <Table
              rowKey="ths_code"
              size="small"
              columns={boardColumns}
              dataSource={boardItems}
              loading={boardHoldingsLoading}
              pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100] }}
              scroll={{ x: 900 }}
              rowClassName={record => (record.ths_code === selectedBoard?.thsCode ? 'eastmoney-holdings-row-selected' : '')}
              onRow={record => ({ onClick: () => selectBoard(record) })}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={9}>
          <Card
            bordered={false}
            title={<Space><LineChartOutlined />板块历史权重与价格</Space>}
            loading={boardHistoryLoading}
          >
            {selectedBoard && boardHistoryRows.length ? (
              <>
                <Text strong>{selectedBoard.name}</Text>
                <ReactECharts option={boardChartOption} style={{ height: 390 }} notMerge lazyUpdate />
              </>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={selectedBoard ? '暂无板块历史数据' : '点击左侧板块查看历史曲线'}
                style={{ margin: '118px 0' }}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} xl={14}>
          <Card
            bordered={false}
            title={(
              <Space wrap>
                <StockOutlined />{snapshotDate ? '历史综合持仓' : '最新综合持仓'}
                {selectedBoard ? <Tag color="blue">{selectedBoard.name}</Tag> : null}
              </Space>
            )}
            extra={(
              <Space wrap className="eastmoney-holdings-toolbar">
                <Switch
                  checked={activeOnly}
                  checkedChildren="活跃"
                  unCheckedChildren="全部"
                  onChange={value => {
                    clearBoardSelection();
                    setActiveOnly(value);
                  }}
                />
                <DatePicker
                  allowClear
                  placeholder="选择持仓日期"
                  value={snapshotDate ? dayjs(snapshotDate) : null}
                  disabledDate={current => (
                    current && (
                      current.isAfter(dayjs(), 'day')
                      || current.isBefore(dayjs('2026-06-25'), 'day')
                    )
                  )}
                  onChange={value => {
                    clearBoardSelection();
                    setSnapshotDate(value ? value.format('YYYY-MM-DD') : '');
                  }}
                />
                <Input
                  allowClear
                  prefix={<SearchOutlined />}
                  placeholder="搜索股票/名称"
                  value={searchText}
                  onChange={event => setSearchText(event.target.value)}
                />
                <Button icon={<ReloadOutlined />} onClick={fetchLatest} loading={latestLoading} />
                <Button icon={<SettingOutlined />} onClick={openCredentialModal}>
                  凭据配置
                </Button>
              </Space>
            )}
          >
            <Table
              rowKey="stock_symbol"
              size="small"
              className="eastmoney-holdings-latest-table"
              loading={latestLoading}
              columns={latestColumns}
              dataSource={filteredItems}
              pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100, 200] }}
              scroll={{ x: 1400, y: 560 }}
              rowClassName={record => (record.stock_symbol === selectedSymbol ? 'eastmoney-holdings-row-selected' : '')}
              onRow={record => ({
                onClick: () => setSelectedSymbol(record.stock_symbol),
              })}
              locale={{ emptyText: latestData?.available === false ? '暂无东方财富持仓快照' : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={10}>
          <Card
            bordered={false}
            title={<Space><LineChartOutlined />权重和排名历史</Space>}
            loading={historyLoading}
          >
            {selectedSymbol ? (
              <>
                <div className="eastmoney-holdings-selected">
                  <div>
                    <Text type="secondary">当前标的</Text>
                    <h2>
                      <EastmoneyStockLink symbol={selectedSymbol}>
                        {selectedSymbol}
                      </EastmoneyStockLink>
                      {' '}
                      {selectedItem?.stock_name || historyData?.latest?.stock_name || ''}
                    </h2>
                  </div>
                  <Space size={6} wrap>
                    <Tag color="blue">#{latestRow?.composite_rank || '-'}</Tag>
                    <Tag color="geekblue">{percentFormatter(latestRow?.composite_weight_pct)}</Tag>
                  </Space>
                </div>
                {historyRows.length ? (
                  <ReactECharts option={chartOption} style={{ height: 340 }} notMerge lazyUpdate />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
                )}
                <Table
                  rowKey="snapshot_date"
                  size="small"
                  className="eastmoney-holdings-history-table"
                  columns={historyColumns}
                  dataSource={[...historyRows].reverse()}
                  pagination={{ defaultPageSize: 8, hideOnSinglePage: true }}
                  scroll={{ x: 640 }}
                  rowClassName={record => (
                    record.snapshot_date === selectedHistoryDate
                      ? 'eastmoney-holdings-history-row-selected'
                      : ''
                  )}
                  onRow={record => ({
                    onClick: () => setSelectedHistoryDate(record.snapshot_date),
                  })}
                />
                <div className="eastmoney-holdings-details">
                  <div className="eastmoney-holdings-details__header">
                    <div>
                      <Text type="secondary">组合详情</Text>
                      <h3>{detailSummaryText}</h3>
                    </div>
                    <Tag color="blue">{selectedSymbol}</Tag>
                  </div>
                  <Table
                    rowKey={record => `${record.snapshot_date}-${record.cube_symbol}`}
                    size="small"
                    className="eastmoney-holdings-detail-table"
                    loading={detailLoading}
                    columns={detailColumns}
                    dataSource={detailRows}
                    pagination={{ defaultPageSize: 8, showSizeChanger: true, pageSizeOptions: [8, 20, 50, 100] }}
                    scroll={{ x: 720, y: 320 }}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
                  />
                </div>
              </>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Card>
        </Col>
      </Row>

      <Modal
        title="东方财富接口凭据"
        open={credentialOpen}
        onCancel={() => setCredentialOpen(false)}
        footer={(
          <Space>
            <Button onClick={() => setCredentialOpen(false)}>取消</Button>
            <Button type="primary" loading={credentialSaving} onClick={saveCredentials}>保存</Button>
          </Space>
        )}
      >
        <Form form={credentialForm} layout="vertical">
          <Form.Item name="user_id" label="userId" rules={[{ required: true }]}>
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="ct_token" label="ctToken" rules={[{ required: true }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="ut_token" label="utToken" rules={[{ required: true }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

    </div>
  );
};

export default EastmoneyHoldingsResearch;
