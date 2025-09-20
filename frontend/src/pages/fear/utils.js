// 恐贪指数相关工具函数

// 时间范围选项
export const TIME_RANGES = [
  { label: '1年', value: 1 },
  { label: '3年', value: 3 },
  { label: '5年', value: 5 },
  { label: '10年', value: 10 },
  { label: '20年', value: 20 },
  { label: '全部', value: -1 }
];

// 获取恐贪指数颜色
export const getFearGreedColor = (value) => {
  if (value >= 75) return '#cf1322';  // 极度贪婪
  if (value >= 55) return '#fa8c16';  // 贪婪
  if (value >= 45) return '#d9d9d9';  // 中性
  if (value >= 25) return '#52c41a';  // 恐惧
  return '#237804';  // 极度恐惧
};

// 获取恐贪指数状态
export const getFearGreedStatus = (score) => {
  if (score >= 75) return '极度贪婪';
  if (score >= 55) return '贪婪';
  if (score >= 45) return '中性';
  if (score >= 25) return '恐惧';
  return '极度恐惧';
};

// 格式化季度
export const formatQuarter = (dateStr) => {
  const date = new Date(dateStr);
  const year = date.getFullYear();
  const month = date.getMonth() + 1;
  const quarter = Math.ceil(month / 3);
  return `${year}Q${quarter}`;
};

// 计算拟合曲线
export const fitExponentialCurve = (data) => {
  if (!data || data.length === 0) return { A: 0, B: 0 };
  
  // 将数据转换为对数形式进行线性拟合
  const n = data.length;
  let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
  const x = Array.from({length: n}, (_, i) => i);
  const y = data.map(v => Math.log(v));
  
  for (let i = 0; i < n; i++) {
    sumX += x[i];
    sumY += y[i];
    sumXY += x[i] * y[i];
    sumXX += x[i] * x[i];
  }
  
  // 计算线性回归系数
  const b = (n * sumXY - sumX * sumY) / (n * sumXX - sumX * sumX);
  const a = (sumY - b * sumX) / n;
  
  // 转换回指数形式
  const A = Math.exp(a);
  const B = Math.exp(b);
  
  return { A, B };
};

// 获取表格单元格颜色
export const getCellColor = (forwardTable) => {
  // 预处理每一列的最大最小概率
  const colProbMap = {};
  for (const dateStr of forwardTable.columns) {
    const probs = forwardTable.rows.map(row => parseFloat((row[dateStr] || '').replace('%', ''))).filter(v => !isNaN(v));
    if (probs.length === 0) continue;
    const max = Math.max(...probs);
    const min = Math.min(...probs);
    colProbMap[dateStr] = { max, min };
  }
  // 返回一个函数用于渲染
  return (row, dateStr) => {
    const val = parseFloat((row[dateStr] || '').replace('%', ''));
    if (isNaN(val)) return {};
    const { max, min } = colProbMap[dateStr] || {};
    if (val === max) return { background: '#003a8c', color: '#fff' };
    if (val === min) return { background: '#fff' };
    // 渐变色，最大深蓝，最小白色
    const percent = (val - min) / (max - min || 1);
    const blue = Math.round(255 - percent * 100);
    return { background: `rgb(${blue},${blue + 30},255)` };
  };
};
