import { useState, useEffect } from 'react';
import request from '../../utils/request';

// 联邦利率数据 hook
export const useFedRateData = () => {
  const [fedRateFrom, setFedRateFrom] = useState(null);
  const [fedRateTo, setFedRateTo] = useState(null);
  const [forwardMin, setForwardMin] = useState(null);
  const [forwardMax, setForwardMax] = useState(null);
  const [forwardTable, setForwardTable] = useState({ columns: [], rows: [] });

  // 获取当前利率from/to
  useEffect(() => {
    async function fetchFedRate() {
      try {
        const resp = await fetch('https://markets.newyorkfed.org/read?productCode=50&eventCodes=500&limit=1&startPosition=0&format=json');
        const data = await resp.json();
        if (data && data.refRates && data.refRates.length > 0) {
          setFedRateFrom(data.refRates[0].targetRateFrom);
          setFedRateTo(data.refRates[0].targetRateTo);
        }
      } catch (error) {
        console.error('获取联邦利率失败:', error);
      }
    }
    fetchFedRate();
  }, []);

  // 获取未来一年所有预测区间和表格数据
  useEffect(() => {
    async function fetchForward1y() {
      try {
        const resp = await request.get('/api/fed-rate/monitor');
        const result = resp.data;
        if (!result || result.status !== 'success' || !Array.isArray(result.data) || result.data.length === 0) return;
        
        const data = result.data;
        const now = new Date();
        const oneYearLater = new Date(now);
        oneYearLater.setFullYear(now.getFullYear() + 1);

        // 1. 收集所有日期（列头）
        const columns = [];
        const dateMap = {};
        for (const item of data) {
          item.date = item.date.replace(/年|月/g, '-').replace('日', '');
          columns.push(item.date);
          dateMap[item.date] = item;
        }

        // 2. 收集所有区间（行头，去重升序，所有rate_info都要）
        const rateSet = new Set();
        for (const item of data) {
          if (item.rate_info && item.rate_info.length > 0) {
            for (const rate of item.rate_info) {
              rateSet.add(rate.target_rate);
            }
          }
        }
        const rates = Array.from(rateSet).sort((a, b) => {
          const aLow = parseFloat(a.split('-')[0]);
          const bLow = parseFloat(b.split('-')[0]);
          return aLow - bLow;
        });

        // 3. 构建表格内容（所有区间都显示，去除全为0或空的行）
        const rows = rates.map(rate => {
          const row = { rate };
          let hasNonZero = false;
          for (const dateStr of columns) {
            const item = dateMap[dateStr];
            let prob = '';
            if (item && item.rate_info) {
              const found = item.rate_info.find(r => r.target_rate === rate);
              prob = found ? found.current_probability : '';
            }
            row[dateStr] = prob;
            // 判断是否有非0且非空概率
            if (prob && parseFloat(prob.replace('%', '')) > 1) {
              hasNonZero = true;
            }
          }
          return hasNonZero ? row : null;
        }).filter(Boolean);
        setForwardTable({ columns, rows });

        // 4. 计算贪恐区间（只用每个会议概率最高的区间）
        let allLowers = [], allUppers = [];
        for (const item of data) {
          if (new Date(item.date.replace(/年|月/g, '-').replace('日', '')) > oneYearLater) continue;
          if (item.rate_info && item.rate_info.length > 0) {
            let maxProb = -1, bestRate = null;
            for (const rate of item.rate_info) {
              const prob = parseFloat(rate.current_probability.replace('%', ''));
              if (prob > maxProb) {
                maxProb = prob;
                bestRate = rate.target_rate;
              }
            }
            if (bestRate) {
              const [low, up] = bestRate.split('-').map(s => parseFloat(s));
              allLowers.push(low);
              allUppers.push(up);
            }
          }
        }
        setForwardMin(allLowers.length > 0 ? Math.min(...allLowers) : null);
        setForwardMax(allUppers.length > 0 ? Math.max(...allUppers) : null);
      } catch (error) {
        console.error('获取联邦利率预测数据失败:', error);
      }
    }
    fetchForward1y();
  }, []);

  return {
    fedRateFrom,
    fedRateTo,
    forwardMin,
    forwardMax,
    forwardTable
  };
};
