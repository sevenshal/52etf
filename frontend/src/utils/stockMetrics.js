const DEFAULT_WINDOWS = [14, 20, 60, 120];

const toFiniteNumber = (value) => {
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
};

const sortKlines = (klines) => (
  [...(klines || [])]
    .map(item => ({
      ...item,
      timestamp: item.timestamp ? new Date(item.timestamp) : null,
      open: toFiniteNumber(item.open),
      high: toFiniteNumber(item.high),
      low: toFiniteNumber(item.low),
      close: toFiniteNumber(item.close),
      volume: toFiniteNumber(item.volume),
      turnover: toFiniteNumber(item.turnover),
    }))
    .filter(item => item.timestamp && item.close !== null)
    .sort((a, b) => a.timestamp - b.timestamp)
);

const mean = (values) => {
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
};

const sampleStdDev = (values) => {
  if (values.length < 2) return null;
  const avg = mean(values);
  if (avg === null) return null;
  const variance = values.reduce((sum, value) => sum + ((value - avg) ** 2), 0) / (values.length - 1);
  return Math.sqrt(variance);
};

const percentileRank = (values, current) => {
  const validValues = values.filter(value => Number.isFinite(value));
  if (!validValues.length || !Number.isFinite(current)) return null;

  let less = 0;
  let equal = 0;
  validValues.forEach(value => {
    if (value < current) {
      less += 1;
    } else if (value === current) {
      equal += 1;
    }
  });

  return ((less + (equal * 0.5)) / validValues.length) * 100;
};

const calculateReturns = (closes) => {
  const returns = [];
  for (let i = 1; i < closes.length; i += 1) {
    const prev = closes[i - 1];
    const current = closes[i];
    if (!(prev > 0) || !(current > 0)) continue;
    returns.push((current / prev) - 1);
  }
  return returns;
};

const calculateTrueRanges = (klines) => {
  const trueRanges = [];
  for (let i = 0; i < klines.length; i += 1) {
    const current = klines[i];
    const prevClose = i > 0 ? klines[i - 1].close : current.close;
    if (!(current.high > 0) || !(current.low > 0) || !(prevClose > 0)) {
      trueRanges.push(null);
      continue;
    }
    const range1 = current.high - current.low;
    const range2 = Math.abs(current.high - prevClose);
    const range3 = Math.abs(current.low - prevClose);
    trueRanges.push(Math.max(range1, range2, range3));
  }
  return trueRanges;
};

const calculateWilderAtrSeries = (trueRanges, window) => {
  const atrSeries = Array(trueRanges.length).fill(null);
  const validRanges = trueRanges.filter(value => Number.isFinite(value));
  if (validRanges.length < window) return atrSeries;

  const initialRanges = trueRanges.slice(0, window).filter(value => Number.isFinite(value));
  if (initialRanges.length < window) return atrSeries;

  let currentAtr = mean(initialRanges);
  atrSeries[window - 1] = currentAtr;

  for (let i = window; i < trueRanges.length; i += 1) {
    const tr = trueRanges[i];
    if (!Number.isFinite(tr)) {
      atrSeries[i] = null;
      continue;
    }
    currentAtr = ((currentAtr * (window - 1)) + tr) / window;
    atrSeries[i] = currentAtr;
  }

  return atrSeries;
};

const calculateDrawdownDepthSeries = (closes) => {
  const depthSeries = [];
  let runningPeak = null;

  closes.forEach(close => {
    if (!(close > 0)) {
      depthSeries.push(null);
      return;
    }

    runningPeak = runningPeak === null ? close : Math.max(runningPeak, close);
    depthSeries.push(runningPeak > 0 ? (1 - (close / runningPeak)) * 100 : null);
  });

  return depthSeries;
};

