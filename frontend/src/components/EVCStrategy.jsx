import React, { useState, useEffect } from 'react';
import { Card, Form, InputNumber, Button, Switch, message } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';

// 在文件开头的 imports 中添加
import { Modal, Input, DatePicker } from 'antd';

const EVCStrategy = () => {
    const [form] = Form.useForm();
    const [tokenExpiry, setTokenExpiry] = useState(null);
    const navigate = useNavigate();
    // 在现有的 state 声明后添加
    const [isTokenModalVisible, setIsTokenModalVisible] = useState(false);
    const [tokenForm] = Form.useForm();

    // 加载策略配置
    const loadStrategyConfig = async () => {
        try {
            const { data } = await request.get('/api/evc/config');
            form.setFieldsValue(data);
            setTokenExpiry(data.access_token_expired_at);
        } catch (error) {
            message.error('加载策略配置失败');
        }
    };

    useEffect(() => {
        loadStrategyConfig();
    }, []);

    // 更新策略参数
    const handleUpdateStrategy = async (values) => {
        try {
            await request.post('/api/evc/update-strategy', values);
            message.success('更新成功');
        } catch (error) {
            message.error('更新失败');
        }
    };

    // 添加这些新函数
    const showTokenModal = () => {
        setIsTokenModalVisible(true);
    };

    const handleTokenUpdate = async (values) => {
        try {
            const { data } = await request.post('/api/evc/update-token', {
                access_token: values.access_token,
                access_token_expired_at: values.expired_at
            });
            setTokenExpiry(data.access_token_expired_at);
            setIsTokenModalVisible(false);
            tokenForm.resetFields();
            message.success('Token更新成功');
        } catch (error) {
            message.error('Token更新失败');
        }
    };

    // 在 return 语句中修改显示过期时间的部分
    return (
        <Card 
            title={
                <div style={{ display: 'flex', alignItems: 'center' }}>
                    <LeftOutlined 
                        onClick={() => navigate('/evc')}
                        style={{ 
                            fontSize: '16px',
                            marginRight: '10px',
                            cursor: 'pointer'
                        }}
                    />
                    EVC自动化交易策略
                </div>
            }
        >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <p>Access Token 过期时间: {tokenExpiry ? new Date(tokenExpiry).toLocaleDateString() : '未设置'}</p>
                <Button size="small" onClick={showTokenModal}>更新Token</Button>
            </div>
            <Modal
                title="更新 Access Token"
                open={isTokenModalVisible}
                onCancel={() => setIsTokenModalVisible(false)}
                footer={null}
            ><Form
                    form={tokenForm}
                    onFinish={handleTokenUpdate}
                    layout="vertical"
                >
                    <Form.Item
                        label="Access Token"
                        name="access_token"
                        rules={[{ required: true, message: '请输入 Access Token' }]}
                    >
                        <Input.TextArea rows={4} />
                    </Form.Item>
                    <Form.Item
                        label="过期时间"
                        name="expired_at"
                        rules={[{ required: true, message: '请选择过期时间' }]}
                    >
                        <DatePicker
                            showTime
                            format="YYYY-MM-DD HH:mm:ss"
                            style={{ width: '100%' }}
                        />
                    </Form.Item>
                    <Form.Item>
                        <Button type="primary" htmlType="submit" block>
                            更新
                        </Button>
                    </Form.Item>
                </Form>
            </Modal>
            <Form 
                form={form} 
                onFinish={handleUpdateStrategy} 
                labelCol={{ span: 8 }}
                wrapperCol={{ span: 8 }}
                style={{ maxWidth: 600 }}
            >
                <Form.Item 
                    label="自动化交易开关" 
                    name="auto_trading_enabled"
                    tooltip="开启后机器人会在下财年增长超过阈值且低估超过阈值时买入，在超过当前财年估值上限阈值且超过下财年估值中位数阈值时卖出"
                    valuePropName="checked"
                >
                    <Switch 
                        checkedChildren="开启" 
                        unCheckedChildren="关闭" 
                    />
                </Form.Item>
                <Form.Item 
                    label="低估阈值" 
                    name="undervalue_threshold"
                    tooltip="股价相对于估值的比例阈值，例如0.9表示股价低于估值的90%"
                >
                    <InputNumber min={0} max={1} step={0.1} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item 
                    label="下财年增长阈值" 
                    name="next_fy_growth_threshold"
                    tooltip="下一财年预期增长率的最小值，例如1.1表示下一财年预期增长率大于10%"
                >
                    <InputNumber min={1} step={0.1} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item 
                    label="当前财年估值上限阈值" 
                    name="current_fy_hi_threshold"
                    tooltip="股价超过当前财年估值上限的比例阈值，例如1.1表示股价高于当前财年估值上限10%"
                >
                    <InputNumber min={0.1} step={0.1} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item 
                    label="下财年估值中位数阈值" 
                    name="next_fy_median_threshold"
                    tooltip="股价超过下财年估值中位数的比例阈值，例如1.1表示股价高于下财年估值中位数10%"
                >
                    <InputNumber min={0.1} step={0.1} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item 
                    label="最大持仓股票数量" 
                    name="max_hold_stock_count"
                    tooltip="投资组合中最多持有的股票数量"
                >
                    <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item 
                    label="每只股票买入金额" 
                    name="max_hold_amount_per_stock"
                    tooltip="单只股票的最大投资金额（美元）"
                >
                    <InputNumber 
                        min={1000} 
                        step={1000} 
                        precision={0} 
                        style={{ width: '100%' }}
                        formatter={value => `$ ${value}`.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}
                        parser={value => value.replace(/\$\s?|(,*)/g, '')}
                    />
                </Form.Item>
                <Form.Item wrapperCol={{ offset: 8, span: 16 }}>
                    <Button type="primary" htmlType="submit">
                        更新
                    </Button>
                </Form.Item>
            </Form>
        </Card>
    );
};

export default EVCStrategy; 