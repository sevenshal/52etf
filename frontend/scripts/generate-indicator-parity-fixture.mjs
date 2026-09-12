// 生成前后端技术指标一致性夹具：node frontend/scripts/generate-indicator-parity-fixture.mjs
// 前端算法改动后必须重新生成，并同步修改 backend/src/core/services/stock_system/indicators.py。
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { buildIndicatorParityFixture } from '../src/utils/indicatorParityFixture.js';

const here = dirname(fileURLToPath(import.meta.url));
const target = resolve(here, '../../backend/tests/fixtures/stock_indicator_parity.json');

writeFileSync(target, `${JSON.stringify(buildIndicatorParityFixture())}\n`);
console.log(`wrote ${target}`);
