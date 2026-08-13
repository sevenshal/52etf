import React, { useEffect, useMemo, useState } from 'react';
import { Button, Input, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import {
  ClearOutlined,
  MailOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import request from '../utils/request';
import { PageSection, PageShell } from '../components/PageScaffold';

const { Text } = Typography;

const CATEGORY_COLORS = {
  系统: 'blue',
  交易: 'green',
  外部交易: 'purple',
  报告: 'orange',
};

const asText = value => value || '';

const EmailSettings = () => {
  const [settings, setSettings] = useState({ default_email: '', scenarios: [] });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const fetchSettings = async (showLoading = true) => {
    if (showLoading) {
      setLoading(true);
    }
    try {
      const { data } = await request.get('/api/email-settings');
      setSettings({
        default_email: asText(data.default_email),
        scenarios: Array.isArray(data.scenarios) ? data.scenarios : [],
      });
    } catch (error) {
      message.error(error.response?.data?.detail || '获取邮箱配置失败');
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    fetchSettings();
  }, []);

  const updateScenarioEmail = (key, value) => {
    setSettings(prev => ({
      ...prev,
      scenarios: prev.scenarios.map(item => (
        item.key === key ? { ...item, recipient_email: value } : item
      )),
    }));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const scenarioPayload = {};
      settings.scenarios.forEach(item => {
        scenarioPayload[item.key] = asText(item.recipient_email);
      });
      const { data } = await request.put('/api/email-settings', {
        default_email: asText(settings.default_email),
        scenarios: scenarioPayload,
      });
      setSettings({
        default_email: asText(data.default_email),
        scenarios: Array.isArray(data.scenarios) ? data.scenarios : [],
      });
      message.success('邮箱配置已保存');
    } catch (error) {
      message.error(error.response?.data?.detail || '保存邮箱配置失败');
    } finally {
      setSaving(false);
    }
  };

  const sortedScenarios = useMemo(() => settings.scenarios, [settings.scenarios]);

  const columns = [
    {
      title: '场景',
      dataIndex: 'name',
      width: 320,
      render: (_, item) => (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Space size={6} wrap>
            <Tag color={CATEGORY_COLORS[item.category] || 'default'}>{item.category}</Tag>
            <Text strong>{item.name}</Text>
          </Space>
          <Text type="secondary" style={{ fontSize: 12, lineHeight: 1.5 }}>
            {item.description}
          </Text>
        </Space>
      ),
    },
    {
      title: '专用邮箱',
      dataIndex: 'recipient_email',
      width: 360,
      render: (_, item) => (
        <Input.TextArea
          value={asText(item.recipient_email)}
          placeholder="未配置时使用默认邮箱"
          autoSize={{ minRows: 1, maxRows: 3 }}
          onChange={event => updateScenarioEmail(item.key, event.target.value)}
        />
      ),
    },
    {
      title: '生效状态',
      dataIndex: 'effective_email',
      width: 300,
      render: (_, item) => {
        const ownEmail = asText(item.recipient_email).trim();
        const defaultEmail = asText(settings.default_email).trim();
        const effectiveEmail = ownEmail || defaultEmail;
        if (ownEmail) {
          return (
            <Space direction="vertical" size={4}>
              <Tag color="blue">专用</Tag>
              <Text style={{ wordBreak: 'break-all' }}>{ownEmail}</Text>
            </Space>
          );
        }
        if (defaultEmail) {
          return (
            <Space direction="vertical" size={4}>
              <Tag>默认</Tag>
              <Text style={{ wordBreak: 'break-all' }}>{effectiveEmail}</Text>
            </Space>
          );
        }
        return <Tag color="warning">不发送</Tag>;
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 82,
      align: 'center',
      render: (_, item) => (
        <Tooltip title="清空专用邮箱">
          <Button
            icon={<ClearOutlined />}
            aria-label="清空专用邮箱"
            disabled={!asText(item.recipient_email)}
            onClick={() => updateScenarioEmail(item.key, '')}
          />
        </Tooltip>
      ),
    },
  ];

  return (
    <PageShell
      title="邮箱管理"
      actions={(
        <Space>
          <Tooltip title="刷新">
            <Button icon={<ReloadOutlined />} onClick={() => fetchSettings()} aria-label="刷新" />
          </Tooltip>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
            保存
          </Button>
        </Space>
      )}
    >
      <PageSection
        title={(
          <Space size={8}>
            <MailOutlined />
            <span>默认邮箱</span>
          </Space>
        )}
      >
        <Input.TextArea
          value={asText(settings.default_email)}
          placeholder="name@example.com"
          autoSize={{ minRows: 1, maxRows: 3 }}
          onChange={event => setSettings(prev => ({ ...prev, default_email: event.target.value }))}
        />
      </PageSection>

      <PageSection title="场景邮箱">
        <Table
          rowKey="key"
          columns={columns}
          dataSource={sortedScenarios}
          loading={loading}
          pagination={false}
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 1060 }}
          locale={{ emptyText: '暂无邮件场景' }}
        />
      </PageSection>
    </PageShell>
  );
};

export default EmailSettings;
