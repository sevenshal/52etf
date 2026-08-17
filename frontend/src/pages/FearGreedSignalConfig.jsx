import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Col,
  Form,
  InputNumber,
  Row,
  Space,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { ReloadOutlined, SaveOutlined, ThunderboltOutlined } from '@ant-design/icons';
import request from '../utils/request';
import { PageSection, PageShell } from '../components/PageScaffold';

const { Text } = Typography;

const FIELDS = [
  {
    key: 'ma5_bottom_score',
    label: '均线底分数阈值',
    tooltip: '均线型底：恐贪MA5上穿（当日>前一日）且最近N日任意一天恐贪 ≤ 该值（默认 25）',
    min: 1,
    max: 99,
    step: 1,
    suffix: '分',
  },
  {
    key: 'ma5_top_score',
    label: '均线顶分数阈值',
    tooltip: '均线型顶：恐贪MA5下穿（当日<前一日）且最近N日任意一天恐贪 ≥ 该值（默认 75）',
    min: 1,
    max: 99,
    step: 1,
    suffix: '分',
  },
  {
    key: 'ma5_lookback_days',
    label: 'MA5回看天数',
    tooltip: '最近N日内任意一天满足分数条件即视为触发（默认 5）',
    min: 1,
    max: 30,
    step: 1,
    suffix: '天',
  },
  {
    key: 'volume_bottom_score',
    label: '量能底恐贪阈值',
    tooltip: '量能型底：恐贪 ≤ 该值且放量（默认 30）',
    min: 1,
    max: 99,
    step: 1,
    suffix: '分',
  },
  {
    key: 'volume_top_score',
    label: '量能顶恐贪阈值',
    tooltip: '量能型顶：恐贪 ≥ 该值且缩量（默认 75）',
    min: 1,
    max: 99,
    step: 1,
    suffix: '分',
  },
  {
    key: 'volume_expand_std',
    label: '量能底放量标准差',
    tooltip: '放量确认：log(成交量) 高于不含当日过去20日均值该标准差（默认 1.25）',
    min: 0,
    max: 10,
    step: 0.05,
    suffix: 'σ',
  },
  {
    key: 'volume_shrink_std',
    label: '量能顶缩量标准差',
    tooltip: '缩量确认：log(成交量) 低于不含当日过去20日均值该标准差（默认 0.25）',
    min: 0,
    max: 10,
    step: 0.05,
    suffix: 'σ',
  },
  {
    key: 'cooldown_days',
    label: '信号冷却天数',
    tooltip: '同类底/顶信号（各类型顶/底分别独立）出后 N 个交易日不重复出信号（默认 5）',
    min: 0,
    max: 60,
    step: 1,
    suffix: '天',
  },
];

const FearGreedSignalConfig = () => {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const fetchConfig = async (showLoading = true) => {
    if (showLoading) {
      setLoading(true);
    }
    try {
      const { data } = await request.get('/api/fear-greed-signal-config');
      const values = {};
      FIELDS.forEach(field => {
        values[field.key] = data[field.key];
      });
      form.setFieldsValue(values);
    } catch (error) {
      message.error(error.response?.data?.detail || '获取贪恐信号配置失败');
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    fetchConfig();
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      const values = await form.validateFields();
      const payload = {};
      FIELDS.forEach(field => {
        const value = values[field.key];
        if (value !== undefined && value !== null) {
          payload[field.key] = Number(value);
        }
      });
      const { data } = await request.put('/api/fear-greed-signal-config', payload);
      const updated = {};
      FIELDS.forEach(field => {
        updated[field.key] = data[field.key];
      });
      form.setFieldsValue(updated);
      message.success('贪恐信号配置已保存');
    } catch (error) {
      if (error?.errorFields) {
        message.error('请检查表单填写');
        return;
      }
      message.error(error.response?.data?.detail || '保存贪恐信号配置失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <PageShell
      title="贪恐信号配置"
      actions={(
        <Space>
          <Tooltip title="刷新">
            <Button icon={<ReloadOutlined />} onClick={() => fetchConfig()} aria-label="刷新" />
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
            <ThunderboltOutlined />
            <span>自算贪恐底/顶信号（统一配置）</span>
          </Space>
        )}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="星澜壹贰叁号（雪球组合）与自算贪恐历史曲线共用这一套信号参数"
          description={(
            <>
              两种信号类型，每类信号的顶/底分别独立冷却：
              <ul style={{ margin: '8px 0 0 0', paddingLeft: 20 }}>
                <li><Text strong>均线型</Text>：底 = 恐贪MA5上穿且最近N日任意一天恐贪 ≤ 均线底分数阈值；顶 = 恐贪MA5下穿且最近N日任意一天恐贪 ≥ 均线顶分数阈值。</li>
                <li><Text strong>量能型</Text>：底 = 恐贪 ≤ 量能底恐贪阈值且放量（log量比z &gt; 放量标准差）；顶 = 恐贪 ≥ 量能顶恐贪阈值且缩量（log量比z &lt; -缩量标准差）。log量比z = 当日log(成交量) 相对不含当日过去20日log(成交量)的z-score。</li>
              </ul>
            </>
          )}
        />
        <Form form={form} layout="vertical" style={{ maxWidth: 720 }}>
          <Row gutter={16}>
            {FIELDS.map(field => (
              <Col xs={24} sm={12} key={field.key}>
                <Form.Item
                  name={field.key}
                  label={(
                    <Tooltip title={field.tooltip}>
                      {field.label}
                    </Tooltip>
                  )}
                >
                  <InputNumber
                    min={field.min}
                    max={field.max}
                    step={field.step}
                    precision={field.key === 'ma5_lookback_days' || field.key === 'cooldown_days' ? 0 : 2}
                    style={{ width: '100%' }}
                    addonAfter={field.suffix}
                  />
                </Form.Item>
              </Col>
            ))}
          </Row>
        </Form>
      </PageSection>
    </PageShell>
  );
};

export default FearGreedSignalConfig;
