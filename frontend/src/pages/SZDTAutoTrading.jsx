import React, { useCallback, useEffect, useState } from 'react';
import { Card, Form, Select, Button, Switch, Typography, message, Skeleton } from 'antd';
import request from '../utils/request';

const { Title, Text } = Typography;
const { Option } = Select;

export const SZDTConfigForm = ({ onSuccess }) => {
    const [loading, setLoading] = useState(false);
    const [config, setConfig] = useState(null);
  const [ibAccounts, setIbAccounts] = useState([]);
  const [form] = Form.useForm();

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [configRes, ibRes] = await Promise.all([
        request.get('/api/szdt-configs/'),
        request.get('/api/ib-accounts/options')
      ]);
      setConfig(configRes.data);
      setIbAccounts(ibRes.data);
      form.setFieldsValue(configRes.data);
    } catch (error) {
      message.error('加载配置失败');
    } finally {
      setLoading(false);
    }
  }, [form]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

    const handleSave = async (values) => {
        try {
            await request.post('/api/szdt-configs/', values);
            message.success('配置已保存');
            fetchData(); // Refresh to ensure sync
            if (onSuccess) {
                onSuccess();
            }
        } catch (error) {
            message.error('保存失败');
        }
    };

    if (loading && !config) {
        return <Skeleton active />;
    }

    return (
        <Form
            form={form}
            layout="vertical"
            onFinish={handleSave}
            initialValues={config}
        >
            <Form.Item label="启用美股自动化交易" name="enabled" valuePropName="checked">
                <Switch checkedChildren="开启" unCheckedChildren="关闭" />
            </Form.Item>

            <div style={{ marginBottom: 16 }}>
                <Text type="secondary">
                    开启后，系统将在美股交易时段每分钟检查一次持仓和情绪指标，自动执行买入或卖出操作。
                </Text>
            </div>

            <Form.Item label="启用A股自动化交易" name="enabled_a" valuePropName="checked">
                <Switch checkedChildren="开启" unCheckedChildren="关闭" />
            </Form.Item>

            <div style={{ marginBottom: 16 }}>
                <Text type="secondary">
                    开启后，系统将在A股交易时段检查持仓和情绪指标，自动执行买入或卖出操作 (需配置对应A股账户)。
                </Text>
            </div>

            <Form.Item
                label="IBKR 交易账户"
                name="ib_account_id"
                rules={[{ required: true, message: '请选择 IBKR 账户' }]}
            >
                <Select placeholder="选择用于交易的 IBKR 账户">
                    {ibAccounts.map(account => (
                        <Option key={account.id} value={account.id}>
                            {account.name} (Port: {account.ib_port})
                        </Option>
                    ))}
                </Select>
            </Form.Item>

            <Form.Item>
                <Button type="primary" htmlType="submit">
                    保存配置
                </Button>
            </Form.Item>
        </Form>
    );
};

const SZDTAutoTrading = () => {
    return (
        <div style={{ padding: '24px' }}>
            <Card title={<Title level={4}>贪恐策略自动化交易配置</Title>}>
                <SZDTConfigForm />
            </Card>
        </div>
    );
};

export default SZDTAutoTrading;
