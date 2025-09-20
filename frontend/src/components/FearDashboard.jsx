import React, { useState } from 'react';
import { Card, Tabs } from 'antd';
import { useFearGreedData } from './fear/hooks/useFearGreedData';
import { useFedRateData } from './fear/hooks/useFedRateData';
import { useBondData } from './fear/hooks/useBondData';
import { useAutoTrading } from './fear/hooks/useAutoTrading';
import FearGreedCurrent from './fear/components/FearGreedCurrent';
import FearGreedHistorical from './fear/components/FearGreedHistorical';
import FearGreedPrediction from './fear/components/FearGreedPrediction';
import FearGreedAiae from './fear/components/FearGreedAiae';
import BondFearGreed from './fear/components/BondFearGreed';
import AutoTradingPanel from './fear/components/AutoTradingPanel';

const FearDashboard = () => {
  const [activeTab, setActiveTab] = useState('current');
  
  // 使用自定义 hooks 获取数据
  const { fearGreedData, loading: fearGreedLoading } = useFearGreedData();
  const { 
    fedRateFrom, 
    fedRateTo, 
    forwardMin, 
    forwardMax, 
    forwardTable 
  } = useFedRateData();
  const { us10y, bondFearGreed } = useBondData(fedRateFrom, fedRateTo, forwardMin, forwardMax);
  const { autoTrading, loading: autoTradingLoading, handleAutoTradingChange } = useAutoTrading();

  const loading = fearGreedLoading || autoTradingLoading;

  if (loading) {
    return <div>加载中...</div>;
  }

  return (
    <>
      {fearGreedData && (
        <Card title='标普500恐贪指数' style={{ marginBottom: 16 }}>
          <Tabs activeKey={activeTab} onChange={setActiveTab}>
            <Tabs.TabPane tab="当前指数" key="current">
              <FearGreedCurrent fearGreedData={fearGreedData} />
            </Tabs.TabPane>
            <Tabs.TabPane tab="历史走势" key="historical">
              <FearGreedHistorical />
            </Tabs.TabPane>
            <Tabs.TabPane tab="走势预测" key="prediction">
              <FearGreedPrediction />
            </Tabs.TabPane>
            <Tabs.TabPane tab="AIAE" key="aiae">
              <FearGreedAiae />
            </Tabs.TabPane>
          </Tabs>
        </Card>
      )}

      <Card title='美债贪恐指数及联邦概率预测' style={{ marginBottom: 16 }}>
        <BondFearGreed 
          bondFearGreed={bondFearGreed}
          us10y={us10y}
          fedRateFrom={fedRateFrom}
          fedRateTo={fedRateTo}
          forwardMin={forwardMin}
          forwardMax={forwardMax}
          forwardTable={forwardTable}
        />
      </Card>

      <Card title='守猪逮兔恐贪模型'>
        <AutoTradingPanel 
          autoTrading={autoTrading}
          onAutoTradingChange={handleAutoTradingChange}
        />
      </Card>
    </>
  );
};

export default FearDashboard;