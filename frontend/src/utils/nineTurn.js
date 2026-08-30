const toFiniteNumber = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

export const appendNineTurnAtr = (klines, atrWindow = 14) => {
  let highCount = 0;
  let lowCount = 0;
  let latestRisingClose = null;
  let latestRisingCount = null;
  const trueRanges = [];

  return (klines || []).map((item, index) => {
    const high = toFiniteNumber(item.high);
    const low = toFiniteNumber(item.low);
    const close = toFiniteNumber(item.close);
    const previousClose = index > 0 ? toFiniteNumber(klines[index - 1]?.close) : close;
    const closeLag4 = index >= 4 ? toFiniteNumber(klines[index - 4]?.close) : null;
    const trueRange = [
      high !== null && low !== null ? high - low : null,
      high !== null && previousClose !== null ? Math.abs(high - previousClose) : null,
      low !== null && previousClose !== null ? Math.abs(low - previousClose) : null,
    ].filter(Number.isFinite).reduce((maximum, value) => Math.max(maximum, value), 0);
    trueRanges.push(trueRange);

    highCount = close !== null && closeLag4 !== null && close > closeLag4 ? highCount + 1 : 0;
    lowCount = close !== null && closeLag4 !== null && close < closeLag4 ? lowCount + 1 : 0;
    if (highCount >= 2 && close !== null) {
      latestRisingClose = close;
      latestRisingCount = highCount;
    }

    const atr14 = index >= atrWindow - 1
      ? trueRanges.slice(index - atrWindow + 1, index + 1)
        .reduce((sum, value) => sum + value, 0) / atrWindow
      : null;

    return {
      ...item,
      atr14,
      highCount,
      lowCount,
      latestRisingClose,
      latestRisingCount,
      risingDrawdownPct: latestRisingClose !== null && close !== null && latestRisingClose !== 0
        ? (latestRisingClose - close) / latestRisingClose * 100
        : null,
      risingDrawdownAtr: latestRisingClose !== null && close !== null && atr14 > 0
        ? (latestRisingClose - close) / atr14
        : null,
    };
  });
};
