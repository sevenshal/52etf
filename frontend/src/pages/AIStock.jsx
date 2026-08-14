import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Col,
  Descriptions,
  Divider,
  Empty,
  Input,
  Modal,
  Row,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { PlayCircleOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';
import useRealtimeQuotes from '../hooks/useRealtimeQuotes';
import './AIStock.css';

const { Text, Title } = Typography;

const errorText = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) return fallback;
  return typeof detail === 'string' ? detail : JSON.stringify(detail);
};

const money = value => (value === null || value === undefined ? '-' : `¥${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`);
const percent = value => (value === null || value === undefined ? '-' : `${Number(value).toFixed(2)}%`);
const dateTime = value => (value ? String(value).replace('T', ' ').slice(0, 16) : '-');
const valueClass = value => (Number(value || 0) > 0 ? 'is-up' : Number(value || 0) < 0 ? 'is-down' : '');

const readableModelContent = content => {
  if (content === null || content === undefined) return '-';
  if (typeof content !== 'string') return JSON.stringify(content, null, 2);
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch (_) {
    return content;
  }
};

const messageLabel = role => ({ system: '系统规则', user: '发送给 DeepSeek', assistant: 'DeepSeek 回复' }[role] || role);
const stageLabel = (stage, index) => ({
  NEWS_EVENTS: '第 1 轮：全部新闻标题 → 新闻事件/热词',
  EVENTS_TO_THS_BOARDS: '第 2 轮：新闻事件 → THS 全量板块目录',
  THS_BOARDS_TO_STOCK_SELECTION: '第 3 轮：THS 板块/成分股 → 推荐股票',
}[stage] || `第 ${index + 1} 轮：AI 会话`);

