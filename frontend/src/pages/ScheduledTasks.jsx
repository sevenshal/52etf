import React, { useEffect, useMemo, useState } from 'react';
import dayjs from 'dayjs';
import {
  Alert,
  Button,
  DatePicker,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Space,
  Switch,
  Table,
  Tooltip,
  Typography,
  message,
  Tag,
  Select,
} from 'antd';
import {
  ClockCircleOutlined,
  ExpandAltOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import request from '../utils/request';
import { subscribeBackendEvent } from '../utils/backendEvents';

const { Title, Text } = Typography;
const RESULT_PREVIEW_LENGTH = 96;
const RUN_MODE_AUTO = 'auto';
const RUN_MODE_BACKFILL = 'backfill';
const getDefaultManualRunStartDate = () => dayjs().subtract(7, 'day');
const timezoneOptions = [
  { value: 'Asia/Shanghai', label: '上海时间' },
  { value: 'America/New_York', label: '美东时间' },
];
const timezoneSortValue = value => (value === 'America/New_York' ? 1 : 0);

const getRunStartDateHint = (taskKey) => {
  if (taskKey === 'evc_static_info_sync') {
    return {
      autoLabel: '增量同步',
      backfillLabel: '按日期全量同步',
      autoDetail: '不传开始日期，日K按每只标的最新日期向前重叠 7 天补齐；如检测到复权变化，会自动重刷该标的历史日K。',
      backfillDetail: '传入开始日期后，所有美股日K标的都会从所选日期同步到今天；static_info 快照也会正常刷新。'
    };
  }
  if (taskKey === 'a_stock_base_data_sync') {
    return {
      autoLabel: '增量同步',
      backfillLabel: '按日期回刷',
      autoDetail: '不传开始日期，后端按最新入库日期和重叠窗口补齐数据。',
      backfillDetail: '传入开始日期后，行情、复权、财务等模块会按各自 warmup 向前扩展回刷。'
    };
  }
  if (taskKey === 'hk_stock_base_data_sync') {
    return {
      autoLabel: '增量同步',
      backfillLabel: '按日期低速回填',
      autoDetail: '每天同步一个缺失港股交易日，并刷新主要指数；适配当前 Tushare 每分钟一次的权限。',
      backfillDetail: '从所选日期寻找缺失交易日，按接口限速续跑；可在任务参数中调整单次行情天数。'
    };
  }
  if (taskKey === 'etf_holdings_backfill') {
    return {
      autoLabel: '增量同步',
      backfillLabel: '按日期全量回刷',
      autoDetail: '不传开始日期，只抓取各 ETF 发行商当前最新持仓快照，并按返回的持仓日期覆盖入库。',
      backfillDetail: '传入开始日期后，系统会从该日期往后抓到今天，并按持仓日期覆盖写入数据库。'
    };
  }
  if (taskKey === 'soxx_fear_greed_backfill' || taskKey === 'a_stock_etf_fear_greed_backfill' || taskKey === 'hk_index_fear_greed_backfill') {
    return {
      autoLabel: '增量同步',
      backfillLabel: '按日期回跑',
      autoDetail: '不传开始日期，只刷新最近尾部结果，并自动向前取足计算窗口。',
      backfillDetail: '传入开始日期后，从所选日期起重新计算并覆盖历史结果；计算会自动向前取足窗口。'
    };
  }
  return {
    autoLabel: '增量同步',
    backfillLabel: '按日期回刷',
    autoDetail: '不传开始日期，由后端使用该任务的默认同步范围。',
    backfillDetail: '传入开始日期后，系统会从该日期起重新计算并写入历史记录。'
  };
};

const formatDateTime = (value) => {
  if (!value) {
    return '暂无';
  }
  return dayjs(value).format('YYYY-MM-DD HH:mm:ss');
};

const splitCronRules = value => String(value || '')
  .split(/[;\n]+/)
  .map(item => item.trim())
  .filter(Boolean);

const expandCronNumberField = (field, min, max) => {
  const values = new Set();
  const parts = String(field || '').split(',');
  for (const rawPart of parts) {
    let part = rawPart.trim();
    if (!part) continue;
    let step = 1;
    if (part.includes('/')) {
      const pieces = part.split('/');
      part = pieces[0];
      step = Math.max(Number(pieces[1]) || 1, 1);
    }
    let start;
    let end;
    if (part === '*') {
      start = min;
      end = max;
    } else if (part.includes('-')) {
      const pieces = part.split('-').map(Number);
      [start, end] = pieces;
    } else {
      start = Number(part);
      end = start;
    }
    if (!Number.isFinite(start) || !Number.isFinite(end)) return [];
    start = Math.max(start, min);
    end = Math.min(end, max);
    for (let value = start; value <= end; value += step) {
      values.add(value);
    }
  }
  return Array.from(values).sort((a, b) => a - b);
};

const getCronSortValue = task => {
  const backendValue = Number(task.first_daily_trigger_minutes);
  if (Number.isFinite(backendValue)) return backendValue;
  let best = Number.POSITIVE_INFINITY;
  splitCronRules(task.cron_rule || task.schedule_time).forEach(rule => {
    const fields = rule.split(/\s+/);
    if (fields.length !== 5) return;
    const minutes = expandCronNumberField(fields[0], 0, 59);
    const hours = expandCronNumberField(fields[1], 0, 23);
    if (minutes.length && hours.length) {
      best = Math.min(best, hours[0] * 60 + minutes[0]);
    }
  });
  if (Number.isFinite(best)) return best;
  return best;
};

const buildStatusTag = (task) => {
  if (task.is_running) {
    return <Tag color="processing">执行中</Tag>;
  }
  if (task.is_queued) {
    return <Tag color="blue">排队中</Tag>;
  }
  if (task.last_run_status === 'SUCCESS') {
    return <Tag color="success">最近成功</Tag>;
  }
  if (task.last_run_status === 'FAILED') {
    return <Tag color="error">最近失败</Tag>;
  }
  return <Tag>未执行</Tag>;
};

const getMessagePreview = (messageText) => {
  if (!messageText || messageText.length <= RESULT_PREVIEW_LENGTH) {
    return messageText;
  }
  return `${messageText.slice(0, RESULT_PREVIEW_LENGTH)}...`;
};

const showFullResultMessage = (task) => {
  Modal.info({
    title: `${task.name}完整执行结果`,
    width: 820,
    okText: '关闭',
    content: (
      <pre
        style={{
          margin: 0,
          maxHeight: '60vh',
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          fontSize: 12,
          lineHeight: 1.6,
        }}
      >
        {task.last_run_message}
      </pre>
    ),
  });
};

const renderParameterLabel = (parameter) => {
  const label = (
    <Text type="secondary" style={{ fontSize: 12 }}>
      {parameter.label || parameter.key}
    </Text>
  );
  if (!parameter.description) {
    return label;
  }
  return (
    <Tooltip title={parameter.description}>
      {label}
    </Tooltip>
  );
};

const TaskResultMessage = ({ task }) => {
  const messageText = task.last_run_message;
  if (!messageText) {
    return null;
  }

  const isLong = messageText.length > RESULT_PREVIEW_LENGTH;
  const content = (
    <Text
      type={task.last_run_status === 'FAILED' ? 'danger' : 'secondary'}
      style={{
        cursor: isLong ? 'pointer' : 'default',
        wordBreak: 'break-word',
        lineHeight: 1.6,
      }}
      onClick={() => {
        if (isLong) {
          showFullResultMessage(task);
        }
      }}
    >
      结果：{getMessagePreview(messageText)}
    </Text>
  );

  return (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      {isLong ? (
        <Tooltip title={messageText} overlayStyle={{ maxWidth: 720 }}>
          {content}
        </Tooltip>
      ) : (
        content
      )}
      {isLong ? (
        <Button
          type="link"
          size="small"
          icon={<ExpandAltOutlined />}
          style={{ padding: 0, height: 20, alignSelf: 'flex-start' }}
          onClick={() => showFullResultMessage(task)}
        >
          查看完整结果
        </Button>
      ) : null}
    </Space>
  );
};

const ScheduledTasks = () => {
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(false);
  const [savingTaskKey, setSavingTaskKey] = useState(null);
  const [runningTaskKey, setRunningTaskKey] = useState(null);
  const [runModalTask, setRunModalTask] = useState(null);
  const [runMode, setRunMode] = useState(RUN_MODE_AUTO);
  const [runStartDate, setRunStartDate] = useState(getDefaultManualRunStartDate());

  const fetchTasks = async (showLoading = true) => {
    if (showLoading) {
      setLoading(true);
    }
    try {
      const { data } = await request.get('/api/scheduled-tasks');
      setTasks(data);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取定时任务失败');
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    fetchTasks();
  }, []);

  useEffect(() => {
    return subscribeBackendEvent('scheduled_tasks', (data) => {
      if (Array.isArray(data.tasks)) {
        setTasks(data.tasks);
      }
    });
  }, []);

  const updateTaskField = (taskKey, patch) => {
    setTasks((prev) =>
      prev.map((task) => (task.task_key === taskKey ? { ...task, ...patch } : task))
    );
  };

  const updateTaskParameter = (taskKey, parameterKey, value) => {
    setTasks((prev) =>
      prev.map((task) => {
        if (task.task_key !== taskKey) {
          return task;
        }
        return {
          ...task,
          parameters: {
            ...(task.parameters || {}),
            [parameterKey]: value,
          },
        };
      })
    );
  };

  const handleSave = async (task) => {
    setSavingTaskKey(task.task_key);
    try {
      const { data } = await request.put(`/api/scheduled-tasks/${task.task_key}`, {
        enabled: task.enabled,
        cron_rule: task.cron_rule,
        timezone: task.timezone,
        parameters: task.parameters || {},
      });
      updateTaskField(task.task_key, data);
      message.success('任务配置已保存');
    } catch (error) {
      message.error(error.response?.data?.detail || '保存失败');
    } finally {
      setSavingTaskKey(null);
    }
  };

  const handleRunNow = async (task, options = {}) => {
    setRunningTaskKey(task.task_key);
    try {
      const { data } = await request.post(`/api/scheduled-tasks/${task.task_key}/run`, options);
      updateTaskField(task.task_key, data);
      message.success(data.is_queued ? '任务已加入执行队列' : '任务已开始执行');
    } catch (error) {
      message.warning(error.response?.data?.detail || '触发失败');
    } finally {
      setRunningTaskKey(null);
    }
  };

  const handleRunButtonClick = (task) => {
    if (task.supports_start_date) {
      setRunModalTask(task);
      setRunMode(RUN_MODE_AUTO);
      setRunStartDate(getDefaultManualRunStartDate());
      return;
    }
    handleRunNow(task);
  };

  const handleConfirmRun = async () => {
    if (!runModalTask) {
      return;
    }
    const currentTask = runModalTask;
    const payload = {};
    if (currentTask.supports_start_date && runMode === RUN_MODE_BACKFILL && runStartDate) {
      payload.start_date = runStartDate.format('YYYY-MM-DD');
    }
    setRunModalTask(null);
    await handleRunNow(currentTask, payload);
    setRunMode(RUN_MODE_AUTO);
    setRunStartDate(getDefaultManualRunStartDate());
  };

  const sortedTasks = useMemo(() => {
    return [...tasks].sort((left, right) => {
      const leftTime = getCronSortValue(left);
      const rightTime = getCronSortValue(right);
      const leftTimezone = timezoneSortValue(left.timezone);
      const rightTimezone = timezoneSortValue(right.timezone);
      if (leftTimezone !== rightTimezone) {
        return leftTimezone - rightTimezone;
      }
      if (leftTime !== rightTime) {
        return leftTime - rightTime;
      }
      const leftOrder = Number.isFinite(left.sort_order) ? left.sort_order : Number.MAX_SAFE_INTEGER;
      const rightOrder = Number.isFinite(right.sort_order) ? right.sort_order : Number.MAX_SAFE_INTEGER;
      if (leftOrder !== rightOrder) {
        return leftOrder - rightOrder;
      }
      return String(left.name || '').localeCompare(String(right.name || ''), 'zh-Hans-CN');
    });
  }, [tasks]);

  const columns = [
    {
      title: '任务',
      dataIndex: 'name',
      width: 280,
      render: (_, task) => (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Space size={8} wrap>
            <Text strong>{task.name}</Text>
            {buildStatusTag(task)}
          </Space>
          <Text type="secondary" style={{ fontSize: 12, lineHeight: 1.5 }}>
            {task.description}
          </Text>
        </Space>
      ),
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      width: 82,
      align: 'center',
      render: (_, task) => (
        <Switch
          checked={task.enabled}
          onChange={(checked) => updateTaskField(task.task_key, { enabled: checked })}
        />
      ),
    },
    {
      title: '触发 Cron',
      dataIndex: 'cron_rule',
      width: 280,
      render: (_, task) => (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Space size={6}>
            <ClockCircleOutlined />
            <Text type="secondary" style={{ fontSize: 12 }}>
              多条用分号或换行
            </Text>
          </Space>
          <Input.TextArea
            value={task.cron_rule || ''}
            autoSize={{ minRows: 1, maxRows: 3 }}
            size="small"
            onChange={(event) =>
              updateTaskField(task.task_key, {
                cron_rule: event.target.value,
              })
            }
          />
        </Space>
      ),
    },
    {
      title: '时区',
      dataIndex: 'timezone',
      width: 118,
      render: (_, task) => (
        <Select
          size="small"
          style={{ width: 104 }}
          options={timezoneOptions}
          value={task.timezone || 'Asia/Shanghai'}
          onChange={(value) => updateTaskField(task.task_key, { timezone: value })}
        />
      ),
    },
    {
      title: '参数',
      dataIndex: 'parameters',
      width: 260,
      render: (_, task) => {
        const schema = Array.isArray(task.parameter_schema) ? task.parameter_schema : [];
        if (!schema.length) {
          return <Text type="secondary">无</Text>;
        }
        return (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {schema.map((parameter) => {
              const value = (task.parameters || {})[parameter.key] ?? parameter.default;
              const inputStyle = { width: 112 };
              let control = null;
              if (parameter.type === 'boolean') {
                control = (
                  <Switch
                    size="small"
                    checked={!!value}
                    onChange={(checked) => updateTaskParameter(task.task_key, parameter.key, checked)}
                  />
                );
              } else if (parameter.type === 'integer' || parameter.type === 'float') {
                control = (
                  <InputNumber
                    size="small"
                    style={inputStyle}
                    value={value}
                    min={parameter.min_value ?? undefined}
                    max={parameter.max_value ?? undefined}
                    step={parameter.step ?? 1}
                    precision={parameter.type === 'integer' ? 0 : undefined}
                    addonAfter={parameter.suffix || null}
                    onChange={(nextValue) => updateTaskParameter(task.task_key, parameter.key, nextValue)}
                  />
                );
              } else {
                control = (
                  <Input
                    size="small"
                    value={value ?? ''}
                    suffix={parameter.suffix || null}
                    onChange={(event) => updateTaskParameter(task.task_key, parameter.key, event.target.value)}
                  />
                );
              }
              return (
                <div
                  key={parameter.key}
                  style={{
                    display: 'grid',
                    gridTemplateColumns: '86px 128px',
                    gap: 8,
                    alignItems: 'center',
                  }}
                >
                  {renderParameterLabel(parameter)}
                  {control}
                </div>
              );
            })}
          </Space>
        );
      },
    },
    {
      title: '执行时间',
      dataIndex: 'next_run_at',
      width: 300,
      render: (_, task) => (
        <Space direction="vertical" size={2}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            下次：{task.next_run_at ? formatDateTime(task.next_run_at) : '未安排'}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            开始：{formatDateTime(task.last_run_started_at)}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            结束：{formatDateTime(task.last_run_finished_at)}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            来源：{task.last_trigger_source || '暂无'}
            {typeof task.last_duration_seconds === 'number'
              ? ` · ${task.last_duration_seconds.toFixed(3)}s`
              : ''}
          </Text>
        </Space>
      ),
    },
    {
      title: '结果',
      dataIndex: 'last_run_message',
      width: 300,
      render: (_, task) => task.last_run_message ? (
        <TaskResultMessage task={task} />
      ) : (
        <Text type="secondary">暂无</Text>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 144,
      fixed: 'right',
      align: 'center',
      render: (_, task) => (
        <Space size={8}>
          <Tooltip title="保存配置">
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={savingTaskKey === task.task_key}
              onClick={() => handleSave(task)}
              aria-label="保存配置"
            />
          </Tooltip>
          <Tooltip title="立即执行一次">
            <Button
              icon={<PlayCircleOutlined />}
              loading={runningTaskKey === task.task_key}
              disabled={task.is_running || task.is_queued}
              onClick={() => handleRunButtonClick(task)}
              aria-label="立即执行一次"
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: '24px 12px', maxWidth: 'none', margin: '0 auto' }}>
      <Space
        align="start"
        style={{
          width: '100%',
          justifyContent: 'space-between',
          marginBottom: 16,
          gap: 16,
        }}
      >
        <Space direction="vertical" size={2}>
          <Title level={4} style={{ margin: 0 }}>定时任务</Title>
          <Text type="secondary">统一管理系统级定时任务，支持 Cron 和多次触发。</Text>
        </Space>
        <Button icon={<ReloadOutlined />} onClick={() => fetchTasks()}>
          刷新
        </Button>
      </Space>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="Cron 格式为“分 时 日 月 周”，周几请用 mon-fri/sat/sun 这种英文写法。可选择上海时间或美东时间，美东时间会按夏令时自动换算；修改保存后立即重载调度；所有任务进入统一队列顺序执行。"
      />

      <Table
        rowKey="task_key"
        columns={columns}
        dataSource={sortedTasks}
        loading={loading}
        pagination={false}
        size="middle"
        tableLayout="fixed"
        scroll={{ x: 1700 }}
        locale={{ emptyText: '暂无定时任务' }}
      />
      <Modal
        title={runModalTask ? `立即执行${runModalTask.name}` : '立即执行任务'}
        open={!!runModalTask}
        onCancel={() => {
          setRunModalTask(null);
          setRunMode(RUN_MODE_AUTO);
          setRunStartDate(getDefaultManualRunStartDate());
        }}
        onOk={handleConfirmRun}
        confirmLoading={runModalTask ? runningTaskKey === runModalTask.task_key : false}
        okText="开始执行"
        cancelText="取消"
      >
        <Space direction="vertical" size={12}>
          {runModalTask?.supports_start_date ? (
            (() => {
              const hint = getRunStartDateHint(runModalTask.task_key);
              return (
                <>
                  <Segmented
                    value={runMode}
                    onChange={setRunMode}
                    options={[
                      { label: hint.autoLabel, value: RUN_MODE_AUTO },
                      { label: hint.backfillLabel, value: RUN_MODE_BACKFILL },
                    ]}
                  />
                  {runMode === RUN_MODE_BACKFILL ? (
                    <DatePicker
                      value={runStartDate}
                      onChange={(value) => setRunStartDate(value)}
                      allowClear={false}
                      format="YYYY-MM-DD"
                      style={{ width: 180 }}
                    />
                  ) : null}
                  <Text type="secondary">
                    {runMode === RUN_MODE_BACKFILL ? hint.backfillDetail : hint.autoDetail}
                  </Text>
                </>
              );
            })()
          ) : null}
        </Space>
      </Modal>
    </div>
  );
};

export default ScheduledTasks;
