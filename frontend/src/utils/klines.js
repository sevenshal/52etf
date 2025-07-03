
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
        const bodyMiddle = (bodyTop + bodyBottom) / 2;

        for(const bodyPrice of [bodyBottom, bodyMiddle, bodyTop]) {
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