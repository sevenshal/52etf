import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  InputNumber,
  Progress,
  Slider,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import request from '../utils/request';
import XueqiuStockLink from '../components/XueqiuStockLink';
import './NineTurnBreadthResearch.css';

const { Text, Title } = Typography;
const DEFAULT_PERCENTILE = 90;

const getErrorMessage = (error, fallback) => (
  error?.response?.data?.detail || error?.response?.data?.message || error?.message || fallback
);

const formatPercent = value => {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '-';
};

const formatNumber = (value, digits = 2) => {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : '-';
};

const getColumnCount = width => {
  if (width >= 1280) return 4;
  if (width >= 900) return 3;
  if (width >= 600) return 2;
  return 1;
};

const chunkRows = (items, count) => {
  const rows = [];
  for (let index = 0; index < items.length; index += count) {
    rows.push(items.slice(index, index + count));
  }
  return rows;
};

const signalLabel = signal => ({
  high: '高九阶段突破',
  low: '低九阶段突破',
  both: '高低九同时突破',
  neutral: '未触发',
}[signal] || '未触发');

const renderStage = record => {
  const tags = [];
  if (Number(record.high_stage) >= 2) {
    tags.push(<Tag color="red" key="high">高{record.high_stage}</Tag>);
  }
  if (Number(record.low_stage) >= 2) {
    tags.push(<Tag color="green" key="low">低{record.low_stage}</Tag>);
  }
  return tags.length ? <Space size={2} wrap>{tags}</Space> : <Text type="secondary">—</Text>;
};

const detailColumns = [
  {
    title: '成分股',
    dataIndex: 'name',
    key: 'name',
    render: (value, record) => (
      <Space direction="vertical" size={0}>
        <Text strong>{value || record.ts_code}</Text>
        <XueqiuStockLink symbol={record.ts_code} className="nine-turn-detail__code">{record.ts_code}</XueqiuStockLink>
      </Space>
    ),
  },
  {
    title: '九转阶段',
    key: 'stage',
    width: 140,
    sorter: (left, right) => Math.max(left.high_stage, left.low_stage) - Math.max(right.high_stage, right.low_stage),
    render: (_, record) => renderStage(record),
  },
  {
    title: '权重',
    dataIndex: 'weight',
    width: 100,
    align: 'right',
    render: value => (value === null || value === undefined ? '-' : `${formatNumber(value)}%`),
  },
  {
    title: '收盘',
    dataIndex: 'close',
    width: 100,
    align: 'right',
    render: value => formatNumber(value),
  },
  {
    title: '涨跌幅',
    dataIndex: 'pct_chg',
    width: 100,
    align: 'right',
    render: value => {
      const number = Number(value);
      if (!Number.isFinite(number)) return '-';
      return <Text className={number > 0 ? 'is-up' : (number < 0 ? 'is-down' : '')}>{number.toFixed(2)}%</Text>;
    },
  },
];

