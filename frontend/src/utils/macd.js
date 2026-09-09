const toNumber = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
};

export const DEFAULT_MACD_PARAMS = { fast: 12, slow: 26, signal: 9 };

const normalizePeriod = (value, fallback) => {
  const period = Math.floor(Number(value));
  return Number.isFinite(period) && period > 0 ? period : fallback;
};

/**
 * 递归 EMA：EMA_t = EMA_{t-1} + α × (x_t − EMA_{t-1})，α = 2/(N+1)，用首个有效值播种。
 *
 * 这是通达信/同花顺等 A 股行情软件的算法，和"先算 N 期 SMA 再递推"的教科书写法在
 * 序列前段会有差异；这里跟随行情软件，用户拿本图和券商软件对数时才不会对不上。
 */
const exponentialMovingAverage = (values, period) => {
  const alpha = 2 / (period + 1);
  let previous = null;
  return values.map((value) => {
    if (value === null) return null;
    previous = previous === null ? value : previous + alpha * (value - previous);
    return previous;
  });
};

/**
 * 每根 MACD 柱是在放大还是在收缩，决定画实心还是空心。
 *
 * 同号时比绝对值；**变号的那一根一律算放大**——柱子刚翻红/翻绿是新一轮动能的起点，
 * 按绝对值比会把它误判成"收缩"（比如从 −0.5 翻到 +0.1）画成空心，那是反的。
 */
export const histogramGrowing = (histogram) => {
  let previous = null;
  return (histogram || []).map((value) => {
    if (value === null) return null;
    const signChanged = previous === null || (value >= 0) !== (previous >= 0);
    const growing = signChanged ? true : Math.abs(value) >= Math.abs(previous);
    previous = value;
    return growing;
  });
};

/**
 * MACD（指数平滑异同移动平均线）。
 *
 * - DIF  = EMA(收盘, fast) − EMA(收盘, slow)
 * - DEA  = EMA(DIF, signal)，又叫 MACD 信号线
 * - MACD柱 = (DIF − DEA) × 2 —— **乘 2 是 A 股口径**，国际上通常不乘；两者形态一致，
 *   只是柱子高度差一倍。这里跟随 A 股行情软件，方便和券商软件对数。
 *
 * 前 `slow - 1` 根返回 null：EMA 用首个收盘价播种，warm-up 期的值带着明显的初始化偏差
 * （越靠前偏差越大），画出来是一段假的收敛过程，不如不画。被屏蔽的只是显示，递推本身
 * 从第一根开始，所以后面的取值和行情软件一致。
 */
export const calculateMacd = (klines, params = {}) => {
  const fast = normalizePeriod(params.fast, DEFAULT_MACD_PARAMS.fast);
  const slow = normalizePeriod(params.slow, DEFAULT_MACD_PARAMS.slow);
  const signal = normalizePeriod(params.signal, DEFAULT_MACD_PARAMS.signal);

  const closes = (klines || []).map(item => toNumber(item?.close));
  const fastEma = exponentialMovingAverage(closes, fast);
  const slowEma = exponentialMovingAverage(closes, slow);

  const rawDif = closes.map((_, index) => (
    fastEma[index] === null || slowEma[index] === null ? null : fastEma[index] - slowEma[index]
  ));
  const rawDea = exponentialMovingAverage(rawDif, signal);

  const warmUp = Math.max(slow, fast, signal) - 1;
  const dif = [];
  const dea = [];
  const histogram = [];
  rawDif.forEach((value, index) => {
    if (index < warmUp || value === null || rawDea[index] === null) {
      dif.push(null);
      dea.push(null);
      histogram.push(null);
      return;
    }
    dif.push(value);
    dea.push(rawDea[index]);
    histogram.push((value - rawDea[index]) * 2);
  });

  return { dif, dea, histogram, growing: histogramGrowing(histogram), params: { fast, slow, signal } };
};

export default calculateMacd;
