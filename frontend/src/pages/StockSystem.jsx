import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Popconfirm,
  Segmented,
  Select,
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
import {
  InfoCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import request from '../utils/request';
import StockDetailLink from '../components/StockDetailLink';
import StockSystemAllocation from './StockSystemAllocation';
import StockSystemTrading from './StockSystemTrading';
import StockSystemBacktest from './StockSystemBacktest';
import './StockSystem.css';

const { Text, Title, Paragraph } = Typography;

const VIEW_OPTIONS = [
  { label: '入池', value: 'pool' },
  { label: '通过闸门', value: 'passed' },
  { label: '未通过闸门', value: 'excluded' },
  { label: '全部', value: 'all' },
];
const GROUP_KEYS = ['valuation', 'growth', 'quality', 'expectation'];
const TASK_POLL_MS = 5000;
// 定时任务状态由后端写成大写（与「定时任务」页同一口径）
const TASK_STATUS_META = {
  SUCCESS: { label: '成功', color: 'green' },
  FAILED: { label: '失败', color: 'red' },
};

const isNumber = value => typeof value === 'number' && Number.isFinite(value);

const formatNumber = (value, digits = 1, suffix = '') => (
  isNumber(value) ? `${value.toFixed(digits)}${suffix}` : '-'
);

const formatValue = (value, unit) => {
  if (!isNumber(value)) return '-';
  if (unit === '倍') return `${value.toFixed(2)}倍`;
  return `${value.toFixed(1)}${unit || ''}`;
};

// A 股习惯：涨红跌绿
const SignedPercent = ({ value }) => {
  if (!isNumber(value)) return <span>-</span>;
  const className = value > 0 ? 'stock-system__up' : value < 0 ? 'stock-system__down' : '';
  return <span className={className}>{`${value > 0 ? '+' : ''}${value.toFixed(1)}%`}</span>;
};

const ScoreCell = ({ value }) => {
  if (!isNumber(value)) return <Text type="secondary">-</Text>;
  return (
    <span className="stock-system__score">
      <span className="stock-system__score-bar" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
      <span className="stock-system__score-text">{value.toFixed(1)}</span>
    </span>
  );
};

const formatErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item?.msg || String(item)).join('；') || fallback;
  return typeof detail === 'object' ? JSON.stringify(detail) : String(detail);
};

const numericSorter = key => (a, b) => {
  const left = isNumber(a[key]) ? a[key] : -Infinity;
  const right = isNumber(b[key]) ? b[key] : -Infinity;
  return left - right;
};

const cloneConfig = config => JSON.parse(JSON.stringify(config || {}));

const stripConfigMeta = (config) => {
  const { updated_at: _updatedAt, updated_by: _updatedBy, ...rest } = config || {};
  return rest;
};

