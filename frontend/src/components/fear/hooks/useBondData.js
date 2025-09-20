import { useState, useEffect } from 'react';

// 美债数据 hook
export const useBondData = (fedRateFrom, fedRateTo, forwardMin, forwardMax) => {
  const [us10y, setUs10y] = useState(null);
  const [bondFearGreed, setBondFearGreed] = useState(null);

  // 实时获取10年期国债收益率
  useEffect(() => {
    const { US10YWS } = require('../../utils/us10yWS');
    const ws = new US10YWS({
      onYieldUpdate: (val) => setUs10y(val)
    });
    ws.connect();
    return () => ws.disconnect();
  }, []);

  // 计算美债贪恐值
  useEffect(() => {
    if (us10y && fedRateFrom !== null && fedRateTo !== null && forwardMin !== null && forwardMax !== null) {
      // 取所有下限和上限的最小值和最大值
      const minRate = Math.min(fedRateFrom, fedRateTo, forwardMin, forwardMax);
      const maxRate = Math.max(fedRateFrom, fedRateTo, forwardMin, forwardMax);
      let val = 100 * (maxRate - us10y) / (maxRate - minRate);
      if (us10y <= minRate) val = 100;
      if (us10y >= maxRate) val = 0;
      setBondFearGreed(Math.round(val));
    }
  }, [us10y, fedRateFrom, fedRateTo, forwardMin, forwardMax]);

  return {
    us10y,
    bondFearGreed
  };
};
