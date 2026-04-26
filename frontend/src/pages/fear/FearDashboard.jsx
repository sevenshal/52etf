import React, { useState } from 'react';
import { Card, Tabs } from 'antd';
import { useFearGreedData } from './hooks/useFearGreedData';
import { useFedRateData } from './hooks/useFedRateData';
import { useBondData } from './hooks/useBondData';
import FearGreedCurrent from './components/FearGreedCurrent';
import FearGreedHistorical from './components/FearGreedHistorical';
import FearGreedYearlyAverage from './components/FearGreedYearlyAverage';
import FearGreedAiae from './components/FearGreedAiae';
import BondFearGreed from './components/BondFearGreed';
import SOXXFearGreed from './components/SOXXFearGreed';

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

  const loading = fearGreedLoading;

  if (loading) {
    return <div>加载中...</div>;
  }

  return (
    <>
      <Card
        title='标普500恐贪指数'
        style={{ marginBottom: 16 }}
      >
        <Tabs activeKey={activeTab} onChange={setActiveTab}>
          <Tabs.TabPane tab="当前指数" key="current">
            <FearGreedCurrent fearGreedData={fearGreedData} />
          </Tabs.TabPane>
          <Tabs.TabPane tab="历史走势" key="historical">
            <FearGreedHistorical />
          </Tabs.TabPane>
          <Tabs.TabPane tab="年度平均" key="yearly-average">
            <FearGreedYearlyAverage />
          </Tabs.TabPane>
          <Tabs.TabPane tab="AIAE" key="aiae">
            <FearGreedAiae />
          </Tabs.TabPane>
        </Tabs>
      </Card>

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

      <SOXXFearGreed />
    </>
  );
};

export default FearDashboard;