const ConversationViewer = ({ runId }) => {
  const [raw, setRaw] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    setLoading(true);
    request.get(`/api/ai-stock/recommendations/runs/${runId}/transcript`).then(response => {
      if (!cancelled) setRaw(response.data?.ai_raw_response || null);
    }).catch(() => {
      if (!cancelled) setRaw(null);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [runId]);
  if (loading) return <Spin size="small" />;
  if (!raw) return <Text type="secondary">该批次尚未保存 AI 会话记录。</Text>;
  const stages = raw?.stages || [];
  if (!stages.length) {
    return <pre className="ai-stock-transcript">{JSON.stringify(raw, null, 2)}</pre>;
  }
  return (
    <div className="ai-stock-chat-log">
      <Text type="secondary">按真实调用顺序展示。第 2、3 轮只展示新增消息；前序新闻和模型回复已随实际 messages 继承。</Text>
      {stages.map((stage, index) => {
        const requestMessages = stage?.request?.messages || [];
        // The second request replays prior messages for DeepSeek's stateless API.
        // In this human-facing view we keep only its newly added user message.
        const visibleMessages = index === 0
          ? requestMessages.filter(item => item.role !== 'assistant')
          : requestMessages.slice().reverse().filter(item => item.role === 'user').slice(0, 1).reverse();
        const label = stageLabel(stage.stage, index);
        return (
          <div className="ai-stock-chat-stage" key={`${stage.stage || 'stage'}-${index}`}>
            <div className="ai-stock-chat-stage-title">{label}</div>
            {visibleMessages.map((item, messageIndex) => (
              <div className={`ai-stock-chat-bubble ai-stock-chat-${item.role}`} key={`${item.role}-${messageIndex}`}>
                <div className="ai-stock-chat-bubble-label">{messageLabel(item.role)}</div>
                <pre>{readableModelContent(item.content)}</pre>
              </div>
            ))}
            <div className="ai-stock-chat-bubble ai-stock-chat-assistant">
              <div className="ai-stock-chat-bubble-label">DeepSeek 回复</div>
              <pre>{readableModelContent(stage.response_content)}</pre>
            </div>
            <Collapse
              size="small"
              className="ai-stock-chat-audit"
              items={[{
                key: 'raw',
                label: '核验本轮原始 API 记录（模型、参数、完整 messages、原始回复）',
                children: <pre className="ai-stock-transcript">{JSON.stringify(stage, null, 2)}</pre>,
              }]}
            />
          </div>
        );
      })}
    </div>
  );
};

const EvidenceChain = ({ runId }) => {
  const [evidence, setEvidence] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    setLoading(true);
    request.get(`/api/ai-stock/recommendations/runs/${runId}/evidence`).then(response => {
      if (!cancelled) setEvidence(response.data || null);
    }).catch(() => {
      if (!cancelled) setEvidence(null);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [runId]);
  if (loading) return <Spin size="small" />;
  if (!evidence) return <Text type="secondary">该批次暂无证据链数据。</Text>;
  const market = evidence.market_snapshot || {};
  const events = market.events || [];
  const mappings = market.board_mappings || [];
  const boards = new Map((market.boards || []).map(item => [item.ths_code, item]));
  const headlines = new Map((evidence.news_snapshot || []).map(item => [item.headline_id, item]));
  if (!events.length) return <Text type="secondary">这是 V2 历史批次，未保存 THS 证据链。</Text>;
  const rows = events.map(event => {
    const related = mappings.filter(item => item.event_id === event.event_id);
    return {
      ...event,
      headlineTitles: (event.headline_ids || []).map(id => `${id} · ${headlines.get(id)?.title || '-'}`),
      boards: related.map(mapping => ({ ...mapping, board: boards.get(mapping.ths_code) })),
    };
  });
  const columns = [
    { title: '新闻事件', key: 'event', width: 190, render: (_, row) => <Space direction="vertical" size={0}><Text strong>{row.hotword}</Text><Text type="secondary">{row.event_id} · {row.direction || '中性'}</Text></Space> },
    { title: '标题证据', dataIndex: 'headlineTitles', width: 410, render: values => <Space direction="vertical" size={2}>{values.map(value => <Text key={value}>{value}</Text>)}</Space> },
    { title: 'THS 板块映射', dataIndex: 'boards', width: 300, render: values => <Space direction="vertical" size={3}>{values.map(item => <span key={item.ths_code}><Tag color="blue">{item.board?.type || 'THS'}</Tag><Text strong>{item.board?.name || item.ths_code}</Text><Text type="secondary"> · {item.ths_code}</Text></span>)}</Space> },
    { title: '强弱证据', dataIndex: 'boards', width: 220, render: values => <Space direction="vertical" size={3}>{values.map(item => { const daily = item.board?.strength?.ths_daily || {}; const flow = item.board?.strength?.moneyflow_cnt_ths || {}; return <Text key={item.ths_code}>涨跌 {percent(daily.pct_change)} · 净额 {flow.net_amount ?? '-'} 亿</Text>; })}</Space> },
  ];
  return <Table className="ai-stock-table" columns={columns} dataSource={rows} rowKey="event_id" size="small" pagination={false} scroll={{ x: 1120 }} />;
};

const livePriceCell = (quotes, flashes, tsCode, fallback) => {
  const quote = quotes?.[tsCode];
  if (quote && quote.last_px) {
    const flashKey = flashes?.[tsCode] || 0;
    return (
      <span key={flashKey} className={flashKey > 0 ? 'ai-stock-price-flash' : undefined}>
        {money(quote.last_px)}
      </span>
    );
  }
  return fallback !== undefined ? money(fallback) : <Text type="secondary">-</Text>;
};

// 现价相对建议买入价的偏离度 = (实时价 - 建议买入价) / 建议买入价
const recommendationDistancePct = (item, quote) => {
  const live = quote?.last_px;
  if (!live || !item?.recommendation_price || !Number(item.recommendation_price)) return null;
  return ((Number(live) - Number(item.recommendation_price)) / Number(item.recommendation_price)) * 100;
};

// 目标价相对实时价的距离
const targetDistancePct = (item, quote) => {
  const live = quote?.last_px;
  if (!live || !item?.target_price) return null;
  return ((Number(item.target_price) - Number(live)) / Number(live)) * 100;
};

const recommendationColumns = (quotes, flashes) => [
  { title: '#', dataIndex: 'rank', width: 48, align: 'center' },
  {
    title: '股票',
    key: 'stock',
    width: 152,
    render: (_, item) => (
      <Space direction="vertical" size={0}>
        <Text strong>{item.name}</Text>
        <Text type="secondary">{item.ts_code}</Text>
        {item.industry ? <Text type="secondary" style={{ fontSize: 12 }}>{item.industry}</Text> : null}
      </Space>
    ),
  },
  {
    title: '实时价',
    key: 'live_price',
    width: 108,
    align: 'right',
    render: (_, item) => {
      const quote = quotes?.[item.ts_code];
      const live = quote?.last_px;
      if (!live) return <Text type="secondary">-</Text>;
      const flashKey = flashes?.[item.ts_code] || 0;
      const chg = recommendationDistancePct(item, quote);
      return (
        <Space direction="vertical" size={0} align="end">
          <span key={flashKey} className={flashKey > 0 ? 'ai-stock-price-flash' : undefined}>{money(live)}</span>
          {chg !== null && <Text className={valueClass(chg)} style={{ fontSize: 12 }}>距买入 {percent(chg)}</Text>}
        </Space>
      );
    },
  },
  { title: '建议买入价', dataIndex: 'recommendation_price', width: 96, align: 'right', render: value => money(value) },
  {
    title: '今日开盘价',
    key: 'open_price',
    width: 96,
    align: 'right',
    render: (_, item) => {
      const quote = quotes?.[item.ts_code];
      return quote?.open_px ? money(quote.open_px) : <Text type="secondary">-</Text>;
    },
  },
  {
    title: '目标盈利点',
    key: 'target',
    width: 132,
    align: 'right',
    render: (_, item) => {
      const quote = quotes?.[item.ts_code];
      const dist = targetDistancePct(item, quote);
      const flashKey = flashes?.[item.ts_code] || 0;
      return (
        <Space direction="vertical" size={0} align="end">
          <Text>{money(item.target_price)}</Text>
          <Text className="is-up" style={{ fontSize: 12 }}>目标 {percent(item.target_return_pct)}</Text>
          {dist !== null && (
            <Text
              key={flashKey}
              className={flashKey > 0 ? `${valueClass(dist)} ai-stock-price-flash` : valueClass(dist)}
              style={{ fontSize: 12 }}
            >
              距现价 {percent(dist)}
            </Text>
          )}
        </Space>
      );
    },
  },
  { title: 'AI 信心', dataIndex: 'ai_confidence', width: 92, render: value => `${Number(value).toFixed(0)} / 100` },
  {
    title: '错价机会',
    dataIndex: 'news_signal',
    width: 100,
    render: value => {
      if (value === undefined || value === null) return '-';
      const num = Number(value);
      const label = num >= 65 ? '反应不足' : num <= 35 ? '反应过度' : '中性';
      const color = num >= 65 ? 'red' : num <= 35 ? 'green' : 'default';
      return <Tag color={color}>{label} {num.toFixed(0)}</Tag>;
    },
  },
  {
    title: '题材',
    dataIndex: 'themes',
    width: 160,
    render: themes => <Space size={[4, 4]} wrap>{(themes || []).slice(0, 3).map(theme => <Tag key={theme}>{theme}</Tag>)}</Space>,
  },
  { title: 'AI 结论', dataIndex: 'reason', width: 360, ellipsis: true },
  { title: '风险', dataIndex: 'risks', width: 220, ellipsis: true },
];

const clockTime = value => {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  } catch {
    return String(value).slice(11, 16);
  }
};

const todayColumns = (quotes, flashes) => [
  { title: '时间', dataIndex: 'run_at', width: 76, render: clockTime },
  {
    title: '股票',
    key: 'stock',
    width: 128,
    render: (_, item) => (
      <Space direction="vertical" size={0}>
        <Text strong>{item.name}</Text>
        <Text type="secondary">{item.ts_code}</Text>
      </Space>
    ),
  },
  { title: '次数', dataIndex: 'recommendation_count', width: 58, align: 'center', render: value => (value > 1 ? <Tag color="orange">{value}</Tag> : value) },
  { title: 'AI 信心', dataIndex: 'ai_confidence', width: 84, render: value => `${Number(value).toFixed(0)} / 100` },
  { title: '建议价', dataIndex: 'recommendation_price', width: 84, align: 'right', render: value => money(value) },
  { title: '现价', key: 'live_price', width: 84, align: 'right', render: (_, item) => livePriceCell(quotes, flashes, item.ts_code) },
  { title: '目标价', dataIndex: 'target_price', width: 84, align: 'right', render: value => money(value) },
  { title: '建议收益', dataIndex: 'target_return_pct', width: 84, render: value => <Text className="is-up">{percent(value)}</Text> },
  {
    title: '题材',
    dataIndex: 'themes',
    width: 150,
    render: themes => <Space size={[4, 4]} wrap>{(themes || []).slice(0, 3).map(theme => <Tag key={theme}>{theme}</Tag>)}</Space>,
  },
  { title: 'AI 结论', dataIndex: 'reason', width: 260, ellipsis: true },
];

const AIStock = () => {
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [current, setCurrent] = useState({ run: null, recommendations: [] });
  const [todayRecs, setTodayRecs] = useState([]);
  const [history, setHistory] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [overview, setOverview] = useState(null);
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [holdEvals, setHoldEvals] = useState([]);
  const [paperStats, setPaperStats] = useState(null);
  const [curve, setCurve] = useState([]);
  const [benchmark, setBenchmark] = useState(null);
  const [evaluation, setEvaluation] = useState(null);
  const [settings, setSettings] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [deepseekApiKey, setDeepseekApiKey] = useState('');
  const [deepseekModel, setDeepseekModel] = useState('deepseek-chat');
  const [maxCandidates, setMaxCandidates] = useState(1500);
  const [maxEvents, setMaxEvents] = useState(8);
  const [maxBoards, setMaxBoards] = useState(8);
  const [maxCandidatesPerBoard, setMaxCandidatesPerBoard] = useState(200);
  const [minMarketCap, setMinMarketCap] = useState(1000000);
  const [minAvgTurnover, setMinAvgTurnover] = useState(20000);
  const [maxRecommendations, setMaxRecommendations] = useState(10);
  const [minListingDays, setMinListingDays] = useState(183);
  const [targetReturnPctMin, setTargetReturnPctMin] = useState(5);
  const [targetReturnPctMax, setTargetReturnPctMax] = useState(10);
  const [newsSignalWeight, setNewsSignalWeight] = useState(0.5);
  const [paperConfig, setPaperConfig] = useState(null);
  const [paperConfigOpen, setPaperConfigOpen] = useState(false);
  const [savingPaperConfig, setSavingPaperConfig] = useState(false);
  const [paperEnabled, setPaperEnabled] = useState(true);
  const [paperMaxPositions, setPaperMaxPositions] = useState(10);
  const [paperSlotCount, setPaperSlotCount] = useState(5);
  const [paperSingleStockCap, setPaperSingleStockCap] = useState(0.2);
  const [paperMaxExecutionTarget, setPaperMaxExecutionTarget] = useState(0.9);
  const [paperEntryPriceCapPct, setPaperEntryPriceCapPct] = useState(1.0);
  const [paperStopLossHalfPct, setPaperStopLossHalfPct] = useState(-8.0);
  const [paperStopLossFullPct, setPaperStopLossFullPct] = useState(-12.0);
  const [paperTradingStartMinute, setPaperTradingStartMinute] = useState(585);
  const [paperHoldEvalEnabled, setPaperHoldEvalEnabled] = useState(false);
  const [paperHoldSellThreshold, setPaperHoldSellThreshold] = useState(30);
  const [paperMaxBuysPerDay, setPaperMaxBuysPerDay] = useState(3);
  const [paperTrailingTakeProfitPct, setPaperTrailingTakeProfitPct] = useState(5.0);
  const [paperRotationConfidenceGap, setPaperRotationConfidenceGap] = useState(20.0);
  const [todayPage, setTodayPage] = useState(1);

  // 实时行情：订阅 tick 推送 + 通过长连接注册当前展示代码（断线自动清理、重连自动重注册）
  const { quotes, flashes, register } = useRealtimeQuotes('ai_stock_page');

  // 注册当前渲染的代码：AI推荐 + 今日当前页 + 历史批次详情 + 模拟仓持仓
  useEffect(() => {
    const visibleToday = todayRecs.slice((todayPage - 1) * 50, todayPage * 50);
    const codes = [
      ...(current.recommendations || []).map(item => item.ts_code),
      ...visibleToday.map(item => item.ts_code),
      ...(positions || []).map(item => item.ts_code),
      ...((selectedRun?.recommendations) || []).map(item => item.ts_code),
    ];
    register(codes);
  }, [current, todayRecs, positions, selectedRun, todayPage, register]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    const calls = [
      request.get('/api/ai-stock/recommendations/current'),
      request.get('/api/ai-stock/recommendations/today'),
      request.get('/api/ai-stock/recommendations/history'),
      request.get('/api/ai-stock/paper/overview'),
      request.get('/api/ai-stock/paper/positions'),
      request.get('/api/ai-stock/paper/trades'),
      request.get('/api/ai-stock/paper/equity-curve'),
      request.get('/api/ai-stock/benchmark/status'),
      request.get('/api/ai-stock/evaluation/latest'),
      request.get('/api/ai-stock/settings'),
      request.get('/api/ai-stock/paper/config'),
      request.get('/api/ai-stock/paper/hold-evaluations'),
      request.get('/api/ai-stock/paper/statistics'),
    ];
    const results = await Promise.allSettled(calls);
    const data = index => (results[index].status === 'fulfilled' ? results[index].value.data : null);
    const failed = results.find(result => result.status === 'rejected');
    if (failed) message.warning(errorText(failed.reason, '部分 AI 荐股数据暂时不可用'));
    setCurrent(data(0) || { run: null, recommendations: [] });
    setTodayRecs(data(1) || []);
    setHistory(data(2) || []);
    setOverview(data(3));
    setPositions(data(4) || []);
    setTrades(data(5)?.items || []);
    setCurve(data(6) || []);
    setBenchmark(data(7));
    setEvaluation(data(8));
    setSettings(data(9));
    setPaperConfig(data(10));
    setHoldEvals(data(11) || []);
    setPaperStats(data(12) || null);
    setLoading(false);
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

  const loadRun = useCallback(async runId => {
    try {
      const [runResponse, performanceResponse] = await Promise.all([
        request.get(`/api/ai-stock/recommendations/runs/${runId}`),
        request.get(`/api/ai-stock/recommendations/runs/${runId}/performance`),
      ]);
      const performanceByRecommendation = new Map((performanceResponse.data.items || []).map(item => [item.recommendation_id, item]));
      setSelectedRun({
        ...runResponse.data,
        recommendations: (runResponse.data.recommendations || []).map(item => ({ ...item, ...(performanceByRecommendation.get(item.id) || {}) })),
      });
    } catch (error) {
      message.error(errorText(error, '加载历史批次失败'));
    }
  }, []);

  const runNow = useCallback(async () => {
    setRunning(true);
    try {
      await request.post('/api/ai-stock/recommendations/run');
      message.info('AI 荐股已触发，生成中（完成/失败将通过推送通知）...');
    } catch (error) {
      message.error(errorText(error, '触发 AI 推荐失败'));
      setRunning(false);
    }
    // setRunning(false) happens when the ai_stock_run_updated event arrives.
  }, []);

  useEffect(() => {
    // Live updates for both manual triggers and the scheduler's auto runs.
    const unsubscribe = subscribeBackendEvent('ai_stock_run_updated', event => {
      if (event.status === 'SUCCESS') {
        message.success(`AI 推荐批次 #${event.run_id} 生成完成`);
        setRunning(false);
        loadAll();
      } else if (event.status === 'FAILED') {
        message.error(`AI 推荐失败：${event.message || '未知错误'}`);
        setRunning(false);
      }
    });
    return unsubscribe;
  }, [loadAll]);

  const openSettings = useCallback(() => {
    setDeepseekApiKey('');
    setDeepseekModel(settings?.deepseek_model || 'deepseek-chat');
    setMaxCandidates(settings?.max_candidates ?? 1500);
    setMaxEvents(settings?.max_events ?? 8);
    setMaxBoards(settings?.max_boards ?? 8);
    setMaxCandidatesPerBoard(settings?.max_candidates_per_board ?? 200);
    setMinMarketCap(settings?.min_market_cap ?? 1000000);
    setMinAvgTurnover(settings?.min_avg_turnover ?? 20000);
    setMaxRecommendations(settings?.max_recommendations ?? 10);
    setMinListingDays(settings?.min_listing_days ?? 183);
    setTargetReturnPctMin(settings?.target_return_pct_min ?? 5);
    setTargetReturnPctMax(settings?.target_return_pct_max ?? 10);
    setNewsSignalWeight(settings?.news_signal_weight ?? 0.5);
    setSettingsOpen(true);
  }, [settings]);

  const saveSettings = useCallback(async () => {
    setSavingSettings(true);
    try {
      const payload = { deepseek_model: deepseekModel.trim() || 'deepseek-chat' };
      if (deepseekApiKey.trim()) payload.deepseek_api_key = deepseekApiKey.trim();
      payload.max_candidates = maxCandidates;
      payload.max_events = maxEvents;
      payload.max_boards = maxBoards;
      payload.max_candidates_per_board = maxCandidatesPerBoard;
      payload.min_market_cap = minMarketCap;
      payload.min_avg_turnover = minAvgTurnover;
      payload.max_recommendations = maxRecommendations;
      payload.min_listing_days = minListingDays;
      payload.target_return_pct_min = targetReturnPctMin;
      payload.target_return_pct_max = targetReturnPctMax;
      payload.news_signal_weight = newsSignalWeight;
      const response = await request.put('/api/ai-stock/settings', payload);
      setSettings(response.data);
      setDeepseekApiKey('');
      setSettingsOpen(false);
      message.success('AI 荐股配置已保存');
    } catch (error) {
      message.error(errorText(error, '保存配置失败'));
    } finally {
      setSavingSettings(false);
    }
  }, [deepseekApiKey, deepseekModel, maxCandidates, maxEvents, maxBoards, maxCandidatesPerBoard, minMarketCap, minAvgTurnover, maxRecommendations, minListingDays, targetReturnPctMin, targetReturnPctMax, newsSignalWeight]);

  const openPaperConfig = useCallback(() => {
    const p = paperConfig?.parameters || {};
    setPaperEnabled(paperConfig?.enabled ?? true);
    setPaperMaxPositions(p.max_positions ?? 10);
    setPaperSlotCount(p.slot_count ?? 5);
    setPaperSingleStockCap(p.single_stock_cap ?? 0.2);
    setPaperMaxExecutionTarget(p.max_execution_target ?? 0.9);
    setPaperEntryPriceCapPct(p.entry_price_cap_pct ?? 1.0);
    setPaperStopLossHalfPct(p.stop_loss_half_pct ?? -8.0);
    setPaperStopLossFullPct(p.stop_loss_full_pct ?? -12.0);
    setPaperTradingStartMinute(p.trading_start_minute ?? 585);
    setPaperHoldEvalEnabled(p.hold_evaluation_enabled ?? false);
    setPaperHoldSellThreshold(p.hold_sell_threshold ?? 30);
    setPaperMaxBuysPerDay(p.max_buys_per_day ?? 3);
    setPaperTrailingTakeProfitPct(p.trailing_take_profit_pct ?? 5.0);
    setPaperRotationConfidenceGap(p.rotation_confidence_gap ?? 20.0);
    setPaperConfigOpen(true);
  }, [paperConfig]);

  const savePaperConfig = useCallback(async () => {
    setSavingPaperConfig(true);
    try {
      const payload = {
        enabled: paperEnabled,
        max_positions: paperMaxPositions,
        slot_count: paperSlotCount,
        single_stock_cap: paperSingleStockCap,
        max_execution_target: paperMaxExecutionTarget,
        entry_price_cap_pct: paperEntryPriceCapPct,
        stop_loss_half_pct: paperStopLossHalfPct,
        stop_loss_full_pct: paperStopLossFullPct,
        trading_start_minute: paperTradingStartMinute,
        hold_evaluation_enabled: paperHoldEvalEnabled,
        hold_sell_threshold: paperHoldSellThreshold,
        max_buys_per_day: paperMaxBuysPerDay,
        trailing_take_profit_pct: paperTrailingTakeProfitPct,
        rotation_confidence_gap: paperRotationConfidenceGap,
      };
      const response = await request.put('/api/ai-stock/paper/config', payload);
      setPaperConfig(response.data);
      setPaperConfigOpen(false);
      message.success('模拟盘策略参数已保存');
    } catch (error) {
      message.error(errorText(error, '保存模拟盘参数失败'));
    } finally {
      setSavingPaperConfig(false);
    }
  }, [paperEnabled, paperMaxPositions, paperSlotCount, paperSingleStockCap, paperMaxExecutionTarget, paperEntryPriceCapPct, paperStopLossHalfPct, paperStopLossFullPct, paperTradingStartMinute, paperHoldEvalEnabled, paperHoldSellThreshold, paperMaxBuysPerDay, paperTrailingTakeProfitPct, paperRotationConfidenceGap]);

  const curveOption = useMemo(() => {
    const rows = curve.slice(-600);
    if (!rows.length) return null;
    return {
      grid: { left: 58, right: 22, top: 28, bottom: 42 },
      tooltip: { trigger: 'axis', valueFormatter: value => money(value) },
      xAxis: { type: 'category', boundaryGap: false, data: rows.map(item => dateTime(item.recorded_at)) },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: value => `${(value / 10000).toFixed(0)}万` } },
      series: [{ name: '净值', type: 'line', smooth: true, showSymbol: false, data: rows.map(item => item.total_equity), lineStyle: { color: '#1677ff', width: 2 }, areaStyle: { color: 'rgba(22,119,255,.10)' } }],
    };
  }, [curve]);

  const historyColumns = [
    { title: '批次时间', dataIndex: 'run_at', width: 160, render: dateTime },
    { title: '类型', dataIndex: 'run_type', width: 100, render: value => <Tag color={value === 'PREOPEN' ? 'purple' : value === 'OPENING' ? 'blue' : 'cyan'}>{value}</Tag> },
    { title: '候选池', dataIndex: 'candidate_count', width: 92, render: value => `${value} 只` },
    { title: '有效推荐', dataIndex: 'recommendation_count', width: 104, render: value => `${value} 只` },
    { title: '模型', dataIndex: 'model_name', width: 150 },
    { title: '查看', key: 'action', width: 84, render: (_, row) => <Button size="small" onClick={() => loadRun(row.id)}>详情</Button> },
  ];

  const latestHoldEval = useMemo(() => {
    // 接口按 evaluated_at 倒序返回，每只股票取第一条即最新评估
    const map = {};
    for (const ev of holdEvals) {
      if (!map[ev.ts_code]) map[ev.ts_code] = ev;
    }
    return map;
  }, [holdEvals]);

  const positionColumns = [
    { title: '股票', key: 'stock', width: 150, render: (_, row) => <Space direction="vertical" size={0}><Text strong>{row.name}</Text><Text type="secondary">{row.ts_code}</Text></Space> },
    { title: '持仓', dataIndex: 'quantity', width: 90, align: 'right' },
    { title: '可卖', dataIndex: 'sellable_quantity', width: 90, align: 'right' },
    { title: '成本', dataIndex: 'cost', width: 90, align: 'right', render: money },
    { title: '现价', dataIndex: 'price', width: 90, align: 'right', render: (value, row) => livePriceCell(quotes, flashes, row.ts_code, value) },
    { title: '盈亏', dataIndex: 'pnl_pct', width: 90, align: 'right', render: (value, row) => {
      const quote = quotes?.[row.ts_code];
      const live = quote?.last_px ? Number(quote.last_px) : null;
      const pnl = live && row.cost ? ((live / row.cost - 1) * 100) : Number(value);
      return <Text className={valueClass(pnl)}>{percent(pnl)}</Text>;
    } },
    { title: '目标价', dataIndex: 'target_price', width: 90, align: 'right', render: money },
    { title: '持有天数', dataIndex: 'held_days', width: 95, align: 'right' },
    {
      title: 'AI 评分', key: 'hold_score', width: 95, align: 'right',
      render: (_, row) => {
        const ev = latestHoldEval[row.ts_code];
        if (!ev) return <Text type="secondary">-</Text>;
        return (
          <Tooltip title={`${dateTime(ev.evaluated_at)} ${ev.reason || ''}`}>
            <Text>{Number(ev.hold_score).toFixed(0)}</Text>
          </Tooltip>
        );
      },
    },
  ];

  const tradeColumns = [
    { title: '时间', dataIndex: 'executed_at', width: 150, render: dateTime },
    { title: '方向', dataIndex: 'side', width: 72, render: side => <Tag color={side === 'BUY' ? 'blue' : 'red'}>{side === 'BUY' ? '买入' : '卖出'}</Tag> },
    { title: '股票', key: 'stock', width: 140, render: (_, row) => `${row.name} ${row.ts_code}` },
    { title: '成交', key: 'fill', width: 140, align: 'right', render: (_, row) => `${row.quantity} × ${money(row.price)}` },
    { title: '费用', dataIndex: 'fee', width: 90, align: 'right', render: money },
    { title: '已实现盈亏', dataIndex: 'realized_pnl', width: 112, align: 'right', render: value => <Text className={valueClass(value)}>{money(value)}</Text> },
    { title: '触发依据', dataIndex: 'reason', width: 320, ellipsis: true },
  ];

  const performanceColumns = [
    { title: '当日（成本后）', dataIndex: 'same_day_return_pct', width: 118, render: value => <Text className={valueClass(value)}>{percent(value)}</Text> },
    { title: '次日（成本后）', dataIndex: 'next_day_return_pct', width: 118, render: value => <Text className={valueClass(value)}>{percent(value)}</Text> },
    { title: '5日（成本后）', dataIndex: 'five_day_return_pct', width: 118, render: value => <Text className={valueClass(value)}>{percent(value)}</Text> },
    { title: '5日最高', dataIndex: 'five_day_high_return_pct', width: 105, render: value => <Text className={valueClass(value)}>{percent(value)}</Text> },
  ];

  const recommendationContent = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="AI 仅从受控候选池选择股票；分钟策略决定是否实际进入模拟盘。"
        description={`${settings?.deepseek_configured ? `DeepSeek 已配置（${settings.deepseek_model}）` : '尚未配置 DeepSeek API Key；请点击“设置”。'} ${benchmark?.configured ? `目标站对标：最近采集 ${dateTime(benchmark.last_captured_at)}` : '目标站对标尚未配置运行环境凭据。'}`}
      />
      <Card className="ai-stock-card" title="当前 AI 推荐" extra={<Space><Button icon={<SettingOutlined />} onClick={openSettings}>设置</Button><Button icon={<ReloadOutlined />} onClick={loadAll}>刷新</Button><Button type="primary" icon={<PlayCircleOutlined />} loading={running} onClick={runNow}>立即生成</Button></Space>}>
        {current.run ? (
          <Descriptions size="small" column={{ xs: 1, sm: 2, md: 4 }} className="ai-stock-run-meta">
            <Descriptions.Item label="批次">{current.run.run_type}</Descriptions.Item>
            <Descriptions.Item label="时间">{dateTime(current.run.run_at)}</Descriptions.Item>
            <Descriptions.Item label="模型">{current.run.model_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="候选池">{current.run.candidate_count} 只</Descriptions.Item>
          </Descriptions>
        ) : <Empty description="尚无成功的 AI 推荐批次" />}
        <Table className="ai-stock-table" columns={recommendationColumns(quotes, flashes)} dataSource={current.recommendations} rowKey="id" size="small" pagination={false} scroll={{ x: 1560 }} />
        {current.run ? <Collapse className="ai-stock-conversation" items={[
          { key: 'current-evidence', label: '查看新闻 → THS 板块 → 成分股证据链', children: <EvidenceChain runId={current.run.id} /> },
          { key: 'current-conversation', label: '查看本批次完整 AI 会话', children: <ConversationViewer runId={current.run.id} /> },
        ]} /> : null}
      </Card>
    </Space>
  );

  const todayContent = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card className="ai-stock-card" title={`今日推荐（${todayRecs.length} 只）`} extra={<Text type="secondary">今日全部批次 · 重复股票已合并，按平均 AI 信心排序</Text>}>
        <Table className="ai-stock-table" columns={todayColumns(quotes, flashes)} dataSource={todayRecs} rowKey="ts_code" size="small" pagination={{ pageSize: 50, current: todayPage, onChange: setTodayPage, showSizeChanger: false }} scroll={{ x: 1100 }} />
      </Card>
    </Space>
  );

  const historyContent = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card className="ai-stock-card" title="历史推荐批次">
        <Table className="ai-stock-table" columns={historyColumns} dataSource={history} rowKey="id" size="small" pagination={{ pageSize: 12 }} scroll={{ x: 720 }} />
      </Card>
      {selectedRun ? (
        <Card className="ai-stock-card" title={`${selectedRun.run_type} · ${dateTime(selectedRun.run_at)}`} extra={<Text type="secondary">候选池 {selectedRun.candidate_count} 只</Text>}>
          <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} className="ai-stock-run-meta">
            <Descriptions.Item label="模型">{selectedRun.model_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="提示词版本">{selectedRun.prompt_version || '-'}</Descriptions.Item>
            <Descriptions.Item label="新闻快照">{selectedRun.news_count ?? 0} 条</Descriptions.Item>
          </Descriptions>
          <Table className="ai-stock-table" columns={[...recommendationColumns(quotes, flashes), ...performanceColumns]} dataSource={selectedRun.recommendations || []} rowKey="id" size="small" pagination={false} scroll={{ x: 2000 }} />
          <Collapse className="ai-stock-conversation" items={[
            { key: 'history-evidence', label: '查看新闻 → THS 板块 → 成分股证据链', children: <EvidenceChain runId={selectedRun.id} /> },
            { key: 'history-conversation', label: '查看本批次完整 AI 会话', children: <ConversationViewer runId={selectedRun.id} /> },
          ]} />
        </Card>
      ) : null}
    </Space>
  );

  const paperContent = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Row justify="space-between" align="middle" gutter={[12, 12]}>
        <Col flex="auto"><Alert type="warning" showIcon message="仅自动模拟交易，不连接真实券商。费用、T+1 和 FIFO 已按 A 股规则计入。" /></Col>
        <Col><Button icon={<SettingOutlined />} onClick={openPaperConfig}>{paperConfig?.enabled === false ? '模拟盘已停用 · 参数设置' : '模拟盘参数设置'}</Button></Col>
      </Row>
      <Row gutter={[12, 12]}>
        {[
          ['总权益', overview?.total_equity, money],
          ['累计盈亏', overview?.total_pnl, money],
          ['累计收益', overview?.total_return_pct, percent],
          ['可用资金', overview?.cash, money],
        ].map(([label, value, formatter]) => <Col xs={12} md={6} key={label}><Card className="ai-stock-stat"><Statistic title={label} value={value ?? 0} formatter={() => <span className={label.includes('盈亏') || label.includes('收益') ? valueClass(value) : ''}>{value === null || value === undefined ? '-' : formatter(value)}</span>} /></Card></Col>)}
      </Row>
      {paperStats ? (
        <Card className="ai-stock-card" title="胜率统计" size="small">
          <Row gutter={[12, 12]}>
            {[
              ['已平仓', paperStats.closed_trades, value => value],
              ['胜率', paperStats.win_rate_pct, percent],
              ['已实现盈亏', paperStats.total_realized_pnl, money],
              ['平均持有天数', paperStats.avg_holding_days, value => (value === null || value === undefined ? '-' : `${value} 天`)],
            ].map(([label, value, formatter]) => <Col xs={12} md={6} key={label}><Statistic title={label} value={value ?? 0} formatter={() => <span className={label === '已实现盈亏' ? valueClass(value) : ''}>{value === null || value === undefined ? '-' : formatter(value)}</span>} /></Col>)}
          </Row>
        </Card>
      ) : null}
      <Card className="ai-stock-card" title="模拟盘净值"><div className="ai-stock-chart">{curveOption ? <ReactECharts option={curveOption} style={{ height: 260 }} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无净值记录" />}</div></Card>
      <Card className="ai-stock-card" title="当前持仓"><Table className="ai-stock-table" columns={positionColumns} dataSource={positions} rowKey="ts_code" size="small" pagination={false} scroll={{ x: 860 }} /></Card>
      <Card className="ai-stock-card" title="成交流水"><Table className="ai-stock-table" columns={tradeColumns} dataSource={trades} rowKey="id" size="small" pagination={{ pageSize: 10 }} scroll={{ x: 960 }} /></Card>
    </Space>
  );

  return (
    <div className="ai-stock-page">
      <div className="ai-stock-heading"><div><Title level={3}>AI荐股</Title><Text type="secondary">AI 选股、历史复盘与自动模拟盘</Text></div>{evaluation ? <Tag color={evaluation.passed ? 'success' : 'default'}>对标门槛：{evaluation.passed ? '已达到' : '尚未达到'}</Tag> : null}</div>
      <Spin spinning={loading}>
        <Tabs items={[{ key: 'recommendations', label: 'AI 推荐', children: recommendationContent }, { key: 'today', label: '今日推荐', children: todayContent }, { key: 'history', label: '历史推荐', children: historyContent }, { key: 'paper', label: '模拟盘交易', children: paperContent }]} />
      </Spin>
      <Modal
        title="AI 荐股配置"
        open={settingsOpen}
        onCancel={() => setSettingsOpen(false)}
        onOk={saveSettings}
        confirmLoading={savingSettings}
        okText="保存"
        cancelText="取消"
        width={520}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text type="secondary">密钥仅提交给后端保存，页面不会显示已保存的完整值。留空可保留当前密钥。</Text>
          <Input.Password
            value={deepseekApiKey}
            onChange={event => setDeepseekApiKey(event.target.value)}
            placeholder={settings?.deepseek_configured ? '已配置；输入新 Key 可替换' : '请输入 DeepSeek API Key'}
            autoComplete="new-password"
          />
          <Input
            value={deepseekModel}
            onChange={event => setDeepseekModel(event.target.value)}
            placeholder="deepseek-chat"
            addonBefore="模型"
          />
          <Divider style={{ margin: '4px 0' }}>策略参数</Divider>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最多候选股</Text><Input type="number" value={maxCandidates} onChange={e => setMaxCandidates(Number(e.target.value) || 1)} min={1} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最多新闻事件</Text><Input type="number" value={maxEvents} onChange={e => setMaxEvents(Number(e.target.value) || 1)} min={1} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最多板块数</Text><Input type="number" value={maxBoards} onChange={e => setMaxBoards(Number(e.target.value) || 1)} min={1} /></Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>每板块最多候选</Text><Input type="number" value={maxCandidatesPerBoard} onChange={e => setMaxCandidatesPerBoard(Number(e.target.value) || 1)} min={1} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最小市值(万元)</Text><Input type="number" value={minMarketCap} onChange={e => setMinMarketCap(Number(e.target.value) || 0)} min={0} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最小成交额(千元)</Text><Input type="number" value={minAvgTurnover} onChange={e => setMinAvgTurnover(Number(e.target.value) || 0)} min={0} /></Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最多推荐股票</Text><Input type="number" value={maxRecommendations} onChange={e => setMaxRecommendations(Number(e.target.value) || 1)} min={1} max={50} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>上市最少天数</Text><Input type="number" value={minListingDays} onChange={e => setMinListingDays(Number(e.target.value) || 1)} min={1} /></Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>建议收益下限(%)</Text><Input type="number" value={targetReturnPctMin} onChange={e => setTargetReturnPctMin(Number(e.target.value) || 0)} min={0} max={100} step={0.5} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>建议收益上限(%)</Text><Input type="number" value={targetReturnPctMax} onChange={e => setTargetReturnPctMax(Number(e.target.value) || 0)} min={0} max={100} step={0.5} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>错价机会权重(0关闭)</Text><Input type="number" value={newsSignalWeight} onChange={e => setNewsSignalWeight(Number(e.target.value) || 0)} min={0} max={1} step={0.1} /></Col>
          </Row>
        </Space>
      </Modal>
      <Modal
        title="模拟盘策略参数"
        open={paperConfigOpen}
        onCancel={() => setPaperConfigOpen(false)}
        onOk={savePaperConfig}
        confirmLoading={savingPaperConfig}
        okText="保存"
        cancelText="取消"
        width={560}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text type="secondary">控制自动模拟盘的买入/卖出行为，改动立即对下一个交易分钟生效。</Text>
          <Switch checked={paperEnabled} onChange={setPaperEnabled} checkedChildren="启用" unCheckedChildren="停用" />
          <Divider style={{ margin: '4px 0' }}>买入</Divider>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最大持仓数</Text><Input type="number" value={paperMaxPositions} onChange={e => setPaperMaxPositions(Number(e.target.value) || 1)} min={1} step={1} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>仓位槽位数</Text><Input type="number" value={paperSlotCount} onChange={e => setPaperSlotCount(Number(e.target.value) || 1)} min={1} step={1} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>单股上限(比例)</Text><Input type="number" value={paperSingleStockCap} onChange={e => setPaperSingleStockCap(Number(e.target.value) || 0)} min={0} max={1} step={0.05} /></Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>最大总仓位(比例)</Text><Input type="number" value={paperMaxExecutionTarget} onChange={e => setPaperMaxExecutionTarget(Number(e.target.value) || 0)} min={0} max={1} step={0.05} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>追高上限(%)</Text><Input type="number" value={paperEntryPriceCapPct} onChange={e => setPaperEntryPriceCapPct(Number(e.target.value) || 0)} min={0} step={0.1} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>单日买入上限(次)</Text><Input type="number" value={paperMaxBuysPerDay} onChange={e => setPaperMaxBuysPerDay(Number(e.target.value) || 1)} min={1} step={1} /></Col>
          </Row>
          <Divider style={{ margin: '4px 0' }}>卖出</Divider>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>半仓止损线(%)</Text><Input type="number" value={paperStopLossHalfPct} onChange={e => setPaperStopLossHalfPct(Number(e.target.value) || 0)} max={0} step={0.5} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>全仓止损线(%)</Text><Input type="number" value={paperStopLossFullPct} onChange={e => setPaperStopLossFullPct(Number(e.target.value) || 0)} max={0} step={0.5} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>交易开始时间(分钟, 585=9:45)</Text><Input type="number" value={paperTradingStartMinute} onChange={e => setPaperTradingStartMinute(Number(e.target.value) || 585)} min={570} max={690} /></Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>移动止盈回撤(%)</Text><Input type="number" value={paperTrailingTakeProfitPct} onChange={e => setPaperTrailingTakeProfitPct(Number(e.target.value) || 0)} min={0} max={100} step={0.5} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>换仓信心差(0-100)</Text><Input type="number" value={paperRotationConfidenceGap} onChange={e => setPaperRotationConfidenceGap(Number(e.target.value) || 0)} min={0} max={100} disabled={!paperHoldEvalEnabled} /></Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>AI 持仓评估</Text><Switch checked={paperHoldEvalEnabled} onChange={setPaperHoldEvalEnabled} /></Col>
            <Col span={8}><Text type="secondary" style={{ fontSize: 12 }}>AI 卖出阈值(0-100)</Text><Input type="number" value={paperHoldSellThreshold} onChange={e => setPaperHoldSellThreshold(Number(e.target.value) || 30)} min={0} max={100} disabled={!paperHoldEvalEnabled} /></Col>
          </Row>
        </Space>
      </Modal>
    </div>
  );
};

export default AIStock;
