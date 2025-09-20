
export const calculateSupportResistanceValues = (klines, days, volumeRatio) => {
    const recentKlines = klines.slice(-days);
    const currentPrice = recentKlines[recentKlines.length - 1].close;

    let supports = [];
    let resistances = [];

    for (let i = 4; i < recentKlines.length; i++) {
      const kline = recentKlines[i];
      const avgVolume = recentKlines.slice(i - 4, i).reduce((sum, k) => sum + k.volume, 0) / 4;
      const isVolumeSpike = kline.volume > avgVolume * volumeRatio;
      const bodySize = Math.abs(kline.close - kline.open);
      const upperShadow = kline.high - Math.max(kline.close, kline.open);
      const lowerShadow = Math.min(kline.close, kline.open) - kline.low;

      if (isVolumeSpike && bodySize > Math.max(upperShadow, lowerShadow)) {
        const bodyTop = Math.max(kline.close, kline.open);
        const bodyBottom = Math.min(kline.close, kline.open);

        for(const bodyPrice of [bodyBottom, bodyTop]) {
          if (bodyPrice > currentPrice) {
            resistances.push(bodyPrice.toFixed(2))
          } else {
            supports.push(bodyPrice.toFixed(2))
          }
        }
      }
    }
    return { supports, resistances };
  };

/**
 * 预处理K线数据，计算成交量N日均线、标准差和放量判断
 * @param {Array} klines - K线数据数组
 * @param {number} stdDevMultiplier - 标准差倍数，默认为1
 * @returns {Array} 处理后的K线数据，每个元素包含volumeMA20、volumeStdDev、isVolumeSpike属性
 */
export const preprocessKlinesVolume = (klines, stdDevMultiplier = 1, days = 60) => {
  return klines.map((kline, index) => {
    if (index < (days-1)) {
      // 前N个数据点，无法计算均线
      return {
        ...kline,
        volumeMA: null,
        volumeStdDev: null,
        isVolumeSpike: false
      };
    }
    
    // 计算N日成交量均线和标准差
    const startIndex = index - (days-1);
    const volumes = klines.slice(startIndex, index + 1).map(k => k.volume);
    const mean = volumes.reduce((sum, v) => sum + v, 0) / days;
    const variance = volumes.reduce((sum, v) => sum + Math.pow(v - mean, 2), 0) / days;
    const stdDev = Math.sqrt(variance);
    
    // 判断是否放量（超过均线+n个标准差）
    const isVolumeSpike = kline.volume > mean + (stdDev * stdDevMultiplier);
    
    return {
      ...kline,
      volumeMA: mean,
      volumeStdDev: stdDev,
      isVolumeSpike: isVolumeSpike
    };
  });
};

/**
 * 使用新的放量判断标准计算支撑压力位
 * @param {Array} klines - 预处理后的K线数据
 * @param {number} days - 计算天数
 * @returns {Object} 包含支撑位和压力位的对象
 */
export const calculateSupportResistanceValuesNew = (klines, days) => {
  const recentKlines = klines.slice(-days);
  const currentPrice = recentKlines[recentKlines.length - 1].close;

  let supports = [];
  let resistances = [];

  for (let i = 0; i < recentKlines.length; i++) {
    const kline = recentKlines[i];
    
    // 使用预处理后的放量判断
    if (kline.isVolumeSpike) {
      const bodySize = Math.abs(kline.close - kline.open);
      const upperShadow = kline.high - Math.max(kline.close, kline.open);
      const lowerShadow = Math.min(kline.close, kline.open) - kline.low;

      if (bodySize > Math.max(upperShadow, lowerShadow)) {
        const bodyTop = Math.max(kline.close, kline.open);
        const bodyBottom = Math.min(kline.close, kline.open);

        for(const bodyPrice of [bodyBottom, bodyTop]) {
          if (bodyPrice > currentPrice) {
            resistances.push(bodyPrice.toFixed(2))
          } else {
            supports.push(bodyPrice.toFixed(2))
          }
        }
      }
    }
  }
  return { supports, resistances };
};
