import React, { useEffect, useMemo, useState } from 'react';
import dayjs from 'dayjs';
import {
  Alert,
  Button,
  DatePicker,
  Modal,
  Space,
  Switch,
  Table,
  TimePicker,
  Tooltip,
  Typography,
  message,
  Tag,
} from 'antd';
import {
  ClockCircleOutlined,
  ExpandAltOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import request from '../utils/request';

const { Title, Text } = Typography;
const RESULT_PREVIEW_LENGTH = 96;

const parseTimeValue = (time) => {
  if (!time) {
    return null;
  }
  const [hour, minute] = time.split(':').map(Number);
  return dayjs().hour(hour || 0).minute(minute || 0).second(0);
};

const formatDateTime = (value) => {
  if (!value) {
    return '暂无';
  }
  return dayjs(value).format('YYYY-MM-DD HH:mm:ss');
};

const getScheduleSortValue = (time) => {
  if (!time || typeof time !== 'string') {
    return Number.POSITIVE_INFINITY;
  }
  const [hour, minute] = time.split(':').map(Number);
  if (Number.isNaN(hour) || Number.isNaN(minute)) {
    return Number.POSITIVE_INFINITY;
  }
  return hour * 60 + minute;
};

const buildStatusTag = (task) => {
  if (task.is_running) {
    return <Tag color="processing">执行中</Tag>;
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
  const [runStartDate, setRunStartDate] = useState(dayjs('2023-12-08'));

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

  const updateTaskField = (taskKey, patch) => {
    setTasks((prev) =>
      prev.map((task) => (task.task_key === taskKey ? { ...task, ...patch } : task))
    );
  };

  const handleSave = async (task) => {
    setSavingTaskKey(task.task_key);
    try {
      const { data } = await request.put(`/api/scheduled-tasks/${task.task_key}`, {
        enabled: task.enabled,
        schedule_time: task.schedule_time,
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
      message.success('任务已开始执行');
      setTimeout(() => fetchTasks(false), 3000);
      setTimeout(() => fetchTasks(false), 12000);
    } catch (error) {
      message.warning(error.response?.data?.detail || '触发失败');
    } finally {
      setRunningTaskKey(null);
    }
  };

  const handleRunButtonClick = (task) => {
    if (task.supports_start_date) {
      setRunModalTask(task);
      setRunStartDate(dayjs('2023-12-08'));
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
    if (currentTask.supports_start_date && runStartDate) {
      payload.start_date = runStartDate.format('YYYY-MM-DD');
    }
    setRunModalTask(null);
    await handleRunNow(currentTask, payload);
    setRunStartDate(dayjs('2023-12-08'));
  };

  const sortedTasks = useMemo(() => {
    return [...tasks].sort((left, right) => {
      const leftTime = getScheduleSortValue(left.schedule_time);
      const rightTime = getScheduleSortValue(right.schedule_time);
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
      title: '执行时间',
      dataIndex: 'schedule_time',
      width: 132,
      render: (_, task) => (
        <Space size={8} align="center">
          <ClockCircleOutlined />
          <TimePicker
            value={parseTimeValue(task.schedule_time)}
            format="HH:mm"
            minuteStep={1}
            allowClear={false}
            size="small"
            style={{ width: 88 }}
            onChange={(value) =>
              updateTaskField(task.task_key, {
                schedule_time: value ? value.format('HH:mm') : task.schedule_time,
              })
            }
          />
        </Space>
      ),
    },
    {
      title: '下次执行',
      dataIndex: 'next_run_at',
      width: 168,
      render: (value) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {value ? formatDateTime(value) : '未安排'}
        </Text>
      ),
    },
    {
      title: '最近运行',
      dataIndex: 'last_run_started_at',
      width: 260,
      render: (_, task) => (
        <Space direction="vertical" size={2}>
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
      width: 120,
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
              onClick={() => handleRunButtonClick(task)}
              aria-label="立即执行一次"
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: '24px', maxWidth: 1440, margin: '0 auto' }}>
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
          <Text type="secondary">统一管理系统级定时任务，时间精确到时分。</Text>
        </Space>
        <Button icon={<ReloadOutlined />} onClick={() => fetchTasks()}>
          刷新
        </Button>
      </Space>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="任务按服务器本地时间执行。修改保存后会立即重载调度；服务启动时只会补执行当天未执行且已错过计划时间的任务。"
      />

      <Table
        rowKey="task_key"
        columns={columns}
        dataSource={sortedTasks}
        loading={loading}
        pagination={false}
        size="middle"
        tableLayout="fixed"
        scroll={{ x: 1260 }}
        locale={{ emptyText: '暂无定时任务' }}
      />
      <Modal
        title={runModalTask ? `立即执行${runModalTask.name}` : '立即执行任务'}
        open={!!runModalTask}
        onCancel={() => {
          setRunModalTask(null);
          setRunStartDate(dayjs('2023-12-08'));
        }}
        onOk={handleConfirmRun}
        confirmLoading={runModalTask ? runningTaskKey === runModalTask.task_key : false}
        okText="开始执行"
        cancelText="取消"
      >
        <Space direction="vertical" size={12}>
          {runModalTask?.supports_start_date ? (
            <>
              <Text>选择回跑开始日期，系统会从该日期起重新计算并写入历史记录。</Text>
              <DatePicker
                value={runStartDate}
                onChange={(value) => setRunStartDate(value)}
                allowClear={false}
                format="YYYY-MM-DD"
                style={{ width: 180 }}
              />
              <Text type="secondary">
                计算时会自动向前取足滚动窗口数据，但只保存所选日期之后的结果。
              </Text>
            </>
          ) : null}
        </Space>
      </Modal>
    </div>
  );
};

export default ScheduledTasks;
