// 显式 .js 扩展名：生成脚本在 Node 原生 ESM 下运行，不做扩展名补全
import { appendNineTurnAtr } from './nineTurn.js';
import { calculateMacd } from './macd.js';
import { appendRollingPocSupportResistance, preprocessKlinesVolume } from './klines.js';

/**
 * 前后端技术指标一致性夹具。
 *
 * 后端 `core/services/stock_system/indicators.py` 逐行移植了 K 线图上的九转/ATR、MACD、
 * 支撑压力和放量 z 值，用来全市场扫描和自动交易。页面上看到的信号必须和交易用的信号
 * 同一套算法，所以两边共用一份夹具：
 *
 * - 本文件用确定性的伪随机 K 线跑一遍前端算法，得到夹具内容；
 * - `frontend/scripts/generate-indicator-parity-fixture.mjs` 把它写到
 *   `backend/tests/fixtures/stock_indicator_parity.json`；
 * - 前端测试断言夹具 == 当前 JS 输出，后端测试断言夹具 == Python 输出。
 *
 * 改了任一边的算法，重新生成夹具后另一边的测试会失败，提醒同步移植。
 */

// 与 StockKlineChart.jsx 的默认值保持一致
export const PARITY_CHART_PARAMS = {
  supportResistanceWindow: 125,
  volumeStdDevMultiplier: 1,
  enableTurnoverDecay: true,
  volumeLookbackDays: 60,
};

// 32 位线性同余，保证每次生成的 K 线完全一样
const createRandom = (seed) => {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
};

const round = (value, digits) => Number(value.toFixed(digits));

export const buildParityKlines = (length = 240, seed = 20260912) => {
  const random = createRandom(seed);
  const klines = [];
  let close = 20;
  for (let index = 0; index < length; index += 1) {
    // 前段上涨、中段回落、后段震荡，让九转高/低计数、支撑和压力都出现
    const drift = index < 90 ? 0.004 : index < 160 ? -0.005 : 0.0005;
    const change = drift + (random() - 0.5) * 0.05;
    const open = close;
    close = Math.max(1, open * (1 + change));
    const high = Math.max(open, close) * (1 + random() * 0.02);
    const low = Math.min(open, close) * (1 - random() * 0.02);
    const day = new Date(Date.UTC(2025, 0, 1 + index));
    klines.push({
      timestamp: day.toISOString().slice(0, 10),
      open: round(open, 2),
      high: round(high, 2),
      low: round(low, 2),
      close: round(close, 2),
      volume: Math.round(1e6 * (0.5 + random() * 1.5) * (index % 37 === 0 ? 4 : 1)),
      // 换手率跨过 1：覆盖衰减率被夹到 1 的分支
      turnover_rate: round(0.2 + random() * 1.6, 3),
    });
  }
  // 边界：一根零成交(不计入成交量分布)、一根一字板(high == low)
  klines[150] = { ...klines[150], volume: 0 };
  klines[200] = { ...klines[200], high: klines[200].close, low: klines[200].close, open: klines[200].close };
  return klines;
};

const OUTPUT_FIELDS = [
  'support_resistance',
  'volumeMA',
  'volumeStdDev',
  'volumeArithmeticMA',
  'logVolume',
  'logVolumeMean',
  'logVolumeStdDev',
  'volumeZScore',
  'volumeMultiple',
  'isVolumeSpike',
  'atr14',
  'highCount',
  'lowCount',
  'latestRisingClose',
  'latestRisingCount',
  'risingDrawdownPct',
  'risingDrawdownAtr',
];

const pickOutputs = row => Object.fromEntries(OUTPUT_FIELDS.map(key => [key, row[key] ?? null]));

export const buildIndicatorParityFixture = () => {
  const klines = buildParityKlines();
  const {
    supportResistanceWindow, volumeStdDevMultiplier, enableTurnoverDecay, volumeLookbackDays,
  } = PARITY_CHART_PARAMS;

  // 与 StockKlineChart.jsx 同一顺序：支撑压力 → 放量 z 值 → 九转/ATR
  const chart = appendNineTurnAtr(
    preprocessKlinesVolume(
      appendRollingPocSupportResistance(klines, {
        window: supportResistanceWindow,
        binCount: 48,
        maxLevelsPerSide: 2,
        minPeriods: supportResistanceWindow,
        volumeStdDevMultiplier,
        enableTurnoverDecay,
      }),
      volumeStdDevMultiplier,
      volumeLookbackDays,
    ),
  );

  // 另一组参数：关闭换手衰减、默认 minPeriods，覆盖不同分支
  const supportResistanceNoDecay = appendRollingPocSupportResistance(klines, {
    window: 60,
    volumeStdDevMultiplier: 0.5,
    enableTurnoverDecay: false,
  }).map(row => row.support_resistance ?? null);

  return {
    description: '由 frontend/scripts/generate-indicator-parity-fixture.mjs 生成，请勿手改',
    params: PARITY_CHART_PARAMS,
    klines,
    chart: chart.map(pickOutputs),
    support_resistance_no_decay: supportResistanceNoDecay,
    macd: calculateMacd(klines),
    macd_custom: calculateMacd(klines, { fast: 5, slow: 20, signal: 7 }),
  };
};