const calculateMomentumSnapshot = (closes) => {
  if (closes.length < 2) return null;
  if (closes.some(close => !(close > 0))) return null;

  const logPrices = closes.map(close => Math.log(close));
  const xMean = (closes.length - 1) / 2;
  const yMean = mean(logPrices);
  if (yMean === null) return null;

  let ssxx = 0;
  let ssxy = 0;
  let sst = 0;

  for (let i = 0; i < logPrices.length; i += 1) {
    const dx = i - xMean;
    const dy = logPrices[i] - yMean;
    ssxx += dx * dx;
    ssxy += dx * dy;
    sst += dy * dy;
  }

  if (ssxx <= 0) return null;

  const slope = ssxy / ssxx;
  const intercept = yMean - (slope * xMean);
  let ssRes = 0;

  for (let i = 0; i < logPrices.length; i += 1) {
    const fitted = (slope * i) + intercept;
    ssRes += (logPrices[i] - fitted) ** 2;
  }

  const rSquared = sst <= 0 ? 0 : Math.max(0, 1 - (ssRes / sst));
  const annualizedSlopePct = slope * 252 * 100;
  const rawScore = annualizedSlopePct * rSquared;
  const returns = calculateReturns(closes);
  const annualizedVolatilityPct = (() => {
    const stdDev = sampleStdDev(returns);
    return stdDev === null ? null : stdDev * Math.sqrt(252) * 100;
  })();
  const riskAdjustedScore = annualizedVolatilityPct && annualizedVolatilityPct > 0
    ? (rawScore / annualizedVolatilityPct) * 100
    : 0;

  return {
    annualizedSlopePct,
    rSquared,
    rawScore,
    annualizedVolatilityPct,
    riskAdjustedScore,
  };
};

const calculateRollingMomentumScores = (closes, window) => {
  const scores = Array(closes.length).fill(null);
  if (closes.length < window) return scores;

  for (let endIndex = window - 1; endIndex < closes.length; endIndex += 1) {
    const snapshot = calculateMomentumSnapshot(closes.slice(endIndex - window + 1, endIndex + 1));
    scores[endIndex] = snapshot ? snapshot.riskAdjustedScore : null;
  }

  return scores;
};

export const computeStockWindowMetrics = (klines, windows = DEFAULT_WINDOWS) => {
  const normalized = sortKlines(klines);
  const closes = normalized.map(item => item.close);
  const trueRanges = calculateTrueRanges(normalized);
  const latest = normalized.length > 0 ? normalized[normalized.length - 1] : null;
  const previousClose = normalized.length > 1 ? normalized[normalized.length - 2].close : null;

  const latestSnapshot = latest ? {
    date: latest.timestamp,
    close: latest.close,
    previousClose,
    changePct: previousClose > 0 ? ((latest.close / previousClose) - 1) * 100 : null,
    volume: latest.volume,
    turnover: latest.turnover,
    sampleSize: normalized.length,
  } : null;

  const rows = windows.map(window => {
    if (normalized.length < window) {
      return {
        window,
        annualizedVolatility: null,
        sharpeRatio: null,
        atr: null,
        atrp: null,
        drawdownPercentile: null,
        riskAdjustedMomentum: null,
        momentumPercentile: null,
      };
    }

    const windowCloses = closes.slice(-window);
    const windowReturns = calculateReturns(windowCloses);
    const returnStdDev = sampleStdDev(windowReturns);
    const annualizedVolatility = returnStdDev === null ? null : returnStdDev * Math.sqrt(252) * 100;
    const sharpeRatio = returnStdDev === null || returnStdDev === 0
      ? 0
      : (mean(windowReturns) / returnStdDev) * Math.sqrt(252);

    const atrSeries = calculateWilderAtrSeries(trueRanges, window);
    const currentAtr = atrSeries[atrSeries.length - 1];
    const atrp = Number.isFinite(currentAtr) && latest?.close > 0
      ? (currentAtr / latest.close) * 100
      : null;

    const drawdownDepthSeries = calculateDrawdownDepthSeries(windowCloses);
    const currentDrawdownDepth = drawdownDepthSeries[drawdownDepthSeries.length - 1];
    const drawdownPercentile = percentileRank(drawdownDepthSeries.slice(0, -1), currentDrawdownDepth);

    const momentumScores = calculateRollingMomentumScores(closes, window);
    const currentRiskAdjustedMomentum = momentumScores[momentumScores.length - 1];
    const momentumPercentile = percentileRank(momentumScores.slice(0, -1), currentRiskAdjustedMomentum);

    return {
      window,
      annualizedVolatility,
      sharpeRatio,
      atr: currentAtr,
      atrp,
      drawdownPercentile,
      riskAdjustedMomentum: currentRiskAdjustedMomentum,
      momentumPercentile,
    };
  });

  return {
    latest: latestSnapshot,
    rows,
  };
};

export const STOCK_METRIC_WINDOWS = DEFAULT_WINDOWS;
