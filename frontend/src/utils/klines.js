const toNumber = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
};

const meanAndStd = (values) => {
  if (!values.length) return { mean: 0, std: 0 };
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const variance = values.reduce((sum, value) => sum + Math.pow(value - mean, 2), 0) / values.length;
  return { mean, std: Math.sqrt(variance) };
};

const log10Volume = (volume) => {
  const numericVolume = toNumber(volume);
  return numericVolume !== null && numericVolume > 0 ? Math.log10(numericVolume) : null;
};

const isValidProfileKline = (kline) => {
  const high = toNumber(kline?.high);
  const low = toNumber(kline?.low);
  const close = toNumber(kline?.close);
  const volume = toNumber(kline?.volume);
  return (
    high !== null
    && low !== null
    && close !== null
    && volume !== null
    && high > 0
    && low > 0
    && close > 0
    && volume > 0
    && high >= low
  );
};

const calculatePocWindow = (
  windowKlines,
  referencePrice,
  {
    binCount = 48,
    maxLevelsPerSide = 2,
    volumeStdDevMultiplier = 1,
  } = {}
) => {
  const validKlines = windowKlines.filter(isValidProfileKline);
  const currentPrice = toNumber(referencePrice);
  if (!validKlines.length || currentPrice === null || currentPrice <= 0) return null;

  const minPrice = Math.min(...validKlines.map(kline => Number(kline.low)));
  const maxPrice = Math.max(...validKlines.map(kline => Number(kline.high)));
  const priceRange = maxPrice - minPrice;
  if (priceRange <= 0) return null;

  const bins = Math.max(12, Math.min(120, Number(binCount) || 48));
  const levelsPerSide = Math.max(1, Math.min(2, Number(maxLevelsPerSide) || 2));
  const binSize = priceRange / bins;

  const profile = Array.from({ length: bins }, (_, index) => ({
    price: minPrice + (index + 0.5) * binSize,
    volume: 0,
    touch_count: 0,
    max_daily_volume: 0,
  }));

  const clampIndex = (index) => Math.max(0, Math.min(profile.length - 1, index));

  validKlines.forEach((kline) => {
    const high = Number(kline.high);
    const low = Number(kline.low);
    const close = Number(kline.close);
    const volume = Number(kline.volume);

    if (high <= low) {
      const index = clampIndex(Math.floor(((close || low) - minPrice) / binSize));
      profile[index].volume += volume;
      profile[index].touch_count += 1;
      profile[index].max_daily_volume = Math.max(profile[index].max_daily_volume, volume);
      return;
    }

    const startIndex = clampIndex(Math.floor((low - minPrice) / binSize));
    const endIndex = clampIndex(Math.floor((high - minPrice) / binSize));

    for (let index = startIndex; index <= endIndex; index += 1) {
      profile[index].volume += volume;
      profile[index].touch_count += 1;
      profile[index].max_daily_volume = Math.max(profile[index].max_daily_volume, volume);
    }
  });

  const profileVolumes = profile
    .map(item => item.volume)
    .filter(volume => volume > 0);
  const { mean: profileVolumeMean, std: profileVolumeStd } = meanAndStd(profileVolumes);
  const profileVolumeThreshold = profileVolumeMean + profileVolumeStd * Math.max(0, Number(volumeStdDevMultiplier) || 0);

  const highVolumeBins = [];
  profile.forEach((item) => {
    if (item.volume <= 0) return;
    if (item.volume <= profileVolumeThreshold) return;

    const dailyEquivalentVolume = item.volume / Math.max(1, item.touch_count);
    highVolumeBins.push({
      ...item,
      average_daily_volume: dailyEquivalentVolume,
      daily_equivalent_volume: dailyEquivalentVolume,
    });
  });

  const candidatesByPrice = new Map();
  highVolumeBins.forEach((item) => {
    candidatesByPrice.set(Number(item.price).toFixed(8), item);
  });
  const candidates = Array.from(candidatesByPrice.values())
    .sort((a, b) => b.volume - a.volume);
  if (!candidates.length) return null;

  const distanceToCurrentPrice = item => Math.abs(Number(item.price) - currentPrice);

  const pickStrongest = items => items.reduce((best, item) => {
    if (item.volume > best.volume) return item;
    if (item.volume < best.volume) return best;

    const itemDistance = distanceToCurrentPrice(item);
    const bestDistance = distanceToCurrentPrice(best);
    if (itemDistance < bestDistance) return item;
    if (itemDistance > bestDistance) return best;

    return item.daily_equivalent_volume > best.daily_equivalent_volume ? item : best;
  }, items[0]);

  const formatLevel = (item, side, roles) => {
    const price = Number(item.price);
    const dailyEquivalentVolume = Number(item.daily_equivalent_volume);
    const volumeZscore = profileVolumeStd > 0
      ? (item.volume - profileVolumeMean) / profileVolumeStd
      : null;
    return {
      rank: 0,
      side,
      roles,
      price: Number(price.toFixed(2)),
      volume: Number(item.volume.toFixed(2)),
      daily_equivalent_volume: Number(dailyEquivalentVolume.toFixed(2)),
      average_daily_volume: Number(item.average_daily_volume.toFixed(2)),
      max_daily_volume: Number(item.max_daily_volume.toFixed(2)),
      volume_threshold: Number(profileVolumeThreshold.toFixed(2)),
      volume_zscore: volumeZscore === null ? null : Number(volumeZscore.toFixed(2)),
      touch_count: item.touch_count,
      distance_pct: Number((Math.abs(price - currentPrice) / currentPrice * 100).toFixed(2)),
    };
  };

  const selectSideLevels = (items, side) => {
    if (!items.length) return [];

    const strongest = pickStrongest(items);

    const nearest = items.reduce((best, item) => {
      const itemDistance = distanceToCurrentPrice(item);
      const bestDistance = distanceToCurrentPrice(best);
      if (itemDistance < bestDistance) return item;
      if (itemDistance === bestDistance && item.volume > best.volume) return item;
      return best;
    }, items[0]);

    const selectedByPrice = new Map();
    [
      ['strongest', strongest],
      ['nearest', nearest],
    ].forEach(([role, item]) => {
      const priceKey = Number(item.price).toFixed(8);
      if (!selectedByPrice.has(priceKey)) {
        selectedByPrice.set(priceKey, formatLevel(item, side, [role]));
      } else {
        const level = selectedByPrice.get(priceKey);
        if (!level.roles.includes(role)) level.roles.push(role);
      }
    });

    const levels = Array.from(selectedByPrice.values()).slice(0, levelsPerSide);
    levels.sort((a, b) => (side === 'support' ? b.price - a.price : a.price - b.price));
    return levels.map((level, index) => ({
      ...level,
      rank: index + 1,
    }));
  };

  const supportCandidates = candidates.filter(item => Number(item.price) < currentPrice);
  const resistanceCandidates = candidates.filter(item => Number(item.price) > currentPrice);
  const supports = selectSideLevels(supportCandidates, 'support');
  const resistances = selectSideLevels(resistanceCandidates, 'resistance');
  const levels = [...supports, ...resistances];
  const pocLevel = formatLevel(pickStrongest(candidates), 'poc', ['poc']);

  return {
    poc: pocLevel.price,
    poc_level: pocLevel,
    levels,
    supports,
    resistances,
    profile_volume_mean: Number(profileVolumeMean.toFixed(2)),
    profile_volume_std: Number(profileVolumeStd.toFixed(2)),
    profile_volume_threshold: Number(profileVolumeThreshold.toFixed(2)),
  };
};