const NineTurnBreadthResearch = () => {
  const containerRef = useRef(null);
  const [columnCount, setColumnCount] = useState(4);
  const [percentile, setPercentile] = useState(DEFAULT_PERCENTILE);
  const [draftPercentile, setDraftPercentile] = useState(DEFAULT_PERCENTILE);
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [expandedCode, setExpandedCode] = useState(null);
  const [details, setDetails] = useState({});
  const [detailLoading, setDetailLoading] = useState(null);

  const loadOverview = useCallback(async ({ refresh = false } = {}) => {
    setLoading(true);
    try {
      const { data } = await request.get('/api/research/nine-turn-breadth/overview', {
        params: { percentile, refresh },
      });
      setOverview(data);
      setDetails({});
      setExpandedCode(null);
    } catch (error) {
      message.error(getErrorMessage(error, '加载九转宽度失败'));
    } finally {
      setLoading(false);
    }
  }, [percentile]);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return undefined;
    const update = width => setColumnCount(getColumnCount(width));
    update(node.getBoundingClientRect().width);
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(entries => {
      if (entries[0]) update(entries[0].contentRect.width);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const loadDetail = useCallback(async code => {
    const cacheKey = `${code}:${percentile}`;
    if (details[cacheKey]) return;
    setDetailLoading(code);
    try {
      const { data } = await request.get(`/api/research/nine-turn-breadth/boards/${encodeURIComponent(code)}/detail`, {
        params: { percentile },
      });
      setDetails(current => ({ ...current, [cacheKey]: data }));
    } catch (error) {
      message.error(getErrorMessage(error, '加载板块成分详情失败'));
      setExpandedCode(null);
    } finally {
      setDetailLoading(null);
    }
  }, [details, percentile]);

  const handleCardClick = useCallback(code => {
    if (expandedCode === code) {
      setExpandedCode(null);
      return;
    }
    setExpandedCode(code);
    void loadDetail(code);
  }, [expandedCode, loadDetail]);

  const boards = useMemo(() => overview?.boards || [], [overview]);
  const rows = useMemo(() => chunkRows(boards, columnCount), [boards, columnCount]);
  const triggeredCount = boards.filter(item => item.signal !== 'neutral').length;

  const renderCard = board => {
    const selected = expandedCode === board.index_code;
    return (
      <Card
        key={board.index_code}
        className={`nine-turn-card signal-${board.signal}${selected ? ' is-selected' : ''}`}
        hoverable
        role="button"
        tabIndex={0}
        onClick={() => handleCardClick(board.index_code)}
        onKeyDown={event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            handleCardClick(board.index_code);
          }
        }}
      >
        <div className="nine-turn-card__head">
          <div>
            <Text strong className="nine-turn-card__name">{board.name}</Text>
            <Text type="secondary" className="nine-turn-card__code">{board.index_code}</Text>
          </div>
          <Tag>{board.category}</Tag>
        </div>
        <div className="nine-turn-card__signal">
          <span>{signalLabel(board.signal)}</span>
          <strong>{board.eligible_members}只</strong>
        </div>
        <div className="nine-turn-card__metric high">
          <div><span>高九7—9</span><strong>{formatPercent(board.high_share)}</strong></div>
          <Progress percent={Math.min(100, Number(board.high_share || 0) * 100)} showInfo={false} strokeColor="#cf1322" trailColor="rgba(0,0,0,.08)" />
          <small>阈值 {formatPercent(board.high_threshold)} · {board.high_count}只</small>
        </div>
        <div className="nine-turn-card__metric low">
          <div><span>低九7—9</span><strong>{formatPercent(board.low_share)}</strong></div>
          <Progress percent={Math.min(100, Number(board.low_share || 0) * 100)} showInfo={false} strokeColor="#389e0d" trailColor="rgba(0,0,0,.08)" />
          <small>阈值 {formatPercent(board.low_threshold)} · {board.low_count}只</small>
        </div>
        <div className="nine-turn-card__footer">点击查看成分股九转阶段</div>
      </Card>
    );
  };

  const renderDetail = code => {
    if (!code) return null;
    const detail = details[`${code}:${percentile}`];
    return (
      <div className="nine-turn-detail">
        <Spin spinning={detailLoading === code}>
          {detail ? (
            <>
              <div className="nine-turn-detail__header">
                <div>
                  <Title level={4}>{detail.name}成分详情</Title>
                  <Text type="secondary">{detail.as_of_date} · 高/低连续计数达到2才显示标签</Text>
                </div>
                <Space wrap>
                  <Tag color="red">高九7—9 {detail.high_count}只</Tag>
                  <Tag color="green">低九7—9 {detail.low_count}只</Tag>
                </Space>
              </div>
              <Table
                rowKey="ts_code"
                columns={detailColumns}
                dataSource={detail.members || []}
                size="small"
                pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: [20, 50, 100] }}
                scroll={{ x: 720 }}
              />
            </>
          ) : <div className="nine-turn-detail__loading" />}
        </Spin>
      </div>
    );
  };

  return (
    <div className="nine-turn-research" ref={containerRef}>
      <div className="nine-turn-toolbar">
        <div>
          <Title level={3}>神奇九转宽度</Title>
          <Text type="secondary">
            成分股收盘连续高于/低于4日前收盘；统计第7—9阶段占比，并与自身过去一年比较。
          </Text>
        </div>
        <Space wrap className="nine-turn-toolbar__controls">
          <Text>一年分位阈值</Text>
          <Slider
            min={50}
            max={99}
            value={draftPercentile}
            onChange={setDraftPercentile}
            onChangeComplete={value => setPercentile(value)}
            className="nine-turn-toolbar__slider"
          />
          <InputNumber
            min={50}
            max={99}
            precision={0}
            value={draftPercentile}
            formatter={value => `${value}%`}
            parser={value => Number(String(value || '').replace('%', ''))}
            onChange={value => setDraftPercentile(Number(value || DEFAULT_PERCENTILE))}
            onPressEnter={() => setPercentile(Math.max(50, Math.min(99, draftPercentile)))}
          />
          <Button onClick={() => setPercentile(Math.max(50, Math.min(99, draftPercentile)))}>应用</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => loadOverview({ refresh: true })}>刷新</Button>
        </Space>
      </div>

      {overview && (
        <Alert
          type={triggeredCount ? 'info' : 'success'}
          showIcon
          message={`${overview.as_of_date} · ${boards.length}个板块 · ${triggeredCount}个触发 · 当前阈值P${overview.percentile}`}
          description="高九阶段突破标红，低九阶段突破标绿；颜色是研究提醒，不代表自动交易指令。"
          className="nine-turn-summary"
        />
      )}

      <Spin spinning={loading && !overview}>
        {rows.length ? rows.map((row, rowIndex) => {
          const expandedInRow = row.find(item => item.index_code === expandedCode);
          return (
            <div className="nine-turn-board-row" key={`row-${rowIndex}`}>
              <div className="nine-turn-card-grid" style={{ '--nine-turn-columns': columnCount }}>
                {row.map(renderCard)}
              </div>
              {expandedInRow ? renderDetail(expandedInRow.index_code) : null}
            </div>
          );
        }) : (!loading && <Empty description="暂无可计算的指数成分数据" />)}
      </Spin>
    </div>
  );
};

export default NineTurnBreadthResearch;