const FactorBreakdown = ({ row, definitions, runConfig }) => {
  const groups = definitions?.groups || {};
  return (
    <div className="stock-system__breakdown">
      <table className="stock-system__breakdown-table">
        <thead>
          <tr>
            <th>因子</th>
            <th>分组</th>
            <th>原值</th>
            <th>百分位</th>
            <th>权重</th>
          </tr>
        </thead>
        <tbody>
          {(definitions?.factors || []).map((definition) => {
            const setting = runConfig?.factors?.[definition.key];
            const active = setting?.enabled && setting?.weight > 0;
            return (
              <tr key={definition.key} className={active ? '' : 'stock-system__muted'}>
                <td>
                  <Tooltip title={definition.description}>
                    <span>{definition.label} <InfoCircleOutlined /></span>
                  </Tooltip>
                </td>
                <td>{groups[definition.group] || definition.group}</td>
                <td>{formatValue(row[definition.key], definition.unit)}</td>
                <td>{formatNumber(row.factor_scores?.[definition.key], 1)}</td>
                <td>{active ? setting.weight : '未启用'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="stock-system__breakdown-notes">
        {isNumber(row.coverage) ? <div>因子覆盖率：{(row.coverage * 100).toFixed(0)}%</div> : null}
        {row.gate_reasons?.length ? (
          <div>
            <Text strong>未通过闸门：</Text>
            <ul>{row.gate_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
          </div>
        ) : null}
        {row.gate_notes?.length ? (
          <div>
            <Text type="secondary">提示：</Text>
            <ul>{row.gate_notes.map(note => <li key={note}><Text type="secondary">{note}</Text></li>)}</ul>
          </div>
        ) : null}
      </div>
    </div>
  );
};

// 仓位控制参数：[键, 标签, 说明]
const POSITION_FIELDS = [
  ['max_positions', '最大持仓数', null],
  ['max_single_weight_pct', '单只仓位上限 %', '单只目标权重 = min(该上限, 总仓位上限 / 最大持仓数)'],
  ['min_position_weight_pct', '单只仓位下限 %', '受板块或总额度限制分到的仓位不足它就跳过，名额让给后面的股票'],
  ['exposure_offense_pct', '市场进攻 · 总仓位 %', null],
  ['exposure_neutral_pct', '市场中性 · 总仓位 %', null],
  ['exposure_defense_pct', '市场防守/过热 · 总仓位 %', null],
  ['sector_cap_offense_pct', '板块进攻 · 板块上限 %', null],
  ['sector_cap_neutral_pct', '板块中性 · 板块上限 %', null],
  ['sector_cap_defense_pct', '板块防守 · 板块上限 %', '0 表示防守板块不开新仓；调大则允许防守板块开小仓位（市场防守时也用它限制未归类股票）。过热板块始终不开新仓'],
  ['unmapped_sector_cap_pct', '未归类股票 · 合计上限 %', '不属于任何行业贪恐指数的股票合在一起算，状态跟随市场'],
];

// 触发条件的参数：[键, 标签, 步长或 'bool']
const TRIGGER_PARAMS = {
  nine_turn_reversal: [['min_low_count', '低 N ≥', 1], ['window_days', '最近 N 日内', 1]],
  support_bounce: [['max_distance_atr', '距支撑 ≤ ATR', 0.1]],
  macd_golden_cross: [['below_zero_only', '仅零轴下方', 'bool']],
  breakout: [['breakout_atr', '突破 ≥ ATR', 0.05], ['min_volume_z', '放量 z ≥', 0.1]],
};
const XUEQIU_PARAMS = [
  ['lookback_days', '权价比窗口(快照日)', 1],
  ['min_ratio', '权价比 ≥', 0.05],
  ['min_holding_cubes', '持有组合数 ≥', 1],
  ['block_missing', '无雪球数据时拦截', 'bool'],
];
// [键, 标签, 步长或 'bool', 说明]
const EXIT_FIELDS = [
  ['initial_stop_atr', '初始止损（ATR 倍数）', 0.1, '止损幅度 = 倍数 × ATR / 收盘价，夹在 2%~25%'],
  ['trailing_stop_atr', '移动止损（ATR 倍数）', 0.1, '买入后最近红点（高 N≥2）收盘回撤超过这么多个 ATR 且出现低 N 就卖'],
  ['defense_trailing_stop_atr', '防守时移动止损（ATR 倍数）', 0.1, '所属板块或市场处于防守/过热时收紧到这一档'],
  ['trailing_min_low_count', '移动止损需要低 N ≥', 1, null],
  ['take_profit_high_nine', '高九止盈', 'bool', '研究显示顶部信号直接离场在强趋势里过早，默认关闭'],
  ['exit_on_gate_fail', '基本面闸门不通过就卖', 'bool', null],
  ['exit_pool_rank', '综合排名跌出多少名就卖', 10, '留缓冲，避免排名在入池线附近来回抖动导致频繁换手'],
  ['risk_per_trade_pct', '单笔风险预算（占总资产 %）', 0.1, '计划仓位 = min(第二层目标仓位, 风险预算 / 止损幅度)'],
];
const PAPER_FIELDS = [
  ['enabled', '启用模拟盘', 'bool', null],
  ['initial_capital', '初始资金（元）', 100000, '只在开户或重置模拟盘时生效'],
  ['commission_pct', '佣金（%，买卖双边）', 0.01, null],
  ['stamp_tax_pct', '印花税（%，卖出）', 0.01, null],
];

const ParamInput = ({ label, value, step, disabled, onChange }) => {
  if (step === 'bool') {
    return (
      <Space size={4}>
        {label ? <Text type="secondary">{label}</Text> : null}
        <Switch size="small" checked={Boolean(value)} disabled={disabled} onChange={onChange} />
      </Space>
    );
  }
  return (
    <InputNumber
      size="small"
      style={{ width: label ? 180 : 140 }}
      addonBefore={label}
      step={step}
      disabled={disabled}
      value={value}
      onChange={onChange}
    />
  );
};

const ConfigEditor = ({ draft, definitions, onChange }) => {
  if (!draft || !definitions) return <Spin />;
  const groups = definitions.groups || {};
  return (
    <div className="stock-system__config">
      <Title level={5}>股票池范围</Title>
      <div className="stock-system__config-grid">
        <span>剔除 ST / 退市整理</span>
        <Switch
          checked={draft.universe.exclude_st}
          onChange={value => onChange(['universe', 'exclude_st'], value)}
        />
        <span>平均窗口（交易日）</span>
        <InputNumber
          min={1}
          max={60}
          value={draft.universe.avg_window_days}
          onChange={value => onChange(['universe', 'avg_window_days'], value)}
        />
        <span>平均总市值下限（亿元）</span>
        <InputNumber
          min={0}
          value={draft.universe.min_avg_total_mv_100m}
          onChange={value => onChange(['universe', 'min_avg_total_mv_100m'], value)}
        />
        <span>平均成交额下限（万元）</span>
        <InputNumber
          min={0}
          step={100}
          value={draft.universe.min_avg_amount_10k}
          onChange={value => onChange(['universe', 'min_avg_amount_10k'], value)}
        />
      </div>

      <Title level={5}>硬闸门（只拦明显有问题的，数据缺失不拦）</Title>
      <div className="stock-system__config-list">
        {definitions.gates.map((gate) => {
          const setting = draft.gates[gate.key] || {};
          return (
            <div key={gate.key} className="stock-system__config-row">
              <Switch
                size="small"
                checked={setting.enabled}
                onChange={value => onChange(['gates', gate.key, 'enabled'], value)}
              />
              <Tooltip title={gate.description}>
                <span className="stock-system__config-label">{gate.label} <InfoCircleOutlined /></span>
              </Tooltip>
              <InputNumber
                size="small"
                disabled={!setting.enabled}
                value={setting.threshold}
                step={gate.unit === '倍' ? 0.1 : 1}
                addonAfter={gate.unit}
                onChange={value => onChange(['gates', gate.key, 'threshold'], value)}
              />
            </div>
          );
        })}
      </div>

      <Title level={5}>软评分因子（截面百分位 × 权重）</Title>
      {GROUP_KEYS.map(group => (
        <div key={group} className="stock-system__config-group">
          <Text type="secondary">{groups[group]}</Text>
          <div className="stock-system__config-list">
            {definitions.factors.filter(factor => factor.group === group).map((factor) => {
              const setting = draft.factors[factor.key] || {};
              return (
                <div key={factor.key} className="stock-system__config-row">
                  <Switch
                    size="small"
                    checked={setting.enabled}
                    onChange={value => onChange(['factors', factor.key, 'enabled'], value)}
                  />
                  <Tooltip title={factor.description}>
                    <span className="stock-system__config-label">{factor.label} <InfoCircleOutlined /></span>
                  </Tooltip>
                  <InputNumber
                    size="small"
                    min={0}
                    step={0.5}
                    disabled={!setting.enabled}
                    value={setting.weight}
                    addonBefore="权重"
                    onChange={value => onChange(['factors', factor.key, 'weight'], value)}
                  />
                </div>
              );
            })}
          </div>
        </div>
      ))}

      <Title level={5}>评分与入池</Title>
      <div className="stock-system__config-grid">
        <Tooltip title="有值因子的权重之和占全部启用权重的比例低于它时不给综合分，避免只靠一两个因子排名">
          <span>最低因子覆盖率 <InfoCircleOutlined /></span>
        </Tooltip>
        <InputNumber
          min={0}
          max={1}
          step={0.05}
          value={draft.scoring.min_factor_coverage}
          onChange={value => onChange(['scoring', 'min_factor_coverage'], value)}
        />
        <span>目标价变化回看（自然日）</span>
        <InputNumber
          min={5}
          max={365}
          value={draft.scoring.revision_lookback_days}
          onChange={value => onChange(['scoring', 'revision_lookback_days'], value)}
        />
        <span>入池数量</span>
        <InputNumber
          min={1}
          max={1000}
          value={draft.pool.size}
          onChange={value => onChange(['pool', 'size'], value)}
        />
      </div>

      <Title level={5}>情绪择时</Title>
      <div className="stock-system__config-grid">
        <span>定总仓位的市场指数</span>
        <Select
          style={{ width: 160 }}
          value={draft.timing.market_index}
          options={(definitions.market_index_options || []).map(option => ({ value: option.symbol, label: option.name }))}
          onChange={value => onChange(['timing', 'market_index'], value)}
        />
        <Tooltip title="0 表示信号一直有效，直到出现反向信号；大于 0 时超过这么多个交易日退回中性">
          <span>信号有效期（交易日） <InfoCircleOutlined /></span>
        </Tooltip>
        <InputNumber
          min={0}
          max={250}
          value={draft.timing.signal_expiry_days}
          onChange={value => onChange(['timing', 'signal_expiry_days'], value)}
        />
        <Tooltip title="贪恐分数不低于阈值时不开新仓，仓位上限按防守档">
          <span>过热不追新仓 <InfoCircleOutlined /></span>
        </Tooltip>
        <Space>
          <Switch
            checked={draft.timing.overheat_enabled}
            onChange={value => onChange(['timing', 'overheat_enabled'], value)}
          />
          <InputNumber
            min={50}
            max={100}
            disabled={!draft.timing.overheat_enabled}
            value={draft.timing.overheat_score}
            addonBefore="贪恐 ≥"
            onChange={value => onChange(['timing', 'overheat_score'], value)}
          />
        </Space>
      </div>

      <Title level={5}>仓位控制（占总资产）</Title>
      <div className="stock-system__config-grid">
        {POSITION_FIELDS.map(([key, label, tip]) => (
          <React.Fragment key={key}>
            {tip ? (
              <Tooltip title={tip}><span>{label} <InfoCircleOutlined /></span></Tooltip>
            ) : <span>{label}</span>}
            <InputNumber
              min={key === 'max_positions' ? 1 : 0}
              max={key === 'max_positions' ? 200 : 100}
              step={key === 'max_positions' ? 1 : 1}
              value={draft.position[key]}
              onChange={value => onChange(['position', key], value)}
            />
          </React.Fragment>
        ))}
      </div>

      <Title level={5}>技术信号（入场 = 任一触发 × 全部过滤）</Title>
      <div className="stock-system__config-list">
        {(definitions.triggers || []).map((trigger) => {
          const setting = draft.signals[trigger.key] || {};
          return (
            <div key={trigger.key} className="stock-system__config-row stock-system__config-row--wide">
              <Switch
                size="small"
                checked={setting.enabled}
                onChange={value => onChange(['signals', trigger.key, 'enabled'], value)}
              />
              <Tooltip title={trigger.description}>
                <span className="stock-system__config-label">{trigger.label} <InfoCircleOutlined /></span>
              </Tooltip>
              <Space size={6} wrap>
                {(TRIGGER_PARAMS[trigger.key] || []).map(([key, label, step]) => (
                  <ParamInput
                    key={key}
                    label={label}
                    step={step}
                    disabled={!setting.enabled}
                    value={setting[key]}
                    onChange={value => onChange(['signals', trigger.key, key], value)}
                  />
                ))}
              </Space>
            </div>
          );
        })}
        <div className="stock-system__config-row stock-system__config-row--wide">
          <Switch
            size="small"
            checked={draft.signals.xueqiu_ratio.enabled}
            onChange={value => onChange(['signals', 'xueqiu_ratio', 'enabled'], value)}
          />
          <Tooltip title="入场过滤：雪球活跃组合的 N 日权价比（权重倍数 ÷ 股价倍数，与雪球持仓页同一口径）≥ 阈值，且持有它的组合数 ≥ 下限。雪球数据从 2026-06-25 起才有效，覆盖不到的日期自动跳过这条过滤。">
            <span className="stock-system__config-label">雪球过滤 <InfoCircleOutlined /></span>
          </Tooltip>
          <Space size={6} wrap>
            {XUEQIU_PARAMS.map(([key, label, step]) => (
              <ParamInput
                key={key}
                label={label}
                step={step}
                disabled={!draft.signals.xueqiu_ratio.enabled}
                value={draft.signals.xueqiu_ratio[key]}
                onChange={value => onChange(['signals', 'xueqiu_ratio', key], value)}
              />
            ))}
          </Space>
        </div>
      </div>

      <Title level={5}>止损与出场</Title>
      <div className="stock-system__config-grid">
        {EXIT_FIELDS.map(([key, label, step, tip]) => (
          <React.Fragment key={key}>
            {tip ? <Tooltip title={tip}><span>{label} <InfoCircleOutlined /></span></Tooltip> : <span>{label}</span>}
            <ParamInput value={draft.signals[key]} step={step} onChange={value => onChange(['signals', key], value)} />
          </React.Fragment>
        ))}
      </div>

      <Title level={5}>模拟盘</Title>
      <div className="stock-system__config-grid">
        {PAPER_FIELDS.map(([key, label, step, tip]) => (
          <React.Fragment key={key}>
            {tip ? <Tooltip title={tip}><span>{label} <InfoCircleOutlined /></span></Tooltip> : <span>{label}</span>}
            <ParamInput value={draft.paper[key]} step={step} onChange={value => onChange(['paper', key], value)} />
          </React.Fragment>
        ))}
      </div>
    </div>
  );
};

const StockSystem = () => {
  const [activeTab, setActiveTab] = useState('pool');
  const [view, setView] = useState('pool');
  const [snapshot, setSnapshot] = useState({ run: null, rows: [] });
  // 分页必须受控：只传 pageSize 常量而不接 onChange 时，antd 会把切换器的改动丢掉（点了没反应）
  const [tablePage, setTablePage] = useState({ current: 1, pageSize: 50 });

  // 切换视图（入池/剔除等）数据整批换掉，回到第 1 页
  useEffect(() => {
    setTablePage(prev => ({ ...prev, current: 1 }));
  }, [view]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [configState, setConfigState] = useState(null);
  const [draft, setDraft] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [task, setTask] = useState(null);
  const [asOf, setAsOf] = useState('');
  const [refreshToken, setRefreshToken] = useState(0);
  const viewRef = useRef(view);
  viewRef.current = view;

  const loadPool = useCallback(async (targetView) => {
    setLoading(true);
    setError(null);
    try {
      const { data } = await request.get('/api/stock-system/pool', { params: { view: targetView } });
      setSnapshot({ run: data?.run || null, rows: data?.rows || [] });
    } catch (loadError) {
      setError(formatErrorMessage(loadError, '股票池加载失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const { data } = await request.get('/api/stock-system/config');
      setConfigState(data);
      setDraft(cloneConfig(stripConfigMeta(data.config)));
    } catch (loadError) {
      message.error(formatErrorMessage(loadError, '配置加载失败'));
    }
  }, []);

  const loadTask = useCallback(async () => {
    const { data } = await request.get('/api/stock-system/pool/task');
    setTask(data);
    return data;
  }, []);

  useEffect(() => {
    loadConfig();
    loadTask().catch(() => null);
  }, [loadConfig, loadTask]);

  useEffect(() => {
    loadPool(view);
  }, [loadPool, view]);

  const busy = Boolean(task?.is_running || task?.is_queued);
  useEffect(() => {
    if (!busy) return undefined;
    const timer = setInterval(async () => {
      const latest = await loadTask().catch(() => null);
      if (latest && !latest.is_running && !latest.is_queued) {
        loadPool(viewRef.current);
        setRefreshToken(token => token + 1);
      }
    }, TASK_POLL_MS);
    return () => clearInterval(timer);
  }, [busy, loadPool, loadTask]);

  const runTask = useCallback(async ({ onlyAllocation = false } = {}) => {
    try {
      const payload = { only_allocation: onlyAllocation };
      if (asOf) payload.as_of = asOf;
      const { data } = await request.post('/api/stock-system/pool/run', payload);
      setTask(data);
      message.success(onlyAllocation ? '已开始重算择时与仓位，完成后自动刷新' : '已开始计算，完成后自动刷新');
    } catch (runError) {
      message.error(formatErrorMessage(runError, '启动计算失败'));
    }
  }, [asOf]);

  const updateDraft = useCallback((path, value) => {
    setDraft((previous) => {
      const next = cloneConfig(previous);
      let cursor = next;
      path.slice(0, -1).forEach((key) => {
        cursor[key] = cursor[key] || {};
        cursor = cursor[key];
      });
      cursor[path[path.length - 1]] = value;
      return next;
    });
  }, []);

  const saveConfig = useCallback(async (runAfterSave) => {
    setSaving(true);
    try {
      const { data } = await request.put('/api/stock-system/config', draft);
      setConfigState(data);
      setDraft(cloneConfig(stripConfigMeta(data.config)));
      message.success('配置已保存');
      setDrawerOpen(false);
      if (runAfterSave) await runTask({ onlyAllocation: runAfterSave === 'allocation' });
    } catch (saveError) {
      message.error(formatErrorMessage(saveError, '保存失败'));
    } finally {
      setSaving(false);
    }
  }, [draft, runTask]);

  const resetConfig = useCallback(async () => {
    setSaving(true);
    try {
      const { data } = await request.post('/api/stock-system/config/reset');
      setConfigState(data);
      setDraft(cloneConfig(stripConfigMeta(data.config)));
      message.success('已恢复默认配置');
    } catch (resetError) {
      message.error(formatErrorMessage(resetError, '恢复默认失败'));
    } finally {
      setSaving(false);
    }
  }, []);

  const definitions = configState?.definitions;
  const groups = definitions?.groups || {};
  const gateLabels = useMemo(
    () => Object.fromEntries((definitions?.gates || []).map(gate => [gate.key, gate.label])),
    [definitions],
  );
  const run = snapshot.run;
  const summary = run?.summary || {};

  const stockColumn = {
    title: '股票',
    key: 'stock',
    fixed: 'left',
    width: 160,
    render: (_, row) => (
      <div>
        <StockDetailLink symbol={row.ts_code}>{row.name || row.ts_code}</StockDetailLink>
        <div className="stock-system__code">{row.ts_code} · {row.industry || '-'}</div>
      </div>
    ),
  };

  const scoredColumns = [
    {
      title: '排名', dataIndex: 'pool_rank', width: 70, fixed: 'left', sorter: numericSorter('pool_rank'),
      render: value => (isNumber(value) ? value : '-'),
    },
    stockColumn,
    {
      title: '综合分', dataIndex: 'composite_score', width: 110, sorter: numericSorter('composite_score'),
      render: value => <ScoreCell value={value} />,
    },
    ...GROUP_KEYS.map(group => ({
      title: groups[group] || group,
      dataIndex: `${group}_score`,
      width: 96,
      sorter: numericSorter(`${group}_score`),
      render: value => <ScoreCell value={value} />,
    })),
    { title: 'DCF回报', dataIndex: 'dcf_return_pct', width: 92, sorter: numericSorter('dcf_return_pct'), render: value => <SignedPercent value={value} /> },
    { title: '共识低估率', dataIndex: 'consensus_upside_pct', width: 100, sorter: numericSorter('consensus_upside_pct'), render: value => <SignedPercent value={value} /> },
    { title: '营收同比', dataIndex: 'revenue_yoy_pct', width: 92, sorter: numericSorter('revenue_yoy_pct'), render: value => <SignedPercent value={value} /> },
    { title: '扣非同比', dataIndex: 'profit_yoy_pct', width: 92, sorter: numericSorter('profit_yoy_pct'), render: value => <SignedPercent value={value} /> },
    { title: 'ROIC(12M)', dataIndex: 'profitability_pct', width: 96, sorter: numericSorter('profitability_pct'), render: value => formatNumber(value, 1, '%') },
    { title: '市值(亿)', dataIndex: 'avg_total_mv_100m', width: 90, sorter: numericSorter('avg_total_mv_100m'), render: value => formatNumber(value, 0) },
    { title: '成交额(万)', dataIndex: 'avg_amount_10k', width: 100, sorter: numericSorter('avg_amount_10k'), render: value => formatNumber(value, 0) },
  ];
  if (view === 'all') {
    scoredColumns.push({
      title: '状态',
      key: 'status',
      width: 96,
      render: (_, row) => {
        if (row.in_pool) return <Tag color="red">入池</Tag>;
        if (row.gate_passed) return <Tag color="blue">通过闸门</Tag>;
        return <Tooltip title={(row.gate_reasons || []).join('；')}><Tag>未通过</Tag></Tooltip>;
      },
    });
  }

  const excludedColumns = [
    stockColumn,
    {
      title: '未通过原因',
      dataIndex: 'gate_reasons',
      render: reasons => (reasons || []).map(reason => <div key={reason}>{reason}</div>),
    },
    { title: 'DCF回报', dataIndex: 'dcf_return_pct', width: 92, sorter: numericSorter('dcf_return_pct'), render: value => <SignedPercent value={value} /> },
    { title: '营收同比', dataIndex: 'revenue_yoy_pct', width: 92, sorter: numericSorter('revenue_yoy_pct'), render: value => <SignedPercent value={value} /> },
    { title: '市值(亿)', dataIndex: 'avg_total_mv_100m', width: 90, sorter: numericSorter('avg_total_mv_100m'), render: value => formatNumber(value, 0) },
  ];

  const taskStatus = task ? (
    <Space size={4} wrap>
      {busy ? <Tag color="processing">{task.is_running ? '计算中' : '排队中'}</Tag> : null}
      {!busy && task.last_run_status ? (
        <Tooltip title={task.last_run_message}>
          <Tag color={TASK_STATUS_META[task.last_run_status]?.color || 'default'}>
            上次{TASK_STATUS_META[task.last_run_status]?.label || task.last_run_status}
          </Tag>
        </Tooltip>
      ) : null}
      {task.next_run_at ? <Text type="secondary">下次自动计算 {task.next_run_at.replace('T', ' ').slice(0, 16)}</Text> : null}
    </Space>
  ) : null;

  const poolTab = (
    <>
      {run ? (
        <>
          <div className="stock-system__stats">
            <Statistic title="交易日" value={run.trade_date || '-'} />
            <Statistic title="有行情股票" value={summary.universe_total ?? '-'} />
            <Statistic title="股票池范围" value={summary.universe_size ?? '-'} />
            <Statistic title="通过闸门" value={summary.gate_passed ?? '-'} />
            <Statistic title="有综合分" value={summary.scored ?? '-'} />
            <Statistic title="入池" value={summary.pool_size ?? '-'} />
          </div>
          <div className="stock-system__reasons">
            {Object.entries(summary.universe_excluded || {}).map(([reason, count]) => (
              <Tag key={reason}>{reason}：{count}</Tag>
            ))}
            {Object.entries(summary.gate_failures || {}).map(([key, count]) => (
              <Tag key={key} color="orange">{gateLabels[key] || (key === 'no_fundamentals' ? '缺少财务数据' : key)}：{count}</Tag>
            ))}
            {isNumber(summary.consensus_covered) ? <Tag color="geekblue">有卖方共识估值：{summary.consensus_covered}</Tag> : null}
            {run.duration_seconds ? <Text type="secondary">计算耗时 {run.duration_seconds}s</Text> : null}
          </div>
        </>
      ) : null}

      <div className="stock-system__toolbar">
        <Segmented options={VIEW_OPTIONS} value={view} onChange={setView} />
        <Space>
          <Text type="secondary">共 {snapshot.rows.length} 只</Text>
          <Button icon={<ReloadOutlined />} onClick={() => loadPool(view)} loading={loading}>刷新</Button>
        </Space>
      </div>

      <Spin spinning={loading}>
        {!run && !loading ? (
          <Empty description="还没有股票池快照，点「立即计算」生成第一份（全市场约 1~2 分钟）" />
        ) : (
          <Table
            rowKey="ts_code"
            size="small"
            dataSource={snapshot.rows}
            columns={view === 'excluded' ? excludedColumns : scoredColumns}
            scroll={{ x: 1400 }}
            pagination={{
              current: tablePage.current,
              pageSize: tablePage.pageSize,
              showSizeChanger: true,
              pageSizeOptions: [50, 100, 200, 500],
              // 改每页条数时回到第 1 页，避免当前页码超出新的总页数
              onChange: (current, pageSize) => setTablePage(prev => ({
                current: pageSize !== prev.pageSize ? 1 : current,
                pageSize,
              })),
            }}
            expandable={view === 'excluded' ? undefined : {
              expandedRowRender: row => (
                <FactorBreakdown row={row} definitions={definitions} runConfig={run?.config} />
              ),
            }}
          />
        )}
      </Spin>
    </>
  );

  return (
    <div className="stock-system">
      <div className="stock-system__header">
        <div>
          <Title level={4} style={{ margin: 0 }}>选股系统</Title>
          <Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
            第一层基本面股票池：按市值、成交额和 ST 划定范围，硬闸门只拦明显有问题的股票，再按估值、成长、质量、预期
            四组因子的截面百分位加权排名取前 N 名。第二层情绪择时与仓位：市场和板块的贪恐顶/底信号决定总仓位、
            能不能开新仓和板块上限。所有数据按估值日当时可见的口径计算，每个交易日 19:10 自动计算。
          </Paragraph>
        </div>
        <Space wrap>
          <Input
            type="date"
            value={asOf}
            onChange={event => setAsOf(event.target.value)}
            style={{ width: 150 }}
            placeholder="估值日"
            allowClear
          />
          <Button type="primary" icon={<PlayCircleOutlined />} loading={busy} onClick={() => runTask()}>
            {asOf ? '按该日计算' : '立即计算'}
          </Button>
          <Button icon={<SettingOutlined />} onClick={() => setDrawerOpen(true)}>参数配置</Button>
        </Space>
      </div>

      {taskStatus ? <div className="stock-system__task">{taskStatus}</div> : null}
      {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} /> : null}

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: 'pool', label: '基本面股票池', children: poolTab },
          {
            key: 'allocation',
            label: '情绪择时与仓位',
            children: (
              <StockSystemAllocation
                refreshToken={refreshToken}
                busy={busy}
                onRunAllocation={() => runTask({ onlyAllocation: true })}
              />
            ),
          },
          {
            key: 'trading',
            label: '技术信号与模拟盘',
            children: <StockSystemTrading refreshToken={refreshToken} />,
          },
          {
            key: 'backtest',
            label: '回测与消融',
            children: <StockSystemBacktest />,
          },
        ]}
      />

      <Drawer
        title="选股系统参数"
        width={680}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnClose={false}
        extra={(
          <Space>
            <Popconfirm title="恢复全部默认参数？" onConfirm={resetConfig}>
              <Button loading={saving}>恢复默认</Button>
            </Popconfirm>
            <Button loading={saving} onClick={() => saveConfig(false)}>保存</Button>
            <Tooltip title="只改了情绪择时/仓位参数时用：沿用已有股票池，几十秒就好">
              <Button loading={saving} onClick={() => saveConfig('allocation')}>保存并重算仓位</Button>
            </Tooltip>
            <Button type="primary" loading={saving} onClick={() => saveConfig('all')}>保存并全部计算</Button>
          </Space>
        )}
      >
        {configState?.config?.updated_at ? (
          <Paragraph type="secondary">
            上次保存：{configState.config.updated_at.replace('T', ' ').slice(0, 19)}
            {configState.config.updated_by ? `（${configState.config.updated_by}）` : ''}
            。保存后下一次计算生效。
          </Paragraph>
        ) : null}
        <ConfigEditor draft={draft} definitions={definitions} onChange={updateDraft} />
      </Drawer>
    </div>
  );
};

export default StockSystem;