export const appendRollingPocSupportResistance = (
  klines,
  {
    window = 200,
    binCount = 48,
    maxLevelsPerSide = 2,
    minPeriods,
    volumeStdDevMultiplier = 1,
    outputStartIndex = 0,
  } = {}
) => {
  const lookback = Math.max(1, Number(window) || 1);
  const requiredPeriods = minPeriods === undefined ? Math.min(lookback, 20) : minPeriods;
  const startIndex = Math.max(0, Math.min(Number(outputStartIndex) || 0, klines.length));

  return klines.map((kline, index) => {
    if (index < startIndex) {
      return {
        ...kline,
        support_resistance: null,
      };
    }

    const windowStart = Math.max(0, index - lookback);
    const windowKlines = klines.slice(windowStart, index);
    const supportResistance = windowKlines.length < requiredPeriods
      ? null
      : calculatePocWindow(
        windowKlines,
        kline.close,
        {
          binCount,
          maxLevelsPerSide,
          volumeStdDevMultiplier,
        }
      );

    return {
      ...kline,
      support_resistance: supportResistance,
    };
  });
};

/**
 * 预处理K线数据，用过去N日log10(成交量)计算放量z-score，不包含当天。
 * @param {Array} klines - K线数据数组
 * @param {number} zScoreThreshold - log成交量z-score阈值，默认为1
 * @param {number} days - 回看交易日数量，默认为60
 * @returns {Array} 处理后的K线数据，每个元素包含volumeMA、volumeZScore、volumeMultiple、isVolumeSpike属性
 */
export const preprocessKlinesVolume = (klines, zScoreThreshold = 1, days = 60) => {
  const lookback = Math.max(1, Number(days) || 60);
  const threshold = Math.max(0, Number(zScoreThreshold) || 0);

  return klines.map((kline, index) => {
    const currentLogVolume = log10Volume(kline.volume);
    if (index < lookback || currentLogVolume === null) {
      return {
        ...kline,
        volumeMA: null,
        volumeStdDev: null,
        volumeArithmeticMA: null,
        logVolume: currentLogVolume,
        logVolumeMean: null,
        logVolumeStdDev: null,
        volumeZScore: null,
        volumeMultiple: null,
        isVolumeSpike: false
      };
    }

    const windowKlines = klines.slice(index - lookback, index);
    const windowVolumes = windowKlines
      .map(item => toNumber(item.volume))
      .filter(volume => volume !== null && volume > 0);
    const windowLogVolumes = windowKlines
      .map(item => log10Volume(item.volume))
      .filter(value => value !== null);

    if (windowLogVolumes.length < lookback) {
      return {
        ...kline,
        volumeMA: null,
        volumeStdDev: null,
        volumeArithmeticMA: null,
        logVolume: currentLogVolume,
        logVolumeMean: null,
        logVolumeStdDev: null,
        volumeZScore: null,
        volumeMultiple: null,
        isVolumeSpike: false
      };
    }

    const { mean: logMean, std: logStdDev } = meanAndStd(windowLogVolumes);
    const volumeZScore = logStdDev > 0 ? (currentLogVolume - logMean) / logStdDev : null;
    const logDiff = currentLogVolume - logMean;
    const volumeGeometricMA = Math.pow(10, logMean);
    const volumeMultiple = Math.pow(10, logDiff);
    const volumeArithmeticMA = windowVolumes.reduce((sum, value) => sum + value, 0) / windowVolumes.length;
    const isVolumeSpike = volumeZScore !== null && volumeZScore > threshold;

    return {
      ...kline,
      volumeMA: volumeGeometricMA,
      volumeStdDev: logStdDev,
      volumeArithmeticMA,
      logVolume: currentLogVolume,
      logVolumeMean: logMean,
      logVolumeStdDev: logStdDev,
      volumeZScore,
      volumeMultiple,
      isVolumeSpike
    };
  });
};
