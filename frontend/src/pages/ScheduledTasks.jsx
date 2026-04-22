import React, { useEffect, useState } from 'react';
import dayjs from 'dayjs';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  List,
  Modal,
  Space,
  Spin,
  Switch,
  TimePicker,
  Typography,
  message,
  Tag,
} from 'antd';
import {
  ClockCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import request from '../utils/request';

const { Title, Text } = Typography;

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

const ScheduledTasks = () => {
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(false);
  const [savingTaskKey, setSavingTaskKey] = useState(null);
  const [runningTaskKey, setRunningTaskKey] = useState(null);
  const [runModalTask, setRunModalTask] = useState(null);
  const [forceFetch, setForceFetch] = useState(false);

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
    if (task.supports_force_fetch) {
      setRunModalTask(task);
      setForceFetch(false);
      return;
    }
    handleRunNow(task);
  };

  const handleConfirmRun = async () => {
    if (!runModalTask) {
      return;
    }
    const currentTask = runModalTask;
    setRunModalTask(null);
    await handleRunNow(currentTask, { force_fetch: forceFetch });
    setForceFetch(false);
  };

  return (
    <div style={{ padding: '24px' }}>
      <Card
        title={
          <Space direction="vertical" size={0}>
            <Title level={4} style={{ margin: 0 }}>定时任务</Title>
            <Text type="secondary">统一管理系统级定时任务，时间精确到时分。</Text>
          </Space>
        }
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => fetchTasks()}>
            刷新
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="任务按服务器本地时间执行。修改保存后会立即重载调度，手动执行不受启用状态限制。"
        />

        <Spin spinning={loading}>
          {tasks.length === 0 ? (
            <Empty description="暂无定时任务" />
          ) : (
            <List
              grid={{ gutter: 16, xs: 1, sm: 1, md: 2, lg: 2, xl: 2 }}
              dataSource={tasks}
              renderItem={(task) => (
                <List.Item key={task.task_key}>
                  <Card
                    size="small"
                    title={
                      <Space>
                        <span>{task.name}</span>
                        {buildStatusTag(task)}
                      </Space>
                    }
                  >
                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                      <Text type="secondary">{task.description}</Text>

                      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                        <Text>启用任务</Text>
                        <Switch
                          checked={task.enabled}
                          onChange={(checked) => updateTaskField(task.task_key, { enabled: checked })}
                        />
                      </Space>

                      <Space style={{ justifyContent: 'space-between', width: '100%' }} align="center">
                        <Space>
                          <ClockCircleOutlined />
                          <Text>执行时间</Text>
                        </Space>
                        <TimePicker
                          value={parseTimeValue(task.schedule_time)}
                          format="HH:mm"
                          minuteStep={1}
                          allowClear={false}
                          onChange={(value) =>
                            updateTaskField(task.task_key, {
                              schedule_time: value ? value.format('HH:mm') : task.schedule_time,
                            })
                          }
                        />
                      </Space>

                      <Space direction="vertical" size={4}>
                        <Text type="secondary">下次执行：{task.next_run_at ? formatDateTime(task.next_run_at) : '未安排'}</Text>
                        <Text type="secondary">最近开始：{formatDateTime(task.last_run_started_at)}</Text>
                        <Text type="secondary">最近结束：{formatDateTime(task.last_run_finished_at)}</Text>
                        <Text type="secondary">
                          最近来源：{task.last_trigger_source || '暂无'}
                          {typeof task.last_duration_seconds === 'number' ? ` · ${task.last_duration_seconds.toFixed(3)}s` : ''}
                        </Text>
                        {task.last_run_message ? (
                          <Text type={task.last_run_status === 'FAILED' ? 'danger' : 'secondary'}>
                            结果：{task.last_run_message}
                          </Text>
                        ) : null}
                      </Space>

                      <Space wrap>
                        <Button
                          type="primary"
                          icon={<SaveOutlined />}
                          loading={savingTaskKey === task.task_key}
                          onClick={() => handleSave(task)}
                        >
                          保存配置
                        </Button>
                        <Button
                          icon={<PlayCircleOutlined />}
                          loading={runningTaskKey === task.task_key}
                          onClick={() => handleRunButtonClick(task)}
                        >
                          立即执行一次
                        </Button>
                      </Space>
                    </Space>
                  </Card>
                </List.Item>
              )}
            />
          )}
        </Spin>
      </Card>
      <Modal
        title="立即执行股票估值数据抓取"
        open={!!runModalTask}
        onCancel={() => {
          setRunModalTask(null);
          setForceFetch(false);
        }}
        onOk={handleConfirmRun}
        confirmLoading={runModalTask ? runningTaskKey === runModalTask.task_key : false}
        okText="开始执行"
        cancelText="取消"
      >
        <Space direction="vertical" size={12}>
          <Text>这次手动执行时，可以选择是否强制拉取当日数据。</Text>
          <Checkbox
            checked={forceFetch}
            onChange={(event) => setForceFetch(event.target.checked)}
          >
            强制拉取
          </Checkbox>
          <Text type="secondary">
            勾选后会忽略“今日已抓取”的检查，重新执行一次 EVC 数据抓取。
          </Text>
        </Space>
      </Modal>
    </div>
  );
};

export default ScheduledTasks;
