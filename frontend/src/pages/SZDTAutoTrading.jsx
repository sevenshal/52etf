import React, { useCallback, useEffect, useState } from 'react';
import { Card, Form, Select, Button, Switch, Typography, message, Skeleton } from 'antd';
import request from '../utils/request';

const { Title, Text } = Typography;
const { Option } = Select;

export const SZDTConfigForm = ({ onSuccess }) => {
    const [loading, setLoading] = useState(false);
    const [config, setConfig] = useState(null);
    const [ibAccounts, setIbAccounts] = useState([]);
    const [externalAccounts, setExternalAccounts] = useState([]);
    const [externalSubAccounts, setExternalSubAccounts] = useState([]);
    const [form] = Form.useForm();
    const selectedExternalAccountId = Form.useWatch('external_trading_account_id', form);

  const loadExternalSubAccounts = useCallback(async (externalAccountId) => {
    if (!externalAccountId) {
      setExternalSubAccounts([]);
      return;
    }
    try {
      const { data } = await request.get(`/api/external-trading-accounts/${externalAccountId}/sub-accounts/options`);
      setExternalSubAccounts(data);
    } catch (error) {
      setExternalSubAccounts([]);
      message.error('加载外部交易子账户失败');
    }
  }, []);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [configRes, ibRes, externalAccountsRes] = await Promise.all([
        request.get('/api/szdt-configs/'),
        request.get('/api/ib-accounts/options'),
        request.get('/api/external-trading-accounts'),
      ]);
      setConfig(configRes.data);
      setIbAccounts(ibRes.data);
      setExternalAccounts((externalAccountsRes.data || []).filter(
        account => account.enabled && account.market_type === 'A_STOCK'
      ));
      form.setFieldsValue(configRes.data);
      await loadExternalSubAccounts(configRes.data.external_trading_account_id);
    } catch (error) {
      message.error('加载配置失败');
    } finally {
      setLoading(false);
    }
  }, [form, loadExternalSubAccounts]);

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

    const handleValuesChange = (changedValues) => {
      if (Object.prototype.hasOwnProperty.call(changedValues, 'external_trading_account_id')) {
        form.setFieldValue('live_sub_account_id', undefined);
        loadExternalSubAccounts(changedValues.external_trading_account_id);
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
            onValuesChange={handleValuesChange}
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
                    开启后，后端将在A股交易时段每分钟检查一次贪恐指标，并通过所选外部交易账户的虚拟子账户自动执行。
                </Text>
            </div>

            <Form.Item noStyle shouldUpdate={(prev, current) => prev.enabled_a !== current.enabled_a}>
                {({ getFieldValue }) => getFieldValue('enabled_a') && (
                    <>
                        <Form.Item
                            label="A股外部交易账户"
                            name="external_trading_account_id"
                            rules={[{ required: true, message: '请选择 A股外部交易账户' }]}
                        >
                            <Select placeholder="选择用于自动交易的 A股外部账户">
                                {externalAccounts.map(account => (
                                    <Option key={account.id} value={account.id}>
                                        {account.name}
                                    </Option>
                                ))}
                            </Select>
                        </Form.Item>

                        <Form.Item
                            label="虚拟子账户"
                            name="live_sub_account_id"
                            rules={[{ required: true, message: '请选择专用虚拟子账户' }]}
                            extra="只能选择空闲子账户；保存后会由守猪逮兔 A股策略独占。"
                        >
                            <Select placeholder="选择虚拟子账户" disabled={!selectedExternalAccountId}>
                                {externalSubAccounts
                                    .filter(subAccount => subAccount.enabled && (
                                        subAccount.binding_status === 'FREE' || subAccount.id === config?.live_sub_account_id
                                    ))
                                    .map(subAccount => (
                                        <Option key={subAccount.id} value={subAccount.id}>
                                            {subAccount.name}（{subAccount.binding_label}）
                                        </Option>
                                    ))}
                            </Select>
                        </Form.Item>
                    </>
                )}
            </Form.Item>

            <Form.Item noStyle shouldUpdate={(prev, current) => prev.enabled !== current.enabled}>
                {({ getFieldValue }) => getFieldValue('enabled') && (
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
                )}
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
