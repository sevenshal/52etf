import React, { useCallback, useEffect, useState } from 'react';
import dayjs from 'dayjs';
import { Button, Card, Col, Descriptions, Progress, Row, Statistic, Tag, Tooltip, Typography, message } from 'antd';
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import request from '../utils/request';
import { PageShell } from '../components/PageScaffold';
import './SystemInfo.css';

const { Text } = Typography;

const formatBytes = (bytes) => {
  if (bytes === null || bytes === undefined || isNaN(bytes)) return '-';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, i);
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[i]}`;
};

const formatDuration = (seconds) => {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return '-';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const parts = [];
  if (days > 0) parts.push(`${days} 天`);
  if (hours > 0) parts.push(`${hours} 小时`);
  if (minutes > 0 || parts.length === 0) parts.push(`${minutes} 分钟`);
  return parts.join(' ');
};

// 使用率 -> 进度条颜色
const usageColor = (percent) => {
  if (percent === null || percent === undefined) return '#1677ff';
  if (percent >= 90) return '#cf1322';
  if (percent >= 70) return '#fa8c16';
  return '#52c41a';
};

const Meter = ({ label, percent, suffix }) => (
  <div className="sysinfo-meter">
    <div className="sysinfo-meter__head">
      <span className="sysinfo-meter__label">{label}</span>
      <span className="sysinfo-meter__value" style={{ color: usageColor(percent) }}>
        {percent === null || percent === undefined ? '-' : `${percent.toFixed(1)}%`}
      </span>
    </div>
    <Progress
      percent={percent === null || percent === undefined ? 0 : Math.min(100, percent)}
      strokeColor={usageColor(percent)}
      showInfo={false}
      size={{ height: 10 }}
    />
    {suffix && <div className="sysinfo-meter__suffix">{suffix}</div>}
  </div>
);

const SystemInfo = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchInfo = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await request.get('/api/system/info');
      setData(response.data);
    } catch (e) {
      setError(e.response?.data?.detail || e.message || '获取系统信息失败');
      message.error('获取系统信息失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchInfo();
  }, [fetchInfo]);

  const cpu = data?.cpu || {};
  const memory = data?.memory || {};
  const swap = data?.swap || {};
  const disks = data?.disks || [];
  const processInfo = data?.process || {};
  const temp = cpu?.temperature?.temperature_c;

  return (
    <PageShell
      title="系统信息"
      actions={
        <div className="sysinfo-actions">
          {data?.collected_at && (
            <Text type="secondary" className="sysinfo-collected-at">
              采集于 {dayjs(data.collected_at * 1000).format('YYYY-MM-DD HH:mm:ss')}
            </Text>
          )}
          <Button
            type="primary"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={fetchInfo}
          >
            刷新
          </Button>
        </div>
      }
    >
      {error && !data && (
        <Card className="sysinfo-card sysinfo-card--error">
          <Text type="danger">加载失败：{error}</Text>
        </Card>
      )}

      {/* 系统概览 */}
      <Card className="sysinfo-card" title="系统概览" loading={loading && !data}>
        <Descriptions column={{ xs: 1, sm: 2, lg: 3 }} size="small">
          <Descriptions.Item label="主机名">{data?.hostname || '-'}</Descriptions.Item>
          <Descriptions.Item label="操作系统">{data?.system || '-'} {data?.release || ''}</Descriptions.Item>
          <Descriptions.Item label="架构">{data?.architecture || '-'}</Descriptions.Item>
          <Descriptions.Item label="Python 版本">{data?.python_version || '-'}</Descriptions.Item>
          <Descriptions.Item label="开机时间">
            {data?.boot_time ? dayjs(data.boot_time * 1000).format('YYYY-MM-DD HH:mm:ss') : '-'}
          </Descriptions.Item>
          <Descriptions.Item label="已运行">
            {data?.uptime_seconds ? formatDuration(data.uptime_seconds) : '-'}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* CPU 与内存 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <Card className="sysinfo-card" title="CPU" loading={loading && !data}>
            <div className="sysinfo-cpu">
              <Progress
                type="dashboard"
                percent={cpu.percent === undefined ? 0 : Math.min(100, cpu.percent)}
                strokeColor={usageColor(cpu.percent)}
                format={(p) => `${p.toFixed(0)}%`}
                size={132}
              />
              <div className="sysinfo-cpu__meta">
                <div className="sysinfo-cpu__temp">
                  <ThunderboltOutlined style={{ color: '#fa8c16' }} />
                  <span>CPU 温度</span>
                  <strong>{temp !== undefined && temp !== null ? `${temp.toFixed(1)} °C` : 'N/A'}</strong>
                </div>
                <Statistic title="物理核心 / 逻辑核心" value={`${cpu.count_physical ?? '-'} / ${cpu.count_logical ?? '-'}`} />
                <Statistic title="负载 (1/5/15 分钟)" value={cpu.load_avg ? cpu.load_avg.join(' / ') : '-'} />
                <Statistic title="当前频率" value={cpu.frequency_mhz ? `${cpu.frequency_mhz.toFixed(0)} MHz` : '-'} />
              </div>
            </div>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card className="sysinfo-card" title="内存" loading={loading && !data}>
            <Meter label="内存使用率" percent={memory.percent} />
            <div className="sysinfo-memory__grid">
              <Statistic title="总量" value={formatBytes(memory.total)} />
              <Statistic title="已用" value={formatBytes(memory.used)} />
              <Statistic title="可用" value={formatBytes(memory.available)} />
              <Statistic title="空闲" value={formatBytes(memory.free)} />
            </div>
            <div className="sysinfo-swap">
              <Meter label="Swap 使用率" percent={swap.percent} />
              <div className="sysinfo-memory__grid sysinfo-memory__grid--swap">
                <Statistic title="Swap 总量" value={formatBytes(swap.total)} />
                <Statistic title="Swap 已用" value={formatBytes(swap.used)} />
              </div>
            </div>
          </Card>
        </Col>
      </Row>

      {/* 磁盘 */}
      <Card className="sysinfo-card" title="磁盘" loading={loading && !data}>
        {disks.length === 0 ? (
          <Text type="secondary">暂无磁盘信息</Text>
        ) : (
          <Row gutter={[16, 16]}>
            {disks.map((disk) => (
              <Col xs={24} sm={12} lg={8} key={disk.device}>
                <Card size="small" className="sysinfo-disk">
                  <div className="sysinfo-disk__head">
                    <Tooltip title={disk.device}>
                      <Text strong className="sysinfo-disk__mount">{disk.device}</Text>
                    </Tooltip>
                    <Text type="secondary" className="sysinfo-disk__fstype">{disk.fstype}</Text>
                  </div>
                  <Progress
                    percent={Math.min(100, disk.percent)}
                    strokeColor={usageColor(disk.percent)}
                    size="small"
                  />
                  <div className="sysinfo-memory__grid sysinfo-memory__grid--disk">
                    <Statistic title="总量" value={formatBytes(disk.total)} />
                    <Statistic title="已用" value={formatBytes(disk.used)} />
                    <Statistic title="可用" value={formatBytes(disk.free)} />
                  </div>
                  {(disk.mountpoints || []).length > 0 && (
                    <div className="sysinfo-disk__mountpoints">
                      <Text type="secondary" className="sysinfo-disk__mountpoints-label">挂载点</Text>
                      <div className="sysinfo-disk__mountpoints-list">
                        {disk.mountpoints.map((mp) => (
                          <Tag key={mp} className="sysinfo-disk__mountpoints-tag">{mp}</Tag>
                        ))}
                      </div>
                    </div>
                  )}
                </Card>
              </Col>
            ))}
          </Row>
        )}
      </Card>

      {/* 后端进程 */}
      <Card className="sysinfo-card" title="后端服务进程" loading={loading && !data}>
        {processInfo.error ? (
          <Text type="secondary">进程信息不可用</Text>
        ) : (
          <>
            <div className="sysinfo-process">
              <Meter label="进程 CPU 使用率" percent={processInfo.cpu_percent} />
              <Meter label="进程内存占用" percent={processInfo.memory_percent} />
            </div>
            <Descriptions column={{ xs: 1, sm: 2, lg: 3 }} size="small">
              <Descriptions.Item label="PID">{processInfo.pid ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="进程名">{processInfo.name || '-'}</Descriptions.Item>
              <Descriptions.Item label="线程数">{processInfo.num_threads ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="内存占用 (RSS)">{formatBytes(processInfo.memory_rss)}</Descriptions.Item>
              <Descriptions.Item label="启动时间">
                {processInfo.start_time ? dayjs(processInfo.start_time * 1000).format('YYYY-MM-DD HH:mm:ss') : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="运行用户">{processInfo.username || '-'}</Descriptions.Item>
            </Descriptions>
            {processInfo.cmdline && (
              <div className="sysinfo-cmdline">
                <Text type="secondary" style={{ wordBreak: 'break-all' }}>{processInfo.cmdline}</Text>
              </div>
            )}
          </>
        )}
      </Card>
    </PageShell>
  );
};

export default SystemInfo;
